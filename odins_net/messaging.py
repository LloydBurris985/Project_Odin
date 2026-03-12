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
from .runway import Runway
from .runwaypoller import RunwayPoller
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
        return int.from_bytes(h, 'big') % (2**32)  # simple 32-bit runway start

def get_dynamic_boards(user: UserState):
    # Placeholder – replace with your actual logic
    return [
        (1, "Odins-Hall", "Public hub & announcements", 10000, 10099),
        (2, f"{user.username}-private", "Your personal mailbox & chains", user.runway_start, user.runway_start + user.runway_length)
    ]

def poll_inbox(user: UserState, eye: OdinsEye, poller):
    # Placeholder – replace with your actual polling logic
    print("Polling inbox...")
    return 0  # number of new messages

def read_board(user: UserState, eye: OdinsEye, board_name: str):
    print(f"\nBoard: {board_name}")
    print("Threads loading... (placeholder)")
    input("Press Enter to return...")

def send_message(user: UserState, eye: OdinsEye, msg, runway=None):
    # Placeholder – replace with your actual send logic
    print("Message sent (placeholder)")
    return {"coord": "placeholder_coord", "runway": runway or "unknown"}

if __name__ == "__main__":
    user = UserState("bubba")  # change to load() later
    eye = OdinsEye()
    poller = create_default_poller()

    print(BANNER)
    print(f"{GREEN}{BOLD}Welcome to the Temporal Intergalactic BBS{RESET}")
    print(f"Logged in as: {user.username}")
    print(f"Runway: {user.runway_start}–{user.runway_start + user.runway_length}")
    print("Type ? for help at any prompt | Q to quit\n")

    # start_polling(user, eye)  # uncomment when ready

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
            print("Polling... (placeholder)")
            input("Press Enter...")

        elif choice == "8":
            print("Compose / Post (placeholder)")
            input("Press Enter...")

        elif choice == "9":
            print("Active chains / reply – coming soon")
            input("Press Enter...")

        elif choice.isdigit():
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(boards):
                    name = boards[idx][1]
                    read_board(user, eye, name)
                elif choice.upper() == "A":
                    print("Showing all boards (subscription filter off)")
                    print("All Boards mode – coming soon (full implementation in v0.2)")
                else:
                    print("Invalid board number")
            except:
                print("Error entering board")
            input("Press Enter...")

        elif choice.upper() == "S":
            print("Subscribe to boards (placeholder)")
            input("Press Enter...")

        elif choice.upper() == "U":
            print("Unsubscribe from boards (placeholder)")
            input("Press Enter...")

        else:
            print("Invalid choice. Type ? for help.")
            input("Press Enter...")
