import os, time, subprocess, datetime, json, shutil, threading
from pathlib import Path

LOG = "airport.log"
CMD_FILE = "command.txt"          # exactly as you said
QUEUE_FILE = "sent_queue.txt"
RUNWAY_DIR = "runways"
RECV_BUFFER = "recv_buffer.txt"   # apps read this

os.makedirs(RUNWAY_DIR, exist_ok=True)
os.makedirs("sent_traffic", exist_ok=True)
os.makedirs("received_traffic", exist_ok=True)

RUNWAYS = {}  # dynamic: {runway_name: {"start": int, "mid": int, "end": int, "app": str, "sent":0, "recv":0}}

def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG, "a") as f: f.write(f"[{ts}] {msg}\n")
    print(f"[AIRPORT] {msg}")

def clear(): os.system('cls' if os.name == 'nt' else 'clear')

def load_commands():
    if not os.path.exists(CMD_FILE): return []
    with open(CMD_FILE) as f: lines = [line.strip() for line in f if line.strip()]
    Path(CMD_FILE).write_text("")  # clear after read
    return lines

def register_app(cmd):
    # format: register Runway_42 MyTemporalMailApp 1000000 3000000
    parts = cmd.split()
    if len(parts) < 5: return
    name, app, start, length = parts[1], parts[2], int(parts[3]), int(parts[4])
    mid = start + length // 2
    end = start + length
    RUNWAYS[name] = {"start": start, "mid": mid, "end": end, "app": app, "sent":0, "recv":0}
    log(f"✅ Registered {app} to {name} | coding {start}-{mid} | decoding {mid}-{end}")

def service_runway(runway):
    rw = RUNWAYS[runway]
    # 1. Check for data to SEND (app wrote to command.txt "send Runway_42 file.bin")
    cmds = load_commands()
    for c in cmds:
        if c.startswith("send") and runway in c:
            _, rw_name, file = c.split()
            if rw_name == runway and os.path.exists(file):
                # encode ONLY in coding section (start → mid)
                subprocess.run(["python3", "odins_eye.py", "--mode", "encode", "--file", file,
                                "--start_p", str(rw["start"]), "--seed", "2026-03-20T04:43"])
                lattice = f"{runway}_{os.path.basename(file)}.lattice"
                shutil.move(file + ".lattice", lattice)
                rw["sent"] += 1
                log(f"ENCODED & QUEUED on {runway} (coding section)")

    # 2. Decode from decoding section (mid → end) + send buffer to app
    for f in os.listdir("."):
        if f.endswith(".lattice") and runway in f:
            coord_file = f + ".coord.json"
            if os.path.exists(coord_file):
                with open(coord_file) as cf: coord = json.load(cf)
                if coord["start_p"] >= rw["mid"]:  # only decode section
                    subprocess.run(["python3", "odins_eye.py", "--mode", "decode", "--file", f, "--any"])
                    decoded = f.replace(".lattice", ".decoded")
                    if os.path.exists(decoded):
                        with open(decoded, "rb") as df: buffer_data = df.read()
                        # write to shared buffer for the app
                        with open(RECV_BUFFER, "ab") as bf: bf.write(buffer_data)
                        rw["recv"] += 1
                        log(f"DECODED buffer → {rw['app']} | coord passed back")
                        shutil.move(f, f"received_traffic/{f}")
                        # send coord back for verification
                        with open(f"{rw['app']}_verified.txt", "w") as vf: vf.write(json.dumps(coord))

def dashboard_loop():
    while True:
        clear()
        print("╔════════════════════════════════════════════════════════════╗")
        print("║          ODIN AIRPORT v11 – LIVE RUNWAY CONTROL            ║")
        print("╚════════════════════════════════════════════════════════════╝\n")
        for name, rw in RUNWAYS.items():
            print(f" {name:12} App: {rw['app']}  Ratio: {rw['recv']/max(rw['sent'],1)*100:.1f}%  Pass: {rw.get('pass',0)}")
        print(f"\nBUFFER READY → apps read {RECV_BUFFER}")
        print("SERVICING ALL APPS EVENLY (send → poll → next app)...\n")
        
        # even servicing
        for rw_name in list(RUNWAYS.keys()):
            service_runway(rw_name)
            time.sleep(0.5)  # fair share
        
        time.sleep(2)

if __name__ == "__main__":
    log("=== AIRPORT v11 STARTED – waiting for registrations ===")
    threading.Thread(target=dashboard_loop, daemon=True).start()
    while True:
        cmds = load_commands()
        for c in cmds:
            if c.startswith("register"):
                register_app(c)
        time.sleep(1)
