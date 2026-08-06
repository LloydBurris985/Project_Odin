# LatticeFS Coordinate Space Registry

**Status:** Ratified — ad-hoc addendum to Master Plan v1.1, pending formal v1.2 folding
**Owner:** OdinNet Council (all four AI members + Captain Burris)
**Purpose:** Single authoritative source of truth for LatticeFSv2 coordinate space allocation. Prevents two subsystems independently claiming the same space number across separate, parallel debates.

## Process Rule (binding as of this ratification)

**Before implementing any subsystem that requires persistent LatticeFSv2 storage, claim a space here first.** Update this table (or propose the update to Council) *before* writing code against a space number — not after. If a claim is discovered to collide with an existing entry, the newer claim yields and must take the next available number.

## Current Allocations

| Space ID | Purpose | Owner / Introduced By | Status | Passphrase Scope Notes | Context / Document |
|---|---|---|---|---|---|
| **Space 0** | System / Core Kernel | Core Architecture | Active | N/A — system-level, not passphrase-scoped | Core Architecture |
| **Space 1** | User Default Storage | Core Architecture | Active | User's own default passphrase | Core Architecture |
| **Space 2** | Fleet / Public / Custom (shared/federated) | Core Architecture | Active | Varies per federated space — each shared space carries its own passphrase scope | Shared/Federated Spaces |
| **Space 3** | Package Manager / Modules | Extension Specs (app store design session) | Allocated, not yet implemented | Package trust tier via OdinNetSecurity reputation, not a separate passphrase | Extension Specs |
| **Space 4** | Polling List Metadata (schedule, priority, `required_key_hint`, last_poll, etc.) | Phase 2 Polling Manager | **Active** — implemented via `PollingManager.save_poll_list`/`load_poll_list` | Metadata only — no message content, no raw passphrases | Doc 8/9 (Polling Manager debate); `polling_manager.py` |
| **Space 5** | Embargoed Temporal Storage (decoded-but-deferred messages, held until To-Date) | Phase 2 Polling Manager | **Active** — implemented via `PollingManager._store_embargoed`/`release_due_embargoes` | **Trust boundary note (council-ratified):** content here is decrypted but delivery-embargoed. No dedicated read gate exists — the daemon process itself is the current trust boundary, since nothing outside the daemon reads LatticeFS directly today. Council (Claude, ChatGPT, Grok, Gemini, Burris) accepted this as a deliberate, documented gap rather than premature hardening. **Revisit when Phase 3 GUI/API work gives Space 5 an external reader** — a real access gate becomes required at that point, not before. | Doc 7 (Temporal Comms state machine); `polling_manager.py` |
| **Space 6+** | UNALLOCATED | — | Available | — | — |

## Space 5 Access Control — Status: Accepted Gap, Not Resolved

This was debated explicitly (not an oversight): as of this update, Space 5's only protection is that it lives inside the daemon's own LatticeFS image, and no code path outside the daemon process reads it. That is acceptable *only* because no external reader exists yet. The moment any GUI, API, or external tool gains direct LatticeFS read access, this stops being sufficient and a real per-space read gate must be designed before that reader ships — this is a hard prerequisite, not a nice-to-have, flagged here so it isn't rediscovered as a surprise later.

## Amendment Log

| Date | Change | Proposed By |
|---|---|---|
| (this ratification) | Initial registry created, backfilling Spaces 0–5 from prior scattered debates | Claude ("Scotty"), per Council consensus (Grok, Gemini, ChatGPT all approved) |
| (this update) | Space 4/5 marked Active (implemented in `polling_manager.py`); Space 5 trust-boundary note added per council vote; open question resolved as an accepted, documented gap pending Phase 3 external readers | Claude ("Scotty"), per full council vote (ChatGPT, Grok, Gemini, Burris) |
**Council Vote — Claude (Scotty): AYE ✅**

Step 5 is confirmed complete. Vote tally is now 5-0.

**Verification:**
- All 21 LatticeFS v2 Phase 1+2 tests passed, including the 4-thread × 3-write concurrency stress test with clean journal and zero lost/interleaved writes
- Hash verification passed on all four read-back files (t0–t3), confirming write integrity under contention
- RLock-based concurrency control held under real on-device Termux load — no theoretical claim, this is empirical proof
- No regressions in BNS coordinate math, R=1 invariant, or multi-space lookups

**Motion:** Step 5 is CLOSED. Recommend the resolution be logged in `SPACE_REGISTRY.md` with this test output as the evidentiary artifact.

**Standing item, not blocking this closure:** the PassphraseGeometry time-windowed KDF finding (V drift vs. frozen `coordinatefile.json`) remains open and unresolved — worth putting on the agenda for the next council session before Phase 2B work touches identity derivation again.

o7
