import os
import hashlib
import sqlite3
from datetime import datetime

# Grab paths relative to this script's independent directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STAGING_DIR = os.path.join(BASE_DIR, "odin_ai", "staging")
DB_PATH = os.path.join(BASE_DIR, "odin_ai", "db", "odin_archive.db")

def calculate_sha256(file_path):
    """Generates a unique fingerprint for the file to prevent duplicates."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        # Read in 64KB chunks to optimize local memory usage
        for byte_block in iter(lambda: f.read(65536), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def scan_and_register_staging():
    print("[-] Scanning isolated staging directory for new carved outputs...")
    
    if not os.path.exists(STAGING_DIR):
        print(f"[!] Error: Staging directory not found at {STAGING_DIR}")
        return

    files = [f for f in os.listdir(STAGING_DIR) if os.path.isfile(os.path.join(STAGING_DIR, f))]
    
    if not files:
        print("[*] Staging folder is currently empty. Drop carved files here to test.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    new_registrations = 0

    for filename in files:
        file_path = os.path.join(STAGING_DIR, filename)
        file_size = os.path.getsize(file_path)
        
        # Extract extension as type (e.g., 'jpg', 'txt')
        _, file_extension = os.path.splitext(filename)
        file_type = file_extension.lower().replace(".", "") or "unknown"
        
        # Calculate file signature
        file_hash = calculate_sha256(file_path)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            # Check if this file hash already exists in our offline index
            cursor.execute("""
                INSERT INTO file_archive (filename, file_type, file_hash, file_size_bytes, date_discovered)
                VALUES (?, ?, ?, ?, ?)
            """, (filename, file_type, file_hash, file_size, timestamp))
            
            print(f"[+] Registered New Asset: {filename} [{file_type.upper()}]")
            new_registrations += 1
            
        except sqlite3.IntegrityError:
            # Caught duplicate hash protection rule
            print(f"[*] Skipped (Duplicate Signature): {filename}")

    conn.commit()
    conn.close()
    print(f"\n[!] Scan complete. Successfully cataloged {new_registrations} new files.")

if __name__ == "__main__":
    scan_and_register_staging()
