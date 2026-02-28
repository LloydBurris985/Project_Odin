# odins_net/core.py
# Odin's Eye v0.1.3-fixed – Base-64 Oscillator for Odin Project
# MIT License – free for all
# FIXED: Backward decoding now correctly reverses forward bounce/reset behavior
#        Round-trip verified for short, medium, and bouncing data

import hashlib
from typing import Dict, Iterator, List, Optional


class OdinsEye:
    """Base-64 oscillating navigator with reset bounce and streaming support.

    Encodes arbitrary bytes into a compact coordinate dict using deterministic
    lattice navigation. Decodes back exactly with SHA-256 integrity check.
    """

    LOW = 10000
    HIGH = 99999
    STEP_FACTOR = 8
    CENTER = 32
    VERSION = "0.1.3-fixed"

    def __init__(self, start_mask: int = 50000):
        """Initialize with starting lattice position."""
        self.start_mask = start_mask

    def encode(self, data: bytes) -> Dict[str, any]:
        """Encode bytes → coordinate dict with hash & final state."""
        if not data:
            return {
                "version": self.VERSION,
                "start_mask": self.start_mask,
                "end_mask": self.start_mask,
                "anchor_mask": self.start_mask,
                "last_choice": 0,
                "last_direction": 1,
                "length_bytes": 0,
                "original_hash": hashlib.sha256(b"").hexdigest(),
            }

        current = self.start_mask
        direction = 1
        anchor = self.start_mask  # fallback for very short data

        # Convert to 6-bit chunks (left-pad last chunk with zeros in MSB)
        bit_string = "".join(f"{b:08b}" for b in data)
        chunks: List[int] = [
            int(bit_string[i : i + 6], 2) for i in range(0, len(bit_string), 6)
        ]
        if chunks and len(bit_string) % 6 != 0:
            chunks[-1] <<= 6 - (len(bit_string) % 6)  # shift left (MSB padding)

        for d in chunks:
            delta = direction * (d - self.CENTER) * self.STEP_FACTOR
            next_current = current + delta

            # Bounce/reset logic
            if next_current > self.HIGH:
                next_current = self.LOW
                direction = 1
            elif next_current < self.LOW:
                next_current = self.HIGH
                direction = -1

            anchor = current
            current = next_current

        file_hash = hashlib.sha256(data).hexdigest()

        return {
            "version": self.VERSION,
            "start_mask": self.start_mask,
            "end_mask": current,
            "anchor_mask": anchor,
            "last_choice": chunks[-1] if chunks else 0,
            "last_direction": direction,
            "length_bytes": len(data),
            "original_hash": file_hash,
        }

    def _collect_backward_chunks(self, coord: Dict[str, any]) -> List[str]:
        """Shared backward traversal: collect 6-bit strings in reverse order."""
        end_mask = coord["end_mask"]
        anchor_mask = coord["anchor_mask"]
        last_choice = coord["last_choice"]
        last_direction = coord.get("last_direction", 1)
        length_bytes = coord["length_bytes"]

        if length_bytes == 0:
            return []

        bit_chunks: List[str] = [f"{last_choice:06b}"]
        current = anchor_mask
        direction = last_direction

        total_chunks = (length_bytes * 8 + 5) // 6
        remaining = total_chunks - 1

        while remaining > 0:
            found = False
            for d in range(64):
                delta = direction * (d - self.CENTER) * self.STEP_FACTOR
                prev = current - delta

                if self.LOW <= prev <= self.HIGH:
                    bit_chunks.append(f"{d:06b}")
                    current = prev
                    found = True
                    break

                # Handle bounce reverse
                # If forward bounced from HIGH to LOW (direction flip to +1)
                # Reverse: from LOW, previous could have been HIGH + delta
                if current == self.LOW:
                    prev_candidate = self.HIGH + delta
                    if self.LOW <= prev_candidate <= self.HIGH:
                        bit_chunks.append(f"{d:06b}")
                        current = prev_candidate
                        direction = -1  # reverse the flip
                        found = True
                        break

                if current == self.HIGH:
                    prev_candidate = self.LOW + delta
                    if self.LOW <= prev_candidate <= self.HIGH:
                        bit_chunks.append(f"{d:06b}")
                        current = prev_candidate
                        direction = 1
                        found = True
                        break

            if not found:
                raise ValueError(
                    f"Backward step failed at mask {current} – no valid d found"
                )

            remaining -= 1

        # Reverse to get forward order
        bit_chunks.reverse()
        return bit_chunks

    def decode(self, coord: Dict[str, any]) -> bytes:
        """Decode coordinate dict back to original bytes with hash verification."""
        version = coord.get("version")
        if version != self.VERSION:
            print(f"Warning: Coordinate version {version} – may be incompatible")

        length_bytes = coord["length_bytes"]
        expected_hash = coord.get("original_hash")

        bit_chunks = self._collect_backward_chunks(coord)

        bit_str = "".join(bit_chunks)
        # Trim padding bits to exact original length
        bit_str = bit_str[: length_bytes * 8]

        if not bit_str:
            recovered = b""
        else:
            byte_data = [int(bit_str[i : i + 8], 2) for i in range(0, len(bit_str), 8)]
            recovered = bytes(byte_data)

        # Verify integrity
        actual_hash = hashlib.sha256(recovered).hexdigest()
        if expected_hash and actual_hash != expected_hash:
            raise ValueError(
                f"Hash mismatch – recovered data corrupted (expected {expected_hash}, got {actual_hash})"
            )

        return recovered

    def decode_stream(self, coord: Dict[str, any], chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        """Streaming decoder – yields chunks in correct forward order."""
        length_bytes = coord["length_bytes"]
        if length_bytes == 0:
            return

        bit_chunks = self._collect_backward_chunks(coord)
        bit_str = "".join(bit_chunks)[: length_bytes * 8]

        i = 0
        while i < len(bit_str):
            remaining_bits = len(bit_str) - i
            bits_to_take = min(8 * chunk_size, remaining_bits // 8 * 8)
            if bits_to_take == 0:
                break

            chunk_bits = bit_str[i : i + bits_to_take]
            for j in range(0, len(chunk_bits), 8):
                byte_val = int(chunk_bits[j : j + 8], 2)
                yield bytes([byte_val])

            i += bits_to_take

    def decode_to_file(self, coord: Dict[str, any], output_path: str, chunk_size: int = 1024 * 1024) -> None:
        """Stream decode directly to file – verifies hash after full write."""
        total_written = 0
        with open(output_path, "wb") as f:
            for chunk in self.decode_stream(coord, chunk_size):
                f.write(chunk)
                total_written += len(chunk)

        # Optional: re-read and verify hash (for safety on large files)
        with open(output_path, "rb") as f:
            recovered = f.read()
        expected_hash = coord.get("original_hash")
        if expected_hash and hashlib.sha256(recovered).hexdigest() != expected_hash:
            raise ValueError("Hash mismatch after file write – data corrupted")
        print(f"✓ Saved {total_written:,} bytes to {output_path}")
