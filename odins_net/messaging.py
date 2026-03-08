# Odins_net/messaging.py
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

        self.subscribed_boards = set(["Odins-Hall", f"{username}-private"])  # default subs

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

    
    
    def subscribe_board(self, board_name: str):
        self.subscribed_boards.add(board_name)
        self.save()
        logger.info(f"Subscribed to board: {board_name}")

    def unsubscribe_board(self, board_name: str):
        if board_name in self.subscribed_boards:
            self.subscribed_boards.remove(board_name)
            self.save()
            logger.info(f"Unsubscribed from board: {board_name}")
            
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

    # @classmethod
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

def get_encryption_key(secret: bytes) -> bytes:
    return Fernet(hashlib.sha256(secret).digest())
    
def poll_inbox(user: UserState, eye: OdinsEye, poller: RunwayPoller):
    logger.info(f"Polling for {user.username}...")

    discoveries = poller.poll_all(max_per_runway=50)  # adjust as needed for volume
    key = Fernet(hashlib.sha256(user.private_secret).digest())
    cipher = Fernet(key)

    found = 0
    targeted_hits = 0
    full_scan_hits = 0

    for runway_name, items in discoveries.items():
        for item in items:
            coord = item["coord"]
            try:
                raw = eye.decode(coord)
                if len(raw) < 12:
                    continue

                length_bytes = int.from_bytes(raw[:4], 'big')
                hash_prefix = raw[4:8]
                prefixed = raw[8:]

                if len(prefixed) != length_bytes:
                    continue

                expected_hash = hashlib.sha256(prefixed).digest()[:4]
                if hash_prefix != expected_hash:
                    continue

                if not prefixed.startswith(MAGIC_PREFIX):
                    continue

                recipient_prefix = prefixed[4:8]
                expected_recipient = user.username.encode('utf-8')[:4]
                if recipient_prefix != expected_recipient:
                    continue

                encrypted = prefixed[8:]
                payload = cipher.decrypt(encrypted)
                msg_data = json.loads(payload)

                if msg_data["to"] != user.username:
                    continue

                # Validation + readability
                body_valid = is_human_readable_text(msg_data.get("body", ""))
                attach_valid = True
                if msg_data.get("attachment"):
                    attach_data = eye.decode(msg_data["attachment"])
                    m_type = infer_media_type(attach_data)
                    attach_valid = validate_media(attach_data, m_type) if MEDIA_LIBS_AVAILABLE else False

                if body_valid and attach_valid:
                    msg = Message.deserialize(payload)
                    flags = get_message_flags(msg.__dict__)
                    if msg.delivery_date and msg.delivery_date > datetime.now().isoformat():
                        user.queue.append({"msg": msg.__dict__, "coord": coord})
                        logger.info(f"Queued future: {msg.subject} {flags}")
                    else:
                        user.inbox.append({"msg": msg.__dict__, "coord": coord})
                        logger.info(f"Delivered: {msg.subject} {flags}")
                    found += 1
                    # Targeted vs full scan (for logging)
                    targeted_hits += 1  # simulate - in real, check if mask in targeted set
                else:
                    msg_data["flag"] = "trash" if not attach_valid else "suspect"
                    user.suspect.append({"msg": msg_data, "coord": coord})
                    logger.info(f"Flagged {msg_data['flag']}: {msg_data['subject']}")

            except Exception as e:
                logger.debug(f"Invalid coord skipped: {e}")

    user.save()
    logger.info(f"Poll complete – {found} new messages ({targeted_hits} targeted)")
    return found

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

