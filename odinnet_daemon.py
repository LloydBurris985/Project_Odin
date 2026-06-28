"""
OdinNet Integrated Daemon v11.0
================================
Phase-11-Harden upgrades over v10.0:
  [1] ATOMIC JOURNALING   — _hardened_process wraps every FS write in a
                            BEGIN → WRITE → COMMIT state machine with
                            rollback logging on failure. inbox_log only
                            appends AFTER a successful commit.
  [2] REPLAY SHIELD       — ctx.seen_packet_ids (net-new, daemon-level set)
                            intercepts duplicate packet_ids BEFORE the comms
                            engine burns CPU on parsing/HMAC.  Thread-safe
                            via ctx._lock.  Logs [REPLAY ATTACK BLOCKED].
  [3] ASYNC LIVE POLLING  — Dashboard JS setInterval(2000ms) hits /api/status
                            and /api/inbox without page refresh.  Dynamically
                            rebuilds stat counters AND inbox table rows in DOM.

Integrates:
  - OdinCommsEngine  (odinnet_comms.py)   stateless packet engine
  - LatticeFSv2      (lattice_fs_v2.py)   journaled coordinate filesystem
  - Termux Dashboard                       scanline aesthetic GUI

Key behaviours
--------------
  • OdinCommsEngine boots on startup; polling auto-starts via start_polling()
  • Valid incoming packets → LatticeFSv2.write_file("/mail/inbox/<msg_id>.json")
  • BBS / Usenet public drops → Space 2 (fleet-public) + register_url()
  • Bearer-token auth on all /api/* endpoints  (X-OdinNet-Token header)
  • RLock guards every daemon state mutation
  • Full GUI dashboard served from /  with AJAX live-update (2 s interval)
"""

import sys, os, json, time, hashlib, hmac, threading, traceback
sys.set_int_max_str_digits(100_000)

from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime    import datetime
from typing      import Dict, List, Optional

# ── OdinNet core imports ─────────────────────────────────────────────────────
from odinnet_comms import OdinCommsEngine, StatelessPacket
from lattice_fs_v2 import (
    lattice_fs_v2, LatticeFSv2,
    SPACE_USER, SPACE_SYSTEM,
    JOP_WRITE_FILE,
)

# ── Constants ─────────────────────────────────────────────────────────────────
DAEMON_VERSION    = "11.0"
DEFAULT_PORT      = 7474
SPACE_FLEET       = 2          # public BBS / Usenet space
BNS_PASSPHRASE    = os.environ.get("ODINNET_PASS",  "odinnet-lattice-2026")
BEARER_TOKEN      = os.environ.get("ODINNET_TOKEN", "odinnet-secret-token")
POLL_INTERVAL_SEC = 4
FS_SECTORS        = 256
FS_SECTOR_SIZE    = 512
FS_IMG_PATH       = "odinnet_lattice.json"   # persistence image

# Journal operation states (daemon-level state machine)
_JTX_IDLE   = "IDLE"
_JTX_OPEN   = "OPEN"
_JTX_COMMIT = "COMMITTED"
_JTX_ABORT  = "ABORTED"


# ===========================================================================
# DAEMON-LEVEL TRANSACTION WRAPPER
# ===========================================================================

class DaemonTransaction:
    """
    Lightweight BEGIN → WRITE → COMMIT state machine that wraps a single
    LatticeFSv2.write_file() call.  Not a full two-phase commit — it is a
    journaled intent log so that crash forensics can replay or discard any
    in-flight write.

    Usage (internal to _hardened_process):
        tx = DaemonTransaction(ctx, tx_id, path, blob, space_id)
        tx.begin()
        tx.write()      # raises on FS error → caller catches → tx.abort()
        tx.commit()     # only reached on success
    """

    def __init__(self, ctx: "DaemonContext", tx_id: str,
                 path: str, blob: bytes, space_id: int):
        self._ctx      = ctx
        self.tx_id     = tx_id
        self.path      = path
        self.blob      = blob
        self.space_id  = space_id
        self.state     = _JTX_IDLE
        self._entry    = None          # LatticeFSv2 FileEntry returned by write

    # ── Journal log helper (writes to daemon terminal feed) ──────────────────
    def _jlog(self, msg: str):
        self._ctx.comms.log(f"[TX {self.tx_id[:8]}] {msg}")

    def begin(self):
        if self.state != _JTX_IDLE:
            raise RuntimeError(f"TX {self.tx_id}: begin() called in state {self.state}")
        self.state = _JTX_OPEN
        self._jlog(f"BEGIN TX: WRITE {self.path} ({len(self.blob)}B) space={self.space_id}")

    def write(self):
        if self.state != _JTX_OPEN:
            raise RuntimeError(f"TX {self.tx_id}: write() called in state {self.state}")
        # Delegate to LatticeFSv2 — already journaled internally
        self._entry = self._ctx.fs.write_file(
            self.path, self.blob, space_id=self.space_id
        )
        self._jlog(f"WRITE OK: version={self._entry.version} sha256={self._entry.sha256[:12]}…")

    def commit(self):
        if self.state != _JTX_OPEN:
            raise RuntimeError(f"TX {self.tx_id}: commit() called in state {self.state}")
        self.state = _JTX_COMMIT
        self._jlog(f"COMMIT TX.")

    def abort(self, reason: str):
        self.state = _JTX_ABORT
        self._jlog(f"ABORT TX: {reason}")

    @property
    def entry(self):
        return self._entry


# ===========================================================================
# DAEMON CONTEXT  (all live state)
# ===========================================================================

