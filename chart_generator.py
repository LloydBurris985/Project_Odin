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
  - LatticeDrive:
        Paired-universe virtual block device.  Universe A encodes writes; the resulting
        coordinate feeds Universe B which decodes to produce the "lattice representation."
        The inverse path reads back the original bytes exactly.  The drive is sector-
        addressable, serialisable, and behaves like a real block device (read/write/seek).
  - LatticeFS:
        Lightweight filesystem layer on top of LatticeDrive.
        Maps filenames → sector extents.  The index lives in a reserved sector and
        is persisted automatically with the drive image.
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
        """
        hm = self.hm

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
#   write(data, sector_no)   — encode data, store coordinate pair
#   read(sector_no)          — recover original bytes from stored coordinates
#   seek(sector_no)          — position the read/write head
#   format(n_sectors)        — initialise empty sector table
#   info()                   — print drive status
#   save(path) / load(path)  — persist / restore the entire drive image
#   hex_dump(sector_no)      — hex dump a sector (like xxd)
#
# Sector table entry schema:
#   {
#       "sector":      int,    # sector number (0-based)
#       "byte_length": int,    # number of original (unpadded) data bytes
#       "V_A":         int,    # coordinate in universe A after encoding
#       "V_B":         int,    # coordinate in universe B after decoding from V_A
#       "written":     bool,   # True once data has been stored
#   }
# ===========================================================================

class LatticeDrive:
    """
    Paired-universe virtual block device.

    Two ChartGenerator instances (universe A and universe B) are kept in sync:
      - Writing a sector encodes bytes through A to produce V_A, then decodes
        through B starting at V_A to produce the lattice representation V_B.
      - Reading a sector recovers original bytes by decoding from the stored V_A
        through universe A.

    The drive is fully serialisable to/from a self-contained JSON image.
    All large integers (V_A, V_B) are stored as decimal strings to avoid
    JSON integer size limits.

    Args:
        sector_size : bytes per sector (default 512, matching a physical HDD)
        n_sectors   : total sectors on the drive (default 64)
        chart_base  : encoding base (default 256 for raw bytes)
        mask_base   : HandMath limb base
        num_digits  : HandMath limb count (arithmetic precision)
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

        # Two independent ChartGenerator instances — one per universe
        self._cg_a = ChartGenerator(chart_base, mask_base, num_digits)
        self._cg_b = ChartGenerator(chart_base, mask_base, num_digits)

        # Sector table: one dict per sector
        self._sectors: list = [self._empty_sector(i) for i in range(n_sectors)]

        # Read/write head
        self._head: int = 0

        # Lifetime I/O counters
        self._write_count: int = 0
        self._read_count:  int = 0

    # -----------------------------------------------------------------------
    # Dunder helpers
    # -----------------------------------------------------------------------

    def __repr__(self) -> str:
        used = sum(1 for s in self._sectors if s["written"])
        return (
            f"<LatticeDrive sectors={self.n_sectors} "
            f"sector_size={self.sector_size}B "
            f"used={used} free={self.n_sectors - used} "
            f"head={self._head}>"
        )

    def __len__(self) -> int:
        """Total drive capacity in bytes."""
        return self.n_sectors * self.sector_size

    def __contains__(self, sector_no: int) -> bool:
        """True if sector_no is within range and has been written."""
        return (
            0 <= sector_no < self.n_sectors
            and self._sectors[sector_no]["written"]
        )

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _empty_sector(n: int) -> dict:
        return {
            "sector":      n,
            "byte_length": 0,
            "V_A":         0,
            "V_B":         0,
            "written":     False,
        }

    def _assert_sector(self, n: int):
        """Raise IndexError if sector_no is out of range."""
        if not (0 <= n < self.n_sectors):
            raise IndexError(
                f"Sector {n} is out of range [0, {self.n_sectors - 1}]."
            )

    def _encode_bytes_up(self, cg: ChartGenerator, data: bytes, u: int = 0) -> int:
        """
        Encode `data` into cg (UP direction, end-to-front).
        Resets V to R=1 before encoding so each call is independent.
        Returns the final V as a plain int.
        """
        hm = cg.hm
        cg.Vs[u] = hm.from_int(1)
        cg.Rs[u] = hm.from_int(1)
        for i in range(len(data) - 1, -1, -1):
            cg._encode_step(data[i], u)
        return hm.to_int(cg.Vs[u])

    def _decode_bytes_up(self, cg: ChartGenerator, V: int, length: int, u: int = 0) -> bytes:
        """
        Load coordinate V into cg (UP direction), decode `length` bytes.
        Returns recovered bytes.
        """
        hm = cg.hm
        cg.Vs[u] = hm.from_int(V)
        cg.Rs[u] = hm.from_int(1)
        recovered = []
        for _ in range(length):
            recovered.append(cg._decode_step(u))
        return bytes(recovered)

    # -----------------------------------------------------------------------
    # PUBLIC INTERFACE — Block device operations
    # -----------------------------------------------------------------------

    def seek(self, sector_no: int) -> "LatticeDrive":
        """
        Position the read/write head at sector_no.

        Returns self to allow chaining: drive.seek(5).read()
        """
        self._assert_sector(sector_no)
        self._head = sector_no
        print(f"[LATTICE-DRIVE] Head → sector {sector_no}")
        return self

    def write(self, data: bytes, sector_no: int = None) -> dict:
        """
        Write raw bytes to a sector.

        Write path:
            data  ──encode(A)──▶  V_A  ──decode(B from V_A)──▶  V_B  (stored)

        Args:
            data      : bytes to store; must not exceed sector_size
            sector_no : target sector; if None, uses current head (then advances it)

        Returns:
            The updated sector record dict.

        Raises:
            IndexError  : if sector_no is out of range
            ValueError  : if len(data) > sector_size
        """
        if sector_no is None:
            sector_no = self._head
        self._assert_sector(sector_no)

        if len(data) > self.sector_size:
            raise ValueError(
                f"Data ({len(data)} bytes) exceeds sector size ({self.sector_size} bytes). "
                f"Use write_file() to span multiple sectors."
            )

        # Zero-pad to a full sector so every sector decodes to the same length
        padded  = data + bytes(self.sector_size - len(data))
        n_bytes = self.sector_size

        # Universe A: encode padded bytes → V_A
        V_A = self._encode_bytes_up(self._cg_a, padded)

        # Universe B: starting at V_A, decode sector_size bytes to advance B's state → V_B
        self._cg_b.Vs[0] = self._cg_b.hm.from_int(V_A)
        self._cg_b.Rs[0] = self._cg_b.hm.from_int(1)
        for _ in range(n_bytes):
            self._cg_b._decode_step(0)
        V_B = self._cg_b.hm.to_int(self._cg_b.Vs[0])

        rec = {
            "sector":      sector_no,
            "byte_length": len(data),   # original unpadded length
            "V_A":         V_A,
            "V_B":         V_B,
            "written":     True,
        }
        self._sectors[sector_no] = rec
        self._write_count += 1
        self._head = min(sector_no + 1, self.n_sectors - 1)

        print(
            f"[WRITE] Sector {sector_no:4d}  "
            f"{len(data):4d} bytes  "
            f"V_A={fmt_short(V_A)}  V_B={fmt_short(V_B)}"
        )
        return rec

    def read(self, sector_no: int = None) -> bytes:
        """
        Read raw bytes from a sector.

        Read path (exact inverse of write):
            V_A  ──decode(A)──▶  original data

        Args:
            sector_no : sector to read; if None, reads from current head (then advances it)

        Returns:
            Original bytes (without zero-padding).
            Returns sector_size zero bytes if the sector has never been written.

        Raises:
            IndexError : if sector_no is out of range
        """
        if sector_no is None:
            sector_no = self._head
        self._assert_sector(sector_no)

        rec = self._sectors[sector_no]
        if not rec["written"]:
            print(f"[READ] Sector {sector_no} is empty — returning zero-fill.")
            self._head = min(sector_no + 1, self.n_sectors - 1)
            return bytes(self.sector_size)

        V_A      = rec["V_A"]
        V_B      = rec["V_B"]
        byte_len = rec["byte_length"]
        n_bytes  = self.sector_size     # always decode a full padded sector

        recovered_padded = self._decode_bytes_up(self._cg_a, V_A, n_bytes)
        recovered        = recovered_padded[:byte_len]

        self._read_count += 1
        self._head = min(sector_no + 1, self.n_sectors - 1)

        print(
            f"[READ]  Sector {sector_no:4d}  "
            f"{byte_len:4d} bytes  "
            f"V_A={fmt_short(V_A)}  V_B={fmt_short(V_B)}"
        )
        return recovered

    def write_file(self, data: bytes, start_sector: int = 0) -> list:
        """
        Write arbitrary-length bytes across consecutive sectors.

        Args:
            data         : raw bytes to store
            start_sector : first sector to use

        Returns:
            List of sector numbers that were written.

        Raises:
            IndexError : if the data requires more sectors than remain from start_sector
        """
        sectors_needed = (len(data) + self.sector_size - 1) // self.sector_size
        end_sector = start_sector + sectors_needed - 1
        if end_sector >= self.n_sectors:
            raise IndexError(
                f"File requires sectors {start_sector}–{end_sector}, "
                f"but the drive only has {self.n_sectors} sectors (0–{self.n_sectors - 1})."
            )

        used   = []
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
        Read n_sectors consecutive sectors starting at start_sector.

        Returns concatenated raw bytes (the final sector's zero-padding is stripped
        automatically using the stored byte_length).

        Raises:
            IndexError : if any addressed sector is out of range
        """
        result = bytearray()
        for i in range(n_sectors):
            sno = start_sector + i
            result += self.read(sno)
        return bytes(result)

    def format(self, n_sectors: int = None) -> "LatticeDrive":
        """
        Zero out the entire sector table (equivalent to formatting a disk).

        Args:
            n_sectors : if provided, resizes the drive to this many sectors

        Returns self for chaining.
        """
        if n_sectors is not None:
            self.n_sectors = n_sectors
        self._sectors     = [self._empty_sector(i) for i in range(self.n_sectors)]
        self._head        = 0
        self._write_count = 0
        self._read_count  = 0
        print(
            f"[FORMAT] Drive formatted  —  "
            f"{self.n_sectors} sectors × {self.sector_size} bytes/sector"
        )
        return self

    def hex_dump(self, sector_no: int, cols: int = 16):
        """
        Print a hex dump of a sector's content (xxd style).

        Args:
            sector_no : sector to dump
            cols      : bytes per display row (default 16)
        """
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
        """Print a full drive status report (geometry, usage, sector table)."""
        used     = sum(1 for s in self._sectors if s["written"])
        free     = self.n_sectors - used
        used_mb  = (used * self.sector_size) / 1_048_576
        total_mb = (self.n_sectors * self.sector_size) / 1_048_576

        print(f"\n{self._BORDER}")
        print(f"  ⬡  LATTICE DRIVE — STATUS")
        print(f"{self._BORDER}")
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
            print(
                f"  {rec['sector']:>4}  {rec['byte_length']:>6}  "
                f"{V_A_s:>14}  {V_B_s:>14}  {status}"
            )
        print(f"{self._BORDER}\n")

    # -----------------------------------------------------------------------
    # PERSISTENCE — save / load
    # -----------------------------------------------------------------------

    def save(self, path: str):
        """
        Serialise the entire drive image to a JSON file.

        V_A and V_B are stored as decimal strings to avoid JSON integer overflow.
        The file is self-contained: loading it restores geometry, all sector data,
        the head position, and I/O counters.

        Args:
            path : destination file path (will be created or overwritten)
        """
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
                {
                    "sector":      s["sector"],
                    "byte_length": s["byte_length"],
                    "V_A":         str(s["V_A"]),   # decimal string — safe for huge ints
                    "V_B":         str(s["V_B"]),
                    "written":     s["written"],
                }
                for s in self._sectors
            ],
        }
        with open(path, "w") as f:
            json.dump(image, f, indent=2)
        print(f"[LATTICE-DRIVE] Image saved → {path}")

    def load(self, path: str) -> "LatticeDrive":
        """
        Restore a drive image from a JSON file produced by save().

        Rebuilds ChartGenerator instances with the correct parameters and
        restores all sector records, geometry, head position, and counters.

        Args:
            path : source file path

        Returns self for chaining.

        Raises:
            FileNotFoundError : if path does not exist
            KeyError          : if the image file is malformed
        """
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

        # Rebuild ChartGenerators with the loaded parameters
        self._cg_a = ChartGenerator(self.chart_base, self.mask_base, self.num_digits)
        self._cg_b = ChartGenerator(self.chart_base, self.mask_base, self.num_digits)

        # Restore sector table — convert V_A / V_B back from decimal strings
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
        return self


# ===========================================================================
# LATTICE FILESYSTEM  (LatticeFS)
#
# A lightweight filename → sector-extent index layered on top of LatticeDrive.
#
# Design:
#   - Sector 0 is the "superblock": a JSON index of all stored files.
#     It is written automatically whenever any file is added or deleted.
#   - Data sectors start at sector 1 and grow upward.
#   - The index is always reloaded from the drive before operations so a
#     saved/loaded drive image stays consistent with no extra state needed.
#
# Index entry schema (stored as JSON in sector 0):
#   {
#       "filename":    str,   # logical filename (acts as the key)
#       "start":       int,   # first data sector
#       "n_sectors":   int,   # number of sectors occupied
#       "byte_length": int,   # total original byte count (unpadded)
#   }
#
# Public API:
#   write_file(filename, data)      — store bytes under a name
#   read_file(filename)             — retrieve bytes by name
#   delete_file(filename)           — remove index entry (sectors not zeroed)
#   rename_file(old_name, new_name) — rename an index entry
#   ls()                            — list all files with sizes and sector extents
#   exists(filename)                — True / False
#   stat(filename)                  — return raw index entry dict
#
# Limitations (by design — this is a simple index, not a full FS):
#   - No defragmentation: deleted sectors are not reclaimed automatically.
#     Call compact() to rebuild the drive with no gaps.
#   - Filenames are arbitrary strings; no directory hierarchy.
#   - Maximum file size is limited by drive capacity.
# ===========================================================================

class LatticeFS:
    """
    Lightweight filesystem index on top of a LatticeDrive.

    Sector 0 is reserved as the superblock (a JSON index of all files).
    All data starts at sector 1.

    Args:
        drive : an existing LatticeDrive instance to use as the backing store.
                The drive must have at least 2 sectors.
    """

    _SUPERBLOCK_SECTOR = 0

    def __init__(self, drive: LatticeDrive):
        if drive.n_sectors < 2:
            raise ValueError("LatticeFS requires a drive with at least 2 sectors.")
        self._drive = drive
        # In-memory index: filename → {start, n_sectors, byte_length}
        self._index: dict = {}
        # Next free sector pointer (grows upward from 1)
        self._next_free: int = 1
        # Load any existing index from the superblock
        self._load_index()

    # -----------------------------------------------------------------------
    # Internal: superblock I/O
    # -----------------------------------------------------------------------

    def _flush_index(self):
        """Serialise the in-memory index to sector 0."""
        raw = json.dumps(
            {
                "lattice_fs_version": 1,
                "next_free":          self._next_free,
                "files":              self._index,
            }
        ).encode("utf-8")

        if len(raw) > self._drive.sector_size:
            raise OverflowError(
                f"Filesystem index ({len(raw)} bytes) exceeds one sector "
                f"({self._drive.sector_size} bytes). Reduce the number of files "
                f"or increase sector_size."
            )
        self._drive.write(raw, self._SUPERBLOCK_SECTOR)

    def _load_index(self):
        """
        Load the filesystem index from sector 0.
        If sector 0 is empty (fresh drive), initialise a blank index.
        """
        if not self._drive._sectors[self._SUPERBLOCK_SECTOR]["written"]:
            # Fresh drive — initialise
            self._index     = {}
            self._next_free = 1
            self._flush_index()
            return

        raw = self._drive.read(self._SUPERBLOCK_SECTOR)
        try:
            # Trim null bytes from zero-padding
            decoded = raw.rstrip(b"\x00").decode("utf-8")
            data    = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"LatticeFS: superblock in sector 0 is corrupt: {exc}"
            ) from exc

        self._index     = data.get("files", {})
        self._next_free = data.get("next_free", 1)

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
                f"Available files: {list(self._index.keys())}"
            )

    # -----------------------------------------------------------------------
    # PUBLIC API
    # -----------------------------------------------------------------------

    def write_file(self, filename: str, data: bytes) -> dict:
        """
        Store bytes under a logical filename.

        If the file already exists it is overwritten in-place if the new
        data fits in the same number of sectors; otherwise it is appended
        to the end of the drive (the old sectors are abandoned — call
        compact() to reclaim them).

        Args:
            filename : logical name (key in the index)
            data     : raw bytes to store

        Returns:
            The index entry dict for the stored file.

        Raises:
            IndexError  : if the drive has insufficient free sectors
            OverflowError : if the index itself grows beyond one sector
        """
        n_sec = self._sectors_needed(len(data))

        # Reuse existing allocation if possible (same or fewer sectors)
        if filename in self._index:
            entry = self._index[filename]
            if n_sec <= entry["n_sectors"]:
                # Write in-place, reuse the original sector range
                start = entry["start"]
                offset = 0
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

        # Allocate new sectors at the end of the used area
        start = self._next_free
        end   = start + n_sec - 1
        if end >= self._drive.n_sectors:
            raise IndexError(
                f"LatticeFS: Not enough free sectors for '{filename}' "
                f"(need {n_sec}, drive has {self._drive.n_sectors - self._next_free} free)."
            )

        offset = 0
        for i in range(n_sec):
            chunk = data[offset : offset + self._drive.sector_size]
            self._drive.write(chunk, start + i)
            offset += self._drive.sector_size

        entry = {
            "start":       start,
            "n_sectors":   n_sec,
            "byte_length": len(data),
        }
        self._index[filename]  = entry
        self._next_free       += n_sec
        self._flush_index()

        print(f"[LatticeFS] WRITE '{filename}'  {len(data)} bytes  "
              f"sectors {start}–{end}")
        return entry

    def read_file(self, filename: str) -> bytes:
        """
        Retrieve bytes stored under filename.

        Args:
            filename : logical name as passed to write_file()

        Returns:
            Original bytes (no zero-padding).

        Raises:
            FileNotFoundError : if filename is not in the index
        """
        self._assert_exists(filename)
        entry  = self._index[filename]
        start  = entry["start"]
        n_sec  = entry["n_sectors"]
        b_len  = entry["byte_length"]

        raw = bytearray()
        for i in range(n_sec):
            raw += self._drive.read(start + i)

        result = bytes(raw[:b_len])
        print(f"[LatticeFS] READ '{filename}'  {b_len} bytes  "
              f"sectors {start}–{start + n_sec - 1}")
        return result

    def delete_file(self, filename: str):
        """
        Remove a file from the index.

        The sectors previously used by the file are *not* zeroed — they are
        simply abandoned.  Call compact() to reclaim them.

        Args:
            filename : logical name to remove

        Raises:
            FileNotFoundError : if filename is not in the index
        """
        self._assert_exists(filename)
        entry = self._index.pop(filename)
        self._flush_index()
        print(f"[LatticeFS] DELETE '{filename}'  "
              f"(sectors {entry['start']}–{entry['start'] + entry['n_sectors'] - 1} abandoned)")

    def rename_file(self, old_name: str, new_name: str):
        """
        Rename a file (index entry only — no sector data is touched).

        Args:
            old_name : current logical name
            new_name : desired logical name

        Raises:
            FileNotFoundError : if old_name is not in the index
            FileExistsError   : if new_name is already in the index
        """
        self._assert_exists(old_name)
        if new_name in self._index:
            raise FileExistsError(
                f"LatticeFS: '{new_name}' already exists. Delete it first."
            )
        self._index[new_name] = self._index.pop(old_name)
        self._flush_index()
        print(f"[LatticeFS] RENAME '{old_name}'  →  '{new_name}'")

    def exists(self, filename: str) -> bool:
        """Return True if filename is in the index."""
        return filename in self._index

    def stat(self, filename: str) -> dict:
        """
        Return the raw index entry for filename.

        Returns a copy so callers cannot accidentally mutate the index.

        Raises:
            FileNotFoundError : if filename is not in the index
        """
        self._assert_exists(filename)
        return dict(self._index[filename])

    def ls(self):
        """Print a directory listing of all stored files."""
        border = "─" * 62
        print(f"\n  {border}")
        print(f"  ⬡  LATTICE FILESYSTEM  —  {len(self._index)} file(s)")
        print(f"  {border}")
        print(f"  {'NAME':<30}  {'BYTES':>8}  {'START':>6}  {'SECS':>5}")
        print(f"  {'─'*30}  {'─'*8}  {'─'*6}  {'─'*5}")
        if not self._index:
            print(f"  (empty)")
        for name, entry in sorted(self._index.items()):
            print(
                f"  {name:<30}  {entry['byte_length']:>8}  "
                f"{entry['start']:>6}  {entry['n_sectors']:>5}"
            )
        print(f"  {border}")
        drive = self._drive
        used_data = sum(e["n_sectors"] for e in self._index.values())
        total_data = drive.n_sectors - 1  # sector 0 is superblock
        print(f"  Superblock : sector 0")
        print(f"  Data sectors used : {used_data} / {total_data}")
        print(f"  Next free sector  : {self._next_free}")
        print(f"  {border}\n")

    def compact(self) -> "LatticeFS":
        """
        Rebuild the drive from scratch, removing abandoned (deleted) sectors.

        All live files are re-written sequentially from sector 1.  The
        superblock is updated to reflect the new layout.

        Returns self for chaining.
        """
        print(f"\n[LatticeFS] COMPACT — rebuilding drive image...")

        # Read all live file data before we reformat
        live: dict = {}
        for name, entry in self._index.items():
            live[name] = self.read_file(name)

        # Reformat the backing drive (leaves geometry intact)
        self._drive.format()
        self._index     = {}
        self._next_free = 1

        # Re-write all files in sorted name order (deterministic layout)
        for name in sorted(live.keys()):
            self.write_file(name, live[name])

        print(f"[LatticeFS] COMPACT complete — {len(live)} files, "
              f"next free sector = {self._next_free}")
        return self


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
    """
    Create and return a fresh LatticeDrive.

    Example:
        drive = lattice_drive(sector_size=512, n_sectors=128)
        drive.write(b"Hello, Burris universe!", 0)
        data = drive.read(0)
    """
    ld = LatticeDrive(sector_size, n_sectors, chart_base, mask_base, num_digits)
    print(
        f"\n⬡  Lattice Drive initialised  —  "
        f"{n_sectors} × {sector_size}B sectors  "
        f"(base={chart_base}, digits={num_digits})\n"
    )
    return ld


def lattice_fs(
    sector_size: int = 512,
    n_sectors:   int = 128,
    chart_base:  int = 256,
    mask_base:   int = 1_000_000_000_000,
    num_digits:  int = 100,
) -> LatticeFS:
    """
    Create a fresh LatticeDrive and wrap it in a LatticeFS.

    The first sector is automatically reserved as the filesystem superblock.

    Example:
        fs = lattice_fs(n_sectors=64)
        fs.write_file("hello.txt", b"Hello, Burris universe!")
        data = fs.read_file("hello.txt")
        fs.ls()
    """
    drive = LatticeDrive(sector_size, n_sectors, chart_base, mask_base, num_digits)
    print(
        f"\n⬡  Lattice Drive initialised  —  "
        f"{n_sectors} × {sector_size}B sectors  "
        f"(base={chart_base}, digits={num_digits})"
    )
    fs = LatticeFS(drive)
    print(f"⬡  LatticeFS mounted  —  superblock at sector 0\n")
    return fs


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
        V_coord  = cg5.hm.to_int(cg5.Vs[0])
        disk_out = os.path.join(tmp, "disk_image.bin")
        cg6      = ChartGenerator()
        recovered_img = cg6.write_disk_image(V_coord, len(TEST_DATA), disk_out, direction="up")
        if recovered_img == TEST_DATA:
            print("✅  write_disk_image PASSED — coordinate decoded correctly")
        else:
            print("❌  write_disk_image MISMATCH")

    # -----------------------------------------------------------------------
    print()
    print("=" * 60)
    print("  TEST 2 — LatticeDrive round-trip")
    print("=" * 60)

    drive = lattice_drive(sector_size=64, n_sectors=8)

    msg      = b"Hello, Burris universe!"
    payload2 = bytes(range(50))

    drive.write(msg, 0)
    drive.write_file(payload2, start_sector=1)
    drive.info()

    r0 = drive.read(0)
    if r0 == msg:
        print("✅  Sector 0 read PASSED")
    else:
        print(f"❌  Sector 0 MISMATCH  got={r0!r}  expected={msg!r}")

    r_file = drive.read_file(1, 1)
    if r_file[:50] == payload2:
        print("✅  write_file / read_file PASSED")
    else:
        print(f"❌  write_file MISMATCH  got={list(r_file[:50])}  expected={list(payload2)}")

    drive.hex_dump(0)

    with tempfile.TemporaryDirectory() as tmp2:
        img_path = os.path.join(tmp2, "lattice.json")
        drive.save(img_path)
        drive2 = LatticeDrive()
        drive2.load(img_path)
        r0b = drive2.read(0)
        if r0b == msg:
            print("✅  LatticeDrive save/load PASSED")
        else:
            print(f"❌  LatticeDrive save/load MISMATCH")

    # -----------------------------------------------------------------------
    print()
    print("=" * 60)
    print("  TEST 3 — LatticeFS")
    print("=" * 60)

    fs = lattice_fs(sector_size=128, n_sectors=32)

    # Write three files
    files = {
        "readme.txt":   b"Burris Numerical System — LatticeFS demo.",
        "data.bin":     bytes(range(100)),
        "greeting.txt": b"Hello from the informational universe!",
    }
    for name, content in files.items():
        fs.write_file(name, content)

    fs.ls()

    # Read them back
    for name, expected in files.items():
        got = fs.read_file(name)
        status = "✅" if got == expected else "❌"
        print(f"{status}  read '{name}'")

    # Rename
    fs.rename_file("greeting.txt", "hello.txt")
    assert fs.exists("hello.txt") and not fs.exists("greeting.txt")
    print("✅  rename PASSED")

    # Stat
    s = fs.stat("data.bin")
    assert s["byte_length"] == 100
    print("✅  stat PASSED")

    # Overwrite in-place
    new_content = b"Overwritten data."
    fs.write_file("readme.txt", new_content)
    assert fs.read_file("readme.txt") == new_content
    print("✅  overwrite PASSED")

    # Delete
    fs.delete_file("hello.txt")
    assert not fs.exists("hello.txt")
    print("✅  delete PASSED")

    # Compact
    fs.compact()
    fs.ls()

    # Save and reload the underlying drive; re-mount LatticeFS
    with tempfile.TemporaryDirectory() as tmp3:
        img = os.path.join(tmp3, "fs_image.json")
        fs._drive.save(img)

        drive3 = LatticeDrive()
        drive3.load(img)
        fs2 = LatticeFS(drive3)

        for name, expected in [("readme.txt", new_content), ("data.bin", bytes(range(100)))]:
            got = fs2.read_file(name)
            status = "✅" if got == expected else "❌"
            print(f"{status}  post-reload read '{name}'")

    print("\n✅  All tests complete.")
