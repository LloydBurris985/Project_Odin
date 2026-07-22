import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STAGING_DIR = os.path.join(BASE_DIR, "odin_ai", "staging")

def dispatch_muninn(content, filename=None):
    """
    Simulates Muninn dropping raw text/memory straight into the 
    Odin AI staging matrix for automated background ingestion.
    """
    if not os.path.exists(STAGING_DIR):
        os.makedirs(STAGING_DIR)
        
    # If no filename is provided, generate a unique timestamped file
    if not filename:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"muninn_memory_{timestamp}.txt"
    elif not filename.endswith(".txt"):
        filename += ".txt"
        
    target_path = os.path.join(STAGING_DIR, filename)
    
    try:
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(content.strip())
        print(f"[+] Muninn successfully delivered memory asset: {filename} ──► Staging")
    except Exception as e:
        print(f"[!] Muninn flight interrupted. Failed to write text: {e}")

if __name__ == "__main__":
    # Allows you to quickly pass text via command line arguments
    if len(sys.argv) > 1:
        raw_text = " ".join(sys.argv[1:])
        dispatch_muninn(raw_text)
    else:
        print("[-] Muninn is waiting for input. Provide text to cast into memory.")
        print("Usage: python muninn_text.py 'Your text content here'")
