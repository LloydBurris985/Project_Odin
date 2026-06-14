"""
ChartGenerator — File encode/decode using chart-based arithmetic coding.
Burris Numerical System — Informational Universe Navigator.

Encode UP (per byte, no walk):
    V_new = V + (V - R) * (BASE - 1) + byte

Decode UP (direct inverse):
    num   = V + BASE - 1
    V_old = num // BASE
    byte  = num %  BASE

Encode DOWN (per byte, counting downward):
    W        = R - V
    W_new    = W * BASE + byte
    V_new    = R - W_new

Decode DOWN (direct inverse):
    W        = R - V
    byte     = W % BASE
    W_old    = W // BASE
    V_old    = R - W_old

Navigation extensions (Burris Navigational System):
  sublight / hyperspace / change_r / change_direction / bookmarks / galactic map

Disk extensions:
  write_disk_image   — decode coordinate to raw bytes file
  LatticeDrive       — paired-universe virtual block device (sector-addressable)
  LatticeFS          — lightweight filesystem on LatticeDrive with optional
                       AES-256-GCM superblock encryption and burris:// URL registry

Folding extensions (NEW):
  fold_r             — dynamically adjust R based on V-R delta (keeps encoding stable)
  fold_stats         — track min/max V, fold events, drift metrics
"""

import base64
import json
import os
import random
import copy

# ---------------------------------------------------------------------------
# Optional encryption support
# ---------------------------------------------------------------------------
try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False
    print("⚠  cryptography not installed — LatticeFS encryption disabled.")
    print("   Install with: pip install cryptography --break-system-packages")


# ---------------------------------------------------------------------------
# Large number formatters
# ---------------------------------------------------------------------------

def fmt_large(n: int, max_digits: int = 30) -> str:
    """Format a large integer with commas; truncate very large values."""
    if n == 0:
        return "0"
    s    = str(abs(n))
    sign = "-" if n < 0 else ""
    if len(s) > max_digits:
        return f"{sign}{s[:6]}…{s[-4:]}  [10^{len(s)-1}]"
    out = []
    for i, ch in enumerate(reversed(s)):
        if i and i % 3 == 0:
            out.append(",")
        out.append(ch)
    return sign + "".join(reversed(out))


def fmt_short(n: int) -> str:
    """4-sig-fig scientific-style display."""
    if n == 0:
        return "0"
    s   = str(abs(n))
    exp = len(s) - 1
    if exp < 6:
        return fmt_large(n)
    return f"{s[0]}.{s[1:5]}e+{exp:02d}"


# ---------------------------------------------------------------------------
# Hand math: arbitrary-precision integer as little-endian limb list
# ---------------------------------------------------------------------------

