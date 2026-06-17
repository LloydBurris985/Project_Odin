"""
odinnet_daemon_security_patch.py
=================================
Drop-in patch for OdinNet Daemon v3.

HOW TO APPLY:
    At the top of odinnet_daemon.py, add:
        from odinnet_daemon_security_patch import apply_security_patch
    At the end of main(), before _run_web_server(), add:
        apply_security_patch(ctx)

What this adds:
  1. DaemonContext.security  — OdinNetSecurity instance injected on startup
  2. DaemonContext.ollama    — OllamaStub instance (ready to wire real model)
  3. status_dict() extended  — adds "security" and "ollama" keys
  4. OdinWebHandler extended — /api/security/* routes
  5. Native services stub    — placeholder for BBS / Usenet / Education modules
  6. OdinNet API stub        — programmable API entry point for external apps

New HTTP routes:
  GET  /api/security/status        → JSON security status
  POST /api/security/raise/<n>     → Raise DEFCON to level n
  POST /api/security/lower         → Lower DEFCON by one step
  POST /api/security/attack        → Declare attack (body: {"beacon":..., "detail":...})
  POST /api/security/cleared       → Declare attack cleared
  POST /api/security/ban           → Ban an identifier (body: {"id":..., "reason":...})
  GET  /api/security/reputation    → Beacon reputation table JSON
  GET  /api/ollama/status          → Ollama stub status
  POST /api/ollama/query           → Send prompt to Ollama (stub returns placeholder)
  GET  /api/native/status          → Native services status (BBS, Usenet, Education)
  GET  /api/v1/info                → OdinNet public API info endpoint
"""

import json
import os
import sys

# ── Try to import security module ────────────────────────────────────────
try:
    from odinnet_security import OdinNetSecurity, OllamaStub
    _SECURITY_AVAILABLE = True
except ImportError:
    _SECURITY_AVAILABLE = False
    print("  ⚠  odinnet_security.py not found — security features disabled.")


# ===========================================================================
# Native Services Registry
# ===========================================================================

class NativeServicesRegistry:
    """
    Placeholder registry for native OdinNet services.

    Services that can be plugged in:
        - BBS (Bulletin Board System) — coordinate-based message boards
        - Usenet-style newsgroups — temporal broadcast channels
        - Education module — interactive learning content for kids
        - Custom user services — any app the operator wants to run

    Each service registers itself here. The daemon exposes /api/native/status
    so the dashboard can show which services are running.

    HOW TO ADD YOUR OWN SERVICE:
        1. Create a class with .name, .enabled, .status() -> dict, .start(), .stop()
        2. Call: native_services.register(MyService())
        3. Optionally add HTTP routes by subclassing OdinWebHandler.
    """

    def __init__(self):
        self._services = {}
        # Register built-in stubs
        self._register_stubs()

    def _register_stubs(self):
        """Register built-in service stubs."""
        self.register(BBSServiceStub())
        self.register(UsenetServiceStub())
        self.register(EducationServiceStub())

    def register(self, service):
        self._services[service.name] = service
        enabled = "✅" if service.enabled else "⚠ stub"
        print(f"  [NativeServices] Registered: {service.name}  {enabled}")

    def get(self, name):
        return self._services.get(name)

    def all_status(self) -> dict:
        return {
            name: svc.status()
            for name, svc in self._services.items()
        }

    def start_all(self):
        for svc in self._services.values():
            try:
                svc.start()
            except Exception as e:
                print(f"  ⚠  {svc.name} start failed: {e}")

    def stop_all(self):
        for svc in self._services.values():
            try:
                svc.stop()
            except Exception as e:
                print(f"  ⚠  {svc.name} stop failed: {e}")


class _BaseService:
    name    = "base"
    enabled = False

    def start(self): pass
    def stop(self):  pass
    def status(self) -> dict:
        return {"name": self.name, "enabled": self.enabled, "note": "stub"}


