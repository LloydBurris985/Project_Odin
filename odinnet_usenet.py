"""                                             odinnet_usenet.py
OdinNet Usenet Newsfeed Protocol — Integration Layer

Binds the geometric newsgroup protocol directly to LatticeFS v2 (Space 3+).
"""

import hashlib
import json
import time
from typing import List, Optional, Dict

# Import from the Phase 2 filesystem script provided by the user
from lattice_fs_v2 import LatticeFSv2, FileEntry

# Allocate Space 3 explicitly for Usenet matrix distribution
SPACE_USENET = 3
WINDOW_SIZE  = 1_000_000  # Virtual block range size per newsgroup

class OdinNetUsenet:
    def __init__(self, fs: LatticeFSv2, defcon: int = 1):
        """
        Initialises the Usenet Layer on top of LatticeFS v2.
        
        defcon: 1 (Normal), 3 (Filter low reputation), 5 (Strict signature block)
        """
        self.fs = fs
        self.defcon = defcon
        self.reputation_matrix: Dict[str, int] = {} # Simulated peer tracking
        
        # Formally register the coordinate space inside the engine
        if SPACE_USENET not in self.fs._store._spaces.all_spaces():
            self.fs.define_space(SPACE_USENET, "usenet_feed")

    # ── Geometric Calculations ───────────────────────────────────────────────

    def _get_group_window(self, group_name: str) -> int:
        """
        Deterministically derives the anchor coordinate for a newsgroup name
        using the filesystem's master passphrase seed.
        """
        passphrase = self.fs._store._passphrase or "odinnet_fallback_seed"
        raw_hash = hashlib.sha256((passphrase + group_name).encode("utf-8")).hexdigest()
        
        # Mask anchor coordinate space to safely fit within standard BNS bounds
        v_anchor = int(raw_hash, 16) % 100_000_000_000
        return v_anchor

    def _generate_provenance_sig(self, v_target: int, epoch: int) -> str:
        """Computes the geometric provenance validation string."""
        passphrase = self.fs._store._passphrase or "odinnet_fallback_seed"
        payload = f"{passphrase}:{epoch}:{v_target}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # ── Core Protocol Actions ────────────────────────────────────────────────

    def post(self, group: str, subject: str, body: str, parent_v: int = 0) -> int:
        """
        Executes a 'Beacon Drop' into the BNS space layer.
        Calculates geometric coordinates, signs the provenance, and writes data frame.
        """
        v_anchor = self._get_group_window(group)
        epoch = int(time.time() // 3600)  # 1-hour horison frame synchronization
        subject_hash = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:16]
        
        # Scan local index to see how many slots exist under this virtual group layout
        existing_heads = self.fs._store._versions.all_live_heads(space_id=SPACE_USENET)
        slot_index = sum(1 for p in existing_heads if p.startswith(f"/usenet/{group}/"))
        
        v_target = v_anchor + slot_index

        # Assemble canonical MessageFrame
        message_frame = {
            "v_target": str(v_target),
            "parent_v": str(parent_v),
            "subject_hash": subject_hash,
            "epoch": epoch,
            "rep_min": 0,
            "provenance_sig": self._generate_provenance_sig(v_target, epoch),
            "subject": subject,
            "body": body,
            "sender_node": hashlib.sha256(subject_hash.encode()).hexdigest()[:8] # Anonymous marker
        }

        # Serialise frame and drop it into the immutable coordinate store
        virtual_path = f"/usenet/{group}/msg_{slot_index}"
        self.fs.write_file(virtual_path, json.dumps(message_frame).encode("utf-8"), space_id=SPACE_USENET)
        
        return v_target

    def poll(self, group: str, scan_depth: int = 50) -> List[dict]:
        """
        Sweeps the designated coordinate window blocks and parses valid message arrays.
        Enforces defensive DEFCON gates against structural signature anomalies.
        """
        v_anchor = self._get_group_window(group)
        current_epoch = int(time.time() // 3600)
        valid_frames = []

        for slot in range(scan_depth):
            virtual_path = f"/usenet/{group}/msg_{slot}"
            if not self.fs.exists(virtual_path):
                continue
                
            try:
                raw_data = self.fs.read_file(virtual_path, space_id=SPACE_USENET)
                frame = json.loads(raw_data.decode("utf-8"))
                
                v_target = int(frame["v_target"])
                epoch = int(frame["epoch"])
                sender = frame.get("sender_node", "unknown")
                
                # ── DEFCON Gate Intercepts ──
                # Check 1: Geometric signature validation
                expected_sig = self._generate_provenance_sig(v_target, epoch)
                if frame["provenance_sig"] != expected_sig:
                    if self.defcon >= 5:
                        print(f"[DEFCON 5 Intercept] Dropped spoofed frame signature at V={v_target}")
                        continue
                    
                # Check 2: Reputation limits (DEFCON 3+)
                if self.defcon >= 3:
                    if self.reputation_matrix.get(sender, 100) < 0:
                        print(f"[DEFCON 3 Intercept] Dropped frame from blacklisted sender: {sender}")
                        continue
                        
                valid_frames.append(frame)
                
            except Exception as e:
                # Fail silently during structural mapping exceptions to shield engine loops
                continue

        return valid_frames

    # ── Thread Tree Assembly ──────────────────────────────────────────────────

    def build_threads(self, frames: List[dict]) -> Dict[str, List[dict]]:
        """
        Assembles a list of flat coordinate frames into nested relational thread chains
        without checking or mutating external system databases.
        """
        threads: Dict[str, List[dict]] = {}
        
        # First pass: map out primary root entries
        for frame in frames:
            if int(frame["parent_v"]) == 0:
                threads[frame["v_target"]] = [frame]
                
        # Second pass: append replies down matching their parental coordinate paths
        for frame in frames:
            parent = frame["parent_v"]
            if parent != "0" and parent in threads:
                threads[parent].append(frame)
                
        return threads


# ===========================================================================
# VERIFICATION SUITE
# ===========================================================================

if __name__ == "__main__":
    from lattice_fs_v2 import lattice_fs_v2
    
    print("=" * 65)
    print("  OdinNet Usenet Protocol Layer — Integration Test")
    print("=" * 65)

    # Boot local memory instance of the LatticeFS v2 base engine
    raw_fs = lattice_fs_v2(sector_size=1024, n_sectors=128, passphrase="alpha_omega_net")
    usenet = OdinNetUsenet(raw_fs, defcon=1)

    print("\n[Step 1] Posting root thread to sci.burris.odinnet...")
    root_v = usenet.post(
        group="sci.burris.odinnet",
        subject="BNS Clock Synch Experiments",
        body="Node Alpha online. Initialising coordinate mapping sequences."
    )
    print(f"  Root beacon dropped successfully. Target V coordinate: {root_v}")

    print("\n[Step 2] Dropping inline message reply...")
    reply_v = usenet.post(
        group="sci.burris.odinnet",
        subject="Re: BNS Clock Synch Experiments",
        body="Node Beta acknowledges. Synchronization offset calculates cleanly within limits.",
        parent_v=root_v
    )
    print(f"  Reply beacon dropped successfully. Target V coordinate: {reply_v}")

    print("\n[Step 3] Polling coordinate space window...")
    active_feed = usenet.poll("sci.burris.odinnet")
    assert len(active_feed) == 2
    print(f"  Scan successful. Found {len(active_feed)} verified frames within the window parameters.")

    print("\n[Step 4] Reassembling thread hierarchies...")
    discussion_tree = usenet.build_threads(active_feed)
    
    for thread_id, posts in discussion_tree.items():
        print(f"\n  ◆ Thread Anchor V: {thread_id} [Subject: '{posts[0]['subject']}']")
        for index, post in enumerate(posts):
            indent = "    └── " if index > 0 else "    "
            print(f"{indent}[Node {post['sender_node']}]: \"{post['body']}\"")

    print("\n[Step 5] Engaging DEFCON 3 Reputation Intercepts...")
    # Inject an entry, flag it negatively in reputation table
    bad_post_v = usenet.post("sci.burris.odinnet", "SPAM", "Malicious telemetry transmission.")
    bad_feed = usenet.poll("sci.burris.odinnet")
    malicious_sender = bad_feed[-1]["sender_node"]
    
    # Apply negative reputation rank block
    usenet.reputation_matrix[malicious_sender] = -50
    usenet.defcon = 3
    
    # Re-poll with high alerts enabled
    secure_feed = usenet.poll("sci.burris.odinnet")
    assert len(secure_feed) == 2 # Dropped the bad post cleanly
    print("  ✅ DEFCON 3 Gate fully filtered out blacklisted node coordinates.")

    print("\n" + "=" * 65)
    print("  All protocol validation benchmarks passed successfully.")
    print("=" * 65)
