"""
OdinNet Daemon  v7.3 — Stateless Primary Engine
================================================
Background service for OdinNet.

Changes from v7.2:
  - NEW: StatelessCommsNode wired as the primary communication engine.
         Passphrase-derived coordinates; no coordinatefile.json required
         for the stateless path.
  - NEW: DaemonContext.stateless_node  — StatelessCommsNode instance.
  - NEW: DaemonContext.active_comms    — points to stateless_node by default.
  - NEW: DaemonContext.fallback_comms  — retains GrokComms for compatibility.
  - NEW: --legacy-mode CLI flag forces all traffic through old GrokComms path.
  - NEW: --passphrase CLI flag sets the shared OdinNet passphrase.
  - NEW: /api/send, /api/inbox, /api/status routed to active_comms.
  - KEPT: All v7.2 fleet, BBS, LatticeFS, and privacy endpoints unchanged.
  - KEPT: Background poller unchanged for GrokComms fallback path.
  - KEPT: StatelessPollWorker — dedicated background thread for stateless polling.
"""

import argparse
import hashlib
import http.server
import json
import mimetypes
import os
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

def _import_optional(module_name: str) -> object:
    import importlib
    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None

_gc_mod  = _import_or_die("grok_comms",      "Ensure grok_comms.py is in the same directory.")
_cg_mod  = _import_or_die("chart_generator", "Ensure chart_generator.py is in the same directory.")
_sc_mod  = _import_optional("stateless_comms")

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

lattice_fs_ctor              = _cg_mod.lattice_fs
LatticeDrive                 = _cg_mod.LatticeDrive
LatticeFS                    = _cg_mod.LatticeFS

# Stateless layer — optional so daemon still boots if stateless_comms.py is absent
if _sc_mod is not None:
    StatelessCommsNode = _sc_mod.StatelessCommsNode
    _STATELESS_AVAILABLE = True
else:
    StatelessCommsNode   = None
    _STATELESS_AVAILABLE = False
    print("  ⚠  stateless_comms not found — stateless engine disabled. "
          "Running in legacy mode.")

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

DEFAULT_HOST               = "0.0.0.0"
DEFAULT_PORT               = 8080
DEFAULT_PASSPHRASE         = os.environ.get("ODINNET_PASSPHRASE", "OdinNet_Shared_Ether_2026")
DEFAULT_NODE_ID            = os.environ.get("ODINNET_NODE_ID",    "OdinLocalNode")

POLL_INTERVAL_SEC          = 8
POLL_RT_INTERVAL_SEC       = 5
POLL_ERROR_BACKOFF         = 20
STATELESS_POLL_INTERVAL    = 10     # seconds between stateless coordinate scans
STATELESS_POLL_STEPS       = 100    # coordinate probes per stateless scan cycle
ANOMALY_INTERVAL_SEC       = 3600
LATTICE_IMAGE_PATH         = "odinnet_drive.json"
BBS_DATA_PATH              = "bbs_data.json"
BLOCKED_LIST_PATH          = "blocked.json"
PUBLISHED_URLS_PATH        = "published_urls.json"

FLEET_LOCAL_RADIUS_DEFAULT = 5000
FLEET_LOCAL_PROBES_DEFAULT = 50

API_TOKEN = os.environ.get("ODINNET_TOKEN", "odinnet-dev")

_DAEMON_DIR    = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_HTML = os.path.join(_DAEMON_DIR, "dashboard.html")
GUI_DIR        = os.path.join(_DAEMON_DIR, "gui")
WEB_ERROR_LOG  = os.path.join(_DAEMON_DIR, "web_error.log")

MAX_RECEIVED_LOG  = 100
MAX_RT_LOG        = 100
MAX_FLEET_LOG     = 100
MAX_ACTIVITY_LOG  = 500

def _log_web_error(msg: str, exc: Exception = None):
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(WEB_ERROR_LOG, "a") as f:
            f.write(f"\n[{ts}] {msg}\n")
            if exc:
                f.write(traceback.format_exc())
                f.write("\n")
        print(f"[WebError] {msg}", flush=True)
    except Exception:
        pass

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
# Port conflict recovery
# ─────────────────────────────────────────────────────────────

