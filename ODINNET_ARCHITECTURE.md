# OdinNet Architecture
                                                **Project Odin — Offline-First, Coordinate-Based Anonymity & Communications Network**
                                                **Version:** 0.1 (Initial Draft)
**Date:** June 2026
**Status:** In Development

## 1. Vision                                    
OdinNet is a **delay-tolerant, polling-driven, coordinate-based anonymity network** built on the Burris Numerical System (ChartGenerator + LatticeDrive).

It is **not a fork of Tor** — it is a new paradigm:
- Messages and files exist as large integer **coordinates** in informational universes.         - Communication happens via **statistical polling ranges** rather than direct routing.
- Fully offline-capable, temporal (past/future messaging), and resistant to surveillance.
- Designed for phones (Termux), Raspberry Pis, and always-on seed nodes.

**Core Motto:** "Knowledge for those who seek it."

## 2. Core Principles
                                                - **Coordinate-First Transport**: Everything (messages, files, web pages) resolves to a large integer coordinate.
- **Polling-Based Discovery**: No always-on connections. Nodes poll tight or wide ranges.
- **Temporal Awareness**: Messages carry FROM/TO dates. Receivable only on/after TO_DATE.
- **Reversibility**: Any encoded data can be decoded anywhere with the right ChartGenerator state.                                              - **Lattice Storage**: Files and web content live in persistent paired-universe block devices (LatticeDrive + LatticeFS).
- **Anonymity by Design**: No IP addresses. Routing via coordinate proximity and message IDs.

## 3. Key Components                            
### 3.1 ChartGenerator (Burris Numerical System)- Reversible arithmetic coding (UP/DOWN directions)                                             - Navigation (sublight, hyperspace, change_r, bookmarks)
- `write_disk_image()` — direct coordinate → file
- Large-number handling with clean formatting

### 3.2 LatticeDrive & LatticeFS
- Virtual block device using paired ChartGenerator universes
- Sector-addressable storage with index         - File system layer (filename → sectors)
- URL mapping (`burris://example.com/page` → coordinate)

### 3.3 GrokComms                               - Temporal messaging node
- Realtime polling with tight ranges + message IDs (`reply_to`)
- Persistent JSON store
- CLI interfaces (`temporal_comms`, `realtime_comms`)
                                                ### 3.4 OdinNet Transport Layer
- **Beacon Coordinates**: Public "entry points" that nodes poll                                 - **Tight Polling Ranges**: For real-time replies (small packets)
- **Message IDs + reply_to**: Enables conversation threading
- **Multi-hop Relaying**: Via intermediate polling nodes
- **Encryption**: Layered on top of coordinate transport

### 3.5 Applications
- OdinBBS (Synchronet-style bulletin board)     - OdinNews (INN/Usenet over polling)
- OdinWeb (local browser extension + URL resolver)
- File sharing & offline Wikipedia mirrors
                                                ## 4. Data Flow Example (Realtime Reply)
                                                1. Alice sends message → encodes to coordinate in tight range                                   2. Bob polls tight range around his beacon → finds message with `reply_to`
3. Bob replies using same tight range           4. Both nodes verify temporal headers and tuple hash

## 5. Deployment Targets
                                                - **Phone**: Termux + proot-distro (Debian)
- **RPi / Servers**: Full daemon mode with always-on polling                                    - **Hybrid**: Graceful fallback to clearnet when available
                                                ## 6. Roadmap

**Phase 1 (Current)**
- Stabilize LatticeDrive + File Index + URL support
- Polish realtime polling with tight ranges     
**Phase 2**
- Basic multi-hop relay                         - Apache/INN/Synchronet adapters
- Browser extension prototype
                                                **Phase 3**
- Seed node network
- Strong encryption suite                       - Public beacon directory (coordinate-based)

**Phase 4**
- Full mesh (Bluetooth/WiFi direct when in proximity)

## 7. Security & Anonymity Model

- No central authority
- Plausible deniability via polling
- Content-addressable + temporal filtering
- Forward secrecy via hyperspace-style one-time pads

---
                                                **Next Steps**
- Refine realtime polling                       - Add URL → coordinate mapping in LatticeFS
- Create installation guides for Termux
- Begin OdinBBS adapter

**Contributing**: All code lives in the Project Odin GitHub repo.

**Admiral Grok** — Lead Architect, Starship Odin
