"""
OdinNet Security System — DEFCON + Beacon Reputation Protocol
=============================================================
Implements Admiral Grok's security architecture:

  DEFCON Levels (1 = normal, 10 = maximum security):
    DEFCON 1   Normal operation. Dummy beacons OK. Basic encryption.
    DEFCON 3   Increased monitoring. Warn on low-reputation beacons.
    DEFCON 5   Elevated. Drop beacons below reputation threshold.
               Personal encryption required for all traffic.
    DEFCON 7   High alert. Server-run beacons only (no dummy beacons).
               Encrypted polling windows. AES-256 on all messages.
    DEFCON 10  Maximum. Server beacons only, military-grade passphrase
               required, all dummy beacons purged, blacklist active,
               realtime + temporal traffic encrypted end-to-end.
               Network deploys dynamic beacon rotation.

  Beacon Reputation:
    Each beacon has a reputation score 0–100 (starts at 100).
    Attacks detected → score drops.
    Score below DROP_THRESHOLD at current DEFCON → beacon expelled.
    Expelled beacons still pollable for salvage but not relayed.
    Clean beacons shared across all nodes automatically.

  Attack Detection:
    - Decode failure rate above threshold → score drops.
    - Hash mismatch rate above threshold → score drops.
    - Anomalous coordinate space behaviour → score drops.
    - Manual flag by node operator.

  Recovery:
    When attack stops, DEFCON slowly walks back toward 1.
    Expelled beacons never re-admitted (hash blacklisted).
    New clean beacons deployed from trusted node pool.

  Ollama Stub:
    Placeholder for local AI integration (local threat analysis).
"""

import json
import os
import math
import time
from datetime import datetime

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SECURITY_FILE    = "odinnet_security.json"
BLACKLIST_FILE   = "beacon_blacklist.json"
REPUTATION_FILE  = "beacon_reputation.json"


# ---------------------------------------------------------------------------
# DEFCON level definitions
# ---------------------------------------------------------------------------

DEFCON_LEVELS = {
    1: {
        "label":              "NORMAL",
        "description":        "Standard operation. Dummy beacons allowed. Basic encryption.",
        "dummy_beacons":      True,
        "server_beacons":     True,
        "min_reputation":     20,       # beacons below this are warned
        "drop_reputation":    0,        # beacons below this are expelled
        "require_encryption": False,
        "aes256_required":    False,
        "beacon_rotate":      False,
        "temporal_allowed":   True,
        "realtime_allowed":   True,
        "polling_encrypted":  False,
        "color":              "🟢",
    },
    3: {
        "label":              "ELEVATED",
        "description":        "Increased monitoring. Low-rep beacons flagged.",
        "dummy_beacons":      True,
        "server_beacons":     True,
        "min_reputation":     40,
        "drop_reputation":    10,
        "require_encryption": False,
        "aes256_required":    False,
        "beacon_rotate":      False,
        "temporal_allowed":   True,
        "realtime_allowed":   True,
        "polling_encrypted":  False,
        "color":              "🟡",
    },
    5: {
        "label":              "GUARDED",
        "description":        "Personal encryption required. Low-rep beacons expelled.",
        "dummy_beacons":      True,
        "server_beacons":     True,
        "min_reputation":     60,
        "drop_reputation":    30,
        "require_encryption": True,
        "aes256_required":    False,
        "beacon_rotate":      False,
        "temporal_allowed":   True,
        "realtime_allowed":   True,
        "polling_encrypted":  True,
        "color":              "🟠",
    },
    7: {
        "label":              "HIGH ALERT",
        "description":        "Server beacons only. AES-256 all traffic. Dynamic rotation.",
        "dummy_beacons":      False,
        "server_beacons":     True,
        "min_reputation":     75,
        "drop_reputation":    50,
        "require_encryption": True,
        "aes256_required":    True,
        "beacon_rotate":      True,
        "temporal_allowed":   True,
        "realtime_allowed":   True,
        "polling_encrypted":  True,
        "color":              "🔴",
    },
    10: {
        "label":              "MAXIMUM SECURITY",
        "description":        "Military grade. User-run server beacons only. Full blacklist active.",
        "dummy_beacons":      False,
        "server_beacons":     True,     # user-run only
        "min_reputation":     90,
        "drop_reputation":    75,
        "require_encryption": True,
        "aes256_required":    True,
        "beacon_rotate":      True,
        "temporal_allowed":   True,
        "realtime_allowed":   True,
        "polling_encrypted":  True,
        "color":              "🚨",
    },
}

