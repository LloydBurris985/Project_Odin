cat > README.md << 'EOF'
# Project Odin — Burris Numerical System

**Starship Odin Temporal Communications & Informational Universe Engine**

A complete offline-first, temporal, coordinate-based communication and storage system built on reversible arithmetic coding.

## Core Components

### 1. ChartGenerator (`chart_generator.py`)
- Bidirectional arithmetic coding (UP / DOWN)
- Large-number hand-math (limb-based, no Python int bloat)
- Navigation system: sublight, hyperspace jumps, change_r, bookmarks
- Galactic Map with clean large-number display
- `write_disk_image()` — decode any coordinate directly to file
- **LatticeDrive** — paired-universe virtual block device (read/write heads)

### 2. GrokComms (`grok_comms.py`)
- Temporal Communications Node
- Coordinate-based messaging (your "temporal phone number")
- Polling range calculation (fixed-length random or real file sampling + std dev)
- Temporal Protocol (FROM_DATE / TO_DATE filtering)
- Persistent message store
- Interactive CLI (`temporal_comms`)

### 3. Key Features
- True temporal messaging (send to past/future with TO_DATE filtering)
- Offline-first design (no internet required)
- Reversible encoding — any file can be encoded to a coordinate and decoded anywhere
- Lattice Drive — virtual disk using two synchronized universes
- Navigation console with galactic map

## Quick Start

```bash
python grok_comms.py
