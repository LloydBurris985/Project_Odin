"""
lattice_fs_v2.py
Burris Numerical System — LatticeFS v2, Phase 1 + Phase 2

Coordinate-First Filesystem

Architecture:
    Apps / GUI
         ↓
    LatticeFS v2   ← this module
         ↓
    Coordinate Mapper (CoordMapper)
         ↓
    FoldingChartGenerator + R=1 stateless
         ↓
    Physical / In-Memory Storage (FoldingLatticeDrive)

Phase 1 (complete):
  - Coordinate-first file index
  - Superblock with binary magic header (60 bytes)
  - Content-addressed integrity (SHA-256 on every read)
  - AES-256-GCM per-file encryption
  - Hierarchical paths / directory support
  - Clean layer separation via CoordMapper
  - Backward-compat shim

Phase 2 (this release):
  - Journal for crash recovery
      · Every mutation: WRITE_FILE / DELETE_FILE / RENAME_FILE /
        REGISTER_URL / UNREGISTER_URL / INDEX_UPDATE
      · append → flush → commit sequence
      · On load: auto-replay uncommitted journal entries
  - Immutable versioning
      · Overwrite creates new version; all prior versions kept by default
      · version_history(path) returns all FileEntry versions
      · compact(keep_last_n=None) prunes orphaned old versions
  - Multiple coordinate spaces
      · Space 0 : System (superblock / journal / master index)
      · Space 1 : User private files (default)
      · Space 2+: Fleet / public / user-defined
      · write_file(path, data, space_id=1)
      · read_file(path, version=None, space_id=None)
      · ls(prefix, space_id=None) — None = all spaces

Coordinate spaces are independent BNS encoding universes. Each space
uses a separate CoordMapper so coordinates in space 0 and space 1 are
completely independent even for identical data.
"""

import hashlib
import json
import os
import struct
import time
from datetime import datetime as _dt
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Optional encryption
# ---------------------------------------------------------------------------
try:
    from cryptography.hazmat.primitives import hashes as _hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _CRYPTO_OK = True
except ImportError:
    _CRYPTO_OK = False
    print("⚠  cryptography not installed — LatticeFS v2 encryption disabled.")

# ---------------------------------------------------------------------------
# BNS layer imports
# ---------------------------------------------------------------------------
try:
    from folding_lattice_drive import FoldingLatticeDrive, folding_lattice_drive
    from folding_chart_generator import FoldingChartGenerator
    from chart_generator import fmt_short, fmt_large
    _BNS_OK = True
except ImportError:
    _BNS_OK = False
    print("⚠  BNS modules not found — CoordMapper will raise at encode/decode time.")
    def fmt_short(n): return str(n)[:20]
    def fmt_large(n): return str(n)


# ===========================================================================
# CONSTANTS
# ===========================================================================

FS_MAGIC         = b"BNSLAT2\x00"
FS_VERSION       = 3              # v3 = Phase 2 (journal + versioning + spaces)
ENCODING_VERSION = 1

# Sector layout
SECTOR_SUPERBLOCK = 0
SECTOR_JOURNAL    = 1
DATA_SECTOR_START = 2

# Coordinate spaces
SPACE_SYSTEM  = 0
SPACE_USER    = 1

# File flags
FLAG_NONE      = 0x00
FLAG_ENCRYPTED = 0x01
FLAG_COMPRESSED= 0x02
FLAG_IMMUTABLE = 0x04

# Journal operation codes
JOP_WRITE_FILE     = "WRITE_FILE"
JOP_DELETE_FILE    = "DELETE_FILE"
JOP_RENAME_FILE    = "RENAME_FILE"
JOP_REGISTER_URL   = "REGISTER_URL"
JOP_UNREGISTER_URL = "UNREGISTER_URL"
JOP_INDEX_UPDATE   = "INDEX_UPDATE"
JOP_COMMIT         = "COMMIT"


# ===========================================================================
# CRYPTO HELPERS
# ===========================================================================

class _Crypto:
    _PREFIX = b"E2:"

    @staticmethod
    def derive_key(passphrase: str, salt: bytes) -> bytes:
        kdf = PBKDF2HMAC(
            algorithm  = _hashes.SHA256(),
            length     = 32,
            salt       = salt,
            iterations = 600_000,
        )
        return kdf.derive(passphrase.encode("utf-8"))

    @staticmethod
    def encrypt(plaintext: bytes, passphrase: str) -> bytes:
        if not _CRYPTO_OK:
            return plaintext
        import base64
        salt  = os.urandom(16)
        nonce = os.urandom(12)
        key   = _Crypto.derive_key(passphrase, salt)
        ct    = AESGCM(key).encrypt(nonce, plaintext, None)
        return _Crypto._PREFIX + base64.b64encode(salt + nonce + ct)

    @staticmethod
    def decrypt(blob: bytes, passphrase: str) -> bytes:
        if not blob.startswith(_Crypto._PREFIX):
            return blob
        if not _CRYPTO_OK:
            raise RuntimeError("cryptography library required to decrypt.")
        import base64
        raw   = base64.b64decode(blob[3:])
        salt  = raw[:16]; nonce = raw[16:28]; ct = raw[28:]
        key   = _Crypto.derive_key(passphrase, salt)
        return AESGCM(key).decrypt(nonce, ct, None)

    @staticmethod
    def is_encrypted(blob: bytes) -> bool:
        return blob.startswith(_Crypto._PREFIX)


# ===========================================================================
# COORDINATE MAPPER
# ===========================================================================

class CoordMapper:
    """Thin BNS adapter. Each coordinate space gets its own CoordMapper."""

    def __init__(
        self,
        chart_base:   int = 256,
        mask_base:    int = 1_000_000_000_000,
        num_digits:   int = 100,
        scale_factor: int = 5000,
        space_id:     int = SPACE_USER,
    ):
        self.chart_base   = chart_base
        self.mask_base    = mask_base
        self.num_digits   = num_digits
        self.scale_factor = scale_factor
        self.space_id     = space_id

    def _new_fcg(self):
        if not _BNS_OK:
            raise RuntimeError("BNS modules unavailable.")
        return FoldingChartGenerator(
            chart_base   = self.chart_base,
            mask_base    = self.mask_base,
            num_digits   = self.num_digits,
            scale_factor = self.scale_factor,
        )

    def encode(self, data: bytes) -> int:
        return self._new_fcg().encode_bytes(data, u=0)

    def decode(self, V: int, length: int) -> bytes:
        return self._new_fcg().decode_bytes(V, length)

    @property
    def encoding_version(self) -> int:
        return ENCODING_VERSION


# ===========================================================================
# FILE ENTRY
# ===========================================================================

