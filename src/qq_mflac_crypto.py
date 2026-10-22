"""Offline research spike for long-material payload decrypt.

Standard library only.  The caller supplies key bytes derived elsewhere and a
file payload with any musicex 192-byte trailer already removed.  This module
does not parse keys, read a client, or publish material.  Synthetic tests only.

State size follows len(key) (Ghidra this+8), not the RC4 constant 256.
"""

MASK32 = 0xFFFFFFFF
SEGMENT = 5120
HEAD = 128


def decrypt(data: bytes, key: bytes) -> bytes:
    """Return data XORed with the observed long-material keystream."""
    if len(key) <= 300:
        raise ValueError("key must be longer than 300 bytes")

    n = len(key)
    seed = 1
    for v in key:
        if v == 0:
            continue
        candidate = (seed * v) & MASK32
        if candidate == 0 or candidate <= seed:
            break
        seed = candidate

    out = bytearray(data)

    # First 128 bytes: direct key-index XOR.
    for pos in range(min(HEAD, len(out))):
        denom = key[pos % n] * (pos + 1)
        if denom == 0:
            raise ZeroDivisionError("zero denominator at head pos %d" % pos)
        index = int((float(seed) / float(denom)) * 100.0) % n
        out[pos] ^= key[index]

    # Initial KSA state, rebuilt for every 5120-byte segment.
    initial = bytearray(i & 255 for i in range(n))
    j = 0
    for i in range(n):
        j = (initial[i] + key[i % n] + j) % n
        initial[i], initial[j] = initial[j], initial[i]

    pos = HEAD
    while pos < len(out):
        segment = pos // SEGMENT
        denom = key[segment & 511] * (segment + 1)
        if denom == 0:
            raise ZeroDivisionError("zero denominator at segment %d" % segment)
        skip = (int((float(seed) / float(denom)) * 100.0) & 511) + (pos % SEGMENT)

        s = bytearray(initial)
        i = j = 0
        for _ in range(skip):
            i = (i + 1) % n
            j = (s[i] + j) % n
            s[i], s[j] = s[j], s[i]
            s[(s[i] + s[j]) % n]  # advance PRGA, emit nothing

        end = min(len(out), (segment + 1) * SEGMENT)
        for p in range(pos, end):
            i = (i + 1) % n
            j = (s[i] + j) % n
            s[i], s[j] = s[j], s[i]
            out[p] ^= s[(s[i] + s[j]) % n]
        pos = end

    return bytes(out)
