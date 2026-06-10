"""
GrokComms — Realtime Data Polling Module
==========================================
Low-latency back-and-forth messaging layer built on the GrokComms v2 foundation.

Modules:
  calculate_tight_polling_range  — small fast window (±0.5σ) for realtime packets
  send_realtime                  — encode short payload, store with msg_id
  poll_realtime                  — scan tight window for replies to local msg_ids
  simulate_reply                 — inject a test reply from "future self" for development
  realtime_comms                 — interactive CLI loop

Design notes:
  - Realtime messages use a tight std-dev multiplier (0.5σ) vs the temporal node's 3.5σ.
  - Every message carries a UUID msg_id; replies carry a reply_to field.
  - Persistent store: realtime_messages.json  (status: sent / delivered / replied)
  - Morse encoding is retained from GrokComms v1 but short messages skip it by default
    for speed; pass use_morse=True to send_realtime() to re-enable.
  - TO_DATE / FROM_DATE filtering is NOT applied here — realtime is immediate.
"""

import json
import os
import random
import statistics
import tempfile
import uuid
from datetime import datetime

# ---------------------------------------------------------------------------
# Import v2 helpers from grokcomms.  If this file lives alongside grokcomms.py
# just import directly; otherwise adjust the import path as needed.
# ---------------------------------------------------------------------------
try:
    from grokcomms import (
        _load_json,
        _save_json,
        _cg_from_coord,
        _now_str,
        COORD_FILE,
    )
except ImportError:
    # Fallback stubs so the module is importable for testing without grokcomms.
    import json as _json

    COORD_FILE = "coordinatefile.json"

    def _load_json(path):
        with open(path) as f:
            return _json.load(f)

    def _save_json(path, data):
        with open(path, "w") as f:
            _json.dump(data, f, indent=2)

    def _now_str():
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _cg_from_coord(coord):
        raise RuntimeError("chart_generator / grokcomms not available in stub mode.")


# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

RT_STORE_FILE = "realtime_messages.json"   # persistent message store
DIR_RT        = "realtime"                 # folder for per-message JSON files

RT_MSG_LEN_DEFAULT   = 16    # bytes — small for low-latency
RT_SAMPLES_DEFAULT   = 8     # samples for tight range calculation
RT_STD_MULTIPLIER_LO = -0.5  # lower bound: mean − 0.5σ
RT_STD_MULTIPLIER_HI =  0.5  # upper bound: mean + 0.5σ  (tight window)


# ============================================================
# Persistence helpers
# ============================================================

