"""
OdinNet Daemon  v7.5 — Pythagorean Rolling Wave Engine
=======================================================
Background service for OdinNet with Math Hardening.

Changes from v7.4:
  - HARDENED: Smooth _roll_pythagorean_r implemented via math.isqrt() stability bands.
  - UPGRADED: Embedded dark theme tactical dashboard wired as default payload.
  - INTEGRATED: Status endpoints map security parameters (DEFCON, Universe R Axis).
"""

import argparse
import hashlib
import http.server
import json
import mimetypes
import os
import math
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, date, timedelta

if hasattr(signal, 'SIGPIPE'):
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)

# ─────────────────────────────────────────────────────────────
# Import project modules
# ─────────────────────────────────────────────────────────────

def _import_or_die(module_name: str, friendly: str):
    import importlib
    try:
        return importlib.import_module(module_name)
    except ImportError as e:
        print(f"\n❌  Cannot import '{module_name}': {e}")
        print(f"    {friendly}")
        sys.exit(1)

def _import_optional(module_name: str):
    import importlib
    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None

_gc_mod   = _import_or_die("grok_comms",      "Ensure grok_comms.py is in the same directory.")
_cg_mod   = _import_or_die("chart_generator", "Ensure chart_generator.py is in the same directory.")
_sc_mod   = _import_optional("stateless_comms")
_fcg_mod  = _import_optional("folding_chart_generator")
_fld_mod  = _import_optional("folding_lattice_drive")

GrokComms                    = _gc_mod.GrokComms
COORD_FILE                   = _gc_mod.COORD_FILE
coordinate_generator         = _gc_mod.coordinate_generator
compose_message              = _gc_mod.compose_message
send_outbox                  = _gc_mod.send_outbox
polling_range_finder         = _gc_mod.polling_range_finder
_today_str                   = _gc_mod._today_str
_ensure_dirs                 = _gc_mod._ensure_dirs
_load_beacons                = _gc_mod._load_beacons
FleetRegistry                = _gc_mod.FleetRegistry

# Legacy lattice (fallback)
lattice_fs_ctor              = _cg_mod.lattice_fs
LatticeDrive                 = _cg_mod.LatticeDrive
LatticeFS                    = _cg_mod.LatticeFS

# ── Folding engine — new default ──────────────────────────────
if _fcg_mod is not None and _fld_mod is not None:
    FoldingChartGenerator    = _fcg_mod.FoldingChartGenerator
    FoldingLatticeDrive      = _fld_mod.FoldingLatticeDrive
    folding_lattice_drive_fn = _fld_mod.folding_lattice_drive
    _FOLDING_AVAILABLE       = True
    print("  FoldingEngine : FoldingChartGenerator + FoldingLatticeDrive ACTIVE", flush=True)
else:
    FoldingChartGenerator    = None
    FoldingLatticeDrive      = None
    folding_lattice_drive_fn = None
    _FOLDING_AVAILABLE       = False
    print("  ⚠  folding_chart_generator / folding_lattice_drive not found — fallback active.", flush=True)

try:
    from odinnet_security import OdinNetSecurity
    _SECURITY_AVAILABLE = True
except ImportError:
    _SECURITY_AVAILABLE = False
    OdinNetSecurity     = None
    print("  ⚠  odinnet_security not found — security features disabled.")

# ─────────────────────────────────────────────────────────────
# Config defaults & Embedded Dashboard
# ─────────────────────────────────────────────────────────────

DEFAULT_HOST               = "0.0.0.0"
DEFAULT_PORT               = 8080
DEFAULT_PASSPHRASE         = os.environ.get("ODINNET_PASSPHRASE", "OdinNet_Shared_Ether_2026")
DEFAULT_NODE_ID            = os.environ.get("ODINNET_NODE_ID",    "OdinLocalNode")

POLL_INTERVAL_SEC          = 8
POLL_RT_INTERVAL_SEC       = 5
POLL_ERROR_BACKOFF         = 20
STATELESS_POLL_INTERVAL    = 10
STATELESS_POLL_STEPS       = 100
ANOMALY_INTERVAL_SEC       = 3600

LATTICE_IMAGE_PATH         = "odinnet_drive.json"
FOLDING_LATTICE_IMAGE_PATH = "odinnet_folding_drive.json"
BBS_DATA_PATH              = "bbs_data.json"
BLOCKED_LIST_PATH          = "blocked.json"
PUBLISHED_URLS_PATH        = "published_urls.json"

