"""
OdinNet Integrated Daemon v11.1 (Temporal Sync & Integrated Resolver)
======================================================================
Upgrades implemented:
  [1] THREADING HTTP     — Replaced HTTPServer with ThreadingHTTPServer to eliminate UI locking.
  [2] INTEGRATED RESOLVER— Native dashboard textbox handles burris:// lookups directly on the backend.
  [3] TEMPORAL SYNC READY— DaemonContext modified to track channel locks across distinct dates/clocks.
"""

import sys, os, json, time, hashlib, hmac, threading, traceback
sys.set_int_max_str_digits(100_000)

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime    import datetime
from typing      import Dict, List, Optional

# ── OdinNet core imports ─────────────────────────────────────────────────────
from odinnet_comms import OdinCommsEngine, StatelessPacket
from lattice_fs_v2 import (
    lattice_fs_v2, LatticeFSv2,
    SPACE_USER, SPACE_SYSTEM,
    JOP_WRITE_FILE,
)

# ── Constants ─────────────────────────────────────────────────────────────────
DAEMON_VERSION    = "11.1"
DEFAULT_PORT      = 7474
SPACE_FLEET       = 2          
BNS_PASSPHRASE    = os.environ.get("ODINNET_PASS",  "odinnet-lattice-2026")
BEARER_TOKEN      = os.environ.get("ODINNET_TOKEN", "odinnet-secret-token")
POLL_INTERVAL_SEC = 4
FS_SECTORS        = 256
FS_SECTOR_SIZE    = 512
FS_IMG_PATH       = "odinnet_lattice.json"

_JTX_IDLE   = "IDLE"
_JTX_OPEN   = "OPEN"
_JTX_COMMIT = "COMMITTED"
_JTX_ABORT  = "ABORTED"

class DaemonTransaction:
    def __init__(self, ctx: "DaemonContext", tx_id: str, path: str, blob: bytes, space_id: int):
        self._ctx      = ctx
        self.tx_id     = tx_id
        self.path      = path
        self.blob      = blob
        self.space_id  = space_id
        self.state     = _JTX_IDLE
        self._entry    = None

    def _jlog(self, msg: str):
        self._ctx.comms.log(f"[TX {self.tx_id[:8]}] {msg}")

    def begin(self):
        if self.state != _JTX_IDLE: raise RuntimeError("TX open error")
        self.state = _JTX_OPEN
        self._jlog(f"BEGIN TX: WRITE {self.path}")

    def write(self):
        if self.state != _JTX_OPEN: raise RuntimeError("TX state error")
        self._entry = self._ctx.fs.write_file(self.path, self.blob, space_id=self.space_id)

    def commit(self):
        if self.state != _JTX_OPEN: raise RuntimeError("TX commit error")
        self.state = _JTX_COMMIT
        self._jlog(f"COMMIT TX.")

    def abort(self, reason: str):
        self.state = _JTX_ABORT
        self._jlog(f"ABORT TX: {reason}")

