"""
FoldingLatticeDrive
===================
A LatticeDrive variant that uses FoldingChartGenerator internally,
enabling fully reversible R-folding across encode/decode cycles.

Design contract:
  - Completely separate from the main LatticeDrive / LatticeFS.
  - Main LatticeDrive stays clean and carving-ready (untouched).
  - Use FoldingLatticeDrive for local compressed storage where long
    sequences cause coordinate drift and stability matters.
  - Every sector carries its own fold log so round-trips are exact.

Triangle rolling model (per Admiral Grok's spec):
  During encode: triangle rolls forward (leg_a grows, fold fires, R pins to V).
  During decode: triangle rolls backward (fold log replayed, R restored to
                 R_before before each matching decode step).
  The triangle_log stored per sector is the complete replay record.
  Rolling on: full log kept (default, 100% reversible).
  Rolling off: no log kept — fold events still fire but decode won't be
               reversible unless V+R+log are all stored. Use with care.

Factory:
    from folding_lattice_drive import folding_lattice_drive

    fld = folding_lattice_drive(sector_size=512, n_sectors=64, fold_threshold=500)
    fld.write(b"hello world", 0)
    data = fld.read(0)   # round-trip exact
    fld.save("fld_image.json")
    fld.load("fld_image.json")
"""

import json
import os

from chart_generator import fmt_short, fmt_large
from folding_chart_generator import FoldingChartGenerator


# ---------------------------------------------------------------------------
# FoldingLatticeDrive
# ---------------------------------------------------------------------------