def _free_port(port: int) -> bool:
    print(f"  [Port] Attempting to free port {port}...", flush=True)
    pid_file = os.path.join(_DAEMON_DIR, f"odinnet_{port}.pid")
    if os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                old_pid = int(f.read().strip())
            if old_pid != os.getpid():
                print(f"  [Port] Killing old daemon PID {old_pid}...", flush=True)
                os.kill(old_pid, signal.SIGTERM)
                time.sleep(1.5)
        except Exception as e:
            print(f"  [Port] PID file kill attempt: {e}", flush=True)

    try:
        ret = subprocess.call(
            ["fuser", "-k", f"{port}/tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if ret == 0:
            print(f"  [Port] fuser freed port {port}.", flush=True)
            time.sleep(1)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"  [Port] fuser error: {e}", flush=True)

    try:
        lsof_cmd = f"lsof -ti:{port} 2>/dev/null | xargs kill -9 2>/dev/null"
        subprocess.call(lsof_cmd, shell=True)
        time.sleep(1)
    except Exception as e:
        print(f"  [Port] lsof kill error: {e}", flush=True)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            print(f"  [Port] Port {port} is now free.", flush=True)
            return True
        except OSError:
            print(f"  [Port] Port {port} still occupied after all attempts.", flush=True)
            return False

def _write_pid_file(port: int):
    pid_file = os.path.join(_DAEMON_DIR, f"odinnet_{port}.pid")
    try:
        with open(pid_file, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────
# GUI folder bootstrap
# ─────────────────────────────────────────────────────────────

def _ensure_gui_dir():
    """Create the gui/ folder if it doesn't exist (fixes Issue #1)."""
    os.makedirs(GUI_DIR, exist_ok=True)

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

        fs = lattice_fs_ctor(sector_size=1024, n_sectors=256, passphrase=passphrase)
        _seed_lattice_fs_with_education(fs)
        fs._drive.save(LATTICE_IMAGE_PATH)
        print(f"  LatticeFS  : created fresh → {LATTICE_IMAGE_PATH}")
        return fs
    except Exception as e:
        print(f"  ⚠  LatticeFS init failed: {e}")
        return None

def _seed_lattice_fs_with_education(fs: "LatticeFS"):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if os.path.exists(DASHBOARD_HTML):
        with open(DASHBOARD_HTML, "rb") as fh:
            dashboard_bytes = fh.read()
    else:
        dashboard_bytes = (
            f"<html><body><h1>⬡ OdinNet — Temporal Node</h1>"
            f"<p>Seeded {now}</p><p><a href='/status'>status</a></p></body></html>"
        ).encode("utf-8")
    readme = f"OdinNet — Temporal Node\n=======================\nNode started : {now}\n".encode("utf-8")
    fs.write_file("index.html", dashboard_bytes)
    fs.write_file("readme.txt", readme)

def _update_dashboard_in_fs(fs: "LatticeFS") -> bool:
    if not os.path.exists(DASHBOARD_HTML):
        return False
    try:
        with open(DASHBOARD_HTML, "rb") as fh:
            fresh = fh.read()
        fs.write_file("index.html", fresh)
        return True
    except Exception:
        return False

def _save_lattice(fs: "LatticeFS"):
    try:
        fs._drive.save(LATTICE_IMAGE_PATH)
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────
# Data layers
# ─────────────────────────────────────────────────────────────

def _load_published_urls() -> list:
    if not os.path.exists(PUBLISHED_URLS_PATH):
        return []
    try:
        with open(PUBLISHED_URLS_PATH) as f: return json.load(f)
    except Exception: return []

def _save_published_urls(urls: list):
    try:
        with open(PUBLISHED_URLS_PATH, "w") as f: json.dump(urls, f, indent=2)
    except Exception: pass

_BBS_DEFAULT = {
    "rooms": {
        "the_thing":     {"name": "The Thing",     "description": "Public chat.",    "policy": "open",       "posts": []},
        "announcements": {"name": "Announcements",  "description": "System notices.", "policy": "admin_post", "posts": []},
        "coordinates":   {"name": "Coordinates",    "description": "Coord sharing.",  "policy": "open",       "posts": []},
    }
}

def _load_bbs() -> dict:
    if os.path.exists(BBS_DATA_PATH):
        try:
            with open(BBS_DATA_PATH) as f: return json.load(f)
        except Exception: pass
    return json.loads(json.dumps(_BBS_DEFAULT))

def _save_bbs(data: dict):
    try:
        with open(BBS_DATA_PATH, "w") as f: json.dump(data, f, indent=2)
    except Exception: pass

def _load_blocked() -> dict:
    if os.path.exists(BLOCKED_LIST_PATH):
        try:
            with open(BLOCKED_LIST_PATH) as f: return json.load(f)
        except Exception: pass
    return {"blocked": [], "muted": []}

def _save_blocked(data: dict):
    try:
        with open(BLOCKED_LIST_PATH, "w") as f: json.dump(data, f, indent=2)
    except Exception: pass

# ─────────────────────────────────────────────────────────────
# Background loops
# ─────────────────────────────────────────────────────────────

def _anomaly_detector_loop(ctx: "DaemonContext"):
    time.sleep(60)
    while True:
        try:
            now_str       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            yesterday_str = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
            compose_message(
                to_date    = yesterday_str,
                subject    = f"[ANOMALY] Status — {now_str}",
                body       = f"Node ID: {ctx.fallback_comms.my_id}\nTime: {now_str}",
                coord_file = ctx.fallback_comms.coord_file,
            )
        except Exception:
            pass
        time.sleep(ANOMALY_INTERVAL_SEC)

def _ensure_coordinate(coord_file: str) -> bool:
    if os.path.exists(coord_file):
        return True
    try:
        coordinate_generator(f"odinnet-{socket.gethostname()}", num_digits=150, output_file=coord_file)
        polling_range_finder(num_samples=30, coord_file=coord_file)
        return True
    except Exception:
        return False

# ─────────────────────────────────────────────────────────────
# DaemonContext  — v7.3 bridge architecture
# ─────────────────────────────────────────────────────────────

class DaemonContext:
    """
    Central shared-state container for the OdinNet daemon.

    v7.3 Bridge Architecture
    ------------------------
    self.stateless_node  — StatelessCommsNode (new primary engine)
    self.fallback_comms  — GrokComms          (legacy engine, kept for compatibility)
    self.active_comms    — alias pointing to whichever engine is active
                           default → stateless_node
                           --legacy-mode → fallback_comms

    The web handler and background workers always call self.active_comms
    so the engine swap is transparent to all upstream code.

    active_comms contract (both engines honour this interface):
        .send(text)          → (coordinate, pad_bytes)
        .poll(steps=N)       → list[dict]
        .inbox()             → list[dict]
        .status()            → dict
    """

    def __init__(
        self,
        comms:          "GrokComms",
        fs:             "LatticeFS | None",
        security:       "OdinNetSecurity | None" = None,
        stateless_node: "StatelessCommsNode | None" = None,
        legacy_mode:    bool = False,
    ):
        # ── Engine layer ──────────────────────────────────────────────────
        self.fallback_comms  = comms            # GrokComms (legacy)
        self.stateless_node  = stateless_node   # StatelessCommsNode (primary)

        if stateless_node is not None and not legacy_mode:
            self.active_comms = stateless_node
            self._engine_name = "stateless"
        else:
            self.active_comms = comms           # GrokComms implements the same contract
            self._engine_name = "legacy"

        # ── Supporting subsystems ─────────────────────────────────────────
        self.fs            = fs
        self.security      = security
        self.start_time    = datetime.now()
        self._lock         = threading.RLock()
        self._fleet_lock   = threading.RLock()

        self._last_poll     = None
        self._poll_count    = 0
        self._activity_log  = []
        self._received_log  = []    # stateless inbox mirror (for dashboard compat)
        self._rt_log        = []
        self._fleet_log     = []
        self._beacon_status = []

        self._bbs_lock      = threading.RLock()
        self._pub_lock      = threading.RLock()
        self._blocked_lock  = threading.RLock()

    # ── Engine bridge methods — used by web handler ───────────────────────

    def send_message(self, text: str, subject: str = "") -> dict:
        """
        Send via active_comms.  Returns a result dict suitable for JSON response.
        Uniform interface — works whether stateless or legacy is active.
        """
        try:
            if self._engine_name == "stateless":
                coord, pad = self.active_comms.send(text, subject=subject)
                return {"status": "success", "engine": "stateless",
                        "coordinate": str(coord)[:40], "pad_bytes": pad}
            else:
                # GrokComms legacy path: compose + send_outbox
                fname = self.fallback_comms.compose_message(
                    to_date = _today_str(),
                    subject = subject or "(no subject)",
                    body    = text,
                )
                self.fallback_comms.send_outbox()
                return {"status": "success", "engine": "legacy",
                        "coordinate": "see sent/ folder", "pad_bytes": 0}
        except Exception as e:
            return {"status": "error", "engine": self._engine_name, "message": str(e)}

    def get_inbox(self) -> list:
        """
        Return inbox from active engine.
        Stateless: in-memory list.  Legacy: reads from _received_log.
        """
        if self._engine_name == "stateless":
            return self.active_comms.inbox()
        else:
            with self._lock:
                return list(reversed(self._received_log))

    def get_status(self) -> dict:
        """
        Merge active_comms.status() with DaemonContext runtime metrics.
        Always returns a dict safe to JSON-serialise.
        """
        engine_status = {}
        try:
            engine_status = self.active_comms.status()
        except Exception:
            pass

        with self._lock:
            ctx_status = {
                "node_id":        getattr(self.fallback_comms, "my_id", "unknown"),
                "engine":         self._engine_name,
                "uptime_sec":     int((datetime.now() - self.start_time).total_seconds()),
                "poll_count":     self._poll_count,
                "last_poll":      self._last_poll,
                "lattice_fs":     self.fs is not None,
                "active_fleets":  self.get_current_fleets(),
                "activity_tail":  list(self._activity_log[-40:]),
                "lattice_files":  [] if not self.fs else [{"name": n} for n in self.fs._index],
                "beacons":        self._beacon_status,
            }

        # Engine status takes precedence on shared keys where richer data exists
        merged = {**ctx_status, **engine_status}
        return merged

    # ── Fleet methods (delegate to fallback_comms / GrokComms) ───────────

    def join_fleet(self, name: str, v: int, r: int,
                   level: str = "fleet", radius: int = FLEET_LOCAL_RADIUS_DEFAULT):
        with self._fleet_lock:
            self.fallback_comms.set_active_fleet(name, v, r, level=level, radius=radius)
            self.log(f"[Fleet] Joined '{name}'")

    def leave_fleet(self, name: str = None):
        with self._fleet_lock:
            self.fallback_comms.clear_active_fleet(name)

    def leave_all_fleets(self):
        self.leave_fleet(name=None)

    def get_current_fleets(self) -> dict:
        return self.fallback_comms.get_active_fleets()

    def get_all_local_traffic_ranges(self) -> list:
        return self.fallback_comms.get_active_contexts()

    # ── Logging ───────────────────────────────────────────────────────────

    def log(self, msg: str):
        ts   = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        with self._lock:
            self._activity_log.append(line)
            if len(self._activity_log) > MAX_ACTIVITY_LOG:
                self._activity_log = self._activity_log[-MAX_ACTIVITY_LOG:]

    def add_temporal_received(self, records: list):
        with self._lock:
            self._received_log = records + self._received_log
            if len(self._received_log) > MAX_RECEIVED_LOG:
                self._received_log = self._received_log[:MAX_RECEIVED_LOG]

    def add_rt_received(self, records: list):
        with self._lock:
            self._rt_log = records + self._rt_log
            if len(self._rt_log) > MAX_RT_LOG:
                self._rt_log = self._rt_log[:MAX_RT_LOG]

    def add_fleet_received(self, records: list, fleet_name: str = ""):
        with self._lock:
            self._fleet_log = records + self._fleet_log
            if len(self._fleet_log) > MAX_FLEET_LOG:
                self._fleet_log = self._fleet_log[:MAX_FLEET_LOG]

    def refresh_beacons(self):
        try:
            self._beacon_status = _load_beacons()
        except Exception:
            pass

    def status_dict(self) -> dict:
        """Backwards-compat alias — dashboard JS calls /api/status → status_dict."""
        return self.get_status()

# ─────────────────────────────────────────────────────────────
# Background poller threads
# ─────────────────────────────────────────────────────────────

def _legacy_background_poller(ctx: DaemonContext):
    """
    Original GrokComms polling loop — runs when --legacy-mode is set
    OR when stateless engine is unavailable.
    Always runs in background regardless of active engine so the
    GrokComms received log stays warm.
    """
    while True:
        try:
            t_res = ctx.fallback_comms.poll()
            if t_res:
                ctx.add_temporal_received(t_res)
            rt_res = ctx.fallback_comms.poll_realtime()
            if rt_res:
                ctx.add_rt_received(rt_res)

            for (name, low, high) in ctx.get_all_local_traffic_ranges():
                f_res = ctx.fallback_comms.poll_range(low, high)
                if f_res:
                    ctx.add_fleet_received(f_res, fleet_name=name)

            with ctx._lock:
                ctx._poll_count += 1
                ctx._last_poll   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            time.sleep(POLL_INTERVAL_SEC)
        except Exception:
            time.sleep(POLL_ERROR_BACKOFF)


def _stateless_background_poller(ctx: DaemonContext):
    """
    Dedicated background thread for StatelessCommsNode polling.
    Runs continuously regardless of active_comms setting so the
    stateless inbox stays current in the background.
    """
    if ctx.stateless_node is None:
        return

    print("[StatelessPoller] Background poll thread started.", flush=True)
    while True:
        try:
            new_msgs = ctx.stateless_node.poll(steps=STATELESS_POLL_STEPS)
            if new_msgs:
                ctx.log(f"[StatelessPoller] {len(new_msgs)} new message(s) received.")
                # Mirror into the legacy received log so dashboard works for both engines
                ctx.add_temporal_received(new_msgs)

            with ctx._lock:
                if ctx._engine_name == "stateless":
                    ctx._poll_count += 1
                    ctx._last_poll   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        except Exception as e:
            ctx.log(f"[StatelessPoller] Error: {e}")
        time.sleep(STATELESS_POLL_INTERVAL)


def _check_token(handler: "OdinWebHandler") -> bool:
    if handler.headers.get("X-OdinNet-Token", "") == API_TOKEN:
        return True
    handler._respond(401, "application/json", b'{"ok":false,"error":"Unauthorized"}')
    return False

# ─────────────────────────────────────────────────────────────
# HTTP handler
# ─────────────────────────────────────────────────────────────

class OdinWebHandler(http.server.BaseHTTPRequestHandler):
    daemon_ctx = None

    def log_message(self, fmt, *args):
        pass  # Suppress SimpleHTTPRequestHandler stdout noise (Issue #2 fix)

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            _log_web_error("Socket connection exception during handle state", e)

    def do_GET(self):
        try:
            path = self.path.split("?")[0]

            # ── Stateless / unified API endpoints ──────────────────────
            if path in ("/status", "/api/status"):
                self._serve_status_json()

            elif path == "/api/inbox":
                self._serve_inbox_json()

            elif path in ("/", ""):
                self._serve_root()

            # ── Fleet endpoints (token-gated) ───────────────────────────
            elif path == "/api/fleet/status":
                if _check_token(self): self._serve_fleet_status()

            # ── BBS endpoints ────────────────────────────────────────────
            elif path == "/api/bbs/rooms":
                self._serve_bbs_rooms()

            elif path.startswith("/api/bbs/posts/"):
                room_id = path.split("/api/bbs/posts/")[-1].strip("/")
                self._serve_bbs_posts(room_id)

            # ── Published URLs / LatticeFS passthrough ───────────────────
            elif path == "/api/urls":
                self._respond(200, "application/json",
                              json.dumps(_load_published_urls()).encode())

            elif path == "/api/engine":
                ctx = self.daemon_ctx
                info = {
                    "engine":    ctx._engine_name,
                    "stateless": ctx.stateless_node is not None,
                    "legacy":    True,
                }
                self._respond(200, "application/json", json.dumps(info).encode())

            else:
                self._serve_lattice(path.lstrip("/"))

        except Exception as e:
            _log_web_error(f"GET failed on routing layer for {self.path}", e)
            self._respond(500, "application/json", b'{"ok":false,"error":"Internal Crash"}')

    # ── GET response builders ─────────────────────────────────────────────

    def _serve_root(self):
        ctx = self.daemon_ctx
        # Try LatticeFS index.html first
        if ctx and ctx.fs and ctx.fs.exists("index.html"):
            self._respond(200, "text/html", ctx.fs.read_file("index.html"))
            return
        # Try gui/ folder on disk
        gui_index = os.path.join(GUI_DIR, "index.html")
        if os.path.exists(gui_index):
            with open(gui_index, "rb") as fh:
                self._respond(200, "text/html", fh.read())
            return
        # Try dashboard.html on disk directly
        if os.path.exists(DASHBOARD_HTML):
            with open(DASHBOARD_HTML, "rb") as fh:
                self._respond(200, "text/html", fh.read())
            return
        self._serve_status_html_fallback()

    def _serve_status_html_fallback(self):
        ctx = self.daemon_ctx
        d   = ctx.status_dict()
        body = (
            f'<!DOCTYPE html><html><head><meta charset="utf-8">'
            f'<title>OdinNet Node</title>'
            f'<style>body{{font-family:monospace;background:#06080a;color:#00ff66;padding:20px;}}'
            f'pre{{color:#88ffcc;}}</style></head>'
            f'<body><h1>⬡ OdinNet Node Panel v7.3</h1>'
            f'<p>ID: {d.get("node_id","?")}  |  Engine: {d.get("engine","?")}  |  '
            f'Polls: {d.get("poll_count",0)}</p>'
            f'<pre>{"<br>".join(d.get("activity_tail",[]))}</pre>'
            f'<p><a href="/api/status" style="color:#00ff66">→ /api/status (JSON)</a></p>'
            f'</body></html>'
        ).encode("utf-8")
        self._respond(200, "text/html", body)

    def _serve_status_json(self):
        self._respond(200, "application/json",
                      json.dumps(self.daemon_ctx.status_dict(), indent=2).encode())

    def _serve_inbox_json(self):
        """
        /api/inbox — returns messages from active_comms.
        Works for both stateless (in-memory) and legacy (file-backed) engines.
        """
        inbox = self.daemon_ctx.get_inbox()
        self._respond(200, "application/json", json.dumps(inbox).encode())

    def _serve_fleet_status(self):
        ctx = self.daemon_ctx
        self._respond(200, "application/json",
                      json.dumps({"ok": True,
                                  "active_fleets": ctx.get_current_fleets()}).encode())

    def _serve_bbs_rooms(self):
        self._respond(200, "application/json", json.dumps(_load_bbs()).encode())

    def _serve_bbs_posts(self, room_id: str):
        bbs  = _load_bbs()
        room = bbs.get("rooms", {}).get(room_id)
        if room is None:
            self._respond(404, "application/json",
                          json.dumps({"ok": False, "error": "Room not found"}).encode())
        else:
            self._respond(200, "application/json",
                          json.dumps(room.get("posts", [])).encode())

    def _serve_lattice(self, filename: str):
        ctx = self.daemon_ctx
        # 1. LatticeFS
        if ctx and ctx.fs and ctx.fs.exists(filename):
            self._respond(200, _mime_for(filename), ctx.fs.read_file(filename))
            return
        # 2. gui/ folder on disk
        disk_path = os.path.join(GUI_DIR, filename)
        if os.path.exists(disk_path) and os.path.isfile(disk_path):
            with open(disk_path, "rb") as fh:
                self._respond(200, _mime_for(filename), fh.read())
            return
        self._respond(404, "text/plain", b"File not found inside filesystem mount.")

    # ── POST routing ──────────────────────────────────────────────────────

    def do_POST(self):
        try:
            path = self.path.split("?")[0]
            if not _check_token(self):
                return

            if path == "/api/send":
                self._handle_send()
            elif path == "/api/fleet/join":
                self._handle_fleet_join()
            elif path == "/api/fleet/leave":
                self._handle_fleet_leave()
            elif path == "/api/bbs/post":
                self._handle_bbs_post()
            elif path == "/api/publish":
                self._handle_publish_url()
            else:
                self._respond(404, "application/json", b'{"ok":false,"error":"Not Found"}')
        except Exception as e:
            _log_web_error(f"POST failed on {self.path}", e)
            self._respond(500, "application/json", b'{"ok":false}')

    def _read_json_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return None

    def _handle_send(self):
        """
        /api/send — routes through active_comms (stateless or legacy).
        Payload: {"message": "text", "subject": "optional"}
        """
        body = self._read_json_body()
        if not body or "message" not in body:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"Missing message field"}')
            return

        result = self.daemon_ctx.send_message(
            text    = body["message"],
            subject = body.get("subject", ""),
        )
        ok = result.get("status") == "success"
        payload = {"ok": ok, **result}
        self._respond(200 if ok else 500, "application/json",
                      json.dumps(payload).encode())

    def _handle_fleet_join(self):
        body = self._read_json_body()
        if body and "name" in body and "v" in body and "r" in body:
            self.daemon_ctx.join_fleet(body["name"], int(body["v"]), int(body["r"]))
            self._respond(200, "application/json", b'{"ok":true,"status":"joined"}')
        else:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"Missing name/v/r"}')

    def _handle_fleet_leave(self):
        body = self._read_json_body() or {}
        self.daemon_ctx.leave_fleet(body.get("name"))
        self._respond(200, "application/json", b'{"ok":true,"status":"evacuated"}')

    def _handle_bbs_post(self):
        body = self._read_json_body()
        if not body or "room" not in body or "text" not in body:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"Missing room or text"}')
            return
        with self.daemon_ctx._bbs_lock:
            bbs    = _load_bbs()
            room   = bbs.get("rooms", {}).get(body["room"])
            if room is None:
                self._respond(404, "application/json",
                              b'{"ok":false,"error":"Room not found"}')
                return
            post = {
                "id":        str(len(room["posts"]) + 1),
                "author":    body.get("author", "anonymous"),
                "text":      body["text"],
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            room["posts"].append(post)
            if len(room["posts"]) > 200:
                room["posts"] = room["posts"][-200:]
            _save_bbs(bbs)
        self._respond(200, "application/json",
                      json.dumps({"ok": True, "post": post}).encode())

    def _handle_publish_url(self):
        body = self._read_json_body()
        if not body or "url" not in body:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"Missing url"}')
            return
        with self.daemon_ctx._pub_lock:
            urls = _load_published_urls()
            entry = {
                "url":       body["url"],
                "published": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "note":      body.get("note", ""),
            }
            urls.append(entry)
            _save_published_urls(urls)
        self._respond(200, "application/json",
                      json.dumps({"ok": True, "entry": entry}).encode())

    # ── CORS + OPTIONS ────────────────────────────────────────────────────

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-OdinNet-Token")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def _respond(self, code: int, ctype: str, body: bytes):
        try:
            self.send_response(code)
            self.send_header("Content-Type",   ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection",     "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except Exception:
            pass

# ─────────────────────────────────────────────────────────────
# Threaded web server
# ─────────────────────────────────────────────────────────────

class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads      = True

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (AttributeError, OSError):
            pass
        super().server_bind()


def _run_web_server(ctx: DaemonContext, host: str, port: int):
    def handler_factory(*args, **kwargs):
        h = OdinWebHandler(*args, **kwargs)
        h.daemon_ctx = ctx
        return h

    print(f"[WebServer] Binding on {host}:{port}", flush=True)
    resolved_host = host if host != "0.0.0.0" else ""

    try:
        server = ThreadingHTTPServer((resolved_host, port), handler_factory)
        _write_pid_file(port)
        ctx.log(f"OdinWeb v7.3 running on port {port}  engine={ctx._engine_name}")
        server.serve_forever()
    except Exception as e:
        _log_web_error("Fatal startup error on primary bind", e)
        if _free_port(port):
            try:
                server = ThreadingHTTPServer((resolved_host, port), handler_factory)
                server.serve_forever()
            except Exception as e2:
                _log_web_error("Secondary bind attempt also failed", e2)

# ─────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="OdinNet Engine v7.3")
    parser.add_argument("--coord",        default=COORD_FILE,
                        help="GrokComms coordinate file (legacy path)")
    parser.add_argument("--port",         type=int, default=DEFAULT_PORT)
    parser.add_argument("--host",         default=DEFAULT_HOST)
    parser.add_argument("--no-web",       action="store_true",
                        help="Run poller only, no HTTP server")
    parser.add_argument("--legacy-mode",  action="store_true",
                        help="Force GrokComms file-based engine as active_comms")
    parser.add_argument("--passphrase",   default=DEFAULT_PASSPHRASE,
                        help="Shared OdinNet passphrase for stateless engine")
    parser.add_argument("--node-id",      default=DEFAULT_NODE_ID,
                        help="Node identity label")
    args = parser.parse_args()

    # ── GrokComms (always needed for fleet/BBS/LatticeFS) ────────────────
    if not _ensure_coordinate(args.coord):
        print("⚠  Could not create coordinate file — legacy polling disabled.",
              flush=True)
    comms = GrokComms(coord_file=args.coord)

    # ── LatticeFS ─────────────────────────────────────────────────────────
    fs = _try_load_lattice_fs()
    if fs:
        _update_dashboard_in_fs(fs)
        _save_lattice(fs)

    # ── gui/ folder ────────────────────────────────────────────────────────
    _ensure_gui_dir()   # Issue #1 fix: guarantee gui/ always exists

    # ── StatelessCommsNode ────────────────────────────────────────────────
    stateless_node = None
    if _STATELESS_AVAILABLE and not args.legacy_mode:
        try:
            stateless_node = StatelessCommsNode(
                passphrase = args.passphrase,
                node_id    = args.node_id,
            )
            print(f"  StatelessNode : initialised  "
                  f"passphrase={args.passphrase[:4]}****  "
                  f"node_id={args.node_id}", flush=True)
        except Exception as e:
            print(f"  ⚠  StatelessCommsNode init failed: {e} — falling back to legacy.",
                  flush=True)
    elif args.legacy_mode:
        print("  [--legacy-mode] StatelessCommsNode suppressed.", flush=True)

    # ── DaemonContext ─────────────────────────────────────────────────────
    ctx = DaemonContext(
        comms          = comms,
        fs             = fs,
        stateless_node = stateless_node,
        legacy_mode    = args.legacy_mode or (stateless_node is None),
    )

    # ── Background threads ─────────────────────────────────────────────────
    threading.Thread(
        target  = _legacy_background_poller,
        args    = (ctx,),
        daemon  = True,
        name    = "LegacyPoller",
    ).start()

    if stateless_node is not None:
        threading.Thread(
            target  = _stateless_background_poller,
            args    = (ctx,),
            daemon  = True,
            name    = "StatelessPoller",
        ).start()

    threading.Thread(
        target  = _anomaly_detector_loop,
        args    = (ctx,),
        daemon  = True,
        name    = "AnomalyDetector",
    ).start()

    # ── Banner ────────────────────────────────────────────────────────────
    print(f"\n{'='*55}")
    print(f"  ODINNET NODE v7.3  —  {ctx._engine_name.upper()} ENGINE ACTIVE")
    print(f"  Node ID    : {args.node_id}")
    print(f"  Passphrase : {args.passphrase[:4]}****")
    print(f"  Coord file : {args.coord}  (legacy fallback)")
    print(f"  URL        : http://localhost:{args.port}/")
    print(f"  Token      : {API_TOKEN}")
    print(f"{'='*55}\n")

    if args.no_web:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return

    _run_web_server(ctx, args.host, args.port)


if __name__ == "__main__":
    main()
