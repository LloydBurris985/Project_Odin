import os
import json
import time
import datetime
import hashlib

# Create directories
DIRS = ['sent', 'receive', 'code_decode']
for d in DIRS:
    os.makedirs(d, exist_ok=True)

SAVE_FILE = 'save.json'
HEALTH_FILE = 'health.json'

def load_save():
    if os.path.exists(SAVE_FILE):
        try:
            with open(SAVE_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {'v': None, 'counter': 0}

def save_state(v, counter):
    with open(SAVE_FILE, 'w') as f:
        json.dump({'v': v, 'counter': counter}, f)

def load_health():
    if os.path.exists(HEALTH_FILE):
        try:
            with open(HEALTH_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {'sent': 0, 'received': 0}

def update_health(sent_delta=0, received_delta=0):
    health = load_health()
    health['sent'] += sent_delta
    health['received'] += received_delta
    with open(HEALTH_FILE, 'w') as f:
        json.dump(health, f)
    return health

def print_header():
    print("\n**************************")
    print("*Welcome Time Traveler*")
    print("**************************")

def get_current_date():
    return datetime.datetime.now().strftime("%d%m%y")

def get_yesterday_date():
    yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
    return yesterday.strftime("%d%m%y")

def get_tomorrow_date():
    tomorrow = datetime.datetime.now() + datetime.timedelta(days=1)
    return tomorrow.strftime("%d%m%y")

def format_time():
    return datetime.datetime.now().strftime("%H%M")

def beep():
    print('\a')

def encode_to_v(data_str):
    v = 0
    for char in data_str:
        if char.isdigit():
            v = v * 10 + int(char)
    return v

def generate_hash(message):
    msg_len = len(message)
    last_part = message[-10:] if len(message) > 10 else message
    hash_input = f"{last_part}{msg_len}".encode()
    return hashlib.md5(hash_input).hexdigest()[:8]

def create_message(lottery_numbers):
    from_date = get_current_date()
    to_date = get_yesterday_date()          # Sending to yesterday
    sent_time = format_time()
    recv_time = "0000"
    
    raw_lottery = ''.join(c for c in lottery_numbers if c.isdigit())
    lottery_encoded = encode_to_v(raw_lottery[::-1])
    msg_hash = generate_hash(str(lottery_encoded) + str(len(raw_lottery)))
    
    full_message = f"{from_date}{to_date}{sent_time}{recv_time}{msg_hash}{raw_lottery}"
    return full_message, lottery_encoded

def encode_full_message(full_message):
    digit_str = ''.join(c for c in full_message if c.isdigit())
    return encode_to_v(digit_str)

def search_for_message(target_v, header_prefix):
    for filename in os.listdir('code_decode'):
        path = os.path.join('code_decode', filename)
        try:
            with open(path, 'r') as f:
                content = f.read().strip()
            if content.startswith(header_prefix):
                return content, path
        except:
            continue
    return None, None

def process_found_message(content, original_v):
    from_date = content[0:6]
    to_date = content[6:12]
    sent_time = content[12:16]
    recv_time = content[16:20]
    msg_hash = content[20:28]
    lottery = content[28:]
    
    formatted = f"""From: {from_date}
To: {to_date}
Sent: {sent_time}
Received: {format_time()}
Hash: {msg_hash}

Lottery Numbers: {lottery}"""
    
    with open(os.path.join('receive', f"received_{original_v}.txt"), 'w') as f:
        f.write(formatted)
    return formatted

def main():
    save_data = load_save()
    current_v = save_data.get('v')
    search_counter = save_data.get('counter', 0)
    
    while True:
        print_header()
        print("1. Enter lottery numbers.")
        print("2. Get tomorrows lottery numbers.")
        print("3. Continue previous search.")
        print("4. Exit wormhole.")
        
        choice = input("\nChoice: ").strip()
        
        if choice == '1':
            print("\nPlease enter your lottery numbers (digits 0-9 only):")
            numbers = input().strip()
            raw = ''.join(c for c in numbers if c.isdigit())
            if not raw:
                print("No valid numbers entered.")
                continue
            
            full_msg, _ = create_message(numbers)
            search_v = encode_full_message(full_msg)
            
            with open(os.path.join('sent', f"sent_{search_v}.txt"), 'w') as f:
                f.write(full_msg)
            
            print(f"\nYour Temporal search coordinate is prepared: {search_v}")
            save_state(search_v, 0)
            current_v = search_v
            update_health(sent_delta=1)
            input("\nPress Enter to return to menu...")
            
        elif choice in ('2', '3'):
            if not current_v:
                print("No search coordinate prepared. Use option 1 first.")
                continue
            
            hours = int(input("\nHow many hours do you want to search? "))
            search_seconds = hours * 3600
            start_time = time.time()
            
            header_from = get_tomorrow_date()
            header_to = get_current_date()
            header_prefix = f"{header_from}{header_to}"
            
            if choice == '3':
                print(f"\nContinuing search from counter {search_counter}...")
                step = search_counter if search_counter > 0 else 1
            else:
                print(f"\nStarting search from coordinate {current_v}...")
                step = 1
            
            found = False
            print("Searching for message from tomorrow...")
            
            while time.time() - start_time < search_seconds:
                for direction in [1, -1]:
                    test_v = current_v + direction * step
                    if test_v <= 0:
                        continue
                    msg, path = search_for_message(test_v, header_prefix)
                    if msg:
                        formatted = process_found_message(msg, test_v)
                        print("\n=== MESSAGE FROM TOMORROW FOUND! ===")
                        print(formatted)
                        beep()
                        found = True
                        try:
                            os.remove(path)
                        except:
                            pass
                        update_health(received_delta=1)
                        break
                if found:
                    break
                
                step *= 2   # Exponential search: 1, 2, 4, 8, 16...
                if step % 10000 == 0:
                    print(f"  Reached step {step:,}...", end='\r')
            
            save_state(current_v, step)
            
            if not found:
                print(f"\nSearch time expired. Next step is around {step:,}")
            else:
                print("\nMessage successfully received!")
            
            input("\nPress Enter to return to menu...")
            
        elif choice == '4':
            print("Closing wormhole...")
            break
        else:
            print("Invalid choice.")

if __name__ == "__main__":
    main()
