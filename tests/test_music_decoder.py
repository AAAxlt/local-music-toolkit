"""Publication regression: original changes during validation must not publish."""
import hashlib,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import legacy_decoders as legacy
import music_decoder
from music_decoder import _publish_copy, decode
from qq_mflac import MflacError
class PublicationTest(unittest.TestCase):
    def test_original_changes_before_publish(self):
        for ncm in (False,True):
            with self.subTest(ncm=ncm),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);source=root/'source';payload=root/'payload';target=root/'output'
                source.write_bytes(b'original-container' if ncm else b'audio');payload.write_bytes(b'audio')
                original_sha=hashlib.sha256(source.read_bytes()).hexdigest();expected=hashlib.sha256(b'audio').hexdigest()
                def mutate(*args):source.write_bytes(b'changed');return {'duration':1}
                with patch('music_decoder.strict_audio',side_effect=mutate):
                    with self.assertRaises(MflacError) as caught:
                        _publish_copy(payload if ncm else source,target,'unused',expected,source,original_sha)
                self.assertEqual(caught.exception.category,'source-changed');self.assertFalse(target.exists())
    def test_existing_output_is_retained_on_source_change(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source';target=root/'output';source.write_bytes(b'audio');target.write_bytes(b'audio')
            digest=hashlib.sha256(b'audio').hexdigest()
            def mutate(*args):source.write_bytes(b'changed');return {'duration':1}
            with patch('music_decoder.strict_audio',side_effect=mutate):
                with self.assertRaises(MflacError):_publish_copy(source,target,'unused',digest)
            self.assertEqual(target.read_bytes(),b'audio')
class Provider:
    def __init__(self, material=None, expected_name=None):
        self.material=material
        self.expected_name=expected_name
        self.calls=0
    def fetch(self, paths):
        self.calls+=1
        if self.material is None:return {}
        return {music_decoder.mflac._sha(path):self.material for path in paths
                if self.expected_name is None or Path(path).name==self.expected_name}

class DecodeIntegrationTest(unittest.TestCase):
    def fake_audio(self,ffmpeg,path):return dict(codec='fake',sampleRate=44100,channels=2,duration=1.0,bits=16)
    def run_decode(self,source,out,cache,provider=None):
        with patch('music_decoder.strict_audio',side_effect=self.fake_audio):
            return decode(source,out,cache,'unused',provider=provider)
    def test_static_qmc_and_content_first_fake_suffix(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);inp=root/'in';out=root/'out';cache=root/'cache';inp.mkdir()
            plain=b'ID3'+bytes(range(200))+b'\xff\xff\xff\xff'
            encrypted=legacy.make_qmc_cipher(b'').decrypt(plain,0)
            (inp/'song.qmc0').write_bytes(encrypted)
            (inp/'plain.mgg').write_bytes(b'OggS'+bytes(range(200)))
            report=self.run_decode(inp,out,cache,Provider(None))
            self.assertTrue(report['ok'],report)
            self.assertEqual((out/'song.mp3').read_bytes(),plain)
            self.assertEqual((out/'plain.ogg').read_bytes(),b'OggS'+bytes(range(200)))
            self.assertEqual(report['counts']['ok'],2)
            first=next(item for item in report['items'] if item['source']=='song.qmc0')
            self.assertEqual(first['kind'],'qmc')
            self.assertEqual(first['materialSource'],'none')
            self.assertTrue(first['outputSHA'])
            again=self.run_decode(inp,out,cache,Provider(None))
            self.assertEqual(again['counts']['unchanged'],2)
    def test_musicex_mgg_waiting_and_cache_backed(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);inp=root/'in';out=root/'out';cache=root/'cache';inp.mkdir()
            tag=b'x'*40+self._musicex('O8M0MISSING')
            (inp/'missing.mgg').write_bytes(tag)
            report=self.run_decode(inp,out,cache,Provider(None))
            self.assertEqual(report['items'][0]['status'],'waiting')
            self.assertEqual(report['items'][0]['category'],'local-material-missing')
            self.assertTrue(report['items'][0]['message'])
            self.assertFalse((out/'missing.ogg').exists())
            key=bytes((i*11+3)&0xff for i in range(256))
            plain=b'OggS'+bytes(range(256))
            encrypted=legacy.make_qmc_cipher(key).decrypt(plain,0)
            (inp/'missing.mgg').unlink()
            (inp/'ready.mgg').write_bytes(encrypted+self._musicex('O8M0READY'))
            provider=Provider(key,expected_name='ready.mgg')
            report=self.run_decode(inp,out,cache,provider)
            self.assertTrue(report['ok'],report)
            self.assertEqual(report['items'][0]['materialSource'],'local-cache')
            self.assertEqual((out/'ready.ogg').read_bytes(),plain)
            self.assertEqual(provider.calls,1)
    def test_two_mgg_files_use_their_own_material(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);inp=root/'in';out=root/'out';cache=root/'cache';inp.mkdir()
            rows=[]
            for index in (1,2):
                key=bytes((i*7+index)&0xff for i in range(256))
                plain=b'OggS'+bytes((index,))*300
                encrypted=legacy.make_qmc_cipher(key).decrypt(plain,0)
                path=inp/('song%d.mgg'%index)
                path.write_bytes(encrypted+self._musicex('O8M0FILE%d'%index))
                rows.append((path,key,plain))
            material={music_decoder.mflac._sha(path):key for path,key,_ in rows}
            class Multi:
                def __init__(self):self.seen=[]
                def fetch(self,paths):
                    self.seen.extend(paths)
                    return {music_decoder.mflac._sha(p):material[music_decoder.mflac._sha(p)] for p in paths}
            provider=Multi()
            report=self.run_decode(inp,out,cache,provider)
            self.assertTrue(report['ok'],report)
            for path,key,plain in rows:
                self.assertEqual((out/(path.stem+'.ogg')).read_bytes(),plain)
            self.assertEqual(len(provider.seen),2)
    def test_fast_mflac_core_is_gated_to_single_mflac_layout(self):
        cases = (
            ('renamed.mgg', '.mflac', 1),
            ('wrong-id.mflac', '.mgg', 1),
            ('repeated.mflac', 'song.mflac', 2),
        )
        for index, (name, identifier, tails) in enumerate(cases):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root=Path(folder);inp=root/'in';out=root/'out';cache=root/'cache';inp.mkdir()
                key=bytes((i*17+index)&0xff for i in range(256))
                plain=b'OggS'+bytes((index+1,))*300
                encrypted=legacy.make_qmc_cipher(key).decrypt(plain,0)
                tag=self._musicex(identifier)
                (inp/name).write_bytes(encrypted+tag*tails)
                with patch('music_decoder.mflac._preflight',
                           side_effect=AssertionError('fast MFLAC core must be gated')):
                    report=self.run_decode(inp,out,cache,Provider(key))
                self.assertTrue(report['ok'],report)
                self.assertEqual(report['items'][0]['kind'],'qmc')
                self.assertEqual((out/(Path(name).stem+'.ogg')).read_bytes(),plain)
    def test_fast_path_accepts_single_mflac_identifier(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'song.mflac';cache=root/'cache';cache.mkdir()
            source.write_bytes(b'fLaC'+b'x'*100+self._musicex('Song Title.mflac'))
            digest=music_decoder.mflac._sha(source)
            tail=source.read_bytes()[-192:]
            with patch('music_decoder.mflac._preflight',return_value=digest), \
                 patch('music_decoder.mflac._cache_lookup',return_value={'material':b'K'*512}), \
                 patch('music_decoder.mflac._process',return_value={'status':'ok'}) as process:
                handled=music_decoder._try_existing_mflac(
                    source,Path('song.mflac'),root,cache,'unused',Provider(None),digest,tail)
            self.assertEqual(handled[0],'done')
            self.assertEqual(handled[1]['algorithm'],music_decoder.mflac.ALGORITHM)
            process.assert_called_once()
    def test_kgm_v5_waiting_does_not_publish(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);inp=root/'in';out=root/'out';cache=root/'cache';inp.mkdir()
            header=bytearray(0x3C);header[:16]=legacy.KGM_HEADER
            header[0x10:0x14]=(0x3C).to_bytes(4,'little')
            header[0x14:0x18]=(5).to_bytes(4,'little')
            (inp/'v5.kgm').write_bytes(bytes(header)+b'payload')
            report=self.run_decode(inp,out,cache,Provider(None))
            self.assertEqual(report['items'][0]['status'],'waiting')
            self.assertEqual(report['items'][0]['category'],'kgm-v5-db-required')
            self.assertNotIn('播放',report['items'][0]['message'])
            self.assertFalse(any(out.iterdir()))
    def test_source_mutation_does_not_publish(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);inp=root/'in';out=root/'out';cache=root/'cache';inp.mkdir()
            plain=b'ID3'+bytes(range(100))+b'\xff\xff\xff\xff'
            encrypted=legacy.make_qmc_cipher(b'').decrypt(plain,0)
            source=inp/'song.qmc0';source.write_bytes(encrypted)
            def mutate(ffmpeg,path):
                source.write_bytes(b'changed')
                return dict(codec='fake',sampleRate=44100,channels=2,duration=1.0,bits=16)
            with patch('music_decoder.strict_audio',side_effect=mutate):
                report=decode(inp,out,cache,'unused',provider=Provider(None))
            self.assertEqual(report['items'][0]['status'],'failed')
            self.assertEqual(report['items'][0]['category'],'source-changed')
            self.assertFalse((out/'song.mp3').exists())
    def _musicex(self,identifier):
        import struct
        tag=bytearray(192)
        name=identifier.encode('utf-16-le')[:100]
        tag[0x48:0x48+len(name)]=name
        tag[176:180]=struct.pack('<I',192);tag[180:184]=struct.pack('<I',1);tag[184:192]=b'musicex\x00'
        return bytes(tag)

if __name__=='__main__':unittest.main()
