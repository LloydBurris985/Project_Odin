# odins_net/runway.py
"""
Core Runway class for Project Odin – handles bounded/chained number generation,
state tracking, and masking for message/chain operations.
"""

from typing import Optional, Any
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
        end_mask: Optional[int] = None,
        name: Optional[str] = None,
        is_public: Optional[bool] = None,
        prefix_filter: Optional[str] = None,
        metadata_schema: Optional[dict] = None,
        **kwargs,                           # ← catches name, is_public, prefix_filter, metadata_schema, etc.
    ):
        """
        Accepts known parameters + silently ignores any extra/unknown ones.
        This prevents TypeError when the caller passes future/planned arguments.
        """
        # Store known fields
        self.name = name or "Unnamed Runway"
        self.is_public = is_public if is_public is not None else True
        self.prefix_filter = prefix_filter
        self.metadata_schema = metadata_schema

        # Warn about ignored arguments (helpful for debugging)
        if kwargs:
            print(f"Runway ignored unknown arguments: {list(kwargs.keys())}")

        self.base = 10
        self.limit = 4
        self.low = 20000
        self.high = 39999

        # Starting mask
        self.current_mask = start_mask if start_mask is not None else self.low

        # End/upper mask
        self.end_mask = end_mask if end_mask is not None else self.high

        # Reference point
        self.reference = self.current_mask

        # Direction/sign
        self.sign = 1

        # Optional internal RNG
        self.rng: Optional[BNSRNG] = None
        # seed logic removed for now – add back if needed

        # Timestamps
        self.created_at = datetime.now().isoformat()
        self.last_advance = time.time()

    def advance(self, bit: int = 0) -> int:
        delta = self.sign * ((self.current_mask - self.reference) * (self.base - 1)) + bit
        self.current_mask += delta

        if self.current_mask > self.end_mask:
            self.current_mask = self.low
            self.sign = 1
        elif self.current_mask < self.low:
            self.current_mask = self.end_mask
            self.sign = -1

        if abs(self.current_mask - self.reference) >= self.limit:
            self.reference += self.sign * self.limit

        self.last_advance = time.time()
        return self.current_mask

    def get_current_mask(self) -> int:
        return self.current_mask

    def reset(self, new_mask: Optional[int] = None):
        if new_mask is not None:
            self.current_mask = new_mask
        else:
            self.current_mask = self.low
        self.reference = self.current_mask
        self.sign = 1
        self.last_advance = time.time()

    def __repr__(self) -> str:
        name_str = f'name="{self.name}" ' if self.name else ''
        return (
            f"Runway({name_str}mask={self.current_mask}, ref={self.reference}, "
            f"sign={self.sign}, range={self.low}–{self.end_mask})"
        )


# Factory function – also accepts extra kwargs
def create_hall_runway(
    start_mask: int = 20000,
    end_mask: Optional[int] = None,
    name: Optional[str] = "Odin's Hall",
    **kwargs
) -> Runway:
    return Runway(
        start_mask=start_mask,
        end_mask=end_mask,
        name=name,
        **kwargs
    )
