# odins_net/nexus_hub.py
# Odins Hall – the primary public Network Access Point (NAP) / hub runway
# Every Odins Net node should poll this occasionally for discovery

from typing import List, Optional
from .runway import Runway, RunwayPoller
from .core import OdinsEye


# Fixed well-known Odins Hall runway (small, public, everyone knows these bounds)
ODINS_HALL_START = 10000
ODINS_HALL_END   = 10099   # 100 masks – enough for announcements, directory, updates
ODINS_HALL_NAME  = "Odins-Hall"
ODINS_HALL_POLL_INTERVAL = 86400.0  # once per day (86400 seconds)


def get_odins_hall_runway() -> Runway:
    """Return the standard Odins Hall public runway."""
    return Runway(
        start_mask=ODINS_HALL_START,
        end_mask=ODINS_HALL_END,
        name=ODINS_HALL_NAME,
        is_public=True,
        poll_interval=ODINS_HALL_POLL_INTERVAL,
        # Future: could add prefix_filter="hub:" or metadata schema
    )


def create_default_poller(extra_runways: Optional[List[Runway]] = None) -> RunwayPoller:
    """
    Create a poller that always includes Odins Hall + any user/private runways.
    
    This is the recommended way for nodes to start polling.
    """
    hub = get_odins_hall_runway()
    runways = [hub]
    
    if extra_runways:
        runways.extend(extra_runways)
    
    return RunwayPoller(runways)


def poll_odins_hall(max_results: int = 5, simulate_fetch=None) -> List[dict]:
    """
    Convenience: Poll only Odins Hall for new traffic.
    
    Useful for quick checks, onboarding new nodes, or background hub sync.
    """
    hub = get_odins_hall_runway()
    poller = RunwayPoller([hub])
    
    return poller.poll_single_runway(
        runway=hub,
        max_results=max_results,
        simulate_fetch=simulate_fetch,
    )


# Example / demo usage (can be run directly or used in tests)
if __name__ == "__main__":
    print("=== Odins Hall Hub Demo ===\n")
    
    # Quick poll of Odins Hall only
    print("Polling Odins Hall...")
    hub_discoveries = poll_odins_hall(max_results=3, simulate_fetch=None)  # uses dummy fetch
    
    if hub_discoveries:
        print(f"Found {len(hub_discoveries)} items in Odins Hall:")
        for item in hub_discoveries:
            print(f"  - Mask {item['mask']}: {item['decoded_size']} bytes decoded")
    else:
        print("No traffic found in this simulation (random dummy fetch)")
    
    # Or create full default poller with extra runways
    my_private = Runway(55000, 55999, name="My-Private")
    default_poller = create_default_poller([my_private])
    print(f"\nDefault poller created with {len(default_poller.runways)} runways (including Odins Hall)")
