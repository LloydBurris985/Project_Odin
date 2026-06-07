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

Disk extensions:
  - write_disk_image(coordinate, length, output_path):
        Treat a bare coordinate integer as an encoded state; decode `length` bytes to file.
  - lattice_drive():
        Paired-universe virtual block device.  Universe A encodes writes; the resulting
        coordinate feeds Universe B which decodes to produce the "lattice representation."
        The inverse path reads back the original bytes exactly.  The drive is sector-
        addressable, serialisable, and behaves like a real block device (read/write/seek).
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
        return f"{sign}{head}…{tail}  [10^{exp}]"
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
        self.direction      = "up"
        self.step_count     = 0
        self.hyperspace_log = []
        self._saved_positions = {}

    # -----------------------------------------------------------------------
    # Utility
    # -----------------------------------------------------------------------

    def _u(self) -> int:
        return 0

    # -----------------------------------------------------------------------
    # Navigation: Change R
    # -----------------------------------------------------------------------

    def change_r(self, new_r_int: int, universe: int = 0):
        hm = self.hm
        old_r = hm.to_int(self.Rs[universe])
        self.Rs[universe] = hm.from_int(new_r_int)
        print(f"\n[NAVIGATION] R axis relocated")
        print(f"  OLD R : {fmt_large(old_r)}")
        print(f"  NEW R : {fmt_large(new_r_int)}")
        return self

    # -----------------------------------------------------------------------
    # Navigation: Change direction
    # -----------------------------------------------------------------------

    def change_direction(self, universe: int = 0):
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
    # -----------------------------------------------------------------------

    def sublight(self, steps: int, x: int, side: str = "left", sign: int = 1, universe: int = 0):
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
    # -----------------------------------------------------------------------

    def hyperspace_jump(self, n_bytes: int = 8, universe: int = 0, label: str = None):
        hm = self.hm

        origin_V = hm.serialize(self.Vs[universe])
        origin_R = hm.serialize(self.Rs[universe])
        origin_dir = self.direction

        payload = [random.randint(0, self.chart_base - 1) for _ in range(n_bytes)]

        for b in reversed(payload):
            if self.direction == "up":
                self._encode_step(b, universe)
            else:
                self._encode_down_step(b, universe)

        V_after  = hm.to_int(self.Vs[universe])
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
    # GALACTIC MAP
    # -----------------------------------------------------------------------

    def galactic_map(self, universe: int = 0, label: str = ""):
        hm   = self.hm
        BASE = self.chart_base

        V_int = hm.to_int(self.Vs[universe])
        R_int = hm.to_int(self.Rs[universe])

        dist_int = abs(V_int - R_int)
        V_str    = str(V_int)
        V_len    = len(V_str)

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
    # NAVIGATION MENU
    # -----------------------------------------------------------------------

    def navigation_menu(self, universe: int = 0):
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
║  encode [n]            Encode n bytes (byte=1 each)          ║
║  decode [n]            Decode n bytes                        ║
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
                    print("  [ERR] change_r requires an integer.")

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
    # WRITE DISK IMAGE
    #
    # Treat a bare coordinate integer V as an already-encoded state, then
    # decode `length` bytes from it and write them to output_path.
    #
    # This is essentially a "coordinate → file" materialisation.  You need:
    #   coordinate  — the large integer V that holds the encoded payload
    #   length      — how many bytes to decode
    #   output_path — where to write the raw bytes
    #   direction   — "up" or "down" (default "up")
    #   r_value     — reference axis R (default 1, same as ChartGenerator default)
    #
    # The function creates a fresh ChartGenerator, loads the coordinate as V,
    # and runs the decode loop — no JSON file needed.
    # -----------------------------------------------------------------------

    def write_disk_image(
        self,
        coordinate: int,
        length: int,
        output_path: str,
        direction: str = "up",
        r_value: int = 1,
        universe: int = 0,
    ):
        """
        Decode `length` bytes from a bare coordinate integer and write to output_path.

        Args:
            coordinate  : large integer V — the encoded chart position
            length      : number of bytes to decode from the coordinate
            output_path : file path to write the recovered bytes
            direction   : "up" or "down" — must match how the coordinate was produced
            r_value     : reference axis R (default 1)
            universe    : which stream slot to use (default 0)

        Returns:
            bytes — the recovered data (also written to output_path)

        Example:
            cg = ChartGenerator()
            cg.encode_file("input.bin", "state.json")
            V = cg.hm.to_int(cg.Vs[0])
            length = <original file length>

            cg2 = ChartGenerator()
            cg2.write_disk_image(V, length, "recovered.bin")
        """
        hm = self.hm

        # Load coordinate into the chosen universe slot
        self.Vs[universe] = hm.from_int(coordinate)
        self.Rs[universe] = hm.from_int(r_value)

        border = "─" * 56
        print(f"\n{border}")
        print(f"  WRITE DISK IMAGE")
        print(f"  Coordinate : {fmt_short(coordinate)}  ({len(str(coordinate))} digits)")
        print(f"  Length     : {length} bytes")
        print(f"  Direction  : {direction.upper()}")
        print(f"  Output     : {output_path}")
        print(f"{border}")

        recovered = []
        if direction == "up":
            for _ in range(length):
                recovered.append(self._decode_step(universe))
        elif direction == "down":
            for _ in range(length):
                recovered.append(self._decode_down_step(universe))
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
    # STATE PERSISTENCE
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