def read_board(user: UserState, eye: OdinsEye, board_name: str):
    PAGE_SIZE = 20  # threads or messages per page

    print(f"\n{BOLD}Board: {board_name}{RESET}")
    print("-" * 60)

    # Group messages by chain_id for thread view
    threads = {}
    for item in user.inbox:
        msg = item["msg"]
        chain_id = msg.get("chain_id", f"single-{hash(msg['subject'] + msg['sent_date']):x}"[:12])
        seq = msg.get("seq", 0)
        if chain_id not in threads:
            threads[chain_id] = []
        threads[chain_id].append((seq, msg, item["coord"]))

    if not threads:
        print("No threads or messages on this board yet.")
        input("Press Enter to continue...")
        return

    # Sort threads by latest message seq
    thread_list = sorted(threads.items(), key=lambda x: max(m[0] for m in x[1]), reverse=True)

    page = 0
    total_pages = (len(thread_list) // PAGE_SIZE) + (1 if len(thread_list) % PAGE_SIZE else 0)

    while True:
        start = page * PAGE_SIZE
        end = start + PAGE_SIZE
        page_threads = thread_list[start:end]
  print(f"\nThreads (page {page+1} of {total_pages}):")
for i, (chain_id, messages) in enumerate(page_threads, start + 1):
    messages.sort(key=lambda x: x[0])
    first_msg = messages[0][1]
    flags = get_message_flags(first_msg)
    seq = messages[0][0]  # seq of first message
    subject_display = "Re: " * (seq - 1) + first_msg['subject'] if seq > 0 else first_msg['subject']
    print(f"  {i:2}. {flags} [{chain_id}] {subject_display} ({len(messages)} messages)")
    print(f"     Started by {first_msg['from']} ({first_msg['sent_date']})")
    print(f"     Latest: {messages[-1][1]['from']} ({messages[-1][1]['sent_date']})")
    
    # Simple nesting: show replies with arrows
    if len(messages) > 1:
        print("     Replies:")
        for msg_tuple in messages[1:]:
            msg_seq, msg, _ = msg_tuple
            reply_prefix = "  " * (msg_seq - 1) + "→ "
            print(f"       {reply_prefix}{msg['from']}: {msg['subject'][:50]}{'...' if len(msg['subject']) > 50 else ''}")
  
        if sub_cmd in ["n", "next"]:
            page += 1
            if page >= total_pages:
                page = total_pages - 1
            continue
        elif sub_cmd in ["p", "previous"]:
            page = max(0, page - 1)
            continue
        elif sub_cmd == "q":
            break
        elif sub_cmd.startswith("r "):
            try:
                thread_num = int(sub_cmd.split()[1]) - 1
                real_idx = start + thread_num
                chain_id, messages = thread_list[real_idx]
                messages.sort(key=lambda x: x[0])
                parent_msg = messages[-1][1]
                parent_coord = messages[-1][2]

                print(f"Replying to thread [{chain_id}] - {parent_msg['subject']}")
                print(f"From: {parent_msg['from']} ({parent_msg['sent_date']})")
                print(f"Body preview: {parent_msg['body'][:100]}{'...' if len(parent_msg['body']) > 100 else ''}")

                body = ""
                print("Reply body (multi-line, end with empty line):")
                while True:
                    line = input("Press Enter...")
                    if not line and body:
                        break
                    body += line + "\n"

                next_seq = max(m[0] for m in messages) + 1
                rng = BNSRNG(seed=chain_id)
                predicted_offset = rng.advance_to(next_seq) * POLL_STEP_SIZE
                logger.info(f"Chained prediction offset for {chain_id}: {predicted_offset}")

                reply_msg = Message(
                    sender=user.username,
                    recipient=parent_msg["from"],
                    subject=f"Re: {parent_msg['subject']}",
                    body=body,
                    mode=parent_msg.get("mode", "async"),
                    chain_id=chain_id,
                    seq=next_seq,
                )

                result = send_message(user, eye, reply_msg)
                print(f"Reply sent! Coord: {result['coord']}")
                print(f"Dropped into: {result['runway']}")
            except Exception as e:
                print(f"Error: {e}")
        elif sub_cmd == "p" and "poll" in sub_cmd.lower():
            count = poll_inbox(user, eye, poller)
            print(f"Board poll complete – {count} new messages")
#        elif sub_cmd.isdigit():
#           # Future: read single thread full
#            print("Full thread reading – coming soon")
        else:
            print("Invalid command. Use R #, N, P, Q, or poll")

        input("Press Enter to continue...")
    
def get_dynamic_boards(user: UserState):
 #   all_boards = []

    # Fixed public
    all_boards.append(("Odins-Hall", "Public hub & announcements", 10000, 10099))

    # Private
    private_name = f"{user.username}-private"
    all_boards.append((private_name, "Personal mailbox & chains", 
                       user.runway_start, user.runway_start + user.runway_length))

    # Active chains
    for chain_id in sorted(user.active_chains.keys()):
        seq = user.active_chains[chain_id]
        chain_name = f"Chain-{chain_id[:8]}"
        all_boards.append((chain_name, f"Active conversation thread (last seq {seq})", 0, 0))

    # Filter to subscribed only
    subscribed = []
    counter = 1
    for name, desc, start, end in all_boards:
        if name in user.subscribed_boards:
            subscribed.append((str(counter), name, desc, start, end))
            counter += 1

    # Add "All Boards" option at end
    subscribed.append(("A", "All Boards", "Show every known board/runway", 0, 0))

    return subscribed
    
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

    start_polling(user, eye)

    while True:
        print("\n" + "-" * 60)
        print(f"{BOLD}TIBBS Main Menu{RESET}")
        print("-" * 60)
        print(f"User: {user.username}   Position: {user.runway_start}–{user.runway_start + user.runway_length}")
        print(f"Last poll: {datetime.now().strftime('%Y-%m-%d %H:%M')}   Unread: {len(user.inbox)}")

        boards = get_dynamic_boards(user)
        print("\nBoards (runways):")
        for num, name, desc, start, end in boards:
            unread = " (NEW)" if name == "Odins-Hall" and len(user.inbox) > 0 else ""
            print(f"  {num}. {name:<15} {desc} ({start}–{end}){unread}")

        print("\nOther:")
        print("  7. Poll Now")
        print("  8. Compose / Post")
        print("  9. Active Chains / Reply")
        print(" 10. Queue (future delivery)")
        print(" 11. Suspect / Flagged")
        print(" 12. The Thing (disputes)")
        print("  S  Subscribe to boards")
        print("  U  Unsubscribe from boards")
        print("  ?  Help")
        print("  Q  Quit")

        choice = input("\nEnter choice (1+ for boards, 7=poll, 8=compose, 9=chains, 10=queue, 11=suspect, 12=thing, S=subscribe, U=unsubscribe, ?=help, Q=quit): ").strip().lower()


        if choice in ["q", "quit"]:
            user.polling = False
            user.save()
            print("Goodbye, traveler.")
            break

        elif choice in ["?", "help"]:
            print("""
TIBBS Commands:
  1+: Enter board (dynamic numbers)
  7: Poll now
  8: Compose new message/video
  9: Active chains & reply
 10: Queue
 11: Suspect
 12: The Thing
  S: Subscribe
  U: Unsubscribe
  ?: Help
  Q: Quit
""")
            input("Press Enter...")

        elif choice == "7":
            count = poll_inbox(user, eye, poller)
            print(f"Poll complete – {count} new messages")
            input("Press Enter...")

        elif choice == "8":
            # Polished compose
            print("\nCompose / Post")
            print("1. Text post")
            print("2. Video chunk")
            print("3. Private")
# ---------------------------------------------------------------------------------------------
# Added by Lloyd in homework
sub = input("Choose mode (1=Text post, 2=Video chunk, 3=Private) [1]: ").strip() or "1"
     to = input("Recipient username (leave blank for public/board post): ").strip()
     subject = input("Subject line: ").strip()
     print("Enter body text (multi-line, press Enter on blank line to finish):")
     while True:
         line = input()
         if not line and lines:
             break
         lines.append(line)
     path = input("Full path to video file (mp4/webm) or blank to skip: ").strip()
     delivery = input("Future delivery date/time (YYYY-MM-DD HH:MM) or blank for immediate: ").strip() or None
     pick = input("Select board number (or Enter for Odins-Hall): ").strip()

   - After every action (poll, compose, reply, S/U): keep or add:
     input("Press Enter to return to main menu...")

# ---------------------------------------------------------------------------------------------
         

            to = input("To (blank for public): ").strip()
            subject = input("Subject: ").strip()

            body = ""
            if sub == "2":
#-----------------------------------------------------------------------------------
#added by Lloyd
           if path:
           print(f"Simulating chunking {path} into 10-second segments...")
           # Placeholder: in real code, use moviepy to split into list of bytes
           chunk_count = 3  # simulate 3 chunks
           print(f"Generated {chunk_count} chunks")
           for chunk_num in range(1, chunk_count + 1):
               msg = Message(user.username, to or "public", f"{subject} - Chunk {chunk_num}/{chunk_count}", "[VIDEO CHUNK]", mode, delivery)
               result = send_message(user, eye, msg, runway)
               print(f"  Chunk {chunk_num} sent! Coord: {result['coord']}")
           print("Video chain complete! Poll to receive chunks.")
       else:
           print("No video path – skipping chunking.")

#------------------------------------------------------------------------------------
                path = input("Video path: ").strip()
                if path:
                    body = "[VIDEO CHAIN - chunks queued]"
                    print("Chunking simulated...")
                else:
                    sub = "1"
            else:
                print("Body (end with empty line):")
                lines = []
                while True:
                    line = input("Press Enter...")
                    if not line and lines:
                        break
                    lines.append(line)
                body = "\n".join(lines)

            mode = input("Mode [async]: ").strip() or "async"
            delivery = input("Delivery date or empty: ").strip() or None

            msg = Message(user.username, to or "public", subject, body, mode, delivery)

            runway = None
            if sub in ["1", "2"]:
                print("\nBoards:")
                for n, nm, d, s, e in boards:
                    print(f"  {n}. {nm}")
                pick = input("Board # or Enter for Odins-Hall: ").strip()
                if pick.isdigit():
                    idx = int(pick) - 1
                    runway = Runway(boards[idx][3], boards[idx][4], boards[idx][1])
                else:
                    runway = get_odins_hall_runway()

            result = send_message(user, eye, msg, runway)
            print(f"Sent! Coord: {result['coord']}")
            input("Press Enter...")

#        elif choice == "9":
#        print("Active chains / reply – coming soon")
#        input("Press Enter...")

        elif choice == "10":
            if not user.queue:
                print("Queue is empty.")
            else:
                print("\nQueued future messages:")
                for i, item in enumerate(user.queue, 1):
                    m = item["msg"]
                    print(f"  {i}. {m['subject']} to {m['recipient']} (deliver: {m['delivery_date']})")
            input("Press Enter...")

        elif choice == "11":
            if not user.suspect:
                print("No flagged messages.")
            else:
                print("\nSuspect / Flagged:")
                for i, item in enumerate(user.suspect, 1):
                    m = item["msg"]
                    print(f"  {i}. [FLAGGED {m.get('flag','?')}] {m['subject']} from {m['from']}")
            input("Press Enter...")

#        elif choice == "12":
#        print("The Thing (disputes) – coming soon")
#        Input("Press Enter...")


     elif choice.upper() == "S":
            boards = get_dynamic_boards(user)
            print("\n" + "="*60)
            print(f"{BOLD}Subscribe to Boards{RESET}")
            print("="*60)
            print(f"Currently subscribed: {len(user.subscribed_boards)}")
            print("\nAvailable boards (all known):")
            for num, name, desc, start, end in boards[:-1]:
                status = " (subscribed)" if name in user.subscribed_boards else ""
                print(f"  {num}. {name:<20} {status} ({desc})")
            pick = input("\nEnter number to subscribe, A for All, or Enter to cancel: ").strip().upper()
            if pick == "A":
                for _, name, _, _, _ in boards[:-1]:
                    if name not in user.subscribed_boards:
                        user.subscribe_board(name)
                print("Subscribed to all available boards.")
            elif pick.isdigit():
                idx = int(pick) - 1
                if 0 <= idx < len(boards) - 1:
                    board_name=boards[idx][1]
            elif choice.upper() == "A":
         print("Showing all boards (subscription filter off)")
         # Optional: reload menu or set flag to show all boards temporarily
         # For now: simple print
         print("All Boards mode – coming soon (full implementation in v0.2)")
     input("Press Enter to continue...")


     if board_name in user.subscribed_boards:
                        print(f"Already subscribed to {board_name}.")
                    else:
                        user.subscribe_board(board_name)
                        print(f"Subscribed to {board_name}")
                else:
                    print("Invalid number.")
            else:
                print("Cancelled.")
            input("Press Enter to return to menu...")

        elif choice.upper() == "U":
            boards = get_dynamic_boards(user)
            print("\n" + "="*60)
            print(f"{BOLD}Unsubscribe from Boards{RESET}")
            print("="*60)
            print(f"Currently subscribed: {len(user.subscribed_boards)}")
            subscribed_list = [b for b in boards[:-1] if b[1] in user.subscribed_boards]
            if not subscribed_list:
                print("No subscriptions to unsubscribe from.")
            else:
                for i, (_, name, desc, _, _) in enumerate(subscribed_list, 1):
                    print(f"  {i}. {name:<20} ({desc})")
                pick = input("\nEnter number to unsubscribe, or Enter to cancel: ").strip()
                if pick.isdigit():
                    idx = int(pick) - 1
                    if 0 <= idx < len(subscribed_list):
                        board_name = subscribed_list[idx][1]
                        user.unsubscribe_board(board_name)
                        print(f"Unsubscribed from {board_name}")
                    else:
                        print("Invalid number.")
                else:
                    print("Cancelled.")
            input("Press Enter to return to menu...")
 


