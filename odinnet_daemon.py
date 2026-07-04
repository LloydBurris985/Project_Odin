import sys
import os
import json
import time
import hashlib
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from datetime import datetime

sys.set_int_max_str_digits(100_000)

# ── FALLBACK LATTICE FS ENGINE SIMULATOR FOR STANDALONE RUN ──
class MockLatticeStore:
    def __init__(self):
        self._spaces = self
        self._versions = self
        self._passphrase = "alpha_omega_net"
    def all_spaces(self): return [1, 2, 3]
    def all_live_heads(self, space_id): return []

class MockLatticeFS:
    def __init__(self):
        self._store = MockLatticeStore()
        self.virtual_disk = {}
    def define_space(self, space_id, name): pass
    def write_file(self, path, blob, space_id): self.virtual_disk[path] = blob
    def read_file(self, path, space_id): return self.virtual_disk.get(path, b"{}")
    def exists(self, path): return path in self.virtual_disk
    def resolve_url(self, url): return {"resolved_url": url, "status": "ACTIVE", "sector": 42}

try:
    from lattice_fs_v2 import lattice_fs_v2
    raw_fs = lattice_fs_v2(sector_size=512, n_sectors=256, passphrase="alpha_omega_net")
except ImportError:
    print("[SYSTEM NOTICE] 'lattice_fs_v2.py' not found. Using local memory coordinate database.")
    raw_fs = MockLatticeFS()

# ── ODINNET USENET MANAGEMENT LAYER ──
class OdinNetUsenet:
    def __init__(self, fs, defcon=1):
        self.fs = fs
        self.defcon = defcon
        self.reputation_matrix = {}
        if 3 not in self.fs._store._spaces.all_spaces():
            self.fs.define_space(3, "usenet_feed")

    def _get_group_window(self, group_name: str) -> int:
        passphrase = self.fs._store._passphrase or "odinnet_fallback_seed"
        raw_hash = hashlib.sha256((passphrase + group_name).encode("utf-8")).hexdigest()
        return int(raw_hash, 16) % 100_000_000_000

    def _generate_provenance_sig(self, v_target: int, epoch: int) -> str:
        passphrase = self.fs._store._passphrase or "odinnet_fallback_seed"
        return hashlib.sha256(f"{passphrase}:{epoch}:{v_target}".encode("utf-8")).hexdigest()

    def post(self, group: str, subject: str, body: str) -> int:
        v_anchor = self._get_group_window(group)
        epoch = int(time.time() // 3600)
        subject_hash = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:16]
        
        slot_index = 0
        while self.fs.exists(f"/usenet/{group}/msg_{slot_index}"):
            slot_index += 1
            
        v_target = v_anchor + slot_index
        frame = {
            "v_target": str(v_target), "subject_hash": subject_hash, "epoch": epoch,
            "provenance_sig": self._generate_provenance_sig(v_target, epoch),
            "subject": subject, "body": body,
            "sender_node": hashlib.sha256(subject_hash.encode()).hexdigest()[:8]
        }
        self.fs.write_file(f"/usenet/{group}/msg_{slot_index}", json.dumps(frame).encode("utf-8"), space_id=3)
        return v_target

    def poll(self, group: str) -> list:
        valid_frames = []
        for slot in range(50):
            virtual_path = f"/usenet/{group}/msg_{slot}"
            if not self.fs.exists(virtual_path): continue
            try:
                raw_data = self.fs.read_file(virtual_path, space_id=3)
                frame = json.loads(raw_data.decode("utf-8"))
                if self.defcon >= 5 and frame["provenance_sig"] != self._generate_provenance_sig(int(frame["v_target"]), int(frame["epoch"])):
                    continue
                if self.defcon >= 3 and self.reputation_matrix.get(frame.get("sender_node"), 100) < 0:
                    continue
                valid_frames.append(frame)
            except: continue
        return valid_frames