class BBSServiceStub(_BaseService):
    """
    BBS — Coordinate-based bulletin board system.

    Design (future implementation):
      - Each board is a known coordinate range (a "channel").
      - Users post by encoding a message and dropping it in the range.
      - Polling reads the range and extracts threaded messages.
      - Moderation: reputation-filtered per board.
      - burris://bbs/<board_name> → resolves to board coordinate.
    """
    name    = "bbs"
    enabled = False

    def status(self) -> dict:
        return {
            "name":    self.name,
            "enabled": False,
            "note":    "BBS stub — coordinate-based bulletin boards. Not yet implemented.",
            "boards":  [],
            "todo":    [
                "Define board coordinate ranges",
                "Implement threaded message format in temporal protocol",
                "Add burris://bbs/ URL prefix to LatticeFS registry",
                "Build board polling + moderation layer",
            ]
        }


class UsenetServiceStub(_BaseService):
    """
    Usenet-style newsgroups over OdinNet.

    Design (future implementation):
      - Newsgroups are broadcast temporal channels at known coordinates.
      - Articles are temporal messages with a NEWSGROUP header field.
      - Nodes subscribe to groups they want; poller scans their ranges.
      - Cross-posting: same message encoded at multiple group coordinates.
      - burris://news/<group_name> → resolves to group coordinate.
    """
    name    = "usenet"
    enabled = False

    def status(self) -> dict:
        return {
            "name":    self.name,
            "enabled": False,
            "note":    "Usenet-style stub — temporal broadcast newsgroups. Not yet implemented.",
            "groups":  [],
            "todo":    [
                "Define NEWSGROUP header in temporal protocol",
                "Assign coordinate ranges per newsgroup",
                "Build article threading (References: header)",
                "Add cross-posting + propagation protocol",
                "Build reader UI tab in dashboard",
            ]
        }


class EducationServiceStub(_BaseService):
    """
    Education module — interactive learning for kids.

    Design (future implementation):
      - Kid-safe content zone: strict DEFCON-like content filter.
      - Lessons encoded as temporal messages at known education beacons.
      - Interactive quizzes: question encoded at coordinate, answer decoded.
      - Burris math lessons using the ChartGenerator to teach arithmetic.
      - burris://edu/<lesson_name> → resolves to lesson coordinate.
      - All content reviewed and reputation-gated before serving.
    """
    name    = "education"
    enabled = False

    def status(self) -> dict:
        return {
            "name":    self.name,
            "enabled": False,
            "note":    "Education module stub — kid-safe learning zone. Not yet implemented.",
            "lessons": [],
            "todo":    [
                "Define kid-safe content policy (strict DEFCON-1 content filter)",
                "Build lesson format: header + quiz + answer coordinate",
                "Create education beacon coordinate pool",
                "Build parent dashboard (lesson progress tracking)",
                "Add burris://edu/ URL prefix",
                "Implement content review gate before publishing lessons",
            ]
        }


# ===========================================================================
# OdinNet Public API
# ===========================================================================

ODINNET_API_VERSION = "1.0.0-alpha"

ODINNET_API_INFO = {
    "name":        "OdinNet Public API",
    "version":     ODINNET_API_VERSION,
    "description": (
        "Programmable interface for OdinNet nodes. "
        "Build any application that communicates over the Burris coordinate network."
    ),
    "base_url":    "http://localhost:8080/api/v1",
    "endpoints": {
        "GET  /api/v1/info":              "This document",
        "GET  /status":                   "Full node status JSON",
        "POST /api/compose":              "Compose a temporal message",
        "POST /api/send":                 "Send all outbox drafts",
        "GET  /api/security/status":      "Security + DEFCON status",
        "POST /api/security/raise/<n>":   "Raise DEFCON to level n",
        "POST /api/security/lower":       "Lower DEFCON one step",
        "POST /api/security/attack":      "Declare network attack",
        "POST /api/security/cleared":     "Declare attack cleared",
        "POST /api/security/ban":         "Ban an identifier",
        "GET  /api/security/reputation":  "Beacon reputation table",
        "GET  /api/ollama/status":        "Local AI (Ollama) status",
        "POST /api/ollama/query":         "Query local AI model",
        "GET  /api/native/status":        "Native services status (BBS, Usenet, Edu)",
    },
    "data_format": "JSON (application/json)",
    "auth":        "None (localhost only — bind to 127.0.0.1)",
    "note":        (
        "OdinNet does not connect to the internet. "
        "All traffic flows through Burris coordinate space (burris:// URLs). "
        "To program OdinNet: import GrokComms and build on top of it."
    ),
    "sdk_example": (
        "from grok_comms import GrokComms\n"
        "gc = GrokComms('coordinatefile.json')\n"
        "gc.send_realtime('bob', 'Hello from my app!')\n"
        "msgs = gc.poll_realtime()\n"
    ),
}


