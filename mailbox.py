import os, time, json, shutil
from pathlib import Path

INBOX = "inbox/"
OUTBOX = "outbox/"
SENT = "sent/"
LOG = "mailbox.log"
MAILBOX_CMD = "mailbox_command.txt"
ADDRESS_FILE = "my_address.txt"

for d in [INBOX, OUTBOX, SENT]: os.makedirs(d, exist_ok=True)

def mlog(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    with open(LOG, "a") as f: f.write(f"[{ts}] {msg}\n")
    if len(open(LOG).readlines()) > 1000: open(LOG, "w").write("")
    print(f"[MAILBOX] {msg}")

if not os.path.exists(ADDRESS_FILE):
    print("Welcome to temporal messaging.")
    addr = input("Enter your to: address: ")
    with open(ADDRESS_FILE, "w") as f: f.write(addr)
    mlog(f"Setup complete for {addr}")

with open(ADDRESS_FILE) as f: MY_ADDR = f.read().strip()

def send_message():
    subject = input("Subject: ")
    to_addr = input("To address: ")
    body = input("Body: ")
    from_date = int(time.time())
    to_date = int(input("Delivery unix timestamp (future or 0=now): ") or from_date)
    msg = {"subject": subject, "to_addr": to_addr, "from_addr": MY_ADDR, "from_date": from_date, "to_date": to_date, "body": body}
    data = json.dumps(msg).encode()[:1024]
    if len(data) < 1024: data += b"\0" * (1024 - len(data))
    msg_file = f"{OUTBOX}/{int(time.time())}.msg"
    with open(msg_file, "wb") as f: f.write(data)
    with open("command.txt", "a") as f: f.write(f"send Runway_42 {os.path.abspath(msg_file)}\n")
    shutil.copy(msg_file, f"{SENT}/{os.path.basename(msg_file)}")
    mlog(f"SENT fixed 1024B message (from {from_date} → {to_date})")

def poll():
    mlog("POLL - checking proofs + traffic from airport")
    if os.path.exists(MAILBOX_CMD):
        for line in open(MAILBOX_CMD):
            if "proof" in line:
                mlog(f"VERIFIED sent at coord {line.split()[-1]}")
        open(MAILBOX_CMD, "w").close()
    for f in os.listdir("inbox"):  # your app dir
        if f.endswith(".msg"):
            data = open(f"inbox/{f}", "rb").read().replace(b"\0", b"")
            try:
                msg = json.loads(data.decode(errors="ignore"))
                if msg.get("to_addr") == MY_ADDR:
                    now = int(time.time())
                    if msg["to_date"] <= now:
                        shutil.move(f"inbox/{f}", f"{INBOX}/{f}")
                        mlog(f"DELIVERED message from airport")
                    else:
                        mlog(f"QUEUED future message until {msg['to_date']}")
            except: pass

if __name__ == "__main__":
    print("Temporal Mailbox - protocol active")
    while True:
        cmd = input("> ").strip().lower()
        if cmd == "send": send_message()
        elif cmd == "poll": poll()
        elif cmd == "inbox":
            for f in os.listdir(INBOX): print(f"  {f}")
        elif cmd in ("quit", "exit"): break
