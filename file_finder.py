import subprocess
import os
import random

def main():
    start_p = int(input("Enter lattice coordinates to start decode: ") or 500000)
    ext = input("Enter file extension (e.g. txt): ") or "txt"
    size = int(input("Enter file size in bytes: ") or 1024)
    num_files = int(input("Number of files to find: ") or 5)
    
    found = 0
    buffer = b""
    current_p = start_p
    
    print("Filling buffer and searching...")
    while found < num_files:
        # Generate & decode chunk with Odins Eye
        dummy = "temp.bin"
        with open(dummy, "w") as f: f.write("dummy")
        subprocess.run(["python3", "odins_eye.py", "--mode", "encode", "--file", dummy, "--start_p", str(current_p), "--seed", "2026-03-15T04:41"])
        subprocess.run(["python3", "odins_eye.py", "--mode", "decode", "--file", dummy + ".lattice"])
        
        with open(dummy + ".decoded", "rb") as f:
            chunk = f.read()
        buffer += chunk
        
        # Check buffer
        if len(buffer) >= size and buffer[:len(ext.encode())] == ext.encode():
            filename = f"{found+1}.{ext}"
            with open(filename, "wb") as f:
                f.write(buffer[:size])
            print(f"✅ Found & wrote {filename}")
            found += 1
            buffer = buffer[1:]  # discard 1 byte, add next
        else:
            buffer = buffer[1:] if buffer else b""
        
        current_p += 1
        os.remove(dummy + ".lattice") if os.path.exists(dummy + ".lattice") else None
    
    print("Search complete.")

if __name__ == "__main__":
    main()
