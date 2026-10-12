"""Regression checks use generated audio and temporary folders, never real songs."""
from pathlib import Path
import importlib.util
import os
import shutil
import sys
SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
(SRC.parent / ".work" / "test-fixtures").mkdir(parents=True, exist_ok=True)
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('organizer', SRC / 'organize-library.py')
lib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lib)
FF = os.environ.get('FFMPEG_PATH') or shutil.which('ffmpeg')
if not FF: raise RuntimeError('Install FFmpeg or set FFMPEG_PATH to its executable')

class Tests(unittest.TestCase):
    def row(self, name='Song', artist='Singer', album='Album', dur=3, size=100):
        return {'title':name,'artist':artist,'album':album,'duration':dur,'size':size,'version':lib.versions(name,album),'path':Path(name+'.flac')}

    def test_version_and_unicode(self):
        self.assertNotEqual(lib.norm('Song (Live)'),lib.norm('Song'))
        self.assertNotEqual(lib.norm('ハルカ'),lib.norm('アイドル'))
        track=self.row('Song (Live)')
        hit={'trackName':'Song','artistName':'Singer','albumName':'Album','duration':3,'id':1}
        self.assertIsNone(lib.choose_candidate(track,[hit],'lrclib'))

    def test_group_and_size_rule(self):
        a,b,c,d=self.row(size=100),self.row(size=200),self.row(artist='Another'),self.row('Song (Live)')
        groups=lib.group_tracks([a,b,c,d])
        self.assertEqual(sorted(map(len,groups)),[1,1,2])
        dup=next(g for g in groups if len(g)==2)
        self.assertEqual(max(dup,key=lambda x:x['size'])['size'],200)
        self.assertEqual(len(lib.group_tracks([a,self.row(dur=20)])),2)

    def test_ambiguity_and_album(self):
        t=self.row()
        hits=[{'trackName':'Song','artistName':'Singer','albumName':'A','duration':3,'id':1},
              {'trackName':'Song','artistName':'Singer','albumName':'B','duration':3,'id':2}]
        self.assertIsNone(lib.choose_candidate(t,hits,'lrclib'))
        t['album']='B'
        self.assertEqual(lib.choose_candidate(t,hits,'lrclib')['id'],2)
        t['artist']=''
        self.assertIsNone(lib.choose_candidate(t,hits,'lrclib'))

    def test_explicit_translation_alias_preserves_other_versions(self):
        alias = {'artist': 'Toe', 'album': 'Album', 'title': 'メトロノーム',
                 'canonicalTitle': 'メトロノーム (节拍器)', 'duration': 217.44}
        original = self.row('メトロノーム', 'Toe', dur=217.44)
        translated = self.row('メトロノーム (节拍器)', 'Toe', dur=217.44, size=200)
        other_album = self.row('メトロノーム', 'Toe', album='Other', dur=217.44)
        live = self.row('メトロノーム (Live)', 'Toe', dur=217.44)
        groups = lib.group_tracks([original, translated, other_album, live], [alias])
        self.assertEqual(sorted(map(len, groups)), [1, 1, 2])
        pair = next(g for g in groups if len(g) == 2)
        self.assertEqual(max(pair, key=lambda r: r['size'])['title'], alias['canonicalTitle'])

    def test_embedding_preserves_audio_and_readable_cover(self):
        with tempfile.TemporaryDirectory(dir=str(SRC.parent / ".work" / "test-fixtures")) as td:
            root=Path(td)
            img=lib.Image.new('RGB',(120,120),'blue')
            b=lib.io.BytesIO();img.save(b,'PNG')
            cover=lib.image_jpeg(b.getvalue())
            for ext,codec in [('flac','flac'),('ogg','libvorbis'),('mp3','libmp3lame')]:
                p=root/('Singer - Song.'+ext)
                subprocess.run([FF,'-v','error','-f','lavfi','-i','sine=frequency=440:duration=3','-c:a',codec,'-metadata','title=Song','-metadata','artist=Singer',str(p)],check=True)
                before=lib.audio_check(FF,p,True)
                lib.embed(p,self.row(),cover,'[00:00.00]sample\n[00:02.00]next')
                self.assertEqual(before,lib.audio_check(FF,p,True),ext)
                lib.audio_check(FF,p)
                meta=lib.read_track(p)
                self.assertTrue(meta['cover'],ext)
                self.assertIn('sample',meta['lyrics'],ext)

    def test_full_workflow_dry_run_and_idempotence(self):
        with tempfile.TemporaryDirectory(dir=str(SRC.parent / ".work" / "test-fixtures")) as td:
            root=Path(td);src=root/'input';src.mkdir();out=root/'output'
            for name,codec,ext in [('Singer - Song','libmp3lame','mp3'),('Singer - Song (2)','flac','flac')]:
                subprocess.run([FF,'-v','error','-f','lavfi','-i','sine=frequency=440:duration=3','-c:a',codec,'-metadata','title=Song','-metadata','artist=Singer',str(src/(name+'.'+ext))],check=True)
            (src/'Singer - Song.lrc').write_text('[00:00.00]sample\n[00:02.00]next','utf-8')
            cfg={'libraryDir':str(src),'organizedDir':str(out),'ffmpegPath':FF,'reportDir':str(root/'reports'),'cacheDir':str(root/'cache'),'backupDir':str(root/'backup')}
            cf=root/'cfg.json';cf.write_text(json.dumps(cfg),'utf-8')
            hashes={str(p):lib.file_hash(p) for p in src.iterdir()}
            cmd=[os.sys.executable,str(SRC / 'organize-library.py'),'--config',str(cf),'--offline']
            subprocess.run(cmd+['--dry-run'],check=True,capture_output=True)
            self.assertFalse(out.exists())
            subprocess.run(cmd,check=True,capture_output=True)
            outputs=list(out.glob('*.flac'))+list(out.glob('*.mp3'))
            self.assertEqual(len(outputs),1)
            largest=max((p for p in src.iterdir() if p.suffix in {'.flac','.mp3'}),key=lambda p:p.stat().st_size)
            self.assertEqual(outputs[0].suffix,largest.suffix)
            self.assertTrue(outputs[0].with_suffix('.lrc').exists())
            first=lib.file_hash(outputs[0])
            subprocess.run(cmd,check=True,capture_output=True)
            self.assertEqual(first,lib.file_hash(outputs[0]))
            report=json.loads((root/'reports'/'latest.json').read_text('utf-8'))
            self.assertEqual(report['summary']['reused'],1)
            self.assertEqual(hashes,{str(p):lib.file_hash(p) for p in src.iterdir()})

    def test_tail_cleanup_uses_pcm_md5_and_keeps_original(self):
        with tempfile.TemporaryDirectory(dir=str(SRC.parent / ".work" / "test-fixtures")) as td:
            root=Path(td); p=root/'tail.flac'
            subprocess.run([FF,'-v','error','-f','lavfi','-i','sine=frequency=440:duration=3','-c:a','flac',str(p)],check=True)
            with p.open('ab') as f:f.write(b'qq-trailer-junk!')
            before=lib.file_hash(p)
            fixed,trim=lib.clean_flac_tail(FF,p,root/'work')
            self.assertEqual(trim,len(b'qq-trailer-junk!'))
            lib.audio_check(FF,fixed)
            self.assertEqual(lib.file_hash(p),before)

    def test_ogg_trailer_is_trimmed_only_when_audio_is_unchanged(self):
        with tempfile.TemporaryDirectory(dir=str(SRC.parent / ".work" / "test-fixtures")) as td:
            root=Path(td);p=root/'song.ogg'
            subprocess.run([FF,'-v','error','-f','lavfi','-i','sine=frequency=440:duration=3','-c:a','libvorbis',str(p)],check=True)
            trailer=b'\x00\x00\x00\x01\x00\x00\x00musicex\x00'
            with p.open('ab') as f:f.write(trailer)
            before=lib.file_hash(p)
            fixed,cut=lib.clean_ogg_tail(FF,p,root/'work')
            self.assertEqual(cut,len(trailer))
            lib.audio_check(FF,fixed)
            self.assertEqual(lib.file_hash(p),before)
            # Tag rewriting must now succeed and must not alter the audio.
            audio=lib.pcm_digest(FF,fixed)
            lib.embed(fixed,self.row(),lib.image_jpeg(self._png()),'[00:00.00]sample')
            self.assertEqual(audio,lib.pcm_digest(FF,fixed))
            self.assertTrue(lib.read_track(fixed)['cover'])

    def _png(self):
        img=lib.Image.new('RGB',(120,120),'red');b=lib.io.BytesIO();img.save(b,'PNG');return b.getvalue()

    def test_canonical_cover_is_not_reencoded(self):
        cover = lib.image_jpeg(self._png())
        self.assertEqual(lib.image_jpeg(cover), cover)

    def test_managed_refresh_reuses_tagged_audio_but_applies_new_lyrics(self):
        with tempfile.TemporaryDirectory(dir=str(SRC.parent / '.work/test-fixtures')) as td:
            root = Path(td); out = root/'library'; out.mkdir()
            audio = out/'Singer - Song.flac'
            subprocess.run([FF,'-v','error','-f','lavfi','-i','sine=frequency=440:duration=1',
                            '-c:a','flac','-metadata','title=Song','-metadata','artist=Singer',str(audio)],check=True)
            audio.with_suffix('.png').write_bytes(self._png())
            lyrics = audio.with_suffix('.lrc')
            lyrics.write_text('[00:00.00]original sample','utf-8')
            (out/'.library-manifest.json').write_text(json.dumps({'entries':{}}),'utf-8')
            cfg = {'libraryDir':str(out),'organizedDir':str(out),'allowManagedInPlace':True,
                   'ffmpegPath':FF,'reportDir':str(root/'reports'),
                   'cacheDir':str(root/'cache'),'backupDir':str(root/'backup')}
            path = root/'cfg.json';path.write_text(json.dumps(cfg),'utf-8')
            command = [sys.executable,str(SRC/'organize-library.py'),'--config',str(path),'--offline']
            def refresh():
                subprocess.run(command,check=True,capture_output=True)
                return json.loads((root/'reports/latest.json').read_text('utf-8'))['summary']
            refresh()
            first = lib.file_hash(audio)
            self.assertEqual(refresh()['reused'], 1)
            self.assertEqual(lib.file_hash(audio), first)
            self.assertEqual(refresh()['reused'], 1)
            lyrics.write_text('[00:00.00]original sample\n[00:00.50]new longer sample','utf-8')
            self.assertEqual(refresh()['reused'], 0)
            self.assertIn('new longer sample', lib.read_track(audio)['lyrics'])
            updated = lib.file_hash(audio)
            self.assertNotEqual(updated, first)
            self.assertEqual(refresh()['reused'], 1)
            self.assertEqual(lib.file_hash(audio), updated)

    def test_lock_recovers_dead_run_but_respects_live_process(self):
        with tempfile.TemporaryDirectory(dir=str(SRC.parent / ".work" / "test-fixtures")) as td:
            p=Path(td)/'lock';p.write_text('2147483000','ascii')
            lib.acquire_lock(p)
            self.assertEqual(p.read_text('ascii'),str(os.getpid()))
            with self.assertRaises(RuntimeError):lib.acquire_lock(p)

    def test_stale_cleanup_keeps_assets_of_kept_entry(self):
        with tempfile.TemporaryDirectory(dir=str(SRC.parent / ".work" / "test-fixtures")) as td:
            root=Path(td);src=root/'in';src.mkdir();out=root/'out';out.mkdir()
            # Source holds only the FLAC, so the OGG is a stale orphan output.
            subprocess.run([FF,'-v','error','-f','lavfi','-i','sine=frequency=440:duration=3','-c:a','flac','-metadata','title=Song','-metadata','artist=Singer',str(src/'Singer - Song.flac')],check=True)
            subprocess.run([FF,'-v','error','-f','lavfi','-i','sine=frequency=440:duration=3','-c:a','libvorbis',str(out/'Singer - Song.ogg')],check=True)
            (out/'Singer - Song.lrc').write_text('[00:00.00]keep me','utf-8')
            cfg={'libraryDir':str(src),'organizedDir':str(out),'ffmpegPath':FF,'reportDir':str(root/'r'),'cacheDir':str(root/'c'),'backupDir':str(root/'b')}
            cf=root/'cfg.json';cf.write_text(json.dumps(cfg),'utf-8')
            subprocess.run([os.sys.executable,str(SRC / 'organize-library.py'),'--config',str(cf),'--offline'],check=True,capture_output=True)
            self.assertFalse((out/'Singer - Song.ogg').exists())
            self.assertTrue((out/'Singer - Song.flac').exists())
            self.assertTrue((out/'Singer - Song.lrc').exists())

    def test_managed_in_place_rebinds_import_after_rename(self):
        with tempfile.TemporaryDirectory(dir=str(SRC.parent / ".work" / "test-fixtures")) as td:
            root = Path(td); out = root/'library'; out.mkdir()
            raw = out/'Raw filename.flac'
            subprocess.run([FF,'-v','error','-f','lavfi','-i','sine=frequency=440:duration=3',
                            '-c:a','flac','-metadata','title=Song','-metadata','artist=Singer',str(raw)],check=True)
            (out/'.library-manifest.json').write_text(json.dumps({'entries':{}}),'utf-8')
            imported = {'entries':{'download.ncm':{'status':'ok','output':raw.name}}}
            (out/'.juyin-import-manifest.json').write_text(json.dumps(imported),'utf-8')
            cfg = {'libraryDir':str(out),'organizedDir':str(out),'allowManagedInPlace':True,
                   'ffmpegPath':FF,'reportDir':str(root/'reports'),
                   'cacheDir':str(root/'cache'),'backupDir':str(root/'backup')}
            path = root/'cfg.json';path.write_text(json.dumps(cfg),'utf-8')
            cmd = [os.sys.executable,str(SRC / 'organize-library.py'),
                   '--config',str(path),'--offline']
            subprocess.run(cmd,check=True,capture_output=True)
            final = out/'Singer - Song.flac'
            self.assertTrue(final.exists());self.assertFalse(raw.exists())
            bound = json.loads((out/'.juyin-import-manifest.json').read_text('utf-8'))
            self.assertEqual(bound['entries']['download.ncm']['output'],final.name)
            before = lib.file_hash(final)
            subprocess.run(cmd,check=True,capture_output=True)
            self.assertEqual(lib.file_hash(final),before)
            self.assertEqual(len(list(out.glob('*.flac'))),1)

if __name__=='__main__':
    unittest.main()
