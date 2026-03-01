# odins_net/tests/test_runway.py
# Demo / test for runway polling and coord decoding
# Simulates finding a coordinate in a runway, decodes it, verifies round-trip

from odins_net.runway import Runway, RunwayPoller
from odins_net.core import OdinsEye
import hashlib
import random


def simulate_real_fetch(mask: int) -> dict | None:
    """
    Fake 'fetch' logic: pretend some masks contain valid encoded coordinates.
    In real Odins Net this would be Bluetooth/LoRa/sneakernet reading.
    """
    # For demo: 1 in 50 masks has traffic (adjust as needed)
    if random.random() < 0.02:
        # Create a fake but valid-looking coord
        eye = OdinsEye(start_mask=50000)
        fake_data = f"Message from mask {mask} at {random.randint(10000,99999)}".encode()
        real_coord = eye.encode(fake_data)
        return real_coord
    return None


def test_runway_polling():
    print("=== Odins Net Runway Polling Demo ===\n")

    # Example runways
    public_hub = Runway(
        start_mask=10000,
        end_mask=10999,
        name="Odins-Hall-Public",
        is_public=True,
        poll_interval=86400.0,  # daily
    )

    private_chat = Runway(
        start_mask=55000,
        end_mask=55999,
        name="Private-Chat-Lloyd",
        prefix_filter="lloyd:",  # future filter example
        poll_interval=300.0,     # 5 min
    )

    # Poller managing both
    poller = RunwayPoller([public_hub, private_chat])

    print(f"Polling {len(poller.runways)} runways:")
    for r in poller.runways:
        print(f"  - {r}")

    # Run a simulated poll on one runway
    print("\nSimulating poll on Odins-Hall-Public...")
    discoveries = poller.poll_single_runway(
        runway=public_hub,
        max_results=3,
        simulate_fetch=simulate_real_fetch,
    )

    if not discoveries:
        print("No traffic found in this simulation run (try again – it's random)")
        return

    print(f"\nFound {len(discoveries)} new coordinate(s):")
    eye = OdinsEye()

    for item in discoveries:
        coord = item["coord"]
        mask = item["mask"]
        print(f"\nMask {mask}:")
        print(f"  Coord: {coord}")
        print(f"  From runway: {item['from_runway']}")

        try:
            decoded = eye.decode(coord)
            original_hash = coord.get("original_hash")
            computed_hash = hashlib.sha256(decoded).hexdigest()

            print(f"  Decoded {len(decoded)} bytes")
            print(f"  First 50 chars: {decoded[:50]!r}")
            print(f"  Hash match: {original_hash == computed_hash}")

            if original_hash == computed_hash:
                
                print("  ✓ Valid round-trip decode")
            else:
                print("  ✗ Hash mismatch – decode failed")

        except ValueError as e:
            print(f"  Decode failed: {e}")

    print("\nDemo complete. In real use: replace simulate_fetch with actual mesh/neighbor query.")


if __name__ == "__main__":
    test_runway_polling()
    
def test_send_and_poll_simulation():
    print("\n=== Messaging Send + Poll Integration Test ===\n")

    user = UserState("testuser")  # temp state
    eye = OdinsEye()
    poller = create_default_poller()

    # Compose & send a test message
    msg = Message(
        sender="testuser",
        recipient="friend",
        subject="Test Message via Runway",
        body="This is a simulated send → poll round-trip test!",
        mode="async",
    )

    send_result = send_message(user, eye, msg, use_hub=True)
    print("Sent message:", send_result)

    # Simulate poll (should "find" it if dummy fetch picks it up)
    print("\nPolling for the message...")
    discoveries = poller.poll_all(max_per_runway=5)

    found = False
    for runway, items in discoveries.items():
        for item in items:
            coord = item["coord"]
            try:
                received = Message.from_coord(coord)
                if received.subject == "Test Message via Runway":
                    print("Found our test message!")
                    print(f"From: {received.sender}")
                    print(f"Body: {received.body[:100]}...")
                    found = True
                    break
            except:
                pass

    if not found:
        print("No match in this simulation run (dummy fetch is random – try again)")

    print("Test complete.")
