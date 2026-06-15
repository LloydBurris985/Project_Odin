"""
FoldingChartGenerator
=====================
Extends ChartGenerator with fully reversible R-folding.

Design contract:
  - main ChartGenerator stays completely clean (no folding logic).
  - This class is opt-in, for users who want stable encoding over
    very long sequences (e.g. compressed local LatticeDrives).
  - The fold is reversible: the triangle-state log records enough
    information to replay exact R positions on decode.

Fold model — "Pythagorean triangle tracking":
  Each fold event captures a right-triangle snapshot:
    leg_a = |V - R_before|   (drift before fold — one leg)
    leg_b = byte_value        (current byte — other leg)
    R_before                  (reference axis before fold)
    R_after                   (reference axis after fold = V)
    step                      (encode step index)

  On decode, the triangle log is replayed in reverse: when the decode
  step counter matches a fold step, R is restored to R_before so the
  inverse arithmetic is applied over the same reference axis that was
  used during encoding.

Usage:
    from folding_chart_generator import FoldingChartGenerator

    # Encode
    fcg = FoldingChartGenerator(fold_threshold=500)
    for b in reversed(data):
        fcg._encode_step(b)
    V_coord    = fcg.hm.to_int(fcg.Vs[0])
    fold_log   = fcg.export_fold_log()

    # Decode (fresh instance, needs fold log)
    fcg2 = FoldingChartGenerator()
    fcg2.import_fold_log(fold_log)
    fcg2.Vs[0] = fcg2.hm.from_int(V_coord)
    recovered  = [fcg2._decode_step_folded() for _ in range(len(data))]

    assert bytes(recovered) == data
"""

import json
import math
from chart_generator import ChartGenerator, HandMath, fmt_short, fmt_large


# ---------------------------------------------------------------------------
# FoldTriangle  — immutable record of one fold event
# ---------------------------------------------------------------------------

class FoldTriangle:
    """
    Records the geometric state at a single fold event.

    Fields:
      step      — encode step_count when fold fired
      leg_a     — |V - R_before| (drift magnitude, one leg of triangle)
      leg_b     — byte value at this step (the other conceptual leg)
      hyp       — integer sqrt of leg_a² + leg_b²  (informational only)
      R_before  — R axis value before fold
      R_after   — R axis value after fold (always == V at fold moment)
      V_at_fold — V value when fold occurred
    """

    __slots__ = ("step", "leg_a", "leg_b", "hyp", "R_before", "R_after", "V_at_fold")

    def __init__(
        self,
        step:      int,
        leg_a:     int,
        leg_b:     int,
        R_before:  int,
        R_after:   int,
        V_at_fold: int,
    ):
        self.step      = step
        self.leg_a     = leg_a
        self.leg_b     = leg_b
        self.hyp       = math.isqrt(leg_a * leg_a + leg_b * leg_b)
        self.R_before  = R_before
        self.R_after   = R_after
        self.V_at_fold = V_at_fold

    def to_dict(self) -> dict:
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
    def from_dict(d: dict) -> "FoldTriangle":
        ft = FoldTriangle.__new__(FoldTriangle)
        ft.step      = int(d["step"])
        ft.leg_a     = int(d["leg_a"])
        ft.leg_b     = int(d["leg_b"])
        ft.hyp       = int(d["hyp"])
        ft.R_before  = int(d["R_before"])
        ft.R_after   = int(d["R_after"])
        ft.V_at_fold = int(d["V_at_fold"])
        return ft

    def __repr__(self) -> str:
        return (
            f"FoldTriangle(step={self.step}, "
            f"leg_a={fmt_short(self.leg_a)}, leg_b={self.leg_b}, "
            f"hyp={fmt_short(self.hyp)}, "
            f"R_before={fmt_short(self.R_before)} → R_after={fmt_short(self.R_after)})"
        )


# ---------------------------------------------------------------------------
# FoldingChartGenerator
# ---------------------------------------------------------------------------

