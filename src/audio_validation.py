"""Existing CRC + full-PCM proven FLAC tail repair, isolated from UI/organizer imports."""
import hashlib,json,os,shutil,subprocess
from pathlib import Path
from mutagen.flac import FLAC
def file_hash(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for chunk in iter(lambda:f.read(1<<20),b""):h.update(chunk)
    return h.hexdigest()

def audio_check(ffmpeg, p, payload=False):
    args = [ffmpeg, '-v', 'error', '-i', str(p), '-map', '0:a:0', '-vn', '-sn', '-dn']
    args += ['-c:a', 'copy', '-f', 'hash', '-hash', 'sha256', '-'] if payload else ['-f', 'null', '-']
    r = subprocess.run(args, capture_output=True, timeout=600)
    if r.returncode or r.stderr.strip():
        raise ValueError(r.stderr.decode('utf-8', 'replace')[:300] or 'audio validation failed')
    return r.stdout.decode().strip() if payload else True

def clean_flac_tail(ffmpeg, source, work):
    """Trim ONLY a CRC-validated last-frame tail, with full STREAMINFO PCM MD5 proof."""
    info = FLAC(source).info
    expected = format(info.md5_signature, '032x')
    if not info.md5_signature or info.bits_per_sample not in (8, 16, 24, 32):
        raise ValueError('FLAC tail cannot be repaired without a supported PCM MD5 oracle')
    def pcm_md5(p):
        bits = info.bits_per_sample
        codec = 'pcm_s8' if bits == 8 else 'pcm_s' + str(bits) + 'le'
        r = subprocess.run([ffmpeg, '-v', 'error', '-i', str(p), '-map', '0:a:0', '-c:a', codec,
                            '-f', 'hash', '-hash', 'md5', '-'], capture_output=True, timeout=600)
        return r.stdout.decode().strip().removeprefix('MD5=') if r.returncode == 0 else ''
    if pcm_md5(source) != expected:
        raise ValueError('FLAC PCM MD5 mismatch; no automatic repair')
    ffprobe = str(Path(ffmpeg).with_name('ffprobe.exe' if os.name == 'nt' else 'ffprobe'))
    r = subprocess.run([ffprobe, '-v', 'error', '-select_streams', 'a:0', '-show_packets',
                        '-show_entries', 'packet=pos,size', '-of', 'json', str(source)], capture_output=True, timeout=120)
    packets = json.loads(r.stdout).get('packets', [])
    if r.returncode or not packets:
        raise ValueError('cannot locate FLAC last frame')
    pos = int(packets[-1]['pos'])
    with open(source, 'rb') as f:
        f.seek(pos)
        tail = f.read(1024 * 1024 + 1)
    if len(tail) > 1024 * 1024:
        raise ValueError('unexpected FLAC frame size')
    crc, cuts = 0, []
    for i, byte in enumerate(tail):
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005 if crc & 0x8000 else crc << 1) & 0xffff
        # A final constant/silent FLAC frame can be only ~17 bytes long.
        if crc == 0 and i >= 8 and 0 < len(tail)-i-1 <= 1024:
            cuts.append(i+1)
    if not cuts:
        raise ValueError('no complete last-frame CRC; original retained')
    work.mkdir(parents=True, exist_ok=True)
    cleaned = work / (file_hash(source) + '.flac')
    for length in reversed(cuts):
        shutil.copyfile(source, cleaned)
        with open(cleaned, 'r+b') as f:
            f.truncate(pos + length)
        try:
            audio_check(ffmpeg, cleaned)
            if pcm_md5(cleaned) != expected:
                raise ValueError('repaired PCM does not match STREAMINFO')
            return cleaned, len(tail)-length
        except ValueError:
            continue
    raise ValueError('tail repair failed verification; original retained')

# ---------------------------------------------------------------------------
# Complete Ogg page framing / CRC / sequence / EOS proof
# ---------------------------------------------------------------------------