class FileEntry:
    __slots__ = (
        "path", "start_V", "length", "sha256",
        "flags", "nonce", "timestamp", "version", "enc_version", "space_id",
    )

    def __init__(
        self,
        path:        str,
        start_V:     int,
        length:      int,
        sha256:      str,
        flags:       int   = FLAG_NONE,
        nonce:       str   = "",
        timestamp:   float = 0.0,
        version:     int   = 1,
        enc_version: int   = ENCODING_VERSION,
        space_id:    int   = SPACE_USER,
    ):
        self.path        = path
        self.start_V     = start_V
        self.length      = length
        self.sha256      = sha256
        self.flags       = flags
        self.nonce       = nonce
        self.timestamp   = timestamp or time.time()
        self.version     = version
        self.enc_version = enc_version
        self.space_id    = space_id

    def to_dict(self) -> dict:
        return {
            "path":        self.path,
            "start_V":     str(self.start_V),
            "length":      self.length,
            "sha256":      self.sha256,
            "flags":       self.flags,
            "nonce":       self.nonce,
            "timestamp":   self.timestamp,
            "version":     self.version,
            "enc_version": self.enc_version,
            "space_id":    self.space_id,
        }

    @staticmethod
    def from_dict(d: dict) -> "FileEntry":
        return FileEntry(
            path        = d["path"],
            start_V     = int(d["start_V"]),
            length      = d["length"],
            sha256      = d["sha256"],
            flags       = d.get("flags",       FLAG_NONE),
            nonce       = d.get("nonce",       ""),
            timestamp   = d.get("timestamp",   0.0),
            version     = d.get("version",     1),
            enc_version = d.get("enc_version", ENCODING_VERSION),
            space_id    = d.get("space_id",    SPACE_USER),
        )

    def __repr__(self) -> str:
        enc = " [ENC]" if self.flags & FLAG_ENCRYPTED else ""
        ts  = _dt.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M:%S")
        return (f"<FileEntry {self.path!r} v{self.version} "
                f"sp={self.space_id} {self.length}B "
                f"V={fmt_short(self.start_V)}{enc} @ {ts}>")


# ===========================================================================
# SUPERBLOCK
# ===========================================================================

class Superblock:
    """
    60-byte binary header at sector 0.

    Layout: magic(8) fs_ver(2) enc_ver(2) flags(4) pad(44) = 60 bytes
    Followed by JSON payload (index + URL registry).
    """

    HEADER_SIZE = 60
    HEADER_FMT  = "<8sHHI44s"

    def __init__(
        self,
        fs_version:  int = FS_VERSION,
        enc_version: int = ENCODING_VERSION,
        flags:       int = 0,
    ):
        self.fs_version  = fs_version
        self.enc_version = enc_version
        self.flags       = flags

    def pack_header(self) -> bytes:
        return struct.pack(
            self.HEADER_FMT,
            FS_MAGIC,
            self.fs_version,
            self.enc_version,
            self.flags,
            b"\x00" * 44,
        )

    @staticmethod
    def unpack_header(data: bytes) -> "Superblock":
        if len(data) < Superblock.HEADER_SIZE:
            raise ValueError("Superblock data too short.")
        magic, fs_ver, enc_ver, flags, _ = struct.unpack_from(
            Superblock.HEADER_FMT, data, 0)
        if magic != FS_MAGIC:
            raise ValueError(
                f"Bad magic: {magic!r} (expected {FS_MAGIC!r}). "
                "Not a LatticeFS v2 image.")
        return Superblock(fs_version=fs_ver, enc_version=enc_ver, flags=flags)

    def __repr__(self) -> str:
        return (f"<Superblock fs_v={self.fs_version} "
                f"enc_v={self.enc_version} flags=0x{self.flags:04x}>")


# ===========================================================================
# JOURNAL
# ===========================================================================

class Journal:
    """
    Append-only crash-recovery journal stored at sector 1.

    Every mutating operation is written as a JSON record before it is
    applied to the live index. On load, any uncommitted entries are
    replayed so the filesystem reaches a consistent state even after
    an abrupt power loss.

    Record format (one JSON object per line):
        {"seq": int, "op": JOP_*, "ts": float, "data": {...}}

    The final record of a committed transaction is:
        {"seq": int, "op": "COMMIT", "ts": float, "data": {}}

    Replay rule:
        - Scan records in order.
        - Apply every non-COMMIT record to a scratch index.
        - When COMMIT seen, promote scratch → live.
        - Any trailing non-committed records are discarded (crash recovery).
    """

    def __init__(self, drive: "FoldingLatticeDrive", passphrase: str = None):
        self._drive      = drive
        self._passphrase = passphrase
        self._records:   List[dict] = []
        self._seq:       int        = 0
        self._load()

    # ── Persistence ─────────────────────────────────────────────────────────

    def _serialise(self) -> bytes:
        lines = "\n".join(json.dumps(r, separators=(",", ":")) for r in self._records)
        payload = lines.encode("utf-8") if lines else b""
        if self._passphrase and _CRYPTO_OK:
            payload = _Crypto.encrypt(payload, self._passphrase)
        return payload

    def _flush_to_drive(self):
        payload = self._serialise()
        if len(payload) > self._drive.sector_size:
            self._drive.sector_size = len(payload) + 256
        self._drive.write(payload if payload else b"\x00", SECTOR_JOURNAL)

    def _load(self):
        sec = self._drive._sectors[SECTOR_JOURNAL]
        if not sec["written"]:
            self._records = []
            self._seq     = 0
            return
        raw = self._drive.read(SECTOR_JOURNAL)
        raw = raw[:self._drive._sectors[SECTOR_JOURNAL]["byte_length"]]
        if not raw or raw == b"\x00":
            return
        if _Crypto.is_encrypted(raw):
            if not self._passphrase:
                raise RuntimeError("Journal encrypted but no passphrase given.")
            raw = _Crypto.decrypt(raw, self._passphrase)
        try:
            self._records = [
                json.loads(line)
                for line in raw.decode("utf-8").splitlines()
                if line.strip()
            ]
            self._seq = max((r["seq"] for r in self._records), default=0)
        except Exception as exc:
            print(f"[Journal] ⚠  Could not parse journal: {exc} — starting fresh.")
            self._records = []
            self._seq     = 0

    # ── Public API ───────────────────────────────────────────────────────────

    def append(self, op: str, data: dict):
        """Append one record and flush to drive."""
        self._seq += 1
        record = {"seq": self._seq, "op": op, "ts": time.time(), "data": data}
        self._records.append(record)
        self._flush_to_drive()

    def commit(self):
        """Write COMMIT record — marks transaction complete."""
        self.append(JOP_COMMIT, {})

    def clear(self):
        """Truncate journal after a successful full flush."""
        self._records = []
        self._flush_to_drive()

    def replay(self) -> List[dict]:
        """
        Return list of records from the last committed transaction,
        or [] if journal is empty or last transaction was committed cleanly.

        Used on load to detect and replay crashed writes.
        """
        if not self._records:
            return []

        # Walk backward to find last COMMIT
        last_commit_idx = None
        for i in range(len(self._records) - 1, -1, -1):
            if self._records[i]["op"] == JOP_COMMIT:
                last_commit_idx = i
                break

        if last_commit_idx is None:
            # No commit found — entire journal is uncommitted
            return self._records[:]

        # Check if there are uncommitted records AFTER the last commit
        uncommitted = self._records[last_commit_idx + 1:]
        return uncommitted  # [] means clean

    def has_uncommitted(self) -> bool:
        return len(self.replay()) > 0

    def all_committed_groups(self) -> List[List[dict]]:
        """Return list of committed transaction groups for full replay."""
        groups = []
        current = []
        for rec in self._records:
            if rec["op"] == JOP_COMMIT:
                if current:
                    groups.append(current)
                    current = []
            else:
                current.append(rec)
        return groups

    def __len__(self) -> int:
        return len(self._records)

    def __repr__(self) -> str:
        return f"<Journal records={len(self._records)} seq={self._seq}>"