class HandMath:
    def __init__(self, mask_base: int, num_digits: int):
        self.M = mask_base
        self.D = num_digits

    def zero(self) -> list:
        return [0] * self.D

    def from_int(self, value: int) -> list:
        limbs = [0] * self.D
        v = abs(value)
        for i in range(self.D):
            if v == 0:
                break
            limbs[i] = v % self.M
            v //= self.M
        return limbs

    def to_int(self, a: list) -> int:
        result = 0
        for i in range(self.D - 1, -1, -1):
            result = result * self.M + (a[i] if i < len(a) else 0)
        return result

    def cmp(self, a: list, b: list) -> int:
        for i in range(self.D - 1, -1, -1):
            ai = a[i] if i < len(a) else 0
            bi = b[i] if i < len(b) else 0
            if ai > bi: return 1
            if ai < bi: return -1
        return 0

    def add(self, a: list, b: list) -> list:
        r, carry = [0] * self.D, 0
        for i in range(self.D):
            total  = (a[i] if i < len(a) else 0) + (b[i] if i < len(b) else 0) + carry
            r[i]   = total % self.M
            carry  = total // self.M
        if carry:
            extra = max(8, self.D // 4)
            self.D += extra
            r += [0] * extra
            idx = self.D - extra
            while carry and idx < self.D:
                total  = r[idx] + carry
                r[idx] = total % self.M
                carry  = total // self.M
                idx   += 1
        return r

    def sub(self, a: list, b: list) -> list:
        """abs(a - b) — always non-negative."""
        c = self.cmp(a, b)
        if c == 0:
            return self.zero()
        big, small = (a, b) if c > 0 else (b, a)
        r, borrow = [0] * self.D, 0
        for i in range(self.D):
            t = (big[i] if i < len(big) else 0) - (small[i] if i < len(small) else 0) - borrow
            if t < 0:
                t      += self.M
                borrow  = 1
            else:
                borrow  = 0
            r[i] = t
        return r

    def mul_scalar(self, a: list, s: int) -> list:
        r, carry = [0] * self.D, 0
        for i in range(self.D):
            total  = (a[i] if i < len(a) else 0) * s + carry
            r[i]   = total % self.M
            carry  = total // self.M
        if carry:
            extra = max(8, self.D // 4)
            self.D += extra
            r += [0] * extra
            idx = self.D - extra
            while carry and idx < self.D:
                total  = r[idx] + carry
                r[idx] = total % self.M
                carry  = total // self.M
                idx   += 1
        return r

    def div_scalar(self, a: list, s: int):
        """Return (quotient_limbs, remainder_int)."""
        q, rem = [0] * self.D, 0
        for i in range(self.D - 1, -1, -1):
            cur  = rem * self.M + (a[i] if i < len(a) else 0)
            q[i] = cur // s
            rem  = cur  % s
        return q, rem

    def serialize(self, a: list) -> list:
        return a[: self.D]

    def deserialize(self, data: list) -> list:
        r = list(data)
        while len(r) < self.D:
            r.append(0)
        return r[: self.D]


# ---------------------------------------------------------------------------
# FoldStats — tracks encoding drift and fold events
# ---------------------------------------------------------------------------

class FoldStats:
    """
    Lightweight min/max/fold tracker attached to a ChartGenerator.

    Records:
      - min_V / max_V seen across all encode steps
      - Number of times fold_r() triggered
      - History of fold events (step_count, V_before, R_before, R_after)
      - Current drift = |V - R| relative to R (as a ratio, integer-scaled ×1000)
    """

    def __init__(self):
        self.min_V:       int  = None
        self.max_V:       int  = None
        self.fold_count:  int  = 0
        self.fold_log:    list = []   # list of dicts
        self.total_steps: int  = 0

    def record_V(self, V_int: int):
        """Update min/max after each encode step."""
        if self.min_V is None or V_int < self.min_V:
            self.min_V = V_int
        if self.max_V is None or V_int > self.max_V:
            self.max_V = V_int
        self.total_steps += 1

    def record_fold(self, step: int, V_before: int, R_before: int, R_after: int):
        """Record a fold_r event."""
        self.fold_count += 1
        self.fold_log.append({
            "step":     step,
            "V":        V_before,
            "R_before": R_before,
            "R_after":  R_after,
            "delta":    abs(V_before - R_before),
        })
        # Keep last 50 fold events
        if len(self.fold_log) > 50:
            self.fold_log = self.fold_log[-50:]

    def drift_ratio_x1000(self, V_int: int, R_int: int) -> int:
        """Return |V-R| / R × 1000, integer-only. 0 if R == 0."""
        if R_int == 0:
            return 0
        return abs(V_int - R_int) * 1000 // R_int

    def print_summary(self, V_int: int, R_int: int):
        border = "─" * 56
        print(f"\n{border}")
        print(f"  ⬡  FOLD STATS")
        print(f"{border}")
        print(f"  Total encode steps : {self.total_steps:,}")
        print(f"  Fold events        : {self.fold_count:,}")
        print(f"  Min V              : {fmt_short(self.min_V) if self.min_V is not None else '—'}")
        print(f"  Max V              : {fmt_short(self.max_V) if self.max_V is not None else '—'}")
        print(f"  Current V          : {fmt_short(V_int)}")
        print(f"  Current R          : {fmt_short(R_int)}")
        drift = self.drift_ratio_x1000(V_int, R_int)
        print(f"  Drift |V-R|/R ×1000: {drift:,}")
        if self.fold_log:
            print(f"{border}")
            print(f"  Last fold events (up to 5):")
            for ev in self.fold_log[-5:]:
                print(f"    step={ev['step']:,}  δ={fmt_short(ev['delta'])}  "
                      f"R: {fmt_short(ev['R_before'])} → {fmt_short(ev['R_after'])}")
        print(f"{border}\n")


# ---------------------------------------------------------------------------
# ChartGenerator  (Burris Numerical System — Informational Universe)
# ---------------------------------------------------------------------------

class ChartGenerator:
    # Folding thresholds (class-level defaults; override per-instance if desired)
    FOLD_DRIFT_THRESHOLD  = 1000    # |V-R|/R ×1000 must exceed this to trigger fold
    FOLD_SCALE_NUMERATOR  = 1       # R_new = V + (V-R) * N / D  where N/D is the
    FOLD_SCALE_DENOMINATOR = 2      #   fraction of drift to keep as headroom

    def __init__(
        self,
        chart_base:    int = 256,
        mask_base:     int = 1_000_000_000_000,
        num_digits:    int = 100,
        num_n_streams: int = 12,
    ):
        self.chart_base    = chart_base
        self.mask_base     = mask_base
        self.num_digits    = num_digits
        self.num_n_streams = num_n_streams

        self.hm = HandMath(mask_base, num_digits)

        self.Vs = [self.hm.from_int(1) for _ in range(num_n_streams)]
        self.Rs = [self.hm.from_int(1) for _ in range(num_n_streams)]

        self.direction        = "up"
        self.step_count       = 0
        self.hyperspace_log   = []
        self._saved_positions = {}

        # Folding stats — one per universe stream
        self.fold_stats = [FoldStats() for _ in range(num_n_streams)]

    # -----------------------------------------------------------------------
    # Navigation: Change R / direction
    # -----------------------------------------------------------------------

    def change_r(self, new_r_int: int, universe: int = 0):
        old_r = self.hm.to_int(self.Rs[universe])
        self.Rs[universe] = self.hm.from_int(new_r_int)
        print(f"\n[NAVIGATION] R axis relocated")
        print(f"  OLD R : {fmt_large(old_r)}")
        print(f"  NEW R : {fmt_large(new_r_int)}")
        return self

    def change_direction(self, universe: int = 0):
        if self.direction == "up":
            self.direction    = "down"
            self.Vs[universe] = self.hm.zero()
            print(f"\n[NAVIGATION] Direction: UP → DOWN  (W-accumulator reset to 0)")
        else:
            self.direction    = "up"
            self.Vs[universe] = self.hm.deserialize(self.Rs[universe])
            print(f"\n[NAVIGATION] Direction: DOWN → UP  (V reset to R)")
        print(f"  Direction is now: {self.direction.upper()}")
        return self

    # -----------------------------------------------------------------------
    # Folding: dynamic R adjustment
    # -----------------------------------------------------------------------

    def fold_r(self, universe: int = 0, verbose: bool = False) -> bool:
        """
        Dynamically adjust R based on current V-R drift.

        Logic:
          drift_ratio = |V - R| / R × 1000
          If drift_ratio > FOLD_DRIFT_THRESHOLD:
              R_new = V  (pin R to current V, resetting relative distance to 0)

        This keeps the encoding numerically stable over very long sequences
        by preventing |V - R| from growing unboundedly relative to R.

        The fold is purely a reference-axis relocation — it does NOT alter V
        and does NOT change the semantic content of the coordinate.  Round-trip
        correctness is preserved as long as the same fold_r calls are replayed
        on decode (or the new R is persisted alongside V in the state file).

        Returns True if a fold was performed, False otherwise.
        """
        hm    = self.hm
        V_int = hm.to_int(self.Vs[universe])
        R_int = hm.to_int(self.Rs[universe])

        if R_int == 0:
            return False

        drift = abs(V_int - R_int) * 1000 // R_int

        if drift <= self.FOLD_DRIFT_THRESHOLD:
            return False

        # Compute new R: set R = V (pin to current position)
        R_new = V_int
        self.Rs[universe] = hm.from_int(R_new)

        self.fold_stats[universe].record_fold(
            step     = self.step_count,
            V_before = V_int,
            R_before = R_int,
            R_after  = R_new,
        )

        if verbose:
            print(f"\n[FOLD] universe={universe}  step={self.step_count:,}")
            print(f"  Drift ratio ×1000 : {drift:,}  (threshold={self.FOLD_DRIFT_THRESHOLD})")
            print(f"  R: {fmt_short(R_int)}  →  {fmt_short(R_new)}  (pinned to V)")

        return True

    def auto_fold(self, universe: int = 0, every_n_steps: int = 100, verbose: bool = False):
        """
        Call fold_r() automatically every `every_n_steps` encode steps.
        Meant to be called from encode loops that want passive drift control.
        Only fires when step_count is a multiple of every_n_steps.
        """
        if self.step_count > 0 and self.step_count % every_n_steps == 0:
            self.fold_r(universe=universe, verbose=verbose)

    # -----------------------------------------------------------------------
    # Core encode / decode  (UP)
    # -----------------------------------------------------------------------

    def _encode_step(self, byte_val: int, u: int = 0):
        hm   = self.hm
        BASE = self.chart_base
        V, R = self.Vs[u], self.Rs[u]
        self.Vs[u] = hm.add(V, hm.add(hm.mul_scalar(hm.sub(V, R), BASE - 1),
                                       hm.from_int(byte_val)))
        self.step_count += 1
        # Update min/max tracker
        self.fold_stats[u].record_V(hm.to_int(self.Vs[u]))

    def _decode_step(self, u: int = 0) -> int:
        hm   = self.hm
        BASE = self.chart_base
        num        = hm.add(self.Vs[u], hm.from_int(BASE - 1))
        V_old, rem = hm.div_scalar(num, BASE)
        self.Vs[u] = V_old
        self.step_count += 1
        return rem

    # -----------------------------------------------------------------------
    # Core encode / decode  (DOWN)
    # -----------------------------------------------------------------------

    def _encode_down_step(self, byte_val: int, u: int = 0):
        hm   = self.hm
        BASE = self.chart_base
        self.Vs[u] = hm.add(hm.mul_scalar(self.Vs[u], BASE), hm.from_int(byte_val))
        self.step_count += 1
        self.fold_stats[u].record_V(hm.to_int(self.Vs[u]))

    def _decode_down_step(self, u: int = 0) -> int:
        hm   = self.hm
        BASE = self.chart_base
        W_old, rem = hm.div_scalar(self.Vs[u], BASE)
        self.Vs[u] = W_old
        self.step_count += 1
        return rem

    # -----------------------------------------------------------------------
    # Sublight travel
    # -----------------------------------------------------------------------

    def sublight(self, steps: int, x: int, side: str = "left",
                 sign: int = 1, universe: int = 0):
        hm        = self.hm
        delta_int = steps * x * (self.chart_base if side == "right" else 1)
        delta     = hm.from_int(abs(delta_int))
        V_old     = hm.to_int(self.Vs[universe])

        if sign >= 0:
            self.Vs[universe] = hm.add(self.Vs[universe], delta)
        else:
            self.Vs[universe] = hm.sub(self.Vs[universe], delta)

        V_new = hm.to_int(self.Vs[universe])
        self.step_count += steps
        print(f"\n[SUBLIGHT] {'+' if sign >= 0 else '-'}{steps} steps × X={x}"
              f"  side={side.upper()}  δ={fmt_large(delta_int * sign)}")
        print(f"  V: {fmt_short(V_old)}  →  {fmt_short(V_new)}")
        return delta_int * sign

    # -----------------------------------------------------------------------
    # Hyperspace travel
    # -----------------------------------------------------------------------

    def hyperspace_jump(self, n_bytes: int = 8, universe: int = 0, label: str = None):
        hm       = self.hm
        origin_V = hm.serialize(self.Vs[universe])
        origin_R = hm.serialize(self.Rs[universe])
        payload  = [random.randint(0, self.chart_base - 1) for _ in range(n_bytes)]

        for b in reversed(payload):
            if self.direction == "up":
                self._encode_step(b, universe)
            else:
                self._encode_down_step(b, universe)

        V_before = hm.to_int(hm.deserialize(origin_V))
        V_after  = hm.to_int(self.Vs[universe])
        jump_id  = label or f"JUMP_{len(self.hyperspace_log):04d}"

        self.hyperspace_log.append({
            "jump_id":    jump_id,
            "n_bytes":    n_bytes,
            "payload":    payload,
            "origin_V":   origin_V,
            "origin_R":   origin_R,
            "origin_dir": self.direction,
            "V_before":   V_before,
            "V_after":    V_after,
            "direction":  self.direction,
            "universe":   universe,
        })
        print(f"\n[HYPERSPACE JUMP] {jump_id}  ({n_bytes} bytes, dir={self.direction.upper()})")
        print(f"  Origin : {fmt_short(V_before)}")
        print(f"  Arrived: {fmt_short(V_after)}")
        print(f"  Distance travelled: {fmt_short(abs(V_after - V_before))}")
        return jump_id

    def hyperspace_return(self, jump_id: str = None, universe: int = 0):
        hm = self.hm
        if not self.hyperspace_log:
            print("[HYPERSPACE RETURN] No jumps on log.")
            return False
        record = (
            next((r for r in self.hyperspace_log if r["jump_id"] == jump_id), None)
            if jump_id else self.hyperspace_log[-1]
        )
        if record is None:
            print(f"[HYPERSPACE RETURN] Jump ID '{jump_id}' not found.")
            return False

        payload   = record["payload"]
        n_bytes   = record["n_bytes"]
        direction = record["direction"]
        V_current = hm.to_int(self.Vs[universe])

        recovered = []
        for _ in range(n_bytes):
            recovered.append(
                self._decode_step(universe) if direction == "up"
                else self._decode_down_step(universe)
            )

        V_restored = hm.to_int(self.Vs[universe])
        V_origin   = hm.to_int(hm.deserialize(record["origin_V"]))
        match      = (recovered == payload) and (V_restored == V_origin)

        print(f"\n[HYPERSPACE RETURN] ← {record['jump_id']}")
        print(f"  Departed : {fmt_short(V_current)}")
        print(f"  Returned : {fmt_short(V_restored)}")
        print(f"  Expected : {fmt_short(V_origin)}")
        print(f"  Payload match  : {'✅ YES' if recovered == payload else '❌ NO'}")
        print(f"  Position match : {'✅ YES' if V_restored == V_origin else '❌ NO'}")
        return match

    # -----------------------------------------------------------------------
    # Bookmarks
    # -----------------------------------------------------------------------

    def save_position(self, name: str = "bookmark", universe: int = 0):
        hm = self.hm
        self._saved_positions[name] = {
            "V":         hm.serialize(self.Vs[universe]),
            "R":         hm.serialize(self.Rs[universe]),
            "direction": self.direction,
            "steps":     self.step_count,
            "universe":  universe,
        }
        print(f"\n[BOOKMARK] '{name}' saved  V={fmt_short(hm.to_int(self.Vs[universe]))}  "
              f"dir={self.direction.upper()}")
        return self

    def load_position(self, name: str = "bookmark", universe: int = 0):
        hm  = self.hm
        rec = self._saved_positions.get(name)
        if rec is None:
            print(f"[BOOKMARK] '{name}' not found.")
            return False
        self.Vs[universe] = hm.deserialize(rec["V"])
        self.Rs[universe] = hm.deserialize(rec["R"])
        self.direction    = rec["direction"]
        print(f"\n[BOOKMARK] '{name}' restored  V={fmt_short(hm.to_int(self.Vs[universe]))}  "
              f"dir={self.direction.upper()}")
        return True

    def list_bookmarks(self):
        if not self._saved_positions:
            print("[BOOKMARKS] None saved.")
            return
        hm = self.hm
        print(f"\n{'═'*50}\n  SAVED POSITIONS ({len(self._saved_positions)})\n{'═'*50}")
        for name, rec in self._saved_positions.items():
            V_int = hm.to_int(hm.deserialize(rec["V"]))
            R_int = hm.to_int(hm.deserialize(rec["R"]))
            print(f"  [{name}]")
            print(f"    V={fmt_short(V_int)}  R={fmt_short(R_int)}  "
                  f"dir={rec['direction'].upper()}  steps={rec['steps']}")
        print(f"{'═'*50}")

    # -----------------------------------------------------------------------
    # Galactic map
    # -----------------------------------------------------------------------

    def galactic_map(self, universe: int = 0, label: str = ""):
        hm    = self.hm
        BASE  = self.chart_base
        V_int = hm.to_int(self.Vs[universe])
        R_int = hm.to_int(self.Rs[universe])
        fs    = self.fold_stats[universe]

        border = "═" * 64
        title  = "✦  BURRIS NAVIGATIONAL SYSTEM — GALACTIC MAP  ✦"
        if label:
            title += f"  [{label}]"
        print(f"\n{border}")
        print(f"  {title}")
        print(f"{border}")
        print(f"  UNIVERSE   : {universe}  |  DIRECTION : {self.direction.upper()}  |  BASE : {BASE}")
        print(f"  TOTAL STEPS: {self.step_count:,}")
        print(f"{'-'*64}")
        print(f"  V (position)  : {fmt_large(V_int)}")
        print(f"  V (short)     : {fmt_short(V_int)}")
        print(f"  V (digit len) : {len(str(V_int))} digits")
        print(f"{'-'*64}")
        print(f"  R (reference) : {fmt_large(R_int)}")
        print(f"  R (short)     : {fmt_short(R_int)}")
        print(f"{'-'*64}")
        print(f"  |V - R|       : {fmt_large(abs(V_int - R_int))}")
        print(f"  Drift ×1000   : {fs.drift_ratio_x1000(V_int, R_int):,}")
        print(f"  Fold events   : {fs.fold_count:,}")
        print(f"  LEFT  side V  : {fmt_short(V_int)}")
        print(f"  RIGHT side V  : {fmt_short(V_int * BASE)}  (V × BASE)")
        print(f"{'-'*64}")
        if fs.min_V is not None:
            print(f"  Min V seen    : {fmt_short(fs.min_V)}")
            print(f"  Max V seen    : {fmt_short(fs.max_V)}")
            print(f"{'-'*64}")
        if self.hyperspace_log:
            print(f"  HYPERSPACE LOG  ({len(self.hyperspace_log)} jumps)")
            for rec in self.hyperspace_log[-3:]:
                print(f"    {rec['jump_id']}  {fmt_short(rec['V_before'])} → {fmt_short(rec['V_after'])}")
        else:
            print(f"  HYPERSPACE LOG  : empty")
        if self._saved_positions:
            print(f"{'-'*64}")
            print(f"  BOOKMARKS  ({len(self._saved_positions)})")
            for name, rec in self._saved_positions.items():
                bV = hm.to_int(hm.deserialize(rec["V"]))
                print(f"    ★ {name:16s}  V={fmt_short(bV)}  dir={rec['direction'].upper()}")
        print(f"{border}\n")

    # -----------------------------------------------------------------------
    # Navigation menu
    # -----------------------------------------------------------------------

    def navigation_menu(self, universe: int = 0):
        self.save_position("ORIGIN", universe)
        print("\n" + "★" * 64)
        print("  BURRIS INFORMATIONAL UNIVERSE — STARSHIP NAVIGATION CONSOLE")
        print("★" * 64)
        print("  Type 'help' for commands.\n")
        self.galactic_map(universe, "CURRENT POSITION")

        cmd_help = """
╔══════════════════════════════════════════════════════════════╗
║  map                   Show galactic map                     ║
║  sublight [+/-][n] [x] [left/right]  Sublight travel        ║
║  hyperspace [n]        Jump: encode n random bytes (def=8)   ║
║  return [id]           Return from last hyperspace jump      ║
║  change_r [n]          Relocate reference axis R             ║
║  fold                  Fold R to current V (drift reset)     ║
║  foldstats             Print fold statistics                 ║
║  flip                  Flip encoding direction UP ↔ DOWN     ║
║  save [name]           Save current position                 ║
║  load [name]           Restore position from bookmark        ║
║  bookmarks             List all saved positions              ║
║  encode [n]            Encode n bytes (byte=1 each)          ║
║  decode [n]            Decode n bytes                        ║
║  reset                 Return to ORIGIN bookmark             ║
║  help / quit / exit                                          ║
╚══════════════════════════════════════════════════════════════╝"""

        while True:
            try:
                raw = input("\n  NAV> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n[NAV] Console closed.")
                break
            if not raw:
                continue
            parts = raw.split()
            cmd   = parts[0].lower()

            if cmd in ("quit", "exit", "q"):
                print("\n[NAV] Logging off. Safe travels.\n")
                break
            elif cmd == "help":
                print(cmd_help)
            elif cmd == "map":
                self.galactic_map(universe, " ".join(parts[1:]))
            elif cmd == "sublight":
                try:
                    rs    = parts[1] if len(parts) > 1 else "+1"
                    sign  = -1 if rs.startswith("-") else 1
                    steps = int(rs.lstrip("+-")) if len(rs) > 1 else 1
                    x     = int(parts[2]) if len(parts) > 2 else 1
                    side  = parts[3].lower() if len(parts) > 3 else "left"
                    self.sublight(steps, x, side=side, sign=sign, universe=universe)
                    self.galactic_map(universe, "AFTER SUBLIGHT")
                except (ValueError, IndexError) as e:
                    print(f"  [ERR] {e}")
            elif cmd == "hyperspace":
                n   = int(parts[1]) if len(parts) > 1 else 8
                lbl = parts[2] if len(parts) > 2 else None
                self.hyperspace_jump(n, universe=universe, label=lbl)
                self.galactic_map(universe, "AFTER HYPERSPACE JUMP")
            elif cmd == "return":
                jid = parts[1] if len(parts) > 1 else None
                ok  = self.hyperspace_return(jid, universe=universe)
                self.galactic_map(universe, "AFTER HYPERSPACE RETURN")
                print("  ✅ Returned." if ok else "  ❌ Return mismatch.")
            elif cmd == "change_r":
                try:
                    self.change_r(int(parts[1]) if len(parts) > 1 else 1, universe)
                    self.galactic_map(universe, "AFTER CHANGE R")
                except ValueError:
                    print("  [ERR] change_r requires an integer.")
            elif cmd == "fold":
                folded = self.fold_r(universe=universe, verbose=True)
                if folded:
                    self.galactic_map(universe, "AFTER FOLD")
                else:
                    drift = self.fold_stats[universe].drift_ratio_x1000(
                        self.hm.to_int(self.Vs[universe]),
                        self.hm.to_int(self.Rs[universe]),
                    )
                    print(f"  [FOLD] No fold needed. Drift ×1000 = {drift:,} "
                          f"(threshold={self.FOLD_DRIFT_THRESHOLD})")
            elif cmd == "foldstats":
                V_int = self.hm.to_int(self.Vs[universe])
                R_int = self.hm.to_int(self.Rs[universe])
                self.fold_stats[universe].print_summary(V_int, R_int)
            elif cmd == "flip":
                self.change_direction(universe)
                self.galactic_map(universe, "AFTER DIRECTION FLIP")
            elif cmd == "save":
                self.save_position(parts[1] if len(parts) > 1 else "quicksave", universe)
            elif cmd == "load":
                name = parts[1] if len(parts) > 1 else "quicksave"
                self.load_position(name, universe)
                self.galactic_map(universe, f"LOADED: {name}")
            elif cmd == "bookmarks":
                self.list_bookmarks()
            elif cmd == "encode":
                n = int(parts[1]) if len(parts) > 1 else 1
                for _ in range(n):
                    if self.direction == "up":
                        self._encode_step(1, universe)
                    else:
                        self._encode_down_step(1, universe)
                self.galactic_map(universe, f"AFTER {n} ENCODE STEPS")
            elif cmd == "decode":
                n       = int(parts[1]) if len(parts) > 1 else 1
                results = []
                for _ in range(n):
                    results.append(
                        self._decode_step(universe) if self.direction == "up"
                        else self._decode_down_step(universe)
                    )
                print(f"  Decoded bytes: {results}")
                self.galactic_map(universe, f"AFTER {n} DECODE STEPS")
            elif cmd == "reset":
                if self.load_position("ORIGIN", universe):
                    self.galactic_map(universe, "RESET TO ORIGIN")
                else:
                    print("  [ERR] No ORIGIN bookmark found.")
            else:
                print(f"  [?] Unknown command: '{cmd}'.  Type 'help'.")

    # -----------------------------------------------------------------------
    # File encode / decode  (UP)
    # -----------------------------------------------------------------------

    def encode_file(self, input_path: str, output_json_path: str, universe: int = 0):
        with open(input_path, "rb") as f:
            data = f.read()
        print(f"=== Encoding Phase (UP) === ({len(data)} bytes, reversed)")
        for i in range(len(data) - 1, -1, -1):
            self._encode_step(data[i], universe)
        state = {
            "chart_base":    self.chart_base,
            "mask_base":     self.mask_base,
            "num_digits":    self.num_digits,
            "num_n_streams": self.num_n_streams,
            "universe":      universe,
            "file_length":   len(data),
            "direction":     "up",
            "V":             self.hm.serialize(self.Vs[universe]),
            "R":             self.hm.serialize(self.Rs[universe]),
        }
        with open(output_json_path, "w") as f:
            json.dump(state, f)
        print(f"Encoding PASSED 😁  →  {output_json_path}")

    def decode_file(self, input_json_path: str, output_path: str):
        with open(input_json_path, "r") as f:
            state = json.load(f)
        u           = state["universe"]
        file_length = state["file_length"]
        self.chart_base = state["chart_base"]
        self.mask_base  = state["mask_base"]
        self.num_digits = state["num_digits"]
        self.hm         = HandMath(self.mask_base, self.num_digits)
        self.Vs[u]      = self.hm.deserialize(state["V"])
        self.Rs[u]      = self.hm.deserialize(state["R"])
        print(f"=== Decoding Phase (UP) === ({file_length} bytes)")
        recovered = [self._decode_step(u) for _ in range(file_length)]
        with open(output_path, "wb") as f:
            f.write(bytes(recovered))
        print(f"Decoding PASSED 😁  →  {output_path}")

    # -----------------------------------------------------------------------
    # File encode / decode  (DOWN)
    # -----------------------------------------------------------------------

    def encode_file_down(self, input_path: str, output_json_path: str, universe: int = 0):
        with open(input_path, "rb") as f:
            data = f.read()
        self.Vs[universe] = self.hm.zero()
        print(f"=== Encoding Phase (DOWN) === ({len(data)} bytes, reversed)")
        for i in range(len(data) - 1, -1, -1):
            self._encode_down_step(data[i], universe)
        state = {
            "chart_base":    self.chart_base,
            "mask_base":     self.mask_base,
            "num_digits":    self.num_digits,
            "num_n_streams": self.num_n_streams,
            "universe":      universe,
            "file_length":   len(data),
            "direction":     "down",
            "V":             self.hm.serialize(self.Vs[universe]),
            "R":             self.hm.serialize(self.Rs[universe]),
        }
        with open(output_json_path, "w") as f:
            json.dump(state, f)
        print(f"Encoding DOWN PASSED 😁  →  {output_json_path}")

    def decode_file_down(self, input_json_path: str, output_path: str):
        with open(input_json_path, "r") as f:
            state = json.load(f)
        u           = state["universe"]
        file_length = state["file_length"]
        self.chart_base = state["chart_base"]
        self.mask_base  = state["mask_base"]
        self.num_digits = state["num_digits"]
        self.hm         = HandMath(self.mask_base, self.num_digits)
        self.Vs[u]      = self.hm.deserialize(state["V"])
        self.Rs[u]      = self.hm.deserialize(state["R"])
        print(f"=== Decoding Phase (DOWN) === ({file_length} bytes)")
        recovered = [self._decode_down_step(u) for _ in range(file_length)]
        with open(output_path, "wb") as f:
            f.write(bytes(recovered))
        print(f"Decoding DOWN PASSED 😁  →  {output_path}")

    # -----------------------------------------------------------------------
    # write_disk_image
    # -----------------------------------------------------------------------

    def write_disk_image(
        self,
        coordinate:  int,
        length:      int,
        output_path: str,
        direction:   str = "up",
        r_value:     int = 1,
        universe:    int = 0,
    ) -> bytes:
        """Decode `length` bytes from a bare coordinate integer, write to output_path."""
        hm = self.hm
        self.Vs[universe] = hm.from_int(coordinate)
        self.Rs[universe] = hm.from_int(r_value)

        border = "─" * 56
        print(f"\n{border}")
        print(f"  WRITE DISK IMAGE")
        print(f"  Coordinate : {fmt_short(coordinate)}  ({len(str(coordinate))} digits)")
        print(f"  Length     : {length} bytes  |  Direction : {direction.upper()}")
        print(f"  Output     : {output_path}")
        print(f"{border}")

        if direction == "up":
            recovered = [self._decode_step(universe) for _ in range(length)]
        elif direction == "down":
            recovered = [self._decode_down_step(universe) for _ in range(length)]
        else:
            raise ValueError(f"Unknown direction '{direction}'. Use 'up' or 'down'.")

        raw = bytes(recovered)
        with open(output_path, "wb") as f:
            f.write(raw)

        print(f"  ✅ Decoded {length} bytes  →  {output_path}")
        print(f"  First bytes: {list(raw[:16])}" + (" ..." if length > 16 else ""))
        print(f"{border}\n")
        return raw

    # -----------------------------------------------------------------------
    # State persistence
    # -----------------------------------------------------------------------

    def save_state(self, filename: str = "universe_state.json"):
        state = {
            "chart_base":    self.chart_base,
            "mask_base":     self.mask_base,
            "num_digits":    self.num_digits,
            "num_n_streams": self.num_n_streams,
            "direction":     self.direction,
            "step_count":    self.step_count,
            "Vs": [self.hm.serialize(v) for v in self.Vs],
            "Rs": [self.hm.serialize(r) for r in self.Rs],
        }
        with open(filename, "w") as f:
            json.dump(state, f)
        print(f"State saved → {filename}")

    def load_state(self, filename: str = "universe_state.json"):
        with open(filename, "r") as f:
            state = json.load(f)
        self.chart_base    = state["chart_base"]
        self.mask_base     = state["mask_base"]
        self.num_digits    = state["num_digits"]
        self.num_n_streams = state["num_n_streams"]
        self.direction     = state.get("direction", "up")
        self.step_count    = state.get("step_count", 0)
        self.hm = HandMath(self.mask_base, self.num_digits)
        self.Vs = [self.hm.deserialize(v) for v in state["Vs"]]
        self.Rs = [self.hm.deserialize(r) for r in state["Rs"]]
        print(f"State loaded ← {filename}")

    def print_state(self, universe_idx: int = 0):
        hm    = self.hm
        V_int = hm.to_int(self.Vs[universe_idx])
        R_int = hm.to_int(self.Rs[universe_idx])
        print(f"Universe {universe_idx}: V={fmt_large(V_int)}  R={fmt_large(R_int)}  "
              f"dir={self.direction.upper()}")


# ===========================================================================
# ENCRYPTION HELPER  (AES-256-GCM, PBKDF2 key derivation)
# ===========================================================================

class LatticeFSEncrypted:
    """
    AES-256-GCM encryption for LatticeFS superblock data.
    Encrypted blobs are self-contained: salt + nonce are embedded.
    Unencrypted data passes through unchanged (backward-compatible).
    """

    _PREFIX = b"ENC:"

    @staticmethod
    def derive_key(passphrase: str, salt: bytes = None):
        if salt is None:
            salt = os.urandom(16)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=600_000,
        )
        return kdf.derive(passphrase.encode("utf-8")), salt

    @staticmethod
    def encrypt(data: bytes, passphrase: str) -> bytes:
        if not CRYPTO_AVAILABLE:
            return data
        key, salt = LatticeFSEncrypted.derive_key(passphrase)
        nonce     = os.urandom(12)
        ct        = AESGCM(key).encrypt(nonce, data, None)
        return LatticeFSEncrypted._PREFIX + base64.b64encode(salt + nonce + ct)

    @staticmethod
    def decrypt(data: bytes, passphrase: str) -> bytes:
        if not data.startswith(LatticeFSEncrypted._PREFIX):
            return data                              # plaintext — pass through
        if not CRYPTO_AVAILABLE:
            raise RuntimeError("cryptography library required to decrypt drive.")
        raw   = base64.b64decode(data[4:], validate=False)
        salt  = raw[:16]
        nonce = raw[16:28]
        ct    = raw[28:]
        key, _ = LatticeFSEncrypted.derive_key(passphrase, salt)
        return AESGCM(key).decrypt(nonce, ct, None)

    @staticmethod
    def is_encrypted(data: bytes) -> bool:
        return data.startswith(LatticeFSEncrypted._PREFIX)


# ===========================================================================
# LATTICE DRIVE
# ===========================================================================

class LatticeDrive:
    """
    Paired-universe virtual block device.

    Write path:
        sector_bytes  ──encode(A)──▶  V_A  ──decode(B from V_A)──▶  V_B  (stored)

    Read path:
        V_A  ──decode(A)──▶  original sector_bytes

    Serialisable to/from a self-contained JSON image.
    """

    _BORDER = "═" * 60

    def __init__(
        self,
        sector_size: int = 512,
        n_sectors:   int = 64,
        chart_base:  int = 256,
        mask_base:   int = 1_000_000_000_000,
        num_digits:  int = 100,
    ):
        self.sector_size = sector_size
        self.n_sectors   = n_sectors
        self.chart_base  = chart_base
        self.mask_base   = mask_base
        self.num_digits  = num_digits

        self._cg_a = ChartGenerator(chart_base, mask_base, num_digits)
        self._cg_b = ChartGenerator(chart_base, mask_base, num_digits)

        self._sectors:     list = [self._empty_sector(i) for i in range(n_sectors)]
        self._head:        int  = 0
        self._write_count: int  = 0
        self._read_count:  int  = 0

    def __repr__(self) -> str:
        used = sum(1 for s in self._sectors if s["written"])
        return (f"<LatticeDrive sectors={self.n_sectors} "
                f"sector_size={self.sector_size}B "
                f"used={used} free={self.n_sectors - used} "
                f"head={self._head}>")

    def __len__(self) -> int:
        return self.n_sectors * self.sector_size

    def __contains__(self, sector_no: int) -> bool:
        return 0 <= sector_no < self.n_sectors and self._sectors[sector_no]["written"]

    @staticmethod
    def _empty_sector(n: int) -> dict:
        return {"sector": n, "byte_length": 0, "V_A": 0, "V_B": 0, "written": False}

    def _assert_sector(self, n: int):
        if not (0 <= n < self.n_sectors):
            raise IndexError(f"Sector {n} out of range [0, {self.n_sectors - 1}].")

    def _encode_bytes_up(self, cg: ChartGenerator, data: bytes, u: int = 0) -> int:
        hm = cg.hm
        cg.Vs[u] = hm.from_int(1)
        cg.Rs[u] = hm.from_int(1)
        for i in range(len(data) - 1, -1, -1):
            cg._encode_step(data[i], u)
        return hm.to_int(cg.Vs[u])

    def _decode_bytes_up(self, cg: ChartGenerator, V: int, length: int, u: int = 0) -> bytes:
        hm = cg.hm
        needed = 0
        v_tmp  = V
        while v_tmp > 0:
            needed += 1
            v_tmp //= hm.M
        if needed > hm.D:
            hm.D = needed + 8
        cg.Vs[u] = hm.from_int(V)
        cg.Rs[u] = hm.from_int(1)
        return bytes(cg._decode_step(u) for _ in range(length))

    # ── Public block-device interface ──────────────────────────────────────

    def seek(self, sector_no: int) -> "LatticeDrive":
        self._assert_sector(sector_no)
        self._head = sector_no
        print(f"[LATTICE-DRIVE] Head → sector {sector_no}")
        return self

    def write(self, data: bytes, sector_no: int = None) -> dict:
        if sector_no is None:
            sector_no = self._head
        self._assert_sector(sector_no)
        if len(data) > self.sector_size:
            raise ValueError(
                f"Data ({len(data)} bytes) exceeds sector size ({self.sector_size} bytes).")

        padded  = data + bytes(self.sector_size - len(data))
        V_A     = self._encode_bytes_up(self._cg_a, padded)

        self._cg_b.Vs[0] = self._cg_b.hm.from_int(V_A)
        self._cg_b.Rs[0] = self._cg_b.hm.from_int(1)
        for _ in range(self.sector_size):
            self._cg_b._decode_step(0)
        V_B = self._cg_b.hm.to_int(self._cg_b.Vs[0])

        rec = {"sector": sector_no, "byte_length": len(data),
               "V_A": V_A, "V_B": V_B, "written": True}
        self._sectors[sector_no] = rec
        self._write_count       += 1
        self._head               = min(sector_no + 1, self.n_sectors - 1)

        print(f"[WRITE] Sector {sector_no:4d}  {len(data):4d} bytes  "
              f"V_A={fmt_short(V_A)}  V_B={fmt_short(V_B)}")
        return rec

    def read(self, sector_no: int = None) -> bytes:
        if sector_no is None:
            sector_no = self._head
        self._assert_sector(sector_no)

        rec = self._sectors[sector_no]
        if not rec["written"]:
            print(f"[READ] Sector {sector_no} is empty — returning zero-fill.")
            self._head = min(sector_no + 1, self.n_sectors - 1)
            return bytes(self.sector_size)

        recovered_padded = self._decode_bytes_up(self._cg_a, rec["V_A"], self.sector_size)
        recovered        = recovered_padded[: rec["byte_length"]]
        self._read_count += 1
        self._head        = min(sector_no + 1, self.n_sectors - 1)

        print(f"[READ]  Sector {sector_no:4d}  {rec['byte_length']:4d} bytes  "
              f"V_A={fmt_short(rec['V_A'])}  V_B={fmt_short(rec['V_B'])}")
        return recovered

    def write_file(self, data: bytes, start_sector: int = 0) -> list:
        sectors_needed = (len(data) + self.sector_size - 1) // self.sector_size
        end_sector     = start_sector + sectors_needed - 1
        if end_sector >= self.n_sectors:
            raise IndexError(
                f"File requires sectors {start_sector}–{end_sector}, "
                f"drive only has {self.n_sectors} sectors.")
        used, offset = [], 0
        for i in range(sectors_needed):
            chunk = data[offset : offset + self.sector_size]
            self.write(chunk, start_sector + i)
            used.append(start_sector + i)
            offset += self.sector_size
        print(f"[WRITE-FILE] {len(data)} bytes  →  sectors {used[0]}–{used[-1]}")
        return used

    def read_file(self, start_sector: int, n_sectors: int) -> bytes:
        result = bytearray()
        for i in range(n_sectors):
            result += self.read(start_sector + i)
        return bytes(result)

    def format(self, n_sectors: int = None) -> "LatticeDrive":
        if n_sectors is not None:
            self.n_sectors = n_sectors
        self._sectors     = [self._empty_sector(i) for i in range(self.n_sectors)]
        self._head        = 0
        self._write_count = 0
        self._read_count  = 0
        print(f"[FORMAT] Drive formatted  —  "
              f"{self.n_sectors} sectors × {self.sector_size} bytes/sector")
        return self

    def hex_dump(self, sector_no: int, cols: int = 16):
        data = self.read(sector_no)
        print(f"\n  HEX DUMP — Sector {sector_no}  ({len(data)} bytes)")
        print(f"  {'─' * 58}")
        for offset in range(0, len(data), cols):
            chunk      = data[offset : offset + cols]
            hex_part   = " ".join(f"{b:02x}" for b in chunk)
            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            print(f"  {offset:04x}:  {hex_part:<{cols * 3}}  |{ascii_part}|")
        print(f"  {'─' * 58}\n")

    def info(self):
        used     = sum(1 for s in self._sectors if s["written"])
        free     = self.n_sectors - used
        used_mb  = (used  * self.sector_size) / 1_048_576
        total_mb = (self.n_sectors * self.sector_size) / 1_048_576

        print(f"\n{self._BORDER}")
        print(f"  ⬡  LATTICE DRIVE — STATUS")
        print(f"{self._BORDER}")
        print(f"  Geometry   : {self.n_sectors} sectors × {self.sector_size} bytes/sector")
        print(f"  Capacity   : {total_mb:.3f} MB  ({self.n_sectors * self.sector_size:,} bytes)")
        print(f"  Used       : {used} sectors  ({used_mb:.3f} MB)")
        print(f"  Free       : {free} sectors")
        print(f"  Head pos   : sector {self._head}")
        print(f"  Writes     : {self._write_count}  |  Reads: {self._read_count}")
        print(f"  Base       : {self.chart_base}  |  Digits: {self.num_digits}")
        print(f"{'-' * 60}")
        print(f"  {'SEC':>4}  {'BYTES':>6}  {'V_A (short)':>14}  {'V_B (short)':>14}  STS")
        print(f"  {'─'*4}  {'─'*6}  {'─'*14}  {'─'*14}  ───")
        for rec in self._sectors:
            status = "WR" if rec["written"] else "--"
            V_A_s  = fmt_short(rec["V_A"]) if rec["written"] else "—"
            V_B_s  = fmt_short(rec["V_B"]) if rec["written"] else "—"
            print(f"  {rec['sector']:>4}  {rec['byte_length']:>6}  "
                  f"{V_A_s:>14}  {V_B_s:>14}  {status}")
        print(f"{self._BORDER}\n")

    def save(self, path: str):
        image = {
            "lattice_drive_version": 2,
            "sector_size":  self.sector_size,
            "n_sectors":    self.n_sectors,
            "chart_base":   self.chart_base,
            "mask_base":    self.mask_base,
            "num_digits":   self.num_digits,
            "head":         self._head,
            "write_count":  self._write_count,
            "read_count":   self._read_count,
            "sectors": [
                {"sector":      s["sector"],
                 "byte_length": s["byte_length"],
                 "V_A":         str(s["V_A"]),
                 "V_B":         str(s["V_B"]),
                 "written":     s["written"]}
                for s in self._sectors
            ],
        }
        with open(path, "w") as f:
            json.dump(image, f, indent=2)
        print(f"[LATTICE-DRIVE] Image saved → {path}")

    def load(self, path: str) -> "LatticeDrive":
        with open(path, "r") as f:
            image = json.load(f)
        self.sector_size  = image["sector_size"]
        self.n_sectors    = image["n_sectors"]
        self.chart_base   = image["chart_base"]
        self.mask_base    = image["mask_base"]
        self.num_digits   = image["num_digits"]
        self._head        = image["head"]
        self._write_count = image["write_count"]
        self._read_count  = image["read_count"]
        self._cg_a = ChartGenerator(self.chart_base, self.mask_base, self.num_digits)
        self._cg_b = ChartGenerator(self.chart_base, self.mask_base, self.num_digits)
        self._sectors = [
            {"sector":      s["sector"],
             "byte_length": s["byte_length"],
             "V_A":         int(s["V_A"]),
             "V_B":         int(s["V_B"]),
             "written":     s["written"]}
            for s in image["sectors"]
        ]
        print(f"[LATTICE-DRIVE] Image loaded ← {path}")
        return self


# ===========================================================================
# LATTICE FILESYSTEM  (LatticeFS)
# ===========================================================================

class LatticeFS:
    """
    Lightweight filesystem on top of LatticeDrive.

    Sector 0 is the superblock — a JSON index of all files + URL registry.
    Data sectors start at sector 1 and grow upward.

    NEW in this version:
      - AES-256-GCM superblock encryption (optional, backward-compatible)
      - burris:// URL registry (register_url / resolve_url)
      - set_encryption_key(passphrase) — enable/rotate encryption at runtime

    Superblock schema:
      {
        "lattice_fs_version": 2,
        "next_free": int,
        "files": { filename: {start, n_sectors, byte_length} },
        "urls":  { url_path:  {coordinate, metadata} }
      }
    """

    _SUPERBLOCK_SECTOR = 0
    _FS_VERSION        = 2

    def __init__(self, drive: LatticeDrive, passphrase: str = None):
        if drive.n_sectors < 2:
            raise ValueError("LatticeFS requires a drive with at least 2 sectors.")
        self._drive:       LatticeDrive = drive
        self._index:       dict         = {}
        self._url_index:   dict         = {}
        self._next_free:   int          = 1
        self._passphrase:  str | None   = passphrase
        self._load_index()

    # -----------------------------------------------------------------------
    # Encryption helpers
    # -----------------------------------------------------------------------

    def set_encryption_key(self, passphrase: str):
        self._passphrase = passphrase
        self._flush_index()
        if passphrase:
            print(f"[LatticeFS] Superblock encryption ENABLED (AES-256-GCM).")
        else:
            print(f"[LatticeFS] Superblock encryption DISABLED.")

    # -----------------------------------------------------------------------
    # Superblock I/O
    # -----------------------------------------------------------------------

    def _flush_index(self):
        payload = json.dumps({
            "lattice_fs_version": self._FS_VERSION,
            "next_free":          self._next_free,
            "files":              self._index,
            "urls":               self._url_index,
        }).encode("utf-8")

        if self._passphrase:
            if not CRYPTO_AVAILABLE:
                print("⚠  cryptography not available — writing plaintext superblock.")
            else:
                payload = LatticeFSEncrypted.encrypt(payload, self._passphrase)

        if len(payload) > self._drive.sector_size:
            new_size = len(payload) + 64
            print(f"[LatticeFS] Superblock auto-grew: {len(payload)} bytes "
                  f"(was {self._drive.sector_size}; now {new_size}).")
            self._drive.sector_size = new_size
        self._drive.write(payload, self._SUPERBLOCK_SECTOR)
        self._drive._sectors[self._SUPERBLOCK_SECTOR]["byte_length"] = len(payload)

    def _load_index(self):
        if not self._drive._sectors[self._SUPERBLOCK_SECTOR]["written"]:
            self._index     = {}
            self._url_index = {}
            self._next_free = 1
            self._flush_index()
            return

        raw = self._drive.read(self._SUPERBLOCK_SECTOR)
        raw = raw.rstrip(b"\x00")

        if LatticeFSEncrypted.is_encrypted(raw):
            if self._passphrase is None:
                self._passphrase = input(
                    "[LatticeFS] Superblock is encrypted. Enter passphrase: "
                ).strip()
            raw = LatticeFSEncrypted.decrypt(raw, self._passphrase)

        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"LatticeFS: superblock corrupt: {exc}") from exc

        self._index     = data.get("files",      {})
        self._url_index = data.get("urls",        {})
        self._next_free = data.get("next_free",   1)

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _sectors_needed(self, byte_length: int) -> int:
        ss = self._drive.sector_size
        return (byte_length + ss - 1) // ss

    def _assert_exists(self, filename: str):
        if filename not in self._index:
            raise FileNotFoundError(
                f"LatticeFS: '{filename}' not found. "
                f"Files: {list(self._index.keys())}")

    # -----------------------------------------------------------------------
    # File API
    # -----------------------------------------------------------------------

    def write_file(self, filename: str, data: bytes) -> dict:
        n_sec = self._sectors_needed(len(data))

        if filename in self._index:
            entry = self._index[filename]
            if n_sec <= entry["n_sectors"]:
                start, offset = entry["start"], 0
                for i in range(n_sec):
                    chunk = data[offset : offset + self._drive.sector_size]
                    self._drive.write(chunk, start + i)
                    offset += self._drive.sector_size
                entry.update({"n_sectors": n_sec, "byte_length": len(data)})
                self._index[filename] = entry
                self._flush_index()
                print(f"[LatticeFS] OVERWRITE '{filename}'  {len(data)} bytes  "
                      f"sectors {start}–{start + n_sec - 1}")
                return entry

        start = self._next_free
        end   = start + n_sec - 1
        if end >= self._drive.n_sectors:
            raise IndexError(
                f"LatticeFS: Not enough free sectors for '{filename}' "
                f"(need {n_sec}, drive free from sector {self._next_free}).")

        offset = 0
        for i in range(n_sec):
            chunk = data[offset : offset + self._drive.sector_size]
            self._drive.write(chunk, start + i)
            offset += self._drive.sector_size

        entry = {"start": start, "n_sectors": n_sec, "byte_length": len(data)}
        self._index[filename] = entry
        self._next_free      += n_sec
        self._flush_index()
        print(f"[LatticeFS] WRITE '{filename}'  {len(data)} bytes  sectors {start}–{end}")
        return entry

    def read_file(self, filename: str) -> bytes:
        self._assert_exists(filename)
        entry  = self._index[filename]
        raw    = bytearray()
        for i in range(entry["n_sectors"]):
            raw += self._drive.read(entry["start"] + i)
        result = bytes(raw[: entry["byte_length"]])
        print(f"[LatticeFS] READ '{filename}'  {entry['byte_length']} bytes  "
              f"sectors {entry['start']}–{entry['start'] + entry['n_sectors'] - 1}")
        return result

    def delete_file(self, filename: str):
        self._assert_exists(filename)
        entry = self._index.pop(filename)
        self._flush_index()
        print(f"[LatticeFS] DELETE '{filename}'  "
              f"(sectors {entry['start']}–{entry['start'] + entry['n_sectors'] - 1} abandoned)")

    def rename_file(self, old_name: str, new_name: str):
        self._assert_exists(old_name)
        if new_name in self._index:
            raise FileExistsError(f"LatticeFS: '{new_name}' already exists. Delete it first.")
        self._index[new_name] = self._index.pop(old_name)
        self._flush_index()
        print(f"[LatticeFS] RENAME '{old_name}'  →  '{new_name}'")

    def exists(self, filename: str) -> bool:
        return filename in self._index

    def stat(self, filename: str) -> dict:
        self._assert_exists(filename)
        return dict(self._index[filename])

    def ls(self):
        border = "─" * 62
        print(f"\n  {border}")
        print(f"  ⬡  LATTICE FILESYSTEM  —  {len(self._index)} file(s)  "
              f"{len(self._url_index)} URL(s)")
        enc_status = "🔒 ENCRYPTED" if self._passphrase else "🔓 plaintext"
        print(f"  Superblock: {enc_status}")
        print(f"  {border}")
        print(f"  {'NAME':<30}  {'BYTES':>8}  {'START':>6}  {'SECS':>5}")
        print(f"  {'─'*30}  {'─'*8}  {'─'*6}  {'─'*5}")
        if not self._index:
            print(f"  (no files)")
        for name, entry in sorted(self._index.items()):
            print(f"  {name:<30}  {entry['byte_length']:>8}  "
                  f"{entry['start']:>6}  {entry['n_sectors']:>5}")
        if self._url_index:
            print(f"  {border}")
            print(f"  {'URL PATH':<40}  COORDINATE (short)")
            print(f"  {'─'*40}  ──────────────")
            for url, rec in sorted(self._url_index.items()):
                coord_s = fmt_short(int(rec["coordinate"])) if rec.get("coordinate") else "?"
                print(f"  {url:<40}  {coord_s}")
        print(f"  {border}")
        used_data  = sum(e["n_sectors"] for e in self._index.values())
        total_data = self._drive.n_sectors - 1
        print(f"  Superblock : sector 0")
        print(f"  Data sectors used : {used_data} / {total_data}")
        print(f"  Next free sector  : {self._next_free}")
        print(f"  {border}\n")

    def compact(self) -> "LatticeFS":
        print(f"\n[LatticeFS] COMPACT — rebuilding drive image...")
        live = {name: self.read_file(name) for name in list(self._index)}
        self._drive.format()
        self._index     = {}
        self._url_index = self._url_index
        self._next_free = 1
        for name in sorted(live.keys()):
            self.write_file(name, live[name])
        print(f"[LatticeFS] COMPACT complete — {len(live)} files, "
              f"next free sector = {self._next_free}")
        return self

    # -----------------------------------------------------------------------
    # URL Registry  (burris:// namespace)
    # -----------------------------------------------------------------------

    def register_url(self, url_path: str, coordinate: int, metadata: dict = None) -> dict:
        entry = {
            "coordinate": str(coordinate),
            "registered": _now_str(),
            "metadata":   metadata or {},
        }
        self._url_index[url_path] = entry
        self._flush_index()
        print(f"[LatticeFS] REGISTER URL  '{url_path}'  →  {fmt_short(coordinate)}")
        return entry

    def resolve_url(self, url_path: str) -> dict | None:
        entry = self._url_index.get(url_path)
        if entry is None:
            print(f"[LatticeFS] RESOLVE URL  '{url_path}'  →  NOT FOUND")
            return None
        result = dict(entry)
        result["coordinate"] = int(result["coordinate"])
        print(f"[LatticeFS] RESOLVE URL  '{url_path}'  →  "
              f"{fmt_short(result['coordinate'])}")
        return result

    def unregister_url(self, url_path: str):
        if url_path not in self._url_index:
            print(f"[LatticeFS] UNREGISTER URL  '{url_path}'  →  NOT FOUND")
            return
        self._url_index.pop(url_path)
        self._flush_index()
        print(f"[LatticeFS] UNREGISTER URL  '{url_path}'  REMOVED")

    def list_urls(self):
        if not self._url_index:
            print("[LatticeFS] URL registry is empty.")
            return
        border = "─" * 70
        print(f"\n  {border}")
        print(f"  ⬡  BURRIS URL REGISTRY  —  {len(self._url_index)} entry/entries")
        print(f"  {border}")
        print(f"  {'URL PATH':<42}  {'COORD (short)':>14}  META")
        print(f"  {'─'*42}  {'─'*14}  ────")
        for url, rec in sorted(self._url_index.items()):
            coord_s = fmt_short(int(rec["coordinate"])) if rec.get("coordinate") else "?"
            meta_s  = ", ".join(f"{k}={v}" for k, v in rec.get("metadata", {}).items())
            print(f"  {url:<42}  {coord_s:>14}  {meta_s[:30]}")
        print(f"  {border}\n")


