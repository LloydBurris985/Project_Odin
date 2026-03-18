import subprocess
import os

def odins_encode(file_in, start_p=500000):
    subprocess.run(["python3", "odins_eye.py", "--mode", "encode", "--file", file_in, "--start_p", str(start_p), "--seed", "2026-03-15T04:41"])
    return file_in + ".lattice"

def odins_decode(lattice_file):
    subprocess.run(["python3", "odins_eye.py", "--mode", "decode", "--file", lattice_file])
    return lattice_file.replace(".lattice", ".decoded")

def airport_send(runway, file_path):
    with open("airport_command.txt", "a") as f:
        f.write(f"send {runway} {file_path}\n")
    print("Command sent to Airport")

def airport_poll():
    with open("airport_command.txt", "a") as f:
        f.write("poll\n")
    print("Poll command sent to Airport")

def rng_lattice_coord(num=5):
    subprocess.run(["python3", "rng_lattice_coord.py"], input=str(num), text=True)
    return "lattice_coords.json"

def status():
    print("✅ Phase 1 API ready — all features exposed (including RNG)")

if __name__ == "__main__":
    status()
