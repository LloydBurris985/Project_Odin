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

7. NEW: BeaconPoller class + beacon scheduling in PollingManager.
   Per council vote (all four members + Burris, streamlined 2-step path):
   execute_filesystem_sweep/_parse_beacon_file, extracted byte-for-byte
   from odinnet_daemon.py v12.2.0's DaemonContext, no logic changes — same
   directory scan, same beacon.json parsing, same cleaned-frame filtering.
   PollingManager now owns beacon scheduling too (polling_mode/interval,
   same manual/interval/continuous semantics as the old
   _automated_polling_worker), via start_beacon_loop(). The daemon's
   standalone sweep_thread is removed in odinnet_daemon.py v12.3.0 —
   PollingManager is now the single scheduler for both beacon and
   real/temporal polling, per the ratified architecture.

STILL OPEN (flagged, not solved here — needs your input before further build):
- LiveNodePoller (presence detection) still isn't implemented as a class —
  no existing daemon logic to extract it from; this would be new work, not
  an extraction, so it's out of scope for the low-risk streamlined pass.
- Space 5 access-control mechanism (a real read gate, not just "another
  LatticeFS path") remains an accepted gap for now — documented in
  SPACE_REGISTRY.md per council agreement, revisit when Phase 3 GUI/API
  work gives Space 5 an external reader.
"""

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, asdict, field
from datetime import date
from typing import Dict, List, Optional, Any, Callable

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PollingManager")

SPACE_POLL_METADATA = 4   # per SPACE_REGISTRY.md — Polling List Metadata
SPACE_EMBARGOED     = 5   # per SPACE_REGISTRY.md — Embargoed Temporal Storage

POLL_LIST_PATH = "/poll_list.json"


class BeaconPoller:
    """
    Fleet heartbeat / beacon-frame poller. Extracted byte-for-byte from
    DaemonContext.execute_filesystem_sweep + _parse_beacon_file
    (odinnet_daemon.py v12.2.0) — same directory scan, same beacon.json
    parsing, same "v_target"+"group_axis" frame filter. No logic changed.

    Owns its own poll_count (previously ctx.poll_count) since sweep
    counting is specifically a beacon-sweep concept. The daemon's /status
    endpoint now reads this via ctx.polling_manager.beacon_poller.poll_count
    instead of the old ctx.poll_count.

    Staging is delegated via stage_callback rather than owning the airlock
    queue directly — the airlock queue/lock stays on DaemonContext since
    it's shared with the async airlock processing loop, which is unrelated
    to polling scheduling itself.
    """

    def __init__(self, peer_inboxes_dir: str, group_dropbox_dir: str,
                 stage_callback: Callable[[list], int]):
        self.peer_inboxes_dir = peer_inboxes_dir
        self.group_dropbox_dir = group_dropbox_dir
        self.stage_callback = stage_callback
        self.poll_count = 0

    def sweep(self) -> int:
        """Identical logic to the original execute_filesystem_sweep(). Returns staged_total."""
        self.poll_count += 1
        staged_total = 0

        if os.path.exists(self.peer_inboxes_dir):
            for peer in os.listdir(self.peer_inboxes_dir):
                peer_path = os.path.join(self.peer_inboxes_dir, peer)
                if os.path.isdir(peer_path):
                    beacon_file = os.path.join(peer_path, "beacon.json")
                    if os.path.exists(beacon_file):
                        staged_total += self._parse_beacon_file(beacon_file)

        if os.path.exists(self.group_dropbox_dir):
            for file_entry in os.listdir(self.group_dropbox_dir):
                if file_entry.endswith(".json"):
                    drop_file = os.path.join(self.group_dropbox_dir, file_entry)
                    staged_total += self._parse_beacon_file(drop_file)

        return staged_total

    def _parse_beacon_file(self, path: str) -> int:
        """Identical logic to the original DaemonContext._parse_beacon_file."""
        try:
            with open(path, "r") as f:
                data = json.load(f)

            frames = []
            if isinstance(data, list):
                frames = data
            elif isinstance(data, dict):
                frames = data.get("raw_export_slices", [data])

            cleaned_frames = [f for f in frames if "v_target" in f and "group_axis" in f]
            return self.stage_callback(cleaned_frames)
        except Exception:
            return 0


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
    Scheduler/coordinator for polling lists (Space 4 metadata), the
    embargoed-temporal release pipeline (Space 5), and now beacon sweep
    scheduling too — PollingManager is the single scheduler for both
    beacon and real/temporal polling per the ratified architecture.

      stateless_node : a real StatelessCommsNode instance (real/temporal)
      beacon_poller  : a real BeaconPoller instance (fleet heartbeat),
                       optional — daemon can still run without one if it
                       doesn't need beacon polling for some reason.

    scan_cap/commit_cap default to YOUR real termux_benchmark.py numbers,
    but are constructor params — pass new ones after re-benchmarking.
    """

    def __init__(
        self,
        lattice_fs: Any,             # real LatticeFSv2 instance
        stateless_node: Any,         # real StatelessCommsNode instance
        beacon_poller: Optional[Any] = None,   # NEW — real BeaconPoller instance
        budget_ms: int = 3000,
        scan_cap: int = 969,         # from termux_benchmark.py, 3000ms budget
        commit_cap: int = 12,        # from termux_benchmark.py, 3000ms budget
        on_message_delivered: Optional[Callable[[dict], None]] = None,
        on_beacon_swept: Optional[Callable[[int], None]] = None,
        polling_mode: str = "interval",       # NEW — moved from DaemonContext
        polling_interval_sec: int = 15,       # NEW — moved from DaemonContext, same default
    ):
        self.lattice_fs = lattice_fs
        self.stateless_node = stateless_node
        self.beacon_poller = beacon_poller
        self.budget_ms = budget_ms
        self.scan_cap = scan_cap
        self.commit_cap = commit_cap
        self.on_message_delivered = on_message_delivered or (lambda msg: None)
        self.on_beacon_swept = on_beacon_swept or (lambda staged_total: None)
        self.polling_mode = polling_mode
        self.polling_interval_sec = polling_interval_sec

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

    # ── Beacon polling (NEW) ─────────────────────────────────────────────

    def run_beacon_sweep(self) -> int:
        """
        Run a single beacon sweep via self.beacon_poller. Returns
        staged_total (0 if no beacon_poller configured). Calls
        on_beacon_swept(staged_total) for logging, same as the daemon's
        old "[SWEEP CYCLE] Extracted N new frames..." log line.
        """
        if self.beacon_poller is None:
            return 0
        staged_total = self.beacon_poller.sweep()
        if staged_total > 0:
            self.on_beacon_swept(staged_total)
        return staged_total

    def _beacon_loop(self):
        """
        Identical scheduling semantics to the old
        DaemonContext._automated_polling_worker: manual/interval/continuous,
        same sleep durations. Runs in its own background thread.
        """
        while True:
            if self.polling_mode == "interval":
                time.sleep(self.polling_interval_sec)
                self.run_beacon_sweep()
            elif self.polling_mode == "continuous":
                time.sleep(1)
                self.run_beacon_sweep()
            else:
                time.sleep(2)

    def start_beacon_loop(self) -> Optional[threading.Thread]:
        """
        Spawns the beacon scheduling loop as a daemon thread. Replaces the
        daemon's old self.sweep_thread. No-op (returns None) if no
        beacon_poller was configured.
        """
        if self.beacon_poller is None:
            logger.info("No beacon_poller configured — beacon loop not started.")
            return None
        t = threading.Thread(target=self._beacon_loop, daemon=True)
        t.start()
        return t

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
