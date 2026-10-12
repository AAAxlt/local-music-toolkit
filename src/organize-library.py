#!/usr/bin/env python3
"""Build a recoverable, deduplicated library. Input audio is never modified.

Only metadata/lyrics/artwork are requested online; no audio downloads or QQ injection.
"""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request

from project_paths import load_settings
import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, TIT2, TPE1, TALB, USLT
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from PIL import Image

AUDIO = {'.flac', '.ogg', '.mp3', '.m4a', '.wav', '.opus'}
STAMP = re.compile(r'\[\d{1,3}:\d{2}(?:[.:]\d+)?\]')

def digest(data):
    return hashlib.sha256(data).hexdigest()

def file_hash(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def norm(s):
    # Preserve Unicode letters AND all version words. Never erase Live/现场.
    return ''.join(c for c in unicodedata.normalize('NFKC', s or '').casefold() if c.isalnum())

def versions(title, album=''):
    t = unicodedata.normalize('NFKC', title or '').casefold()
    markers = []
    for word, pattern in [('live', r'\blive\b|现场|演唱会|concert'),
                          ('acoustic', r'\bacoustic\b|不插电'),
                          ('remaster', r'\bremaster\w*\b|重制'),
                          ('old', r'旧版'), ('cover', r'\bcover\b|翻唱'),
                          ('instrumental', r'\binstrumental\b|伴奏|纯音乐'),
                          ('remix', r'\bremix\b|混音'), ('demo', r'\bdemo\b')]:
        if re.search(pattern, t):
            markers.append(word)
    if re.search(r'\blive\b|现场|演唱会|concert', (album or '').casefold()):
        markers.append('live')
    return tuple(sorted(set(markers)))

def tag_text(tags, *keys):
    for k in keys:
        try:
            v = tags.get(k) if tags else None
        except (ValueError, KeyError):
            continue
        if v:
            if hasattr(v, 'text'):
                v = v.text
            if isinstance(v, (list, tuple)):
                v = '/'.join(str(x) for x in v)
            return str(v).strip()
    return ''

def read_track(p):
    a = mutagen.File(p)
    if a is None or not getattr(a, 'info', None):
        raise ValueError('unsupported or invalid audio container')
    tags = a.tags or {}
    stem = re.sub(r'\s*\(\d+\)$', '', p.stem)
    parts = stem.split(' - ', 1)
    title = tag_text(tags, 'title', 'TIT2', '\xa9nam') or parts[-1]
    artist = tag_text(tags, 'artist', 'TPE1', '\xa9ART') or (parts[0] if len(parts) == 2 else '')
    album = tag_text(tags, 'album', 'TALB', '\xa9alb')
    cover = None
    if isinstance(a, FLAC) and a.pictures:
        cover = next((x.data for x in a.pictures if x.type == 3), a.pictures[0].data)
    elif isinstance(a, MP3) and a.tags:
        pics = a.tags.getall('APIC')
        if pics:
            cover = next((x.data for x in pics if x.type == 3), pics[0].data)
    elif isinstance(a, MP4) and tags.get('covr'):
        cover = bytes(tags['covr'][0])
    elif tags.get('metadata_block_picture'):
        try:
            cover = Picture(base64.b64decode(tags['metadata_block_picture'][0])).data
        except Exception:
            pass
    lyrics = tag_text(tags, 'lyrics', 'syncedlyrics', 'unsyncedlyrics', '\xa9lyr')
    if isinstance(a, MP3) and a.tags:
        frames = a.tags.getall('USLT')
        if frames:
            lyrics = frames[0].text
    return {'path': p, 'title': title, 'artist': artist, 'album': album,
            'duration': float(a.info.length), 'size': p.stat().st_size,
            'version': versions(title + ' ' + stem, album), 'cover': cover, 'lyrics': lyrics}

def group_tracks(rows, title_aliases=()):
    buckets = {}
    for r in rows:
        identity_title = r['title']
        # Explicit user-confirmed aliases only; do not strip translations globally.
        for alias in title_aliases:
            if (norm(r['artist']) == norm(alias['artist'])
                    and norm(r['album']) == norm(alias['album'])
                    and r['title'] in (alias['title'], alias['canonicalTitle'])
                    and abs(r['duration'] - alias['duration']) <= 2):
                identity_title = alias['canonicalTitle']
                break
        key = (norm(identity_title), norm(r['artist']), r['version'])
        # Distinct live albums are different performances even with equal titles.
        if 'live' in r['version']:
            key += (norm(r['album']),)
        if not key[0] or not key[1]:
            key += (str(r['path']),)
        buckets.setdefault(key, []).append(r)
    groups = []
    for key in sorted(buckets, key=str):
        clusters = []
        for row in sorted(buckets[key], key=lambda x: x['duration']):
            hit = next((g for g in clusters if abs(row['duration'] - g[0]['duration']) <= 2), None)
            if hit is None:
                clusters.append([row])
            else:
                hit.append(row)
        groups.extend(clusters)
    return groups

def choose_candidate(track, candidates, provider):
    valid = []
    for c in candidates:
        if provider == 'netease':
            name, artist = c.get('name', ''), '/'.join(a['name'] for a in c.get('artists', []))
            album, duration = c.get('album', {}).get('name', ''), c.get('duration', 0) / 1000
        else:
            name, artist = c.get('trackName', ''), c.get('artistName', '')
            album, duration = c.get('albumName', ''), c.get('duration', 0)
        if not track['artist'] or norm(name) != norm(track['title']) or norm(artist) != norm(track['artist']):
            continue
        if versions(name, album) != track.get('version', versions(track['title'], track['album'])):
            continue
        if duration and abs(duration - track['duration']) <= 2:
            valid.append((c, album, abs(duration - track['duration'])))
    by_album = [x for x in valid if track['album'] and norm(x[1]) == norm(track['album'])]
    pool = by_album or valid
    if len(pool) == 1:
        return pool[0][0]
    # Duplicate catalogue entries are accepted only if the best duration is unique.
    pool.sort(key=lambda x: x[2])
    if pool and pool[0][2] <= .3 and (len(pool) == 1 or pool[1][2] - pool[0][2] >= .5):
        return pool[0][0]
    return None

def image_jpeg(data):
    if not data or len(data) > 12 * 1024 * 1024:
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            if img.width < 32 or img.height < 32:
                return None
            img = img.convert('RGB')
            img.thumbnail((1200, 1200))
            buf = io.BytesIO()
            # Canonical JPEG drops malformed EXIF rather than rejecting valid audio.
            img.save(buf, 'JPEG', quality=92)
            return buf.getvalue()
    except Exception:
        return None

class Network:
    def __init__(self, cache, enabled):
        self.cache, self.enabled = cache, enabled
        self.cache.mkdir(parents=True, exist_ok=True)
        # A stale local proxy is not allowed to break a metadata-only operation.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.errors = []
        self.last = 0

    def get(self, url, post=None, binary=False):
        key = digest((url + str(post)).encode())
        cached = self.cache / (key + ('.bin' if binary else '.json'))
        if cached.exists() and time.time() - cached.stat().st_mtime < 7 * 86400:
            return cached.read_bytes() if binary else json.loads(cached.read_text('utf-8'))
        if not self.enabled:
            return None
        for attempt in range(3):
            try:
                time.sleep(max(0, .35 - (time.monotonic() - self.last)))
                data = urllib.parse.urlencode(post).encode() if post else None
                req = urllib.request.Request(url, data=data, headers={'User-Agent': 'Mozilla/5.0 JuyinLocal/2', 'Referer': 'https://music.163.com/'})
                self.last = time.monotonic()
                with self.opener.open(req, timeout=12) as res:
                    body = res.read(12 * 1024 * 1024 + 1)
                if len(body) > 12 * 1024 * 1024:
                    raise ValueError('response exceeds size limit')
                obj = body if binary else json.loads(body)
                # Do not cache a temporary provider failure as a permanent no-match.
                if isinstance(obj, dict) and obj.get('code', 200) != 200:
                    raise ValueError('provider code ' + str(obj.get('code')))
                atomic_bytes(cached, body)
                return obj
            except Exception as e:
                if attempt == 2:
                    self.errors.append({'url': url, 'error': str(e)})
                else:
                    time.sleep(attempt + 1)
        return None

def atomic_bytes(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + '.partial-' + str(os.getpid()))
    try:
        with open(tmp, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            tmp.unlink()

def json_write(p, obj):
    atomic_bytes(p, json.dumps(obj, ensure_ascii=False, indent=2).encode('utf-8'))

def valid_lyrics(text):
    return bool(text and len(text.strip()) > 8 and not text.lstrip().startswith(('<html', '<!DOCTYPE')))

def local_assets(group, source_map):
    covers, lyrics = [], []
    for row in group:
        if row['cover']:
            covers.append(row['cover'])
        if valid_lyrics(row['lyrics']):
            lyrics.append(row['lyrics'])
        paths = [row['path']] + [Path(x) for x in source_map.get(row['path'].name, [])]
        for p in paths:
            for suffix in ('.cover.jpg', '.jpg', '.png', '.jpeg'):
                f = p.with_suffix(suffix)
                if f.exists():
                    covers.append(f.read_bytes())
            for f in (p.with_suffix('.lrc'), p.with_name(p.name + '.lrc')):
                if f.exists():
                    b = f.read_bytes()
                    text = None
                    for enc in ('utf-8-sig', 'utf-16', 'gb18030'):
                        try:
                            text = b.decode(enc)
                            break
                        except UnicodeError:
                            pass
                    if valid_lyrics(text):
                        lyrics.append(text)
    clean = [x for x in (image_jpeg(b) for b in covers) if x]
    cover = max(clean, key=len, default=None)
    lyric = max(lyrics, key=lambda t: (len(STAMP.findall(t)), len(t)), default=None)
    return cover, lyric

def online_assets(net, row, need_cover, need_lyrics):
    cover = lyric = None
    provenance = {}
    if not row['artist']:
        return None, None, {'reason': 'missing artist; no automatic match'}
    result = net.get('https://music.163.com/api/search/get?' + urllib.parse.urlencode({'s': row['artist'] + ' ' + row['title'], 'type': 1, 'limit': 30, 'offset': 0}))
    hit = choose_candidate(row, (result or {}).get('result', {}).get('songs', []), 'netease')
    if hit:
        sid = hit['id']
        provenance['neteaseId'] = sid
        if need_cover:
            detail = net.get('https://music.163.com/api/song/detail/?' + urllib.parse.urlencode({'id': sid, 'ids': json.dumps([sid])}))
            songs = (detail or {}).get('songs', [])
            url = songs[0].get('album', {}).get('picUrl') if songs else None
            if url:
                parsed = urllib.parse.urlparse(url)
                if parsed.hostname and (parsed.hostname.endswith('.music.126.net') or parsed.hostname.endswith('.music.163.com')):
                    url = urllib.parse.urlunparse(parsed._replace(scheme='https'))
                    cover = image_jpeg(net.get(url, binary=True))
                    provenance['coverUrl'] = url
        if need_lyrics:
            data = net.get('https://music.163.com/api/song/lyric?' + urllib.parse.urlencode({'id': sid, 'lv': -1, 'kv': -1, 'tv': -1}))
            if (data or {}).get('nolyric') is True:
                provenance['instrumental'] = True
            text = (data or {}).get('lrc', {}).get('lyric')
            if valid_lyrics(text):
                lyric = text
                provenance['lyricsProvider'] = 'netease'
    # Artwork belongs to an album. An exact artist+album match can recover a
    # live album's own cover even when no reliable matching lyric track exists.
    if need_cover and not cover and row['album']:
        url = 'https://music.163.com/api/search/get?' + urllib.parse.urlencode({'s': row['artist'] + ' ' + row['album'], 'type': 10, 'limit': 30, 'offset': 0})
        albums = (net.get(url) or {}).get('result', {}).get('albums', [])
        exact = [a for a in albums if norm(a.get('name', '')) == norm(row['album']) and norm(a.get('artist', {}).get('name', '')) == norm(row['artist'])]
        urls = {a.get('picUrl') for a in exact if a.get('picUrl')}
        if len(urls) == 1:
            url = urls.pop()
            parsed = urllib.parse.urlparse(url)
            if parsed.hostname and parsed.hostname.endswith('.music.126.net'):
                url = urllib.parse.urlunparse(parsed._replace(scheme='https'))
                cover = image_jpeg(net.get(url, binary=True))
                provenance.update(coverUrl=url, coverMatch='exact artist+album')
    if need_lyrics and not lyric:
        url = 'https://lrclib.net/api/search?' + urllib.parse.urlencode({'track_name': row['title'], 'artist_name': row['artist']})
        data = net.get(url)
        hit = choose_candidate(row, data if isinstance(data, list) else [], 'lrclib')
        if hit:
            if hit.get('instrumental') is True:
                provenance['instrumental'] = True
            text = hit.get('syncedLyrics') or hit.get('plainLyrics')
            if valid_lyrics(text):
                lyric = text
                provenance.update(lyricsProvider='lrclib', lrclibId=hit['id'])
    return cover, lyric, provenance

def embed(p, row, cover, lyrics):
    a = mutagen.File(p)
    if a.tags is None:
        a.add_tags()
    if isinstance(a, MP3):
        a.tags.add(TIT2(encoding=3, text=row['title']))
        a.tags.add(TPE1(encoding=3, text=row['artist']))
        a.tags.add(TALB(encoding=3, text=row['album']))
        if cover:
            a.tags.delall('APIC')
            a.tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=cover))
        if lyrics:
            a.tags.delall('USLT')
            a.tags.add(USLT(encoding=3, lang='und', desc='', text=lyrics))
        a.save(v2_version=3)
        return
    if isinstance(a, MP4):
        a['\xa9nam'], a['\xa9ART'], a['\xa9alb'] = [row['title']], [row['artist']], [row['album']]
        if cover:
            a['covr'] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
        if lyrics:
            a['\xa9lyr'] = [lyrics]
    elif not p.suffix.lower() == '.wav':
        a['title'], a['artist'], a['album'] = [row['title']], [row['artist']], [row['album']]
        if cover:
            pic = Picture()
            pic.type, pic.mime, pic.desc, pic.data = 3, 'image/jpeg', 'Cover', cover
            with Image.open(io.BytesIO(cover)) as img:
                pic.width, pic.height = img.size
            pic.depth = 24
            if isinstance(a, FLAC):
                a.clear_pictures()
                a.add_picture(pic)
            else:
                a['metadata_block_picture'] = [base64.b64encode(pic.write()).decode('ascii')]
        if lyrics:
            a['lyrics'] = [lyrics]
    a.save()

