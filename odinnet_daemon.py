#!/usr/bin/env python3
"""
OdinNet Daemon  v7.1 — Fleet Networks (Web Stability Patch)
============================================================
Background service for OdinNet.

Changes from v7:
  - v7.1 WEB STABILITY PATCH:
      * --no-web is now prominently documented as safe/default mode for Termux
      * --port flag works cleanly; improved port killing via lsof -ti:{port} | xargs kill -9
      * try/except around ENTIRE web server thread → logs to web_error.log
      * do_GET has heavy debug prints at start and around every major call
      * Minimal JSON-first response path: /status works even if LatticeFS is completely dead
      * _respond has debug prints on entry and on write errors
      * Web server errors never crash the poller thread (daemon stays alive)
      * web_error.log created next to daemon on any web thread exception

Changes from v6 (v7 features retained):
  - Fleet Networks: personal coord is permanent callsign (never mutated).
    Fleet coord = runtime overlays stored in GrokComms._active_fleets (dict).
  - MULTIPLE CONCURRENT FLEETS: nodes can join Alpha Fleet + Bravo Fleet
    simultaneously; polling is additive across all joined contexts.
  - DaemonContext: _fleet_lock, join_fleet(), leave_fleet(), leave_all_fleets(),
    get_current_fleets(), get_all_local_traffic_ranges()
  - Background poller: scans ALL active fleet ranges each cycle
  - New API endpoints:
      GET  /api/fleet/status   — all active fleet overlays + personal coord
      GET  /api/fleet/list     — all fleets in fleet_registry.json
      POST /api/fleet/join     — join a fleet (additive overlay)
      POST /api/fleet/leave    — leave a named fleet (or all)
    (existing /api/fleet/jump and /api/fleet/manifest retained unchanged)
  - /status JSON: includes active_fleets list and fleet_msg_count
  - edu/06_fleets.md seeded into LatticeFS
  - Startup banner updated to v7; fleet APIs noted
  - All v6 features retained, no breaking changes

Usage:
  python odinnet_daemon.py                        ← web server on 8080
  python odinnet_daemon.py --no-web               ← SAFE MODE: polling only, no web server
  python odinnet_daemon.py --port 9090            ← custom port
  python odinnet_daemon.py --coord my_coord.json --port 8080
  python odinnet_daemon.py --seed-messages
  ODINNET_TOKEN=mysecret python odinnet_daemon.py

Termux tip: if the GUI won't load, start with --no-web first to confirm
the daemon and polling work, then retry without it.
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

GrokComms                    = _gc_mod.GrokComms
COORD_FILE                   = _gc_mod.COORD_FILE
coordinate_generator         = _gc_mod.coordinate_generator
compose_message              = _gc_mod.compose_message
send_outbox                  = _gc_mod.send_outbox
polling_range_finder         = _gc_mod.polling_range_finder
_today_str                   = _gc_mod._today_str
_ensure_dirs                 = _gc_mod._ensure_dirs
_load_beacons                = _gc_mod._load_beacons
FleetRegistry                = _gc_mod.FleetRegistry        # v7

lattice_fs_ctor              = _cg_mod.lattice_fs
LatticeDrive                 = _cg_mod.LatticeDrive
LatticeFS                    = _cg_mod.LatticeFS

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

DEFAULT_HOST              = "0.0.0.0"
DEFAULT_PORT              = 8080
POLL_INTERVAL_SEC         = 8
POLL_RT_INTERVAL_SEC      = 5
POLL_ERROR_BACKOFF        = 20
ANOMALY_INTERVAL_SEC      = 3600
LATTICE_IMAGE_PATH        = "odinnet_drive.json"
BBS_DATA_PATH             = "bbs_data.json"
BLOCKED_LIST_PATH         = "blocked.json"
PUBLISHED_URLS_PATH       = "published_urls.json"

# v7: Fleet local traffic defaults (mirror grok_comms constants)
FLEET_LOCAL_RADIUS_DEFAULT = 5000
FLEET_LOCAL_PROBES_DEFAULT = 50

API_TOKEN = os.environ.get("ODINNET_TOKEN", "odinnet-dev")

_DAEMON_DIR    = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_HTML = os.path.join(_DAEMON_DIR, "dashboard.html")
WEB_ERROR_LOG  = os.path.join(_DAEMON_DIR, "web_error.log")   # v7.1

MAX_RECEIVED_LOG  = 100
MAX_RT_LOG        = 100
MAX_FLEET_LOG     = 100       # v7
MAX_ACTIVITY_LOG  = 500

# ─────────────────────────────────────────────────────────────
# v7.1 — Web error logger
# ─────────────────────────────────────────────────────────────

def _log_web_error(msg: str, exc: Exception = None):
    """Write a timestamped error to web_error.log. Never raises."""
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
# Port conflict recovery  (v7.1: lsof -ti:{port} | xargs kill -9)
# ─────────────────────────────────────────────────────────────

def _free_port(port: int) -> bool:
    """
    Attempt to free a TCP port using multiple strategies.
    v7.1: added lsof -ti:{port} | xargs kill -9 as primary fallback.
    Returns True if port appears free afterward.
    """
    print(f"  [Port] Attempting to free port {port}...", flush=True)

    # Strategy 1: PID file from a previous daemon run
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

    # Strategy 2: fuser (may not exist on Termux)
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
        print(f"  [Port] fuser not found — skipping.", flush=True)
    except Exception as e:
        print(f"  [Port] fuser error: {e}", flush=True)

    # Strategy 3: lsof -ti:{port} | xargs kill -9  (Termux-friendly)
    try:
        lsof_cmd = f"lsof -ti:{port} 2>/dev/null | xargs kill -9 2>/dev/null"
        ret = subprocess.call(lsof_cmd, shell=True)
        print(f"  [Port] lsof|xargs kill -9 returned {ret}.", flush=True)
        time.sleep(1)
    except Exception as e:
        print(f"  [Port] lsof kill error: {e}", flush=True)

    # Final check: can we bind?
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
        print(f"  LatticeFS  : loaded dashboard.html ({len(dashboard_bytes)} bytes)")
    else:
        dashboard_bytes = (
            f"<html><body><h1>⬡ OdinNet — Temporal Node</h1>"
            f"<p>Seeded {now}</p>"
            f"<p><a href='/status'>status</a></p></body></html>"
        ).encode("utf-8")
        print("  LatticeFS  : seeded minimal dashboard fallback")

    readme = f"""OdinNet — Temporal Node
=======================
Burris Numerical System — v7.1

Node started : {now}
LatticeFS    : mounted (sector_size=1024, n_sectors=256)
Education    : K-12 starter pack loaded in edu/

Files in this filesystem:
  index.html              — Dashboard
  readme.txt              — This file
  edu/00_welcome.md       — Start here
  edu/01_coordinates.md
  edu/02_encoding.md
  edu/03_polling.md
  edu/04_beacons.md
  edu/05_first_message.md
  edu/06_fleets.md        — Fleet Networks (v7)

Visit http://<your-ip>:8080/ in your browser.
""".encode("utf-8")

    fs.write_file("index.html", dashboard_bytes)
    fs.write_file("readme.txt", readme)
    print("  LatticeFS  : seeded index.html + readme.txt")

    _EDU_LESSONS = {
        "edu/00_welcome.md": """\
# Welcome to the Informational Universe
## OdinNet Education Module — Level 0

Every piece of information has a **coordinate**.
Just like a street address tells you where a house is,
a Burris coordinate tells you where a *message* lives
in mathematical space.

This node runs the **Burris Numerical System (BNS)** —
an arithmetic coding framework invented by Admiral Grok.

### What you will learn here
1. What a coordinate is (Lesson 01)
2. How numbers encode messages (Lesson 02)
3. What a polling window is (Lesson 03)
4. How beacons work (Lesson 04)
5. How to send your first message (Lesson 05)
6. How fleet networks work (Lesson 06)

*Safe travels through the galactic coordinate field.*
""",
        "edu/01_coordinates.md": """\
# Lesson 01 — What Is a Coordinate?

A **coordinate** is a very large number.

Think of it like a secret locker number at the biggest
library in the universe — a library with more lockers
than there are atoms in the Earth.