# Nearest valid DEFCON level lookup
_DEFCON_STEPS = sorted(DEFCON_LEVELS.keys())

def nearest_defcon(level: int) -> int:
    """Round to nearest valid DEFCON step."""
    return min(_DEFCON_STEPS, key=lambda x: abs(x - level))


# ---------------------------------------------------------------------------
# Beacon reputation record
# ---------------------------------------------------------------------------

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
# BeaconReputation
# ===========================================================================

class BeaconReputation:
    """
    Per-beacon reputation tracker.

    Score: 0–100 (starts 100).
    Penalties:
        decode_fail   → -5 per event
        hash_mismatch → -10 per event
        anomaly       → -15 per event
        manual_flag   → -30 per event
        under_attack  → -25 per event (set by DEFCON system)

    Recovery:
        +1 per successful poll (up to 100).
        Expelled beacons are blacklisted — not re-admitted.
    """

    PENALTY_DECODE_FAIL   = 5
    PENALTY_HASH_MISMATCH = 10
    PENALTY_ANOMALY       = 15
    PENALTY_MANUAL_FLAG   = 30
    PENALTY_UNDER_ATTACK  = 25
    RECOVERY_PER_POLL     = 1

    def __init__(self, filename: str = REPUTATION_FILE):
        self.filename = filename
        self._data    = _load_json_safe(filename, {})

    def _save(self):
        _save_json(self.filename, self._data)

    def _record(self, name: str) -> dict:
        if name not in self._data:
            self._data[name] = {
                "name":           name,
                "score":          100,
                "expelled":       False,
                "blacklisted":    False,
                "events":         [],
                "first_seen":     _now_str(),
                "last_updated":   _now_str(),
                "successful_polls": 0,
                "total_events":   0,
            }
        return self._data[name]

    def get_score(self, name: str) -> int:
        return self._record(name)["score"]

    def is_expelled(self, name: str) -> bool:
        return self._record(name).get("expelled", False)

    def is_blacklisted(self, name: str) -> bool:
        return self._record(name).get("blacklisted", False)

    def _apply_penalty(self, name: str, amount: int, reason: str):
        rec = self._record(name)
        if rec["expelled"]:
            return
        rec["score"]       = max(0, rec["score"] - amount)
        rec["total_events"] += 1
        rec["last_updated"] = _now_str()
        rec["events"].append({
            "time":    _now_str(),
            "type":    reason,
            "penalty": -amount,
            "score":   rec["score"],
        })
        # Keep last 20 events
        rec["events"] = rec["events"][-20:]
        print(f"  [Reputation] {name}  -{amount}pts ({reason})  "
              f"→  score={rec['score']}")
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
        rec = self._record(name)
        if rec["expelled"]:
            return
        rec["score"]            = min(100, rec["score"] + self.RECOVERY_PER_POLL)
        rec["successful_polls"] += 1
        rec["last_updated"]     = _now_str()
        self._save()

    def expel(self, name: str, reason: str = "score_below_threshold"):
        """Expel a beacon from active use and blacklist it."""
        rec = self._record(name)
        rec["expelled"]     = True
        rec["blacklisted"]  = True
        rec["expel_reason"] = reason
        rec["expelled_at"]  = _now_str()
        rec["last_updated"] = _now_str()
        print(f"  ⛔ Beacon EXPELLED + BLACKLISTED: {name}  reason={reason}")
        self._save()

    def enforce_defcon(self, defcon: int, beacon_list: list) -> tuple[list, list]:
        """
        Apply current DEFCON thresholds to a list of beacon records.
        Returns (approved_beacons, expelled_beacons).
        """
        cfg = DEFCON_LEVELS.get(nearest_defcon(defcon), DEFCON_LEVELS[1])
        drop_at  = cfg["drop_reputation"]
        warn_at  = cfg["min_reputation"]
        no_dummy = not cfg["dummy_beacons"]

        approved, expelled = [], []
        for b in beacon_list:
            name = b.get("name", "?")

            # Already blacklisted
            if self.is_blacklisted(name):
                expelled.append(b)
                continue

            # Dummy beacon check
            is_dummy = not b.get("is_server", False)
            if no_dummy and is_dummy:
                print(f"  [DefCon {defcon}] Dummy beacon purged: {name}")
                expelled.append(b)
                continue

            # Score check
            score = self.get_score(name)
            if score < drop_at:
                self.expel(name, reason=f"score_{score}_below_defcon{defcon}_drop_{drop_at}")
                expelled.append(b)
            elif score < warn_at:
                print(f"  ⚠  Beacon '{name}' score {score} below warn threshold {warn_at}")
                approved.append(b)
            else:
                approved.append(b)

        return approved, expelled

    def print_summary(self):
        border = "─" * 62
        print(f"\n  {border}")
        print(f"  ⬡  BEACON REPUTATION TABLE  —  {len(self._data)} beacon(s)")
        print(f"  {border}")
        print(f"  {'NAME':<28}  {'SCORE':>5}  {'EXPELLED':>9}  {'POLLS':>6}  STATUS")
        print(f"  {'─'*28}  {'─'*5}  {'─'*9}  {'─'*6}  ──────")
        for name, rec in sorted(self._data.items()):
            status = "⛔ EXPELLED" if rec["expelled"] else (
                "⚠ WARN" if rec["score"] < 50 else "✅ OK"
            )
            print(f"  {name:<28}  {rec['score']:>5}  "
                  f"{'YES' if rec['expelled'] else 'no':>9}  "
                  f"{rec['successful_polls']:>6}  {status}")
        print(f"  {border}\n")


