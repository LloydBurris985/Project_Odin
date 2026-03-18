import subprocess
import os
import datetime

LOG_FILE = "lattice_drive.log"
COUNT = {"saved": 0, "read": 0, "written": 0, "deleted": 0}

def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}] {msg}\n")
    print(f"DRIVE: {msg}")

def main():
    print("=== LATTICE DRIVE STARTED ===")
    while True:
        cmd = input("lattice> ").strip().split()
        if not cmd:
            continue
        action = cmd[0].lower()

        if action == "store" and len(cmd) > 2:
            file_in = cmd[1]
            start_p = int(cmd[2])
            subprocess.run(["python3", "odins_eye.py", "--mode", "encode", "--file", file_in, "--start_p", str(start_p), "--seed", "2026-03-15T04:41"])
            COUNT["saved"] += 1
            log(f"STORED {file_in} at {start_p}")

        elif action == "retrieve" and len(cmd) > 2:
            coord_file = cmd[1]
            out_name = cmd[2]
            lattice_file = coord_file.replace(".coord.json", ".lattice") if coord_file.endswith(".coord.json") else coord_file
            print(f"Retrieving {lattice_file} → {out_name}")
            subprocess.run(["python3", "odins_eye.py", "--mode", "decode", "--file", lattice_file])
            COUNT["read"] += 1
            log(f"RETRIEVED to {out_name}")

        elif action == "delete" and len(cmd) > 1:
            f = cmd[1]
            if os.path.exists(f):
                os.remove(f)
                COUNT["deleted"] += 1
                log(f"DELETED {f}")

        elif action == "status":
            print(f"Saved: {COUNT['saved']} | Reads: {COUNT['read']} | Writes: {COUNT['written']} | Deleted: {COUNT['deleted']}")

        elif action == "exit":
            print("Lattice drive shutdown")
            break

if __name__ == "__main__":
    main()