# ---------------------------------------------------------------------------
# Helper to get current time string (used in register_url)
# ---------------------------------------------------------------------------
from datetime import datetime as _datetime

def _now_str() -> str:
    return _datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ===========================================================================
# CONVENIENCE FACTORIES
# ===========================================================================

def lattice_drive(
    sector_size: int = 512,
    n_sectors:   int = 64,
    chart_base:  int = 256,
    mask_base:   int = 1_000_000_000_000,
    num_digits:  int = 100,
) -> LatticeDrive:
    ld = LatticeDrive(sector_size, n_sectors, chart_base, mask_base, num_digits)
    print(f"\n⬡  Lattice Drive initialised  —  "
          f"{n_sectors} × {sector_size}B sectors  "
          f"(base={chart_base}, digits={num_digits})\n")
    return ld


def lattice_fs(
    sector_size: int = 512,
    n_sectors:   int = 128,
    chart_base:  int = 256,
    mask_base:   int = 1_000_000_000_000,
    num_digits:  int = 100,
    passphrase:  str = None,
) -> LatticeFS:
    drive = LatticeDrive(sector_size, n_sectors, chart_base, mask_base, num_digits)
    print(f"\n⬡  Lattice Drive initialised  —  "
          f"{n_sectors} × {sector_size}B sectors  "
          f"(base={chart_base}, digits={num_digits})")
    fs = LatticeFS(drive, passphrase=passphrase)
    enc_note = " [ENCRYPTED]" if passphrase else ""
    print(f"⬡  LatticeFS mounted  —  superblock at sector 0{enc_note}\n")
    return fs


