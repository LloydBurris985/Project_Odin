"""
stateless_comms.py — OdinNet Stateless Communications Layer
=============================================================
Fileless, passphrase-derived coordinate communication.

All coordinate state is derived mathematically from a shared passphrase —
no JSON coordinate files, no disk reads during normal operation.

Classes
-------
PassphraseGeometry
    Derives (V, R) coordinate landmarks purely from a passphrase.
    Both parties run the same math independently; results are identical.

MessageFrame
    Builds / parses the temporal message header format used across
    all OdinNet transports.

CoordPadder
    Iteratively pads a payload with null bytes until its encoded
    coordinate lands inside the target polling window.

CoordScanner
    Walks a coordinate range and decodes candidate message frames.

StatelessCommsNode
    Top-level API used by odinnet_daemon.py.
    Exposes: send(), poll(), inbox(), status()

Transport note
--------------
BNS provides *stateless coordinate agreement* — both nodes sharing a
passphrase independently derive identical coordinate windows.  A transport
layer (daemon socket, Usenet feed, packet radio, etc.) still carries the
raw coordinate integers between nodes.  The mathematical ether is the
shared medium, not a replacement for transport.

Compatibility with GrokComms
-----------------------------
StatelessCommsNode is intentionally API-compatible with the subset of
GrokComms used by OdinNetGUIHandler:
    node.send(text)          → (coordinate: int, pad_bytes: int)
    node.poll(steps=N)       → list[dict]
    node.inbox()             → list[dict]
    node.status()            → dict
"""

import math
import os
import random
import threading
import uuid
from datetime import datetime, date

# ---------------------------------------------------------------------------
# Import the real ChartGenerator; fall back to a safe mock so this module
# can be imported and tested without the full BNS engine present.
# ---------------------------------------------------------------------------
try:
    from chart_generator import ChartGenerator as _RealCG
    _HAVE_REAL_CG = True
except ImportError:
    _HAVE_REAL_CG = False
    _RealCG = None


class _MockHandMath:
    """Minimal stand-in for ChartGenerator.hm when the real engine is absent."""
    def to_int(self, val):
        return sum(int(x) for x in val) if isinstance(val, list) else int(val)

    def from_int(self, n):
        return [n]

    def serialize(self, val):
        return str(val)

    def deserialize(self, s):
        try:
            return [int(s)]
        except Exception:
            return [0]


