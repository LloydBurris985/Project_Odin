#!/usr/bin/env python3
"""
OdinNet Daemon  v2
==================
Background service for OdinNet.

Features:
  - Continuous GrokComms polling (temporal + realtime + beacons)
  - Local HTTP server (OdinWeb) — serves real LatticeFS content, with MIME
    detection and a styled directory listing at / if no index.html exists.
  - Auto-coordinate generation on first run (no manual setup needed)
  - Test message seeding — injects sample temporal messages into outbox
    so polling has something to find on a fresh node
  - Improved startup diagnostics: clear error messages, guided recovery steps
  - Easy to run in Termux: python odinnet_daemon.py [--port 8080]

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
    """Import a module, printing a helpful message if it fails."""
    import importlib
    try:
        return importlib.import_module(module_name)
    except ImportError as e:
        print(f"\n❌  Cannot import '{module_name}': {e}")
        print(f"    {friendly}")
        sys.exit(1)

_gc_mod  = _import_or_die("grok_comms",      "Ensure grok_comms.py is in the same directory.")
_cg_mod  = _import_or_die("chart_generator", "Ensure chart_generator.py is in the same directory.")

GrokComms           = _gc_mod.GrokComms
COORD_FILE          = _gc_mod.COORD_FILE
coordinate_generator = _gc_mod.coordinate_generator
compose_message     = _gc_mod.compose_message
polling_range_finder = _gc_mod.polling_range_finder
_today_str          = _gc_mod._today_str
_ensure_dirs        = _gc_mod._ensure_dirs

lattice_fs_ctor     = _cg_mod.lattice_fs
LatticeDrive        = _cg_mod.LatticeDrive
LatticeFS           = _cg_mod.LatticeFS

# ─────────────────────────────────────────────────────────────
# Config defaults
# ─────────────────────────────────────────────────────────────

DEFAULT_HOST           = "127.0.0.1"
DEFAULT_PORT           = 8080
POLL_INTERVAL_SEC      = 15
POLL_ERROR_BACKOFF     = 30
LATTICE_IMAGE_PATH     = "odinnet_drive.json"
LATTICE_DEFAULT_PASS   = None   # set via --lattice-passphrase

# ─────────────────────────────────────────────────────────────
# MIME helpers
# ─────────────────────────────────────────────────────────────

_EXTRA_MIME = {
    ".json": "application/json",
    ".md":   "text/markdown; charset=utf-8",
    ".txt":  "text/plain; charset=utf-8",
    ".bin":  "application/octet-stream",
    ".py":   "text/plain; charset=utf-8",
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
    """
    Load or create a LatticeFS instance.

    Priority order:
      1. Load existing drive image from LATTICE_IMAGE_PATH
      2. Create a fresh LatticeFS and seed it with a welcome file + status page
    """
    try:
        if os.path.exists(LATTICE_IMAGE_PATH):
            drive = LatticeDrive()
            drive.load(LATTICE_IMAGE_PATH)
            fs = LatticeFS(drive, passphrase=passphrase)
            print(f"  LatticeFS  : loaded from {LATTICE_IMAGE_PATH}")
            return fs

        # Fresh creation
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
        print(f"     OdinWeb will serve status-only until LatticeFS is available.")
        return None


def _seed_lattice_fs(fs: "LatticeFS"):
    """Write seed files to a freshly created LatticeFS."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    welcome_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>OdinNet Node</title>
  <style>
    body {{ font-family: monospace; background: #0d0d0d; color: #00ff88;
            max-width: 720px; margin: 60px auto; padding: 0 24px; }}
    h1   {{ border-bottom: 1px solid #00ff88; padding-bottom: 8px; }}
    a    {{ color: #00ccff; }}
    pre  {{ background: #111; padding: 12px; border-radius: 4px; }}
  </style>
</head>
<body>
  <h1>⬡ OdinNet Node — Online</h1>
  <p>Burris Numerical System — LatticeFS is mounted and serving.</p>
  <p>Seeded: {now}</p>
  <ul>
    <li><a href="/readme.txt">readme.txt</a></li>
    <li><a href="/status">JSON status</a></li>
  </ul>
  <pre>
  ✦  BURRIS NAVIGATIONAL SYSTEM
     OdinNet communications layer active.
     Temporal + realtime polling running.
  </pre>
</body>
</html>
""".encode("utf-8")

    readme = f"""OdinNet Node
============
Burris Numerical System — OdinNet daemon running.

Node started : {now}
LatticeFS    : mounted (sector_size=1024, n_sectors=256)

This file is served directly from LatticeFS via OdinWeb.
""".encode("utf-8")

    fs.write_file("index.html", welcome_html)
    fs.write_file("readme.txt",  readme)
    print("  LatticeFS  : seeded index.html + readme.txt")


def _save_lattice(fs: "LatticeFS"):
    """Persist LatticeFS drive image to disk."""
    try:
        fs._drive.save(LATTICE_IMAGE_PATH)
    except Exception as e:
        print(f"  ⚠  LatticeFS save failed: {e}")


# ─────────────────────────────────────────────────────────────
# Auto coordinate generation
# ─────────────────────────────────────────────────────────────

def _ensure_coordinate(coord_file: str) -> bool:
    """
    Ensure a coordinate file exists.  If not, auto-generate one using a
    hostname + timestamp passphrase, then compute a polling range so polling
    can start immediately.

    Returns True if the node is ready, False if something failed.
    """
    if os.path.exists(coord_file):
        return True

    import socket
    print(f"\n[Init] Coordinate file '{coord_file}' not found.")
    print(f"[Init] Auto-generating coordinate for this node...")

    try:
        hostname  = socket.gethostname()
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        passphrase = f"odinnet-{hostname}-{timestamp}"

        coordinate_generator(passphrase, num_digits=150, output_file=coord_file)

        # Set a default message_length before running range finder
        coord = json.load(open(coord_file))
        coord["message_length"] = 64
        json.dump(coord, open(coord_file, "w"), indent=2)

        print(f"[Init] Computing polling range (30 samples)...")
        polling_range_finder(num_samples=30, coord_file=coord_file)

        print(f"[Init] ✅ Node coordinate ready → {coord_file}")
        print(f"[Init]    Passphrase used: {passphrase}")
        print(f"[Init]    ⚠  Save this passphrase — you'll need it to recover your identity.")
        return True

    except Exception as e:
        print(f"[Init] ❌ Auto-generation failed: {e}")
        print(f"       Manual fix: python grok_comms.py  → admin_menu → option 1")
        return False


# ─────────────────────────────────────────────────────────────
# Test message seeding (temporal)
# ─────────────────────────────────────────────────────────────

_SEED_MESSAGES = [
    {
        "subject": "OdinNet Node Online",
        "body": (
            "This node has joined the Burris coordinate network.\n"
            "OdinNet daemon is running and polling is active.\n"
            "Temporal + realtime channels are open."
        ),
    },
    {
        "subject": "Burris System Status",
        "body": (
            "ChartGenerator encode/decode paths: NOMINAL\n"
            "LatticeFS mount status: see /status endpoint\n"
            "Polling window: calibrated from coordinate\n"
            "Beacon registry: check beacons.json"
        ),
    },
    {
        "subject": "Welcome to the Informational Universe",
        "body": (
            "You are navigating coordinate space.\n"
            "Every byte is a position. Every position is a message.\n"
            "The Burris Numerical System encodes meaning into arithmetic.\n"
            "Safe travels through the galactic coordinate field."
        ),
    },
]

def seed_test_messages(coord_file: str = COORD_FILE, force: bool = False) -> int:
    """
    Write seed temporal messages into outbox/ so the first polling run
    has something to work with.

    Skips seeding if outbox/ already has files, unless force=True.
    Returns number of messages written.
    """
    _ensure_dirs()
    outbox_dir = "outbox"

    existing = [f for f in os.listdir(outbox_dir) if f.endswith(".json")] \
               if os.path.exists(outbox_dir) else []

    if existing and not force:
        print(f"[Seed] Outbox already has {len(existing)} message(s) — skipping seed.")
        print(f"       Use --seed-messages --force to re-seed.")
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
            print(f"[Seed] ⚠  Failed to compose '{msg['subject']}': {e}")

    print(f"[Seed] ✅ {count} test message(s) seeded into outbox/")
    return count


# ─────────────────────────────────────────────────────────────
# OdinWeb HTTP handler
# ─────────────────────────────────────────────────────────────

_STATUS_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="15">
  <title>OdinNet Node — {node_id}</title>
  <style>
    body  {{ font-family: monospace; background:#0d0d0d; color:#00ff88;
             max-width:740px; margin:40px auto; padding:0 20px; }}
    h1,h2 {{ border-bottom:1px solid #00ff88; padding-bottom:6px; }}
    .label {{ color:#888; width:140px; display:inline-block; }}
    .ok   {{ color:#00ff88; }}
    .warn {{ color:#ff8800; }}
    .err  {{ color:#ff4444; }}
    pre   {{ background:#111; padding:12px; border-radius:4px;
             overflow-x:auto; font-size:0.85em; }}
    table {{ border-collapse:collapse; width:100%; }}
    td,th {{ padding:4px 10px; border:1px solid #1a1a1a; text-align:left; }}
    th    {{ color:#888; font-weight:normal; }}
    a     {{ color:#00ccff; }}
  </style>
</head>
<body>
  <h1>⬡ OdinNet Node</h1>

  <table>
    <tr><th>Node ID</th>   <td>{node_id}</td></tr>
    <tr><th>Coord File</th><td>{coord_file}</td></tr>
    <tr><th>Uptime</th>    <td>{uptime}</td></tr>
    <tr><th>Last Poll</th> <td>{last_poll}</td></tr>
    <tr><th>Poll Count</th><td>{poll_count}</td></tr>
    <tr><th>LatticeFS</th> <td class="{lfs_class}">{lfs_status}</td></tr>
    <tr><th>LatticeFS files</th><td>{lfs_files}</td></tr>
  </table>

  <h2>Recent Activity</h2>
  <pre>{activity}</pre>

  {lfs_dir_section}

  <p style="color:#444;font-size:0.8em">
    Auto-refreshes every 15 s &nbsp;|&nbsp;
    <a href="/status">JSON status</a>
  </p>
</body>
</html>
"""

_LFS_DIR_SECTION = """\
  <h2>LatticeFS Files</h2>
  <table>
    <tr><th>Name</th><th>Size</th></tr>
    {rows}
  </table>
"""


class OdinWebHandler(http.server.BaseHTTPRequestHandler):
    """
    OdinWeb HTTP handler.

    GET /           → styled status page (with LatticeFS file listing if available)
    GET /status     → JSON status blob
    GET /<path>     → serve file from LatticeFS (real file, not stub)
    """

    # Injected by server factory
    daemon_ctx = None

    def log_message(self, fmt, *args):
        ctx = self.daemon_ctx
        if ctx:
            ctx.log(f"HTTP {self.command} {self.path}  [{args[1] if len(args)>1 else '?'}]")

    def do_GET(self):
        path = self.path.split("?")[0]
        # Normalise: / and /index.html → status dashboard (unless LatticeFS has index.html)
        if path in ("/", ""):
            self._serve_root()
        elif path == "/status":
            self._serve_status_json()
        else:
            self._serve_lattice(path.lstrip("/"))

    def _serve_root(self):
        ctx = self.daemon_ctx
        fs  = ctx.fs if ctx else None

        # If LatticeFS has an index.html, serve it directly
        if fs and fs.exists("index.html"):
            try:
                data = fs.read_file("index.html")
                self._respond(200, "text/html; charset=utf-8", data)
                return
            except Exception:
                pass   # fall through to status page

        self._serve_status_html()

    def _serve_status_html(self):
        ctx    = self.daemon_ctx
        uptime = str(datetime.now() - ctx.start_time).split(".")[0]
        fs     = ctx.fs

        lfs_class  = "ok"    if fs else "warn"
        lfs_status = "connected ✅" if fs else "not available ⚠"
        lfs_files  = str(len(fs._index)) + " file(s)" if fs else "—"

        lfs_dir_html = ""
        if fs and fs._index:
            rows = "\n    ".join(
                f"<tr><td><a href='/{name}'>{name}</a></td>"
                f"<td>{entry['byte_length']:,} bytes</td></tr>"
                for name, entry in sorted(fs._index.items())
            )
            lfs_dir_html = _LFS_DIR_SECTION.format(rows=rows)

        content = _STATUS_HTML.format(
            node_id          = ctx.comms.my_id,
            coord_file       = ctx.comms.coord_file,
            uptime           = uptime,
            last_poll        = ctx.last_poll or "never",
            poll_count       = ctx.poll_count,
            lfs_class        = lfs_class,
            lfs_status       = lfs_status,
            lfs_files        = lfs_files,
            activity         = "\n".join(ctx.activity_log[-30:]) or "(none yet)",
            lfs_dir_section  = lfs_dir_html,
        )
        self._respond(200, "text/html; charset=utf-8", content.encode("utf-8"))

    def _serve_status_json(self):
        ctx  = self.daemon_ctx
        fs   = ctx.fs
        data = {
            "node_id":      ctx.comms.my_id,
            "coord_file":   ctx.comms.coord_file,
            "poll_count":   ctx.poll_count,
            "last_poll":    ctx.last_poll,
            "lattice_fs":   fs is not None,
            "lattice_files": list(fs._index.keys()) if fs else [],
            "uptime_sec":   int((datetime.now() - ctx.start_time).total_seconds()),
        }
        self._respond(200, "application/json", json.dumps(data, indent=2).encode())

    def _serve_lattice(self, filename: str):
        """Serve a file from LatticeFS."""
        ctx = self.daemon_ctx
        fs  = ctx.fs if ctx else None

        if fs is None:
            body = (
                b"LatticeFS is not available on this node.\n"
                b"Check daemon logs for details."
            )
            self._respond(503, "text/plain; charset=utf-8", body)
            return

        if not fs.exists(filename):
            body = f"404: '{filename}' not found in LatticeFS.\n".encode()
            self._respond(404, "text/plain; charset=utf-8", body)
            return

        try:
            data  = fs.read_file(filename)
            ctype = _mime_for(filename)
            self._respond(200, ctype, data)
            # Persist the drive after reads (updates read_count)
            _save_lattice(fs)
        except Exception as e:
            body = f"LatticeFS read error: {e}\n".encode()
            ctx.log(f"LatticeFS ERROR reading '{filename}': {e}")
            self._respond(500, "text/plain; charset=utf-8", body)

    def _respond(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


# ─────────────────────────────────────────────────────────────
# Daemon context
# ─────────────────────────────────────────────────────────────

class DaemonContext:
    def __init__(self, comms: "GrokComms", fs: "LatticeFS | None"):
        self.comms        = comms
        self.fs           = fs
        self.start_time   = datetime.now()
        self.last_poll    = None
        self.poll_count   = 0
        self.activity_log = []   # capped at 500 lines

    def log(self, msg: str):
        ts   = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        self.activity_log.append(line)
        if len(self.activity_log) > 500:
            self.activity_log = self.activity_log[-500:]


# ─────────────────────────────────────────────────────────────
# Background poller thread
# ─────────────────────────────────────────────────────────────

def _background_poller(ctx: DaemonContext):
    ctx.log("Background poller started.")
    while True:
        try:
            temporal_results = ctx.comms.poll()
            ctx.log(f"Temporal poll: {len(temporal_results)} message(s)")

            rt_results = ctx.comms.poll_realtime()
            ctx.log(f"Realtime poll: {len(rt_results)} message(s)")

            ctx.last_poll   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ctx.poll_count += 1

            time.sleep(POLL_INTERVAL_SEC)

        except KeyboardInterrupt:
            break
        except Exception as e:
            ctx.log(f"Polling error: {e}")
            time.sleep(POLL_ERROR_BACKOFF)


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

def _print_startup_banner(comms: "GrokComms", fs, host: str, port: int, coord_file: str):
    """Print a clear, informative startup summary."""
    now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    border = "★" * 62

    print(f"\n{border}")
    print(f"  ODINNET DAEMON  v2  —  {now}")
    print(f"{border}")
    print(f"  Node ID     : {comms.my_id}")
    print(f"  Coord file  : {coord_file}")

    # Coordinate health check
    try:
        with open(coord_file) as f:
            coord = json.load(f)
        poll_low  = coord.get("polling_low",  "not set")
        poll_high = coord.get("polling_high", "not set")
        msg_len   = coord.get("message_length", "not set")
        rt_low    = coord.get("rt_polling_low",  "not set")
        rt_high   = coord.get("rt_polling_high", "not set")

        if poll_low == "not set":
            poll_status = "⚠  NO RANGE SET — run polling_range_finder()"
        else:
            poll_status = f"✅  {str(poll_low)[:20]}...  →  {str(poll_high)[:20]}..."

        print(f"  Poll range  : {poll_status}")
        print(f"  Msg length  : {msg_len} bytes")
        print(f"  RT window   : {str(rt_low)[:18]}...  →  {str(rt_high)[:18]}...")
    except Exception as e:
        print(f"  Coord read  : ⚠  {e}")

    # LatticeFS
    if fs:
        file_count = len(fs._index)
        url_count  = len(fs._url_index)
        print(f"  LatticeFS   : ✅  {file_count} file(s)  {url_count} URL(s)  "
              f"→ {LATTICE_IMAGE_PATH}")
        if fs._index:
            names = ", ".join(sorted(fs._index.keys())[:6])
            print(f"  FS files    : {names}")
    else:
        print(f"  LatticeFS   : ⚠  not mounted  (status page only)")

    print(f"  OdinWeb     : http://{host}:{port}/")
    print(f"  Poll cycle  : every {POLL_INTERVAL_SEC}s")
    print(f"{border}\n")

    if fs is None:
        print("  ──────────────────────────────────────────────────────")
        print("  To enable LatticeFS, the daemon will auto-create a drive")
        print(f"  image at '{LATTICE_IMAGE_PATH}' on next restart.")
        print("  ──────────────────────────────────────────────────────\n")


# ─────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="OdinNet Daemon — Burris Numerical System node",
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
        help="Force re-seed even if outbox already has messages (use with --seed-messages)"
    )
    parser.add_argument(
        "--no-web", action="store_true",
        help="Run polling daemon without starting the HTTP server"
    )
    args = parser.parse_args()

    # ── Ensure coordinate exists (auto-generate if needed) ───────────────
    ok = _ensure_coordinate(args.coord)
    if not ok:
        print("\n❌  Cannot start daemon without a coordinate file.")
        print("   Fix the error above and retry.")
        sys.exit(1)

    # ── Seed messages mode ───────────────────────────────────────────────
    if args.seed_messages:
        n = seed_test_messages(coord_file=args.coord, force=args.force)
        print(f"\n  {n} message(s) seeded.  Run the daemon normally to send + poll.")
        sys.exit(0)

    # ── GrokComms init ───────────────────────────────────────────────────
    try:
        comms = GrokComms(coord_file=args.coord)
    except Exception as e:
        print(f"\n❌  GrokComms init failed: {e}")
        print("   Check that grok_comms.py and chart_generator.py are present.")
        sys.exit(1)

    # ── LatticeFS init ───────────────────────────────────────────────────
    fs = _try_load_lattice_fs(passphrase=args.lattice_passphrase)

    # ── Ensure runtime dirs exist ─────────────────────────────────────────
    _ensure_dirs()

    # ── Startup banner ───────────────────────────────────────────────────
    _print_startup_banner(comms, fs, args.host, args.port, args.coord)

    # ── Build daemon context ─────────────────────────────────────────────
    ctx = DaemonContext(comms=comms, fs=fs)

    # ── Background poller thread ─────────────────────────────────────────
    poller = threading.Thread(
        target=_background_poller, args=(ctx,),
        daemon=True, name="OdinPoller"
    )
    poller.start()

    # ── OdinWeb server (or polling-only mode) ─────────────────────────────
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
        print(f"     2. Check what's using port {args.port}:  lsof -i :{args.port}")
        print(f"     3. Run polling only:  python odinnet_daemon.py --no-web")
        print(f"\n   Keeping polling thread alive. Press Ctrl-C to exit.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n\n👋 OdinNet Daemon shutting down.")


if __name__ == "__main__":
    main()
