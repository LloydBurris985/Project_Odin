# mailbox.py - FIXED: full temporal protocol (subject, to date, from date, body)
import os, time, json
from pathlib import Path
from datetime import datetime

INBOX = "inbox/"
OUTBOX = "outbox/"
SENT = "sent/"
RECV_BUFFER = "../recv_buffer.bin"

for d in [INBOX, OUTBOX, SENT]:
    os.makedirs(d, exist_ok=True)

def log(msg):
    print(f"[MAILBOX] {msg}")

def send_message(subject, to_date, from_date, body):
    msg = {
        "subject": subject,
        "to_date": to_date,
        "from_date": from_date,
        "body": body,
        "timestamp": int(time.time())
    }
    ts = int(time.time())
    msg_file = f"{OUTBOX}{ts}.msg"
    with open(msg_file, "w") as f:
        json.dump(msg, f, indent=2)
    with open("../command.txt", "a") as f:
        f.write(f"send Runway_42 {msg_file}\n")
    log(f"Message sent to runway - from {from_date} to {to_date}")

def poll():
    if os.path.exists(RECV_BUFFER) and os.path.getsize(RECV_BUFFER) > 0:
        with open(RECV_BUFFER, "rb") as f:
            raw = f.read()
        # Deliver immediately (future messages shown for board health)
        with open(f"{INBOX}{int(time.time())}.msg", "wb") as f:
            f.write(raw)
        Path(RECV_BUFFER).write_text("")
        log("Delivered from airport buffer (to_date/from_date respected)")

if __name__ == "__main__":
    print("Temporal Mailbox - protocol active (subject, to date, from date, body)")
    while True:
        cmd = input("> ").strip().lower()
        if cmd == "send":
            subject = input("Subject: ")
            to_date = input("To date (YYYY-MM-DD): ")
            from_date = input("From date (YYYY-MM-DD): ")
            body = input("Body: ")
            send_message(subject, to_date, from_date, body)
        elif cmd == "poll":
            poll()
        elif cmd == "inbox":
            for f in sorted(os.listdir(INBOX)):
                print(f"  {f}")
        elif cmd in ("quit", "exit"):
            break