# ===========================================================================
# SELF-TESTS
# ===========================================================================

if __name__ == "__main__":
    import tempfile

    TEST_DATA = bytes(range(25))
    print("=" * 60)
    print("  TEST 1 — ChartGenerator UP/DOWN round-trips")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        src  = os.path.join(tmp, "input.bin")
        enc  = os.path.join(tmp, "encoded.json")
        dec  = os.path.join(tmp, "decoded.bin")
        with open(src, "wb") as f:
            f.write(TEST_DATA)

        # UP
        cg = ChartGenerator()
        cg.encode_file(src, enc)
        cg2 = ChartGenerator()
        cg2.decode_file(enc, dec)
        with open(dec, "rb") as f:
            result = f.read()
        print("✅  UP round-trip PASSED" if result == TEST_DATA else "❌  UP MISMATCH")

        # DOWN
        enc_d = os.path.join(tmp, "encoded_down.json")
        dec_d = os.path.join(tmp, "decoded_down.bin")
        cg3 = ChartGenerator()
        cg3.encode_file_down(src, enc_d)
        cg4 = ChartGenerator()
        cg4.decode_file_down(enc_d, dec_d)
        with open(dec_d, "rb") as f:
            result_down = f.read()
        print("✅  DOWN round-trip PASSED" if result_down == TEST_DATA else "❌  DOWN MISMATCH")

        # write_disk_image
        cg5 = ChartGenerator()
        cg5.encode_file(src, enc)
        V_coord = cg5.hm.to_int(cg5.Vs[0])
        disk_out = os.path.join(tmp, "disk.bin")
        cg6 = ChartGenerator()
        img = cg6.write_disk_image(V_coord, len(TEST_DATA), disk_out, "up")
        print("✅  write_disk_image PASSED" if img == TEST_DATA else "❌  disk image MISMATCH")

    print("\n" + "=" * 60)
    print("  TEST 1b — FoldStats + fold_r")
    print("=" * 60)
    cg_fold = ChartGenerator()
    cg_fold.FOLD_DRIFT_THRESHOLD = 0   # force fold on every check
    for b in range(50):
        cg_fold._encode_step(b % 256, 0)
    folded = cg_fold.fold_r(universe=0, verbose=True)
    print(f"✅  fold_r returned {folded}  (fold_count={cg_fold.fold_stats[0].fold_count})")
    cg_fold.fold_stats[0].print_summary(
        cg_fold.hm.to_int(cg_fold.Vs[0]),
        cg_fold.hm.to_int(cg_fold.Rs[0]),
    )

    print("\n" + "=" * 60)
    print("  TEST 2 — LatticeDrive round-trip")
    print("=" * 60)
    drive = lattice_drive(sector_size=64, n_sectors=8)
    msg   = b"Hello, Burris universe!"
    drive.write(msg, 0)
    r0 = drive.read(0)
    print("✅  Sector 0 read PASSED" if r0 == msg else f"❌  MISMATCH  got={r0!r}")

    print("\n" + "=" * 60)
    print("  TEST 3 — LatticeFS + Encryption + URL Registry")
    print("=" * 60)
    fs = lattice_fs(sector_size=512, n_sectors=32, passphrase="grok-nav-2026")

    fs.write_file("readme.txt",   b"Burris Numerical System - LatticeFS demo.")
    fs.write_file("data.bin",     bytes(range(100)))
    fs.write_file("greeting.txt", b"Hello from the informational universe!")

    fs.register_url("burris://odinnet.io/index",
                     123456789012345678,
                     {"mime_type": "text/html", "version": "1.0"})
    fs.register_url("burris://odinnet.io/about",
                     987654321098765432)

    fs.ls()
    fs.list_urls()

    for name, expected in [
        ("readme.txt",   b"Burris Numerical System - LatticeFS demo."),
        ("data.bin",     bytes(range(100))),
        ("greeting.txt", b"Hello from the informational universe!"),
    ]:
        got    = fs.read_file(name)
        status = "✅" if got == expected else "❌"
        print(f"{status}  read '{name}'")

    rec = fs.resolve_url("burris://odinnet.io/index")
    assert rec and rec["coordinate"] == 123456789012345678
    print("✅  resolve_url PASSED")

    with tempfile.TemporaryDirectory() as tmp2:
        img = os.path.join(tmp2, "fs_image.json")
        fs._drive.save(img)
        drive2 = LatticeDrive()
        drive2.load(img)
        fs2 = LatticeFS(drive2, passphrase="grok-nav-2026")
        for name, expected in [("readme.txt", b"Burris Numerical System - LatticeFS demo."),
                                ("data.bin",   bytes(range(100)))]:
            got    = fs2.read_file(name)
            status = "✅" if got == expected else "❌"
            print(f"{status}  post-reload '{name}'")
        rec2 = fs2.resolve_url("burris://odinnet.io/index")
        assert rec2 and rec2["coordinate"] == 123456789012345678
        print("✅  URL registry persisted through save/load PASSED")

    print("\n✅  All tests complete.")
