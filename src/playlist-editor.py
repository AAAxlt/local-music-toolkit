#!/usr/bin/env python3
"""Click-to-pick playlist editor for the organised library (local music toolkit).

Left panel: every song in the library, with a live search box.
Right panel: the playlist being built.
Pick a song, click Add; save writes a standard .m3u8.

The library is never modified; a playlist is only a text list of full paths.
"""
import json
import re
import sys
import unicodedata
from pathlib import Path
from project_paths import PROJECT_ROOT, playlist_reference
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

AUDIO = {'.flac', '.ogg', '.mp3', '.m4a', '.wav', '.opus'}
DEFAULT_LIBRARY = str(PROJECT_ROOT / 'library')
DEFAULT_OUT = str(PROJECT_ROOT / 'playlists')


def norm(s):
    return ''.join(c for c in unicodedata.normalize('NFKC', s or '').casefold() if c.isalnum())


def library_songs(library):
    return sorted(
        (p for p in library.iterdir() if p.is_file() and p.suffix.lower() in AUDIO),
        key=lambda p: p.name.casefold(),
    )


def write_m3u8(path, name, names, library=DEFAULT_LIBRARY, absolute=False):
    lines = ['#EXTM3U', '#PLAYLIST:' + name, '']
    for n in names:
        stem = n.rsplit('.', 1)[0]
        lines.append('#EXTINF:-1,' + stem)
        lines.append(playlist_reference(Path(library) / n, path, absolute))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        f.write('\r\n'.join(lines) + '\r\n')


def parse_m3u8(path):
    """Return (name, [file names]) from an existing playlist."""
    name, names = None, []
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        s = line.strip()
        if not s:
            continue
        if s.upper().startswith('#PLAYLIST:'):
            name = s[10:].strip()
        elif not s.startswith('#'):
            names.append(Path(s.replace('\\', '/')).name)
    return name or path.stem, names