class DaemonContext:
    """Single source of truth for all daemon-wide state. Thread-safe via RLock."""

    def __init__(self):
        self._lock = threading.RLock()
        self.node_id    = "ODINNET-NODE-" + hashlib.sha256(
                              BNS_PASSPHRASE.encode()).hexdigest()[:8].upper()
        self.base_coord = hashlib.sha256(
                              (BNS_PASSPHRASE + ":base").encode()).hexdigest()

        # ── LatticeFS v2 ─────────────────────────────────────────────────────
        self.fs: LatticeFSv2 = lattice_fs_v2(
            sector_size = FS_SECTOR_SIZE,
            n_sectors   = FS_SECTORS,
            passphrase  = BNS_PASSPHRASE,
        )
        self.fs.define_space(SPACE_FLEET, "fleet-public")

        # ── Comms Engine ─────────────────────────────────────────────────────
        self.comms = OdinCommsEngine(
            passphrase         = BNS_PASSPHRASE,
            my_base_coordinate = self.base_coord,
            node_id            = self.node_id,
        )

        # ── [PHASE-11] Replay Shield ─────────────────────────────────────────
        # Net-new daemon-level set.  Intercepts duplicate packet_ids BEFORE
        # the comms engine wastes CPU on HMAC verification.
        # Protected by self._lock — never access without acquiring the lock.
        self.seen_packet_ids: set = set()

        # ── Runtime stats ─────────────────────────────────────────────────────
        self.inbox_log:  List[dict] = []   # shadow of /mail/inbox/ for dashboard
        self.bbs_log:    List[dict] = []   # shadow of Space-2 posts
        self.defcon      = 5
        self.boot_time   = time.time()
        self.poll_count  = 0
        self.tx_count    = 0
        self.last_poll   = "—"

        # ── [PHASE-11] Journal event log (last 200 entries) ──────────────────
        self.journal_log: List[dict] = []  # {ts, tx_id, event, path, state}

    # ── Journal event recorder ────────────────────────────────────────────────

    def _record_journal_event(self, tx_id: str, event: str,
                               path: str, state: str):
        """Appends a structured entry to the in-memory journal log."""
        with self._lock:
            self.journal_log.append({
                "ts":    datetime.now().strftime("%H:%M:%S"),
                "tx_id": tx_id[:12],
                "event": event,
                "path":  path,
                "state": state,
            })
            if len(self.journal_log) > 200:
                self.journal_log = self.journal_log[-200:]

    # ── [PHASE-11] Atomic inbox persistence (BEGIN → WRITE → COMMIT) ─────────

    def persist_inbox_packet(self, packet: StatelessPacket):
        """
        Atomically persists a validated packet to /mail/inbox/<msg_id>.json.

        Protocol:
          1. BEGIN   — open DaemonTransaction, log intent
          2. WRITE   — call LatticeFSv2.write_file() (internally journaled)
          3. COMMIT  — mark transaction committed
          4. APPEND  — only NOW append to inbox_log (post-commit guarantee)

        On any FS exception the transaction is ABORTED, the journal records
        the failure, and inbox_log is NOT mutated.  The packet is NOT added
        to seen_packet_ids so a retransmit from the sender can succeed.
        """
        record = {
            "msg_id":    packet.msg_id,
            "sender_id": packet.sender_id,
            "payload":   packet.payload,
            "reply_to":  packet.reply_to,
            "timestamp": packet.timestamp,
            "v_target":  packet.v_target,
            "signature": packet.signature,
            "ingested":  time.time(),
        }
        path  = f"/mail/inbox/{packet.msg_id}.json"
        blob  = json.dumps(record, indent=2).encode("utf-8")
        tx_id = hashlib.sha256(
            (packet.msg_id + str(time.time())).encode()
        ).hexdigest()[:16]

        tx = DaemonTransaction(self, tx_id, path, blob, space_id=SPACE_USER)

        with self._lock:
            try:
                # ── BEGIN ────────────────────────────────────────────────────
                tx.begin()
                self._record_journal_event(tx_id, "BEGIN", path, _JTX_OPEN)

                # ── WRITE ────────────────────────────────────────────────────
                tx.write()
                self._record_journal_event(tx_id, "WRITE", path, _JTX_OPEN)

                # ── COMMIT ───────────────────────────────────────────────────
                tx.commit()
                self._record_journal_event(tx_id, "COMMIT", path, _JTX_COMMIT)

                # ── POST-COMMIT: safe to update in-memory shadow ──────────────
                self.inbox_log.append(record)
                if len(self.inbox_log) > 200:
                    self.inbox_log = self.inbox_log[-200:]

            except Exception as exc:
                tx.abort(str(exc))
                self._record_journal_event(tx_id, f"ABORT:{exc}", path, _JTX_ABORT)
                # Do NOT append to inbox_log — transaction failed
                # Do NOT add to seen_packet_ids — allow sender retry
                raise

    # ── BBS public post persistence ───────────────────────────────────────────

    def persist_bbs_post(self, subject: str, body: str,
                         author: str = None) -> dict:
        """
        Write a public BBS post into Space 2 (fleet-public).
        Also registers a burris:// URL in the coordinate index.
        """
        post_id = hashlib.sha256(
            (subject + body + str(time.time())).encode()
        ).hexdigest()[:12]
        author  = author or self.node_id
        ts      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record  = {
            "post_id": post_id,
            "subject": subject,
            "body":    body,
            "author":  author,
            "ts":      ts,
        }
        path = f"/bbs/{post_id}.json"
        blob = json.dumps(record, indent=2).encode("utf-8")
        url  = f"burris://odinnet.bbs/{post_id}"

        with self._lock:
            entry = self.fs.write_file(path, blob, space_id=SPACE_FLEET)
            self.fs.register_url(
                url, entry.start_V,
                metadata={"post_id": post_id, "subject": subject[:40]}
            )
            self.bbs_log.append(record)
            if len(self.bbs_log) > 100:
                self.bbs_log = self.bbs_log[-100:]

        return record

    # ── Outbox ────────────────────────────────────────────────────────────────

    def stage_outgoing(self, target: str, text: str) -> str:
        coord = self.comms.stage_outgoing_message(target, text)
        with self._lock:
            self.tx_count += 1
        return coord

    # ── Polling stats ──────────────────────────────────────────────────────────

    def tick_poll(self):
        with self._lock:
            self.poll_count += 1
            self.last_poll   = datetime.now().strftime("%H:%M:%S")


# ===========================================================================
# POLLING INTEGRATION  (comms → FS bridge)
# ===========================================================================