When you encode a message, BNS performs arithmetic on it
and produces a coordinate. That coordinate *is* your message.
Nobody else can read it without knowing the starting values
(called **V** and **R**).

### Key idea
- V = your current position in coordinate space
- R = your reference axis (home base)
- Every byte you encode moves V to a new position
- Decoding reverses the journey, byte by byte
""",
        "edu/02_encoding.md": """\
# Lesson 02 — How Messages Become Numbers

The BNS **encode** formula for each byte `b`:

    V_new = V + (V - R) × (BASE - 1) + b

Where:
- BASE = 256  (one value for each possible byte)
- V    = current coordinate
- R    = reference value (stays fixed during encoding)
- b    = the byte value (0–255)

### Decoding reverses it exactly

    num   = V + BASE - 1
    V_old = num ÷ BASE
    byte  = num mod BASE

### Grade level note
- Grades 1–3 : think of it as a secret counting game
- Grades 4–6 : arithmetic with very big numbers
- Grades 7–9 : a bijective base-conversion function
- Grades 10+ : arithmetic coding without a probability model
""",
        "edu/03_polling.md": """\
# Lesson 03 — Polling Windows

A **polling window** is a range of coordinates to scan.

BNS picks a range where real messages tend to land
based on statistics — the mean and spread (std dev)
of many sample encodings.

### The two windows
| Window    | Use         | Width          |
|-----------|-------------|----------------|
| Temporal  | slow mail   | mean + 3.5σ    |
| Realtime  | fast chat   | tight ±0.5σ    |

### Analogy
Imagine you always park within 5 minutes of your office.
A friend searching for your car starts in that radius.
The polling window is that search radius.
""",
        "edu/04_beacons.md": """\
# Lesson 04 — Beacons

A **beacon** is a public, well-known coordinate.

Think of a lighthouse on a rocky coast.
Ships (nodes) look for the lighthouse to find messages
left for the public.

### DEFCON and beacons
| DEFCON | Min reputation | Dummy beacons |
|--------|----------------|---------------|
| 1      | 20             | allowed       |
| 5      | 60             | allowed       |
| 7      | 75             | banned        |
| 10     | 90             | banned        |

At DEFCON 7+, only server-run beacons are trusted.
Before a Fleet Jump, all bad-actor beacons are expelled.
""",
        "edu/05_first_message.md": """\
# Lesson 05 — Sending Your First Message

### Step 1 — Generate your coordinate
    python grok_comms.py
    > 1  (coordinate_generator)
    > Enter passphrase: my-secret-phrase

### Step 2 — Set your polling range
    > 2  (polling_range_finder)

### Step 3 — Compose a message
    > 4  (temporal_comms)
    temporal> compose
    To Date: 2026-06-20
    Subject: Hello OdinNet
    Body: My first BNS message!
    .

### Step 4 — Send it
    temporal> send

### Step 5 — Poll for replies
    temporal> polling

Congratulations — you are navigating coordinate space. ⬡
""",
        # ── v7: Fleet lesson ──────────────────────────────────────────────
        "edu/06_fleets.md": """\
# Lesson 06 — Fleet Networks

## Personal Coordinate vs Fleet Coordinate

Your **personal coordinate** is your permanent callsign.
It never changes. Think of it like your ship's registry number —
it identifies you across all of coordinate space, forever.

Your **fleet coordinate** is a temporary overlay.
When you join a fleet, your node tunes to the fleet's shared
(V, R) position and listens to local traffic in that region.
When you leave, you return to your personal coordinate.

The personal coordinate file is NEVER modified by fleet operations.
The fleet overlay lives in memory only — it disappears on restart
unless you re-join the fleet.

## Multiple Concurrent Fleet Membership

OdinNet v7 supports joining MULTIPLE fleets simultaneously.
Your node polls its personal range PLUS all joined fleet ranges
on every poll cycle. This is additive — no context is replaced.

    # Join two fleets at once:
    curl -X POST .../api/fleet/join -d '{"name":"Alpha","v":123,"r":100}'
    curl -X POST .../api/fleet/join -d '{"name":"Bravo","v":456,"r":200}'
    # Your node now polls: personal + Alpha + Bravo

## The Fleet Hierarchy

| Level          | Scale          | Purpose                        |
|----------------|----------------|--------------------------------|
| Fleet          | 5–200 nodes    | Daily ops, local comms         |
| Battlegroup    | 3–20 fleets    | Cross-fleet coordination       |
| Armada         | Dozens fleets  | Major campaigns, sector ops    |
| Grand Fleet    | Strategic      | Federation-level anchors       |

Every level is just a coordinate range. Higher levels use
wider polling windows and slower update cycles.

## The Three Types of Coordinates

| Type     | Scope      | Duration   | Purpose                    |
|----------|------------|------------|----------------------------|
| Personal | Your node  | Permanent  | Identity / callsign        |
| Fleet    | Formation  | Temporary  | Local traffic / formation  |
| Beacon   | Public     | Registered | Network landmarks          |

## Joining a Fleet

Via the API (requires token):

    curl -X POST http://<node>:8080/api/fleet/join \\
      -H "X-OdinNet-Token: odinnet-dev" \\
      -H "Content-Type: application/json" \\
      -d '{"name":"Alpha Fleet","v":123456789,"r":100000,"radius":5000}'

The daemon will begin scanning ±radius around the fleet V on every
poll cycle and storing hits in fleet_traffic/.

## Leaving a Fleet

    # Leave one fleet:
    curl -X POST http://<node>:8080/api/fleet/leave \\
      -H "X-OdinNet-Token: odinnet-dev" \\
      -d '{"name":"Alpha Fleet"}'

    # Leave ALL fleets:
    curl -X POST http://<node>:8080/api/fleet/leave \\
      -H "X-OdinNet-Token: odinnet-dev"

## Fleet Status

    curl http://<node>:8080/api/fleet/status \\
      -H "X-OdinNet-Token: odinnet-dev"

Returns all active fleet overlays and your personal node ID.

## Fleet Jump vs Fleet Join

These are different operations:

- **Fleet Join** (this lesson): tune your polling window to a fleet's
  shared coordinate. Your personal identity is unchanged.
  Multiple fleet joins are additive.

- **Fleet Jump** (/api/fleet/jump): relocate the entire network's
  universe reference axis R. Requires DEFCON ≥ 3. This is a
  major security operation that expels bad actors and changes
  the coordinate space for all trusted nodes.

Fleet Join = formation flying.
Fleet Jump = jumping to a new universe to escape cylons.