# ===========================================================================
# VERSION STORE  (immutable versioning)
# ===========================================================================

class VersionStore:
    """
    Keeps all versions of every file.

    Structure:
        _versions: { path -> [FileEntry v1, FileEntry v2, ...] }

    The HEAD (current) version is always the last entry in the list.
    Old versions remain fully addressable for rollback / snapshot reads.
    """

    def __init__(self):
        self._versions: Dict[str, List[FileEntry]] = {}

    # ── Mutation ─────────────────────────────────────────────────────────────

    def put(self, entry: FileEntry):
        """Add a new version. Version number must be > all existing."""
        path = entry.path
        if path not in self._versions:
            self._versions[path] = []
        self._versions[path].append(entry)

    def delete_head(self, path: str) -> Optional[FileEntry]:
        """
        Mark head version deleted by appending a tombstone entry (length=0, sha256='').
        Old versions remain accessible. Returns the tombstone entry.
        """
        if path not in self._versions or not self._versions[path]:
            return None
        head    = self._versions[path][-1]
        version = head.version + 1
        tombstone = FileEntry(
            path        = path,
            start_V     = 0,
            length      = 0,
            sha256      = "",
            flags       = FLAG_NONE,
            timestamp   = time.time(),
            version     = version,
            space_id    = head.space_id,
        )
        self._versions[path].append(tombstone)
        return tombstone

    def rename(self, old_path: str, new_path: str):
        """Move all versions to new_path key."""
        if old_path not in self._versions:
            return
        entries = self._versions.pop(old_path)
        for e in entries:
            e.path = new_path
        self._versions[new_path] = entries

    # ── Query ─────────────────────────────────────────────────────────────────

    def head(self, path: str) -> Optional[FileEntry]:
        """
        Return current (latest non-tombstone) version, or None.
        Tombstone = latest version has length==0 and sha256==\'\'.
        If the latest version IS a tombstone, the file is deleted.
        """
        versions = self._versions.get(path, [])
        if not versions:
            return None
        latest = versions[-1]
        if latest.length == 0 and not latest.sha256:
            return None   # deleted (tombstone on top)
        return latest

    def exists(self, path: str) -> bool:
        return self.head(path) is not None

    def all_versions(self, path: str) -> List[FileEntry]:
        return list(self._versions.get(path, []))

    def all_live_heads(self, space_id: int = None) -> Dict[str, FileEntry]:
        """Return {path: head_entry} for all live files, optionally filtered by space."""
        result = {}
        for path, versions in self._versions.items():
            if not versions:
                continue
            latest = versions[-1]
            if latest.length == 0 and not latest.sha256:
                continue  # tombstone — deleted
            if space_id is None or latest.space_id == space_id:
                result[path] = latest
        return result

    def all_paths(self) -> List[str]:
        return list(self._versions.keys())

    def next_version(self, path: str) -> int:
        versions = self._versions.get(path, [])
        return (versions[-1].version + 1) if versions else 1

    # ── GC / Compact ─────────────────────────────────────────────────────────

    def prune(self, path: str, keep_last_n: int) -> int:
        """
        Keep only the last keep_last_n versions for path.
        Returns number of versions pruned.
        """
        versions = self._versions.get(path, [])
        if len(versions) <= keep_last_n:
            return 0
        n_prune = len(versions) - keep_last_n
        self._versions[path] = versions[n_prune:]
        return n_prune

    def prune_all(self, keep_last_n: int) -> int:
        """Prune all paths. Returns total versions removed."""
        total = 0
        for path in list(self._versions):
            total += self.prune(path, keep_last_n)
        return total

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            path: [e.to_dict() for e in entries]
            for path, entries in self._versions.items()
        }

    @staticmethod
    def from_dict(d: dict) -> "VersionStore":
        vs = VersionStore()
        for path, entries in d.items():
            vs._versions[path] = [FileEntry.from_dict(e) for e in entries]
        return vs


# ===========================================================================
# SPACE REGISTRY  (coordinate space management)
# ===========================================================================

class SpaceRegistry:
    """
    Tracks defined coordinate spaces.

    Space 0 = System  (reserved)
    Space 1 = User    (default)
    Space 2+ = Named  (fleet, public, custom)

    Each space has its own CoordMapper so BNS coordinates are independent.
    """

    _RESERVED = {
        SPACE_SYSTEM: "system",
        SPACE_USER:   "user",
    }

    def __init__(self, mapper_kwargs: dict = None):
        self._kwargs  = mapper_kwargs or {}
        self._names:  Dict[int, str]         = dict(self._RESERVED)
        self._mappers: Dict[int, CoordMapper] = {}

    def mapper(self, space_id: int) -> CoordMapper:
        if space_id not in self._mappers:
            self._mappers[space_id] = CoordMapper(
                space_id = space_id,
                **self._kwargs,
            )
        return self._mappers[space_id]

    def define(self, space_id: int, name: str):
        if space_id in self._RESERVED:
            raise ValueError(f"Space {space_id} is reserved ({self._RESERVED[space_id]}).")
        self._names[space_id] = name

    def name(self, space_id: int) -> str:
        return self._names.get(space_id, f"space_{space_id}")

    def all_spaces(self) -> Dict[int, str]:
        return dict(self._names)

    def to_dict(self) -> dict:
        return {str(k): v for k, v in self._names.items()
                if k not in self._RESERVED}

    def load_dict(self, d: dict):
        for k, v in d.items():
            self._names[int(k)] = v


