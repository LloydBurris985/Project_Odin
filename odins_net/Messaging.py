# odins_net/messaging.py
# Unified Odins Mail + Odins Temporal – Async chained & live temporal messaging
# Integrates prefix/hash wrapper, encryption, BNS chaining, validation, runway delivery
# MIT License

import json
import time
import hashlib
import secrets
import math
from datetime import datetime
from typing import Dict, List, Optional, Any
import threading
import logging
from io import BytesIO

from .core import OdinsEye
from .rng import BNSRNG  # Move BNSRNG here if not already in rng.py
from .runway import Runway, RunwayPoller
from .nexus_hub import get_odins_hall_runway, create_default_poller
from cryptography.fernet import Fernet

# Optional media validation (graceful fallback)
try:
    from PIL import Image
    import moviepy.editor as mp
    from pydub import AudioSegment
    MEDIA_LIBS_AVAILABLE = True
except ImportError:
    MEDIA_LIBS_AVAILABLE = False
    print("Media validation disabled (install pillow, moviepy, pydub)")

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("OdinsMessaging")

# Globals / Config
MAGIC_PREFIX = b"AB42"
POLL_INTERVAL_SEC = 60          # adjustable
POLL_BATCH_SIZE = 1000
POLL_STEP_SIZE = 5
POLL_THROTTLE_SEC = 0.05
MAX_TEXT_LENGTH = 64 * 1024
DEFAULT_RUNWAY_LENGTH = 10000

BANNER = r"""
   _____           _   _   _____ _____ _____ _____ _____ 
  |  __ \         | \ | | / ____|  __ \_   _/ ____|  __ \
  | |  | |___  ___|  \| || |  __| |  | || || |  __| |__) |
  | |  | / __|/ _ \ . ` || | |_ | |  | || || | |_ |  _  / 
  | |__| \__ \  __/ |\  || |__| | |__| || || |__| | | \ \ 
  |_____/|___/\___|_| \_(_)_____|_____/_____\_____|_|  \_\
                                                              
TEMPORAL INTERGALACTIC BBS – Odins Net v0.1-dev
"The lattice is eternal. All information already exists."
"""

# Colors (ANSI)
GREEN = "\033[92m"
RESET = "\033[0m"
BOLD = "\033[1m"

print(BANNER)
print(f"{GREEN}{BOLD}Welcome, traveler.{RESET}")
print(f"Logged in as: {user.username}")
print(f"Current position: mask {user.runway_start}")
print("\nType ? for help | Q to quit\n")

class UserState:
    def main_menu(user: UserState, eye: OdinsEye, poller: RunwayPoller):
    while True:
        print("\n" + "="*60)
        print(f" {BOLD}TEMPORAL INTERGALACTIC BBS – Main Menu{RESET}")
        print("="*60)
        print(f" User: {user.username}   Position: {user.runway_start}–{user.runway_start + user.runway_length}")
        print(f" Last poll: {datetime.now().strftime('%Y-%m-%d %H:%M')} – {len(user.inbox)} unread")

        print("\nAvailable Boards (runways):")
        boards = [
            ("1", "Odins-Hall", "Public hub & announcements", 10000, 10099),
            ("2", "bubba-private", "Your personal mailbox & chains", user.runway_start, user.runway_start + user.runway_length),
            # Add more dynamically from known runways later
        ]
        for num, name, desc, start, end in boards:
            unread = " (NEW)" if name == "Odins-Hall" and len(user.inbox) > 0 else ""
            print(f"  {num}. {name:<15} {desc} ({start}–{end}){unread}")

        print("\nOther:")
        print("  7. Poll Now")
        print("  8. Compose / Post")
        print("  9. Active Chains")
        print(" 10. Queue (future delivery)")
        print(" 11. Suspect / Flagged")
        print(" 12. The Thing (disputes)")
        print("  ?  Help")
        print("  Q  Quit")

        choice = input("\nEnter choice [1-12,?,Q]: ").strip().lower()

        if choice == "q":
            print("Goodbye, traveler.")
            user.save()
            break

        elif choice == "?":
            print_help()

        elif choice == "7":
            poll_inbox(user, eye, poller)
            print(f"Poll complete – {len(user.inbox)} total messages")

        elif choice == "8":
            compose_post(user, eye)

        elif choice in ["1", "2"]:  # board selection
            board_idx = int(choice) - 1
            board_name = boards[board_idx][1]
            read_board(user, eye, board_name)

        # Add more handlers later (chains, queue, etc.)

