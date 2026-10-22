"""Offline ports of common Unlock Music container algorithms.

Ported from Unlock Music CLI (MIT, Copyright (c) 2020-2021 Unlock Music),
pinned at commit ``aec0d85fd9f258a513ecc930a72eaf85af9e1781``:
https://github.com/leafxdd/unlock-music/tree/aec0d85fd9f258a513ecc930a72eaf85af9e1781
See ``THIRD_PARTY_NOTICES.md`` for the full attribution and retained license.

This module is a deliberately bounded, no-Frida transliteration of the MIT
licensed Go sources pinned in ``third_party/unlock-music/SOURCE.json``:

* QMC static, map, and general RC4 stream ciphers (``algo/qmc``)
* QMC footer forms: QTag, raw embedded key, static/no-footer, musicex
* Kuwo KWM (``algo/kwm``)
* Xiami XM (``algo/xiami``)
* Ximalaya X2M/X3M header descrambling (``algo/ximalaya`` plus tables)
* QQ Music iOS TM2/TM6 header restoration (``algo/tm``)
* Kugou KGM/KGMA/VPR crypto v3 slot 1 (``algo/kgm``)

KGM/KGG crypto v5 is intentionally *not* implemented: it needs a separate
Kugou database route and is reported as an explicit unsupported/waiting case.

No module here reads a process, cache, account, network, or client.  Callers
provide bytes and, for musicex, one material blob that belongs to that exact
source file.  Nothing is printed and no key material is stored in this module.
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
from dataclasses import dataclass
from pathlib import Path

try:  # package-style import when available
    from . import qq_ekey
except ImportError:  # normal flat source-directory import
    import qq_ekey

MAX_CONTAINER_BYTES = 256 * 1024 * 1024
MAX_QMC_META_BYTES = 64 * 1024
MAX_MUSICEX_TAG_BYTES = 1024 * 1024
# Some MGG containers carry two or three consecutive cleartext v1 tags with the
# same identifier (Tianya 2x192, Flower/River 3x192).  Strip only this small,
# structurally proven bound; never trim arbitrary bytes after EOS.
MAX_MUSICEX_TAILS = 8
MAX_QMC_MATERIAL_BYTES = qq_ekey.MAX_MATERIAL_BYTES

MUSICEX_MAGIC = b"musicex\x00"
MUSICEX_VERSION = 1
MUSICEX_MIN_TAG = 0xC0
MUSICEX_NAME_OFFSET = 0x48
MUSICEX_NAME_BYTES = 0x64

# Upper-case hex aliases are the Weiyun / legacy spellings registered upstream.
QMC_ALIAS_EXT = {
    "qmc0": ".mp3",
    "qmc3": ".mp3",
    "qmcflac": ".flac",
    "qmcogg": ".ogg",
    "qmc2": ".m4a",
    "qmc4": ".m4a",
    "qmc6": ".m4a",
    "qmc8": ".m4a",
    "tkm": ".m4a",
    "mmp4": ".mp4",
    "bkcmp3": ".mp3",
    "bkcm4a": ".m4a",
    "bkcflac": ".flac",
    "bkcwav": ".wav",
    "bkcape": ".ape",
    "bkcogg": ".ogg",
    "bkcwma": ".wma",
    "666c6163": ".flac",
    "6d7033": ".mp3",
    "6f6767": ".ogg",
    "6d3461": ".m4a",
    "776176": ".wav",
}

_QMC_ALIAS_NAMES = "|".join(sorted(map(re.escape, QMC_ALIAS_EXT), key=len, reverse=True))
_QMC_NAME_RE = re.compile(r"\.(mgg|mflac)(?:[0-9ahml])?(?:\.flac)?$", re.I)
_QMC_ALIAS_RE = re.compile(r"\.(" + _QMC_ALIAS_NAMES + r")$", re.I)
_LEGACY_NAME_RE = re.compile(
    r"\.(kgm|kgma|kgg|vpr|kwm|xm|x2m|x3m|tm[0-9a-z]?)(?:\.flac)?$", re.I
)

KGM_HEADER = bytes(
    (0x7C, 0xD5, 0x32, 0xEB, 0x86, 0x02, 0x7F, 0x4B,
     0xA8, 0xAF, 0xA6, 0x8E, 0x0F, 0xFF, 0x99, 0x14)
)
VPR_HEADER = bytes(
    (0x05, 0x28, 0xBC, 0x96, 0xE9, 0xE4, 0x5A, 0x43,
     0x91, 0xAA, 0xBD, 0xD0, 0x7A, 0xF5, 0x36, 0x31)
)
KWM_HEADER_1 = b"yeelion-kuwo-tme"
KWM_HEADER_2 = b"yeelion-kuwo\x00\x00\x00\x00"
KWM_KEY_PREDEFINED = b"MoOtOiTvINGwd2E6n0E1i7L5t2IoOoNk"
XIAMI_TYPE = {" WAV": ".wav", "FLAC": ".flac", " MP3": ".mp3", " A4M": ".m4a"}
TM_REPLACEMENT = b"\x00\x00\x00\x20ftyp"
ASF_GUID = bytes(
    (0x30, 0x26, 0xB2, 0x75, 0x8E, 0x66, 0xCF, 0x11,
     0xA6, 0xD9, 0x00, 0xAA, 0x00, 0x62, 0xCE, 0x6C)
)

_TABLES = Path(__file__).resolve().parents[1] / "third_party" / "unlock-music" / "tables"
X2M_TABLE_SHA256 = "f47bc3ea94f109650f9753329b4faa62b77eab1d37df738413c819b344838605"
X3M_TABLE_SHA256 = "5cd0abe24ad9b04b31262f552605b62c78549f8c4aef3bf54e6c780d7e3de39e"
X2M_KEY = b"xmly"
X3M_KEY = bytes(
    (0x33, 0x39, 0x38, 0x39, 0x64, 0x31, 0x31, 0x31,
     0x61, 0x61, 0x64, 0x35, 0x36, 0x31, 0x33, 0x39,
     0x34, 0x30, 0x66, 0x34, 0x66, 0x63, 0x34, 0x34,
     0x62, 0x36, 0x33, 0x39, 0x62, 0x32, 0x39, 0x32)
)


class LegacyError(Exception):
    """Bounded structural failure; messages never contain key bytes."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category
        self.message = str(message)


