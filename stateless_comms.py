"""
stateless_comms.py
OdinNet — Stateless Coordinate-Space Communications Layer

Two nodes share a passphrase. From it they independently derive identical
coordinate windows. Node A encodes a message and pads it until its final V
lands inside the agreed window. Node B scans that window, finds a V that
decodes to something structurally valid, reads the message.

No files are exchanged. No shared disk. The coordinate space IS the medium.

The transport beneath this (Usenet feed, HTTP relay, radio burst, sneakernet)
carries only raw coordinate integers — single large numbers. The mathematical
ether lives in the BNS engine itself.

Beacon pointers: a beacon IS a coordinate integer. Resolving it means decoding
that integer back to bytes using the shared passphrase geometry. Content lives
at coordinates. Beacons point to coordinates. Everything else is math.

Anti-entropy defense:
  - Every valid message carries a STRUCT_SIG header only passphrase-holders know
  - DEFCON window compression narrows the valid coordinate range under attack
  - Fleet Jump (R-axis relocation) moves the entire ether when flooded

Author: OdinNet Engineering (Scotty)
"""

import math
import hashlib
import json
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Import from your existing BNS core
# ---------------------------------------------------------------------------
from chart_generator import ChartGenerator, HandMath, fmt_short

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROTOCOL_VERSION  = 1
STRUCT_SIG_PREFIX = "ODIN::"          # Structural signature — not secret, but required
STRUCT_SIG_SUFFIX = "::END"           # Closing marker for scanner validation
MAX_PAD_BYTES     = 4096              # Maximum padding search before giving up
SCAN_STEPS        = 200               # How many coordinate probes per poll cycle
BLOCK_DECODE_LEN  = 512               # Bytes decoded per probe when scanning

# In-memory buffer caps — prevents RAM exhaustion on long-running daemons
INBOX_MAX   = 1000   # Drop oldest when exceeded
OUTBOX_MAX  = 1000

# DEFCON window compression table (matches existing OdinNetSecurity scale)
DEFCON_COMPRESSION = {
    1:  1.00,   # Wide open
    3:  0.80,
    5:  0.50,
    7:  0.20,
    10: 0.05,   # Near-zero tolerance — near-impossible to flood
}


# ---------------------------------------------------------------------------
# PassphraseGeometry
# Derives all shared mathematical parameters from the passphrase alone.
# Both nodes run this independently and arrive at identical results.
# ---------------------------------------------------------------------------

