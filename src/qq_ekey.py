"""Standalone parser for local QQ Music ekey material.

Pure standard library.  Derived from the supplied stripped Ghidra pseudocode
for ``CommonFunction.dll`` (pinned 21.11 x86):

* ``FUN_10005750`` at 0x5750 -- SetKey / EncV2 peel / final material parse
* ``FUN_1000acc5`` at 0xacc5 -- chained QQ-TEA block unpack
* ``FUN_1000a570`` at 0xa570 -- static wrapping-word decoder

The leaf at 0xa9aa is now statically proved as 16-round big-endian QQ-TEA.  The
SetKey tan bytes are statically recovered as
``abs(tan(i * 0.1 + 106)) * 100`` -> ``695646382b20150b`` and are the default.
The confirmed material layout is ``full_plain[pad+3:-7]`` with exactly seven
zero trailer bytes, where ``pad = full_plain[0] & 7``.  EncV2 wrapping uses the
same bounded unpack helper for each layer; it is implemented but has only
synthetic coverage because the five real samples were not EncV2.

Raw material is returned as bytes and never printed here.  Error messages carry
only structural descriptions, never raw input/key/material bytes.
"""

from __future__ import annotations

import base64
import struct

__all__ = [
    "EkeyError",
    "MAX_EKEY_BYTES",
    "MAX_MATERIAL_BYTES",
    "ENC_V2_PREFIX",
    "DEFAULT_TAN_KEY",
    "STATIC_KEY_1",
    "STATIC_KEY_2",
    "decode_wrapping_words",
    "derive_encv2_keys",
    "interleave_tan_key",
    "qqtea_decrypt_block",
    "qqtea_encrypt_block",
    "qqtea_chained_decrypt",
    "unpack_bounded",
    "parse_material",
    "parse_ekey",
]

MAX_EKEY_BYTES = 16 * 1024
MAX_MATERIAL_BYTES = 8 * 1024

# 18 bytes == the source's 0x12 prefix strip.  The source literal is
# "QQMusic EncV2,Key:" (17 printable bytes plus the terminating NUL).
ENC_V2_PREFIX = b"QQMusic EncV2,Key:"
assert len(ENC_V2_PREFIX) == 0x12

DEFAULT_TAN_KEY = bytes.fromhex("695646382b20150b")
TEA_ROUNDS = 16
TEA_WORD_ENDIAN = "big"

_TEA_DELTA = 0x9E3779B9
_TEA_MASK = 0xFFFFFFFF

# Seven uint32 words as laid out on the x86 stack at each a570 call: three
# explicit local_2c words followed by local_20 bytes 0..15.  a570 reads only
# indices 1..5; the seventh word is listed for fidelity.
_ENC_V2_WORDS_1 = (
    0xC2D276E4, 0x08CF43F9, 0x98E44ED7,
    0x82F32FAE, 0xE7F65CC7, 0xEAFB50BA, 0xCA1D350D,
)
_ENC_V2_WORDS_2 = (
    0xEC19BB35, 0xCD3A911F, 0xA9EC7BAC,
    0xC93D981D, 0xDD78E513, 0xB835E156, 0x45F5C0A9,
)


