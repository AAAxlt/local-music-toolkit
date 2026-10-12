"""Import local downloads, then maintain one managed music folder."""
import argparse
import json
import subprocess
import sys
from pathlib import Path
from project_paths import PROJECT_ROOT, load_settings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=str(PROJECT_ROOT / 'config.json'))
    ap.add_argument('--preview', action='store_true')
    ap.add_argument('--online', action='store_true', help='Opt in to metadata queries')
    ap.add_argument('--allow-qqd', action='store_true', help='Opt in to a separately installed Windows QQ helper')
    args = ap.parse_args()
    cfg = load_settings(args.config)
    cfg['allowQqd'] = args.allow_qqd
    out = Path(cfg['outputDir'])
    if out.resolve() != Path(cfg['libraryDir']).resolve() or out.resolve() != Path(cfg['organizedDir']).resolve():
        raise ValueError('outputDir, libraryDir and organizedDir must point to the same managed folder')
    if args.preview:
        return subprocess.run(['node', str(PROJECT_ROOT / 'src/import-cli.mjs'),
                               '--config', str(Path(args.config).resolve()), '--dry-run']).returncode
    marker = out / '.library-manifest.json'
    if not marker.exists():
        if out.exists() and any(p.is_file() and p.suffix.lower() in
                               {'.flac','.ogg','.mp3','.m4a','.wav','.opus'} for p in out.iterdir()):
            raise ValueError('Choose an empty output folder for the first import; existing audio is protected.')
        out.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({'version':2,'entries':{}}), 'utf-8')
    work = Path(cfg['workDir']); work.mkdir(parents=True, exist_ok=True)
    runtime = work / 'runtime-config.json'
    runtime.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), 'utf-8')
    imported = subprocess.run(['node', str(PROJECT_ROOT / 'src/import-cli.mjs'),
                               '--config', str(runtime)]).returncode
    if imported not in (0,1):
        return imported
    command = [sys.executable, str(PROJECT_ROOT / 'src/organize-library.py'), '--config', str(runtime)]
    command += ['--online'] if args.online else ['--offline']
    organized = subprocess.run(command).returncode
    return organized or imported


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, FileNotFoundError, KeyError) as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)
