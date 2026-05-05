import os
import json
import time
import datetime
import hashlib
import shutil

# Directories
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
    return {'sent': 0, 'received': 0, 'latency': 0}

def update_health(sent_delta=0, received_delta=0, latency=0):
    health = load_health()
    health['sent'] += sent_delta
    health['received'] += received_delta
    if latency > 0:
        health['latency'] = latency
    with open(HEALTH_FILE, 'w') as f:
        json.dump(health, f)
    return health

def print_header():
    print("\n" + "*" * 30)
    print("*Welcome Time Traveler*")
    print("*" * 30)

def get_current_date():
    return datetime.datetime.now().strftime("%d%m%y")

def get_tomorrow_date():
    tomorrow = datetime.datetime.now() + datetime.timedelta(days=1)
    return tomorrow.strftime("%d%m%y")

def format_time():
    return datetime.datetime.now().strftime("%H%M")

def beep():
    print('\a')  # System beep

def encode_to_v(data_str):
    """Encode string of digits (0-9) to integer V using base 10 accumulation"""
    v = 0
    for char in data_str:
        if char.isdigit():
            n = int(char)
            v = v * 10 + n
    return v

def decode_from_v(v):
    """Decode V back to digit string"""
    if v == 0:
        return '0'
    digits = []
    while v > 0:
        digits.append(str(v % 10))
        v //= 10
    return ''.join(reversed(digits))

def generate_hash(message):
    """Simple hash from last part of encoded message and length"""
    msg_len = len(message)
    last_part = message[-10:] if len(message) > 10 else message
    hash_input = f"{last_part}{msg_len}".encode()
    return hashlib.md5(hash_input).hexdigest()[:8]

def create_message(lottery_numbers):
    """Create full message with header"""
    from_date = get_current_date()
    to_date = get_tomorrow_date()  # For sending to tomorrow
    sent_time = format_time()
    recv_time = "0000"
    
    # Unformatted raw content
    raw_lottery = ''.join(lottery_numbers.split())  # remove spaces etc.
    
    # Build message body
    message_body = f"{from_date}{to_date}{sent_time}{recv_time}{raw_lottery}"
    
    # Encode lottery backwards for hash
    lottery_encoded = encode_to_v(raw_lottery[::-1])  # backwards
    msg_hash = generate_hash(str(lottery_encoded) + str(len(raw_lottery)))
    
    # Full message with hash inserted
    full_message = f"{from_date}{to_date}{sent_time}{recv_time}{msg_hash}{raw_lottery}"
    
    return full_message, lottery_encoded

def encode_full_message(full_message):
    """Encode entire message to search coordinate V"""
    # Ensure only digits
    digit_str = ''.join([c for c in full_message if c.isdigit()])
    return encode_to_v(digit_str)

def decode_message(v):
    """Decode V back to message string"""
    digit_str = decode_from_v(v)
    return digit_str

def save_sent_message(full_message, v):
    filename = f"sent_{v}.txt"
    path = os.path.join('sent', filename)
    with open(path, 'w') as f:
        f.write(full_message)
    return path

def search_for_message(target_v, header_prefix):
    """Search in code_decode for matching header"""
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
    """Format and save received message"""
    # Parse
    from_date = content[0:6]
    to_date = content[6:12]
    sent_time = content[12:16]
    recv_time = content[16:20]
    hash_part = content[20:28]
    lottery = content[28:]
    
    formatted = f"""From: {from_date}
To: {to_date}
Sent: {sent_time}
Received: {format_time()}
Hash: {hash_part}

Lottery Numbers: {lottery}"""
    
    recv_path = os.path.join('receive', f"received_{original_v}.txt")
    with open(recv_path, 'w') as f:
        f.write(formatted)
    
    # Update time received placeholder in original if exists
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
            if not numbers or not all(c.isdigit() for c in numbers.replace(' ', '')):
                print("Invalid numbers. Only 0-9 allowed.")
                continue
            
            full_msg, lottery_v = create_message(numbers)
            search_v = encode_full_message(full_msg)
            
            save_sent_message(full_msg, search_v)
            print(f"\nYour Temporal search coordinate is prepared: {search_v}")
            
            # Save state
            save_state(search_v, 0)
            current_v = search_v
            search_counter = 0
            update_health(sent_delta=1)
            
            input("\nPress Enter to return to menu...")
            
        elif choice == '2':
            if not current_v:
                print("No search coordinate prepared. Use option 1 first.")
                continue
            
            hours = int(input("\nHow many hours do you want to search? "))
            search_seconds = hours * 3600
            start_time = time.time()
            
            header_from = get_tomorrow_date()
            header_to = get_current_date()
            header_prefix = f"{header_from}{header_to}"
            
            print(f"\nSearching for message from tomorrow with coordinate {current_v}...")
            
            found = False
            counter = 1
            
            while time.time() - start_time < search_seconds:
                # Try +counter
                test_v_plus = current_v + counter
                msg, path = search_for_message(test_v_plus, header_prefix)
                if msg:
                    formatted = process_found_message(msg, test_v_plus)
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
                
                # Try -counter
                test_v_minus = current_v - counter
                if test_v_minus > 0:
                    msg, path = search_for_message(test_v_minus, header_prefix)
                    if msg:
                        formatted = process_found_message(msg, test_v_minus)
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
                
                counter += 1
                if counter % 100 == 0:
                    print(f"  Searched ±{counter}...", end='\r')
            
            if not found:
                print("\nSearch time expired. No message found from tomorrow.")
                save_state(current_v, counter)
            else:
                save_state(current_v, 0)  # Reset counter on success?
            
            input("\nPress Enter to return to menu...")
            
        elif choice == '3':
            if not current_v:
                print("No previous search to continue.")
                continue
            
            hours = int(input("\nHow many hours do you want to search? "))
            search_seconds = hours * 3600
            start_time = time.time()
            
            header_from = get_tomorrow_date()
            header_to = get_current_date()
            header_prefix = f"{header_from}{header_to}"
            
            print(f"\nContinuing search from counter {search_counter} at coordinate {current_v}...")
            
            found = False
            counter = search_counter + 1
            
            while time.time() - start_time < search_seconds:
                test_v_plus = current_v + counter
                msg, path = search_for_message(test_v_plus, header_prefix)
                if msg:
                    formatted = process_found_message(msg, test_v_plus)
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
                
                test_v_minus = current_v - counter
                if test_v_minus > 0:
                    msg, path = search_for_message(test_v_minus, header_prefix)
                    if msg:
                        formatted = process_found_message(msg, test_v_minus)
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
                
                counter += 1
                if counter % 100 == 0:
                    print(f"  Searched ±{counter}...", end='\r')
            
            if not found:
                print("\nSearch time expired.")
                save_state(current_v, counter)
            else:
                save_state(current_v, 0)
            
            input("\nPress Enter to return to menu...")
            
        elif choice == '4':
            print("Closing wormhole...")
            break
        
        else:
            print("Invalid choice.")

if __name__ == "__main__":
    main()
