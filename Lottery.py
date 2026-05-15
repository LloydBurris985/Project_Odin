import os
import json
import time
import datetime
import hashlib

# Create directories
for d in ['sent', 'receive', 'code_decode']:
    os.makedirs(d, exist_ok=True)

SAVE_FILE = 'save.json'

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

def encode_to_v(text):
    v = 0
    for c in str(text):
        if c.isdigit():
            v = v * 10 + int(c)
    return v

def generate_hash(msg):
    last_part = msg[-20:] if len(msg) > 20 else msg
    return hashlib.md5(f"{last_part}{len(msg)}".encode()).hexdigest()[:8]

def create_message(lottery_numbers):
    from_date = get_current_date()
    to_date = get_yesterday_date()          # Sending to yesterday
    raw_lottery = ''.join(c for c in lottery_numbers if c.isdigit())
    
    # Create hash based on lottery
    lottery_hash = generate_hash(raw_lottery)
    
    # Full message: from(6) + to(6) + hash(8) + lottery
    full_message = f"{from_date}{to_date}{lottery_hash}{raw_lottery}"
    
    # Encode entire message to V (search coordinate)
    search_v = encode_to_v(full_message)
    
    return full_message, search_v

def find_messages_with_prefix(prefix):
    """Only return messages that start with the correct 12-digit date prefix"""
    results = []
    for filename in os.listdir('code_decode'):
        path = os.path.join('code_decode', filename)
        try:
            with open(path, 'r') as f:
                content = f.read().strip()
            if content.startswith(prefix):
                results.append((content, path))
        except:
            continue
    return results

def load_state():
    if os.path.exists(SAVE_FILE):
        try:
            with open(SAVE_FILE) as f:
                return json.load(f)
        except:
            pass
    return {'v': None, 'last_step': 0}

def save_state(v, last_step):
    with open(SAVE_FILE, 'w') as f:
        json.dump({'v': v, 'last_step': last_step}, f)

def main():
    state = load_state()
    current_v = state.get('v')
    
    while True:
        print_header()
        print("1. Enter lottery numbers.")
        print("2. Get tomorrows lottery numbers.")
        print("3. Continue previous search.")
        print("4. Exit wormhole.")
        
        choice = input("\nChoice: ").strip()
        
        if choice == '1':
            print("\nEnter your lottery numbers (digits only):")
            numbers = input().strip()
            raw = ''.join(c for c in numbers if c.isdigit())
            if not raw:
                print("No numbers entered.")
                continue
            
            full_msg, search_v = create_message(numbers)
            
            with open(os.path.join('sent', f"sent_{search_v}.txt"), 'w') as f:
                f.write(full_msg)
            
            print(f"\nYour Temporal search coordinate is prepared:")
            print(search_v)
            save_state(search_v, 0)
            current_v = search_v
            input("\nPress Enter to return to menu...")
            
        elif choice in ('2', '3'):
            if not current_v:
                print("No search coordinate prepared. Use option 1 first.")
                continue
            
            hours = int(input("\nHow many hours do you want to search? "))
            end_time = time.time() + hours * 3600
            
            # Search header: from tomorrow + to today
            search_prefix = get_tomorrow_date() + get_current_date()
            
            print(f"\nSearching for messages with prefix: {search_prefix}")
            print("Only checking likely coordinates...\n")
            
            step = state.get('last_step', 0)
            if choice == '2' or step == 0:
                step = 1
            
            found = False
            checked = 0
            
            while time.time() < end_time and not found:
                for direction in [1, -1]:
                    test_v = current_v + direction * step
                    if test_v <= 0:
                        continue
                    
                    # Only look for messages with correct date prefix
                    matches = find_messages_with_prefix(search_prefix)
                    for msg, path in matches:
                        print("\n=== MESSAGE FROM TOMORROW FOUND! ===")
                        print(msg)
                        beep()
                        found = True
                        try:
                            os.remove(path)
                        except:
                            pass
                        break
                    if found:
                        break
                
                if not found:
                    step += 1
                    checked += 1
                    if checked % 30 == 0:
                        print(f"  Checked ±{step}...", end="\r")
            
            save_state(current_v, step)
            
            if found:
                print("\nMessage received successfully!")
            else:
                print(f"\nSearch ended. Checked up to ±{step}")
            
            input("\nPress Enter to return to menu...")
            
        elif choice == '4':
            print("Closing wormhole...")
            break
        else:
            print("Invalid choice.")

if __name__ == "__main__":
    main()
