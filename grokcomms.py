"""                                             GrokComms — Temporal Communications Node
=========================================
Modules:
  coordinate_generator   — generate primary V/R coordinate, save to coordinatefile.json
  polling_range_finder   — sample random messages, compute RMS high/low, append to coord file
  polling                — decode chart space in range, extract temporal headers, filter by To Date
  range_padder           — pad a composed message so its coordinate lands inside polling range
  temporal_node          — interactive CLI: compose / inbox / received / sent / outbox / polling
  admin_menu             — top-level menu wiring all modules

Temporal Header format (first N bytes of every decoded message):
  FROM_DATE   YYYY-MM-DD
  TO_DATE     YYYY-MM-DD
  FROM_TIME   HH:MM:SS
  RECV_TIME   ----------  (placeholder; stamped on receipt)
  TUPLE_HASH  <last byte of message as 3-digit decimal>
  ---
  <application payload below the dashes>

Temporal protocol:
  - TO_DATE today or in the past  → message is RECEIVED  (returned)
  - TO_DATE in the future         → message is IGNORED
  - FROM_DATE is NEVER filtered — messages from the future are allowed
    as long as TO_DATE is today or past.
"""

import json
import math
import os
import random
import shutil
import statistics
import tempfile
from datetime import date, datetime, timezone

from chart_generator import ChartGenerator

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

COORD_FILE     = "coordinatefile.json"
TNODE_COORD    = "temporal_node_coordinates.json"

DIR_POLLING   = "polling"
DIR_RECEIVED  = "received"
DIR_SENT      = "sent"
DIR_INBOX     = "inbox"
DIR_OUTBOX    = "outbox"
ALL_DIRS      = [DIR_POLLING, DIR_RECEIVED, DIR_SENT, DIR_INBOX, DIR_OUTBOX]

HEADER_SEP    = "---"          # separates temporal header from payload
HEADER_LINES  = 5              # FROM_DATE / TO_DATE / FROM_TIME / RECV_TIME / TUPLE_HASH


# ============================================================
# Helpers
# ============================================================

def _load_json(path: str) -> dict:
    with open(path, "r") as f:
        return json.load(f)

def _save_json(path: str, data: dict):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def _today_str() -> str:
    return date.today().isoformat()

def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _ensure_dirs():
    for d in ALL_DIRS:
        os.makedirs(d, exist_ok=True)

def _cg_from_coord(coord: dict) -> ChartGenerator:
    """Build a fresh ChartGenerator matching parameters in coord dict."""
    cg = ChartGenerator(
        chart_base    = coord.get("chart_base",    256),
        mask_base     = coord.get("mask_base",     1_000_000_000_000),
        num_digits    = coord.get("num_digits",    100),
        num_n_streams = coord.get("num_n_streams", 12),
    )
    cg.Vs[0] = cg.hm.deserialize(coord["V"])
    cg.Rs[0] = cg.hm.deserialize(coord["R"])
    return cg


# ============================================================
# 1. coordinate_generator
# ============================================================

def coordinate_generator(
    passphrase: str,
    num_digits: int = 80,
    output_file: str = COORD_FILE,
):
    """
    Generate a primary coordinate from a passphrase + random seed.
    Writes V, R, chart parameters to output_file.
    """
    print(f"\n[CoordGen] Generating coordinate  (passphrase='{passphrase}', digits={num_digits})")

    # Use passphrase to seed a deterministic starting point,
    # then encode random bytes to reach a unique region of the chart.
    seed_bytes = passphrase.encode("utf-8")
    random.seed(seed_bytes)
    rand_payload = bytes(random.randint(0, 255) for _ in range(num_digits))

    cg = ChartGenerator(chart_base=256, num_digits=num_digits)
    # Encode the seed payload end-to-front (same as encode_file)
    for i in range(len(rand_payload) - 1, -1, -1):
        cg._encode_step(rand_payload[i], 0)

    V_int = cg.hm.to_int(cg.Vs[0])
    R_int = cg.hm.to_int(cg.Rs[0])

    coord = {
        "passphrase_hint": passphrase[:4] + "****",
        "chart_base":      256,
        "mask_base":       1_000_000_000_000,
        "num_digits":      num_digits,
        "num_n_streams":   12,
        "V":               cg.hm.serialize(cg.Vs[0]),
        "R":               cg.hm.serialize(cg.Rs[0]),
        "V_int_preview":   str(V_int)[:30] + "...",
        "created":         _now_str(),
        # polling range — filled in by polling_range_finder
        "polling_high":    None,
        "polling_low":     None,
        "message_length":  None,
    }
    _save_json(output_file, coord)
    print(f"[CoordGen] Primary coordinate written → {output_file}")
    print(f"[CoordGen] V (first 30 digits): {str(V_int)[:30]}...")
    return coord


