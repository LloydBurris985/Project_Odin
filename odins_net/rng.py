from datetime import datetime
from typing import Optional

class BNSRNG:
    """Bounded Number System pseudo-random generator seeded by time or chain ID."""
    
    def __init__(self, seed: Optional[str] = None, base=10, limit=4, low=20000, high=39999):
        if seed is None:
            seed = datetime.now().isoformat()
        
        bits = [int(b) for b in ''.join(f'{ord(c):08b}' for c in seed)]
        
        self.v = low
        self.r = low
        self.sign = 1
        self.base = base
        self.limit = limit
        self.low = low
        self.high = high
        
        for bit in bits:
            self._advance(bit)

    def _advance(self, bit: int = 0):
        delta = self.sign * ((self.v - self.r) * (self.base - 1)) + bit
        self.v += delta
        
        if self.v > self.high:
            self.v = self.low
            self.sign = 1
        elif self.v < self.low:
            self.v = self.high
            self.sign = -1
            
        if abs(self.v - self.r) >= self.limit:
            self.r += self.sign * self.limit

    def next(self) -> int:
        self._advance()
        return (self.v - self.low) % 64

    def advance_to(self, steps: int) -> int:
        for _ in range(steps):
            self._advance()
        return self.next()


# Original one-shot BNS function (kept for compatibility)
def bns_code_date_time(date_str: str, base=10, limit=4):
    bits = [int(b) for b in ''.join(f'{ord(c):08b}' for c in date_str)]
    
    v = 20000
    r = 20000
    sign = 1
    
    for bit in bits:
        delta = sign * ((v - r) * (base - 1)) + bit
        v += delta
        
        if v > 39999:
            v = 20000
            sign = 1
        elif v < 20000:
            v = 39999
            sign = -1
            
        if abs(v - r) >= limit:
            r += sign * limit
    
    return v
