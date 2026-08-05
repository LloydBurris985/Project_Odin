"""
OdinNet Security System — DEFCON + Beacon Reputation + Fleet Jump Protocol
===========================================================================

Changes from previous version:
  - fleet_jump(new_r, reason)  — Defcon-gated universe R relocation
      1. Expel all bad-actor beacons via enforce_beacons() + blacklist
      2. Persist a signed jump manifest (jump_manifest.json)
      3. Broadcast new R to all trusted beacons via beacon records
      4. Old R=1 coordinates remain readable — migration path preserved
  - status_dict() now includes current_r, jump_count, last_jump fields
  - FleetJumpManifest — persisted record of each jump event
  - JUMP_MANIFEST_FILE = "jump_manifest.json"

Concurrency hardening (Council finding C2, Step 5 review):
  - BeaconReputation._save(), FleetJumpManifest._save(), and
    OdinNetSecurity._save() all do read-modify-write JSON persistence.
    ThreadingHTTPServer spawns a new thread per HTTP request, so concurrent
    calls into /api/security/* endpoints (raise/lower/jump/ban/etc.) could
    previously race on the same in-memory dict before either _save() landed,
    corrupting or losing history entries.
  - Fix: each of the three classes now owns its own threading.RLock,
    guarding every method that reads or mutates its persisted state.
    Three separate locks (not one shared lock) because the three classes
    manage three independent files/state — no reason to serialize a
    reputation update behind an unrelated jump-manifest write.
  - RLock (not Lock) because OdinNetSecurity.fleet_jump() calls
    self.raise_defcon() internally on the same thread, and
    OdinNetSecurity.declare_attack() calls self.raise_defcon() the same way.
"""

import json
import os
import math
import threading
import time
from datetime import datetime

SECURITY_FILE    = "odinnet_security.json"
BLACKLIST_FILE   = "beacon_blacklist.json"
REPUTATION_FILE  = "beacon_reputation.json"
JUMP_MANIFEST_FILE = "jump_manifest.json"

DEFCON_LEVELS = {
    1: {
        "label": "NORMAL",
        "description": "Standard operation. Dummy beacons allowed. Basic encryption.",
        "dummy_beacons": True, "server_beacons": True,
        "min_reputation": 20, "drop_reputation": 0,
        "require_encryption": False, "aes256_required": False,
        "beacon_rotate": False, "temporal_allowed": True,
        "realtime_allowed": True, "polling_encrypted": False,
        "color": "🟢",
    },
    3: {
        "label": "ELEVATED",
        "description": "Increased monitoring. Low-rep beacons flagged.",
        "dummy_beacons": True, "server_beacons": True,
        "min_reputation": 40, "drop_reputation": 10,
        "require_encryption": False, "aes256_required": False,
        "beacon_rotate": False, "temporal_allowed": True,
        "realtime_allowed": True, "polling_encrypted": False,
        "color": "🟡",
    },
    5: {
        "label": "GUARDED",
        "description": "Personal encryption required. Low-rep beacons expelled.",
        "dummy_beacons": True, "server_beacons": True,
        "min_reputation": 60, "drop_reputation": 30,
        "require_encryption": True, "aes256_required": False,
        "beacon_rotate": False, "temporal_allowed": True,
        "realtime_allowed": True, "polling_encrypted": True,
        "color": "🟠",
    },
    7: {
        "label": "HIGH ALERT",
        "description": "Server beacons only. AES-256 all traffic. Dynamic rotation.",
        "dummy_beacons": False, "server_beacons": True,
        "min_reputation": 75, "drop_reputation": 50,
        "require_encryption": True, "aes256_required": True,
        "beacon_rotate": True, "temporal_allowed": True,
        "realtime_allowed": True, "polling_encrypted": True,
        "color": "🔴",
    },
    10: {
        "label": "MAXIMUM SECURITY",
        "description": "Military grade. User-run server beacons only. Full blacklist active.",
        "dummy_beacons": False, "server_beacons": True,
        "min_reputation": 90, "drop_reputation": 75,
        "require_encryption": True, "aes256_required": True,
        "beacon_rotate": True, "temporal_allowed": True,
        "realtime_allowed": True, "polling_encrypted": True,
        "color": "🚨",
    },
}

_DEFCON_STEPS = sorted(DEFCON_LEVELS.keys())

# Minimum DEFCON required to issue a Fleet Jump command.
# Below this level, jump is blocked (no reason to change universe at peace).
FLEET_JUMP_MIN_DEFCON = 3


def nearest_defcon(level: int) -> int:
    return min(_DEFCON_STEPS, key=lambda x: abs(x - level))

def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _load_json_safe(path: str, default) -> dict:
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default