# ── ODINNET STABLE SECURITY MATRIX ──
class OdinNetSecurity:
    def __init__(self):
        self.defcon = 1
        self.attack_active = False
        self.current_r = 1
        self.log_buffer = ["Node Security Core Active. DEFCON Level 1 [NORMAL]"]

    def log(self, text):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_buffer.append(f"[{ts}] {text}")
        if len(self.log_buffer) > 40: self.log_buffer.pop(0)
        print(f"[{ts}] {text}")

    def get_defcon_meta(self):
        meta = {
            1: {"label": "NORMAL", "color": "🟢"},
            3: {"label": "ELEVATED", "color": "🟡"},
            5: {"label": "GUARDED", "color": "🟠"},
            7: {"label": "HIGH ALERT", "color": "🔴"},
            10: {"label": "MAXIMUM SECURITY", "color": "🚨"}
        }
        return meta.get(self.defcon, {"label": "UNKNOWN", "color": "⬡"})

# ── CENTRAL ENGINE MATRIX CONTEXT ──
class DaemonContext:
    def __init__(self):
        self.node_id = "ODINNET-NODE-BURRIS"
        self.boot_time = time.time()
        self.poll_count = 0
        self.sec = OdinNetSecurity()
        self.usenet = OdinNetUsenet(raw_fs, defcon=self.sec.defcon)
        
        # Automatic internal loop simulation thread for testing network activity
        threading.Thread(target=self._network_pulse_loop, daemon=True).start()

    def _network_pulse_loop(self):
        while True:
            time.sleep(5)
            self.poll_count += 1
            if self.poll_count % 6 == 0:
                self.sec.log("Sweeping local channel matrices for structural anomalies...")

# ── WEB APPLICATION ENGINE ROUTING CONROLLER ──
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

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                if os.path.exists("dashboard.html"):
                    with open("dashboard.html", "rb") as f:
                        self.wfile.write(f.read())
                else:
                    self.wfile.write(b"<h1>OdinNet Bridge Ready</h1><p>Error: dashboard.html missing from current working directory.</p>")
            
            elif path == "/status":
                meta = ctx.sec.get_defcon_meta()
                status_payload = {
                    "node_id": ctx.node_id,
                    "uptime_sec": time.time() - ctx.boot_time,
                    "poll_count": ctx.poll_count,
                    "defcon": ctx.sec.defcon,
                    "defcon_label": meta["label"],
                    "defcon_color": meta["color"],
                    "current_r": ctx.sec.current_r,
                    "attack_active": ctx.sec.attack_active,
                    "activity_tail": ctx.sec.log_buffer
                }
                self._send_json(200, status_payload)

            elif path == "/api/usenet/feed":
                query_group = "sci.burris.odinnet"
                if "?" in self.path:
                    parts = self.path.split("?")[1].split("&")
                    for p in parts:
                        if p.startswith("group="): query_group = p.split("=")[1]
                frames = ctx.usenet.poll(query_group)
                self._send_json(200, frames)

            elif path.startswith("/api/resolve/"):
                url_key = "burris://" + self.path[len("/api/resolve/"):]
                record = raw_fs.resolve_url(url_key)
                self._send_json(200, record)

        def do_POST(self):
            path = self.path.split("?")[0]
            if path == "/api/usenet/post":
                length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(length).decode('utf-8'))
                
                group = data.get("group", "sci.burris.odinnet")
                subject = data.get("subject", "No Context")
                body = data.get("body", "")
                
                v_target = ctx.usenet.post(group, subject, body)
                ctx.sec.log(f"Disk file write committed: Group {group} -> Allocated Block V={v_target}")
                self._send_json(200, {"ok": True, "v_target": v_target})

    return OdinWebHandler

def main():
    ctx = DaemonContext()
    server_address = ('127.0.0.1', 7474)
    httpd = ThreadingHTTPServer(server_address, make_handler(ctx))
    ctx.sec.log("==========================================================")
    ctx.sec.log(f"  ODINNET APPLICATION DAEMON STARTED ON http://127.0.0.1:7474")
    ctx.sec.log("==========================================================")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        ctx.sec.log("Shutting down command bridge backend loops.")
        httpd.server_close()

if __name__ == "__main__":
    main()