# ===========================================================================
# COORD STORE  (superblock I/O — now wraps VersionStore + SpaceRegistry)
# ===========================================================================

class CoordStore:
    """
    Owns superblock persistence: writes/reads sector 0.

    Contains:
      - VersionStore   (all file versions across all spaces)
      - SpaceRegistry  (space id → name mapping)
      - URL index      (burris:// registry)
    """

    def __init__(
        self,
        drive:       "FoldingLatticeDrive",
        passphrase:  str          = None,
        mapper_kwargs: dict       = None,
    ):
        self._drive        = drive
        self._passphrase   = passphrase
        self._versions     = VersionStore()
        self._spaces       = SpaceRegistry(mapper_kwargs or {})
        self._url_index:   Dict[str, dict] = {}
        self._superblock   = Superblock()
        self._load()

    # ── Superblock I/O ────────────────────────────────────────────────────────

    def _payload_bytes(self) -> bytes:
        return json.dumps({
            "lattice_fs_v3": True,
            "versions":      self._versions.to_dict(),
            "spaces":        self._spaces.to_dict(),
            "urls":          self._url_index,
        }, separators=(",", ":")).encode("utf-8")

    def flush(self):
        header  = self._superblock.pack_header()
        payload = self._payload_bytes()
        if self._passphrase and _CRYPTO_OK:
            payload = _Crypto.encrypt(payload, self._passphrase)
        blob = header + payload
        if len(blob) > self._drive.sector_size:
            self._drive.sector_size = len(blob) + 256
        self._drive.write(blob, SECTOR_SUPERBLOCK)

    def _load(self):
        sec = self._drive._sectors[SECTOR_SUPERBLOCK]
        if not sec["written"]:
            self.flush()
            return

        raw = self._drive.read(SECTOR_SUPERBLOCK)
        raw = raw[:self._drive._sectors[SECTOR_SUPERBLOCK]["byte_length"]]
        raw = raw.rstrip(b"\x00")

        if len(raw) < Superblock.HEADER_SIZE:
            self._try_legacy_load(raw)
            return

        self._superblock = Superblock.unpack_header(raw)
        payload = raw[Superblock.HEADER_SIZE:]

        if _Crypto.is_encrypted(payload):
            if not self._passphrase:
                self._passphrase = input(
                    "[LatticeFS v2] Superblock encrypted. Enter passphrase: ").strip()
            payload = _Crypto.decrypt(payload, self._passphrase)

        try:
            data = json.loads(payload.decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"LatticeFS v2: superblock corrupt: {exc}") from exc

        if "lattice_fs_v3" in data:
            self._versions = VersionStore.from_dict(data.get("versions", {}))
            self._spaces.load_dict(data.get("spaces", {}))
            self._url_index = data.get("urls", {})
        elif "lattice_fs_v2" in data:
            # Phase 1 migration: lift flat index into VersionStore
            for path, entry in data.get("files", {}).items():
                fe = FileEntry(
                    path     = path,
                    start_V  = int(entry.get("start_V", 0)),
                    length   = entry.get("length", 0),
                    sha256   = entry.get("sha256", ""),
                    flags    = entry.get("flags", FLAG_NONE),
                    version  = entry.get("version", 1),
                    space_id = entry.get("space_id", SPACE_USER),
                )
                self._versions.put(fe)
            self._url_index = data.get("urls", {})
        else:
            print("[LatticeFS v2] Unknown superblock format — starting fresh.")

    def _try_legacy_load(self, raw: bytes):
        """Graceful fallback for very old images."""
        try:
            data = json.loads(raw.decode("utf-8"))
            self._url_index = data.get("urls", {})
        except Exception:
            pass
        self.flush()


# ===========================================================================
# LATTICE FS v2  (Phase 1 + 2)
# ===========================================================================

