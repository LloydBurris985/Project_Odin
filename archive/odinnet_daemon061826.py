#!/usr/bin/env python3
"""
OdinNet Daemon  v3
==================
Background service for OdinNet — Phase 1 GUI upgrade.

Changes from v2:
  - /status JSON extended with received_count, recent_received,
    recent_realtime, beacons, activity_tail, poll_range_set flag
  - DaemonContext now tracks received_log (temporal) and rt_log (realtime)
    and beacon_status — all exposed in /status
  - dashboard.html seeded into LatticeFS on fresh init
  - Message type field support (PUBLIC / PRIVATE) on received records
  - Phase 1 compose stub (/api/compose POST) wired up
  - Beacon status probed and cached in DaemonContext

Usage:
  python odinnet_daemon.py
  python odinnet_daemon.py --port 9090
  python odinnet_daemon.py --coord my_coord.json --port 8080
  python odinnet_daemon.py --seed-messages        # inject test msgs then exit
  python odinnet_daemon.py --no-web               # polling only, no HTTP server
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

# ─────────────────────────────────────────────────────────────
# Config defaults
# ─────────────────────────────────────────────────────────────

DEFAULT_HOST          = "127.0.0.1"
DEFAULT_PORT          = 8080
POLL_INTERVAL_SEC     = 15
POLL_ERROR_BACKOFF    = 30
LATTICE_IMAGE_PATH    = "odinnet_drive.json"

# Path to the dashboard HTML on disk (next to the daemon)
_DAEMON_DIR     = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_HTML  = os.path.join(_DAEMON_DIR, "dashboard.html")

# Max items kept in in-memory logs
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
    """Load or create a LatticeFS instance."""
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
    """Write seed files to a freshly created LatticeFS."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── dashboard.html ────────────────────────────────────────────────────
    # Load from disk if available (allows hot-updating the dashboard),
    # otherwise use a minimal fallback.
    if os.path.exists(DASHBOARD_HTML):
        with open(DASHBOARD_HTML, "rb") as fh:
            dashboard_bytes = fh.read()
        print(f"  LatticeFS  : loaded dashboard.html from {DASHBOARD_HTML}")
    else:
        # Minimal fallback (user should have dashboard.html in project dir)
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
        print("  LatticeFS  : dashboard.html not found on disk — seeding fallback")

    readme = f"""OdinNet Node
============
Burris Numerical System — OdinNet daemon running.

Node started : {now}
LatticeFS    : mounted (sector_size=1024, n_sectors=256)

This file is served directly from LatticeFS via OdinWeb.
""".encode("utf-8")

    fs.write_file("index.html",   dashboard_bytes)
    fs.write_file("readme.txt",   readme)
    print("  LatticeFS  : seeded index.html (dashboard) + readme.txt")


def _update_dashboard_in_fs(fs: "LatticeFS") -> bool:
    """
    If dashboard.html on disk is newer than what's in LatticeFS, refresh it.
    Call this on daemon startup after LatticeFS loads.
    Returns True if an update was performed.
    """
    if not os.path.exists(DASHBOARD_HTML):
        return False
    try:
        with open(DASHBOARD_HTML, "rb") as fh:
            fresh = fh.read()
        # Compare with what's stored
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
# Test message seeding (temporal)
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
# Daemon context
# ─────────────────────────────────────────────────────────────