*Stay in formation. Watch the local traffic. Fly safe.* ⬡
""",
    }

    count = 0
    for filename, content in _EDU_LESSONS.items():
        try:
            fs.write_file(filename, content.encode("utf-8"))
            count += 1
        except Exception as e:
            print(f"  ⚠  Failed to seed '{filename}': {e}")
    print(f"  LatticeFS  : seeded {count} education lesson(s) in edu/")


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
# Published URLs
# ─────────────────────────────────────────────────────────────

def _load_published_urls() -> list:
    if not os.path.exists(PUBLISHED_URLS_PATH):
        return []
    try:
        with open(PUBLISHED_URLS_PATH) as f:
            return json.load(f)
    except Exception:
        return []


def _save_published_urls(urls: list):
    try:
        with open(PUBLISHED_URLS_PATH, "w") as f:
            json.dump(urls, f, indent=2)
    except Exception as e:
        print(f"  ⚠  Published URLs save failed: {e}")


# ─────────────────────────────────────────────────────────────
# BBS Data Layer
# ─────────────────────────────────────────────────────────────

_BBS_DEFAULT = {
    "rooms": {
        "the_thing": {
            "name": "The Thing",
            "description": (
                "Odin's Hall public dispute room. "
                "Air grievances freely — all speech permitted. "
                "Users filter their own feed. Policy: no moderation by host."
            ),
            "policy": "open",
            "posts": [],
        },
        "announcements": {
            "name": "Announcements",
            "description": "Official OdinNet node announcements.",
            "policy": "admin_post",
            "posts": [],
        },
        "coordinates": {
            "name": "Coordinates",
            "description": "Share and discover public coordinate addresses.",
            "policy": "open",
            "posts": [],
        },
    }
}


def _load_bbs() -> dict:
    if os.path.exists(BBS_DATA_PATH):
        try:
            with open(BBS_DATA_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return json.loads(json.dumps(_BBS_DEFAULT))


def _save_bbs(data: dict):
    try:
        with open(BBS_DATA_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"  ⚠  BBS save failed: {e}")


# ─────────────────────────────────────────────────────────────
# Block / Mute list
# ─────────────────────────────────────────────────────────────

def _load_blocked() -> dict:
    if os.path.exists(BLOCKED_LIST_PATH):
        try:
            with open(BLOCKED_LIST_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {"blocked": [], "muted": []}


def _save_blocked(data: dict):
    try:
        with open(BLOCKED_LIST_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"  ⚠  Blocked list save failed: {e}")


# ─────────────────────────────────────────────────────────────
# Anomaly Detector
# ─────────────────────────────────────────────────────────────

def _anomaly_hash(node_id: str, timestamp: str, status_snapshot: dict) -> str:
    payload = json.dumps({
        "node_id":    node_id,
        "timestamp":  timestamp,
        "poll_count": status_snapshot.get("poll_count", 0),
        "defcon":     (status_snapshot.get("security") or {}).get("defcon", 1),
        "beacons":    len(status_snapshot.get("beacons", [])),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _anomaly_detector_loop(ctx: "DaemonContext"):
    time.sleep(60)
    ctx.log("[Anomaly] Detector started — hourly reports active.")

    while True:
        try:
            now_str       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            yesterday_str = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
            snap          = ctx.status_dict()
            ahash         = _anomaly_hash(ctx.comms.my_id, now_str, snap)

            defcon  = (snap.get("security") or {}).get("defcon", 1)
            polls   = snap.get("poll_count", 0)
            beacons = len(snap.get("beacons", []))

            subject = f"[ANOMALY] Node Status — {now_str}"
            body = (
                f"OdinNet Anomaly Detector Report\n"
                f"================================\n"
                f"Node ID    : {ctx.comms.my_id}\n"
                f"Report Time: {now_str}\n"
                f"To Date    : {yesterday_str}  (24h past anchor)\n"
                f"DEFCON     : {defcon}\n"
                f"Poll Count : {polls}\n"
                f"Beacons    : {beacons}\n"
                f"Hash       : {ahash}\n"
                f"\n"
                f"This report was automatically composed by the Anomaly Detector.\n"
                f"Verify hash to confirm node integrity across time coordinates.\n"
            )

            compose_message(
                to_date    = yesterday_str,
                subject    = subject,
                body       = body,
                coord_file = ctx.comms.coord_file,
            )
            ctx.log(f"[Anomaly] Report composed → to_date={yesterday_str} hash={ahash[:16]}...")

        except Exception as e:
            ctx.log(f"[Anomaly] Error: {e}")

        time.sleep(ANOMALY_INTERVAL_SEC)


# ─────────────────────────────────────────────────────────────
# Auto coordinate generation
# ─────────────────────────────────────────────────────────────

def _ensure_coordinate(coord_file: str) -> bool:
    if os.path.exists(coord_file):
        return True

    print(f"\n[Init] Coordinate file '{coord_file}' not found.")
    print(f"[Init] Auto-generating coordinate for this Temporal Node...")

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

        print(f"[Init] ✅ Temporal Node coordinate ready → {coord_file}")
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
        "subject": "OdinNet Temporal Node Online",
        "body": (
            "This Temporal Node has joined the Burris coordinate network.\n"
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
# Daemon context  (v7: multiple fleet overlays)
# ─────────────────────────────────────────────────────────────

class DaemonContext:
    """
    Shared state for poller thread + HTTP handler.
    All writes to shared lists protected by threading.RLock.

    v7 additions (MULTIPLE CONCURRENT FLEETS):
      _fleet_lock       — RLock protecting fleet reads/writes
      join_fleet()      — set overlay (additive; never touches coordinatefile.json)
      leave_fleet()     — clear named fleet overlay
      leave_all_fleets()— clear all fleet overlays
      get_current_fleets()           — return dict of all active fleets
      get_all_local_traffic_ranges() — return list of (name, low, high) for all fleets
      _fleet_log        — received fleet local messages
      add_fleet_received()           — append to fleet log
    """

    def __init__(
        self,
        comms:    "GrokComms",
        fs:       "LatticeFS | None",
        security: "OdinNetSecurity | None" = None,
    ):
        self.comms         = comms
        self.fs            = fs
        self.security      = security
        self.start_time    = datetime.now()
        self._lock         = threading.RLock()
        self._fleet_lock   = threading.RLock()   # v7

        self._last_poll     = None
        self._poll_count    = 0
        self._activity_log  = []
        self._received_log  = []
        self._rt_log        = []
        self._fleet_log     = []                 # v7: local fleet traffic
        self._beacon_status = []

        self._bbs_lock       = threading.RLock()
        self._pub_lock       = threading.RLock()
        self._blocked_lock   = threading.RLock()

    # ── Properties ────────────────────────────────────────────────────────

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

    # ── Fleet overlay (v7 — MULTIPLE) ────────────────────────────────────

    def join_fleet(
        self,
        name:   str,
        v:      int,
        r:      int,
        level:  str = "fleet",
        radius: int = FLEET_LOCAL_RADIUS_DEFAULT,
    ):
        with self._fleet_lock:
            self.comms.set_active_fleet(name, v, r, level=level, radius=radius)
            self.log(
                f"[Fleet] Joined '{name}' [{level}]  "
                f"V={str(v)[:20]}  R={str(r)[:20]}  radius=±{radius}  "
                f"| Total formations: {len(self.comms.get_active_fleets())}"
            )

    def leave_fleet(self, name: str = None):
        with self._fleet_lock:
            fleets_before = self.comms.get_active_fleets()
            self.comms.clear_active_fleet(name)
            if name:
                self.log(f"[Fleet] Left '{name}' — "
                         f"{len(self.comms.get_active_fleets())} formation(s) remaining.")
            else:
                self.log(f"[Fleet] Left all {len(fleets_before)} formation(s) — "
                         f"returned to personal coord.")

    def leave_all_fleets(self):
        self.leave_fleet(name=None)

    def get_current_fleets(self) -> dict:
        return self.comms.get_active_fleets()

    def get_all_local_traffic_ranges(self) -> list[tuple[str, int, int]]:
        return self.comms.get_active_contexts()

    def add_fleet_received(self, records: list, fleet_name: str = ""):
        for r in records:
            r.setdefault("type",        "fleet_local")
            r.setdefault("fleet_name",  fleet_name)
            r.setdefault("recipient",   "")
        with self._lock:
            self._fleet_log = records + self._fleet_log
            if len(self._fleet_log) > MAX_FLEET_LOG:
                self._fleet_log = self._fleet_log[:MAX_FLEET_LOG]

    # ── Logging ───────────────────────────────────────────────────────────

    def log(self, msg: str):
        ts   = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        with self._lock:
            self._activity_log.append(line)
            if len(self._activity_log) > MAX_ACTIVITY_LOG:
                self._activity_log = self._activity_log[-MAX_ACTIVITY_LOG:]

    # ── Message logs ──────────────────────────────────────────────────────

    def add_temporal_received(self, records: list):
        for r in records:
            r.setdefault("msg_type",  "PRIVATE")
            r.setdefault("type",      "temporal")
            r.setdefault("recipient", "")
            r.setdefault("reply_to",  None)
            r.setdefault("reply_id",  None)
        with self._lock:
            self._received_log = records + self._received_log
            if len(self._received_log) > MAX_RECEIVED_LOG:
                self._received_log = self._received_log[:MAX_RECEIVED_LOG]

    def add_rt_received(self, records: list):
        for r in records:
            r.setdefault("msg_type",  "PRIVATE")
            r.setdefault("type",      "realtime")
            r.setdefault("recipient", "")
            r.setdefault("reply_to",  None)
            r.setdefault("reply_id",  None)
        with self._lock:
            self._rt_log = records + self._rt_log
            if len(self._rt_log) > MAX_RT_LOG:
                self._rt_log = self._rt_log[:MAX_RT_LOG]

    def get_messages_for_recipient(self, recipient: str, log: str = "temporal") -> list:
        with self._lock:
            src = self._received_log if log == "temporal" else self._rt_log
            return [
                m for m in src
                if not m.get("recipient") or m.get("recipient") == recipient
            ]

    # ── Beacons ───────────────────────────────────────────────────────────

    def refresh_beacons(self):
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

    def _tick_poll(self):
        with self._lock:
            self._poll_count += 1
            self._last_poll   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── Status dict (v7: active_fleets list) ──────────────────────────────

    def status_dict(self) -> dict:
        with self._lock:
            activity_tail  = list(self._activity_log[-80:])
            received_log   = list(self._received_log[:10])
            rt_log         = list(self._rt_log[:10])
            fleet_log      = list(self._fleet_log[:10])
            beacon_status  = list(self._beacon_status)
            poll_count     = self._poll_count
            last_poll      = self._last_poll
            received_count = len(self._received_log)

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

        pub_count     = len(_load_published_urls())
        active_fleets = self.get_current_fleets()

        result = {
            "node_id":         self.comms.my_id,
            "coord_file":      self.comms.coord_file,
            "uptime_sec":      uptime_sec,
            "poll_count":      poll_count,
            "last_poll":       last_poll,
            "poll_range_set":  poll_range_set,
            "lattice_fs":      fs is not None,
            "lattice_files":   lfs_files,
            "received_count":  received_count,
            "recent_received": received_log,
            "rt_count":        len(self._rt_log),
            "recent_realtime": rt_log,
            "published_urls":  pub_count,
            "active_fleets":      active_fleets,
            "fleet_count":        len(active_fleets),
            "fleet_msg_count":    len(self._fleet_log),
            "recent_fleet":       fleet_log,
            "current_fleet":      next(iter(active_fleets.values()), None) if active_fleets else None,
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

        if self.security:
            try:
                result["security"] = self.security.status_dict()
            except Exception as e:
                result["security"] = {"error": str(e)}
        else:
            result["security"] = None

        return result


# ─────────────────────────────────────────────────────────────
# Background poller thread  (v7: polls ALL active fleet ranges)
# ─────────────────────────────────────────────────────────────

def _background_poller(ctx: DaemonContext):
    ctx.log("Temporal Node background poller started. (v7.1 — Multi-Fleet Networks)")
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

            all_ranges = ctx.get_all_local_traffic_ranges()
            if all_ranges:
                for (fleet_name, low, high) in all_ranges:
                    try:
                        local_results = ctx.comms.poll_range(low, high)
                        if local_results:
                            ctx.log(
                                f"[Fleet] '{fleet_name}': "
                                f"{len(local_results)} local message(s)"
                            )
                            ctx.add_fleet_received(local_results, fleet_name=fleet_name)
                        else:
                            ctx.log(f"[Fleet] '{fleet_name}': local scan clear")
                    except Exception as e:
                        ctx.log(f"[Fleet] '{fleet_name}' scan error: {e}")

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
# OdinWeb HTTP handler  (v7.1 — heavy debug + stable fallbacks)
# ─────────────────────────────────────────────────────────────

class OdinWebHandler(http.server.BaseHTTPRequestHandler):
    """
    OdinWeb HTTP handler — v7.1.

    v7.1 changes:
      - do_GET has debug print at ENTRY and around every major branch
      - _respond has debug print at entry and catches write errors
      - Minimal JSON path: /status returns immediately without LatticeFS
      - Any exception inside do_GET is caught; returns 500 JSON rather than crashing
      - LatticeFS failures in _serve_root fall back to status HTML (not crash)

    GET  /                              → dashboard from LatticeFS (fallback: status HTML)
    GET  /status                        → JSON status (includes active_fleets) [NO LatticeFS needed]
    GET  /<path>                        → serve file from LatticeFS

    POST /api/compose                   → compose temporal message    [token]
    POST /api/send                      → trigger send_outbox()       [token]

    POST /api/security/raise            → raise DEFCON                [token]
    POST /api/security/lower            → lower DEFCON                [token]
    POST /api/security/attack           → declare attack              [token]
    POST /api/security/cleared          → clear attack                [token]
    POST /api/security/ban              → ban an identifier           [token]
    GET  /api/security/status           → security status JSON        [token]

    POST /api/fleet/jump                → Fleet Jump (universe R)     [token]
    GET  /api/fleet/manifest            → jump manifest JSON          [token]
    GET  /api/fleet/status              → all active fleet overlays   [token]  v7
    GET  /api/fleet/list                → all known fleets            [token]  v7
    POST /api/fleet/join                → join a fleet (additive)     [token]  v7
    POST /api/fleet/leave               → leave fleet(s)              [token]  v7

    POST /api/publish                   → register burris:// URL      [token]
    GET  /api/publish/list              → list published URLs

    GET  /api/bbs/rooms                 → list BBS rooms
    GET  /api/bbs/room/<name>           → get posts in room
    POST /api/bbs/post                  → post to BBS room            [token]

    POST /api/privacy/block             → block a coordinate/alias    [token]
    POST /api/privacy/mute              → mute a coordinate/alias     [token]
    GET  /api/privacy/list              → list blocked/muted          [token]
    POST /api/privacy/unblock           → unblock                     [token]
    """

    daemon_ctx = None

    def log_message(self, fmt, *args):
        ctx = self.daemon_ctx
        if ctx:
            ctx.log(f"HTTP {self.command} {self.path}  [{args[1] if len(args)>1 else '?'}]")

    # ── GET routing (v7.1: debug prints + top-level try/except) ──────────

    def do_GET(self):
        # ── v7.1 DEBUG ENTRY ──────────────────────────────────────────────
        print(f"[do_GET] ENTER  path={self.path!r}  "
              f"client={self.client_address}  "
              f"ctx={'ok' if self.daemon_ctx else 'NONE'}",
              flush=True)

        try:
            path = self.path.split("?")[0]
            print(f"[do_GET] routing → {path!r}", flush=True)

            # ── Minimal fast path: /status — NO LatticeFS dependency ──────
            if path in ("/status", "/api/status"):
                print("[do_GET] → _serve_status_json (fast path, no LatticeFS)", flush=True)
                self._serve_status_json()

            elif path in ("/", ""):
                print("[do_GET] → _serve_root", flush=True)
                self._serve_root()

            elif path == "/api/security/status":
                print("[do_GET] → _serve_security_status", flush=True)
                if _check_token(self):
                    self._serve_security_status()

            elif path == "/api/fleet/manifest":
                print("[do_GET] → _serve_fleet_manifest", flush=True)
                if _check_token(self):
                    self._serve_fleet_manifest()

            elif path == "/api/fleet/status":
                print("[do_GET] → _serve_fleet_status", flush=True)
                if _check_token(self):
                    self._serve_fleet_status()

            elif path == "/api/fleet/list":
                print("[do_GET] → _serve_fleet_list", flush=True)
                if _check_token(self):
                    self._serve_fleet_list()

            elif path == "/api/publish/list":
                print("[do_GET] → _serve_published_list", flush=True)
                self._serve_published_list()

            elif path == "/api/bbs/rooms":
                print("[do_GET] → _serve_bbs_rooms", flush=True)
                self._serve_bbs_rooms()

            elif path.startswith("/api/bbs/room/"):
                room = path[len("/api/bbs/room/"):]
                print(f"[do_GET] → _serve_bbs_room({room!r})", flush=True)
                self._serve_bbs_room(room)

            elif path == "/api/privacy/list":
                print("[do_GET] → _serve_privacy_list", flush=True)
                if _check_token(self):
                    self._serve_privacy_list()

            else:
                filename = path.lstrip("/")
                print(f"[do_GET] → _serve_lattice({filename!r})", flush=True)
                self._serve_lattice(filename)

            print(f"[do_GET] EXIT OK  path={path!r}", flush=True)

        except Exception as e:
            err_msg = f"[do_GET] UNHANDLED EXCEPTION path={self.path!r}: {e}"
            print(err_msg, flush=True)
            _log_web_error(err_msg, e)
            try:
                body = json.dumps({
                    "ok":    False,
                    "error": f"Internal server error: {e}",
                    "path":  self.path,
                }).encode()
                self._respond(500, "application/json", body)
            except Exception as e2:
                print(f"[do_GET] Could not send 500 response: {e2}", flush=True)

    def _serve_root(self):
        print("[_serve_root] ENTER", flush=True)
        ctx = self.daemon_ctx
        fs  = ctx.fs if ctx else None

        if fs:
            print(f"[_serve_root] LatticeFS available, checking index.html...", flush=True)
            try:
                if fs.exists("index.html"):
                    print("[_serve_root] index.html found — reading...", flush=True)
                    data = fs.read_file("index.html")
                    print(f"[_serve_root] index.html read OK ({len(data)} bytes) — sending", flush=True)
                    self._respond(200, "text/html; charset=utf-8", data)
                    return
                else:
                    print("[_serve_root] index.html NOT in LatticeFS — falling back", flush=True)
            except Exception as e:
                err = f"[_serve_root] LatticeFS read error: {e}"
                print(err, flush=True)
                _log_web_error(err, e)
        else:
            print("[_serve_root] LatticeFS not available — using fallback", flush=True)

        print("[_serve_root] → _serve_status_html_fallback", flush=True)
        self._serve_status_html_fallback()

    def _serve_status_html_fallback(self):
        print("[_serve_status_html_fallback] ENTER", flush=True)
        ctx    = self.daemon_ctx
        d      = ctx.status_dict()
        uptime = str(datetime.now() - ctx.start_time).split(".")[0]
        sec    = d.get("security") or {}
        defcon_line = (
            f"DEFCON {sec.get('defcon', '?')} [{sec.get('defcon_label', '?')}]"
            if sec else "Security: unavailable"
        )
        jump_line = ""
        if sec.get("jump_count"):
            jump_line = (
                f"<p>Fleet Jumps: {sec['jump_count']} "
                f"| Current R: {sec.get('current_r', 1)}</p>"
            )

        active_fleets = d.get("active_fleets", {})
        if active_fleets:
            fleet_lines = "".join(
                f"<p>⬡ Fleet: <b>{f['name']}</b>  [{f.get('level','fleet')}]  "
                f"V={str(f['v'])[:16]}...  radius=±{f.get('radius','?')}</p>"
                for f in active_fleets.values()
            )
        else:
            fleet_lines = "<p>Fleet: not joined</p>"

        body = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="8">
  <title>OdinNet — Temporal Node — {d['node_id']}</title>
  <style>
    body {{ font-family: monospace; background:#07090d; color:#00e87a;
            max-width:700px; margin:40px auto; padding:0 20px; }}
    a {{ color:#00ccff; }}
    pre {{ background:#0d1117; padding:12px; border-radius:4px; overflow-x:auto; }}
  </style>
</head>
<body>
  <h1>⬡ OdinNet — Temporal Node  <small style="font-size:0.6em">v7.1</small></h1>
  <p>Node: {d['node_id']} | Uptime: {uptime} | Polls: {d['poll_count']}</p>
  <p>LatticeFS: {'✅ ' + str(len(d['lattice_files'])) + ' files' if d['lattice_fs'] else '⚠ not mounted'}</p>
  <p>{defcon_line}</p>
  {jump_line}
  {fleet_lines}
  <p>
    <a href="/status">JSON status</a> |
    <a href="/readme.txt">readme.txt</a> |
    <a href="/api/bbs/rooms">BBS rooms</a> |
    <a href="/api/publish/list">Published URLs</a> |
    <a href="/edu/06_fleets.md">Fleet Guide</a>
  </p>
  <pre>{chr(10).join(d['activity_tail'][-20:])}</pre>
</body>
</html>""".encode("utf-8")
        print(f"[_serve_status_html_fallback] sending {len(body)} byte fallback page", flush=True)
        self._respond(200, "text/html; charset=utf-8", body)

    def _serve_status_json(self):
        # v7.1: this is the MINIMAL fast path — LatticeFS not touched at all
        print("[_serve_status_json] ENTER — building status dict", flush=True)
        ctx = self.daemon_ctx
        try:
            data = ctx.status_dict()
            payload = json.dumps(data, indent=2).encode("utf-8")
            print(f"[_serve_status_json] status dict OK ({len(payload)} bytes) — sending", flush=True)
            self._respond(200, "application/json", payload)
        except Exception as e:
            err = f"[_serve_status_json] ERROR: {e}"
            print(err, flush=True)
            _log_web_error(err, e)
            fallback = json.dumps({"ok": False, "error": str(e), "node": "unknown"}).encode()
            self._respond(500, "application/json", fallback)

    def _serve_security_status(self):
        print("[_serve_security_status] ENTER", flush=True)
        ctx = self.daemon_ctx
        if not ctx.security:
            self._respond(503, "application/json",
                          b'{"ok":false,"error":"Security module not available"}')
            return
        self._respond(200, "application/json",
                      json.dumps(ctx.security.status_dict(), indent=2).encode())

    def _serve_fleet_manifest(self):
        print("[_serve_fleet_manifest] ENTER", flush=True)
        ctx = self.daemon_ctx
        if not ctx.security:
            self._respond(503, "application/json",
                          b'{"ok":false,"error":"Security module not available"}')
            return
        manifest = ctx.security.manifest._data
        self._respond(200, "application/json",
                      json.dumps(manifest, indent=2).encode())

    # ── Fleet GET endpoints (v7) ──────────────────────────────────────────

    def _serve_fleet_status(self):
        print("[_serve_fleet_status] ENTER", flush=True)
        ctx           = self.daemon_ctx
        active_fleets = ctx.get_current_fleets()
        contexts      = ctx.get_all_local_traffic_ranges()
        body = {
            "ok":             True,
            "personal_coord": ctx.comms.my_id,
            "active_fleets":  active_fleets,
            "fleet_count":    len(active_fleets),
            "in_fleet":       len(active_fleets) > 0,
            "fleet_msg_count": len(ctx._fleet_log),
            "active_contexts": [
                {"name": name, "low": str(low), "high": str(high)}
                for (name, low, high) in contexts
            ],
        }
        self._respond(200, "application/json", json.dumps(body, indent=2).encode())

    def _serve_fleet_list(self):
        print("[_serve_fleet_list] ENTER", flush=True)
        try:
            registry = FleetRegistry()
            fleets   = registry.list_fleets()
        except Exception as e:
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())
            return
        self._respond(200, "application/json",
                      json.dumps({"ok": True, "fleets": fleets,
                                  "count": len(fleets)}, indent=2).encode())

    def _serve_lattice(self, filename: str):
        print(f"[_serve_lattice] ENTER  filename={filename!r}", flush=True)
        ctx = self.daemon_ctx
        fs  = ctx.fs if ctx else None

        if fs is None:
            print(f"[_serve_lattice] LatticeFS not available — 503", flush=True)
            self._respond(503, "text/plain; charset=utf-8",
                          b"LatticeFS is not available on this node.\n")
            return

        print(f"[_serve_lattice] checking exists({filename!r})...", flush=True)
        if not fs.exists(filename):
            print(f"[_serve_lattice] NOT FOUND in LatticeFS", flush=True)
            self._respond(404, "text/plain; charset=utf-8",
                          f"404: '{filename}' not found in LatticeFS.\n".encode())
            return

        try:
            print(f"[_serve_lattice] reading file...", flush=True)
            data  = fs.read_file(filename)
            ctype = _mime_for(filename)
            print(f"[_serve_lattice] read OK ({len(data)} bytes)  mime={ctype!r} — sending", flush=True)
            self._respond(200, ctype, data)
            _save_lattice(fs)
        except Exception as e:
            err = f"[_serve_lattice] ERROR reading '{filename}': {e}"
            print(err, flush=True)
            _log_web_error(err, e)
            ctx.log(f"LatticeFS ERROR reading '{filename}': {e}")
            self._respond(500, "text/plain; charset=utf-8",
                          f"LatticeFS read error: {e}\n".encode())

    # ── BBS GET endpoints ─────────────────────────────────────────────────

    def _serve_bbs_rooms(self):
        print("[_serve_bbs_rooms] ENTER", flush=True)
        bbs = _load_bbs()
        rooms_out = []
        for key, rm in bbs.get("rooms", {}).items():
            rooms_out.append({
                "key":         key,
                "name":        rm.get("name", key),
                "description": rm.get("description", ""),
                "policy":      rm.get("policy", "open"),
                "post_count":  len(rm.get("posts", [])),
            })
        self._respond(200, "application/json",
                      json.dumps({"ok": True, "rooms": rooms_out}, indent=2).encode())

    def _serve_bbs_room(self, room_key: str):
        print(f"[_serve_bbs_room] ENTER  room={room_key!r}", flush=True)
        bbs  = _load_bbs()
        room = bbs.get("rooms", {}).get(room_key)
        if not room:
            self._respond(404, "application/json",
                          json.dumps({"ok": False,
                                      "error": f"Room '{room_key}' not found"}).encode())
            return
        posts = list(reversed(room.get("posts", [])[-50:]))
        self._respond(200, "application/json",
                      json.dumps({
                          "ok":    True,
                          "room":  room_key,
                          "name":  room.get("name", room_key),
                          "posts": posts,
                      }, indent=2).encode())

    def _serve_published_list(self):
        print("[_serve_published_list] ENTER", flush=True)
        urls = _load_published_urls()
        self._respond(200, "application/json",
                      json.dumps({"ok": True, "published": urls}, indent=2).encode())

    def _serve_privacy_list(self):
        print("[_serve_privacy_list] ENTER", flush=True)
        data = _load_blocked()
        self._respond(200, "application/json",
                      json.dumps({"ok": True, "blocked": data}, indent=2).encode())

    # ── POST routing ──────────────────────────────────────────────────────

    def do_POST(self):
        print(f"[do_POST] ENTER  path={self.path!r}  client={self.client_address}", flush=True)
        try:
            path = self.path.split("?")[0]
            if not _check_token(self):
                print(f"[do_POST] AUTH FAILED for {path!r}", flush=True)
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
            elif path == "/api/fleet/jump":
                self._handle_fleet_jump()
            elif path == "/api/fleet/join":
                self._handle_fleet_join()
            elif path == "/api/fleet/leave":
                self._handle_fleet_leave()
            elif path == "/api/publish":
                self._handle_publish()
            elif path == "/api/bbs/post":
                self._handle_bbs_post()
            elif path == "/api/privacy/block":
                self._handle_privacy_block()
            elif path == "/api/privacy/mute":
                self._handle_privacy_mute()
            elif path == "/api/privacy/unblock":
                self._handle_privacy_unblock()
            else:
                self._respond(404, "application/json",
                              b'{"ok":false,"error":"Unknown API endpoint"}')
            print(f"[do_POST] EXIT OK  path={path!r}", flush=True)
        except Exception as e:
            err = f"[do_POST] UNHANDLED EXCEPTION path={self.path!r}: {e}"
            print(err, flush=True)
            _log_web_error(err, e)
            try:
                self._respond(500, "application/json",
                              json.dumps({"ok": False, "error": str(e)}).encode())
            except Exception:
                pass

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

        to_date   = body.get("to_date", "")
        subject   = body.get("subject", "")
        text      = body.get("body", "")
        msg_type  = body.get("msg_type", "PRIVATE")
        recipient = body.get("recipient", "")
        reply_to  = body.get("reply_to", None)
        reply_id  = body.get("reply_id", None)

        if not to_date or not subject:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"to_date and subject are required"}')
            return

        try:
            fname = compose_message(to_date, subject, text, ctx.comms.coord_file)
            ctx.log(f"API /compose: '{subject}' to={to_date} type={msg_type}"
                    + (f" reply_to={reply_to}" if reply_to else ""))
            try:
                with open(fname) as fh:
                    draft = json.load(fh)
                draft["msg_type"]  = msg_type
                draft["recipient"] = recipient
                if reply_to:
                    draft["reply_to"] = reply_to
                if reply_id:
                    draft["reply_id"] = reply_id
                with open(fname, "w") as fh:
                    json.dump(draft, fh, indent=2)
            except Exception:
                pass
            result = json.dumps({
                "ok":       True,
                "file":     fname,
                "msg_type": msg_type,
                "reply_to": reply_to,
                "reply_id": reply_id,
            })
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
            outbox_before = (
                len([f for f in os.listdir("outbox") if f.endswith(".json")])
                if os.path.exists("outbox") else 0
            )
            send_outbox(cf)
            outbox_after = (
                len([f for f in os.listdir("outbox") if f.endswith(".json")])
                if os.path.exists("outbox") else 0
            )
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
        if self.daemon_ctx.security:
            return True
        self._respond(503, "application/json",
                      b'{"ok":false,"error":"Security module not available"}')
        return False

    def _handle_security_raise(self):
        if not self._security_required():
            return
        ctx    = self.daemon_ctx
        body   = self._read_json_body() or {}
        lvl    = body.get("level", ctx.security.defcon + 1)
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
        ctx    = self.daemon_ctx
        body   = self._read_json_body() or {}
        lvl    = body.get("level")
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

    # ── /api/fleet/jump (retained from v6) ───────────────────────────────

    def _handle_fleet_jump(self):
        if not self._security_required():
            return

        ctx  = self.daemon_ctx
        body = self._read_json_body()
        if not body:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"Invalid JSON body"}')
            return

        raw_r  = body.get("new_r")
        reason = body.get("reason", "api_fleet_jump")
        force  = bool(body.get("force", False))

        if raw_r is None:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"new_r (integer) required"}')
            return

        try:
            new_r = int(raw_r)
        except (TypeError, ValueError):
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"new_r must be an integer"}')
            return

        if new_r < 1:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"new_r must be >= 1"}')
            return

        try:
            beacons = _load_beacons()
            result  = ctx.security.fleet_jump(
                new_r       = new_r,
                reason      = reason,
                beacon_list = beacons,
                force       = force,
            )
            ctx.log(
                f"API /fleet/jump: R → {new_r}  expelled={result['expelled_count']}  "
                f"trusted={result['trusted_count']}  DEFCON={result['defcon']}"
            )
            ctx.refresh_beacons()
            self._respond(200, "application/json",
                          json.dumps(result, indent=2).encode())
        except ValueError as e:
            ctx.log(f"API /fleet/jump blocked: {e}")
            self._respond(409, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())
        except Exception as e:
            ctx.log(f"API /fleet/jump error: {e}")
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())

    # ── /api/fleet/join (v7 — ADDITIVE multi-fleet) ───────────────────────

    def _handle_fleet_join(self):
        ctx  = self.daemon_ctx
        body = self._read_json_body()
        if not body:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"Invalid JSON body"}')
            return

        name = body.get("name", "").strip()
        if not name:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"fleet name required"}')
            return

        raw_v  = body.get("v")
        raw_r  = body.get("r")
        level  = body.get("level", "fleet")
        radius = int(body.get("radius", FLEET_LOCAL_RADIUS_DEFAULT))

        if raw_v is None or raw_r is None:
            try:
                registry = FleetRegistry()
                record   = registry.get(name)
                if record is None:
                    self._respond(404, "application/json",
                                  json.dumps({
                                      "ok": False,
                                      "error": (
                                          f"Fleet '{name}' not in registry and "
                                          f"v/r not provided."
                                      )
                                  }).encode())
                    return
                raw_v  = int(record["v"])
                raw_r  = int(record["r"])
                radius = int(record.get("radius", FLEET_LOCAL_RADIUS_DEFAULT))
                level  = record.get("level", "fleet")
            except Exception as e:
                self._respond(500, "application/json",
                              json.dumps({"ok": False, "error": str(e)}).encode())
                return

        try:
            v = int(raw_v)
            r = int(raw_r)
        except (TypeError, ValueError):
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"v and r must be integers"}')
            return

        try:
            ctx.join_fleet(name, v, r, level=level, radius=radius)
            active_fleets = ctx.get_current_fleets()
            ctx.log(f"API /fleet/join: '{name}' [{level}]  "
                    f"V={str(v)[:20]}  R={str(r)[:20]}  radius={radius}  "
                    f"| Total active: {len(active_fleets)}")
            self._respond(200, "application/json",
                          json.dumps({
                              "ok":             True,
                              "joined":         name,
                              "active_fleets":  active_fleets,
                              "fleet_count":    len(active_fleets),
                              "personal_coord": ctx.comms.my_id,
                              "message": (
                                  f"Joined fleet '{name}' [{level}]. "
                                  f"Local scan active (±{radius} around V). "
                                  f"Personal coord unchanged. "
                                  f"Now in {len(active_fleets)} formation(s)."
                              ),
                          }, indent=2).encode())
        except Exception as e:
            ctx.log(f"API /fleet/join error: {e}")
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())

    # ── /api/fleet/leave (v7 — named or all) ─────────────────────────────

    def _handle_fleet_leave(self):
        ctx  = self.daemon_ctx
        body = self._read_json_body() or {}
        name = body.get("name", "").strip() or None

        active_before = ctx.get_current_fleets()

        if name:
            if name not in active_before:
                self._respond(200, "application/json",
                              json.dumps({
                                  "ok":     True,
                                  "message": f"Not currently in fleet '{name}'.",
                              }).encode())
                return
            ctx.leave_fleet(name)
            ctx.log(f"API /fleet/leave: left '{name}'")
            active_after = ctx.get_current_fleets()
            self._respond(200, "application/json",
                          json.dumps({
                              "ok":             True,
                              "left_fleet":     name,
                              "active_fleets":  active_after,
                              "fleet_count":    len(active_after),
                              "personal_coord": ctx.comms.my_id,
                              "message": (
                                  f"Left fleet '{name}'. "
                                  f"{len(active_after)} formation(s) remaining."
                              ),
                          }, indent=2).encode())
        else:
            count = len(active_before)
            ctx.leave_all_fleets()
            ctx.log(f"API /fleet/leave: left all {count} fleet(s)")
            self._respond(200, "application/json",
                          json.dumps({
                              "ok":             True,
                              "left_count":     count,
                              "left_fleets":    list(active_before.keys()),
                              "active_fleets":  {},
                              "fleet_count":    0,
                              "personal_coord": ctx.comms.my_id,
                              "message":        f"Left all {count} formation(s). Returned to personal coord.",
                          }, indent=2).encode())

    # ── /api/publish ──────────────────────────────────────────────────────

    def _handle_publish(self):
        ctx  = self.daemon_ctx
        body = self._read_json_body()
        if not body:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"Invalid JSON body"}')
            return

        url        = body.get("url", "").strip()
        title      = body.get("title", "Untitled")
        desc       = body.get("description", "")
        visibility = body.get("visibility", "public")
        coordinate = body.get("coordinate", "")

        if not url:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"url is required"}')
            return

        if not url.startswith("burris://") and not url.startswith("http"):
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"url must start with burris:// or http"}')
            return

        entry = {
            "url":         url,
            "title":       title,
            "description": desc,
            "visibility":  visibility,
            "coordinate":  coordinate,
            "published":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "node_id":     ctx.comms.my_id,
        }

        with ctx._pub_lock:
            urls = _load_published_urls()
            urls = [u for u in urls if u.get("url") != url]
            urls.append(entry)
            _save_published_urls(urls)

        if ctx.fs and hasattr(ctx.fs, "_url_index"):
            try:
                ctx.fs._url_index[url] = entry
                _save_lattice(ctx.fs)
            except Exception as e:
                ctx.log(f"API /publish: LatticeFS index warn: {e}")

        ctx.log(f"API /publish: '{title}' [{visibility}] → {url}")
        self._respond(200, "application/json",
                      json.dumps({"ok": True, "entry": entry}, indent=2).encode())

    # ── /api/bbs/post ─────────────────────────────────────────────────────

    def _handle_bbs_post(self):
        ctx  = self.daemon_ctx
        body = self._read_json_body()
        if not body:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"Invalid JSON body"}')
            return

        room_key = body.get("room", "the_thing")
        author   = body.get("author", "Anonymous")
        subject  = body.get("subject", "")
        text     = body.get("body", "")

        if not subject or not text:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"subject and body are required"}')
            return

        with ctx._bbs_lock:
            bbs  = _load_bbs()
            room = bbs.get("rooms", {}).get(room_key)
            if not room:
                self._respond(404, "application/json",
                              json.dumps({"ok": False,
                                          "error": f"Room '{room_key}' not found"}).encode())
                return

            post_id = f"{room_key}_{int(time.time()*1000)}"
            post = {
                "id":        post_id,
                "author":    author,
                "subject":   subject,
                "body":      text,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "node_id":   ctx.comms.my_id,
            }
            room["posts"].append(post)
            if len(room["posts"]) > 500:
                room["posts"] = room["posts"][-500:]
            _save_bbs(bbs)

        ctx.log(f"API /bbs/post: [{room_key}] '{subject}' by {author}")
        self._respond(200, "application/json",
                      json.dumps({"ok": True, "post_id": post_id}, indent=2).encode())

    # ── /api/privacy/* ────────────────────────────────────────────────────

    def _handle_privacy_block(self):
        ctx  = self.daemon_ctx
        body = self._read_json_body()
        if not body:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"Invalid JSON body"}')
            return
        target = body.get("target", "").strip()
        if not target:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"target required"}')
            return
        with ctx._blocked_lock:
            data = _load_blocked()
            if target not in data["blocked"]:
                data["blocked"].append(target)
            data["muted"] = [m for m in data["muted"] if m != target]
            _save_blocked(data)
        ctx.log(f"API /privacy/block: blocked '{target}'")
        self._respond(200, "application/json",
                      json.dumps({"ok": True, "blocked": target}).encode())

    def _handle_privacy_mute(self):
        ctx  = self.daemon_ctx
        body = self._read_json_body()
        if not body:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"Invalid JSON body"}')
            return
        target = body.get("target", "").strip()
        if not target:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"target required"}')
            return
        with ctx._blocked_lock:
            data = _load_blocked()
            if target not in data["muted"] and target not in data["blocked"]:
                data["muted"].append(target)
            _save_blocked(data)
        ctx.log(f"API /privacy/mute: muted '{target}'")
        self._respond(200, "application/json",
                      json.dumps({"ok": True, "muted": target}).encode())

    def _handle_privacy_unblock(self):
        ctx  = self.daemon_ctx
        body = self._read_json_body()
        if not body:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"Invalid JSON body"}')
            return
        target = body.get("target", "").strip()
        with ctx._blocked_lock:
            data = _load_blocked()
            data["blocked"] = [b for b in data["blocked"] if b != target]
            data["muted"]   = [m for m in data["muted"]   if m != target]
            _save_blocked(data)
        ctx.log(f"API /privacy/unblock: unblocked/unmuted '{target}'")
        self._respond(200, "application/json",
                      json.dumps({"ok": True, "unblocked": target}).encode())

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

    # ── Response helper (v7.1: debug prints + write error handling) ───────

    def _respond(self, code: int, ctype: str, body: bytes):
        # ── v7.1 DEBUG ────────────────────────────────────────────────────
        print(f"[_respond] code={code}  ctype={ctype!r}  body_len={len(body)}", flush=True)
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            print(f"[_respond] write OK", flush=True)
        except BrokenPipeError:
            print(f"[_respond] BrokenPipe — client disconnected before response", flush=True)
        except ConnectionResetError:
            print(f"[_respond] ConnectionReset — client dropped connection", flush=True)
        except Exception as e:
            err = f"[_respond] WRITE ERROR code={code}: {e}"
            print(err, flush=True)
            _log_web_error(err, e)


