# odins_net/messaging.py
# Unified Odins Mail + Odins Temporal – Async chained & live temporal messaging
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
from .rng import BNSRNG
from .runway import Runway, RunwayPoller
from .nexus_hub import get_odins_hall_runway, create_default_poller
from cryptography.fernet import Fernet

# Optional media validation
try:
    from PIL import Image
    import moviepy.editor as mp
    from pydub import AudioSegment
    MEDIA_LIBS_AVAILABLE = True
except ImportError:
    MEDIA_LIBS_AVAILABLE = False

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("OdinsMessaging")

# Globals
MAGIC_PREFIX = b"AB42"
POLL_INTERVAL_SEC = 60
POLL_BATCH_SIZE = 1000
POLL_STEP_SIZE = 5
POLL_THROTTLE_SEC = 0.05
MAX_TEXT_LENGTH = 64 * 1024
DEFAULT_RUNWAY_LENGTH = 10000

GREEN = "\033[92m"
RESET = "\033[0m"
BOLD = "\033[1m"

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

def get_message_flags(msg_data: Dict) -> str:
    flags = []
    if not msg_data.get("secret"):
        flags.append("[UNSEC]")
    if msg_data.get("delivery_date") and msg_data["delivery_date"] > datetime.now().isoformat():
        flags.append("[FUTURE]")
    if msg_data.get("timestamp") and abs(msg_data["timestamp"] - int(time.time())) > 86400 * 30:
        if msg_data["timestamp"] < time.time():
            flags.append("[PAST]")
        else:
            flags.append("[FUTURE]")
    return " ".join(flags) if flags else ""