@dataclass(frozen=True)
class Decoded:
    data: bytes
    extension: str
    algorithm: str
    kind: str
    note: str = ""


@dataclass(frozen=True)
class QmcFooter:
    kind: str                 # qtag | rawkey | static | musicex
    audio_length: int
    material: bytes | None = None
    identifier: str | None = None
    tag_size: int | None = None
    meta_size: int | None = None
    tag_count: int = 1


# ---------------------------------------------------------------------------
# Content sniffing
# ---------------------------------------------------------------------------

def _mpeg4_brand(head):
    if len(head) < 16 or head[4:8] != b"ftyp":
        return None
    size = int.from_bytes(head[0:4], "big")
    if size < 16:
        return None
    major = head[8:12]
    compatible = [head[i:i + 4] for i in range(16, min(len(head), size), 4)]
    return major, compatible


def sniff_audio(head: bytes):
    """Return a conservative output extension from a real leading signature."""
    if not isinstance(head, (bytes, bytearray)):
        return None
    head = bytes(head)
    if head.startswith(b"fLaC"):
        return ".flac"
    if head.startswith(b"OggS"):
        return ".ogg"
    if head.startswith(b"ID3"):
        return ".mp3"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return ".wav"
    if len(head) >= 12 and head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        return ".aiff"
    if head.startswith(b"MAC "):
        return ".ape"
    if head.startswith(ASF_GUID):
        return ".wma"
    brand = _mpeg4_brand(head)
    if brand is not None:
        major, compatible = brand
        if major in (b"M4A ", b"M4B ", b"M4P ") or b"M4A " in compatible:
            return ".m4a"
        return ".mp4"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        layer = (head[1] >> 1) & 0x03
        return ".aac" if layer == 0 else ".mp3"
    return None


def qmc_hint(filename: str):
    """Return the expected output extension for a known QMC filename alias."""
    name = str(filename).lower()
    match = _QMC_NAME_RE.search(name)
    if match:
        return ".ogg" if match.group(1).lower() == "mgg" else ".flac"
    match = _QMC_ALIAS_RE.search(name)
    if match:
        return QMC_ALIAS_EXT[match.group(1).lower()]
    return None


