from typing import Optional, List, Any
import threading
import queue
import time


class RunwayPoller:
    """
    Polls the Runway (or related source) periodically and collects new/unique results.
    Uses known_coords set to deduplicate if your items have coordinate-like keys.
    """

    def __init__(
        self,
        runway: Runway,
        poll_interval: float = 5.0,
        known_coords: Optional[set[str]] = None,
    ):
        self.runway = runway
        self.poll_interval = poll_interval
        self.known_coords = known_coords if known_coords is not None else set()

        self.results: List[Any] = []
        self.lock = threading.Lock()
        self.running = False
        self.thread: Optional[threading.Thread] = None

    def _poll(self):
        """Replace this with your real polling logic (e.g. advance runway, check events)."""
        try:
            # Example: advance the runway and get current state
            new_mask = self.runway.advance()
            coord_key = f"mask_{new_mask}"  # ← customize this key logic!

            with self.lock:
                if coord_key not in self.known_coords:
                    self.known_coords.add(coord_key)
                    self.results.append({
                        "timestamp": time.time(),
                        "mask": new_mask,
                        "source": "runway"
                    })
        except Exception as e:
            print(f"Poller error: {e}")

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3.0)

    def _loop(self):
        while self.running:
            self._poll()
            time.sleep(self.poll_interval)

    def get_results(self, clear: bool = False) -> List[Any]:
        with self.lock:
            res = self.results.copy()
            if clear:
                self.results.clear()
            return res
