import os, time, json, shutil, datetime
from pathlib import Path

LOG = "airport.log"
BUFFER_LOG = "airport_buffer.log"
CMD_FILE = "command.txt"
RUNWAY_DIR = "runways"
RECV_BUFFER = "recv_buffer.bin"

os.makedirs(RUNWAY_DIR, exist_ok=True)
os.makedirs("sent_traffic", exist_ok=True)
os.makedirs("received_traffic", exist_ok=True)

RUNWAYS = {}

def log(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    with open(LOG, "a") as f:
        f.write(f"[{ts}] {msg}\n")
    if len(open(LOG).readlines()) > 1000:
        open(LOG, "w").write("")
    print(f"[AIRPORT] {msg}")

def buffer_log(coord):
    with open(BUFFER_LOG, "a") as f:
        f.write(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] DECODED coord {coord}\n")
    if len(open(BUFFER_LOG).readlines()) > 1000:
        open(BUFFER_LOG, "w").write("")

def load_commands():
    if not os.path.exists(CMD_FILE):
        return []
    lines = [line.strip() for line in open(CMD_FILE) if line.strip()]
    Path(CMD_FILE).write_text("")
    return lines

def register(cmd):
    parts = cmd.split()
    name = parts[1]
    app = parts[2]
    start = int(parts[3])
    length = int(parts[4])
    mid = start + length // 2
    end = start + length
    RUNWAYS[name] = {"start": start, "mid": mid, "end": end, "app": app}
    log(f"Registered {app} → {name} (coding {start}-{mid} | decoding {mid}-{end})")

def service(runway):
    rw = RUNWAYS[runway]
    log(f"Polling runway {runway} for {rw['app']}")
    cmds = load_commands()
    for c in cmds:
        if c.startswith("send") and runway in c:
            _, rw_name, fpath = c.split(maxsplit=2)
            if rw_name == runway and os.path.exists(fpath):
                log(f"ENCODING traffic from {rw['app']} at coding coord {rw['start']}")
                os.system(f"python3 odins_eye.py --mode encode --file {fpath} --start_p {rw['start']} --seed 2026-03-20")
                lattice = f"{runway}_{os.path.basename(fpath)}.lattice"
                if os.path.exists(fpath + ".lattice"):
                    shutil.move(fpath + ".lattice", lattice)
                    log(f"CODED & queued on runway (proof coord returned)")
                    with open(f"{rw['app']}_proof.txt", "w") as f:
                        f.write(json.dumps({"coord": rw['start']}))

    for f in os.listdir("."):
        if f.endswith(".lattice") and runway in f:
            coord_file = f + ".coord.json"
            if os.path.exists(coord_file):
                coord = json.load(open(coord_file))
                if coord.get("start_p", 0) >= rw["mid"]:
                    log(f"DECODING full section starting at coord {coord['start_p']}")
                    os.system(f"python3 odins_eye.py --mode decode --file {f} --any")
                    decoded = f.replace(".lattice", ".decoded")
                    if os.path.exists(decoded):
                        with open(decoded, "rb") as df:
                            data = df.read()
                        with open(RECV_BUFFER, "ab") as bf:
                            bf.write(data)
                        buffer_log(coord["start_p"])
                        shutil.move(f, f"received_traffic/{f}")
                        log(f"DECODED traffic passed to {rw['app']} buffer")

if __name__ == "__main__":
    log("AIRPORT STARTED - AB rules active (temporal database ready)")
    while True:
        for cmd in load_commands():
            if cmd.startswith("register"):
                register(cmd)
        for rw_name in list(RUNWAYS.keys()):
            service(rw_name)
            time.sleep(0.3)
        time.sleep(1)