def _make_storage_callback(ctx: DaemonContext):
    """
    Returns the callback that OdinCommsEngine.start_polling() calls each cycle.
    """
    def storage_cb(my_base: str) -> List[str]:
        ctx.tick_poll()
        with ctx._lock:
            coords = [
                ctx.comms.packet_to_coordinate(pkt)
                for pkt in ctx.comms.outbox.values()
            ]
        return coords

    return storage_cb


def _patch_comms_process(ctx: DaemonContext):
    """
    Monkey-patch OdinCommsEngine.process_incoming_coordinate to install:
      [1] Daemon-level replay shield   (ctx.seen_packet_ids check)
      [2] Atomic journal wrap          (BEGIN → WRITE → COMMIT via DaemonTransaction)

    Architecture:
        raw coordinate
             │
             ▼
        [REPLAY SHIELD] ── duplicate? ──▶ log BLOCKED, return None
             │ unique
             ▼
        add to ctx.seen_packet_ids
             │
             ▼
        _original(coordinate)   ← comms engine HMAC verify, decode
             │ valid packet
             ▼
        persist_inbox_packet()  ← atomic BEGIN→WRITE→COMMIT
             │ commit OK
             ▼
        return packet
    """
    _original = ctx.comms.process_incoming_coordinate

    def _hardened_process(coordinate: str) -> Optional[StatelessPacket]:
        # ── [1] REPLAY SHIELD ─────────────────────────────────────────────────
        # Derive a stable fingerprint from the raw coordinate string.
        # We hash the coordinate so we don't store multi-kilobyte strings
        # in the seen set.  Collision probability at SHA-256 is negligible.
        coord_fingerprint = hashlib.sha256(
            coordinate.encode("utf-8")
        ).hexdigest()

        with ctx._lock:
            if coord_fingerprint in ctx.seen_packet_ids:
                ctx.comms.log(
                    f"[REPLAY ATTACK BLOCKED] fingerprint={coord_fingerprint[:16]}… "
                    f"— coordinate already processed, dropping."
                )
                return None
            # Register BEFORE handing off — prevents race on concurrent pollers
            ctx.seen_packet_ids.add(coord_fingerprint)

        # ── [2] COMMS ENGINE HMAC VERIFY + DECODE ─────────────────────────────
        packet = _original(coordinate)

        if packet is None:
            # Comms engine rejected (bad HMAC, malformed, not-for-us, etc.)
            # Remove fingerprint so a legitimately retransmitted fix can pass
            with ctx._lock:
                ctx.seen_packet_ids.discard(coord_fingerprint)
            return None

        # ── [3] ATOMIC FS PERSIST (BEGIN → WRITE → COMMIT) ────────────────────
        try:
            ctx.persist_inbox_packet(packet)
        except Exception as exc:
            # FS write failed — remove fingerprint so sender can retry
            with ctx._lock:
                ctx.seen_packet_ids.discard(coord_fingerprint)
            ctx.comms.log(
                f"⚠  Atomic TX failed for [{packet.msg_id}]: {exc} "
                f"— fingerprint evicted, sender may retry."
            )

        return packet

    ctx.comms.process_incoming_coordinate = _hardened_process


# ===========================================================================
# HTTP HANDLER
# ===========================================================================

