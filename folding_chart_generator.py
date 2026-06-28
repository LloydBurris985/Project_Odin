"""
folding_chart_generator.py
Burris Numerical System — Stateless Geometric Universe Navigator.

FoldingChartGenerator: a ChartGenerator subclass with stateless encode/decode.

Encode/Decode use R=1 (the mathematically correct stateless baseline).
The Pythagorean rolling formula is available for navigation display (galactic
map, drift metrics) but is NOT used in encode/decode paths — doing so would
make R depend on V-after-encoding, making decode impossible without a log.

Round-trip correctness is guaranteed:
    V_new = V + (V - R) * (BASE - 1) + byte   [R=1, so (V-1)*(BASE-1)+byte]
    num   = V + R * (BASE - 1)                 [R=1]
    V_old = num // BASE
    byte  = num %  BASE

FoldTriangle is kept for API schema compatibility but is never populated.
"""

import math
import json
from chart_generator import ChartGenerator, HandMath, fmt_short, fmt_large


# ---------------------------------------------------------------------------
# FoldTriangle  (schema-compat stub)
# ---------------------------------------------------------------------------

class FoldTriangle:
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
    Stateless chart generator with correct round-trip encode/decode.

    Encode/decode always use R=1 (mathematically proven correct baseline).
    The Pythagorean rolling formula computes a display-only R for navigation
    metrics (galactic map, drift) — it is never used in encode/decode.

    Why R=1 and not rolling R:
        encode: V_new = V + (V-R)*(BASE-1) + byte  requires R constant to invert
        decode: num = V + R*(BASE-1); V_old=num//BASE; byte=num%BASE
        If R=roll(V) changes per step, decode cannot recover the same R used
        during encoding without a stored log — defeating statelessness.
        R=1 is the unique constant that makes the system stateless AND correct.

    Parameters
    ----------
    chart_base      : encoding base (256 for bytes)
    mask_base       : HandMath limb modulus
    num_digits      : initial limb count
    num_n_streams   : number of parallel universes (inherited)
    fold_threshold  : legacy param, kept for API compat
    auto_fold_every : legacy param, kept for API compat
    scale_factor    : Pythagorean leg_a for display-only R derivation
    """

    def __init__(
        self,
        chart_base:     int  = 256,
        mask_base:      int  = 1_000_000_000_000,
        num_digits:     int  = 100,
        num_n_streams:  int  = 12,
        fold_threshold: int  = 1000,
        auto_fold_every      = None,
        scale_factor:   int  = 5000,
    ):
        super().__init__(chart_base, mask_base, num_digits, num_n_streams)
        self._fold_threshold  = fold_threshold
        self._auto_fold_every = auto_fold_every
        self.scale_factor     = scale_factor
        self._triangle_log    = []
        self._decode_fold_ptr = 0

    # ── Pythagorean display-only R  ────────────────────────────────────────

    def _pythagorean_r_display(self, u: int = 0) -> int:
        """
        Compute display-only R from current V using Pythagorean triangle.
        Used only for galactic map / drift metrics. NOT used in encode/decode.
        """
        hm    = self.hm
        V_int = hm.to_int(self.Vs[u])
        leg_a = self.scale_factor
        leg_b = (V_int % hm.M) + 1
        return math.isqrt(leg_a * leg_a + leg_b * leg_b)

    # kept for API compat — no-op in stateless mode
    def _roll_pythagorean_r(self, u: int = 0):
        pass

    # ── Public stateless interface  ────────────────────────────────────────

    def encode_bytes(self, data: bytes, u: int = 0) -> int:
        """
        Encode raw bytes → coordinate integer using R=1 (stateless, correct).
        No fold log produced. Returns final V as a plain Python int.
        """
        hm = self.hm

        min_digits = max(self.num_digits, len(data) // 4 + 16)
        if hm.D < min_digits:
            hm.D = min_digits

        self.Vs[u] = hm.from_int(1)
        self.Rs[u] = hm.from_int(1)   # R=1, constant
        self.step_count = 0

        for i in range(len(data) - 1, -1, -1):
            self._encode_step(data[i], u)   # inherited from ChartGenerator

        return hm.to_int(self.Vs[u])

    def decode_bytes(
        self,
        V:       int,
        length:  int,
        log      = None,   # ignored — stateless
        u:       int = 0,
        r_start  = None,   # ignored — R=1 always
    ) -> bytes:
        """
        Decode coordinate integer → raw bytes using R=1 (stateless, correct).
        `log` and `r_start` accepted for API compat but ignored.
        """
        hm = self.hm

        needed = 0
        v_tmp  = V
        while v_tmp > 0:
            needed += 1
            v_tmp  //= hm.M
        if needed > hm.D:
            hm.D = needed + 16

        self.Vs[u]      = hm.from_int(V)
        self.Rs[u]      = hm.from_int(1)   # R=1, constant
        self.step_count = 0

        return bytes(self._decode_step(u) for _ in range(length))

    # ── Legacy compatibility stubs ─────────────────────────────────────────

    def export_fold_log(self) -> list:
        return []

    def import_fold_log(self, data) -> None:
        pass

    def print_fold_log(self, max_rows: int = 20) -> None:
        print("  Fold log: 0 events (Stateless mode — R=1 constant)")

    def fold_summary(self, universe: int = 0) -> dict:
        hm    = self.hm
        V_int = hm.to_int(self.Vs[universe])
        R_disp = self._pythagorean_r_display(universe)
        return {
            "fold_count":     0,
            "fold_threshold": self._fold_threshold,
            "step_count":     self.step_count,
            "V":              V_int,
            "R":              R_disp,
            "drift_x1000":    abs(V_int - R_disp) * 1000 // R_disp if R_disp else 0,
            "triangles":      [],
        }


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os, tempfile

    print("=" * 64)
    print("  FoldingChartGenerator — Stateless Self-Tests")
    print("=" * 64)

    # Test 1: small block round-trip
    print("\n[Test 1] Small block round-trip")
    fcg  = FoldingChartGenerator()
    data = b"Hello, OdinNet! BNS rolling wave test."
    V    = fcg.encode_bytes(data)
    fcg2 = FoldingChartGenerator()
    got  = fcg2.decode_bytes(V, len(data))
    assert got == data, f"MISMATCH\n  exp={data!r}\n  got={got!r}"
    print(f"  ✅ PASSED  ({len(data)} B, V={fmt_short(V)})")

    # Test 2: 512-byte sector
    print("\n[Test 2] 512-byte sector round-trip")
    sector = bytes(range(256)) * 2
    fcg3   = FoldingChartGenerator()
    V3     = fcg3.encode_bytes(sector)
    fcg4   = FoldingChartGenerator()
    got3   = fcg4.decode_bytes(V3, len(sector))
    assert got3 == sector, "MISMATCH on 512-byte sector"
    print(f"  ✅ PASSED  (512 B, V={fmt_short(V3)})")

    # Test 3: all-zero block
    print("\n[Test 3] All-zero block")
    zeros = bytes(512)
    fcg5  = FoldingChartGenerator()
    V5    = fcg5.encode_bytes(zeros)
    fcg6  = FoldingChartGenerator()
    got5  = fcg6.decode_bytes(V5, 512)
    assert got5 == zeros, "MISMATCH on zero block"
    print(f"  ✅ PASSED  (512 zeros, V={fmt_short(V5)})")

    # Test 4: export_fold_log always empty
    print("\n[Test 4] export_fold_log returns []")
    assert fcg5.export_fold_log() == []
    print("  ✅ PASSED")

    # Test 5: digit-length guard
    print("\n[Test 5] Digit-length guard (large block)")
    big   = bytes(i % 256 for i in range(1024))
    fcg7  = FoldingChartGenerator(num_digits=10)
    V7    = fcg7.encode_bytes(big)
    fcg8  = FoldingChartGenerator(num_digits=10)
    got7  = fcg8.decode_bytes(V7, len(big))
    assert got7 == big, "MISMATCH on large block"
    print(f"  ✅ PASSED  (1024 B, V={fmt_short(V7)}, hm.D={fcg7.hm.D})")

    # Test 6: fold_summary structure
    print("\n[Test 6] fold_summary structure")
    summary = fcg7.fold_summary()
    assert summary["fold_count"] == 0
    assert summary["triangles"]  == []
    print(f"  ✅ PASSED")

    print("\n✅  All FoldingChartGenerator tests passed.\n")
