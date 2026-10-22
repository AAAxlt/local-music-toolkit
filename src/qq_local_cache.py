"""No-Frida local QQ Music MMKV material provider.

Reads a bounded snapshot of QQ Music machine-derived MMKV files plus their ``*.crc``
sidecars, derives candidate AES keys from read-only current-user machine inputs,
and returns song ekey material keyed by the *current* source SHA-256.

Hard boundaries: no QQ client import, no process access, no Frida, no network,
no persisted identifiers or keys, no writes at fetch time.  All sensitive values
stay in memory.  Missing or undecryptable material is simply absent from the
result; diagnostics expose counts only, never names, keys, ekeys or material.

MMKV layout used here (validated, not guessed):
  file[0:4]   actual ciphertext size, little-endian
  file[4:4+n] AES-CFB128 ciphertext
  sidecar[0:4]   CRC32(ciphertext), little-endian
  sidecar[4:8]   MMKV metadata version, accepted 2..4
  sidecar[8:12]  sequence (unused)
  sidecar[12:28] 16-byte CFB IV

The AES key is the first 16 ASCII bytes of the 32-byte uppercase hex key.
The inner log is a strict bounded whole-buffer protobuf-style map: varint
holder, then repeated varint key length + key + varint value length + value
(empty value deletes the key).  Typical ekey values are ~704 ASCII bytes and
may themselves have one inner varint length prefix.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import struct
import uuid
import zlib
from pathlib import Path

try:  # package-style import when available
    from . import qq_ekey
    from .qq_windows_inputs import normalize_mac, read_machine_inputs
except ImportError:  # normal flat source-directory import
    import qq_ekey
    from qq_windows_inputs import normalize_mac, read_machine_inputs

__all__ = [
    "LocalCacheMaterialProvider",
    "CacheError",
    "default_cache_root",
    "hardware_variants",
    "iter_key_candidates",
    "derive_cache_filename",
    "parse_mmkv_fields",
    "source_identifier",
    "source_sha256",
    "SUPPORTED_MATERIAL_LENGTHS",
]

MAX_CACHE_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_FILES = 2048
MAX_KEY_CANDIDATES = 4096
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_DRIVES = 16
MATERIAL_BYTES = 512
TAIL_BYTES = 192
MAX_MUSICEX_TAG_BYTES = 1024 * 1024
MUSICEX_MIN_TAG_BYTES = 0xC0
MUSICEX_NAME_OFFSET = 0x48
MUSICEX_NAME_BYTES = 0x64
MUSICEX_MAGIC = b"musicex\0"

# The local material parser is shared by every QQ container.  MFLAC's proven
# 512-byte core stays 512-only, while QMC map/RC4 can consume these explicit
# key lengths.  Anything else is not guessed or truncated.
SUPPORTED_MATERIAL_LENGTHS = frozenset((
    16, 24, 32, 48, 64, 80, 96, 112, 128, 160, 192, 224,
    256, 320, 384, 448, 512, 640, 768, 896, 1024,
))
SALT = bytes.fromhex("5cbd987c1c38178e")
PREFIXES = ("Driver", "Sumary", "tencent", "dllmain", "Mution", "Check", "Desktop")
SUFFIXES = (".dat", ".db", ".bak", ".data", ".cah")
HEX_UPPER = re.compile(rb"[0-9A-F]{32}")


class CacheError(Exception):
    """Structural cache failure; contains no private values."""


# ---------------------------------------------------------------------------
# AES-128 CFB128
# ---------------------------------------------------------------------------

try:
    from Cryptodome.Cipher import AES
except Exception as exc:  # a single external AES dependency is allowed here
    raise CacheError("AES backend (PyCryptodomex) is unavailable") from exc


def _aes_cfb128_decrypt(key, iv, data):
    return AES.new(key, AES.MODE_CFB, iv=iv, segment_size=128).decrypt(data)


# ---------------------------------------------------------------------------
# Bounded MMKV parsing
# ---------------------------------------------------------------------------

def _read_varint(raw, position):
    value = 0
    for shift in range(0, 35, 7):
        if position >= len(raw):
            raise CacheError("truncated varint")
        byte = raw[position]
        position += 1
        if shift == 28 and byte > 0x0F:
            raise CacheError("varint overflow")
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            if shift and byte == 0:
                raise CacheError("non-canonical varint")
            return value, position
    raise CacheError("invalid varint")


def parse_mmkv_fields(raw):
    """Parse the whole MMKV protobuf map; reject truncation/trailing junk."""
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise CacheError("MMKV plaintext type is invalid")
    raw = bytes(raw)
    holder, position = _read_varint(raw, 0)
    # MMKV skips this uint32 size holder; it is not a trusted map byte count.
    result = {}
    while position < len(raw):
        key_length, position = _read_varint(raw, position)
        if not 1 <= key_length <= 4096 or position + key_length > len(raw):
            raise CacheError("MMKV key length is out of bounds")
        key = raw[position:position + key_length]
        position += key_length
        value_length, position = _read_varint(raw, position)
        if value_length > 65536 or position + value_length > len(raw):
            raise CacheError("MMKV value length is out of bounds")
        value = raw[position:position + value_length]
        position += value_length
        if value_length:
            result[key] = value
        else:
            result.pop(key, None)
    if not result:
        raise CacheError("MMKV map is empty")
    return result


def _read_stable(path, limit):
    try:
        before = os.stat(path, follow_symlinks=False)
    except OSError:
        raise CacheError("cache entry is unreadable")
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise CacheError("cache entry is not a bounded regular file")
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
        after = os.stat(path, follow_symlinks=False)
    except OSError:
        raise CacheError("cache entry read failed")
    if len(data) > limit:
        raise CacheError("cache entry exceeds its bound")
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise CacheError("cache entry changed during read")
    return data


def _decrypt_candidate(path, key):
    data = _read_stable(path, MAX_CACHE_BYTES)
    meta = _read_stable(Path(str(path) + ".crc"), 4096)
    if len(data) + len(meta) > MAX_CACHE_BYTES:
        raise CacheError("candidate bundle exceeds the snapshot bound")
    if len(data) < 8 or len(meta) < 28:
        raise CacheError("candidate header is too short")
    actual = struct.unpack_from("<I", data, 0)[0]
    if not 1 <= actual <= MAX_CACHE_BYTES or 4 + actual > len(data):
        raise CacheError("MMKV actual size is out of bounds")
    version = struct.unpack_from("<I", meta, 4)[0]
    if not 2 <= version <= 4:
        raise CacheError("MMKV metadata version is unsupported")
    ciphertext = data[4:4 + actual]
    expected_crc = struct.unpack_from("<I", meta, 0)[0]
    if zlib.crc32(ciphertext) & 0xFFFFFFFF != expected_crc:
        raise CacheError("MMKV ciphertext CRC mismatch")
    iv = meta[12:28]
    plaintext = _aes_cfb128_decrypt(key[:16], iv, ciphertext)
    return parse_mmkv_fields(plaintext)


# ---------------------------------------------------------------------------
# Machine-derived candidates and filename derivation
# ---------------------------------------------------------------------------

def _printable(text, limit=256):
    if not isinstance(text, str):
        return ""
    text = text.strip()
    if not text or len(text) > limit:
        return ""
    if any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in text):
        return ""
    return text


def _pair_swap(text):
    if len(text) < 2:
        return ""
    pairs = [text[i:i + 2][::-1] for i in range(0, len(text) - 1, 2)]
    if len(text) % 2:
        pairs.append(text[-1])
    return _printable("".join(pairs))


def _hex_decoded(text):
    if len(text) < 4 or len(text) % 2 or re.fullmatch(r"[0-9A-Fa-f]+", text) is None:
        return ""
    try:
        return _printable(bytes.fromhex(text).decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return ""


def hardware_variants(machine_inputs):
    """Empty tuple, then raw/pair-swapped/hex-decoded serial per drive.

    No whitespace-padded variants.  Model and firmware are trimmed only.
    Variant index 2 for the first drive is the calibrated pair-swapped serial.
    """
    variants = [("", "", "")]
    seen = set(variants)
    drives = machine_inputs.get("drives", []) if isinstance(machine_inputs, dict) else []
    for drive in drives[:MAX_DRIVES]:
        if not isinstance(drive, dict):
            continue
        serial = _printable(drive.get("serial", ""), 128)
        model = _printable(drive.get("model", ""), 256)
        firmware = _printable(drive.get("firmware", ""), 256)
        for candidate in (serial, _pair_swap(serial), _hex_decoded(serial)):
            item = (candidate, model, firmware)
            if candidate and item not in seen:
                seen.add(item)
                variants.append(item)
    return variants


def iter_key_candidates(machine_inputs):
    """Yield bounded 32-byte ASCII keys; empty MAC/serial inputs are allowed."""
    macs = {""}
    if isinstance(machine_inputs, dict):
        for raw in machine_inputs.get("macs", [])[:64]:
            mac = normalize_mac(raw)
            if mac:
                macs.add(mac)
    if not macs:
        macs.add("")
    hardware = hardware_variants(machine_inputs)
    seen = set()
    count = 0
    for mac in sorted(macs):
        for serial, model, firmware in hardware:
            text = (mac + serial + model + firmware).encode("ascii") + SALT
            digest = hashlib.md5(text).digest()
            key = uuid.UUID(bytes_le=digest).hex.upper().encode("ascii")
            if key in seen:
                continue
            seen.add(key)
            yield key
            count += 1
            if count >= MAX_KEY_CANDIDATES:
                return


def derive_cache_filename(key, prefixes, suffixes):
    """Fixed prefix/suffix + four lowercase key letters (MD5 GUID field order)."""
    if not isinstance(key, (bytes, bytearray)) or HEX_UPPER.fullmatch(bytes(key)) is None:
        raise CacheError("cache key is not 32 uppercase hex bytes")
    if len(prefixes) != 7 or len(suffixes) != 5:
        raise CacheError("cache filename tables must be 7/5 entries")
    letters = bytes(value for value in key if 65 <= value <= 90).lower()[:4]
    return prefixes[key[3] % 7] + letters.decode("ascii") + suffixes[key[16] % 5]


def default_cache_root():
    """%APPDATA%/Tencent/QQMusic, or None when APPDATA is unavailable."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Tencent" / "QQMusic"


