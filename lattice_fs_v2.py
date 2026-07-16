"""
lattice_fs_v2.py
Burris Numerical System — LatticeFS v2, Phase 1 + Phase 2

Coordinate-First Filesystem
(local copy of Lloyd's real source, for patching + verification only)
"""

import hashlib
import json
import os
import struct
import time
from datetime import datetime as _dt
from typing import Dict, List, Optional, Tuple

try:
    from cryptography.hazmat.primitives import hashes as _hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _CRYPTO_OK = True
except ImportError:
    _CRYPTO_OK = False
    print("⚠  cryptography not installed — LatticeFS v2 encryption disabled.")

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


FS_MAGIC         = b"BNSLAT2\x00"
FS_VERSION       = 3
ENCODING_VERSION = 1

SECTOR_SUPERBLOCK = 0
SECTOR_JOURNAL    = 1
DATA_SECTOR_START = 2

SPACE_SYSTEM  = 0
SPACE_USER    = 1

FLAG_NONE      = 0x00
FLAG_ENCRYPTED = 0x01
FLAG_COMPRESSED= 0x02
FLAG_IMMUTABLE = 0x04

JOP_WRITE_FILE     = "WRITE_FILE"
JOP_DELETE_FILE    = "DELETE_FILE"
JOP_RENAME_FILE    = "RENAME_FILE"
JOP_REGISTER_URL   = "REGISTER_URL"
JOP_UNREGISTER_URL = "UNREGISTER_URL"
JOP_INDEX_UPDATE   = "INDEX_UPDATE"
JOP_COMMIT         = "COMMIT"


class _Crypto:
    _PREFIX = b"E2:"

    @staticmethod
    def derive_key(passphrase: str, salt: bytes) -> bytes:
        kdf = PBKDF2HMAC(algorithm=_hashes.SHA256(), length=32, salt=salt, iterations=600_000)
        return kdf.derive(passphrase.encode("utf-8"))

    @staticmethod
    def encrypt(plaintext: bytes, passphrase: str) -> bytes:
        if not _CRYPTO_OK:
            return plaintext
        import base64
        salt = os.urandom(16)
        nonce = os.urandom(12)
        key = _Crypto.derive_key(passphrase, salt)
        ct = AESGCM(key).encrypt(nonce, plaintext, None)
        return _Crypto._PREFIX + base64.b64encode(salt + nonce + ct)

    @staticmethod
    def decrypt(blob: bytes, passphrase: str) -> bytes:
        if not blob.startswith(_Crypto._PREFIX):
            return blob
        if not _CRYPTO_OK:
            raise RuntimeError("cryptography library required to decrypt.")
        import base64
        raw = base64.b64decode(blob[3:])
        salt = raw[:16]; nonce = raw[16:28]; ct = raw[28:]
        key = _Crypto.derive_key(passphrase, salt)
        return AESGCM(key).decrypt(nonce, ct, None)

    @staticmethod
    def is_encrypted(blob: bytes) -> bool:
        return blob.startswith(_Crypto._PREFIX)


class CoordMapper:
    def __init__(self, chart_base: int = 256, mask_base: int = 1_000_000_000_000,
                 num_digits: int = 100, scale_factor: int = 5000, space_id: int = SPACE_USER):
        self.chart_base = chart_base
        self.mask_base = mask_base
        self.num_digits = num_digits
        self.scale_factor = scale_factor
        self.space_id = space_id

    def _new_fcg(self):
        if not _BNS_OK:
            raise RuntimeError("BNS modules unavailable.")
        return FoldingChartGenerator(chart_base=self.chart_base, mask_base=self.mask_base,
                                      num_digits=self.num_digits, scale_factor=self.scale_factor)

    def encode(self, data: bytes) -> int:
        return self._new_fcg().encode_bytes(data, u=0)

    def decode(self, V: int, length: int) -> bytes:
        return self._new_fcg().decode_bytes(V, length)

    @property
    def encoding_version(self) -> int:
        return ENCODING_VERSION


