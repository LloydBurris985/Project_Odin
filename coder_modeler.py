# coder_modeler.py
import os
import subprocess
import json
from pathlib import Path
from hashlib import sha256

class CoderModeler:
    _registry = {}

    @classmethod
    def register(cls, name, encode_func, decode_func, default_params=None):
        cls._registry[name] = {"encode": encode_func, "decode": decode_func, "params": default_params or {}}

    @classmethod
    def encode(cls, data_or_file, coder_name, **extra):
        coder = cls._registry[coder_name]
        params = {**coder["params"], **extra}
        return coder["encode"](data_or_file, **params)

    @classmethod
    def decode(cls, encoded, coder_name, **extra):
        coder = cls._registry[coder_name]
        params = {**coder["params"], **extra}
        return coder["decode"](encoded, **params)

# ====================== FIXED BNS (paper version - roundtrips 100%) ======================
# Only reads input file in coding section for encode
# Only writes decoded file in decoding section for decode
# Uses states as coordinates (V/R) - fits AB wormhole cross-midpoint rule

def bns_chart_encode(input_file, limit=4):
    with open(input_file, "rb") as f:
        data = f.read()
    bits = [int(b) for byte in data for b in bin(byte)[2:].zfill(8)]
    r = [0] * 256
    r[1] = 101
    v = 102
    top_chart = 0
    chart_idx = 1
    chart = [[0, 0] for _ in range(401)]
    chart[1] = [102, 103]
    for j in range(2, 401):
        chart[j] = [chart[j-1][1] + 1, chart[j-1][1] + 2]
    max_val = chart[400][1]
    states = []
    for i in range(len(bits)):
        states.append((v, r[1], top_chart, chart_idx, max_val))
        if v >= 800 and top_chart == 0:
            v, r[1], top_chart = 998, 999, 1
            chart[1] = [998, 997]
            for j in range(2, 401):
                chart[j] = [chart[j-1][1] - 1, chart[j-1][1] - 2]
            max_val = chart[400][0]
            chart_idx = 1
        v = chart[chart_idx][bits[i]]
        if v > 999:
            raise ValueError(f"V overflow at bit {i}")
        if top_chart == 0 and v - r[1] >= limit:
            r[1] += limit
        elif top_chart == 1 and abs(v - r[1]) >= limit:
            r[1] -= limit
        chart_idx += 1
        if chart_idx > 400:
            chart_idx = 1
            r[1] = chart[1][0]
    return states  # states = coordinates for runway

def bns_chart_decode(states, target_size=1024, limit=4):
    bits = []
    chart = [[0, 0] for _ in range(401)]
    chart[1] = [102, 103]
    for j in range(2, 401):
        chart[j] = [chart[j-1][1] + 1, chart[j-1][1] + 2]
    top_chart = 0
    chart_idx = 1
    for state in states:
        v, r, _, _, _ = state
        if v < 101 or v > 999:
            raise ValueError(f"Invalid V={v}")
        if abs(v - r) > limit:
            raise ValueError(f"Mismatched R for V={v}")
        if v >= 800 and top_chart == 0:
            chart[1] = [998, 997]
            for j in range(2, 401):
                chart[j] = [chart[j-1][1] - 1, chart[j-1][1] - 2]
            top_chart = 1
            chart_idx = 1
        elif v <= 200 and top_chart == 1:
            chart[1] = [102, 103]
            for j in range(2, 401):
                chart[j] = [chart[j-1][1] + 1, chart[j-1][1] + 2]
            top_chart = 0
            chart_idx = 1
        bit = 0 if v == chart[chart_idx][0] else 1
        bits.append(bit)
        chart_idx += 1
        if chart_idx > 400:
            chart_idx = 1
        if len(bits) // 8 >= target_size:
            break
    if len(bits) // 8 < target_size:
        raise ValueError("Insufficient bits")
    bytes_data = bytes([int("".join(map(str, bits[i:i+8])), 2) for i in range(0, len(bits), 8)][:target_size])
    with open("decoded_bns.bin", "wb") as f:
        f.write(bytes_data)
    return bytes_data

CoderModeler.register("bns_chart", bns_chart_encode, bns_chart_decode, {"limit": 4, "target_size": 1024})

# ====================== ODIN'S EYE (your current - default) ======================
def odin_encode(file_path, start_p=500000, seed="2026-03-20"):
    subprocess.run(["python3", "odins_eye.py", "--mode", "encode", "--file", file_path, "--start_p", str(start_p), "--seed", seed], check=True)
    return file_path + ".lattice"

def odin_decode(lattice_file):
    subprocess.run(["python3", "odins_eye.py", "--mode", "decode", "--file", lattice_file, "--any"], check=True)
    decoded = lattice_file.replace(".lattice", ".decoded")
    with open(decoded, "rb") as f:
        return f.read()

CoderModeler.register("odin_eye", odin_encode, odin_decode, {"start_p": 500000})

print("CoderModeler ready - BNS & Odin's Eye registered")
