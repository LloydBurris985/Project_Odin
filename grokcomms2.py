"""
GrokComms — Unified Temporal + Realtime Communications Node
=============================================================
All functionality in one module, one coordinatefile.json, one ChartGenerator.
"""

import json
import math
import os
import random
import shutil
import uuid
from datetime import date, datetime, timezone

from chart_generator import ChartGenerator

COORD_FILE    = "coordinatefile.json"
TNODE_COORD   = "temporal_node_coordinates.json"
RT_STORE_FILE = "realtime_messages.json"
ADDR_BOOK_FILE = "address_book.json"
BEACON_FILE   = "beacons.json"

DIR_POLLING  = "polling"
DIR_RECEIVED = "received"
DIR_SENT     = "sent"
DIR_INBOX    = "inbox"
DIR_OUTBOX   = "outbox"
DIR_RT       = "realtime"
ALL_DIRS     = [DIR_POLLING, DIR_RECEIVED, DIR_SENT, DIR_INBOX, DIR_OUTBOX, DIR_RT]

HEADER_SEP   = "---"
HEADER_LINES = 5

RT_MSG_LEN_DEFAULT   = 16
RT_SAMPLES_DEFAULT   = 8
RT_STD_MULTIPLIER_LO = -0.5
RT_STD_MULTIPLIER_HI =  0.5


# ---------------------------------------------------------------------------
# Integer-only mean / std_dev  (avoids OverflowError on large coordinates)
# ---------------------------------------------------------------------------

def _int_mean(samples: list) -> int:
    return sum(samples) // len(samples)

def _int_std_dev(samples: list, mean: int = None) -> int:
    if len(samples) < 2:
        return 0
    if mean is None:
        mean = _int_mean(samples)
    variance = sum((s - mean) ** 2 for s in samples) // len(samples)
    return math.isqrt(variance)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _load_json(path: str) -> dict:
    with open(path, "r") as f:
        return json.load(f)

def _save_json(path: str, data):
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
    cg = ChartGenerator(
        chart_base    = coord.get("chart_base",    256),
        mask_base     = coord.get("mask_base",     1_000_000_000_000),
        num_digits    = coord.get("num_digits",    150),
        num_n_streams = coord.get("num_n_streams", 12),
    )
    cg.Vs[0] = cg.hm.deserialize(coord["V"])
    cg.Rs[0] = cg.hm.deserialize(coord["R"])
    return cg


# ===========================================================================
# ADDRESS BOOK
# ===========================================================================

class OdinAddressBook:
    def __init__(self, filename: str = ADDR_BOOK_FILE):
        self.filename = filename
        self.contacts = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.filename):
            with open(self.filename) as f:
                return json.load(f)
        return {}

    def _save(self):
        with open(self.filename, "w") as f:
            json.dump(self.contacts, f, indent=2)

    def add_contact(self, name, coordinate, public_key_hint=None, notes=""):
        self.contacts[name.lower()] = {
            "coordinate": str(coordinate), "alias": name,
            "public_key_hint": public_key_hint, "notes": notes, "added": _now_str(),
        }
        self._save()
        print(f"  Contact added: {name}  ->  {str(coordinate)[:24]}...")

    def get_contact(self, name: str):
        entry = self.contacts.get(name.lower())
        if entry:
            return int(entry["coordinate"])
        print(f"  Contact '{name}' not found in address book.")
        return None

    def list_contacts(self):
        if not self.contacts:
            print("  Address book is empty.")
            return
        border = "-" * 62
        print(f"\n  {border}")
        print(f"  ODINNET ADDRESS BOOK  --  {len(self.contacts)} contact(s)")
        print(f"  {border}")
        for key, data in sorted(self.contacts.items()):
            coord_preview = str(data["coordinate"])[:22] + "..."
            notes_preview = (data.get("notes") or "")[:28]
            print(f"  {data['alias']:<22}  {coord_preview:>25}  {notes_preview}")
        print(f"  {border}\n")

    def remove_contact(self, name: str):
        key = name.lower()
        if key in self.contacts:
            del self.contacts[key]
            self._save()
            print(f"  Removed contact: {name}")
        else:
            print(f"  Contact '{name}' not found.")

    def interactive_menu(self):
        print("\n" + "=" * 52)
        print("  ODINNET ADDRESS BOOK")
        print("=" * 52)
        print("  Commands: list | add | remove | lookup | exit\n")
        while True:
            try:
                raw = input("addrbook> ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\n[AddrBook] Closed.")
                break
            if not raw:
                continue
            parts = raw.split(maxsplit=1)
            cmd   = parts[0].lower()
            if cmd in ("exit", "quit"):
                break
            elif cmd == "list":
                self.list_contacts()
            elif cmd == "add":
                name  = input("  Name/alias : ").strip()
                coord = input("  Coordinate : ").strip()
                notes = input("  Notes      : ").strip()
                try:
                    self.add_contact(name, int(coord), notes=notes)
                except ValueError:
                    print("  Coordinate must be an integer.")
            elif cmd == "remove":
                name = parts[1].strip() if len(parts) > 1 else input("  Name: ").strip()
                self.remove_contact(name)
            elif cmd == "lookup":
                name  = parts[1].strip() if len(parts) > 1 else input("  Name: ").strip()
                coord = self.get_contact(name)
                if coord is not None:
                    print(f"  {name}  ->  {str(coord)[:40]}...")
            else:
                print("  Commands: list | add | remove | lookup | exit")


# ===========================================================================
# BEACON REGISTRY
# ===========================================================================

def _load_beacons() -> list:
    if os.path.exists(BEACON_FILE):
        with open(BEACON_FILE) as f:
            data = json.load(f)
        return data.get("beacons", [])
    return []

def _save_beacons(beacons: list):
    with open(BEACON_FILE, "w") as f:
        json.dump({"beacons": beacons}, f, indent=2)