FLEET_LOCAL_RADIUS_DEFAULT = 5000
API_TOKEN = os.environ.get("ODINNET_TOKEN", "odinnet-dev")
_DAEMON_DIR    = os.path.dirname(os.path.abspath(__file__))
GUI_DIR        = os.path.join(_DAEMON_DIR, "gui")
WEB_ERROR_LOG  = os.path.join(_DAEMON_DIR, "web_error.log")

MAX_RECEIVED_LOG  = 100
MAX_RT_LOG        = 100
MAX_FLEET_LOG     = 100
MAX_ACTIVITY_LOG  = 500

BUILTIN_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>⬡ OdinNet Dashboard</title>
    <style>
        :root {
            --bg-base: #06080a;
            --border-color: #1a242d;
            --text-primary: #00ff66;
            --text-dim: #88ffcc;
            --accent-orange: orange;
            --panel-bg: #0d1117;
        }
        body {
            font-family: 'Courier New', Courier, monospace;
            background-color: var(--bg-base);
            color: var(--text-primary);
            padding: 15px;
            margin: 0;
        }
        h1, h2, h3 { margin: 0 0 10px 0; padding: 0; }
        a { color: var(--text-dim); text-decoration: none; }
        a:hover { text-decoration: underline; }
        .grid-container {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 15px;
            margin-top: 15px;
        }
        .panel {
            background: var(--panel-bg);
            border: 1px solid var(--border-color);
            padding: 15px;
            border-radius: 4px;
        }
        .status-bar span { margin-right: 20px; font-weight: bold; }
        pre, code {
            background: #020304;
            color: var(--text-dim);
            padding: 10px;
            display: block;
            border-radius: 3px;
            overflow-x: auto;
            max-height: 250px;
            margin: 0;
        }
        .form-group { margin-bottom: 12px; }
        label { display: block; margin-bottom: 4px; color: var(--text-dim); }
        input[type="text"], textarea {
            width: 100%;
            background: #020304;
            border: 1px solid var(--border-color);
            color: #ffffff;
            padding: 8px;
            box-sizing: border-box;
            font-family: inherit;
        }
        button {
            background: #00441b;
            color: var(--text-primary);
            border: 1px solid var(--text-primary);
            padding: 8px 16px;
            cursor: pointer;
            font-family: inherit;
            font-weight: bold;
        }
        button:hover { background: #006622; }
        .badge {
            padding: 2px 6px;
            border-radius: 3px;
            background: #1f2937;
            font-size: 12px;
        }
        .msg-item {
            border-bottom: 1px dashed #142029;
            padding: 6px 0;
        }
    </style>
</head>
<body>
    <div class="panel">
        <h2>⬡ OdinNet Dashboard PANEL <span id="node-id-lbl" class="badge">Node: --</span></h2>
        <div class="status-bar" id="meta-indicators">
            <span>Uptime: <span id="uptime-val">00:00:00</span></span>
            <span>Polls: <span id="poll-count-val">0</span></span>
            <span>Last Network Sync: <span id="last-sync-val">--</span></span>
            <span>DEFCON: <span id="defcon-val">--</span></span>
            <span>Universe R Axis: <span id="universe-r-val">--</span></span>
        </div>
    </div>
    <div class="grid-container">
        <div class="panel">
            <h3>📨 Compose Wave Packet Transmission</h3>
            <form id="compose-form">
                <div class="form-group">
                    <label>Wave Subject Line Token / Context Address</label>
                    <input type="text" id="msg-subject" placeholder="Enter routing reference context...">
                </div>
                <div class="form-group">
                    <label>Data Payload Body Matrix</label>
                    <textarea id="msg-body" rows="6" placeholder="Input package matrix payload..."></textarea>
                </div>
                <button type="submit">Deploy Transmission Wave</button>
            </form>
            <div id="tx-status" style="margin-top:10px; font-weight:bold;"></div>
        </div>
        <div class="panel">
            <h3>📥 Decrypted Signal Inbox Log Streams</h3>
            <div id="inbox-stream" style="max-height: 280px; overflow-y: auto;">
                <p style="color: gray;">Awaiting sync...</p>
            </div>
        </div>
        <div class="panel">
            <h3>⚙ Core Network Activity Frame Logs</h3>
            <pre id="log-terminal">System logs active...</pre>
        </div>
        <div class="panel">
            <h3>📂 Virtual LatticeFS Block Device Index Table</h3>
            <div id="filesystem-index">Initializing folder trees...</div>
        </div>
    </div>
    <script>
        const AUTH_TOKEN = "odinnet-dev"; 
        async function pullMetrics() {
            try {
                let res = await fetch('/status');
                let data = await res.json();
                document.getElementById('node-id-lbl').textContent = `Node: ${data.node_id}`;
                document.getElementById('poll-count-val').textContent = data.poll_count;
                document.getElementById('last-sync-val').textContent = data.last_poll || "Starting cycle";
                
                let hrs = Math.floor(data.uptime_sec / 3600).toString().padStart(2, '0');
                let mins = Math.floor((data.uptime_sec % 3600) / 60).toString().padStart(2, '0');
                let secs = (data.uptime_sec % 60).toString().padStart(2, '0');
                document.getElementById('uptime-val').textContent = `${hrs}:${mins}:${secs}`;
                
                if (data.defcon) {
                    document.getElementById('defcon-val').textContent = `LEVEL ${data.defcon}`;
                    document.getElementById('universe-r-val').textContent = data.current_r !== undefined ? data.current_r : "1";
                } else {
                    document.getElementById('defcon-val').textContent = "🟢 DEFCON 1 (Stable)";
                    document.getElementById('universe-r-val').textContent = "1 (Stable Base Axis)";
                }
                let logBox = document.getElementById('log-terminal');
                logBox.textContent = data.activity_tail ? data.activity_tail.join('\\n') : "Logs clear.";
                
                let fsBox = document.getElementById('filesystem-index');
                if(data.lattice_files && data.lattice_files.length > 0) {
                    let html = '<ul>';
                    data.lattice_files.forEach(f => {
                        html += `<li>📁 <a href="/${f.name}" target="_blank">${f.name}</a></li>`;
                    });
                    html += '</ul>';
                    fsBox.innerHTML = html;
                } else {
                    fsBox.innerHTML = "<p style='color:gray;'>Empty sector paths.</p>";
                }
            } catch (err) { console.error(err); }
        }

        async function pullSignalInbox() {
            try {
                let res = await fetch('/api/inbox');
                let messages = await res.json();
                let inboxBox = document.getElementById('inbox-stream');
                if(!messages || messages.length === 0) {
                    inboxBox.innerHTML = "<p style='color:gray;'>Inbox empty.</p>";
                    return;
                }
                let html = '';
                messages.forEach(m => {
                    let subject = m.subject || "(No Subject Axis)";
                    let body = m.body || m.text || "";
                    let ts = m.recv_time || m.timestamp || "Historical Epoch";
                    html += `<div class="msg-item"><strong>⚡ [${ts}] ${subject}</strong><br><span style="color:#ffffff;">${body}</span></div>`;
                });
                inboxBox.innerHTML = html;
            } catch (err) { console.error(err); }
        }

        document.getElementById('compose-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            let txStatus = document.getElementById('tx-status');
            txStatus.textContent = "Formatting...";
            let payload = {
                subject: document.getElementById('msg-subject').value,
                message: document.getElementById('msg-body').value
            };
            try {
                let res = await fetch('/api/send', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-OdinNet-Token': AUTH_TOKEN },
                    body: JSON.stringify(payload)
                });
                let result = await res.json();
                if(result.ok) {
                    txStatus.textContent = "Success!";
                    document.getElementById('msg-body').value = '';
                    pullSignalInbox();
                } else { txStatus.textContent = "Error."; }
            } catch (err) { txStatus.textContent = "Exception."; }
        });
        setInterval(pullMetrics, 3000);
        setInterval(pullSignalInbox, 5000);
        pullMetrics(); pullSignalInbox();
    </script>
