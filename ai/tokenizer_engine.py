import os
import re
import collections
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "odin_ai", "db", "odin_archive.db")
STAGING_DIR = os.path.join(BASE_DIR, "odin_ai", "staging")

class OdinTokenizer:
    def __init__(self, vocab_size=5000):
        self.vocab_size = vocab_size
        # Seed the base vocabulary with standard ASCII
        self.vocab = {i: bytes([i]) for i in range(256)}
        # Add a couple of core structural utility tokens
        self.special_tokens = {256: b"<PAD>", 257: b"<UNK>"}
        self.vocab.update(self.special_tokens)
        
    def _get_stats(self, ids):
        counts = collections.defaultdict(int)
        for pair in zip(ids, ids[1:]):
            counts[pair] += 1
        return counts

    def _merge(self, ids, pair, idx):
        new_ids = []
        i = 0
        while i < len(ids):
            if i < len(ids) - 1 and ids[i] == pair[0] and ids[i+1] == pair[1]:
                new_ids.append(idx)
                i += 2
            else:
                new_ids.append(ids[i])
                i += 1
        return new_ids

    def train_on_text(self, text):
        """Trains the vocabulary directly on custom carved inputs completely offline."""
        print(f"[-] Training isolated BPE vocabulary up to size {self.vocab_size}...")
        raw_bytes = text.encode("utf-8")
        ids = list(raw_bytes)
        
        num_merges = self.vocab_size - len(self.vocab)
        merges = {}
        
        for i in range(num_merges):
            stats = self._get_stats(ids)
            if not stats:
                break
            top_pair = max(stats, key=stats.get)
            idx = 258 + i
            ids = self._merge(ids, top_pair, idx)
            merges[top_pair] = idx
            self.vocab[idx] = self.vocab[top_pair[0]] + self.vocab[top_pair[1]]
            
        self.merges = merges
        print("[+] Vocabulary compilation completed successfully.")

    def encode(self, text):
        """Converts raw text strings into arrays of numerical machine tokens."""
        raw_bytes = text.encode("utf-8")
        ids = list(raw_bytes)
        if not hasattr(self, 'merges'):
            return ids
            
        while len(ids) >= 2:
            stats = self._get_stats(ids)
            pair = min(stats, key=lambda p: self.merges.get(p, float('inf')))
            if pair not in self.merges:
                break
            idx = self.merges[pair]
            ids = self._merge(ids, pair, idx)
        return ids

def process_unindexed_text():
    """Scans SQLite for registered unindexed text data to process."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT id, filename FROM file_archive 
        WHERE file_type = 'txt' AND processing_status = 'Unprocessed'
    """)
    rows = cursor.fetchall()
    
    if not rows:
        print("[*] No unprocessed text documents found in the archive index.")
        conn.close()
        return

    tokenizer = OdinTokenizer()

    for row in rows:
        file_id, filename = row
        file_path = os.path.join(STAGING_DIR, filename)
        
        print(f"[-] Processing data structure for target: {filename}")
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            
        if not content.strip():
            continue
            
        # Run local training loop & produce structured mathematical token stream
        tokenizer.train_on_text(content)
        tokens = tokenizer.encode(content)
        
        # Log a snippet profile into SQLite and update state to 'Indexed'
        summary_placeholder = f"Tokens Generated: {len(tokens)} | Sample Segment: {content[:100]}..."
        
        cursor.execute("""
            UPDATE file_archive 
            SET profile_summary = ?, processing_status = 'Indexed'
            WHERE id = ?
        """, (summary_placeholder, file_id))
        
        print(f"[+] Successfully indexed and mapped vectors for asset ID: {file_id}")
        
    conn.commit()
    conn.close()

if __name__ == "__main__":
    process_unindexed_text()