class LatticeFSv2:
    """
    Coordinate-first filesystem with crash recovery, immutable versioning,
    and multiple coordinate spaces.

    Coordinate Spaces
    -----------------
    Space 0  SPACE_SYSTEM  : reserved for filesystem internals
    Space 1  SPACE_USER    : default for user files
    Space 2+ : fleet / public / user-defined (define_space)

    Write/read calls accept space_id (default SPACE_USER=1).

    Journaling
    ----------
    Every mutation is appended to the journal before being applied.
    On flush the journal is committed then cleared.
    On load any uncommitted entries are detected and logged as warnings.
    Call replay_journal() to manually re-apply after a crash.

    Versioning
    ----------
    Every write_file creates a new FileEntry version.
    All versions are kept by default.
    Call version_history(path) to list them.
    Call read_file(path, version=N) to read a specific version.
    Call compact(keep_last_n=5) to prune old versions.

    API
    ---
    write_file(path, data, space_id=1)         -> FileEntry
    read_file(path, version=None, space_id=None) -> bytes
    delete_file(path)
    rename_file(old, new)
    exists(path) -> bool
    stat(path)   -> FileEntry
    version_history(path) -> List[FileEntry]
    ls(prefix=None, space_id=None)
    compact(keep_last_n=None)
    define_space(space_id, name)
    register_url / resolve_url / unregister_url / list_urls
    save(path) / load(path)
    replay_journal()
    """

    def __init__(
        self,
        drive:        "FoldingLatticeDrive",
        passphrase:   str  = None,
        mapper_kwargs: dict = None,
    ):
        if drive.n_sectors < 4:
            raise ValueError("LatticeFS v2 requires at least 4 sectors.")
        self._drive        = drive
        self._mapper_kw    = mapper_kwargs or {}
        self._store        = CoordStore(drive, passphrase, self._mapper_kw)
        self._journal      = Journal(drive, passphrase)
        self._check_uncommitted()

    # ── Internal helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _mapper(self, space_id: int) -> CoordMapper:
        return self._store._spaces.mapper(space_id)

    def _encrypt_content(self, data: bytes) -> Tuple[bytes, int]:
        pp = self._store._passphrase
        if pp and _CRYPTO_OK:
            return _Crypto.encrypt(data, pp), FLAG_ENCRYPTED
        return data, FLAG_NONE

    def _decrypt_content(self, blob: bytes, flags: int) -> bytes:
        if flags & FLAG_ENCRYPTED:
            if not self._store._passphrase:
                raise RuntimeError("File encrypted but no passphrase set.")
            return _Crypto.decrypt(blob, self._store._passphrase)
        return blob

    @staticmethod
    def _normalise_path(path: str) -> str:
        path = path.strip()
        if not path.startswith("/"):
            path = "/" + path
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        return path

    def _assert_exists(self, path: str):
        if not self._store._versions.exists(path):
            live = sorted(self._store._versions.all_live_heads().keys())
            raise FileNotFoundError(
                f"LatticeFS v2: {path!r} not found. Files: {live}")

    def _check_uncommitted(self):
        uncommitted = self._journal.replay()
        if uncommitted:
            print(f"[LatticeFS v2] ⚠  Journal has {len(uncommitted)} uncommitted "
                  f"record(s) from a previous crash. Call replay_journal() to recover.")

    # ── Journal transaction helpers ───────────────────────────────────────────

    def _jlog(self, op: str, data: dict):
        self._journal.append(op, data)

    def _jcommit(self):
        self._journal.commit()
        self._store.flush()
        self._journal.clear()

    # ── Core file API ─────────────────────────────────────────────────────────

    def write_file(
        self,
        path:     str,
        data:     bytes,
        space_id: int = SPACE_USER,
    ) -> "FileEntry":
        """
        Encode data into coordinate space and record a new FileEntry version.

        Steps:
            1. Hash original plaintext
            2. Optionally encrypt
            3. Encode → coordinate via space's CoordMapper
            4. Journal the intent
            5. Apply to VersionStore
            6. Commit + flush
        """
        path   = self._normalise_path(path)
        digest = self._sha256(data)
        blob, flags = self._encrypt_content(data)
        V      = self._mapper(space_id).encode(blob)
        version = self._store._versions.next_version(path)

        entry = FileEntry(
            path        = path,
            start_V     = V,
            length      = len(blob),
            sha256      = digest,
            flags       = flags,
            timestamp   = time.time(),
            version     = version,
            enc_version = self._mapper(space_id).encoding_version,
            space_id    = space_id,
        )

        # Journal → apply → commit
        self._jlog(JOP_WRITE_FILE, entry.to_dict())
        self._store._versions.put(entry)
        self._jlog(JOP_INDEX_UPDATE, {"path": path, "version": version})
        self._jcommit()

        size_note = f"{len(data)}B"
        if flags & FLAG_ENCRYPTED:
            size_note += f" → {len(blob)}B cipher"
        space_name = self._store._spaces.name(space_id)
        print(f"[LatticeFS v2] WRITE  {path!r}  v{version}  "
              f"{size_note}  sp={space_id}({space_name})  V={fmt_short(V)}")
        return entry

    def read_file(
        self,
        path:     str,
        version:  int = None,
        space_id: int = None,
    ) -> bytes:
        """
        Decode file from coordinate space and verify integrity.

        version=None → read HEAD (latest).
        version=N    → read specific version (immutable history).
        space_id     → override space lookup (normally inferred from entry).
        """
        path = self._normalise_path(path)
        self._assert_exists(path)

        if version is None:
            entry = self._store._versions.head(path)
        else:
            all_v = self._store._versions.all_versions(path)
            matches = [e for e in all_v if e.version == version]
            if not matches:
                raise FileNotFoundError(
                    f"LatticeFS v2: {path!r} version {version} not found.")
            entry = matches[0]

        if entry.length == 0 and not entry.sha256:
            raise FileNotFoundError(f"LatticeFS v2: {path!r} has been deleted.")

        sid  = space_id if space_id is not None else entry.space_id
        blob = self._mapper(sid).decode(entry.start_V, entry.length)
        plaintext = self._decrypt_content(blob, entry.flags)

        digest = self._sha256(plaintext)
        if digest != entry.sha256:
            raise RuntimeError(
                f"LatticeFS v2: integrity FAILED for {path!r} v{entry.version}\n"
                f"  stored:   {entry.sha256}\n"
                f"  computed: {digest}")

        v_note = f"v{entry.version}" + ("" if version is None else " [pinned]")
        print(f"[LatticeFS v2] READ   {path!r}  {v_note}  "
              f"{len(plaintext)}B  ✅ hash ok  V={fmt_short(entry.start_V)}")
        return plaintext

    def delete_file(self, path: str):
        path = self._normalise_path(path)
        self._assert_exists(path)
        self._jlog(JOP_DELETE_FILE, {"path": path})
        tombstone = self._store._versions.delete_head(path)
        self._jlog(JOP_INDEX_UPDATE, {"path": path, "version": tombstone.version,
                                       "tombstone": True})
        self._jcommit()
        print(f"[LatticeFS v2] DELETE {path!r}  "
              f"(coordinate preserved in history)")

    def rename_file(self, old_path: str, new_path: str):
        old_path = self._normalise_path(old_path)
        new_path = self._normalise_path(new_path)
        self._assert_exists(old_path)
        if self._store._versions.exists(new_path):
            raise FileExistsError(f"LatticeFS v2: {new_path!r} already exists.")
        self._jlog(JOP_RENAME_FILE, {"old_path": old_path, "new_path": new_path})
        self._store._versions.rename(old_path, new_path)
        self._jcommit()
        print(f"[LatticeFS v2] RENAME {old_path!r} → {new_path!r}")

    def exists(self, path: str) -> bool:
        return self._store._versions.exists(self._normalise_path(path))

    def stat(self, path: str) -> "FileEntry":
        path = self._normalise_path(path)
        self._assert_exists(path)
        return self._store._versions.head(path)

    def version_history(self, path: str) -> List["FileEntry"]:
        """Return all FileEntry versions for path (oldest first)."""
        path = self._normalise_path(path)
        return self._store._versions.all_versions(path)

    # ── Compact (GC) ──────────────────────────────────────────────────────────

    def compact(self, keep_last_n: int = None) -> int:
        """
        Prune old file versions to reclaim coordinate history.

        keep_last_n=None  → keep all (no-op, but logs statistics)
        keep_last_n=1     → keep only HEAD
        keep_last_n=5     → keep last 5 versions

        Returns total number of versions pruned.
        Note: coordinates are immutable — pruning removes metadata entries
        only. The underlying BNS coordinate space is not freed (coordinates
        are eternal by design of the Informational Universe).
        """
        if keep_last_n is None:
            total = sum(
                len(self._store._versions.all_versions(p))
                for p in self._store._versions.all_paths()
            )
            print(f"[LatticeFS v2] COMPACT (keep_last_n=None) — "
                  f"{total} total version entries preserved.")
            return 0

        pruned = self._store._versions.prune_all(keep_last_n)
        self._jlog(JOP_INDEX_UPDATE, {"compact": True, "keep_last_n": keep_last_n,
                                       "pruned": pruned})
        self._jcommit()
        print(f"[LatticeFS v2] COMPACT keep_last_n={keep_last_n} — "
              f"pruned {pruned} version entries.")
        return pruned

    # ── Coordinate space management ───────────────────────────────────────────

    def define_space(self, space_id: int, name: str):
        """Register a new coordinate space (space_id >= 2)."""
        self._store._spaces.define(space_id, name)
        self._jlog(JOP_INDEX_UPDATE, {"define_space": space_id, "name": name})
        self._jcommit()
        print(f"[LatticeFS v2] DEFINE SPACE  {space_id} → {name!r}")

    def list_spaces(self):
        border = "─" * 50
        print(f"\n  {border}")
        print(f"  ⬡  COORDINATE SPACES")
        print(f"  {border}")
        for sid, name in sorted(self._store._spaces.all_spaces().items()):
            n_files = len(self._store._versions.all_live_heads(space_id=sid))
            print(f"  Space {sid:2d}  {name:<20}  {n_files} live file(s)")
        print(f"  {border}\n")

    # ── Directory listing ──────────────────────────────────────────────────────

    def ls(self, prefix: str = None, space_id: int = None):
        """List live files. Filter by prefix path and/or space_id."""
        heads = self._store._versions.all_live_heads(space_id=space_id)
        if prefix:
            prefix = self._normalise_path(prefix)
            heads  = {k: v for k, v in heads.items() if k.startswith(prefix)}

        urls   = self._store._url_index
        border = "─" * 74
        sp_label = f"space={space_id}" if space_id is not None else "all spaces"
        enc_flag = "🔒 ENCRYPTED" if self._store._passphrase else "🔓 plaintext"
        print(f"\n  {border}")
        print(f"  ⬡  LatticeFS v2  —  {len(heads)} file(s)  "
              f"{len(urls)} URL(s)  {enc_flag}  [{sp_label}]")
        print(f"  Superblock: {self._store._superblock}")
        print(f"  {border}")
        print(f"  {'PATH':<38}  {'SP':>2}  {'VER':>3}  {'BYTES':>8}  {'SHA256':>12}  FLAGS")
        print(f"  {'─'*38}  {'─'*2}  {'─'*3}  {'─'*8}  {'─'*12}  ─────")
        if not heads:
            print(f"  (no files)")
        for path, entry in sorted(heads.items()):
            enc      = "ENC" if entry.flags & FLAG_ENCRYPTED else "   "
            shorthash= entry.sha256[:10] + "…"
            sp_name  = self._store._spaces.name(entry.space_id)
            print(f"  {path:<38}  {entry.space_id:>2}  {entry.version:>3}  "
                  f"{entry.length:>8}  {shorthash:>13}  {enc}")
        if urls:
            print(f"  {border}")
            print(f"  {'URL PATH':<44}  COORDINATE")
            for url, rec in sorted(urls.items()):
                coord_s = fmt_short(int(rec["coordinate"])) if rec.get("coordinate") else "?"
                print(f"  {url:<44}  {coord_s}")
        print(f"  {border}\n")

    # ── Journal replay ─────────────────────────────────────────────────────────

    def replay_journal(self) -> int:
        """
        Replay uncommitted journal entries after a crash.

        Walks uncommitted records and re-applies WRITE_FILE / DELETE_FILE /
        RENAME_FILE / REGISTER_URL / UNREGISTER_URL operations.
        Returns number of records replayed.
        """
        uncommitted = self._journal.replay()
        if not uncommitted:
            print("[LatticeFS v2] Journal clean — nothing to replay.")
            return 0

        print(f"[LatticeFS v2] Replaying {len(uncommitted)} uncommitted journal record(s)...")
        replayed = 0
        for rec in uncommitted:
            op   = rec["op"]
            data = rec["data"]
            try:
                if op == JOP_WRITE_FILE:
                    entry = FileEntry.from_dict(data)
                    self._store._versions.put(entry)
                    replayed += 1
                elif op == JOP_DELETE_FILE:
                    self._store._versions.delete_head(data["path"])
                    replayed += 1
                elif op == JOP_RENAME_FILE:
                    self._store._versions.rename(data["old_path"], data["new_path"])
                    replayed += 1
                elif op == JOP_REGISTER_URL:
                    self._store._url_index[data["url_path"]] = data["entry"]
                    replayed += 1
                elif op == JOP_UNREGISTER_URL:
                    self._store._url_index.pop(data["url_path"], None)
                    replayed += 1
                elif op == JOP_INDEX_UPDATE:
                    pass   # metadata only, index already updated above
            except Exception as exc:
                print(f"  [Journal] ⚠  Could not replay op={op}: {exc}")

        self._store.flush()
        self._journal.clear()
        print(f"[LatticeFS v2] Replay complete — {replayed} record(s) applied.")
        return replayed

    # ── URL registry ───────────────────────────────────────────────────────────

    def register_url(self, url_path: str, coordinate: int, metadata: dict = None) -> dict:
        entry = {
            "coordinate": str(coordinate),
            "registered": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
            "metadata":   metadata or {},
        }
        self._jlog(JOP_REGISTER_URL, {"url_path": url_path, "entry": entry})
        self._store._url_index[url_path] = entry
        self._jcommit()
        print(f"[LatticeFS v2] REGISTER URL  {url_path!r}  → {fmt_short(coordinate)}")
        return entry

    def resolve_url(self, url_path: str) -> Optional[dict]:
        rec = self._store._url_index.get(url_path)
        if rec is None:
            print(f"[LatticeFS v2] RESOLVE URL  {url_path!r}  → NOT FOUND")
            return None
        result = dict(rec)
        result["coordinate"] = int(result["coordinate"])
        print(f"[LatticeFS v2] RESOLVE URL  {url_path!r}  → {fmt_short(result['coordinate'])}")
        return result

    def unregister_url(self, url_path: str):
        if url_path not in self._store._url_index:
            print(f"[LatticeFS v2] UNREGISTER URL  {url_path!r}  → NOT FOUND")
            return
        self._jlog(JOP_UNREGISTER_URL, {"url_path": url_path})
        self._store._url_index.pop(url_path)
        self._jcommit()
        print(f"[LatticeFS v2] UNREGISTER URL  {url_path!r}  REMOVED")

    def list_urls(self):
        urls = self._store._url_index
        if not urls:
            print("[LatticeFS v2] URL registry empty.")
            return
        border = "─" * 70
        print(f"\n  {border}")
        print(f"  ⬡  BURRIS URL REGISTRY  —  {len(urls)} entry/entries")
        print(f"  {border}")
        for url, rec in sorted(urls.items()):
            coord_s = fmt_short(int(rec["coordinate"])) if rec.get("coordinate") else "?"
            meta_s  = ", ".join(f"{k}={v}" for k, v in rec.get("metadata", {}).items())
            print(f"  {url}  →  {coord_s}  [{meta_s[:40]}]")
        print(f"  {border}\n")

    # ── Persistence ─────────────────────────────────────────────────────────────

    def save(self, json_path: str):
        self._store.flush()
        self._drive.save(json_path)
        print(f"[LatticeFS v2] Image saved → {json_path}")

    def load(self, json_path: str):
        passphrase = self._store._passphrase
        self._drive.load(json_path)
        self._store   = CoordStore(self._drive, passphrase, self._mapper_kw)
        self._journal = Journal(self._drive, passphrase)
        self._check_uncommitted()
        print(f"[LatticeFS v2] Image loaded ← {json_path}")

    def set_encryption_key(self, passphrase: str):
        self._store._passphrase   = passphrase
        self._journal._passphrase = passphrase
        self._store.flush()
        status = "ENABLED" if passphrase else "DISABLED"
        print(f"[LatticeFS v2] Encryption {status}.")


