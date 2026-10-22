"""Single Windows entry for ordinary audio and common encrypted music families."""
import argparse,json,os,sys
from pathlib import Path
from music_decoder import decode
def main():
    parser=argparse.ArgumentParser(description='本地音乐解码：普通音频、NCM、QMC/MGG/MFLAC 及常见已知封装；原件保留，不需要 Frida。')
    parser.add_argument('input',nargs='?');parser.add_argument('--output');parser.add_argument('--cache');parser.add_argument('--qq-cache-root');parser.add_argument('--ffmpeg');parser.add_argument('--report')
    args=parser.parse_args();root=Path(__file__).resolve().parent.parent
    if not args.input:
        if not sys.stdin.isatty():parser.error('Provide an input file/folder')
        args.input=input('请输入文件或文件夹路径（可拖入后按回车）：').strip().strip('"')
    source=Path(args.input).expanduser().absolute();out=Path(args.output).absolute() if args.output else source.parent/(source.stem+'-decoded')
    cache=Path(args.cache).absolute() if args.cache else root/'.cache/decoder-materials'
    ffmpeg=args.ffmpeg or root/'.runtime/decoder/ffmpeg/ffmpeg.exe'
    path=None
    if args.report:
        path=Path(args.report).absolute()
        if path.exists():raise ValueError('Report exists; choose a new path')
        if path.is_relative_to(source if source.is_dir() else source.parent):raise ValueError('Report must stay outside source folder')
    report=decode(source,out,cache,ffmpeg,args.qq_cache_root)
    text=json.dumps(report,ensure_ascii=False,indent=2)
    if path:
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('x',encoding='utf-8') as f:f.write(text+'\n')
    print(text);return 0 if report['ok'] else 1
if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    try:raise SystemExit(main())
    except (ValueError,OSError,RuntimeError) as e:print('Decode incomplete: '+str(e),file=sys.stderr);raise SystemExit(1)