_OGG_CRC_TABLE = []
for _index in range(256):
    _value = _index << 24
    for _ in range(8):
        _value = ((_value << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if _value & 0x80000000 else (_value << 1) & 0xFFFFFFFF
    _OGG_CRC_TABLE.append(_value)


def ogg_page_crc(page: bytes) -> int:
    """CRC-32 with Ogg's non-reflected polynomial and zero init/xorout."""
    crc = 0
    for byte in page:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _OGG_CRC_TABLE[((crc >> 24) & 0xFF) ^ byte]
    return crc


def validate_ogg_stream(path):
    """Reject truncated or structurally incomplete Ogg streams.

    FFmpeg can decode a truncated Ogg and still exit successfully.  This walks
    every page, verifies framing, the page CRC, per-logical-stream sequence
    numbering, packet continuation lacing, and a final EOS page for every
    stream.  It does not decode the codec payload.
    """
    problem = 'Ogg stream is incomplete'
    try:
        total = os.path.getsize(path)
        if total <= 0:
            raise ValueError('Ogg stream is empty')
        streams = {}
        offset = 0
        with open(path, 'rb') as handle:
            while offset < total:
                header = handle.read(27)
                if len(header) != 27 or header[:4] != b'OggS':
                    raise ValueError('Ogg capture pattern/framing is invalid')
                if header[4] != 0:
                    raise ValueError('Ogg page version is unsupported')
                header_type = header[5]
                # Only 0x01 (continuation), 0x02 (BOS) and 0x04 (EOS) are
                # defined; everything outside 0x07 is reserved.
                if header_type & 0xF8:
                    raise ValueError('Ogg page has a reserved header flag')
                granule = int.from_bytes(header[6:14], 'little')
                serial = int.from_bytes(header[14:18], 'little')
                sequence = int.from_bytes(header[18:22], 'little')
                stored_crc = int.from_bytes(header[22:26], 'little')
                segments = handle.read(header[26])
                if len(segments) != header[26]:
                    raise ValueError('Ogg segment table is truncated')
                payload_length = sum(segments)
                payload = handle.read(payload_length)
                if len(payload) != payload_length:
                    raise ValueError('Ogg page payload is truncated')
                page = bytearray(header)
                page += segments
                page += payload
                page[22:26] = b'\x00\x00\x00\x00'
                if ogg_page_crc(page) != stored_crc:
                    raise ValueError('Ogg page CRC mismatch')
                is_bos = bool(header_type & 0x02)
                previous = streams.get(serial)
                if previous is None:
                    if not is_bos:
                        raise ValueError('Ogg logical stream does not start with a BOS page')
                    if sequence != 0:
                        raise ValueError('Ogg logical stream does not start at sequence 0')
                else:
                    if previous['eos']:
                        raise ValueError('Ogg page appears after the logical stream EOS')
                    if is_bos:
                        raise ValueError('Ogg logical stream repeats its BOS page')
                    if sequence != previous['sequence'] + 1:
                        raise ValueError('Ogg page sequence is discontinuous')
                continued = bool(header_type & 0x01)
                if continued:
                    if previous is None or not previous['last_lacing'] == 255:
                        raise ValueError('Ogg page continues a missing packet')
                elif previous is not None and previous['last_lacing'] == 255:
                    raise ValueError('Ogg packet is not continued on the next page')
                last_lacing = segments[-1] if segments else 0
                if header_type & 0x04 and last_lacing == 255:
                    raise ValueError('Ogg EOS page ends with an unfinished packet')
                streams[serial] = {
                    'sequence': sequence,
                    'last_lacing': last_lacing,
                    'eos': bool(header_type & 0x04),
                    'granule': granule,
                }
                offset += len(page)
        if not streams:
            raise ValueError('Ogg stream has no pages')
        for state in streams.values():
            if not state['eos']:
                raise ValueError('Ogg logical stream has no EOS page')
            if state['granule'] == 0xFFFFFFFFFFFFFFFF:
                raise ValueError('Ogg EOS page has no final granule position')
        return True
    except (OSError, ValueError) as exc:
        raise ValueError('%s: %s' % (problem, exc)) from None