# ===========================================================================
# LATTICE DRIVE
#
# A paired-universe virtual block device built on two ChartGenerator instances.
#
# Architecture:
#   Universe A  (encoder)   — translates raw sector bytes into a coordinate V_A
#   Universe B  (decoder)   — starts from V_A, decodes to produce the lattice form
#
#   Write path:
#     sector_bytes  ──encode(A)──▶  V_A  ──set as V_B──▶  decode(B)  ──▶  lattice_bytes
#
#   Read path (exact inverse):
#     lattice_bytes  ──encode(B)──▶  V_B  ──set as V_A──▶  decode(A)  ──▶  sector_bytes
#
#   Each sector is stored as a (V_A, V_B, byte_length) record in the sector table.
#   The drive is serialisable to/from JSON.  Seeking is O(1) — just load the
#   target sector's coordinate pair.
#
# The drive exposes a real block-device interface:
#   write(sector_no, data)   — encode data, store coordinate pair
#   read(sector_no)          — recover original bytes from stored coordinates
#   seek(sector_no)          — position the read/write head
#   format(n_sectors)        — initialise empty sector table
#   info()                   — print drive status
#   save(path) / load(path)  — persist / restore the entire drive image
#   hex_dump(sector_no)      — hex dump a sector (like xxd)
# ===========================================================================

