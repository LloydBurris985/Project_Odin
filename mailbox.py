import json
import time
import subprocess
import os
from datetime import datetime

INBOX = "floyd_inbox.json"
QUEUE = "floyd_queue.json"
RUNWAY = "Runway_42"

def load_json(file):
    try:
        with open(file) as f: return json.load(f)
    except: return []

def save_json(file, data):
    with open(file, "w") as f: json.dump(data, f, indent=2)

def send_message(subject, body, days_before=1):
    delivery_time = int(time.time()) - (days_before * 86400)
    msg = {"from": "future-floyd", "to": "floyd", "subject": subject, "body": body, "delivery_time": delivery_time, "chain_id": f"temporal-{int(time.time())}"}
    queue = load_json(QUEUE)
    queue.append(msg)
    save_json(QUEUE, queue)
    with open("airport_command.txt", "a") as f:
        f.write(f"send {RUNWAY} floyd_queue.json\n")
    print(f"✅ Sent from FUTURE — will appear {days_before} day(s) before sent.")

def poll():
    current = int(time.time())
    queue = load_json(QUEUE)
    new = []
    remaining = []
    for msg in queue:
        if msg["delivery_time"] <= current:
            subprocess.run(["python3", "odins_eye.py", "--mode", "decode", "--file", f"{RUNWAY}_floyd_queue.json.lattice"])
            new.append(msg)
        else:
            remaining.append(msg)
    save_json(QUEUE, remaining)
    save_json(INBOX, load_json(INBOX) + new)
    if new:
        print(f"{len(new)} messages arrived FROM THE FUTURE!")
        for m in new:
            print(f"Subject: {m['subject']} | Body: {m['body'][:100]}...")
    else:
        print("No new temporal messages yet.")

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
            subject = input("Subject: ") or "Lottery Numbers"
            body = input("Body: ")
            days = int(input("Days before sent? (1 = tomorrow's message today): ") or 1)
            send_message(subject, body, days)
        elif cmd == "poll":
            poll()
        elif cmd == "inbox":
            view_inbox()
