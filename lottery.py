import os
import datetime
import hashlib

os.makedirs("sent_received", exist_ok=True)
LOG_FILE = "floyd_system.log"

def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    with open(LOG_FILE, "a") as f:
        f.write(f"{ts} - {msg}\n")
    print(msg)

def simple_hash(s):
    return hashlib.sha256(s.encode()).hexdigest()[:8]

def encode_message(msg, base=10):
    msg = msg[::-1]                    # encode from back to front
    v = 0
    for char in msg:
        n = int(char)
        v = v * base + n
    return v, len(msg)

def decode_message(v, base=10):
    if v == 0:
        return ""
    parts = []
    while v > 0:
        n = v % base
        parts.append(str(n))
        v = v // base
    return "".join(reversed(parts))

def two_pass_decode(v, base=10):
    tmp = decode_message(v, base)
    v2, _ = encode_message(tmp, base)
    return decode_message(v2, base)

while True:
    print("\n---------------------Welcome Time Traveler----------------------------")
    print("1. Send todays lottery numbers.")
    print("2. Check tomorrows numbers.")
    print("3. Exit Wormhole.")
    choice = input("Enter choice: ").strip()

    if choice == "3":
        print("Exiting wormhole.")
        break

    if choice == "1":
        now = datetime.datetime.now()
        today = now.strftime("%d%m%y")
        yesterday = (now - datetime.timedelta(days=1)).strftime("%d%m%y")
        time_sent = now.strftime("%H%M")
        lottery = input("Enter lottery numbers (no spaces): ").strip()

        full_msg = f"{today}{yesterday}{time_sent}0000{lottery}"
        final_v, _ = encode_message(full_msg)
        coordinate = final_v + 10000

        print(f"Message: {full_msg}")
        print(f"Final V: {final_v}")
        print(f"Search Coordinate: {coordinate}")

        with open(f"sent_received/sent_{today}_{time_sent}.txt", "w") as f:
            f.write(full_msg)

        log(f"SENT | {full_msg} | Final V: {final_v}")

    elif choice == "2":
        log("=== Time Hacker Search for Tomorrow's Message ===")
        
        sent_files = [f for f in os.listdir("sent_received") if f.startswith("sent_")]
        if not sent_files:
            print("No sent messages yet.")
            continue

        sent_file = os.path.join("sent_received", max(sent_files))
        with open(sent_file) as f:
            sent_msg = f.read().strip()

        exact_coord = encode_message(sent_msg)[0]
        search_min = exact_coord - 50000
        search_max = exact_coord + 50000
        
        print(f"Searching around sent coordinate: {exact_coord:,}")
        print(f"Range: {search_min:,} to {search_max:,}  (±50,000)")
        log(f"Search range: {search_min} to {search_max} (width 100k)")
        
        msg_len = len(sent_msg)
        found = False
        checked = 0

        for coord in range(search_min, search_max + 1):
            checked += 1
            if checked % 10000 == 0:
                print(f"Progress: {checked:,}/{100000} checked...")

            decoded = two_pass_decode(coord, 10)
            
            if len(decoded) != msg_len:
                continue
                
            if decoded == sent_msg:
                continue  # Skip the sent message itself

            # Better check: must start with tomorrow's date + today's date
            if decoded.startswith(sent_msg[:12]):
                # Optional: verify hash if you want to add it later
                print("\n🎉 MESSAGE FROM TOMORROW FOUND!")
                print(f"Coordinate: {coord:,}")
                print(f"Message: {decoded}")
                
                received_time = datetime.datetime.now().strftime("%H%M")
                # Rebuild final message with current receive time
                final_msg = decoded[:16] + received_time + decoded[20:]
                
                today_str = datetime.datetime.now().strftime("%d%m%y")
                with open(f"sent_received/received_{today_str}_{received_time}.txt", "w") as f:
                    f.write(final_msg)
                
                log(f"FOUND at coord {coord}")
                found = True
                break
        
        if not found:
            print("No new message found in the search window.")
            log("Search completed - no new message found.")
