"""
odinnet_daemon.py — OdinNet Control Daemon
v12.3.0  (PollingManager consolidation — Scotty/Claude)

Changes from v12.2.0
------------------------------------------------
19. BEACON SWEEP EXTRACTED INTO PollingManager (was: standalone daemon loop)
    Per council vote (all four AI members + Burris, streamlined 2-step
    path): execute_filesystem_sweep()/_parse_beacon_file() are now
    BeaconPoller.sweep()/_parse_beacon_file() in polling_manager.py,
    extracted byte-for-byte — same directory scan, same beacon.json
    parsing, same frame filtering, no logic changed. DaemonContext no
    longer owns self.sweep_thread or ctx.poll_count directly; both moved
    to PollingManager (ctx.polling_manager.beacon_poller.poll_count,
    started via ctx.polling_manager.start_beacon_loop()).
    stage_to_airlock() stays on DaemonContext — it's shared with the async
    airlock processing loop and isn't a scheduling concern.

20. PollingManager IS NOW THE SINGLE SCHEDULER
    ctx.polling_mode/ctx.polling_interval_sec removed — moved to
    ctx.polling_manager.polling_mode/polling_interval_sec. /api/polling/mode
    now sets those instead. /status and /api/sweep/trigger updated to read/
    call through ctx.polling_manager accordingly. This completes the
    ratified "PollingManager coordinates specialized pollers" architecture
    for the beacon role — StatelessCommsNode (real/temporal) was already
    wired in polling_manager.py; BeaconPoller joins it here.

VERSION BUMP: DAEMON_VERSION -> "12.3.0". No ledger schema changes, no
identity/passphrase changes — everything from v12.2.0 (real passphrase
loading, real identity geometry, canonical node ID, real identity/unlock)
is preserved exactly, updated only where noted above.
"""

import sys
import os
import json
import time
import hmac
import hashlib
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime import datetime

sys.set_int_max_str_digits(100_000)

from chart_generator import LatticeFSEncrypted, fmt_short, HandMath
from folding_chart_generator import FoldingChartGenerator
from folding_lattice_drive import FoldingLatticeDrive
from lattice_fs_v2 import LatticeFSv2, lattice_fs_v2
from odinnet_security import OdinNetSecurity
from stateless_comms import StatelessCommsNode
from polling_manager import PollingManager, BeaconPoller

DAEMON_VERSION = "12.3.0"


def _semver_tuple(v: str) -> tuple:
    """Parse 'a.b.c' into (a,b,c) ints for correct numeric version comparison."""
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return (0,)


# ===========================================================================
# 📦 STATELESS PACKET  (frame/beacon architecture — unchanged schema)
# ===========================================================================

