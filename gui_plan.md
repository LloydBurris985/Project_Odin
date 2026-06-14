# OdinNet GUI Layer — Design Plan
## Project Odin / Starship Odin
### Scotty's Engineering Log — v0.1 Draft

---

## Overview

A lightweight GUI layer for OdinNet, runnable in Termux via a local web
browser (the daemon's OdinWeb already serves HTTP).  No Tkinter, no Qt —
pure HTML/CSS/JS served from LatticeFS, talking to the daemon via its
`/status` JSON endpoint and a new `/api` route.

Two phases:

1. **Phase 1 — Dashboard** (read-only, polling display, no new server deps)
2. **Phase 2 — Compose UI** (write outbox drafts via the browser)

---

## Phase 1 — Dashboard

### What it shows

```
┌─────────────────────────────────────────────────────────┐
│  ⬡ OdinNet Dashboard                    [node: abc123] │
├─────────────────────────────────────────────────────────┤
│  STATUS BAR                                             │
│  Uptime: 00:14:32   Polls: 47   Last: 14:22:01         │
│  LatticeFS: ✅ 5 files   Poll range: ✅ set             │
├────────────────┬────────────────────────────────────────┤
│  ACTIVITY LOG  │  RECEIVED MESSAGES                     │
│  (scrollable)  │  (last 10, newest first)               │
│                │                                        │
│  [14:22] poll  │  📨 "OdinNet Node Online"  14:10       │
│  [14:22] 0 msg │  📨 "Burris System Status" 14:10       │
│  [14:07] poll  │  📨 "Welcome to Universe"  14:10       │
│  ...           │  ...                                   │
├────────────────┴────────────────────────────────────────┤
│  LATTICE FILES              BEACONS                     │
│  index.html  1.2 KB         OdinNet-Public-1  active    │
│  readme.txt  0.4 KB         ...                         │
└─────────────────────────────────────────────────────────┘
```

### Implementation

**Served from:** LatticeFS as `dashboard.html`
**Auto-refreshes:** every 10 seconds via `<meta http-equiv="refresh">` or
  a JS `setInterval` + `fetch('/status')` call.
**No build step:** single self-contained HTML file, inline CSS + JS.
**Data source:** `/status` JSON endpoint (already exists in daemon).

Extend `/status` to include:
```json
{
  "node_id": "...",
  "poll_count": 47,
  "last_poll": "2026-06-14 14:22:01",
  "lattice_fs": true,
  "lattice_files": ["index.html", "readme.txt"],
  "received_count": 3,
  "recent_received": [
    {"subject": "OdinNet Node Online", "recv_time": "14:10:00"}
  ],
  "beacons": [],
  "activity_tail": ["[14:22:01] Temporal poll: 0 messages", "..."]
}
```

**New field additions to DaemonContext:**
- `received_log`  — list of dicts from polling results
- `beacon_status` — from `_load_beacons()`

---

## Phase 2 — Compose UI

### What it shows

```
┌─────────────────────────────────────────────────────────┐
│  ⬡ Compose Message                                      │
├─────────────────────────────────────────────────────────┤
│  To Date:   [2026-06-14        ]                        │
│  Subject:   [                  ]                        │
│  Body:                                                  │
│  ┌──────────────────────────────────────────────────┐  │
│  │                                                  │  │
│  │                                                  │  │
│  └──────────────────────────────────────────────────┘  │
│                                                         │
│  [  Save to Outbox  ]   [  Send Now  ]                  │
└─────────────────────────────────────────────────────────┘
```

### Implementation

**Requires:** A new `/api/compose` POST endpoint in the daemon.
**Payload:** `{ "to_date": "YYYY-MM-DD", "subject": "...", "body": "..." }`
**Response:** `{ "ok": true, "file": "outbox/draft_..." }`

The daemon handler calls `compose_message()` and optionally `send_outbox()`.

**New endpoint additions to OdinWebHandler:**
```python
def do_POST(self):
    if self.path == "/api/compose":
        self._handle_api_compose()
    elif self.path == "/api/send":
        self._handle_api_send()
```

---

## File Plan

Files to create (Phase 1):

| File                  | Where          | Purpose                         |
|-----------------------|----------------|---------------------------------|
| `dashboard.html`      | LatticeFS      | Main dashboard, JS polling      |
| `odinweb_api.py`      | project root   | API route mixin for OdinWebHandler |

Files to modify (Phase 1):

| File                  | Change                                        |
|-----------------------|-----------------------------------------------|
| `odinnet_daemon.py`   | Extend `/status` with received + beacon data  |
| `odinnet_daemon.py`   | Seed `dashboard.html` into LatticeFS on init  |
| `odinnet_daemon.py`   | Track `received_log` in DaemonContext         |

Files to create (Phase 2):

| File                  | Where          | Purpose                         |
|-----------------------|----------------|---------------------------------|
| `compose.html`        | LatticeFS      | Compose form, POST to /api      |
| `odinweb_api.py`      | project root   | `/api/compose` + `/api/send`    |

---

## Termux-specific notes

- Use `127.0.0.1` only (LAN access via `0.0.0.0` optional, security risk)
- Browser: Firefox for Android or via Termux's `termux-open` command
- No pip installs needed for Phase 1 (zero extra deps)
- Phase 2 needs no extra deps either — stdlib `http.server` handles POST bodies

---

## Priority order

1. Extend `/status` JSON (10 min — modify DaemonContext + handler)
2. `dashboard.html` — static HTML with JS refresh (30 min)
3. Seed `dashboard.html` into LatticeFS on init (5 min — add to `_seed_lattice_fs`)
4. `odinweb_api.py` + `/api/compose` endpoint (45 min)
5. `compose.html` (20 min)

Total estimated: ~2 hours of focused work for Phase 1 + 2 combined.

---

## Open questions for the Captain

1. Should the dashboard auto-send outbox on load, or manual button only?
2. Do you want realtime message history displayed separately from temporal?
3. Should compose support address book lookup (type alias, resolve coordinate)?
4. LatticeFS passphrase — store in daemon config file, env var, or prompt on start?
5. Phase 3: multi-node map view showing beacon coordinates on a number line?