def preview_kind(filename: str, head: bytes, tail: bytes):
    """Cheap content/filename preview; the full bytes are read only for hits."""
    head = bytes(head[:16])
    tail = bytes(tail)
    if head[:4] == b"ifmt" and len(head) >= 12 and head[8:12] == b"\xfe" * 4:
        return "xiami"
    if head[:4] == b"QQMU":
        return "tm"
    if head[:16] in (KGM_HEADER, VPR_HEADER):
        return "kgm"
    if head[:16] in (KWM_HEADER_1, KWM_HEADER_2):
        return "kwm"
    name = str(filename).lower()
    if name.endswith((".x2m", ".x3m", ".xm")):
        return "ximalaya"
    if len(tail) >= 8 and tail[-8:] == MUSICEX_MAGIC:
        return "qmc"
    if len(tail) >= 4 and tail[-4:] in (b"QTag", b"STag"):
        return "qmc"
    if qmc_hint(name) is not None:
        return "qmc"
    return None


def _replace_suffix(name: str, extension: str):
    if _QMC_NAME_RE.search(name):
        return _QMC_NAME_RE.sub(extension, name)
    if _QMC_ALIAS_RE.search(name):
        return _QMC_ALIAS_RE.sub(extension, name)
    if _LEGACY_NAME_RE.search(name):
        return _LEGACY_NAME_RE.sub(extension, name)
    path = Path(name)
    if path.suffix:
        return path.with_suffix(extension).name
    return name + extension


def output_name(name: str, extension: str):
    """Map an encrypted source name to an output name without traversal."""
    candidate = _replace_suffix(str(name), extension)
    if Path(candidate).name != candidate:
        raise LegacyError("output-escape", "output name is not a basename")
    return candidate


# ---------------------------------------------------------------------------
# QMC key material and footers
# ---------------------------------------------------------------------------

def _parse_qkey(raw: bytes, label: str):
    raw = bytes(raw)
    if not raw:
        raise LegacyError("qmc-key-invalid", "%s is empty" % label)
    if len(raw) > qq_ekey.MAX_EKEY_BYTES:
        raise LegacyError("qmc-key-invalid", "%s exceeds the key-size bound" % label)
    try:
        material = qq_ekey.parse_ekey(raw)
    except qq_ekey.EkeyError as exc:
        raise LegacyError("qmc-key-invalid", "%s is invalid (%s)" % (label, exc.code)) from None
    except Exception:
        raise LegacyError("qmc-key-invalid", "%s is structurally invalid" % label) from None
    if not 0 < len(material) <= MAX_QMC_MATERIAL_BYTES:
        raise LegacyError("qmc-key-invalid", "%s material length is out of bounds" % label)
    return material


def _tag_identifier(buffer: bytes):
    try:
        text = bytes(buffer).decode("utf-16-le", "strict").split("\x00", 1)[0]
    except UnicodeDecodeError:
        raise LegacyError("musicex-identifier-invalid", "musicex name is not UTF-16LE") from None
    text = text.strip()
    if not text or len(text) > 200 or any(ord(ch) < 0x20 for ch in text):
        raise LegacyError("musicex-identifier-invalid", "musicex name is unusable")
    return text


def _valid_musicex_tail(data: bytes, end: int):
    """Return (tag_size, identifier) for a cleartext v1 tag ending at ``end``.

    Used only for the bounded repeated-tail check.  A malformed or partial
    block is simply not a known tag, so parsing stops without trimming audio.
    """
    if end < 16 or end > len(data):
        return None
    if data[end - 8:end] != MUSICEX_MAGIC:
        return None
    tag_size = int.from_bytes(data[end - 16:end - 12], "little")
    version = int.from_bytes(data[end - 12:end - 8], "little")
    if version != MUSICEX_VERSION:
        return None
    if not MUSICEX_MIN_TAG <= tag_size <= end or tag_size > MAX_MUSICEX_TAG_BYTES:
        return None
    tag = data[end - tag_size:end]
    try:
        identifier = _tag_identifier(tag[MUSICEX_NAME_OFFSET:
                                        MUSICEX_NAME_OFFSET + MUSICEX_NAME_BYTES])
    except LegacyError:
        return None
    return tag_size, identifier