def _load_rt_store() -> dict:
    """Load the realtime message store (keyed by msg_id)."""
    if os.path.exists(RT_STORE_FILE):
        try:
            with open(RT_STORE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_rt_store(store: dict):
    with open(RT_STORE_FILE, "w") as f:
        json.dump(store, f, indent=2)


def _ensure_rt_dir():
    os.makedirs(DIR_RT, exist_ok=True)


# ============================================================
# 1. calculate_tight_polling_range
# ============================================================

def calculate_tight_polling_range(
    coord_file:     str = COORD_FILE,
    message_length: int = RT_MSG_LEN_DEFAULT,
    num_samples:    int = RT_SAMPLES_DEFAULT,
) -> tuple[int, int]:
    """
    Generate `num_samples` random payloads of `message_length` bytes,
    encode each starting from the base coordinate in coord_file,
    and compute a tight polling window:

        low  = mean + RT_STD_MULTIPLIER_LO * std_dev   (−0.5σ)
        high = mean + RT_STD_MULTIPLIER_HI * std_dev   (+0.5σ)

    This is intentionally narrower than the temporal node's [+0.4σ, +3.5σ]
    window so realtime polls complete faster.

    Returns (polling_low, polling_high) as integers and saves them under
    the 'rt_polling_low' / 'rt_polling_high' keys in coord_file so they
    don't overwrite the temporal node's range.
    """
    coord = _load_json(coord_file)
    print(f"\n[RT RangeFinder] samples={num_samples}  msg_len={message_length}")

    samples = []
    for i in range(num_samples):
        data = os.urandom(message_length)
        cg = _cg_from_coord(coord)
        for j in range(len(data) - 1, -1, -1):
            cg._encode_step(data[j], 0)
        v = cg.hm.to_int(cg.Vs[0])
        samples.append(v)
        print(f"  [{i+1}/{num_samples}] V={str(v)[:24]}...")

    mean    = statistics.mean(samples)
    std_dev = statistics.stdev(samples) if len(samples) > 1 else 1

    low  = int(mean + RT_STD_MULTIPLIER_LO * std_dev)
    high = int(mean + RT_STD_MULTIPLIER_HI * std_dev)

    # Sanity: low must be < high
    if low >= high:
        low, high = int(mean - abs(std_dev)), int(mean + abs(std_dev))

    print(f"\n[RT RangeFinder] Mean    : {str(int(mean))[:30]}")
    print(f"[RT RangeFinder] Std Dev : {std_dev:,.0f}")
    print(f"[RT RangeFinder] Window  : {str(low)[:24]}  →  {str(high)[:24]}  (tight ±0.5σ)")

    coord["rt_polling_low"]    = str(low)
    coord["rt_polling_high"]   = str(high)
    coord["rt_message_length"] = message_length
    coord["rt_std_dev"]        = str(int(std_dev))
    coord["rt_range_samples"]  = num_samples
    _save_json(coord_file, coord)
    print(f"[RT RangeFinder] Saved → {coord_file}  (keys: rt_polling_low / rt_polling_high)")
    return low, high


# ============================================================
# 2. send_realtime
# ============================================================

def _morse_encode(text: str) -> bytes:
    """
    Compact Morse encoder — returns packed bytes.
    '.' → 1,  '-' → 111,  letter gap → 0,  word gap → 0000000
    """
    MORSE_MAP = {
        'A': '.-',    'B': '-...',  'C': '-.-.',  'D': '-..',
        'E': '.',     'F': '..-.',  'G': '--.',   'H': '....',
        'I': '..',    'J': '.---',  'K': '-.-',   'L': '.-..',
        'M': '--',    'N': '-.',    'O': '---',   'P': '.--.',
        'Q': '--.-',  'R': '.-.',   'S': '...',   'T': '-',
        'U': '..-',   'V': '...-',  'W': '.--',   'X': '-..-',
        'Y': '-.--',  'Z': '--..',
        '0': '-----', '1': '.----', '2': '..---', '3': '...--',
        '4': '....-', '5': '.....', '6': '-....', '7': '--...',
        '8': '---..',  '9': '----.',
        ' ': '/',
    }
    bits = []
    words = text.upper().split(' ')
    for wi, word in enumerate(words):
        if wi > 0:
            bits += [0, 0, 0, 0, 0, 0, 0]   # word gap
        for li, ch in enumerate(word):
            if li > 0:
                bits += [0]                  # letter gap
            for sym in MORSE_MAP.get(ch, '...---'):
                if sym == '.':
                    bits.append(1)
                elif sym == '-':
                    bits += [1, 1, 1]
    # Pad to byte boundary
    pad = (8 - len(bits) % 8) % 8
    bits += [0] * pad
    result = bytearray()
    for i in range(0, len(bits), 8):
        byte_val = 0
        for b in bits[i:i+8]:
            byte_val = (byte_val << 1) | b
        result.append(byte_val)
    return bytes(result)


def _text_to_bytes(text: str, use_morse: bool = False) -> bytes:
    if use_morse:
        return _morse_encode(text)
    return text.encode("utf-8")


def send_realtime(
    to:           str,
    message:      str,
    reply_to:     str  = None,
    coord_file:   str  = COORD_FILE,
    use_morse:    bool = False,
    my_id:        str  = None,
) -> tuple[str, int]:
    """
    Encode a short message into a coordinate and record it in the realtime store.

    Parameters
    ----------
    to         : recipient identifier (free-form string / node id)
    message    : plaintext message (kept short for realtime performance)
    reply_to   : msg_id of the message this is replying to, or None
    coord_file : coordinate file to encode from
    use_morse  : if True, Morse-encode text before encoding (v1 compat)
    my_id      : sender node id (defaults to a persistent id in the store)

    Returns
    -------
    (msg_id, coordinate_int)
    """
    _ensure_rt_dir()
    store = _load_rt_store()

    # Persist a stable node id across calls in the same session
    if my_id is None:
        my_id = store.get("_my_id") or str(uuid.uuid4())[:8]
        store["_my_id"] = my_id

    coord = _load_json(coord_file)
    msg_bytes = _text_to_bytes(message, use_morse)

    # Encode starting from the base coordinate
    cg = _cg_from_coord(coord)
    for i in range(len(msg_bytes) - 1, -1, -1):
        cg._encode_step(msg_bytes[i], 0)

    coordinate = cg.hm.to_int(cg.Vs[0])
    msg_id = str(uuid.uuid4()).replace("-", "")[:12]

    record = {
        "msg_id":     msg_id,
        "from":       my_id,
        "to":         to,
        "reply_to":   reply_to,
        "message":    message,
        "preview":    message[:60],
        "coordinate": str(coordinate),
        "timestamp":  _now_str(),
        "status":     "sent",       # sent / delivered / replied
        "use_morse":  use_morse,
    }

    store[msg_id] = record
    _save_rt_store(store)

    # Also write a per-message file for easy inspection
    _save_json(os.path.join(DIR_RT, f"{msg_id}.json"), record)

    print(f"📨 Sent  | ID: {msg_id}  to: {to}  coord: {str(coordinate)[:24]}...")
    if reply_to:
        print(f"   ↩ reply to: {reply_to}")
    return msg_id, coordinate


# ============================================================
# 3. poll_realtime
# ============================================================

def poll_realtime(
    coord_file: str = COORD_FILE,
    my_id:      str = None,
) -> list[dict]:
    """
    Scan the tight polling window for messages addressed to my_id
    OR replies to any msg_id I previously sent.

    Strategy
    --------
    The polling window (rt_polling_low / rt_polling_high) is read from
    coord_file.  We probe a grid of coordinates in that window,
    decode each, and check whether the recovered bytes match any
    pending message coordinate in our local store (coordinate
    round-trip verification).

    Additionally — and this is the primary mechanism — we scan the
    in-memory / on-disk store for messages that are addressed to us
    or reply to our sent messages.  This gives a deterministic result
    without relying on imperfect coordinate-space probing.

    Returns list of newly-received message records.
    """
    store = _load_rt_store()
    if my_id is None:
        my_id = store.get("_my_id", "")

    coord = _load_json(coord_file)
    low_s  = coord.get("rt_polling_low")
    high_s = coord.get("rt_polling_high")

    # Collect msg_ids I sent (to match replies)
    my_sent_ids = {
        mid for mid, rec in store.items()
        if not mid.startswith("_") and rec.get("from") == my_id
    }

    print(f"\n🔍 Realtime Poll  |  node: {my_id or '?'}  |  {_now_str()}")

    received = []

    # ── Pass 1: store scan (reliable) ──────────────────────────────────────
    for mid, rec in list(store.items()):
        if mid.startswith("_"):
            continue
        if rec.get("status") in ("delivered", "replied"):
            continue

        addressed_to_me = rec.get("to") == my_id
        reply_to_mine   = rec.get("reply_to") in my_sent_ids

        if addressed_to_me or reply_to_mine:
            rec["status"] = "delivered"
            rec["recv_time"] = _now_str()
            store[mid] = rec
            _save_json(os.path.join(DIR_RT, f"{mid}.json"), rec)

            tag = "→ to me" if addressed_to_me else f"↩ reply to {rec['reply_to']}"
            print(f"  📬 [{tag}]  ID: {mid}  from: {rec.get('from','?')}")
            print(f"      \"{rec.get('preview', '')}\"")
            received.append(rec)

            # Mark the original as "replied" if this is a reply
            if reply_to_mine:
                orig_id = rec.get("reply_to")
                if orig_id and orig_id in store:
                    store[orig_id]["status"] = "replied"

    # ── Pass 2: coordinate-space probe (best-effort, shows window health) ──
    if low_s and high_s:
        low, high = int(low_s), int(high_s)
        span = high - low
        num_probes = min(20, max(5, span // max(1, 10_000)))
        step = max(1, span // num_probes)

        probe_hits = 0
        known_coords = {rec.get("coordinate"): mid for mid, rec in store.items()
                        if not mid.startswith("_")}

        for pi in range(num_probes):
            probe_V = low + pi * step
            if str(probe_V) in known_coords:
                probe_hits += 1

        print(f"\n  [Window probe] range span: {span:,}  probes: {num_probes}  coord hits: {probe_hits}")
    else:
        print("  ⚠  No realtime polling range set — run 'rtrange' first.")

    _save_rt_store(store)

    if not received:
        print("  No new messages.")
    else:
        print(f"\n  ✅ {len(received)} new message(s) received.")
    return received


# ============================================================
# 4. simulate_reply
# ============================================================

def simulate_reply(
    original_msg_id: str,
    reply_text:      str = None,
    from_node:       str = "future_self",
    coord_file:      str = COORD_FILE,
) -> str:
    """
    Inject a simulated reply to `original_msg_id` into the store.
    Useful for testing the full send → poll → reply cycle in one session.

    Returns the new reply msg_id.
    """
    store = _load_rt_store()
    if original_msg_id not in store:
        print(f"  ⚠ simulate_reply: msg_id '{original_msg_id}' not found in store.")
        return ""

    original = store[original_msg_id]
    if reply_text is None:
        reply_text = f"[AUTO-REPLY] Received: \"{original.get('preview', ''[:40])}\""

    reply_id = str(uuid.uuid4()).replace("-", "")[:12]
    record = {
        "msg_id":     reply_id,
        "from":       from_node,
        "to":         original.get("from", "unknown"),
        "reply_to":   original_msg_id,
        "message":    reply_text,
        "preview":    reply_text[:60],
        "coordinate": str(random.randint(10**10, 10**15)),  # simulated coord
        "timestamp":  _now_str(),
        "status":     "sent",
        "simulated":  True,
    }
    store[reply_id] = record
    _save_rt_store(store)
    _ensure_rt_dir()
    _save_json(os.path.join(DIR_RT, f"{reply_id}.json"), record)

    print(f"🤖 Simulated reply injected  | ID: {reply_id}  to: {record['to']}")
    print(f"   \"{reply_text[:60]}\"")
    return reply_id


# ============================================================
# 5. realtime_comms  — interactive CLI
# ============================================================

_HELP_TEXT = """
  REALTIME COMMANDS
  ─────────────────────────────────────────────────────
  send <to> <message>        Send a message to a node id
  reply <msg_id> <message>   Reply to a specific message
  poll                       Scan for new messages / replies
  simulate <msg_id>          Inject an auto-reply (for testing)
  simulate <msg_id> <text>   Inject a specific reply text
  history [n]                Show last n messages (default 10)
  status <msg_id>            Show full record for a message
  rtrange                    Recalculate tight polling range
  myid                       Show / change your node id
  help                       This help text
  exit / quit                Leave realtime node
  ─────────────────────────────────────────────────────
"""


def realtime_comms(coord_file: str = COORD_FILE):
    """
    Interactive realtime communications CLI.

    Demonstrates:
      1. Sending a message
      2. Simulating a reply (for testing without a second node)
      3. Polling and receiving that reply
    """
    _ensure_rt_dir()

    if not os.path.exists(coord_file):
        print(f"\n⚠  Coordinate file '{coord_file}' not found.")
        print("   Run coordinate_generator() first, or point to an existing coord file.")
        return

    store = _load_rt_store()
    my_id = store.get("_my_id") or str(uuid.uuid4())[:8]
    store["_my_id"] = my_id
    _save_rt_store(store)

    coord = _load_json(coord_file)
    rt_low  = coord.get("rt_polling_low",  "not set")
    rt_high = coord.get("rt_polling_high", "not set")

    print("\n" + "═" * 58)
    print("  GROKCOMMS — REALTIME NODE")
    print("═" * 58)
    print(f"  Node ID    : {my_id}")
    print(f"  Coord file : {coord_file}")
    print(f"  RT window  : {str(rt_low)[:22]}  →  {str(rt_high)[:22]}")
    print("\n  Type 'help' for commands.\n")

    while True:
        try:
            raw = input("realtime> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n[Realtime] Session ended.")
            break

        if not raw:
            continue

        # Split into at most 3 parts: cmd arg1 rest-as-one-string
        parts = raw.split(maxsplit=2)
        cmd   = parts[0].lower()

        # ── exit ────────────────────────────────────────────────────────────
        if cmd in ("exit", "quit"):
            print("[Realtime] Goodbye.")
            break

        # ── help ────────────────────────────────────────────────────────────
        elif cmd == "help":
            print(_HELP_TEXT)

        # ── myid ────────────────────────────────────────────────────────────
        elif cmd == "myid":
            store = _load_rt_store()
            print(f"  Current node ID: {store.get('_my_id', my_id)}")
            new_id = input("  New node ID (Enter to keep): ").strip()
            if new_id:
                my_id = new_id
                store["_my_id"] = my_id
                _save_rt_store(store)
                print(f"  Node ID updated → {my_id}")

        # ── send <to> <message> ─────────────────────────────────────────────
        elif cmd == "send":
            if len(parts) < 3:
                print("  Usage: send <to> <message>")
                continue
            to_node = parts[1]
            message = parts[2]
            try:
                msg_id, coord_val = send_realtime(
                    to=to_node, message=message,
                    coord_file=coord_file, my_id=my_id
                )
            except Exception as e:
                print(f"  ⚠ send_realtime error: {e}")

        # ── reply <msg_id> <message> ─────────────────────────────────────────
        elif cmd == "reply":
            if len(parts) < 3:
                print("  Usage: reply <msg_id> <message>")
                continue
            orig_id = parts[1]
            message = parts[2]
            # Identify the original sender as the 'to' for this reply
            store = _load_rt_store()
            orig = store.get(orig_id)
            to_node = orig.get("from", "unknown") if orig else "unknown"
            try:
                msg_id, _ = send_realtime(
                    to=to_node, message=message, reply_to=orig_id,
                    coord_file=coord_file, my_id=my_id
                )
            except Exception as e:
                print(f"  ⚠ reply error: {e}")

        # ── poll ─────────────────────────────────────────────────────────────
        elif cmd == "poll":
            poll_realtime(coord_file=coord_file, my_id=my_id)

        # ── simulate <msg_id> [text] ─────────────────────────────────────────
        elif cmd == "simulate":
            if len(parts) < 2:
                print("  Usage: simulate <msg_id> [reply text]")
                continue
            orig_id   = parts[1]
            reply_txt = parts[2] if len(parts) >= 3 else None
            simulate_reply(orig_id, reply_txt, coord_file=coord_file)
            print("  → Run 'poll' to receive the simulated reply.")

        # ── history [n] ──────────────────────────────────────────────────────
        elif cmd == "history":
            n = 10
            if len(parts) >= 2:
                try:
                    n = int(parts[1])
                except ValueError:
                    pass
            store = _load_rt_store()
            msgs = [
                (mid, rec) for mid, rec in store.items()
                if not mid.startswith("_")
            ]
            # Sort by timestamp descending
            msgs.sort(key=lambda x: x[1].get("timestamp", ""), reverse=True)
            print(f"\n  HISTORY (last {min(n, len(msgs))} of {len(msgs)} messages)")
            print(f"  {'ID':<14} {'FROM':<12} {'TO':<12} {'STATUS':<12} PREVIEW")
            print("  " + "─" * 66)
            for mid, rec in msgs[:n]:
                sim_tag = " 🤖" if rec.get("simulated") else ""
                reply_tag = f" ↩{rec['reply_to'][:8]}" if rec.get("reply_to") else ""
                print(
                    f"  {mid:<14} {rec.get('from','?'):<12} {rec.get('to','?'):<12} "
                    f"{rec.get('status','?'):<12} "
                    f"\"{rec.get('preview','')[:28]}\"{sim_tag}{reply_tag}"
                )
            print()

        # ── status <msg_id> ──────────────────────────────────────────────────
        elif cmd == "status":
            if len(parts) < 2:
                print("  Usage: status <msg_id>")
                continue
            store = _load_rt_store()
            rec = store.get(parts[1])
            if rec is None:
                print(f"  ⚠ msg_id '{parts[1]}' not found.")
            else:
                print("\n" + "─" * 50)
                print(json.dumps(rec, indent=2))
                print("─" * 50)

        # ── rtrange ──────────────────────────────────────────────────────────
        elif cmd == "rtrange":
            try:
                msg_len = int(input(f"  Message length bytes (Enter for {RT_MSG_LEN_DEFAULT}): ").strip()
                              or RT_MSG_LEN_DEFAULT)
            except ValueError:
                msg_len = RT_MSG_LEN_DEFAULT
            try:
                n_samp = int(input(f"  Num samples (Enter for {RT_SAMPLES_DEFAULT}): ").strip()
                             or RT_SAMPLES_DEFAULT)
            except ValueError:
                n_samp = RT_SAMPLES_DEFAULT
            try:
                calculate_tight_polling_range(coord_file, msg_len, n_samp)
                coord = _load_json(coord_file)
                rt_low  = coord.get("rt_polling_low",  "not set")
                rt_high = coord.get("rt_polling_high", "not set")
                print(f"\n  New RT window: {str(rt_low)[:22]}  →  {str(rt_high)[:22]}")
            except Exception as e:
                print(f"  ⚠ rtrange error: {e}")

        else:
            print(f"  [?] Unknown command: '{cmd}'.  Type 'help'.")


# ============================================================
# Quick demo — run directly to test the full cycle
# ============================================================

def demo(coord_file: str = COORD_FILE):
    """
    Automated demonstration of the realtime send → simulate → poll cycle.
    Requires a valid coord_file.
    """
    if not os.path.exists(coord_file):
        print(f"⚠  '{coord_file}' not found — cannot run demo.")
        return

    print("\n" + "★" * 58)
    print("  REALTIME DATA POLLING — DEMO")
    print("★" * 58)

    # Step 1: Calculate tight range
    print("\n[Demo] Step 1 — Calculate tight polling range")
    try:
        calculate_tight_polling_range(coord_file, message_length=16, num_samples=8)
    except Exception as e:
        print(f"  (range calc skipped: {e})")

    # Step 2: Send a message
    print("\n[Demo] Step 2 — Send a message")
    store = _load_rt_store()
    my_id = store.get("_my_id") or str(uuid.uuid4())[:8]
    store["_my_id"] = my_id
    _save_rt_store(store)

    try:
        msg_id, coord_val = send_realtime(
            to="bob", message="Hello Bob from demo!",
            coord_file=coord_file, my_id=my_id
        )
    except Exception as e:
        print(f"  send failed: {e}")
        return

    # Step 3: Simulate a reply
    print("\n[Demo] Step 3 — Simulate reply from Bob")
    reply_id = simulate_reply(
        original_msg_id=msg_id,
        reply_text="Hey! Got your message. All systems go.",
        from_node="bob",
        coord_file=coord_file,
    )

    # Step 4: Poll and receive
    print("\n[Demo] Step 4 — Poll for replies")
    received = poll_realtime(coord_file=coord_file, my_id=my_id)

    # Step 5: Summary
    print("\n[Demo] Summary")
    print(f"  Sent    msg_id : {msg_id}")
    print(f"  Reply   msg_id : {reply_id}")
    print(f"  Received count : {len(received)}")
    for rec in received:
        print(f"    ↩ \"{rec.get('preview', '')}\"  (status: {rec.get('status')})")
    print("\n[Demo] Done. Run realtime_comms() for the interactive CLI.")


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        demo()
    else:
        realtime_comms()