def register_beacon(name, coordinate, msg_length=64, notes="") -> dict:
    beacons = _load_beacons()
    record = {"name": name, "coordinate": str(coordinate),
              "msg_length": msg_length, "notes": notes, "registered": _now_str()}
    beacons = [b for b in beacons if b.get("name") != name]
    beacons.append(record)
    _save_beacons(beacons)
    print(f"  Beacon registered: {name}  ->  {str(coordinate)[:24]}...")
    return record

def unregister_beacon(name: str):
    beacons = _load_beacons()
    new_list = [b for b in beacons if b.get("name") != name]
    if len(new_list) == len(beacons):
        print(f"  Beacon '{name}' not found.")
        return
    _save_beacons(new_list)
    print(f"  Beacon removed: {name}")

def list_beacons():
    beacons = _load_beacons()
    if not beacons:
        print("  No beacons registered.")
        return
    border = "-" * 68
    print(f"\n  {border}")
    print(f"  BEACON REGISTRY  --  {len(beacons)} beacon(s)")
    print(f"  {border}")
    for b in beacons:
        coord_p = str(b["coordinate"])[:20] + "..."
        notes_p = (b.get("notes") or "")[:24]
        print(f"  {b['name']:<26}  {coord_p:>23}  {b.get('msg_length','?'):>3}  {notes_p}")
    print(f"  {border}\n")

def _poll_beacons(coord: dict) -> list:
    beacons = _load_beacons()
    if not beacons:
        return []
    received = []
    print(f"\n[Beacons] Polling {len(beacons)} registered beacon(s)...")
    for beacon in beacons:
        name       = beacon.get("name", "unnamed")
        coord_int  = int(beacon["coordinate"])
        msg_length = beacon.get("msg_length", 64)
        print(f"  [Beacon] {name}  coord={str(coord_int)[:20]}...")
        try:
            cg = _cg_from_coord(coord)
            cg.Vs[0] = cg.hm.from_int(coord_int)
            decoded_bytes = [cg._decode_step(0) for _ in range(msg_length)]
            text = bytes(decoded_bytes).decode("utf-8", errors="replace")
        except Exception as e:
            print(f"    Decode error: {e}")
            continue
        header = _parse_header(text)
        if header is None:
            print(f"    -- No valid header found.")
            continue
        hash_ok = _verify_hash(text, header.get("tuple_hash", "-1"))
        if not _temporal_filter(header):
            print(f"    -- Future-dated (TO_DATE={header.get('to_date')}) -- ignored.")
            continue
        header["recv_time"] = _now_str()
        msg_record = {
            "source": f"beacon:{name}", "probe_V": str(coord_int)[:30],
            "from_date": header.get("from_date"), "to_date": header.get("to_date"),
            "from_time": header.get("from_time"), "recv_time": header["recv_time"],
            "tuple_hash": header.get("tuple_hash"), "hash_ok": hash_ok,
            "subject": header.get("subject", "(no subject)"),
            "payload": header.get("payload", ""), "raw_text": text,
        }
        received.append(msg_record)
        fname = os.path.join(DIR_RECEIVED, f"beacon_{name}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json")
        _save_json(fname, msg_record)
        print(f"    Beacon message received  hash={'OK' if hash_ok else 'FAIL'}")
    print(f"[Beacons] Done. {len(received)} beacon message(s) received.")
    return received


# ===========================================================================
# 1. coordinate_generator
# ===========================================================================

def coordinate_generator(passphrase, num_digits=150, output_file=COORD_FILE) -> dict:
    print(f"\n[CoordGen] Generating coordinate  (passphrase='{passphrase}', digits={num_digits})")
    seed_bytes   = passphrase.encode("utf-8")
    random.seed(seed_bytes)
    rand_payload = bytes(random.randint(0, 255) for _ in range(num_digits))
    cg = ChartGenerator(chart_base=256, num_digits=num_digits)
    for i in range(len(rand_payload) - 1, -1, -1):
        cg._encode_step(rand_payload[i], 0)
    V_int = cg.hm.to_int(cg.Vs[0])
    coord = {
        "passphrase_hint": passphrase[:4] + "****",
        "chart_base": 256, "mask_base": 1_000_000_000_000,
        "num_digits": num_digits, "num_n_streams": 12,
        "V": cg.hm.serialize(cg.Vs[0]), "R": cg.hm.serialize(cg.Rs[0]),
        "V_int_preview": str(V_int)[:30] + "...", "created": _now_str(),
        "polling_high": None, "polling_low": None, "message_length": None,
        "rt_polling_low": None, "rt_polling_high": None,
    }
    _save_json(output_file, coord)
    print(f"[CoordGen] Coordinate written -> {output_file}")
    print(f"[CoordGen] V (first 30 digits): {str(V_int)[:30]}...")
    return coord


# ===========================================================================
# 2. polling_range_finder  (temporal -- asymmetric window)
# FIX: uses integer-only mean/std_dev to avoid OverflowError
# ===========================================================================