class StatelessPacket:
    def __init__(self, v_target: str, sender_id: str, payload: str, reply_to: str = "", from_epoch: int = 0):
        self.v_target = v_target
        self.sender_id = sender_id
        self.payload = payload
        self.reply_to = reply_to
        self.from_epoch = from_epoch or int(time.time() // 3600)
        self.timestamp = time.time()
        self.msg_id = self._generate_id()
        self.signature = ""

    def _generate_id(self) -> str:
        raw_ctx = f"{self.v_target}:{self.sender_id}:{self.timestamp}"
        return hashlib.sha256(raw_ctx.encode()).hexdigest()[:16]

    def serialize_to_json(self) -> str:
        packet_dict = {
            "h": {"vt": self.v_target, "sid": self.sender_id, "mid": self.msg_id, "rt": self.reply_to, "fe": self.from_epoch, "ts": self.timestamp},
            "p": self.payload,
            "sig": self.signature
        }
        return json.dumps(packet_dict, separators=(',', ':'))

    @staticmethod
    def deserialize_from_json(json_str: str) -> "StatelessPacket":
        d = json.loads(json_str)
        h = d["h"]
        pkt = StatelessPacket(v_target=h["vt"], sender_id=h["sid"], payload=d["p"], reply_to=h["rt"], from_epoch=h["fe"])
        pkt.msg_id = h["mid"]
        pkt.timestamp = h["ts"]
        pkt.signature = d.get("sig", "")
        return pkt


# ===========================================================================
# 🚀 REAL BNS COORDINATE ENGINE
# ===========================================================================

class OdinCommsEngine:
    """
    Turns a StatelessPacket into a coordinate string and back.

    Coordinate format: "{key_hint}:{byte_len}:{V}"

    NOTE (v12.2.0): node_id is now passed in explicitly rather than computed
    here — see changelog item 16. This class is the beacon/ledger message
    engine and intentionally stays at its own 150-digit space, independent
    of the identity/stateless layer's 80-digit coordinatefile.json space.
    """

    def __init__(self, initial_passphrase: str, node_id: str,
                 chart_base: int = 256, mask_base: int = 1_000_000_000_000,
                 num_digits: int = 150):
        self._lock = threading.RLock()
        self.key_ring = {}       # key_hash -> passphrase (str)
        self.active_hash = ""
        self.chart_base = chart_base
        self.mask_base = mask_base
        self.num_digits = num_digits
        self.register_passphrase(initial_passphrase)
        self.node_id = node_id

    def register_passphrase(self, passphrase: str) -> str:
        with self._lock:
            h_key = hashlib.md5(passphrase.encode('utf-8')).hexdigest()[:8]
            self.key_ring[h_key] = passphrase
            self.active_hash = h_key
            return h_key

    def get_active_passphrase(self) -> str:
        with self._lock:
            return self.key_ring[self.active_hash]

    def _new_fcg(self) -> FoldingChartGenerator:
        return FoldingChartGenerator(chart_base=self.chart_base, mask_base=self.mask_base,
                                      num_digits=self.num_digits)

    def packet_to_coordinate(self, packet: "StatelessPacket") -> str:
        with self._lock:
            passphrase = self.get_active_passphrase()
            key_hint = self.active_hash

        raw_json = packet.serialize_to_json().encode('utf-8')
        blob = LatticeFSEncrypted.encrypt(raw_json, passphrase)
        byte_len = len(blob)

        fcg = self._new_fcg()
        V = fcg.encode_bytes(blob)

        return f"{key_hint}:{byte_len}:{V}"

    def coordinate_to_packet(self, coordinate_str: str, expected_group: str = None):
        """Returns (StatelessPacket, passphrase_bytes) or (None, None) on any failure."""
        try:
            parts = coordinate_str.split(":")
            if len(parts) != 3:
                return None, None
            key_hint, byte_len_str, raw_coord_str = parts
            byte_len = int(byte_len_str)
            raw_coord = int(raw_coord_str)

            with self._lock:
                if key_hint not in self.key_ring:
                    return None, None
                passphrase = self.key_ring[key_hint]

            fcg = self._new_fcg()
            blob = fcg.decode_bytes(V=raw_coord, length=byte_len)

            if not LatticeFSEncrypted.is_encrypted(blob):
                return None, None

            raw_json = LatticeFSEncrypted.decrypt(blob, passphrase)
            pkt = StatelessPacket.deserialize_from_json(raw_json.decode('utf-8'))
            return pkt, passphrase.encode('utf-8')
        except Exception:
            return None, None


# ===========================================================================
# 📂 REAL LATTICEFS-BACKED LEDGER STORE (journal + versioning + integrity)
# ===========================================================================

class LatticeLedgerStore:
    """
    Replaces the flat json.dump ledger with LatticeFSv2: journaled writes,
    immutable versioning, SHA-256 integrity, optional AES-256-GCM at rest.
    """

    LEDGER_PATH = "/coordinate_ledger.json"

    def __init__(self, image_path: str = "odinnet_lattice_v2.json", passphrase: str = None):
        self.image_path = image_path
        self.transaction_lock = threading.RLock()
        self.passphrase = passphrase
        self._mapper_kwargs = dict(chart_base=256, mask_base=1_000_000_000_000,
                                    num_digits=150, scale_factor=5000)
        self.fs = None
        self._mount()

    def _mount(self):
        if os.path.exists(self.image_path):
            drive = FoldingLatticeDrive(sector_size=512, n_sectors=8,
                                         **{k: v for k, v in self._mapper_kwargs.items()
                                            if k in ("chart_base", "mask_base", "num_digits", "scale_factor")})
            drive.load(self.image_path)
            self.fs = LatticeFSv2(drive, passphrase=self.passphrase, mapper_kwargs=self._mapper_kwargs)
        else:
            self.fs = lattice_fs_v2(sector_size=512, n_sectors=8,
                                     chart_base=self._mapper_kwargs["chart_base"],
                                     mask_base=self._mapper_kwargs["mask_base"],
                                     num_digits=self._mapper_kwargs["num_digits"],
                                     scale_factor=self._mapper_kwargs["scale_factor"],
                                     passphrase=self.passphrase)
            self.fs.save(self.image_path)

    def unsafe_read_ledger(self) -> dict:
        try:
            if not self.fs.exists(self.LEDGER_PATH):
                return {"file_version": DAEMON_VERSION, "universe_r_steady": 1, "coordinate_ledger": []}
            raw = self.fs.read_file(self.LEDGER_PATH)
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            print(f"[LatticeLedgerStore] read error: {e}")
            return {"file_version": DAEMON_VERSION, "universe_r_steady": 1, "coordinate_ledger": []}

    def unsafe_write_ledger(self, complete_data: dict):
        try:
            raw = json.dumps(complete_data).encode("utf-8")
            self.fs.write_file(self.LEDGER_PATH, raw)
            self.fs.save(self.image_path)
        except Exception as e:
            print(f"[CRITICAL DISK ERROR] {e}")


# ===========================================================================
# ⚙️ DAEMON CONTEXT
# ===========================================================================

class DaemonContext:
    def __init__(self):
        self.version = DAEMON_VERSION
        self.node_callsign = "OdinPrime"

        self.peer_inboxes_dir = "./peer_inboxes"
        self.group_dropbox_dir = "./group_dropbox"

        self.boot_time = time.time()
        self.tx_counter = 5000
        self.log_buffer = []

        self.bookmark_ledger = {}
        self.frame_stats = {}
        self.unlock_fail_log = {}

        self._initialize_transport_directories()

        # NEW (v12.2.0): real identity geometry + canonical node_id, loaded
        # from coordinatefile.json when present (verified match — changelog
        # item 15/16). root_seed_coord_legacy is only set on fallback.
        self.root_seed_coord_legacy = None
        self.identity = self._load_identity_geometry()
        self.node_id = self.identity["node_id"]

        # NEW (v12.2.0): real passphrase, never hardcoded, never logged.
        self.passphrase = self._load_passphrase()

        # Beacon/ledger engine: independent 150-digit space, unchanged,
        # now takes the canonical node_id instead of computing its own.
        self.engine = OdinCommsEngine(self.passphrase, self.node_id)

        self.security = OdinNetSecurity(
            coord_file="node_coordinate.txt",
            security_file="odinnet_security.json",
            blacklist_file="beacon_blacklist.json",
            reputation_file="beacon_reputation.json",
            manifest_file="jump_manifest.json",
        )

        self.lattice_store = LatticeLedgerStore("odinnet_lattice_v2.json", passphrase=None)

        # NEW (v12.2.0): stateless identity/polling node, correctly
        # parametrized to match the real coordinatefile.json.
        self.stateless_node = StatelessCommsNode(
            passphrase=self.passphrase,
            node_id=self.node_id,
            chart_base=self.identity["chart_base"],
            mask_base=self.identity["mask_base"],
            num_digits=self.identity["num_digits"],
            num_n_streams=self.identity["num_n_streams"],
        )

        self.airlock_queue = []
        self.airlock_lock = threading.Lock()

        # NEW (v12.3.0): BeaconPoller extracted from the old
        # execute_filesystem_sweep/_parse_beacon_file, wrapped under the
        # single PollingManager scheduler alongside stateless_node. Replaces
        # ctx.polling_mode/ctx.polling_interval_sec/ctx.poll_count/
        # ctx.sweep_thread entirely — see changelog items 19/20.
        beacon_poller = BeaconPoller(
            peer_inboxes_dir=self.peer_inboxes_dir,
            group_dropbox_dir=self.group_dropbox_dir,
            stage_callback=self.stage_to_airlock,
        )
        self.polling_manager = PollingManager(
            lattice_fs=self.lattice_store.fs,
            stateless_node=self.stateless_node,
            beacon_poller=beacon_poller,
            on_beacon_swept=lambda staged_total: self.log(
                f"[SWEEP CYCLE] Extracted {staged_total} new frames across file channels."
            ),
        )

        self.log("LATTICE CONFIGURATION VERIFIED. INITIALIZING CHANNELS.")
        self.log(f"DETERMINISTIC NODE ADMISSION COMPLETE. ADDRESS SIGNATURE: {self.node_id}")
        self.log(f"IDENTITY SOURCE: {self.identity['source']} "
                 f"(num_digits={self.identity['num_digits']})")

        self.bg_thread = threading.Thread(target=self._airlock_processing_loop, daemon=True)
        self.bg_thread.start()

        self.polling_manager.start_beacon_loop()

    # ── Identity (v12.2.0) ───────────────────────────────────────────────

    def _load_passphrase(self) -> str:
        """
        Priority order:
          1. ODINNET_PASSPHRASE environment variable
          2. ./node_passphrase.txt (should be chmod 600 — warns if not)
          3. Insecure hardcoded fallback — logs a CRITICAL warning.
        The passphrase value itself is never printed, logged, or returned
        by any endpoint.
        """
        env_val = os.environ.get("ODINNET_PASSPHRASE")
        if env_val:
            print("[IDENTITY] Passphrase loaded from ODINNET_PASSPHRASE environment variable.")
            return env_val

        pass_file = "node_passphrase.txt"
        if os.path.exists(pass_file):
            try:
                mode = oct(os.stat(pass_file).st_mode)[-3:]
                if mode != "600":
                    print(f"[IDENTITY][WARN] {pass_file} permissions are {mode}, not 600. "
                          f"Run: chmod 600 {pass_file}")
                with open(pass_file, "r") as f:
                    val = f.read().strip()
                if val:
                    print(f"[IDENTITY] Passphrase loaded from {pass_file}.")
                    return val
            except Exception as e:
                print(f"[IDENTITY][WARN] Could not read {pass_file}: {e}")

        print("[IDENTITY][CRITICAL WARNING] No real passphrase found (ODINNET_PASSPHRASE env var "
              "or ./node_passphrase.txt). Falling back to an INSECURE placeholder passphrase — "
              "this will NOT reproduce your established coordinate identity. Create "
              "./node_passphrase.txt (chmod 600) with your real passphrase, or set "
              "ODINNET_PASSPHRASE, then restart the daemon.")
        return "odinnet-lattice-2026"

    def _load_identity_geometry(self) -> dict:
        """
        Loads chart_base/mask_base/num_digits/num_n_streams + canonical
        node_id from coordinatefile.json when present (verified via
        verify_passphrase_geometry.py to reproduce your real V/R exactly).
        Falls back to legacy defaults + the pre-v12.2.0 opaque-seed node ID
        scheme if coordinatefile.json is absent.
        """
        coord_json_path = "coordinatefile.json"
        if os.path.exists(coord_json_path):
            try:
                with open(coord_json_path, "r") as f:
                    data = json.load(f)
                node_id = self._generate_node_id_from_coordfile(data)
                return {
                    "chart_base": data["chart_base"],
                    "mask_base": data["mask_base"],
                    "num_digits": data["num_digits"],
                    "num_n_streams": data["num_n_streams"],
                    "node_id": node_id,
                    "source": "coordinatefile.json",
                }
            except Exception as e:
                print(f"[IDENTITY][WARN] Could not parse {coord_json_path}: {e} — falling back to legacy scheme.")

        legacy_seed = self._load_root_seed_coord_legacy()
        self.root_seed_coord_legacy = legacy_seed
        node_hash = hashlib.sha256(legacy_seed.encode("utf-8")).hexdigest()[:16]
        print("[IDENTITY] No coordinatefile.json found — using legacy opaque-seed node ID scheme. "
              "Drop your real coordinatefile.json in the project directory to use your "
              "established identity instead.")
        return {
            "chart_base": 256,
            "mask_base": 1_000_000_000_000,
            "num_digits": 150,
            "num_n_streams": 12,
            "node_id": f"ODINNET-NODE-{node_hash.upper()}",
            "source": "legacy_node_coordinate.txt",
        }

    @staticmethod
    def _generate_node_id_from_coordfile(data: dict) -> str:
        """
        Node ID derivation confirmed by council: sha256 of the first 12 V
        limbs + first 32 chars of polling_high + message_length +
        num_n_streams, all read directly from coordinatefile.json.
        """
        v_key = data["V"][:12]
        polling_key = str(data["polling_high"])[:32]
        seed = str(v_key) + polling_key + str(data["message_length"]) + str(data["num_n_streams"])
        full_hash = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        return full_hash[:32]

    def _load_root_seed_coord_legacy(self) -> str:
        """Pre-v12.2.0 opaque-string loader. Kept ONLY as a fallback."""
        coord_file = "node_coordinate.txt"
        default = "dca1af7d:216:727732793373"
        if os.path.exists(coord_file):
            try:
                with open(coord_file, "r") as f:
                    content = f.read().strip()
                if content:
                    print(f"[IDENTITY] Legacy node coordinate loaded from {coord_file}")
                    return content
            except Exception as e:
                print(f"[IDENTITY][WARN] Could not read {coord_file}: {e} — using default seed.")
        return default

    def get_identity_reveal(self) -> dict:
        """
        Returns the REAL unmasked V/R identity coordinate, decoded via
        HandMath from coordinatefile.json. Falls back to the legacy opaque
        seed, clearly labeled, if coordinatefile.json wasn't the source.
        """
        if self.identity.get("source") == "coordinatefile.json":
            coord_json_path = "coordinatefile.json"
            try:
                with open(coord_json_path, "r") as f:
                    data = json.load(f)
                hm = HandMath(data["mask_base"], data["num_digits"])
                V_int = hm.to_int(data["V"])
                R_int = hm.to_int(data["R"])
                return {
                    "format": "coordinatefile_v1",
                    "V_int": str(V_int),
                    "R_int": str(R_int),
                    "chart_base": data["chart_base"],
                    "mask_base": data["mask_base"],
                    "num_digits": data["num_digits"],
                }
            except Exception as e:
                return {"format": "error", "error": str(e)}

        return {
            "format": "legacy_opaque",
            "root_seed_coord": self.root_seed_coord_legacy,
            "note": ("coordinatefile.json was not used as the identity source for this boot "
                     "(missing or failed to parse) — this is the pre-v12.2.0 opaque seed, not "
                     "your real structured V/R credential.")
        }

    def _initialize_transport_directories(self):
        os.makedirs(self.peer_inboxes_dir, exist_ok=True)
        os.makedirs(self.group_dropbox_dir, exist_ok=True)
        for fleet_peer in ["Heimdall", "Thor", "Loki"]:
            os.makedirs(os.path.join(self.peer_inboxes_dir, fleet_peer), exist_ok=True)

    def log(self, text):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_buffer.append(f"[{ts}] {text}")
        if len(self.log_buffer) > 30:
            self.log_buffer.pop(0)
        print(f"[{ts}] {text}")

    def flash_downgrade_warning(self, file_ver: str):
        print("\n\033[91m\033[1m" + "#" * 70)
        print(f"⚠️  CRITICAL SECURITY ALERT: DATABASE DOWNGRADE OR EXTENSION ATTACK BLOCKED")
        print("#" * 70 + "\033[0m")
        print(f"Running System Matrix Version: {self.version}")
        print(f"Detected Unverified Payload Version: {file_ver}")
        print("\033[91mExecution safely held in memory quarantine. Overwrite rejected.\033[0m\n")

    def stage_to_airlock(self, unverified_frames: list) -> int:
        staged_count = 0
        with self.airlock_lock:
            for frame in unverified_frames:
                m_id = frame.get("msg_id")
                if not m_id or not frame.get("v_target"):
                    continue
                if any(x.get("msg_id") == m_id for x in self.airlock_queue):
                    continue
                self.airlock_queue.append(frame)
                staged_count += 1
        if staged_count > 0:
            self.log(f"[AIRLOCK QUARANTINE] Buffered {staged_count} unverified frames into memory.")
        return staged_count

    def execute_filesystem_sweep(self):
        """
        DEPRECATED (v12.3.0): logic moved to BeaconPoller.sweep() in
        polling_manager.py. Kept as a thin forwarding shim so
        /api/sweep/trigger and any external callers don't break.
        """
        self.polling_manager.run_beacon_sweep()

    def _record_frame_stat(self, callsign: str, committed: bool = False, rejected: bool = False):
        entry = self.frame_stats.setdefault(callsign, {"committed": 0, "rejected": 0, "last_seen": None})
        if committed:
            entry["committed"] += 1
            entry["last_seen"] = time.time()
        if rejected:
            entry["rejected"] += 1
            entry["last_seen"] = time.time()

    def _airlock_processing_loop(self):
        while True:
            time.sleep(2)

            with self.airlock_lock:
                if not self.airlock_queue:
                    continue
                working_batch = list(self.airlock_queue)

            retained_queue = []
            committed_any = False

            with self.lattice_store.transaction_lock:
                store_data = self.lattice_store.unsafe_read_ledger()

                file_ver = store_data.get("file_version", self.version)
                if _semver_tuple(file_ver) < _semver_tuple(self.version):
                    self.flash_downgrade_warning(file_ver)
                    continue

                current_ledger = store_data.get("coordinate_ledger", [])

                for frame in working_batch:
                    m_id = frame.get("msg_id")
                    g_axis = frame.get("group_axis")
                    v_val = frame.get("v_target")
                    sig_auth = frame.get("signature_auth")

                    if any(x.get("msg_id") == m_id for x in current_ledger):
                        continue

                    pkt, target_key = self.engine.coordinate_to_packet(v_val, g_axis)

                    if pkt is None or target_key is None:
                        if frame not in retained_queue:
                            retained_queue.append(frame)
                        continue

                    calc_mac = hmac.new(target_key, pkt.payload.encode('utf-8'), hashlib.sha256).hexdigest()[:16]
                    if calc_mac != sig_auth:
                        self.log(f"[SECURITY ALERT] Dropping corrupted signature frame [{m_id}].")
                        sender = frame.get("sender_callsign", "unknown")
                        self.security.reputation.report_hash_mismatch(sender)
                        self._record_frame_stat(sender, rejected=True)
                        continue

                    frame["sender_callsign"] = frame.get("sender_callsign", "OdinPeer")
                    current_ledger.append(frame)
                    committed_any = True
                    self.security.reputation.record_success(frame.get("sender_callsign", "unknown"))
                    self._record_frame_stat(frame["sender_callsign"], committed=True)
                    self.log(f"[CRYPTO VALIDATED] Reconstructed matrix state from node string [{pkt.sender_id[:12]}].")

                if committed_any:
                    self.tx_counter += 1
                    store_data["coordinate_ledger"] = current_ledger
                    store_data["file_version"] = self.version
                    self.lattice_store.unsafe_write_ledger(store_data)
                    self.log(f"COMMIT TX {self.tx_counter}: Airlock batch synchronized to disk (journaled).")

            with self.airlock_lock:
                self.airlock_queue = retained_queue

    def post_mathematical_beacon(self, group: str, subject: str, body: str) -> str:
        pkt = StatelessPacket(v_target=group, sender_id=self.node_id, payload=body, reply_to=subject)
        pkt.sender_callsign = self.node_callsign

        active_pass = self.engine.get_active_passphrase().encode('utf-8')
        mac = hmac.new(active_pass, pkt.payload.encode('utf-8'), hashlib.sha256)
        pkt.signature = mac.hexdigest()[:16]

        coordinate_v = self.engine.packet_to_coordinate(pkt)

        frame = {
            "v_target": coordinate_v,
            "group_axis": group,
            "msg_id": pkt.msg_id,
            "signature_auth": pkt.signature,
            "sender_callsign": self.node_callsign
        }

        with self.lattice_store.transaction_lock:
            self.tx_counter += 1
            self.log(f"BEGIN TX {self.tx_counter}: ATOMIC WRITE OPERATION START")
            store_data = self.lattice_store.unsafe_read_ledger()
            current_ledger = store_data.get("coordinate_ledger", [])

            if not any(x.get("msg_id") == pkt.msg_id for x in current_ledger):
                current_ledger.append(frame)
                store_data["coordinate_ledger"] = current_ledger
                store_data["file_version"] = self.version
                self.lattice_store.unsafe_write_ledger(store_data)
                self.log(f"COMMIT TX {self.tx_counter}: DISK REPLICATION COMPLETE (journaled).")
        return coordinate_v

    def collect_and_decode_group(self, group: str) -> list:
        with self.lattice_store.transaction_lock:
            store_data = self.lattice_store.unsafe_read_ledger()
            raw_ledger = store_data.get("coordinate_ledger", [])

        decoded_frames = []
        for frame in raw_ledger:
            if frame.get("group_axis") != group:
                continue

            pkt, key_bytes = self.engine.coordinate_to_packet(frame["v_target"], group)
            if pkt is None or key_bytes is None:
                continue

            check_mac = hmac.new(key_bytes, pkt.payload.encode('utf-8'), hashlib.sha256).hexdigest()[:16]
            if check_mac != frame.get("signature_auth"):
                continue

            decoded_frames.append({
                "v_target": frame["v_target"], "group_axis": group, "subject": pkt.reply_to,
                "body": pkt.payload, "sender_node": pkt.sender_id[:12],
                "sender_callsign": frame.get("sender_callsign", "OdinPeer"), "epoch": pkt.from_epoch
            })
        return decoded_frames

    def get_airlock_status(self) -> dict:
        with self.airlock_lock:
            return {"queue_len": len(self.airlock_queue), "contents": list(self.airlock_queue)}

    def get_fleet_status(self) -> list:
        known_peers = set()
        if os.path.exists(self.peer_inboxes_dir):
            for peer in os.listdir(self.peer_inboxes_dir):
                if os.path.isdir(os.path.join(self.peer_inboxes_dir, peer)):
                    known_peers.add(peer)
        known_peers |= set(self.frame_stats.keys())
        known_peers.add(self.node_callsign)

        entries = []
        for peer in sorted(known_peers):
            beacon_file = os.path.join(self.peer_inboxes_dir, peer, "beacon.json")
            data_hash = None
            last_seen_disk = None
            if os.path.exists(beacon_file):
                try:
                    with open(beacon_file, "rb") as f:
                        raw = f.read()
                    data_hash = hashlib.sha256(raw).hexdigest()[:8]
                    last_seen_disk = os.path.getmtime(beacon_file)
                except Exception:
                    pass

            stats = self.frame_stats.get(peer, {"committed": 0, "rejected": 0, "last_seen": None})
            rep_hash = hashlib.sha256(json.dumps(stats, sort_keys=True).encode()).hexdigest()[:8]
            total = stats["committed"] + stats["rejected"]
            integrity = round(stats["committed"] / total, 2) if total > 0 else None

            last_seen = stats["last_seen"] or last_seen_disk
            entries.append({
                "vessel_id": peer,
                "status_hash_d": data_hash or "N/A",
                "status_hash_r": rep_hash,
                "integrity": integrity,
                "committed": stats["committed"],
                "rejected": stats["rejected"],
                "last_seen": datetime.fromtimestamp(last_seen).strftime("%H:%M:%S") if last_seen else None,
            })
        return entries

    def check_unlock_rate_limit(self, ip: str) -> bool:
        now = time.time()
        attempts = [t for t in self.unlock_fail_log.get(ip, []) if now - t < 60]
        self.unlock_fail_log[ip] = attempts
        return len(attempts) < 5

    def record_unlock_failure(self, ip: str):
        self.unlock_fail_log.setdefault(ip, []).append(time.time())


# ===========================================================================
# CONTROL TIER SERVER LAYER
# ===========================================================================

def make_handler(ctx: DaemonContext):
    class OdinWebHandler(BaseHTTPRequestHandler):
        log_message = lambda *a: None

        def _send_json(self, code: int, obj: dict):
            body = json.dumps(obj).encode('utf-8')
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict:
            length = int(self.headers.get('Content-Length', 0))
            if length == 0:
                return {}
            return json.loads(self.rfile.read(length).decode('utf-8'))

        def do_GET(self):
            path = self.path.split("?")[0]
            query = {}
            if "?" in self.path:
                for p in self.path.split("?")[1].split("&"):
                    if "=" in p:
                        k, v = p.split("=", 1)
                        query[k] = v

            if path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                if os.path.exists("dashboard.html"):
                    with open("dashboard.html", "rb") as f:
                        self.wfile.write(f.read())

            elif path == "/status":
                airlock_info = ctx.get_airlock_status()
                sec = ctx.security.status_dict()
                status_payload = {
                    "node_id": ctx.node_id, "node_callsign": ctx.node_callsign,
                    "uptime_sec": time.time() - ctx.boot_time,
                    "poll_count": ctx.polling_manager.beacon_poller.poll_count,
                    "defcon": sec["defcon"],
                    "defcon_label": f"{sec['defcon_label']} | AIRLOCK ({airlock_info['queue_len']} HOLD)",
                    "defcon_color": sec["defcon_color"],
                    "current_r": sec["current_r"], "jump_count": sec["jump_count"],
                    "polling_mode": ctx.polling_manager.polling_mode,
                    "polling_interval_sec": ctx.polling_manager.polling_interval_sec,
                    "identity_source": ctx.identity["source"],
                    "activity_tail": ctx.log_buffer
                }
                self._send_json(200, status_payload)

            elif path == "/api/security/status":
                self._send_json(200, ctx.security.status_dict())

            elif path == "/api/usenet/feed":
                query_group = query.get("group", "sci.burris.odinnet")
                self._send_json(200, ctx.collect_and_decode_group(query_group))

            elif path == "/api/sneakernet/export":
                self._send_json(200, {"raw_export": json.dumps(ctx.lattice_store.unsafe_read_ledger())})

            elif path == "/api/stateless/status":
                self._send_json(200, ctx.stateless_node.status())

            elif path == "/api/stateless/poll":
                steps = int(query.get("steps", 100))
                self._send_json(200, ctx.stateless_node.poll(steps=steps))

            elif path == "/api/stateless/inbox":
                self._send_json(200, ctx.stateless_node.inbox())

            elif path == "/api/fleet/status":
                self._send_json(200, {"fleet": ctx.get_fleet_status()})

            elif path == "/api/identity/status":
                self._send_json(200, {
                    "node_id": ctx.node_id,
                    "node_callsign": ctx.node_callsign,
                    "identity_source": ctx.identity["source"],
                    "active_key_hint": ctx.engine.active_hash,
                    "known_key_hints": list(ctx.engine.key_ring.keys()),
                    "masked": True
                })

            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self):
            path = self.path.split("?")[0]
            try:
                data = self._read_json_body()
            except Exception:
                self._send_json(400, {"error": "Invalid JSON body"})
                return

            if path == "/api/usenet/post":
                v_target = ctx.post_mathematical_beacon(data.get("group"), data.get("subject"), data.get("body"))
                self._send_json(200, {"ok": True, "v_target": v_target})

            elif path == "/api/security/rotate_key":
                new_key = data.get("passphrase", "odinnet-lattice-2026")
                ctx.engine.register_passphrase(new_key)
                ctx.log(f"[SECURITY KEY ENGINE] Added key plane index mapping.")
                self._send_json(200, {"status": "ROTATED"})

            elif path == "/api/security/raise":
                try:
                    level = int(data.get("level", ctx.security.defcon + 1))
                    ctx.security.raise_defcon(level, reason=data.get("reason", "api"))
                    self._send_json(200, {"ok": True, "defcon": ctx.security.defcon})
                except Exception as e:
                    self._send_json(400, {"error": str(e)})

            elif path == "/api/security/lower":
                try:
                    level = data.get("level")
                    ctx.security.lower_defcon(level=int(level) if level is not None else None,
                                               reason=data.get("reason", "api"))
                    self._send_json(200, {"ok": True, "defcon": ctx.security.defcon})
                except Exception as e:
                    self._send_json(400, {"error": str(e)})

            elif path == "/api/security/jump":
                try:
                    new_r = int(data["new_r"])
                    result = ctx.security.fleet_jump(
                        new_r, reason=data.get("reason", "api_jump"),
                        beacon_list=data.get("beacon_list", []),
                        force=bool(data.get("force", False)),
                    )
                    self._send_json(200, result)
                except (ValueError, KeyError) as e:
                    self._send_json(400, {"error": str(e)})

            elif path == "/api/identity/callsign":
                new_callsign = data.get("callsign", "").strip()
                if not new_callsign:
                    self._send_json(400, {"error": "callsign required"})
                    return
                ctx.node_callsign = new_callsign
                ctx.log(f"[IDENTITY] Callsign overridden → {new_callsign}")
                self._send_json(200, {"ok": True, "callsign": ctx.node_callsign})

            elif path == "/api/identity/unlock":
                client_ip = self.client_address[0]
                if not ctx.check_unlock_rate_limit(client_ip):
                    self._send_json(429, {"error": "Too many failed unlock attempts. Try again in a moment."})
                    return
                passphrase = data.get("passphrase", "")
                if not passphrase:
                    self._send_json(400, {"error": "passphrase required"})
                    return
                key_hint = hashlib.md5(passphrase.encode('utf-8')).hexdigest()[:8]
                candidate = ctx.engine.key_ring.get(key_hint)
                if candidate is None or not hmac.compare_digest(candidate, passphrase):
                    ctx.record_unlock_failure(client_ip)
                    ctx.log(f"[SECURITY] Failed identity/passphrase unlock attempt from {client_ip}")
                    self._send_json(401, {"error": "Invalid passphrase"})
                    return
                ctx.log(f"[SECURITY] Identity/passphrase unlock SUCCESS from {client_ip} (key_hint={key_hint})")
                reveal = ctx.get_identity_reveal()
                self._send_json(200, {"ok": True, "key_hint": key_hint, **reveal})

            elif path == "/api/polling/mode":
                mode = data.get("mode")
                if mode not in ("manual", "interval", "continuous"):
                    self._send_json(400, {"error": "mode must be manual, interval, or continuous"})
                    return
                ctx.polling_manager.polling_mode = mode
                if "interval_sec" in data:
                    try:
                        ctx.polling_manager.polling_interval_sec = max(1, int(data["interval_sec"]))
                    except Exception:
                        pass
                ctx.log(f"[POLLING] Mode set to {mode.upper()} (interval={ctx.polling_manager.polling_interval_sec}s)")
                self._send_json(200, {
                    "ok": True,
                    "polling_mode": ctx.polling_manager.polling_mode,
                    "polling_interval_sec": ctx.polling_manager.polling_interval_sec
                })

            elif path == "/api/sneakernet/inject":
                try:
                    injected_data = json.loads(data.get("raw_import", "[]"))
                    injected_list = []
                    if isinstance(injected_data, dict):
                        injected_list = injected_data.get("coordinate_ledger", [])
                    elif isinstance(injected_data, list):
                        injected_list = injected_data
                    count = ctx.stage_to_airlock(injected_list)
                    self._send_json(200, {"ok": True, "staged": count})
                except Exception:
                    self._send_json(400, {"error": "Invalid payload format"})

            elif path == "/api/sweep/trigger":
                ctx.execute_filesystem_sweep()
                self._send_json(200, {"status": "SWEEP_COMPLETE", "polls": ctx.polling_manager.beacon_poller.poll_count})

            elif path == "/api/stateless/send":
                try:
                    coord, pad_bytes = ctx.stateless_node.send(
                        data.get("text", ""), subject=data.get("subject", ""),
                        to_date=data.get("to_date"),
                    )
                    self._send_json(200, {"ok": True, "coordinate": str(coord), "pad_bytes": pad_bytes})
                except Exception as e:
                    self._send_json(400, {"error": str(e)})

            else:
                self._send_json(404, {"error": "not found"})

    return OdinWebHandler


def main():
    ctx = DaemonContext()
    host = "0.0.0.0"
    port = 7474
    httpd = None
    for attempt in range(10):
        try:
            httpd = ThreadingHTTPServer((host, port), make_handler(ctx))
            break
        except OSError as e:
            ctx.log(f"[BIND] Port {port} unavailable ({e}) — trying {port + 1}")
            port += 1

    if httpd is None:
        ctx.log("[FATAL] Could not bind to any port in range 7474-7483.")
        return

    ctx.log(f"ODINNET DAEMON LISTENING on {host}:{port} (reachable from phone browser + LAN)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.server_close()


if __name__ == "__main__":
    main()
