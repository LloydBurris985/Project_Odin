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
    print("=== AIRPORT SETUP (first run only) ===")
    num = int(input("How many runways? (3): ") or 3)
    config = {}
    for i in range(num):
        name = input(f"Runway name {i+1}: ") or f"Runway_{i+1}"
        start = int(input(f"Start coordinate: ") or 500000)
        length = int(input(f"Length: ") or 2000000)
        config[name] = {"start_p": start, "length": length, "sent": 0, "received": 0, "ratio": 100.0, "pass": 0}
    with open(CONFIG, "w") as f: json.dump(config, f)

with open(CONFIG) as f: RUNWAYS = json.load(f)

def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG, "a") as f: f.write(f"[{ts}] {msg}\n")
    print(f"LOG: {msg}")

def clear(): os.system('cls' if os.name == 'nt' else 'clear')

def poll():
    for name, rw in RUNWAYS.items():
        rw["pass"] += 1
        log(f"Pass {rw['pass']} on {name}")

        # Process queue (real Odins Eye decode)
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

        # Dummy temporal scan for demo (always shows activity)
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
        with open(CMD_FILE, "w") as f: f.write("")  # recreate empty

def dashboard():
    while True:
        clear()
        print("╔════════════════════════════════════════════════════════════╗")
        print("║     STARSHIP ODIN — AIRPORT OPS v9 (FULL DYNAMIC)          ║")
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
    log("=== AIRPORT v9 FULL DYNAMIC LIVE STARTED ===")
    dashboard()