def parse_qmc_footer(data: bytes):
    """Parse one bounded QMC footer.  Never guesses a key for musicex."""
    data = bytes(data)
    size = len(data)
    if size < 4:
        raise LegacyError("qmc-unsupported", "QMC container is too short")
    if size > MAX_CONTAINER_BYTES:
        raise LegacyError("too-large", "QMC container exceeds 256 MiB")

    tail4 = data[-4:]
    if tail4 == b"QTag":
        if size < 9:
            raise LegacyError("qmc-footer-truncated", "QTag footer is truncated")
        meta_len = int.from_bytes(data[-8:-4], "big")
        if not 1 <= meta_len <= MAX_QMC_META_BYTES or meta_len > size - 8:
            raise LegacyError("qmc-footer-truncated", "QTag metadata length is out of bounds")
        audio_len = size - 8 - meta_len
        meta = data[audio_len:audio_len + meta_len]
        parts = meta.split(b",")
        if len(parts) != 3:
            raise LegacyError("qmc-qtag-invalid", "QTag metadata is not three fields")
        try:
            int(parts[1])
            int(parts[2])
        except ValueError:
            raise LegacyError("qmc-qtag-invalid", "QTag song/extra identifier is not numeric") from None
        material = _parse_qkey(parts[0], "QTag ekey")
        return QmcFooter("qtag", audio_len, material=material, meta_size=meta_len)

    if tail4 == b"STag":
        raise LegacyError("qmc-stag-unsupported", "STag containers carry no media key")

    if data[-8:] == MUSICEX_MAGIC:
        if size < 16:
            raise LegacyError("musicex-truncated", "musicex footer is truncated")
        tag_size = int.from_bytes(data[-16:-12], "little")
        version = int.from_bytes(data[-12:-8], "little")
        if version != MUSICEX_VERSION:
            raise LegacyError("musicex-version-unsupported",
                              "musicex version %d is unsupported" % version)
        if not MUSICEX_MIN_TAG <= tag_size <= size:
            raise LegacyError("musicex-size-invalid", "musicex tag size is out of bounds")
        if tag_size > MAX_MUSICEX_TAG_BYTES:
            raise LegacyError("musicex-size-invalid", "musicex tag exceeds the parsing bound")
        tag = data[size - tag_size:]
        identifier = _tag_identifier(tag[MUSICEX_NAME_OFFSET:
                                        MUSICEX_NAME_OFFSET + MUSICEX_NAME_BYTES])
        # Known MGGs may repeat the same cleartext v1 tag immediately before
        # the final one.  Strip only identical span/version/identifier tags,
        # bounded by MAX_MUSICEX_TAILS; opaque reserved bytes may differ.
        audio_len = size - tag_size
        tag_count = 1
        while tag_count < MAX_MUSICEX_TAILS:
            previous = _valid_musicex_tail(data, audio_len)
            if previous is None:
                break
            previous_size, previous_identifier = previous
            if previous_size != tag_size or previous_identifier != identifier:
                break
            audio_len -= previous_size
            tag_count += 1
        return QmcFooter("musicex", audio_len, identifier=identifier,
                         tag_size=tag_size, tag_count=tag_count)

    key_len = int.from_bytes(tail4, "little")
    if 0 < key_len <= 0xFFFF:
        if key_len + 4 > size:
            raise LegacyError("qmc-footer-truncated", "embedded QMC key length is out of bounds")
        audio_len = size - 4 - key_len
        raw = data[audio_len:audio_len + key_len].rstrip(b"\x00")
        material = _parse_qkey(raw, "embedded QMC key")
        return QmcFooter("rawkey", audio_len, material=material)

    return QmcFooter("static", size)


# ---------------------------------------------------------------------------
# QMC ciphers
# ---------------------------------------------------------------------------

