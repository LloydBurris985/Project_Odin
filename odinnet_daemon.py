#!/usr/bin/env python3
"""
OdinNet Daemon
==============
Background service for OdinNet.

Features:
  - Continuous GrokComms polling (temporal + realtime + beacons)
  - Local HTTP server (OdinWeb) — serves LatticeFS content if available,
    falls back to a plain status page if lattice_fs is not yet built.
  - Easy to run in Termux: python odinnet_daemon.py [--port 8080]

Usage:
  python odinnet_daemon.py
  python odinnet_daemon.py --port 9090
  python odinnet_daemon.py --coord my_coord.json --port 8080
"""

import argparse
import http.server
import json
import os
import socketserver
import threading
import time
from datetime import datetime

from grok_comms import GrokComms, COORD_FILE

# ─────────────────────────────────────────────────────────────
# Config defaults (overridable via CLI args)
# ─────────────────────────────────────────────────────────────
DEFAULT_HOST       = "127.0.0.1"
DEFAULT_PORT       = 8080
POLL_INTERVAL_SEC  = 15   # seconds between background polls
POLL_ERROR_BACKOFF = 30   # seconds to wait after a polling error

# ─────────────────────────────────────────────────────────────
# Optional LatticeFS integration
# ─────────────────────────────────────────────────────────────
try:
    from chart_generator import lattice_fs as _lattice_fs_ctor
    _LATTICE_AVAILABLE = True
except ImportError:
    _LATTICE_AVAILABLE = False