def read_board(user: UserState, eye: OdinsEye, board_name: str):
    print(f"\nEntering board: {board_name}")
    # TODO: list threads/chains in this runway
    # For now, just show recent inbox items as placeholder
    for item in user.inbox[:10]:
        m = item["msg"]
        print(f"[{m['seq']}] {m['subject']} by {m['from']} ({m['sent_date']})")
    print("\n[R]eply   [Q]uit board")
    # etc.
    
    """Persistent user state: inbox, sent, queue, chains, runway config."""
    def __init__(self, username: str):
        self.username = username
        self.private_secret = secrets.token_bytes(32)
        self.runway_start = self._compute_runway_start()
        self.runway_length = DEFAULT_RUNWAY_LENGTH
        self.inbox: List[Dict] = []
        self.sent: List[Dict] = []
        self.queue: List[Dict] = []     # future delivery
        self.suspect: List[Dict] = []   # flagged trash/suspect
        self.active_chains: Dict[str, int] = {}  # chain_id -> last seq
        self.last_checked_mask = self.runway_start
        self.polling = False

    def _compute_runway_start(self) -> int:
        h = hashlib.sha256(self.private_secret + self.username.encode()).digest()
        return 50000 + int.from_bytes(h[:8], 'big') % 100000

    def save(self, path: str = "odin_state.json"):
        state = {
            "username": self.username,
            "private_secret": self.private_secret.hex(),
            "runway_start": self.runway_start,
            "runway_length": self.runway_length,
            "inbox": self.inbox,
            "sent": self.sent,
            "queue": self.queue,
            "suspect": self.suspect,
            "active_chains": self.active_chains,
            "last_checked_mask": self.last_checked_mask
        }
        with open(path, "w") as f:
            json.dump(state, f, indent=2)
        logger.info(f"State saved for {self.username}")

    @classmethod
    def load(cls, path: str = "odin_state.json") -> "UserState":
        try:
            with open(path) as f:
                state = json.load(f)
            user = cls(state["username"])
            user.private_secret = bytes.fromhex(state["private_secret"])
            user.runway_start = state.get("runway_start", user._compute_runway_start())
            user.runway_length = state.get("runway_length", DEFAULT_RUNWAY_LENGTH)
            user.inbox = state.get("inbox", [])
            user.sent = state.get("sent", [])
            user.queue = state.get("queue", [])
            user.suspect = state.get("suspect", [])
            user.active_chains = state.get("active_chains", {})
            user.last_checked_mask = state.get("last_checked_mask", user.runway_start)
            return user
        except FileNotFoundError:
            username = input("Enter username (e.g. bubba): ").strip()
            return cls(username)


class Message:
    """Unified message with AB temporal metadata, chaining, encryption."""
    def __init__(
        self,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
        mode: str = "async",  # "async" (chained) or "live" (temporal)
        delivery_date: Optional[str] = None,
        attachment_coord: Optional[Dict] = None,
        parent_code: Optional[int] = None,
        chain_id: Optional[str] = None,
        seq: int = 0,
    ):
        self.sender = sender
        self.recipient = recipient
        self.subject = subject
        self.body = body[:MAX_TEXT_LENGTH]
        self.mode = mode.lower()
        self.delivery_date = delivery_date
        self.attachment = attachment_coord
        self.parent_code = parent_code
        self.chain_id = chain_id
        self.seq = seq
        self.sent_date = datetime.now().isoformat()
        self.timestamp = int(time.time())
        self.status = "queued" if delivery_date else "sent"

    def serialize(self) -> bytes:
        data = {
            "from": self.sender,
            "to": self.recipient,
            "subject": self.subject,
            "body": self.body,
            "sent_date": self.sent_date,
            "delivery_date": self.delivery_date,
            "attachment": self.attachment,
            "parent_code": self.parent_code,
            "chain_id": self.chain_id,
            "seq": self.seq,
            "mode": self.mode,
            "timestamp": self.timestamp,
        }
        return json.dumps(data).encode()

    @classmethod
    def deserialize(cls, raw: bytes) -> "Message":
        data = json.loads(raw)
        return cls(
            sender=data["from"],
            recipient=data["to"],
            subject=data["subject"],
            body=data["body"],
            mode=data["mode"],
            delivery_date=data.get("delivery_date"),
            attachment_coord=data.get("attachment"),
            parent_code=data.get("parent_code"),
            chain_id=data.get("chain_id"),
            seq=data.get("seq", 0),
        )


