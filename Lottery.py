import os
import datetime
import hashlib                              import random

os.makedirs("sent_received", exist_ok=True)
os.makedirs("code_decode", exist_ok=True)
LOG_FILE = "floyd_system.log"               
def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    with open(LOG_FILE, "a") as f:
        f.write(f"{ts} - {msg}\n")              print(msg)

def simple_hash(s):
    return hashlib.sha256(s.encode()).hexdigest()[:8]

def encode_message(msg, base=10):
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
        final_v, length = encode_message(full_msg)
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

        msg_len = len(sent_msg)
        header = sent_msg[:16]
        lottery_part = sent_msg[20:]

        # Bottom coordinate (0-padded + hash)
        bottom_header = header + "0000" + lottery_part
        bottom_hash = int(simple_hash(bottom_header), 16) % 100000000
        bottom_str = bottom_header + f"{bottom_hash:08d}"
        bottom_coord = encode_message(bottom_str)[0]

        # Top coordinate (9-padded + hash)
        top_header = header + "9999" + lottery_part
        top_hash = int(simple_hash(top_header), 16) % 100000000
        top_str = top_header + f"{top_hash:08d}"
        top_coord = encode_message(top_str)[0]

        print(f"Bottom coordinate: {bottom_coord}")
        print(f"Top coordinate:    {top_coord}")
        log(f"Search range: {bottom_coord} to {top_coord}")

        found = False
        for _ in range(99999999):
            coord = random.randint(min(bottom_coord, top_coord), max(bottom_coord, top_coord))
            decoded = two_pass_decode(coord, 10)
            if len(decoded) != msg_len:
                continue

            from_date = decoded[0:6]
            to_date = decoded[6:12]
            time_sent_d = decoded[12:16]
            lottery_d = decoded[20:]

            embedded_hash = decoded[-8:] if len(decoded) > 8 else ""
            expected_hash = simple_hash(decoded[:-8])

            log(f"Coord {coord} → from={from_date} to={to_date} lottery={lottery_d} hash={embedded_hash}")

            tomorrow_exp = (datetime.datetime.now() + datetime.timedelta(days=1)).strftime("%d%m%y")
            today_exp = datetime.datetime.now().strftime("%d%m%y")

            if from_date == tomorrow_exp and to_date == today_exp and embedded_hash == expected_hash:
                received_time = datetime.datetime.now().strftime("%H%M")
                final_msg = decoded[:16] + received_time + decoded[20:-8]

                with open(f"sent_received/received_{today_exp}_{received_time}.txt", "w") as f:
                    f.write(final_msg)

                print("\n🎉 MESSAGE FROM TOMORROW FOUND!")
                print(f"Date from: {from_date}")
                print(f"Date to: {to_date}")
                print(f"Time sent: {time_sent_d}")
                print(f"Time received: {received_time}")
                print(f"Lottery: {lottery_d}")

                try:
                    os.system("termux-vibrate -d 800")
                except:
                    pass

                log(f"FOUND at coord {coord}")
                found = True
                break

        if not found:
            log("Search completed - no message found this cycle.")