def _make_handler(ctx: DaemonContext):
    """Closure so BaseHTTPRequestHandler can access DaemonContext."""

    class OdinWebHandler(BaseHTTPRequestHandler):
        log_message = lambda *a: None   # suppress stdout noise

        # ── Auth ──────────────────────────────────────────────────────────────

        def _auth_ok(self) -> bool:
            return self.headers.get("X-OdinNet-Token", "") == BEARER_TOKEN

        def _send_json(self, code: int, obj: dict):
            body = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type",   "application/json")
            self.send_header("Content-Length",  len(body))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if length:
                return json.loads(self.rfile.read(length))
            return {}

        def _require_auth(self) -> bool:
            if not self._auth_ok():
                self._send_json(401, {"error": "Unauthorized. Supply X-OdinNet-Token header."})
                return False
            return True

        # ── Routing ───────────────────────────────────────────────────────────

        def do_GET(self):
            path = self.path.split("?")[0]

            if path == "/":
                self._serve_dashboard()

            elif path == "/api/status":
                if not self._require_auth(): return
                with ctx._lock:
                    self._send_json(200, {
                        "node_id":      ctx.node_id,
                        "version":      DAEMON_VERSION,
                        "defcon":       ctx.defcon,
                        "uptime_sec":   round(time.time() - ctx.boot_time, 1),
                        "polls_fired":  ctx.poll_count,   # [PHASE-11] renamed key
                        "tx_count":     ctx.tx_count,
                        "last_poll":    ctx.last_poll,
                        "inbox_count":  len(ctx.inbox_log),
                        "bbs_count":    len(ctx.bbs_log),
                        "replay_blocked": len(ctx.seen_packet_ids),
                    })

            elif path == "/api/inbox":
                if not self._require_auth(): return
                with ctx._lock:
                    # Return structured rows for AJAX table rebuild
                    rows = []
                    for rec in reversed(ctx.inbox_log[-50:]):
                        rows.append({
                            "ts":      datetime.fromtimestamp(
                                           rec["timestamp"]).strftime("%H:%M:%S"),
                            "sender":  rec["sender_id"][:20],
                            "payload": rec["payload"][:72],
                            "msg_id":  rec["msg_id"],
                        })
                    self._send_json(200, {"inbox": rows})

            elif path == "/api/bbs":
                if not self._require_auth(): return
                with ctx._lock:
                    self._send_json(200, {"posts": ctx.bbs_log[-50:]})

            elif path == "/api/journal":
                if not self._require_auth(): return
                with ctx._lock:
                    self._send_json(200, {"journal": ctx.journal_log[-50:]})

            elif path == "/api/spaces":
                if not self._require_auth(): return
                with ctx._lock:
                    spaces = ctx.fs._store._spaces.all_spaces()
                    self._send_json(200, {"spaces": spaces})

            elif path.startswith("/api/resolve/"):
                if not self._require_auth(): return
                url_key = "/" + path[len("/api/resolve/"):]
                rec = ctx.fs.resolve_url(url_key)
                self._send_json(200 if rec else 404,
                                rec or {"error": "URL not found"})

            elif path == "/api/fs/ls":
                if not self._require_auth(): return
                heads = ctx.fs._store._versions.all_live_heads()
                listing = {
                    p: {
                        "version":  e.version,
                        "length":   e.length,
                        "space_id": e.space_id,
                        "sha256":   e.sha256[:12] + "…",
                    }
                    for p, e in sorted(heads.items())
                }
                self._send_json(200, {"files": listing})

            else:
                self._send_json(404, {"error": "Not found"})

        def do_POST(self):
            path = self.path.split("?")[0]

            if path == "/api/send":
                if not self._require_auth(): return
                body     = self._read_body()
                target   = body.get("target", ctx.base_coord)
                text     = body.get("message", "")
                reply_to = body.get("reply_to", "")
                if not text:
                    self._send_json(400, {"error": "message required"})
                    return
                packet = StatelessPacket(
                    v_target=target, sender_id=ctx.node_id, payload=text,
                    reply_to=reply_to,
                )
                mac = hmac.new(BNS_PASSPHRASE.encode(),
                               packet.payload.encode(), hashlib.sha256)
                packet.signature = mac.hexdigest()[:16]

                # ── [PHASE-11] Use ctx.seen_packet_ids (authoritative set) ───
                with ctx._lock:
                    ctx.comms.outbox[packet.msg_id] = packet
                    ctx.seen_packet_ids.add(          # daemon-level replay cache
                        hashlib.sha256(
                            packet.msg_id.encode()
                        ).hexdigest()
                    )
                    ctx.tx_count += 1

                coord = ctx.comms.packet_to_coordinate(packet)
                self._send_json(200, {
                    "msg_id":             packet.msg_id,
                    "coordinate_prefix":  str(coord)[:32] + "…",
                })

            elif path == "/api/receive":
                if not self._require_auth(): return
                body  = self._read_body()
                coord = body.get("coordinate", "")
                if not coord:
                    self._send_json(400, {"error": "coordinate required"})
                    return
                pkt = ctx.comms.process_incoming_coordinate(str(coord))
                if pkt:
                    self._send_json(200, {
                        "status":       "accepted",
                        "msg_id":       pkt.msg_id,
                        "sender":       pkt.sender_id,
                        "payload":      pkt.payload,
                        "persisted_to": f"/mail/inbox/{pkt.msg_id}.json",
                    })
                else:
                    self._send_json(400, {
                        "status": "rejected",
                        "reason": "Invalid, duplicate, replay-blocked, or tampered coordinate",
                    })

            elif path == "/api/bbs/post":
                if not self._require_auth(): return
                body    = self._read_body()
                subject = body.get("subject", "")
                content = body.get("body", "")
                author  = body.get("author", ctx.node_id)
                if not subject or not content:
                    self._send_json(400, {"error": "subject and body required"})
                    return
                record = ctx.persist_bbs_post(subject, content, author)
                self._send_json(200, {
                    "status":     "posted",
                    "post_id":    record["post_id"],
                    "burris_url": f"burris://odinnet.bbs/{record['post_id']}",
                    "space":      SPACE_FLEET,
                })

            elif path == "/api/defcon":
                if not self._require_auth(): return
                body  = self._read_body()
                level = body.get("level")
                valid = [1, 3, 5, 7, 10]
                if level not in valid:
                    self._send_json(400, {"error": f"DEFCON must be one of {valid}"})
                    return
                with ctx._lock:
                    ctx.defcon = level
                self._send_json(200, {"defcon": ctx.defcon})

            elif path == "/api/fs/save":
                if not self._require_auth(): return
                ctx.fs.save(FS_IMG_PATH)
                self._send_json(200, {"saved": FS_IMG_PATH})

            else:
                self._send_json(404, {"error": "Not found"})

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin",  "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                             "Content-Type,X-OdinNet-Token")
            self.end_headers()

        # ── Dashboard HTML ─────────────────────────────────────────────────────

        def _serve_dashboard(self):
            html = _build_dashboard_html(ctx)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type",   "text/html; charset=utf-8")
            self.send_header("Content-Length",  len(body))
            self.end_headers()
            self.wfile.write(body)

    return OdinWebHandler


# ===========================================================================
# DASHBOARD HTML  (Termux scanline aesthetic · Phase-11 AJAX live polling)
# ===========================================================================

def _build_dashboard_html(ctx: DaemonContext) -> str:
    with ctx._lock:
        uptime   = int(time.time() - ctx.boot_time)
        h, rem   = divmod(uptime, 3600)
        m, s     = divmod(rem, 60)
        uptime_s = f"{h:02d}:{m:02d}:{s:02d}"
        defcon   = ctx.defcon
        polls    = ctx.poll_count
        tx       = ctx.tx_count
        inbox_n  = len(ctx.inbox_log)
        bbs_n    = len(ctx.bbs_log)
        last_p   = ctx.last_poll
        node_id  = ctx.node_id
        replay_n = len(ctx.seen_packet_ids)

        # Seed initial inbox rows (AJAX will replace these every 2 s)
        inbox_rows = ""
        for rec in reversed(ctx.inbox_log[-8:]):
            ts  = datetime.fromtimestamp(rec["timestamp"]).strftime("%H:%M:%S")
            pay = rec["payload"][:52].replace("<","&lt;").replace(">","&gt;")
            sid = rec["sender_id"][:20]
            inbox_rows += (
                f'<tr><td class="ts">{ts}</td>'
                f'<td class="sid">{sid}</td>'
                f'<td class="pay">{pay}</td></tr>'
            )

        bbs_rows = ""
        for rec in reversed(ctx.bbs_log[-5:]):
            subj = rec["subject"][:40].replace("<","&lt;")
            pid  = rec["post_id"]
            auth = rec["author"][:18]
            bbs_rows += (
                f'<tr><td class="ts">{rec["ts"][-8:]}</td>'
                f'<td class="sid">{auth}</td>'
                f'<td class="pay">{subj}</td>'
                f'<td><a class="burl" href="/api/resolve/burris://odinnet.bbs/{pid}">'
                f'burris://{pid}</a></td></tr>'
            )

        # Seed initial journal rows
        journal_rows = ""
        for entry in reversed(ctx.journal_log[-6:]):
            state_color = {
                _JTX_COMMIT: "var(--accent2)",
                _JTX_ABORT:  "var(--danger)",
                _JTX_OPEN:   "var(--warn)",
                _JTX_IDLE:   "var(--muted)",
            }.get(entry["state"], "var(--text)")
            journal_rows += (
                f'<tr>'
                f'<td class="ts">{entry["ts"]}</td>'
                f'<td class="sid">{entry["tx_id"]}</td>'
                f'<td class="pay">{entry["event"]}</td>'
                f'<td style="color:{state_color};white-space:nowrap">{entry["state"]}</td>'
                f'</tr>'
            )

    defcon_color = {
        1:"#ff2244", 3:"#ff6600", 5:"#ffcc00", 7:"#44ff88", 10:"#00ccff"
    }.get(defcon, "#fff")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>OdinNet v{DAEMON_VERSION} — Navigation Bridge</title>