QMC_STATIC_BOX = bytes((
    0x77, 0x48, 0x32, 0x73, 0xDE, 0xF2, 0xC0, 0xC8, 0x95, 0xEC, 0x30, 0xB2, 0x51, 0xC3, 0xE1, 0xA0,
    0x9E, 0xE6, 0x9D, 0xCF, 0xFA, 0x7F, 0x14, 0xD1, 0xCE, 0xB8, 0xDC, 0xC3, 0x4A, 0x67, 0x93, 0xD6,
    0x28, 0xC2, 0x91, 0x70, 0xCA, 0x8D, 0xA2, 0xA4, 0xF0, 0x08, 0x61, 0x90, 0x7E, 0x6F, 0xA2, 0xE0,
    0xEB, 0xAE, 0x3E, 0xB6, 0x67, 0xC7, 0x92, 0xF4, 0x91, 0xB5, 0xF6, 0x6C, 0x5E, 0x84, 0x40, 0xF7,
    0xF3, 0x1B, 0x02, 0x7F, 0xD5, 0xAB, 0x41, 0x89, 0x28, 0xF4, 0x25, 0xCC, 0x52, 0x11, 0xAD, 0x43,
    0x68, 0xA6, 0x41, 0x8B, 0x84, 0xB5, 0xFF, 0x2C, 0x92, 0x4A, 0x26, 0xD8, 0x47, 0x6A, 0x7C, 0x95,
    0x61, 0xCC, 0xE6, 0xCB, 0xBB, 0x3F, 0x47, 0x58, 0x89, 0x75, 0xC3, 0x75, 0xA1, 0xD9, 0xAF, 0xCC,
    0x08, 0x73, 0x17, 0xDC, 0xAA, 0x9A, 0xA2, 0x16, 0x41, 0xD8, 0xA2, 0x06, 0xC6, 0x8B, 0xFC, 0x66,
    0x34, 0x9F, 0xCF, 0x18, 0x23, 0xA0, 0x0A, 0x74, 0xE7, 0x2B, 0x27, 0x70, 0x92, 0xE9, 0xAF, 0x37,
    0xE6, 0x8C, 0xA7, 0xBC, 0x62, 0x65, 0x9C, 0xC2, 0x08, 0xC9, 0x88, 0xB3, 0xF3, 0x43, 0xAC, 0x74,
    0x2C, 0x0F, 0xD4, 0xAF, 0xA1, 0xC3, 0x01, 0x64, 0x95, 0x4E, 0x48, 0x9F, 0xF4, 0x35, 0x78, 0x95,
    0x7A, 0x39, 0xD6, 0x6A, 0xA0, 0x6D, 0x40, 0xE8, 0x4F, 0xA8, 0xEF, 0x11, 0x1D, 0xF3, 0x1B, 0x3F,
    0x3F, 0x07, 0xDD, 0x6F, 0x5B, 0x19, 0x30, 0x19, 0xFB, 0xEF, 0x0E, 0x37, 0xF0, 0x0E, 0xCD, 0x16,
    0x49, 0xFE, 0x53, 0x47, 0x13, 0x1A, 0xBD, 0xA4, 0xF1, 0x40, 0x19, 0x60, 0x0E, 0xED, 0x68, 0x09,
    0x06, 0x5F, 0x4D, 0xCF, 0x3D, 0x1A, 0xFE, 0x20, 0x77, 0xE4, 0xD9, 0xDA, 0xF9, 0xA4, 0x2B, 0x76,
    0x1C, 0x71, 0xDB, 0x00, 0xBC, 0xFD, 0x0C, 0x6C, 0xA5, 0x47, 0xF7, 0xF6, 0x00, 0x79, 0x4A, 0x11,
))
_QMC_PERIOD = 0x7FFF


def _xor_integer(data: bytes, mask: bytes):
    if len(data) != len(mask):
        raise ValueError("mask length mismatch")
    if not data:
        return b""
    value = int.from_bytes(data, "big") ^ int.from_bytes(mask, "big")
    return value.to_bytes(len(data), "big")


def _repeat_to(pattern: bytes, length: int):
    if length <= 0:
        return b""
    count = (length + len(pattern) - 1) // len(pattern)
    return (pattern * count)[:length]


def _keystream(table: bytes, offset: int, length: int):
    """Rebuild the upstream QMC offset rule exactly.

    Offsets 0..0x7fff use their own mask.  From 0x8000 on the upstream code
    applies ``offset % 0x7fff``, so the tail period is table[1:0x7fff]+table[0].
    """
    if offset < 0 or length < 0:
        raise ValueError("negative QMC offset/length")
    if length == 0:
        return b""
    if offset <= _QMC_PERIOD:
        first = table[offset:_QMC_PERIOD + 1]
        if len(first) >= length:
            return first[:length]
        period = table[1:_QMC_PERIOD] + table[0:1]
        return first + _repeat_to(period, length - len(first))
    index = (offset - (_QMC_PERIOD + 1)) % _QMC_PERIOD
    period = table[1:_QMC_PERIOD] + table[0:1]
    rotated = period[index:] + period[:index]
    return _repeat_to(rotated, length)


def _xor_keystream(data: bytes, offset: int, table: bytes):
    data = bytes(data)
    stream = _keystream(table, offset, len(data))
    out = bytearray(len(data))
    chunk = 1 << 20
    for start in range(0, len(data), chunk):
        end = min(len(data), start + chunk)
        out[start:end] = _xor_integer(data[start:end], stream[start:end])
    return bytes(out)


def _static_table():
    return bytes(QMC_STATIC_BOX[((i * i + 27) & 0xFF)] for i in range(_QMC_PERIOD + 1))


def _map_table(key: bytes):
    size = len(key)

    def rotate(value, bits):
        shift = (bits + 4) % 8
        return ((value << shift) & 0xFF) | (value >> shift)

    table = bytearray(_QMC_PERIOD + 1)
    for offset in range(_QMC_PERIOD + 1):
        index = (offset * offset + 71214) % size
        table[offset] = rotate(key[index], index & 7)
    return bytes(table)