class UserState:
    def __init__(self, username: str):
        self.username = username
        self.private_secret = secrets.token_bytes(32)
        self.runway_start = self._compute_runway_start()
        self.runway_length = DEFAULT_RUNWAY_LENGTH
        self.inbox: List[Dict] = []
        self.sent: List[Dict] = []
        self.queue: List[Dict] = []
        self.suspect: List[Dict] = []
        self.active_chains: Dict[str, int] = {}
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
    def __init__(
        self,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
        mode: str = "async",
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


def send_message(user: UserState, eye: OdinsEye, msg: Message, target_runway: Optional[Runway] = None, use_hub: bool = True) -> Dict[str, Any]:
    if msg.mode == "async":
        if not msg.chain_id:
            rng = BNSRNG(seed=f"{msg.sender}{msg.recipient}{msg.sent_date}")
            msg.chain_id = str(rng.next())
            msg.seq = 0
        else:
            msg.seq = user.active_chains.get(msg.chain_id, 0) + 1
        user.active_chains[msg.chain_id] = msg.seq

    key = Fernet(hashlib.sha256(user.private_secret).digest())
    cipher = Fernet(key)
    serialized = msg.serialize()
    encrypted = cipher.encrypt(serialized)

    recipient_prefix = msg.recipient.encode('utf-8')[:4]
    full_prefix = MAGIC_PREFIX + recipient_prefix
    prefixed = full_prefix + encrypted
    length_bytes = len(prefixed).to_bytes(4, 'big')
    hash_prefix = hashlib.sha256(prefixed).digest()[:4]
    full_payload = length_bytes + hash_prefix + prefixed

    coord = eye.encode(full_payload)

    runway = target_runway or get_odins_hall_runway() if use_hub else None
    if not runway:
        raise ValueError("No runway specified")

    if msg.delivery_date and msg.delivery_date > datetime.now().isoformat():
        user.queue.append({"msg": msg.__dict__, "coord": coord})
        logger.info(f"Queued future msg to {msg.recipient} (chain {msg.chain_id})")
    else:
        user.sent.append({"msg": msg.__dict__, "coord": coord})
        logger.info(f"Sent msg to {msg.recipient} (chain {msg.chain_id}, seq {msg.seq})")

    user.save()
    return {"status": "dropped", "coord": coord, "runway": runway.name}

def poll_inbox(user: UserState, eye: OdinsEye):
    runway_start = user.runway_start
    runway_end = runway_start + user.runway_length

    print(f"Polling {user.username}@odin runway: {runway_start} â†’ {runway_end}")

    found_count = 0

    # â”€â”€â”€ Targeted polling for active chains first (real-time priority) â”€â”€â”€
    targeted_masks = []
    for chain_id, last_seq in list(user.active_chains.items()):
        rng = BNSRNG(seed=chain_id)
        next_seq = last_seq + 1
        predicted_offset = rng.advance_to(next_seq) * POLL_STEP_SIZE
        predicted_mask = runway_start + (predicted_offset % user.runway_length)
        targeted_masks.append(predicted_mask)

    for mask in set(targeted_masks):
        try:
            coord_short = {
                "version": "0.1.1",
                "start_mask": runway_start,
                "end_mask": mask,
                "anchor_mask": mask - 8,
                "last_choice": 0,
                "last_direction": 1,
                "length_bytes": 8
            }
            short_data = eye.decode(coord_short)
            if len(short_data) < 8: continue

            length_bytes = int.from_bytes(short_data[:4], 'big')
            hash_prefix = short_data[4:8]

            coord_full = coord_short.copy()
            coord_full["length_bytes"] = length_bytes + 8
            data = eye.decode(coord_full)

            expected_prefix = MAGIC_PREFIX + user.username.encode('utf-8')[:4]
            prefix_len = len(expected_prefix)

            if data.startswith(expected_prefix):
                after_prefix = data[prefix_len:]
                if len(after_prefix) < 8: continue
                computed_hash = hashlib.sha256(after_prefix).digest()[:4]
                if computed_hash == hash_prefix:
                    encrypted = after_prefix
                    key = get_encryption_key(user.private_secret)
                    cipher = Fernet(key)
                    try:
                        payload = cipher.decrypt(encrypted)
                        msg = json.loads(payload)

                        if msg["to"] == user.username:
                            expected_code = bns_code_date_time(msg["sent_date"])
                            if msg["quantum_code"] == expected_code:
                                # â”€â”€â”€ VALIDATION LAYER â”€â”€â”€
                                body_valid = is_human_readable_text(msg.get("body", ""))
                                attach_valid = True
                                attach_hash = None
                                if msg.get("attachment"):
                                    attach_data = eye.decode(msg["attachment"])
                                    m_type = infer_media_type(attach_data)
                                    attach_valid = validate_media(attach_data, m_type)
                                    attach_hash = hashlib.sha256(attach_data).hexdigest()

                                if body_valid and attach_valid:
                                    # Valid message
                                    if msg.get("delivery_date") and msg["delivery_date"] > datetime.now().isoformat():
                                        user.queue.append({"msg": msg, "coord": coord_full})
                                        print(f"Queued future message from {msg['from']}: {msg['subject']}")
                                    else:
                                        user.inbox.append({"msg": msg, "coord": coord_full})
                                        print(f"Delivered message from {msg['from']}: {msg['subject']} (chain {msg.get('chain_id')})")
                                    found_count += 1
                                    # Update chain tracking
                                    if "chain_id" in msg:
                                        user.active_chains[msg["chain_id"]] = max(
                                            user.active_chains.get(msg["chain_id"], 0),
                                            msg.get("seq", 0)
                                        )
                                else:
                                    # Flagged
                                    msg["flag"] = "trash" if not attach_valid else "suspect"
                                    msg["reason"] = f"body_valid:{body_valid} attach_valid:{attach_valid}"
                                    if attach_hash:
                                        msg["attach_hash"] = attach_hash
                                    user.suspect.append({"msg": msg, "coord": coord_full})
                                    print(f"Flagged {msg['flag']}: {msg['subject']} ({msg['reason']})")
                    except Exception as e:
                        print(f"Decrypt failed: {e}")
        except Exception:
            pass

        time.sleep(POLL_THROTTLE_SEC)

    # â”€â”€â”€ Regular brute-force poll (fallback) â”€â”€â”€
    current = max(user.last_checked_mask, runway_start)
    batch_end = min(current + POLL_BATCH_SIZE, runway_end)

    for mask in range(current, batch_end, POLL_STEP_SIZE):
        # Same decode logic as above â€“ omitted for brevity, copy from your original or duplicate the block
        # (to keep this full file self-contained, you can paste your original range loop here)
        time.sleep(POLL_THROTTLE_SEC)

    user.last_checked_mask = batch_end
    user.save()

    print(f"Poll cycle complete â€“ {found_count} new valid messages")

def show_inbox(user: UserState):
    if not user.inbox:
        print("Inbox is empty.")
        return
    print("\nInbox:")
    for i, item in enumerate(user.inbox, 1):
        msg = item["msg"]
        flags = get_message_flags(msg)
        print(f"{i:2}. {flags} {msg['subject']} from {msg['from']} ({msg['sent_date']})")
        body_preview = msg['body'][:60] + "..." if len(msg['body']) > 60 else msg['body']
        print(f"   {body_preview}")


def start_polling(user: UserState, eye: OdinsEye):
    poller = create_default_poller()
    user.polling = True

    def poll_loop():
        while user.polling:
            poll_inbox(user, eye, poller)
            time.sleep(POLL_INTERVAL_SEC)

    threading.Thread(target=poll_loop, daemon=True).start()

if __name__ == "__main__":
    user = UserState.load()
    eye = OdinsEye()
    poller = create_default_poller()

    print(BANNER)
    print(f"{GREEN}{BOLD}Welcome to the Temporal Intergalactic BBS{RESET}")
    print(f"Logged in as: {user.username}")
    print(f"Runway: {user.runway_start}–{user.runway_start + user.runway_length}")
    print("Type ? for help at any prompt | Q to quit\n")

    start_polling(user, eye)  # background thread

    while True:
        print("\n" + "-" * 60)
        print(f"{BOLD}TIBBS Main Menu{RESET}")
        print("-" * 60)
        print(f"User: {user.username}   Position: {user.runway_start}–{user.runway_start + user.runway_length}")
        print(f"Last poll: {datetime.now().strftime('%Y-%m-%d %H:%M')}   Unread: {len(user.inbox)}")

        print("\nBoards (runways):")
        boards = [
            ("1", "Odins-Hall", "Public hub & announcements", 10000, 10099),
            ("2", "bubba-private", "Your personal mailbox & chains", user.runway_start, user.runway_start + user.runway_length),
            # Add dynamic boards later from discovered runways
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

        if choice in ["q", "quit"]:
            user.polling = False
            user.save()
            print("Goodbye, traveler.")
            break

        elif choice in ["?", "help"]:
            print("""
TIBBS Commands:
  1-6: Enter a board (poll & read threads)
  7: Poll now (check for new messages)
  8: Compose new message/post
  9: View active chains
 10: View queued future messages
 11: View flagged suspect messages
 12: View active Things (disputes)
  ?: Show this help
  Q: Quit & save
""")
            input("Press Enter to continue...")

        elif choice == "7":
            count = poll_inbox(user, eye, poller)
            print(f"Poll complete – {count} new messages found")
            input("Press Enter to continue...")

        elif choice == "8":
            print("Compose mode – coming in next chunk")
            # We'll add compose next

        elif choice in ["1", "2"]:
            board_idx = int(choice) - 1
            board_name = boards[board_idx][1]
            read_board(user, eye, board_name)

        else:
            print("Invalid choice. Type ? for help.")