# ============================================================
# 2. polling_range_finder
# ============================================================

def polling_range_finder(
    num_samples:    int = 30,
    coord_file:     str = COORD_FILE,
    sample_file:    str = None,       # NEW: use a real file instead of os.urandom
):
    """
    Sample `num_samples` messages, encode each starting from V/R in coord_file.
    If sample_file is given, reads chunks from it; otherwise uses os.urandom.
    Computes mean ± k*std_dev polling window (Grok's asymmetric formula).
    """
    coord   = _load_json(coord_file)
    msg_len = coord.get("message_length") or 64

    print(f"\n[RangeFinder] samples={num_samples}  msg_len={msg_len}")
    if sample_file:
        print(f"[RangeFinder] Source file: {sample_file}")

    # Load file bytes once if provided
    file_bytes = None
    if sample_file and os.path.exists(sample_file):
        with open(sample_file, "rb") as f:
            file_bytes = f.read()
        if len(file_bytes) < msg_len:
            file_bytes = None
            print(f"  ⚠ File too short ({len(file_bytes)} bytes); falling back to random.")

    samples = []
    for i in range(num_samples):
        if file_bytes is not None:
            # Slide a window through the file, wrapping around
            offset = (i * msg_len) % (len(file_bytes) - msg_len)
            data = file_bytes[offset : offset + msg_len]
        else:
            data = os.urandom(msg_len)

        cg = _cg_from_coord(coord)
        for j in range(len(data) - 1, -1, -1):
            cg._encode_step(data[j], 0)
        samples.append(cg.hm.to_int(cg.Vs[0]))

        if (i + 1) % 5 == 0:
            print(f"  [{i+1}/{num_samples}] coord: {str(samples[-1])[:20]}...")

    mean    = statistics.mean(samples)
    std_dev = statistics.stdev(samples) if len(samples) > 1 else 0

    # Grok's asymmetric window: low = mean+0.4σ, high = mean+3.5σ
    polling_low  = int(mean + 0.4 * std_dev)
    polling_high = int(mean + 3.5 * std_dev)

    print(f"\n[RangeFinder] Mean        : {str(int(mean))[:30]}")
    print(f"[RangeFinder] Std Dev     : {std_dev:,.0f}")
    print(f"[RangeFinder] Polling LOW : {str(polling_low)[:30]}")
    print(f"[RangeFinder] Polling HIGH: {str(polling_high)[:30]}")

    coord["polling_low"]    = str(polling_low)
    coord["polling_high"]   = str(polling_high)
    coord["message_length"] = msg_len
    coord["range_std_dev"]  = str(int(std_dev))
    coord["range_samples"]  = num_samples
    _save_json(coord_file, coord)
    print(f"[RangeFinder] Polling range saved → {coord_file}")
    return polling_low, polling_high

# ============================================================
# 3. range_padder
# ============================================================