def send_message(
    user: UserState,
    eye: OdinsEye,
    msg: Message,
    target_runway: Optional[Runway] = None,
    use_hub: bool = True,
) -> Dict[str, Any]:
    """Encode and drop message into runway (or hub)."""
    # Chaining / quantum code
    if msg.mode == "async":
        if not msg.chain_id:
            rng = BNSRNG(seed=f"{msg.sender}{msg.recipient}{msg.sent_date}")
            msg.chain_id = str(rng.next())
            msg.seq = 0
        else:
            msg.seq = user.active_chains.get(msg.chain_id, 0) + 1
        user.active_chains[msg.chain_id] = msg.seq

    # Encrypt
    key = Fernet(hashlib.sha256(user.private_secret).digest())
    cipher = Fernet(key)
    serialized = msg.serialize()
    encrypted = cipher.encrypt(serialized)

    # Prefix + length/hash wrapper
    recipient_prefix = msg.recipient.encode('utf-8')[:4]
    full_prefix = MAGIC_PREFIX + recipient_prefix
    prefixed = full_prefix + encrypted
    length_bytes = len(prefixed).to_bytes(4, 'big')
    hash_prefix = hashlib.sha256(prefixed).digest()[:4]
    full_payload = length_bytes + hash_prefix + prefixed

    coord = eye.encode(full_payload)

    # Choose runway
    runway = target_runway or get_odins_hall_runway() if use_hub else None
    if not runway:
        raise ValueError("No runway specified")

    # "Drop" (in real: broadcast to mesh)
    if msg.delivery_date and msg.delivery_date > datetime.now().isoformat():
        user.queue.append({"msg": msg.__dict__, "coord": coord})
        logger.info(f"Queued future msg to {msg.recipient} (chain {msg.chain_id})")
    else:
        user.sent.append({"msg": msg.__dict__, "coord": coord})
        logger.info(f"Sent msg to {msg.recipient} (chain {msg.chain_id}, seq {msg.seq})")

    user.save()
    return {"status": "dropped", "coord": coord, "runway": runway.name}


