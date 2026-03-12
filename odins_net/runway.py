# odins_net/runway.py
"""
Core Runway class for Project Odin – handles bounded/chained number generation,
state tracking, and masking for message/chain operations.
"""

from typing import Optional
from datetime import datetime
import time

# If you use BNSRNG from rng.py in the same package
from .rng import BNSRNG


class Runway:
    """
    Main runway controller – manages a running state with masking,
    bounds, and optional BNS seeding.
    """

    def __init__(
        self,
        start_mask: Optional[int] = None,
        base: int = 10,
        limit: int = 4,
        low: int = 20000,
        high: int = 39999,
        seed: Optional[str] = None,
    ):
        """
        Args:
            start_mask: Initial mask value (often ODINS_HALL_START)
            base: Number base for BNS calculations
            limit: Step limit before reference update
            low/high: Bounding range for value oscillation
            seed: Optional seed string (defaults to current ISO time)
        """
        self.base = base
        self.limit = limit
        self.low = low
        self.high = high

        # Starting mask – this was the missing parameter
        self.current_mask = start_mask if start_mask is not None else 20000

        # Reference point (r in BNS logic)
        self.reference = self.current_mask

        # Direction/sign of movement
        self.sign = 1

        # Optional internal RNG if you want time/chain-based variation
        self.rng: Optional[BNSRNG] = None
        if seed is not None:
            self.rng = BNSRNG(
                seed=seed,
                base=base,
                limit=limit,
                low=low,
                high=high
            )

        # Timestamp of creation / last reset
        self.created_at = datetime.now().isoformat()
        self.last_advance = time.time()

    def advance(self, bit: int = 0) -> int:
        """
        Single step forward – similar to BNSRNG._advance but tied to this instance.
        Returns the new mask value.
        """
        delta = self.sign * ((self.current_mask - self.reference) * (self.base - 1)) + bit

        self.current_mask += delta

        # Bound wrapping + sign flip
        if self.current_mask > self.high:
            self.current_mask = self.low
            self.sign = 1
        elif self.current_mask < self.low:
            self.current_mask = self.high
            self.sign = -1

        # Update reference when deviation is large enough
        if abs(self.current_mask - self.reference) >= self.limit:
            self.reference += self.sign * self.limit

        self.last_advance = time.time()
        return self.current_mask

    def get_current_mask(self) -> int:
        """Current active mask value"""
        return self.current_mask

    def reset(self, new_mask: Optional[int] = None):
        """Reset to a specific mask or current low bound"""
        if new_mask is not None:
            self.current_mask = new_mask
        else:
            self.current_mask = self.low
        self.reference = self.current_mask
        self.sign = 1
        self.last_advance = time.time()

    def __repr__(self) -> str:
        return (
            f"Runway(mask={self.current_mask}, ref={self.reference}, "
            f"sign={self.sign}, range={self.low}–{self.high})"
        )


# Optional factory / convenience function if used in nexus_hub.py
def create_hall_runway(start_mask: int = 20000) -> Runway:
    """Quick factory for Odin's Hall runway instance"""
    return Runway(
        start_mask=start_mask,
        # You can add more fixed defaults here if needed
    )