class FoldingChartGenerator(ChartGenerator):
    """
    ChartGenerator with reversible R-folding.

    Constructor args (beyond ChartGenerator defaults):
      fold_threshold : int
          Drift ratio ×1000 that triggers a fold.
          drift = |V - R| * 1000 // R
          A fold fires when drift > fold_threshold.
          Default 1000 (same as ChartGenerator.FOLD_DRIFT_THRESHOLD).
      auto_fold_every : int | None
          If set, also check for fold every N encode steps regardless of
          drift ratio.  None (default) = drift-only triggering.

    New public API:
      _encode_step_folded(byte_val, u)  — encode step with auto-fold check
      _decode_step_folded(u)            — decode step with fold-log replay
      export_fold_log()                 — serialize triangle log to list-of-dicts
      import_fold_log(data)             — restore triangle log from list-of-dicts
      print_fold_log()                  — human-readable fold table
      encode_bytes(data, u)             — convenience: encode bytes[], return V
      decode_bytes(V, length, log, u)   — convenience: decode from V + log
    """

    def __init__(
        self,
        chart_base:     int  = 256,
        mask_base:      int  = 1_000_000_000_000,
        num_digits:     int  = 100,
        num_n_streams:  int  = 12,
        fold_threshold: int  = 1000,
        auto_fold_every: int | None = None,
    ):
        super().__init__(chart_base, mask_base, num_digits, num_n_streams)
        self._fold_threshold  = fold_threshold
        self._auto_fold_every = auto_fold_every

        # Triangle log: list of FoldTriangle, ordered by step ascending
        self._triangle_log: list[FoldTriangle] = []

        # Decode-side: index into triangle_log for the next upcoming fold to replay
        self._decode_fold_ptr: int = 0

    # -----------------------------------------------------------------------
    # Internal: check and execute a fold, recording the triangle
    # -----------------------------------------------------------------------

    def _maybe_fold(self, byte_val: int, u: int = 0) -> bool:
        """
        Check drift and fold if threshold exceeded.
        Records a FoldTriangle before adjusting R.
        Returns True if a fold was performed.
        """
        hm    = self.hm
        V_int = hm.to_int(self.Vs[u])
        R_int = hm.to_int(self.Rs[u])

        if R_int == 0:
            return False

        drift = abs(V_int - R_int) * 1000 // R_int
        if drift <= self._fold_threshold:
            return False

        leg_a   = abs(V_int - R_int)
        R_after = V_int    # pin R to current V

        triangle = FoldTriangle(
            step      = self.step_count,
            leg_a     = leg_a,
            leg_b     = byte_val,
            R_before  = R_int,
            R_after   = R_after,
            V_at_fold = V_int,
        )
        self._triangle_log.append(triangle)
        self.Rs[u] = hm.from_int(R_after)

        # Also update parent FoldStats so galactic_map stays consistent
        self.fold_stats[u].record_fold(
            step     = self.step_count,
            V_before = V_int,
            R_before = R_int,
            R_after  = R_after,
        )
        return True

    # -----------------------------------------------------------------------
    # Encode step with fold check
    # -----------------------------------------------------------------------

    def _encode_step_folded(self, byte_val: int, u: int = 0):
        """
        Encode one byte (UP direction), checking for a fold afterward.
        Fold check fires:
          - Always after every step (drift-based threshold), AND
          - Also every auto_fold_every steps if that option is set.
        """
        # Call parent encode (updates V, step_count, fold_stats min/max)
        self._encode_step(byte_val, u)

        # Drift-based fold check
        self._maybe_fold(byte_val, u)

        # Optional period-based fold check
        if (
            self._auto_fold_every is not None
            and self.step_count > 0
            and self.step_count % self._auto_fold_every == 0
        ):
            self._maybe_fold(byte_val, u)

    # -----------------------------------------------------------------------
    # Decode step with fold-log replay
    # -----------------------------------------------------------------------

    def _decode_step_folded(self, u: int = 0) -> int:
        """
        Decode one byte (UP direction), replaying fold events from the log.

        On decode, steps run in the same forward order as encode, so when
        step_count matches a recorded fold step, R is restored to R_before
        (the value that was in force during that encode step) BEFORE we
        decode.  This guarantees the inverse arithmetic uses the same R
        that was used during encoding.
        """
        # Replay any folds whose step == current step_count
        # (there should be at most one per step, but handle multiples safely)
        while (
            self._decode_fold_ptr < len(self._triangle_log)
            and self._triangle_log[self._decode_fold_ptr].step == self.step_count
        ):
            tri = self._triangle_log[self._decode_fold_ptr]
            # On decode we need R_before to be in force when decoding this step
            # The fold fires AFTER the encode step, so R_before is what was active
            # during the encode of this step — meaning we need to restore it now.
            self.Rs[u] = self.hm.from_int(tri.R_before)
            self._decode_fold_ptr += 1

        byte_val = self._decode_step(u)

        # After decoding, advance R to R_after if this step had a fold
        # (so subsequent decode steps use the folded R, matching encode)
        ptr = self._decode_fold_ptr - 1
        while ptr >= 0 and self._triangle_log[ptr].step == self.step_count - 1:
            tri = self._triangle_log[ptr]
            self.Rs[u] = self.hm.from_int(tri.R_after)
            ptr -= 1

        return byte_val

    # -----------------------------------------------------------------------
    # Convenience encode / decode
    # -----------------------------------------------------------------------

    def encode_bytes(self, data: bytes, u: int = 0) -> int:
        """
        Encode a bytes object (tail-first, UP direction).
        Returns the final V as an integer.
        Resets V and R to 1 before encoding.
        """
        hm = self.hm
        self.Vs[u] = hm.from_int(1)
        self.Rs[u] = hm.from_int(1)
        self.step_count = 0
        self._triangle_log.clear()
        self._decode_fold_ptr = 0

        for i in range(len(data) - 1, -1, -1):
            self._encode_step_folded(data[i], u)

        return hm.to_int(self.Vs[u])

    def decode_bytes(
        self,
        V:      int,
        length: int,
        log:    list | None = None,
        u:      int = 0,
    ) -> bytes:
        """
        Decode `length` bytes from coordinate V.
        log: list-of-dicts (from export_fold_log()) or None.
        If log is None, assumes no folds occurred.
        """
        hm = self.hm

        # Expand internal digit count if V is very large
        needed = 0
        v_tmp  = V
        while v_tmp > 0:
            needed += 1
            v_tmp //= hm.M
        if needed > hm.D:
            hm.D = needed + 8

        self.Vs[u] = hm.from_int(V)
        self.Rs[u] = hm.from_int(1)
        self.step_count = 0
        self._decode_fold_ptr = 0

        if log:
            self._triangle_log = [FoldTriangle.from_dict(d) for d in log]
        else:
            self._triangle_log = []

        return bytes(self._decode_step_folded(u) for _ in range(length))

    # -----------------------------------------------------------------------
    # Serialisation
    # -----------------------------------------------------------------------

    def export_fold_log(self) -> list[dict]:
        """Return the triangle log as a list of dicts (JSON-serialisable)."""
        return [t.to_dict() for t in self._triangle_log]

    def import_fold_log(self, data: list[dict]):
        """Load a triangle log from list-of-dicts (e.g. loaded from JSON)."""
        self._triangle_log    = [FoldTriangle.from_dict(d) for d in data]
        self._decode_fold_ptr = 0

    def save_state_folded(self, filename: str, universe: int = 0):
        """Save V, R, and the full fold log to a JSON file."""
        hm = self.hm
        state = {
            "chart_base":    self.chart_base,
            "mask_base":     self.mask_base,
            "num_digits":    self.num_digits,
            "num_n_streams": self.num_n_streams,
            "fold_threshold": self._fold_threshold,
            "direction":     self.direction,
            "step_count":    self.step_count,
            "V":             hm.serialize(self.Vs[universe]),
            "R":             hm.serialize(self.Rs[universe]),
            "triangle_log":  self.export_fold_log(),
        }
        with open(filename, "w") as f:
            json.dump(state, f, indent=2)
        print(f"[FoldingCG] State + fold log saved → {filename}  "
              f"({len(self._triangle_log)} triangle(s))")

    def load_state_folded(self, filename: str, universe: int = 0):
        """Restore V, R, and fold log from a saved state file."""
        with open(filename) as f:
            state = json.load(f)
        self.chart_base      = state["chart_base"]
        self.mask_base       = state["mask_base"]
        self.num_digits      = state["num_digits"]
        self.num_n_streams   = state["num_n_streams"]
        self._fold_threshold = state.get("fold_threshold", 1000)
        self.direction       = state.get("direction", "up")
        self.step_count      = state.get("step_count", 0)
        self.hm = HandMath(self.mask_base, self.num_digits)
        self.Vs[universe] = self.hm.deserialize(state["V"])
        self.Rs[universe] = self.hm.deserialize(state["R"])
        self.import_fold_log(state.get("triangle_log", []))
        print(f"[FoldingCG] State + fold log loaded ← {filename}  "
              f"({len(self._triangle_log)} triangle(s))")

    # -----------------------------------------------------------------------
    # Diagnostics
    # -----------------------------------------------------------------------

    def print_fold_log(self, max_rows: int = 20):
        """Print the triangle log in a formatted table."""
        log = self._triangle_log
        if not log:
            print("[FoldingCG] No folds recorded.")
            return

        border = "─" * 76
        print(f"\n  {border}")
        print(f"  ⬡  FOLD TRIANGLE LOG  —  {len(log)} event(s)")
        print(f"  {border}")
        print(f"  {'STEP':>8}  {'LEG_A (drift)':>16}  {'LEG_B (byte)':>12}  "
              f"{'HYP':>14}  R_BEFORE → R_AFTER")
        print(f"  {'─'*8}  {'─'*16}  {'─'*12}  {'─'*14}  ──────────────────────")

        rows = log if len(log) <= max_rows else (log[:max_rows // 2] + log[-(max_rows // 2):])
        prev_skipped = False
        for i, tri in enumerate(log):
            if len(log) > max_rows and (i == max_rows // 2):
                if not prev_skipped:
                    print(f"  {'...':>8}  {'(truncated)':>16}")
                prev_skipped = True
                continue
            prev_skipped = False
            if tri in rows:
                print(
                    f"  {tri.step:>8,}  {fmt_short(tri.leg_a):>16}  "
                    f"{tri.leg_b:>12}  {fmt_short(tri.hyp):>14}  "
                    f"{fmt_short(tri.R_before)} → {fmt_short(tri.R_after)}"
                )

        print(f"  {border}")
        if len(log) > max_rows:
            print(f"  (showing {max_rows} of {len(log)} rows)")
        print()

    def fold_summary(self, universe: int = 0) -> dict:
        """Return a summary dict for external inspection."""
        hm    = self.hm
        V_int = hm.to_int(self.Vs[universe])
        R_int = hm.to_int(self.Rs[universe])
        return {
            "fold_count":     len(self._triangle_log),
            "fold_threshold": self._fold_threshold,
            "step_count":     self.step_count,
            "V":              V_int,
            "R":              R_int,
            "drift_x1000":    abs(V_int - R_int) * 1000 // R_int if R_int else 0,
            "triangles":      [t.to_dict() for t in self._triangle_log[-5:]],
        }


# ===========================================================================
# SELF-TESTS
# ===========================================================================

if __name__ == "__main__":
    import os
    import tempfile

    print("=" * 62)
    print("  FoldingChartGenerator — Self-Tests")
    print("=" * 62)

    # ── Test 1: basic round-trip, no folds (threshold very high) ──────────
    print("\n[Test 1] Round-trip, no folds")
    data = bytes(range(50))
    fcg  = FoldingChartGenerator(fold_threshold=10_000_000)
    V    = fcg.encode_bytes(data)
    log  = fcg.export_fold_log()
    assert len(log) == 0, f"Expected 0 folds, got {len(log)}"

    fcg2 = FoldingChartGenerator()
    got  = fcg2.decode_bytes(V, len(data), log)
    assert got == data, f"MISMATCH\n  got={list(got)}\n  exp={list(data)}"
    print("  ✅ PASSED (0 folds, exact round-trip)")

    # ── Test 2: forced folds (threshold = 0 → fold every step) ──────────
    print("\n[Test 2] Aggressive folding (threshold=0)")
    data2 = bytes(range(30))
    fcg3  = FoldingChartGenerator(fold_threshold=0)
    V2    = fcg3.encode_bytes(data2)
    log2  = fcg3.export_fold_log()
    print(f"  Folds recorded: {len(log2)}")
    assert len(log2) > 0, "Expected at least one fold with threshold=0"

    fcg4 = FoldingChartGenerator()
    got2 = fcg4.decode_bytes(V2, len(data2), log2)
    assert got2 == data2, (
        f"MISMATCH\n  got={list(got2)}\n  exp={list(data2)}"
    )
    print("  ✅ PASSED (folds active, exact round-trip)")
    fcg3.print_fold_log(max_rows=10)

    # ── Test 3: moderate threshold ────────────────────────────────────────
    print("\n[Test 3] Moderate threshold (threshold=100)")
    data3 = bytes(b"Hello, OdinNet! Burris coordinate encoding with folding active.") * 3
    fcg5  = FoldingChartGenerator(fold_threshold=100)
    V3    = fcg5.encode_bytes(data3)
    log3  = fcg5.export_fold_log()
    print(f"  Data length : {len(data3)} bytes")
    print(f"  Folds       : {len(log3)}")

    fcg6 = FoldingChartGenerator()
    got3 = fcg6.decode_bytes(V3, len(data3), log3)
    assert got3 == data3, (
        f"MISMATCH at byte index "
        f"{next(i for i,(a,b) in enumerate(zip(got3,data3)) if a!=b)}"
    )
    print("  ✅ PASSED (moderate folding, exact round-trip)")

    # ── Test 4: save/load round-trip ─────────────────────────────────────
    print("\n[Test 4] Save / load state")
    data4 = bytes(range(20))
    fcg7  = FoldingChartGenerator(fold_threshold=200)
    V4    = fcg7.encode_bytes(data4)

    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "fold_state.json")
        fcg7.save_state_folded(state_path)

        fcg8 = FoldingChartGenerator()
        fcg8.load_state_folded(state_path)
        # Restore V for decode
        fcg8.Vs[0] = fcg8.hm.from_int(V4)
        fcg8.Rs[0] = fcg8.hm.from_int(1)
        fcg8.step_count = 0
        fcg8._decode_fold_ptr = 0
        got4 = bytes(fcg8._decode_step_folded() for _ in range(len(data4)))
        assert got4 == data4, f"Save/load MISMATCH\n  got={list(got4)}"
    print("  ✅ PASSED (save/load + round-trip)")

    # ── Test 5: fold summary ─────────────────────────────────────────────
    print("\n[Test 5] fold_summary()")
    summary = fcg5.fold_summary()
    assert "fold_count"  in summary
    assert "drift_x1000" in summary
    print(f"  fold_count  : {summary['fold_count']}")
    print(f"  drift ×1000 : {summary['drift_x1000']:,}")
    print("  ✅ PASSED")

    print("\n✅  All FoldingChartGenerator tests passed.\n")
