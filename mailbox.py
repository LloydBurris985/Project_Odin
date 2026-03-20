import os, time, json, datetime
from pathlib import Path

INBOX = "inbox/"
OUTBOX = "outbox/"
SENT = "sent/"
RECV_BUFFER = "../recv_buffer.bin"
LOG = "mailbox.log"
ADDRESS_FILE = "my_address.txt"

for d in [INBOX, OUTBOX, SENT]:
    os.makedirs(d, exist_ok=True)

def mlog(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    with open(LOG, "a") as f:
        f.write(f"[{ts}] {msg}\n")
    if len(open(LOG).readlines()) > 1000:
        open(LOG, "w").write("")
    print(f"[MAILBOX] {msg}")

if not os.path.exists(ADDRESS_FILE):
    print("Welcome to temporal messaging.")
    address = input("Enter your to: address you will use: ")
    with open(ADDRESS_FILE, "w") as f:
        f.write(address)
    mlog(f"Mailbox setup complete for address: {address}")

with open(ADDRESS_FILE) as f:
    MY_ADDRESS = f.read().strip()

def send_message():
    subject = input("Subject: ")
    to_addr = input("To address: ")
    body = input("Body: ")
    days = int(input("Delivery days ahead (0=now): ") or 0)
    from_date = int(time.time())
    to_date = from_date + days * 86400
    msg = {"subject": subject, "to": to_addr, "from_date": from_date, "to_date": to_date, "body": body}
    msg_file = f"{OUTBOX}{int(time.time())}.msg"
    with open(msg_file, "w") as f:
        json.dump(msg, f)
    with open("../command.txt", "a") as f:
        f.write(f"send Runway_42 {msg_file}\n")
    mlog(f"Message queued to runway (from {from_date} to {to_date})")

def poll():
    mlog("Polling runway for new mail")
    if os.path.exists(RECV_BUFFER) and os.path.getsize(RECV_BUFFER) > 0:
        with open(RECV_BUFFER, "rb") as f:
            raw = f.read()
        try:
            msg = json.loads(raw)
            if msg.get("to") == MY_ADDRESS or msg.get("to") == "all":
                now = int(time.time())
                if msg["to_date"] <= now or msg["to_date"] == 0:
                    inbox_file = f"{INBOX}{int(time.time())}.msg"
                    with open(inbox_file, "w") as f:
                        json.dump(msg, f)
                    mlog(f"Delivered mail from airport (to_date {msg['to_date']})")
                else:
                    mlog(f"Queued future mail (will deliver at {msg['to_date']})")
        except:
            mlog("Received raw traffic from airport")
        Path(RECV_BUFFER).write_text("")
    else:
        mlog("No new mail in buffer this poll")

def show_inbox():
    mlog("Opening inbox")
    files = os.listdir(INBOX)
    if not files:
        print("Inbox empty")
        return
    for f in files:
        with open(f"{INBOX}{f}") as ff:
            msg = json.load(ff)
            status = "DELIVERED" if msg["to_date"] <= int(time.time()) else "QUEUED"
            print(f"{f} | {status} | {msg['subject']} → {msg['to']}")

def reply():
    show_inbox()
    target = input("Reply to which inbox file? ")
    if os.path.exists(f"{INBOX}{target}"):
        with open(f"{INBOX}{target}") as f:
            orig = json.load(f)
        subject = "RE: " + orig["subject"]
        body = input("Reply body: ")
        msg = {"subject": subject, "to": orig["from"] or orig["to"], "from_date": int(time.time()), "to_date": int(time.time()), "body": body}
        msg_file = f"{OUTBOX}{int(time.time())}.msg"
        with open(msg_file, "w") as f:
            json.dump(msg, f)
        with open("../command.txt", "a") as f:
            f.write(f"send Runway_42 {msg_file}\n")
        mlog("Reply queued to runway")

if __name__ == "__main__":
    print("Temporal Mailbox - protocol active (subject, to date, from date, body)")
    while True:
        cmd = input("> ").strip().lower()
        if cmd == "send":
            send_message()
        elif cmd == "poll":
            poll()
        elif cmd == "inbox":
            show_inbox()
        elif cmd == "reply":
            reply()
        elif cmd in ("quit", "exit"):
            mlog("Mailbox shutdown")
            break