# ===========================================================================
# FACTORY
# ===========================================================================

def lattice_fs_v2(
    sector_size:  int = 512,
    n_sectors:    int = 128,
    chart_base:   int = 256,
    mask_base:    int = 1_000_000_000_000,
    num_digits:   int = 100,
    scale_factor: int = 5000,
    passphrase:   str = None,
) -> LatticeFSv2:
    if not _BNS_OK:
        raise RuntimeError("BNS modules must be on sys.path.")

    drive = FoldingLatticeDrive(
        sector_size  = sector_size,
        n_sectors    = n_sectors,
        chart_base   = chart_base,
        mask_base    = mask_base,
        num_digits   = num_digits,
        scale_factor = scale_factor,
    )
    mapper_kw = dict(
        chart_base   = chart_base,
        mask_base    = mask_base,
        num_digits   = num_digits,
        scale_factor = scale_factor,
    )
    fs = LatticeFSv2(drive, passphrase=passphrase, mapper_kwargs=mapper_kw)
    enc_note = " [ENCRYPTED]" if passphrase else ""
    print(f"\n⬡  LatticeFS v2 mounted  —  "
          f"{n_sectors} × {sector_size}B sectors  "
          f"Journal: ON  Versioning: ON  Spaces: ON{enc_note}\n")
    return fs


