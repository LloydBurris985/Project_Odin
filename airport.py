import os, time, json, shutil, subprocess, datetime
from pathlib import Path

LOG = "airport.log"
CMD_FILE = "command.txt"
MAILBOX_CMD = "mailbox_command.txt"
TEMPORAL_DB = "temporal_db.json"

RECEIVED_DIR = "Received_Traffic"
SENT_DIR = "Sent_traffic"

os.makedirs(RECEIVED_DIR, exist_ok=True)
os.makedirs(SENT_DIR, exist_ok=True)
os.makedirs("sent_traffic", exist_ok=True)
os.makedirs("received_traffic", exist_ok=True)

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
    name = parts[1]
    app = parts[2]
    start = int(parts[3])
    length = int(parts[4])
    received_dir = parts[5]
    sent_dir = parts[6]
    mailbox_cmd = parts[7]
    mode = parts[8]
    mid = start + length // 2
    end = start + length
    RUNWAYS[name] = {"start": start, "mid": mid, "end": end, "app": app, "received_dir": received_dir, "sent_dir": sent_dir, "mailbox_cmd": mailbox_cmd, "mode": mode}
    os.makedirs(received_dir, exist_ok=True)
    os.makedirs(sent_dir, exist_ok=True)
    log(f"REGISTERED {app} → {name} (coding {start}-{mid} | decoding {mid}-{end})")
    update_db({"action": "register", "runway": name})

def service(runway):
    rw = RUNWAYS[runway]
    log(f"SERVICE {runway} - checking Sent_traffic + decoding EVERY coord")
    # Encode from mailbox Sent_traffic
    for f in os.listdir(rw["sent_dir"]):
        if f.endswith(".msg"):
            fpath = f"{rw['sent_dir']}/{f}"
            log(f"ENCODING from Sent_traffic at coord {rw['start']}")
            subprocess.check_call(["python3", "odins_eye.py", "--mode", "encode", "--file", fpath, "--start_p", str(rw["start"]), "--seed", "2026-03-20"])
            shutil.move(fpath + ".lattice", f"sent_traffic/{runway}_{f}.lattice")
            proof = rw["start"]
            with open(rw["mailbox_cmd"], "a") as mf: mf.write(f"proof {runway} {proof}\n")
            log(f"CODED - proof coord {proof} sent to mailbox_command.txt")
            update_db({"action": "encode", "coord": proof})
            os.remove(fpath)
    # Decode every coord to Received_Traffic
    for f in os.listdir("."):
        if f.endswith(".lattice") and runway in f:
            cf = f + ".coord.json"
            if os.path.exists(cf):
                coord = json.load(open(cf))
                if coord.get("start_p", 0) >= rw["mid"]:
                    log(f"DECODING coord {coord['start_p']} → {rw['received_dir']}")
                    subprocess.check_call(["python3", "odins_eye.py", "--mode", "decode", "--file", f, "--any"])
                    dec = f.replace(".lattice", ".decoded")
                    if os.path.exists(dec):
                        data = open(dec, "rb").read()[:1024]
                        if len(data) < 1024: data += b"\0" * (1024 - len(data))
                        msg_file = f"{rw['received_dir']}/{coord['start_p']}.msg"
                        with open(msg_file, "wb") as mf: mf.write(data)
                        shutil.move(f, f"received_traffic/{f}")
                        log(f"DELIVERED 1024B to Received_Traffic")
                        update_db({"action": "decode", "coord": coord['start_p']})

if __name__ == "__main__":
    log("AIRPORT STARTED - temporal database + full comms protocol active")
    while True:
        for cmd in load_commands():
            if cmd.startswith("register"): register(cmd)
        for rw_name in list(RUNWAYS.keys()):
            service(rw_name)
            time.sleep(0.3)
        time.sleep(1)