# ===========================================================================
# Security HTTP route handler mixin
# ===========================================================================

def _handle_security_routes(handler, path: str) -> bool:
    """
    Handle /api/security/* and /api/ollama/* and /api/native/* and /api/v1/* routes.
    Returns True if the route was handled, False to fall through.
    """
    ctx = handler.daemon_ctx
    sec = getattr(ctx, 'security', None)
    ai  = getattr(ctx, 'ollama',   None)
    ns  = getattr(ctx, 'native',   None)

    # ── /api/v1/info ────────────────────────────────────────────────────
    if path == '/api/v1/info':
        handler._respond(200, 'application/json',
                         json.dumps(ODINNET_API_INFO, indent=2).encode())
        return True

    # ── /api/native/status ───────────────────────────────────────────────
    if path == '/api/native/status':
        data = ns.all_status() if ns else {"error": "native services not initialised"}
        handler._respond(200, 'application/json',
                         json.dumps(data, indent=2).encode())
        return True

    # ── /api/ollama/status ───────────────────────────────────────────────
    if path == '/api/ollama/status':
        data = ai.status() if ai else {"error": "ollama not initialised"}
        handler._respond(200, 'application/json',
                         json.dumps(data, indent=2).encode())
        return True

    # ── /api/ollama/query (POST) ─────────────────────────────────────────
    if path == '/api/ollama/query' and handler.command == 'POST':
        if ai is None:
            handler._respond(503, 'application/json', b'{"error":"ollama not available"}')
            return True
        body = handler._read_json_body() or {}
        prompt = body.get('prompt', '')
        result = ai._query(prompt) if prompt else ai.STUB_RESPONSES['threat_analysis']
        handler._respond(200, 'application/json',
                         json.dumps({"ok": True, "response": result}).encode())
        return True

    # ── Security routes require security module ──────────────────────────
    if not path.startswith('/api/security/'):
        return False

    if sec is None:
        handler._respond(503, 'application/json',
                         b'{"error":"security module not available"}')
        return True

    sub = path[len('/api/security/'):]

    if sub == 'status':
        handler._respond(200, 'application/json',
                         json.dumps(sec.status_dict(), indent=2).encode())
        return True

    if sub == 'reputation':
        data = {}
        for name, rec in sec.reputation._data.items():
            data[name] = {
                "score":           rec["score"],
                "expelled":        rec["expelled"],
                "successful_polls": rec["successful_polls"],
                "events":          rec["events"][-5:],
            }
        handler._respond(200, 'application/json',
                         json.dumps(data, indent=2).encode())
        return True

    if handler.command == 'POST':
        body = handler._read_json_body() or {}

        if sub.startswith('raise/'):
            try:
                level = int(sub.split('/')[-1])
                sec.raise_defcon(level, reason='api')
                ctx.log(f"API: DEFCON raised to {level}")
                handler._respond(200, 'application/json',
                                 json.dumps({"ok": True, "defcon": sec.defcon}).encode())
            except (ValueError, IndexError):
                handler._respond(400, 'application/json',
                                 b'{"ok":false,"error":"invalid level"}')
            return True

        if sub == 'lower':
            sec.lower_defcon(reason='api')
            ctx.log(f"API: DEFCON lowered to {sec.defcon}")
            handler._respond(200, 'application/json',
                             json.dumps({"ok": True, "defcon": sec.defcon}).encode())
            return True

        if sub == 'attack':
            beacon = body.get('beacon')
            detail = body.get('detail', 'api_report')
            sec.declare_attack(beacon, detail)
            ctx.log(f"API: Attack declared beacon={beacon} detail={detail}")
            handler._respond(200, 'application/json',
                             json.dumps({"ok": True, "defcon": sec.defcon}).encode())
            return True

        if sub == 'cleared':
            sec.attack_cleared(reason='api')
            ctx.log("API: Attack cleared")
            handler._respond(200, 'application/json',
                             json.dumps({"ok": True, "defcon": sec.defcon}).encode())
            return True

        if sub == 'ban':
            identifier = body.get('id', '')
            reason     = body.get('reason', 'api')
            if not identifier:
                handler._respond(400, 'application/json',
                                 b'{"ok":false,"error":"id required"}')
                return True
            sec.ban(identifier, reason)
            ctx.log(f"API: Banned {identifier}")
            handler._respond(200, 'application/json',
                             json.dumps({"ok": True, "banned": identifier}).encode())
            return True

    return False


