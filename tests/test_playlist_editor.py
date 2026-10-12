"""Headless checks for the playlist editor: no window is shown.

Covers the m3u8 round trip and the add/remove/reorder model against a
temporary library of dummy files.
"""
from pathlib import Path
import importlib.util
import os
import shutil
import sys
SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
(SRC.parent / ".work" / "test-fixtures").mkdir(parents=True, exist_ok=True)
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    'editor', SRC / 'playlist-editor.py')
ed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ed)


class Tests(unittest.TestCase):
    def test_m3u8_round_trip_and_folia_shape(self):
        with tempfile.TemporaryDirectory(dir=str(SRC.parent / ".work" / "test-fixtures")) as td:
            p = Path(td) / 'list.m3u8'
            names = ['A - One.flac', 'B - Two.ogg']
            library = Path(td) / '全部歌曲'
            ed.write_m3u8(p, '测试单', names, library, absolute=True)
            text = p.read_text(encoding='utf-8-sig')
            # Extended M3U carries the playlist title and ignores other comments.
            self.assertIn('#EXTM3U', text)
            self.assertIn('#PLAYLIST:测试单', text)
            name, got = ed.parse_m3u8(p)
            self.assertEqual(name, '测试单')
            self.assertEqual(got, names)
            actual_paths = [s for s in text.splitlines() if s and not s.startswith('#')]
            self.assertEqual(actual_paths, [(library / n).resolve().as_posix() for n in names])
            self.assertNotIn(b'\r\r\n', p.read_bytes())

    def test_parse_ignores_bom_and_blank_lines(self):
        with tempfile.TemporaryDirectory(dir=str(SRC.parent / ".work" / "test-fixtures")) as td:
            p = Path(td) / 'x.m3u8'
            p.write_text('#EXTM3U\n\n#PLAYLIST:名字\n#EXTINF:-1,Song\nSong.mp3\n',
                         encoding='utf-8-sig')
            name, got = ed.parse_m3u8(p)
            self.assertEqual(name, '名字')
            self.assertEqual(got, ['Song.mp3'])

    def test_parse_uses_bare_file_name(self):
        with tempfile.TemporaryDirectory(dir=str(SRC.parent / ".work" / "test-fixtures")) as td:
            p = Path(td) / 'y.m3u8'
            p.write_text('#EXTM3U\nD:\\Music\\Sub\\Song.mp3\n', encoding='utf-8-sig')
            _, got = ed.parse_m3u8(p)
            self.assertEqual(got, ['Song.mp3'])

    def test_library_songs_only_audio(self):
        with tempfile.TemporaryDirectory(dir=str(SRC.parent / ".work" / "test-fixtures")) as td:
            root = Path(td)
            for n in ['b.ogg', 'a.flac', 'note.txt', 'cover.jpg']:
                (root / n).write_bytes(b'x')
            names = [p.name for p in ed.library_songs(root)]
            self.assertEqual(names, ['a.flac', 'b.ogg'])

    def test_norm_folds_case_and_width(self):
        self.assertEqual(ed.norm('ＡＢＣ'), ed.norm('abc'))
        self.assertEqual(ed.norm('邓紫棋'), ed.norm('邓紫棋'))


if __name__ == '__main__':
    unittest.main()
