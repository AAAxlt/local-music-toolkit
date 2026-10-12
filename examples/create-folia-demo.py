#!/usr/bin/env python3
"""Create two original test tones and an isolated config; never play them."""
import argparse
import json
import math
import struct
import wave
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]


def write_tone(path, frequency):
    rate, seconds = 44100, 2
    samples = bytearray()
    for i in range(rate * seconds):
        # Low amplitude, with 50 ms fades to avoid abrupt edges.
        fade = min(1.0, i / (rate * .05), (rate * seconds - 1 - i) / (rate * .05))
        value = round(1600 * fade * math.sin(2 * math.pi * frequency * i / rate))
        samples.extend(struct.pack('<h', value))
    with wave.open(str(path), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(samples)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', type=Path, default=PROJECT / '.work/folia-demo')
    args = parser.parse_args()
    root = args.out_dir.resolve()
    # Refuse any existing directory so demonstrations never overwrite user data.
    if root.exists():
        parser.error('Output already exists; choose a new --out-dir.')
    downloads = root / 'downloads'
    downloads.mkdir(parents=True)
    for hz in (440, 660):
        write_tone(downloads / f'Demo - Tone {hz}Hz.wav', hz)
    cfg = json.loads((PROJECT / 'config.example.json').read_text('utf-8-sig'))
    cfg['ncmDecoder'] = (PROJECT / 'src/ncm-decrypt.mjs').as_posix()
    # Everything else remains relative to this isolated config.
    (root / 'config.json').write_text(json.dumps(cfg, indent=2) + '\n', 'utf-8')
    print('Demo directory:', root)
    print('Created 2 original WAV test tones; no audio played, no real library changed.')
    print('Next: follow docs/folia-example.md to import and create a playlist.')


if __name__ == '__main__':
    main()