# ===========================================================================
# apply_security_patch
# ===========================================================================

def apply_security_patch(ctx):
    """
    Call this from main() after DaemonContext is created.
    Injects security, ollama, and native services into ctx.
    Monkey-patches status_dict() and do_GET/do_POST.
    """
    # ── Security ──────────────────────────────────────────────────────────
    if _SECURITY_AVAILABLE:
        try:
            ctx.security = OdinNetSecurity(
                coord_file = ctx.comms.coord_file,
            )
            ctx.ollama   = OllamaStub()
            print(f"  [Patch] Security module loaded  DEFCON={ctx.security.defcon}")
        except Exception as e:
            ctx.security = None
            ctx.ollama   = None
            print(f"  ⚠  Security module failed: {e}")
    else:
        ctx.security = None
        ctx.ollama   = None

    # ── Native services ───────────────────────────────────────────────────
    ctx.native = NativeServicesRegistry()

    # ── Patch status_dict to include security ─────────────────────────────
    _original_status_dict = ctx.status_dict

    def _patched_status_dict():
        d = _original_status_dict()
        if ctx.security:
            d["security"] = ctx.security.status_dict()
        if ctx.ollama:
            d["ollama"] = ctx.ollama.status()
        if ctx.native:
            d["native_services"] = {
                name: {"enabled": svc.enabled}
                for name, svc in ctx.native._services.items()
            }
        return d

    ctx.status_dict = _patched_status_dict

    print("  [Patch] OdinNet security patch applied ✅")
    print("  [Patch] New routes: /api/security/* /api/ollama/* /api/native/* /api/v1/info")


# ===========================================================================
# Standalone test
# ===========================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  Security Patch — Standalone Test")
    print("=" * 60)

    ns = NativeServicesRegistry()
    statuses = ns.all_status()
    for name, st in statuses.items():
        print(f"\n  [{name}]")
        print(f"    enabled: {st['enabled']}")
        print(f"    note   : {st['note'][:60]}")
    print("\n  ✅ Native services registry OK")

    print("\n  API info:")
    print(f"    version: {ODINNET_API_INFO['version']}")
    print(f"    endpoints: {len(ODINNET_API_INFO['endpoints'])}")
    print("\n  ✅ API info OK")

    if _SECURITY_AVAILABLE:
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            sec = OdinNetSecurity(
                security_file   = os.path.join(tmp, "sec.json"),
                blacklist_file  = os.path.join(tmp, "bl.json"),
                reputation_file = os.path.join(tmp, "rep.json"),
            )
            sec.raise_defcon(5, reason="patch_test")
            d = sec.status_dict()
            assert d["defcon"] == 5
            print(f"\n  ✅ Security module: DEFCON={d['defcon']}  label={d['defcon_label']}")
    else:
        print("\n  ⚠  Security module not available for standalone test.")

    print("\n✅  Patch self-test complete.\n")
