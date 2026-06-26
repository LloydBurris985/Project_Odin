"""
folding_lattice_drive.py
Burris Numerical System — Stateless Folding Lattice Drive.

A LatticeDrive variant backed by the stateless Pythagorean Rolling
FoldingChartGenerator. Every sector stores only:

    byte_length  — actual data length before padding
    V_A          — coordinate after rolling-UP encode of sector data
    triangle_log — always [] (preserved for JSON schema compat)
    fold_count   — always 0

Round-trips are exact with zero log overhead because R is re-derived
deterministically from V after every encode/decode step.

Write path:  data ──rolling_encode(UP)──▶ V_A
Read path:   V_A  ──rolling_decode(UP)──▶ original data

Factory
-------
    from folding_lattice_drive import folding_lattice_drive
    fld = folding_lattice_drive(sector_size=512, n_sectors=64)
    fld.write(b"hello", 0)
    fld.read(0)           # exact round-trip
    fld.save("image.json")
    fld.load("image.json")
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
    Sector-addressable virtual block device backed by stateless rolling
    Pythagorean FoldingChartGenerator.

    Parameters
    ----------
    sector_size     : bytes per sector (default 512)
    n_sectors       : total sectors on the drive (default 64)
    chart_base      : encoding base, 256 for raw bytes
    mask_base       : HandMath limb modulus
    num_digits      : initial HandMath limb count
    fold_threshold  : passed to FoldingChartGenerator for API compat (not used internally)
    auto_fold_every : passed to FoldingChartGenerator for API compat (not used internally)
    rolling         : accepted for API compat; always treated as True
    scale_factor    : Pythagorean leg_a for rolling R derivation
    """

    _BORDER = "═" * 62

    def __init__(
        self,
        sector_size:     int  = 512,
        n_sectors:       int  = 64,
        chart_base:      int  = 256,
        mask_base:       int  = 1_000_000_000_000,
        num_digits:      int  = 100,
        fold_threshold:  int  = 1000,
        auto_fold_every       = None,
        rolling:         bool = True,   # always True; param kept for API compat
        scale_factor:    int  = 5000,
    ):
        self.sector_size     = sector_size
        self.n_sectors       = n_sectors
        self.chart_base      = chart_base
        self.mask_base       = mask_base
        self.num_digits      = num_digits
        self.fold_threshold  = fold_threshold
        self.auto_fold_every = auto_fold_every
        self.rolling         = True          # locked on
        self.scale_factor    = scale_factor

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
            scale_factor    = self.scale_factor,
        )

    @staticmethod
    def _empty_sector(n: int) -> dict:
        return {
            "sector":       n,
            "byte_length":  0,
            "V_A":          0,
            "triangle_log": [],   # always empty — schema compat only
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
        Encode data into a coordinate using stateless rolling Pythagorean encoding.
        Stores only V_A (no log, no R_final needed).
        """
        if sector_no is None:
            sector_no = self._head
        self._assert_sector(sector_no)

        if len(data) > self.sector_size:
            raise ValueError(
                f"Data ({len(data)} B) exceeds sector_size ({self.sector_size} B).")

        padded = data + bytes(self.sector_size - len(data))

        fcg = self._new_fcg()
        V_A = fcg.encode_bytes(padded, u=0)

        rec = {
            "sector":       sector_no,
            "byte_length":  len(data),
            "V_A":          V_A,
            "triangle_log": [],
            "fold_count":   0,
            "written":      True,
        }
        self._sectors[sector_no] = rec
        self._write_count       += 1
        self._head               = min(sector_no + 1, self.n_sectors - 1)

        print(f"[FLD WRITE] Sector {sector_no:4d}  {len(data):4d} B  "
              f"V_A={fmt_short(V_A)}  (Pythagorean Rolling)")
        return rec

    def read(self, sector_no: int = None) -> bytes:
        """
        Decode sector data from V_A using stateless rolling geometry.
        No log or R_final required.
        """
        if sector_no is None:
            sector_no = self._head
        self._assert_sector(sector_no)

        rec = self._sectors[sector_no]
        if not rec["written"]:
            print(f"[FLD READ] Sector {sector_no} empty — zero-fill.")
            self._head = min(sector_no + 1, self.n_sectors - 1)
            return bytes(self.sector_size)

        fcg = self._new_fcg()
        recovered_padded = fcg.decode_bytes(
            V      = rec["V_A"],
            length = self.sector_size,
        )
        recovered        = recovered_padded[: rec["byte_length"]]
        self._read_count += 1
        self._head        = min(sector_no + 1, self.n_sectors - 1)

        print(f"[FLD READ]  Sector {sector_no:4d}  {rec['byte_length']:4d} B  "
              f"V_A={fmt_short(rec['V_A'])}  (Pure Math Reversal)")
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

    def set_rolling(self, enabled: bool):
        """API-compat stub. Internal operations are locked to stateless rolling mode."""
        print("[FLD] Pythagorean rolling is permanently active — no change applied.")

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

        print(f"\n{self._BORDER}")
        print(f"  ⬡  STATELESS FOLDING LATTICE DRIVE — STATUS")
        print(f"{self._BORDER}")
        print(f"  Geometry   : {self.n_sectors} sectors × {self.sector_size} B/sector")
        print(f"  Capacity   : {total_mb:.3f} MB  ({self.n_sectors*self.sector_size:,} B)")
        print(f"  Used       : {used} sectors  ({used_mb:.3f} MB)")
        print(f"  Free       : {free} sectors")
        print(f"  Head pos   : sector {self._head}")
        print(f"  Writes     : {self._write_count}  |  Reads: {self._read_count}")
        print(f"  Rolling    : ON ✅ (Stateless Pythagorean — zero log overhead)")
        print(f"  Leg A (scale_factor): {self.scale_factor}")
        print(f"  Base       : {self.chart_base}  |  Digits: {self.num_digits}")
        print(f"{'-'*62}")
        print(f"  {'SEC':>4}  {'BYTES':>6}  {'V_A (short)':>16}  STS")
        print(f"  {'─'*4}  {'─'*6}  {'─'*16}  ───")
        for rec in self._sectors:
            if not rec["written"]:
                continue
            print(f"  {rec['sector']:>4}  {rec['byte_length']:>6}  "
                  f"{fmt_short(rec['V_A']):>16}  WR")
        print(f"{self._BORDER}\n")

    # ── Persistence ────────────────────────────────────────────────────────

    def save(self, path: str):
        """Serialize drive image to JSON. Triangle logs stay empty — clean footprint."""
        image = {
            "folding_lattice_drive_version": 3,
            "sector_size":     self.sector_size,
            "n_sectors":       self.n_sectors,
            "chart_base":      self.chart_base,
            "mask_base":       self.mask_base,
            "num_digits":      self.num_digits,
            "fold_threshold":  self.fold_threshold,
            "auto_fold_every": self.auto_fold_every,
            "rolling":         True,
            "scale_factor":    self.scale_factor,
            "head":            self._head,
            "write_count":     self._write_count,
            "read_count":      self._read_count,
            "sectors": [
                {
                    "sector":       s["sector"],
                    "byte_length":  s["byte_length"],
                    "V_A":          str(s["V_A"]),
                    "triangle_log": [],
                    "fold_count":   0,
                    "written":      s["written"],
                }
                for s in self._sectors
            ],
        }
        with open(path, "w") as f:
            json.dump(image, f, indent=2)
        print(f"[FLD] Image saved → {path}  (zero log overhead)")

    def load(self, path: str) -> "FoldingLatticeDrive":
        """Restore drive image from JSON."""
        with open(path) as f:
            image = json.load(f)
        self.sector_size     = image["sector_size"]
        self.n_sectors       = image["n_sectors"]
        self.chart_base      = image["chart_base"]
        self.mask_base       = image["mask_base"]
        self.num_digits      = image["num_digits"]
        self.fold_threshold  = image.get("fold_threshold",  1000)
        self.auto_fold_every = image.get("auto_fold_every", None)
        self.rolling         = True
        self.scale_factor    = image.get("scale_factor", 5000)
        self._head           = image["head"]
        self._write_count    = image["write_count"]
        self._read_count     = image["read_count"]
        self._sectors = [
            {
                "sector":       s["sector"],
                "byte_length":  s["byte_length"],
                "V_A":          int(s["V_A"]),
                "triangle_log": [],
                "fold_count":   0,
                "written":      s["written"],
            }
            for s in image["sectors"]
        ]
        print(f"[FLD] Image loaded ← {path}")
        return self

    # ── Dunder helpers ─────────────────────────────────────────────────────

    def __repr__(self) -> str:
        used = sum(1 for s in self._sectors if s["written"])
        return (f"<FoldingLatticeDrive sectors={self.n_sectors} "
                f"sector_size={self.sector_size}B used={used} "
                f"rolling=ON scale_factor={self.scale_factor}>")

    def __len__(self) -> int:
        return self.n_sectors * self.sector_size

    def __contains__(self, sector_no: int) -> bool:
        return (0 <= sector_no < self.n_sectors
                and self._sectors[sector_no]["written"])


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
    auto_fold_every       = None,
    rolling:         bool = True,
    scale_factor:    int  = 5000,
) -> FoldingLatticeDrive:
    """
    Create and initialise a stateless FoldingLatticeDrive.
    All parameters forwarded to FoldingLatticeDrive.__init__.
    """
    fld = FoldingLatticeDrive(
        sector_size     = sector_size,
        n_sectors       = n_sectors,
        chart_base      = chart_base,
        mask_base       = mask_base,
        num_digits      = num_digits,
        fold_threshold  = fold_threshold,
        auto_fold_every = auto_fold_every,
        rolling         = True,
        scale_factor    = scale_factor,
    )
    print(f"\n⬡  Stateless FoldingLatticeDrive initialised  —  "
          f"{n_sectors} × {sector_size}B sectors  "
          f"Pythagorean Rolling: ACTIVE  scale_factor={scale_factor}\n")
    return fld


# ===========================================================================
# SELF-TESTS
# ===========================================================================

if __name__ == "__main__":
    import tempfile

    print("=" * 64)
    print("  FoldingLatticeDrive — Stateless Self-Tests")
    print("=" * 64)

    # ── Test 1: Basic round-trip ───────────────────────────────────────────
    print("\n[Test 1] Basic write → read round-trip")
    fld = folding_lattice_drive(sector_size=64, n_sectors=8)
    msg = b"Hello, OdinNet!"
    fld.write(msg, 0)
    got = fld.read(0)
    assert got == msg, f"MISMATCH: got={got!r}"
    print(f"  ✅ PASSED  ({len(msg)} B, zero log)")

    # ── Test 2: Full 512-byte sector ──────────────────────────────────────
    print("\n[Test 2] Full-sector round-trip (512 B)")
    fld2  = folding_lattice_drive(sector_size=512, n_sectors=4)
    data2 = bytes(range(256)) * 2
    fld2.write(data2, 0)
    got2  = fld2.read(0)
    assert got2 == data2, "MISMATCH on 512-byte sector"
    print("  ✅ PASSED")

    # ── Test 3: Multi-sector file ─────────────────────────────────────────
    print("\n[Test 3] Multi-sector file round-trip")
    fld3  = folding_lattice_drive(sector_size=32, n_sectors=16)
    data3 = bytes(range(100))
    used  = fld3.write_file(data3, start_sector=0)
    raw   = fld3.read_file(0, len(used))
    got3  = raw[: len(data3)]
    assert got3 == data3, "MISMATCH on multi-sector file"
    print(f"  ✅ PASSED  ({len(data3)} B across sectors {used[0]}–{used[-1]})")

    # ── Test 4: save → load → read ────────────────────────────────────────
    print("\n[Test 4] Save / load round-trip")
    fld4  = folding_lattice_drive(sector_size=64, n_sectors=8)
    data4 = b"Persistence test - Stateless BNS Pythagorean FLD"
    fld4.write(data4, 2)
    with tempfile.TemporaryDirectory() as tmp:
        img = os.path.join(tmp, "fld_stateless.json")
        fld4.save(img)
        fld5 = FoldingLatticeDrive()
        fld5.load(img)
        got4 = fld5.read(2)
        assert got4 == data4, f"Post-load MISMATCH: got={got4!r}"
    print("  ✅ PASSED  (save/load with empty logs)")

    # ── Test 5: info() display ────────────────────────────────────────────
    print("\n[Test 5] info() display")
    fld4.info()
    print("  ✅ PASSED")

    # ── Test 6: set_rolling() stub ────────────────────────────────────────
    print("\n[Test 6] set_rolling() stub")
    fld4.set_rolling(False)
    fld4.set_rolling(True)
    print("  ✅ PASSED  (no crash, mode unchanged)")

    print("\n✅  All Stateless FoldingLatticeDrive tests passed.\n")