class DaemonContext:
    def __init__(self):
        self._lock = threading.RLock()
        self.node_id    = "ODINNET-NODE-" + hashlib.sha256(BNS_PASSPHRASE.encode()).hexdigest()[:8].upper()
        self.base_coord = hashlib.sha256((BNS_PASSPHRASE + ":base").encode()).hexdigest()

        self.fs: LatticeFSv2 = lattice_fs_v2(sector_size=FS_SECTOR_SIZE, n_sectors=FS_SECTORS, passphrase=BNS_PASSPHRASE)
        self.fs.define_space(SPACE_FLEET, "fleet-public")

        self.comms = OdinCommsEngine(passphrase=BNS_PASSPHRASE, my_base_coordinate=self.base_coord, node_id=self.node_id)
        self.seen_packet_ids: set = set()

        self.inbox_log:  List[dict] = []
        self.bbs_log:    List[dict] = []
        self.journal_log: List[dict] = []
        
        # ── [MEETING UPDATE] Temporal Channel Matrices ──
        self.temporal_sync_locks: Dict[str, dict] = {} # channel_id -> {epoch_A, epoch_B, status}
        
        self.defcon      = 5
        self.boot_time   = time.time()
        self.poll_count  = 0
        self.tx_count    = 0
        self.last_poll   = "—"

    def _record_journal_event(self, tx_id: str, event: str, path: str, state: str):
        with self._lock:
            self.journal_log.append({
                "ts": datetime.now().strftime("%H:%M:%S"),
                "tx_id": tx_id[:12], "event": event, "path": path, "state": state
            })
            if len(self.journal_log) > 50: self.journal_log = self.journal_log[-50:]

    def register_temporal_sync(self, channel_id: str, date_a: str, date_b: str):
        """Locks a single channel identifier across two different dates/clocks."""
        with self._lock:
            self.temporal_sync_locks[channel_id] = {
                "plane_alpha": date_a,
                "plane_beta": date_b,
                "synchronized": True,
                "locked_at": time.time()
            }
            self.comms.log(f"⏳ [TEMPORAL SYNC] Channel {channel_id} successfully mapped: {date_a} ⇄ {date_b}")

    def persist_inbox_packet(self, packet: StatelessPacket):
        record = {
            "msg_id": packet.msg_id, "sender_id": packet.sender_id, "payload": packet.payload,
            "reply_to": packet.reply_to, "timestamp": packet.timestamp, "signature": packet.signature, "ingested": time.time()
        }
        path  = f"/mail/inbox/{packet.msg_id}.json"
        blob  = json.dumps(record, indent=2).encode("utf-8")
        tx_id = hashlib.sha256((packet.msg_id + str(time.time())).encode()).hexdigest()[:16]

        tx = DaemonTransaction(self, tx_id, path, blob, space_id=SPACE_USER)
        with self._lock:
            try:
                tx.begin(); self._record_journal_event(tx_id, "BEGIN", path, _JTX_OPEN)
                tx.write(); self._record_journal_event(tx_id, "WRITE", path, _JTX_OPEN)
                tx.commit(); self._record_journal_event(tx_id, "COMMIT", path, _JTX_COMMIT)
                self.inbox_log.append(record)
            except Exception as exc:
                tx.abort(str(exc)); self._record_journal_event(tx_id, f"ABORT:{exc}", path, _JTX_ABORT)
                raise

# ===========================================================================
# HTTP ROUTING ENGINE & INTERACTION UTILITIES
# ===========================================================================

def _make_handler(ctx: DaemonContext):
    class OdinWebHandler(BaseHTTPRequestHandler):
        log_message = lambda *a: None
        
        def _send_json(self, code: int, obj: dict):
            body = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/":
                # Clean abstraction: load from local web dashboard module if preferred
                # Splitting layout directly down to prevent main cluttering
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h1>OdinNet Phase-11 Ready</h1><p>Dashboard split ongoing.</p>")
            
            elif path.startswith("/api/resolve/"):
                # Backend URL parsing engine
                url_key = "burris://" + path[len("/api/resolve/"):]
                rec = ctx.fs.resolve_url(url_key)
                self._send_json(200 if rec else 404, rec or {"error": f"Link '{url_key}' could not be resolved by coordinate filesystem."})

        def do_POST(self):
            path = self.path.split("?")[0]
            if path == "/api/temporal/sync":
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}
                cid = body.get("channel_id")
                da  = body.get("date_a")
                db  = body.get("date_b")
                if not (cid and da and db):
                    self._send_json(400, {"error": "Missing temporal synchronization parameters."})
                    return
                ctx.register_temporal_sync(cid, da, db)
                self._send_json(200, {"status": "synchronized", "channel": cid})

    return OdinWebHandler

def main():
    ctx = DaemonContext()
    handler_cls = _make_handler(ctx)
    # Spock's recommendation implementation to support lightning multi-peer incoming packets
    server = ThreadingHTTPServer(("0.0.0.0", DEFAULT_PORT), handler_cls)
    print(f"📡 OdinNet Terminal Core Online on Port {DEFAULT_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()

if __name__ == "__main__":
    main()
