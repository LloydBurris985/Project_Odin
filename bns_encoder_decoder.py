import hashlib
import os
from odin_chart_generator import ChartGenerator  # Import from your alpha file

class BNS_EncoderDecoder:
    def __init__(self, chart_base=256, mask_base=1000000, num_digits=8):
        self.cg = ChartGenerator(chart_base=chart_base, mask_base=mask_base, num_digits=num_digits)

    def get_file_checksum(self, filepath):
        """Calculate SHA256 checksum"""
        with open(filepath, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()

    def encode_file(self, input_path, output_path):
        """Encode file backwards (your arrow of time rule)"""
        with open(input_path, 'rb') as f:
            data = f.read()
        
        # Encode backwards
        encoded = []
        for byte in reversed(data):
            encoded.append(self.cg.encode_byte(byte))
        
        # Write encoded bytes
        with open(output_path, 'wb') as f:
            f.write(bytes(encoded))
        
        original_hash = self.get_file_checksum(input_path)
        print(f"Encoded: {input_path} -> {output_path}")
        print(f"Original SHA256: {original_hash}")
        return original_hash

    def decode_file(self, input_path, output_path):
        """Decode file (reverses the backwards encoding)"""
        with open(input_path, 'rb') as f:
            data = f.read()
        
        decoded = []
        for byte in data:
            recovered = self.cg.decode_byte()
            decoded.append(recovered)
        
        # Reverse again to restore original order
        decoded = list(reversed(decoded))
        
        with open(output_path, 'wb') as f:
            f.write(bytes(decoded))
        
        decoded_hash = self.get_file_checksum(output_path)
        print(f"Decoded: {input_path} -> {output_path}")
        print(f"Decoded SHA256: {decoded_hash}")
        return decoded_hash