class StaticQmcCipher:
    name = "static"

    def __init__(self):
        self._table = _static_table()

    def decrypt(self, data, offset=0):
        return _xor_keystream(data, offset, self._table)


class MapQmcCipher:
    name = "map"

    def __init__(self, key):
        key = bytes(key)
        if not key:
            raise LegacyError("qmc-key-invalid", "map cipher key is empty")
        if len(key) > 300:
            raise LegacyError("qmc-key-invalid", "map cipher key is too long")
        self.key = key
        self._table = _map_table(key)

    def decrypt(self, data, offset=0):
        return _xor_keystream(data, offset, self._table)


class Rc4QmcCipher:
    name = "rc4"

    def __init__(self, key):
        key = bytes(key)
        if not 1 <= len(key) <= MAX_QMC_MATERIAL_BYTES:
            raise LegacyError("qmc-key-invalid", "RC4 key length is out of bounds")
        self.key = key
        self.n = len(key)
        box = bytearray((i & 0xFF) for i in range(self.n))
        j = 0
        for i in range(self.n):
            j = (j + box[i] + key[i % self.n]) % self.n
            box[i], box[j] = box[j], box[i]
        self.box = bytes(box)
        self.hash = 1
        for value in key:
            if value == 0:
                continue
            nxt = (self.hash * value) & 0xFFFFFFFF
            if nxt == 0 or nxt <= self.hash:
                break
            self.hash = nxt

    def _skip(self, ident):
        seed = self.key[ident % self.n]
        if seed == 0:
            return 0  # upstream zero-byte guard; avoids divide-by-zero
        index = int((float(self.hash) / float((ident + 1) * seed)) * 100.0)
        skip = index % self.n
        if skip < 0:
            skip += self.n
        return skip

    def decrypt(self, data, offset=0):
        data = bytes(data)
        if offset < 0:
            raise ValueError("negative RC4 offset")
        out = bytearray(data)
        length = len(out)
        position = 0
        absolute = offset
        if absolute < 128:
            block = min(length, 128 - absolute)
            for i in range(block):
                out[i] ^= self.key[self._skip(absolute + i)]
            position = block
            absolute += block
        while position < length:
            within = absolute % 5120
            limit = min(length - position, 5120 - within)
            skip_len = within + self._skip(absolute // 5120)
            box = bytearray(self.box)
            j = 0
            k = 0
            for i in range(-skip_len, limit):
                j = (j + 1) % self.n
                k = (box[j] + k) % self.n
                box[j], box[k] = box[k], box[j]
                if i >= 0:
                    out[position + i] ^= box[(box[j] + box[k]) % self.n]
            position += limit
            absolute += limit
        return bytes(out)


def make_qmc_cipher(material):
    material = bytes(material or b"")
    if not material:
        return StaticQmcCipher()
    if len(material) > 300:
        return Rc4QmcCipher(material)
    return MapQmcCipher(material)


def decrypt_qmc(data: bytes, footer: QmcFooter, material=None):
    """Decrypt the audio region described by ``footer`` and sniff its type."""
    data = bytes(data)
    if footer.kind in ("qtag", "rawkey"):
        key = footer.material
    elif footer.kind == "static":
        key = b""
    elif footer.kind == "musicex":
        if material is None:
            raise LegacyError("material-missing", "musicex material is not available")
        key = bytes(material)
    else:
        raise LegacyError("qmc-unsupported", "unknown QMC footer kind")
    if not 0 < footer.audio_length <= len(data):
        raise LegacyError("qmc-footer-truncated", "QMC audio length is out of bounds")
    cipher = make_qmc_cipher(key)
    output = cipher.decrypt(data[:footer.audio_length], 0)
    extension = sniff_audio(output[:64])
    if extension is None:
        raise LegacyError("qmc-key-mismatch", "QMC output does not start with a known audio signature")
    route = "qmc-" + cipher.name
    note = footer.kind
    return Decoded(output, extension, route, "qmc", note)


# ---------------------------------------------------------------------------
# KWM / Kuwo
# ---------------------------------------------------------------------------

def _kwm_mask(key: bytes):
    if len(key) != 8:
        raise LegacyError("kwm-header-invalid", "KWM key field is not eight bytes")
    number = int.from_bytes(key, "little")
    text = str(number).encode("ascii")
    if not text:
        text = b""
    if len(text) == 0:
        padded = b"\x00" * 32
    elif len(text) >= 32:
        padded = text[:32]
    else:
        padded = (text * ((32 // len(text)) + 1))[:32]
    return bytes(a ^ b for a, b in zip(KWM_KEY_PREDEFINED, padded))


def _kwm_type(field: bytes):
    text = field.rstrip(b"\x00")
    split = None
    for index, value in enumerate(text):
        if not 48 <= value <= 57:
            split = index
            break
    if split is None:
        try:
            bitrate = int(text.decode("ascii")) if text else 0
        except ValueError:
            bitrate = 0
        return bitrate, ""
    try:
        bitrate = int(text[:split].decode("ascii"))
    except ValueError:
        bitrate = 0
    extension = text[split:].decode("ascii", "replace").lower()
    if extension and not extension.startswith("."):
        extension = "." + extension
    return bitrate, extension


def decode_kwm(data: bytes):
    data = bytes(data)
    if len(data) < 0x400:
        raise LegacyError("kwm-header-invalid", "KWM header is shorter than 1024 bytes")
    if data[:16] not in (KWM_HEADER_1, KWM_HEADER_2):
        raise LegacyError("kwm-header-invalid", "KWM magic header does not match")
    mask = _kwm_mask(data[0x18:0x20])
    _bitrate, hinted = _kwm_type(data[0x30:0x38])
    payload = data[0x400:]
    output = bytearray(payload)
    for i in range(len(output)):
        output[i] ^= mask[i & 0x1F]
    extension = sniff_audio(output[:64]) or (hinted if hinted in (".mp3", ".flac", ".wav", ".ape", ".ogg") else None)
    if extension is None:
        raise LegacyError("kwm-output-unknown", "KWM output type is not recognized")
    return Decoded(bytes(output), extension, "kwm-mask", "kwm")


# ---------------------------------------------------------------------------
# Xiami XM
# ---------------------------------------------------------------------------

def decode_xiami(data: bytes):
    data = bytes(data)
    if len(data) < 16:
        raise LegacyError("xiami-header-invalid", "Xiami header is shorter than 16 bytes")
    if data[:4] != b"ifmt" or data[8:12] != b"\xfe" * 4:
        raise LegacyError("xiami-header-invalid", "Xiami magic header does not match")
    extension = XIAMI_TYPE.get(data[4:8].decode("latin-1"))
    if extension is None:
        raise LegacyError("xiami-format-unknown", "Xiami audio type is unknown")
    start = data[12] | (data[13] << 8) | (data[14] << 16)
    mask = data[15]
    payload = bytearray(data[16:])
    for i in range(len(payload)):
        if i >= start:
            payload[i] ^= mask
    detected = sniff_audio(payload[:64])
    if detected is not None and detected != extension:
        # M4A and sniffed MP4 are the same MPEG-4 audio family.  The XM header
        # is authoritative for the file extension; FFmpeg still validates the
        # full stream before publication.  Any other contradiction is rejected.
        if not ({detected, extension} <= {".m4a", ".mp4"}):
            raise LegacyError("xiami-format-mismatch", "Xiami header/type disagree")
    return Decoded(bytes(payload), extension, "xiami-xor", "xiami")


# ---------------------------------------------------------------------------
# Ximalaya X2M / X3M
# ---------------------------------------------------------------------------

def _load_table(name, expected_sha):
    path = _TABLES / name
    try:
        raw = path.read_bytes()
    except OSError:
        raise LegacyError("ximalaya-table-missing", "Ximalaya scramble table is unavailable") from None
    if len(raw) != 2048:
        raise LegacyError("ximalaya-table-invalid", "Ximalaya scramble table size is not 2048")
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise LegacyError("ximalaya-table-invalid", "Ximalaya scramble table checksum mismatch")
    return tuple(struct.unpack("<1024H", raw))


def _ximalaya_header(data: bytes, table, key):
    if len(data) < 1024:
        raise LegacyError("ximalaya-header-truncated", "Ximalaya header is shorter than 1024 bytes")
    output = bytearray(1024)
    for dst in range(1024):
        src = table[dst]
        if src < len(data):
            output[dst] = data[src] ^ key[dst % len(key)]
    return bytes(output)


def decode_ximalaya(data: bytes):
    data = bytes(data)
    candidates = []
    for label, name, checksum, key in (
        ("x2m", "x2m_scramble_table.bin", X2M_TABLE_SHA256, X2M_KEY),
        ("x3m", "x3m_scramble_table.bin", X3M_TABLE_SHA256, X3M_KEY),
    ):
        table = _load_table(name, checksum)
        header = _ximalaya_header(data, table, key)
        extension = sniff_audio(header[:64])
        if extension is not None:
            candidates.append((label, header, extension))
            break
    if not candidates:
        raise LegacyError("ximalaya-format-unknown", "X2M/X3M header descramble did not yield audio")
    label, header, extension = candidates[0]
    return Decoded(header + data[1024:], extension, label + "-scramble", label)


# ---------------------------------------------------------------------------
# QQ Music iOS TM2/TM6
# ---------------------------------------------------------------------------

def decode_tm(data: bytes):
    data = bytes(data)
    if len(data) < 8 or data[:4] != b"QQMU":
        raise LegacyError("tm-header-invalid", "TM header does not start with QQMU")
    output = TM_REPLACEMENT + data[8:]
    extension = sniff_audio(output[:64])
    if extension is None:
        raise LegacyError("tm-output-unknown", "TM header restoration did not yield audio")
    return Decoded(output, extension, "tm-header-restore", "tm")


# ---------------------------------------------------------------------------
# Kugou KGM / KGMA / VPR v3
# ---------------------------------------------------------------------------

_KGM_V3_SLOT_KEYS = {1: bytes((0x6C, 0x2C, 0x2F, 0x27))}


def _kugou_md5(value: bytes):
    digest = hashlib.md5(value).digest()
    output = bytearray(16)
    for i in range(0, 16, 2):
        output[i] = digest[14 - i]
        output[i + 1] = digest[15 - i]
    return bytes(output)


def _xor_collapse(value: int):
    return (value & 0xFF) ^ ((value >> 8) & 0xFF) ^ ((value >> 16) & 0xFF) ^ ((value >> 24) & 0xFF)


def decode_kgm(data: bytes):
    data = bytes(data)
    if len(data) < 0x3C:
        raise LegacyError("kgm-header-invalid", "KGM header is shorter than 60 bytes")
    if data[:16] not in (KGM_HEADER, VPR_HEADER):
        raise LegacyError("kgm-header-invalid", "KGM/VPR magic header does not match")
    audio_offset = int.from_bytes(data[0x10:0x14], "little")
    version = int.from_bytes(data[0x14:0x18], "little")
    slot = int.from_bytes(data[0x18:0x1C], "little")
    if version == 5:
        raise LegacyError(
            "kgm-v5-db-required",
            "KGM/KGG crypto v5 needs a separate Kugou database key route; "
            "this offline decoder does not improvise it",
        )
    if version != 3:
        raise LegacyError("kgm-version-unsupported", "KGM crypto version %d is unsupported" % version)
    slot_key = _KGM_V3_SLOT_KEYS.get(slot)
    if slot_key is None:
        raise LegacyError("kgm-slot-unsupported", "KGM v3 key slot is unsupported")
    if not 0x3C <= audio_offset < len(data):
        raise LegacyError("kgm-offset-invalid", "KGM audio offset is out of bounds")
    slot_box = _kugou_md5(slot_key)
    file_box = _kugou_md5(data[0x2C:0x3C]) + b"\x6b"
    output = bytearray(data[audio_offset:])
    # Upstream seeks to AudioOffset and then resets d.offset to 0; cipher
    # offsets are payload-relative, never absolute file offsets.
    for i in range(len(output)):
        value = output[i]
        value ^= file_box[i % len(file_box)]
        value ^= (value << 4) & 0xFF
        value ^= slot_box[i % len(slot_box)]
        value ^= _xor_collapse(i)
        output[i] = value
    extension = sniff_audio(output[:64])
    if extension is None:
        raise LegacyError("kgm-key-mismatch", "KGM output does not start with a known audio signature")
    return Decoded(bytes(output), extension, "kgm-v3-slot1", "kgm")


# ---------------------------------------------------------------------------
# Public dispatch for non-QMC transforms
# ---------------------------------------------------------------------------

def decode_legacy(data: bytes, kind: str):
    data = bytes(data)
    if len(data) > MAX_CONTAINER_BYTES:
        raise LegacyError("too-large", "container exceeds 256 MiB")
    if kind == "kwm":
        return decode_kwm(data)
    if kind == "xiami":
        return decode_xiami(data)
    if kind == "ximalaya":
        return decode_ximalaya(data)
    if kind == "tm":
        return decode_tm(data)
    if kind == "kgm":
        return decode_kgm(data)
    raise LegacyError("unsupported-format", "container kind is unsupported")
