# odins_net/runway.py
# Runway polling and traffic discovery for Odins Net
# MIT License

from typing import Dict, List, Optional, Tuple, Callable
from .core import OdinsEye  # Import the eye for decoding found coords


class Runway:
    """Represents a polling range in the lattice (a 'channel' for traffic)."""

    def __init__(
        self,
        start_mask: int,
        end_mask: int,
        name: Optional[str] = None,
        prefix_filter: Optional[str] = None,  # e.g. username prefix or chain ID
        poll_interval: float = 3600.0,        # seconds, e.g. 1 hour
        is_public: bool = False,
    ):
        if start_mask < OdinsEye.LOW or end_mask > OdinsEye.HIGH:
            raise ValueError("Runway masks must be within lattice bounds (10000-99999)")
        if start_mask > end_mask:
            start_mask, end_mask = end_mask, start_mask  # normalize

        self.start_mask = start_mask
        self.end_mask = end_mask
        self.name = name or f"Runway-{start_mask}-{end_mask}"
        self.prefix_filter = prefix_filter  # optional string prefix on coord metadata
        self.poll_interval = poll_interval
        self.is_public = is_public
        self.eye = OdinsEye()  # Shared instance for decoding

    def size(self) -> int:
        """Number of possible masks in this runway."""
        return self.end_mask - self.start_mask + 1

    def __repr__(self) -> str:
        pub = "public" if self.is_public else "private"
        return f"<Runway '{self.name}' ({self.start_mask}-{self.end_mask}, {self.size()} masks, {pub})>"


class RunwayPoller:
    """Polls one or more runways for new traffic (coordinates)."""

    def __init__(self, runways: List[Runway]):
        self.runways = runways
        self.eye = OdinsEye()  # One instance shared across polls

    def poll_single_runway(
        self,
        runway: Runway,
        known_coords: Optional[Set[str]] = None,  # to avoid duplicates
        max_results: int = 10,
        simulate_fetch: Callable[[int], Optional[Dict]] = None,
    ) -> List[Dict]:
        """
        Poll a single runway for new coordinates.

        Args:
            runway: The runway to scan
            known_coords: Set of already-seen coord strings (to skip)
            max_results: Stop after finding this many new items
            simulate_fetch: Optional callback(mask) → coord dict or None
                           (for testing; in real impl, replace with mesh/sneakernet fetch)

        Returns:
            List of new decoded/verified coord dicts with metadata
        """
        found = []
        seen = known_coords or set()

        # Simulate scanning the range (in real code: Bluetooth beacon, LoRa packet, QR scan, etc.)
        for mask in range(runway.start_mask, runway.end_mask + 1):
            if simulate_fetch:
                coord = simulate_fetch(mask)
            else:
                # Placeholder: in real Odins Net, this would query local mesh neighbors,
                # read NFC/QR/paper drops, or poll a known billboard coord
                coord = self._dummy_fetch(mask)  # replace with real impl

            if coord is None:
                continue

            coord_str = str(coord)  # simple dedup key (improve later with hash)
            if coord_str in seen:
                continue

            try:
                # Attempt decode to verify it's valid traffic
                data = self.eye.decode(coord)
                # Optional: apply prefix filter if set
                if runway.prefix_filter and not self._matches_prefix(coord, runway.prefix_filter):
                    continue

                found.append({
                    "mask": mask,
                    "coord": coord,
                    "decoded_size": len(data),
                    "hash": coord.get("original_hash"),
                    "from_runway": runway.name,
                })

                seen.add(coord_str)
                if len(found) >= max_results:
                    break

            except ValueError as e:
                # Invalid coord — skip silently (noise in runway)
                pass

        return found

    def _dummy_fetch(self, mask: int) -> Optional[Dict]:
        """Placeholder for real fetch logic. Returns fake coord for testing."""
        # In real code: query neighbors, read local cache, etc.
        # For skeleton: pretend 1 in 1000 masks has traffic
        import random
        if random.random() < 0.001:
            return {
                "version": OdinsEye.VERSION,
                "start_mask": 50000,
                "end_mask": mask,
                "anchor_mask": 50000,
                "last_choice": 32,
                "last_direction": 1,
                "length_bytes": 42,
                "original_hash": "deadbeef" * 16,
            }
        return None

    def _matches_prefix(self, coord: Dict, prefix: str) -> bool:
        """Placeholder filter check (e.g. on metadata or hash)."""
        # Future: check coord["metadata"] or derived prefix
        return True  # for now, pass everything

    def poll_all(self, max_per_runway: int = 5) -> Dict[str, List[Dict]]:
        """Poll all managed runways and collect new traffic."""
        results = {}
        for runway in self.runways:
            new_items = self.poll_single_runway(runway, max_results=max_per_runway)
            if new_items:
                results[runway.name] = new_items
        return results


# Example usage (for README or tests)
if __name__ == "__main__":
    test_runway_polling()
    test_send_and_poll_simulation()
    # Public hub runway (everyone polls this sometimes)
    odins_hall = Runway(10000, 10999, name="Odins-Hall", is_public=True, poll_interval=86400)

    # Private user runway
    my_private = Runway(60000, 60999, name="Lloyd-Private", prefix_filter="lloyd:", poll_interval=300)

    poller = RunwayPoller([odins_hall, my_private])

    # Simulate a poll cycle
    discoveries = poller.poll_all()
    print("Discovered traffic:", discoveries)
