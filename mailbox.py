import os, time, subprocess, datetime
from pathlib import Path

INBOX = "inbox/"
OUTBOX = "outbox/"
SENT = "sent/"
RUNWAY = "Runway_42"   # or register your own
RECV_BUFFER = "../recv_buffer.txt"  # from airport

for d in [INBOX, OUTBOX, SENT]: os.makedirs(d, exist_ok=True)

def log(msg): print(f"[MAILBOX] {msg}")

def new_user():
    print("Creating new temporal user...")
    # create address file etc. – expand as needed

def send_message(subject, to, body, delivery_days=0):
    delivery_ts = int(time.time()) + (delivery_days * 86400)
    msg_file = f"{OUTBOX}{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.msg"
    with open(msg_file, "w") as f:
        f.write(f"subject:{subject}\nto:{to}\ndelivery:{delivery_ts}\nbody:{body}")
    # forward to airport
    with open("command.txt", "a") as f:
        f.write(f"send {RUNWAY} {msg_file}\n")
    log(f"Queued for runway – will deliver at {delivery_ts}")

def poll_and_deliver():
    # airport already wrote decoded data to RECV_BUFFER
    if os.path.exists(RECV_BUFFER) and os.path.getsize(RECV_BUFFER) > 0:
        with open(RECV_BUFFER, "r") as f: raw = f.read()
        # simple parse – expand with BNS decode if needed
        for line in raw.split("---MSG---"):
            if "subject:" in line:
                # extract & deliver
                ts = int(time.time())
                # future messages already in buffer → show as received for health
                with open(f"{INBOX}{ts}.msg", "w") as f: f.write(line)
                log("Future message delivered early (board health OK)")
        Path(RECV_BUFFER).write_text("")  # clear buffer

    # late messages deliver immediately
    for f in os.listdir(OUTBOX):
        # check delivery time etc. – your logic

def main():
    print("Temporal Mailbox – Odin Edition (real airport delivery)")
    while True:
        cmd = input("> ").strip().lower()
        if cmd == "send":
            subject = input("Subject: ")
            to = input("To: ")
            body = input("Body: ")
            days = int(input("Delivery days ahead (0=now, negative=future): ") or 0)
            send_message(subject, to, body, days)
        elif cmd == "poll":
            poll_and_deliver()
        elif cmd == "inbox":
            print("Inbox:")
            for f in os.listdir(INBOX): print(f"  {f}")
        elif cmd in ("quit", "exit"): break

if __name__ == "__main__":
    main()
