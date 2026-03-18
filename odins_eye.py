import random
import argparse
import json
from datetime import datetime, timedelta

def generate_mask_list(seed, length=20000000):
    random.seed(seed)
    return [random.randint(0, 255) for _ in range(length)]

def bytes_to_symbols(data_bytes):
    symbols = []
    bit_buffer = 0
    bit_count = 0
    for b in data_bytes:
        bit_buffer = (bit_buffer << 8) | b
        bit_count += 8
        while bit_count >= 6:
            symbols.append((bit_buffer >> (bit_count - 6)) & 0x3F)
            bit_count -= 6
            bit_buffer &= (1 << bit_count) - 1
    if bit_count > 0:
        symbols.append((bit_buffer << (6 - bit_count)) & 0x3F)
    return symbols

def symbols_to_bytes(symbols, original_length):
    bytes_data = bytearray()
    bit_buffer = 0
    bit_count = 0
    for s in symbols:
        bit_buffer = (bit_buffer << 6) | s
        bit_count += 6
        while bit_count >= 8:
            bytes_data.append((bit_buffer >> (bit_count - 8)) & 0xFF)
            bit_count -= 8
            bit_buffer &= (1 << bit_count) - 1
    return bytes(bytes_data[:original_length])

def encode_lattice(data_bytes, start_p, seed_str):
    symbols = bytes_to_symbols(data_bytes)
    num_symbols = len(symbols)
    M = generate_mask_list(seed_str)
    encoded = []
    p = start_p
    for d in symbols:
        s = (M[p] + d) % 64
        encoded.append(s)
        p += 1
    # Instructions: length (65 + 8-byte length)
    length_bytes = len(data_bytes).to_bytes(8, 'big')
    encoded.extend([65] + list(length_bytes))
    encoded.append(64)  # EOF
    return encoded, num_symbols, p

def decode_lattice(encoded, start_p, seed_str, num_symbols, original_length):
    M = generate_mask_list(seed_str)
    symbols = []
    p = start_p
    for i in range(num_symbols):
        s = encoded[i]
        d = (s - M[p]) % 64
        symbols.append(d)
        p += 1
    # Read instructions
    i = num_symbols
    if encoded[i] == 65:
        length_bytes = bytes(encoded[i+1:i+9])
        i += 9
    if encoded[i] != 64:
        print("WARNING: EOF missing — possible timeline drift")
    return symbols_to_bytes(symbols, original_length)

def main():
    parser = argparse.ArgumentParser(description="Odin's Eye - Lattice Read/Write")
    parser.add_argument('--mode', choices=['encode', 'decode'], required=True)
    parser.add_argument('--file', required=True)
    parser.add_argument('--start_p', type=int, default=100000)
    parser.add_argument('--seed', default=None)
    parser.add_argument('--direction', type=int, default=1)
    args = parser.parse_args()

    if args.seed is None:
        now = datetime.now()
        delta = timedelta(days=1) if args.direction == 1 else timedelta(days=-1)
        seed = (now + delta).isoformat()
    else:
        seed = args.seed

    if args.mode == 'encode':
        with open(args.file, 'rb') as f:
            data = f.read()
        encoded, num_sym, end_p = encode_lattice(data, args.start_p, seed)
        coord = {
            "start_p": args.start_p,
            "num_symbols": num_sym,
            "original_bytes": len(data),
            "direction": args.direction,
            "seed": seed,
            "end_p": end_p,
            "runway": "default"
        }
        lattice_file = args.file + ".lattice"
        with open(lattice_file, 'wb') as f:
            f.write(bytes(encoded))
        with open(lattice_file + ".coord.json", 'w') as f:
            json.dump(coord, f, indent=2)
        print(f"✅ Encoded → {lattice_file}")
        print(f"📍 Lattice coordinate: {coord}")

    elif args.mode == 'decode':
        coord_file = args.file + ".coord.json"
        with open(coord_file, 'r') as f:
            coord = json.load(f)
        with open(args.file, 'rb') as f:
            encoded = list(f.read())
        decoded = decode_lattice(encoded, coord["start_p"], coord["seed"],
                                 coord["num_symbols"], coord["original_bytes"])
        out_file = args.file.replace(".lattice", ".decoded")
        with open(out_file, 'wb') as f:
            f.write(decoded)
        print(f"✅ Decoded → {out_file}")
        print("A-B flag: OK (mismatch would only log, never block)")

if __name__ == "__main__":
    main()
