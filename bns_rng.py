# bns_rng.py - Complete BNS RNG for Project Odin (fixed for Termux)
import hashlib
import time
import random  # ← this was missing
from typing import List

class BNSRNG:
    """Bounded Number System RNG - generates mask list for message encoding."""

    LOW = 10000
    HIGH = 99999
    CENTER = 32
    STEP_FACTOR = 8

    def __init__(self, start_mask: int = 50000):
        self.start_mask = start_mask
        self.log_file = "bns_rng.log"

    def log(self, msg: str):
        with open(self.log_file, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")

    def generate_mask_list(self, message: bytes) -> List[int]:
        """Input starting mask + message bytes → list of masks used."""
        self.log(f"Starting BNS RNG for {len(message)} byte message")
        # Seed from message hash + start_mask for perfect reproducibility
        seed = int(hashlib.sha256(message).hexdigest(), 16) % (self.HIGH - self.LOW)
        rng = list(range(self.LOW, self.HIGH, self.STEP_FACTOR))
        random.seed(seed + self.start_mask)
        masks = []
        for i in range(len(message) * 2):  # generous for symbols + instructions
            mask = rng[random.randint(0, len(rng)-1)]
            masks.append(mask)
        self.log(f"Generated {len(masks)} masks")
        return masks

# Quick test when run directly
if __name__ == "__main__":
    rng = BNSRNG(start_mask=50000)
    test_msg = b"Project Odin Test Message 123"
    masks = rng.generate_mask_list(test_msg)
    print(f"✅ BNS RNG Test: {len(masks)} masks generated")
    print(f"First 10 masks: {masks[:10]}")
    print("Log written to bns_rng.log")
