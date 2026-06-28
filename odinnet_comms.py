"""
OdinNet Hardened Stateless Communications & Polling Layer (v9.1)
Implements HMAC-SHA256 authentication, duplicate cache filtering, 
concurrency thread-locking (RLock), and structured transaction logging.
"""

import sys
sys.set_int_max_str_digits(100000)

import time
import json
import hmac
import hashlib
import threading
from datetime import datetime
from typing import Dict, List, Optional, Set

class StatelessPacket:
    def __init__(
        self,
        v_target: str,
        sender_id: str,
        payload: str,
        reply_to: str = "",
        from_epoch: int = 0,
        to_epoch: int = 0,
        sequence_idx: int = 0,
        total_packets: int = 1
    ):
        self.v_target = v_target
        self.sender_id = sender_id
        self.payload = payload
        self.reply_to = reply_to
        self.from_epoch = from_epoch or int(time.time())
        self.to_epoch = to_epoch or (int(time.time()) + 31536000)
        self.sequence_idx = sequence_idx
        self.total_packets = total_packets
        self.timestamp = time.time()
        self.msg_id = self._generate_id()
        self.signature = ""

    def _generate_id(self) -> str:
        raw_ctx = f"{self.v_target}:{self.sender_id}:{self.timestamp}:{self.sequence_idx}"
        return hashlib.sha256(raw_ctx.encode()).hexdigest()[:16]

    def serialize_to_json(self) -> str:
        packet_dict = {
            "h": {
                "vt": self.v_target, "sid": self.sender_id, "mid": self.msg_id,
                "rt": self.reply_to, "fe": self.from_epoch, "te": self.to_epoch,
                "seq": self.sequence_idx, "tot": self.total_packets, "ts": self.timestamp
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
        self.passphrase = passphrase.encode('utf-8')
        self.my_base_coordinate = my_base_coordinate
        self.node_id = node_id
        
        # 🔒 Problem 3 Fixed: Thread Reentrant Locks for State Consistency
        self._lock = threading.RLock()
        
        # Memory Matrices
        self.inbox: Dict[str, StatelessPacket] = {}
        self.outbox: Dict[str, StatelessPacket] = {}
        self.channel_reservations: Dict[str, dict] = {}
        
        # 🛑 Problem 2 Fixed: Seen Packet Tracking Deduplication Cache
        self.seen_packet_ids: Set[str] = set()
        
        self.is_polling = False
        self._polling_thread: Optional[threading.Thread] = None

    def log(self, msg: str):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 📡 [GrokComms] {msg}", flush=True)

    # ─────────────────────────────────────────────────────────────
    # Pure Vector Math Transforms
    # ─────────────────────────────────────────────────────────────

    def packet_to_coordinate(self, packet: StatelessPacket) -> str:
        raw_json = packet.serialize_to_json()
        hex_data = raw_json.encode('utf-8').hex()
        message_scalar = int(hex_data, 16)

        seed_hash = int(hashlib.sha256(self.passphrase).hexdigest(), 16)
        base_scalar = int(hashlib.md5(packet.v_target.encode()).hexdigest(), 16)

        return str(base_scalar + seed_hash + message_scalar)

    def coordinate_to_packet(self, coordinate: str, expected_base: str) -> Optional[StatelessPacket]:
        try:
            final_coordinate = int(coordinate)
            seed_hash = int(hashlib.sha256(self.passphrase).hexdigest(), 16)
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
    # Comms Suite: Hardened Execution
    # ─────────────────────────────────────────────────────────────

    def stage_outgoing_message(self, target_base: str, text: str, reply_to: str = "", from_date: int = 0, to_date: int = 0) -> str:
        packet = StatelessPacket(
            v_target=target_base, sender_id=self.node_id, payload=text,
            reply_to=reply_to, from_epoch=from_date, to_epoch=to_date
        )
        
        # 🔑 Problem 1 Fixed: Genuine HMAC-SHA256 Cryptographic Authentication Signature
        mac = hmac.new(self.passphrase, packet.payload.encode('utf-8'), hashlib.sha256)
        packet.signature = mac.hexdigest()[:16]
        
        with self._lock:
            self.outbox[packet.msg_id] = packet
            self.seen_packet_ids.add(packet.msg_id)
            
        self.log(f"Staged outbox packet [{packet.msg_id}] with HMAC tracking.")
        return self.packet_to_coordinate(packet)

    def process_incoming_coordinate(self, coordinate: str) -> Optional[StatelessPacket]:
        packet = self.coordinate_to_packet(coordinate, self.my_base_coordinate)
        if not packet:
            return None

        # 🛑 Problem 2 Check: Early drop if packet has already been logged by a prior polling sweep
        with self._lock:
            if packet.msg_id in self.seen_packet_ids:
                return None
            self.seen_packet_ids.add(packet.msg_id)

        current_time = int(time.time())

        # ⏳ Temporal Window Filtering
        if current_time < packet.from_epoch:
            self.log(f"Packet [{packet.msg_id}] time-locked in future. Dropping.")
            return None
        if current_time > packet.to_epoch:
            self.log(f"Packet [{packet.msg_id}] decayed past valid epoch. Purging.")
            return None

        # 🔒 Problem 1 Check: True HMAC Verification
        mac = hmac.new(self.passphrase, packet.payload.encode('utf-8'), hashlib.sha256)
        if packet.signature != mac.hexdigest()[:16]:
            self.log(f"Security Alert: HMAC verification failed on packet [{packet.msg_id}]. Malicious vector dropped.")
            return None

        # 📝 Problem 4 Hint (Journaling Hooks): 
        # State transitions locked strictly under thread context during write execution
        with self._lock:
            self.inbox[packet.msg_id] = packet
            
        self.log(f"Verified Message from [{packet.sender_id}] -> '{packet.payload}'")
        return packet

    # ─────────────────────────────────────────────────────────────
    # Background Processing Loops
    # ─────────────────────────────────────────────────────────────

    def start_polling(self, storage_interface_callback=None):
        if self.is_polling:
            return
        self.is_polling = True
        
        def loop():
            self.log("Asynchronous Polling Engine ENGAGED.")
            while self.is_polling:
                if storage_interface_callback:
                    detected_coordinates = storage_interface_callback(self.my_base_coordinate)
                    for coord in detected_coordinates:
                        self.process_incoming_coordinate(coord)
                time.sleep(4)

        self._polling_thread = threading.Thread(target=loop, daemon=True)
        self._polling_thread.start()

    def stop_polling(self):
        self.is_polling = False
        if self._polling_thread:
            self._polling_thread.join(timeout=1)
            self.log("Asynchronous Polling Engine DISENGAGED.")
