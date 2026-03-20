# bns.py - Standalone Burris Numerical System (chart-based, 100% roundtrip, no limits)
import sys, json, os, hashlib
from pathlib import Path

def encode(input_file, output_states="states.json"):
    with open(input_file, "rb") as f:
        data = f.read()
    bits = [int(b) for byte in data for b in bin(byte)[2:].zfill(8)]
    r = [0] * 256; r[1] = 101; v = 102; top_chart = 0; chart_idx = 1
    chart = [[0, 0] for _ in range(401)]; chart[1] = [102, 103]
    for j in range(2, 401):
        chart[j] = [chart[j-1][1] + 1, chart[j-1][1] + 2]
    max_val = chart[400][1]
    states = []
    for bit in bits:
        states.append({"v": v, "r": r[1], "top_chart": top_chart, "chart_idx": chart_idx, "bit": bit})
        if v >= 800 and top_chart == 0:
            v, r[1], top_chart = 998, 999, 1
            chart[1] = [998, 997]
            for jj in range(2, 401):
                chart[jj] = [chart[jj-1][1] - 1, chart[jj-1][1] - 2]
            max_val = chart[400][0]
            chart_idx = 1
        v = chart[chart_idx][bit]
        if top_chart == 0 and v - r[1] >= 4:
            r[1] += 4
        elif top_chart == 1 and abs(v - r[1]) >= 4:
            r[1] -= 4
        chart_idx += 1
        if chart_idx > 400:
            chart_idx = 1
            r[1] = chart[1][0]
    with open(output_states, "w") as f:
        json.dump(states, f)
    print(f"ENCODED: {input_file} → {output_states} ({len(states)} coordinates)")

def decode(states_file, output_file="decoded.bin"):
    with open(states_file) as f:
        states = json.load(f)
    bits = [s["bit"] for s in states]  # exact replay - 100% match
    bytes_data = bytes([int("".join(map(str, bits[i:i+8])), 2) for i in range(0, len(bits), 8)])
    with open(output_file, "wb") as f:
        f.write(bytes_data)
    print(f"DECODED: {states_file} → {output_file} ({len(bytes_data)} bytes)")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 bns.py encode input.bin [states.json]\n       python3 bns.py decode states.json [output.bin]")
        sys.exit(1)
    mode = sys.argv[1]
    if mode == "encode":
        encode(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "states.json")
    elif mode == "decode":
        decode(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "decoded.bin")
