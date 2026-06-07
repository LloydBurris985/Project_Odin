"""
ChartGenerator - File encode/decode using chart-based arithmetic coding.
Burris Numerical System — Informational Universe Navigator.

Key design notes:
  - N is byte + 1.  Byte is N - 1.
  - abs(V - R) used throughout; since V >= R always, this equals V - R.
  - File encoded END-TO-FRONT (bytes read in reverse).
  - File decoded FRONT-TO-END (bytes written in forward order).
  - All arithmetic uses hand math (list-of-limbs, little-endian).
  - 100-digit mode kept for experimental/testing use.

Encode UP (per byte, no walk):
    V_new = V + (V - R) * (BASE - 1) + byte

Decode UP (direct inverse, no walk):
    num   = V + BASE - 1
    V_old = num // BASE
    byte  = num %  BASE
    restore V = V_old

Encode DOWN (per byte, counting downward):
    W        = R - V                 (distance below R; 0 at start)
    W_new    = W * BASE + byte
    V_new    = R - W_new

Decode DOWN (direct inverse, counting upward):
    W        = R - V
    byte     = W % BASE
    W_old    = W // BASE
    V_old    = R - W_old

Navigation extensions (Burris Navigational System):
  - Sublight travel: ± steps × X on left side (V direct), ± steps × X × BASE on right side
  - Hyperspace: encode random bytes forward (jump), decode returns to exact origin
  - change_r(new_r):       relocate the reference axis R
  - change_direction():    flip between UP and DOWN encoding modes
  - Galactic map:          display chart state as formatted large numbers (no sci notation)
"""

import json
import os
import random
import copy


# ---------------------------------------------------------------------------
# Large number formatter — no scientific notation, clean cosmic display
# ---------------------------------------------------------------------------

def fmt_large(n: int, max_digits: int = 30) -> str:
    """Format a large integer cleanly: commas, no sci notation, truncated with '…' if huge."""
    if n == 0:
        return "0"
    s = str(abs(n))
    sign = "-" if n < 0 else ""
    if len(s) > max_digits:
        head = s[:6]
        tail = s[-4:]
        exp  = len(s) - 1
        # Insert commas in head
        return f"{sign}{head}…{tail}  [10^{exp}]"
    # Insert commas
    out = []
    for i, ch in enumerate(reversed(s)):
        if i and i % 3 == 0:
            out.append(",")
        out.append(ch)
    return sign + "".join(reversed(out))


def fmt_short(n: int) -> str:
    """Short display: always show as X.XXe+YY style but clean, 4 sig figs."""
    if n == 0:
        return "0"
    s = str(abs(n))
    exp = len(s) - 1
    if exp < 6:
        return fmt_large(n)
    mantissa = s[0] + "." + s[1:5]
    return f"{mantissa}e+{exp:02d}"


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
        r = [0] * self.D
        carry = 0
        for i in range(self.D):
            total = (a[i] if i < len(a) else 0) + (b[i] if i < len(b) else 0) + carry
            r[i] = total % self.M
            carry = total // self.M
        if carry:
            raise OverflowError("add: carry beyond num_digits")
        return r

    def sub(self, a: list, b: list) -> list:
        """abs(a - b) — always non-negative."""
        c = self.cmp(a, b)
        if c == 0:
            return self.zero()
        big, small = (a, b) if c > 0 else (b, a)
        r = [0] * self.D
        borrow = 0
        for i in range(self.D):
            t = (big[i] if i < len(big) else 0) - (small[i] if i < len(small) else 0) - borrow
            if t < 0:
                t += self.M
                borrow = 1
            else:
                borrow = 0
            r[i] = t
        return r

    def mul_scalar(self, a: list, s: int) -> list:
        r = [0] * self.D
        carry = 0
        for i in range(self.D):
            total = (a[i] if i < len(a) else 0) * s + carry
            r[i] = total % self.M
            carry = total // self.M
        if carry:
            raise OverflowError("mul_scalar: overflow beyond num_digits")
        return r

    def div_scalar(self, a: list, s: int):
        """Return (quotient_limbs, remainder_int)."""
        q = [0] * self.D
        rem = 0
        for i in range(self.D - 1, -1, -1):
            cur = rem * self.M + (a[i] if i < len(a) else 0)
            q[i] = cur // s
            rem = cur % s
        return q, rem

    def serialize(self, a: list) -> list:
        return a[: self.D]

    def deserialize(self, data: list) -> list:
        r = list(data)
        while len(r) < self.D:
            r.append(0)
        return r[: self.D]


