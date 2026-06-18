#!/usr/bin/env python3
"""
OdinNet Daemon  v4
==================
Background service for OdinNet.

Changes from v3:
  - DaemonContext gets threading.RLock — all shared list writes and status_dict
    reads are now protected (PATCH v4, critical fix for daemon poller race)
  - OdinNetSecurity integrated: security.status_dict() included in /status JSON
  - /api/security/* endpoints: raise_defcon, lower_defcon, declare_attack, cleared
  - /api/* endpoints require X-OdinNet-Token header (simple bearer token auth)
  - API_TOKEN loaded from ODINNET_TOKEN env var or defaults to 'odinnet-dev'
  - beacon_status refreshed through security.enforce_beacons() filter
  - Startup banner shows DEFCON level and security status

Usage:
  python odinnet_daemon.py
  python odinnet_daemon.py --port 9090
  python odinnet_daemon.py --coord my_coord.json --port 8080
  python odinnet_daemon.py --seed-messages
  python odinnet_daemon.py --no-web
  ODINNET_TOKEN=mysecret python odinnet_daemon.py
"""

import argparse
import http.server
import json
import mimetypes
import os
import socketserver
import sys
import threading
import time
from datetime import datetime, date

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

_gc_mod  = _import_or_die("grok_comms",      "Ensure grok_comms.py is in the same directory.")
_cg_mod  = _import_or_die("chart_generator", "Ensure chart_generator.py is in the same directory.")

GrokComms            = _gc_mod.GrokComms
COORD_FILE           = _gc_mod.COORD_FILE
coordinate_generator  = _gc_mod.coordinate_generator
compose_message      = _gc_mod.compose_message
send_outbox          = _gc_mod.send_outbox
polling_range_finder  = _gc_mod.polling_range_finder
_today_str           = _gc_mod._today_str
_ensure_dirs         = _gc_mod._ensure_dirs
_load_beacons        = _gc_mod._load_beacons

lattice_fs_ctor      = _cg_mod.lattice_fs
LatticeDrive         = _cg_mod.LatticeDrive
LatticeFS            = _cg_mod.LatticeFS

# Security — optional; daemon degrades gracefully without it
try:
    from odinnet_security import OdinNetSecurity
    _SECURITY_AVAILABLE = True
except ImportError:
    _SECURITY_AVAILABLE = False
    OdinNetSecurity     = None
    print("  ⚠  odinnet_security not found — security features disabled.")

# ─────────────────────────────────────────────────────────────
# Config defaults
# ─────────────────────────────────────────────────────────────

DEFAULT_HOST          = "127.0.0.1"
DEFAULT_PORT          = 8080
POLL_INTERVAL_SEC     = 15
POLL_ERROR_BACKOFF    = 30
LATTICE_IMAGE_PATH    = "odinnet_drive.json"

# API token — set ODINNET_TOKEN env var in production
API_TOKEN = os.environ.get("ODINNET_TOKEN", "odinnet-dev")

_DAEMON_DIR     = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_HTML  = os.path.join(_DAEMON_DIR, "dashboard.html")

MAX_RECEIVED_LOG = 100
MAX_RT_LOG       = 100
MAX_ACTIVITY_LOG = 500

# ─────────────────────────────────────────────────────────────
# MIME helpers
# ─────────────────────────────────────────────────────────────

_EXTRA_MIME = {
    ".json": "application/json",
    ".md":   "text/markdown; charset=utf-8",
    ".txt":  "text/plain; charset=utf-8",
    ".bin":  "application/octet-stream",
    ".py":   "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
}

