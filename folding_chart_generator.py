"""
folding_chart_generator.py
Burris Numerical System — Stateless Geometric Universe Navigator.

Implements FoldingChartGenerator: a ChartGenerator subclass that eliminates
fold-log storage by computing the rolling reference plane (R) deterministically
from a Pythagorean triangle derived purely from the current V.

Rolling Pythagorean Wave:
    leg_a = scale_factor          (constant platform base, default 5000)
    leg_b = (V % mask_base) + 1   (dynamic, tracks V)
    R     = isqrt(leg_a² + leg_b²)

Both encoder and decoder call _roll_pythagorean_r() after every step, so R
stays in perfect sync with no log required.

FoldTriangle is kept for API schema compatibility but is never populated.
"""

import math
import json
from chart_generator import ChartGenerator, HandMath, fmt_short, fmt_large


# ---------------------------------------------------------------------------
# FoldTriangle  (schema-compat stub — never populated in rolling mode)
# ---------------------------------------------------------------------------

class FoldTriangle:
    """
    Retained for API/schema compatibility with legacy code that may
    inspect or serialise fold logs. In rolling mode this class is never
    instantiated; logs are always empty.
    """
    __slots__ = ("step", "leg_a", "leg_b", "hyp", "R_before", "R_after", "V_at_fold")

    def __init__(self, step, leg_a, leg_b, R_before, R_after, V_at_fold):
        self.step      = step
        self.leg_a     = leg_a
        self.leg_b     = leg_b
        self.hyp       = math.isqrt(leg_a * leg_a + leg_b * leg_b)
        self.R_before  = R_before
        self.R_after   = R_after
        self.V_at_fold = V_at_fold

    def to_dict(self):
        return {
            "step":      self.step,
            "leg_a":     str(self.leg_a),
            "leg_b":     str(self.leg_b),
            "hyp":       str(self.hyp),
            "R_before":  str(self.R_before),
            "R_after":   str(self.R_after),
            "V_at_fold": str(self.V_at_fold),
        }

    @staticmethod
    def from_dict(d):
        ft           = FoldTriangle.__new__(FoldTriangle)
        ft.step      = int(d["step"])
        ft.leg_a     = int(d["leg_a"])
        ft.leg_b     = int(d["leg_b"])
        ft.hyp       = int(d["hyp"])
        ft.R_before  = int(d["R_before"])
        ft.R_after   = int(d["R_after"])
        ft.V_at_fold = int(d["V_at_fold"])
        return ft


# ---------------------------------------------------------------------------
# FoldingChartGenerator
# ---------------------------------------------------------------------------

