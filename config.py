import argparse
from dataclasses import dataclass

@dataclass
class BNSConfig:
    mask_base: int = 1_000_000
    chart_base: int = 256
    num_digits: int = 12
    num_n_streams: int = 8
    direction: int = 1

def get_config():
    parser = argparse.ArgumentParser(description="PROJECT ODIN - Burris Numerical System (Alpha)")
    parser.add_argument('--mask-base', type=int, default=1_000_000)
    parser.add_argument('--chart-base', type=int, default=256)
    parser.add_argument('--digits', type=int, default=12)
    parser.add_argument('--n-streams', type=int, default=8)
    parser.add_argument('--direction', type=int, default=1, choices=[-1, 1])
    parser.add_argument('--input', '-i', help='Input file')
    parser.add_argument('--output', '-o', help='Output file')
    parser.add_argument('--mode', choices=['encode', 'decode'], default='encode')
    
    args = parser.parse_args()
    
    config = BNSConfig(
        mask_base=args.mask_base,
        chart_base=args.chart_base,
        num_digits=args.digits,
        num_n_streams=args.n_streams,
        direction=args.direction
    )
    
    print(f"✅ BNS Alpha Loaded | Digits: {config.num_digits} | N-Streams: {config.num_n_streams} | Base: {config.chart_base}")
    return config, args