class EkeyError(ValueError):
    """Structural ekey failure; never contains raw key/material bytes."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _require_bytes(value, label):
    if isinstance(value, str):
        try:
            return value.encode("ascii")
        except UnicodeError:
            raise EkeyError("input-not-ascii", "%s is not ASCII" % label) from None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    raise EkeyError("input-type", "%s must be bytes or ASCII text" % label)


def _strict_base64(value, label):
    """Canonical standard base64 with strict size and padding bounds."""
    raw = _require_bytes(value, label)
    if len(raw) > MAX_EKEY_BYTES:
        raise EkeyError("input-too-large", "%s exceeds the ekey size cap" % label)
    raw = raw.strip(b" \t\r\n")
    if not raw:
        raise EkeyError("base64-empty", "%s is empty" % label)
    if any(not (48 <= c <= 57 or 65 <= c <= 90 or 97 <= c <= 122 or c in (43, 47, 61))
           for c in raw):
        raise EkeyError("base64-invalid", "%s has a non-base64 byte" % label)
    body = raw.rstrip(b"=")
    pad = len(raw) - len(body)
    if pad > 2 or b"=" in body:
        raise EkeyError("base64-invalid", "%s has misplaced padding" % label)
    if len(body) % 4 == 1:
        raise EkeyError("base64-invalid", "%s has an impossible length" % label)
    if pad and (len(raw) % 4 != 0 or pad != (4 - len(body) % 4) % 4):
        raise EkeyError("base64-invalid", "%s has incorrect padding" % label)
    padded = body + b"=" * ((4 - len(body) % 4) % 4)
    try:
        decoded = base64.b64decode(padded, validate=True)
    except (ValueError, TypeError):
        raise EkeyError("base64-invalid", "%s cannot be decoded" % label) from None
    if base64.b64encode(decoded).rstrip(b"=") != body:
        raise EkeyError("base64-invalid", "%s has non-canonical trailing bits" % label)
    if len(decoded) > MAX_MATERIAL_BYTES:
        raise EkeyError("decoded-too-large", "%s decodes beyond the size cap" % label)
    return decoded


def decode_wrapping_words(words, count=7):
    """Transliterate FUN_1000a570 for the SetKey call shape.

    XOR every selected word with word 0, skip one based on
    ``word0 % 10 % (count - 3)``, and serialise selected words little-endian.
    For ``count == 7`` this emits exactly four words (16 bytes).
    """
    if not isinstance(words, (tuple, list)):
        raise EkeyError("words-type", "wrapping words must be a sequence")
    if not 4 <= count <= len(words):
        raise EkeyError("words-count", "wrapping-word count is out of range")
    if any(not isinstance(w, int) or not 0 <= w <= _TEA_MASK for w in words):
        raise EkeyError("words-value", "wrapping word is not a uint32")
    first = words[0]
    span = count - 3
    if span <= 0:
        raise EkeyError("words-count", "wrapping-word span is invalid")
    skip = (first % 10) % span
    out = bytearray()
    for index in range(1, count - 1):
        if index - 1 == skip:
            continue
        out += struct.pack("<I", (words[index] ^ first) & _TEA_MASK)
    if len(out) % 4:
        raise EkeyError("words-shape", "wrapping-word output is not word aligned")
    return bytes(out)


def derive_encv2_keys():
    """Return the two 16-byte static EncV2 keys decoded by 0xa570."""
    return (
        decode_wrapping_words(_ENC_V2_WORDS_1, 7),
        decode_wrapping_words(_ENC_V2_WORDS_2, 7),
    )


STATIC_KEY_1, STATIC_KEY_2 = derive_encv2_keys()


def _validate_tea_args(key, rounds, word_endian):
    key = _require_bytes(key, "TEA key")
    if len(key) != 16:
        raise EkeyError("tea-key-length", "TEA key is not 16 bytes")
    if not isinstance(rounds, int) or not 1 <= rounds <= 64:
        raise EkeyError("tea-rounds", "TEA round count is out of range")
    if word_endian not in ("big", "little"):
        raise EkeyError("tea-endian", "TEA word endian is unsupported")
    return key


def qqtea_decrypt_block(block, key, rounds=TEA_ROUNDS,
                        word_endian=TEA_WORD_ENDIAN):
    """Decrypt one 8-byte QQ-TEA block (statically proved 16-round big-endian)."""
    key = _validate_tea_args(key, rounds, word_endian)
    block = _require_bytes(block, "TEA block")
    if len(block) != 8:
        raise EkeyError("tea-block-length", "TEA block is not 8 bytes")
    v0 = int.from_bytes(block[:4], word_endian)
    v1 = int.from_bytes(block[4:], word_endian)
    k = [int.from_bytes(key[i:i + 4], word_endian) for i in range(0, 16, 4)]
    total = (_TEA_DELTA * rounds) & _TEA_MASK
    for _ in range(rounds):
        v1 = (v1 - ((((v0 << 4) + k[2]) & _TEA_MASK) ^ ((v0 + total) & _TEA_MASK)
                    ^ (((v0 >> 5) + k[3]) & _TEA_MASK))) & _TEA_MASK
        v0 = (v0 - ((((v1 << 4) + k[0]) & _TEA_MASK) ^ ((v1 + total) & _TEA_MASK)
                    ^ (((v1 >> 5) + k[1]) & _TEA_MASK))) & _TEA_MASK
        total = (total - _TEA_DELTA) & _TEA_MASK
    return v0.to_bytes(4, word_endian) + v1.to_bytes(4, word_endian)


def qqtea_encrypt_block(block, key, rounds=TEA_ROUNDS,
                        word_endian=TEA_WORD_ENDIAN):
    """Inverse of :func:`qqtea_decrypt_block` (used by synthetic tests)."""
    key = _validate_tea_args(key, rounds, word_endian)
    block = _require_bytes(block, "TEA block")
    if len(block) != 8:
        raise EkeyError("tea-block-length", "TEA block is not 8 bytes")
    v0 = int.from_bytes(block[:4], word_endian)
    v1 = int.from_bytes(block[4:], word_endian)
    k = [int.from_bytes(key[i:i + 4], word_endian) for i in range(0, 16, 4)]
    total = 0
    for _ in range(rounds):
        total = (total + _TEA_DELTA) & _TEA_MASK
        v0 = (v0 + ((((v1 << 4) + k[0]) & _TEA_MASK) ^ ((v1 + total) & _TEA_MASK)
                    ^ (((v1 >> 5) + k[1]) & _TEA_MASK))) & _TEA_MASK
        v1 = (v1 + ((((v0 << 4) + k[2]) & _TEA_MASK) ^ ((v0 + total) & _TEA_MASK)
                    ^ (((v0 >> 5) + k[3]) & _TEA_MASK))) & _TEA_MASK
    return v0.to_bytes(4, word_endian) + v1.to_bytes(4, word_endian)


def qqtea_chained_decrypt(data, key, rounds=TEA_ROUNDS,
                          word_endian=TEA_WORD_ENDIAN):
    """Unpack the chained QQ-TEA stream observed at 0xacc5.

    q0 = D(C0); for i > 0, q_i = D(q_{i-1} XOR C_i); output block i is
    q_i XOR C_{i-1}, with C_-1 eight zero bytes.  Input is a positive multiple
    of eight bytes and at least two blocks long.
    """
    data = _require_bytes(data, "TEA data")
    key = _validate_tea_args(key, rounds, word_endian)
    if len(data) < 16 or len(data) % 8:
        raise EkeyError("tea-data-length", "chained TEA data is not block aligned")
    out = bytearray()
    previous_cipher = b"\x00" * 8
    q = qqtea_decrypt_block(data[:8], key, rounds, word_endian)
    for offset in range(0, len(data), 8):
        cipher = data[offset:offset + 8]
        if offset:
            mixed = bytes(a ^ b for a, b in zip(q, cipher))
            q = qqtea_decrypt_block(mixed, key, rounds, word_endian)
        out += bytes(a ^ b for a, b in zip(q, previous_cipher))
        previous_cipher = cipher
    return bytes(out)


def unpack_bounded(data, key, rounds=TEA_ROUNDS, word_endian=TEA_WORD_ENDIAN):
    """Chained-unpack then apply the confirmed SetKey layout.

    ``pad = full_plain[0] & 7``; output is ``full_plain[pad+3:-7]`` and the
    final seven bytes must all be zero.  This is used for the final material
    payload and for each EncV2 layer.
    """
    plain = qqtea_chained_decrypt(data, key, rounds, word_endian)
    if len(plain) < 8:
        raise EkeyError("padding-shape", "decrypted payload is too short")
    pad = plain[0] & 7
    start = pad + 3
    end = len(plain) - 7
    if start > end:
        raise EkeyError("padding-shape", "padding/header exceeds payload")
    if plain[-7:] != b"\x00" * 7:
        raise EkeyError("padding-invalid", "decrypted trailer is not seven zero bytes")
    return plain[start:end]


def interleave_tan_key(tan_key, simple_key):
    """Interleave tan bytes on even positions and simple key on odd positions."""
    tan_key = _require_bytes(tan_key, "tan key")
    simple_key = _require_bytes(simple_key, "simple key")
    if len(tan_key) != 8 or len(simple_key) != 8:
        raise EkeyError("key-length", "tan and simple keys must be 8 bytes each")
    out = bytearray(16)
    for index in range(16):
        source = tan_key if index % 2 == 0 else simple_key
        out[index] = source[index // 2]
    return bytes(out)


def _peel_encv2(decoded):
    """Peel the synthetic-only EncV2 layers with bounded unpack at each stage."""
    if not decoded.startswith(ENC_V2_PREFIX):
        raise EkeyError("encv2-prefix", "EncV2 material lacks its prefix")
    body = _strict_base64(decoded[len(ENC_V2_PREFIX):], "EncV2 payload")
    if len(body) < 16 or len(body) % 8:
        raise EkeyError("encv2-shape", "EncV2 payload is not block aligned")
    first = unpack_bounded(body, STATIC_KEY_1)
    second = unpack_bounded(first, STATIC_KEY_2)
    return _strict_base64(second, "EncV2 inner material")


def parse_material(material, tan_key=DEFAULT_TAN_KEY, rounds=TEA_ROUNDS,
                   word_endian=TEA_WORD_ENDIAN):
    """Parse already-unwrapped ekey material into ``simple_key + body``."""
    material = _require_bytes(material, "material")
    tan_key = _require_bytes(tan_key, "tan key")
    if len(tan_key) != 8:
        raise EkeyError("tan-key-length", "tan key is not 8 bytes")
    if len(material) <= 8:
        raise EkeyError("material-too-short", "material has no payload")
    if len(material) > MAX_MATERIAL_BYTES:
        raise EkeyError("material-too-large", "material exceeds the size cap")
    simple_key = material[:8]
    payload = material[8:]
    if len(payload) < 16 or len(payload) % 8:
        raise EkeyError("payload-shape", "payload is not block aligned")
    key = interleave_tan_key(tan_key, simple_key)
    body = unpack_bounded(payload, key, rounds, word_endian)
    return simple_key + body


def parse_ekey(ekey, tan_key=DEFAULT_TAN_KEY, rounds=TEA_ROUNDS,
               word_endian=TEA_WORD_ENDIAN):
    """Parse one encoded ekey and return material bytes (never prints them)."""
    if len(_require_bytes(ekey, "ekey")) > MAX_EKEY_BYTES:
        raise EkeyError("input-too-large", "ekey exceeds the size cap")
    decoded = _strict_base64(ekey, "ekey")
    if decoded.startswith(ENC_V2_PREFIX):
        decoded = _peel_encv2(decoded)
    return parse_material(decoded, tan_key, rounds, word_endian)