class DaemonContext:
    """
    Shared state for poller thread + HTTP handler.

    Tracks:
      received_log   — list of temporal message dicts (newest first, capped)
      rt_log         — list of realtime message dicts (newest first, capped)
      beacon_status  — list of beacon dicts from _load_beacons()
    """

    def __init__(self, comms: "GrokComms", fs: "LatticeFS | None"):
        self.comms         = comms
        self.fs            = fs
        self.start_time    = datetime.now()
        self.last_poll     = None
        self.poll_count    = 0
        self.activity_log  = []   # raw strings, oldest→newest, capped at MAX_ACTIVITY_LOG
        self.received_log  = []   # temporal received records, newest first
        self.rt_log        = []   # realtime received records, newest first
        self.beacon_status = []   # refreshed each poll cycle

    def log(self, msg: str):
        ts   = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        self.activity_log.append(line)
        if len(self.activity_log) > MAX_ACTIVITY_LOG:
            self.activity_log = self.activity_log[-MAX_ACTIVITY_LOG:]

    def add_temporal_received(self, records: list):
        """Prepend temporal records; stamp msg_type if missing."""
        for r in records:
            r.setdefault("msg_type", "PRIVATE")
            r.setdefault("type",     "temporal")
        self.received_log = records + self.received_log
        if len(self.received_log) > MAX_RECEIVED_LOG:
            self.received_log = self.received_log[:MAX_RECEIVED_LOG]

    def add_rt_received(self, records: list):
        """Prepend realtime records."""
        for r in records:
            r.setdefault("msg_type", "PRIVATE")
            r.setdefault("type",     "realtime")
        self.rt_log = records + self.rt_log
        if len(self.rt_log) > MAX_RT_LOG:
            self.rt_log = self.rt_log[:MAX_RT_LOG]

    def refresh_beacons(self):
        """Reload beacon registry from disk."""
        try:
            self.beacon_status = _load_beacons()
        except Exception:
            self.beacon_status = []

    def status_dict(self) -> dict:
        """
        Build the full /status JSON payload.
        Includes all fields expected by dashboard.html.
        """
        uptime_sec = int((datetime.now() - self.start_time).total_seconds())
        fs         = self.fs

        # Polling range health
        poll_range_set = False
        try:
            with open(self.comms.coord_file) as fh:
                coord = json.load(fh)
            poll_range_set = bool(
                coord.get("polling_low") and coord.get("polling_high")
            )
        except Exception:
            coord = {}

        # LatticeFS file list — richer format for dashboard
        lfs_files = []
        if fs:
            for name, entry in sorted(fs._index.items()):
                lfs_files.append({
                    "name":        name,
                    "byte_length": entry.get("byte_length", 0),
                })

        return {
            # ── core ──────────────────────────────────────────────────────
            "node_id":          self.comms.my_id,
            "coord_file":       self.comms.coord_file,
            "uptime_sec":       uptime_sec,
            "poll_count":       self.poll_count,
            "last_poll":        self.last_poll,
            "poll_range_set":   poll_range_set,

            # ── LatticeFS ─────────────────────────────────────────────────
            "lattice_fs":       fs is not None,
            "lattice_files":    lfs_files,

            # ── messages ─────────────────────────────────────────────────
            "received_count":   len(self.received_log),
            "recent_received":  self.received_log[:10],

            "rt_count":         len(self.rt_log),
            "recent_realtime":  self.rt_log[:10],

            # ── beacons ──────────────────────────────────────────────────
            "beacons": [
                {
                    "name":       b.get("name", "?"),
                    "coordinate": b.get("coordinate", ""),
                    "notes":      b.get("notes", ""),
                }
                for b in self.beacon_status
            ],

            # ── activity tail (last 80 lines, for dashboard log panel) ───
            "activity_tail": self.activity_log[-80:],
        }


# ─────────────────────────────────────────────────────────────
# Background poller thread
# ─────────────────────────────────────────────────────────────

def _background_poller(ctx: DaemonContext):
    ctx.log("Background poller started.")
    ctx.refresh_beacons()

    while True:
        try:
            # Temporal + beacon poll
            temporal_results = ctx.comms.poll()
            ctx.log(f"Temporal poll: {len(temporal_results)} message(s)")
            if temporal_results:
                ctx.add_temporal_received(temporal_results)

            # Realtime poll
            rt_results = ctx.comms.poll_realtime()
            ctx.log(f"Realtime poll: {len(rt_results)} message(s)")
            if rt_results:
                ctx.add_rt_received(rt_results)

            # Refresh beacon list
            ctx.refresh_beacons()

            ctx.last_poll   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ctx.poll_count += 1

            time.sleep(POLL_INTERVAL_SEC)

        except KeyboardInterrupt:
            break
        except Exception as e:
            ctx.log(f"Polling error: {e}")
            time.sleep(POLL_ERROR_BACKOFF)


# ─────────────────────────────────────────────────────────────
# OdinWeb HTTP handler
# ─────────────────────────────────────────────────────────────