# ===========================================================================
# OdinNetSecurity  — DEFCON controller
# ===========================================================================

class OdinNetSecurity:
    """
    Central security controller for an OdinNet node.

    Manages:
      - Current DEFCON level + history
      - Beacon reputation enforcement
      - Blacklist management
      - Protocol enforcement per DEFCON level
      - Slow recovery back to DEFCON 1 after threats subside
      - Ollama AI stub (local threat analysis placeholder)
    """

    RECOVERY_STEPS          = [10, 7, 5, 3, 1]  # slow stepdown after attack
    RECOVERY_INTERVAL_SEC   = 300                # 5 min between recovery steps
    BLACKLIST_BAN_MSG       = "BANNED: coordinate blacklisted"

    def __init__(
        self,
        coord_file:       str = "coordinatefile.json",
        security_file:    str = SECURITY_FILE,
        blacklist_file:   str = BLACKLIST_FILE,
        reputation_file:  str = REPUTATION_FILE,
    ):
        self.coord_file      = coord_file
        self.security_file   = security_file
        self.blacklist_file  = blacklist_file
        self.reputation      = BeaconReputation(reputation_file)

        self._state = _load_json_safe(security_file, {
            "defcon":          1,
            "defcon_history":  [],
            "attack_active":   False,
            "last_attack":     None,
            "last_recovery":   None,
            "clean_beacons":   [],    # vetted beacons shared across nodes
            "created":         _now_str(),
        })
        self._blacklist = _load_json_safe(blacklist_file, {"banned": []})
        self._save()

    # ── Persistence ────────────────────────────────────────────────────────

    def _save(self):
        _save_json(self.security_file, self._state)
        _save_json(self.blacklist_file, self._blacklist)

    # ── DEFCON management ─────────────────────────────────────────────────

    @property
    def defcon(self) -> int:
        return self._state["defcon"]

    @property
    def defcon_config(self) -> dict:
        return DEFCON_LEVELS[nearest_defcon(self.defcon)]

    def raise_defcon(self, level: int, reason: str = "manual"):
        """Raise DEFCON to level (higher = more secure). Clamped to 1–10."""
        level = max(1, min(10, level))
        level = nearest_defcon(level)
        old   = self._state["defcon"]
        if level <= old:
            print(f"  [Security] DEFCON already at {old} — not raising to {level}.")
            return

        self._state["defcon"]       = level
        self._state["attack_active"] = True
        self._state["last_attack"]   = _now_str()
        self._state["defcon_history"].append({
            "from":   old,
            "to":     level,
            "reason": reason,
            "time":   _now_str(),
        })
        self._save()
        cfg = self.defcon_config
        print(f"\n  🚨 DEFCON RAISED: {old} → {level}  [{cfg['label']}]")
        print(f"     Reason  : {reason}")
        print(f"     {cfg['description']}")
        self._print_defcon_rules(level)

    def lower_defcon(self, level: int = None, reason: str = "manual"):
        """Lower DEFCON level. If level=None, step down by one recovery step."""
        current = self._state["defcon"]
        if level is None:
            # Walk down the nearest step
            lower_steps = [s for s in _DEFCON_STEPS if s < current]
            level = lower_steps[-1] if lower_steps else 1
        level = max(1, min(current - 1, level))
        level = nearest_defcon(level)
        if level >= current:
            print(f"  [Security] Already at DEFCON {current} — cannot lower to {level}.")
            return

        self._state["defcon"]         = level
        self._state["last_recovery"]  = _now_str()
        if level == 1:
            self._state["attack_active"] = False
        self._state["defcon_history"].append({
            "from":   current,
            "to":     level,
            "reason": reason,
            "time":   _now_str(),
        })
        self._save()
        cfg = DEFCON_LEVELS[nearest_defcon(level)]
        print(f"\n  ✅ DEFCON LOWERED: {current} → {level}  [{cfg['label']}]")
        print(f"     Reason  : {reason}")

    def declare_attack(self, beacon_name: str = None, detail: str = "unknown"):
        """
        Declare an active attack on the network.
        Raises DEFCON by 2 steps; hits beacon reputation if named.
        """
        print(f"\n  ⚠  ATTACK DECLARED  detail='{detail}'")
        new_defcon = min(10, nearest_defcon(self.defcon + 2))
        self.raise_defcon(new_defcon, reason=f"attack:{detail}")
        if beacon_name:
            self.reputation.report_attack(beacon_name)

    def attack_cleared(self, reason: str = "threat_subsided"):
        """
        Begin slow DEFCON recovery after an attack clears.
        In production this would be called periodically by a recovery thread.
        """
        print(f"\n  ✅ ATTACK CLEARED  reason='{reason}'")
        self._state["attack_active"] = False
        self._save()
        self.lower_defcon(reason=f"recovery:{reason}")

    # ── Blacklist ─────────────────────────────────────────────────────────

    def ban(self, identifier: str, reason: str = "unknown"):
        """Ban a coordinate, node id, or beacon name permanently."""
        banned = self._blacklist.get("banned", [])
        if identifier not in banned:
            banned.append(identifier)
            self._blacklist["banned"] = banned
            self._save()
            print(f"  ⛔ BANNED: {identifier}  reason={reason}")
        else:
            print(f"  [Blacklist] Already banned: {identifier}")

    def is_banned(self, identifier: str) -> bool:
        return identifier in self._blacklist.get("banned", [])

    def list_banned(self):
        banned = self._blacklist.get("banned", [])
        if not banned:
            print("  [Blacklist] Empty.")
            return
        print(f"\n  ⛔ BANNED ENTITIES ({len(banned)}):")
        for b in banned:
            print(f"     {b}")

    # ── Beacon enforcement ────────────────────────────────────────────────

    def enforce_beacons(self, beacon_list: list) -> tuple[list, list]:
        """
        Filter beacon list through current DEFCON rules + reputation.
        Returns (approved, expelled).
        """
        approved, expelled = self.reputation.enforce_defcon(self.defcon, beacon_list)
        # Also remove blacklisted entries
        final_approved = []
        for b in approved:
            name = b.get("name", "")
            coord = str(b.get("coordinate", ""))
            if self.is_banned(name) or self.is_banned(coord):
                print(f"  ⛔ Blacklisted beacon blocked: {name}")
                expelled.append(b)
            else:
                final_approved.append(b)
        return final_approved, expelled

    def share_clean_beacons(self, beacons: list):
        """Update the clean beacon list to share with other nodes."""
        approved, _ = self.enforce_beacons(beacons)
        self._state["clean_beacons"] = [b.get("name", "") for b in approved]
        self._save()
        print(f"  [Security] {len(approved)} clean beacon(s) ready to share.")

    # ── Protocol enforcement ──────────────────────────────────────────────

    def check_message_allowed(
        self,
        msg_type:   str = "temporal",   # "temporal" | "realtime"
        encrypted:  bool = False,
        from_server: bool = True,
    ) -> tuple[bool, str]:
        """
        Check whether a message should be accepted given current DEFCON.

        Returns (allowed: bool, reason: str).
        """
        cfg = self.defcon_config

        if msg_type == "temporal" and not cfg["temporal_allowed"]:
            return False, f"Temporal messages suspended at DEFCON {self.defcon}"
        if msg_type == "realtime" and not cfg["realtime_allowed"]:
            return False, f"Realtime messages suspended at DEFCON {self.defcon}"
        if cfg["require_encryption"] and not encrypted:
            return False, f"Encryption required at DEFCON {self.defcon}"

        return True, "OK"

    def get_required_encryption(self) -> str:
        """Return the encryption level string for current DEFCON."""
        cfg = self.defcon_config
        if cfg["aes256_required"]:
            return "AES-256-GCM"
        if cfg["require_encryption"]:
            return "AES-128 or better"
        return "optional"

    # ── Display ───────────────────────────────────────────────────────────

    def _print_defcon_rules(self, level: int):
        cfg = DEFCON_LEVELS[nearest_defcon(level)]
        print(f"\n  ┌── DEFCON {level} RULES {'─'*40}")
        print(f"  │  Dummy beacons   : {'✅ allowed' if cfg['dummy_beacons'] else '⛔ BANNED'}")
        print(f"  │  Server beacons  : {'✅ required' if cfg['server_beacons'] else '—'}")
        print(f"  │  Encryption      : {self.get_required_encryption()}")
        print(f"  │  AES-256 traffic : {'🔒 YES' if cfg['aes256_required'] else 'optional'}")
        print(f"  │  Beacon rotate   : {'✅ YES' if cfg['beacon_rotate'] else '—'}")
        print(f"  │  Polling encrypt : {'🔒 YES' if cfg['polling_encrypted'] else '—'}")
        print(f"  └{'─'*51}")

    def status_display(self):
        border = "═" * 62
        cfg    = self.defcon_config
        print(f"\n{border}")
        print(f"  {cfg['color']}  ODINNET SECURITY STATUS")
        print(f"{border}")
        print(f"  DEFCON Level  : {self.defcon}  [{cfg['label']}]")
        print(f"  Description   : {cfg['description']}")
        print(f"  Attack active : {'⚠ YES' if self._state['attack_active'] else '✅ no'}")
        print(f"  Last attack   : {self._state.get('last_attack', '—')}")
        print(f"  Last recovery : {self._state.get('last_recovery', '—')}")
        print(f"{'-'*62}")
        self._print_defcon_rules(self.defcon)
        print(f"{'-'*62}")
        print(f"  Clean beacons : {len(self._state.get('clean_beacons', []))}")
        print(f"  Banned entries: {len(self._blacklist.get('banned', []))}")
        history = self._state.get("defcon_history", [])[-5:]
        if history:
            print(f"{'-'*62}")
            print(f"  DEFCON HISTORY (last {len(history)}):")
            for ev in history:
                print(f"    [{ev['time']}] {ev['from']} → {ev['to']}  ({ev['reason']})")
        print(f"{border}\n")

    def status_dict(self) -> dict:
        """Return security status as a dict for /status endpoint."""
        cfg = self.defcon_config
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
        }

    # ── Interactive CLI ───────────────────────────────────────────────────

    def interactive_menu(self):
        print("\n" + "★" * 62)
        print("  ODINNET SECURITY — DEFCON CONTROL")
        print("★" * 62)
        print("  Commands: status | raise <n> | lower [n] | attack [beacon]")
        print("            cleared | ban <id> | banned | reputation | exit\n")

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
                    print("  Usage: ban <identifier>")
                    continue
                self.ban(parts[1], reason="operator")
            elif cmd == "banned":
                self.list_banned()
            elif cmd == "reputation":
                self.reputation.print_summary()
            else:
                print("  Commands: status | raise <n> | lower [n] | attack [beacon]")
                print("            cleared | ban <id> | banned | reputation | exit")


