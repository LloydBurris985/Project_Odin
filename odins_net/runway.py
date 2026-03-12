# odins_net/runaway.py

from typing import Optional, Any, List, Dict
from datetime import datetime
import time
import threading
import queue
# Add any other imports your actual code needs here
# (for example: requests, json, logging, etc. — add them if you get NameError later)

class Runway:
    """
    Placeholder / base class for whatever Runway is in your project.
    Replace or extend this with your actual implementation if needed.
    """
    def __init__(self):
        pass  # ← put your real initialization here later


class RunwayPoller:
    """
    Polls something (API, chain, coordinates, messages?) and collects results.
    The known_coords set is used to avoid processing duplicates.
    """

    def __init__(
        self,
        runway: Runway,
        poll_interval: float = 5.0,           # seconds between polls
        max_results: int = 100,
        coords: Optional[List[str]] = None,
        known_coords: Optional[set[str]] = None,  # to avoid duplicates
    ):
        self.runway = runway
        self.poll_interval = poll_interval
        self.max_results = max_results
        
        # Use set for fast duplicate checking
        self.known_coords = known_coords if known_coords is not None else set()
        
        # If initial coords were passed, add them to known set
        if coords:
            self.known_coords.update(coords)
        
        self.results_queue: queue.Queue = queue.Queue()
        self.running = False
        self.thread: Optional[threading.Thread] = None

    def _poll_once(self):
        """Single polling action — replace this with your real logic."""
        try:
            # This is placeholder — replace with your actual polling code
            # Example: fetch new coordinates, messages, chain events, etc.
            new_items = self._fetch_new_items()
            
            for item in new_items:
                coord_key = self._get_coord_key(item)  # define your own key logic
                if coord_key not in self.known_coords:
                    self.known_coords.add(coord_key)
                    self.results_queue.put(item)
                    # Optional: print or log new discovery
                    print(f"New unique item discovered: {coord_key}")
                    
        except Exception as e:
            print(f"Polling error: {e}")

    def _fetch_new_items(self) -> List[Dict[str, Any]]:
        """Replace this with your actual data source (API, file, blockchain, etc.)"""
        # Placeholder — return fake or real new data here
        # Example return format: [{"id": "...", "coord": "x,y,z", ...}, ...]
        return []

    def _get_coord_key(self, item: Dict[str, Any]) -> str:
        """Define how you identify uniqueness (replace as needed)"""
        return item.get("coord", str(item))  # example

    def start(self):
        """Start background polling thread"""
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._polling_loop, daemon=True)
        self.thread.start()
        print("RunwayPoller started")

    def stop(self):
        """Stop polling"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=2.0)
        print("RunwayPoller stopped")

    def _polling_loop(self):
        while self.running:
            self._poll_once()
            time.sleep(self.poll_interval)

    def get_results(self, block: bool = False, timeout: Optional[float] = None) -> List[Any]:
        """Get all currently available results (non-blocking by default)"""
        results = []
        while True:
            try:
                item = self.results_queue.get(block=block, timeout=timeout)
                results.append(item)
            except queue.Empty:
                break
        return results

    def clear_results(self):
        """Empty the results queue without consuming them"""
        with self.results_queue.mutex:
            self.results_queue.queue.clear()


# If this file is run directly (for testing)
if __name__ == "__main__":
    rw = Runway()
    poller = RunwayPoller(rw, poll_interval=3.0)
    poller.start()
    
    try:
        time.sleep(15)  # let it run for a bit
        print("Collected:", poller.get_results())
    finally:
        poller.stop()
