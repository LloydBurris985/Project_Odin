# airport.py (rewritten - no log clearing, background safe, AB rules enforced)
import os, time, json, shutil
from pathlib import Path

LOG = "airport.log"
CMD_FILE = "command.txt"
RUNWAY_DIR = "runways"
RECV_BUFFER = "recv_buffer.bin"

os.makedirs(RUNWAY_DIR, exist_ok=True)
os.makedirs("sent_traffic", exist_ok=True)
os.makedirs("received_traffic", exist_ok=True)

RUNWAYS = {}  # {name: {"start": int, "mid": int, "end": int, "app": str}}

def log(msg):
    with open(LOG, "a") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")

def load_commands():
    if not os.path.exists(CMD_FILE):
        return []
    lines = [line.strip() for line in open(CMD_FILE) if line.strip()]
    Path(CMD_FILE).write_text("")
    return lines

def register(cmd):
    parts = cmd.split()
    name, app, start, length = parts[1], parts[2], int(parts[3]), int(parts[4])
    mid = start + length // 2
    end = start + length
    RUNWAYS[name] = {"start": start, "mid": mid, "end": end, "app": app}
    log(f"Registered {app} → {name} (coding {start}-{mid} | decoding {mid}-{end})")

def service(runway):
    rw = RUNWAYS[runway]
    cmds = load_commands()
    for c in cmds:
        if c.startswith("send") and runway in c:
            _, rw_name, fpath = c.split()
            if rw_name == runway and os.path.exists(fpath):
                # encode starts in coding section, must cross into decoding
                states = CoderModeler.encode(fpath, "odin_eye", start_p=rw["start"])
                shutil.move(fpath + ".lattice", f"{runway}_{os.path.basename(fpath)}.lattice")
                log(f"ENCODED (coding → decoding cross)")

    # decode EVERY coordinate in decoding section
    for f in os.listdir("."):
        if f.endswith(".lattice") and runway in f:
            coord_file = f + ".coord.json"
            if os.path.exists(coord_file):
                coord = json.load(open(coord_file))
                if coord["start_p"] >= rw["mid"]:
                    data = CoderModeler.decode(f, "odin_eye")
                    with open(RECV_BUFFER, "ab") as bf:
                        bf.write(data)
                    shutil.move(f, f"received_traffic/{f}")
                    log(f"DECODED full section → buffer for {rw['app']}")

if __name__ == "__main__":
    log("AIRPORT STARTED - AB rules active")
    while True:
        for cmd in load_commands():
            if cmd.startswith("register"):
                register(cmd)
        for rw_name in list(RUNWAYS.keys()):
            service(rw_name)
            time.sleep(0.3)  # even servicing
        time.sleep(1)