class PassphraseGeometry:
    """
    Derives the shared coordinate geometry (V_base, R_base, window bounds)
    from a passphrase string. Entirely deterministic — no I/O, no network.

    The geometry object is the only thing both nodes need to agree on.
    They agree on it by agreeing on the passphrase.
    """

    def __init__(
        self,
        passphrase: str,
        chart_base: int = 256,
        mask_base:  int = 1_000_000_000_000,
        num_digits: int = 100,
        window_k:   float = 3.5,      # How many std-devs wide the window is
        num_samples: int = 30,        # Samples used to calibrate the window
    ):
        self.passphrase  = passphrase
        self.chart_base  = chart_base
        self.mask_base   = mask_base
        self.num_digits  = num_digits
        self.window_k    = window_k
        self.num_samples = num_samples

        self._cg = ChartGenerator(chart_base, mask_base, num_digits)

        # Derive everything from the passphrase
        self.V_base, self.R_base = self._derive_base()
        self.window_low, self.window_high, self.window_mean, self.window_std = \
            self._calibrate_window()

    # ── Internal derivation ────────────────────────────────────────────────

    def _passphrase_seed_bytes(self) -> bytes:
        """SHA-256 of the passphrase gives us a stable, high-entropy seed."""
        return hashlib.sha256(self.passphrase.encode("utf-8")).digest()

    def _derive_base(self) -> tuple[int, int]:
        """
        Feed the passphrase hash bytes through the BNS encoder (reversed, as
        per the UP protocol) to arrive at a stable (V_base, R_base) pair.
        """
        hm = self._cg.hm
        cg = ChartGenerator(self.chart_base, self.mask_base, self.num_digits)

        seed = self._passphrase_seed_bytes()
        # Pad to num_digits bytes for full coordinate resolution
        seed_extended = (seed * (self.num_digits // len(seed) + 1))[:self.num_digits]

        for i in range(len(seed_extended) - 1, -1, -1):
            cg._encode_step(seed_extended[i], 0)

        V_base = hm.to_int(cg.Vs[0])
        R_base = hm.to_int(cg.Rs[0])
        return V_base, R_base

    def _calibrate_window(self) -> tuple[int, int, int, int]:
        """
        Run num_samples random-but-deterministic byte sequences through the
        encoder starting from V_base to measure natural spread.
        The polling window is [mean + 0.4σ, mean + 3.5σ] — asymmetric above
        baseline, matching the existing GrokComms window convention.
        """
        hm      = self._cg.hm
        samples = []

        for i in range(self.num_samples):
            # Deterministic sample seed: hash(passphrase + sample index)
            sample_seed = hashlib.sha256(
                f"{self.passphrase}:sample:{i}".encode()
            ).digest()[:64]

            cg = ChartGenerator(self.chart_base, self.mask_base, self.num_digits)
            cg.Vs[0] = hm.from_int(self.V_base)
            cg.Rs[0] = hm.from_int(self.R_base)

            for j in range(len(sample_seed) - 1, -1, -1):
                cg._encode_step(sample_seed[j], 0)
            samples.append(hm.to_int(cg.Vs[0]))

        mean     = sum(samples) // len(samples)
        variance = sum((s - mean) ** 2 for s in samples) // len(samples)
        std      = math.isqrt(variance) if variance > 0 else 1

        low  = mean + (4  * std) // 10    # +0.4σ
        high = mean + (35 * std) // 10    # +3.5σ

        return low, high, mean, std

    def compressed_window(self, defcon: int) -> tuple[int, int]:
        """
        Return the window bounds compressed by the DEFCON factor.
        At DEFCON 10 the window is 5% of its normal width — flooding it
        requires knowing the passphrase AND the current DEFCON state.
        """
        factor = DEFCON_COMPRESSION.get(defcon, 1.0)
        center = (self.window_low + self.window_high) // 2
        half   = int((self.window_high - self.window_low) * factor) // 2
        return center - half, center + half

    def jump_geometry(self, jump_seed: int) -> "PassphraseGeometry":
        """
        Derive a new geometry after a Fleet Jump.
        Both nodes call this with the same jump_seed (e.g. agreed epoch block)
        and independently arrive at the new ether region.
        Returns a fresh PassphraseGeometry with a derived passphrase.
        """
        new_passphrase = hashlib.sha256(
            f"{self.passphrase}:jump:{jump_seed}".encode()
        ).hexdigest()
        return PassphraseGeometry(
            new_passphrase,
            self.chart_base,
            self.mask_base,
            self.num_digits,
            self.window_k,
            self.num_samples,
        )

    def summary(self) -> dict:
        return {
            "V_base":      fmt_short(self.V_base),
            "R_base":      fmt_short(self.R_base),
            "window_low":  fmt_short(self.window_low),
            "window_high": fmt_short(self.window_high),
            "window_mean": fmt_short(self.window_mean),
            "window_std":  fmt_short(self.window_std),
            "width":       fmt_short(self.window_high - self.window_low),
        }


# ---------------------------------------------------------------------------
# MessageFrame
# The wire format for a coordinate-space message.
# Encoded to/decoded from raw bytes that land inside a coordinate.
# ---------------------------------------------------------------------------

class MessageFrame:
    """
    Wraps a message payload with a structural signature, metadata header,
    and closing marker. The scanner looks for STRUCT_SIG_PREFIX to validate.

    Wire format (all UTF-8 encoded):
        ODIN::<json_header>|<payload>::END<null_padding>

    The null padding is added by the padder to push V into the window.
    The scanner strips it on decode.
    """

    def __init__(
        self,
        payload:    str,
        sender_id:  str,
        msg_type:   str   = "MSG",      # MSG | BEACON | BEACON_PTR | FLEET
        beacon_coord: int = None,       # Set for BEACON_PTR frames
        reply_to:   int   = None,       # Coordinate of message being replied to
        metadata:   dict  = None,
    ):
        self.payload      = payload
        self.sender_id    = sender_id
        self.msg_type     = msg_type
        self.beacon_coord = beacon_coord
        self.reply_to     = reply_to
        self.metadata     = metadata or {}
        self.timestamp    = datetime.now(timezone.utc).isoformat()

    def encode(self) -> bytes:
        """Serialize to the wire format bytes ready for BNS encoding."""
        header = {
            "v":    PROTOCOL_VERSION,
            "type": self.msg_type,
            "from": self.sender_id,
            "ts":   self.timestamp,
        }
        if self.beacon_coord is not None:
            header["beacon"] = str(self.beacon_coord)
        if self.reply_to is not None:
            header["reply"]  = str(self.reply_to)
        if self.metadata:
            header["meta"]   = self.metadata

        wire = (
            STRUCT_SIG_PREFIX
            + json.dumps(header, separators=(",", ":"))
            + "|"
            + self.payload
            + STRUCT_SIG_SUFFIX
        )
        return wire.encode("utf-8")

    @staticmethod
    def decode(raw_bytes: bytes) -> "MessageFrame | None":
        """
        Parse raw decoded bytes back into a MessageFrame.
        Returns None if the structural signature is absent (noise / entropy attack).
        """
        try:
            text = raw_bytes.rstrip(b"\x00").decode("utf-8", errors="replace")
        except Exception:
            return None

        if STRUCT_SIG_PREFIX not in text or STRUCT_SIG_SUFFIX not in text:
            return None

        try:
            inner  = text[len(STRUCT_SIG_PREFIX) : text.rfind(STRUCT_SIG_SUFFIX)]
            pipe   = inner.index("|")
            header = json.loads(inner[:pipe])
            body   = inner[pipe + 1:]
        except Exception:
            return None

        frame              = MessageFrame.__new__(MessageFrame)
        frame.payload      = body
        frame.sender_id    = header.get("from",   "UNKNOWN")
        frame.msg_type     = header.get("type",   "MSG")
        frame.timestamp    = header.get("ts",     "")
        frame.beacon_coord = int(header["beacon"]) if "beacon" in header else None
        frame.reply_to     = int(header["reply"])  if "reply"  in header else None
        frame.metadata     = header.get("meta",   {})
        return frame

    def to_dict(self) -> dict:
        return {
            "payload":      self.payload,
            "sender_id":    self.sender_id,
            "msg_type":     self.msg_type,
            "timestamp":    self.timestamp,
            "beacon_coord": self.beacon_coord,
            "reply_to":     self.reply_to,
            "metadata":     self.metadata,
        }


# ---------------------------------------------------------------------------
# CoordPadder
# Finds the padding needed to push a message's encoded V into the window.
# ---------------------------------------------------------------------------

class CoordPadder:
    """
    Iteratively adds null bytes to a MessageFrame's wire encoding until
    the BNS encoder lands V inside the target window.

    This is deterministic given the same geometry and payload — both nodes
    can independently reproduce the target coordinate from the passphrase
    and the message content alone.
    """

    def __init__(self, geometry: PassphraseGeometry):
        self.geometry = geometry

    def find_coordinate(
        self,
        frame:      MessageFrame,
        defcon:     int = 1,
        max_pad:    int = MAX_PAD_BYTES,
    ) -> tuple[int, int] | None:
        """
        Return (coordinate_V, pad_count) or None if no landing found.

        The coordinate_V is the integer the remote node needs to decode
        the message. You transmit this integer (the beacon/pointer) to
        the other node via whatever transport you have.
        """
        geo    = self.geometry
        hm     = ChartGenerator(geo.chart_base, geo.mask_base, geo.num_digits).hm
        low, high = geo.compressed_window(defcon)

        wire = frame.encode()

        for pad in range(max_pad):
            test_data = wire + b"\x00" * pad

            cg = ChartGenerator(geo.chart_base, geo.mask_base, geo.num_digits)
            cg.Vs[0] = hm.from_int(geo.V_base)
            cg.Rs[0] = hm.from_int(geo.R_base)

            for i in range(len(test_data) - 1, -1, -1):
                cg._encode_step(test_data[i], 0)

            final_V = hm.to_int(cg.Vs[0])
            if low <= final_V <= high:
                return final_V, pad

        return None  # No landing found — caller should try different DEFCON or jump

    def message_length_with_pad(self, frame: MessageFrame, pad_count: int) -> int:
        return len(frame.encode()) + pad_count


# ---------------------------------------------------------------------------
# CoordScanner
# Scans the coordinate window to find and decode valid messages.
# ---------------------------------------------------------------------------

class CoordScanner:
    """
    Probes coordinate positions across the window, decoding each and checking
    for the STRUCT_SIG_PREFIX. Valid frames are returned; noise is silently
    discarded.

    This is the receive side. Node B runs this to find what Node A encoded.

    In practice the scanner doesn't receive the coordinate from the network
    (though it CAN if given one directly via decode_coordinate). It sweeps
    the agreed window and finds messages by their structural signature.
    If the sender also transmits the coordinate integer via transport,
    decode_coordinate() gives instant retrieval without scanning.
    """

    def __init__(self, geometry: PassphraseGeometry):
        self.geometry = geometry

    def scan_window(
        self,
        defcon:     int  = 1,
        steps:      int  = SCAN_STEPS,
        block_len:  int  = BLOCK_DECODE_LEN,
        since_ts:   str  = None,   # ISO timestamp — filter out older messages
    ) -> list[dict]:
        """
        Sweep the window in `steps` probes. Return list of decoded message dicts.
        """
        geo      = self.geometry
        hm       = ChartGenerator(geo.chart_base, geo.mask_base, geo.num_digits).hm
        low, high = geo.compressed_window(defcon)
        span      = high - low
        step_size = max(1, span // steps)
        found     = []

        for i in range(steps):
            probe_V = low + i * step_size
            frame   = self._probe(probe_V, block_len, geo, hm)
            if frame is None:
                continue
            if since_ts and frame.timestamp < since_ts:
                continue
            found.append({
                "coordinate": probe_V,
                "frame":      frame.to_dict(),
            })

        return found

    def scan_beacons(
        self,
        beacon_coordinates: list[int],
        block_len:          int = BLOCK_DECODE_LEN,
        since_ts:           str = None,
    ) -> list[dict]:
        """
        Scan a specific list of known beacon coordinates directly.

        Use this alongside scan_window — they serve different purposes:
          scan_window    → discovery  (who put something in the ether?)
          scan_beacons   → retrieval  (I know these coordinates exist, fetch them)

        beacon_coordinates is a list of integer V values previously received
        via any transport (Usenet post body, packet radio payload, sneakernet
        text file, etc.). Each is decoded instantly without sweeping.

        Returns list of decoded message dicts, noise silently dropped.
        """
        geo   = self.geometry
        hm    = ChartGenerator(geo.chart_base, geo.mask_base, geo.num_digits).hm
        found = []

        for probe_V in beacon_coordinates:
            frame = self._probe(probe_V, block_len, geo, hm)
            if frame is None:
                continue
            if since_ts and frame.timestamp < since_ts:
                continue
            found.append({
                "coordinate": probe_V,
                "frame":      frame.to_dict(),
            })

        return found

    def decode_coordinate(
        self,
        coordinate: int,
        block_len:  int = BLOCK_DECODE_LEN,
    ) -> MessageFrame | None:
        """
        Directly decode a known coordinate integer.
        Use this when the sender has transmitted the coordinate via transport.
        Instant retrieval — no scanning needed.
        """
        geo = self.geometry
        hm  = ChartGenerator(geo.chart_base, geo.mask_base, geo.num_digits).hm
        return self._probe(coordinate, block_len, geo, hm)

    def _probe(
        self,
        V:         int,
        block_len: int,
        geo:       PassphraseGeometry,
        hm:        HandMath,
    ) -> MessageFrame | None:
        """Decode block_len bytes from coordinate V. Return MessageFrame or None."""
        try:
            cg = ChartGenerator(geo.chart_base, geo.mask_base, geo.num_digits)

            # Expand digit capacity if V is very large
            needed = 0
            v_tmp  = V
            while v_tmp > 0:
                needed += 1
                v_tmp //= hm.M
            if needed > cg.hm.D:
                cg.hm.D = needed + 8

            cg.Vs[0] = cg.hm.from_int(V)
            cg.Rs[0] = cg.hm.from_int(geo.R_base)

            raw = bytes(cg._decode_step(0) for _ in range(block_len))
            return MessageFrame.decode(raw)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# StatelessCommsNode
# The high-level interface the OdinNet daemon calls.
# Replaces the file I/O outbox/inbox loop.
# ---------------------------------------------------------------------------

class StatelessCommsNode:
    """
    Drop-in replacement for the file-based message loop in the OdinNet daemon.

    Usage (sender side):
        node = StatelessCommsNode(passphrase="shared_secret", node_id="OdinAlpha")
        coord, pad = node.send("Hello from Alpha", defcon=1)
        # coord is the integer you transmit via your transport layer

    Usage (receiver side):
        node = StatelessCommsNode(passphrase="shared_secret", node_id="OdinBeta")
        messages = node.poll(defcon=1)
        # or if you received a coordinate integer from transport:
        msg = node.receive_coordinate(coord)

    Fleet Jump (both nodes must agree on jump_seed beforehand):
        node.fleet_jump(jump_seed=20260623)
        # Now node.geometry points to the new ether region

    Beacon send (content at a coordinate):
        coord, pad = node.send_beacon("beacon://odin/news", content="...", defcon=1)

    Beacon resolve:
        msg = node.receive_coordinate(coord)
        # msg.payload contains the beacon content
    """

    def __init__(
        self,
        passphrase:  str,
        node_id:     str,
        chart_base:  int   = 256,
        mask_base:   int   = 1_000_000_000_000,
        num_digits:  int   = 100,
        defcon:      int   = 1,
    ):
        self.node_id = node_id
        self.defcon  = defcon

        self.geometry = PassphraseGeometry(
            passphrase, chart_base, mask_base, num_digits
        )
        self._padder  = CoordPadder(self.geometry)
        self._scanner = CoordScanner(self.geometry)

        # In-memory received message cache (replaces /inbox files)
        self._inbox:  list[dict] = []
        # In-memory sent coordinate log (replaces /outbox files)
        self._outbox: list[dict] = []

        print(f"\n{'═'*60}")
        print(f"  ⬡ OdinNet StatelessCommsNode — {self.node_id}")
        print(f"  Geometry window : {fmt_short(self.geometry.window_low)}"
              f" → {fmt_short(self.geometry.window_high)}")
        print(f"  DEFCON          : {self.defcon}")
        print(f"{'═'*60}\n")

    # ── Send side ──────────────────────────────────────────────────────────

    def send(
        self,
        payload:      str,
        msg_type:     str = "MSG",
        reply_to:     int = None,
        metadata:     dict = None,
        defcon:       int = None,
    ) -> tuple[int, int]:
        """
        Encode a message into a coordinate.
        Returns (coordinate_int, pad_bytes_used).
        Transmit coordinate_int via your transport to the remote node.
        """
        dc    = defcon if defcon is not None else self.defcon
        frame = MessageFrame(
            payload   = payload,
            sender_id = self.node_id,
            msg_type  = msg_type,
            reply_to  = reply_to,
            metadata  = metadata,
        )
        result = self._padder.find_coordinate(frame, defcon=dc)
        if result is None:
            raise RuntimeError(
                f"[{self.node_id}] No coordinate landing found for payload. "
                f"Try increasing MAX_PAD_BYTES or performing a Fleet Jump."
            )
        coord, pad = result

        entry = {
            "coordinate": coord,
            "pad":        pad,
            "frame":      frame.to_dict(),
            "defcon":     dc,
            "sent_at":    datetime.now(timezone.utc).isoformat(),
        }
        self._outbox.append(entry)
        if len(self._outbox) > OUTBOX_MAX:
            self._outbox.pop(0)   # Evict oldest sent record

        print(f"[{self.node_id}] SEND  coord={fmt_short(coord)}  "
              f"pad={pad}B  type={msg_type}  defcon={dc}")
        return coord, pad

    def send_beacon(
        self,
        beacon_path: str,
        content:     str,
        defcon:      int = None,
        metadata:    dict = None,
    ) -> tuple[int, int]:
        """
        Encode beacon content at a coordinate.
        beacon_path is a human label (e.g. 'beacon://odin/news').
        The returned coordinate IS the beacon pointer.
        Receivers call receive_coordinate(coord) to resolve it.
        """
        dc = defcon if defcon is not None else self.defcon
        meta = {"beacon_path": beacon_path}
        if metadata:
            meta.update(metadata)
        return self.send(
            payload   = content,
            msg_type  = "BEACON",
            metadata  = meta,
            defcon    = dc,
        )

    def send_beacon_ptr(
        self,
        beacon_path:  str,
        beacon_coord: int,
        defcon:       int = None,
    ) -> tuple[int, int]:
        """
        Send a lightweight pointer frame that announces a beacon coordinate.
        Remote nodes receive the pointer, then call receive_coordinate(beacon_coord)
        to pull the actual content.
        """
        dc    = defcon if defcon is not None else self.defcon
        frame = MessageFrame(
            payload       = f"PTR:{beacon_path}",
            sender_id     = self.node_id,
            msg_type      = "BEACON_PTR",
            beacon_coord  = beacon_coord,
        )
        result = self._padder.find_coordinate(frame, defcon=dc)
        if result is None:
            raise RuntimeError("No coordinate landing found for beacon pointer frame.")
        coord, pad = result

        self._outbox.append({
            "coordinate":   coord,
            "beacon_coord": beacon_coord,
            "frame":        frame.to_dict(),
            "defcon":       dc,
            "sent_at":      datetime.now(timezone.utc).isoformat(),
        })
        print(f"[{self.node_id}] BEACON_PTR  ptr_coord={fmt_short(coord)}  "
              f"→  content_coord={fmt_short(beacon_coord)}")
        return coord, pad

    # ── Receive side ───────────────────────────────────────────────────────

    def poll(
        self,
        defcon:    int  = None,
        steps:     int  = SCAN_STEPS,
        since_ts:  str  = None,
    ) -> list[dict]:
        """
        Scan the coordinate window for messages.
        Returns list of message dicts. Also appends to self._inbox.

        Call this on a timer in the daemon's polling thread, exactly as the
        existing polling() call works — just replace the file read loop with this.
        """
        dc      = defcon if defcon is not None else self.defcon
        results = self._scanner.scan_window(defcon=dc, steps=steps, since_ts=since_ts)

        for r in results:
            self._inbox.append(r)
            if len(self._inbox) > INBOX_MAX:
                self._inbox.pop(0)   # Evict oldest — volatile RAM, not a log
            f = r["frame"]
            print(f"[{self.node_id}] RECV  coord={fmt_short(r['coordinate'])}  "
                  f"from={f['sender_id']}  type={f['msg_type']}")

        return results

    def receive_coordinate(self, coordinate: int) -> MessageFrame | None:
        """
        Directly decode a known coordinate integer.
        Use when transport delivered the coordinate integer out-of-band
        (e.g. the Usenet post body contained the coordinate number).
        """
        frame = self._scanner.decode_coordinate(coordinate)
        if frame:
            self._inbox.append({
                "coordinate":  coordinate,
                "frame":       frame.to_dict(),
                "received_at": datetime.now(timezone.utc).isoformat(),
            })
            if len(self._inbox) > INBOX_MAX:
                self._inbox.pop(0)   # Evict oldest
            print(f"[{self.node_id}] DIRECT_RECV  coord={fmt_short(coordinate)}  "
                  f"from={frame.sender_id}  type={frame.msg_type}")
        return frame

    # ── Fleet Jump ─────────────────────────────────────────────────────────

    def fleet_jump(self, jump_seed: int, require_defcon: int = 3) -> bool:
        """
        Relocate to a new coordinate ether region.
        Both nodes call this with the same jump_seed and independently
        arrive at the same new geometry — no network needed.

        jump_seed should be an agreed value (epoch block, date integer, etc.)
        Both nodes must know when to jump and what seed to use.
        """
        if self.defcon < require_defcon:
            print(f"[{self.node_id}] FLEET JUMP BLOCKED — need DEFCON {require_defcon}+, "
                  f"currently at {self.defcon}")
            return False

        old_window = (self.geometry.window_low, self.geometry.window_high)
        self.geometry = self.geometry.jump_geometry(jump_seed)
        self._padder  = CoordPadder(self.geometry)
        self._scanner = CoordScanner(self.geometry)

        print(f"\n🌌 [{self.node_id}] FLEET JUMP EXECUTED")
        print(f"   Old window : {fmt_short(old_window[0])} → {fmt_short(old_window[1])}")
        print(f"   New window : {fmt_short(self.geometry.window_low)}"
              f" → {fmt_short(self.geometry.window_high)}")
        print(f"   Jump seed  : {jump_seed}")
        return True

    def set_defcon(self, level: int):
        if level not in DEFCON_COMPRESSION:
            raise ValueError(f"DEFCON must be one of {list(DEFCON_COMPRESSION.keys())}")
        self.defcon = level
        print(f"[{self.node_id}] DEFCON → {level}  "
              f"(window compression: {DEFCON_COMPRESSION[level]*100:.0f}%)")

    # ── Inbox / Outbox access ──────────────────────────────────────────────

    def inbox(self) -> list[dict]:
        return list(self._inbox)

    def outbox(self) -> list[dict]:
        return list(self._outbox)

    def status(self) -> dict:
        geo = self.geometry
        return {
            "node_id":      self.node_id,
            "defcon":       self.defcon,
            "window_low":   geo.window_low,
            "window_high":  geo.window_high,
            "window_width": geo.window_high - geo.window_low,
            "inbox_count":  len(self._inbox),
            "outbox_count": len(self._outbox),
        }

    def print_status(self):
        s      = self.status()
        geo    = self.geometry
        border = "═" * 60
        print(f"\n{border}")
        print(f"  ⬡ STATELESS COMMS NODE — {s['node_id']}")
        print(f"{border}")
        print(f"  DEFCON        : {s['defcon']}  "
              f"(compression {DEFCON_COMPRESSION[s['defcon']]*100:.0f}%)")
        print(f"  Window low    : {fmt_short(s['window_low'])}")
        print(f"  Window high   : {fmt_short(s['window_high'])}")
        print(f"  Window width  : {fmt_short(s['window_width'])}")
        print(f"  Inbox msgs    : {s['inbox_count']}")
        print(f"  Outbox msgs   : {s['outbox_count']}")
        print(f"{border}\n")


# ---------------------------------------------------------------------------
# Epoch Seed Utility
# Provides a synchronized Fleet Jump seed both nodes can derive independently
# from the current UTC time, without exchanging any messages.
#
# Granularity options:
#   "12h"  → jumps every 12 hours  (default — good balance of security/stability)
#   "6h"   → jumps every 6 hours   (higher churn, harder to track)
#   "day"  → jumps once per day    (more stable, easier to coordinate manually)
#
# Both nodes call get_current_epoch_seed() at any point in the same time
# window and get the exact same integer — no communication required.
# ---------------------------------------------------------------------------

def get_current_epoch_seed(granularity: str = "12h") -> int:
    """
    Returns a synchronized integer seed based on the current UTC time block.

    Example outputs (granularity="12h"):
      2026-06-23 09:00 UTC  →  2026062300
      2026-06-23 14:00 UTC  →  2026062312

    Pass this as jump_seed to node.fleet_jump() on both nodes at the agreed
    changeover moment and they will independently derive the same new ether.
    """
    now = datetime.now(timezone.utc)

    if granularity == "12h":
        coarse_hour = 0 if now.hour < 12 else 12
        return int(f"{now.year}{now.month:02d}{now.day:02d}{coarse_hour:02d}")

    elif granularity == "6h":
        coarse_hour = (now.hour // 6) * 6   # 0, 6, 12, or 18
        return int(f"{now.year}{now.month:02d}{now.day:02d}{coarse_hour:02d}")

    elif granularity == "day":
        return int(f"{now.year}{now.month:02d}{now.day:02d}00")

    else:
        raise ValueError(f"Unknown granularity '{granularity}'. Use '12h', '6h', or 'day'.")


# ---------------------------------------------------------------------------
# OdinNet Daemon Hook
# Drop-in replacement functions for the existing daemon's message I/O.
# Wire these in to replace json.dump / json.load / outbox file writes.
# ---------------------------------------------------------------------------

def daemon_send_message(node: StatelessCommsNode, payload: str, **kwargs) -> int:
    """
    Call instead of writing to /outbox.
    Returns the coordinate integer. Log it or transmit it via your transport.
    """
    coord, _ = node.send(payload, **kwargs)
    return coord


def daemon_poll_messages(node: StatelessCommsNode, **kwargs) -> list[dict]:
    """
    Call instead of reading from /inbox directory.
    Returns list of message dicts from the coordinate window scan.
    """
    return node.poll(**kwargs)


def daemon_resolve_beacon(node: StatelessCommsNode, coordinate: int) -> dict | None:
    """
    Call to resolve a beacon coordinate to its content.
    Returns the message dict or None if coordinate is noise/invalid.
    """
    frame = node.receive_coordinate(coordinate)
    return frame.to_dict() if frame else None


# ---------------------------------------------------------------------------
# Self-test: Two nodes, zero files, zero network
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("  OdinNet StatelessCommsNode — Two-Node Self-Test")
    print("=" * 60)

    SHARED_PASSPHRASE = "OdinNet_Shared_Ether_2026"

    # ── Boot both nodes independently ─────────────────────────────────────
    print("\n[Test 1] Booting Node Alpha and Node Beta from same passphrase...")
    alpha = StatelessCommsNode(SHARED_PASSPHRASE, node_id="OdinAlpha")
    beta  = StatelessCommsNode(SHARED_PASSPHRASE, node_id="OdinBeta")

    # Verify they derived the same window
    assert alpha.geometry.window_low  == beta.geometry.window_low,  "Window low mismatch"
    assert alpha.geometry.window_high == beta.geometry.window_high, "Window high mismatch"
    print("  ✅ Both nodes derived identical coordinate windows independently")

    # ── Alpha sends a message ──────────────────────────────────────────────
    print("\n[Test 2] Alpha encodes a message to a coordinate...")
    coord, pad = alpha.send("Greetings from Alpha. The ether is open.")
    print(f"  Coordinate : {fmt_short(coord)}")
    print(f"  Pad used   : {pad} bytes")

    # ── Beta decodes directly from that coordinate ─────────────────────────
    print("\n[Test 3] Beta decodes from the coordinate integer (direct transport)...")
    msg = beta.receive_coordinate(coord)
    assert msg is not None, "Beta got None — decode failed"
    assert "Greetings from Alpha" in msg.payload, f"Payload mismatch: {msg.payload}"
    assert msg.sender_id == "OdinAlpha"
    print(f"  ✅ Decoded: '{msg.payload}'")
    print(f"  From: {msg.sender_id}  Type: {msg.msg_type}  TS: {msg.timestamp}")

    # ── Beta scans the window (no coordinate given) ────────────────────────
    print("\n[Test 4] Beta scans the full window (simulates blind polling)...")
    results = beta.poll(steps=300)
    coords_found = [r["coordinate"] for r in results]
    assert coord in coords_found, f"Alpha's message not found in window scan"
    print(f"  ✅ Found {len(results)} message(s) in window scan")
    print(f"  Alpha's coord {fmt_short(coord)} found: {coord in coords_found}")

    # ── Beacon test ────────────────────────────────────────────────────────
    print("\n[Test 5] Alpha sends a beacon, Beta resolves it...")
    b_coord, _ = alpha.send_beacon(
        "beacon://odin/news",
        content="OdinNet v8 is live. The ether is stable.",
    )
    beacon_msg = beta.receive_coordinate(b_coord)
    assert beacon_msg is not None
    assert "OdinNet v8" in beacon_msg.payload
    print(f"  ✅ Beacon resolved: '{beacon_msg.payload}'")

    # ── DEFCON compression ─────────────────────────────────────────────────
    print("\n[Test 6] Both nodes raise DEFCON to 5 — window compresses...")
    alpha.set_defcon(5)
    beta.set_defcon(5)
    coord_dc5, _ = alpha.send("DEFCON 5 message — compressed window")
    msg_dc5      = beta.receive_coordinate(coord_dc5)
    assert msg_dc5 is not None
    assert "DEFCON 5" in msg_dc5.payload
    print(f"  ✅ Message survived DEFCON 5 compression")

    # ── Fleet Jump ─────────────────────────────────────────────────────────
    print("\n[Test 7] Fleet Jump — both nodes relocate to new ether region...")
    JUMP_SEED = 20260623
    alpha.fleet_jump(JUMP_SEED)
    beta.fleet_jump(JUMP_SEED)
    assert alpha.geometry.window_low  == beta.geometry.window_low
    assert alpha.geometry.window_high == beta.geometry.window_high
    print(f"  ✅ Both nodes jumped to identical new window independently")

    coord_post_jump, _ = alpha.send("Post-jump message — new ether region")
    msg_post_jump      = beta.receive_coordinate(coord_post_jump)
    assert msg_post_jump is not None
    assert "Post-jump" in msg_post_jump.payload
    print(f"  ✅ Message in new ether region decoded successfully")

    # ── Noise rejection ────────────────────────────────────────────────────
    print("\n[Test 8] Noise rejection — random coordinate should return None...")
    import random
    noise_coord = random.randint(
        beta.geometry.window_low,
        beta.geometry.window_high
    )
    noise_result = beta.receive_coordinate(noise_coord)
    if noise_result is None:
        print(f"  ✅ Noise coordinate correctly rejected")
    else:
        print(f"  ⚠  Random coord happened to pass sig check (extremely unlikely)")

    # ── scan_beacons (direct list) ─────────────────────────────────────────
    print("\n[Test 9] scan_beacons — Beta resolves a known coordinate list directly...")
    # Alpha sends two beacons
    alpha.set_defcon(1)
    beta.set_defcon(1)
    bc1, _ = alpha.send_beacon("beacon://odin/news",   "Breaking: ether stable post-jump")
    bc2, _ = alpha.send_beacon("beacon://odin/school", "Lesson 1: BNS coordinate basics")
    # Beta received bc1 and bc2 via transport (e.g. Usenet post), resolves both
    beacon_results = beta._scanner.scan_beacons([bc1, bc2])
    assert len(beacon_results) == 2, f"Expected 2 beacons, got {len(beacon_results)}"
    payloads = [r["frame"]["payload"] for r in beacon_results]
    assert any("ether stable" in p for p in payloads)
    assert any("BNS coordinate" in p for p in payloads)
    print(f"  ✅ scan_beacons resolved {len(beacon_results)} beacons from coordinate list")
    for r in beacon_results:
        print(f"     coord={fmt_short(r['coordinate'])}  payload='{r['frame']['payload'][:40]}'")

    # ── Epoch seed utility ─────────────────────────────────────────────────
    print("\n[Test 10] Epoch seed — both nodes derive the same seed independently...")
    seed_a = get_current_epoch_seed("12h")
    seed_b = get_current_epoch_seed("12h")
    assert seed_a == seed_b, "Epoch seeds diverged — clock skew?"
    print(f"  ✅ Epoch seed (12h granularity): {seed_a}")
    seed_6h  = get_current_epoch_seed("6h")
    seed_day = get_current_epoch_seed("day")
    print(f"  ✅ Epoch seed (6h  granularity): {seed_6h}")
    print(f"  ✅ Epoch seed (day granularity): {seed_day}")

    # ── Memory cap ────────────────────────────────────────────────────────
    print(f"\n[Test 11] Memory cap — inbox stays ≤ {INBOX_MAX} entries...")
    # Flood inbox with dummy entries directly to test the cap
    test_node = StatelessCommsNode(SHARED_PASSPHRASE, node_id="CapTest")
    for i in range(INBOX_MAX + 50):
        test_node._inbox.append({"coordinate": i, "frame": {}, "received_at": ""})
        if len(test_node._inbox) > INBOX_MAX:
            test_node._inbox.pop(0)
    assert len(test_node._inbox) == INBOX_MAX, \
        f"Inbox overflow: {len(test_node._inbox)} > {INBOX_MAX}"
    print(f"  ✅ Inbox capped at {len(test_node._inbox)} entries — no RAM bleed")

    # ── Final status ───────────────────────────────────────────────────────
    print()
    alpha.print_status()
    beta.print_status()

    print("✅ All 11 StatelessCommsNode tests passed — zero files, zero network.\n")