class FileEntry:
    __slots__ = ("path", "start_V", "length", "sha256", "flags", "nonce",
                 "timestamp", "version", "enc_version", "space_id")

    def __init__(self, path: str, start_V: int, length: int, sha256: str,
                 flags: int = FLAG_NONE, nonce: str = "", timestamp: float = 0.0,
                 version: int = 1, enc_version: int = ENCODING_VERSION, space_id: int = SPACE_USER):
        self.path = path
        self.start_V = start_V
        self.length = length
        self.sha256 = sha256
        self.flags = flags
        self.nonce = nonce
        self.timestamp = timestamp or time.time()
        self.version = version
        self.enc_version = enc_version
        self.space_id = space_id

    def to_dict(self) -> dict:
        return {"path": self.path, "start_V": str(self.start_V), "length": self.length,
                "sha256": self.sha256, "flags": self.flags, "nonce": self.nonce,
                "timestamp": self.timestamp, "version": self.version,
                "enc_version": self.enc_version, "space_id": self.space_id}

    @staticmethod
    def from_dict(d: dict) -> "FileEntry":
        return FileEntry(path=d["path"], start_V=int(d["start_V"]), length=d["length"],
                          sha256=d["sha256"], flags=d.get("flags", FLAG_NONE),
                          nonce=d.get("nonce", ""), timestamp=d.get("timestamp", 0.0),
                          version=d.get("version", 1), enc_version=d.get("enc_version", ENCODING_VERSION),
                          space_id=d.get("space_id", SPACE_USER))

    def __repr__(self) -> str:
        enc = " [ENC]" if self.flags & FLAG_ENCRYPTED else ""
        ts = _dt.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M:%S")
        return (f"<FileEntry {self.path!r} v{self.version} sp={self.space_id} "
                f"{self.length}B V={fmt_short(self.start_V)}{enc} @ {ts}>")


class Superblock:
    HEADER_SIZE = 60
    HEADER_FMT = "<8sHHI44s"

    def __init__(self, fs_version: int = FS_VERSION, enc_version: int = ENCODING_VERSION, flags: int = 0):
        self.fs_version = fs_version
        self.enc_version = enc_version
        self.flags = flags

    def pack_header(self) -> bytes:
        return struct.pack(self.HEADER_FMT, FS_MAGIC, self.fs_version, self.enc_version, self.flags, b"\x00" * 44)

    @staticmethod
    def unpack_header(data: bytes) -> "Superblock":
        if len(data) < Superblock.HEADER_SIZE:
            raise ValueError("Superblock data too short.")
        magic, fs_ver, enc_ver, flags, _ = struct.unpack_from(Superblock.HEADER_FMT, data, 0)
        if magic != FS_MAGIC:
            raise ValueError(f"Bad magic: {magic!r} (expected {FS_MAGIC!r}). Not a LatticeFS v2 image.")
        return Superblock(fs_version=fs_ver, enc_version=enc_ver, flags=flags)

    def __repr__(self) -> str:
        return f"<Superblock fs_v={self.fs_version} enc_v={self.enc_version} flags=0x{self.flags:04x}>"


class Journal:
    def __init__(self, drive: "FoldingLatticeDrive", passphrase: str = None):
        self._drive = drive
        self._passphrase = passphrase
        self._records: List[dict] = []
        self._seq: int = 0
        self._load()

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
            self._seq = 0
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
            self._records = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
            self._seq = max((r["seq"] for r in self._records), default=0)
        except Exception as exc:
            print(f"[Journal] ⚠  Could not parse journal: {exc} — starting fresh.")
            self._records = []
            self._seq = 0

    def append(self, op: str, data: dict):
        self._seq += 1
        record = {"seq": self._seq, "op": op, "ts": time.time(), "data": data}
        self._records.append(record)
        self._flush_to_drive()

    def commit(self):
        self.append(JOP_COMMIT, {})

    def clear(self):
        self._records = []
        self._flush_to_drive()

    def replay(self) -> List[dict]:
        if not self._records:
            return []
        last_commit_idx = None
        for i in range(len(self._records) - 1, -1, -1):
            if self._records[i]["op"] == JOP_COMMIT:
                last_commit_idx = i
                break
        if last_commit_idx is None:
            return self._records[:]
        uncommitted = self._records[last_commit_idx + 1:]
        return uncommitted

    def has_uncommitted(self) -> bool:
        return len(self.replay()) > 0

    def all_committed_groups(self) -> List[List[dict]]:
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