def _try_load_lattice_fs(passphrase=None):
    """
    Attempt to instantiate LatticeFS.  Returns the fs object on success,
    or None if chart_generator doesn't export lattice_fs yet.
    """
    if not _LATTICE_AVAILABLE:
        return None
    try:
        return _lattice_fs_ctor(passphrase=passphrase)
    except Exception as e:
        print(f"  ⚠  LatticeFS init failed: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# OdinWeb HTTP handler
# ─────────────────────────────────────────────────────────────

_STATUS_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="15">
  <title>OdinNet Node — {node_id}</title>
  <style>
    body {{ font-family: monospace; background:#0d0d0d; color:#00ff88;
            max-width:700px; margin:40px auto; padding:0 20px; }}
    h1   {{ border-bottom:1px solid #00ff88; padding-bottom:8px; }}
    .label {{ color:#888; }}
    .ok  {{ color:#00ff88; }}
    .warn {{ color:#ff8800; }}
    pre  {{ background:#111; padding:12px; border-radius:4px; overflow-x:auto; }}
  </style>
</head>
<body>
  <h1>⬡ OdinNet Node</h1>
  <p><span class="label">Node ID  :</span> {node_id}</p>
  <p><span class="label">Coord File:</span> {coord_file}</p>
  <p><span class="label">Uptime   :</span> {uptime}</p>
  <p><span class="label">Last Poll :</span> {last_poll}</p>
  <p><span class="label">Poll Count:</span> {poll_count}</p>
  <p><span class="label">LatticeFS :</span>
     <span class="{lfs_class}">{lfs_status}</span></p>
  <hr>
  <h2>Recent Activity</h2>
  <pre>{activity}</pre>
  <p style="color:#444; font-size:0.8em">Auto-refreshes every 15 s</p>
</body>
</html>
"""

class OdinWebHandler(http.server.BaseHTTPRequestHandler):
    """
    HTTP handler for OdinWeb.

    GET /           → status page
    GET /status     → JSON status blob
    GET /<path>     → LatticeFS file (if fs available), else 404
    """

    # Injected by the server factory
    fs         = None
    daemon_ctx = None   # reference to DaemonContext for status

    def log_message(self, fmt, *args):
        # Suppress default Apache-style log; daemon prints its own summary
        pass

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")

        if path in ("", "/", "/index.html"):
            self._serve_status_html()
        elif path == "/status":
            self._serve_status_json()
        else:
            self._serve_lattice(path.lstrip("/"))

    def _serve_status_html(self):
        ctx     = self.daemon_ctx
        uptime  = str(datetime.now() - ctx.start_time).split(".")[0]
        content = _STATUS_HTML_TEMPLATE.format(
            node_id    = ctx.comms.my_id,
            coord_file = ctx.comms.coord_file,
            uptime     = uptime,
            last_poll  = ctx.last_poll or "never",
            poll_count = ctx.poll_count,
            lfs_class  = "ok" if ctx.fs else "warn",
            lfs_status = "connected" if ctx.fs else "not available",
            activity   = "\n".join(ctx.activity_log[-20:]) or "(none yet)",
        )
        self._respond(200, "text/html; charset=utf-8", content.encode())

    def _serve_status_json(self):
        ctx  = self.daemon_ctx
        data = {
            "node_id":    ctx.comms.my_id,
            "coord_file": ctx.comms.coord_file,
            "poll_count": ctx.poll_count,
            "last_poll":  ctx.last_poll,
            "lattice_fs": ctx.fs is not None,
        }
        self._respond(200, "application/json", json.dumps(data, indent=2).encode())

    def _serve_lattice(self, path):
        fs = self.daemon_ctx.fs
        if fs is None:
            body = b"LatticeFS not available on this node."
            self._respond(503, "text/plain", body)
            return
        try:
            if not fs.exists(path):
                self._respond(404, "text/plain", b"404 Not Found in LatticeFS")
                return
            data     = fs.read_file(path)
            ctype    = "text/html" if path.endswith(".html") else "application/octet-stream"
            self._respond(200, ctype, data)
        except Exception as e:
            self._respond(500, "text/plain", f"LatticeFS error: {e}".encode())

    def _respond(self, code, ctype, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ─────────────────────────────────────────────────────────────
# Daemon context  (shared state between threads)
# ─────────────────────────────────────────────────────────────

class DaemonContext:
    def __init__(self, comms: GrokComms, fs):
        self.comms        = comms
        self.fs           = fs
        self.start_time   = datetime.now()
        self.last_poll    = None
        self.poll_count   = 0
        self.activity_log = []   # capped list of recent log lines

    def log(self, msg: str):
        ts      = datetime.now().strftime("%H:%M:%S")
        line    = f"[{ts}] {msg}"
        print(line)
        self.activity_log.append(line)
        if len(self.activity_log) > 200:
            self.activity_log = self.activity_log[-200:]


# ─────────────────────────────────────────────────────────────
# Background poller thread
# ─────────────────────────────────────────────────────────────

def _background_poller(ctx: DaemonContext):
    ctx.log("Background poller started.")
    while True:
        try:
            # Unified poll: temporal + beacons
            temporal_results = ctx.comms.poll()
            ctx.log(f"Temporal poll: {len(temporal_results)} message(s)")

            # Realtime replies
            rt_results = ctx.comms.poll_realtime()
            ctx.log(f"Realtime poll: {len(rt_results)} message(s)")

            ctx.last_poll  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
    # Inject ctx into handler class via a closure factory
    def handler_factory(*args, **kwargs):
        h = OdinWebHandler(*args, **kwargs)
        h.fs         = ctx.fs
        h.daemon_ctx = ctx
        return h

    # Allow rapid restart without "Address already in use"
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((host, port), handler_factory) as httpd:
        ctx.log(f"OdinWeb listening at http://{host}:{port}/")
        httpd.serve_forever()


# ─────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="OdinNet Daemon")
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
    args = parser.parse_args()

    print("\n" + "★" * 60)
    print("  ODINNET DAEMON")
    print("★" * 60)

    # ── GrokComms init ───────────────────────────────────────
    if not os.path.exists(args.coord):
        print(f"\n⚠  Coordinate file '{args.coord}' not found.")
        print("   Run: python grok_comms.py  → admin_menu → 1 (coordinate_generator)")
        print("   Then re-launch the daemon.")
        return

    comms = GrokComms(coord_file=args.coord)
    print(f"  Node ID    : {comms.my_id}")
    print(f"  Coord file : {args.coord}")

    # ── LatticeFS (optional) ─────────────────────────────────
    fs = _try_load_lattice_fs(passphrase=args.lattice_passphrase)
    if fs:
        print("  LatticeFS  : connected ✅")
    else:
        print("  LatticeFS  : not available (OdinWeb will serve status page only)")

    ctx = DaemonContext(comms=comms, fs=fs)

    # ── Start background poller ──────────────────────────────
    poller_thread = threading.Thread(
        target=_background_poller, args=(ctx,), daemon=True, name="OdinPoller"
    )
    poller_thread.start()

    # ── Start OdinWeb (blocks until Ctrl-C) ─────────────────
    try:
        _run_web_server(ctx, args.host, args.port)
    except KeyboardInterrupt:
        print("\n\n👋 OdinNet Daemon shutting down.")
    except OSError as e:
        print(f"\n⚠  OdinWeb failed to start: {e}")
        print(f"   Try a different port: python odinnet_daemon.py --port 9090")
        # Keep poller alive even if web server can't bind
        print("   Poller thread still running. Press Ctrl-C to exit.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n👋 OdinNet Daemon shutting down.")


if __name__ == "__main__":
    main()