def polling_range_finder(num_samples=30, coord_file=COORD_FILE, sample_file=None):
    coord   = _load_json(coord_file)
    msg_len = coord.get("message_length") or 64
    print(f"\n[RangeFinder] samples={num_samples}  msg_len={msg_len}")

    file_bytes = None
    if sample_file and os.path.exists(sample_file):
        with open(sample_file, "rb") as f:
            file_bytes = f.read()
        if len(file_bytes) < msg_len:
            file_bytes = None
            print("  Sample file too short; falling back to os.urandom.")

    samples = []
    for i in range(num_samples):
        if file_bytes is not None:
            offset = (i * msg_len) % (len(file_bytes) - msg_len)
            data   = file_bytes[offset : offset + msg_len]
        else:
            data = os.urandom(msg_len)
        cg = _cg_from_coord(coord)
        for j in range(len(data) - 1, -1, -1):
            cg._encode_step(data[j], 0)
        samples.append(cg.hm.to_int(cg.Vs[0]))
        if (i + 1) % 5 == 0:
            print(f"  [{i+1}/{num_samples}] coord: {str(samples[-1])[:20]}...")

    # ---- INTEGER-ONLY statistics (fixes OverflowError) -------------------
    mean    = _int_mean(samples)
    std_dev = _int_std_dev(samples, mean)

    polling_low  = mean + (4  * std_dev) // 10   # mean + 0.4 * std_dev
    polling_high = mean + (35 * std_dev) // 10   # mean + 3.5 * std_dev
    # ----------------------------------------------------------------------

    print(f"\n[RangeFinder] Mean        : {str(mean)[:30]}")
    print(f"[RangeFinder] Std Dev     : {std_dev:,}")
    print(f"[RangeFinder] Polling LOW : {str(polling_low)[:30]}")
    print(f"[RangeFinder] Polling HIGH: {str(polling_high)[:30]}")

    coord["polling_low"]    = str(polling_low)
    coord["polling_high"]   = str(polling_high)
    coord["message_length"] = msg_len
    coord["range_std_dev"]  = str(std_dev)
    coord["range_samples"]  = num_samples
    _save_json(coord_file, coord)
    print(f"[RangeFinder] Polling range saved -> {coord_file}")
    return polling_low, polling_high


# ===========================================================================
# 3. Temporal header helpers
# ===========================================================================

def _build_header(to_date: str, subject: str = "") -> str:
    lines = [
        f"FROM_DATE   {_today_str()}",
        f"TO_DATE     {to_date}",
        f"FROM_TIME   {datetime.now().strftime('%H:%M:%S')}",
        f"RECV_TIME   ----------",
        f"TUPLE_HASH  ???",
        HEADER_SEP,
    ]
    if subject:
        lines.append(f"SUBJECT     {subject}")
    return "\n".join(lines) + "\n"

def _finalize_message(full_text: str) -> str:
    encoded   = full_text.encode("utf-8")
    last_byte = encoded[-1]
    return full_text.replace("TUPLE_HASH  ???", f"TUPLE_HASH  {last_byte:03d}")

def _parse_header(text: str):
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

def _verify_hash(text: str, claimed_hash: str) -> bool:
    try:
        return text.encode("utf-8")[-1] == int(claimed_hash)
    except Exception:
        return False

def _temporal_filter(header: dict) -> bool:
    try:
        return date.fromisoformat(header.get("to_date", "")) <= date.today()
    except ValueError:
        return False


# ===========================================================================
# 4. polling
# ===========================================================================