def _mime_for(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext in _EXTRA_MIME:
        return _EXTRA_MIME[ext]
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


# ─────────────────────────────────────────────────────────────
# LatticeFS bootstrap
# ─────────────────────────────────────────────────────────────

def _try_load_lattice_fs(passphrase: str = None) -> "LatticeFS | None":
    try:
        if os.path.exists(LATTICE_IMAGE_PATH):
            drive = LatticeDrive()
            drive.load(LATTICE_IMAGE_PATH)
            fs = LatticeFS(drive, passphrase=passphrase)
            print(f"  LatticeFS  : loaded from {LATTICE_IMAGE_PATH}")
            return fs

        fs = lattice_fs_ctor(
            sector_size = 1024,
            n_sectors   = 256,
            passphrase  = passphrase,
        )
        _seed_lattice_fs(fs)
        fs._drive.save(LATTICE_IMAGE_PATH)
        print(f"  LatticeFS  : created fresh → {LATTICE_IMAGE_PATH}")
        return fs

    except Exception as e:
        print(f"  ⚠  LatticeFS init failed: {e}")
        return None


def _seed_lattice_fs(fs: "LatticeFS"):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if os.path.exists(DASHBOARD_HTML):
        with open(DASHBOARD_HTML, "rb") as fh:
            dashboard_bytes = fh.read()
        print(f"  LatticeFS  : loaded dashboard.html from {DASHBOARD_HTML}")
    else:
        dashboard_bytes = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="10;url=/status">
  <title>OdinNet Node</title>
  <style>
    body {{ font-family: monospace; background: #07090d; color: #00e87a;
            max-width: 600px; margin: 60px auto; padding: 0 24px; }}
  </style>
</head>
<body>
  <h1>⬡ OdinNet Node — Online</h1>
  <p>Seeded: {now}</p>
  <p>Dashboard not found — place <code>dashboard.html</code> next to
     <code>odinnet_daemon.py</code> and restart to enable the full GUI.</p>
  <p><a href="/status" style="color:#00ccff">JSON status →</a></p>
</body>
</html>
""".encode("utf-8")
        print("  LatticeFS  : dashboard.html not found — seeding fallback")

    readme = f"""OdinNet Node
============
Burris Numerical System — OdinNet daemon running.

Node started : {now}
LatticeFS    : mounted (sector_size=1024, n_sectors=256)
""".encode("utf-8")

    fs.write_file("index.html",   dashboard_bytes)
    fs.write_file("readme.txt",   readme)
    print("  LatticeFS  : seeded index.html (dashboard) + readme.txt")


def _update_dashboard_in_fs(fs: "LatticeFS") -> bool:
    if not os.path.exists(DASHBOARD_HTML):
        return False
    try:
        with open(DASHBOARD_HTML, "rb") as fh:
            fresh = fh.read()
        if fs.exists("index.html"):
            stored = fs.read_file("index.html")
            if stored == fresh:
                return False
        fs.write_file("index.html", fresh)
        print("  LatticeFS  : dashboard.html refreshed in LatticeFS")
        return True
    except Exception as e:
        print(f"  ⚠  dashboard refresh failed: {e}")
        return False


def _save_lattice(fs: "LatticeFS"):
    try:
        fs._drive.save(LATTICE_IMAGE_PATH)
    except Exception as e:
        print(f"  ⚠  LatticeFS save failed: {e}")


# ─────────────────────────────────────────────────────────────
# Auto coordinate generation
# ─────────────────────────────────────────────────────────────

def _ensure_coordinate(coord_file: str) -> bool:
    if os.path.exists(coord_file):
        return True

    import socket
    print(f"\n[Init] Coordinate file '{coord_file}' not found.")
    print(f"[Init] Auto-generating coordinate for this node...")

    try:
        hostname   = socket.gethostname()
        timestamp  = datetime.now().strftime("%Y%m%d%H%M%S")
        passphrase = f"odinnet-{hostname}-{timestamp}"

        coordinate_generator(passphrase, num_digits=150, output_file=coord_file)

        coord = json.load(open(coord_file))
        coord["message_length"] = 64
        json.dump(coord, open(coord_file, "w"), indent=2)

        print(f"[Init] Computing polling range (30 samples)...")
        polling_range_finder(num_samples=30, coord_file=coord_file)

        print(f"[Init] ✅ Node coordinate ready → {coord_file}")
        print(f"[Init]    Passphrase used: {passphrase}")
        print(f"[Init]    ⚠  Save this passphrase to recover your identity.")
        return True

    except Exception as e:
        print(f"[Init] ❌ Auto-generation failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────
# Test message seeding
# ─────────────────────────────────────────────────────────────

_SEED_MESSAGES = [
    {
        "subject": "OdinNet Node Online",
        "body":    (
            "This node has joined the Burris coordinate network.\n"
            "OdinNet daemon is running and polling is active.\n"
            "Temporal + realtime channels are open."
        ),
    },
    {
        "subject": "Burris System Status",
        "body":    (
            "ChartGenerator encode/decode paths: NOMINAL\n"
            "LatticeFS mount status: see /status endpoint\n"
            "Polling window: calibrated from coordinate\n"
            "Beacon registry: check beacons.json"
        ),
    },
    {
        "subject": "Welcome to the Informational Universe",
        "body":    (
            "You are navigating coordinate space.\n"
            "Every byte is a position. Every position is a message.\n"
            "The Burris Numerical System encodes meaning into arithmetic.\n"
            "Safe travels through the galactic coordinate field."
        ),
    },
]

def seed_test_messages(coord_file: str = COORD_FILE, force: bool = False) -> int:
    _ensure_dirs()
    outbox_dir = "outbox"
    existing = (
        [f for f in os.listdir(outbox_dir) if f.endswith(".json")]
        if os.path.exists(outbox_dir) else []
    )
    if existing and not force:
        print(f"[Seed] Outbox already has {len(existing)} message(s) — skipping.")
        return 0

    today = _today_str()
    count = 0
    for msg in _SEED_MESSAGES:
        try:
            compose_message(
                to_date    = today,
                subject    = msg["subject"],
                body       = msg["body"],
                coord_file = coord_file,
            )
            count += 1
        except Exception as e:
            print(f"[Seed] ⚠  Failed '{msg['subject']}': {e}")

    print(f"[Seed] ✅ {count} test message(s) seeded into outbox/")
    return count


# ─────────────────────────────────────────────────────────────
# Daemon context  (PATCH v4: RLock on all shared state)
# ─────────────────────────────────────────────────────────────

class DaemonContext:
    """
    Shared state for poller thread + HTTP handler.

    PATCH v4:
      - threading.RLock protects all writes to activity_log, received_log,
        rt_log, beacon_status, poll_count, last_poll
      - status_dict() takes the lock for a consistent snapshot
      - Security status included in status_dict() output
    """

    def __init__(
        self,
        comms:    "GrokComms",
        fs:       "LatticeFS | None",
        security: "OdinNetSecurity | None" = None,
    ):
        self.comms         = comms
        self.fs            = fs
        self.security      = security          # PATCH v4: security controller
        self.start_time    = datetime.now()
        self._lock         = threading.RLock() # PATCH v4: critical fix

        # Protected state — always access under self._lock
        self._last_poll     = None
        self._poll_count    = 0
        self._activity_log  = []
        self._received_log  = []
        self._rt_log        = []
        self._beacon_status = []

    # ── Lock-safe accessors ───────────────────────────────────────────────

    @property
    def last_poll(self) -> str | None:
        with self._lock:
            return self._last_poll

    @last_poll.setter
    def last_poll(self, value: str):
        with self._lock:
            self._last_poll = value

    @property
    def poll_count(self) -> int:
        with self._lock:
            return self._poll_count

    # ── Logging ───────────────────────────────────────────────────────────

    def log(self, msg: str):
        ts   = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        with self._lock:
            self._activity_log.append(line)
            if len(self._activity_log) > MAX_ACTIVITY_LOG:
                self._activity_log = self._activity_log[-MAX_ACTIVITY_LOG:]

    # ── Message ingestion ─────────────────────────────────────────────────

    def add_temporal_received(self, records: list):
        """Prepend temporal records; stamp msg_type if missing."""
        for r in records:
            r.setdefault("msg_type", "PRIVATE")
            r.setdefault("type",     "temporal")
        with self._lock:
            self._received_log = records + self._received_log
            if len(self._received_log) > MAX_RECEIVED_LOG:
                self._received_log = self._received_log[:MAX_RECEIVED_LOG]

    def add_rt_received(self, records: list):
        """Prepend realtime records."""
        for r in records:
            r.setdefault("msg_type", "PRIVATE")
            r.setdefault("type",     "realtime")
        with self._lock:
            self._rt_log = records + self._rt_log
            if len(self._rt_log) > MAX_RT_LOG:
                self._rt_log = self._rt_log[:MAX_RT_LOG]

    # ── Beacon refresh ────────────────────────────────────────────────────

    def refresh_beacons(self):
        """
        Reload beacon registry from disk.
        PATCH v4: If security is available, filter through enforce_beacons()
        so expelled / blacklisted beacons never reach the poller.
        """
        try:
            raw = _load_beacons()
            if self.security:
                approved, expelled = self.security.enforce_beacons(raw)
                if expelled:
                    self.log(f"[Security] {len(expelled)} beacon(s) expelled by DEFCON policy")
                raw = approved
            with self._lock:
                self._beacon_status = raw
        except Exception as e:
            self.log(f"refresh_beacons error: {e}")

    # ── Poll accounting ───────────────────────────────────────────────────

    def _tick_poll(self):
        with self._lock:
            self._poll_count    += 1
            self._last_poll      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── Status snapshot ───────────────────────────────────────────────────

    def status_dict(self) -> dict:
        """
        Build the full /status JSON payload.
        PATCH v4: single lock acquisition for consistent snapshot;
        security status appended when available.
        """
        with self._lock:
            activity_tail  = list(self._activity_log[-80:])
            received_log   = list(self._received_log[:10])
            rt_log         = list(self._rt_log[:10])
            beacon_status  = list(self._beacon_status)
            poll_count     = self._poll_count
            last_poll      = self._last_poll
            received_count = len(self._received_log)
            rt_count       = len(self._rt_log)

        uptime_sec = int((datetime.now() - self.start_time).total_seconds())
        fs         = self.fs

        poll_range_set = False
        try:
            with open(self.comms.coord_file) as fh:
                coord = json.load(fh)
            poll_range_set = bool(
                coord.get("polling_low") and coord.get("polling_high")
            )
        except Exception:
            coord = {}

        lfs_files = []
        if fs:
            for name, entry in sorted(fs._index.items()):
                lfs_files.append({
                    "name":        name,
                    "byte_length": entry.get("byte_length", 0),
                })

        result = {
            "node_id":          self.comms.my_id,
            "coord_file":       self.comms.coord_file,
            "uptime_sec":       uptime_sec,
            "poll_count":       poll_count,
            "last_poll":        last_poll,
            "poll_range_set":   poll_range_set,
            "lattice_fs":       fs is not None,
            "lattice_files":    lfs_files,
            "received_count":   received_count,
            "recent_received":  received_log,
            "rt_count":         rt_count,
            "recent_realtime":  rt_log,
            "beacons": [
                {
                    "name":       b.get("name", "?"),
                    "coordinate": b.get("coordinate", ""),
                    "notes":      b.get("notes", ""),
                }
                for b in beacon_status
            ],
            "activity_tail": activity_tail,
        }

        # PATCH v4: append security status if available
        if self.security:
            try:
                result["security"] = self.security.status_dict()
            except Exception as e:
                result["security"] = {"error": str(e)}
        else:
            result["security"] = None

        return result


# ─────────────────────────────────────────────────────────────
# Background poller thread
# ─────────────────────────────────────────────────────────────

def _background_poller(ctx: DaemonContext):
    ctx.log("Background poller started.")
    ctx.refresh_beacons()

    while True:
        try:
            temporal_results = ctx.comms.poll()
            ctx.log(f"Temporal poll: {len(temporal_results)} message(s)")
            if temporal_results:
                ctx.add_temporal_received(temporal_results)

            rt_results = ctx.comms.poll_realtime()
            ctx.log(f"Realtime poll: {len(rt_results)} message(s)")
            if rt_results:
                ctx.add_rt_received(rt_results)

            ctx.refresh_beacons()
            ctx._tick_poll()

            time.sleep(POLL_INTERVAL_SEC)

        except KeyboardInterrupt:
            break
        except Exception as e:
            ctx.log(f"Polling error: {e}")
            time.sleep(POLL_ERROR_BACKOFF)


# ─────────────────────────────────────────────────────────────
# API token auth helper
# ─────────────────────────────────────────────────────────────

def _check_token(handler: "OdinWebHandler") -> bool:
    """
    Validate X-OdinNet-Token header for /api/* endpoints.
    Returns True if token is valid, False and sends 401 if not.
    """
    token = handler.headers.get("X-OdinNet-Token", "")
    if token == API_TOKEN:
        return True
    handler._respond(
        401,
        "application/json",
        json.dumps({"ok": False, "error": "Unauthorized — X-OdinNet-Token required"}).encode(),
    )
    return False


# ─────────────────────────────────────────────────────────────
# OdinWeb HTTP handler
# ─────────────────────────────────────────────────────────────

class OdinWebHandler(http.server.BaseHTTPRequestHandler):
    """
    OdinWeb HTTP handler — v4.

    GET  /                      → dashboard.html from LatticeFS
    GET  /status                → extended JSON status blob (includes security)
    GET  /<path>                → serve file from LatticeFS
    POST /api/compose           → compose temporal message  [token required]
    POST /api/send              → trigger send_outbox()     [token required]
    POST /api/security/raise    → raise DEFCON level        [token required]
    POST /api/security/lower    → lower DEFCON level        [token required]
    POST /api/security/attack   → declare attack            [token required]
    POST /api/security/cleared  → clear attack              [token required]
    POST /api/security/ban      → ban an identifier         [token required]
    GET  /api/security/status   → security status JSON      [token required]
    """

    daemon_ctx = None

    def log_message(self, fmt, *args):
        ctx = self.daemon_ctx
        if ctx:
            ctx.log(f"HTTP {self.command} {self.path}  [{args[1] if len(args)>1 else '?'}]")

    # ── GET routing ───────────────────────────────────────────────────────

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", ""):
            self._serve_root()
        elif path == "/status":
            self._serve_status_json()
        elif path == "/api/security/status":
            if _check_token(self):
                self._serve_security_status()
        else:
            self._serve_lattice(path.lstrip("/"))

    def _serve_root(self):
        ctx = self.daemon_ctx
        fs  = ctx.fs if ctx else None
        if fs and fs.exists("index.html"):
            try:
                data = fs.read_file("index.html")
                self._respond(200, "text/html; charset=utf-8", data)
                return
            except Exception:
                pass
        self._serve_status_html_fallback()

    def _serve_status_html_fallback(self):
        ctx    = self.daemon_ctx
        d      = ctx.status_dict()
        uptime = str(datetime.now() - ctx.start_time).split(".")[0]
        sec    = d.get("security") or {}
        defcon_line = (
            f"DEFCON {sec.get('defcon', '?')} [{sec.get('defcon_label', '?')}]"
            if sec else "Security: unavailable"
        )
        body = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="15">
  <title>OdinNet Node — {d['node_id']}</title>
  <style>
    body {{ font-family: monospace; background:#07090d; color:#00e87a;
            max-width:700px; margin:40px auto; padding:0 20px; }}
    a {{ color:#00ccff; }}
    pre {{ background:#0d1117; padding:12px; border-radius:4px; overflow-x:auto; }}
  </style>
</head>
<body>
  <h1>⬡ OdinNet Node</h1>
  <p>Node: {d['node_id']} | Uptime: {uptime} | Polls: {d['poll_count']}</p>
  <p>LatticeFS: {'✅ ' + str(len(d['lattice_files'])) + ' files' if d['lattice_fs'] else '⚠ not mounted'}</p>
  <p>{defcon_line}</p>
  <p><a href="/status">JSON status</a> | <a href="/readme.txt">readme.txt</a></p>
  <pre>{chr(10).join(d['activity_tail'][-20:])}</pre>
</body>
</html>""".encode("utf-8")
        self._respond(200, "text/html; charset=utf-8", body)

    def _serve_status_json(self):
        ctx  = self.daemon_ctx
        data = ctx.status_dict()
        self._respond(200, "application/json",
                      json.dumps(data, indent=2).encode("utf-8"))

    def _serve_security_status(self):
        ctx = self.daemon_ctx
        if not ctx.security:
            self._respond(503, "application/json",
                          b'{"ok":false,"error":"Security module not available"}')
            return
        self._respond(200, "application/json",
                      json.dumps(ctx.security.status_dict(), indent=2).encode())

    def _serve_lattice(self, filename: str):
        ctx = self.daemon_ctx
        fs  = ctx.fs if ctx else None
        if fs is None:
            self._respond(503, "text/plain; charset=utf-8",
                          b"LatticeFS is not available on this node.\n")
            return
        if not fs.exists(filename):
            self._respond(404, "text/plain; charset=utf-8",
                          f"404: '{filename}' not found in LatticeFS.\n".encode())
            return
        try:
            data  = fs.read_file(filename)
            ctype = _mime_for(filename)
            self._respond(200, ctype, data)
            _save_lattice(fs)
        except Exception as e:
            ctx.log(f"LatticeFS ERROR reading '{filename}': {e}")
            self._respond(500, "text/plain; charset=utf-8",
                          f"LatticeFS read error: {e}\n".encode())

    # ── POST routing ──────────────────────────────────────────────────────

    def do_POST(self):
        path = self.path.split("?")[0]
        if not _check_token(self):
            return
        if path == "/api/compose":
            self._handle_api_compose()
        elif path == "/api/send":
            self._handle_api_send()
        elif path == "/api/security/raise":
            self._handle_security_raise()
        elif path == "/api/security/lower":
            self._handle_security_lower()
        elif path == "/api/security/attack":
            self._handle_security_attack()
        elif path == "/api/security/cleared":
            self._handle_security_cleared()
        elif path == "/api/security/ban":
            self._handle_security_ban()
        else:
            self._respond(404, "application/json",
                          b'{"ok":false,"error":"Unknown API endpoint"}')

    def _read_json_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw    = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    # ── /api/compose ──────────────────────────────────────────────────────

    def _handle_api_compose(self):
        ctx  = self.daemon_ctx
        body = self._read_json_body()
        if not body:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"Invalid JSON body"}')
            return

        to_date  = body.get("to_date", "")
        subject  = body.get("subject", "")
        text     = body.get("body", "")
        msg_type = body.get("msg_type", "PRIVATE")

        if not to_date or not subject:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"to_date and subject are required"}')
            return

        try:
            fname = compose_message(to_date, subject, text, ctx.comms.coord_file)
            ctx.log(f"API /compose: '{subject}' to={to_date} type={msg_type}")
            try:
                with open(fname) as fh:
                    draft = json.load(fh)
                draft["msg_type"] = msg_type
                with open(fname, "w") as fh:
                    json.dump(draft, fh, indent=2)
            except Exception:
                pass
            result = json.dumps({"ok": True, "file": fname, "msg_type": msg_type})
            self._respond(200, "application/json", result.encode("utf-8"))
        except Exception as e:
            ctx.log(f"API /compose error: {e}")
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())

    # ── /api/send ─────────────────────────────────────────────────────────

    def _handle_api_send(self):
        ctx  = self.daemon_ctx
        body = self._read_json_body() or {}
        cf   = body.get("coord_file", ctx.comms.coord_file)

        try:
            outbox_before = len([
                f for f in os.listdir("outbox") if f.endswith(".json")
            ]) if os.path.exists("outbox") else 0
            send_outbox(cf)
            outbox_after = len([
                f for f in os.listdir("outbox") if f.endswith(".json")
            ]) if os.path.exists("outbox") else 0
            sent = max(0, outbox_before - outbox_after)
            ctx.log(f"API /send: {sent} message(s) sent")
            self._respond(200, "application/json",
                          json.dumps({"ok": True, "sent": sent}).encode())
        except Exception as e:
            ctx.log(f"API /send error: {e}")
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())

    # ── /api/security/* ───────────────────────────────────────────────────

    def _security_required(self) -> bool:
        """Returns True if security module is available; sends 503 if not."""
        if self.daemon_ctx.security:
            return True
        self._respond(503, "application/json",
                      b'{"ok":false,"error":"Security module not available"}')
        return False

    def _handle_security_raise(self):
        if not self._security_required():
            return
        ctx  = self.daemon_ctx
        body = self._read_json_body() or {}
        lvl  = body.get("level", ctx.security.defcon + 1)
        reason = body.get("reason", "api")
        try:
            ctx.security.raise_defcon(int(lvl), reason=reason)
            ctx.log(f"API /security/raise: DEFCON → {ctx.security.defcon} ({reason})")
            self._respond(200, "application/json",
                          json.dumps({"ok": True, "defcon": ctx.security.defcon}).encode())
        except Exception as e:
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())

    def _handle_security_lower(self):
        if not self._security_required():
            return
        ctx  = self.daemon_ctx
        body = self._read_json_body() or {}
        lvl  = body.get("level")
        reason = body.get("reason", "api")
        try:
            ctx.security.lower_defcon(int(lvl) if lvl is not None else None, reason=reason)
            ctx.log(f"API /security/lower: DEFCON → {ctx.security.defcon} ({reason})")
            self._respond(200, "application/json",
                          json.dumps({"ok": True, "defcon": ctx.security.defcon}).encode())
        except Exception as e:
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())

    def _handle_security_attack(self):
        if not self._security_required():
            return
        ctx    = self.daemon_ctx
        body   = self._read_json_body() or {}
        beacon = body.get("beacon")
        detail = body.get("detail", "api_reported")
        try:
            ctx.security.declare_attack(beacon, detail)
            ctx.log(f"API /security/attack: DEFCON → {ctx.security.defcon}")
            self._respond(200, "application/json",
                          json.dumps({"ok": True, "defcon": ctx.security.defcon}).encode())
        except Exception as e:
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())

    def _handle_security_cleared(self):
        if not self._security_required():
            return
        ctx    = self.daemon_ctx
        body   = self._read_json_body() or {}
        reason = body.get("reason", "api_cleared")
        try:
            ctx.security.attack_cleared(reason=reason)
            ctx.log(f"API /security/cleared: DEFCON → {ctx.security.defcon}")
            self._respond(200, "application/json",
                          json.dumps({"ok": True, "defcon": ctx.security.defcon}).encode())
        except Exception as e:
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())

    def _handle_security_ban(self):
        if not self._security_required():
            return
        ctx    = self.daemon_ctx
        body   = self._read_json_body() or {}
        ident  = body.get("identifier", "")
        reason = body.get("reason", "api")
        if not ident:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"identifier required"}')
            return
        try:
            ctx.security.ban(ident, reason=reason)
            ctx.log(f"API /security/ban: banned '{ident}'")
            self._respond(200, "application/json",
                          json.dumps({"ok": True, "banned": ident}).encode())
        except Exception as e:
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())

    # ── CORS ──────────────────────────────────────────────────────────────

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-OdinNet-Token")
        self.end_headers()

    # ── Response helper ───────────────────────────────────────────────────

    def _respond(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


# ─────────────────────────────────────────────────────────────
# OdinWeb server thread
# ─────────────────────────────────────────────────────────────

def _run_web_server(ctx: DaemonContext, host: str, port: int):
    def handler_factory(*args, **kwargs):
        h = OdinWebHandler(*args, **kwargs)
        h.daemon_ctx = ctx
        return h

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((host, port), handler_factory) as httpd:
        ctx.log(f"OdinWeb listening at http://{host}:{port}/")
        httpd.serve_forever()


# ─────────────────────────────────────────────────────────────
# Startup banner
# ─────────────────────────────────────────────────────────────

def _print_startup_banner(
    comms, fs, host: str, port: int, coord_file: str,
    security: "OdinNetSecurity | None" = None,
):
    now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    border = "★" * 62

    print(f"\n{border}")
    print(f"  ODINNET DAEMON  v4  —  {now}")
    print(f"{border}")
    print(f"  Node ID     : {comms.my_id}")
    print(f"  Coord file  : {coord_file}")

    try:
        with open(coord_file) as f:
            coord = json.load(f)
        poll_low  = coord.get("polling_low",  "not set")
        poll_high = coord.get("polling_high", "not set")
        msg_len   = coord.get("message_length", "not set")
        rt_low    = coord.get("rt_polling_low",  "not set")
        rt_high   = coord.get("rt_polling_high", "not set")
        poll_status = (
            f"✅  {str(poll_low)[:20]}...  →  {str(poll_high)[:20]}..."
            if poll_low != "not set"
            else "⚠  NO RANGE SET — run polling_range_finder()"
        )
        print(f"  Poll range  : {poll_status}")
        print(f"  Msg length  : {msg_len} bytes")
        print(f"  RT window   : {str(rt_low)[:18]}...  →  {str(rt_high)[:18]}...")
    except Exception as e:
        print(f"  Coord read  : ⚠  {e}")

    if fs:
        file_count = len(fs._index)
        url_count  = len(fs._url_index)
        dashboard_note = " [dashboard ✅]" if fs.exists("index.html") else " [no dashboard]"
        print(f"  LatticeFS   : ✅  {file_count} file(s)  {url_count} URL(s)"
              f"  →  {LATTICE_IMAGE_PATH}{dashboard_note}")
        if fs._index:
            names = ", ".join(sorted(fs._index.keys())[:6])
            print(f"  FS files    : {names}")
    else:
        print(f"  LatticeFS   : ⚠  not mounted")

    # PATCH v4: security status in banner
    if security:
        cfg = security.defcon_config
        print(f"  Security    : ✅  DEFCON {security.defcon} [{cfg['label']}] "
              f"{cfg['color']}")
        print(f"  API Token   : {'default (change ODINNET_TOKEN)' if API_TOKEN == 'odinnet-dev' else 'custom ✅'}")
    else:
        print(f"  Security    : ⚠  module not available")
        print(f"  API Token   : {'default' if API_TOKEN == 'odinnet-dev' else 'custom ✅'}")

    dash_path = os.path.exists(DASHBOARD_HTML)
    print(f"  Dashboard   : {'✅ found' if dash_path else '⚠ dashboard.html not on disk'}"
          f"  ({DASHBOARD_HTML})")
    print(f"  OdinWeb     : http://{host}:{port}/")
    print(f"  Poll cycle  : every {POLL_INTERVAL_SEC}s")
    print(f"{border}\n")


# ─────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="OdinNet Daemon v4 — Burris Numerical System node",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python odinnet_daemon.py
  python odinnet_daemon.py --port 9090
  python odinnet_daemon.py --coord my_node.json --port 8080
  python odinnet_daemon.py --seed-messages
  python odinnet_daemon.py --no-web
  ODINNET_TOKEN=mysecret python odinnet_daemon.py
        """,
    )
    parser.add_argument("--coord",   default=COORD_FILE)
    parser.add_argument("--port",    type=int, default=DEFAULT_PORT)
    parser.add_argument("--host",    default=DEFAULT_HOST)
    parser.add_argument("--lattice-passphrase", default=None)
    parser.add_argument("--seed-messages", action="store_true")
    parser.add_argument("--force",   action="store_true")
    parser.add_argument("--no-web",  action="store_true")
    args = parser.parse_args()

    if not _ensure_coordinate(args.coord):
        print("\n❌  Cannot start daemon without a coordinate file.")
        sys.exit(1)

    if args.seed_messages:
        n = seed_test_messages(coord_file=args.coord, force=args.force)
        print(f"\n  {n} message(s) seeded.")
        sys.exit(0)

    try:
        comms = GrokComms(coord_file=args.coord)
    except Exception as e:
        print(f"\n❌  GrokComms init failed: {e}")
        sys.exit(1)

    fs = _try_load_lattice_fs(passphrase=args.lattice_passphrase)
    if fs:
        _update_dashboard_in_fs(fs)
        _save_lattice(fs)

    _ensure_dirs()

    # PATCH v4: init security controller
    security = None
    if _SECURITY_AVAILABLE:
        try:
            security = OdinNetSecurity()
            print(f"  Security    : ✅  OdinNetSecurity loaded  "
                  f"DEFCON={security.defcon}")
        except Exception as e:
            print(f"  Security    : ⚠  OdinNetSecurity init failed: {e}")

    _print_startup_banner(comms, fs, args.host, args.port, args.coord, security)

    ctx = DaemonContext(comms=comms, fs=fs, security=security)

    poller = threading.Thread(
        target=_background_poller, args=(ctx,),
        daemon=True, name="OdinPoller"
    )
    poller.start()

    if args.no_web:
        ctx.log("Running in polling-only mode (--no-web). Press Ctrl-C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n\n👋 OdinNet Daemon shutting down.")
        return

    try:
        _run_web_server(ctx, args.host, args.port)
    except KeyboardInterrupt:
        print("\n\n👋 OdinNet Daemon shutting down.")
    except OSError as e:
        print(f"\n⚠  OdinWeb could not bind to {args.host}:{args.port}")
        print(f"   Error: {e}")
        print(f"\n   Fix options:")
        print(f"     1. Try a different port:  python odinnet_daemon.py --port 9090")
        print(f"     2. Check what's bound:    lsof -i :{args.port}")
        print(f"     3. Polling only:          python odinnet_daemon.py --no-web")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n\n👋 OdinNet Daemon shutting down.")


if __name__ == "__main__":
    main()
