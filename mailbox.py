# mailbox.py (rewritten - fixed indentation, no sim, real airport delivery)
import os, time
from pathlib import Path

INBOX = "inbox/"
OUTBOX = "outbox/"
SENT = "sent/"
RECV_BUFFER = "../recv_buffer.bin"

for d in [INBOX, OUTBOX, SENT]:
    os.makedirs(d, exist_ok=True)

def log(msg):
    print(f"[MAILBOX] {msg}")

def send(subject, to, body, delivery_days=0):
    ts = int(time.time()) + delivery_days * 86400
    msg_file = f"{OUTBOX}{ts}.msg"
    with open(msg_file, "w") as f:
        f.write(f"subject:{subject}\nto:{to}\ndelivery:{ts}\nbody:{body}")
    with open("../command.txt", "a") as f:
        f.write(f"send Runway_42 {msg_file}\n")
    log(f"Sent to runway - delivery at {ts}")

def poll():
    if os.path.exists(RECV_BUFFER) and os.path.getsize(RECV_BUFFER) > 0:
        with open(RECV_BUFFER, "rb") as f:
            raw = f.read()
        # parse & deliver (future messages shown immediately for health)
        with open(f"{INBOX}{int(time.time())}.msg", "wb") as f:
            f.write(raw)
        Path(RECV_BUFFER).write_text("")
        log("Delivered from airport buffer (including future messages)")

if __name__ == "__main__":
    while True:
        cmd = input("> ").strip().lower()
        if cmd == "send":
            s = input("Subject: ")
            t = input("To: ")
            b = input("Body: ")
            d = int(input("Days ahead (0=now): ") or 0)
            send(s, t, b, d)
        elif cmd == "poll":
            poll()
        elif cmd == "inbox":
            for f in os.listdir(INBOX):
                print(f"  {f}")
        elif cmd in ("quit", "exit"):
            break