def audio_check(ffmpeg, p, payload=False):
    args = [ffmpeg, '-v', 'error', '-i', str(p), '-map', '0:a:0', '-vn', '-sn', '-dn']
    args += ['-c:a', 'copy', '-f', 'hash', '-hash', 'sha256', '-'] if payload else ['-f', 'null', '-']
    r = subprocess.run(args, capture_output=True, timeout=600)
    if r.returncode or r.stderr.strip():
        raise ValueError(r.stderr.decode('utf-8', 'replace')[:300] or 'audio validation failed')
    return r.stdout.decode().strip() if payload else True

def pcm_digest(ffmpeg, p):
    """SHA-256 of the decoded PCM, used to prove a container edit left audio untouched."""
    r = subprocess.run([ffmpeg, '-v', 'error', '-i', str(p), '-map', '0:a:0', '-f', 's16le', '-'],
                       capture_output=True, timeout=600)
    if r.returncode:
        raise ValueError('PCM decode failed during verification')
    return digest(r.stdout)

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

def clean_ogg_tail(ffmpeg, source, work):
    """Drop trailing non-Ogg data (e.g. a QQ Music 'musicex' block) only when
    the pages parse cleanly and the decoded audio is provably unchanged."""
    data = source.read_bytes()
    offset = 0
    while offset < len(data):
        if data[offset:offset + 4] != b'OggS':
            break
        segments = data[offset + 26]
        offset += 27 + segments + sum(data[offset + 27:offset + 27 + segments])
    if offset == 0 or offset == len(data):
        return source, 0
    trimmed = len(data) - offset
    if trimmed > 1024 * 1024:
        raise ValueError('unexpected Ogg trailer size')
    work.mkdir(parents=True, exist_ok=True)
    cleaned = work / (file_hash(source) + '.ogg')
    if cleaned.exists() and file_hash(cleaned) == digest(data[:offset]):
        return cleaned, trimmed
    cleaned.write_bytes(data[:offset])
    audio_check(ffmpeg, cleaned)
    if pcm_digest(ffmpeg, cleaned) != pcm_digest(ffmpeg, source):
        raise ValueError('trimmed Ogg PCM does not match original')
    return cleaned, trimmed