def poll_inbox(user: UserState, eye: OdinsEye, poller: RunwayPoller):
    """Enhanced polling: targeted chain prediction first, then full scan fallback."""
    discoveries = poller.poll_all(max_per_runway=20)  # from RunwayPoller
    key = Fernet(hashlib.sha256(user.private_secret).digest())
    cipher = Fernet(key)

    found = 0
    targeted_hits = 0
    full_scan_hits = 0

    # ─── Phase 1: Targeted polling for active chains (high priority) ───
    targeted_masks = set()
    for chain_id, last_seq in list(user.active_chains.items()):
        rng = BNSRNG(seed=chain_id)
        next_seq = last_seq + 1
        predicted_offset = rng.advance_to(next_seq) * POLL_STEP_SIZE
        predicted_mask = user.runway_start + (predicted_offset % user.runway_length)
        targeted_masks.add(predicted_mask)
        logger.debug(f"Targeted chain {chain_id} seq {next_seq} → mask {predicted_mask}")

    # Poll only targeted masks first (faster, lower CPU)
    for mask in targeted_masks:
        try:
            # Use dummy/sim fetch from poller or direct decode attempt
            # For simplicity: build a short coord probe
            coord_short = {
                "version": OdinsEye.VERSION,
                "start_mask": user.runway_start,
                "end_mask": mask,
                "anchor_mask": mask - 8,
                "last_choice": 0,
                "last_direction": 1,
                "length_bytes": 8
            }
            short_data = eye.decode(coord_short)
            if len(short_data) < 8:
                continue

            length_bytes = int.from_bytes(short_data[:4], 'big')
            hash_prefix = short_data[4:8]

            coord_full = coord_short.copy()
            coord_full["length_bytes"] = length_bytes + 8
            raw = eye.decode(coord_full)

            if len(raw) < 12:
                continue

            length_check = int.from_bytes(raw[:4], 'big')
            if length_check != len(raw) - 8:
                continue

            expected_hash = hashlib.sha256(raw[8:]).digest()[:4]
            if hash_prefix != expected_hash:
                continue

            if not raw.startswith(MAGIC_PREFIX):
                continue

            recipient_prefix = raw[4:8]
            expected_recipient = user.username.encode('utf-8')[:4]
            if recipient_prefix != expected_recipient:
                continue

            encrypted = raw[8 + 8:]  # skip length + hash + prefix
            payload = cipher.decrypt(encrypted)
            msg_data = json.loads(payload)

            if msg_data["to"] != user.username:
                continue

            # Validation layer (from Temporal)
            body_valid = is_human_readable_text(msg_data.get("body", ""))
            attach_valid = True
            if msg_data.get("attachment"):
                attach_data = eye.decode(msg_data["attachment"])
                m_type = infer_media_type(attach_data)
                attach_valid = validate_media(attach_data, m_type) if MEDIA_LIBS_AVAILABLE else False

            if body_valid and attach_valid:
                msg = Message.deserialize(payload)
                if msg.delivery_date and msg.delivery_date > datetime.now().isoformat():
                    user.queue.append({"msg": msg_data, "coord": coord_full})
                else:
                    user.inbox.append({"msg": msg_data, "coord": coord_full})
                    logger.info(f"Targeted delivery from {msg.sender}: {msg.subject} (chain {msg.chain_id})")
                found += 1
                targeted_hits += 1
                # Update chain tracking
                if msg.chain_id:
                    user.active_chains[msg.chain_id] = max(
                        user.active_chains.get(msg.chain_id, 0),
                        msg.seq
                    )
            else:
                msg_data["flag"] = "trash" if not attach_valid else "suspect"
                user.suspect.append({"msg": msg_data, "coord": coord_full})
                logger.info(f"Flagged {msg_data['flag']} (targeted): {msg_data['subject']}")

        except Exception as e:
            logger.debug(f"Targeted mask {mask} skipped: {e}")

    # ─── Phase 2: Full scan fallback (only if needed or periodic) ───
    # For now, always run a small batch after targeted
    runway_start = user.runway_start
    runway_end = runway_start + user.runway_length
    current = max(user.last_checked_mask, runway_start)
    batch_end = min(current + POLL_BATCH_SIZE // 4, runway_end)  # smaller batch after targeted

    for mask in range(current, batch_end, POLL_STEP_SIZE):
        # Same decode + validation logic as above (duplicated for simplicity)
        # ... (copy the try/except block from targeted phase, replace mask)
        # If found: full_scan_hits += 1; found += 1
        time.sleep(POLL_THROTTLE_SEC)

    user.last_checked_mask = batch_end
    user.save()

    logger.info(f"Poll complete – {found} new messages ({targeted_hits} targeted, {full_scan_hits} full scan)")
    return found



# Validation helpers (from Temporal)
def calculate_entropy(text: str) -> float:
    data = text.encode('utf-8', errors='ignore')
    if not data: return 0.0
    counts = [0] * 256
    for byte in data: counts[byte] += 1
    ent = 0.0
    total = len(data)
    for count in counts:
        if count > 0:
            p = count / total
            ent -= p * math.log(p, 2)
    return ent

def is_human_readable_text(body: str, threshold_ent=6.0) -> bool:
    ent = calculate_entropy(body)
    return ent <= threshold_ent  # Simplified; add dict check later if needed

def infer_media_type(data: bytes) -> str:
    if len(data) < 12: return 'unknown'
    if data.startswith(b'\xFF\xD8'): return 'image'
    if data.startswith(b'\x89PNG\r\n\x1A\n'): return 'image'
    if data[4:12] == b'ftyp': return 'video'
    if data.startswith(b'RIFF') and data[8:12] == b'WAVE': return 'audio'
    if data.startswith(b'ID3'): return 'audio'
    return 'unknown'

def validate_media(data: bytes, media_type: str) -> bool:
    if not MEDIA_LIBS_AVAILABLE: return False
    try:
        buf = BytesIO(data)
        if media_type == 'image':
            Image.open(buf).verify()
            return True
        elif media_type == 'video':
            clip = mp.VideoFileClip(buf)
            return clip.duration > 0
        elif media_type == 'audio':
            seg = AudioSegment.from_file(buf)
            return len(seg) > 0
    except:
        return False
    return False


# CLI / Background (simplified)
def start_polling(user: UserState, eye: OdinsEye):
    poller = create_default_poller()  # includes Odins Hall + user runway
    user.polling = True

    def poll_loop():
        while user.polling:
            poll_inbox(user, eye, poller)
            time.sleep(POLL_INTERVAL_SEC)

    threading.Thread(target=poll_loop, daemon=True).start()


if __name__ == "__main__":
    user = UserState.load()
    eye = OdinsEye()
    poller = create_default_poller()  # includes Odins Hall + user's private runway

    print(f"Welcome {user.username} – runway {user.runway_start} → {user.runway_start + user.runway_length}")
    print("Commands: compose, inbox, sent, queue, suspect, poll, quit")

    start_polling(user, eye)  # background polling thread

    while True:
        cmd = input("\n> ").strip().lower()

        if cmd == "compose":
            to = input("To: ").strip()
            subject = input("Subject: ").strip()
            body = input("Body (multi-line ok, end with empty line):\n")
            while True:
                line = input()
                if not line:
                    break
                body += "\n" + line

            mode = input("Mode (async/live) [async]: ").strip() or "async"
            delivery = input("Delivery date (YYYY-MM-DD HH:MM) or empty: ").strip() or None

            msg = Message(
                sender=user.username,
                recipient=to,
                subject=subject,
                body=body,
                mode=mode,
                delivery_date=delivery,
            )

            result = send_message(user, eye, msg)
            print(f"Sent! Coord: {result['coord']}")
            print(f"Dropped into: {result['runway']}")

        elif cmd == "inbox":
            if not user.inbox:
                print("Inbox empty")
            for item in user.inbox:
                m = item["msg"]
                print(f"From {m['from']} | {m['subject']} | {m['sent_date']}")

        elif cmd == "sent":
            if not user.sent:
                print("No sent messages")
            for item in user.sent:
                m = item["msg"]
                print(f"To {m['to']} | {m['subject']} | {m['sent_date']}")

        elif cmd == "queue":
            if not user.queue:
                print("Queue empty")
            for item in user.queue:
                m = item["msg"]
                print(f"To {m['to']} | {m['subject']} | Delivery: {m['delivery_date']}")

        elif cmd == "suspect":
            if not user.suspect:
                print("No flagged messages")
            for item in user.suspect:
                m = item["msg"]
                print(f"Flagged {m.get('flag')} from {m['from']} | {m['subject']}")

        elif cmd == "poll":
            count = poll_inbox(user, eye, poller)
            print(f"Poll complete – {count} new messages found")

        elif cmd == "quit":
            user.polling = False
            user.save()
            print("Goodbye!")
            break

        else:
            print("Unknown command")
            
        elif cmd.startswith("reply "):
            subject = cmd[6:].strip()  # everything after "reply "
            if not subject:
                print("Usage: reply <subject of original message>")
                continue

            # Find the original message in inbox by subject (case-insensitive partial match)
            original = None
            for item in user.inbox:
                m = item["msg"]
                if subject.lower() in m["subject"].lower():
                    original = m
                    break

            if not original:
                print(f"No message found with subject containing '{subject}'")
                continue

            print(f"Replying to: {original['subject']} from {original['from']}")

            body = input("Reply body (multi-line, end with empty line):\n")
            while True:
                line = input()
                if not line:
                    break
                body += "\n" + line

            # Auto-chain: use same chain_id, increment seq
            chain_id = original.get("chain_id")
            seq = original.get("seq", 0) + 1

            # Optional: use BNSRNG for next "quantum" prediction (as in Temporal)
            if chain_id:
                rng = BNSRNG(seed=chain_id)
                predicted_offset = rng.advance_to(seq) * POLL_STEP_SIZE
                print(f"Chained prediction offset: {predicted_offset} (for targeted polling)")

            reply_msg = Message(
                sender=user.username,
                recipient=original["from"],
                subject=f"Re: {original['subject']}",
                body=body,
                mode=original.get("mode", "async"),
                chain_id=chain_id,
                seq=seq,
            )

            result = send_message(user, eye, reply_msg)
            print(f"Reply sent! Coord: {result['coord']}")
            print(f"Dropped into: {result['runway']}")
        # Add compose/send later
