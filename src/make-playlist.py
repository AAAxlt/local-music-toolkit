#!/usr/bin/env python3
"""Generate M3U8 playlists without copying audio files."""
import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from project_paths import PROJECT_ROOT, playlist_reference

AUDIO = {'.flac', '.ogg', '.mp3', '.m4a', '.wav', '.opus'}

# Keep the console's own code page (a Windows cmd is GBK) but never crash on
# a character it cannot represent. Forcing UTF-8 here would garble the output.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(errors='replace')
    except (AttributeError, ValueError):
        pass


def norm(s):
    return ''.join(c for c in unicodedata.normalize('NFKC', s or '').casefold() if c.isalnum())


def library_songs(library):
    return sorted(
        (p for p in library.iterdir() if p.is_file() and p.suffix.lower() in AUDIO),
        key=lambda p: p.name.casefold(),
    )


def write_m3u8(path, name, songs, absolute=False):
    lines = ['#EXTM3U', '#PLAYLIST:' + name, '']
    for p in songs:
        lines.append('#EXTINF:-1,' + p.stem)
        lines.append(playlist_reference(p, path, absolute))
    body = '\r\n'.join(lines) + '\r\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        f.write(body)
    return path


def resolve(songs, keys, exact):
    """Map user keys to songs. Exact: stem equals key (case-insensitive).
    Fuzzy: key is a substring of stem or the artist prefix."""
    picked, seen = [], set()
    unmatched = []
    for key in keys:
        k = key.strip()
        if not k:
            continue
        if exact:
            hits = [s for s in songs if norm(s.stem) == norm(k)]
        else:
            nk = norm(k)
            hits = [s for s in songs if nk and nk in norm(s.stem)]
        if not hits:
            unmatched.append(k)
            continue
        for s in hits:
            if s.name not in seen:
                seen.add(s.name)
                picked.append(s)
    return picked, unmatched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--library', default=str(PROJECT_ROOT / 'library'))
    ap.add_argument('--out-dir', default=str(PROJECT_ROOT / 'playlists'))
    ap.add_argument('--name')
    ap.add_argument('--match', action='append', default=[])
    ap.add_argument('--from-file')
    ap.add_argument('--exact', action='store_true')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--absolute', action='store_true')
    args = ap.parse_args()

    library = Path(args.library)
    if not library.is_dir():
        sys.exit('library not found: ' + str(library))
    songs = library_songs(library)

    if args.list:
        for s in songs:
            print(s.name)
        print(f'-- {len(songs)} songs --', file=sys.stderr)
        return

    keys = []
    for m in args.match:
        keys += [x for x in re.split(r'[,，、]', m)]
    if args.from_file:
        keys += Path(args.from_file).read_text(encoding='utf-8-sig').splitlines()
    if not keys:
        sys.exit('nothing to add: pass --match or --from-file')

    picked, unmatched = resolve(songs, keys, args.exact)
    if not picked:
        sys.exit('no songs matched')

    name = args.name or 'Imported Playlist'
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .') or 'playlist'
    out = Path(args.out_dir) / (safe + '.m3u8')
    write_m3u8(out, name, picked, args.absolute)

    print(json.dumps({
        'playlist': str(out),
        'name': name,
        'songs': len(picked),
        'unmatched': unmatched,
    }, ensure_ascii=False, indent=2))
    if unmatched:
        print('未匹配（已跳过）:', file=sys.stderr)
        for u in unmatched:
            print('  ' + u, file=sys.stderr)


if __name__ == '__main__':
    main()
