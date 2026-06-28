"""
OdinNet Stateless Communications & Polling Layer (v9.0)
De-coupled from HTTP. Implements Pure Packet Headers, Realtime Threads, 
and Temporal Time-Locks using the Burris Numerical System vectors.
"""

import sys
sys.set_int_max_str_digits(100000)

import time
import json
import hashlib
import threading
from datetime import datetime
from typing import Dict, List, Optional

class StatelessPacket:
    """
    Defines the layout of an autonomous geometric packet inside OdinNet.
    Everything serializes down into a unified text schema before 
    transforming into a massive scalar coordinate.
    """
    def __init__(
        self,
        v_target: str,          # Target recipient base coordinate
        sender_id: str,         # Alias or sender node ID
        payload: str,           # Plaintext message text, or raw string representation of media data
        reply_to: str = "",     # Message ID being replied to (for threading)
        from_epoch: int = 0,    # Temporal lock start (Unix timestamp)
        to_epoch: int = 0,      # Temporal lock expiration (Unix timestamp)
        sequence_idx: int = 0,  # Packet index for large content arrays (voice/video)
        total_packets: int = 1  # Total packet count in the transmission chain
    ):
        self.v_target = v_target
        self.sender_id = sender_id
        self.payload = payload
        self.reply_to = reply_to
        self.from_epoch = from_epoch or int(time.time())
        self.to_epoch = to_epoch or (int(time.time()) + 31536000) # Default 1 year TTL
        self.sequence_idx = sequence_idx
        self.total_packets = total_packets
        self.timestamp = time.time()
        
        # Unique deterministic identifier for thread tracking
        self.msg_id = self._generate_id()
        self.signature = ""

    def _generate_id(self) -> str:
        raw_ctx = f"{self.v_target}:{self.sender_id}:{self.timestamp}:{self.sequence_idx}"
        return hashlib.sha256(raw_ctx.encode()).hexdigest()[:16]

    def serialize_to_json(self) -> str:
        """Packs the header definitions and payload neatly for vector transformation."""
        packet_dict = {
            "h": {
                "vt": self.v_target,
                "sid": self.sender_id,
                "mid": self.msg_id,
                "rt": self.reply_to,
                "fe": self.from_epoch,
                "te": self.to_epoch,
                "seq": self.sequence_idx,
                "tot": self.total_packets,
                "ts": self.timestamp
            },
            "p": self.payload,
            "sig": self.signature
        }
        return json.dumps(packet_dict, separators=(',', ':'))

    @staticmethod
    def deserialize_from_json(json_str: str) -> "StatelessPacket":
        d = json.loads(json_str)
        h = d["h"]
        pkt = StatelessPacket(
            v_target=h["vt"], sender_id=h["sid"], payload=d["p"],
            reply_to=h["rt"], from_epoch=h["fe"], to_epoch=h["te"],
            sequence_idx=h["seq"], total_packets=h["tot"]
        )
        pkt.msg_id = h["mid"]
        pkt.timestamp = h["ts"]
        pkt.signature = d.get("sig", "")
        return pkt