# ─────────────────────────────────────────────────────────────
# OdinWeb server thread  (v7.1: full try/except → web_error.log)
# ─────────────────────────────────────────────────────────────

def _run_web_server(ctx: DaemonContext, host: str, port: int):
    """
    v7.1: Entire function is wrapped in try/except.
    Any exception at any level is caught, logged to web_error.log,
    and the daemon continues in polling-only mode rather than dying.
    """

    def handler_factory(*args, **kwargs):
        h = OdinWebHandler(*args, **kwargs)
        h.daemon_ctx = ctx
        return h

    socketserver.TCPServer.allow_reuse_address = True

    def _try_bind_and_serve(port: int) -> bool:
        """Try to bind and serve. Returns False if port is unavailable."""
        try:
            with socketserver.TCPServer((host, port), handler_factory) as httpd:
                ctx.log(f"OdinWeb listening at http://{host}:{port}/")
                print(f"[WebServer] TCPServer bound OK on {host}:{port}", flush=True)
                _write_pid_file(port)
                httpd.serve_forever()
            return True
        except OSError as e:
            if e.errno == 98:   # Address already in use
                return False
            raise

    try:
        print(f"[WebServer] Starting on {host}:{port} ...", flush=True)
        bound = _try_bind_and_serve(port)

        if not bound:
            ctx.log(f"⚠  Port {port} in use — attempting to free it...")
            print(f"[WebServer] Port {port} in use — running _free_port()...", flush=True)
            freed = _free_port(port)
            if freed:
                ctx.log(f"Port {port} freed — restarting web server...")
                print(f"[WebServer] Port freed — retrying bind...", flush=True)
                time.sleep(1)
                _try_bind_and_serve(port)
            else:
                msg = (
                    f"❌  Could not free port {port}. "
                    f"Try: python odinnet_daemon.py --port 9090\n"
                    f"   Continuing in polling-only mode (web server unavailable)."
                )
                ctx.log(msg)
                _log_web_error(msg)
                print(f"[WebServer] Falling back to polling-only (port locked).", flush=True)
                # Stay alive — poller thread continues independently
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    pass

    except KeyboardInterrupt:
        print("\n[WebServer] KeyboardInterrupt — shutting down.", flush=True)

    except Exception as e:
        err = f"[WebServer] FATAL EXCEPTION on {host}:{port}: {e}"
        print(err, flush=True)
        _log_web_error(err, e)
        ctx.log(f"❌  Web server crashed: {e}  (see web_error.log)  "
                f"Daemon continues in polling-only mode.")
        # Daemon stays alive — poller thread is independent
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


