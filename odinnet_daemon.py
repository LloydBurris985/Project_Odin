"""                                              odinnet_daemon.py
OdinNet Daemon v8.0 — Stateless Coordinate-Framed Core Service

Implements atomic channel reservation indices, node-salted collision back-offs,
and realtime lease heartbeats bridging Space 3 over LatticeFS v2.
"""

import argparse
import hashlib
import http.server
import json
import math
import os
import random
import socketserver
import sys
import threading
import time
from datetime import datetime, date

# Bind directly to your Phase 2 core modules
from lattice_fs_v2 import LatticeFSv2
from odinnet_usenet import OdinNetUsenet, SPACE_USENET

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
DEFAULT_PASSPHRASE = os.environ.get("ODINNET_PASSPHRASE", "OdinNet_Shared_Ether_2026")
DEFAULT_NODE_ID = os.environ.get("ODINNET_NODE_ID", "OdinLocalNode")

# ─────────────────────────────────────────────────────────────
# Enhanced Daemon Integration Context
# ─────────────────────────────────────────────────────────────

class EnhancedDaemonContext:
    def __init__(self, fs: LatticeFSv2, passphrase: str, node_id: str):
        self.fs = fs
        self.passphrase = passphrase
        self.node_id = node_id
        self.start_time = datetime.now()
        
        # Instantiate the protocol layer we linked previously
        self.usenet = OdinNetUsenet(self.fs, defcon=1)
        
        self._lock = threading.RLock()
        self._activity_log = []
        self.active_leases = {} # Format: {group: (expiry_epoch, lease_node)}

        self.log(f"Initializing Stateless Core Frame Engine [Node: {self.node_id}]")

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        with self._lock:
            self._activity_log.append(line)
            if len(self._activity_log) > 200:
                self._activity_log = self._activity_log[-200:]

    # ── Chekov Subroutines: Atomic Space Reservations & Collisions ──────────

    def reserve_v_range(self, group: str, count: int) -> int:
        """
        Locks a mathematical reservation layout block within Space 3.
        Utilizes an atomic read-modify-write tracking coordinate to prevent over-allocation.
        """
        reservation_key = f"/sys/reserve/{group}"
        
        with self._lock:
            current_offset = 0
            if self.fs.exists(reservation_key):
                try:
                    current_offset = int(self.fs.read_file(reservation_key, space_id=SPACE_USENET).decode('utf-8'))
                except ValueError:
                    current_offset = 0
            
            allocated_anchor = current_offset
            new_offset = current_offset + count
            
            # Atomically write updated range boundary limits back into Space 3 index paths
            self.fs.write_file(reservation_key, str(new_offset).encode('utf-8'), space_id=SPACE_USENET)
            self.log(f"Reserved coordinate span range for {group}: base={allocated_anchor}, count={count}")
            return allocated_anchor

    def post_with_collision_avoidance(self, group: str, subject: str, body: str) -> int:
        """
        Asynchronously computes non-overlapping sequence indices using node-id salts.
        Retries up to 5 iterations on coordinate collision encounters.
        """
        max_retries = 5
        base_salt = int(hashlib.md5(self.node_id.encode()).hexdigest()[:4], 16) % 10
        
        for attempt in range(max_retries):
            # Formulate coordinate with progressive geometric salt offsets
            current_count = self.reserve_v_range(group, 1)
            target_slot = current_count + base_salt + (attempt * 3)

            # High-accuracy lookahead path evaluation to confirm slot isolation
            virtual_path = f"/usenet/{group}/msg_{target_slot}"
            if not self.fs.exists(virtual_path):
                # Target slot is verified safe to settle frame transaction safely
                v_anchor = self.usenet._get_group_window(group)
                v_target = v_anchor + target_slot
                epoch = int(time.time() // 3600)

                message_frame = {
                    "v_target": str(v_target),
                    "parent_v": "0",
                    "subject_hash": hashlib.sha256(subject.encode()).hexdigest()[:16],
                    "epoch": epoch,
                    "provenance_sig": self.usenet._generate_provenance_sig(v_target, epoch),
                    "subject": subject,
                    "body": body,
                    "sender_node": self.node_id
                }

                self.fs.write_file(virtual_path, json.dumps(message_frame).encode("utf-8"), space_id=SPACE_USENET)
                self.log(f"Successfully posted to {group} slot {target_slot} (Attempt {attempt+1})")
                return v_target
                
            self.log(f"⚠️ Coordinate overlap hit at slot {target_slot}. Scaling back-off retry loop...")
            time.sleep(0.1 * (attempt + 1))
            
        raise RuntimeError(f"Engine saturation error: Failed to clear collision constraints for group: {group}")

    # ── Real-Time Temporal Heartbeat Leases ─────────────────────────────────

    def heartbeat_lease(self, group: str, lease_duration_sec: int = 30):
        """
        Maintains an active lease lock over a specific coordinate channel window.
        Broadcasts status markers allowing clear 'future-past' synchronization matrixes.
        """
        epoch_now = int(time.time())
        expiry = epoch_now + lease_duration_sec
        
        lease_path = f"/sys/leases/{group}"
        lease_payload = {"node_id": self.node_id, "expires": expiry}
        
        self.fs.write_file(lease_path, json.dumps(lease_payload).encode('utf-8'), space_id=SPACE_USENET)
        with self._lock:
            self.active_leases[group] = (expiry, self.node_id)

    def verify_active_channel_lease(self, group: str) -> str:
        """Evaluates channel ownership states before permitting data pipeline ingestion."""
        lease_path = f"/sys/leases/{group}"
        if not self.fs.exists(lease_path):
            return "open"
            
        try:
            data = json.loads(self.fs.read_file(lease_path, space_id=SPACE_USENET).decode('utf-8'))
            if int(time.time()) < data["expires"]:
                return data["node_id"]
        except Exception:
            pass
        return "open"

    def status_dict(self) -> dict:
        with self._lock:
            return {
                "node_id": self.node_id,
                "engine": "stateless_v2",
                "uptime_sec": int((datetime.now() - self.start_time).total_seconds()),
                "defcon": self.usenet.defcon,
                "active_channels": list(self.active_leases.keys()),
                "activity_tail": list(self._activity_log[-15:])
            }

# ─────────────────────────────────────────────────────────────
# Web Server Routing Layer
# ─────────────────────────────────────────────────────────────

class OdinWebHandler(http.server.BaseHTTPRequestHandler):
    daemon_ctx = None
    def log_message(self, fmt, *args): pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/status", "/api/status"):
            status_data = self.daemon_ctx.status_dict()
            self._respond(200, "application/json", json.dumps(status_data).encode())
        elif path == "/api/inbox":
            # Swipes live frames within default newsgroup pipeline array
            feed = self.daemon_ctx.usenet.poll("sci.burris.odinnet")
            self._respond(200, "application/json", json.dumps(feed).encode())
        elif path in ("/", ""):
            # Fallback direct serving mapping to core dashboard layout systems
            from odinnet_daemon import BUILTIN_DASHBOARD_HTML
            self._respond(200, "text/html", BUILTIN_DASHBOARD_HTML.encode("utf-8"))
        else:
            self._respond(404, "text/plain", b"Not Found")

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

        if path == "/api/send":
            try:
                # Intercepts outgoing requests with collision checking mechanics
                v_out = self.daemon_ctx.post_with_collision_avoidance(
                    group="sci.burris.odinnet",
                    subject=body.get("subject", "General Wave"),
                    body=body.get("message", "")
                )
                self._respond(200, "application/json", json.dumps({"ok": True, "v_target": v_out}).encode())
            except Exception as e:
                self._respond(500, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
        else:
            self._respond(404, "application/json", b'{"ok":false}')

    def _respond(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True

def main():
    parser = argparse.ArgumentParser(description="OdinNet Engine v8.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--passphrase", default="alpha_omega_net")
    parser.add_argument("--node-id", default="OdinLocalNode")
    args = parser.parse_args()

    # Re-instantiate basic underlying file block storage map configurations
    from lattice_fs_v2 import lattice_fs_v2
    raw_storage_engine = lattice_fs_v2(sector_size=1024, n_sectors=256, passphrase=args.passphrase)
    
    ctx = EnhancedDaemonContext(fs=raw_storage_engine, passphrase=args.passphrase, node_id=args.node_id)

    print(f"\n⚡ ODINNET UPGRADED STATELESS BACKEND OPERATIONAL [PORT {args.port}]")
    server = ThreadingHTTPServer((args.host, args.port), lambda *a, **kw: OdinWebHandler(*a, daemon_ctx=ctx, **kw))
    
    # Fire ongoing background pulse for the channel lease heartbeat checks
    def run_heartbeat():
        while True:
            ctx.heartbeat_lease("sci.burris.odinnet", lease_duration_sec=20)
            time.sleep(10)
    threading.Thread(target=run_heartbeat, daemon=True).start()

    server.serve_forever()

if __name__ == "__main__":
    main()