class FoldingChartGenerator(ChartGenerator):
    """
    Stateless rolling Pythagorean chart generator.

    R is never stored in a log — it is re-derived after every encode/decode
    step from a Pythagorean triangle whose legs are:
        leg_a = scale_factor            (constant, prevents R → 0)
        leg_b = (V % mask_base) + 1    (float-tracks V)
        R     = isqrt(leg_a² + leg_b²)

    Parameters
    ----------
    chart_base      : encoding base (256 for bytes)
    mask_base       : HandMath limb modulus
    num_digits      : initial limb count
    num_n_streams   : number of parallel universes (inherited)
    fold_threshold  : legacy param, kept for API compat — not used in rolling mode
    auto_fold_every : legacy param, kept for API compat — not used in rolling mode
    scale_factor    : Pythagorean leg_a; raise if R collapses at very large V
    """

    def __init__(
        self,
        chart_base:     int  = 256,
        mask_base:      int  = 1_000_000_000_000,
        num_digits:     int  = 100,
        num_n_streams:  int  = 12,
        fold_threshold: int  = 1000,        # kept for API compat
        auto_fold_every = None,             # kept for API compat
        scale_factor:   int  = 5000,
    ):
        super().__init__(chart_base, mask_base, num_digits, num_n_streams)
        self._fold_threshold  = fold_threshold   # unused internally, exposed for callers
        self._auto_fold_every = auto_fold_every  # unused internally
        self.scale_factor     = scale_factor

        # Triangle log kept permanently empty — rolling geometry replaces it
        self._triangle_log    = []
        self._decode_fold_ptr = 0

    # ── Pythagorean Rolling Core ───────────────────────────────────────────

    def _roll_pythagorean_r(self, u: int = 0):
        """
        Deterministically derive R from V using a Pythagorean triangle.
        Called after every encode step and after every decode step so both
        sides stay in perfect sync without any log.
        """
        hm    = self.hm
        V_int = hm.to_int(self.Vs[u])

        leg_a = self.scale_factor               # constant platform base
        leg_b = (V_int % hm.M) + 1             # dynamic, bounded by mask

        hypotenuse = math.isqrt(leg_a * leg_a + leg_b * leg_b)

        self.Rs[u] = hm.from_int(hypotenuse)

    # ── Encode / Decode steps (rolling) ───────────────────────────────────

    def _encode_step_rolling(self, byte_val: int, u: int = 0):
        """Encode one byte then re-roll R."""
        self._encode_step(byte_val, u)
        self._roll_pythagorean_r(u)

    def _decode_step_rolling(self, u: int = 0) -> int:
        """
        Decode one byte then re-roll R.

        Order matters: decode first (uses current R that matches what the
        encoder had before it rolled), then roll to advance the frame.
        """
        byte_val = self._decode_step(u)
        self._roll_pythagorean_r(u)
        return byte_val

    # ── Public zero-log interface ──────────────────────────────────────────

    def encode_bytes(self, data: bytes, u: int = 0) -> int:
        """
        Encode a block of raw bytes into a single coordinate integer.
        No fold log is produced or stored.

        Returns the final V as a plain Python int.
        """
        hm = self.hm

        # Digit-length guard: pre-size HandMath for the expected coordinate
        # A block of N bytes in base-256 can grow to ~N * log2(256)/log2(mask_base) limbs
        min_digits = max(self.num_digits, len(data) // 4 + 16)
        if hm.D < min_digits:
            hm.D = min_digits

        self.Vs[u]  = hm.from_int(1)
        self._roll_pythagorean_r(u)   # sync initial geometry
        self.step_count = 0

        for i in range(len(data) - 1, -1, -1):
            self._encode_step_rolling(data[i], u)

        return hm.to_int(self.Vs[u])

    def decode_bytes(
        self,
        V:       int,
        length:  int,
        log      = None,    # ignored — kept for API compat with log-based callers
        u:       int = 0,
        r_start  = None,    # ignored — R is always derived from V
    ) -> bytes:
        """
        Decode a coordinate integer back into `length` raw bytes.
        `log` and `r_start` are accepted but ignored; rolling geometry
        makes them unnecessary.
        """
        hm = self.hm

        # Digit-length guard
        needed = 0
        v_tmp  = V
        while v_tmp > 0:
            needed += 1
            v_tmp  //= hm.M
        if needed > hm.D:
            hm.D = needed + 16

        self.Vs[u]      = hm.from_int(V)
        self.step_count = 0

        # Establish the final-frame geometry before the first decode step
        self._roll_pythagorean_r(u)

        return bytes(self._decode_step_rolling(u) for _ in range(length))

    # ── Legacy compatibility stubs ─────────────────────────────────────────

    def export_fold_log(self) -> list:
        """Always returns [] — rolling mode produces no log."""
        return []

    def import_fold_log(self, data) -> None:
        """No-op — rolling mode ignores imported logs."""
        pass

    def print_fold_log(self, max_rows: int = 20) -> None:
        print("  Fold log: 0 events (Stateless Pythagorean Rolling Mode active)")

    def fold_summary(self, universe: int = 0) -> dict:
        hm    = self.hm
        V_int = hm.to_int(self.Vs[universe])
        R_int = hm.to_int(self.Rs[universe])
        return {
            "fold_count":     0,
            "fold_threshold": self._fold_threshold,
            "step_count":     self.step_count,
            "V":              V_int,
            "R":              R_int,
            "drift_x1000":    abs(V_int - R_int) * 1000 // R_int if R_int else 0,
            "triangles":      [],
        }


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os, tempfile

    print("=" * 64)
    print("  FoldingChartGenerator — Pythagorean Rolling Self-Tests")
    print("=" * 64)

    # ── Test 1: round-trip, small block ───────────────────────────────────
    print("\n[Test 1] Small block round-trip")
    fcg  = FoldingChartGenerator()
    data = b"Hello, OdinNet! BNS rolling wave test."
    V    = fcg.encode_bytes(data)
    fcg2 = FoldingChartGenerator()
    got  = fcg2.decode_bytes(V, len(data))
    assert got == data, f"MISMATCH\n  exp={data!r}\n  got={got!r}"
    print(f"  ✅ PASSED  ({len(data)} B, V={fmt_short(V)}, log size=0)")

    # ── Test 2: 512-byte sector (typical FoldingLatticeDrive sector) ──────
    print("\n[Test 2] 512-byte sector round-trip")
    sector = bytes(range(256)) * 2
    fcg3   = FoldingChartGenerator()
    V3     = fcg3.encode_bytes(sector)
    fcg4   = FoldingChartGenerator()
    got3   = fcg4.decode_bytes(V3, len(sector))
    assert got3 == sector, "MISMATCH on 512-byte sector"
    print(f"  ✅ PASSED  (512 B, V={fmt_short(V3)})")

    # ── Test 3: all-zero block ────────────────────────────────────────────
    print("\n[Test 3] All-zero block")
    zeros = bytes(512)
    fcg5  = FoldingChartGenerator()
    V5    = fcg5.encode_bytes(zeros)
    fcg6  = FoldingChartGenerator()
    got5  = fcg6.decode_bytes(V5, 512)
    assert got5 == zeros, "MISMATCH on zero block"
    print(f"  ✅ PASSED  (512 zeros, V={fmt_short(V5)})")

    # ── Test 4: export_fold_log always empty ──────────────────────────────
    print("\n[Test 4] export_fold_log returns []")
    assert fcg5.export_fold_log() == []
    print("  ✅ PASSED")

    # ── Test 5: digit-length guard ────────────────────────────────────────
    print("\n[Test 5] Digit-length guard (large block)")
    big   = bytes(i % 256 for i in range(1024))
    fcg7  = FoldingChartGenerator(num_digits=10)   # start tiny, guard must expand
    V7    = fcg7.encode_bytes(big)
    fcg8  = FoldingChartGenerator(num_digits=10)
    got7  = fcg8.decode_bytes(V7, len(big))
    assert got7 == big, "MISMATCH on large block with digit guard"
    print(f"  ✅ PASSED  (1024 B, V={fmt_short(V7)}, hm.D expanded to {fcg7.hm.D})")

    # ── Test 6: fold_summary has correct structure ────────────────────────
    print("\n[Test 6] fold_summary structure")
    summary = fcg7.fold_summary()
    assert summary["fold_count"] == 0
    assert summary["triangles"]  == []
    print(f"  ✅ PASSED  summary={summary}")

    print("\n✅  All FoldingChartGenerator tests passed.\n")