def _cache_files(root):
    if root is None:
        return []
    root = Path(root)
    try:
        if not root.is_dir():
            return []
    except OSError:
        return []
    files = []
    for base, dirs, names in os.walk(root, followlinks=False):
        depth = len(Path(base).relative_to(root).parts)
        if depth >= 3:
            dirs[:] = []
        else:
            dirs[:] = [name for name in dirs
                       if not name.startswith(".")
                       and not os.path.islink(os.path.join(base, name))]
        for name in sorted(names):
            if not name.lower().endswith(SUFFIXES):
                continue
            path = Path(base) / name
            try:
                info = os.stat(path, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                files.append(path)
                if len(files) >= MAX_SNAPSHOT_FILES:
                    return files
    return files


# ---------------------------------------------------------------------------
# Source metadata and provider
# ---------------------------------------------------------------------------

def source_sha256(path):
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_SOURCE_BYTES:
                raise CacheError("source exceeds the size bound")
            digest.update(chunk)
    return digest.hexdigest()


def source_identifier(path):
    """Read the per-file musicex MediaFileName (variable tag size >= 0xC0).

    The 192-byte v1 form used by the proven MFLAC route is unchanged.  MGG and
    other musicex variants can carry a larger tag; the whole tag is bounded by
    both the source size and an explicit parsing cap before its name is read.
    """
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        if size < 16:
            raise CacheError("source is too short for a musicex tail")
        handle.seek(size - 16)
        footer = handle.read(16)
        if len(footer) != 16 or footer[8:] != MUSICEX_MAGIC:
            raise CacheError("source tail magic is invalid")
        tag_size = struct.unpack_from("<I", footer, 0)[0]
        version = struct.unpack_from("<I", footer, 4)[0]
        if version != 1:
            raise CacheError("source tail version is invalid")
        if not MUSICEX_MIN_TAG_BYTES <= tag_size <= size:
            raise CacheError("source tail size is invalid")
        if tag_size > MAX_MUSICEX_TAG_BYTES:
            raise CacheError("source tail exceeds the parsing bound")
        handle.seek(size - tag_size)
        tag = handle.read(tag_size)
    if len(tag) != tag_size:
        raise CacheError("source tail is truncated")
    raw_name = tag[MUSICEX_NAME_OFFSET:MUSICEX_NAME_OFFSET + MUSICEX_NAME_BYTES]
    try:
        text = raw_name.decode("utf-16-le", "strict").split("\x00", 1)[0]
    except UnicodeDecodeError:
        raise CacheError("source identifier is not UTF-16LE")
    text = text.strip()
    if not text or len(text) > 200 or any(ord(ch) < 0x20 for ch in text):
        raise CacheError("source identifier is unusable")
    return text


def _identifier_variants(identifier):
    return [identifier]


def _lookup_ekey(fields, identifier):
    for variant in _identifier_variants(identifier):
        for encoding in ("utf-8", "ascii"):
            try:
                key = variant.encode(encoding)
            except UnicodeEncodeError:
                continue
            value = fields.get(key)
            if value:
                return value
    return None


def _ekey_value(value):
    value = bytes(value).strip(b"\x00 \t\r\n")
    if not 16 <= len(value) <= qq_ekey.MAX_EKEY_BYTES:
        return None
    try:
        length, position = _read_varint(value, 0)
    except CacheError:
        return None
    if 0 < position < len(value) and length == len(value) - position:
        value = value[position:]
    return value if 16 <= len(value) <= qq_ekey.MAX_EKEY_BYTES else None


class LocalCacheMaterialProvider:
    """Material provider over a bounded local MMKV snapshot.

    ``cache_root`` defaults lazily to ``%APPDATA%/Tencent/QQMusic``.
    ``machine_inputs`` may be supplied by tests; otherwise current-user Windows
    inputs are collected only when a fetch has real requested paths and cache
    candidates.  ``filename_parts`` may replace the fixed 7 prefixes and
    5 suffixes for isolated fixtures; production uses the recovered tables.
    """

    def __init__(self, cache_root=None, machine_inputs=None, filename_parts=None):
        self._cache_root = cache_root
        self._machine_inputs = machine_inputs
        self._filename_parts = filename_parts or (PREFIXES, SUFFIXES)
        self.diagnostics = {
            "files": 0, "keys": 0, "decrypted": 0,
            "materials": 0, "errors": 0,
        }

    def _root(self):
        if self._cache_root is not None:
            return Path(self._cache_root)
        return default_cache_root()

    def _inputs(self):
        if self._machine_inputs is None:
            self._machine_inputs = read_machine_inputs()
        return self._machine_inputs

    def _candidate_files(self, key, files):
        try:
            prefixes, suffixes = self._filename_parts
            wanted = derive_cache_filename(key, prefixes, suffixes)
        except (CacheError, TypeError):
            return []
        # Fixture suffix tables may omit an extension; native tables include it.
        names=(wanted,) if wanted.endswith(SUFFIXES) else (wanted,wanted+'.cah')
        return [path for path in files if path.name in names]

    def fetch(self, paths):
        """Return ``{sourceSHA256: 512-byte material}`` for resolvable paths."""
        wanted = {}
        if isinstance(paths, (str, bytes, os.PathLike)):
            paths = [paths]
        try:
            for path in paths:
                try:
                    sha = source_sha256(path)
                    if sha in wanted:
                        continue
                    identifier = source_identifier(path)
                except (OSError, CacheError):
                    continue
                wanted[sha] = identifier
        except TypeError:
            return {}
        if not wanted:
            return {}
        files = _cache_files(self._root())
        self.diagnostics["files"] = len(files)
        if not files:
            return {}
        try:
            machine_inputs = self._inputs()
        except Exception:
            return {}
        results = {}
        for key in iter_key_candidates(machine_inputs):
            self.diagnostics["keys"] += 1
            for path in self._candidate_files(key, files):
                try:
                    fields = _decrypt_candidate(path, key)
                except Exception:
                    self.diagnostics["errors"] += 1
                    continue
                self.diagnostics["decrypted"] += 1
                for sha, identifier in list(wanted.items()):
                    if sha in results:
                        continue
                    value = _lookup_ekey(fields, identifier)
                    if not value:
                        continue
                    value = _ekey_value(value)
                    if not value:
                        continue
                    try:
                        material = qq_ekey.parse_ekey(value)
                    except Exception:
                        continue
                    if len(material) not in SUPPORTED_MATERIAL_LENGTHS:
                        continue
                    results[sha] = material
                    self.diagnostics["materials"] += 1
                    if len(results) == len(wanted):
                        return results
        return results
