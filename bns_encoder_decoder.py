import hashlib
import os
from config import get_config
from chart_generator import ChartGenerator

class BNS_EncoderDecoder:
    def __init__(self, config):
        self.config = config
        self.chart = ChartGenerator(
            mask_base=config.mask_base,
            chart_base=config.chart_base,
            num_digits=config.num_digits,
            num_n_streams=config.num_n_streams,
            direction=config.direction
        )

    def encode_file(self, input_path, output_path):
        with open(input_path, 'rb') as f:
            data = f.read()
        
        original_hash = hashlib.sha256(data).hexdigest()
        
        # Simple encode for alpha (we will improve with chunks later)
        for byte in data:
            self.chart.encode_byte(n_index=0)
        
        with open(output_path, 'wb') as f:
            f.write(data)  # placeholder - in real version we would store encoded state
        
        print(f"Encoded {len(data)} bytes → {output_path}")
        return original_hash

    def decode_file(self, input_path, output_path):
        with open(input_path, 'rb') as f:
            data = f.read()
        
        decoded = []
        for _ in range(len(data)):
            byte = self.chart.fast_decode_byte(n_index=0)
            decoded.append(byte)
        
        with open(output_path, 'wb') as f:
            f.write(bytes(decoded))
        
        decoded_hash = hashlib.sha256(bytes(decoded)).hexdigest()
        print(f"Decoded to {output_path}")
        return decoded_hash


if __name__ == "__main__":
    config, args = get_config()
    engine = BNS_EncoderDecoder(config)
    
    if args.mode == 'encode' and args.input and args.output:
        engine.encode_file(args.input, args.output)
    elif args.mode == 'decode' and args.input and args.output:
        engine.decode_file(args.input, args.output)
    else:
        print("Usage: python bns_encoder_decoder.py --mode encode --input file.txt --output encoded.bin")