class Editor(tk.Tk):
    def __init__(self, library, out_dir, playlist=None):
        super().__init__()
        self.library = Path(library)
        self.out_dir = Path(out_dir)
        self.songs = library_songs(self.library)
        self.by_name = {p.name: p for p in self.songs}
        self.chosen = []            # ordered list of file names
        self.title('歌单编辑器 — 本地音乐工具')
        self.geometry('1040x680')
        self.minsize(820, 520)
        self._build()
        if playlist and Path(playlist).exists():
            self.load_playlist(Path(playlist))
        else:
            self.name_var.set('我的歌单')
        self.refresh_playlist()

    # ---------- layout ----------
    def _build(self):
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill='x')
        ttk.Label(top, text='歌单名：').pack(side='left')
        self.name_var = tk.StringVar(value='我的歌单')
        ttk.Entry(top, textvariable=self.name_var, width=28).pack(side='left')
        ttk.Button(top, text='保存歌单', command=self.save).pack(side='left', padx=6)
        ttk.Button(top, text='打开歌单…', command=self.open_dialog).pack(side='left')
        ttk.Button(top, text='新建（清空）', command=self.new).pack(side='left', padx=6)
        self.absolute_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text='使用绝对路径', variable=self.absolute_var).pack(side='left')
        self.status = ttk.Label(top, text='')
        self.status.pack(side='right')

        body = ttk.Frame(self, padding=(10, 0, 10, 10))
        body.pack(fill='both', expand=True)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(2, weight=2)
        body.rowconfigure(1, weight=1)

        # --- left: library ---
        ttk.Label(body, text='曲库（双击加入）').grid(row=0, column=0, sticky='w')
        left = ttk.Frame(body)
        left.grid(row=1, column=0, sticky='nsew')
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)
        self.search_var = tk.StringVar()
        self.search_var.trace_add('write', lambda *_: self.fill_library())
        ttk.Entry(left, textvariable=self.search_var).grid(row=0, column=0, sticky='ew', pady=(0, 4))
        self.lib_box = tk.Listbox(left, selectmode='extended', activestyle='none',
                                  exportselection=False)
        self.lib_box.grid(row=1, column=0, sticky='nsew')
        lib_sb = ttk.Scrollbar(left, orient='vertical', command=self.lib_box.yview)
        lib_sb.grid(row=1, column=1, sticky='ns')
        self.lib_box.configure(yscrollcommand=lib_sb.set)
        self.lib_box.bind('<Double-Button-1>', lambda e: self.add_selected())

        # --- middle: buttons ---
        mid = ttk.Frame(body)
        mid.grid(row=1, column=1, padx=10)
        ttk.Button(mid, text='加入 →', command=self.add_selected).pack(pady=4)
        ttk.Button(mid, text='← 移除', command=self.remove_selected).pack(pady=4)

        # --- right: playlist ---
        ttk.Label(body, text='当前歌单（双击移除）').grid(row=0, column=2, sticky='w')
        right = ttk.Frame(body)
        right.grid(row=1, column=2, sticky='nsew')
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)
        self.updown = ttk.Frame(right)
        self.updown.grid(row=0, column=0, sticky='ew', pady=(0, 4))
        ttk.Button(self.updown, text='上移', command=lambda: self.move(-1)).pack(side='left')
        ttk.Button(self.updown, text='下移', command=lambda: self.move(1)).pack(side='left', padx=4)
        self.pl_box = tk.Listbox(right, selectmode='extended', activestyle='none',
                                 exportselection=False)
        self.pl_box.grid(row=1, column=0, sticky='nsew')
        pl_sb = ttk.Scrollbar(right, orient='vertical', command=self.pl_box.yview)
        pl_sb.grid(row=1, column=1, sticky='ns')
        self.pl_box.configure(yscrollcommand=pl_sb.set)
        self.pl_box.bind('<Double-Button-1>', lambda e: self.remove_selected())

        self.bind('<Return>', lambda e: self.add_selected())
        self.bind('<Delete>', lambda e: self.remove_selected())
        self.bind('<Control-s>', lambda e: self.save())
        self.fill_library()

    # ---------- data ----------
    def fill_library(self):
        q = norm(self.search_var.get())
        self.lib_box.delete(0, 'end')
        self._lib_names = []
        for p in self.songs:
            if q and q not in norm(p.name):
                continue
            self.lib_box.insert('end', p.name)
            self._lib_names.append(p.name)
        self.status.config(text=f'曲库 {len(self._lib_names)}/{len(self.songs)} 首 · 歌单 {len(self.chosen)} 首')

    def refresh_playlist(self):
        self.pl_box.delete(0, 'end')
        for i, n in enumerate(self.chosen, 1):
            self.pl_box.insert('end', f'{i}. {n}')
        self.status.config(text=f'曲库 {len(getattr(self, "_lib_names", []))}/{len(self.songs)} 首 · 歌单 {len(self.chosen)} 首')

    def add_selected(self):
        picks = [self._lib_names[i] for i in self.lib_box.curselection()]
        added = 0
        for n in picks:
            if n not in self.chosen:
                self.chosen.append(n)
                added += 1
        if added:
            self.refresh_playlist()

    def remove_selected(self):
        idx = sorted(self.pl_box.curselection(), reverse=True)
        for i in idx:
            del self.chosen[i]
        if idx:
            self.refresh_playlist()

    def move(self, delta):
        sel = list(self.pl_box.curselection())
        if not sel:
            return
        order = sel if delta < 0 else list(reversed(sel))
        for i in order:
            j = i + delta
            if 0 <= j < len(self.chosen):
                self.chosen[i], self.chosen[j] = self.chosen[j], self.chosen[i]
        self.refresh_playlist()
        for i in sel:
            j = i + delta
            if 0 <= j < len(self.chosen):
                self.pl_box.selection_set(j)

    # ---------- file ops ----------
    def new(self):
        if self.chosen and not messagebox.askyesno('新建', '清空当前歌单？'):
            return
        self.chosen = []
        self.name_var.set('我的歌单')
        self.refresh_playlist()

    def save(self):
        name = self.name_var.get().strip() or '我的歌单'
        if not self.chosen:
            messagebox.showwarning('保存', '歌单是空的，先加入一些歌。')
            return
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .') or 'playlist'
        out = self.out_dir / (safe + '.m3u8')
        if out.exists() and not messagebox.askyesno('覆盖', f'{out.name} 已存在，覆盖？'):
            return
        write_m3u8(out, name, self.chosen, self.library, self.absolute_var.get())
        messagebox.showinfo('已保存', f'{len(self.chosen)} 首\n{out}\n\n可用支持 M3U8 的播放器打开；歌曲文件没有复制。')

    def open_dialog(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        dlg = tk.Toplevel(self)
        dlg.title('打开歌单')
        dlg.geometry('520x420')
        dlg.transient(self)
        files = sorted(self.out_dir.glob('*.m3u8'))
        tk.Label(dlg, text=f'{self.out_dir}').pack(anchor='w', padx=8, pady=6)
        box = tk.Listbox(dlg)
        box.pack(fill='both', expand=True, padx=8)
        for f in files:
            box.insert('end', f.name)

        def take():
            sel = box.curselection()
            if not sel:
                return
            self.load_playlist(files[sel[0]])
            dlg.destroy()
        box.bind('<Double-Button-1>', lambda e: take())
        ttk.Button(dlg, text='打开', command=take).pack(pady=8)

    def load_playlist(self, path):
        name, names = parse_m3u8(path)
        known, missing = [], []
        for n in names:
            if n in self.by_name:
                if n not in known:
                    known.append(n)
            else:
                missing.append(n)
        self.chosen = known
        self.name_var.set(name)
        self.refresh_playlist()
        if missing:
            messagebox.showwarning('打开歌单',
                                   f'{len(missing)} 首不在当前曲库，已跳过：\n' +
                                   '\n'.join(missing[:8]))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    auto_close = next((float(a.split('=', 1)[1]) for a in sys.argv[1:]
                       if a.startswith('--auto-close=')), None)
    lib = args[0] if len(args) > 0 else DEFAULT_LIBRARY
    out = args[1] if len(args) > 1 else DEFAULT_OUT
    playlist = args[2] if len(args) > 2 else None
    if not Path(lib).is_dir():
        root = tk.Tk(); root.withdraw()
        messagebox.showerror('歌单编辑器', '找不到曲库目录：\n' + lib)
        return
    app = Editor(lib, out, playlist)
    if auto_close:
        app.after(int(auto_close * 1000), app.destroy)
    app.mainloop()


if __name__ == '__main__':
    main()
