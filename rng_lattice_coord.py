import random
import json
import os

def generate(num=1, min_p=100000, max_p=1000000):
    coords = []
    for i in range(num):
        start = random.randint(min_p, max_p)
        coords.append({"id": i+1, "start_p": start, "seed": "2026-03-15T04:41"})
    with open("lattice_coords.json", "w") as f:
        json.dump(coords, f, indent=2)
    print(f"Generated {num} random lattice coordinates → lattice_coords.json")
    return coords

if __name__ == "__main__":
    num = int(input("How many random coordinates? (default 5): ") or 5)
    generate(num)
