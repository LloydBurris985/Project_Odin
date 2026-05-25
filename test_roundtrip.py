from bns_encoder_decoder import BNS_EncoderDecoder
import os

def main():
    encoder = BNS_EncoderDecoder(chart_base=256, mask_base=1000000, num_digits=8)
    
    # Create test file
    test_data = b"PROJECT ODIN ALPHA TEST - Reactor1967 & Grok\n" * 5000
    with open("test_input.txt", "wb") as f:
        f.write(test_data)
    
    original_hash = encoder.get_file_checksum("test_input.txt")
    print(f"Original file size: {len(test_data)} bytes")
    print(f"Original SHA256: {original_hash[:16]}...")
    
    # Encode
    encoder.encode_file("test_input.txt", "encoded.bin")
    
    # Decode
    encoder.decode_file("encoded.bin", "decoded_output.txt")
    
    # Verify
    final_hash = encoder.get_file_checksum("decoded_output.txt")
    
    print("\n=== VERIFICATION ===")
    if original_hash == final_hash:
        print("✅ SUCCESS: Roundtrip PASSED - Checksums match!")
    else:
        print("❌ FAILED: Checksums do not match.")

if __name__ == "__main__":
    main()