class OdinWebHandler(http.server.BaseHTTPRequestHandler):
    """
    OdinWeb HTTP handler — Phase 1 + compose stub.

    GET  /            → dashboard.html from LatticeFS (or status page fallback)
    GET  /status      → extended JSON status blob
    GET  /<path>      → serve file from LatticeFS
    POST /api/compose → compose a temporal message (Phase 2 stub)
    POST /api/send    → trigger send_outbox() (Phase 2 stub)
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
        else:
            self._serve_lattice(path.lstrip("/"))

    def _serve_root(self):
        ctx = self.daemon_ctx
        fs  = ctx.fs if ctx else None

        # Prefer LatticeFS index.html (our dashboard)
        if fs and fs.exists("index.html"):
            try:
                data = fs.read_file("index.html")
                self._respond(200, "text/html; charset=utf-8", data)
                return
            except Exception:
                pass

        # Fallback: generate a minimal status page
        self._serve_status_html_fallback()

    def _serve_status_html_fallback(self):
        """Minimal fallback for when LatticeFS has no index.html."""
        ctx    = self.daemon_ctx
        d      = ctx.status_dict()
        uptime = str(datetime.now() - ctx.start_time).split(".")[0]
        body   = f"""<!DOCTYPE html>
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
        if path == "/api/compose":
            self._handle_api_compose()
        elif path == "/api/send":
            self._handle_api_send()
        else:
            self._respond(404, "application/json",
                          b'{"ok":false,"error":"Unknown API endpoint"}')

    def _read_json_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw    = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            return None

    def _handle_api_compose(self):
        """
        POST /api/compose
        Body: { "to_date": "YYYY-MM-DD", "subject": "...", "body": "..." }
        Response: { "ok": true, "file": "outbox/draft_..." }
        """
        ctx  = self.daemon_ctx
        body = self._read_json_body()
        if not body:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"Invalid JSON body"}')
            return

        to_date = body.get("to_date", "")
        subject = body.get("subject", "")
        text    = body.get("body", "")
        msg_type = body.get("msg_type", "PRIVATE")   # PUBLIC or PRIVATE

        if not to_date or not subject:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"to_date and subject are required"}')
            return

        try:
            fname = compose_message(to_date, subject, text, ctx.comms.coord_file)
            ctx.log(f"API /compose: '{subject}' to={to_date} type={msg_type}")

            # Annotate draft with msg_type
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
            result = json.dumps({"ok": False, "error": str(e)})
            self._respond(500, "application/json", result.encode("utf-8"))

    def _handle_api_send(self):
        """
        POST /api/send
        Body: {} (empty OK) or { "coord_file": "..." }
        Response: { "ok": true, "sent": N }
        """
        ctx  = self.daemon_ctx
        body = self._read_json_body() or {}
        cf   = body.get("coord_file", ctx.comms.coord_file)

        try:
            # Count outbox files before
            outbox_before = len([
                f for f in os.listdir("outbox") if f.endswith(".json")
            ]) if os.path.exists("outbox") else 0

            send_outbox(cf)

            outbox_after = len([
                f for f in os.listdir("outbox") if f.endswith(".json")
            ]) if os.path.exists("outbox") else 0

            sent = max(0, outbox_before - outbox_after)
            ctx.log(f"API /send: {sent} message(s) sent")
            result = json.dumps({"ok": True, "sent": sent})
            self._respond(200, "application/json", result.encode("utf-8"))

        except Exception as e:
            ctx.log(f"API /send error: {e}")
            result = json.dumps({"ok": False, "error": str(e)})
            self._respond(500, "application/json", result.encode("utf-8"))

    # ── CORS (allow dashboard JS from same origin) ────────────────────────

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
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
# Startup diagnostics banner
# ─────────────────────────────────────────────────────────────

def _print_startup_banner(comms, fs, host: str, port: int, coord_file: str):
    now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    border = "★" * 62

    print(f"\n{border}")
    print(f"  ODINNET DAEMON  v3  —  {now}")
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
        description="OdinNet Daemon v3 — Burris Numerical System node",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python odinnet_daemon.py
  python odinnet_daemon.py --port 9090
  python odinnet_daemon.py --coord my_node.json --port 8080
  python odinnet_daemon.py --seed-messages
  python odinnet_daemon.py --no-web
        """,
    )
    parser.add_argument(
        "--coord", default=COORD_FILE,
        help=f"Coordinate file (default: {COORD_FILE})"
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"OdinWeb HTTP port (default: {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST,
        help=f"OdinWeb bind address (default: {DEFAULT_HOST})"
    )
    parser.add_argument(
        "--lattice-passphrase", default=None,
        help="Passphrase for LatticeFS decryption (optional)"
    )
    parser.add_argument(
        "--seed-messages", action="store_true",
        help="Seed test temporal messages into outbox/ and exit"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Force re-seed even if outbox/ already has messages"
    )
    parser.add_argument(
        "--no-web", action="store_true",
        help="Run polling daemon without starting the HTTP server"
    )
    args = parser.parse_args()

    # ── Ensure coordinate ────────────────────────────────────────────────
    if not _ensure_coordinate(args.coord):
        print("\n❌  Cannot start daemon without a coordinate file.")
        sys.exit(1)

    # ── Seed-messages mode ───────────────────────────────────────────────
    if args.seed_messages:
        n = seed_test_messages(coord_file=args.coord, force=args.force)
        print(f"\n  {n} message(s) seeded.  Run the daemon normally to send + poll.")
        sys.exit(0)

    # ── GrokComms init ───────────────────────────────────────────────────
    try:
        comms = GrokComms(coord_file=args.coord)
    except Exception as e:
        print(f"\n❌  GrokComms init failed: {e}")
        sys.exit(1)

    # ── LatticeFS init ───────────────────────────────────────────────────
    fs = _try_load_lattice_fs(passphrase=args.lattice_passphrase)
    if fs:
        _update_dashboard_in_fs(fs)
        _save_lattice(fs)

    # ── Ensure runtime dirs ──────────────────────────────────────────────
    _ensure_dirs()

    # ── Startup banner ───────────────────────────────────────────────────
    _print_startup_banner(comms, fs, args.host, args.port, args.coord)

    # ── Daemon context ───────────────────────────────────────────────────
    ctx = DaemonContext(comms=comms, fs=fs)

    # ── Background poller ────────────────────────────────────────────────
    poller = threading.Thread(
        target=_background_poller, args=(ctx,),
        daemon=True, name="OdinPoller"
    )
    poller.start()

    # ── OdinWeb server or polling-only ───────────────────────────────────
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
