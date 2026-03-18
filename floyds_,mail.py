
import json
import time
import subprocess
import os
from datetime import datetime

INBOX = "floyd_inbox.json"
QUEUE = "floyd_queue.json"
RUNWAY = "Runway_42"  # dedicated temporal mailbox runway

def load_json(file):
    try:
        with open(file) as f: return json.load(f)
    except: return []

def save_json(file, data):
    with open(file, "w") as f: json.dump(data, f, indent=2)

def send_lottery(numbers, delay_min=1):
    msg = {"from": "floyd", "to": "floyd-future", "subject": "Lottery Numbers", "body": numbers, "delay_min": delay_min}
    queue = load_json(QUEUE)
    queue.append(msg)
    save_json(QUEUE, queue)
    # Send via Airport + Odins Eye
    with open("airport_command.txt", "a") as f:
        f.write(f"send {RUNWAY} floyd_queue.json\n")
    print(f"Queued for {delay_min} min → sent to runway via Odins Eye")

def poll():
    current = time.time()
    queue = load_json(QUEUE)
    inbox = load_json(INBOX)
    new = []
    remaining = []
    for msg in queue:
        if current >= msg.get("sent_time", 0) + (msg.get("delay_min", 1) * 60):
            # Real Odins Eye decode from runway
            subprocess.run(["python3", "odins_eye.py", "--mode", "decode", "--file", f"{RUNWAY}_floyd_queue.json.lattice"])
            inbox.append(msg)
            new.append(msg)
        else:
            remaining.append(msg)
    save_json(QUEUE, remaining)
    save_json(INBOX, inbox)
    if new:
        print(f"{len(new)} messages arrived!")
        for m in new:
            print(f"Subject: {m['subject']} | Body: {m['body']}")
    else:
        print("No new messages yet.")

def view_inbox():
    inbox = load_json(INBOX)
    print(f"Inbox ({len(inbox)} messages):")
    for m in inbox:
        print(f"- {m['subject']}: {m['body'][:80]}...")

if __name__ == "__main__":
    print("Floyd's Temporal Mailbox — Odin Edition")
    while True:
        cmd = input("\n> ").strip().lower()
        if cmd == "quit": break
        elif cmd.startswith("send"):
            delay = int(cmd.split()[1]) if len(cmd.split()) > 1 else 1
            numbers = input("Lottery numbers: ")
            send_lottery(numbers, delay)
        elif cmd == "poll":
            poll()
        elif cmd == "inbox":
            view_inbox()
