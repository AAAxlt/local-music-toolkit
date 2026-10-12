"""Start the click-to-pick editor with configured folders or a folder picker."""
import importlib.util
import json
import tkinter as tk
from tkinter import filedialog
from pathlib import Path
from project_paths import PROJECT_ROOT

config = PROJECT_ROOT / 'config.json'
cfg = json.loads(config.read_text('utf-8-sig')) if config.exists() else {}
library = (PROJECT_ROOT / cfg.get('libraryDir', './library')).resolve()
output = (PROJECT_ROOT / './playlists').resolve()
if not library.exists() or not any(p.suffix.lower() in {'.flac','.ogg','.mp3','.m4a','.wav','.opus'}
                                  for p in library.iterdir() if p.is_file()):
    root = tk.Tk(); root.withdraw()
    selected = filedialog.askdirectory(title='选择你的本地歌曲文件夹')
    root.destroy()
    if not selected:
        raise SystemExit(0)
    library = Path(selected)
spec = importlib.util.spec_from_file_location('editor', PROJECT_ROOT / 'src/playlist-editor.py')
editor = importlib.util.module_from_spec(spec); spec.loader.exec_module(editor)
editor.Editor(library, output).mainloop()
