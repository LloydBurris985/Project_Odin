import os
import time
import sys
from ingest_manager import scan_and_register_staging
from tokenizer_engine import process_unindexed_text
from orchestrator_core import AgentOrchestrator

def run_daemon_loop(interval_seconds=5):
    print("====================================================")
    print("[!] ODIN AI DAEMON ACTIVATED - BACKGROUND CORES LIVE")
    print(f"[-] Monitoring loop set to check every {interval_seconds} seconds.")
    print("====================================================")
    
    orchestrator = AgentOrchestrator()
    cycle_count = 0

    try:
        while True:
            cycle_count += 1
            # print(f"\n[-] Starting Automation Cycle #{cycle_count}...", end="\r")
            
            # Step 1: Scan staging directory and hash new assets into SQLite
            scan_and_register_staging()
            
            # Step 2: Grab any Unprocessed text files and tokenize them
            process_unindexed_text()
            
            # Step 3: Trigger orchestrator to check for pending processed assets
            # We bypass the query prompt and instruct agents directly via backend loops
            orchestrator.execute_command("Show me the book profile summaries inside the database")
            
            # Optional: Add image automated processing triggers here when ready
            
            # Sleep until the next sweep
            time.sleep(interval_seconds)
            
    except KeyboardInterrupt:
        print("\n[!] Daemon manually suspended. Offloading local memory pools safely.")
        print("[*] System Idle.")
        sys.exit(0)

if __name__ == "__main__":
    run_daemon_loop(interval_seconds=5)
