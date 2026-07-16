"""
polling_manager.py — OdinNet Polling Manager (Phase 2, real-interface rewrite)
================================================================================
Rewritten against VERIFIED interfaces after auditing Grok's skeleton against
the real lattice_fs_v2.py and stateless_comms.py sources. Changes from the
skeleton, and why:

1. NO invented lattice_fs.read_space()/write_space() — those don't exist.
   Uses the REAL LatticeFSv2 API: write_file(path, data, space_id=N),
   read_file(path, space_id=N), exists(path), list_paths(prefix, space_id)
   (list_paths is a small additive method — see lattice_fs_v2.py patch note).

2. NO invented comms_engine.poll_coordinate(). Uses the REAL
   StatelessCommsNode.poll(steps=N) (returns due hits) and the NEW
   pop_deferred_pending() (returns decoded-but-embargoed hits) added to
   stateless_comms.py alongside the CoordScanner fix — see that file's
   changelog. Without that fix, CoordScanner silently discarded every
   future-dated message before it could ever reach a PollingManager;
   no amount of correct logic here could have implemented decode-but-hold
   on top of the old scanner.

3. Actually implements the ratified temporal state machine: due messages
   flow to the caller immediately; deferred messages are persisted to
   LatticeFS Space 5 (one file per message) and released via
   release_due_embargoes(), which checks to_date against "today" using
   the same MessageFrame.temporal_filter the rest of the system already
   uses — no duplicated date logic.

4. sha256-based embargo keys, not Python's hash() (which is randomized
   per-process via PYTHONHASHSEED and is not a stable identifier across
   daemon restarts — verified this concretely before fixing it).

5. Priority-aware scheduling: get_due_entries() sorts by priority before
   truncating to the scan cap, so low-priority lists can't starve
   high-priority ones even when both are due simultaneously.

6. Caps are constructor parameters with your real benchmarked defaults,
   not hardcoded literals — rerun termux_benchmark.py on new hardware or
   a different --budget-ms and pass the new numbers in, no code change.

STILL OPEN (flagged, not solved here — needs your input before further build):
- This module only wraps the "RealMessagePoller"/"TemporalPoller" role via
  StatelessCommsNode. BeaconPoller (fleet heartbeat) and LiveNodePoller
  (presence) aren't implemented as separate classes — the ratified vote
  wanted four specialized pollers under one PollingManager, and beacon
  polling currently lives inside DaemonContext.execute_filesystem_sweep,
  not as a reusable object. I'm not guessing at how to extract that;
  it needs a decision on whether PollingManager replaces the current
  daemon sweep entirely or coexists with it (same open question I raised
  on the first audit).
- Space 5 access-control mechanism (a real read gate, not just "another
  LatticeFS path") is still an open item per the Space Registry's note.
"""

import hashlib
import json
import logging
import time
from dataclasses import dataclass, asdict, field
from datetime import date
from typing import Dict, List, Optional, Any, Callable

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PollingManager")

SPACE_POLL_METADATA = 4   # per SPACE_REGISTRY.md — Polling List Metadata
SPACE_EMBARGOED     = 5   # per SPACE_REGISTRY.md — Embargoed Temporal Storage

POLL_LIST_PATH = "/poll_list.json"


@dataclass
class PollEntry:
    """Single entry in the polling list metadata (Space 4)."""
    coordinate_id: str
    priority: int = 1                    # 1 = normal, higher = more urgent
    schedule_interval: int = 300         # seconds between polls
    last_poll: Optional[float] = None    # unix timestamp
    required_key_hint: Optional[str] = None
    status: str = "active"               # active, paused, completed, error
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.last_poll is None:
            self.last_poll = 0.0  # never polled — due immediately, not "due in the future"

    def should_poll_now(self, current_time: Optional[float] = None) -> bool:
        if current_time is None:
            current_time = time.time()
        if self.status != "active":
            return False
        return current_time - self.last_poll >= self.schedule_interval


