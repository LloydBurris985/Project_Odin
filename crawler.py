import os
import argparse
import hashlib
import math
import subprocess

def shannon_entropy(data):
    if not data: return 0
    freq = {}
    for b in data:
        freq[b] = freq.get(b, 0) + 1
    entropy = 0
    for c in freq.values():
        p = c / len(data)
        entropy -= p * math.log2(p)
    return entropy

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lattice_file", required=True)
    parser.add_argument("--start_p", type=int, required=True)
    parser.add_argument("--length", type=int, required=True)
    parser.add_argument("--any", action="store_true")
    parser.add_argument("--min_entropy", type=float, default=4.0)  # new entropy param
    args = parser.parse_args()

    subprocess.run(["python3", "odins_eye.py", "--mode", "decode", "--file", args.lattice_file])
    decoded_file = args.lattice_file.replace(".lattice", ".decoded")
    if not os.path.exists(decoded_file):
        print(0)
        return
    with open(decoded_file, "rb") as f:
        data = f.read()
    ent = shannon_entropy(data)
    if args.any or ent >= args.min_entropy:
        print(1)
    else:
        print(0)

if __name__ == "__main__":
    main()