class VersionStore:
    def __init__(self):
        self._versions: Dict[str, List[FileEntry]] = {}

    def put(self, entry: FileEntry):
        path = entry.path
        if path not in self._versions:
            self._versions[path] = []
        self._versions[path].append(entry)

    def delete_head(self, path: str) -> Optional[FileEntry]:
        if path not in self._versions or not self._versions[path]:
            return None
        head = self._versions[path][-1]
        version = head.version + 1
        tombstone = FileEntry(path=path, start_V=0, length=0, sha256="", flags=FLAG_NONE,
                               timestamp=time.time(), version=version, space_id=head.space_id)
        self._versions[path].append(tombstone)
        return tombstone

    def rename(self, old_path: str, new_path: str):
        if old_path not in self._versions:
            return
        entries = self._versions.pop(old_path)
        for e in entries:
            e.path = new_path
        self._versions[new_path] = entries

    def head(self, path: str) -> Optional[FileEntry]:
        versions = self._versions.get(path, [])
        if not versions:
            return None
        latest = versions[-1]
        if latest.length == 0 and not latest.sha256:
            return None
        return latest

    def exists(self, path: str) -> bool:
        return self.head(path) is not None

    def all_versions(self, path: str) -> List[FileEntry]:
        return list(self._versions.get(path, []))

    def all_live_heads(self, space_id: int = None) -> Dict[str, FileEntry]:
        result = {}
        for path, versions in self._versions.items():
            if not versions:
                continue
            latest = versions[-1]
            if latest.length == 0 and not latest.sha256:
                continue
            if space_id is None or latest.space_id == space_id:
                result[path] = latest
        return result

    def all_paths(self) -> List[str]:
        return list(self._versions.keys())

    def next_version(self, path: str) -> int:
        versions = self._versions.get(path, [])
        return (versions[-1].version + 1) if versions else 1

    def prune(self, path: str, keep_last_n: int) -> int:
        versions = self._versions.get(path, [])
        if len(versions) <= keep_last_n:
            return 0
        n_prune = len(versions) - keep_last_n
        self._versions[path] = versions[n_prune:]
        return n_prune

    def prune_all(self, keep_last_n: int) -> int:
        total = 0
        for path in list(self._versions):
            total += self.prune(path, keep_last_n)
        return total

    def to_dict(self) -> dict:
        return {path: [e.to_dict() for e in entries] for path, entries in self._versions.items()}

    @staticmethod
    def from_dict(d: dict) -> "VersionStore":
        vs = VersionStore()
        for path, entries in d.items():
            vs._versions[path] = [FileEntry.from_dict(e) for e in entries]
        return vs


class SpaceRegistry:
    _RESERVED = {SPACE_SYSTEM: "system", SPACE_USER: "user"}

    def __init__(self, mapper_kwargs: dict = None):
        self._kwargs = mapper_kwargs or {}
        self._names: Dict[int, str] = dict(self._RESERVED)
        self._mappers: Dict[int, CoordMapper] = {}

    def mapper(self, space_id: int) -> CoordMapper:
        if space_id not in self._mappers:
            self._mappers[space_id] = CoordMapper(space_id=space_id, **self._kwargs)
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
        return {str(k): v for k, v in self._names.items() if k not in self._RESERVED}

    def load_dict(self, d: dict):
        for k, v in d.items():
            self._names[int(k)] = v


class CoordStore:
    def __init__(self, drive: "FoldingLatticeDrive", passphrase: str = None, mapper_kwargs: dict = None):
        self._drive = drive
        self._passphrase = passphrase
        self._versions = VersionStore()
        self._spaces = SpaceRegistry(mapper_kwargs or {})
        self._url_index: Dict[str, dict] = {}
        self._superblock = Superblock()
        self._load()

    def _payload_bytes(self) -> bytes:
        return json.dumps({"lattice_fs_v3": True, "versions": self._versions.to_dict(),
                            "spaces": self._spaces.to_dict(), "urls": self._url_index},
                           separators=(",", ":")).encode("utf-8")

    def flush(self):
        header = self._superblock.pack_header()
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
                self._passphrase = input("[LatticeFS v2] Superblock encrypted. Enter passphrase: ").strip()
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
            for path, entry in data.get("files", {}).items():
                fe = FileEntry(path=path, start_V=int(entry.get("start_V", 0)), length=entry.get("length", 0),
                                sha256=entry.get("sha256", ""), flags=entry.get("flags", FLAG_NONE),
                                version=entry.get("version", 1), space_id=entry.get("space_id", SPACE_USER))
                self._versions.put(fe)
            self._url_index = data.get("urls", {})
        else:
            print("[LatticeFS v2] Unknown superblock format — starting fresh.")

    def _try_legacy_load(self, raw: bytes):
        try:
            data = json.loads(raw.decode("utf-8"))
            self._url_index = data.get("urls", {})
        except Exception:
            pass
        self.flush()


