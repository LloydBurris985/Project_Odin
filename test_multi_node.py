#!/usr/bin/env python3
"""
OdinNet Multi-Node Test
=======================
Exercises a two-node send → poll → reply → poll cycle using
the real GrokComms API.

Run:
    python test_multi_node.py

Both nodes use separate coordinate files (node_a.json / node_b.json)
so they behave as independent peers sharing only the realtime message
store (realtime_messages.json).

NOTE: coordinate_generator + polling_range_finder are intentionally
lightweight here (num_digits=150, num_samples=10) to keep the test
fast.  Increase for production use.
"""

import time
import os
import json

from grok_comms import GrokComms, _load_rt_store

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

DIVIDER = "─" * 54

def section(title: str):
    print(f"\n{DIVIDER}")
    print(f"  {title}")
    print(DIVIDER)

def cleanup(*files):
    """Remove coord files created by this test so re-runs are clean."""
    for f in files:
        if os.path.exists(f):
            os.remove(f)
            print(f"  [cleanup] removed {f}")

# ─────────────────────────────────────────────────────────────
# Main test
# ─────────────────────────────────────────────────────────────

def main():
    print("\n" + "★" * 54)
    print("  OdinNet Multi-Node Test")
    print("★" * 54)

    # ── Setup: remove stale coord files so each run is reproducible ──
    cleanup("node_a.json", "node_b.json")

    # ═══════════════════════════════════════════════════════════
    # Node A — Alice
    # ═══════════════════════════════════════════════════════════
    section("Node A (Alice) — init")
    node_a = GrokComms("node_a.json")
    node_a.coordinate_generator("alice-secret", num_digits=150)
    node_a.polling_range_finder(num_samples=10)   # fast; raise for production
    node_a.calculate_tight_polling_range(message_length=16, num_samples=8)
    print(f"  Alice node ID : {node_a.my_id}")

    # ═══════════════════════════════════════════════════════════
    # Node B — Bob
    # ═══════════════════════════════════════════════════════════
    section("Node B (Bob) — init")
    node_b = GrokComms("node_b.json")
    node_b.coordinate_generator("bob-secret", num_digits=150)
    node_b.polling_range_finder(num_samples=10)
    node_b.calculate_tight_polling_range(message_length=16, num_samples=8)
    print(f"  Bob node ID   : {node_b.my_id}")

    # ═══════════════════════════════════════════════════════════
    # Step 1 — Alice sends to Bob
    # ═══════════════════════════════════════════════════════════
    section("Step 1 — Alice → Bob")
    msg_id, coord_val = node_a.send_realtime(
        to      = node_b.my_id,
        message = "Hey Bob, this is a test from Alice via OdinNet!",
    )
    print(f"  msg_id    : {msg_id}")
    print(f"  coord     : {str(coord_val)[:30]}...")

    # ═══════════════════════════════════════════════════════════
    # Step 2 — Bob polls
    # ═══════════════════════════════════════════════════════════
    section("Step 2 — Bob polls for messages")
    time.sleep(0.5)   # let the store flush
    received_by_bob = node_b.poll_realtime()
    print(f"\n  Bob received : {len(received_by_bob)} message(s)")
    for rec in received_by_bob:
        print(f"    from={rec.get('from')}  preview=\"{rec.get('preview', '')}\"")

    # ═══════════════════════════════════════════════════════════
    # Step 3 — Bob replies (via simulate_reply so no coord math needed)
    # ═══════════════════════════════════════════════════════════
    section("Step 3 — Bob replies to Alice")
    reply_id = ""
    if received_by_bob:
        original_id = received_by_bob[0]["msg_id"]
        reply_id = node_b.simulate_reply(
            original_msg_id = original_id,
            reply_text      = "Got it Alice! Systems nominal.",
            from_node       = node_b.my_id,
        )
        print(f"  reply msg_id : {reply_id}")
    else:
        print("  (no message to reply to — skipping)")

    # ═══════════════════════════════════════════════════════════
    # Step 4 — Alice polls for Bob's reply
    # ═══════════════════════════════════════════════════════════
    section("Step 4 — Alice polls for replies")
    time.sleep(0.5)
    received_by_alice = node_a.poll_realtime()
    print(f"\n  Alice received : {len(received_by_alice)} message(s)")
    for rec in received_by_alice:
        rtype = "reply" if rec.get("reply_to") else "direct"
        print(f"    [{rtype}]  from={rec.get('from')}  "
              f"preview=\"{rec.get('preview', '')}\"")

    # ═══════════════════════════════════════════════════════════
    # Step 5 — Unified poll (temporal + beacons) on both nodes
    # ═══════════════════════════════════════════════════════════
    section("Step 5 — Unified poll (temporal + beacons)")
    temporal_a = node_a.poll()
    print(f"  Alice temporal/beacon messages : {len(temporal_a)}")
    temporal_b = node_b.poll()
    print(f"  Bob   temporal/beacon messages : {len(temporal_b)}")

    # ═══════════════════════════════════════════════════════════
    # Summary
    # ═══════════════════════════════════════════════════════════
    section("Summary")
    all_passed = True

    def check(label, condition):
        nonlocal all_passed
        status = "✅" if condition else "❌"
        print(f"  {status}  {label}")
        if not condition:
            all_passed = False

    check("Node A coord file created",  os.path.exists("node_a.json"))
    check("Node B coord file created",  os.path.exists("node_b.json"))
    check("Alice sent a message",       bool(msg_id))
    check("Bob received ≥1 message",    len(received_by_bob) >= 1)
    check("Bob sent a reply",           bool(reply_id))
    check("Alice received ≥1 reply",    len(received_by_alice) >= 1)

    print()
    if all_passed:
        print("  🎉 All checks passed — OdinNet network is alive.")
    else:
        print("  ⚠  Some checks failed — review output above.")

    print(f"\n  Alice ID : {node_a.my_id}")
    print(f"  Bob ID   : {node_b.my_id}")
    print(f"  Sent ID  : {msg_id}")
    print(f"  Reply ID : {reply_id}")
    print()


if __name__ == "__main__":
    main()