class _MockChartGenerator:
    """
    Stand-in for ChartGenerator used when chart_generator.py is absent.
    Produces deterministic but mathematically trivial coordinates —
    good enough for integration testing without the full BNS engine.
    """
    def __init__(self, chart_base=256, mask_base=1_000_000_000_000,
                 num_digits=150, num_n_streams=12):
        self.chart_base    = chart_base
        self.mask_base     = mask_base
        self.num_digits    = num_digits
        self.num_n_streams = num_n_streams
        self.Vs            = [[1000] * num_digits]
        self.Rs            = [[500]  * num_digits]
        self.hm            = _MockHandMath()

    def _encode_step(self, byte_val: int, stream_idx: int):
        self.Vs[0][0] = (self.Vs[0][0] + byte_val * 7) % self.mask_base
        self.Rs[0][0] = (self.Rs[0][0] + byte_val * 3) % self.mask_base

    def _decode_step(self, stream_idx: int) -> int:
        val = (self.Vs[0][0] // 7) % 256
        return val if val != 0 else 32


def _make_cg(chart_base=256, mask_base=1_000_000_000_000,
             num_digits=150, num_n_streams=12):
    """Factory: returns a real or mock ChartGenerator instance."""
    cls = _RealCG if _HAVE_REAL_CG else _MockChartGenerator
    return cls(
        chart_base    = chart_base,
        mask_base     = mask_base,
        num_digits    = num_digits,
        num_n_streams = num_n_streams,
    )


def _cg_from_state(state: dict):
    """Restore a ChartGenerator from a PassphraseGeometry state dict."""
    cg = _make_cg(
        chart_base    = state["chart_base"],
        mask_base     = state["mask_base"],
        num_digits    = state["num_digits"],
        num_n_streams = state["num_n_streams"],
    )
    if _HAVE_REAL_CG:
        cg.Vs[0] = cg.hm.deserialize(state["V_serial"])
        cg.Rs[0] = cg.hm.deserialize(state["R_serial"])
    else:
        cg.Vs[0] = list(state["V_raw"])
        cg.Rs[0] = list(state["R_raw"])
    return cg


# ---------------------------------------------------------------------------
# Integer-safe statistics (avoids float overflow on Burris large integers)
# ---------------------------------------------------------------------------

def _int_mean(samples: list) -> int:
    return sum(samples) // len(samples)

def _int_std_dev(samples: list, mean: int = None) -> int:
    if len(samples) < 2:
        return 1
    if mean is None:
        mean = _int_mean(samples)
    variance = sum((s - mean) ** 2 for s in samples) // len(samples)
    return math.isqrt(variance) or 1


# ===========================================================================
# PassphraseGeometry
# ===========================================================================

class PassphraseGeometry:
    """
    Derives the base (V, R) coordinate landmarks purely from a passphrase.

    Both communicating parties run this independently with the same
    passphrase and obtain identical results — no file exchange needed.

    Parameters
    ----------
    passphrase   : shared secret string
    chart_base   : ChartGenerator BASE (default 256)
    mask_base    : modulus ceiling (default 1_000_000_000_000)
    num_digits   : coordinate width in limbs (default 150)
    num_n_streams: stream count (default 12)
    num_samples  : samples used to compute the polling window (default 30)
    """

    def __init__(
        self,
        passphrase:    str,
        chart_base:    int = 256,
        mask_base:     int = 1_000_000_000_000,
        num_digits:    int = 150,
        num_n_streams: int = 12,
        num_samples:   int = 30,
    ):
        self.passphrase    = passphrase
        self.chart_base    = chart_base
        self.mask_base     = mask_base
        self.num_digits    = num_digits
        self.num_n_streams = num_n_streams
        self.num_samples   = num_samples

        self._state        = None   # cached geometry state dict
        self._window       = None   # cached (low, high) tuple
        self._lock         = threading.Lock()

    # ── Internal helpers ──────────────────────────────────────────────────

    def _derive_rand_payload(self) -> bytes:
        """Deterministic random bytes seeded by passphrase."""
        seed = self.passphrase.encode("utf-8")
        rng  = random.Random(seed)
        return bytes(rng.randint(0, 255) for _ in range(self.num_digits))

    def _build_base_cg(self) -> object:
        """Run the passphrase payload through encode to set V/R baseline."""
        payload = self._derive_rand_payload()
        cg      = _make_cg(self.chart_base, self.mask_base,
                           self.num_digits, self.num_n_streams)
        for i in range(len(payload) - 1, -1, -1):
            cg._encode_step(payload[i], 0)
        return cg

    # ── Public API ────────────────────────────────────────────────────────

    def state(self) -> dict:
        """
        Return (and cache) the geometry state dict.
        Contains everything needed to reconstruct a ChartGenerator
        at the passphrase-derived baseline.
        """
        with self._lock:
            if self._state is not None:
                return self._state

            cg    = self._build_base_cg()
            V_int = cg.hm.to_int(cg.Vs[0])

            self._state = {
                "chart_base":    self.chart_base,
                "mask_base":     self.mask_base,
                "num_digits":    self.num_digits,
                "num_n_streams": self.num_n_streams,
                # Raw list form (always available)
                "V_raw":         list(cg.Vs[0]),
                "R_raw":         list(cg.Rs[0]),
                # Serialized form (for real ChartGenerator.hm.deserialize)
                "V_serial":      cg.hm.serialize(cg.Vs[0]),
                "R_serial":      cg.hm.serialize(cg.Rs[0]),
                "V_int":         V_int,
            }
            return self._state

    def polling_window(self) -> tuple:
        """
        Compute and cache the asymmetric polling window.
        Returns (polling_low, polling_high).

        Window formula (mirrors GrokComms polling_range_finder):
            low  = mean + 0.4 × std_dev
            high = mean + 3.5 × std_dev
        """
        with self._lock:
            if self._window is not None:
                return self._window

        st      = self.state()
        samples = []

        for i in range(self.num_samples):
            rng  = random.Random(st["V_int"] + i)
            data = bytes(rng.randint(0, 255) for _ in range(64))
            cg   = _cg_from_state(st)
            for j in range(len(data) - 1, -1, -1):
                cg._encode_step(data[j], 0)
            samples.append(cg.hm.to_int(cg.Vs[0]))

        mean    = _int_mean(samples)
        std_dev = _int_std_dev(samples, mean)
        low     = mean + (4  * std_dev) // 10
        high    = mean + (35 * std_dev) // 10

        with self._lock:
            self._window = (low, high)
        return self._window


# ===========================================================================
# MessageFrame
# ===========================================================================

HEADER_SEP   = "---"
HEADER_LINES = 5

class MessageFrame:
    """
    Builds and parses the OdinNet temporal message header.

    Header format (identical to GrokComms temporal header so both
    systems can read each other's messages):

        FROM_DATE   YYYY-MM-DD
        TO_DATE     YYYY-MM-DD
        FROM_TIME   HH:MM:SS
        RECV_TIME   ----------
        TUPLE_HASH  NNN
        ---
        [optional SUBJECT line]
        <payload body>
    """

    @staticmethod
    def today() -> str:
        return date.today().isoformat()

    @staticmethod
    def now_str() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @classmethod
    def build(cls, body: str, to_date: str = None, subject: str = "") -> str:
        td = to_date or cls.today()
        lines = [
            f"FROM_DATE   {cls.today()}",
            f"TO_DATE     {td}",
            f"FROM_TIME   {datetime.now().strftime('%H:%M:%S')}",
            f"RECV_TIME   ----------",
            f"TUPLE_HASH  ???",
            HEADER_SEP,
        ]
        if subject:
            lines.append(f"SUBJECT     {subject}")
        full = "\n".join(lines) + "\n" + body
        # Stamp TUPLE_HASH with last byte of encoded payload
        last_byte = full.encode("utf-8")[-1]
        return full.replace("TUPLE_HASH  ???", f"TUPLE_HASH  {last_byte:03d}")

    @staticmethod
    def parse(text: str) -> dict | None:
        """Parse header; return dict with 'payload' key, or None if no header found."""
        lines   = text.splitlines()
        h       = {}
        sep_idx = None
        for i, line in enumerate(lines):
            if line.strip() == HEADER_SEP:
                sep_idx = i
                break
            parts = line.split(None, 1)
            if len(parts) == 2:
                h[parts[0].lower()] = parts[1].strip()
        if sep_idx is None:
            return None
        h["payload"] = "\n".join(lines[sep_idx + 1:])
        return h

    @staticmethod
    def verify_hash(text: str, claimed: str) -> bool:
        try:
            return text.encode("utf-8")[-1] == int(claimed)
        except Exception:
            return False

    @staticmethod
    def temporal_filter(header: dict) -> bool:
        """Return True if TO_DATE is today or in the past (message is due)."""
        try:
            return date.fromisoformat(header.get("to_date", "")) <= date.today()
        except ValueError:
            return False


# ===========================================================================
# CoordPadder
# ===========================================================================

class CoordPadder:
    """
    Iteratively pads a text payload with null bytes until its encoded
    coordinate lands inside [polling_low, polling_high].

    Mirrors GrokComms.range_padder but operates on raw text rather
    than a JSON file path.
    """

    def __init__(self, geometry: PassphraseGeometry):
        self.geometry = geometry

    def pad(self, text: str) -> tuple:
        """
        Returns (padded_text: str, final_coordinate: int).
        Raises RuntimeError if window cannot be hit within max_pad attempts.
        """
        st        = self.geometry.state()
        low, high = self.geometry.polling_window()
        payload   = text.encode("utf-8")
        max_pad   = st["num_digits"] * 4

        for pad in range(max_pad):
            test_data = payload + b'\x00' * pad
            cg        = _cg_from_state(st)
            for i in range(len(test_data) - 1, -1, -1):
                cg._encode_step(test_data[i], 0)
            final_V = cg.hm.to_int(cg.Vs[0])
            if low <= final_V <= high:
                return text + '\x00' * pad, final_V

        raise RuntimeError(
            f"CoordPadder: coordinate convergence failure after {max_pad} attempts. "
            f"Window=[{low}…{high}]. Run PassphraseGeometry with more samples."
        )


# ===========================================================================
# CoordScanner
# ===========================================================================

class CoordScanner:
    """
    Walks [low, high] in `steps` probes, decoding `block_len` bytes at
    each coordinate and checking for a valid MessageFrame header.
    """

    def __init__(self, geometry: PassphraseGeometry, block_len: int = 64):
        self.geometry  = geometry
        self.block_len = block_len

    def scan(self, low: int = None, high: int = None,
             steps: int = 100) -> list:
        """
        Returns list of message record dicts for every coordinate that
        yields a valid, temporally-due header.
        """
        st = self.geometry.state()
        if low is None or high is None:
            low, high = self.geometry.polling_window()

        span      = high - low
        step_size = max(1, span // steps)
        results   = []

        for i in range(steps):
            probe_V = low + i * step_size
            cg      = _cg_from_state(st)
            cg.Vs[0] = cg.hm.from_int(probe_V)

            try:
                decoded = [cg._decode_step(0) for _ in range(self.block_len)]
                text    = bytes(decoded).decode("utf-8", errors="replace")
            except Exception:
                continue

            header = MessageFrame.parse(text)
            if header is None:
                continue
            if not MessageFrame.temporal_filter(header):
                continue

            hash_ok = MessageFrame.verify_hash(text, header.get("tuple_hash", "-1"))
            results.append({
                "source":     "stateless_scan",
                "probe_V":    str(probe_V)[:30],
                "from_date":  header.get("from_date"),
                "to_date":    header.get("to_date"),
                "from_time":  header.get("from_time"),
                "recv_time":  MessageFrame.now_str(),
                "tuple_hash": header.get("tuple_hash"),
                "hash_ok":    hash_ok,
                "subject":    header.get("subject", "(no subject)"),
                "payload":    header.get("payload", ""),
                "raw_text":   text,
            })

        return results


# ===========================================================================
# StatelessCommsNode
# ===========================================================================

class StatelessCommsNode:
    """
    Top-level stateless communications node.

    API surface used by odinnet_daemon.py
    ---------------------------------------
    node.send(text)         → (coordinate: int, pad_bytes: int)
    node.poll(steps=100)    → list[dict]
    node.inbox()            → list[dict]   (received message log)
    node.status()           → dict         (node health / metrics)

    Parameters
    ----------
    passphrase : shared passphrase — both sides must use the same value
    node_id    : optional human-readable node label (default: auto UUID)
    block_len  : bytes decoded per coordinate probe (default 64)
    """

    def __init__(
        self,
        passphrase: str,
        node_id:    str = None,
        block_len:  int = 64,
    ):
        self.passphrase = passphrase
        self.node_id    = node_id or ("node-" + str(uuid.uuid4())[:8])
        self.block_len  = block_len

        self.geometry   = PassphraseGeometry(passphrase)
        self.padder     = CoordPadder(self.geometry)
        self.scanner    = CoordScanner(self.geometry, block_len=block_len)

        self._lock          = threading.RLock()
        self._inbox         = []        # list of received message dicts
        self._sent_log      = []        # list of sent coordinate records
        self._poll_count    = 0
        self._start_time    = datetime.now()
        self._last_poll_ts  = None
        self._last_seen_vs  = set()     # de-duplicate received probe_V values

        # Pre-compute geometry in background so first poll isn't slow
        self._ready_event   = threading.Event()
        threading.Thread(target=self._warm_up, daemon=True).start()

    # ── Internal ──────────────────────────────────────────────────────────

    def _warm_up(self):
        try:
            self.geometry.state()
            self.geometry.polling_window()
        except Exception as e:
            print(f"[StatelessNode] Warm-up error: {e}", flush=True)
        finally:
            self._ready_event.set()

    # ── Public API ────────────────────────────────────────────────────────

    def send(self, text: str, subject: str = "", to_date: str = None) -> tuple:
        """
        Build a framed message, pad it into the coordinate window,
        and return (coordinate, pad_bytes).

        The coordinate integer is what gets transmitted to the remote
        node (via whatever transport layer is in use).

        Returns
        -------
        (coordinate: int, pad_bytes: int)
        """
        self._ready_event.wait(timeout=30)

        framed          = MessageFrame.build(text, to_date=to_date, subject=subject)
        padded, final_V = self.padder.pad(framed)
        pad_bytes       = len(padded) - len(framed)

        record = {
            "coordinate": final_V,
            "pad_bytes":  pad_bytes,
            "subject":    subject,
            "preview":    text[:60],
            "sent_at":    MessageFrame.now_str(),
            "node_id":    self.node_id,
        }
        with self._lock:
            self._sent_log.append(record)
            if len(self._sent_log) > 200:
                self._sent_log = self._sent_log[-200:]

        print(f"[StatelessNode] Sent  coord={str(final_V)[:24]}...  "
              f"pad={pad_bytes}b", flush=True)
        return final_V, pad_bytes

    def poll(self, steps: int = 100) -> list:
        """
        Scan the coordinate window for incoming messages.
        New messages are appended to the internal inbox.
        Returns the list of newly received message records.
        """
        self._ready_event.wait(timeout=30)

        low, high = self.geometry.polling_window()
        hits      = self.scanner.scan(low=low, high=high, steps=steps)

        new_hits = []
        with self._lock:
            for hit in hits:
                pv = hit.get("probe_V", "")
                if pv not in self._last_seen_vs:
                    self._last_seen_vs.add(pv)
                    self._inbox.append(hit)
                    new_hits.append(hit)
                    if len(self._last_seen_vs) > 5000:
                        self._last_seen_vs = set(list(self._last_seen_vs)[-5000:])

            if len(self._inbox) > 500:
                self._inbox = self._inbox[-500:]

            self._poll_count     += 1
            self._last_poll_ts    = MessageFrame.now_str()

        print(f"[StatelessNode] Poll #{self._poll_count}  "
              f"new={len(new_hits)}  inbox={len(self._inbox)}", flush=True)
        return new_hits

    def inbox(self) -> list:
        """
        Return current inbox as a list of message dicts (newest first).
        Safe to call from the HTTP handler thread.
        """
        with self._lock:
            return list(reversed(self._inbox))

    def status(self) -> dict:
        """
        Return a status dict compatible with the /api/status endpoint.
        Keys overlap with GrokComms-era status so the dashboard JS needs
        no changes.
        """
        low, high = (None, None)
        try:
            low, high = self.geometry.polling_window()
        except Exception:
            pass

        with self._lock:
            uptime = int((datetime.now() - self._start_time).total_seconds())
            return {
                "engine":        "stateless",
                "node_id":       self.node_id,
                "passphrase_hint": self.passphrase[:4] + "****",
                "uptime_sec":    uptime,
                "poll_count":    self._poll_count,
                "last_poll":     self._last_poll_ts,
                "inbox_count":   len(self._inbox),
                "sent_count":    len(self._sent_log),
                "polling_low":   str(low)[:30] if low else "pending",
                "polling_high":  str(high)[:30] if high else "pending",
                "ready":         self._ready_event.is_set(),
                "engine_backend": "real" if _HAVE_REAL_CG else "mock",
            }
