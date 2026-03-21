import os, time, json, shutil, subprocess, datetime
from pathlib import Path

LOG = "airport.log"
CMD_FILE = "command.txt"
MAILBOX_CMD = "mailbox_command.txt"
TEMPORAL_DB = "temporal_db.json"

os.makedirs("sent_traffic", exist_ok=True)
os.makedirs("received_traffic", exist_ok=True)
os.makedirs("runways", exist_ok=True)

RUNWAYS = {}

def log(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    with open(LOG, "a") as f: f.write(f"[{ts}] {msg}\n")
    if len(open(LOG).readlines()) > 1000: open(LOG, "w").write("")
    print(f"[AIRPORT] {msg}")

def update_db(entry):
    db = json.load(open(TEMPORAL_DB)) if os.path.exists(TEMPORAL_DB) else {}
    db[str(int(time.time()))] = entry
    with open(TEMPORAL_DB, "w") as f: json.dump(db, f, indent=2)

def load_commands():
    if not os.path.exists(CMD_FILE): return []
    lines = [line.strip() for line in open(CMD_FILE) if line.strip()]
    Path(CMD_FILE).write_text("")
    return lines

def register(cmd):
    parts = cmd.split()
    name, app, start, length, app_dir, mode = parts[1], parts[2], int(parts[3]), int(parts[4]), parts[5], parts[6]
    mid = start + length // 2
    end = start + length
    RUNWAYS[name] = {"start": start, "mid": mid, "end": end, "app": app, "dir": app_dir, "mode": mode}
    os.makedirs(app_dir, exist_ok=True)
    log(f"REGISTERED {app} → {name} (coding {start}-{mid} | decoding {mid}-{end} | dir {app_dir})")
    update_db({"action": "register", "runway": name})

def service(runway):
    rw = RUNWAYS[runway]
    log(f"SERVICE {runway} - polling send + decoding EVERY coord")
    for c in load_commands():
        if c.startswith("send") and runway in c:
            _, _, fpath = c.split(maxsplit=2)
            if os.path.exists(fpath):
                log(f"ENCODING at coding coord {rw['start']}")
                subprocess.check_call(["python3", "odins_eye.py", "--mode", "encode", "--file", fpath, "--start_p", str(rw["start"]), "--seed", "2026-03-20"])
                lattice = f"{runway}_{os.path.basename(fpath)}.lattice"
                shutil.move(fpath + ".lattice", f"sent_traffic/{lattice}")
                proof = rw["start"]
                with open(MAILBOX_CMD, "a") as f: f.write(f"proof {runway} {proof}\n")
                log(f"CODED - proof coord {proof} returned")
                update_db({"action": "encode", "coord": proof})

    for f in os.listdir("."):
        if f.endswith(".lattice") and runway in f:
            cf = f + ".coord.json"
            if os.path.exists(cf):
                coord = json.load(open(cf))
                if coord.get("start_p", 0) >= rw["mid"]:
                    log(f"DECODING coord {coord['start_p']} (full section to sent territory)")
                    subprocess.check_call(["python3", "odins_eye.py", "--mode", "decode", "--file", f, "--any"])
                    dec = f.replace(".lattice", ".decoded")
                    if os.path.exists(dec):
                        data = open(dec, "rb").read()[:1024]
                        if len(data) < 1024: data += b"\0" * (1024 - len(data))
                        msg_file = f"{rw['dir']}/{coord['start_p']}.msg"
                        with open(msg_file, "wb") as mf: mf.write(data)
                        shutil.move(f, f"received_traffic/{f}")
                        log(f"DELIVERED 1024B {msg_file}")
                        update_db({"action": "decode", "coord": coord['start_p'], "file": msg_file})

if __name__ == "__main__":
    log("AIRPORT STARTED - temporal database active")
    while True:
        for cmd in load_commands():
            if cmd.startswith("register"): register(cmd)
        for rw in list(RUNWAYS.keys()):
            service(rw)
            time.sleep(0.3)
        time.sleep(1)