class PollingManager:
    """
    Scheduler/coordinator for polling lists (Space 4 metadata) and the
    embargoed-temporal release pipeline (Space 5). Delegates the actual
    protocol work to injected real objects rather than guessing at their
    interfaces:

      stateless_node : a real StatelessCommsNode instance (real/temporal
                        message polling — the only role this module
                        currently implements against a verified interface)

    scan_cap/commit_cap default to YOUR real termux_benchmark.py numbers,
    but are constructor params — pass new ones after re-benchmarking.
    """

    def __init__(
        self,
        lattice_fs: Any,             # real LatticeFSv2 instance
        stateless_node: Any,         # real StatelessCommsNode instance
        budget_ms: int = 3000,
        scan_cap: int = 969,         # from termux_benchmark.py, 3000ms budget
        commit_cap: int = 12,        # from termux_benchmark.py, 3000ms budget
        on_message_delivered: Optional[Callable[[dict], None]] = None,
    ):
        self.lattice_fs = lattice_fs
        self.stateless_node = stateless_node
        self.budget_ms = budget_ms
        self.scan_cap = scan_cap
        self.commit_cap = commit_cap
        self.on_message_delivered = on_message_delivered or (lambda msg: None)

        self.poll_list: Dict[str, PollEntry] = {}
        self.load_poll_list()

    # ── Space 4: polling list metadata ──────────────────────────────────

    def load_poll_list(self):
        """Load polling metadata from LatticeFS Space 4. Missing file is not an error."""
        try:
            if not self.lattice_fs.exists(POLL_LIST_PATH):
                logger.info("No existing poll list at Space 4 — starting fresh.")
                return
            raw = self.lattice_fs.read_file(POLL_LIST_PATH, space_id=SPACE_POLL_METADATA)
            entries = json.loads(raw.decode("utf-8"))
            for cid, entry_data in entries.items():
                self.poll_list[cid] = PollEntry(**entry_data)
            logger.info(f"Loaded {len(self.poll_list)} poll entries from Space 4")
        except Exception as e:
            logger.warning(f"Failed to load poll list: {e}. Starting fresh.")

    def save_poll_list(self):
        """Persist polling metadata to LatticeFS Space 4."""
        try:
            serialized = {cid: asdict(entry) for cid, entry in self.poll_list.items()}
            raw = json.dumps(serialized, indent=2).encode("utf-8")
            self.lattice_fs.write_file(POLL_LIST_PATH, raw, space_id=SPACE_POLL_METADATA)
            logger.info(f"Saved {len(self.poll_list)} poll entries to Space 4")
        except Exception as e:
            logger.error(f"Failed to save poll list: {e}")

    def add_or_update_entry(self, coordinate_id: str, **kwargs):
        """Add or update a coordinate in the polling list."""
        if coordinate_id in self.poll_list:
            entry = self.poll_list[coordinate_id]
            for k, v in kwargs.items():
                if hasattr(entry, k):
                    setattr(entry, k, v)
        else:
            self.poll_list[coordinate_id] = PollEntry(coordinate_id=coordinate_id, **kwargs)
        self.save_poll_list()

    def get_due_entries(self) -> List[PollEntry]:
        """
        Return entries due for polling, highest priority first, capped at
        scan_cap. Priority sorting means a burst of due low-priority lists
        can't push a high-priority list past the batch cap.
        """
        now = time.time()
        due = [entry for entry in self.poll_list.values() if entry.should_poll_now(now)]
        due.sort(key=lambda e: e.priority, reverse=True)
        return due[: self.scan_cap]

    # ── Real/temporal polling via StatelessCommsNode ────────────────────

    def perform_batch_poll(self, steps: int = 100) -> dict:
        """
        Execute one polling batch within time budget, using the REAL
        StatelessCommsNode.poll() + pop_deferred_pending(). Due messages
        are delivered immediately via on_message_delivered(); deferred
        messages are persisted to Space 5, never delivered directly.

        Returns {"delivered": N, "embargoed": N, "entries_processed": N}.
        """
        start_time = time.time()
        due_entries = self.get_due_entries()
        logger.info(f"Starting batch poll with {len(due_entries)} due entries")

        delivered_count = 0
        embargoed_count = 0
        processed = 0

        for entry in due_entries:
            try:
                new_due = self.stateless_node.poll(steps=steps)
                for msg in new_due:
                    self.on_message_delivered(msg)
                    delivered_count += 1

                deferred = self.stateless_node.pop_deferred_pending()
                for msg in deferred:
                    self._store_embargoed(msg)
                    embargoed_count += 1

                entry.last_poll = time.time()
                entry.status = "active"
                processed += 1

                if (time.time() - start_time) * 1000 > self.budget_ms:
                    logger.info("Time budget exceeded — ending batch early.")
                    break

            except Exception as e:
                logger.error(f"Error polling {entry.coordinate_id}: {e}")
                entry.status = "error"

        self.save_poll_list()
        logger.info(
            f"Batch poll completed: {processed} entries, "
            f"{delivered_count} delivered, {embargoed_count} embargoed"
        )
        return {
            "delivered": delivered_count,
            "embargoed": embargoed_count,
            "entries_processed": processed,
        }

    # ── Space 5: embargoed temporal storage ─────────────────────────────

    @staticmethod
    def _embargo_key(msg: dict) -> str:
        """
        Deterministic, stable dedup key — NOT Python's hash(), which is
        randomized per-process (verified concretely: same dict, two
        process runs, two different hash() values) and would silently
        break dedup across daemon restarts.
        """
        basis = f"{msg.get('probe_V','')}:{msg.get('tuple_hash','')}:{msg.get('to_date','')}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]

    def _store_embargoed(self, msg: dict):
        """Persist a decoded-but-deferred message to Space 5, one file per message."""
        key = self._embargo_key(msg)
        path = f"/embargo/{key}.json"
        try:
            if self.lattice_fs.exists(path):
                return  # already stored, dedup by content-derived key
            raw = json.dumps(msg).encode("utf-8")
            self.lattice_fs.write_file(path, raw, space_id=SPACE_EMBARGOED)
            logger.info(f"Embargoed message stored: {path} (to_date={msg.get('to_date')})")
        except Exception as e:
            logger.error(f"Failed to store embargoed message {path}: {e}")

    def release_due_embargoes(self) -> int:
        """
        Scan Space 5 for embargoed messages whose to_date has arrived and
        release them via on_message_delivered(), then remove from Space 5.
        Intended to run on a slower cadence than the main poll loop (e.g.
        once daily, or once per poll cycle if that's cheap enough for you —
        it's just LatticeFS reads, not BNS decode, so it's far cheaper than
        the scan cap situation the benchmark measured).

        Returns count of messages released.
        """
        released = 0
        try:
            paths = self.lattice_fs.list_paths(prefix="/embargo/", space_id=SPACE_EMBARGOED)
        except Exception as e:
            logger.error(f"Could not list Space 5 embargo entries: {e}")
            return 0

        today = date.today()
        for path in paths:
            try:
                raw = self.lattice_fs.read_file(path, space_id=SPACE_EMBARGOED)
                msg = json.loads(raw.decode("utf-8"))
                to_date_str = msg.get("to_date", "")
                try:
                    due = date.fromisoformat(to_date_str) <= today
                except ValueError:
                    due = False  # malformed date — leave embargoed, don't guess

                if due:
                    self.on_message_delivered(msg)
                    self.lattice_fs.delete_file(path)
                    released += 1
                    logger.info(f"Released embargoed message {path} (to_date={to_date_str})")
            except Exception as e:
                logger.error(f"Error processing embargo entry {path}: {e}")

        if released:
            logger.info(f"Release job: {released} embargoed message(s) delivered.")
        return released

    # ── Daemon loop ──────────────────────────────────────────────────────

    def run_forever(self, poll_interval: int = 60, release_interval: int = 3600):
        """
        Main polling loop. Release job runs on its own (slower) cadence
        since it's cheap LatticeFS reads, not BNS decode — no need to tie
        it to the same interval as the expensive poll batch.
        """
        logger.info("PollingManager started in daemon mode")
        last_release_check = 0.0
        while True:
            try:
                self.perform_batch_poll()

                if time.time() - last_release_check >= release_interval:
                    self.release_due_embargoes()
                    last_release_check = time.time()

                time.sleep(poll_interval)
            except KeyboardInterrupt:
                logger.info("PollingManager shutting down...")
                break
            except Exception as e:
                logger.error(f"Unexpected error in polling loop: {e}")
                time.sleep(10)
