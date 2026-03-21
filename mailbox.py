import os, time, json, shutil, datetime
from pathlib import Path

INBOX = "inbox/"
SENT_MAIL = "sent/"
LOG = "mailbox.log"
MAILBOX_CMD = "mailbox_command.txt"
ADDRESS_FILE = "my_address.txt"
RECEIVED_DIR = "Received_Traffic"
SENT_DIR = "Sent_traffic"

for d in [INBOX, SENT_MAIL]: os.makedirs(d, exist_ok=True)
os.makedirs(RECEIVED_DIR, exist_ok=True)
os.makedirs(SENT_DIR, exist_ok=True)

def mlog(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    with open(LOG, "a") as f: f.write(f"[{ts}] {msg}\n")
    if len(open(LOG).readlines()) > 1000: open(LOG, "w").write("")
    print(f"[MAILBOX] {msg}")

if not os.path.exists(ADDRESS_FILE):
    print("Welcome to temporal messaging.")
    addr = input("Enter your to: address you will use: ")
    with open(ADDRESS_FILE, "w") as f: f.write(addr)
    mlog(f"Setup complete for {addr}")

with open(ADDRESS_FILE) as f: MY_ADDR = f.read().strip()

print("Temporal Mailbox - protocol active")
print("Commands: send, poll, inbox, sent, reply, quit")

def send_message():
    subject = input("Subject: ")
    to_addr = input("To address: ")
    body = input("Body: ")
    from_date = int(time.time())
    to_date = int(input("To date (unix timestamp or 0=now): ") or from_date)
    msg = {"subject": subject, "to_addr": to_addr, "from_addr": MY_ADDR, "from_date": from_date, "to_date": to_date, "body": body}
    data = json.dumps(msg).encode()[:1024]
    if len(data) < 1024: data += b"\0" * (1024 - len(data))
    msg_file = f"{SENT_DIR}/{int(time.time())}.msg"
    with open(msg_file, "wb") as f: f.write(data)
    shutil.copy(msg_file, f"{SENT_MAIL}/{os.path.basename(msg_file)}")
    mlog(f"SENT to Sent_traffic (airport will encode)")

def poll():
    mlog("POLL - checking proofs + Received_Traffic")
    if os.path.exists(MAILBOX_CMD):
        for line in open(MAILBOX_CMD):
            if "proof" in line:
                mlog(f"PROOF from airport: coord {line.split()[-1]}")
        open(MAILBOX_CMD, "w").close()
    for f in list(os.listdir(RECEIVED_DIR)):
        if f.endswith(".msg"):
            data = open(f"{RECEIVED_DIR}/{f}", "rb").read().replace(b"\0", b"")
            try:
                msg = json.loads(data.decode(errors="ignore"))
                if msg.get("to_addr") == MY_ADDR:
                    now = int(time.time())
                    if msg["to_date"] <= now:
                        shutil.move(f"{RECEIVED_DIR}/{f}", f"{INBOX}/{f}")
                        mlog(f"DELIVERED to inbox")
                    else:
                        mlog(f"QUEUED future (received early for board health)")
                else:
                    os.remove(f"{RECEIVED_DIR}/{f}")
                    mlog(f"DELETED unused traffic")
            except: os.remove(f"{RECEIVED_DIR}/{f}")

def show_inbox():
    mlog("INBOX")
    for f in os.listdir(INBOX):
        with open(f"{INBOX}{f}") as ff:
            msg = json.load(ff)
            status = "DELIVERED" if msg["to_date"] <= int(time.time()) else "QUEUED FUTURE"
            print(f"{f} | {status} | {msg['subject']} from {msg['from_addr']}")

def show_sent():
    mlog("SENT MAIL")
    for f in os.listdir(SENT_MAIL): print(f"  {f}")

def reply():
    show_inbox()
    target = input("Reply to file: ")
    if os.path.exists(f"{INBOX}{target}"):
        with open(f"{INBOX}{target}") as f:
            orig = json.load(f)
        subject = "RE: " + orig["subject"]
        body = input("Body: ")
        to_date = int(time.time())
        msg = {"subject": subject, "to_addr": orig["from_addr"], "from_addr": MY_ADDR, "from_date": int(time.time()), "to_date": to_date, "body": body}
        data = json.dumps(msg).encode()[:1024]
        if len(data) < 1024: data += b"\0" * (1024 - len(data))
        msg_file = f"{SENT_DIR}/{int(time.time())}.msg"
        with open(msg_file, "wb") as f: f.write(data)
        shutil.copy(msg_file, f"{SENT_MAIL}/{os.path.basename(msg_file)}")
        mlog("REPLY to Sent_traffic")

if __name__ == "__main__":
    while True:
        cmd = input("> ").strip().lower()
        if cmd == "send": send_message()
        elif cmd == "poll": poll()
        elif cmd == "inbox": show_inbox()
        elif cmd == "sent": show_sent()
        elif cmd == "reply": reply()
        elif cmd in ("quit", "exit"):
            print("Have a nice life.")
            mlog("Mailbox shutdown")
            break