def _save_json(path: str, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ===========================================================================
# BeaconReputation  (concurrency-hardened: internal RLock added)
# ===========================================================================

class BeaconReputation:
    PENALTY_DECODE_FAIL   = 5
    PENALTY_HASH_MISMATCH = 10
    PENALTY_ANOMALY       = 15
    PENALTY_MANUAL_FLAG   = 30
    PENALTY_UNDER_ATTACK  = 25
    RECOVERY_PER_POLL     = 1

    def __init__(self, filename: str = REPUTATION_FILE):
        self.filename = filename
        self._lock    = threading.RLock()
        self._data    = _load_json_safe(filename, {})

    def _save(self):
        # Callers already hold self._lock; kept private/unlocked to avoid
        # a second acquire on an RLock we already own.
        _save_json(self.filename, self._data)

    def _record(self, name: str) -> dict:
        if name not in self._data:
            self._data[name] = {
                "name": name, "score": 100, "expelled": False,
                "blacklisted": False, "events": [],
                "first_seen": _now_str(), "last_updated": _now_str(),
                "successful_polls": 0, "total_events": 0,
            }
        return self._data[name]

    def get_score(self, name: str) -> int:
        with self._lock:
            return self._record(name)["score"]

    def is_expelled(self, name: str) -> bool:
        with self._lock:
            return self._record(name).get("expelled", False)

    def is_blacklisted(self, name: str) -> bool:
        with self._lock:
            return self._record(name).get("blacklisted", False)

    def _apply_penalty(self, name: str, amount: int, reason: str):
        with self._lock:
            rec = self._record(name)
            if rec["expelled"]:
                return
            rec["score"] = max(0, rec["score"] - amount)
            rec["total_events"] += 1
            rec["last_updated"]  = _now_str()
            rec["events"].append({"time": _now_str(), "type": reason,
                                   "penalty": -amount, "score": rec["score"]})
            rec["events"] = rec["events"][-20:]
            print(f" [Reputation] {name} -{amount}pts ({reason}) → score={rec['score']}")
            self._save()

    def report_decode_fail(self, name: str):
        self._apply_penalty(name, self.PENALTY_DECODE_FAIL, "decode_fail")

    def report_hash_mismatch(self, name: str):
        self._apply_penalty(name, self.PENALTY_HASH_MISMATCH, "hash_mismatch")

    def report_anomaly(self, name: str):
        self._apply_penalty(name, self.PENALTY_ANOMALY, "anomaly")

    def manual_flag(self, name: str, reason: str = "operator"):
        self._apply_penalty(name, self.PENALTY_MANUAL_FLAG, f"manual_flag:{reason}")

    def report_attack(self, name: str):
        self._apply_penalty(name, self.PENALTY_UNDER_ATTACK, "under_attack")

    def record_success(self, name: str):
        with self._lock:
            rec = self._record(name)
            if rec["expelled"]:
                return
            rec["score"] = min(100, rec["score"] + self.RECOVERY_PER_POLL)
            rec["successful_polls"] += 1
            rec["last_updated"] = _now_str()
            self._save()

    def expel(self, name: str, reason: str = "score_below_threshold"):
        with self._lock:
            rec = self._record(name)
            rec["expelled"]    = True
            rec["blacklisted"] = True
            rec["expel_reason"] = reason
            rec["expelled_at"]  = _now_str()
            rec["last_updated"] = _now_str()
            print(f" ⛔ Beacon EXPELLED + BLACKLISTED: {name} reason={reason}")
            self._save()

    def enforce_defcon(self, defcon: int, beacon_list: list) -> tuple[list, list]:
        with self._lock:
            cfg      = DEFCON_LEVELS.get(nearest_defcon(defcon), DEFCON_LEVELS[1])
            drop_at  = cfg["drop_reputation"]
            warn_at  = cfg["min_reputation"]
            no_dummy = not cfg["dummy_beacons"]

            approved, expelled = [], []
            for b in beacon_list:
                name = b.get("name", "?")
                if self.is_blacklisted(name):
                    expelled.append(b)
                    continue
                is_dummy = not b.get("is_server", False)
                if no_dummy and is_dummy:
                    print(f" [DefCon {defcon}] Dummy beacon purged: {name}")
                    expelled.append(b)
                    continue
                score = self.get_score(name)
                if score < drop_at:
                    self.expel(name, reason=f"score_{score}_below_defcon{defcon}_drop_{drop_at}")
                    expelled.append(b)
                elif score < warn_at:
                    print(f" ⚠ Beacon '{name}' score {score} below warn threshold {warn_at}")
                    approved.append(b)
                else:
                    approved.append(b)
            return approved, expelled

    def print_summary(self):
        with self._lock:
            border = "─" * 62
            print(f"\n {border}")
            print(f" ⬡ BEACON REPUTATION TABLE — {len(self._data)} beacon(s)")
            print(f" {border}")
            print(f" {'NAME':<28} {'SCORE':>5} {'EXPELLED':>9} {'POLLS':>6} STATUS")
            print(f" {'─'*28} {'─'*5} {'─'*9} {'─'*6} ──────")
            for name, rec in sorted(self._data.items()):
                status = "⛔ EXPELLED" if rec["expelled"] else (
                    "⚠ WARN" if rec["score"] < 50 else "✅ OK"
                )
                print(f" {name:<28} {rec['score']:>5} "
                      f"{'YES' if rec['expelled'] else 'no':>9} "
                      f"{rec['successful_polls']:>6} {status}")
            print(f" {border}\n")


# ===========================================================================
# FleetJumpManifest  — persisted record of each jump event
# (concurrency-hardened: internal RLock added)
# ===========================================================================

class FleetJumpManifest:
    """
    Persists the history of Fleet Jump events to jump_manifest.json.

    Each jump record contains:
      - jump_id       : sequential jump number (1, 2, 3…)
      - timestamp     : when the jump was issued
      - old_r         : previous R value (always 1 on first jump)
      - new_r         : new R value after jump
      - reason        : operator-provided reason string
      - defcon        : DEFCON level at time of jump
      - expelled      : list of expelled beacon names
      - trusted       : list of trusted beacon names that received the new R
      - migration_note: reminder that old coordinates remain readable

    Migration path:
      Old R=1 coordinates are NEVER deleted.
      Any node that missed the jump can re-read old-universe messages by
      instantiating ChartGenerator with R=1 (the default).
      New messages after the jump use the new R.
    """

    MIGRATION_NOTE = (
        "Old R=1 coordinates remain readable. "
        "Instantiate ChartGenerator(R=old_r) to decode pre-jump messages. "
        "New messages use new_r."
    )

    def __init__(self, filename: str = JUMP_MANIFEST_FILE):
        self.filename = filename
        self._lock    = threading.RLock()
        self._data    = _load_json_safe(filename, {"jumps": []})

    def _save(self):
        # Callers already hold self._lock.
        _save_json(self.filename, self._data)

    def record_jump(
        self,
        old_r:    int,
        new_r:    int,
        reason:   str,
        defcon:   int,
        expelled: list,
        trusted:  list,
    ) -> dict:
        with self._lock:
            jump_id = len(self._data["jumps"]) + 1
            record  = {
                "jump_id":        jump_id,
                "timestamp":      _now_str(),
                "old_r":          str(old_r),
                "new_r":          str(new_r),
                "reason":         reason,
                "defcon":         defcon,
                "expelled_count": len(expelled),
                "expelled":       [b.get("name", "?") for b in expelled],
                "trusted_count":  len(trusted),
                "trusted":        [b.get("name", "?") for b in trusted],
                "migration_note": self.MIGRATION_NOTE,
            }
            self._data["jumps"].append(record)
            self._save()
            return record

    @property
    def jump_count(self) -> int:
        with self._lock:
            return len(self._data.get("jumps", []))

    @property
    def last_jump(self) -> dict | None:
        with self._lock:
            jumps = self._data.get("jumps", [])
            return jumps[-1] if jumps else None

    @property
    def current_r(self) -> int:
        """Return the R value from the most recent jump, or 1 (default stable R)."""
        with self._lock:
            lj = self.last_jump
            if lj:
                return int(lj["new_r"])
            return 1

    def print_history(self):
        with self._lock:
            jumps = self._data.get("jumps", [])
            border = "═" * 66
            print(f"\n{border}")
            print(f" ⬡  FLEET JUMP MANIFEST  —  {len(jumps)} jump(s)")
            print(f"{border}")
            if not jumps:
                print("  No jumps recorded. Fleet is in home universe (R=1).")
            for j in jumps:
                print(f"\n  Jump #{j['jump_id']}  [{j['timestamp']}]")
                print(f"    DEFCON   : {j['defcon']}")
                print(f"    Reason   : {j['reason']}")
                print(f"    Old R    : {str(j['old_r'])[:40]}...")
                print(f"    New R    : {str(j['new_r'])[:40]}...")
                print(f"    Expelled : {j['expelled_count']}  {j['expelled']}")
                print(f"    Trusted  : {j['trusted_count']}  {j['trusted']}")
                print(f"    Migration: {j['migration_note']}")
            print(f"\n{border}\n")


# ===========================================================================
# OdinNetSecurity  — adds fleet_jump() and jump manifest support
# (concurrency-hardened: internal RLock added)
# ===========================================================================

class OdinNetSecurity:
    RECOVERY_STEPS        = [10, 7, 5, 3, 1]
    RECOVERY_INTERVAL_SEC = 300
    BLACKLIST_BAN_MSG     = "BANNED: coordinate blacklisted"

    def __init__(
        self,
        coord_file:      str = "coordinatefile.json",
        security_file:   str = SECURITY_FILE,
        blacklist_file:  str = BLACKLIST_FILE,
        reputation_file: str = REPUTATION_FILE,
        manifest_file:   str = JUMP_MANIFEST_FILE,
    ):
        self.coord_file     = coord_file
        self.security_file  = security_file
        self.blacklist_file = blacklist_file
        self._lock           = threading.RLock()
        self.reputation     = BeaconReputation(reputation_file)
        self.manifest       = FleetJumpManifest(manifest_file)

        self._state = _load_json_safe(security_file, {
            "defcon": 1, "defcon_history": [],
            "attack_active": False, "last_attack": None,
            "last_recovery": None, "clean_beacons": [],
            "created": _now_str(),
        })
        self._blacklist = _load_json_safe(blacklist_file, {"banned": []})
        self._save()

    def _save(self):
        # Callers already hold self._lock (or are __init__, single-threaded).
        _save_json(self.security_file, self._state)
        _save_json(self.blacklist_file, self._blacklist)

    @property
    def defcon(self) -> int:
        with self._lock:
            return self._state["defcon"]

    @property
    def defcon_config(self) -> dict:
        return DEFCON_LEVELS[nearest_defcon(self.defcon)]

    # ── DEFCON management ─────────────────────────────────────────────────

    def raise_defcon(self, level: int, reason: str = "manual"):
        with self._lock:
            level = max(1, min(10, level))
            level = nearest_defcon(level)
            old   = self._state["defcon"]
            if level <= old:
                print(f" [Security] DEFCON already at {old} — not raising to {level}.")
                return
            self._state["defcon"]        = level
            self._state["attack_active"] = True
            self._state["last_attack"]   = _now_str()
            self._state["defcon_history"].append(
                {"from": old, "to": level, "reason": reason, "time": _now_str()}
            )
            self._save()
            cfg = self.defcon_config
            print(f"\n 🚨 DEFCON RAISED: {old} → {level} [{cfg['label']}]")
            print(f" Reason : {reason}")
            print(f" {cfg['description']}")
            self._print_defcon_rules(level)

    def lower_defcon(self, level: int = None, reason: str = "manual"):
        with self._lock:
            current = self._state["defcon"]
            if level is None:
                lower_steps = [s for s in _DEFCON_STEPS if s < current]
                level = lower_steps[-1] if lower_steps else 1
            level = max(1, min(current - 1, level))
            level = nearest_defcon(level)
            if level >= current:
                print(f" [Security] Already at DEFCON {current} — cannot lower to {level}.")
                return
            self._state["defcon"]        = level
            self._state["last_recovery"] = _now_str()
            if level == 1:
                self._state["attack_active"] = False
            self._state["defcon_history"].append(
                {"from": current, "to": level, "reason": reason, "time": _now_str()}
            )
            self._save()
            cfg = DEFCON_LEVELS[nearest_defcon(level)]
            print(f"\n ✅ DEFCON LOWERED: {current} → {level} [{cfg['label']}]")
            print(f" Reason : {reason}")

    def declare_attack(self, beacon_name: str = None, detail: str = "unknown"):
        with self._lock:
            print(f"\n ⚠ ATTACK DECLARED detail='{detail}'")
            new_defcon = min(10, nearest_defcon(self.defcon + 2))
            self.raise_defcon(new_defcon, reason=f"attack:{detail}")
            if beacon_name:
                self.reputation.report_attack(beacon_name)

    def attack_cleared(self, reason: str = "threat_subsided"):
        with self._lock:
            print(f"\n ✅ ATTACK CLEARED reason='{reason}'")
            self._state["attack_active"] = False
            self._save()
            self.lower_defcon(reason=f"recovery:{reason}")

    # ── Blacklist / ban ───────────────────────────────────────────────────

    def ban(self, identifier: str, reason: str = "unknown"):
        with self._lock:
            banned = self._blacklist.get("banned", [])
            if identifier not in banned:
                banned.append(identifier)
                self._blacklist["banned"] = banned
                self._save()
                print(f" ⛔ BANNED: {identifier} reason={reason}")
            else:
                print(f" [Blacklist] Already banned: {identifier}")

    def is_banned(self, identifier: str) -> bool:
        with self._lock:
            return identifier in self._blacklist.get("banned", [])

    def list_banned(self):
        with self._lock:
            banned = self._blacklist.get("banned", [])
            if not banned:
                print(" [Blacklist] Empty.")
                return
            print(f"\n ⛔ BANNED ENTITIES ({len(banned)}):")
            for b in banned:
                print(f" {b}")

    # ── Beacon enforcement ────────────────────────────────────────────────

    def enforce_beacons(self, beacon_list: list) -> tuple[list, list]:
        with self._lock:
            approved, expelled = self.reputation.enforce_defcon(self.defcon, beacon_list)
            final_approved = []
            for b in approved:
                name  = b.get("name", "")
                coord = str(b.get("coordinate", ""))
                if self.is_banned(name) or self.is_banned(coord):
                    print(f" ⛔ Blacklisted beacon blocked: {name}")
                    expelled.append(b)
                else:
                    final_approved.append(b)
            return final_approved, expelled

    def share_clean_beacons(self, beacons: list):
        with self._lock:
            approved, _ = self.enforce_beacons(beacons)
            self._state["clean_beacons"] = [b.get("name", "") for b in approved]
            self._save()
            print(f" [Security] {len(approved)} clean beacon(s) ready to share.")

    # ── Message policy ────────────────────────────────────────────────────

    def check_message_allowed(
        self, msg_type: str = "temporal", encrypted: bool = False, from_server: bool = True
    ) -> tuple[bool, str]:
        cfg = self.defcon_config
        if msg_type == "temporal" and not cfg["temporal_allowed"]:
            return False, f"Temporal messages suspended at DEFCON {self.defcon}"
        if msg_type == "realtime" and not cfg["realtime_allowed"]:
            return False, f"Realtime messages suspended at DEFCON {self.defcon}"
        if cfg["require_encryption"] and not encrypted:
            return False, f"Encryption required at DEFCON {self.defcon}"
        return True, "OK"

    def get_required_encryption(self) -> str:
        cfg = self.defcon_config
        if cfg["aes256_required"]:
            return "AES-256-GCM"
        if cfg["require_encryption"]:
            return "AES-128 or better"
        return "optional"

    # ── Fleet Jump ────────────────────────────────────────────────────────

    def fleet_jump(
        self,
        new_r:       int,
        reason:      str  = "defcon_command",
        beacon_list: list = None,
        force:       bool = False,
    ) -> dict:
        """
        Execute a Fleet Jump: relocate the universe reference axis R for all
        trusted nodes.

        Protocol
        --------
        1. Gate check: DEFCON must be >= FLEET_JUMP_MIN_DEFCON (default 3)
           unless force=True.
        2. Expel all bad-actor beacons via enforce_beacons() + blacklist.
           Bad actors are locked out before the new R is announced.
        3. Persist the jump manifest (jump_manifest.json) with full audit trail.
        4. Broadcast: update beacon records with new_r field so trusted nodes
           can pick it up on their next poll.
        5. Migration: old_r is recorded in the manifest; old R=1 coordinates
           remain readable by instantiating ChartGenerator(R=old_r).

        Whole sequence (gate check → expel → manifest write → broadcast →
        DEFCON raise) runs under self._lock so a concurrent
        /api/security/jump or /api/security/raise call from another request
        thread can't interleave mid-jump.

        Parameters
        ----------
        new_r       : new integer R value for the whole fleet
        reason      : human-readable jump reason (logged in manifest)
        beacon_list : current beacon list (from grok_comms._load_beacons())
                      If None, the jump is recorded but no beacons are updated.
        force       : bypass DEFCON gate (operator override)

        Returns
        -------
        dict with keys:
          ok, new_r, old_r, expelled_count, expelled, trusted_count, trusted,
          jump_id, defcon, migration_note

        Raises
        ------
        ValueError  if DEFCON gate blocks the jump (and force=False)
        ValueError  if new_r == old_r
        """
        with self._lock:
            border = "★" * 64

            # ── Gate: DEFCON check ─────────────────────────────────────────────
            if not force and self.defcon < FLEET_JUMP_MIN_DEFCON:
                msg = (
                    f"Fleet Jump blocked: DEFCON {self.defcon} < "
                    f"minimum {FLEET_JUMP_MIN_DEFCON}. "
                    f"Raise DEFCON first, or pass force=True."
                )
                print(f"\n ⛔ {msg}")
                raise ValueError(msg)

            old_r = self.manifest.current_r

            if new_r == old_r:
                msg = f"Fleet Jump aborted: new_r ({new_r}) == old_r ({old_r}). No change."
                print(f"\n ⚠  {msg}")
                raise ValueError(msg)

            print(f"\n{border}")
            print(f" ⬡  FLEET JUMP INITIATED")
            print(f" Reason   : {reason}")
            print(f" Old R    : {old_r}")
            print(f" New R    : {new_r}")
            print(f" DEFCON   : {self.defcon}")
            print(f"{border}")

            # ── Step 1: Expel bad actors ───────────────────────────────────────
            print(f"\n [Jump] Step 1 — Expelling bad actors before jump...")
            if beacon_list is None:
                beacon_list = []
            trusted, expelled = self.enforce_beacons(beacon_list)

            print(f" [Jump] Expelled : {len(expelled)}  Trusted : {len(trusted)}")
            if expelled:
                for b in expelled:
                    name = b.get("name", "?")
                    print(f"   ⛔ Expelled: {name}")
                    # Also add to hard blacklist so they cannot rejoin after jump
                    self.ban(name, reason=f"expelled_before_jump_{reason}")

            # ── Step 2: Persist jump manifest ─────────────────────────────────
            print(f"\n [Jump] Step 2 — Persisting jump manifest...")
            record = self.manifest.record_jump(
                old_r    = old_r,
                new_r    = new_r,
                reason   = reason,
                defcon   = self.defcon,
                expelled = expelled,
                trusted  = trusted,
            )
            print(f" [Jump] Manifest saved → {self.manifest.filename}  "
                  f"(jump #{record['jump_id']})")

            # ── Step 3: Broadcast to trusted beacons ──────────────────────────
            # We annotate each trusted beacon record with the new_r and jump_id
            # so beacon pollers on other nodes can pick it up.
            print(f"\n [Jump] Step 3 — Broadcasting new R to {len(trusted)} trusted beacon(s)...")
            broadcast_count = 0
            for b in trusted:
                b["fleet_new_r"]   = str(new_r)
                b["fleet_jump_id"] = record["jump_id"]
                b["fleet_reason"]  = reason
                b["fleet_ts"]      = _now_str()
                broadcast_count   += 1
                name = b.get("name", "?")
                print(f"   ✅ Broadcast → {name}")

            # ── Step 4: DEFCON raise (jump is a security event) ───────────────
            # Optionally raise DEFCON by 1 step during the jump window
            # (fleet is briefly vulnerable while nodes migrate)
            if self.defcon < 10:
                jump_defcon = nearest_defcon(self.defcon + 1)
                self.raise_defcon(jump_defcon, reason=f"fleet_jump_in_progress:{reason}")
                print(f" [Jump] DEFCON raised to {jump_defcon} for jump window")

            # ── Result ─────────────────────────────────────────────────────────
            result = {
                "ok":              True,
                "jump_id":         record["jump_id"],
                "old_r":           old_r,
                "new_r":           new_r,
                "reason":          reason,
                "defcon":          self.defcon,
                "expelled_count":  len(expelled),
                "expelled":        [b.get("name", "?") for b in expelled],
                "trusted_count":   len(trusted),
                "trusted":         [b.get("name", "?") for b in trusted],
                "broadcast_count": broadcast_count,
                "migration_note":  FleetJumpManifest.MIGRATION_NOTE,
                "manifest_file":   self.manifest.filename,
                "timestamp":       record["timestamp"],
            }

            print(f"\n{border}")
            print(f" ⬡  FLEET JUMP COMPLETE  (jump #{record['jump_id']})")
            print(f" Old universe (R={old_r}) : abandoned — cylons eat dust.")
            print(f" New universe (R={new_r}) : active")
            print(f" Expelled : {len(expelled)}  Trusted : {len(trusted)}")
            print(f" Migration: {FleetJumpManifest.MIGRATION_NOTE}")
            print(f"{border}\n")

            return result

    # ── Helpers ───────────────────────────────────────────────────────────

    def _print_defcon_rules(self, level: int):
        cfg = DEFCON_LEVELS[nearest_defcon(level)]
        print(f"\n ┌── DEFCON {level} RULES {'─'*40}")
        print(f" │ Dummy beacons  : {'✅ allowed' if cfg['dummy_beacons'] else '⛔ BANNED'}")
        print(f" │ Server beacons : {'✅ required' if cfg['server_beacons'] else '—'}")
        print(f" │ Encryption     : {self.get_required_encryption()}")
        print(f" │ AES-256 traffic: {'🔒 YES' if cfg['aes256_required'] else 'optional'}")
        print(f" │ Beacon rotate  : {'✅ YES' if cfg['beacon_rotate'] else '—'}")
        print(f" │ Polling encrypt: {'🔒 YES' if cfg['polling_encrypted'] else '—'}")
        print(f" └{'─'*51}")

    def status_display(self):
        with self._lock:
            border = "═" * 62
            cfg    = self.defcon_config
            print(f"\n{border}")
            print(f" {cfg['color']} ODINNET SECURITY STATUS")
            print(f"{border}")
            print(f" DEFCON Level  : {self.defcon} [{cfg['label']}]")
            print(f" Description   : {cfg['description']}")
            print(f" Attack active : {'⚠ YES' if self._state['attack_active'] else '✅ no'}")
            print(f" Last attack   : {self._state.get('last_attack', '—')}")
            print(f" Last recovery : {self._state.get('last_recovery', '—')}")
            print(f" Current R     : {self.manifest.current_r}  "
                  f"(jumps={self.manifest.jump_count})")
            print(f"{'-'*62}")
            self._print_defcon_rules(self.defcon)
            print(f"{'-'*62}")
            print(f" Clean beacons : {len(self._state.get('clean_beacons', []))}")
            print(f" Banned entries: {len(self._blacklist.get('banned', []))}")
            history = self._state.get("defcon_history", [])[-5:]
            if history:
                print(f"{'-'*62}")
                print(f" DEFCON HISTORY (last {len(history)}):")
                for ev in history:
                    print(f" [{ev['time']}] {ev['from']} → {ev['to']} ({ev['reason']})")
            if self.manifest.jump_count:
                print(f"{'-'*62}")
                lj = self.manifest.last_jump
                print(f" LAST FLEET JUMP: #{lj['jump_id']}  [{lj['timestamp']}]")
                print(f"   R: {str(lj['old_r'])[:20]}… → {str(lj['new_r'])[:20]}…")
                print(f"   Expelled={lj['expelled_count']}  Trusted={lj['trusted_count']}")
            print(f"{border}\n")

    def status_dict(self) -> dict:
        """
        Extended status dict — includes fleet jump fields for dashboard.
        New fields: current_r, jump_count, last_jump.
        """
        with self._lock:
            cfg = self.defcon_config
            lj  = self.manifest.last_jump
            return {
                "defcon":          self.defcon,
                "defcon_label":    cfg["label"],
                "defcon_color":    cfg["color"],
                "attack_active":   self._state["attack_active"],
                "last_attack":     self._state.get("last_attack"),
                "encryption_req":  self.get_required_encryption(),
                "aes256_required": cfg["aes256_required"],
                "dummy_beacons":   cfg["dummy_beacons"],
                "beacon_rotate":   cfg["beacon_rotate"],
                "banned_count":    len(self._blacklist.get("banned", [])),
                # Fleet jump fields (new)
                "current_r":       self.manifest.current_r,
                "jump_count":      self.manifest.jump_count,
                "last_jump":       lj["timestamp"] if lj else None,
            }

    def interactive_menu(self):
        print("\n" + "★" * 62)
        print(" ODINNET SECURITY — DEFCON CONTROL")
        print("★" * 62)
        print(" Commands: status | raise <n> | lower [n] | attack [beacon]")
        print(" cleared | ban <id> | banned | reputation | jump <new_r>")
        print(" jumphistory | exit\n")

        while True:
            try:
                raw = input("security> ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\n[Security] Console closed.")
                break
            if not raw:
                continue
            parts = raw.split()
            cmd   = parts[0].lower()

            if cmd in ("exit", "quit"):
                break
            elif cmd == "status":
                self.status_display()
            elif cmd == "raise":
                level = int(parts[1]) if len(parts) > 1 else self.defcon + 1
                self.raise_defcon(level, reason="operator")
            elif cmd == "lower":
                level = int(parts[1]) if len(parts) > 1 else None
                self.lower_defcon(level, reason="operator")
            elif cmd == "attack":
                beacon = parts[1] if len(parts) > 1 else None
                detail = " ".join(parts[2:]) if len(parts) > 2 else "reported"
                self.declare_attack(beacon, detail)
            elif cmd == "cleared":
                self.attack_cleared(reason="operator_cleared")
            elif cmd == "ban":
                if len(parts) < 2:
                    print(" Usage: ban <identifier>")
                    continue
                self.ban(parts[1], reason="operator")
            elif cmd == "banned":
                self.list_banned()
            elif cmd == "reputation":
                self.reputation.print_summary()
            elif cmd == "jump":
                if len(parts) < 2:
                    print(" Usage: jump <new_r_integer>")
                    continue
                try:
                    new_r  = int(parts[1])
                    reason = " ".join(parts[2:]) or "operator_jump"
                    # Load beacons from disk if available
                    try:
                        from grok_comms import _load_beacons
                        beacons = _load_beacons()
                    except ImportError:
                        beacons = []
                    self.fleet_jump(new_r, reason=reason, beacon_list=beacons)
                except ValueError as e:
                    print(f" ⚠ {e}")
                except Exception as e:
                    print(f" ❌ Jump error: {e}")
            elif cmd == "jumphistory":
                self.manifest.print_history()
            else:
                print(" Commands: status | raise <n> | lower [n] | attack [beacon]")
                print(" cleared | ban <id> | banned | reputation | jump <new_r>")
                print(" jumphistory | exit")


# ===========================================================================
# OllamaStub  (unchanged)
# ===========================================================================

class OllamaStub:
    STUB_RESPONSES = {
        "threat_analysis":   "[Ollama STUB] Threat analysis not yet wired to a local model.",
        "classify_beacon":   "[Ollama STUB] Beacon classification pending Ollama integration.",
        "summarise_traffic": "[Ollama STUB] Traffic summary pending Ollama integration.",
    }

    def __init__(self, model: str = "llama3", host: str = "localhost", port: int = 11434):
        self.model     = model
        self.host      = host
        self.port      = port
        self.available = False
        print(f" [Ollama] Stub initialised model={model} "
              f"endpoint=http://{host}:{port}/api/generate")
        print(" [Ollama] Status: STUB — not connected to live model.")

    def _query(self, prompt: str) -> str:
        return "[Ollama STUB] Model not connected."

    def threat_analysis(self, context: str) -> str:
        if not self.available:
            return self.STUB_RESPONSES["threat_analysis"]
        return self._query(f"Analyse threat: {context}")

    def classify_beacon(self, beacon_name: str, events: list) -> str:
        if not self.available:
            return self.STUB_RESPONSES["classify_beacon"]
        return self._query(f"Classify beacon '{beacon_name}' events: {events}")

    def summarise_traffic(self, activity_tail: list) -> str:
        if not self.available:
            return self.STUB_RESPONSES["summarise_traffic"]
        return self._query("Summarise: " + "\n".join(activity_tail[-20:]))

    def status(self) -> dict:
        return {
            "available": self.available,
            "model":     self.model,
            "endpoint":  f"http://{self.host}:{self.port}/api/generate",
            "note":      "STUB — wire up _query() to activate",
        }


# ===========================================================================
# SELF-TESTS
# ===========================================================================

if __name__ == "__main__":
    import tempfile

    print("=" * 64)
    print(" OdinNet Security System — Self-Tests (v5 with Fleet Jump)")
    print("=" * 64)

    with tempfile.TemporaryDirectory() as tmp:
        sec_f  = os.path.join(tmp, "sec.json")
        bl_f   = os.path.join(tmp, "blacklist.json")
        rep_f  = os.path.join(tmp, "reputation.json")
        mani_f = os.path.join(tmp, "manifest.json")

        sec = OdinNetSecurity(
            security_file=sec_f, blacklist_file=bl_f,
            reputation_file=rep_f, manifest_file=mani_f,
        )

        print("\n[Test 1] Initial DEFCON state")
        assert sec.defcon == 1
        print(f" ✅ DEFCON={sec.defcon} label={sec.defcon_config['label']}")

        print("\n[Test 2] Raise → lower DEFCON")
        sec.raise_defcon(5, reason="test")
        assert sec.defcon == 5
        sec.lower_defcon(reason="test_recovery")
        assert sec.defcon < 5
        print(f" ✅ After raise→lower: DEFCON={sec.defcon}")

        print("\n[Test 3] Declare attack")
        sec._state["defcon"] = 1; sec._save()
        sec.declare_attack("beacon-A", "DDoS detected")
        assert sec.defcon > 1
        print(f" ✅ DEFCON raised to {sec.defcon} on attack")

        print("\n[Test 4] Beacon reputation penalties")
        rep = BeaconReputation(rep_f)
        rep.report_decode_fail("beacon-X")
        rep.report_hash_mismatch("beacon-X")
        score = rep.get_score("beacon-X")
        assert score < 100
        print(f" ✅ beacon-X score={score} (penalised)")
        rep.record_success("beacon-X")
        score2 = rep.get_score("beacon-X")
        assert score2 == score + 1
        print(f" ✅ recovery: score={score2}")

        print("\n[Test 5] Expel beacon")
        rep.expel("beacon-Y", reason="test_expel")
        assert rep.is_expelled("beacon-Y")
        assert rep.is_blacklisted("beacon-Y")
        print(" ✅ beacon-Y expelled + blacklisted")

        print("\n[Test 6] Enforce beacons at DEFCON 7")
        sec._state["defcon"] = 7; sec._save()
        beacons  = [
            {"name": "dummy-1", "coordinate": "12345", "is_server": False},
            {"name": "server-1", "coordinate": "67890", "is_server": True},
        ]
        approved, expelled = sec.enforce_beacons(beacons)
        assert "dummy-1" in [b["name"] for b in expelled]
        assert any(b["name"] == "server-1" for b in approved)
        print(" ✅ Dummy expelled, server approved")

        print("\n[Test 7] Blacklist ban")
        sec.ban("bad-node-42", reason="test")
        assert sec.is_banned("bad-node-42")
        assert not sec.is_banned("good-node-99")
        print(" ✅ Ban / not-banned working")

        print("\n[Test 8] Message policy at DEFCON 5")
        sec._state["defcon"] = 5; sec._save()
        ok, reason = sec.check_message_allowed("temporal", encrypted=False)
        assert not ok, "Should require encryption at DEFCON 5"
        ok2, _ = sec.check_message_allowed("temporal", encrypted=True)
        assert ok2
        print(" ✅ Unencrypted blocked at DEFCON 5, encrypted OK")

        print("\n[Test 9] Ollama stub")
        ai   = OllamaStub()
        resp = ai.threat_analysis("Beacon under DDoS")
        assert "STUB" in resp
        print(f" ✅ Ollama stub responds correctly")

        print("\n[Test 10] status_dict() includes fleet fields")
        d = sec.status_dict()
        assert "defcon" in d and "defcon_label" in d
        assert "current_r" in d and "jump_count" in d
        print(f" ✅ status_dict defcon={d['defcon']} current_r={d['current_r']} jumps={d['jump_count']}")

        print("\n[Test 11] Fleet Jump — DEFCON gate blocks jump at DEFCON 1")
        sec._state["defcon"] = 1; sec._save()
        try:
            sec.fleet_jump(42, reason="test_gate_block")
            assert False, "Should have raised ValueError"
        except ValueError as e:
            print(f" ✅ Gate correctly blocked: {e}")

        print("\n[Test 12] Fleet Jump — executes at DEFCON 3")
        sec._state["defcon"] = 3; sec._save()
        fleet_beacons = [
            {"name": "good-beacon", "coordinate": "999", "is_server": True},
            {"name": "bad-beacon",  "coordinate": "888", "is_server": False,
             "is_blacklisted": True},
        ]
        # Pre-blacklist bad-beacon via reputation
        sec.reputation.expel("bad-beacon", reason="pre_test")
        result = sec.fleet_jump(42, reason="test_jump", beacon_list=fleet_beacons)
        assert result["ok"]
        assert result["new_r"] == 42
        assert result["jump_id"] == 1
        assert sec.manifest.current_r == 42
        assert sec.manifest.jump_count == 1
        print(f" ✅ Fleet jump OK: new_r={result['new_r']} "
              f"expelled={result['expelled_count']} trusted={result['trusted_count']}")

        print("\n[Test 13] old_r=1 migration path confirmed in manifest")
        lj = sec.manifest.last_jump
        assert lj["old_r"] == "1"
        assert lj["new_r"] == "42"
        assert "R=1" in lj["migration_note"]
        print(f" ✅ Migration note: {lj['migration_note'][:60]}…")

        print("\n[Test 14] Fleet Jump — duplicate R rejected")
        try:
            sec.fleet_jump(42, reason="duplicate_test")
            assert False, "Should have raised ValueError"
        except ValueError as e:
            print(f" ✅ Duplicate R rejected: {e}")

        print("\n[Test 15] Jump history display")
        sec.manifest.print_history()

        sec.status_display()

        print("\n[Test 16] Concurrency — parallel raise/lower/ban do not corrupt state (C2 fix)")
        import concurrent.futures
        sec._state["defcon"] = 1; sec._save()
        sec._blacklist = {"banned": []}; sec._save()

        def _hammer(i):
            sec.ban(f"node-{i}", reason="concurrency_test")
            sec.reputation.report_decode_fail(f"beacon-hammer-{i % 4}")
            sec.reputation.record_success(f"beacon-hammer-{i % 4}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(_hammer, i) for i in range(40)]
            for f in futures:
                f.result()

        # All 40 unique ban identifiers must be present — a lost update
        # under the old unlocked read-modify-write would drop entries here.
        banned_after = set(sec._blacklist.get("banned", []))
        expected     = {f"node-{i}" for i in range(40)}
        assert expected.issubset(banned_after), (
            f"lost ban entries: missing {expected - banned_after}")
        print(f"  ✅ PASSED  (40 concurrent bans, {len(banned_after)} recorded, none lost)")

    print("\n✅ All OdinNet Security tests passed (v5 Fleet Jump).\n")