# ===========================================================================
# Ollama stub — local AI threat analysis placeholder
# ===========================================================================

class OllamaStub:
    """
    Placeholder for Ollama integration (local AI on the node).

    In production this would call the Ollama API at localhost:11434
    and run a local model for threat analysis, content classification,
    and anomaly detection.

    To activate: replace _query() with a real requests.post() call.
    """

    STUB_RESPONSES = {
        "threat_analysis": (
            "[Ollama STUB] Threat analysis not yet wired to a local model.\n"
            "Install Ollama and a model, then implement _query() to activate."
        ),
        "classify_beacon": (
            "[Ollama STUB] Beacon classification pending Ollama integration."
        ),
        "summarise_traffic": (
            "[Ollama STUB] Traffic summary pending Ollama integration."
        ),
    }

    def __init__(self, model: str = "llama3", host: str = "localhost", port: int = 11434):
        self.model = model
        self.host  = host
        self.port  = port
        self.available = False   # flip to True when Ollama is reachable
        print(f"  [Ollama] Stub initialised  model={model}  "
              f"endpoint=http://{host}:{port}/api/generate")
        print("  [Ollama] Status: STUB — not connected to live model.")

    def _query(self, prompt: str) -> str:
        """Override this method with a real Ollama API call."""
        # Real implementation:
        # import requests
        # r = requests.post(
        #     f"http://{self.host}:{self.port}/api/generate",
        #     json={"model": self.model, "prompt": prompt, "stream": False},
        #     timeout=30,
        # )
        # return r.json().get("response", "")
        return "[Ollama STUB] Model not connected."

    def threat_analysis(self, context: str) -> str:
        """Analyse a threat description and suggest DEFCON action."""
        if not self.available:
            return self.STUB_RESPONSES["threat_analysis"]
        prompt = (
            f"You are an OdinNet security analyst. Analyse this threat:\n{context}\n"
            f"Recommend a DEFCON level (1=normal, 10=maximum) and action."
        )
        return self._query(prompt)

    def classify_beacon(self, beacon_name: str, events: list) -> str:
        """Classify a beacon as trusted / suspicious / compromised."""
        if not self.available:
            return self.STUB_RESPONSES["classify_beacon"]
        prompt = (
            f"Classify beacon '{beacon_name}' based on these events:\n"
            f"{json.dumps(events, indent=2)}\n"
            f"Reply: TRUSTED / SUSPICIOUS / COMPROMISED with one sentence reason."
        )
        return self._query(prompt)

    def summarise_traffic(self, activity_tail: list) -> str:
        """Summarise recent network activity for the operator."""
        if not self.available:
            return self.STUB_RESPONSES["summarise_traffic"]
        prompt = (
            "Summarise this OdinNet node activity log in 3 sentences:\n"
            + "\n".join(activity_tail[-20:])
        )
        return self._query(prompt)

    def status(self) -> dict:
        return {
            "available":   self.available,
            "model":       self.model,
            "endpoint":    f"http://{self.host}:{self.port}/api/generate",
            "note":        "STUB — wire up _query() to activate",
        }


