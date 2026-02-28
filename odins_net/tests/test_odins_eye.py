# odins_net/tests/test_odins_eye.py
# Simple round-trip tests for OdinsEye (run manually or with pytest later)

from odins_net.core import OdinsEye
import hashlib


def test_round_trip(data: bytes, start_mask: int = 50000) -> None:
    eye = OdinsEye(start_mask=start_mask)
    coord = eye.encode(data)
    recovered = eye.decode(coord)
    
    original_hash = hashlib.sha256(data).hexdigest()
    recovered_hash = hashlib.sha256(recovered).hexdigest()
    
    success = recovered == data and original_hash == recovered_hash
    
    print(f"\nTest data: {data!r} ({len(data)} bytes)")
    print(f"Start mask: {start_mask}")
    print(f"Coord: {coord}")
    print(f"Recovered matches original: {success}")
    print(f"Hash match: {original_hash == recovered_hash}")
    if not success:
        print("FAIL - round-trip broken!")
    else:
        print("PASS ✓")


if __name__ == "__main__":
    print("OdinsEye Round-Trip Tests\n" + "="*30)
    
    # Empty
    test_round_trip(b"")
    
    # Short text
    test_round_trip(b"Hello, Odin!")
    
    # Longer to likely trigger bounce
    test_round_trip(b"Odin's Net is an offline temporal mesh using lattice coordinates. "
                    b"This is a longer test string to force at least one bounce/reset.")
    
    # Binary-ish (random-like)
    test_round_trip(bytes(range(256)) * 4)  # 1024 bytes
    
    print("\nAll tests complete.")