def validated_source(ffmpeg, candidate, cache, tool_id):
    original = candidate['path']
    candidate['sha256'] = file_hash(original)
    # A QQ Music OGG can carry a container trailer that makes tag rewriting
    # fail. Normalise it before consulting the cache, since an existing entry
    # may still point at the un-trimmed file.
    source = original
    if original.suffix.lower() == '.ogg':
        ogg, cut = clean_ogg_tail(ffmpeg, original, cache / 'clean-ogg')
        if cut:
            source = ogg
    source_sha = file_hash(source)
    check = cache / 'validation' / (digest((source_sha + tool_id).encode()) + '.json')
    if check.exists():
        old = json.loads(check.read_text('utf-8'))
        p = Path(old['path'])
        if p.exists() and file_hash(p) == old['sha256']:
            return p, old.get('trimmedBytes', 0)
    try:
        audio_check(ffmpeg, source)
        result, trimmed = source, 0
    except Exception:
        if source.suffix.lower() != '.flac':
            raise
        result, trimmed = clean_flac_tail(ffmpeg, source, cache / 'clean-flac')
    if source is not original:
        trimmed = len(original.read_bytes()) - len(source.read_bytes())
    json_write(check, {'path': str(result), 'sha256': file_hash(result), 'trimmedBytes': trimmed, 'sourceSha256': candidate['sha256']})
    return result, trimmed