# ─────────────────────────────────────────────────────────────
# Startup banner  (v7.1)
# ─────────────────────────────────────────────────────────────

def _print_startup_banner(
    comms, fs, host: str, port: int, coord_file: str,
    security: "OdinNetSecurity | None" = None,
    no_web: bool = False,
):
    now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    border = "★" * 64

    lan_ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    print(f"\n{border}")
    print(f"  ODINNET DAEMON  v7.1  —  Multi-Fleet Networks  —  {now}")
    print(f"  ⬡  Burris Numerical System — Temporal Node")
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

    if security:
        cfg = security.defcon_config
        print(f"  Security    : ✅  DEFCON {security.defcon} [{cfg['label']}] {cfg['color']}")
        print(f"  Fleet R     : {security.manifest.current_r}  "
              f"(jumps={security.manifest.jump_count})")
        print(f"  API Token   : "
              f"{'default (change ODINNET_TOKEN)' if API_TOKEN == 'odinnet-dev' else 'custom ✅'}")
    else:
        print(f"  Security    : ⚠  module not available")
        print(f"  API Token   : {'default' if API_TOKEN == 'odinnet-dev' else 'custom ✅'}")

    dash_path = os.path.exists(DASHBOARD_HTML)
    print(f"  Dashboard   : {'✅ found' if dash_path else '⚠ not on disk'}"
          f"  ({DASHBOARD_HTML})")

    if no_web:
        print(f"  OdinWeb     : ⛔  DISABLED (--no-web / safe mode)")
        print(f"  Mode        : polling-only — poller + anomaly threads running")
    else:
        print(f"  OdinWeb     : http://0.0.0.0:{port}/  (all interfaces)")
        print(f"  📱 Phone URL : http://{lan_ip}:{port}/   ← USE THIS IN BROWSER")
        print(f"  Error log   : {WEB_ERROR_LOG}")

    print(f"  Poll cycle  : every {POLL_INTERVAL_SEC}s")
    print(f"  Anomaly Det : every {ANOMALY_INTERVAL_SEC//60}min")

    if not no_web:
        print(f"")
        print(f"  ── v7.1 WEB STABILITY ───────────────────────────────────────")
        print(f"  * Debug prints active on do_GET, _respond, _serve_*")
        print(f"  * /status returns JSON immediately (no LatticeFS needed)")
        print(f"  * Web crashes → web_error.log, daemon keeps polling")
        print(f"  * Port kill: fuser + lsof -ti:{port} | xargs kill -9")
        print(f"  * --no-web flag: polling-only safe mode")
        print(f"")
        print(f"  ── v7 FLEET API ─────────────────────────────────────────────")
        print(f"  POST /api/fleet/join    — join a fleet (additive, multi-fleet)")
        print(f"  POST /api/fleet/leave   — leave named fleet or all fleets")
        print(f"  GET  /api/fleet/status  — all active overlays + poll contexts")
        print(f"  GET  /api/fleet/list    — fleet registry")
        print(f"  ── v6 APIs ──────────────────────────────────────────────────")
        print(f"  /api/fleet/jump  /api/publish  /api/bbs/*  /api/privacy/*")

    print(f"{border}\n")

    if not no_web:
        print(f"  ── QUICK TEST ───────────────────────────────────────────────")
        print(f"  # Verify web server (should get JSON):")
        print(f"  curl http://{lan_ip}:{port}/status")
        print(f"")
        print(f"  # Join Alpha Fleet:")
        print(f"  curl -X POST http://{lan_ip}:{port}/api/fleet/join \\")
        print(f'       -H "X-OdinNet-Token: {API_TOKEN}" \\')
        print(f'       -H "Content-Type: application/json" \\')
        print(f"       -d '{{\"name\":\"AlphaFleet\",\"v\":123456789,\"r\":100000}}'")
        print(f"  ─────────────────────────────────────────────────────────────\n")