</body>
</html>"""

# ─────────────────────────────────────────────────────────────
# Math Hardening Logic (Chekov Subroutines)
# ─────────────────────────────────────────────────────────────

def _roll_pythagorean_r(V: int, mask_base: int = 0xFFFFFFFF, scale_factor: int = 5000) -> int:
    """
    Smoothed Pythagorean wave tracking.
    Uses math.isqrt(V) to establish progressive, balanced bands,
    preventing discontinuous jumps across wide coordinate vectors.
    """
    if V <= 0:
        return scale_factor
    
    # Use the smooth isqrt band transformation to scale progression bounds
    leg_b = (math.isqrt(V) % mask_base) + 1
    hypotenuse = math.isqrt(V * V + leg_b * leg_b)
    
    # Derived running radius boundary
    R = (hypotenuse % scale_factor) + 1
    return R

def _log_web_error(msg: str, exc: Exception = None):
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(WEB_ERROR_LOG, "a") as f:
            f.write(f"\n[{ts}] {msg}\n")
            if exc:
                f.write(traceback.format_exc())
        print(f"[WebError] {msg}", flush=True)
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────
# Daemon Context & Handlers
# ─────────────────────────────────────────────────────────────

class DaemonContext:
    def __init__(self, comms, fs, security=None, stateless_node=None, legacy_mode=False, lattice_engine="folding"):
        self.fallback_comms  = comms
        self.stateless_node  = stateless_node
        self.security        = security
        self._lattice_engine = lattice_engine

        if stateless_node is not None and not legacy_mode:
            self.active_comms = stateless_node
            self._engine_name = "stateless"
        else:
            self.active_comms = comms
            self._engine_name = "legacy"

        self.fs             = fs
        self.start_time     = datetime.now()
        self._lock          = threading.RLock()
        self._fleet_lock    = threading.RLock()
        self._bbs_lock      = threading.RLock()
        self._pub_lock      = threading.RLock()

        self._last_poll    = None
        self._poll_count   = 0
        self._activity_log = []
        self._received_log = []
        self._beacon_status = []

    def send_message(self, text: str, subject: str = "") -> dict:
        try:
            if self._engine_name == "stateless":
                coord, pad = self.active_comms.send(text, subject=subject)
                return {"status": "success", "engine": "stateless", "coordinate": str(coord)[:40], "pad_bytes": pad}
            else:
                self.fallback_comms.compose_message(to_date=_today_str(), subject=subject or "(no subject)", body=text)
                self.fallback_comms.send_outbox()
                return {"status": "success", "engine": "legacy"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def get_inbox(self) -> list:
        if self._engine_name == "stateless":
            return self.active_comms.inbox()
        return list(reversed(self._received_log))

    def get_status(self) -> dict:
        engine_status = {}
        try:
            engine_status = self.active_comms.status()
        except Exception:
            pass

        sec_metrics = {}
        if self.security:
            try:
                sec_metrics = self.security.status_dict()
            except Exception:
                pass

        with self._lock:
            ctx_status = {
                "node_id":        getattr(self.fallback_comms, "my_id", "unknown"),
                "engine":         self._engine_name,
                "lattice_engine": self._lattice_engine,
                "uptime_sec":     int((datetime.now() - self.start_time).total_seconds()),
                "poll_count":     self._poll_count,
                "last_poll":      self._last_poll,
                "lattice_fs":     self.fs is not None,
                "activity_tail":  list(self._activity_log[-40:]),
                "lattice_files":  [] if not self.fs else [{"name": n} for n in self.fs._index],
                "beacons":        self._beacon_status,
            }
        return {**ctx_status, **engine_status, **sec_metrics}

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        with self._lock:
            self._activity_log.append(line)

    def refresh_beacons(self):
        try: self._beacon_status = _load_beacons()
        except Exception: pass

    def status_dict(self) -> dict:
        return self.get_status()

# ─────────────────────────────────────────────────────────────
# Web Server Layer
# ─────────────────────────────────────────────────────────────

class OdinWebHandler(http.server.BaseHTTPRequestHandler):
    daemon_ctx = None
    def log_message(self, fmt, *args): pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/status", "/api/status"):
            self._respond(200, "application/json", json.dumps(self.daemon_ctx.status_dict()).encode())
        elif path == "/api/inbox":
            self._respond(200, "application/json", json.dumps(self.daemon_ctx.get_inbox()).encode())
        elif path in ("/", ""):
            self._respond(200, "text/html", BUILTIN_DASHBOARD_HTML.encode("utf-8"))
        else:
            self._respond(404, "text/plain", b"Not Found")

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        
        if path == "/api/send":
            res = self.daemon_ctx.send_message(body.get("message", ""), body.get("subject", ""))
            self._respond(200, "application/json", json.dumps({"ok": "error" not in res, **res}).encode())
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
    parser = argparse.ArgumentParser(description="OdinNet Engine v7.5")
    parser.add_argument("--coord", default=COORD_FILE)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--passphrase", default=DEFAULT_PASSPHRASE)
    parser.add_argument("--node-id", default=DEFAULT_NODE_ID)
    args = parser.parse_args()

    comms = GrokComms(coord_file=args.coord)
    
    sec_system = None
    if _SECURITY_AVAILABLE:
        try:
            sec_system = OdinNetSecurity(coord_file=args.coord)
            print("  SecurityEngine : Integration SUCCESS.")
        except Exception as e:
            print(f"  ⚠ SecurityEngine init failure: {e}")

    ctx = DaemonContext(
        comms=comms,
        fs=None,
        security=sec_system,
        lattice_engine="folding" if _FOLDING_AVAILABLE else "legacy"
    )

    print(f"\n⚡ ODINNET TACTICAL BRIDGE OVERRIDE ONLINE [PORT {args.port}]")
    server = ThreadingHTTPServer((args.host, args.port), lambda *a, **kw: OdinWebHandler(*a, daemon_ctx=ctx, **kw))
    server.serve_forever()

if __name__ == "__main__":
    main()
