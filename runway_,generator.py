import json
import subprocess
import os
from pathlib import Path

def load_config():
    config_file = "runway_generator.json"
    if not Path(config_file).exists():
        default = {
            "start_vr": 60000,
            "end_vr": 50000,
            "m": 32,
            "numerical_base": 64,
            "direction": "+",
            "files_per_d": 1,
            "output_prefix": "traffic_"
        }
        with open(config_file, "w") as f:
            json.dump(default, f, indent=2)
        print(f"Created default {config_file}")
    with open(config_file) as f:
        return json.load(f)

def main():
    config = load_config()
    print(f"🚀 Runway Generator — VR {config['start_vr']} → {config['end_vr']} | M={config['m']}")

    # Encode base payload once
    payload = "runway_payload.bin"
    if not Path(payload).exists():
        with open(payload, "wb") as f:
            f.write(b"Project Odin Temporal Traffic Payload\n" * 20000)
    subprocess.run([
        "python3", "bns.py",
        "--mode", "encode",
        "--file", payload,
        "--vl", "5000",
        "--vr", "5000",
        "--m", "100",
        "--json", "lloyd.json"
    ], check=True)

    # Decode many variations
    vr = config['start_vr']
    file_counter = 1
    while vr >= config['end_vr']:
        for d in range(config['m'] + 1):
            vl = vr - d
            for _ in range(config['files_per_d']):
                out_file = f"{config['output_prefix']}{file_counter:06d}.bin"
                temp_json = "temp_lloyd.json"
                coord = {
                    "vl": vl,
                    "vr": vr,
                    "m": config['m'],
                    "numerical_base": config['numerical_base'],
                    "direction": config['direction'],
                    "num_symbols": 1398102,
                    "original_bytes": 1048576
                }
                with open(temp_json, "w") as f:
                    json.dump(coord, f, indent=2)

                cmd = [
                    "python3", "bns.py",
                    "--mode", "decode",
                    "--decode_json", temp_json,
                    "--output_file", out_file
                ]
                print(f"Decoding VL={vl} VR={vr} D={d} → {out_file}")
                try:
                    subprocess.run(cmd, check=True)
                except:
                    print(f"  Failed for this coordinate")
                finally:
                    if os.path.exists(temp_json):
                        os.remove(temp_json)
                file_counter += 1
        vr -= 1

    print(f"✅ Runway complete — {file_counter-1} traffic files generated")
    print("   Copy some traffic_*.bin to Received_Traffic/ for mailbox to poll")

if __name__ == "__main__":
    main()