# ---------------------------------------------------------------------------
# ChartGenerator  (Burris Numerical System — Informational Universe)
# ---------------------------------------------------------------------------

class ChartGenerator:
    def __init__(
        self,
        chart_base: int = 256,
        mask_base: int = 1_000_000_000_000,
        num_digits: int = 100,
        num_n_streams: int = 12,
    ):
        self.chart_base    = chart_base
        self.mask_base     = mask_base
        self.num_digits    = num_digits
        self.num_n_streams = num_n_streams

        self.hm = HandMath(mask_base, num_digits)

        # Per-universe state — V starts at 1, R fixed at 1
        self.Vs = [self.hm.from_int(1) for _ in range(num_n_streams)]
        self.Rs = [self.hm.from_int(1) for _ in range(num_n_streams)]

        # Navigation state
        self.direction      = "up"          # "up" or "down"
        self.step_count     = 0             # total steps taken
        self.hyperspace_log = []            # list of hyperspace jump records
        self._saved_positions = {}          # named bookmarks

    # -----------------------------------------------------------------------
    # Utility: current universe index for navigation (always 0 by default)
    # -----------------------------------------------------------------------

    def _u(self) -> int:
        return 0

    # -----------------------------------------------------------------------
    # Navigation: Change R (reference axis)
    # -----------------------------------------------------------------------

    def change_r(self, new_r_int: int, universe: int = 0):
        """
        Relocate the reference axis R to new_r_int.
        Prints before/after positions.
        """
        hm = self.hm
        old_r = hm.to_int(self.Rs[universe])
        self.Rs[universe] = hm.from_int(new_r_int)
        print(f"\n[NAVIGATION] R axis relocated")
        print(f"  OLD R : {fmt_large(old_r)}")
        print(f"  NEW R : {fmt_large(new_r_int)}")
        return self

    # -----------------------------------------------------------------------
    # Navigation: Change direction (flip UP ↔ DOWN encoding mode)
    # -----------------------------------------------------------------------

    def change_direction(self, universe: int = 0):
        """
        Flip encoding direction between 'up' and 'down'.
        When switching to DOWN, resets Vs[u] to zero (W=0 accumulator).
        When switching to UP,   resets Vs[u] to Rs[u] (V starts at R).
        Prints transition.
        """
        old_dir = self.direction
        if self.direction == "up":
            self.direction = "down"
            self.Vs[universe] = self.hm.zero()
            print(f"\n[NAVIGATION] Direction: UP → DOWN  (W-accumulator reset to 0)")
        else:
            self.direction = "up"
            self.Vs[universe] = self.hm.deserialize(self.Rs[universe])
            print(f"\n[NAVIGATION] Direction: DOWN → UP  (V reset to R)")
        print(f"  Direction is now: {self.direction.upper()}")
        return self

    # -----------------------------------------------------------------------
    # Core encode / decode steps (UP)
    # -----------------------------------------------------------------------

    def _encode_step(self, byte_val: int, u: int = 0):
        hm   = self.hm
        BASE = self.chart_base
        V    = self.Vs[u]
        R    = self.Rs[u]
        diff  = hm.sub(V, R)
        scale = hm.mul_scalar(diff, BASE - 1)
        dist  = hm.add(scale, hm.from_int(byte_val))
        self.Vs[u] = hm.add(V, dist)
        self.step_count += 1

    def _decode_step(self, u: int = 0) -> int:
        hm   = self.hm
        BASE = self.chart_base
        V    = self.Vs[u]
        num        = hm.add(V, hm.from_int(BASE - 1))
        V_old, rem = hm.div_scalar(num, BASE)
        self.Vs[u] = V_old
        self.step_count += 1
        return rem

    # -----------------------------------------------------------------------
    # Core encode / decode steps (DOWN)
    # -----------------------------------------------------------------------

    def _encode_down_step(self, byte_val: int, u: int = 0):
        hm   = self.hm
        BASE = self.chart_base
        W    = self.Vs[u]
        W_new = hm.add(hm.mul_scalar(W, BASE), hm.from_int(byte_val))
        self.Vs[u] = W_new
        self.step_count += 1

    def _decode_down_step(self, u: int = 0) -> int:
        hm   = self.hm
        BASE = self.chart_base
        W    = self.Vs[u]
        W_old, rem = hm.div_scalar(W, BASE)
        self.Vs[u] = W_old
        self.step_count += 1
        return rem

    # -----------------------------------------------------------------------
    # SUBLIGHT TRAVEL
    #
    # Left side  (V direct):  move ± steps × X
    # Right side (V scaled):  move ± steps × X × BASE
    #
    # steps: how many sublight jumps
    # x:     multiplier (the "X" in the formula)
    # side:  "left"  →  delta = steps * x
    #        "right" →  delta = steps * x * BASE
    # sign:  +1 or -1
    # -----------------------------------------------------------------------

    def sublight(self, steps: int, x: int, side: str = "left", sign: int = 1, universe: int = 0):
        """
        Sublight travel.
          side="left"  →  ΔV = sign × steps × X
          side="right" →  ΔV = sign × steps × X × BASE

        Applies directly to V (UP mode) or W (DOWN mode) in Vs[universe].
        Returns the delta applied.
        """
        hm   = self.hm
        BASE = self.chart_base

        if side == "right":
            delta_int = steps * x * BASE
        else:
            delta_int = steps * x

        delta = hm.from_int(abs(delta_int))
        V_old = hm.to_int(self.Vs[universe])

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
    # HYPERSPACE TRAVEL
    #
    # Jump: encode n_bytes random bytes forward (push V outward)
    # Return: decode exactly those bytes to return to exact origin
    # State is saved before jump and verified on return.
    # -----------------------------------------------------------------------

    def hyperspace_jump(self, n_bytes: int = 8, universe: int = 0, label: str = None):
        """
        Hyperspace jump: encode n_bytes random bytes (direction-aware).
        Saves origin state and jump payload so hyperspace_return() can undo it exactly.
        Returns jump ID string.
        """
        hm = self.hm

        # Save origin
        origin_V = hm.serialize(self.Vs[universe])
        origin_R = hm.serialize(self.Rs[universe])
        origin_dir = self.direction

        # Generate random payload
        payload = [random.randint(0, self.chart_base - 1) for _ in range(n_bytes)]

        # Encode forward (reversed, like file encode)
        for b in reversed(payload):
            if self.direction == "up":
                self._encode_step(b, universe)
            else:
                self._encode_down_step(b, universe)

        V_after = hm.to_int(self.Vs[universe])
        V_before = hm.to_int(hm.deserialize(origin_V))

        jump_id = f"JUMP_{len(self.hyperspace_log):04d}"
        if label:
            jump_id = label

        record = {
            "jump_id":    jump_id,
            "n_bytes":    n_bytes,
            "payload":    payload,
            "origin_V":   origin_V,
            "origin_R":   origin_R,
            "origin_dir": origin_dir,
            "V_before":   V_before,
            "V_after":    V_after,
            "direction":  self.direction,
            "universe":   universe,
        }
        self.hyperspace_log.append(record)

        print(f"\n[HYPERSPACE JUMP] {jump_id}  ({n_bytes} bytes, dir={self.direction.upper()})")
        print(f"  Origin : {fmt_short(V_before)}")
        print(f"  Arrived: {fmt_short(V_after)}")
        print(f"  Distance travelled: {fmt_short(abs(V_after - V_before))}")
        return jump_id

    def hyperspace_return(self, jump_id: str = None, universe: int = 0):
        """
        Decode the hyperspace payload to return to exact origin coordinates.
        If jump_id is None, undoes the most recent jump.
        Returns True on successful return, False on mismatch.
        """
        hm = self.hm

        if not self.hyperspace_log:
            print("[HYPERSPACE RETURN] No jumps on log.")
            return False

        if jump_id is None:
            record = self.hyperspace_log[-1]
        else:
            matches = [r for r in self.hyperspace_log if r["jump_id"] == jump_id]
            if not matches:
                print(f"[HYPERSPACE RETURN] Jump ID '{jump_id}' not found.")
                return False
            record = matches[-1]

        payload   = record["payload"]
        origin_V  = record["origin_V"]
        n_bytes   = record["n_bytes"]
        direction = record["direction"]

        V_current = hm.to_int(self.Vs[universe])

        # Decode forward (same order as file decode: front→back)
        recovered = []
        for _ in range(n_bytes):
            if direction == "up":
                recovered.append(self._decode_step(universe))
            else:
                recovered.append(self._decode_down_step(universe))

        V_restored = hm.to_int(self.Vs[universe])
        V_origin   = hm.to_int(hm.deserialize(origin_V))

        match = (recovered == payload) and (self.Vs[universe] == hm.deserialize(origin_V))

        print(f"\n[HYPERSPACE RETURN] ← {record['jump_id']}")
        print(f"  Departed : {fmt_short(V_current)}")
        print(f"  Returned : {fmt_short(V_restored)}")
        print(f"  Expected : {fmt_short(V_origin)}")
        print(f"  Payload match: {'✅ YES' if recovered == payload else '❌ NO'}")
        print(f"  Position match: {'✅ YES' if V_restored == V_origin else '❌ NO'}")

        return match

    # -----------------------------------------------------------------------
    # SAVE / BOOKMARK POSITION
    # -----------------------------------------------------------------------

    def save_position(self, name: str = "bookmark", universe: int = 0):
        """Save current chart state as a named bookmark."""
        hm = self.hm
        self._saved_positions[name] = {
            "V":        hm.serialize(self.Vs[universe]),
            "R":        hm.serialize(self.Rs[universe]),
            "direction": self.direction,
            "steps":    self.step_count,
            "universe": universe,
        }
        V_int = hm.to_int(self.Vs[universe])
        print(f"\n[BOOKMARK] '{name}' saved  V={fmt_short(V_int)}  dir={self.direction.upper()}")
        return self

    def load_position(self, name: str = "bookmark", universe: int = 0):
        """Restore chart state from a named bookmark."""
        hm = self.hm
        if name not in self._saved_positions:
            print(f"[BOOKMARK] '{name}' not found.")
            return False
        rec = self._saved_positions[name]
        self.Vs[universe] = hm.deserialize(rec["V"])
        self.Rs[universe] = hm.deserialize(rec["R"])
        self.direction = rec["direction"]
        V_int = hm.to_int(self.Vs[universe])
        print(f"\n[BOOKMARK] '{name}' restored  V={fmt_short(V_int)}  dir={self.direction.upper()}")
        return True

    def list_bookmarks(self):
        """List all saved positions."""
        if not self._saved_positions:
            print("[BOOKMARKS] None saved.")
            return
        print(f"\n{'═'*50}")
        print(f"  SAVED POSITIONS ({len(self._saved_positions)})")
        print(f"{'═'*50}")
        for name, rec in self._saved_positions.items():
            hm = self.hm
            V_int = hm.to_int(hm.deserialize(rec["V"]))
            R_int = hm.to_int(hm.deserialize(rec["R"]))
            print(f"  [{name}]")
            print(f"    V={fmt_short(V_int)}  R={fmt_short(R_int)}  dir={rec['direction'].upper()}  steps={rec['steps']}")
        print(f"{'═'*50}")

    # -----------------------------------------------------------------------
    # GALACTIC MAP — display chart state
    # -----------------------------------------------------------------------

    def galactic_map(self, universe: int = 0, label: str = ""):
        """
        Print a galactic map of the current chart state.
        Displays V, R, distance V-R, direction, step count.
        All numbers formatted without scientific notation (clean large-number display).
        """
        hm   = self.hm
        BASE = self.chart_base

        V_int = hm.to_int(self.Vs[universe])
        R_int = hm.to_int(self.Rs[universe])

        dist_int = abs(V_int - R_int)
        V_str    = str(V_int)
        V_len    = len(V_str)

        # Compute digit positions
        left_digits  = V_str
        right_digits = str(V_int * BASE)

        border = "═" * 64
        print(f"\n{border}")
        title = "✦  BURRIS NAVIGATIONAL SYSTEM — GALACTIC MAP  ✦"
        if label:
            title += f"  [{label}]"
        print(f"  {title}")
        print(f"{border}")
        print(f"  UNIVERSE   : {universe}   |  DIRECTION : {self.direction.upper()}   |  BASE : {BASE}")
        print(f"  TOTAL STEPS: {self.step_count:,}")
        print(f"{'-'*64}")
        print(f"  V (position)  : {fmt_large(V_int)}")
        print(f"  V (short)     : {fmt_short(V_int)}")
        print(f"  V (digit len) : {V_len} digits")
        print(f"{'-'*64}")
        print(f"  R (reference) : {fmt_large(R_int)}")
        print(f"  R (short)     : {fmt_short(R_int)}")
        print(f"{'-'*64}")
        print(f"  |V - R|       : {fmt_large(dist_int)}")
        print(f"  |V - R| short : {fmt_short(dist_int)}")
        print(f"{'-'*64}")
        print(f"  LEFT  side V  : {fmt_short(V_int)}")
        print(f"  RIGHT side V  : {fmt_short(V_int * BASE)}  (V × BASE)")
        print(f"{'-'*64}")

        # Hyperspace log summary
        if self.hyperspace_log:
            print(f"  HYPERSPACE LOG  ({len(self.hyperspace_log)} jumps)")
            for rec in self.hyperspace_log[-3:]:  # last 3
                print(f"    {rec['jump_id']}  {fmt_short(rec['V_before'])} → {fmt_short(rec['V_after'])}")
        else:
            print(f"  HYPERSPACE LOG  : empty")

        # Bookmarks
        if self._saved_positions:
            print(f"{'-'*64}")
            print(f"  BOOKMARKS  ({len(self._saved_positions)})")
            for name, rec in self._saved_positions.items():
                bV = hm.to_int(hm.deserialize(rec["V"]))
                print(f"    ★ {name:16s}  V={fmt_short(bV)}  dir={rec['direction'].upper()}")

        print(f"{border}\n")

    # -----------------------------------------------------------------------
    # NAVIGATION MENU (interactive CLI starship console)
    # -----------------------------------------------------------------------

    def navigation_menu(self, universe: int = 0):
        """
        Interactive starship navigation console for the Burris Informational Universe.
        """
        self.save_position("ORIGIN", universe)
        print("\n" + "★" * 64)
        print("  BURRIS INFORMATIONAL UNIVERSE — STARSHIP NAVIGATION CONSOLE")
        print("★" * 64)
        print("  Welcome aboard. Your ship is positioned at chart origin.")
        print("  Type 'help' for commands.\n")

        self.galactic_map(universe, "CURRENT POSITION")

        cmd_help = """
╔══════════════════════════════════════════════════════════════╗
║               NAVIGATION COMMANDS                            ║
╠══════════════════════════════════════════════════════════════╣
║  map                   Show galactic map (current position)  ║
║  sublight [+/-][n] [x] [left/right]                         ║
║                        Sublight travel                       ║
║                        n=steps, x=multiplier, side          ║
║                        Example: sublight +3 1 left           ║
║  hyperspace [n]        Jump: encode n random bytes (def=8)   ║
║  return [id]           Return from last hyperspace jump      ║
║  change_r [n]          Relocate reference axis R to n        ║
║  flip                  Flip encoding direction UP ↔ DOWN     ║
║  save [name]           Save current position as bookmark     ║
║  load [name]           Restore position from bookmark        ║
║  bookmarks             List all saved positions              ║
║  encode [n]            Encode n bytes (UP step × n)          ║
║  decode [n]            Decode n bytes (UP step × n)          ║
║  reset                 Return to ORIGIN bookmark             ║
║  help                  Show this menu                        ║
║  quit / exit           Leave navigation console              ║
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
                lbl = " ".join(parts[1:]) if len(parts) > 1 else ""
                self.galactic_map(universe, lbl)

            elif cmd == "sublight":
                # sublight +3 1 left   OR   sublight -5 2 right
                try:
                    raw_steps = parts[1] if len(parts) > 1 else "+1"
                    sign_char = -1 if raw_steps.startswith("-") else +1
                    steps     = int(raw_steps.lstrip("+-")) if len(raw_steps) > 1 else 1
                    x         = int(parts[2]) if len(parts) > 2 else 1
                    side      = parts[3].lower() if len(parts) > 3 else "left"
                    self.sublight(steps, x, side=side, sign=sign_char, universe=universe)
                    self.galactic_map(universe, "AFTER SUBLIGHT")
                except (ValueError, IndexError) as e:
                    print(f"  [ERR] sublight parse error: {e}")
                    print("  Usage: sublight +3 1 left")

            elif cmd == "hyperspace":
                n = int(parts[1]) if len(parts) > 1 else 8
                lbl = parts[2] if len(parts) > 2 else None
                self.hyperspace_jump(n, universe=universe, label=lbl)
                self.galactic_map(universe, "AFTER HYPERSPACE JUMP")

            elif cmd == "return":
                jid = parts[1] if len(parts) > 1 else None
                ok  = self.hyperspace_return(jid, universe=universe)
                self.galactic_map(universe, "AFTER HYPERSPACE RETURN")
                if ok:
                    print("  ✅ Successfully returned to origin coordinates.")
                else:
                    print("  ❌ Return mismatch — check log.")

            elif cmd == "change_r":
                try:
                    new_r = int(parts[1]) if len(parts) > 1 else 1
                    self.change_r(new_r, universe)
                    self.galactic_map(universe, "AFTER CHANGE R")
                except ValueError:
                    print("  [ERR] change_r requires an integer. Example: change_r 100")

            elif cmd == "flip":
                self.change_direction(universe)
                self.galactic_map(universe, "AFTER DIRECTION FLIP")

            elif cmd == "save":
                name = parts[1] if len(parts) > 1 else "quicksave"
                self.save_position(name, universe)

            elif cmd == "load":
                name = parts[1] if len(parts) > 1 else "quicksave"
                self.load_position(name, universe)
                self.galactic_map(universe, f"LOADED: {name}")

            elif cmd == "bookmarks":
                self.list_bookmarks()

            elif cmd == "encode":
                n = int(parts[1]) if len(parts) > 1 else 1
                print(f"\n[ENCODE] {n} steps (byte=1 each)")
                for _ in range(n):
                    if self.direction == "up":
                        self._encode_step(1, universe)
                    else:
                        self._encode_down_step(1, universe)
                self.galactic_map(universe, f"AFTER {n} ENCODE STEPS")

            elif cmd == "decode":
                n = int(parts[1]) if len(parts) > 1 else 1
                print(f"\n[DECODE] {n} steps")
                results = []
                for _ in range(n):
                    if self.direction == "up":
                        results.append(self._decode_step(universe))
                    else:
                        results.append(self._decode_down_step(universe))
                print(f"  Decoded bytes: {results}")
                self.galactic_map(universe, f"AFTER {n} DECODE STEPS")

            elif cmd == "reset":
                ok = self.load_position("ORIGIN", universe)
                if ok:
                    self.galactic_map(universe, "RESET TO ORIGIN")
                else:
                    print("  [ERR] No ORIGIN bookmark found.")

            else:
                print(f"  [?] Unknown command: '{cmd}'.  Type 'help' for commands.")

    # -----------------------------------------------------------------------
    # File encode / decode  (UP direction)
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
        self.Vs[u] = self.hm.deserialize(state["V"])
        self.Rs[u] = self.hm.deserialize(state["R"])
        print(f"=== Decoding Phase (UP) === ({file_length} bytes)")
        recovered = []
        for _ in range(file_length):
            recovered.append(self._decode_step(u))
        with open(output_path, "wb") as f:
            f.write(bytes(recovered))
        print(f"Decoding PASSED 😁  →  {output_path}")

    # -----------------------------------------------------------------------
    # File encode / decode  (DOWN direction)
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
        self.Vs[u] = self.hm.deserialize(state["V"])
        self.Rs[u] = self.hm.deserialize(state["R"])
        print(f"=== Decoding Phase (DOWN) === ({file_length} bytes)")
        recovered = []
        for _ in range(file_length):
            recovered.append(self._decode_down_step(u))
        with open(output_path, "wb") as f:
            f.write(bytes(recovered))
        print(f"Decoding DOWN PASSED 😁  →  {output_path}")

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
        hm = self.hm
        V_int = hm.to_int(self.Vs[universe_idx])
        R_int = hm.to_int(self.Rs[universe_idx])
        print(
            f"Universe {universe_idx}: "
            f"V={fmt_large(V_int)}  "
            f"R={fmt_large(R_int)}  "
            f"dir={self.direction.upper()}"
        )


# ---------------------------------------------------------------------------
# 25-byte round-trip test  (run as main)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    TEST_DATA = bytes(range(25))
    print("Test bytes:", list(TEST_DATA))

    with tempfile.TemporaryDirectory() as tmp:
        src  = os.path.join(tmp, "input.bin")
        enc  = os.path.join(tmp, "encoded.json")
        dec  = os.path.join(tmp, "decoded.bin")

        with open(src, "wb") as f:
            f.write(TEST_DATA)

        # --- UP direction ---
        print("\n--- UP direction ---")
        cg = ChartGenerator()
        cg.encode_file(src, enc)
        cg.print_state()
        cg2 = ChartGenerator()
        cg2.decode_file(enc, dec)
        with open(dec, "rb") as f:
            result = f.read()
        if result == TEST_DATA:
            print("✅  UP 25-byte round-trip PASSED — input == output")
        else:
            print("❌  UP MISMATCH")

        # --- DOWN direction ---
        enc_down = os.path.join(tmp, "encoded_down.json")
        dec_down = os.path.join(tmp, "decoded_down.bin")
        print("\n--- DOWN direction ---")
        cg3 = ChartGenerator()
        cg3.encode_file_down(src, enc_down)
        cg3.print_state()
        cg4 = ChartGenerator()
        cg4.decode_file_down(enc_down, dec_down)
        with open(dec_down, "rb") as f:
            result_down = f.read()
        if result_down == TEST_DATA:
            print("✅  DOWN 25-byte round-trip PASSED — input == output")
        else:
            print("❌  DOWN MISMATCH")