class LatticeFSv2:
    def __init__(self, drive: "FoldingLatticeDrive", passphrase: str = None, mapper_kwargs: dict = None):
        if drive.n_sectors < 4:
            raise ValueError("LatticeFS v2 requires at least 4 sectors.")
        self._drive = drive
        self._mapper_kw = mapper_kwargs or {}
        self._store = CoordStore(drive, passphrase, self._mapper_kw)
        self._journal = Journal(drive, passphrase)
        self._check_uncommitted()

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
            raise FileNotFoundError(f"LatticeFS v2: {path!r} not found. Files: {live}")

    def _check_uncommitted(self):
        uncommitted = self._journal.replay()
        if uncommitted:
            print(f"[LatticeFS v2] ⚠  Journal has {len(uncommitted)} uncommitted record(s) from a previous crash. Call replay_journal() to recover.")

    def _jlog(self, op: str, data: dict):
        self._journal.append(op, data)

    def _jcommit(self):
        self._journal.commit()
        self._store.flush()
        self._journal.clear()

    def write_file(self, path: str, data: bytes, space_id: int = SPACE_USER) -> "FileEntry":
        path = self._normalise_path(path)
        digest = self._sha256(data)
        blob, flags = self._encrypt_content(data)
        V = self._mapper(space_id).encode(blob)
        version = self._store._versions.next_version(path)
        entry = FileEntry(path=path, start_V=V, length=len(blob), sha256=digest, flags=flags,
                           timestamp=time.time(), version=version,
                           enc_version=self._mapper(space_id).encoding_version, space_id=space_id)
        self._jlog(JOP_WRITE_FILE, entry.to_dict())
        self._store._versions.put(entry)
        self._jlog(JOP_INDEX_UPDATE, {"path": path, "version": version})
        self._jcommit()
        size_note = f"{len(data)}B"
        if flags & FLAG_ENCRYPTED:
            size_note += f" → {len(blob)}B cipher"
        space_name = self._store._spaces.name(space_id)
        print(f"[LatticeFS v2] WRITE  {path!r}  v{version}  {size_note}  sp={space_id}({space_name})  V={fmt_short(V)}")
        return entry

    def read_file(self, path: str, version: int = None, space_id: int = None) -> bytes:
        path = self._normalise_path(path)
        self._assert_exists(path)
        if version is None:
            entry = self._store._versions.head(path)
        else:
            all_v = self._store._versions.all_versions(path)
            matches = [e for e in all_v if e.version == version]
            if not matches:
                raise FileNotFoundError(f"LatticeFS v2: {path!r} version {version} not found.")
            entry = matches[0]
        if entry.length == 0 and not entry.sha256:
            raise FileNotFoundError(f"LatticeFS v2: {path!r} has been deleted.")
        sid = space_id if space_id is not None else entry.space_id
        blob = self._mapper(sid).decode(entry.start_V, entry.length)
        plaintext = self._decrypt_content(blob, entry.flags)
        digest = self._sha256(plaintext)
        if digest != entry.sha256:
            raise RuntimeError(f"LatticeFS v2: integrity FAILED for {path!r} v{entry.version}")
        v_note = f"v{entry.version}" + ("" if version is None else " [pinned]")
        print(f"[LatticeFS v2] READ   {path!r}  {v_note}  {len(plaintext)}B  ✅ hash ok  V={fmt_short(entry.start_V)}")
        return plaintext

    def delete_file(self, path: str):
        path = self._normalise_path(path)
        self._assert_exists(path)
        self._jlog(JOP_DELETE_FILE, {"path": path})
        tombstone = self._store._versions.delete_head(path)
        self._jlog(JOP_INDEX_UPDATE, {"path": path, "version": tombstone.version, "tombstone": True})
        self._jcommit()
        print(f"[LatticeFS v2] DELETE {path!r}  (coordinate preserved in history)")

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
        path = self._normalise_path(path)
        return self._store._versions.all_versions(path)

    def compact(self, keep_last_n: int = None) -> int:
        if keep_last_n is None:
            total = sum(len(self._store._versions.all_versions(p)) for p in self._store._versions.all_paths())
            print(f"[LatticeFS v2] COMPACT (keep_last_n=None) — {total} total version entries preserved.")
            return 0
        pruned = self._store._versions.prune_all(keep_last_n)
        self._jlog(JOP_INDEX_UPDATE, {"compact": True, "keep_last_n": keep_last_n, "pruned": pruned})
        self._jcommit()
        print(f"[LatticeFS v2] COMPACT keep_last_n={keep_last_n} — pruned {pruned} version entries.")
        return pruned

    def define_space(self, space_id: int, name: str):
        self._store._spaces.define(space_id, name)
        self._jlog(JOP_INDEX_UPDATE, {"define_space": space_id, "name": name})
        self._jcommit()
        print(f"[LatticeFS v2] DEFINE SPACE  {space_id} → {name!r}")

    def list_spaces(self):
        for sid, name in sorted(self._store._spaces.all_spaces().items()):
            n_files = len(self._store._versions.all_live_heads(space_id=sid))
            print(f"  Space {sid:2d}  {name:<20}  {n_files} live file(s)")

    def ls(self, prefix: str = None, space_id: int = None):
        heads = self._store._versions.all_live_heads(space_id=space_id)
        if prefix:
            prefix = self._normalise_path(prefix)
            heads = {k: v for k, v in heads.items() if k.startswith(prefix)}
        for path, entry in sorted(heads.items()):
            print(f"  {path}  sp={entry.space_id}  v{entry.version}  {entry.length}B")

    def list_paths(self, prefix: str = None, space_id: int = None) -> List[str]:
        """
        NEW (additive, v12.3.0 patch): returns live file paths as DATA, not
        print output. ls() only prints — there was no public way to get a
        usable list of paths in a space, which PollingManager's Space 5
        release job needs. Purely additive; does not modify ls() or any
        other existing method/behavior.
        """
        heads = self._store._versions.all_live_heads(space_id=space_id)
        if prefix:
            prefix = self._normalise_path(prefix)
            heads = {k: v for k, v in heads.items() if k.startswith(prefix)}
        return sorted(heads.keys())

    def replay_journal(self) -> int:
        uncommitted = self._journal.replay()
        if not uncommitted:
            print("[LatticeFS v2] Journal clean — nothing to replay.")
            return 0
        replayed = 0
        for rec in uncommitted:
            op = rec["op"]
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
            except Exception as exc:
                print(f"  [Journal] ⚠  Could not replay op={op}: {exc}")
        self._store.flush()
        self._journal.clear()
        print(f"[LatticeFS v2] Replay complete — {replayed} record(s) applied.")
        return replayed

    def register_url(self, url_path: str, coordinate: int, metadata: dict = None) -> dict:
        entry = {"coordinate": str(coordinate), "registered": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
                  "metadata": metadata or {}}
        self._jlog(JOP_REGISTER_URL, {"url_path": url_path, "entry": entry})
        self._store._url_index[url_path] = entry
        self._jcommit()
        return entry

    def resolve_url(self, url_path: str) -> Optional[dict]:
        rec = self._store._url_index.get(url_path)
        if rec is None:
            return None
        result = dict(rec)
        result["coordinate"] = int(result["coordinate"])
        return result

    def unregister_url(self, url_path: str):
        if url_path not in self._store._url_index:
            return
        self._jlog(JOP_UNREGISTER_URL, {"url_path": url_path})
        self._store._url_index.pop(url_path)
        self._jcommit()

    def list_urls(self):
        pass

    def save(self, json_path: str):
        self._store.flush()
        self._drive.save(json_path)

    def load(self, json_path: str):
        passphrase = self._store._passphrase
        self._drive.load(json_path)
        self._store = CoordStore(self._drive, passphrase, self._mapper_kw)
        self._journal = Journal(self._drive, passphrase)
        self._check_uncommitted()

    def set_encryption_key(self, passphrase: str):
        self._store._passphrase = passphrase
        self._journal._passphrase = passphrase
        self._store.flush()


def lattice_fs_v2(sector_size: int = 512, n_sectors: int = 128, chart_base: int = 256,
                   mask_base: int = 1_000_000_000_000, num_digits: int = 100,
                   scale_factor: int = 5000, passphrase: str = None) -> LatticeFSv2:
    if not _BNS_OK:
        raise RuntimeError("BNS modules must be on sys.path.")
    drive = FoldingLatticeDrive(sector_size=sector_size, n_sectors=n_sectors, chart_base=chart_base,
                                 mask_base=mask_base, num_digits=num_digits, scale_factor=scale_factor)
    mapper_kw = dict(chart_base=chart_base, mask_base=mask_base, num_digits=num_digits, scale_factor=scale_factor)
    fs = LatticeFSv2(drive, passphrase=passphrase, mapper_kwargs=mapper_kw)
    return fs
