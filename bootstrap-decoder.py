"""Project-local pinned runtime installer; no pip, global PATH, or elevation."""
import argparse,hashlib,json,os,platform,shutil,subprocess,sys,time,urllib.request,uuid,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parent
def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1<<20),b''):h.update(chunk)
    return h.hexdigest()
def extract(archive,target):
    target.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        for item in z.infolist():
            p=(target/item.filename).resolve()
            if not p.is_relative_to(target.resolve()) or item.file_size>512*1024*1024:raise ValueError('Unsafe archive')
        z.extractall(target)
def doctor(runtime):
    py=runtime/'python/python.exe'
    subprocess.run([str(py),'-I','-c','import sys;from Cryptodome.Cipher import AES;import mutagen;print("Python",sys.version.split()[0],"decoder dependencies OK")'],check=True,timeout=30)
    for name in ('ffmpeg','ffprobe'):
        r=subprocess.run([str(runtime/'ffmpeg'/(name+'.exe')),'-version'],capture_output=True,check=True,timeout=30)
        print(r.stdout.decode('utf-8','replace').splitlines()[0])
def main():
    p=argparse.ArgumentParser();p.add_argument('--asset-dir',default=os.environ.get('LMT_ASSET_DIR'));p.add_argument('--doctor',action='store_true');a=p.parse_args()
    if sys.platform!='win32' or platform.machine().upper() not in ('AMD64','X86_64'):raise ValueError('Windows x64 required')
    manifest_path=ROOT/'decoder-assets.json';manifest=json.loads(manifest_path.read_text('utf-8'));digest=sha(manifest_path)
    parent=ROOT/'.runtime';runtime=parent/'decoder';marker=runtime/'installed.json'
    if marker.exists():
        if json.loads(marker.read_text('utf-8'))['manifestSHA']!=digest:raise ValueError('Runtime manifest changed; use a fresh checkout')
        doctor(runtime);return 0
    if a.doctor:raise ValueError('Run bootstrap-decoder.cmd first')
    if runtime.exists():raise ValueError('Incomplete runtime retained; inspect .runtime/decoder or use a fresh checkout')
    downloads=parent/'downloads';downloads.mkdir(parents=True,exist_ok=True)
    stage=parent/('decoder-stage-'+uuid.uuid4().hex);stage.mkdir()
    receipts=[]
    for asset in manifest['assets']:
        name=asset['name']
        if Path(name).name!=name or not asset['url'].startswith('https://'):raise ValueError('Invalid asset manifest')
        path=downloads/name
        if not path.exists():
            cached=Path(a.asset_dir)/name if a.asset_dir else None
            if cached and cached.is_file():shutil.copyfile(cached,path)
            else:
                tmp=path.with_name(name+'.part')
                with urllib.request.urlopen(asset['url'],timeout=90) as response,tmp.open('wb') as f:shutil.copyfileobj(response,f)
                os.replace(tmp,path)
        if path.stat().st_size!=asset['size'] or sha(path)!=asset['sha256']:raise ValueError('Asset checksum mismatch: '+name)
        receipts.append(asset)
        if name.startswith('python-'):extract(path,stage/'python')
        elif name.endswith('.whl'):extract(path,stage/'python/Lib/site-packages')
        elif name.startswith('ffmpeg-'):
            (stage/'ffmpeg').mkdir()
            with zipfile.ZipFile(path) as z:
                for exe in ('ffmpeg.exe','ffprobe.exe'):
                    matches=[i for i in z.infolist() if i.filename.replace('\\','/').endswith('/bin/'+exe)]
                    if len(matches)!=1 or matches[0].file_size>512*1024*1024:raise ValueError('Ambiguous FFmpeg executable')
                    with z.open(matches[0]) as source,(stage/'ffmpeg'/exe).open('wb') as target:shutil.copyfileobj(source,target)
                for entry in z.infolist():
                    if Path(entry.filename).name.lower() in ('license','license.txt','readme.txt') and entry.file_size<1024*1024:
                        (stage/'ffmpeg'/Path(entry.filename).name).write_bytes(z.read(entry))
        else:raise ValueError('Unknown asset type')
    # Explicit project source path is relative to embedded Python; -I ignores ambient PYTHONPATH.
    (stage/'python/python312._pth').write_text('python312.zip\n.\nLib/site-packages\n../../../src\nimport site\n','ascii')
    doctor(stage)
    (stage/'installed.json').write_text(json.dumps(dict(manifestSHA=digest,assets=receipts,platform='Windows x64'),indent=2),'utf-8')
    # Windows may retain a short-lived executable handle after doctor exits.
    for attempt in range(12):
        try:stage.rename(runtime);break
        except PermissionError:
            if attempt==11:raise
            time.sleep(.25)
    print('Decoder runtime ready. Project-local installation complete.')
    return 0
if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError,KeyError,subprocess.SubprocessError) as e:print('Setup incomplete: '+str(e),file=sys.stderr);raise SystemExit(1)