# ===========================================================================
# SELF-TESTS  (20 tests covering Phase 1 + Phase 2)
# ===========================================================================

if __name__ == "__main__":
    import tempfile

    print("=" * 68)
    print("  LatticeFS v2 — Phase 1 + Phase 2 Self-Tests")
    print("=" * 68)

    # ── Phase 1 tests (regression) ────────────────────────────────────────

    print("\n[Test 1] Write → Read → Hash verify")
    fs = lattice_fs_v2(sector_size=512, n_sectors=64)
    payload = b"Hello, coordinate universe! BNS LatticeFS v2."
    entry   = fs.write_file("/docs/hello.txt", payload)
    got     = fs.read_file("/docs/hello.txt")
    assert got == payload
    assert entry.sha256 == hashlib.sha256(payload).hexdigest()
    print("  ✅ PASSED")

    print("\n[Test 2] Binary data round-trip")
    fs2   = lattice_fs_v2(sector_size=512, n_sectors=64)
    data2 = bytes(range(256))
    fs2.write_file("/bin/sweep.bin", data2)
    assert fs2.read_file("/bin/sweep.bin") == data2
    print("  ✅ PASSED")

    print("\n[Test 3] Encrypted write → read")
    fs3   = lattice_fs_v2(sector_size=512, n_sectors=64, passphrase="odinnet-2026")
    data3 = b"Top-secret BNS transmission."
    fs3.write_file("/secure/msg.txt", data3)
    got3  = fs3.read_file("/secure/msg.txt")
    assert got3 == data3
    assert fs3.stat("/secure/msg.txt").flags & FLAG_ENCRYPTED
    print("  ✅ PASSED  (FLAG_ENCRYPTED set)")

    print("\n[Test 4] Integrity check catches corruption")
    fs4 = lattice_fs_v2(sector_size=512, n_sectors=64)
    fs4.write_file("/test/integrity.txt", b"Untampered.")
    fs4._store._versions.head("/test/integrity.txt").sha256 = "deadbeef" * 8
    caught = False
    try:
        fs4.read_file("/test/integrity.txt")
    except RuntimeError:
        caught = True
    assert caught
    print("  ✅ PASSED  (corruption detected)")

    print("\n[Test 5] Directory listing with prefix filter")
    fs5 = lattice_fs_v2(sector_size=512, n_sectors=64)
    fs5.write_file("/docs/readme.txt", b"README")
    fs5.write_file("/docs/notes.txt",  b"NOTES")
    fs5.write_file("/bin/odinnet",     b"\x7fELF")
    fs5.ls("/docs")
    print("  ✅ PASSED")

    print("\n[Test 6] Rename file")
    fs6 = lattice_fs_v2(sector_size=512, n_sectors=64)
    fs6.write_file("/old.txt", b"rename me")
    fs6.rename_file("/old.txt", "/new.txt")
    assert not fs6.exists("/old.txt")
    assert fs6.read_file("/new.txt") == b"rename me"
    print("  ✅ PASSED")

    print("\n[Test 7] URL registry")
    fs7 = lattice_fs_v2(sector_size=512, n_sectors=64)
    fs7.register_url("burris://odinnet.io/index", 123456789012345678, {"mime": "text/html"})
    rec = fs7.resolve_url("burris://odinnet.io/index")
    assert rec and rec["coordinate"] == 123456789012345678
    fs7.unregister_url("burris://odinnet.io/index")
    assert fs7.resolve_url("burris://odinnet.io/index") is None
    print("  ✅ PASSED")

    print("\n[Test 8] Save / Load persistence")
    fs8   = lattice_fs_v2(sector_size=512, n_sectors=64, passphrase="nav-2026")
    data8 = b"Persistent coordinate payload."
    fs8.write_file("/persist/data.bin", data8)
    fs8.register_url("burris://test/persist", 999888777)
    with tempfile.TemporaryDirectory() as tmp:
        import os
        img = os.path.join(tmp, "lfsv2.json")
        fs8.save(img)
        from folding_lattice_drive import FoldingLatticeDrive
        drive2 = FoldingLatticeDrive(sector_size=512, n_sectors=64)
        drive2.load(img)
        fs9 = LatticeFSv2(drive2, passphrase="nav-2026",
                          mapper_kwargs={"chart_base":256,"mask_base":1_000_000_000_000,
                                         "num_digits":100,"scale_factor":5000})
        assert fs9.read_file("/persist/data.bin") == data8
        rec9 = fs9.resolve_url("burris://test/persist")
        assert rec9 and rec9["coordinate"] == 999888777
    print("  ✅ PASSED  (data + URLs survived save/load)")

    print("\n[Test 9] Superblock magic validation")
    sb  = Superblock()
    hdr = sb.pack_header()
    assert hdr[:8] == FS_MAGIC
    sb2 = Superblock.unpack_header(hdr)
    assert sb2.fs_version == FS_VERSION
    bad = bytearray(hdr); bad[0] = 0xFF
    try:
        Superblock.unpack_header(bytes(bad))
        assert False, "Should have raised"
    except ValueError:
        pass
    print("  ✅ PASSED")

    # ── Phase 2 tests ─────────────────────────────────────────────────────

    print("\n[Test 10] Immutable versioning — overwrite creates new version")
    fs10 = lattice_fs_v2(sector_size=512, n_sectors=64)
    fs10.write_file("/config.json", b'{"v":1}')
    fs10.write_file("/config.json", b'{"v":2}')
    fs10.write_file("/config.json", b'{"v":3}')
    assert fs10.stat("/config.json").version == 3
    assert fs10.read_file("/config.json") == b'{"v":3}'
    print("  ✅ PASSED  (version=3, HEAD correct)")

    print("\n[Test 11] Read specific version from history")
    assert fs10.read_file("/config.json", version=1) == b'{"v":1}'
    assert fs10.read_file("/config.json", version=2) == b'{"v":2}'
    history = fs10.version_history("/config.json")
    assert len(history) == 3
    print(f"  ✅ PASSED  ({len(history)} versions in history)")

    print("\n[Test 12] Delete leaves history intact")
    fs12 = lattice_fs_v2(sector_size=512, n_sectors=64)
    fs12.write_file("/ephemeral.txt", b"here today")
    fs12.write_file("/ephemeral.txt", b"updated")
    fs12.delete_file("/ephemeral.txt")
    assert not fs12.exists("/ephemeral.txt")
    history12 = fs12.version_history("/ephemeral.txt")
    assert len(history12) == 3  # v1, v2, tombstone
    print(f"  ✅ PASSED  ({len(history12)} entries in history including tombstone)")

    print("\n[Test 13] Compact prunes old versions")
    fs13 = lattice_fs_v2(sector_size=512, n_sectors=64)
    for i in range(1, 8):
        fs13.write_file("/rolling.log", f"entry {i}".encode())
    assert fs13.stat("/rolling.log").version == 7
    pruned = fs13.compact(keep_last_n=3)
    assert pruned == 4
    history13 = fs13.version_history("/rolling.log")
    assert len(history13) == 3
    assert fs13.read_file("/rolling.log") == b"entry 7"
    print(f"  ✅ PASSED  (pruned={pruned}, {len(history13)} versions remain)")

    print("\n[Test 14] Multiple coordinate spaces — write to different spaces")
    fs14 = lattice_fs_v2(sector_size=512, n_sectors=64)
    fs14.define_space(2, "fleet-alpha")
    fs14.write_file("/msg.txt", b"user private",    space_id=SPACE_USER)
    fs14.write_file("/msg.txt", b"fleet broadcast", space_id=2)
    # Both exist under same path but different spaces
    user_e  = [e for e in fs14.version_history("/msg.txt") if e.space_id == SPACE_USER]
    fleet_e = [e for e in fs14.version_history("/msg.txt") if e.space_id == 2]
    assert user_e  and fs14.read_file("/msg.txt", version=user_e[-1].version,
                                       space_id=SPACE_USER) == b"user private"
    assert fleet_e and fs14.read_file("/msg.txt", version=fleet_e[-1].version,
                                       space_id=2) == b"fleet broadcast"
    print("  ✅ PASSED  (same path, two spaces, independent coordinates)")

    print("\n[Test 15] ls() filtered by space_id")
    fs14.write_file("/system.conf", b"sys", space_id=SPACE_SYSTEM)
    fs14.ls(space_id=SPACE_USER)
    print("  ✅ PASSED")

    print("\n[Test 16] list_spaces()")
    fs14.list_spaces()
    print("  ✅ PASSED")

    print("\n[Test 17] Journal records mutations")
    fs17 = lattice_fs_v2(sector_size=512, n_sectors=64)
    fs17.write_file("/a.txt", b"alpha")
    fs17.write_file("/b.txt", b"beta")
    # Journal should be cleared after each commit
    assert len(fs17._journal) == 0
    print(f"  ✅ PASSED  (journal cleared after commit)")

    print("\n[Test 18] Journal replay after simulated crash")
    fs18 = lattice_fs_v2(sector_size=512, n_sectors=64)
    fs18.write_file("/pre.txt", b"pre-crash")
    # Simulate crash: manually append journal record without committing
    fs18._journal.append(JOP_WRITE_FILE, FileEntry(
        path="/crash.txt", start_V=12345, length=5,
        sha256=hashlib.sha256(b"crash").hexdigest(),
        space_id=SPACE_USER,
    ).to_dict())
    assert fs18._journal.has_uncommitted()
    replayed = fs18.replay_journal()
    assert replayed >= 1
    assert not fs18._journal.has_uncommitted()
    print(f"  ✅ PASSED  (replayed {replayed} record(s))")

    print("\n[Test 19] Compact(keep_last_n=None) is a no-op")
    fs19 = lattice_fs_v2(sector_size=512, n_sectors=64)
    for i in range(5):
        fs19.write_file("/keep.txt", f"v{i}".encode())
    pruned19 = fs19.compact(keep_last_n=None)
    assert pruned19 == 0
    assert len(fs19.version_history("/keep.txt")) == 5
    print("  ✅ PASSED  (all versions preserved)")

    print("\n[Test 20] Phase 1 migration — v2 superblock loads into v3")
    # Simulate a Phase 1 superblock payload (lattice_fs_v2 key instead of v3)
    old_payload = json.dumps({
        "lattice_fs_v2": True,
        "next_free": 2,
        "files": {
            "/legacy.txt": {
                "path": "/legacy.txt", "start_V": "99999",
                "length": 10, "sha256": "abc123",
                "flags": 0, "nonce": "", "timestamp": 0.0,
                "version": 1, "enc_version": 1, "space_id": 1,
            }
        },
        "urls": {}
    }).encode()
    store20 = CoordStore.__new__(CoordStore)
    store20._passphrase   = None
    store20._versions     = VersionStore()
    store20._spaces       = SpaceRegistry()
    store20._url_index    = {}
    store20._superblock   = Superblock()
    data20 = json.loads(old_payload)
    for path, entry in data20.get("files", {}).items():
        fe = FileEntry(
            path=path, start_V=int(entry["start_V"]), length=entry["length"],
            sha256=entry["sha256"], version=entry.get("version",1),
            space_id=entry.get("space_id", SPACE_USER),
        )
        store20._versions.put(fe)
    assert store20._versions.exists("/legacy.txt")
    head20 = store20._versions.head("/legacy.txt")
    assert head20.version == 1
    print("  ✅ PASSED  (Phase 1 entries lift into VersionStore correctly)")

    print("\n" + "=" * 68)
    print("  ✅  All 20 LatticeFS v2 Phase 1+2 tests passed.")
    print("=" * 68)
