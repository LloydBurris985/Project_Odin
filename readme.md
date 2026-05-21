# PROJECT ODIN - Burris Numerical System (BNS)

**A reversible mathematical universe for encoding, decoding, and temporal/offline communications.**

## Overview

PROJECT ODIN implements the Burris Numerical System (BNS) — a lattice-based coordinate system that treats information as positions in a deterministic mathematical universe. 

This alpha release provides the core components needed to create, navigate, and use these informational universes for secure, reversible data encoding and future temporal communications.

## Features (Alpha)

- **High-capacity ChartGenerator** with configurable digits (default 12) and parallel N streams (default 8)
- **Auto-derived states** using Point of Reference system for D and R
- **Folding support** via dynamic reference points
- **Bidirectional movement** (forward and backward)
- **Fast encode/decode** with multiple N streams
- **Configurable parameters** (mask base, chart base, digits, streams, direction)
- Basic file encoding/decoding with checksum verification

## Quick Start

```bash
# Clone the repo
git clone <your-repo-url>
cd project-odin

# Install (optional, no external deps needed)
pip install -r requirements.txt   # currently empty

# Run a quick test
python test_alpha.py