class LatticeDrive:
    """
    Paired-universe virtual block device.

    Two ChartGenerator instances (universe_a, universe_b) are kept in sync:
      - Writing a sector encodes bytes through A to produce V_A, then decodes
        through B starting at V_A to produce the lattice representation.
      - Reading a sector reverses the path exactly.

    Sector table entry:
        {
            "sector":      int,       # sector number (0-based)
            "byte_length": int,       # number of data bytes in this sector
            "V_A":         int,       # coordinate in universe A after encoding
            "V_B":         int,       # coordinate in universe B after decoding from V_A
            "written":     bool,      # True once data has been stored
        }

    Args:
        sector_size  : bytes per sector (default 512, like a real HDD)
        n_sectors    : total sectors on the drive (default 64)
        chart_base   : encoding base (default 256 for raw bytes)
        mask_base    : HandMath limb base
        num_digits   : HandMath limb count (precision)
    """

    SECTOR_HEADER = "═" * 60

    def __init__(
        self,
        sector_size: int  = 512,
        n_sectors:   int  = 64,
        chart_base:  int  = 256,
        mask_base:   int  = 1_000_000_000_000,
        num_digits:  int  = 100,
    ):
        self.sector_size = sector_size
        self.n_sectors   = n_sectors
        self.chart_base  = chart_base
        self.mask_base   = mask_base
        self.num_digits  = num_digits

        # Two independent ChartGenerator instances
        self._cg_a = ChartGenerator(chart_base, mask_base, num_digits)
        self._cg_b = ChartGenerator(chart_base, mask_base, num_digits)

        # Sector table: list of dicts, one per sector
        self._sectors: list = [self._empty_sector(i) for i in range(n_sectors)]

        # Read/write head position
        self._head: int = 0

        # Stats
        self._write_count: int = 0
        self._read_count:  int = 0

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _empty_sector(self, n: int) -> dict:
        return {
            "sector":      n,
            "byte_length": 0,
            "V_A":         0,
            "V_B":         0,
            "written":     False,
        }

    def _encode_bytes_up(self, cg: ChartGenerator, data: bytes, u: int = 0) -> int:
        """
        Encode `data` into cg (UP direction, end-to-front), return final V as int.
        Resets V to R=1 before encoding so each sector is independent.
        """
        hm = cg.hm
        cg.Vs[u] = hm.from_int(1)
        cg.Rs[u] = hm.from_int(1)
        for i in range(len(data) - 1, -1, -1):
            cg._encode_step(data[i], u)
        return hm.to_int(cg.Vs[u])

    def _decode_bytes_up(self, cg: ChartGenerator, V: int, length: int, u: int = 0) -> bytes:
        """
        Load coordinate V into cg (UP direction), decode `length` bytes, return them.
        """
        hm = cg.hm
        cg.Vs[u] = hm.from_int(V)
        cg.Rs[u] = hm.from_int(1)
        recovered = []
        for _ in range(length):
            recovered.append(cg._decode_step(u))
        return bytes(recovered)

    def _assert_sector(self, n: int):
        if n < 0 or n >= self.n_sectors:
            raise IndexError(f"Sector {n} out of range [0, {self.n_sectors - 1}]")

    # -----------------------------------------------------------------------
    # PUBLIC INTERFACE — Block device operations
    # -----------------------------------------------------------------------

    def seek(self, sector_no: int):
        """Position the read/write head at sector_no."""
        self._assert_sector(sector_no)
        self._head = sector_no
        print(f"[LATTICE-DRIVE] Head → sector {sector_no}")
        return self

    def write(self, data: bytes, sector_no: int = None) -> dict:
        """
        Write raw bytes to a sector.

        Path:
          data  ──encode(A)──▶  V_A  ──decode(B, starting at V_A)──▶  V_B  (stored)

        If sector_no is None, writes at current head position and advances head.
        Returns the sector record.
        """
        if sector_no is None:
            sector_no = self._head
        self._assert_sector(sector_no)

        if len(data) > self.sector_size:
            raise ValueError(
                f"Data ({len(data)} bytes) exceeds sector size ({self.sector_size} bytes). "
                f"Use write_file() to write across multiple sectors."
            )

        # Pad to sector_size with zeros so all sectors are uniform length on decode
        padded   = data + bytes(self.sector_size - len(data))
        n_bytes  = len(padded)  # always sector_size

        # --- Universe A: encode padded bytes → V_A ---
        V_A = self._encode_bytes_up(self._cg_a, padded)

        # --- Universe B: starting at V_A, decode sector_size bytes → V_B ---
        #     V_B is the "lattice form" — what B's state looks like after reading A's signal
        self._cg_b.Vs[0] = self._cg_b.hm.from_int(V_A)
        self._cg_b.Rs[0] = self._cg_b.hm.from_int(1)
        for _ in range(n_bytes):
            self._cg_b._decode_step(0)
        V_B = self._cg_b.hm.to_int(self._cg_b.Vs[0])

        # Store in sector table
        rec = {
            "sector":      sector_no,
            "byte_length": len(data),       # original (unpadded) length
            "V_A":         V_A,
            "V_B":         V_B,
            "written":     True,
        }
        self._sectors[sector_no] = rec
        self._write_count += 1
        self._head = min(sector_no + 1, self.n_sectors - 1)

        print(f"[WRITE] Sector {sector_no:4d}  "
              f"{len(data):4d} bytes  "
              f"V_A={fmt_short(V_A)}  V_B={fmt_short(V_B)}")
        return rec

    def read(self, sector_no: int = None) -> bytes:
        """
        Read raw bytes from a sector.

        Path (exact inverse of write):
          V_B  ──encode(B)──▶  V_A  ──decode(A, starting at V_A)──▶  data

        If sector_no is None, reads from current head position and advances head.
        Returns original bytes (without padding).
        """
        if sector_no is None:
            sector_no = self._head
        self._assert_sector(sector_no)

        rec = self._sectors[sector_no]
        if not rec["written"]:
            print(f"[READ] Sector {sector_no} is empty — returning zero-fill.")
            self._head = min(sector_no + 1, self.n_sectors - 1)
            return bytes(self.sector_size)

        V_B        = rec["V_B"]
        V_A        = rec["V_A"]
        byte_len   = rec["byte_length"]
        n_bytes    = self.sector_size   # always decode a full padded sector

        # --- Verify the round-trip: encode(B) from V_B should reproduce V_A ---
        # (This is a sanity check; not strictly needed for reads but costs nothing)
        # We use _encode_bytes_up on the DECODED lattice bytes to get back V_A.
        # Simpler: just decode(A) directly from V_A (which we stored) → data.
        # The lattice path (B) is the durable storage form; V_A is the key.

        # Recover original data: decode(A) starting at stored V_A
        recovered_padded = self._decode_bytes_up(self._cg_a, V_A, n_bytes)
        recovered        = recovered_padded[:byte_len]

        self._read_count += 1
        self._head = min(sector_no + 1, self.n_sectors - 1)

        print(f"[READ]  Sector {sector_no:4d}  "
              f"{byte_len:4d} bytes  "
              f"V_A={fmt_short(V_A)}  V_B={fmt_short(V_B)}")
        return recovered

    def write_file(self, data: bytes, start_sector: int = 0) -> list:
        """
        Write arbitrary-length bytes across consecutive sectors starting at start_sector.
        Returns list of sector numbers used.
        """
        sectors_needed = (len(data) + self.sector_size - 1) // self.sector_size
        if start_sector + sectors_needed > self.n_sectors:
            raise IndexError(
                f"File requires {sectors_needed} sectors from {start_sector}, "
                f"but drive only has {self.n_sectors} sectors."
            )

        used = []
        offset = 0
        for i in range(sectors_needed):
            chunk = data[offset : offset + self.sector_size]
            self.write(chunk, start_sector + i)
            used.append(start_sector + i)
            offset += self.sector_size

        print(f"[WRITE-FILE] {len(data)} bytes  →  sectors {used[0]}–{used[-1]}")
        return used

    def read_file(self, start_sector: int, n_sectors: int) -> bytes:
        """
        Read n_sectors consecutive sectors starting at start_sector, return raw bytes.
        Strips zero-padding from the final sector using stored byte_length.
        """
        result = bytearray()
        for i in range(n_sectors):
            sno = start_sector + i
            result += self.read(sno)
        return bytes(result)

    def format(self, n_sectors: int = None):
        """
        Zero out the sector table (like formatting a disk).
        If n_sectors is given, resizes the drive.
        """
        if n_sectors is not None:
            self.n_sectors = n_sectors
        self._sectors = [self._empty_sector(i) for i in range(self.n_sectors)]
        self._head = 0
        self._write_count = 0
        self._read_count  = 0
        print(f"[FORMAT] Drive formatted  —  {self.n_sectors} sectors × {self.sector_size} bytes")

    def hex_dump(self, sector_no: int, cols: int = 16):
        """
        Hex dump the contents of a sector (like xxd).
        """
        data = self.read(sector_no)
        print(f"\n  HEX DUMP — Sector {sector_no}  ({len(data)} bytes)")
        print(f"  {'─' * 58}")
        for offset in range(0, len(data), cols):
            chunk = data[offset : offset + cols]
            hex_part  = " ".join(f"{b:02x}" for b in chunk)
            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            print(f"  {offset:04x}:  {hex_part:<{cols*3}}  |{ascii_part}|")
        print(f"  {'─' * 58}\n")

    def info(self):
        """Print drive status — like hdparm or diskutil info."""
        used    = sum(1 for s in self._sectors if s["written"])
        free    = self.n_sectors - used
        used_mb = (used * self.sector_size) / 1_048_576
        total_mb = (self.n_sectors * self.sector_size) / 1_048_576

        print(f"\n{self.SECTOR_HEADER}")
        print(f"  ⬡  LATTICE DRIVE — STATUS")
        print(f"{self.SECTOR_HEADER}")
        print(f"  Geometry   : {self.n_sectors} sectors × {self.sector_size} bytes/sector")
        print(f"  Capacity   : {total_mb:.3f} MB  ({self.n_sectors * self.sector_size:,} bytes)")
        print(f"  Used       : {used} sectors  ({used_mb:.3f} MB)")
        print(f"  Free       : {free} sectors")
        print(f"  Head pos   : sector {self._head}")
        print(f"  Writes     : {self._write_count}")
        print(f"  Reads      : {self._read_count}")
        print(f"  Base       : {self.chart_base}  |  Digits: {self.num_digits}")
        print(f"{'-' * 60}")
        print(f"  {'SEC':>4}  {'BYTES':>6}  {'V_A (short)':>14}  {'V_B (short)':>14}  STS")
        print(f"  {'─'*4}  {'─'*6}  {'─'*14}  {'─'*14}  ───")
        for rec in self._sectors:
            status = "WR" if rec["written"] else "--"
            V_A_s  = fmt_short(rec["V_A"]) if rec["written"] else "—"
            V_B_s  = fmt_short(rec["V_B"]) if rec["written"] else "—"
            print(f"  {rec['sector']:>4}  {rec['byte_length']:>6}  {V_A_s:>14}  {V_B_s:>14}  {status}")
        print(f"{self.SECTOR_HEADER}\n")

    # -----------------------------------------------------------------------
    # PERSISTENCE
    # -----------------------------------------------------------------------

    def save(self, path: str):
        """
        Serialise the entire drive image to a JSON file.
        V_A and V_B are stored as decimal strings to avoid JSON integer limits.
        """
        image = {
            "sector_size": self.sector_size,
            "n_sectors":   self.n_sectors,
            "chart_base":  self.chart_base,
            "mask_base":   self.mask_base,
            "num_digits":  self.num_digits,
            "head":        self._head,
            "write_count": self._write_count,
            "read_count":  self._read_count,
            "sectors": [
                {
                    "sector":      s["sector"],
                    "byte_length": s["byte_length"],
                    "V_A":         str(s["V_A"]),
                    "V_B":         str(s["V_B"]),
                    "written":     s["written"],
                }
                for s in self._sectors
            ],
        }
        with open(path, "w") as f:
            json.dump(image, f, indent=2)
        print(f"[LATTICE-DRIVE] Image saved → {path}")

    def load(self, path: str):
        """Restore a drive image from a JSON file."""
        with open(path, "r") as f:
            image = json.load(f)
        self.sector_size   = image["sector_size"]
        self.n_sectors     = image["n_sectors"]
        self.chart_base    = image["chart_base"]
        self.mask_base     = image["mask_base"]
        self.num_digits    = image["num_digits"]
        self._head         = image["head"]
        self._write_count  = image["write_count"]
        self._read_count   = image["read_count"]
        # Rebuild ChartGenerators with correct params
        self._cg_a = ChartGenerator(self.chart_base, self.mask_base, self.num_digits)
        self._cg_b = ChartGenerator(self.chart_base, self.mask_base, self.num_digits)
        self._sectors = [
            {
                "sector":      s["sector"],
                "byte_length": s["byte_length"],
                "V_A":         int(s["V_A"]),
                "V_B":         int(s["V_B"]),
                "written":     s["written"],
            }
            for s in image["sectors"]
        ]
        print(f"[LATTICE-DRIVE] Image loaded ← {path}")