def polling(coord_file=COORD_FILE) -> list:
    _ensure_dirs()
    coord   = _load_json(coord_file)
    low     = int(coord["polling_low"])
    high    = int(coord["polling_high"])
    msg_len = coord.get("message_length", 64)

    print(f"\n[Polling] Scanning {str(low)[:20]}...  ->  {str(high)[:20]}...")
    print(f"[Polling] Message length: {msg_len} bytes")

    span       = high - low
    num_probes = min(200, max(10, span // max(1, msg_len * 10)))
    step       = span // num_probes if num_probes > 0 else 1

    print(f"[Polling] Probing {num_probes} coordinates...")

    received = []
    for probe_idx in range(num_probes):
        probe_V = low + probe_idx * step
        cg      = _cg_from_coord(coord)
        cg.Vs[0] = cg.hm.from_int(probe_V)
        decoded_bytes = []
        try:
            for _ in range(msg_len):
                decoded_bytes.append(cg._decode_step(0))
        except Exception:
            continue
        try:
            text = bytes(decoded_bytes).decode("utf-8", errors="replace")
        except Exception:
            continue
        header = _parse_header(text)
        if header is None:
            continue
        hash_ok = _verify_hash(text, header.get("tuple_hash", "-1"))
        if not _temporal_filter(header):
            print(f"  [Polling] Future-dated ignored (TO_DATE={header.get('to_date')})")
            continue
        header["recv_time"] = _now_str()
        msg_record = {
            "probe_V": str(probe_V)[:30], "from_date": header.get("from_date"),
            "to_date": header.get("to_date"), "from_time": header.get("from_time"),
            "recv_time": header["recv_time"], "tuple_hash": header.get("tuple_hash"),
            "hash_ok": hash_ok, "subject": header.get("subject", "(no subject)"),
            "payload": header.get("payload", ""), "raw_text": text,
        }
        received.append(msg_record)
        fname = os.path.join(DIR_RECEIVED, f"msg_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json")
        _save_json(fname, msg_record)
        print(f"  [Polling] Received -- TO_DATE={header.get('to_date')}  hash={'OK' if hash_ok else 'FAIL'}")

    print(f"[Polling] Done. {len(received)} message(s) received from coordinate space.")
    beacon_msgs = _poll_beacons(coord)
    received.extend(beacon_msgs)
    return received


# ===========================================================================
# 5. range_padder
# ===========================================================================

def range_padder(msg_json_path: str, coord_file=COORD_FILE) -> str:
    coord   = _load_json(coord_file)
    msg     = _load_json(msg_json_path)
    payload = msg["payload"].encode("utf-8")
    low  = int(coord["polling_low"])
    high = int(coord["polling_high"])
    print(f"\n[RangePadder] Target range: {str(low)[:20]}  ->  {str(high)[:20]}")
    max_pad = coord.get("message_length", 64) * 4
    for pad in range(max_pad):
        test_data = payload + b'\x00' * pad
        cg        = _cg_from_coord(coord)
        for i in range(len(test_data) - 1, -1, -1):
            cg._encode_step(test_data[i], 0)
        final_V = cg.hm.to_int(cg.Vs[0])
        if low <= final_V <= high:
            padded_text        = payload.decode("utf-8") + '\x00' * pad
            msg["payload"]     = padded_text
            msg["pad_bytes"]   = pad
            msg["final_coord"] = str(final_V)[:30]
            _save_json(msg_json_path, msg)
            print(f"[RangePadder] Padded {pad} bytes -> coordinate IN RANGE")
            return padded_text
    print(f"[RangePadder] Could not land in range within {max_pad} padding bytes.")
    return payload.decode("utf-8")


# ===========================================================================
# 6. compose_message / send_outbox
# ===========================================================================

def compose_message(to_date, subject, body, coord_file=COORD_FILE) -> str:
    _ensure_dirs()
    header   = _build_header(to_date, subject)
    full     = _finalize_message(header + body)
    msg_data = {"to_date": to_date, "subject": subject, "payload": full,
                "composed": _now_str(), "status": "outbox"}
    fname = os.path.join(DIR_OUTBOX, f"draft_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json")
    _save_json(fname, msg_data)
    print(f"[Compose] Draft saved -> {fname}")
    return fname

def send_outbox(coord_file=COORD_FILE):
    _ensure_dirs()
    outbox_files = sorted(f for f in os.listdir(DIR_OUTBOX) if f.endswith(".json"))
    if not outbox_files:
        print("[Outbox] Empty -- nothing to send.")
        return
    for fname in outbox_files:
        src_path = os.path.join(DIR_OUTBOX, fname)
        print(f"\n[Outbox] Encoding {fname}...")
        try:
            range_padder(src_path, coord_file)
        except Exception as e:
            print(f"  range_padder failed: {e}  (sending as-is)")
        msg           = _load_json(src_path)
        payload_bytes = msg["payload"].encode("utf-8")
        coord         = _load_json(coord_file)
        cg            = _cg_from_coord(coord)
        for i in range(len(payload_bytes) - 1, -1, -1):
            cg._encode_step(payload_bytes[i], 0)
        final_V          = cg.hm.to_int(cg.Vs[0])
        msg["status"]    = "sent"
        msg["sent_time"] = _now_str()
        msg["coord"]     = str(final_V)[:30]
        sent_path = os.path.join(DIR_SENT, fname.replace("draft_", "sent_"))
        _save_json(sent_path, msg)
        os.remove(src_path)
        print(f"[Outbox] Sent -> {sent_path}  coord={msg['coord']}")


# ===========================================================================
# 7. Realtime store helpers
# ===========================================================================

def _load_rt_store() -> dict:
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


# ===========================================================================
# 8. calculate_tight_polling_range  (realtime -- tight +/-0.5 sigma window)
# FIX: uses integer-only mean/std_dev to avoid OverflowError
# ===========================================================================

def calculate_tight_polling_range(coord_file=COORD_FILE, message_length=RT_MSG_LEN_DEFAULT, num_samples=RT_SAMPLES_DEFAULT):
    coord = _load_json(coord_file)
    print(f"\n[RT RangeFinder] samples={num_samples}  msg_len={message_length}")

    samples = []
    for i in range(num_samples):
        data = os.urandom(message_length)
        cg   = _cg_from_coord(coord)
        for j in range(len(data) - 1, -1, -1):
            cg._encode_step(data[j], 0)
        v = cg.hm.to_int(cg.Vs[0])
        samples.append(v)
        print(f"  [{i+1}/{num_samples}] V={str(v)[:24]}...")

    # ---- INTEGER-ONLY statistics (fixes OverflowError) -------------------
    mean    = _int_mean(samples)
    std_dev = _int_std_dev(samples, mean)

    # +/-0.5 sigma  ->  mean - std_dev//2  ..  mean + std_dev//2
    low  = mean - std_dev // 2
    high = mean + std_dev // 2
    if low >= high:
        low  = mean - abs(std_dev)
        high = mean + abs(std_dev)
    # ----------------------------------------------------------------------

    print(f"\n[RT RangeFinder] Mean    : {str(mean)[:30]}")
    print(f"[RT RangeFinder] Std Dev : {std_dev:,}")
    print(f"[RT RangeFinder] Window  : {str(low)[:24]}  ->  {str(high)[:24]}  (tight +/-0.5 sigma)")

    coord["rt_polling_low"]    = str(low)
    coord["rt_polling_high"]   = str(high)
    coord["rt_message_length"] = message_length
    coord["rt_std_dev"]        = str(std_dev)
    coord["rt_range_samples"]  = num_samples
    _save_json(coord_file, coord)
    print(f"[RT RangeFinder] Saved -> {coord_file}")
    return low, high


# ===========================================================================
# 9. send_realtime
# ===========================================================================

_MORSE_MAP = {
    'A': '.-',   'B': '-...', 'C': '-.-.', 'D': '-..',  'E': '.',
    'F': '..-.', 'G': '--.',  'H': '....', 'I': '..',   'J': '.---',
    'K': '-.-',  'L': '.-..', 'M': '--',   'N': '-.',   'O': '---',
    'P': '.--.',  'Q': '--.-', 'R': '.-.',  'S': '...',  'T': '-',
    'U': '..-',  'V': '...-', 'W': '.--',  'X': '-..-', 'Y': '-.--',
    'Z': '--..',
    '0': '-----', '1': '.----', '2': '..---', '3': '...--', '4': '....-',
    '5': '.....', '6': '-....', '7': '--...', '8': '---..', '9': '----.',
    ' ': '/',
}

def _morse_encode(text: str) -> bytes:
    bits  = []
    words = text.upper().split(' ')
    for wi, word in enumerate(words):
        if wi > 0:
            bits += [0, 0, 0, 0, 0, 0, 0]
        for li, ch in enumerate(word):
            if li > 0:
                bits += [0]
            for sym in _MORSE_MAP.get(ch, '...---'):
                if sym == '.':
                    bits.append(1)
                else:
                    bits.extend([1, 1, 1])
    bits += [0] * ((8 - len(bits) % 8) % 8)
    result = bytearray()
    for i in range(0, len(bits), 8):
        byte_val = 0
        for b in bits[i:i + 8]:
            byte_val = (byte_val << 1) | b
        result.append(byte_val)
    return bytes(result)

def _text_to_bytes(text: str, use_morse: bool = False) -> bytes:
    return _morse_encode(text) if use_morse else text.encode("utf-8")

def send_realtime(to, message, reply_to=None, coord_file=COORD_FILE, use_morse=False, my_id=None):
    _ensure_dirs()
    store = _load_rt_store()
    if my_id is None:
        my_id = store.get("_my_id") or str(uuid.uuid4())[:8]
        store["_my_id"] = my_id
    coord     = _load_json(coord_file)
    msg_bytes = _text_to_bytes(message, use_morse)
    cg = _cg_from_coord(coord)
    for i in range(len(msg_bytes) - 1, -1, -1):
        cg._encode_step(msg_bytes[i], 0)
    coordinate = cg.hm.to_int(cg.Vs[0])
    msg_id     = str(uuid.uuid4()).replace("-", "")[:12]
    record = {
        "msg_id": msg_id, "from": my_id, "to": to, "reply_to": reply_to,
        "message": message, "preview": message[:60], "coordinate": str(coordinate),
        "timestamp": _now_str(), "status": "sent", "use_morse": use_morse,
    }
    store[msg_id] = record
    _save_rt_store(store)
    _save_json(os.path.join(DIR_RT, f"{msg_id}.json"), record)
    print(f"  Sent  | ID: {msg_id}  to: {to}  coord: {str(coordinate)[:24]}...")
    if reply_to:
        print(f"     reply to: {reply_to}")
    return msg_id, coordinate


# ===========================================================================
# 10. poll_realtime
# ===========================================================================

def poll_realtime(coord_file=COORD_FILE, my_id=None) -> list:
    store = _load_rt_store()
    if my_id is None:
        my_id = store.get("_my_id", "")
    coord  = _load_json(coord_file)
    low_s  = coord.get("rt_polling_low")
    high_s = coord.get("rt_polling_high")
    my_sent_ids = {
        mid for mid, rec in store.items()
        if not mid.startswith("_") and rec.get("from") == my_id
    }
    print(f"\n  Realtime Poll  |  node: {my_id or '?'}  |  {_now_str()}")
    received = []
    for mid, rec in list(store.items()):
        if mid.startswith("_"):
            continue
        if rec.get("status") in ("delivered", "replied"):
            continue
        addressed_to_me = rec.get("to") == my_id
        reply_to_mine   = rec.get("reply_to") in my_sent_ids
        if addressed_to_me or reply_to_mine:
            rec["status"]    = "delivered"
            rec["recv_time"] = _now_str()
            store[mid]       = rec
            _save_json(os.path.join(DIR_RT, f"{mid}.json"), rec)
            tag = "-> to me" if addressed_to_me else f"reply to {rec['reply_to']}"
            print(f"  [{tag}]  ID: {mid}  from: {rec.get('from', '?')}")
            print(f"       \"{rec.get('preview', '')}\"")
            received.append(rec)
            if reply_to_mine:
                orig_id = rec.get("reply_to")
                if orig_id and orig_id in store:
                    store[orig_id]["status"] = "replied"
    if low_s and high_s:
        low, high = int(low_s), int(high_s)
        span       = high - low
        num_probes = min(20, max(5, span // max(1, 10_000)))
        step       = max(1, span // num_probes)
        known_coords = {rec.get("coordinate"): mid for mid, rec in store.items() if not mid.startswith("_")}
        probe_hits = sum(1 for pi in range(num_probes) if str(low + pi * step) in known_coords)
        print(f"\n  [Window probe] span: {span:,}  probes: {num_probes}  coord hits: {probe_hits}")
    else:
        print("  No realtime range set -- run 'rtrange' first.")
    _save_rt_store(store)
    if not received:
        print("  No new messages.")
    else:
        print(f"\n  {len(received)} new message(s) received.")
    return received


# ===========================================================================
# 11. simulate_reply
# ===========================================================================

def simulate_reply(original_msg_id, reply_text=None, from_node="future_self", coord_file=COORD_FILE) -> str:
    store = _load_rt_store()
    if original_msg_id not in store:
        print(f"  simulate_reply: msg_id '{original_msg_id}' not found.")
        return ""
    original   = store[original_msg_id]
    reply_text = reply_text or (f"[AUTO-REPLY] Received: \"{original.get('preview', '')[:40]}\"")
    reply_id = str(uuid.uuid4()).replace("-", "")[:12]
    record = {
        "msg_id": reply_id, "from": from_node, "to": original.get("from", "unknown"),
        "reply_to": original_msg_id, "message": reply_text, "preview": reply_text[:60],
        "coordinate": str(random.randint(10**10, 10**15)),
        "timestamp": _now_str(), "status": "sent", "simulated": True,
    }
    store[reply_id] = record
    _save_rt_store(store)
    _ensure_dirs()
    _save_json(os.path.join(DIR_RT, f"{reply_id}.json"), record)
    print(f"  Simulated reply  | ID: {reply_id}  to: {record['to']}")
    print(f"     \"{reply_text[:60]}\"")
    return reply_id


# ===========================================================================
# 12. GrokComms  -- unified OO interface
# ===========================================================================

class GrokComms:
    def __init__(self, coord_file: str = COORD_FILE):
        self.coord_file   = coord_file
        self.address_book = OdinAddressBook()
        store             = _load_rt_store()
        self.my_id        = store.get("_my_id") or str(uuid.uuid4())[:8]
        store["_my_id"]   = self.my_id
        _save_rt_store(store)

    def coordinate_generator(self, passphrase, num_digits=150):
        return coordinate_generator(passphrase, num_digits, self.coord_file)
    def polling_range_finder(self, num_samples=30, sample_file=None):
        return polling_range_finder(num_samples, self.coord_file, sample_file)
    def polling(self):
        return polling(self.coord_file)
    def range_padder(self, msg_json_path):
        return range_padder(msg_json_path, self.coord_file)
    def compose_message(self, to_date, subject, body):
        return compose_message(to_date, subject, body, self.coord_file)
    def send_outbox(self):
        return send_outbox(self.coord_file)
    def calculate_tight_polling_range(self, message_length=RT_MSG_LEN_DEFAULT, num_samples=RT_SAMPLES_DEFAULT):
        return calculate_tight_polling_range(self.coord_file, message_length, num_samples)
    def send_realtime(self, to, message, reply_to=None, use_morse=False):
        resolved_coord = self.address_book.get_contact(to)
        to_id = str(resolved_coord) if resolved_coord is not None else to
        return send_realtime(to_id, message, reply_to, self.coord_file, use_morse, self.my_id)
    def poll_realtime(self):
        return poll_realtime(self.coord_file, self.my_id)
    def simulate_reply(self, original_msg_id, reply_text=None, from_node="future_self"):
        return simulate_reply(original_msg_id, reply_text, from_node, self.coord_file)
    def register_beacon(self, name, coordinate, msg_length=64, notes=""):
        return register_beacon(name, coordinate, msg_length, notes)
    def unregister_beacon(self, name):
        return unregister_beacon(name)
    def list_beacons(self):
        return list_beacons()


# ===========================================================================
# 13. Interactive CLIs
# ===========================================================================

_RT_HELP = """
  REALTIME COMMANDS
  ---------------------------------------------------------
  send <to> <message>         Send a message to a node id or alias
  reply <msg_id> <message>    Reply to a specific message
  poll                        Scan for new messages / replies
  simulate <msg_id> [text]    Inject an auto-reply (for testing)
  history [n]                 Show last n messages (default 10)
  status <msg_id>             Show full record for a message
  rtrange                     Recalculate tight polling range
  myid                        Show / change your node id
  contacts                    Open address book
  help / exit / quit
  ---------------------------------------------------------
"""

def realtime_comms(coord_file=COORD_FILE):
    _ensure_dirs()
    if not os.path.exists(coord_file):
        print(f"\n  Coordinate file '{coord_file}' not found.")
        print("     Run coordinate_generator() first.")
        return
    store = _load_rt_store()
    my_id = store.get("_my_id") or str(uuid.uuid4())[:8]
    store["_my_id"] = my_id
    _save_rt_store(store)
    coord   = _load_json(coord_file)
    rt_low  = coord.get("rt_polling_low",  "not set")
    rt_high = coord.get("rt_polling_high", "not set")
    addr    = OdinAddressBook()
    print("\n" + "=" * 60)
    print("  GROKCOMMS -- REALTIME NODE")
    print("=" * 60)
    print(f"  Node ID    : {my_id}")
    print(f"  Coord file : {coord_file}")
    print(f"  RT window  : {str(rt_low)[:22]}  ->  {str(rt_high)[:22]}")
    print("\n  Type 'help' for commands.\n")
    while True:
        try:
            raw = input("realtime> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n[Realtime] Session ended.")
            break
        if not raw:
            continue
        parts = raw.split(maxsplit=2)
        cmd   = parts[0].lower()
        if cmd in ("exit", "quit"):
            print("[Realtime] Goodbye.")
            break
        elif cmd == "help":
            print(_RT_HELP)
        elif cmd == "myid":
            store  = _load_rt_store()
            cur_id = store.get("_my_id", my_id)
            print(f"  Current node ID: {cur_id}")
            new_id = input("  New node ID (Enter to keep): ").strip()
            if new_id:
                my_id = new_id
                store["_my_id"] = my_id
                _save_rt_store(store)
                print(f"  Node ID updated -> {my_id}")
        elif cmd == "send":
            if len(parts) < 3:
                print("  Usage: send <to> <message>")
                continue
            to_raw    = parts[1]
            coord_val = addr.get_contact(to_raw)
            to_id     = str(coord_val) if coord_val is not None else to_raw
            try:
                send_realtime(to_id, parts[2], coord_file=coord_file, my_id=my_id)
            except Exception as e:
                print(f"  {e}")
        elif cmd == "reply":
            if len(parts) < 3:
                print("  Usage: reply <msg_id> <message>")
                continue
            store   = _load_rt_store()
            orig    = store.get(parts[1])
            to_node = orig.get("from", "unknown") if orig else "unknown"
            try:
                send_realtime(to_node, parts[2], reply_to=parts[1], coord_file=coord_file, my_id=my_id)
            except Exception as e:
                print(f"  {e}")
        elif cmd == "poll":
            poll_realtime(coord_file=coord_file, my_id=my_id)
        elif cmd == "simulate":
            if len(parts) < 2:
                print("  Usage: simulate <msg_id> [reply text]")
                continue
            simulate_reply(parts[1], parts[2] if len(parts) >= 3 else None, coord_file=coord_file)
            print("  -> Run 'poll' to receive the simulated reply.")
        elif cmd == "history":
            n = 10
            if len(parts) >= 2:
                try:
                    n = int(parts[1])
                except ValueError:
                    pass
            store = _load_rt_store()
            msgs  = sorted(
                [(mid, rec) for mid, rec in store.items() if not mid.startswith("_")],
                key=lambda x: x[1].get("timestamp", ""), reverse=True,
            )
            print(f"\n  HISTORY (last {min(n, len(msgs))} of {len(msgs)} messages)")
            print(f"  {'ID':<14} {'FROM':<12} {'TO':<12} {'STATUS':<12} PREVIEW")
            print("  " + "-" * 66)
            for mid, rec in msgs[:n]:
                sim_tag   = " [sim]" if rec.get("simulated") else ""
                reply_tag = f" re:{rec['reply_to'][:8]}" if rec.get("reply_to") else ""
                print(f"  {mid:<14} {rec.get('from','?'):<12} {rec.get('to','?'):<12} "
                      f"{rec.get('status','?'):<12} \"{rec.get('preview','')[:28]}\"{sim_tag}{reply_tag}")
            print()
        elif cmd == "status":
            if len(parts) < 2:
                print("  Usage: status <msg_id>")
                continue
            store = _load_rt_store()
            rec   = store.get(parts[1])
            if rec is None:
                print(f"  msg_id '{parts[1]}' not found.")
            else:
                print("\n" + "-" * 50)
                print(json.dumps(rec, indent=2))
                print("-" * 50)
        elif cmd == "rtrange":
            try:
                msg_len = int(input(f"  Message length bytes (Enter for {RT_MSG_LEN_DEFAULT}): ").strip() or RT_MSG_LEN_DEFAULT)
            except ValueError:
                msg_len = RT_MSG_LEN_DEFAULT
            try:
                n_samp = int(input(f"  Num samples (Enter for {RT_SAMPLES_DEFAULT}): ").strip() or RT_SAMPLES_DEFAULT)
            except ValueError:
                n_samp = RT_SAMPLES_DEFAULT
            try:
                calculate_tight_polling_range(coord_file, msg_len, n_samp)
                coord   = _load_json(coord_file)
                rt_low  = coord.get("rt_polling_low",  "not set")
                rt_high = coord.get("rt_polling_high", "not set")
                print(f"\n  New RT window: {str(rt_low)[:22]}  ->  {str(rt_high)[:22]}")
            except Exception as e:
                print(f"  rtrange error: {e}")
        elif cmd == "contacts":
            addr.interactive_menu()
        else:
            print(f"  [?] Unknown command: '{cmd}'.  Type 'help'.")


def temporal_comms(coord_file=TNODE_COORD):
    _ensure_dirs()
    if not os.path.exists(coord_file):
        print(f"\n[TemporalComms] No coordinate file found at '{coord_file}'.")
        phrase = input("  Enter a passphrase to generate one: ").strip()
        coordinate_generator(phrase, num_digits=150, output_file=coord_file)
        print("\n  Run 'pollrange' next to set the polling range.")
    addr  = OdinAddressBook()
    coord = _load_json(coord_file)
    print("\n" + "=" * 62)
    print("  TEMPORAL COMMUNICATIONS NODE  (Temporal Comms)")
    print("=" * 62)
    print(f"  Coord file : {coord_file}")
    print(f"  Poll range : {str(coord.get('polling_low','?'))[:20]}... -> {str(coord.get('polling_high','?'))[:20]}...")
    print(f"  Msg length : {coord.get('message_length', '?')} bytes")
    print(f"  Beacons    : {len(_load_beacons())} registered")
    print("\n  Commands: polling | received | sent | inbox | outbox")
    print("            compose | send | pollrange | coordgen")
    print("            beacons | contacts | realtime | help | exit\n")
    while True:
        try:
            raw = input("temporal> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n[TemporalComms] Session ended.")
            break
        if not raw:
            continue
        parts = raw.split(maxsplit=3)
        cmd   = parts[0].lower()
        if cmd in ("exit", "quit"):
            print("[TemporalComms] Goodbye.")
            break
        elif cmd == "help":
            print("""
  polling           -- scan coordinate space + poll all beacons
  received          -- list messages in received/
  inbox             -- list messages already read (inbox/)
  sent              -- list messages in sent/
  outbox            -- list pending drafts in outbox/
  compose           -- write a new message
  send              -- encode & send all outbox drafts
  pollrange         -- recalculate polling range
  coordgen          -- generate a new coordinate
  beacons           -- manage beacon registry
  contacts          -- manage address book
  realtime          -- switch to realtime communications CLI
  exit / quit
""")
        elif cmd == "polling":
            polling(coord_file)
        elif cmd in ("received", "inbox", "sent", "outbox"):
            folder = cmd
            try:
                files = sorted(f for f in os.listdir(folder) if f.endswith(".json"))
            except FileNotFoundError:
                print(f"  [{folder.upper()}] Directory not found.")
                continue
            if not files:
                print(f"  [{folder.upper()}] Empty.")
                continue
            print(f"\n  {folder.upper()} ({len(files)} message(s)):")
            for i, fn in enumerate(files, 1):
                try:
                    m = _load_json(os.path.join(folder, fn))
                    print(f"  [{i}] {fn}")
                    print(f"       Subject : {m.get('subject', m.get('to_date', '?'))}")
                    print(f"       Date    : {m.get('composed', m.get('recv_time', '?'))}")
                    print(f"       Status  : {m.get('status', '?')}")
                except Exception:
                    print(f"  [{i}] {fn}  (unreadable)")
            pick = input("\n  Enter number to read (or Enter to skip): ").strip()
            if pick.isdigit():
                idx = int(pick) - 1
                if 0 <= idx < len(files):
                    fn  = files[idx]
                    msg = _load_json(os.path.join(folder, fn))
                    print("\n" + "-" * 50)
                    print(msg.get("raw_text") or msg.get("payload") or json.dumps(msg, indent=2))
                    print("-" * 50)
                    if folder == "received":
                        inbox_path = os.path.join(DIR_INBOX, fn)
                        shutil.move(os.path.join(folder, fn), inbox_path)
                        print("  -> Moved to inbox.")
        elif cmd == "compose":
            print("\n  [Compose]")
            to_date = input("  To Date (YYYY-MM-DD, or Enter for today): ").strip() or _today_str()
            subject = input("  Subject: ").strip()
            print("  Body (end with a line containing only '.'): ")
            body_lines = []
            while True:
                line = input()
                if line == ".":
                    break
                body_lines.append(line)
            compose_message(to_date, subject, "\n".join(body_lines), coord_file)
        elif cmd == "send":
            send_outbox(coord_file)
        elif cmd == "pollrange":
            coord   = _load_json(coord_file)
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
            src_file = input("  Sample file path (Enter for random bytes): ").strip() or None
            polling_range_finder(samples, coord_file, src_file)
        elif cmd == "coordgen":
            phrase = input("  Passphrase: ").strip()
            try:
                digits = int(input("  Num digits (Enter for 150): ").strip() or 150)
            except ValueError:
                digits = 150
            coordinate_generator(phrase, digits, coord_file)
        elif cmd == "beacons":
            _beacons_menu()
        elif cmd == "contacts":
            addr.interactive_menu()
        elif cmd == "realtime":
            realtime_comms(coord_file)
        else:
            print(f"  [?] Unknown command: '{cmd}'.  Type 'help'.")


def _beacons_menu():
    print("\n" + "=" * 52)
    print("  BEACON REGISTRY")
    print("=" * 52)
    print("  Commands: list | add | remove | exit\n")
    while True:
        try:
            raw = input("beacons> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n[Beacons] Closed.")
            break
        if not raw:
            continue
        parts = raw.split(maxsplit=1)
        cmd   = parts[0].lower()
        if cmd in ("exit", "quit"):
            break
        elif cmd == "list":
            list_beacons()
        elif cmd == "add":
            name  = input("  Beacon name    : ").strip()
            coord = input("  Coordinate     : ").strip()
            try:
                msg_len = int(input("  Msg length (Enter for 64): ").strip() or 64)
            except ValueError:
                msg_len = 64
            notes = input("  Notes          : ").strip()
            try:
                register_beacon(name, int(coord), msg_len, notes)
            except ValueError:
                print("  Coordinate must be an integer.")
        elif cmd == "remove":
            name = parts[1].strip() if len(parts) > 1 else input("  Beacon name: ").strip()
            unregister_beacon(name)
        else:
            print("  Commands: list | add | remove | exit")


# ===========================================================================
# 14. admin_menu
# ===========================================================================

def admin_menu():
    print("\n" + "*" * 62)
    print("  GROKCOMMS -- ADMIN MENU")
    print("*" * 62)
    print("  1. coordinate_generator")
    print("  2. polling_range_finder")
    print("  3. polling  (coordinate space + beacons)")
    print("  4. temporal_comms  (interactive Temporal node)")
    print("  5. range_padder  (on a specific message file)")
    print("  6. realtime_comms")
    print("  7. realtime demo  (automated send -> simulate -> poll cycle)")
    print("  8. address book")
    print("  9. beacon registry")
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
                digits = int(input("  Num digits (Enter for 150): ").strip() or 150)
            except ValueError:
                digits = 150
            out_file = input(f"  Output file (Enter for '{COORD_FILE}'): ").strip() or COORD_FILE
            coordinate_generator(phrase, digits, out_file)
        elif choice == "2":
            coord_f = input(f"  Coord file (Enter for '{COORD_FILE}'): ").strip() or COORD_FILE
            try:
                samples = int(input("  Num samples (Enter for 30): ").strip() or 30)
            except ValueError:
                samples = 30
            src_file = input("  Sample file (Enter for random): ").strip() or None
            polling_range_finder(samples, coord_f, src_file)
        elif choice == "3":
            coord_f = input(f"  Coord file (Enter for '{COORD_FILE}'): ").strip() or COORD_FILE
            if not os.path.exists(coord_f):
                print(f"  '{coord_f}' not found. Run coordinate_generator first.")
                continue
            coord = _load_json(coord_f)
            if not coord.get("polling_low"):
                print("  No polling range set. Run polling_range_finder first.")
                continue
            polling(coord_f)
        elif choice == "4":
            coord_f = input(f"  Coord file (Enter for '{TNODE_COORD}'): ").strip() or TNODE_COORD
            temporal_comms(coord_f)
        elif choice == "5":
            msg_f   = input("  Message JSON file path: ").strip()
            coord_f = input(f"  Coord file (Enter for '{COORD_FILE}'): ").strip() or COORD_FILE
            if not os.path.exists(msg_f):
                print(f"  '{msg_f}' not found.")
                continue
            range_padder(msg_f, coord_f)
        elif choice == "6":
            coord_f = input(f"  Coord file (Enter for '{COORD_FILE}'): ").strip() or COORD_FILE
            realtime_comms(coord_f)
        elif choice == "7":
            coord_f = input(f"  Coord file (Enter for '{COORD_FILE}'): ").strip() or COORD_FILE
            _demo_realtime(coord_f)
        elif choice == "8":
            OdinAddressBook().interactive_menu()
        elif choice == "9":
            _beacons_menu()
        else:
            print("  Enter 0-9.")


# ===========================================================================
# 15. Realtime demo
# ===========================================================================

def _demo_realtime(coord_file=COORD_FILE):
    if not os.path.exists(coord_file):
        print(f"  '{coord_file}' not found -- cannot run demo.")
        return
    print("\n" + "*" * 60)
    print("  REALTIME DATA POLLING -- DEMO")
    print("*" * 60)
    print("\n[Demo] Step 1 -- Calculate tight polling range")
    try:
        calculate_tight_polling_range(coord_file, message_length=16, num_samples=8)
    except Exception as e:
        print(f"  (range calc skipped: {e})")
    print("\n[Demo] Step 2 -- Send a message")
    store = _load_rt_store()
    my_id = store.get("_my_id") or str(uuid.uuid4())[:8]
    store["_my_id"] = my_id
    _save_rt_store(store)
    try:
        msg_id, coord_val = send_realtime(to="bob", message="Hello Bob from demo!", coord_file=coord_file, my_id=my_id)
    except Exception as e:
        print(f"  send failed: {e}")
        return
    print("\n[Demo] Step 3 -- Simulate reply from Bob")
    reply_id = simulate_reply(msg_id, reply_text="Hey! Got your message. All systems go.", from_node="bob", coord_file=coord_file)
    print("\n[Demo] Step 4 -- Poll for replies")
    received = poll_realtime(coord_file=coord_file, my_id=my_id)
    print("\n[Demo] Summary")
    print(f"  Sent    msg_id : {msg_id}")
    print(f"  Reply   msg_id : {reply_id}")
    print(f"  Received count : {len(received)}")
    for rec in received:
        print(f"    \"{rec.get('preview', '')}\"  (status: {rec.get('status')})")
    print("\n[Demo] Done. Run realtime_comms() for the interactive CLI.")


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        _demo_realtime()
    elif len(sys.argv) > 1 and sys.argv[1] == "realtime":
        realtime_comms()
    elif len(sys.argv) > 1 and sys.argv[1] == "temporal":
        temporal_comms()
    else:
        admin_menu()

