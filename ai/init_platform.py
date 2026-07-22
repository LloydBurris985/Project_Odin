import os
import sqlite3
from datetime import datetime

# ==========================================
# CONFIGURATION: DIRECTORY PATHS
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DIRS = {
    "staging": os.path.join(BASE_DIR, "odin_ai", "staging"),
    "models": os.path.join(BASE_DIR, "odin_ai", "models"),
    "db": os.path.join(BASE_DIR, "odin_ai", "db"),
}

DB_PATH = os.path.join(DIRS["db"], "odin_archive.db")

def setup_environment():
    print("[-] Initializing Project Odin AI Environment...")
    
    # 1. Create Directories safely
    for name, path in DIRS.items():
        if not os.path.exists(path):
            os.makedirs(path)
            print(f"[+] Created directory: {path}")
        else:
            print(f"[*] Directory exists: {path}")

    # 2. Initialize SQLite Database
    print(f"[-] Connecting to database at: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Create the decoupled file-tracking schema
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS file_archive (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            file_type TEXT NOT NULL,
            file_hash TEXT UNIQUE NOT NULL,
            file_size_bytes INTEGER NOT NULL,
            date_discovered TEXT NOT NULL,
            origin_tag TEXT,
            profile_summary TEXT,
            processing_status TEXT DEFAULT 'Unprocessed'
        )
    """)
    
    conn.commit()
    conn.close()
    print("[+] Database schema verified and locked successfully.")
    print("\n[!] Phase 1 Complete. Ready for data ingestion logic.")

if __name__ == "__main__":
    setup_environment()