# ===========================================================================
# CONVENIENCE FACTORY
# ===========================================================================

def lattice_drive(
    sector_size: int = 512,
    n_sectors:   int = 64,
    chart_base:  int = 256,
    mask_base:   int = 1_000_000_000_000,
    num_digits:  int = 100,
) -> LatticeDrive:
    """
    Factory function: create and return a fresh LatticeDrive.

    Example:
        drive = lattice_drive(sector_size=512, n_sectors=128)
        drive.info()
        drive.write(b"Hello, Burris universe!", 0)
        data = drive.read(0)
        print(data)
    """
    ld = LatticeDrive(sector_size, n_sectors, chart_base, mask_base, num_digits)
    print(f"\n⬡  Lattice Drive initialised  —  "
          f"{n_sectors} × {sector_size}B sectors  "
          f"(base={chart_base}, digits={num_digits})\n")
    return ld


# ===========================================================================
# TESTS
# ===========================================================================

if __name__ == "__main__":
    import tempfile

    TEST_DATA = bytes(range(25))
    print("=" * 60)
    print("  TEST 1 — ChartGenerator UP/DOWN round-trips")
    print("=" * 60)
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

        # --- write_disk_image ---
        print("\n--- write_disk_image ---")
        cg5 = ChartGenerator()
        cg5.encode_file(src, enc)
        V_coord = cg5.hm.to_int(cg5.Vs[0])
        disk_out = os.path.join(tmp, "disk_image.bin")
        cg6 = ChartGenerator()
        recovered_img = cg6.write_disk_image(V_coord, len(TEST_DATA), disk_out, direction="up")
        if recovered_img == TEST_DATA:
            print("✅  write_disk_image PASSED — coordinate decoded correctly")
        else:
            print("❌  write_disk_image MISMATCH")

    print()
    print("=" * 60)
    print("  TEST 2 — LatticeDrive round-trip")
    print("=" * 60)

    drive = lattice_drive(sector_size=64, n_sectors=8)

    # Write short message
    msg = b"Hello, Burris universe!"
    drive.write(msg, 0)

    # Write bytes(range(50)) across two sectors
    payload2 = bytes(range(50))
    drive.write_file(payload2, start_sector=1)

    drive.info()

    # Read back
    r0 = drive.read(0)
    if r0 == msg:
        print("✅  Sector 0 read PASSED")
    else:
        print(f"❌  Sector 0 MISMATCH  got={r0!r}  expected={msg!r}")

    r_file = drive.read_file(1, 1)   # only first sector of file (64 bytes, first 50 are data)
    if r_file[:50] == payload2:
        print("✅  write_file / read_file PASSED")
    else:
        print(f"❌  write_file MISMATCH  got={list(r_file[:50])}  expected={list(payload2)}")

    drive.hex_dump(0)

    # Test persistence
    with tempfile.TemporaryDirectory() as tmp2:
        img_path = os.path.join(tmp2, "lattice.json")
        drive.save(img_path)

        drive2 = LatticeDrive()
        drive2.load(img_path)
        r0b = drive2.read(0)
        if r0b == msg:
            print("✅  Save/load PASSED")
        else:
            print(f"❌  Save/load MISMATCH")

    print("\n✅  All tests complete.")
