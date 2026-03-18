import os
import time
import subprocess
import datetime
import json
import shutil

LOG = "airport.log"
CMD_FILE = "airport_command.txt"
QUEUE_FILE = "sent_queue.txt"
CONFIG = "runway_config.json"
SENT_DIR = "sent_traffic"
RECV_DIR = "received_traffic"

if not os.path.exists(SENT_DIR): os.mkdir(SENT_DIR)
if not os.path.exists(RECV_DIR): os.mkdir(RECV_DIR)

if not os.path.exists(CONFIG):
    RUNWAYS = {
        "Runway_42": {"start_p": 500000, "length": 2000000, "sent": 0, "received": 0, "ratio": 100.0, "pass": 0},
        "TEA": {"start_p": 1000000, "length": 5000000, "sent": 0, "received": 0, "ratio": 100.0, "pass": 0},
        "Public": {"start_p": 700000, "length": 5000000, "sent": 0, "received": 0, "ratio": 100.0, "pass": 0}
    }
    with open(CONFIG, "w") as f: json.dump(RUNWAYS, f, indent=2)
else:
    with open(CONFIG) as f: RUNWAYS = json.load(f)

def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG, "a") as f: f.write(f"[{ts}] {msg}\n")
    print(f"LOG: {msg}")

def clear():
    os.system('cls' if os.name == 'nt' else 'clear')

def poll():
    for name, rw in RUNWAYS.items():
        rw["pass"] += 1
        log(f"Pass {rw['pass']} on {name}")
        if os.path.exists(QUEUE_FILE):
            with open(QUEUE_FILE) as f: queue = f.readlines()
            for line in queue:
                if name in line:
                    f_name = line.strip().split()[-1]
                    lattice = f"{name}_{f_name}.lattice"
                    if os.path.exists(lattice):
                        log(f"Pass {rw['pass']} — DECODED with Odins Eye: {lattice}")
                        subprocess.run(["python3", "odins_eye.py", "--mode", "decode", "--file", lattice])
                        rw["received"] += 1
                        ratio = round((rw["received"] / rw["sent"] * 100), 1) if rw["sent"] > 0 else 100.0
                        rw["ratio"] = ratio
                        log(f"✅ DECODED & RECOVERED on Pass {rw['pass']} | Ratio now {ratio}%")
                        shutil.move(lattice, os.path.join(RECV_DIR, lattice))
                        if os.path.exists(lattice + ".coord.json"):
                            shutil.move(lattice + ".coord.json", os.path.join(RECV_DIR, lattice + ".coord.json"))
        else:
            log(f"Pass {rw['pass']} — scanning temporal stream (Odins Eye ready)")
    with open(CONFIG, "w") as f: json.dump(RUNWAYS, f)

def process_commands():
    if os.path.exists(CMD_FILE):
        with open(CMD_FILE) as f: cmd = f.read().strip()
        os.remove(CMD_FILE)
        if cmd.startswith("send"):
            parts = cmd.split()
            runway, file = parts[1], parts[2]
            if runway in RUNWAYS and os.path.exists(file):
                start = RUNWAYS[runway]["start_p"]
                subprocess.run(["python3", "odins_eye.py", "--mode", "encode", "--file", file, "--start_p", str(start), "--seed", "2026-03-15T04:41"])
                lattice = f"{runway}_{file}.lattice"
                shutil.copy(file + ".lattice", lattice)
                RUNWAYS[runway]["sent"] += 1
                with open(QUEUE_FILE, "a") as f: f.write(f"{runway} {file}\n")
                log(f"✅ QUEUED & SENT on {runway} (passenger #{RUNWAYS[runway]['sent']})")
        with open(CMD_FILE, "w") as f: f.write("")

def dashboard():
    while True:
        clear()
        print("╔════════════════════════════════════════════════════════════╗")
        print("║     STARSHIP ODIN — AIRPORT OPS v9 (DYNAMIC LIVE)          ║")
        print("║               Time Enforcement Agency (TEA)                ║")
        print("╚════════════════════════════════════════════════════════════╝\n")
        for name, rw in RUNWAYS.items():
            print(f"🛫 {name:12} Start: {rw['start_p']}  Len: {rw['length']:,}  Ratio: {rw['ratio']}%  Pass: {rw['pass']}")
            print(f"   Sent: {rw.get('sent',0)}   Received: {rw['received']}\n")
        print("🔄 POLLING NOW... (screen updates every 2s — no ENTER ever)")
        process_commands()
        poll()
        time.sleep(2)

if __name__ == "__main__":
    log("=== AIRPORT v9 DYNAMIC LIVE STARTED ===")
    dashboard()
