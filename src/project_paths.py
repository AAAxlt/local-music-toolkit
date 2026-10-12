"""Resolve configuration independently of the shell's current directory."""
import json
import os
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def playlist_reference(audio, playlist, absolute=False):
    audio = Path(audio).resolve()
    if absolute:
        return audio.as_posix()
    try:
        return Path(os.path.relpath(audio, Path(playlist).resolve().parent)).as_posix()
    except ValueError:
        # Windows relative paths cannot cross drives.
        return audio.as_posix()


def load_settings(config):
    config = Path(config).resolve()
    cfg = json.loads(config.read_text('utf-8-sig'))
    def resolve(value):
        return str((config.parent / value).resolve())
    for key in ['libraryDir','organizedDir','outputDir','workDir','backupDir','cacheDir',
                'reportDir','runReportDir','replacedDir','ncmDecoder']:
        if cfg.get(key):
            cfg[key] = resolve(cfg[key])
    for key in ['sourceDirs','additionalPlainDirs']:
        cfg[key] = [resolve(value) for value in cfg.get(key, [])]
    if cfg.get('qqd'):
        cfg['qqd'] = {k: resolve(v) for k,v in cfg['qqd'].items()}
    tool = os.environ.get('FFMPEG_PATH') or cfg.get('ffmpegPath', 'ffmpeg')
    if '/' in tool or '\\' in tool:
        tool = resolve(tool)
    tool = shutil.which(tool)
    if not tool:
        raise FileNotFoundError('FFmpeg not found. Install FFmpeg or configure ffmpegPath.')
    cfg['ffmpegPath'] = tool
    return cfg
