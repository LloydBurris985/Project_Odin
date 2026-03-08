# README.md - Project Odin (Odins Net)

## What is this?

Temporal Intergalactic BBS â€“ a prototype messaging system built on the lattice (eternal, pre-existing information space).
Combines:
- Async chained mail (like email threads across time)
- Temporal delivery (send to future/past self)
- Usenet-style boards & threads (dynamic, paginated, subscribe/unsubscribe)

Core idea: All data already exists in the lattice. We don't create â€” we navigate & organize.

## Current Features (v0.1-alpha)
- Login & user state (saved to odin_state.json)
- Dynamic boards (Odins-Hall public, private runway, auto chain boards)
- Poll inbox (targeted chains first, fallback scan)
- Compose text posts (multi-line, board/private choice)
- Thread view (paginated, reply chaining)
- Subscription (S/U commands to curate feed)
- Flags: [UNSEC], [FUTURE], [PAST], [SUSPECT]
- Queue for future messages

## How to Run (CLI Prototype)
1. Python 3.12+ installed
2. Clone repo:
   git clone https://github.com/LloydBurris985/Project_Odin.git
3. cd Project_Odin/odins_net
4. Install deps:
   pip install cryptography pillow moviepy pydub
5. Run:
   python messaging.py
6. First time: enter username â†’ start posting/polling/replying

Commands:
  1+ : Enter board
  7  : Poll now
  8  : Compose (text/video stub)
  9  : Active chains (coming soon)
 10  : Queue (future messages)
 11  : Suspect / Flagged
 12  : The Thing (disputes)
  S  : Subscribe boards
  U  : Unsubscribe boards
  ?  : Help
  Q  : Quit

## Philosophy
The lattice is eternal. All information already exists.
No blocking traffic â€” only organize, flag, keep human-readable.
Temporal communication: send to future/past, chain replies across time.

## Next (v0.2+)
- Full video chunking demo
- Thread nesting / Re: prefix
- Search/filter in boards
- LNS (Lattice Naming Server â€“ decentralized DNS for coords)
- Carvers & daemon crawlers

MIT License â€“ free to use, modify, share.
Built by Lloyd Burris with Grok assistance.
"The lattice is eternal. All information already exists."