class OdinCommsEngine:
    def __init__(self, passphrase: str, my_base_coordinate: str, node_id: str):
        self.passphrase = passphrase
        self.my_base_coordinate = my_base_coordinate
        self.node_id = node_id
        
        # Local in-memory transaction archives (To be integrated into LatticeFSv2 by Scotty)
        self.inbox: Dict[str, StatelessPacket] = {}
        self.outbox: Dict[str, StatelessPacket] = {}
        self.channel_reservations: Dict[str, dict] = {} # Live channel schedules
        
        self.is_polling = False
        self._polling_thread: Optional[threading.Thread] = None

    def log(self, msg: str):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 📡 [GrokComms] {msg}", flush=True)

    # ─────────────────────────────────────────────────────────────
    # Pure Vector Math Transforms
    # ─────────────────────────────────────────────────────────────

    def packet_to_coordinate(self, packet: StatelessPacket) -> str:
        """Translates a structured packet object straight into a raw scalar coordinate."""
        raw_json = packet.serialize_to_json()
        hex_data = raw_json.encode('utf-8').hex()
        message_scalar = int(hex_data, 16)

        seed_hash = int(hashlib.sha256(self.passphrase.encode()).hexdigest(), 16)
        base_scalar = int(hashlib.md5(packet.v_target.encode()).hexdigest(), 16)

        final_coordinate = base_scalar + seed_hash + message_scalar
        return str(final_coordinate)

    def coordinate_to_packet(self, coordinate: str, expected_base: str) -> Optional[StatelessPacket]:
        """Reverses the arithmetic transformation to extract the structured packet object."""
        try:
            final_coordinate = int(coordinate)
            seed_hash = int(hashlib.sha256(self.passphrase.encode()).hexdigest(), 16)
            base_scalar = int(hashlib.md5(expected_base.encode()).hexdigest(), 16)

            message_scalar = final_coordinate - base_scalar - seed_hash
            if message_scalar <= 0:
                return None

            hex_data = hex(message_scalar)[2:]
            if len(hex_data) % 2 != 0:
                hex_data = '0' + hex_data

            json_str = bytes.fromhex(hex_data).decode('utf-8', errors='ignore')
            return StatelessPacket.deserialize_from_json(json_str)
        except Exception:
            return None

    # ─────────────────────────────────────────────────────────────
    # Comms Suite: Dispatch & Temporal Filtering
    # ─────────────────────────────────────────────────────────────

    def stage_outgoing_message(self, target_base: str, text: str, reply_to: str = "", from_date: int = 0, to_date: int = 0) -> str:
        """Constructs, hashes, and queues a message inside the Outbox."""
        packet = StatelessPacket(
            v_target=target_base, sender_id=self.node_id, payload=text,
            reply_to=reply_to, from_epoch=from_date, to_epoch=to_date
        )
        # Sign the packet via SHA256 of the payload + passphrase
        packet.signature = hashlib.sha256((packet.payload + self.passphrase).encode()).hexdigest()[:10]
        
        self.outbox[packet.msg_id] = packet
        self.log(f"Staged outbox packet [{packet.msg_id}] targeting vector range {target_base[:8]}...")
        return self.packet_to_coordinate(packet)

    def process_incoming_coordinate(self, coordinate: str) -> Optional[StatelessPacket]:
        """Evaluates an intercepted coordinate. Enforces temporal locks and validates signatures."""
        packet = self.coordinate_to_packet(coordinate, self.my_base_coordinate)
        if not packet:
            return None

        current_time = int(time.time())

        # ⏳ Temporal Time-Lock Verification
        if current_time < packet.from_epoch:
            self.log(f"Intercepted packet [{packet.msg_id}] is locked in the future space. Delta: {packet.from_epoch - current_time}s. Dropping.")
            return None
        if current_time > packet.to_epoch:
            self.log(f"Intercepted packet [{packet.msg_id}] has decayed past its valid epoch window. Purging.")
            return None

        # 🔒 Hash Signature Verification
        expected_sig = hashlib.sha256((packet.payload + self.passphrase).encode()).hexdigest()[:10]
        if packet.signature != expected_sig:
            self.log(f"Warning: Packet [{packet.msg_id}] failed hash validation. Counterfeit coordinate vector.")
            return None

        # Store cleanly into history
        self.inbox[packet.msg_id] = packet
        self.log(f"Success: Received message from [{packet.sender_id}] -> Payload: '{packet.payload}'")
        return packet

    # ─────────────────────────────────────────────────────────────
    # Live Channel Reservations
    # ─────────────────────────────────────────────────────────────

    def reserve_live_channel(self, target_node_id: str, target_base: str, start_timestamp: int, end_timestamp: int):
        """Schedules an isolated rolling channel assignment between two distinct nodes."""
        channel_id = hashlib.md5(f"{self.node_id}:{target_node_id}:{start_timestamp}".encode()).hexdigest()[:8]
        self.channel_reservations[channel_id] = {
            "target_id": target_node_id,
            "target_base": target_node_id,
            "start": start_timestamp,
            "end": end_timestamp,
            "active": False
        }
        self.log(f"Live channel channel [{channel_id}] reserved for window {start_timestamp} -> {end_timestamp}")

    # ─────────────────────────────────────────────────────────────
    # Asynchronous Background Polling Loop
    # ─────────────────────────────────────────────────────────────

    def start_polling(self, storage_interface_callback=None):
        """Spins up an independent thread that continuously sweeps the coordinate universe."""
        if self.is_polling:
            return
        self.is_polling = True
        
        def loop():
            self.log("Background Polling Cycle ENGAGED.")
            while self.is_polling:
                # 1. Sweep local coordination spaces (Simulated peer exchange / LatticeFS vector drops)
                if storage_interface_callback:
                    detected_coordinates = storage_interface_callback(self.my_base_coordinate)
                    for coord in detected_coordinates:
                        self.process_incoming_coordinate(coord)
                
                # Default background sweep heartbeat (e.g., check every 4 seconds)
                time.sleep(4)

        self._polling_thread = threading.Thread(target=loop, daemon=True)
        self._polling_thread.start()

    def stop_polling(self):
        self.is_polling = False
        if self._polling_thread:
            self._polling_thread.join(timeout=1)
            self.log("Background Polling Cycle DISENGAGED.")
