# odins_net/messaging.py
# Unified Odins Mail + Odins Temporal: Async chained & live temporal messaging
# Uses runways for delivery, OdinsEye for encoding, AB for temporal features
# MIT License

from typing import Dict, Optional, List, Any
from .core import OdinsEye
from .rng import BNSRNG  # Assuming BNS RNG class exists in rng.py
from .runway import Runway, RunwayPoller
from .nexus_hub import get_odins_hall_runway
import hashlib
from cryptography.fernet import Fernet  # For encryption (optional dep, pip install cryptography)
import time
import json


class Message:
    """Base message structure with AB temporal metadata and chaining."""

    def __init__(
        self,
        sender: str,
        recipient: str,
        payload: bytes,                     # Raw content (text, file bytes, etc.)
        mode: str = "async",                # "async" (chained) or "live" (temporal)
        chain_id: Optional[str] = None,     # For threaded replies
        ab_target_time: Optional[int] = None,  # Unix timestamp for future delivery (AB)
        ab_window: int = 3600,              # Seconds tolerance for live mode
        secret: Optional[bytes] = None,     # Shared secret for encryption/chain seed
    ):
        self.sender = sender
        self.recipient = recipient
        self.payload = payload
        self.mode = mode.lower()
        self.chain_id = chain_id or hashlib.sha256(f"{sender}{recipient}{time.time()}".encode()).hexdigest()[:16]
        self.ab_target_time = ab_target_time
        self.ab_window = ab_window if mode == "live" else None
        self.secret = secret
        self.timestamp = int(time.time())
        self.eye = OdinsEye()

        # Encrypt payload if secret provided
        if secret:
            key = Fernet(secret).generate_key() if not secret else Fernet(secret)
            self.encrypted_payload = Fernet(key).encrypt(payload)
        else:
            self.encrypted_payload = payload

    def to_coord(self) -> Dict[str, Any]:
        """Encode message to lattice coordinate using OdinsEye."""
        serialized = json.dumps({
            "sender": self.sender,
            "recipient": self.recipient,
            "chain_id": self.chain_id,
            "timestamp": self.timestamp,
            "ab_target": self.ab_target_time,
            "mode": self.mode,
            "payload_hash": hashlib.sha256(self.payload).hexdigest(),
        }).encode() + b"|" + self.encrypted_payload

        # Encode full message as coord
        return self.eye.encode(serialized)

    @classmethod
    def from_coord(cls, coord: Dict[str, Any], secret: Optional[bytes] = None) -> "Message":
        """Decode coord back to Message object."""
        eye = OdinsEye()
        raw = eye.decode(coord)
        parts = raw.split(b"|", 1)
        if len(parts) != 2:
            raise ValueError("Invalid message format")

        metadata = json.loads(parts[0])
        payload = parts[1]

        # Decrypt if secret
        if secret:
            payload = Fernet(secret).decrypt(payload)

        return cls(
            sender=metadata["sender"],
            recipient=metadata["recipient"],
            payload=payload,
            mode=metadata["mode"],
            chain_id=metadata["chain_id"],
            ab_target_time=metadata.get("ab_target"),
            secret=secret,
        )


def send_message(
    message: Message,
    target_runway: Optional[Runway] = None,
    use_hub: bool = True,
) -> Dict[str, Any]:
    """Send message by encoding to coord and "dropping" into runway (simulate for now)."""
    coord = message.to_coord()

    # Determine runway: recipient's private if known, else hub
    runway = target_runway or get_odins_hall_runway() if use_hub else None
    if not runway:
        raise ValueError("No runway specified and hub disabled")

    # In real impl: broadcast coord to mesh neighbors or drop in physical medium
    # For now: return coord + runway info
    return {
        "status": "dropped",
        "coord": coord,
        "runway": runway.name,
        "mask_hint": "random in range"  # Real: choose mask in runway
    }


def receive_messages(
    poller: RunwayPoller,
    secret: Optional[bytes] = None,
    max_per_runway: int = 10,
) -> List[Message]:
    """Poll runways and collect/verify messages addressed to you."""
    discoveries = poller.poll_all(max_per_runway=max_per_runway)
    messages = []

    for runway_name, items in discoveries.items():
        for item in items:
            coord = item["coord"]
            try:
                msg = Message.from_coord(coord, secret=secret)
                # Future: filter by recipient == my_id
                messages.append(msg)
            except ValueError:
                pass  # Invalid or not for us

    return messages


# Chained reply helper (async mode)
def reply_chained(
    previous_msg: Message,
    new_payload: bytes,
    secret: bytes,
) -> Message:
    """Create reply with chained coord prediction."""
    rng = BNSRNG(seed=int(hashlib.sha256(previous_msg.chain_id.encode() + secret).hexdigest(), 16))
    next_chain_offset = rng.next() % 1000  # Example: predict next mask offset
    # Future: use AB to make chain "future-entangled"
    return Message(
        sender=previous_msg.recipient,
        recipient=previous_msg.sender,
        payload=new_payload,
        mode="async",
        chain_id=previous_msg.chain_id,
        secret=secret,
    )


# Example usage
if __name__ == "__main__":
    secret = b"our_shared_secret_key_32_bytes!!"  # 32 bytes for Fernet

    msg = Message(
        sender="lloyd",
        recipient="friend",
        payload=b"Hey, this is a chained reply test!",
        mode="async",
        secret=secret,
    )

    sent = send_message(msg)
    print("Sent:", sent)

    # Simulate polling (in real: background thread)
    # poller = create_default_poller()
    # received = receive_messages(poller, secret=secret)