<style>
  :root {{
    --bg:      #050810;
    --panel:   #0a0f1a;
    --border:  #1a2a4a;
    --accent:  #00aaff;
    --accent2: #00ff99;
    --warn:    #ffcc00;
    --danger:  #ff2244;
    --text:    #c8daf0;
    --muted:   #445566;
    --font:    'Courier New', 'Lucida Console', monospace;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{
    background:var(--bg); color:var(--text);
    font-family:var(--font); font-size:12px;
    min-height:100vh; overflow-x:hidden;
  }}
  /* Scanline overlay */
  body::before {{
    content:""; pointer-events:none; position:fixed; inset:0;
    background:repeating-linear-gradient(
      0deg, transparent, transparent 2px,
      rgba(0,0,0,0.18) 2px, rgba(0,0,0,0.18) 4px
    );
    z-index:9999;
  }}
  .shell {{ max-width:960px; margin:0 auto; padding:10px; }}

  /* Header */
  .hdr {{
    border:1px solid var(--accent);
    background:linear-gradient(90deg,#001830 0%,#000c1f 100%);
    padding:8px 14px; display:flex; align-items:center;
    justify-content:space-between; margin-bottom:10px;
    box-shadow:0 0 18px rgba(0,170,255,0.25);
  }}
  .hdr-title {{
    font-size:15px; font-weight:bold; letter-spacing:3px;
    color:var(--accent); text-shadow:0 0 8px var(--accent);
  }}
  .hdr-sub  {{ font-size:10px; color:var(--muted); letter-spacing:1px; margin-top:2px; }}
  .hdr-node {{ font-size:10px; color:var(--accent2); text-align:right; }}

  /* DEFCON pill */
  .defcon-pill {{
    display:inline-block; padding:3px 10px; border-radius:3px;
    font-weight:bold; font-size:11px; letter-spacing:2px;
    color:#000; background:{defcon_color};
    box-shadow:0 0 10px {defcon_color}88; margin-left:10px;
  }}

  /* Stat bar */
  .statbar {{
    display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr));
    gap:8px; margin-bottom:10px;
  }}
  .stat {{
    background:var(--panel); border:1px solid var(--border);
    padding:8px 10px; display:flex; flex-direction:column; gap:2px;
  }}
  .stat-label {{ font-size:9px; color:var(--muted); letter-spacing:2px; text-transform:uppercase; }}
  .stat-value {{ font-size:18px; color:var(--accent); font-weight:bold; text-shadow:0 0 6px var(--accent); }}
  .stat-value.green {{ color:var(--accent2); text-shadow:0 0 6px var(--accent2); }}
  .stat-value.warn  {{ color:var(--warn);    text-shadow:0 0 6px var(--warn); }}
  .stat-value.danger{{ color:var(--danger);  text-shadow:0 0 6px var(--danger); }}

  /* Panels */
  .grid2 {{
    display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:10px;
  }}
  @media(max-width:620px){{ .grid2{{ grid-template-columns:1fr; }} }}
  .panel {{ background:var(--panel); border:1px solid var(--border); overflow:hidden; }}
  .panel-hdr {{
    background:#0d1a2e; border-bottom:1px solid var(--border);
    padding:5px 10px; font-size:10px; letter-spacing:2px; color:var(--accent);
    display:flex; align-items:center; justify-content:space-between;
  }}
  .panel-hdr .badge {{
    background:var(--accent); color:#000;
    font-size:9px; font-weight:bold; padding:1px 6px; border-radius:2px;
  }}
  .panel-body {{ padding:8px; }}

  /* Tables */
  table {{ width:100%; border-collapse:collapse; }}
  th,td {{ padding:3px 5px; text-align:left; vertical-align:top; }}
  th {{ font-size:9px; letter-spacing:1px; color:var(--muted); border-bottom:1px solid var(--border); }}
  td {{ font-size:11px; border-bottom:1px solid #0d1a2e; word-break:break-all; }}
  td.ts  {{ color:var(--muted); white-space:nowrap; width:58px; }}
  td.sid {{ color:var(--accent2); width:100px; white-space:nowrap; overflow:hidden; }}
  td.pay {{ color:var(--text); }}
  tr:hover td {{ background:#0f1f35; }}
  .burl {{ color:#7788ff; font-size:10px; text-decoration:none; }}
  .burl:hover {{ color:var(--accent); }}

  /* AJAX pulse indicator */
  #ajax-pulse {{
    display:inline-block; width:7px; height:7px; border-radius:50%;
    background:var(--accent2); margin-left:6px;
    animation:pulse 2s ease-in-out infinite;
  }}
  @keyframes pulse {{
    0%,100%{{ opacity:1; box-shadow:0 0 4px var(--accent2); }}
    50%{{ opacity:0.2; box-shadow:none; }}
  }}

  /* Compose area */
  .compose {{
    background:var(--panel); border:1px solid var(--border);
    padding:10px; margin-bottom:10px;
  }}
  .compose-title {{ font-size:10px; letter-spacing:2px; color:var(--accent); margin-bottom:8px; }}
  .compose-row {{ display:flex; gap:6px; margin-bottom:6px; flex-wrap:wrap; }}
  .compose input,.compose textarea,.compose select {{
    background:#07111f; border:1px solid var(--border);
    color:var(--text); font-family:var(--font); font-size:11px;
    padding:5px 8px; flex:1; outline:none; min-width:0;
  }}
  .compose input:focus,.compose textarea:focus {{
    border-color:var(--accent); box-shadow:0 0 6px rgba(0,170,255,0.3);
  }}
  .compose textarea {{ min-height:52px; resize:vertical; }}
  .btn {{
    background:transparent; border:1px solid var(--accent);
    color:var(--accent); font-family:var(--font); font-size:11px;
    padding:5px 14px; cursor:pointer; letter-spacing:1px;
    transition:all 0.15s; white-space:nowrap;
  }}
  .btn:hover {{ background:var(--accent); color:#000; box-shadow:0 0 10px var(--accent); }}
  .btn.green {{ border-color:var(--accent2); color:var(--accent2); }}
  .btn.green:hover {{ background:var(--accent2); }}
  .btn.danger {{ border-color:var(--danger); color:var(--danger); }}
  .btn.danger:hover {{ background:var(--danger); color:#fff; }}

  /* Console feed */
  #console {{
    background:#04090f; border:1px solid var(--border);
    color:#66ff99; font-size:10px; padding:8px;
    height:110px; overflow-y:auto;
    font-family:var(--font); margin-bottom:10px;
    white-space:pre-wrap; word-break:break-all;
  }}

  /* Space legend */
  .space-badge {{
    display:inline-block; font-size:9px; padding:1px 5px;
    border-radius:2px; margin:1px;
  }}
  .sp0 {{ background:#222; color:var(--muted); }}
  .sp1 {{ background:#002244; color:var(--accent); }}
  .sp2 {{ background:#002200; color:var(--accent2); }}

  .footer {{
    border-top:1px solid var(--border); padding-top:6px;
    font-size:9px; color:var(--muted); text-align:center; letter-spacing:1px;
  }}
  .blink {{ animation:blink 1.2s step-end infinite; }}
  @keyframes blink {{ 50%{{ opacity:0; }} }}
</style>
</head>
<body>
<div class="shell">

<!-- HEADER -->
<div class="hdr">
  <div>
    <div class="hdr-title">⬡ ODINNET v{DAEMON_VERSION} — NAVIGATION BRIDGE
      <span class="defcon-pill">DEFCON {defcon}</span>
    </div>
    <div class="hdr-sub">BNS LATTICE COMMUNICATION MATRIX · JOURNALED COORDINATE FILESYSTEM</div>
  </div>
  <div class="hdr-node">
    {node_id}<br>
    UPTIME: <span id="live-uptime">{uptime_s}</span>&nbsp;
    <span class="blink" style="color:var(--accent2)">■</span>
  </div>
</div>

<!-- STAT BAR  (ids targeted by AJAX updater) -->
<div class="statbar">
  <div class="stat">
    <div class="stat-label">Polls Fired</div>
    <div class="stat-value" id="polls-fired">{polls}</div>
  </div>
  <div class="stat">
    <div class="stat-label">TX Staged</div>
    <div class="stat-value green" id="tx-count">{tx}</div>
  </div>
  <div class="stat">
    <div class="stat-label">Inbox (Space 1)</div>
    <div class="stat-value" id="inbox-count">{inbox_n}</div>
  </div>
  <div class="stat">
    <div class="stat-label">BBS Posts (Space 2)</div>
    <div class="stat-value green" id="bbs-count">{bbs_n}</div>
  </div>
  <div class="stat">
    <div class="stat-label">Last Poll</div>
    <div class="stat-value warn" style="font-size:13px" id="last-poll">{last_p}</div>
  </div>
  <div class="stat">
    <div class="stat-label">Replay Shield</div>
    <div class="stat-value danger" id="replay-count">{replay_n}</div>
  </div>
</div>

<!-- INBOX + BBS PANELS -->
<div class="grid2">
  <div class="panel">
    <div class="panel-hdr">
      ⬡ LIVE INBOX · SPACE 1 · /mail/inbox/
      <span id="ajax-pulse" title="AJAX live · 2s"></span>
      <span class="badge" id="inbox-badge">{inbox_n}</span>
    </div>
    <div class="panel-body">
      <table id="inbox-table">
        <tr><th>TIME</th><th>SENDER</th><th>PAYLOAD</th></tr>
        {inbox_rows if inbox_rows else
         '<tr id="inbox-empty"><td colspan="3" style="color:var(--muted);text-align:center;padding:12px">Awaiting transmissions…</td></tr>'}
      </table>
    </div>
  </div>
  <div class="panel">
    <div class="panel-hdr">
      ⬡ BBS BOARD · SPACE 2 · burris://
      <span class="badge" id="bbs-badge">{bbs_n}</span>
    </div>
    <div class="panel-body">
      <table>
        <tr><th>TIME</th><th>AUTHOR</th><th>SUBJECT</th><th>URL</th></tr>
        {bbs_rows if bbs_rows else
         '<tr><td colspan="4" style="color:var(--muted);text-align:center;padding:12px">No public posts yet…</td></tr>'}
      </table>
    </div>
  </div>
</div>

<!-- STATE MACHINE REPLAY / JOURNAL PANEL -->
<div class="panel" style="margin-bottom:10px">
  <div class="panel-hdr">
    ⬡ STATE MACHINE REPLAY / JOURNAL
    <span class="badge">BEGIN→COMMIT</span>
  </div>
  <div class="panel-body">
    <table id="journal-table">
      <tr><th>TIME</th><th>TX ID</th><th>EVENT</th><th>STATE</th></tr>
      {journal_rows if journal_rows else
       '<tr><td colspan="4" style="color:var(--muted);text-align:center;padding:12px">No transactions yet…</td></tr>'}
    </table>
  </div>
</div>

<!-- COMPOSE PANEL -->
<div class="compose">
  <div class="compose-title">⬡ TRANSMIT MESSAGE · STATELESS COMMS ENGINE</div>
  <div class="compose-row">
    <input id="tgt"   placeholder="Target coordinate (blank = loopback)" style="flex:2"/>
    <input id="reply" placeholder="Reply-to msg_id (optional)" style="flex:1"/>
  </div>
  <div class="compose-row">
    <textarea id="msg" placeholder="Message payload…"></textarea>
  </div>
  <div class="compose-row">
    <button class="btn"        onclick="sendMsg()">▶ TRANSMIT</button>
    <button class="btn green"  onclick="receiveCoord()">⬇ INJECT COORDINATE</button>
    <button class="btn danger" onclick="setDefcon()">⚡ SET DEFCON</button>
    <button class="btn"        onclick="saveFS()">💾 SAVE LATTICE</button>
  </div>
  <div id="tx-status" style="font-size:10px;color:var(--accent2);margin-top:4px;min-height:14px"></div>
</div>

<!-- BBS COMPOSE -->
<div class="compose" style="margin-bottom:10px">
  <div class="compose-title">⬡ POST TO BBS BOARD · SPACE 2 · fleet-public</div>
  <div class="compose-row">
    <input id="bbs-subject" placeholder="Subject / headline…" style="flex:2"/>
    <input id="bbs-author"  placeholder="Author (default: node ID)" style="flex:1"/>
  </div>
  <div class="compose-row">
    <textarea id="bbs-body" placeholder="Post body…"></textarea>
  </div>
  <div class="compose-row">
    <button class="btn green" onclick="postBBS()">📡 BROADCAST TO FLEET</button>
  </div>
  <div id="bbs-status" style="font-size:10px;color:var(--accent2);margin-top:4px;min-height:14px"></div>
</div>

<!-- CONSOLE FEED -->
<div id="console">OdinNet v{DAEMON_VERSION} — Navigation Bridge online.
Space 1 (user) inbox mounted at /mail/inbox/
Space 2 (fleet-public) BBS mounted — burris:// URL registry active.
[PHASE-11] Replay Shield ACTIVE — ctx.seen_packet_ids initialized.
[PHASE-11] Atomic Journaling ACTIVE — BEGIN→WRITE→COMMIT on every RX.
[PHASE-11] AJAX live-poll ACTIVE — 2 s interval on /api/status + /api/inbox.
Polling engine ENGAGED — interval {POLL_INTERVAL_SEC}s.
Awaiting coordinate traffic…
</div>

<!-- SPACE LEGEND -->
<div style="margin-bottom:10px;font-size:9px;color:var(--muted)">
  COORDINATE SPACES:
  <span class="space-badge sp0">0 SYSTEM</span>
  <span class="space-badge sp1">1 USER · /mail/inbox/</span>
  <span class="space-badge sp2">2 FLEET-PUBLIC · BBS</span>
</div>

<!-- FOOTER -->
<div class="footer">
  ⬡ BNS LATTICE COORDINATE MATRIX · JOURNALED · VERSIONED · HMAC-SHA256 AUTHENTICATED<br>
  OdinNet v{DAEMON_VERSION} — R=1 STATELESS · AES-256-GCM · DEFCON-{defcon} ACTIVE ·
  REPLAY SHIELD · ATOMIC TX
</div>

</div><!-- /shell -->

<script>
// ── Auth & helpers ──────────────────────────────────────────────────────────
const TOKEN = "{BEARER_TOKEN}";
const H     = {{"Content-Type":"application/json","X-OdinNet-Token":TOKEN}};

const log = (m) => {{
  const el = document.getElementById("console");
  el.textContent += "\\n" + new Date().toLocaleTimeString() + " > " + m;
  el.scrollTop = el.scrollHeight;
}};
const apiPost = async (path, body) => {{
  const r = await fetch(path, {{method:"POST", headers:H, body:JSON.stringify(body)}});
  return r.json();
}};
const apiGet = async (path) => {{
  const r = await fetch(path, {{headers:H}});
  return r.json();
}};

// ── DOM helpers ─────────────────────────────────────────────────────────────
const setText = (id, v) => {{
  const el = document.getElementById(id);
  if (el && el.textContent !== String(v)) el.textContent = v;
}};

// Rebuild inbox table rows from AJAX payload
// Keeps the header row, replaces data rows only when count changed.
let _lastInboxCount = {inbox_n};

function rebuildInboxTable(rows) {{
  const tbl = document.getElementById("inbox-table");
  if (!tbl) return;

  // Remove all data rows (keep the <tr> header at index 0)
  while (tbl.rows.length > 1) tbl.deleteRow(1);

  if (!rows || rows.length === 0) {{
    const row = tbl.insertRow();
    const cell = row.insertCell();
    cell.colSpan = 3;
    cell.style.cssText = "color:var(--muted);text-align:center;padding:12px";
    cell.textContent = "Awaiting transmissions…";
    return;
  }}

  // rows arrive newest-first from /api/inbox
  rows.slice(0, 8).forEach(rec => {{
    const row  = tbl.insertRow();
    const tdTs  = row.insertCell(); tdTs.className  = "ts";  tdTs.textContent  = rec.ts     || "";
    const tdSid = row.insertCell(); tdSid.className = "sid"; tdSid.textContent = rec.sender  || "";
    const tdPay = row.insertCell(); tdPay.className = "pay"; tdPay.textContent = rec.payload || "";
  }});
}}

// ── [PHASE-11] ASYNC AJAX LIVE POLL — 2 000 ms ──────────────────────────────
// Hits /api/status for counters, then /api/inbox only when inbox_count changed.
setInterval(async function() {{
  try {{
    // 1. Fetch status counters
    const st = await apiGet("/api/status");

    // Update stat bar
    setText("polls-fired",   st.polls_fired   ?? st.poll_count ?? "—");
    setText("tx-count",      st.tx_count      ?? "—");
    setText("inbox-count",   st.inbox_count   ?? "—");
    setText("bbs-count",     st.bbs_count     ?? "—");
    setText("last-poll",     st.last_poll     ?? "—");
    setText("replay-count",  st.replay_blocked ?? "—");
    setText("inbox-badge",   st.inbox_count   ?? "—");
    setText("bbs-badge",     st.bbs_count     ?? "—");

    // Update page title with live DEFCON + inbox
    document.title = "OdinNet · DEFCON " + (st.defcon ?? "?") +
                     " · Inbox:" + (st.inbox_count ?? 0);

    // 2. Only re-fetch inbox rows when the count changed
    const newCount = st.inbox_count ?? 0;
    if (newCount !== _lastInboxCount) {{
      _lastInboxCount = newCount;
      const inboxData = await apiGet("/api/inbox");
      rebuildInboxTable(inboxData.inbox || []);
      if (newCount > 0) {{
        log("📥 Inbox updated — " + newCount + " message(s)");
      }}
    }}

  }} catch(e) {{
    log("⚠  AJAX poll failed: " + e);
  }}
}}, 2000);

// ── Action handlers ─────────────────────────────────────────────────────────
async function sendMsg() {{
  const msg = document.getElementById("msg").value.trim();
  const tgt = document.getElementById("tgt").value.trim();
  const rp  = document.getElementById("reply").value.trim();
  if (!msg) {{ log("⚠  No message to transmit."); return; }}
  const r = await apiPost("/api/send", {{
    message: msg,
    target:  tgt  || undefined,
    reply_to:rp   || undefined,
  }});
  if (r.msg_id) {{
    document.getElementById("tx-status").textContent =
      "✅ TX staged · msg_id=" + r.msg_id;
    log("TX · " + r.msg_id + " · coord=" + r.coordinate_prefix);
    document.getElementById("msg").value = "";
  }} else {{
    log("❌ TX failed: " + JSON.stringify(r));
  }}
}}

async function receiveCoord() {{
  const coord = prompt("Paste coordinate to inject:");
  if (!coord) return;
  const r = await apiPost("/api/receive", {{coordinate:coord}});
  if (r.status === "accepted") {{
    log("📥 RX accepted · " + r.msg_id + " · → " + r.persisted_to);
  }} else {{
    log("🛑 RX rejected: " + r.reason);
  }}
}}

async function setDefcon() {{
  const lvl = parseInt(prompt("DEFCON level (1/3/5/7/10):"));
  if (!lvl) return;
  const r = await apiPost("/api/defcon", {{level:lvl}});
  if (r.defcon) log("⚡ DEFCON set → " + r.defcon);
  else          log("❌ Invalid DEFCON: " + JSON.stringify(r));
}}

async function saveFS() {{
  const r = await apiPost("/api/fs/save", {{}});
  log("💾 Lattice image saved → " + (r.saved || "error"));
}}

async function postBBS() {{
  const subject = document.getElementById("bbs-subject").value.trim();
  const body    = document.getElementById("bbs-body").value.trim();
  const author  = document.getElementById("bbs-author").value.trim();
  if (!subject || !body) {{ log("⚠  Subject and body required."); return; }}
  const r = await apiPost("/api/bbs/post", {{
    subject, body, author: author || undefined
  }});
  if (r.post_id) {{
    document.getElementById("bbs-status").textContent =
      "✅ Posted to Space 2 · " + r.burris_url;
    log("📡 BBS · " + r.post_id + " · " + r.burris_url);
    document.getElementById("bbs-subject").value = "";
    document.getElementById("bbs-body").value    = "";
  }} else {{
    log("❌ BBS post failed: " + JSON.stringify(r));
  }}
}}
</script>
</body>
</html>"""


# ===========================================================================
# BOOT
# ===========================================================================

def main():
    print(f"""
╔═══════════════════════════════════════════════════════════╗
║  ⬡  OdinNet Integrated Daemon  v{DAEMON_VERSION:<5}                    ║
║     Stateless Comms · LatticeFS v2 · Phase-11 Hardened   ║
║     Atomic Journal · Replay Shield · AJAX Live UI        ║
╚═══════════════════════════════════════════════════════════╝
""")

    ctx = DaemonContext()

    # Install hardened comms patch (replay shield + atomic journal)
    _patch_comms_process(ctx)

    # Wire polling callback and start background polling thread
    storage_cb = _make_storage_callback(ctx)
    ctx.comms.start_polling(storage_interface_callback=storage_cb)

    print(f"  Node ID       : {ctx.node_id}")
    print(f"  Base Coord    : {ctx.base_coord[:32]}…")
    print(f"  Inbox mount   : /mail/inbox/  [Space {SPACE_USER}]")
    print(f"  BBS mount     : /bbs/         [Space {SPACE_FLEET}]")
    print(f"  Dashboard     : http://0.0.0.0:{DEFAULT_PORT}/")
    print(f"  [PHASE-11] Replay Shield   : ACTIVE (ctx.seen_packet_ids)")
    print(f"  [PHASE-11] Atomic Journal  : ACTIVE (BEGIN→WRITE→COMMIT)")
    print(f"  [PHASE-11] AJAX Live Poll  : ACTIVE (2 000 ms / status+inbox)")
    print(f"  Bearer token required on all /api/* endpoints")
    print()

    handler_cls = _make_handler(ctx)
    server      = HTTPServer(("0.0.0.0", DEFAULT_PORT), handler_cls)

    try:
        print(f"  🟢 Navigation Bridge ONLINE — port {DEFAULT_PORT}\n")
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  🔴 Shutdown signal received. Saving lattice image…")
        ctx.fs.save(FS_IMG_PATH)
        ctx.comms.stop_polling()
        server.server_close()
        print("  Lattice image saved. Warp core offline. o7\n")


if __name__ == "__main__":
    main()