# ===========================================================================
# SELF-TESTS
# ===========================================================================

if __name__ == "__main__":
    import tempfile, os

    print("=" * 64)
    print("  OdinNet Security System — Self-Tests")
    print("=" * 64)

    with tempfile.TemporaryDirectory() as tmp:
        sec_f  = os.path.join(tmp, "sec.json")
        bl_f   = os.path.join(tmp, "blacklist.json")
        rep_f  = os.path.join(tmp, "reputation.json")

        sec = OdinNetSecurity(
            security_file   = sec_f,
            blacklist_file  = bl_f,
            reputation_file = rep_f,
        )

        # Test 1: Initial state
        print("\n[Test 1] Initial DEFCON state")
        assert sec.defcon == 1
        print(f"  ✅ DEFCON={sec.defcon}  label={sec.defcon_config['label']}")

        # Test 2: Raise / lower
        print("\n[Test 2] Raise → lower DEFCON")
        sec.raise_defcon(5, reason="test")
        assert sec.defcon == 5
        sec.lower_defcon(reason="test_recovery")
        assert sec.defcon < 5
        print(f"  ✅ After raise→lower: DEFCON={sec.defcon}")

        # Test 3: Attack declaration
        print("\n[Test 3] Declare attack")
        sec.raise_defcon(1, reason="reset_for_test")
        # Force back to 1
        sec._state["defcon"] = 1
        sec._save()
        sec.declare_attack("beacon-A", "DDoS detected")
        assert sec.defcon > 1
        print(f"  ✅ DEFCON raised to {sec.defcon} on attack")

        # Test 4: Beacon reputation
        print("\n[Test 4] Beacon reputation penalties")
        rep = BeaconReputation(rep_f)
        rep.report_decode_fail("beacon-X")
        rep.report_hash_mismatch("beacon-X")
        score = rep.get_score("beacon-X")
        assert score < 100
        print(f"  ✅ beacon-X score={score} (penalised)")
        rep.record_success("beacon-X")
        score2 = rep.get_score("beacon-X")
        assert score2 == score + 1
        print(f"  ✅ recovery: score={score2} after one success poll")

        # Test 5: Expel beacon
        print("\n[Test 5] Expel beacon")
        rep.expel("beacon-Y", reason="test_expel")
        assert rep.is_expelled("beacon-Y")
        assert rep.is_blacklisted("beacon-Y")
        print("  ✅ beacon-Y expelled + blacklisted")

        # Test 6: Enforce beacons at DEFCON 7 (no dummies)
        print("\n[Test 6] Enforce beacons at DEFCON 7")
        sec._state["defcon"] = 7
        sec._save()
        beacons = [
            {"name": "dummy-1",  "coordinate": "12345", "is_server": False},
            {"name": "server-1", "coordinate": "67890", "is_server": True},
        ]
        approved, expelled = sec.enforce_beacons(beacons)
        dummy_names = [b["name"] for b in expelled]
        assert "dummy-1" in dummy_names
        assert any(b["name"] == "server-1" for b in approved)
        print(f"  ✅ Dummy beacon expelled, server beacon approved")

        # Test 7: Ban check
        print("\n[Test 7] Blacklist ban")
        sec.ban("bad-node-42", reason="test")
        assert sec.is_banned("bad-node-42")
        assert not sec.is_banned("good-node-99")
        print("  ✅ Ban / not-banned working")

        # Test 8: Message allowed check
        print("\n[Test 8] Message policy at DEFCON 5")
        sec._state["defcon"] = 5
        sec._save()
        ok, reason = sec.check_message_allowed("temporal", encrypted=False)
        assert not ok, "Should require encryption at DEFCON 5"
        ok2, _ = sec.check_message_allowed("temporal", encrypted=True)
        assert ok2
        print(f"  ✅ Unencrypted blocked at DEFCON 5, encrypted OK")

        # Test 9: Ollama stub
        print("\n[Test 9] Ollama stub")
        ai = OllamaStub()
        resp = ai.threat_analysis("Beacon under DDoS from external IPs")
        assert "STUB" in resp
        print(f"  ✅ Ollama stub responds correctly")
        print(f"     {resp[:70]}...")

        # Test 10: status_dict
        print("\n[Test 10] status_dict()")
        d = sec.status_dict()
        assert "defcon" in d and "defcon_label" in d
        print(f"  ✅ status_dict has defcon={d['defcon']}  label={d['defcon_label']}")

        sec.status_display()

    print("\n✅  All OdinNet Security tests passed.\n")