class FoldingLatticeDrive:
    """
    Sector-addressable virtual block device backed by FoldingChartGenerator.

    Each sector stores:
        byte_length   — actual data length (before padding)
        V_A           — coordinate after folded-UP encode of sector data
        triangle_log  — fold event log needed for exact decode (list of dicts)
        rolling       — True if log is being kept (reversible mode)

    Write path:
        data  ──folded_encode(UP)──▶  V_A  +  triangle_log  (stored)

    Read path:
        V_A  ──folded_decode(UP, log=triangle_log)──▶  original data

    The triangle rolling works because FoldingChartGenerator._decode_step_folded()
    replays each FoldTriangle exactly at the step it was recorded, restoring
    R_before before the decode step and R_after afterward — mirroring the
    encode sequence precisely.
    """

    _BORDER = "═" * 62

    def __init__(
        self,
        sector_size:    int  = 512,
        n_sectors:      int  = 64,
        chart_base:     int  = 256,
        mask_base:      int  = 1_000_000_000_000,
        num_digits:     int  = 100,
        fold_threshold: int  = 1000,
        auto_fold_every: int | None = None,
        rolling:        bool = True,        # True = keep triangle log (reversible)
    ):
        self.sector_size     = sector_size
        self.n_sectors       = n_sectors
        self.chart_base      = chart_base
        self.mask_base       = mask_base
        self.num_digits      = num_digits
        self.fold_threshold  = fold_threshold
        self.auto_fold_every = auto_fold_every
        self.rolling         = rolling

        # When rolling=OFF, use sentinel -1 so _maybe_fold never fires.
        # This guarantees R stays at 1 throughout, making the log unnecessary.
        if not rolling:
            fold_threshold = -1
        self.fold_threshold = fold_threshold

        self._sectors:     list = [self._empty_sector(i) for i in range(n_sectors)]
        self._head:        int  = 0
        self._write_count: int  = 0
        self._read_count:  int  = 0

    # ── Internal helpers ───────────────────────────────────────────────────

    def _new_fcg(self) -> FoldingChartGenerator:
        return FoldingChartGenerator(
            chart_base      = self.chart_base,
            mask_base       = self.mask_base,
            num_digits      = self.num_digits,
            fold_threshold  = self.fold_threshold,
            auto_fold_every = self.auto_fold_every,
        )

    @staticmethod
    def _empty_sector(n: int) -> dict:
        return {
            "sector":       n,
            "byte_length":  0,
            "V_A":          0,
            "triangle_log": [],
            "fold_count":   0,
            "written":      False,
        }

    def _assert_sector(self, n: int):
        if not (0 <= n < self.n_sectors):
            raise IndexError(f"Sector {n} out of range [0, {self.n_sectors - 1}].")

    # ── Public block-device interface ──────────────────────────────────────

    def seek(self, sector_no: int) -> "FoldingLatticeDrive":
        self._assert_sector(sector_no)
        self._head = sector_no
        print(f"[FLD] Head → sector {sector_no}")
        return self

    def write(self, data: bytes, sector_no: int = None) -> dict:
        """
        Encode data into a coordinate using folded-UP encoding.
        Stores V_A + triangle_log for exact round-trip decode.
        """
        if sector_no is None:
            sector_no = self._head
        self._assert_sector(sector_no)

        if len(data) > self.sector_size:
            raise ValueError(
                f"Data ({len(data)} B) exceeds sector_size ({self.sector_size} B).")

        padded = data + bytes(self.sector_size - len(data))

        # Encode with rolling triangle
        fcg = self._new_fcg()
        V_A = fcg.encode_bytes(padded, u=0)
        R_final = fcg.hm.to_int(fcg.Rs[0])

        # Keep triangle log only in rolling mode
        log = fcg.export_fold_log() if self.rolling else []

        rec = {
            "sector":       sector_no,
            "byte_length":  len(data),
            "V_A":          V_A,
            "R_final":      R_final,
            "triangle_log": log,
            "fold_count":   len(log),
            "written":      True,
        }
        self._sectors[sector_no] = rec
        self._write_count       += 1
        self._head               = min(sector_no + 1, self.n_sectors - 1)

        print(f"[FLD WRITE] Sector {sector_no:4d}  {len(data):4d} B  "
              f"V_A={fmt_short(V_A)}  folds={len(log)}")
        return rec

    def read(self, sector_no: int = None) -> bytes:
        """
        Decode sector data from V_A using the stored triangle_log.
        Rolling mode: exact round-trip guaranteed.
        Non-rolling mode: only works if no folds occurred during encode.
        """
        if sector_no is None:
            sector_no = self._head
        self._assert_sector(sector_no)

        rec = self._sectors[sector_no]
        if not rec["written"]:
            print(f"[FLD READ] Sector {sector_no} empty — zero-fill.")
            self._head = min(sector_no + 1, self.n_sectors - 1)
            return bytes(self.sector_size)

        if not self.rolling and rec["fold_count"] > 0:
            print(f"  ⚠  Sector {sector_no} had {rec['fold_count']} fold(s) but "
                  f"rolling=False — decode may be incorrect.")

        fcg             = self._new_fcg()
        # In rolling=False mode pass R_final so decode starts with correct R
        r_start = rec.get("R_final", None) if not self.rolling else None
        recovered_padded = fcg.decode_bytes(
            V       = rec["V_A"],
            length  = self.sector_size,
            log     = rec["triangle_log"] if self.rolling else None,
            r_start = r_start,
        )
        recovered        = recovered_padded[: rec["byte_length"]]
        self._read_count += 1
        self._head        = min(sector_no + 1, self.n_sectors - 1)

        print(f"[FLD READ]  Sector {sector_no:4d}  {rec['byte_length']:4d} B  "
              f"V_A={fmt_short(rec['V_A'])}  folds={rec['fold_count']}")
        return recovered

    def write_file(self, data: bytes, start_sector: int = 0) -> list:
        """Write a multi-sector file starting at start_sector."""
        n_sec = (len(data) + self.sector_size - 1) // self.sector_size
        end   = start_sector + n_sec - 1
        if end >= self.n_sectors:
            raise IndexError(
                f"File needs sectors {start_sector}–{end}, "
                f"drive has {self.n_sectors}.")
        used, offset = [], 0
        for i in range(n_sec):
            chunk = data[offset : offset + self.sector_size]
            self.write(chunk, start_sector + i)
            used.append(start_sector + i)
            offset += self.sector_size
        print(f"[FLD WRITE-FILE] {len(data)} B → sectors {used[0]}–{used[-1]}")
        return used

    def read_file(self, start_sector: int, n_sectors: int) -> bytes:
        """Read n_sectors consecutive sectors and concatenate."""
        result = bytearray()
        for i in range(n_sectors):
            result += self.read(start_sector + i)
        return bytes(result)

    def format(self, n_sectors: int = None) -> "FoldingLatticeDrive":
        if n_sectors is not None:
            self.n_sectors = n_sectors
        self._sectors     = [self._empty_sector(i) for i in range(self.n_sectors)]
        self._head        = 0
        self._write_count = 0
        self._read_count  = 0
        print(f"[FLD FORMAT] {self.n_sectors} sectors × {self.sector_size} B/sector")
        return self

    def __repr__(self) -> str:
        used = sum(1 for s in self._sectors if s["written"])
        return (f"<FoldingLatticeDrive sectors={self.n_sectors} "
                f"sector_size={self.sector_size}B used={used} "
                f"fold_threshold={self.fold_threshold} rolling={self.rolling}>")

    def __len__(self) -> int:
        return self.n_sectors * self.sector_size

    def __contains__(self, sector_no: int) -> bool:
        return (0 <= sector_no < self.n_sectors
                and self._sectors[sector_no]["written"])

    # ── Rolling mode toggle ────────────────────────────────────────────────

    def set_rolling(self, enabled: bool):
        """
        Enable or disable triangle log keeping.

        rolling=True  (default) — triangle log stored per sector, decode is
                                   always exact regardless of fold count.
        rolling=False           — no log stored, smaller sector records,
                                   but decode is only reversible when no
                                   folds occurred during encode.
        """
        self.rolling = enabled
        mode = "ON (reversible)" if enabled else "OFF (log-free, care needed)"
        print(f"[FLD] Triangle rolling: {mode}")

    # ── Diagnostics ────────────────────────────────────────────────────────

    def hex_dump(self, sector_no: int, cols: int = 16):
        data = self.read(sector_no)
        print(f"\n  HEX DUMP — Sector {sector_no}  ({len(data)} B)")
        print(f"  {'─'*58}")
        for offset in range(0, len(data), cols):
            chunk      = data[offset : offset + cols]
            hex_part   = " ".join(f"{b:02x}" for b in chunk)
            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            print(f"  {offset:04x}:  {hex_part:<{cols*3}}  |{ascii_part}|")
        print(f"  {'─'*58}\n")

    def info(self):
        used     = sum(1 for s in self._sectors if s["written"])
        free     = self.n_sectors - used
        total_mb = (self.n_sectors * self.sector_size) / 1_048_576
        used_mb  = (used * self.sector_size) / 1_048_576
        total_folds = sum(s.get("fold_count", 0) for s in self._sectors)

        print(f"\n{self._BORDER}")
        print(f"  ⬡  FOLDING LATTICE DRIVE — STATUS")
        print(f"{self._BORDER}")
        print(f"  Geometry     : {self.n_sectors} sectors × {self.sector_size} B/sector")
        print(f"  Capacity     : {total_mb:.3f} MB  ({self.n_sectors*self.sector_size:,} B)")
        print(f"  Used         : {used} sectors  ({used_mb:.3f} MB)")
        print(f"  Free         : {free} sectors")
        print(f"  Head pos     : sector {self._head}")
        print(f"  Writes       : {self._write_count}  |  Reads: {self._read_count}")
        print(f"  Fold threshold: {self.fold_threshold}  |  Auto-fold: {self.auto_fold_every}")
        print(f"  Rolling      : {'ON ✅ (reversible)' if self.rolling else 'OFF ⚠ (log-free)'}")
        print(f"  Total folds  : {total_folds} across all written sectors")
        print(f"  Base         : {self.chart_base}  |  Digits: {self.num_digits}")
        print(f"{'-'*62}")
        print(f"  {'SEC':>4}  {'BYTES':>6}  {'V_A (short)':>16}  {'FOLDS':>6}  STS")
        print(f"  {'─'*4}  {'─'*6}  {'─'*16}  {'─'*6}  ───")
        for rec in self._sectors:
            if not rec["written"]:
                continue
            print(f"  {rec['sector']:>4}  {rec['byte_length']:>6}  "
                  f"{fmt_short(rec['V_A']):>16}  {rec['fold_count']:>6}  WR")
        print(f"{self._BORDER}\n")

    # ── Persistence ────────────────────────────────────────────────────────

    def save(self, path: str):
        """Serialize the full drive image (including triangle logs) to JSON."""
        image = {
            "folding_lattice_drive_version": 1,
            "sector_size":    self.sector_size,
            "n_sectors":      self.n_sectors,
            "chart_base":     self.chart_base,
            "mask_base":      self.mask_base,
            "num_digits":     self.num_digits,
            "fold_threshold": self.fold_threshold,
            "auto_fold_every": self.auto_fold_every,
            "rolling":        self.rolling,
            "head":           self._head,
            "write_count":    self._write_count,
            "read_count":     self._read_count,
            "sectors": [
                {
                    "sector":       s["sector"],
                    "byte_length":  s["byte_length"],
                    "V_A":          str(s["V_A"]),
                    "triangle_log": s["triangle_log"],
                    "fold_count":   s["fold_count"],
                    "written":      s["written"],
                }
                for s in self._sectors
            ],
        }
        with open(path, "w") as f:
            json.dump(image, f, indent=2)
        total_folds = sum(s.get("fold_count", 0) for s in self._sectors)
        print(f"[FLD] Image saved → {path}  (total folds stored: {total_folds})")

    def load(self, path: str) -> "FoldingLatticeDrive":
        """Restore drive image from JSON, including all triangle logs."""
        with open(path) as f:
            image = json.load(f)
        self.sector_size     = image["sector_size"]
        self.n_sectors       = image["n_sectors"]
        self.chart_base      = image["chart_base"]
        self.mask_base       = image["mask_base"]
        self.num_digits      = image["num_digits"]
        self.fold_threshold  = image.get("fold_threshold",  1000)
        self.auto_fold_every = image.get("auto_fold_every", None)
        self.rolling         = image.get("rolling",         True)
        self._head           = image["head"]
        self._write_count    = image["write_count"]
        self._read_count     = image["read_count"]
        self._sectors = [
            {
                "sector":       s["sector"],
                "byte_length":  s["byte_length"],
                "V_A":          int(s["V_A"]),
                "triangle_log": s["triangle_log"],
                "fold_count":   s.get("fold_count", len(s["triangle_log"])),
                "written":      s["written"],
            }
            for s in image["sectors"]
        ]
        total_folds = sum(s.get("fold_count", 0) for s in self._sectors)
        print(f"[FLD] Image loaded ← {path}  (total folds: {total_folds})")
        return self


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def folding_lattice_drive(
    sector_size:     int  = 512,
    n_sectors:       int  = 64,
    chart_base:      int  = 256,
    mask_base:       int  = 1_000_000_000_000,
    num_digits:      int  = 100,
    fold_threshold:  int  = 1000,
    auto_fold_every: int | None = None,
    rolling:         bool = True,
) -> "FoldingLatticeDrive":
    """
    Create and initialise a FoldingLatticeDrive.

    Args:
        sector_size     : bytes per sector (default 512)
        n_sectors       : total sectors on the drive (default 64)
        fold_threshold  : drift ×1000 before R folds (default 1000)
        auto_fold_every : also fold every N encode steps (None = drift-only)
        rolling         : True = keep triangle log (reversible, default)
                          False = log-free mode (use only if no folds expected)
    """
    fld = FoldingLatticeDrive(
        sector_size     = sector_size,
        n_sectors       = n_sectors,
        chart_base      = chart_base,
        mask_base       = mask_base,
        num_digits      = num_digits,
        fold_threshold  = fold_threshold,
        auto_fold_every = auto_fold_every,
        rolling         = rolling,
    )
    rolling_str = "rolling=ON ✅" if rolling else "rolling=OFF ⚠"
    print(f"\n⬡  FoldingLatticeDrive initialised  —  "
          f"{n_sectors} × {sector_size}B sectors  "
          f"fold_threshold={fold_threshold}  {rolling_str}\n")
    return fld