def safe_name(row):
    s = (row['artist'] + ' - ' if row['artist'] else '') + row['title']
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', s).strip(' .')[:160]
    return s or 'Unknown'

def process_alive(pid):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # access denied means possibly active
        try:
            code = wintypes.DWORD()
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259
        finally:
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False

def acquire_lock(lock):
    for attempt in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return
        except FileExistsError:
            pid = int(lock.read_text('ascii'))
            if process_alive(pid) or attempt:
                raise RuntimeError('another organizer is running; no files changed')
            # Remove only a proven dead run's exact lock file, never terminate a process.
            lock.unlink()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', required=True)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--offline', action='store_true')
    ap.add_argument('--online', action='store_true', help='Opt in to metadata-only network queries')
    args = ap.parse_args()
    cfg = load_settings(args.config)
    src, out = Path(cfg['libraryDir']).resolve(), Path(cfg['organizedDir']).resolve()
    if src == out and not (cfg.get('allowManagedInPlace') is True
                           and (out / '.library-manifest.json').is_file()):
        raise ValueError('organizedDir must differ from libraryDir; originals are protected')
    rows, errors = [], []
    # Input top level is the existing unified library; do not recursively scan our output/backups.
    input_files = list(src.iterdir())
    for extra in cfg.get('additionalPlainDirs', []):
        # Read standard audio only; never trigger decryption or modify downloads.
        input_files.extend(Path(extra).glob('*'))
    for p in sorted(set(input_files)):
        if p.is_file() and p.suffix.lower() in AUDIO:
            try:
                rows.append(read_track(p))
            except Exception as e:
                errors.append({'source': str(p), 'error': str(e)})
    groups = group_tracks(rows, cfg.get('titleAliases', ()))
    if args.dry_run:
        print(json.dumps({'audio': len(rows), 'groups': len(groups), 'duplicates': len(rows)-len(groups),
                          'plan': [{'keep': str(max(g, key=lambda r:r['size'])['path']), 'alternatives': [str(x['path']) for x in g]} for g in groups if len(g)>1]}, ensure_ascii=False))
        return
    out.mkdir(parents=True, exist_ok=True)
    report_dir = Path(cfg['reportDir'])
    report_dir.mkdir(parents=True, exist_ok=True)
    run_id = time.strftime('%Y%m%d-%H%M%S')
    lock = out / '.organizer.lock'
    acquire_lock(lock)
    report = {'runId': run_id, 'inputAudio': len(rows), 'groups': len(groups), 'published': [], 'duplicates': [], 'failed': errors, 'repairedTails': [], 'missingCover': [], 'missingLyrics': [], 'reused': 0}
    manifest_path = out / '.library-manifest.json'
    old = json.loads(manifest_path.read_text('utf-8')) if manifest_path.exists() else {'entries': {}}
    new = {'version': 2, 'runId': run_id, 'sourceDir': str(src), 'entries': {}}
    sources = {}
    source_manifest = src / '.juyin-import-manifest.json'
    if source_manifest.exists():
        for original, entry in json.loads(source_manifest.read_text('utf-8')).get('entries', {}).items():
            if entry.get('status') == 'ok':
                sources.setdefault(entry['output'], []).append(original)
    net = Network(Path(cfg['cacheDir']), args.online and not args.offline)
    tool_id = file_hash(Path(cfg['ffmpegPath']))
    names = set()
    try:
        for index, group in enumerate(groups, 1):
            row = None
            for candidate in sorted(group, key=lambda x:x['size'], reverse=True):
                try:
                    candidate['audioPath'], trimmed = validated_source(cfg['ffmpegPath'], candidate, Path(cfg['cacheDir']), tool_id)
                    if trimmed:
                        report['repairedTails'].append({'source': str(candidate['path']), 'trimmedBytes': trimmed, 'pcmMd5Verified': True})
                    row = candidate
                    break
                except Exception as e:
                    report['failed'].append({'source': str(candidate['path']), 'error': str(e)})
            if row is None:
                continue
            try:
                for x in group:
                    x['sha256'] = x.get('sha256') or file_hash(x['path'])
                cover, lyrics = local_assets(group, sources)
                provenance = {'cover': 'local' if cover else None, 'lyrics': 'local' if lyrics else None}
                # Upgrade plain lyrics when a confidently matched timed version exists.
                online_cover, online_lyrics, matched = online_assets(net, row, not cover, not lyrics or not STAMP.search(lyrics)) if not cover or not lyrics or not STAMP.search(lyrics) else (None, None, {})
                if online_cover:
                    cover, provenance['cover'] = online_cover, 'netease'
                if online_lyrics and (not lyrics or STAMP.search(online_lyrics)):
                    lyrics, provenance['lyrics'] = online_lyrics, matched.get('lyricsProvider')
                provenance.update(matched)
                stem = safe_name(row)
                if norm(stem) in names:
                    stem += ' [' + row['sha256'][:8] + ']'
                names.add(norm(stem))
                name = stem + row['path'].suffix.lower()
                target = out / name
                asset_id = digest((row['sha256'] + digest(cover or b'') + digest((lyrics or '').encode())).encode())
                prev = old.get('entries', {}).get(name, {})
                if target.exists() and prev.get('assetId') == asset_id and prev.get('sha256') == file_hash(target):
                    report['reused'] += 1
                else:
                    tmp = out / ('.working-' + str(os.getpid()) + row['path'].suffix.lower())
                    try:
                        shutil.copyfile(row.get('audioPath', row['path']), tmp)
                        before = audio_check(cfg['ffmpegPath'], tmp, payload=True)
                        embed(tmp, row, cover, lyrics)
                        after = audio_check(cfg['ffmpegPath'], tmp, payload=True)
                        if before != after:
                            raise ValueError('audio packets changed during metadata write')
                        audio_check(cfg['ffmpegPath'], tmp)
                        if cover and not read_track(tmp)['cover'] and tmp.suffix.lower() != '.wav':
                            raise ValueError('cover write did not persist')
                        # Back up an owned previous output before replacement.
                        if target.exists():
                            backup = Path(cfg['backupDir']) / run_id / name
                            backup.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(target, backup)
                        os.replace(tmp, target)
                    finally:
                        if tmp.exists():
                            tmp.unlink()
                if cover:
                    jpg = out / (stem + '.jpg')
                    if not jpg.exists() or digest(jpg.read_bytes()) != digest(cover):
                        atomic_bytes(jpg, cover)
                else:
                    report['missingCover'].append(name)
                if lyrics:
                    lrc = out / (stem + '.lrc')
                    body = (lyrics.rstrip() + '\n').encode('utf-8')
                    if not lrc.exists() or lrc.read_bytes() != body:
                        atomic_bytes(lrc, body)
                elif not provenance.get('instrumental'):
                    report['missingLyrics'].append(name)
                entry = {'source': str(row['path']), 'sourceSize': row['size'], 'sourceSha256': row['sha256'], 'sha256': file_hash(target), 'assetId': asset_id,
                         'title': row['title'], 'artist': row['artist'], 'duration': row['duration'], 'cover': bool(cover), 'lyrics': 'timed' if lyrics and STAMP.search(lyrics) else 'plain' if lyrics else None, 'instrumental': bool(provenance.get('instrumental')), 'provenance': provenance,
                         'mergedSources': [{'path': str(x['path']), 'bytes': x['size'], 'sha256': x['sha256']} for x in group]}
                new['entries'][name] = entry
                report['published'].append({'file': name, 'cover': entry['cover'], 'lyrics': entry['lyrics']})
                if len(group) > 1:
                    report['duplicates'].append({'keep': str(row['path']), 'keepBytes': row['size'], 'otherSources': [str(x['path']) for x in group if x is not row]})
            except Exception as e:
                report['failed'].append({'source': str(row['path']), 'error': str(e)})
            if index % 10 == 0:
                print('PROGRESS ' + json.dumps({'done': index, 'total': len(groups), 'cover': sum(x['cover'] for x in report['published']), 'lyrics': sum(bool(x['lyrics']) for x in report['published'])}), flush=True)
        # Never remove old outputs when a run has failures. No input is moved/deleted.
        if not report['failed']:
            # Scan the output directory, not just the previous manifest: an
            # output orphaned by an older run would otherwise never be seen.
            stale = set(old.get('entries', {})) | {
                p.name for p in out.iterdir()
                if p.is_file() and p.suffix.lower() in AUDIO and not p.name.startswith('.')}
            # A live entry's stem may match a stale one's (e.g. Song.ogg vs
            # Song.flac), so never delete an asset still owned by a kept entry.
            protected = set()
            for name in new['entries']:
                stem = name.rsplit('.', 1)[0]
                protected.update({name, stem + '.jpg', stem + '.lrc'})
            for name in stale:
                if name in new['entries']:
                    continue
                for p in [out / name, (out / name).with_suffix('.jpg'), (out / name).with_suffix('.lrc')]:
                    if p.exists() and p.name not in protected:
                        backup = Path(cfg['backupDir']) / run_id / p.name
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(p, backup)
                        # Copy/hash verification precedes unlink of managed output only.
                        if file_hash(p) != file_hash(backup):
                            raise ValueError('backup hash mismatch')
                        p.unlink()
        report['networkErrors'] = net.errors
        report['summary'] = {'audio': len(new['entries']), 'duplicatesMerged': sum(len(x['otherSources']) for x in report['duplicates']), 'withCover': sum(x['cover'] for x in report['published']), 'withLyrics': sum(bool(x['lyrics']) for x in report['published']), 'timedLyrics': sum(x['lyrics']=='timed' for x in report['published']), 'failed': len(report['failed']), 'reused': report['reused']}
        json_write(manifest_path, new)
        # After renaming/deduplicating managed imports, keep the importer's
        # incremental references bound to the final output, so it does not
        # import the same download again on the next refresh.
        if src == out and source_manifest.exists():
            imported = json.loads(source_manifest.read_text('utf-8'))
            by_path = {str(Path(s['path']).resolve()): name
                       for name, entry in new['entries'].items()
                       for s in entry['mergedSources']}
            for entry in imported.get('entries', {}).values():
                if entry.get('status') == 'ok' and entry.get('output'):
                    final_name = by_path.get(str((src / entry['output']).resolve()))
                    if final_name:
                        entry['output'] = final_name
                        entry['format'] = Path(final_name).suffix.lstrip('.')
            json_write(source_manifest, imported)
        json_write(report_dir / ('organize-' + run_id + '.json'), report)
        json_write(report_dir / 'latest.json', report)
        missing = ['未补齐项（不影响已有音频播放；未知版本不强配）', '']
        missing += ['缺封面：' + x for x in report['missingCover']]
        missing += ['缺歌词：' + x for x in report['missingLyrics']]
        missing += ['校验失败：' + x['source'] + ' — ' + x['error'] for x in report['failed']]
        atomic_bytes(report_dir / '未补齐清单.txt', ('\n'.join(missing) + '\n').encode('utf-8'))
        print(json.dumps(report['summary'], ensure_ascii=False), flush=True)
    finally:
        lock.unlink(missing_ok=True)

if __name__ == '__main__':
    main()
