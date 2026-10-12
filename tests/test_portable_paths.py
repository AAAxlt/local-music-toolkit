import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SRC))
from project_paths import playlist_reference, load_settings
spec = importlib.util.spec_from_file_location('editor', SRC / 'playlist-editor.py')
editor = importlib.util.module_from_spec(spec); spec.loader.exec_module(editor)


class PortablePaths(unittest.TestCase):
    def test_playlist_still_resolves_after_moving_the_bundle(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'original'
            music = root/'library'; music.mkdir(parents=True)
            audio = music/'歌手 - 歌曲.flac';audio.write_bytes(b'generated-test-placeholder')
            playlist = root/'playlists'/'精选.m3u8'
            editor.write_m3u8(playlist, '精选', [audio.name], music)
            refs = [s for s in playlist.read_text('utf-8-sig').splitlines() if s and not s.startswith('#')]
            self.assertEqual(refs, ['../library/' + audio.name])
            moved = Path(td)/'renamed-bundle';root.rename(moved)
            self.assertTrue((moved/'playlists'/refs[0]).resolve().is_file())
            self.assertEqual(editor.parse_m3u8(moved/'playlists'/'精选.m3u8')[1], [audio.name])

    def test_config_paths_are_based_on_config_not_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); folder = base/'settings';folder.mkdir()
            tool = os.environ.get('FFMPEG_PATH') or shutil.which('ffmpeg')
            cfg = folder/'config.json'
            cfg.write_text(json.dumps({'libraryDir':'../music','sourceDirs':['../downloads'],
                                       'ffmpegPath':tool}), 'utf-8')
            previous = Path.cwd()
            try:
                os.chdir(base)
                result = load_settings(cfg)
                self.assertEqual(result['libraryDir'], str((base/'music').resolve()))
                self.assertEqual(result['sourceDirs'], [str((base/'downloads').resolve())])
            finally:
                os.chdir(previous)

    def test_absolute_playlist_option(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            self.assertEqual(playlist_reference(root/'song.flac',root/'list.m3u8',True),
                             (root/'song.flac').resolve().as_posix())


if __name__ == '__main__':
    unittest.main()