def range_padder(msg_json_path: str, coord_file: str = COORD_FILE) -> str:
    """
    Load a composed message from msg_json_path.
    Pad its payload (add null bytes) until its final encoded coordinate
    lands within the polling range defined in coord_file.
    Returns the padded message text.
    """
    coord  = _load_json(coord_file)
    msg    = _load_json(msg_json_path)
    payload = msg["payload"].encode("utf-8")

    low  = int(coord["polling_low"])
    high = int(coord["polling_high"])
    msg_len = coord.get("message_length", 64)

    print(f"\n[RangePadder] Target range: {str(low)[:20]}...  →  {str(high)[:20]}...")

    # Pad with null bytes until encoded coordinate is inside range
    max_pad = msg_len * 4
    for pad in range(0, max_pad):
        test_data = payload + b'\x00' * pad
        cg = _cg_from_coord(coord)
        for i in range(len(test_data) - 1, -1, -1):
            cg._encode_step(test_data[i], 0)
        final_V = cg.hm.to_int(cg.Vs[0])
        if low <= final_V <= high:
            padded_text = payload.decode("utf-8") + '\x00' * pad
            msg["payload"]      = padded_text
            msg["pad_bytes"]    = pad
            msg["final_coord"]  = str(final_V)[:30]
            _save_json(msg_json_path, msg)
            print(f"[RangePadder] Padded with {pad} bytes → coordinate IN RANGE ✅")
            return padded_text

    print(f"[RangePadder] ⚠ Could not land in range within {max_pad} padding bytes.")
    return payload.decode("utf-8")


# ============================================================
# Temporal Header helpers
# ============================================================

def _build_header(to_date: str, subject: str = "") -> str:
    """
    Build the temporal header block.
    RECV_TIME is a placeholder (----------).
    TUPLE_HASH is computed after the full message is assembled.
    """
    lines = [
        f"FROM_DATE   {_today_str()}",
        f"TO_DATE     {to_date}",
        f"FROM_TIME   {datetime.now().strftime('%H:%M:%S')}",
        f"RECV_TIME   ----------",
        f"TUPLE_HASH  ???",          # filled in by _finalize_message
        HEADER_SEP,
    ]
    if subject:
        lines.append(f"SUBJECT     {subject}")
    return "\n".join(lines) + "\n"

def _finalize_message(full_text: str) -> str:
    """Replace TUPLE_HASH ??? with the last byte value of the encoded text."""
    encoded = full_text.encode("utf-8")
    last_byte = encoded[-1]
    return full_text.replace("TUPLE_HASH  ???", f"TUPLE_HASH  {last_byte:03d}")

def _parse_header(text: str) -> dict | None:
    """
    Parse temporal header from a decoded message text.
    Returns dict with keys: from_date, to_date, from_time, recv_time,
    tuple_hash, payload.  Returns None if header not found.
    """
    lines = text.splitlines()
    h = {}
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

def _verify_hash(text: str, claimed_hash: str) -> bool:
    """Last byte of UTF-8 encoded text should equal claimed_hash decimal."""
    try:
        encoded = text.encode("utf-8")
        return encoded[-1] == int(claimed_hash)
    except Exception:
        return False

def _temporal_filter(header: dict) -> bool:
    """
    Return True if message should be RECEIVED.
    Filter: TO_DATE must be today or in the past.
    FROM_DATE is NEVER filtered.
    """
    try:
        to_dt = date.fromisoformat(header.get("to_date", ""))
        return to_dt <= date.today()
    except ValueError:
        return False


# ============================================================
# 4. polling
# ============================================================

