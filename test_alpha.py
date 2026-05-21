from config import get_config
from bns_encoder_decoder import BNS_EncoderDecoder

if __name__ == "__main__":
    config, _ = get_config()
    engine = BNS_EncoderDecoder(config)
    
    # Create test file
    with open("test_input.txt", "wb") as f:
        f.write(b"PROJECT ODIN ALPHA TEST FILE\n" * 10000)
    
    print("Testing encode/decode roundtrip...")
    engine.encode_file("test_input.txt", "encoded.bin")
    engine.decode_file("encoded.bin", "decoded.txt")
    
    print("✅ Alpha test complete. Check decoded.txt")
