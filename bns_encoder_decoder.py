import hashlib
import os
from Tricked_ChartGenerator4 import ChartGenerator

class BNS:
    def __init__(self, num_digits=12, num_n_streams=4):
        self.chart = ChartGenerator(num_digits=num_digits, num_n_streams=num_n_streams)

    def encode_file(self, input_path, output_path):
        with open(input_path, 'rb') as f:
            data = f.read()

        original_hash = hashlib.sha256(data).hexdigest()

        # Encode
        for byte in data:
            self.chart.encode_byte(n_index=0)

        # For alpha we save the original (we'll improve later)
        with open(output_path, 'wb') as f:
            f.write(data)

        print(f"Encoded {len(data)} bytes -> {output_path}")
        return original_hash

    def decode_file(self, input_path, output_path):
        with open(input_path, 'rb') as f:
            data = f.read()

        decoded = []
        for _ in range(len(data)):
            byte = self.chart.decode_byte(n_index=0)
            decoded.append(byte)

        decoded_bytes = bytes(decoded)
        with open(output_path, 'wb') as f:
            f.write(decoded_bytes)

        decoded_hash = hashlib.sha256(decoded_bytes).hexdigest()
        print(f"Decoded to {output_path}")
        return decoded_hash


if __name__ == "__main__":
    bns = BNS()
    bns.encode_file("test_input.txt", "encoded.bin")
    bns.decode_file("encoded.bin", "decoded.txt")
