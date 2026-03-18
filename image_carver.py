import subprocess
import os
import random

MAX_IMAGES = 5

def main():
    start_p = int(input("Enter lattice coordinates to decode: ") or 500000)
    size = int(input("Enter size of image to write (bytes): ") or 1024*1024)
    name = input("Enter name of image (e.g. carved.jpg): ") or "carved.jpg"
    
    found = 0
    print("Writing image... (dynamic update)")
    for i in range(30):
        if found >= MAX_IMAGES: break
        subprocess.run(["python3", "odins_eye.py", "--mode", "encode", "--file", "temp.bin", "--start_p", str(start_p + i), "--seed", "2026-03-15T04:41"])
        subprocess.run(["python3", "odins_eye.py", "--mode", "decode", "--file", "temp.bin.lattice"])
        decoded = "temp.bin.decoded"
        if os.path.exists(decoded):
            carved = f"{name}_{found+1}.jpg"
            with open(decoded, "rb") as f:
                data = f.read(size)
            with open(carved, "wb") as f:
                f.write(data)
            print(f"✅ IMAGE WRITTEN → {carved} (use binwalk or strings to carve)")
            found += 1
    print("Done. Use third-party tools on the .jpg files.")

if __name__ == "__main__":
    main()