def polling(coord_file: str = COORD_FILE) -> list[dict]:
    """
    Load coord_file → get V, R, polling range, message_length.
    Walk the coordinate space between polling_low and polling_high,
    decode each candidate, search for temporal header.
    Verify TUPLE_HASH. Filter by TO_DATE protocol.
    Save received messages to received/ directory.
    Returns list of received message dicts.
    """
    _ensure_dirs()
    coord      = _load_json(coord_file)
    low        = int(coord["polling_low"])
    high       = int(coord["polling_high"])
    msg_len    = coord.get("message_length", 64)

    print(f"\n[Polling] Scanning {str(low)[:20]}...  →  {str(high)[:20]}...")
    print(f"[Polling] Message length: {msg_len} bytes")

    received = []

    # We probe a grid of coordinates between low and high.
    # Step through evenly-spaced probe points.
    span      = high - low
    num_probes = min(200, max(10, span // max(1, msg_len * 10)))
    step      = span // num_probes if num_probes > 0 else 1

    print(f"[Polling] Probing {num_probes} coordinates...")

    for probe_idx in range(num_probes):
        probe_V = low + probe_idx * step

        # Build a CG with V set to this probe coordinate
        cg = _cg_from_coord(coord)
        cg.Vs[0] = cg.hm.from_int(probe_V)

        # Decode msg_len bytes
        decoded_bytes = []
        try:
            for _ in range(msg_len):
                decoded_bytes.append(cg._decode_step(0))
        except Exception:
            continue

        # Try to interpret as UTF-8 text
        try:
            text = bytes(decoded_bytes).decode("utf-8", errors="replace")
        except Exception:
            continue

        header = _parse_header(text)
        if header is None:
            continue

        # Verify hash
        hash_ok = _verify_hash(text, header.get("tuple_hash", "-1"))

        # Temporal filter
        if not _temporal_filter(header):
            print(f"  [Polling] Future-dated message ignored (TO_DATE={header.get('to_date')})")
            continue

        # Stamp recv_time
        header["recv_time"] = _now_str()

        msg_record = {
            "probe_V":    str(probe_V)[:30],
            "from_date":  header.get("from_date"),
            "to_date":    header.get("to_date"),
            "from_time":  header.get("from_time"),
            "recv_time":  header["recv_time"],
            "tuple_hash": header.get("tuple_hash"),
            "hash_ok":    hash_ok,
            "subject":    header.get("subject", "(no subject)"),
            "payload":    header.get("payload", ""),
            "raw_text":   text,
        }
        received.append(msg_record)
        fname = os.path.join(DIR_RECEIVED,
                             f"msg_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json")
        _save_json(fname, msg_record)
        print(f"  [Polling] ✅ Message received — TO_DATE={header.get('to_date')}  hash={'✅' if hash_ok else '❌'}")

    print(f"[Polling] Done. {len(received)} message(s) received.")
    return received


# ============================================================
# 5. Compose helpers
# ============================================================

def compose_message(
    to_date:  str,
    subject:  str,
    body:     str,
    coord_file: str = COORD_FILE,
) -> str:
    """
    Build a temporal message, encode it via the chart, save to outbox/.
    Returns path to the outbox JSON file.
    """
    _ensure_dirs()
    coord = _load_json(coord_file)

    header  = _build_header(to_date, subject)
    full    = header + body
    full    = _finalize_message(full)

    msg_data = {
        "to_date":   to_date,
        "subject":   subject,
        "payload":   full,
        "composed":  _now_str(),
        "status":    "outbox",
    }
    fname = os.path.join(DIR_OUTBOX,
                         f"draft_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json")
    _save_json(fname, msg_data)
    print(f"[Compose] Draft saved → {fname}")
    return fname


def send_outbox(coord_file: str = COORD_FILE):
    """
    Encode all outbox messages and move to sent/.
    Applies range_padder to land inside polling range.
    """
    _ensure_dirs()
    outbox_files = sorted(
        f for f in os.listdir(DIR_OUTBOX) if f.endswith(".json")
    )
    if not outbox_files:
        print("[Outbox] Empty — nothing to send.")
        return

    for fname in outbox_files:
        src_path = os.path.join(DIR_OUTBOX, fname)
        print(f"\n[Outbox] Encoding {fname}...")
        try:
            range_padder(src_path, coord_file)
        except Exception as e:
            print(f"  ⚠ range_padder failed: {e}  (sending as-is)")

        msg  = _load_json(src_path)
        payload_bytes = msg["payload"].encode("utf-8")

        coord = _load_json(coord_file)
        cg    = _cg_from_coord(coord)
        for i in range(len(payload_bytes) - 1, -1, -1):
            cg._encode_step(payload_bytes[i], 0)

        final_V = cg.hm.to_int(cg.Vs[0])
        msg["status"]    = "sent"
        msg["sent_time"] = _now_str()
        msg["coord"]     = str(final_V)[:30]

        sent_path = os.path.join(DIR_SENT, fname.replace("draft_", "sent_"))
        _save_json(sent_path, msg)
        os.remove(src_path)
        print(f"[Outbox] Sent → {sent_path}  coord={msg['coord']}")


# ============================================================
# 6. temporal_node (interactive CLI)
# ============================================================

def temporal_node(coord_file: str = TNODE_COORD):
    """
    Interactive temporal communications node.
    Exchanges coordinate files; all holders can see each others' messages.
    (No privacy yet — use personal encryption for that.)
    """
    _ensure_dirs()

    # Bootstrap coord file if missing
    if not os.path.exists(coord_file):
        print(f"\n[TemporalNode] No coordinate file found at '{coord_file}'.")
        phrase = input("  Enter a passphrase to generate one: ").strip()
        coordinate_generator(phrase, num_digits=80, output_file=coord_file)
        print("\n  Run 'pollrange' next to set the polling range before composing.")

    coord = _load_json(coord_file)

    print("\n" + "═" * 60)
    print("  TEMPORAL COMMUNICATIONS NODE")
    print("═" * 60)
    print(f"  Coord file : {coord_file}")
    print(f"  Poll range : {str(coord.get('polling_low','?'))[:20]}... → {str(coord.get('polling_high','?'))[:20]}...")
    print(f"  Msg length : {coord.get('message_length', '?')} bytes")
    print("\n  Commands: polling | received | sent | inbox | outbox")
    print("            compose | send | pollrange | coordgen | help | exit\n")

    while True:
        try:
            raw = input("temporal> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n[TemporalNode] Session ended.")
            break

        if not raw:
            continue
        parts = raw.split(maxsplit=3)
        cmd   = parts[0].lower()

        if cmd in ("exit", "quit"):
            print("[TemporalNode] Goodbye.")
            break

        elif cmd == "help":
            print("""
  polling           — scan coordinate space, collect received messages
  received          — list messages in received/
  inbox             — list messages in inbox/ (already read)
  sent              — list messages in sent/
  outbox            — list pending drafts in outbox/
  compose           — write a new message
  send              — encode & send all outbox drafts
  pollrange         — recalculate polling range
  coordgen          — generate a new coordinate
  exit / quit       — leave
""")

        elif cmd == "polling":
            polling(coord_file)

        elif cmd in ("received", "inbox", "sent", "outbox"):
            folder = cmd  # folder names match commands
            files  = sorted(f for f in os.listdir(folder) if f.endswith(".json"))
            if not files:
                print(f"  [{folder.upper()}] Empty.")
                continue
            print(f"\n  {folder.upper()} ({len(files)} message(s)):")
            for i, fn in enumerate(files, 1):
                try:
                    m = _load_json(os.path.join(folder, fn))
                    print(f"  [{i}] {fn}")
                    print(f"       Subject: {m.get('subject', m.get('to_date','?'))}")
                    print(f"       Date   : {m.get('composed', m.get('recv_time','?'))}")
                    print(f"       Status : {m.get('status','?')}")
                except Exception:
                    print(f"  [{i}] {fn}  (unreadable)")
            # Let user pick one to read
            pick = input("\n  Enter number to read (or Enter to skip): ").strip()
            if pick.isdigit():
                idx = int(pick) - 1
                if 0 <= idx < len(files):
                    fn  = files[idx]
                    msg = _load_json(os.path.join(folder, fn))
                    print("\n" + "─" * 50)
                    print(msg.get("raw_text") or msg.get("payload") or json.dumps(msg, indent=2))
                    print("─" * 50)
                    # Move received → inbox after reading
                    if folder == "received":
                        inbox_path = os.path.join(DIR_INBOX, fn)
                        shutil.move(os.path.join(folder, fn), inbox_path)
                        print(f"  → Moved to inbox.")

        elif cmd == "compose":
            print("\n  [Compose]")
            to_date = input("  To Date (YYYY-MM-DD, or Enter for today): ").strip()
            if not to_date:
                to_date = _today_str()
            subject = input("  Subject: ").strip()
            print("  Body (end with a line containing only '.'): ")
            body_lines = []
            while True:
                line = input()
                if line == ".":
                    break
                body_lines.append(line)
            body = "\n".join(body_lines)
            compose_message(to_date, subject, body, coord_file)

        elif cmd == "send":
            send_outbox(coord_file)

        elif cmd == "pollrange":
    coord = _load_json(coord_file)
    msg_len = coord.get("message_length") or 64
    try:
        msg_len = int(input(f"  Message length (Enter for {msg_len}): ").strip() or msg_len)
    except ValueError:
        pass
    coord["message_length"] = msg_len
    _save_json(coord_file, coord)
    try:
        samples = int(input("  Number of samples (Enter for 30): ").strip() or 30)
    except ValueError:
        samples = 30
    src_file = input("  Sample file path (Enter to use random bytes): ").strip() or None
    polling_range_finder(samples, coord_file, src_file)

        elif cmd == "coordgen":
            phrase = input("  Passphrase: ").strip()
            try:
                digits = int(input("  Num digits (Enter for 80): ").strip() or 80)
            except ValueError:
                digits = 80
            coordinate_generator(phrase, digits, coord_file)

        else:
            print(f"  [?] Unknown command: '{cmd}'.  Type 'help'.")


# ============================================================
# 7. admin_menu
# ============================================================

def admin_menu():
    """
    Top-level admin CLI wiring all GrokComms modules.
    """
    print("\n" + "★" * 60)
    print("  GROKCOMMS — ADMIN MENU")
    print("★" * 60)
    print("  1. coordinate_generator")
    print("  2. polling_range_finder")
    print("  3. polling")
    print("  4. temporal_node")
    print("  5. range_padder  (on a specific message file)")
    print("  0. exit\n")

    while True:
        try:
            choice = input("admin> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n[Admin] Goodbye.")
            break

        if choice == "0":
            print("[Admin] Goodbye.")
            break

        elif choice == "1":
            phrase = input("  Passphrase: ").strip()
            try:
                digits = int(input("  Num digits (Enter for 80): ").strip() or 80)
            except ValueError:
                digits = 80
            out_file = input(f"  Output file (Enter for '{COORD_FILE}'): ").strip() or COORD_FILE
            coordinate_generator(phrase, digits, out_file)

        elif choice == "2":
    coord_f = input(f"  Coord file (Enter for '{COORD_FILE}'): ").strip() or COORD_FILE
    try:
        samples = int(input("  Num samples (Enter for 30): ").strip() or 30)
    except ValueError:
        samples = 30
    src_file = input("  Sample file path (Enter to use random bytes): ").strip() or None
    polling_range_finder(samples, coord_f, src_file)

        elif choice == "3":
            coord_f = input(f"  Coord file (Enter for '{COORD_FILE}'): ").strip() or COORD_FILE
            if not os.path.exists(coord_f):
                print(f"  ⚠ '{coord_f}' not found. Run coordinate_generator first.")
                continue
            coord = _load_json(coord_f)
            if not coord.get("polling_low"):
                print("  ⚠ No polling range set. Run polling_range_finder first.")
                continue
            polling(coord_f)

        elif choice == "4":
            coord_f = input(f"  Coord file (Enter for '{TNODE_COORD}'): ").strip() or TNODE_COORD
            temporal_node(coord_f)

        elif choice == "5":
            msg_f = input("  Message JSON file path: ").strip()
            coord_f = input(f"  Coord file (Enter for '{COORD_FILE}'): ").strip() or COORD_FILE
            if not os.path.exists(msg_f):
                print(f"  ⚠ '{msg_f}' not found.")
                continue
            range_padder(msg_f, coord_f)

        else:
            print("  Enter 0–5.")


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    admin_menu()