# ─────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="OdinNet Daemon v7.1 — Burris Numerical System Multi-Fleet Networks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python odinnet_daemon.py                     ← default: web on port 8080
  python odinnet_daemon.py --no-web            ← SAFE MODE: no web server (Termux recommended first run)
  python odinnet_daemon.py --port 9090         ← custom port
  python odinnet_daemon.py --coord my_node.json --port 8080
  python odinnet_daemon.py --seed-messages
  ODINNET_TOKEN=mysecret python odinnet_daemon.py

Termux troubleshooting:
  If the GUI doesn't load in your phone browser, try:
    1. python odinnet_daemon.py --no-web      (confirm daemon works)
    2. python odinnet_daemon.py --port 9090   (try a different port)
    3. Check web_error.log for web server errors
    4. curl http://127.0.0.1:8080/status      (test from Termux itself)
        """,
    )
    parser.add_argument("--coord",               default=COORD_FILE,
                        help="Path to coordinate JSON file")
    parser.add_argument("--port",                type=int, default=DEFAULT_PORT,
                        help=f"Web server port (default: {DEFAULT_PORT})")
    parser.add_argument("--host",                default=DEFAULT_HOST,
                        help=f"Bind host (default: {DEFAULT_HOST})")
    parser.add_argument("--lattice-passphrase",  default=None,
                        help="LatticeFS encryption passphrase")
    parser.add_argument("--seed-messages",       action="store_true",
                        help="Seed test messages into outbox and exit")
    parser.add_argument("--force",               action="store_true",
                        help="Force seed even if outbox has messages")
    parser.add_argument("--no-web",              action="store_true",
                        help="Polling-only safe mode — no web server started")
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

    security = None
    if _SECURITY_AVAILABLE:
        try:
            security = OdinNetSecurity()
            print(f"  Security    : ✅  OdinNetSecurity loaded  "
                  f"DEFCON={security.defcon}  R={security.manifest.current_r}")
        except Exception as e:
            print(f"  Security    : ⚠  OdinNetSecurity init failed: {e}")

    _print_startup_banner(
        comms, fs, args.host, args.port, args.coord, security,
        no_web=args.no_web,
    )

    ctx = DaemonContext(comms=comms, fs=fs, security=security)

    # ── Background threads ────────────────────────────────────────────────

    poller = threading.Thread(
        target=_background_poller, args=(ctx,),
        daemon=True, name="OdinPoller"
    )
    poller.start()

    anomaly = threading.Thread(
        target=_anomaly_detector_loop, args=(ctx,),
        daemon=True, name="OdinAnomaly"
    )
    anomaly.start()

    # ── Web server or polling-only mode ───────────────────────────────────

    if args.no_web:
        ctx.log("Running in polling-only safe mode (--no-web). Press Ctrl-C to stop.")
        ctx.log("To enable web server: restart without --no-web flag.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n\n👋 OdinNet Temporal Node shutting down.")
        return

    # v7.1: _run_web_server catches ALL exceptions internally
    # and falls back to polling-only if the web server dies
    _run_web_server(ctx, args.host, args.port)

    print("\n\n👋 OdinNet Temporal Node shutting down.")


if __name__ == "__main__":
    main()