# ===========================================================================
# SELF-TESTS
# ===========================================================================

if __name__ == "__main__":
    import tempfile

    print("=" * 64)
    print("  FoldingLatticeDrive — Self-Tests")
    print("=" * 64)

    # ── Test 1: Basic round-trip, small data, default threshold ───────────
    print("\n[Test 1] Basic write → read round-trip (default threshold)")
    fld  = folding_lattice_drive(sector_size=64, n_sectors=8)
    msg  = b"Hello, OdinNet!"
    fld.write(msg, 0)
    got  = fld.read(0)
    assert got == msg, f"MISMATCH: got={got!r}"
    print(f"  ✅ PASSED  ({len(msg)} B, folds={fld._sectors[0]['fold_count']})")

    # ── Test 2: Aggressive folding (threshold=0, forces folds every step) ─
    print("\n[Test 2] Aggressive folding (threshold=0)")
    fld2 = folding_lattice_drive(sector_size=64, n_sectors=4, fold_threshold=0)
    data2 = bytes(range(40))
    fld2.write(data2, 0)
    got2  = fld2.read(0)
    folds = fld2._sectors[0]["fold_count"]
    assert got2 == data2, f"MISMATCH: got={list(got2)}"
    print(f"  ✅ PASSED  ({len(data2)} B, folds={folds})")

    # ── Test 3: Multi-sector file write/read ──────────────────────────────
    print("\n[Test 3] Multi-sector file round-trip")
    fld3  = folding_lattice_drive(sector_size=32, n_sectors=16, fold_threshold=500)
    data3 = bytes(range(100))
    used  = fld3.write_file(data3, start_sector=0)
    raw   = fld3.read_file(0, len(used))
    got3  = raw[: len(data3)]
    assert got3 == data3, f"MISMATCH"
    print(f"  ✅ PASSED  ({len(data3)} B across sectors {used[0]}–{used[-1]})")

    # ── Test 4: rolling=OFF then ON comparison ────────────────────────────
    print("\n[Test 4] rolling=OFF (no log) — only works when no folds fire")
    fld4 = folding_lattice_drive(
        sector_size=64, n_sectors=4,
        fold_threshold=10_000_000,  # very high → no folds will fire
        rolling=False,
    )
    msg4 = b"No folds expected here"
    fld4.write(msg4, 0)
    got4 = fld4.read(0)
    assert got4 == msg4, f"MISMATCH: got={got4!r}"
    print(f"  ✅ PASSED  rolling=OFF, folds={fld4._sectors[0]['fold_count']}")

    # ── Test 5: save → load → read ────────────────────────────────────────
    print("\n[Test 5] Save / load round-trip (with folds)")
    fld5  = folding_lattice_drive(sector_size=64, n_sectors=8, fold_threshold=0)
    data5 = b"Persistence test - Burris FLD"
    fld5.write(data5, 2)

    with tempfile.TemporaryDirectory() as tmp:
        img = os.path.join(tmp, "fld_image.json")
        fld5.save(img)

        fld6 = FoldingLatticeDrive()
        fld6.load(img)
        got5 = fld6.read(2)
        assert got5 == data5, f"Post-load MISMATCH: got={got5!r}"
    print(f"  ✅ PASSED  save/load + decode with triangle log")

    # ── Test 6: set_rolling() toggle ──────────────────────────────────────
    print("\n[Test 6] set_rolling() toggle")
    fld7  = folding_lattice_drive(sector_size=64, n_sectors=4)
    fld7.set_rolling(False)
    fld7.set_rolling(True)
    msg7 = b"Toggle test"
    fld7.write(msg7, 0)
    got7 = fld7.read(0)
    assert got7 == msg7
    print("  ✅ PASSED  rolling toggle works, data intact")

    # ── Test 7: info() doesn't crash ──────────────────────────────────────
    print("\n[Test 7] info() display")
    fld5.info()
    print("  ✅ PASSED")

    print("\n✅  All FoldingLatticeDrive tests passed.\n")
