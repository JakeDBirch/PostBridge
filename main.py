import os
import sys
import json
import threading
import queue
import tempfile
import time
from concurrent.futures import (ThreadPoolExecutor,
                                wait as _fut_wait, FIRST_COMPLETED)

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# Check for dependencies
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False

try:
    from faster_whisper import WhisperModel
    HAS_WHISPER = True
except ImportError:
    HAS_WHISPER = False

try:
    import aaf2
    HAS_AAF = True
except ImportError:
    HAS_AAF = False

# Import our custom modules!
from config import *
from config import _SANS
from utils import basename, secs_tc, tc_secs, is_video, is_audio, is_media, MEDIA_EXTS, parse_dnd
from parsers import (parse_script, parse_pt_session_text, dedupe_pt_tracks,
                     match_pt_clip_to_media, get_clip_base_name,
                     parse_aaf_session, match_source_to_video,
                     detect_sync_offset, detect_slate_offset)
from gui_components import VoBin, MediaPool, _SlimScrollbar, _FlatDropdown, _FlatProgressBar
import engines

class App(TkinterDnD.Tk if HAS_DND else tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PostBridge")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(820, 600)
        self._center(920, 800)

        self._init_styles()

        # State
        self.workflow   = None   
        self.tokens     = []
        self.parts      = []
        self.pulls      = []
        self.vo_blocks  = []
        self.doc_title  = ""
        self.bins       = {}    
        self.vo_bins    = {}    
        self.results    = []    
        self._cancel    = threading.Event()
        self._aaf_data  = None

        self.seq_name   = tk.StringVar()
        self.out_path   = tk.StringVar()
        self.gap_var    = tk.DoubleVar(value=DEFAULT_GAP)
        self.pad_var    = tk.IntVar(value=PAD_SECS)

        self._header()
        self.body = tk.Frame(self, bg=BG)
        self.body.pack(fill="both", expand=True, padx=36, pady=(0,28))
        self._home()

    def _center(self, w, h):
        self.update_idletasks()
        x = (self.winfo_screenwidth()  - w) // 2
        y = (self.winfo_screenheight() - h) // 2
        self.geometry("{}x{}+{}+{}".format(w, h, x, y))

    def _init_styles(self):
        """Configure ttk styles for dark-theme widgets."""
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            "Dark.TCombobox",
            fieldbackground=SURF2,
            background=SURF2,
            foreground=TEXT,
            bordercolor=BORDER,
            arrowcolor=TEXT,
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", SURF2), ("!disabled", SURF2)],
            foreground=[("readonly", TEXT), ("!disabled", TEXT)],
            arrowcolor=[("readonly", TEXT), ("!disabled", TEXT)],
        )
        # Modern slim scrollbars (dark theme, no chunky 90s look)
        style.configure(
            "Dark.Vertical.TScrollbar",
            background=SURF2,
            troughcolor=SURF3,
            bordercolor=BORDER,
            arrowcolor=SUB,
            width=10,
        )
        style.configure(
            "Dark.Horizontal.TScrollbar",
            background=SURF2,
            troughcolor=SURF3,
            bordercolor=BORDER,
            arrowcolor=SUB,
        )
        style.map(
            "Dark.Vertical.TScrollbar",
            background=[("active", SURF2), ("pressed", ACCENT)],
        )
        style.map(
            "Dark.Horizontal.TScrollbar",
            background=[("active", SURF2), ("pressed", ACCENT)],
        )

    def _header(self):
        tk.Frame(self, bg=ACCENT, height=4).pack(fill="x")
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=36, pady=(20,10))
        tk.Label(bar, text="POSTBRIDGE", font=FH, bg=BG, fg=TEXT).pack(side="left")
        tk.Label(bar, text="  ·  ", font=FS, bg=BG, fg=ACCENT).pack(side="left", pady=(8,0))
        tk.Label(bar, text="audio/video post-production bridge", font=FS, bg=BG, fg=SUB).pack(side="left", pady=(8,0))
        # MeatEater brand: logo if present, else text
        self._logo_photo = None
        _script_dir = os.path.dirname(os.path.abspath(__file__))
        _assets = os.path.join(_script_dir, "assets")
        for _name in ("meateater_logo.png", "channels4_profile-d0f26706-7f7c-46be-b8be-cd47fd3401bf.png"):
            _path = os.path.join(_assets, _name)
            if os.path.isfile(_path):
                try:
                    from tkinter import PhotoImage
                    self._logo_photo = PhotoImage(file=_path)
                    # Scale to a reasonable header size (e.g. 36px tall)
                    _h = self._logo_photo.height()
                    if _h > 36:
                        div = max(1, _h // 36)
                        self._logo_photo = self._logo_photo.subsample(div, div)
                    tk.Label(bar, image=self._logo_photo, bg=BG).pack(side="right", padx=(12,0), pady=(4,0))
                    break
                except Exception:
                    pass
        if self._logo_photo is None:
            tk.Label(bar, text="MEATEATER", font=("Courier New", 9, "bold"),
                     bg=BG, fg=ACCENT).pack(side="right", pady=(8,0))
        warnings = []
        if not HAS_WHISPER:
            warnings.append("pip install faster-whisper")
        if not HAS_DND:
            warnings.append("pip install tkinterdnd2")
        if not HAS_AAF:
            warnings.append("pip install pyaaf2")
        if warnings:
            tk.Label(bar, text="  [" + "  ·  ".join(warnings) + "]",
                     font=("Courier New",10), bg=BG, fg=WARN).pack(side="right")
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", padx=36)

    def _clear(self):
        self.unbind_all("<MouseWheel>")
        for w in self.body.winfo_children(): w.destroy()

    def _home(self):
        self._clear()
        tk.Frame(self.body, bg=BG, height=30).pack()

        tk.Label(self.body, text="Choose a workflow",
                 font=FL, bg=BG, fg=SUB).pack(pady=(0,20))

        cards_frame = tk.Frame(self.body, bg=BG)
        cards_frame.pack(fill="x", padx=20)

        workflows = [
            ("Script \u2192 AAF",
             "script_aaf",
             "Whisper-reconcile a Blood Trails script, then export AAF for Pro Tools. "
             "Skip Premiere entirely.",
             HAS_WHISPER and HAS_AAF),
            ("Script \u2192 XML",
             "script_xml",
             "Whisper-reconcile a Blood Trails script, then export Premiere XML. "
             "Original reconcile workflow.",
             HAS_WHISPER),
            ("AAF \u2192 XML",
             "pt_xml",
             "Parse a Pro Tools AAF export, match clips to video files by source name, "
             "generate Premiere XML for the editor.  Requires pyaaf2.",
             HAS_AAF),
        ]

        _TITLE_FONT = (_SANS, 15, "bold")
        _DESC_FONT  = (_SANS, 11)
        _ARR_FONT   = (_SANS, 20, "bold")
        hover_bg    = "#303030"

        for title, wf_key, desc, available in workflows:
            card = tk.Frame(cards_frame, bg=SURF,
                            highlightbackground=BORDER, highlightthickness=1)
            card.pack(fill="x", pady=(0,10))

            inner = tk.Frame(card, bg=SURF)
            inner.pack(fill="x", padx=24, pady=18)

            left = tk.Frame(inner, bg=SURF)
            left.pack(side="left", fill="both", expand=True)

            title_lbl = tk.Label(left, text=title, font=_TITLE_FONT,
                                 bg=SURF, fg=TEXT if available else SUB, anchor="w")
            title_lbl.pack(anchor="w")

            desc_lbl = tk.Label(left, text=desc, font=_DESC_FONT,
                                bg=SURF, fg=SUB, justify="left",
                                wraplength=640, anchor="w")
            desc_lbl.pack(anchor="w", pady=(5,0))

            arr_lbl = tk.Label(inner, text="\u2192", font=_ARR_FONT,
                               bg=SURF, fg=BORDER, padx=8)
            arr_lbl.pack(side="right", anchor="center")

            all_widgets = [card, inner, left, title_lbl, desc_lbl, arr_lbl]

            if not available:
                dep_lbl = tk.Label(left,
                                   text="\u26a0  missing dependencies \u2014 see header",
                                   font=_DESC_FONT, bg=SURF, fg=ERR, anchor="w")
                dep_lbl.pack(anchor="w", pady=(4,0))
                all_widgets.append(dep_lbl)
            else:
                def _enter(e, ws=all_widgets, c=card, a=arr_lbl):
                    for w in ws: w.config(bg=hover_bg)
                    c.config(highlightbackground=ACCENT)
                    a.config(fg=ACCENT)

                def _leave(e, ws=all_widgets, c=card, a=arr_lbl):
                    for w in ws: w.config(bg=SURF)
                    c.config(highlightbackground=BORDER)
                    a.config(fg=BORDER)

                def _click(e, k=wf_key):
                    self._select_workflow(k)

                for w in all_widgets:
                    w.bind("<Enter>", _enter)
                    w.bind("<Leave>", _leave)
                    w.bind("<Button-1>", _click)


    def _select_workflow(self, key):
        self.workflow = key
        if key in ("script_aaf", "script_xml"):
            self._step1()
        elif key == "pt_xml":
            self._aaf_step1()

    def _tooltip(self, widget, text):
        """Attach a hover tooltip showing full text to any widget."""
        tip = [None]
        def _show(e):
            tip[0] = tk.Toplevel(widget)
            tip[0].overrideredirect(True)
            tip[0].geometry("+{}+{}".format(e.x_root + 14, e.y_root + 14))
            tk.Label(tip[0], text=text, font=FB,
                     bg=SURF3, fg=TEXT, padx=10, pady=5,
                     bd=0, highlightbackground=BORDER,
                     highlightthickness=1).pack()
        def _hide(e):
            if tip[0]:
                tip[0].destroy()
                tip[0] = None
        widget.bind("<Enter>", _show, add="+")
        widget.bind("<Leave>", _hide, add="+")

    def _btn(self, parent, label, cmd, small=False, color=None):
        bg  = color or SURF3
        pad = (6,3) if small else (14,7)
        w   = tk.Label(parent, text=label,
                       font=FB if small else FBT,
                       bg=bg, fg=TEXT,
                       cursor="arrow", padx=pad[0], pady=pad[1],
                       bd=0, highlightbackground=BORDER, highlightthickness=1)
        w.bind("<Enter>", lambda e, w=w: w.config(bg=ACCENT, fg=TEXT))
        w.bind("<Leave>", lambda e, w=w, c=bg: w.config(bg=c, fg=TEXT))
        def _press(e, w=w, f=cmd):
            # Immediate visual feedback: darken to a pressed shade, then restore.
            w.config(bg="#a84400")
            w.update_idletasks()  # flush render so the color actually shows
            def _restore():
                try: w.config(bg=ACCENT)   # cursor likely still over the button
                except Exception: pass
            w.after(130, _restore)
            f()
        w.bind("<Button-1>", _press)
        return w

    def _section(self, text):
        tk.Label(self.body, text=text, font=FL, bg=BG,
                 fg=ACCENT).pack(anchor="w", pady=(16,4))

    def _scroll_frame(self, parent, height=420):
        outer  = tk.Frame(parent, bg=BG)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=BG, bd=0, highlightthickness=0)
        sb     = _SlimScrollbar(outer, command=canvas.yview)
        sf     = tk.Frame(canvas, bg=BG)
        sf.bind("<Configure>",
                lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(win_id, width=e.width))
        win_id = canvas.create_window((0,0), window=sf, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        def _on_wheel(e):
            # Don't scroll when focus is in a popup (e.g. combobox dropdown)
            w = self.focus_get()
            if w is not None and w.winfo_toplevel() != self:
                return
            # macOS uses delta 1/-1 per notch; Windows uses 120/-120
            units = int(-1 * e.delta) if sys.platform == "darwin" else int(-1 * (e.delta / 120))
            canvas.yview_scroll(units, "units")

        self.bind_all("<MouseWheel>", _on_wheel)
        return sf

    def _make_prefetch_panel(self, parent, paths_list,
                             title="MEDIA FILES",
                             hint="Drop audio/video files here — they'll be pre-loaded into Step 2  (optional)"):
        """
        Compact file-add panel that appends validated media paths to paths_list.
        Supports DnD, Browse Files, and Browse Folder.
        Returns (panel_frame, file_list_frame, count_label).
        """
        panel = tk.Frame(parent, bg=SURF,
                         highlightbackground=BORDER, highlightthickness=1)
        panel.pack(fill="x", pady=(10, 0))

        hdr = tk.Frame(panel, bg=SURF)
        hdr.pack(fill="x", padx=12, pady=(8, 2))
        tk.Label(hdr, text=title, font=FL, bg=SURF, fg=ACCENT).pack(side="left")
        count_lbl = tk.Label(hdr, text="0 files", font=FB, bg=SURF, fg=SUB)
        count_lbl.pack(side="right")

        tk.Label(panel, text=hint, font=FB, bg=SURF, fg=SUB,
                 wraplength=700, justify="left").pack(padx=12, anchor="w", pady=(0, 4))

        file_list = tk.Frame(panel, bg=SURF)
        file_list.pack(fill="x", padx=12)

        def _update_count():
            n = len(paths_list)
            count_lbl.config(text="{} file{}".format(n, "s" if n != 1 else ""))

        def _add_media(raw):
            paths = parse_dnd(raw) if isinstance(raw, str) else raw
            for p in paths:
                p = str(p).strip().strip("{}")
                if not p or not os.path.isfile(p): continue
                if os.path.basename(p).startswith("._"): continue
                if not is_media(p): continue
                if p in paths_list: continue
                paths_list.append(p)
                is_v = is_video(p)
                row = tk.Frame(file_list, bg=SURF2,
                               highlightbackground=BORDER, highlightthickness=1)
                row.pack(fill="x", pady=1)
                tk.Label(row, text="VIDEO" if is_v else "AUDIO",
                         font=("Courier New", 9, "bold"),
                         bg="#2a3d2a" if is_v else "#1e2a3a",
                         fg=SUCCESS if is_v else "#7a9fff",
                         padx=5, pady=2).pack(side="left")
                tk.Label(row, text=basename(p), font=FB, bg=SURF2, fg=TEXT,
                         padx=6, anchor="w").pack(side="left", fill="x", expand=True)
                rm = tk.Label(row, text=" × ", font=FB, bg=SURF2, fg=SUB,
                              cursor="hand2", padx=4)
                rm.pack(side="right")
                def _rm(e, p=p, r=row):
                    if p in paths_list: paths_list.remove(p)
                    r.destroy()
                    _update_count()
                rm.bind("<Button-1>", _rm)
            _update_count()

        btn_row = tk.Frame(panel, bg=SURF)
        btn_row.pack(anchor="w", padx=12, pady=(4, 10))

        def _browse_f():
            ext_glob = " ".join("*" + e for e in sorted(MEDIA_EXTS))
            ps = filedialog.askopenfilenames(
                title="Select audio/video files",
                filetypes=[("Media", ext_glob), ("All files", "*.*")])
            if ps: _add_media(list(ps))

        def _browse_d():
            folder = filedialog.askdirectory(title="Select media folder")
            if not folder: return
            ps = []
            for root, _, files in os.walk(folder):
                for fn in sorted(files):
                    fp = os.path.join(root, fn)
                    if is_media(fp): ps.append(fp)
            _add_media(ps)

        self._btn(btn_row, "+ BROWSE FILES",  _browse_f, small=True).pack(side="left", padx=(0, 8))
        self._btn(btn_row, "+ BROWSE FOLDER", _browse_d, small=True).pack(side="left")

        if HAS_DND:
            panel.drop_target_register(DND_FILES)
            panel.dnd_bind("<<Drop>>", lambda e: _add_media(e.data))

        return panel, file_list, count_lbl

    def _step1(self):
        self._clear()
        self._prefetch_media     = []     # reset on fresh entry
        self._step1_next_added   = False
        wf_label = "AAF" if self.workflow == "script_aaf" else "XML"
        self._section("STEP 1 — LOAD SCRIPT  (Script → {})".format(wf_label))

        card = tk.Frame(self.body, bg=SURF,
                        highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x")
        if HAS_DND:
            card.drop_target_register(DND_FILES)
            card.dnd_bind("<<Drop>>",
                          lambda e: self._load_script(e.data.strip().strip("{}")))

        inner = tk.Frame(card, bg=SURF)
        inner.pack(padx=40, pady=30)
        self._s1 = tk.Label(inner,
                             text="Drop script here  or  click below" if HAS_DND
                                  else "Click below to open script",
                             font=FB, bg=SURF, fg=SUB,
                             wraplength=720, justify="center")
        self._s1.pack(pady=(0,14))
        self._btn(inner, "OPEN SCRIPT (.txt)",
                  lambda: self._load_script(
                      filedialog.askopenfilename(
                          filetypes=[("Text","*.txt"),("All","*.*")]))).pack()

        # ── Media pre-load (optional — populates Step 2 automatically) ────────
        self._make_prefetch_panel(
            self.body, self._prefetch_media,
            title="MEDIA FILES",
            hint="Drop audio/video files here — they will be pre-loaded into Step 2  (optional)",
        )

        nav = tk.Frame(self.body, bg=BG)
        nav.pack(fill="x", pady=(8,0))
        self._btn(nav, "← HOME", self._home).pack(side="left")
        self._step1_nav = nav   # NEXT button appended here once script is loaded

    def _load_script(self, path):
        if not path or not os.path.isfile(path): return
        self._script_path = os.path.abspath(path)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception as e:
            messagebox.showerror("Error", str(e)); return

        tokens, parts, pulls, vo_blocks, doc_title, warnings = parse_script(text)

        if not pulls:
            messagebox.showerror("Nothing Found",
                "No @PULL markers found.\n"
                "Make sure the script is in Blood Trails format.")
            return

        no_quote = sum(1 for p in pulls if not p["quote_text"])
        self.tokens    = tokens
        self.parts     = parts
        self.pulls     = pulls
        self.vo_blocks = vo_blocks
        self.doc_title = doc_title

        # Set cache dir next to the script file inside the engines module
        engines._cache_dir = os.path.join(os.path.dirname(os.path.abspath(path)), ".bt_cache")
        
        self.seq_name.set("{} (AUTO)".format(doc_title))

        self._s1.config(
            text="✓  {}  ·  {} pulls  ·  {} parts  ·  {} tokens: {}{}".format(
                doc_title, len(pulls), len(parts), len(tokens),
                "  ".join(tokens),
                "  ·  {} pulls missing quote text".format(no_quote) if no_quote else ""),
            fg=SUCCESS if not no_quote else WARN)

        if warnings:
            messagebox.showwarning("Warnings", "\n".join(warnings[:10]))
        # Add NEXT button to the step 1 nav bar (once, even if script is reloaded)
        if hasattr(self, "_step1_nav") and not getattr(self, "_step1_next_added", False):
            self._btn(self._step1_nav, "NEXT  →  ASSIGN MEDIA",
                      self._step2, color=ACCENT).pack(side="right")
            self._step1_next_added = True
        elif not hasattr(self, "_step1_nav"):
            self.after(350, self._step2)

    def _step2(self):
        self._clear()
        self.bins    = {}
        self.vo_bins = {}
        self._section("STEP 2 — ASSIGN MEDIA")
        tk.Label(self.body,
                 text="Drop all episode files at once — or browse a folder.  "
                      "The app guesses speaker assignments from Riverside filenames.  "
                      "Adjust any incorrect assignments via the dropdown on each row.",
                 font=FB, bg=BG, fg=SUB, wraplength=860).pack(anchor="w", pady=(0,10))

        sf = self._scroll_frame(self.body, height=500)

        self._pool = MediaPool(sf, self.tokens, self.parts,
                              aaf_mode=(self.workflow == "script_aaf"))
        self._pool.pack(fill="x", pady=(0,10), padx=2)

        # Pre-load any files dropped at Step 1
        for p in getattr(self, "_prefetch_media", []):
            self._pool._add(p)

        # Restore setup if user came back via "Redo" from step 4
        if getattr(self, "_redo_setup", None):
            redo = self._redo_setup
            for path, token in redo.get("assignments", []):
                if os.path.isfile(path):
                    self._pool._add(path)
                    self._pool._rows[-1]["var"].set(token)
            for tok, sp in redo.get("transcript_sources", {}).items():
                if tok in self._pool._src_vars and sp:
                    self._pool._src_vars[tok].set(basename(sp))
            self._pool._rebuild_src_dropdowns()
            self._pool._refresh_count()
            del self._redo_setup

        nav = tk.Frame(self.body, bg=BG); nav.pack(fill="x", pady=(8,0))
        self._btn(nav, "← BACK",         self._step1).pack(side="left")
        self._btn(nav, "SAVE SETUP",     self._save_setup).pack(side="left", padx=(8,0))
        self._btn(nav, "LOAD SETUP",     self._load_setup).pack(side="left", padx=(4,0))
        self._btn(nav, "⟳ REFRESH POOL", self._refresh_pool).pack(side="left", padx=(4,0))
        self._btn(nav, "RECONCILE  →",   self._start_reconcile,
                  color=ACCENT).pack(side="right")
        # Cache utilities on a separate right-aligned row so they don't crowd RECONCILE
        cache_row = tk.Frame(self.body, bg=BG); cache_row.pack(fill="x", pady=(4,0))
        self._btn(cache_row, "CLEAR TRANSCRIPTS", lambda: self._clear_cache("transcripts"),
                  small=True).pack(side="right", padx=(0, 4))
        self._btn(cache_row, "CLEAR RESULTS", lambda: self._clear_cache("results"),
                  small=True).pack(side="right", padx=(0, 8))

    def _setup_sidecar_path(self):
        if not hasattr(self, '_script_path') or not self._script_path:
            return None
        base = os.path.splitext(self._script_path)[0]
        return base + "_setup.json"

    def _save_setup(self):
        src_data = {}
        for tok in self.tokens:
            sp = self._pool.get_transcript_source(tok) if self._pool else None
            if sp: src_data[tok] = sp

        data = {
            "version":   1,
            "script":    getattr(self, "_script_path", ""),
            "assignments": {r["path"]: r["var"].get()
                            for r in self._pool._rows} if self._pool else {},
            "transcript_sources": src_data,
        }
        sidecar = self._setup_sidecar_path()
        init_dir  = os.path.dirname(sidecar) if sidecar else ""
        init_file = os.path.basename(sidecar) if sidecar else "setup.json"
        path = filedialog.asksaveasfilename(
            title="Save setup",
            defaultextension=".json",
            filetypes=[("JSON","*.json")],
            initialdir=init_dir,
            initialfile=init_file)
        if not path: return
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        messagebox.showinfo("Saved", "Setup saved to:\n{}".format(path))

    def _load_setup(self):
        path = filedialog.askopenfilename(
            title="Load Setup — choose a saved JSON file",
            filetypes=[("JSON setup files", "*.json"), ("All files", "*.*")])
        if not path or not os.path.exists(path): return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            messagebox.showerror("Load failed", str(e)); return

        assignments = data.get("assignments", {})
        missing = []
        for fpath, token in assignments.items():
            if os.path.exists(fpath):
                self._pool._add(fpath)
            else:
                missing.append(fpath)
        for r in self._pool._rows:
            saved_tok = assignments.get(r["path"])
            if saved_tok:
                r["var"].set(saved_tok)
        src_data = data.get("transcript_sources", {})
        for tok, sp in src_data.items():
            if tok in self._pool._src_vars and basename(sp) in [
                basename(p) for p in self._pool.get_interview_assets().get(tok, [])
                if not is_video(p)
            ]:
                self._pool._src_vars[tok].set(basename(sp))
        self._pool._rebuild_src_dropdowns()
        self._pool._refresh_count()
        if missing:
            messagebox.showwarning("Missing files",
                "{} file(s) from the saved setup were not found:\n{}".format(
                    len(missing), "\n".join(basename(p) for p in missing[:5])))

    def _clear_cache(self, kind="all"):
        """Delete cached files from the .bt_cache directory next to the script.

        kind="transcripts" — Whisper word-list files  ({md5}.json)
        kind="results"     — pull reconciliation files (pull_{md5}.json)
        kind="all"         — everything
        """
        cache_dir = engines._cache_dir
        if not cache_dir or not os.path.isdir(cache_dir):
            messagebox.showinfo("Clear Cache", "No cache found — nothing to clear.")
            return

        all_files = [f for f in os.listdir(cache_dir) if f.endswith(".json")]

        if kind == "transcripts":
            targets  = [f for f in all_files if not f.startswith("pull_")]
            label    = "Whisper transcript"
            warning  = "Re-transcription will be needed on next reconcile."
        elif kind == "results":
            targets  = [f for f in all_files if f.startswith("pull_")]
            label    = "reconciliation result"
            warning  = "Pulls will be re-matched on next reconcile (transcripts kept)."
        else:
            targets  = all_files
            label    = "cache"
            warning  = "Full re-transcription and re-matching on next reconcile."

        if not targets:
            messagebox.showinfo("Clear Cache",
                                "No {} files found.".format(label))
            return

        if not messagebox.askyesno(
                "Clear Cache",
                "Delete {} {} file{}?\n\n{}".format(
                    len(targets), label, "s" if len(targets) != 1 else "",
                    warning)):
            return

        removed = 0
        for fname in targets:
            try:
                os.unlink(os.path.join(cache_dir, fname))
                removed += 1
            except Exception:
                pass

        messagebox.showinfo("Clear Cache",
                            "Cleared {} file{}.".format(
                                removed, "s" if removed != 1 else ""))

    def _refresh_pool(self):
        """Sync pool with disk: remove missing entries and add any new files
        found in the same directories as existing pool entries."""
        if not self._pool:
            return

        MEDIA_EXTS = {
            # Professional / camera formats
            ".mp4", ".mov", ".mxf", ".avi", ".mkv", ".m4v", ".webm",
            # Broadcast / studio audio
            ".wav", ".aif", ".aiff",
            # Consumer / archival audio
            ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus",
        }

        # Collect source directories BEFORE removing anything so we don't
        # lose track of folders if all their files happened to be stale.
        source_dirs = {os.path.dirname(r["path"]) for r in self._pool._rows}

        # ── 1. Remove stale entries ───────────────────────────────────────
        stale = [r for r in self._pool._rows if not os.path.isfile(r["path"])]
        for rec in stale:
            self._pool._remove(rec)

        # ── 2. Add new files from the same directories ────────────────────
        added = 0
        for d in sorted(source_dirs):
            if not os.path.isdir(d):
                continue
            for fn in sorted(os.listdir(d)):
                if os.path.splitext(fn)[1].lower() not in MEDIA_EXTS:
                    continue
                fp = os.path.join(d, fn)
                if not os.path.isfile(fp):
                    continue
                before = len(self._pool._rows)
                self._pool._add(fp)          # _add() deduplicates & auto-assigns
                if len(self._pool._rows) > before:
                    added += 1

        # ── 3. For AAF, prune video rows that now have audio counterparts ────
        pruned = 0
        if self.workflow == "script_aaf":
            pruned = self._pool.prune_redundant_videos()

        # ── 4. Report ─────────────────────────────────────────────────────
        parts = []
        if stale:
            parts.append("Removed {} missing file{}.".format(
                len(stale), "s" if len(stale) != 1 else ""))
        if added:
            parts.append("Added {} new file{}.".format(
                added, "s" if added != 1 else ""))
        if pruned:
            parts.append("Removed {} redundant video file{} (audio present).".format(
                pruned, "s" if pruned != 1 else ""))
        if not parts:
            messagebox.showinfo("Pool Refresh",
                                "Pool is current — nothing to add or remove.")
        else:
            messagebox.showinfo("Pool Refresh", "\n".join(parts))

    def _warn_large_windows(self, suspicious):
        """Pre-flight modal for pulls whose extraction window exceeds MAX_EXTRACT_S.

        Per-pull controls:
          • Editable IN and OUT timecodes — corrections are written back to the
            pull dict before reconcile starts so Whisper uses the fixed window.
          • IGNORE toggle — removes the pull from self.pulls entirely so it is
            skipped during reconcile (it will not appear in the output).

        Returns True to continue (edits/ignores applied), False to go back.
        """
        result = {"proceed": False}
        rows   = []   # one dict per suspicious pull, holds all widget refs

        dlg = tk.Toplevel(self)
        dlg.title("Large Pull Windows Detected")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.grab_set()

        # ── Amber header bar ──────────────────────────────────────────────────
        hdr = tk.Frame(dlg, bg=WARN, padx=16, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="⚠   Large Pull Windows Detected",
                 font=FBT, bg=WARN, fg="#1c1c1c").pack(anchor="w")

        # ── Explanation ───────────────────────────────────────────────────────
        body = tk.Frame(dlg, bg=BG, padx=20, pady=14)
        body.pack(fill="both", expand=True)

        cap_tc = secs_tc(MAX_EXTRACT_S)
        n    = len(suspicious)
        noun = "pull" if n == 1 else "pulls"
        tk.Label(
            body,
            text=(
                "{} {} below {} an extraction window larger than {:.0f}s ({}).\n"
                "This usually means a sentinel out-point (e.g. 99:59:59) was left\n"
                "in the script by mistake.  Fix the timecodes or ignore the clip."
            ).format(n, noun, "has" if n == 1 else "have", MAX_EXTRACT_S, cap_tc),
            font=FB, bg=BG, fg=TEXT, justify="left",
        ).pack(anchor="w", pady=(0, 12))

        # ── One editable row per suspicious pull ──────────────────────────────
        for p in suspicious:
            window_s = p["out_seconds"] - p["in_seconds"]

            rf = tk.Frame(body, bg=SURF, padx=12, pady=10,
                          highlightbackground=WARN, highlightthickness=1)
            rf.pack(fill="x", pady=3)

            # Top line: token + window size + IGNORE toggle
            top = tk.Frame(rf, bg=SURF)
            top.pack(fill="x")

            ignore_var = tk.BooleanVar(value=False)
            ignore_lbl = tk.Label(top, text="IGNORE", font=FS,
                                  bg=SURF, fg=SUB, cursor="hand2")
            ignore_lbl.pack(side="right")

            tk.Label(top, text=p["token"],
                     font=FL, bg=SURF, fg=WARN).pack(side="left")
            tk.Label(top, text="   ({:.0f}s window)".format(window_s),
                     font=FB, bg=SURF, fg=SUB).pack(side="left")

            # Editable IN / OUT entries
            tc_row = tk.Frame(rf, bg=SURF)
            tc_row.pack(fill="x", pady=(8, 0))

            in_var  = tk.StringVar(value=p["in_tc"])
            out_var = tk.StringVar(value=p["out_tc"])

            entries = {}
            for lbl_text, var, key in [("IN ", in_var, "in"), ("OUT", out_var, "out")]:
                tk.Label(tc_row, text=lbl_text, font=FB,
                         bg=SURF, fg=SUB, width=3, anchor="w").pack(side="left")
                e = tk.Entry(tc_row, textvariable=var, font=FB,
                             bg=SURF2, fg=TEXT, insertbackground=TEXT,
                             relief="flat", bd=4, width=11)
                e.pack(side="left", padx=(0, 14))
                entries[key] = e

            err_lbl = tk.Label(tc_row, text="", font=FB, bg=SURF, fg=ERR)
            err_lbl.pack(side="left")

            # Quote snippet
            preview = p.get("quote_text", "")
            if preview:
                snippet = preview[:90] + ("…" if len(preview) > 90 else "")
                tk.Label(rf, text=snippet, font=FB, bg=SURF, fg=SUB,
                         wraplength=480, justify="left").pack(anchor="w", pady=(6, 0))

            row = dict(p=p, in_var=in_var, out_var=out_var,
                       ignore_var=ignore_var, frame=rf,
                       err_lbl=err_lbl, entries=entries, ignore_lbl=ignore_lbl)
            rows.append(row)

            # IGNORE toggle — dims entries while active
            def _toggle(r=row):
                r["ignore_var"].set(not r["ignore_var"].get())
                ign   = r["ignore_var"].get()
                state = "disabled" if ign else "normal"
                r["ignore_lbl"].config(text="⊘ IGNORED" if ign else "IGNORE",
                                       fg=ERR if ign else SUB)
                r["frame"].config(highlightbackground=SURF3 if ign else WARN)
                for e in r["entries"].values():
                    e.config(state=state,
                             bg=SURF3 if ign else SURF2,
                             fg=SUB   if ign else TEXT)
                r["err_lbl"].config(text="")

            ignore_lbl.bind("<Button-1>", lambda e, f=_toggle: f())

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_row = tk.Frame(dlg, bg=SURF, padx=20, pady=14,
                           highlightbackground=BORDER, highlightthickness=1)
        btn_row.pack(fill="x", side="bottom")

        def _go_back():
            result["proceed"] = False
            dlg.destroy()

        def _apply_and_continue():
            valid = True
            for row in rows:
                row["err_lbl"].config(text="")
                if row["ignore_var"].get():
                    continue
                try:
                    in_s = tc_secs(row["in_var"].get().strip())
                except Exception:
                    row["err_lbl"].config(text="invalid IN timecode")
                    valid = False; continue
                try:
                    out_s = tc_secs(row["out_var"].get().strip())
                except Exception:
                    row["err_lbl"].config(text="invalid OUT timecode")
                    valid = False; continue
                if out_s <= in_s:
                    row["err_lbl"].config(text="out must be after in")
                    valid = False; continue
                # Write corrections back to the pull dict before reconcile runs
                row["p"]["in_tc"]       = secs_tc(in_s)
                row["p"]["in_seconds"]  = float(in_s)
                row["p"]["out_tc"]      = secs_tc(out_s)
                row["p"]["out_seconds"] = float(out_s)

            if not valid:
                return

            # Drop ignored pulls so they are skipped entirely
            ignore_ids = {id(r["p"]) for r in rows if r["ignore_var"].get()}
            if ignore_ids:
                self.pulls = [p for p in self.pulls if id(p) not in ignore_ids]

            result["proceed"] = True
            dlg.destroy()

        self._btn(btn_row, "← GO BACK",  _go_back).pack(side="left")
        self._btn(btn_row, "▶  CONTINUE", _apply_and_continue,
                  color=ACCENT).pack(side="right")

        # ── Centre on parent ──────────────────────────────────────────────────
        self.update_idletasks()
        dlg.update_idletasks()
        pw = self.winfo_width()
        ph = self.winfo_height()
        px = self.winfo_rootx()
        py = self.winfo_rooty()
        dw = dlg.winfo_reqwidth()
        dh = dlg.winfo_reqheight()
        dlg.geometry("+{}+{}".format(px + (pw - dw) // 2, py + (ph - dh) // 2))

        self.wait_window(dlg)
        return result["proceed"]

    def _start_reconcile(self):
        if not HAS_WHISPER:
            messagebox.showerror("Not Installed",
                "faster-whisper is not installed.\n\nRun:\n  pip install faster-whisper")
            return

        # Ensure ffmpeg/ffprobe are available (uses system or auto-downloads once)
        ffmpeg_ok, ffmpeg_err = engines.check_ffmpeg()
        if not ffmpeg_ok:
            messagebox.showerror("ffmpeg unavailable", ffmpeg_err or "Could not use or install ffmpeg.")
            return

        # Save setup so "Redo" can restore it (pool is destroyed by _clear below)
        self._redo_setup = {
            "assignments": [(r["path"], r["var"].get()) for r in self._pool._rows],
            "transcript_sources": {
                tok: self._pool.get_transcript_source(tok)
                for tok in self.tokens
                if self._pool.get_transcript_source(tok)
            },
        }

        int_assets = self._pool.get_interview_assets()
        vo_assets  = self._pool.get_vo_assets()

        # AAF → Pro Tools only needs audio.  Drop video files for any token /
        # VO part that already has a matching audio file.  Fall back to video
        # only when there is no audio at all (so we can still extract audio
        # from a video-only source).
        if self.workflow == "script_aaf":
            int_assets = {
                tok: ([p for p in paths if not is_video(p)]
                      or [p for p in paths if is_video(p)])
                for tok, paths in int_assets.items()
            }
            for pi in vo_assets:
                if vo_assets[pi]["audios"]:
                    vo_assets[pi]["videos"] = []   # audio present → skip video

        class _VoBinProxy:
            def __init__(self, d):
                self._audios = d.get("audios", [])
                self._videos = d.get("videos", [])
            def get_audio(self):
                return self._audios[0] if self._audios else None
            def get_video(self):
                return self._videos[0] if self._videos else None
            def get_paths(self):
                return self._videos + self._audios

        self.vo_bins = {pi: _VoBinProxy(d) for pi, d in vo_assets.items()}

        missing = [tok for tok in self.tokens
                   if tok not in int_assets
                   and any(p["token"]==tok for p in self.pulls)]

        if missing:
            msg = (
                "The following session tokens appear in the script but have "
                "no media assigned in the pool:\n\n"
                + "\n".join("  •  {}".format(t) for t in sorted(missing))
                + "\n\nPulls for these tokens will be skipped.  Continue anyway?"
            )
            if not messagebox.askyesno("Missing Media", msg, icon="warning"):
                return

        # ── Pre-flight: warn about oversized pull windows ──────────────────────
        suspicious = [p for p in self.pulls
                      if p.get("out_seconds", 0) - p.get("in_seconds", 0) > MAX_EXTRACT_S]
        if suspicious and not self._warn_large_windows(suspicious):
            return

        self._clear()
        self._section("STEP 3 — RECONCILING")

        tk.Label(self.body,
                 text="Extracting clip windows and transcribing.  "
                      "Each pull takes a few seconds.",
                 font=FB, bg=BG, fg=SUB).pack(anchor="w", pady=(0,10))

        prog_outer = tk.Frame(self.body, bg=SURF,
                              highlightbackground=BORDER, highlightthickness=1)
        prog_outer.pack(fill="x", pady=(0,8))
        prog_row = tk.Frame(prog_outer, bg=SURF)
        prog_row.pack(fill="x", padx=12, pady=8)
        _total_items = len(self.pulls) + len(self.vo_blocks)
        self._prog_lbl = tk.Label(prog_row, text="0 / {}".format(_total_items),
                                   font=FL, bg=SURF, fg=TEXT)
        self._prog_lbl.pack(side="left")
        self._prog_bar = ttk.Progressbar(prog_row, length=560,
                                          maximum=_total_items)
        self._prog_bar.pack(side="left", padx=12)

        tx_row = tk.Frame(prog_outer, bg=SURF)
        tx_row.pack(fill="x", padx=12, pady=(0, 8))
        self._tx_lbl = tk.Label(tx_row, text="", font=FL, bg=SURF, fg=SUB,
                                width=34, anchor="w")
        self._tx_lbl.pack(side="left")
        self._tx_bar = _FlatProgressBar(tx_row, height=4)
        self._tx_bar.pack(side="left", fill="x", expand=True)

        log_frame = tk.Frame(self.body, bg=SURF3,
                             highlightbackground=BORDER, highlightthickness=1)
        log_frame.pack(fill="both", expand=True)
        self._log = tk.Text(log_frame, bg=SURF3, fg=SUB, font=FB,
                            relief="flat", bd=8, state="disabled",
                            height=20, wrap="word")
        sb = _SlimScrollbar(log_frame, command=self._log.yview)
        self._log.configure(yscrollcommand=sb.set)
        self._log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._cancel.clear()
        nav = tk.Frame(self.body, bg=BG); nav.pack(fill="x", pady=(8,0))
        self._btn(nav, "CANCEL", self._cancel_reconcile).pack(side="left")

        threading.Thread(
            target=self._run_reconcile,
            args=(int_assets,),
            daemon=True).start()

    @staticmethod
    def _fmt_duration(secs):
        """Return a string like '2m 14s  (134.2s)' for log output."""
        m = int(secs) // 60
        s = secs - m * 60
        if m:
            return "{:d}m {:.0f}s  ({:.1f}s)".format(m, s, secs)
        return "{:.1f}s".format(secs)

    def _log_line(self, msg, color=None):
        # Also write to the run log file if one is open
        if getattr(self, "_run_log_fh", None):
            try:
                self._run_log_fh.write(msg + "\n")
                self._run_log_fh.flush()
            except Exception:
                pass
        def _do():
            self._log.configure(state="normal")
            tag = "c{}".format(abs(hash(color or "")))
            if color:
                self._log.tag_configure(tag, foreground=color)
            self._log.insert("end", msg + "\n", tag if color else "")
            self._log.see("end")
            self._log.configure(state="disabled")
        self.after(0, _do)

    def _set_tx_progress(self, n, total, label=""):
        def _do():
            if not hasattr(self, "_tx_lbl"):
                return
            self._tx_lbl.configure(text=label)
            self._tx_bar.set(n, total)
        self.after(0, _do)

    def _run_reconcile(self, int_assets):
        pulls       = self.pulls
        total       = len(pulls)
        done        = 0
        results     = []
        result_q    = queue.Queue()

        # Open a run log file next to the script for post-run auditing
        self._run_log_fh = None
        try:
            script_path = getattr(self, "_script_path", None)
            if script_path:
                import datetime
                ts       = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                log_path = os.path.splitext(script_path)[0] + "_run_{}.log".format(ts)
                self._run_log_fh = open(log_path, "w", encoding="utf-8")
                self._run_log_fh.write("PostBridge run log — {}\n{}\n\n".format(
                    ts, script_path))
        except Exception:
            self._run_log_fh = None

        self._log_line("Loading model ({})…".format(WHISPER_MODEL), INFO)

        if HAS_WHISPER:
            try:
                engines.get_model()
                self._log_line("Model loaded.", SUCCESS)
            except Exception as e:
                self._log_line("Model load failed: {}".format(e), ERR)
                self.after(0, lambda: messagebox.showerror(
                    "Model Error", str(e)))
                return

        transcript_sources = {
            tok: self._pool.get_transcript_source(tok)
            for tok in self.tokens
        }

        vo_takes_by_part = {}

        _total_tx = sum(
            len(engines.pair_takes(vb.get_paths()))
            for vb in self.vo_bins.values()
        )
        _tx_done        = 0
        _tx_cached      = 0
        _tx_fresh       = 0
        _chunk_counts   = []   # (part_index, take_i, n_chunks) for chunked runs
        if _total_tx:
            self._set_tx_progress(0, _total_tx, "VO transcription — 0 / {}".format(_total_tx))

        _t_run_start = time.perf_counter()   # overall wall-clock start
        _t_tx_start  = time.perf_counter()
        for pi, vb in self.vo_bins.items():
            if self._cancel.is_set():
                break

            paths = vb.get_paths()
            takes = engines.pair_takes(paths)
            if not takes:
                continue

            self._log_line(
                "Transcribing VO Part {} — {} take{}…".format(
                    pi, len(takes), "s" if len(takes) != 1 else ""), INFO)

            part_takes = []
            for take_i, (vp, ap) in enumerate(takes):
                if self._cancel.is_set():
                    break

                if not ap:
                    self._log_line(
                        "  Take {}: no audio file, skipping".format(take_i + 1), WARN)
                    continue

                blobs       = None
                audio_words = engines.cache_load(ap)
                if audio_words is not None:
                    blobs = engines.cache_load_blobs(ap)
                    self._log_line(
                        "  Take {}: {} words  [cached]".format(
                            take_i + 1, len(audio_words)), INFO)
                    _tx_done   += 1
                    _tx_cached += 1
                    self._set_tx_progress(
                        _tx_done, _total_tx,
                        "VO transcription — {} / {}  [cached]".format(_tx_done, _total_tx))
                else:
                    _t0 = time.perf_counter()
                    if WAVEFORM_CONFORM:
                        # ── Step 1a: silence-split chunked transcription ───────────
                        # Split the file at pauses ≥ CHUNK_SILENCE_S, then transcribe
                        # each chunk independently.  Gives Whisper shorter, cleaner
                        # segments → better accuracy and no mkl_malloc memory errors.
                        chunks = engines.detect_silence_splits(ap)
                        if chunks:
                            _chunk_counts.append((pi, take_i + 1, len(chunks)))
                            self._log_line(
                                "  Take {}: {} chunk{} from silence analysis — "
                                "transcribing…".format(
                                    take_i + 1, len(chunks),
                                    "s" if len(chunks) != 1 else ""), INFO)
                            audio_words = engines.transcribe_in_chunks(ap, chunks)
                            self._log_line(
                                "  Take {}: chunked transcription done in {:.1f}s  "
                                "({} words)".format(
                                    take_i + 1, time.perf_counter() - _t0,
                                    len(audio_words) if audio_words else 0), INFO)
                        else:
                            self._log_line(
                                "  Take {}: silence analysis failed — "
                                "falling back to full-file transcription".format(
                                    take_i + 1), WARN)

                    # ── Step 1b: full-file fallback (WAVEFORM_CONFORM off or failed) ─
                    if audio_words is None:
                        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                            tmp_a = tf.name
                        try:
                            ok, err = engines.extract_window(ap, 0, 9999, tmp_a)
                            if not ok:
                                msg = "  Take {}: audio extract failed".format(take_i + 1)
                                if err:
                                    msg += " — {}".format(err[:200] if len(err) > 200 else err)
                                self._log_line(msg, ERR)
                                continue
                            audio_words = engines.transcribe_clip(tmp_a)
                            self._log_line(
                                "  Take {}: full-file transcription done in {:.1f}s  "
                                "({} words)".format(
                                    take_i + 1, time.perf_counter() - _t0,
                                    len(audio_words) if audio_words else 0), INFO)
                        finally:
                            try: os.unlink(tmp_a)
                            except: pass

                    # ── Step 2: blob detection for in/out point snapping ───────────
                    if WAVEFORM_CONFORM and audio_words:
                        _t1 = time.perf_counter()
                        blobs = engines.detect_speech_blobs(ap)
                        self._log_line(
                            "  Take {}: {} blob{} detected in {:.1f}s  (edge snapping)".format(
                                take_i + 1,
                                len(blobs) if blobs else 0,
                                "s" if (blobs and len(blobs) != 1) else "",
                                time.perf_counter() - _t1), INFO)

                    _tx_done  += 1
                    _tx_fresh += 1
                    self._set_tx_progress(
                        _tx_done, _total_tx,
                        "VO transcription — {} / {}".format(_tx_done, _total_tx))

                    if not audio_words:
                        self._log_line(
                            "  Take {}: no words transcribed".format(take_i + 1), ERR)
                        continue
                    engines.cache_save(ap, audio_words, blobs=blobs)
                    self._log_line(
                        "  Take {}: {} words  [saved to cache]".format(
                            take_i + 1, len(audio_words)), INFO)

                v_offset = 0.0
                if vp:
                    v_offset = engines.detect_av_offset(ap, vp)
                    self._log_line(
                        "  Take {}: {} words  video offset {:+.2f}s".format(
                            take_i + 1, len(audio_words), v_offset), SUCCESS)
                else:
                    self._log_line(
                        "  Take {}: {} words  (no video)".format(
                            take_i + 1, len(audio_words)), SUCCESS)

                part_takes.append((audio_words, v_offset, vp, ap, blobs))

            if part_takes:
                vo_takes_by_part[pi] = part_takes

        if _total_tx:
            self._set_tx_progress(_total_tx, _total_tx, "VO transcription complete")
            _tx_total_s = time.perf_counter() - _t_tx_start
            self._log_line(
                "VO transcription total: {}  ({} fresh  ·  {} cached)".format(
                    self._fmt_duration(_tx_total_s), _tx_fresh, _tx_cached), INFO)
            if _chunk_counts:
                _all_n = [n for _, _, n in _chunk_counts]
                self._log_line(
                    "  Chunks — min: {}  max: {}  avg: {:.1f}  total: {}".format(
                        min(_all_n), max(_all_n),
                        sum(_all_n) / len(_all_n), sum(_all_n)), INFO)

        _t_rec_start = time.perf_counter()

        def process_pull(pull):
            if self._cancel.is_set():
                r = engines._base_result(pull); r["status"] = "cancelled"; return r
            tsrc = transcript_sources.get(pull["token"])
            return [engines.reconcile_interview_pull(pull, tsrc, pad=self.pad_var.get())]

        def process_vo_part(part_index, blocks):
            if self._cancel.is_set():
                return [dict(engines._vo_base_result(b), status="cancelled")
                        for b in blocks]
            takes = vo_takes_by_part.get(part_index, [])
            return engines.reconcile_vo_part(blocks, takes)

        # Group VO blocks by part so they are processed in sequence with a
        # shared cursor — this prevents the matcher from scanning 2000+ words
        # for every block independently.
        from collections import defaultdict as _dd
        _vo_by_part = _dd(list)
        for vb in self.vo_blocks:
            _vo_by_part[vb["part_index"]].append(vb)

        all_items = (
            [("pull",    p)              for p in pulls] +
            [("vo_part", (pi, blocks))   for pi, blocks in _vo_by_part.items()]
        )
        # Total expected *results* (one per pull + one per individual VO block)
        total = len(pulls) + len(self.vo_blocks)

        def dispatch(item):
            t0   = time.perf_counter()
            kind, obj = item
            if kind == "pull":
                res = process_pull(obj)
            else:
                pi, blocks = obj
                res = process_vo_part(pi, blocks)
            elapsed = round(time.perf_counter() - t0, 2)
            for r in res:
                r["_dispatch_s"] = elapsed
            return res

        # ── Reconcile loop ─────────────────────────────────────────────────────
        # We poll with a 2-second timeout rather than blocking in as_completed()
        # so that:
        #   (a) Cancel works immediately — we check _cancel every 2 s even if no
        #       futures have finished yet.
        #   (b) The UI can display "N still running… (Xs)" when items stall so the
        #       user knows the process is alive.
        ex = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        try:
            futures       = {ex.submit(dispatch, item): item for item in all_items}
            pending       = set(futures.keys())
            _stall_t      = time.perf_counter()   # time of last progress update
            _stall_logged = set()                  # thresholds already written to log

            while pending:
                # ── Check cancel before waiting ──────────────────────────────
                if self._cancel.is_set():
                    for f in pending:
                        f.cancel()
                    break

                # Wait up to 2 s for at least one future to finish
                done_set, pending = _fut_wait(
                    pending, timeout=2.0, return_when=FIRST_COMPLETED)

                # ── Still no completions? surface a stall indicator ──────────
                if not done_set:
                    stall_s = time.perf_counter() - _stall_t
                    n_left  = len(pending)
                    _d_snap = done
                    def _stall_upd(d=_d_snap, n=n_left, s=stall_s):
                        self._prog_lbl.config(
                            text="{} / {}  —  {} still running…  ({:.0f}s)".format(
                                d, total, n, s))
                    self.after(0, _stall_upd)

                    # Write to the log pane at 10 / 30 / 60+ s milestones
                    for threshold in (10, 30, 60):
                        if stall_s >= threshold and threshold not in _stall_logged:
                            _stall_logged.add(threshold)
                            self._log_line(
                                "  ⏳  {} item(s) still processing…  ({:.0f}s elapsed)".format(
                                    n_left, stall_s),
                                SUB)
                    # After 60 s log every additional 60 s (120, 180, 240…)
                    if stall_s >= 120:
                        milestone = (int(stall_s) // 60) * 60
                        if milestone not in _stall_logged:
                            _stall_logged.add(milestone)
                            self._log_line(
                                "  ⏳  {} item(s) still processing…  ({:.0f}s elapsed)".format(
                                    n_left, stall_s),
                                WARN)

                    # ── Hard stall timeout: force-abandon after MAX_ITEM_STALL_S ─
                    if stall_s >= MAX_ITEM_STALL_S:
                        msg = ("  ⚠  TIMEOUT: {} item(s) abandoned after {:.0f}s"
                               " — marked as error".format(n_left, stall_s))
                        self._log_line(msg, ERR)
                        if self._run_log_fh:
                            try:
                                self._run_log_fh.write(msg + "\n")
                                self._run_log_fh.flush()
                            except Exception:
                                pass
                        for f in list(pending):
                            item = futures[f]
                            kind, obj = item
                            if kind == "pull":
                                r = engines._base_result(obj)
                                r["status"]       = "error"
                                r["matched_text"] = "timeout ({:.0f}s)".format(stall_s)
                                results.append(r)
                                done += 1
                            else:
                                pi, blocks = obj
                                for b in blocks:
                                    r = engines._vo_base_result(b)
                                    r.update(is_vo=True, part_index=pi,
                                             status="error",
                                             matched_text="timeout ({:.0f}s)".format(stall_s))
                                    results.append(r)
                                    done += 1
                        pending.clear()
                        break   # exit while pending — proceed to summary

                    continue

                # ── Progress resumed — reset stall clock ────────────────────
                _stall_t      = time.perf_counter()
                _stall_logged = set()

                # ── Process completed futures ────────────────────────────────
                for fut in done_set:
                    if self._cancel.is_set():
                        break
                    item = futures[fut]
                    try:
                        res_list = fut.result()
                    except Exception as e:
                        kind, obj = item
                        if kind == "pull":
                            r = engines._base_result(obj)
                            r["status"] = "error"; r["matched_text"] = str(e)
                            res_list = [r]
                        else:
                            pi, blocks = obj
                            res_list = [dict(engines._vo_base_result(b),
                                            status="error", matched_text=str(e))
                                        for b in blocks]

                    for res in res_list:
                        results.append(res)
                        done += 1

                        st    = res["status"]
                        conf  = res.get("confidence", 0)
                        is_vo = res.get("is_vo", False)
                        color = (SUCCESS if st in ("ok","direct")
                                 else WARN if st in ("low_confidence","no_quote")
                                 else ERR)
                        cuts     = res.get("n_internal_cuts",0) + res.get("n_gap_cuts",0)
                        cut_str  = "  {}-segs".format(len(res.get("segments") or [1])) if cuts else ""
                        tag      = "VO" if is_vo else res.get("token","")
                        cache_str = "  [cached]" if res.get("from_cache") else ""
                        disp_s   = res.get("_dispatch_s")
                        time_str = "  [{:.1f}s]".format(disp_s) if disp_s and disp_s > 1.0 else ""
                        self._log_line(
                            "#{:03d}  {}  {}  conf:{:.0%}{}{}{}".format(
                                res["order"], tag, st, conf, cut_str, cache_str, time_str),
                            color)

                        # ── Diagnostic lines ───────────────────────────────
                        if is_vo and st in ("ok", "low_confidence"):
                            self._log_line(
                                "     ↳ {:.1f}s – {:.1f}s".format(
                                    res.get("rec_in_s", 0), res.get("rec_out_s", 0)),
                                SUB)
                        if is_vo and st in ("no_match", "low_confidence"):
                            qt = res.get("quote_text", "")
                            self._log_line(
                                '     ↳ searched: "{}"'.format(
                                    qt[:90] + ("…" if len(qt) > 90 else "")),
                                SUB)

                        def _update(d=done):
                            self._prog_bar["value"] = d
                            self._prog_lbl.config(text="{} / {}".format(d, total))
                        self.after(0, _update)
        finally:
            ex.shutdown(wait=False)

        # ── Hand off to main thread ────────────────────────────────────────────
        # We do NOT call _log_line (which uses self.after) from the background
        # thread here.  By the time all ~438 futures have completed the Tk event
        # queue is saturated with pending after(0,...) callbacks; calling
        # after() again from this thread while the main thread drains that
        # backlog can deadlock on the Tcl interpreter lock.
        #
        # Instead: compute the summary purely in Python (no Tk), write it
        # directly to the log file (file I/O only), store the results and
        # summary lines as instance variables, then schedule ONE after(0,...)
        # call to finish the UI work on the main thread.

        if self._cancel.is_set():
            # Write "Cancelled" directly to file; main-thread UI update via after
            if self._run_log_fh:
                try:
                    self._run_log_fh.write("Cancelled.\n")
                    self._run_log_fh.flush()
                    self._run_log_fh.close()
                except Exception:
                    pass
                self._run_log_fh = None
            self.after(0, lambda: self._log_line("Cancelled.", WARN))
            return

        # ── Compute summary (pure Python, no Tk) ──────────────────────────────
        sorted_results = sorted(results, key=lambda r: r["order"])

        _rec_total_s = time.perf_counter() - _t_rec_start
        _run_total_s = time.perf_counter() - _t_run_start

        vo_results  = [r for r in sorted_results if r.get("is_vo")]
        int_results = [r for r in sorted_results if not r.get("is_vo")]

        def _counts(rlist):
            ok_  = sum(1 for r in rlist if r.get("status") in ("ok", "direct"))
            lc   = sum(1 for r in rlist if r.get("status") == "low_confidence")
            nm   = sum(1 for r in rlist if r.get("status") == "no_match")
            err  = sum(1 for r in rlist if r.get("status") in
                       ("no_quote", "error", "no_file", "cancelled"))
            return ok_, lc, nm, err

        vo_ok, vo_lc, vo_nm, vo_err = _counts(vo_results)
        in_ok, in_lc, in_nm, in_err = _counts(int_results)
        ok    = vo_ok + in_ok
        flags = vo_lc + vo_nm + vo_err + in_lc + in_nm + in_err

        conf_vals = [r.get("confidence", 0) for r in sorted_results
                     if r.get("status") in ("ok", "direct", "low_confidence")]
        c90  = sum(1 for c in conf_vals if c >= 0.90)
        c70  = sum(1 for c in conf_vals if 0.70 <= c < 0.90)
        c50  = sum(1 for c in conf_vals if 0.50 <= c < 0.70)
        clow = sum(1 for c in conf_vals if c < 0.50)

        seg_counts = [len(r.get("segments") or []) for r in sorted_results
                      if r.get("status") in ("ok", "direct") and r.get("segments")]
        seg_outliers = [r for r in sorted_results
                        if r.get("status") in ("ok", "direct")
                        and len(r.get("segments") or []) > 5]

        from collections import defaultdict as _dd2
        _vo_part_fail = _dd2(int)
        _vo_part_tot  = _dd2(int)
        for r in vo_results:
            pi = r.get("part_index", "?")
            _vo_part_tot[pi]  += 1
            if r.get("status") not in ("ok", "direct"):
                _vo_part_fail[pi] += 1

        def _take_dur(t):
            segs = (t or {}).get("segments") or []
            return sum(e - s for s, e in segs if e > s)

        multi_take_notes = []
        for r in vo_results:
            bi = r.get("best_take_index", 0)
            if bi and bi > 0:
                td = (r.get("takes_data") or [])
                best_dur  = _take_dur(td[bi]) if bi < len(td) else 0.0
                first_dur = _take_dur(td[0])  if td else 0.0
                multi_take_notes.append(
                    "  Part {} — Take {} selected  (matched {:.1f}s vs Take 1: {:.1f}s)".format(
                        r.get("part_index", "?"), bi + 1, best_dur, first_dur))

        # ── Build summary lines (strings only, no Tk) ─────────────────────────
        sum_lines = []  # list of (text, color) tuples
        def _sl(txt, col=None):
            sum_lines.append((txt, col))

        _sl("\n" + "─" * 60, SUB)
        _sl("RECONCILE PHASE     {}  ({} items)".format(
            self._fmt_duration(_rec_total_s), len(sorted_results)), INFO)
        _sl("TOTAL RUN TIME      {}".format(self._fmt_duration(_run_total_s)), INFO)

        _sl("\nMATCH RESULTS", SUB)
        _sl("  Interview   ok: {}  low-conf: {}  no-match: {}  error: {}  "
            "/ {}  ({:.0%})".format(
                in_ok, in_lc, in_nm, in_err,
                len(int_results),
                in_ok / len(int_results) if int_results else 0), INFO)
        _sl("  VO          ok: {}  low-conf: {}  no-match: {}  error: {}  "
            "/ {}  ({:.0%})".format(
                vo_ok, vo_lc, vo_nm, vo_err,
                len(vo_results),
                vo_ok / len(vo_results) if vo_results else 0), INFO)

        _sl("\nCONFIDENCE DISTRIBUTION  (matched items only)", SUB)
        _sl("  90–100%: {}   70–89%: {}   50–69%: {}   <50%: {}".format(
            c90, c70, c50, clow), INFO)

        if seg_counts:
            _sl("\nSEGMENT COUNTS  (matched items)", SUB)
            _sl("  min: {}  max: {}  avg: {:.1f}  total segs: {}".format(
                min(seg_counts), max(seg_counts),
                sum(seg_counts) / len(seg_counts), sum(seg_counts)), INFO)
            if seg_outliers:
                _sl("  High-seg outliers (>5 segs):", WARN)
                for r in seg_outliers:
                    _sl("    #{:03d}  {}  {} segs  conf:{:.0%}".format(
                        r["order"], r.get("token", "VO"),
                        len(r.get("segments") or []),
                        r.get("confidence", 0)), WARN)

        _failing_parts = {pi: (_vo_part_fail[pi], _vo_part_tot[pi])
                          for pi in _vo_part_tot
                          if _vo_part_fail[pi] > 0}
        if _failing_parts:
            _sl("\nVO PART FAILURE SUMMARY", SUB)
            for pi in sorted(_failing_parts):
                nf, nt = _failing_parts[pi]
                _sl("  Part {}  —  {}/{} failed  ({:.0%})".format(
                    pi, nf, nt, nf / nt if nt else 0),
                    ERR if nf / nt >= 0.5 else WARN)

        if multi_take_notes:
            _sl("\nMULTI-TAKE SELECTIONS", SUB)
            for note in multi_take_notes:
                _sl(note, INFO)

        _sl("─" * 60, SUB)
        _sl("\nDone.  {} matched  ·  {} need review".format(ok, flags),
            SUCCESS if flags == 0 else WARN)

        # ── Write summary directly to log file (no Tk) ────────────────────────
        if self._run_log_fh:
            try:
                for txt, _ in sum_lines:
                    self._run_log_fh.write(txt + "\n")
                self._run_log_fh.write(
                    "\n--- {} matched  ·  {} need review ---\n".format(ok, flags))
                self._run_log_fh.flush()
                self._run_log_fh.close()
            except Exception:
                pass
            self._run_log_fh = None

        # ── Store results; schedule ONE after() call to finish on main thread ──
        self.results          = sorted_results
        self._vo_takes_by_part = vo_takes_by_part
        self._pending_summary = sum_lines   # consumed by _finish_reconcile

        self.after(0, self._finish_reconcile)

    def _finish_reconcile(self):
        """Called on the main thread after _run_reconcile completes.
        Appends the pre-computed summary lines to the log widget then
        transitions to Step 4.  All Tk operations happen here, safely."""
        for txt, color in getattr(self, "_pending_summary", []):
            self._log_line(txt, color)
        self._pending_summary = []
        self.after(800, self._step4)

    def _cancel_reconcile(self):
        self._cancel.set()
        self._log_line("Cancel requested — stopping after current items finish…", WARN)
        # Disable the cancel button immediately so the user knows the click landed
        for w in self.body.winfo_children():
            if isinstance(w, tk.Frame):
                for child in w.winfo_children():
                    if isinstance(child, tk.Button) and "CANCEL" in (child.cget("text") or ""):
                        try:
                            child.config(state="disabled",
                                         text="Cancelling…",
                                         bg=SURF2)
                        except Exception:
                            pass

    def _step4(self):
        # Capture Step 3 log before clearing so it can be viewed from Step 4
        if getattr(self, "_log", None) and self._log.winfo_exists():
            try:
                self._reconcile_log_text = self._log.get("1.0", "end-1c")
            except Exception:
                self._reconcile_log_text = getattr(self, "_reconcile_log_text", "")
        else:
            self._reconcile_log_text = getattr(self, "_reconcile_log_text", "")

        self._clear()
        self._section("STEP 4 — REVIEW")
        tk.Label(self.body,
                 text="Reconcile log.  "
                      "[~] low confidence    [?] unmatched fallback    "
                      "These are flagged by name in the exported XML.",
                 font=FB, bg=BG, fg=SUB, wraplength=860).pack(anchor="w", pady=(0,6))

        _CLEAN = ("ok", "direct", "no_quote", "cancelled")
        _ATTN  = ("no_match", "error", "low_confidence", "no_file", "not_run")

        ok    = sum(1 for r in self.results if r["status"] in ("ok", "direct"))
        flags = sum(1 for r in self.results if r["status"] in _ATTN)
        skips = sum(1 for r in self.results if r["status"] in ("no_file", "cancelled"))

        srow = tk.Frame(self.body, bg=SURF,
                        highlightbackground=BORDER, highlightthickness=1)
        srow.pack(fill="x", pady=(0,8))
        for lbl, val, col in [
            ("matched",     str(ok),    SUCCESS),
            ("need review", str(flags), WARN if flags else SUB),
            ("no file",     str(skips), ERR  if skips else SUB),
        ]:
            c = tk.Frame(srow, bg=SURF); c.pack(side="left", padx=20, pady=6)
            tk.Label(c, text=val,  font=("Courier New",18,"bold"),
                     bg=SURF, fg=col).pack()
            tk.Label(c, text=lbl, font=FB, bg=SURF, fg=SUB).pack()

        # ── Filter tabs ───────────────────────────────────────────────────────
        _fstate = {"mode": "attention"}
        _fbtns  = {}

        def _apply_filter(mode):
            _fstate["mode"] = mode
            for m, b in _fbtns.items():
                active = (m == mode)
                b.config(bg=ACCENT if active else SURF2,
                         fg=BG    if active else TEXT)
            # Unpack all then repack matching cards — preserves original order
            for e in self._rv:
                e["card"].pack_forget()
            for e in self._rv:
                st = e["res"].get("status", "")
                if mode == "all":
                    show = True
                elif mode == "attention":
                    show = st in _ATTN
                else:                       # "matched"
                    show = st in ("ok", "direct")
                if show:
                    e["card"].pack(fill="x", pady=(0, 4), padx=2)

        frow = tk.Frame(self.body, bg=BG)
        frow.pack(fill="x", pady=(0, 8))
        for _fm, _fl, _fc in [
            ("attention", "NEEDS ATTENTION", flags),
            ("matched",   "MATCHED",         ok),
            ("all",       "ALL",             len(self.results)),
        ]:
            b = tk.Label(frow, text="{}  {}".format(_fl, _fc), font=FB,
                         bg=SURF2, fg=TEXT, cursor="hand2",
                         padx=14, pady=6, bd=0,
                         highlightbackground=BORDER, highlightthickness=1)
            b.pack(side="left", padx=(0, 4))
            b.bind("<Button-1>", lambda e, m=_fm: _apply_filter(m))
            _fbtns[_fm] = b

        sf = self._scroll_frame(self.body, height=380)
        self._rv   = []
        self._skip = []

        STATUS_COLOR = {
            "ok":             SUCCESS,
            "direct":         SUCCESS,
            "low_confidence": WARN,
            "no_match":       ERR,
            "no_quote":       SUB,
            "error":          ERR,
            "no_file":        ERR,
            "not_run":        SUB,
            "cancelled":      SUB,
        }
        STATUS_LABEL = {
            "ok":             "✓  matched",
            "direct":         "✓  matched",
            "low_confidence": "⚠  low confidence",
            "no_match":       "✗  no match",
            "no_quote":       "–  no quote text",
            "error":          "✗  error",
            "no_file":        "✗  no file assigned",
            "not_run":        "–  not run",
            "cancelled":      "–  cancelled",
        }

        for res in self.results:
            st   = res.get("status","")
            sc   = STATUS_COLOR.get(st,  SUB)
            sl   = STATUS_LABEL.get(st,  st)

            card = tk.Frame(sf, bg=SURF,
                            highlightbackground=BORDER, highlightthickness=1)
            card.pack(fill="x", pady=(0,4), padx=2)

            hdr = tk.Frame(card, bg=SURF); hdr.pack(fill="x", padx=10, pady=(6,2))
            tk.Label(hdr, text="#{:03d}".format(res["order"]),
                     font=FL, bg=SURF, fg=SUB, width=5, anchor="w").pack(side="left")
            tk.Label(hdr, text=res["token"],
                     font=FL, bg=SURF, fg=ACCENT, width=14, anchor="w").pack(side="left")
            tk.Label(hdr, text=sl, font=FB, bg=SURF, fg=sc).pack(side="left", padx=8)

            d_in  = res.get("delta_in",  0)
            d_out = res.get("delta_out", 0)
            if st == "ok" and (abs(d_in) > 0.5 or abs(d_out) > 0.5):
                tk.Label(hdr,
                         text="Δin:{:+.1f}s  Δout:{:+.1f}s".format(d_in, d_out),
                         font=FB, bg=SURF, fg=INFO).pack(side="left", padx=4)

            skip_var = tk.BooleanVar(value=False)

            # IGNORE toggle – right side of every card header
            ignore_lbl = tk.Label(hdr, text="IGNORE", font=FS,
                                  bg=SURF, fg=SUB, cursor="hand2")
            ignore_lbl.pack(side="right")

            def _toggle_ignore(sv=skip_var, c=card, lbl=ignore_lbl):
                sv.set(not sv.get())
                if sv.get():
                    lbl.config(text="⊘ IGNORED", fg=ERR)
                    c.config(highlightbackground=SURF3)
                else:
                    lbl.config(text="IGNORE", fg=SUB)
                    c.config(highlightbackground=BORDER)

            ignore_lbl.bind("<Button-1>", lambda e, f=_toggle_ignore: f())

            q = (res.get("quote_text","") or "")[:80]
            if len(res.get("quote_text","")) > 80: q += "…"
            tk.Label(card, text="Script:  " + q,
                     font=FB, bg=SURF, fg=SUB, anchor="w",
                     wraplength=820).pack(anchor="w", padx=10, pady=(0,2))

            if res.get("matched_text"):
                m = res["matched_text"][:80]
                if len(res["matched_text"]) > 80: m += "…"
                tk.Label(card, text="Found:   " + m,
                         font=FB, bg=SURF,
                         fg=TEXT if st=="ok" else WARN,
                         anchor="w", wraplength=820).pack(anchor="w", padx=10)

            segs     = res.get("segments") or []
            seg_vars = []   

            n_ic = res.get("n_internal_cuts", 0)
            n_gc = res.get("n_gap_cuts",      0)
            conf = res.get("confidence", 0)

            if n_ic or n_gc:
                cinfo = tk.Frame(card, bg=SURF); cinfo.pack(anchor="w", padx=10)
                parts_lbl = []
                if n_ic: parts_lbl.append("{} ellipsis cut{}".format(n_ic,"s" if n_ic>1 else ""))
                if n_gc: parts_lbl.append("{} silence cut{}".format(n_gc,"s" if n_gc>1 else ""))
                tk.Label(cinfo,
                         text="  {}  →  {} sub-clip{}".format(
                             "  +  ".join(parts_lbl),
                             len(segs), "s" if len(segs)>1 else ""),
                         font=FB, bg=SURF, fg=INFO).pack(side="left")

            if segs:
                for si, (seg_in_s, seg_out_s) in enumerate(segs):
                    srow = tk.Frame(card, bg=SURF2 if si%2==0 else SURF3)
                    srow.pack(fill="x", padx=10, pady=1)
                    tk.Label(srow,
                             text="SEG {:d}".format(si+1),
                             font=("Courier New",8,"bold"),
                             bg=srow["bg"], fg=SUB,
                             width=6, anchor="w").pack(side="left", padx=(6,0), pady=3)
                    iv = tk.StringVar(value=secs_tc(seg_in_s))
                    ov = tk.StringVar(value=secs_tc(seg_out_s))
                    for lbl2, v2 in [("IN  ", iv), ("OUT ", ov)]:
                        tk.Label(srow, text=lbl2, font=FB,
                                 bg=srow["bg"], fg=SUB,
                                 width=4, anchor="w").pack(side="left")
                        tk.Entry(srow, textvariable=v2, font=FB,
                                 bg=SURF, fg=TEXT, insertbackground=TEXT,
                                 relief="flat", bd=3, width=10).pack(side="left", padx=(0,12))
                    dur = seg_out_s - seg_in_s
                    tk.Label(srow,
                             text="{:.1f}s".format(dur),
                             font=FB, bg=srow["bg"], fg=SUB).pack(side="left")
                    seg_vars.append((iv, ov))
            else:
                # No Whisper match — allow fully manual in/out placement.
                trow = tk.Frame(card, bg=SURF)
                trow.pack(anchor="w", padx=10, pady=(4, 2))
                iv = tk.StringVar(value=res.get("rec_in_tc",  res.get("in_tc",  "")))
                ov = tk.StringVar(value=res.get("rec_out_tc", res.get("out_tc", "")))
                for lbl, var in [("IN  ", iv), ("OUT ", ov)]:
                    tk.Label(trow, text=lbl, font=FB, bg=SURF,
                             fg=SUB, width=5, anchor="w").pack(side="left")
                    tk.Entry(trow, textvariable=var, font=FB,
                             bg=SURF2, fg=TEXT, insertbackground=TEXT,
                             relief="flat", bd=4, width=10).pack(side="left", padx=(0,16))
                tk.Label(trow,
                         text="edit to place this clip manually",
                         font=FS, bg=SURF, fg=SUB).pack(side="left")
                seg_vars.append((iv, ov))

            if conf:
                crow = tk.Frame(card, bg=SURF); crow.pack(anchor="w", padx=10, pady=(2,6))
                tk.Label(crow, text="conf {:.0%}".format(conf),
                         font=FB, bg=SURF, fg=sc).pack(side="left")

            tk.Frame(card, bg=BORDER, height=1).pack(fill="x")

            self._rv.append({"seg_vars": seg_vars,
                             "skip_var": skip_var, "res": res, "card": card})

        # Apply default filter and highlight its tab
        _apply_filter("attention")

        nav = tk.Frame(self.body, bg=BG); nav.pack(fill="x", pady=(8,0))
        self._btn(nav, "← REDO", self._step2).pack(side="left")
        self._btn(nav, "VIEW RECONCILE LOG", self._show_reconcile_log,
                  small=True).pack(side="left", padx=(12,0))
        fmt = "AAF" if self.workflow == "script_aaf" else "XML"
        self._btn(nav, "EXPORT {}  →".format(fmt), self._step5,
                  color=ACCENT).pack(side="right")

    def _show_reconcile_log(self):
        """Open a window showing the saved Step 3 reconcile log (for debugging)."""
        log_text = getattr(self, "_reconcile_log_text", "") or "(no log captured)"
        win = tk.Toplevel(self)
        win.title("Reconcile log (Step 3)")
        win.configure(bg=BG, cursor="arrow")
        win.minsize(520, 320)
        win.geometry("720x480")
        f = tk.Frame(win, bg=BG, cursor="arrow")
        f.pack(fill="both", expand=True, padx=12, pady=12)
        txt = tk.Text(f, bg=SURF3, fg=SUB, font=("Courier New", 10),
                     wrap="word", state="disabled", relief="flat", bd=8,
                     cursor="arrow", insertwidth=2)
        sb = _SlimScrollbar(f, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        txt.configure(state="normal")
        txt.insert("1.0", log_text)
        txt.configure(state="disabled")
        btn_row = tk.Frame(win, bg=BG, cursor="arrow")
        btn_row.pack(fill="x", padx=12, pady=(0, 12))
        def _save():
            path = filedialog.asksaveasfilename(
                title="Save reconcile log",
                defaultextension=".txt",
                filetypes=[("Text", "*.txt"), ("All", "*.*")])
            if path:
                try:
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(log_text)
                    messagebox.showinfo("Saved", "Log saved to:\n{}".format(path))
                except Exception as e:
                    messagebox.showerror("Error", str(e))
        save_btn = tk.Button(btn_row, text="Save to file…", font=FB,
                             bg=SURF2, fg=TEXT, activebackground=ACCENT, activeforeground=TEXT,
                             relief="flat", bd=6, cursor="hand2",
                             command=_save)
        save_btn.pack(side="left", padx=(0, 8))
        close_btn = tk.Button(btn_row, text="Close", font=FB,
                             bg=SURF2, fg=TEXT, activebackground=ACCENT, activeforeground=TEXT,
                             relief="flat", bd=6, cursor="hand2",
                             command=win.destroy)
        close_btn.pack(side="left")

    def _step5(self):
        self._clear()
        is_aaf = self.workflow == "script_aaf"
        fmt    = "AAF" if is_aaf else "XML"
        self._section("STEP 5 — EXPORT {}".format(fmt))

        for lbl, var, wfn in [
            ("SEQUENCE NAME",
             self.seq_name,
             lambda r,v: tk.Entry(r, textvariable=v, font=FB,
                                  bg=SURF2, fg=TEXT, insertbackground=TEXT,
                                  relief="flat", bd=6, width=40)),
            ("GAP BETWEEN PARTS (s)",
             self.gap_var,
             lambda r,v: tk.Spinbox(r, from_=0.0, to=60.0,
                                    increment=0.1,
                                    format="%.1f",
                                    textvariable=v,
                                    font=FB, bg=SURF2, fg=TEXT,
                                    relief="flat", bd=6, width=6)),
        ]:
            row = tk.Frame(self.body, bg=BG); row.pack(fill="x", pady=(0,8))
            tk.Label(row, text=lbl, font=FL, bg=BG, fg=SUB,
                     width=26, anchor="w").pack(side="left")
            wfn(row, var).pack(side="left")

        ext = ".aaf" if is_aaf else ".xml"
        row3 = tk.Frame(self.body, bg=BG); row3.pack(fill="x", pady=(0,20))
        tk.Label(row3, text="SAVE {} TO".format(fmt), font=FL, bg=BG, fg=SUB,
                 width=26, anchor="w").pack(side="left")
        tk.Entry(row3, textvariable=self.out_path, font=FB,
                 bg=SURF2, fg=TEXT, insertbackground=TEXT,
                 relief="flat", bd=6, width=30).pack(side="left", padx=(0,8))
        self._btn(row3, "BROWSE",
                  lambda: self._pick_out(ext), small=True).pack(side="left")

        nav = tk.Frame(self.body, bg=BG); nav.pack(fill="x", pady=(8,0))
        self._btn(nav, "← BACK", self._step4).pack(side="left")
        self._btn(nav, "VIEW RECONCILE LOG", self._show_reconcile_log,
                  small=True).pack(side="left", padx=(12,0))
        self._btn(nav, "BUILD {}  →".format(fmt), self._build,
                  color=ACCENT).pack(side="right")

    def _pick_out(self, ext=".xml"):
        ftypes = [("AAF","*.aaf"),("All","*.*")] if ext == ".aaf" \
            else [("XML","*.xml"),("All","*.*")]
        p = filedialog.asksaveasfilename(
            title="Save {}".format(ext.upper().strip(".")),
            defaultextension=ext,
            filetypes=ftypes,
            initialfile="roughcut{}".format(ext))
        if p: self.out_path.set(p)

    def _build(self):
        if not self.out_path.get():
            messagebox.showwarning("No Path", "Choose a save location first.")
            return

        # ── Snapshot all state on the main thread before handing off ─────────
        # Build a lookup keyed by object id so we can match Step 4 row data
        # (seg_vars edits, skip_var) back to their result dicts.
        rv_by_id = {id(r["res"]): r for r in getattr(self, "_rv", [])}

        edited = []
        for res in self.results:
            st    = res.get("status", "")
            segs  = res.get("segments") or []
            is_vo = res.get("is_vo", False)

            rv_e  = rv_by_id.get(id(res))

            # User marked this clip IGNORE in the Step 4 review screen.
            if rv_e and rv_e["skip_var"].get():
                continue

            if st in ("no_file", "cancelled", "extract_failed", "no_transcript"):
                continue

            if is_vo:
                edited.append(res)
                continue

            if segs:
                # Apply any timecode edits made to segment rows in Step 4.
                if rv_e and rv_e.get("seg_vars"):
                    try:
                        edited_segs = [
                            (tc_secs(iv.get()), tc_secs(ov.get()))
                            for iv, ov in rv_e["seg_vars"]
                        ]
                        clean_segs = [(s, e) for s, e in edited_segs if e > s]
                    except Exception:
                        clean_segs = [(s, e) for s, e in segs if e > s]
                else:
                    clean_segs = [(s, e) for s, e in segs if e > s]
            else:
                # No Whisper match — check for manually entered timecodes.
                clean_segs = []
                if rv_e and rv_e.get("seg_vars"):
                    try:
                        iv, ov = rv_e["seg_vars"][0]
                        in_s   = tc_secs(iv.get())
                        out_s  = tc_secs(ov.get())
                        if out_s > in_s:
                            clean_segs = [(in_s, out_s)]
                    except Exception:
                        pass

            if not clean_segs:
                continue

            res = dict(res)
            res["segments"]  = clean_segs
            res["rec_in_s"]  = clean_segs[0][0]
            res["rec_out_s"] = clean_segs[-1][1]
            edited.append(res)

        int_assets = self._pool.get_interview_assets()
        try:
            vo_bin_data = {pi: vb for pi, vb in self.vo_bins.items()}
        except Exception:
            vo_bin_data = {}

        all_paths = []
        for paths in int_assets.values():
            all_paths.extend(paths)
        for vb in vo_bin_data.values():
            if hasattr(vb, "_audios"):
                all_paths.extend(vb._audios)
            elif hasattr(vb, "get_audio") and vb.get_audio():
                all_paths.append(vb.get_audio())
            if hasattr(vb, "_videos"):
                all_paths.extend(vb._videos)
            elif hasattr(vb, "get_video") and vb.get_video():
                all_paths.append(vb.get_video())

        seq_name = self.seq_name.get()
        gap_secs = self.gap_var.get()
        out_path = self.out_path.get()
        is_aaf   = (self.workflow == "script_aaf")
        fmt      = "AAF" if is_aaf else "XML"
        parts    = list(self.parts)
        vo_takes = dict(getattr(self, "_vo_takes_by_part", {}))

        # ── Replace step UI with a live progress screen ───────────────────────
        self._clear()
        self._section("STEP 5 — BUILDING {}".format(fmt))
        tk.Frame(self.body, bg=BG, height=30).pack()

        pbar = ttk.Progressbar(self.body, mode="indeterminate", length=520)
        pbar.pack(pady=(0, 14), padx=60)
        pbar.start(40)   # 40 ms tick → smooth bounce

        status_lbl = tk.Label(self.body, text="Starting…",
                              font=FB, bg=BG, fg=SUB,
                              wraplength=600, justify="center")
        status_lbl.pack()

        cancel_evt = threading.Event()
        cancel_btn = self._btn(self.body, "CANCEL",
                               lambda: cancel_evt.set(), small=True)
        cancel_btn.pack(pady=(20, 0))

        def _set_status(msg):
            """Thread-safe status update."""
            self.after(0, lambda m=msg: status_lbl.config(text=m))

        def _disable_cancel():
            try:
                cancel_btn.unbind("<Button-1>")
                cancel_btn.config(fg=SUB)
            except Exception:
                pass

        # ── Background worker ─────────────────────────────────────────────────
        def _worker():
            try:
                _t_build_start = time.perf_counter()

                _set_status("Probing media settings…")
                seq_w, seq_h, seq_fps, seq_sr = engines.probe_media_settings(
                    [p for p in all_paths if p])

                if is_aaf:
                    _, skipped = engines.build_aaf(
                        edited, int_assets, vo_bin_data, parts,
                        seq_name, gap_secs,
                        seq_fps=seq_fps, seq_sr=seq_sr,
                        vo_takes_with_offset=vo_takes,
                        out_path=out_path,
                        progress_cb=_set_status,
                        cancel_event=cancel_evt)
                    report_path = None
                else:
                    _set_status("Building XML…")
                    xmeml, skipped = engines.build_xml(
                        edited, int_assets, vo_bin_data, parts,
                        seq_name, gap_secs,
                        seq_w=seq_w, seq_h=seq_h,
                        seq_fps=seq_fps, seq_sr=seq_sr,
                        vo_takes_with_offset=vo_takes)
                    _set_status("Writing XML file…")
                    engines.write_xml(xmeml, out_path)
                    _set_status("Generating report…")
                    _, report_path = engines.generate_report(
                        edited, skipped, parts, seq_name, out_path)

                _build_total_s = time.perf_counter() - _t_build_start
                _set_status("{} build complete in {}  ({} clips, {} skipped)".format(
                    fmt, self._fmt_duration(_build_total_s),
                    len(edited), len(skipped)))

                n_inc  = len(edited)
                n_skip = len(skipped)

                def _finish():
                    pbar.stop()
                    _disable_cancel()
                    self._done(n_inc, n_skip, report_path)
                self.after(0, _finish)

            except engines.BuildCancelled:
                def _on_cancel():
                    pbar.stop()
                    _disable_cancel()
                    self._step5()   # return user to config screen
                self.after(0, _on_cancel)

            except Exception as exc:
                err = str(exc)

                def _on_err():
                    pbar.stop()
                    _disable_cancel()
                    messagebox.showerror("Build Error", err)
                    self._step5()   # return user to config screen
                self.after(0, _on_err)

        threading.Thread(target=_worker, daemon=True).start()

    def _done(self, included, skipped, report_path=None):
        self._clear()
        fmt = "AAF" if self.workflow == "script_aaf" else "XML"
        tk.Frame(self.body, bg=BG, height=40).pack()
        tk.Label(self.body, text="✓",
                 font=("Courier New",52,"bold"), bg=BG, fg=SUCCESS).pack()
        tk.Label(self.body, text="{} EXPORTED".format(fmt),
                 font=FL, bg=BG, fg=TEXT).pack(pady=(6,2))
        tk.Label(self.body, text=self.out_path.get(),
                 font=FB, bg=BG, fg=SUB).pack()
        if report_path:
            tk.Label(self.body, text="Report: {}".format(report_path),
                     font=FB, bg=BG, fg=SUB).pack(pady=(2,0))
        tk.Frame(self.body, bg=BG, height=16).pack()

        card = tk.Frame(self.body, bg=SURF,
                        highlightbackground=BORDER, highlightthickness=1)
        card.pack(padx=60, fill="x")
        for lbl, val in [
            ("Clips in timeline", str(included)),
            ("Skipped",          str(skipped)),
            ("Parts / markers",  str(len(self.parts))),
        ]:
            r = tk.Frame(card, bg=SURF); r.pack(fill="x", padx=16, pady=3)
            tk.Label(r, text="{:<22}".format(lbl),
                     font=FB, bg=SURF, fg=SUB).pack(side="left")
            tk.Label(r, text=val, font=FB, bg=SURF, fg=TEXT).pack(side="left")
        tk.Frame(card, bg=BG, height=8).pack()
        tk.Frame(self.body, bg=BG, height=20).pack()
        self._btn(self.body, "START NEW EPISODE", self._reset).pack()

    # ── AAF → XML workflow ─────────────────────────────────────────────────────

    def _aaf_step1(self):
        self._clear()
        self._prefetch_aaf_media    = []    # reset on fresh entry
        self._aaf_step1_next_added  = False
        self._section("STEP 1 — LOAD PRO TOOLS AAF EXPORT  (AAF → XML)")

        card = tk.Frame(self.body, bg=SURF,
                        highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x")
        if HAS_DND:
            card.drop_target_register(DND_FILES)
            card.dnd_bind("<<Drop>>",
                          lambda e: self._aaf_load(e.data.strip().strip("{}")))

        inner = tk.Frame(card, bg=SURF)
        inner.pack(padx=40, pady=30)
        self._aaf_s1 = tk.Label(inner,
                             text="Drop .aaf file here  or  click below"
                                  if HAS_DND else "Click below to open AAF file",
                             font=FB, bg=SURF, fg=SUB,
                             wraplength=720, justify="center")
        self._aaf_s1.pack(pady=(0,14))

        tk.Label(inner,
                 text="Export from Pro Tools: File \u2192 Export \u2192 As AAF/OMF\n"
                      "Format: AAF  \u00b7  Audio: Link to Source Media  \u00b7  Include all (or selected) tracks",
                 font=FB, bg=SURF, fg=SUB, justify="center").pack(pady=(0,14))

        self._btn(inner, "OPEN AAF FILE (.aaf)",
                  lambda: self._aaf_load(
                      filedialog.askopenfilename(
                          filetypes=[("AAF","*.aaf"),("All","*.*")]))).pack()

        # ── Media pre-load (optional — populates Step 2 video/audio pools) ───
        self._make_prefetch_panel(
            self.body, self._prefetch_aaf_media,
            title="VIDEO & AUDIO FILES",
            hint="Drop video and/or reference audio files here — they will be pre-loaded into Step 2  (optional)",
        )

        nav = tk.Frame(self.body, bg=BG); nav.pack(fill="x", pady=(8,0))
        self._btn(nav, "\u2190 HOME", self._home).pack(side="left")
        self._aaf_step1_nav = nav   # NEXT button appended here once AAF is loaded

    def _aaf_load(self, path):
        if not path or not os.path.isfile(path): return
        self._aaf_s1.config(text="Parsing AAF\u2026", fg=SUB)
        self.update_idletasks()
        try:
            parsed = parse_aaf_session(path)
        except Exception as e:
            messagebox.showerror("AAF Error", str(e)); return

        if not parsed["tracks"]:
            messagebox.showerror("No Tracks",
                "No audio tracks found in this AAF.\n"
                "Make sure the session has track EDL data and was exported\n"
                "with 'Link to Source Media' (not embedded audio).")
            return

        self._aaf_data = parsed
        self._aaf_path = os.path.abspath(path)
        total_clips  = sum(len(t["clips"]) for t in parsed["tracks"])

        # Collect unique source base names
        sources = {}
        for track in parsed["tracks"]:
            for clip in track["clips"]:
                base = get_clip_base_name(clip["clip_name"])
                if base is None:
                    continue
                sources.setdefault(base, set()).add(track["name"])
        self._aaf_sources       = sorted(sources.keys())
        self._aaf_source_tracks = sources   # base_name -> set of track names

        self._aaf_s1.config(
            text="\u2713  {}  \u00b7  {} tracks  \u00b7  {} clips  \u00b7  {} sources".format(
                parsed["session_name"], len(parsed["tracks"]),
                total_clips, len(self._aaf_sources)),
            fg=SUCCESS)

        self.seq_name.set(parsed["session_name"])
        if hasattr(self, "_aaf_step1_nav") and not getattr(self, "_aaf_step1_next_added", False):
            self._btn(self._aaf_step1_nav, "NEXT  →  ASSIGN VIDEO",
                      self._aaf_step2, color=ACCENT).pack(side="right")
            self._aaf_step1_next_added = True
        elif not hasattr(self, "_aaf_step1_nav"):
            self.after(350, self._aaf_step2)

    def _aaf_step2(self):
        self._clear()
        self._section("STEP 2 — ASSIGN VIDEO MEDIA")

        total_clips = sum(len(t["clips"]) for t in self._aaf_data["tracks"])
        tk.Label(self.body,
                 text="{} clips from {} tracks  \u00b7  {} unique sources.  "
                      "Add video files below, then assign each source clip to its video.".format(
                          total_clips, len(self._aaf_data["tracks"]),
                          len(self._aaf_sources)),
                 font=FB, bg=BG, fg=SUB, wraplength=860).pack(anchor="w", pady=(0,10))

        sf = self._scroll_frame(self.body, height=500)

        # ── Video file pool (collapsible) ──────────────────────────────────────
        pool_frame = tk.Frame(sf, bg=SURF,
                              highlightbackground=BORDER, highlightthickness=1)
        pool_frame.pack(fill="x", pady=(0,8), padx=2)

        v_body = tk.Frame(pool_frame, bg=SURF)   # collapsible content
        v_open = [True]

        ph = tk.Frame(pool_frame, bg=SURF, cursor="hand2")
        ph.pack(fill="x", padx=12, pady=(10,4))

        v_arrow = tk.Label(ph, text="\u25bc", font=FB, bg=SURF, fg=ACCENT,
                           cursor="hand2")
        v_arrow.pack(side="left", padx=(0, 6))
        tk.Label(ph, text="VIDEO FILES", font=FL, bg=SURF, fg=ACCENT,
                 cursor="hand2").pack(side="left")
        self._aaf_count_lbl = tk.Label(ph, text="0 files", font=FB, bg=SURF, fg=SUB)
        self._aaf_count_lbl.pack(side="right")

        def _toggle_vpool(arr=v_arrow, body=v_body, state=v_open):
            if state[0]:
                body.pack_forget()
                arr.config(text="\u25b6")
                state[0] = False
            else:
                body.pack(fill="x")
                arr.config(text="\u25bc")
                state[0] = True

        for w in (ph, v_arrow):
            w.bind("<Button-1>", lambda e: _toggle_vpool())

        # Body contents
        v_body.pack(fill="x")
        self._aaf_video_paths = []
        self._aaf_file_list   = tk.Frame(v_body, bg=SURF)
        self._aaf_file_list.pack(fill="x", padx=12)

        btn_row = tk.Frame(v_body, bg=SURF)
        btn_row.pack(anchor="w", padx=12, pady=(4,10))
        self._btn(btn_row, "+ BROWSE FILES",  self._aaf_browse_files,
                  small=True).pack(side="left", padx=(0,8))
        self._btn(btn_row, "+ BROWSE FOLDER", self._aaf_browse_folder,
                  small=True).pack(side="left", padx=(0,8))
        self._aaf_status_lbl = tk.Label(btn_row, text="", font=FB, bg=SURF, fg=SUB)
        self._aaf_status_lbl.pack(side="left")

        if HAS_DND:
            for w in [pool_frame, ph]:
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>",
                           lambda e: self._aaf_add_video_batch(engines.parse_dnd(e.data)))

        # ── Reference audio pool (collapsible) ────────────────────────────────
        apool_frame = tk.Frame(sf, bg=SURF,
                               highlightbackground=BORDER, highlightthickness=1)
        apool_frame.pack(fill="x", pady=(0, 8), padx=2)

        a_body = tk.Frame(apool_frame, bg=SURF)   # collapsible content
        a_open = [True]

        aph = tk.Frame(apool_frame, bg=SURF, cursor="hand2")
        aph.pack(fill="x", padx=12, pady=(10, 4))

        a_arrow = tk.Label(aph, text="\u25bc", font=FB, bg=SURF, fg=ACCENT,
                           cursor="hand2")
        a_arrow.pack(side="left", padx=(0, 6))
        tk.Label(aph, text="REFERENCE AUDIO FILES", font=FL, bg=SURF, fg=ACCENT,
                 cursor="hand2").pack(side="left")
        tk.Label(aph, text="  (camera / scratch audio for dual-system sync \u2014 optional)",
                 font=FB, bg=SURF, fg=SUB).pack(side="left")
        self._aaf_audio_count_lbl = tk.Label(aph, text="0 files", font=FB,
                                             bg=SURF, fg=SUB)
        self._aaf_audio_count_lbl.pack(side="right")

        def _toggle_apool(arr=a_arrow, body=a_body, state=a_open):
            if state[0]:
                body.pack_forget()
                arr.config(text="\u25b6")
                state[0] = False
            else:
                body.pack(fill="x")
                arr.config(text="\u25bc")
                state[0] = True

        for w in (aph, a_arrow):
            w.bind("<Button-1>", lambda e: _toggle_apool())

        # Body contents
        a_body.pack(fill="x")
        self._aaf_audio_paths     = []
        self._aaf_audio_file_list = tk.Frame(a_body, bg=SURF)
        self._aaf_audio_file_list.pack(fill="x", padx=12)

        abtn_row = tk.Frame(a_body, bg=SURF)
        abtn_row.pack(anchor="w", padx=12, pady=(4, 10))
        self._btn(abtn_row, "+ BROWSE FILES",  self._aaf_browse_audio_files,
                  small=True).pack(side="left", padx=(0, 8))
        self._btn(abtn_row, "+ BROWSE FOLDER", self._aaf_browse_audio_folder,
                  small=True).pack(side="left", padx=(0, 8))

        if HAS_DND:
            for w in [apool_frame, aph]:
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>",
                           lambda e: self._aaf_add_audio_batch(engines.parse_dnd(e.data)))

        # ── Per-source assignment dropdowns ────────────────────────────────────
        assign_frame = tk.Frame(sf, bg=SURF,
                                highlightbackground=BORDER, highlightthickness=1)
        assign_frame.pack(fill="x", pady=(0,8), padx=2)

        ah = tk.Frame(assign_frame, bg=SURF)
        ah.pack(fill="x", padx=12, pady=(10,4))
        tk.Label(ah, text="SOURCE \u2192 VIDEO ASSIGNMENTS", font=FL, bg=SURF, fg=ACCENT).pack(side="left")
        tk.Label(ah, text="  (assign each clip source to a video file, or leave as no video)",
                 font=FB, bg=SURF, fg=SUB).pack(side="left")
        self._btn(ah, "REMOVE UNASSIGNED",
                  self._aaf_remove_unassigned_sources, small=True).pack(side="right")

        # Column headers
        col_hdr = tk.Frame(assign_frame, bg=SURF)
        col_hdr.pack(fill="x", padx=12, pady=(2, 0))
        tk.Label(col_hdr, text="VIDEO FILE", font=FB, bg=SURF, fg=SUB,
                 width=44, anchor="w").pack(side="right")
        tk.Label(col_hdr, text="PT TRACK", font=FB, bg=SURF, fg=SUB,
                 width=16, anchor="w").pack(side="right", padx=(4, 0))
        tk.Label(col_hdr, text="SYNC", font=FB, bg=SURF, fg=SUB,
                 width=14, anchor="w").pack(side="right", padx=(4, 0))
        tk.Label(col_hdr, text="\u2715", font=FB, bg=SURF, fg=SUB,
                 width=3).pack(side="right", padx=(4, 0))
        tk.Label(col_hdr, text="SOURCE CLIP", font=FB, bg=SURF, fg=SUB,
                 anchor="w").pack(side="left", fill="x", expand=True)

        self._aaf_assign_frame = tk.Frame(assign_frame, bg=SURF)
        self._aaf_assign_frame.pack(fill="x", padx=12, pady=(0,10))
        self._aaf_source_file_vars      = {}   # base -> StringVar  (video filename)
        self._aaf_source_sync_vars      = {}   # base -> BooleanVar (sync applied flag)
        self._aaf_source_syncaudio_vars = {}   # base -> StringVar  (ref audio path)
        self._aaf_source_offset_vars    = {}   # base -> StringVar  (offset seconds)
        self._aaf_source_sync_label_vars= {}   # base -> StringVar  (button display text)
        self._aaf_sync_btns             = {}   # base -> Button widget (current rebuild)

        self._rebuild_aaf_source_rows()

        # ── Settings ───────────────────────────────────────────────────────────
        settings_frame = tk.Frame(sf, bg=SURF,
                                  highlightbackground=BORDER, highlightthickness=1)
        settings_frame.pack(fill="x", pady=(0,8), padx=2)

        sh = tk.Frame(settings_frame, bg=SURF)
        sh.pack(fill="x", padx=12, pady=(10,6))
        tk.Label(sh, text="SETTINGS", font=FL, bg=SURF, fg=ACCENT).pack(side="left")

        row_grp = tk.Frame(settings_frame, bg=SURF)
        row_grp.pack(anchor="w", padx=12, pady=(0,4))
        tk.Label(row_grp, text="Video tracks:", font=FB, bg=SURF, fg=TEXT,
                 width=18, anchor="w").pack(side="left")
        self._aaf_group_var = tk.StringVar(value="single")
        for val, lbl in [("single",    "Consolidate to one video track"),
                         ("by_source", "Group by source video file"),
                         ("by_track",  "Group by audio track")]:
            tk.Radiobutton(row_grp, text=lbl, variable=self._aaf_group_var, value=val,
                           font=FB, bg=SURF, fg=TEXT, activebackground=SURF,
                           selectcolor=BG).pack(side="left", padx=(0,14))

        row_fps = tk.Frame(settings_frame, bg=SURF)
        row_fps.pack(anchor="w", padx=12, pady=(0,10))
        tk.Label(row_fps, text="Output FPS:", font=FB, bg=SURF, fg=TEXT,
                 width=18, anchor="w").pack(side="left")
        self._aaf_fps_var = tk.StringVar(value="29.97")
        fps_entry = tk.Entry(row_fps, textvariable=self._aaf_fps_var,
                             font=FB, bg=SURF2, fg=TEXT,
                             insertbackground=TEXT, relief="flat", width=8)
        fps_entry.pack(side="left")
        tk.Label(row_fps, text="  (29.97 DF for standard broadcast; override if needed)",
                 font=FB, bg=SURF, fg=SUB).pack(side="left")

        nav = tk.Frame(self.body, bg=BG); nav.pack(fill="x", pady=(8,0))
        self._btn(nav, "\u2190 BACK",    self._aaf_step1).pack(side="left")
        self._btn(nav, "SAVE SETUP",     self._aaf_save_setup).pack(side="left", padx=(8,0))
        self._btn(nav, "LOAD SETUP",     self._aaf_load_setup).pack(side="left", padx=(4,0))
        self._aaf_build_btn = self._btn(nav, "BUILD XML  \u2192", self._aaf_build,
                                        color=ACCENT)
        self._aaf_build_btn.pack(side="right")

        # Pre-load any video/audio files dropped at Step 1
        prefetch = getattr(self, "_prefetch_aaf_media", [])
        if prefetch:
            vp = [p for p in prefetch if is_video(p)]
            ap = [p for p in prefetch if is_audio(p)]
            if vp: self._aaf_add_video_batch(vp)
            if ap: self._aaf_add_audio_batch(ap)

        # Build progress (hidden until BUILD XML is clicked)
        self._aaf_prog_frame = tk.Frame(self.body, bg=BG)
        self._aaf_prog_frame.pack(fill="x", pady=(6, 0))
        self._aaf_prog_lbl = tk.Label(self._aaf_prog_frame, text="", font=FB,
                                      bg=BG, fg=SUB, anchor="w")
        self._aaf_prog_lbl.pack(fill="x", pady=(0, 3))
        self._aaf_prog_bar = _FlatProgressBar(self._aaf_prog_frame, height=4)
        self._aaf_prog_bar.pack(fill="x")
        self._aaf_prog_frame.pack_forget()   # hidden until build starts

    def _rebuild_aaf_source_rows(self):
        """Rebuild per-source video assignment dropdowns based on current video pool."""
        for w in self._aaf_assign_frame.winfo_children():
            w.destroy()
        self._aaf_sync_btns = {}   # stale widget refs — repopulated below

        options = ["— no video —"] + [basename(p) for p in self._aaf_video_paths]

        for base in self._aaf_sources:
            # ── Per-source StringVars (survive rebuilds) ──────────────────────
            if base not in self._aaf_source_file_vars:
                self._aaf_source_file_vars[base] = tk.StringVar(value="— no video —")
            if base not in self._aaf_source_sync_vars:
                self._aaf_source_sync_vars[base] = tk.BooleanVar(value=False)
            if base not in self._aaf_source_syncaudio_vars:
                self._aaf_source_syncaudio_vars[base] = tk.StringVar(value="")
            if base not in self._aaf_source_offset_vars:
                self._aaf_source_offset_vars[base] = tk.StringVar(value="0.000")
            if base not in self._aaf_source_sync_label_vars:
                self._aaf_source_sync_label_vars[base] = tk.StringVar(value="")

            sv = self._aaf_source_file_vars[base]
            if sv.get() not in options:
                sv.set("— no video —")

            # ── Row ───────────────────────────────────────────────────────────
            container = tk.Frame(self._aaf_assign_frame, bg=SURF)
            container.pack(fill="x", pady=2)

            row = tk.Frame(container, bg=SURF)
            row.pack(fill="x")

            # ✕ remove (far right)
            rm_lbl = tk.Label(row, text=" \u2715 ", font=FB, bg=SURF, fg=SUB,
                              cursor="hand2", width=3)
            rm_lbl.pack(side="right")
            rm_lbl.bind("<Button-1>",
                        lambda e, b=base, c=container: self._aaf_remove_source(b, c))

            # SYNC button
            sync_btn = self._btn(row, "SYNC",
                                 lambda b=base: self._aaf_do_sync(b),
                                 small=True)
            sync_btn.pack(side="right", padx=(6, 0))
            self._aaf_sync_btns[base] = sync_btn
            self._aaf_refresh_sync_btn(base, sync_btn)

            # Video dropdown
            _FlatDropdown(row, textvariable=sv, values=options,
                          state="readonly", font=FB, width=44).pack(side="right")

            # PT track label
            tracks_str = ", ".join(sorted(self._aaf_source_tracks.get(base, set())))
            tk.Label(row, text=tracks_str, font=FB, bg=SURF, fg=SUB,
                     width=16, anchor="w").pack(side="right", padx=(4, 0))

            # Source name (expands)
            name_lbl = tk.Label(row, text=base, font=FB, bg=SURF, fg=TEXT, anchor="w")
            name_lbl.pack(side="left", fill="x", expand=True)
            self._tooltip(name_lbl, base)

        self._aaf_auto_match()

    # FPS priority order: higher quality / higher frame rate wins
    _FPS_PRIORITY = [
        120.0, 119.88, 60.0, 59.94, 50.0, 48.0, 47.952,
        30.0, 29.97, 25.0, 24.0, 23.976, 23.98,
    ]

    def _aaf_schedule_detect_fps(self):
        """Debounce: cancel any pending detect job and schedule a fresh one."""
        job = getattr(self, "_aaf_fps_job", None)
        if job:
            self.after_cancel(job)
        self._aaf_set_status("Detecting frame rate…")
        self._aaf_fps_job = self.after(300, self._aaf_detect_fps)

    def _aaf_detect_fps(self):
        """Probe pooled video files off the main thread; update FPS field when done."""
        self._aaf_fps_job = None
        paths = list(self._aaf_video_paths)   # snapshot so thread is safe
        if not paths:
            return

        priority = self._FPS_PRIORITY

        def _probe():
            import subprocess, json as _json
            best_fps  = None
            best_rank = len(priority)
            for vp in paths:
                try:
                    result = subprocess.run(
                        ["ffprobe", "-v", "quiet", "-print_format", "json",
                         "-show_streams", vp],
                        capture_output=True, text=True, timeout=10)
                    info = _json.loads(result.stdout)
                    for stream in info.get("streams", []):
                        if stream.get("codec_type") != "video":
                            continue
                        r = stream.get("r_frame_rate", "")
                        if "/" in r:
                            num, den = r.split("/")
                            fps = round(int(num) / int(den), 3)
                        else:
                            try:    fps = float(r)
                            except: continue
                        rank = len(priority)
                        for i, ref in enumerate(priority):
                            if abs(fps - ref) < 0.1:
                                rank = i
                                break
                        if rank < best_rank or best_fps is None:
                            best_rank = rank
                            best_fps  = fps
                except Exception:
                    continue
            return best_fps

        def _done(fut):
            try:
                best_fps = fut.result()
            except Exception:
                self.after(0, lambda: self._aaf_set_status(""))
                return
            def _apply():
                if best_fps is not None:
                    display = "{:.3f}".format(best_fps).rstrip("0").rstrip(".")
                    self._aaf_fps_var.set(display)
                self._aaf_set_status("")
            self.after(0, _apply)

        try:
            from concurrent.futures import ThreadPoolExecutor
            ex = ThreadPoolExecutor(max_workers=1)
            fut = ex.submit(_probe)
            fut.add_done_callback(_done)
            ex.shutdown(wait=False)
        except Exception:
            pass   # ffprobe not available or thread error — leave FPS as typed

    def _aaf_auto_match(self):
        """Auto-assign video files to unassigned sources by name similarity."""
        if not self._aaf_video_paths:
            return
        options = ["— no video —"] + [basename(p) for p in self._aaf_video_paths]
        for base, sv in self._aaf_source_file_vars.items():
            if sv.get() != "— no video —":
                continue  # never override a manual assignment
            vp = match_source_to_video(base, self._aaf_video_paths)
            if vp:
                fn = basename(vp)
                if fn in options:
                    sv.set(fn)

    def _aaf_remove_source(self, base, container):
        """Remove one source from the assignment list (does not affect the AAF parse)."""
        if base in self._aaf_sources:
            self._aaf_sources.remove(base)
        for d in (self._aaf_source_file_vars, self._aaf_source_sync_vars,
                  self._aaf_source_syncaudio_vars, self._aaf_source_offset_vars):
            d.pop(base, None)
        container.destroy()

    def _aaf_remove_unassigned_sources(self):
        """Remove all sources that have no video file assigned."""
        to_remove = [b for b in self._aaf_sources
                     if self._aaf_source_file_vars.get(
                         b, tk.StringVar(value="— no video —")).get() == "— no video —"]
        if not to_remove:
            messagebox.showinfo("Remove Unassigned",
                                "Every source already has a video file assigned.")
            return
        msg = "Remove {} unassigned source{}?\n\n{}{}".format(
            len(to_remove),
            "s" if len(to_remove) != 1 else "",
            "\n".join(to_remove[:10]),
            "\n…and {} more".format(len(to_remove) - 10) if len(to_remove) > 10 else "")
        if not messagebox.askyesno("Remove Unassigned", msg):
            return
        for base in to_remove:
            self._aaf_sources.remove(base)
            for d in (self._aaf_source_file_vars, self._aaf_source_sync_vars,
                      self._aaf_source_syncaudio_vars, self._aaf_source_offset_vars):
                d.pop(base, None)
        self._rebuild_aaf_source_rows()

    def _aaf_refresh_sync_btn(self, base, btn):
        """Set button text/colour to reflect the stored sync state."""
        label = self._aaf_source_sync_label_vars.get(base)
        text  = label.get() if label else ""
        if not text:
            btn.config(text="SYNC", fg=SUB)
        elif text.startswith("\u2713"):          # ✓ green
            btn.config(text=text, fg=SUCCESS)
        elif text.startswith("\u26a0"):          # ⚠ orange or red
            clr = "#e05050" if "no match" in text else "#e0a030"
            btn.config(text=text, fg=clr)
        else:
            btn.config(text=text, fg=SUB)

    def _aaf_do_sync(self, base):
        """One-click sync: auto-match reference audio then detect offset."""
        btn = self._aaf_sync_btns.get(base)

        # Auto-match reference audio from pool if not already set
        audio_var = self._aaf_source_syncaudio_vars.get(base)
        if audio_var and not audio_var.get() and self._aaf_audio_paths:
            matched = match_source_to_video(base, self._aaf_audio_paths)
            if matched:
                audio_var.set(matched)

        if not audio_var or not audio_var.get():
            messagebox.showwarning("No Reference Audio",
                "Add reference audio files to the pool first.\n"
                "PostBridge will match them to sources by name.")
            return

        # Validate video assignment
        fn = self._aaf_source_file_vars.get(base, tk.StringVar()).get()
        if fn == "— no video —":
            messagebox.showwarning("No Video",
                "Assign a video file to this source before syncing.")
            return

        path_by_fn = {basename(p): p for p in self._aaf_video_paths}
        vp = path_by_fn.get(fn)
        if not vp:
            return

        ap = audio_var.get()
        if not os.path.isfile(ap):
            messagebox.showwarning("Missing Audio",
                "Reference audio file not found:\n{}".format(ap))
            return

        # Disable button while running
        if btn:
            btn.config(text="SYNCING\u2026", state="disabled", fg=SUB)

        def _run():
            return detect_sync_offset(vp, ap)

        def _done(fut):
            try:
                offset, confidence = fut.result()
            except Exception as exc:
                def _err():
                    lv = self._aaf_source_sync_label_vars.get(base)
                    if lv: lv.set("\u26a0 error")
                    if btn:
                        btn.config(text="\u26a0 error", fg="#e05050", state="normal")
                    messagebox.showerror("Sync Failed", str(exc))
                self.after(0, _err)
                return

            def _apply():
                offset_var = self._aaf_source_offset_vars.get(base)
                if offset_var:
                    offset_var.set("{:.3f}".format(offset))
                sync_var = self._aaf_source_sync_vars.get(base)
                if sync_var:
                    sync_var.set(True)

                pct = int(confidence * 100)
                if confidence >= 0.5:
                    lbl = "\u2713 {:.3f}s ({:d}%)".format(offset, pct)
                elif confidence >= 0.25:
                    lbl = "\u26a0 {:.3f}s ({:d}%)".format(offset, pct)
                else:
                    lbl = "\u26a0 no match ({:d}%)".format(pct)

                lv = self._aaf_source_sync_label_vars.get(base)
                if lv: lv.set(lbl)
                if btn:
                    btn.config(state="normal")
                    self._aaf_refresh_sync_btn(base, btn)

            self.after(0, _apply)

        from concurrent.futures import ThreadPoolExecutor
        ex  = ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(_run)
        fut.add_done_callback(_done)
        ex.shutdown(wait=False)

    # ── Reference-audio pool ─────────────────────────────────────────────────

    def _aaf_add_audio(self, path, _batch=False):
        """Add one reference audio/video file to the audio pool."""
        if not path or not os.path.isfile(path): return
        if os.path.basename(path).startswith("._"): return
        if path in self._aaf_audio_paths: return
        if not is_media(path): return
        self._aaf_audio_paths.append(path)
        row = tk.Frame(self._aaf_audio_file_list, bg=SURF2,
                       highlightbackground=BORDER, highlightthickness=1)
        row.pack(fill="x", pady=1)
        tk.Label(row, text="AUDIO", font=("Courier New", 9, "bold"),
                 bg="#2a2a3d", fg="#8888ff", padx=5, pady=3).pack(side="left")
        tk.Label(row, text=basename(path), font=FB, bg=SURF2, fg=TEXT,
                 padx=6, anchor="w").pack(side="left", fill="x", expand=True)
        rm = tk.Label(row, text=" \u2715 ", font=FB, bg=SURF2, fg=SUB,
                      cursor="hand2", padx=4)
        rm.pack(side="right")
        rm.bind("<Button-1>", lambda e, p=path, r=row: self._aaf_remove_audio(p, r))
        if not _batch:
            n = len(self._aaf_audio_paths)
            self._aaf_audio_count_lbl.config(
                text="{} file{}".format(n, "s" if n != 1 else ""))

    def _aaf_remove_audio(self, path, row):
        if path in self._aaf_audio_paths:
            self._aaf_audio_paths.remove(path)
        row.destroy()
        n = len(self._aaf_audio_paths)
        self._aaf_audio_count_lbl.config(
            text="{} file{}".format(n, "s" if n != 1 else ""))

    def _aaf_browse_audio_files(self):
        ext_glob = " ".join("*" + e for e in sorted(MEDIA_EXTS))
        paths = filedialog.askopenfilenames(
            title="Select reference audio / video files",
            filetypes=[("Audio / video", ext_glob), ("All files", "*.*")])
        self._aaf_add_audio_batch(paths)

    def _aaf_browse_audio_folder(self):
        folder = filedialog.askdirectory(title="Select folder with reference audio files")
        if not folder: return
        paths = []
        for root, _, files in os.walk(folder):
            for fn in sorted(files):
                if os.path.splitext(fn)[1].lower() in MEDIA_EXTS:
                    paths.append(os.path.join(root, fn))
        self._aaf_add_audio_batch(paths)

    def _aaf_add_audio_batch(self, paths):
        """Add multiple reference audio files efficiently."""
        for p in paths:
            self._aaf_add_audio(p, _batch=True)
        n = len(self._aaf_audio_paths)
        self._aaf_audio_count_lbl.config(
            text="{} file{}".format(n, "s" if n != 1 else ""))

    # ── Video pool ───────────────────────────────────────────────────────────

    def _aaf_add_video(self, path, _batch=False):
        """Add one video file to the pool.

        Pass _batch=True when adding many files at once; the caller is then
        responsible for calling _rebuild_aaf_source_rows() and
        _aaf_schedule_detect_fps() once after the loop finishes.
        """
        if not path or not os.path.isfile(path): return
        if os.path.basename(path).startswith("._"): return
        if path in self._aaf_video_paths: return
        if not is_video(path): return
        self._aaf_video_paths.append(path)
        row = tk.Frame(self._aaf_file_list, bg=SURF2,
                       highlightbackground=BORDER, highlightthickness=1)
        row.pack(fill="x", pady=1)
        tk.Label(row, text="VIDEO", font=("Courier New",9,"bold"),
                 bg="#2a3d2a", fg=SUCCESS, padx=5, pady=3).pack(side="left")
        tk.Label(row, text=basename(path), font=FB, bg=SURF2, fg=TEXT,
                 padx=6, anchor="w").pack(side="left", fill="x", expand=True)
        rm = tk.Label(row, text=" \u2715 ", font=FB, bg=SURF2, fg=SUB,
                      cursor="hand2", padx=4)
        rm.pack(side="right")
        rm.bind("<Button-1>", lambda e, p=path, r=row: self._aaf_remove_video(p, r))
        if not _batch:
            n = len(self._aaf_video_paths)
            self._aaf_count_lbl.config(text="{} file{}".format(n, "s" if n != 1 else ""))
            if hasattr(self, "_aaf_assign_frame"):
                self._rebuild_aaf_source_rows()
            self._aaf_schedule_detect_fps()

    def _aaf_remove_video(self, path, row):
        if path in self._aaf_video_paths:
            self._aaf_video_paths.remove(path)
        row.destroy()
        n = len(self._aaf_video_paths)
        self._aaf_count_lbl.config(text="{} file{}".format(n, "s" if n != 1 else ""))
        if hasattr(self, "_aaf_assign_frame"):
            self._rebuild_aaf_source_rows()

    def _aaf_remove_unmatched(self):
        """Remove any pooled video files that aren't assigned to any source clip."""
        assigned_fns = {sv.get() for sv in self._aaf_source_file_vars.values()
                        if sv.get() != "— no video —"}
        to_remove = [p for p in self._aaf_video_paths
                     if basename(p) not in assigned_fns]
        if not to_remove:
            messagebox.showinfo("Remove Unmatched",
                                "All files in the pool are currently assigned.")
            return
        msg = ("Remove {} unmatched file{} from the pool?\n\n{}".format(
            len(to_remove),
            "s" if len(to_remove) != 1 else "",
            "\n".join(basename(p) for p in to_remove[:10])
            + ("\n…and {} more".format(len(to_remove) - 10) if len(to_remove) > 10 else "")))
        if not messagebox.askyesno("Remove Unmatched", msg):
            return
        # Destroy the file list rows for removed paths, then rebuild
        for w in self._aaf_file_list.winfo_children():
            w.destroy()
        self._aaf_video_paths = [p for p in self._aaf_video_paths
                                  if p not in to_remove]
        # Re-render the remaining file rows
        for path in self._aaf_video_paths:
            row = tk.Frame(self._aaf_file_list, bg=SURF2,
                           highlightbackground=BORDER, highlightthickness=1)
            row.pack(fill="x", pady=1)
            tk.Label(row, text="VIDEO", font=("Courier New", 9, "bold"),
                     bg="#2a3d2a", fg=SUCCESS, padx=5, pady=3).pack(side="left")
            tk.Label(row, text=basename(path), font=FB, bg=SURF2, fg=TEXT,
                     padx=6, anchor="w").pack(side="left", fill="x", expand=True)
            rm = tk.Label(row, text=" \u2715 ", font=FB, bg=SURF2, fg=SUB,
                          cursor="hand2", padx=4)
            rm.pack(side="right")
            rm.bind("<Button-1>", lambda e, p=path, r=row: self._aaf_remove_video(p, r))
        n = len(self._aaf_video_paths)
        self._aaf_count_lbl.config(text="{} file{}".format(n, "s" if n != 1 else ""))
        self._rebuild_aaf_source_rows()

    def _aaf_browse_files(self):
        from utils import VIDEO_EXTS
        ext_glob = " ".join("*" + e for e in sorted(VIDEO_EXTS))
        paths = filedialog.askopenfilenames(
            title="Select video files",
            filetypes=[("Video files", ext_glob), ("All files", "*.*")])
        self._aaf_add_video_batch(paths)

    def _aaf_browse_folder(self):
        from utils import VIDEO_EXTS
        folder = filedialog.askdirectory(title="Select folder with video files")
        if not folder: return
        self._aaf_set_status("Scanning folder…")
        paths = []
        for root, _, files in os.walk(folder):
            for fn in sorted(files):
                if os.path.splitext(fn)[1].lower() in VIDEO_EXTS:
                    paths.append(os.path.join(root, fn))
        self._aaf_add_video_batch(paths)

    def _aaf_add_video_batch(self, paths):
        """Add multiple video files efficiently — one UI rebuild at the end."""
        total = len(paths)
        added = 0
        for i, p in enumerate(paths):
            if total > 1:
                self._aaf_set_status("Adding file {} of {}…".format(i + 1, total))
                if i % 5 == 0:
                    self.update_idletasks()
            self._aaf_add_video(p, _batch=True)
            added += 1

        n = len(self._aaf_video_paths)
        self._aaf_count_lbl.config(text="{} file{}".format(n, "s" if n != 1 else ""))
        if hasattr(self, "_aaf_assign_frame"):
            self._aaf_set_status("Building assignment rows…")
            self.update_idletasks()
            self._rebuild_aaf_source_rows()
        self._aaf_schedule_detect_fps()

    def _aaf_set_status(self, msg):
        """Update the import status label (no-op if label not yet created)."""
        if hasattr(self, "_aaf_status_lbl"):
            self._aaf_status_lbl.config(text=msg)
            self.update_idletasks()

    def _aaf_sidecar_path(self):
        if not hasattr(self, "_aaf_path") or not self._aaf_path:
            return None
        return os.path.splitext(self._aaf_path)[0] + "_setup.json"

    def _aaf_save_setup(self):
        data = {
            "version":     2,
            "aaf":         getattr(self, "_aaf_path", ""),
            "video_paths": self._aaf_video_paths,
            "audio_paths": self._aaf_audio_paths,
            "assignments": {base: sv.get()
                            for base, sv in self._aaf_source_file_vars.items()},
            "sync": {
                base: {
                    "enabled":    self._aaf_source_sync_vars[base].get(),
                    "audio_path": self._aaf_source_syncaudio_vars[base].get(),
                    "offset":     self._aaf_source_offset_vars[base].get(),
                    "label":      self._aaf_source_sync_label_vars.get(
                                      base, tk.StringVar()).get(),
                }
                for base in self._aaf_sources
                if base in self._aaf_source_sync_vars
            },
            "grouping":    self._aaf_group_var.get(),
            "fps":         self._aaf_fps_var.get(),
            "seq_name":    self.seq_name.get(),
        }
        sidecar   = self._aaf_sidecar_path()
        init_dir  = os.path.dirname(sidecar)  if sidecar else ""
        init_file = os.path.basename(sidecar) if sidecar else "aaf_setup.json"
        path = filedialog.asksaveasfilename(
            title="Save AAF setup",
            defaultextension=".json",
            filetypes=[("JSON","*.json"),("All","*.*")],
            initialdir=init_dir, initialfile=init_file)
        if not path: return
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        messagebox.showinfo("Saved", "Setup saved to:\n{}".format(path))

    def _aaf_load_setup(self):
        path = filedialog.askopenfilename(
            title="Load AAF setup",
            filetypes=[("JSON setup files","*.json"),("All","*.*")])
        if not path or not os.path.exists(path): return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            messagebox.showerror("Load failed", str(e)); return

        # ── Batch-add pool files (one rebuild at the very end) ────────────────
        missing = []
        for vp in data.get("video_paths", []):
            if os.path.exists(vp):
                self._aaf_add_video(vp, _batch=True)
            else:
                missing.append(vp)

        for ap in data.get("audio_paths", []):
            if os.path.exists(ap):
                self._aaf_add_audio(ap, _batch=True)
            else:
                missing.append(ap)

        # Update pool count labels
        nv = len(self._aaf_video_paths)
        self._aaf_count_lbl.config(
            text="{} file{}".format(nv, "s" if nv != 1 else ""))
        na = len(self._aaf_audio_paths)
        self._aaf_audio_count_lbl.config(
            text="{} file{}".format(na, "s" if na != 1 else ""))

        # ── Restore assignments and sync state (before the single rebuild) ────
        assignments = data.get("assignments", {})
        options = ["— no video —"] + [basename(p) for p in self._aaf_video_paths]
        for base, fn in assignments.items():
            if base in self._aaf_source_file_vars and fn in options:
                self._aaf_source_file_vars[base].set(fn)

        for base, sd in data.get("sync", {}).items():
            if base in self._aaf_source_sync_vars:
                self._aaf_source_sync_vars[base].set(sd.get("enabled", False))
            if base in self._aaf_source_syncaudio_vars:
                self._aaf_source_syncaudio_vars[base].set(sd.get("audio_path", ""))
            if base in self._aaf_source_offset_vars:
                self._aaf_source_offset_vars[base].set(sd.get("offset", "0.000"))
            if base in self._aaf_source_sync_label_vars:
                self._aaf_source_sync_label_vars[base].set(sd.get("label", ""))

        # Single rebuild + FPS probe for the entire load
        if hasattr(self, "_aaf_assign_frame"):
            self._rebuild_aaf_source_rows()
        self._aaf_schedule_detect_fps()

        # Restore other settings
        if "grouping" in data:  self._aaf_group_var.set(data["grouping"])
        if "fps"      in data:  self._aaf_fps_var.set(data["fps"])
        if "seq_name" in data:  self.seq_name.set(data["seq_name"])

        if missing:
            messagebox.showwarning("Missing files",
                "{} video file(s) from the saved setup were not found:\n{}".format(
                    len(missing), "\n".join(basename(p) for p in missing[:5])))

    def _aaf_build_progress(self, pct, text):
        """Show/update the build progress bar.  pct is 0–100."""
        frame = getattr(self, "_aaf_prog_frame", None)
        if frame is None:
            return
        if not frame.winfo_ismapped():
            frame.pack(fill="x", pady=(6, 0))
        self._aaf_prog_lbl.config(text=text)
        self._aaf_prog_bar.set(pct, 100)
        self.update_idletasks()

    def _aaf_build(self):
        vpaths = self._aaf_video_paths
        path_by_fn = {basename(p): p for p in vpaths}

        try:
            fps = float(self._aaf_fps_var.get())
        except ValueError:
            messagebox.showerror("Invalid FPS", "Enter a valid frame rate (e.g. 29.97).")
            return

        group_mode = self._aaf_group_var.get()   # "single" or "by_source"

        # Build source → video path lookup from dropdown selections
        source_video = {}
        for base, sv in self._aaf_source_file_vars.items():
            fn = sv.get()
            if fn != "— no video —" and fn in path_by_fn:
                source_video[base] = path_by_fn[fn]

        # Build per-source sync offset (seconds) and reference audio path
        source_offset     = {}
        source_syncaudio  = {}
        for base in self._aaf_sources:
            if self._aaf_source_sync_vars.get(base, tk.BooleanVar()).get():
                try:
                    source_offset[base] = float(
                        self._aaf_source_offset_vars[base].get())
                except ValueError:
                    source_offset[base] = 0.0
                ap = self._aaf_source_syncaudio_vars[base].get()
                if ap and os.path.isfile(ap):
                    source_syncaudio[base] = ap

        self._aaf_build_progress(10, "Processing clips\u2026")

        # Flatten all clips from all tracks
        all_clips = []
        for track in self._aaf_data["tracks"]:
            for clip in track["clips"]:
                all_clips.append({**clip, "track_name": track["name"]})

        matched      = 0
        unmatched    = 0
        clip_results = []   # (clip_name, video_label, color) — for the colored summary
        clips_with_media    = []
        track_names_ordered = []   # preserves insertion order for XML track sequence

        for clip in all_clips:
            base = get_clip_base_name(clip["clip_name"])
            if base is None:
                continue   # skip fades and unresolvable clips entirely

            vp = source_video.get(base)
            if vp:
                matched += 1
                clip_results.append((clip["clip_name"], basename(vp), SUCCESS))
            else:
                unmatched += 1
                clip_results.append((clip["clip_name"], "— no video assigned —", WARN))

            # Determine XML track name based on grouping mode
            if group_mode == "by_source":
                tn = basename(vp) if vp else "No Video"
            elif group_mode == "by_track":
                tn = clip.get("track_name") or "Unknown Track"
            else:
                tn = "_consolidate_"   # placeholder; resolved below

            if tn not in track_names_ordered and tn != "_consolidate_":
                track_names_ordered.append(tn)

            clips_with_media.append({
                **clip,
                "track_name":        tn,
                "video_path":        vp,
                "audio_path":        source_syncaudio.get(base),
                "video_offset_secs": source_offset.get(base, 0.0),
            })

        # Interval scheduling for "consolidate" mode — assign minimum tracks
        if group_mode == "single":
            clips_with_media.sort(key=lambda c: c["start_secs"])
            track_ends = []   # end_secs of the last clip assigned to each track
            track_names_ordered = []
            for c in clips_with_media:
                assigned = None
                for i, tend in enumerate(track_ends):
                    if c["start_secs"] >= tend:
                        assigned = i
                        break
                if assigned is None:
                    assigned = len(track_ends)
                    track_ends.append(0.0)
                tn = "Video {}".format(assigned + 1)
                track_ends[assigned] = c["end_secs"]
                c["track_name"] = tn
                if tn not in track_names_ordered:
                    track_names_ordered.append(tn)

        if matched == 0:
            messagebox.showerror("No Matches",
                "No clips were matched to video files.\n"
                "Add video files and assign at least one source to a video.")
            return

        self._aaf_build_progress(35, "Choose output file\u2026")
        out = filedialog.asksaveasfilename(
            title="Save Premiere XML",
            defaultextension=".xml",
            filetypes=[("XML","*.xml"),("All","*.*")],
            initialfile="{}_aaf.xml".format(
                self._aaf_data.get("session_name","postbridge").replace(" ","_")))
        if not out: return

        try:
            sr = self._aaf_data.get("sample_rate", 48000)
            seq_w, seq_h = 1280, 720
            if vpaths:
                self._aaf_build_progress(55, "Probing media settings\u2026")
                seq_w, seq_h, fps_det, _sr = engines.probe_media_settings(vpaths)
                # Don't auto-override FPS — user set it explicitly

            self._aaf_build_progress(80, "Building XML\u2026")
            xmeml = engines.build_xml_from_pt(
                clips_with_media,
                track_names_ordered,
                self.seq_name.get() or self._aaf_data.get("session_name","PostBridge"),
                seq_w=seq_w, seq_h=seq_h, seq_fps=fps, seq_sr=sr)

            self._aaf_build_progress(95, "Writing file\u2026")
            engines.write_xml(xmeml, out)
        except Exception as e:
            messagebox.showerror("Build Error", str(e)); return

        self.out_path.set(out)
        self._aaf_done(matched, unmatched, len(clips_with_media), clip_results)

    def _aaf_done(self, matched, unmatched, total, clip_results=None):
        self._clear()
        tk.Frame(self.body, bg=BG, height=20).pack()
        tk.Label(self.body, text="\u2713",
                 font=("Courier New",52,"bold"), bg=BG, fg=SUCCESS).pack()
        tk.Label(self.body, text="PREMIERE XML EXPORTED",
                 font=FL, bg=BG, fg=TEXT).pack(pady=(6,2))
        tk.Label(self.body, text=self.out_path.get(),
                 font=FB, bg=BG, fg=SUB).pack()
        tk.Frame(self.body, bg=BG, height=10).pack()

        card = tk.Frame(self.body, bg=SURF,
                        highlightbackground=BORDER, highlightthickness=1)
        card.pack(padx=60, fill="x")
        for lbl, val, col in [
            ("Total AAF clips",   str(total),     TEXT),
            ("Matched to video",  str(matched),   SUCCESS),
            ("Unmatched",         str(unmatched), WARN if unmatched else SUB),
        ]:
            r = tk.Frame(card, bg=SURF); r.pack(fill="x", padx=16, pady=3)
            tk.Label(r, text="{:<22}".format(lbl),
                     font=FB, bg=SURF, fg=SUB).pack(side="left")
            tk.Label(r, text=val, font=FB, bg=SURF, fg=col).pack(side="left")
        tk.Frame(card, bg=BG, height=8).pack()

        tk.Frame(self.body, bg=BG, height=10).pack()
        self._btn(self.body, "START NEW EPISODE", self._reset).pack()

        # ── Per-clip colored log (same scheme as Script→AAF reconcile view) ──
        if clip_results:
            tk.Frame(self.body, bg=BG, height=12).pack()
            tk.Label(self.body, text="CLIP DETAIL",
                     font=FL, bg=BG, fg=ACCENT).pack(anchor="w", pady=(0,4))
            log_outer = tk.Frame(self.body, bg=BG)
            log_outer.pack(fill="x", pady=(0,6))
            log = tk.Text(log_outer,
                          font=("Courier New", 10), bg=SURF, fg=TEXT,
                          relief="flat", bd=0,
                          state="normal", wrap="none",
                          height=min(18, len(clip_results) + 1))
            sbv = tk.Scrollbar(log_outer, orient="vertical",   command=log.yview)
            sbh = tk.Scrollbar(log_outer, orient="horizontal", command=log.xview)
            log.configure(yscrollcommand=sbv.set, xscrollcommand=sbh.set)
            sbv.pack(side="right",  fill="y")
            sbh.pack(side="bottom", fill="x")
            log.pack(side="left", fill="both", expand=True)
            for clip_name, vid_label, color in clip_results:
                tag = "c{}".format(abs(hash(color)))
                log.tag_configure(tag, foreground=color)
                line = "  {:<50s}  →  {}\n".format(clip_name[:50], vid_label)
                log.insert("end", line, tag)
            log.configure(state="disabled")

    def _reset(self):
        self.workflow=None; self.tokens=[]; self.parts=[]; self.pulls=[]
        self.results=[]; self.doc_title=""; self.bins={}; self._pool=None
        self._aaf_data=None
        self.seq_name.set(""); self.out_path.set("")
        self._home()

if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    App().mainloop()