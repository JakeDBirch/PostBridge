import os
import sys
import json
import threading
import queue
import tempfile
import time
from concurrent.futures import (ThreadPoolExecutor,
                                wait as _fut_wait, FIRST_COMPLETED,
                                as_completed as _fut_as_completed)

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
from utils import basename, secs_tc, tc_secs, is_video, is_audio, is_media, MEDIA_EXTS, VIDEO_EXTS, parse_dnd
from parsers import (parse_script, parse_pt_session_text, dedupe_pt_tracks,
                     match_pt_clip_to_media, get_clip_base_name,
                     parse_aaf_session, match_source_to_video,
                     detect_sync_offset, verify_sync_at_offset)
from gui_components import VoBin, MediaPool, _SlimScrollbar, _FlatDropdown, _FlatProgressBar
import engines
from sync_preview import SyncPreviewDialog
from match_review import MatchReviewDialog

# Sequence preset table: (display_name, width, height, fps)
# width/height/fps = None means "detect from media" or "custom — leave fields as-is"
_AAF_SEQ_PRESETS = [
    ("Detect from media",  None,  None,   None),
    ("1080p  23.976 fps",  1920,  1080,  23.976),
    ("1080p  24 fps",      1920,  1080,  24.0),
    ("1080p  25 fps",      1920,  1080,  25.0),
    ("1080p  29.97 fps",   1920,  1080,  29.97),
    ("1080p  30 fps",      1920,  1080,  30.0),
    ("4K UHD  23.976 fps", 3840,  2160,  23.976),
    ("4K UHD  24 fps",     3840,  2160,  24.0),
    ("4K UHD  25 fps",     3840,  2160,  25.0),
    ("4K UHD  29.97 fps",  3840,  2160,  29.97),
    ("4K UHD  30 fps",     3840,  2160,  30.0),
    ("720p  29.97 fps",    1280,   720,  29.97),
    ("720p  30 fps",       1280,   720,  30.0),
    ("Custom",             None,  None,   None),
]
_AAF_PRESET_LOOKUP = {p[0]: p[1:] for p in _AAF_SEQ_PRESETS}

class App(TkinterDnD.Tk if HAS_DND else tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PostBridge")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(960, 700)
        self._center(1440, 1000)
        if sys.platform == "win32":
            self.state("zoomed")   # start maximized on Windows

        self._init_styles()

        # State
        self.workflow              = None
        self._export_fmt           = None   # "aaf" or "xml", chosen at Step 5 for script_session
        self._current_session_file = None   # path of the last Save / Save As target
        self._restore_s4           = False  # True only when explicitly opened via OPEN
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
        self.body.pack(fill="both", expand=True, padx=44, pady=(0, 14))

        # Global keyboard shortcuts — active on every screen
        self.bind_all("<Control-s>",       lambda e: self._quick_save(self._footer_save_btn))
        self.bind_all("<Control-S>",       lambda e: self._save_as())
        self.bind_all("<Control-o>",       lambda e: self._open_session())

        self._home()

    def _center(self, w, h):
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        # Use requested size or 85% of screen, whichever is smaller
        w = min(w, int(sw * 0.85))
        h = min(h, int(sh * 0.85))
        x = (sw - w) // 2
        y = (sh - h) // 2
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
        tk.Frame(self, bg=ACCENT, height=6).pack(fill="x")
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=36, pady=(14, 10))

        # ── Left: persistent action buttons (Save / Save As / Open / Home) ────
        btn_frame = tk.Frame(bar, bg=BG)
        btn_frame.pack(side="left")
        self._footer_save_btn = [None]
        def _hdr_save():
            self._quick_save(self._footer_save_btn)
        _fsb = self._btn(btn_frame, "SAVE", _hdr_save, small=True)
        _fsb.pack(side="left", padx=(0, 4))
        self._footer_save_btn[0] = _fsb
        self._btn(btn_frame, "SAVE AS", self._save_as,
                  small=True).pack(side="left", padx=(0, 4))
        self._btn(btn_frame, "OPEN",    self._open_session,
                  small=True).pack(side="left", padx=(0, 10))
        self._btn(btn_frame, "\u2302 HOME", self._home,
                  small=True).pack(side="left")

        # ── Right: PostBridge + MeatEater branding ───────────────────────────
        brand_frame = tk.Frame(bar, bg=BG)
        brand_frame.pack(side="right")

        # Dependency warnings (packed right-to-left, so they appear between
        # the PostBridge text and the MeatEater logo)
        _dep_warnings = []
        if not HAS_WHISPER:
            _dep_warnings.append("pip install faster-whisper")
        if not HAS_DND:
            _dep_warnings.append("pip install tkinterdnd2")

        # MeatEater brand — rightmost element
        self._logo_photo = None
        _script_dir = os.path.dirname(os.path.abspath(__file__))
        _assets = os.path.join(_script_dir, "assets")
        for _name in ("meateater_logo.png",
                      "channels4_profile-d0f26706-7f7c-46be-b8be-cd47fd3401bf.png"):
            _path = os.path.join(_assets, _name)
            if os.path.isfile(_path):
                try:
                    from tkinter import PhotoImage
                    self._logo_photo = PhotoImage(file=_path)
                    _h = self._logo_photo.height()
                    if _h > 36:
                        div = max(1, _h // 36)
                        self._logo_photo = self._logo_photo.subsample(div, div)
                    tk.Label(brand_frame, image=self._logo_photo,
                             bg=BG).pack(side="right", padx=(10, 0), pady=(4, 0))
                    break
                except Exception:
                    pass
        if self._logo_photo is None:
            tk.Label(brand_frame, text="MEATEATER",
                     font=("Courier New", 9, "bold"),
                     bg=BG, fg=ACCENT).pack(side="right", padx=(10, 0), pady=(8, 0))

        if _dep_warnings:
            tk.Label(brand_frame,
                     text="  [" + "  \u00b7  ".join(_dep_warnings) + "]",
                     font=("Courier New", 10), bg=BG, fg=WARN).pack(side="right")

        # PostBridge title + subtitle (left of MeatEater, packed right-to-left)
        tk.Label(brand_frame, text="  \u00b7  ", font=FS,
                 bg=BG, fg=BORDER).pack(side="right", pady=(8, 0))
        tk.Label(brand_frame, text="audio/video post-production bridge",
                 font=FS, bg=BG, fg=SUB).pack(side="right", pady=(8, 0))
        tk.Label(brand_frame, text="  \u00b7  ", font=FS,
                 bg=BG, fg=ACCENT).pack(side="right", pady=(8, 0))
        tk.Label(brand_frame, text="POSTBRIDGE",
                 font=FH, bg=BG, fg=TEXT).pack(side="right")

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", padx=36)

    def _clear(self):
        self.unbind_all("<MouseWheel>")
        for key in ("<Control-z>", "<Control-Z>",
                    "<Control-Shift-z>", "<Control-Shift-Z>"):
            try:
                self.unbind_all(key)
            except Exception:
                pass
        for w in self.body.winfo_children(): w.destroy()

    def _home(self):
        # Reset session-specific state so a new workflow starts clean
        self._current_session_file = None
        self._export_fmt           = None
        self._restore_s4           = False
        self._clear()
        tk.Frame(self.body, bg=BG, height=30).pack()

        tk.Label(self.body, text="Choose a workflow",
                 font=FBT, bg=BG, fg=SUB).pack(pady=(0,24))

        cards_frame = tk.Frame(self.body, bg=BG)
        cards_frame.pack(fill="x", padx=20)

        _TITLE_FONT = (_SANS, 15, "bold")
        _DESC_FONT  = (_SANS, 11)
        _ARR_FONT   = (_SANS, 20, "bold")
        hover_bg    = "#303030"

        workflows = [
            ("Script Formatter",
             "script_formatter",
             "Build @PART, @VO, and @PULL blocks and copy them into your script.",
             True),
            ("Script \u2192 Session",
             "script_session",
             "Whisper-reconcile a script and export as AAF or XML \u2014 "
             "format is chosen at the export step.",
             HAS_WHISPER),
            ("AAF \u2192 XML",
             "pt_xml",
             "Parse an AAF, match clips to video files by source name, "
             "and generate XML.",
             HAS_AAF),
            ("Transcribe Media",
             "transcribe",
             "Pre-transcribe VO and interview files so reconcile runs instantly. "
             "Transcription files are saved alongside each media file.",
             HAS_WHISPER),
        ]

        for title, wf_key, desc, available in workflows:
            card = tk.Frame(cards_frame, bg=SURF,
                            highlightbackground=BORDER, highlightthickness=1)
            card.pack(fill="x", pady=(0,12))

            # Left accent stripe (always shown; brighter when available)
            stripe_color = ACCENT if available else BORDER
            tk.Frame(card, bg=stripe_color, width=4).pack(side="left", fill="y")

            inner = tk.Frame(card, bg=SURF)
            inner.pack(fill="x", padx=24, pady=20)

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

        # Script Format Reference has moved to the Script Formatter workflow

    def _ai_prompt_text(self):
        """Return the AI reformatting prompt as a string (used by Script Formatter)."""
        return (
            "Reformat the following script for PostBridge. Follow these rules exactly.\n\n"
            "TOKEN DECLARATION BLOCKS — place at the top of the file:\n\n"
            "--- INTERVIEW SESSIONS START ---\n"
            "[TOKEN_NAME]\n"
            "--- INTERVIEW SESSIONS END ---\n\n"
            "--- EPISODE ASSETS START ---\n"
            "[PART_0_NARRATOR]\n"
            "--- EPISODE ASSETS END ---\n\n"
            "  \u2022 Token names: UPPERCASE letters, digits, and underscores only (e.g. JOHN_SMITH)\n"
            "  \u2022 One token per interview speaker\n"
            "  \u2022 PART_N_NARRATOR tokens for narrator VO tracks (PART_0_NARRATOR, PART_1_NARRATOR, etc.)\n\n"
            "BODY DIRECTIVES (in document order):\n\n"
            "@PART  Part Name\n"
            "  \u2014 marks a new section/segment\n\n"
            "@PULL  TOKEN_NAME [HH:MM:SS-HH:MM:SS]\n"
            "Quote text goes here.\n"
            "No blank lines within the quote \u2014 a blank line ends the quote.\n\n"
            "@VO  PART_N_LABEL\n"
            "Narrator voice-over text here.\n"
            "Ends at the next @, //, or blank line.\n\n"
            "// This is a comment \u2014 ignored by the parser.\n\n"
            "RULES:\n"
            "  \u2022 Each token in the declaration block MUST be wrapped in square brackets: [TOKEN_NAME]\n"
            "  \u2022 Timecodes must be HH:MM:SS (zero-padded). Prepend 00: to any MM:SS timecodes.\n"
            "  \u2022 No blank lines inside @PULL quote text.\n"
            "  \u2022 Multiple @VO blocks with the same ID are fine \u2014 collected in order.\n"
            "  \u2022 Plain text not inside an @VO or @PULL block is ignored.\n\n"
            "Please reformat the following script:\n\n"
            "[PASTE YOUR SCRIPT HERE]"
        )

    def _home_copy_ai_prompt(self):
        self.clipboard_clear()
        self.clipboard_append(self._ai_prompt_text())

    def _is_aaf_mode(self):
        """Return True when the current workflow/format requires AAF output."""
        if self.workflow == "script_aaf":
            return True
        if self.workflow == "script_session":
            return self._export_fmt == "aaf"
        return False

    def _select_workflow(self, key):
        self.workflow = key
        self._export_fmt = None   # reset any previous format choice
        if key in ("script_aaf", "script_xml", "script_session"):
            path = filedialog.askopenfilename(
                title="Open Script",
                filetypes=[("Text", "*.txt"), ("All", "*.*")])
            if not path:
                self.workflow = None   # user cancelled — stay on home
                return
            self._load_script(path)
        elif key == "pt_xml":
            path = filedialog.askopenfilename(
                title="Open AAF",
                filetypes=[("AAF", "*.aaf"), ("All", "*.*")])
            if not path:
                self.workflow = None   # user cancelled — stay on home
                return
            self._aaf_load(path)
        elif key == "script_formatter":
            self._script_formatter()
        elif key == "transcribe":
            self._transcribe_workflow()

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
        pad = (8,4) if small else (16,8)
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
        f = tk.Frame(self.body, bg=BG)
        f.pack(fill="x", pady=(20, 6))
        tk.Frame(f, bg=ACCENT, width=3, height=20).pack(side="left", padx=(0, 10))
        tk.Label(f, text=text, font=FL, bg=BG, fg=TEXT).pack(side="left", anchor="w")

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
        # Store so callers can access for programmatic scroll
        self._last_scroll_canvas = canvas

        def _on_wheel(e):
            # Don't scroll when focus is in a popup (e.g. combobox dropdown)
            w = self.focus_get()
            if w is not None and w.winfo_toplevel() != self:
                return
            # macOS uses delta 1/-1 per notch; Windows uses 120/-120
            units = int(-1 * e.delta) if sys.platform == "darwin" else int(-1 * (e.delta / 120))
            canvas.yview_scroll(units, "units")
            canvas.update_idletasks()

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

        def _add_file_row(p):
            """Add a single media file and create its display row."""
            p = str(p).strip().strip("{}")
            if not p or not os.path.isfile(p): return
            if os.path.basename(p).startswith("._"): return
            if not is_media(p): return
            if p in paths_list: return
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

        def _add_folder_row(folder, folder_paths):
            """Bulk-add all media files from a folder as a single display row."""
            added = [p for p in folder_paths if p not in paths_list]
            if not added: return
            for p in added:
                paths_list.append(p)
            row = tk.Frame(file_list, bg=SURF2,
                           highlightbackground=BORDER, highlightthickness=1)
            row.pack(fill="x", pady=1)
            tk.Label(row, text="FOLDER",
                     font=("Courier New", 9, "bold"),
                     bg="#2a2a3d", fg="#aaaaff",
                     padx=5, pady=2).pack(side="left")
            tk.Label(row,
                     text="{}  ({} files)".format(os.path.basename(folder), len(added)),
                     font=FB, bg=SURF2, fg=TEXT,
                     padx=6, anchor="w").pack(side="left", fill="x", expand=True)
            rm = tk.Label(row, text=" × ", font=FB, bg=SURF2, fg=SUB,
                          cursor="hand2", padx=4)
            rm.pack(side="right")
            def _rm(e, ps=added, r=row):
                for p in ps:
                    if p in paths_list: paths_list.remove(p)
                r.destroy()
                _update_count()
            rm.bind("<Button-1>", _rm)
            _update_count()

        def _add_media(raw):
            """Handle DnD drops — supports both individual files and folders."""
            paths = parse_dnd(raw) if isinstance(raw, str) else raw
            for p in paths:
                p = str(p).strip().strip("{}")
                if not p: continue
                if os.path.isdir(p):
                    fps = []
                    for root, _, files in os.walk(p):
                        for fn in sorted(files):
                            fp = os.path.join(root, fn)
                            if is_media(fp): fps.append(fp)
                    if fps: _add_folder_row(p, fps)
                else:
                    _add_file_row(p)

        btn_row = tk.Frame(panel, bg=SURF)
        btn_row.pack(anchor="w", padx=12, pady=(4, 10))

        def _browse_f():
            exts = sorted(MEDIA_EXTS)
            ext_glob = (" ".join("*" + e for e in exts) + " " +
                        " ".join("*" + e.upper() for e in exts))
            ps = filedialog.askopenfilenames(
                title="Select audio/video files",
                filetypes=[("Media", ext_glob), ("All files", "*.*")])
            if ps:
                for p in list(ps): _add_file_row(p)

        def _browse_d():
            folder = filedialog.askdirectory(title="Select media folder")
            if not folder: return
            fps = []
            for root, _, files in os.walk(folder):
                for fn in sorted(files):
                    fp = os.path.join(root, fn)
                    if is_media(fp): fps.append(fp)
            if fps: _add_folder_row(folder, fps)

        self._btn(btn_row, "+ BROWSE FILES",  _browse_f, small=True).pack(side="left", padx=(0, 8))
        self._btn(btn_row, "+ BROWSE FOLDER", _browse_d, small=True).pack(side="left")

        if HAS_DND:
            panel.drop_target_register(DND_FILES)
            panel.dnd_bind("<<Drop>>", lambda e: _add_media(e.data))

        return panel, file_list, count_lbl

    def _step1(self):
        self._clear()
        self._step1_next_added   = False
        wf_label = ("Session" if self.workflow == "script_session"
                    else ("AAF" if self.workflow == "script_aaf" else "XML"))
        self._section("STEP 1 — LOAD SCRIPT  (Script → {})".format(wf_label))

        # ── Nav pinned to bottom first so it's always visible ─────────────────
        nav = tk.Frame(self.body, bg=BG)
        nav.pack(side="bottom", fill="x", pady=(8,0))
        self._btn(nav, "← HOME", self._home).pack(side="left")
        self._step1_nav = nav   # NEXT button appended here once script is loaded

        # ── Scrollable content area fills remaining space ─────────────────────
        sf = self._scroll_frame(self.body)

        card = tk.Frame(sf, bg=SURF,
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
                "Make sure the script is in PostBridge format.")
            return

        no_quote = sum(1 for p in pulls if not p["quote_text"])
        self.tokens    = tokens
        self.parts     = parts
        self.pulls     = pulls
        self.vo_blocks = vo_blocks
        self.doc_title = doc_title

        # Set cache dir next to the script file inside the engines module
        engines._cache_dir = os.path.join(os.path.dirname(os.path.abspath(path)), ".pb_cache")
        
        self.seq_name.set("{} (AUTO)".format(doc_title))

        if hasattr(self, "_s1"):
            self._s1.config(
                text="✓  {}  ·  {} pulls  ·  {} parts  ·  {} tokens: {}{}".format(
                    doc_title, len(pulls), len(parts), len(tokens),
                    "  ".join(tokens),
                    "  ·  {} pulls missing quote text".format(no_quote) if no_quote else ""),
                fg=SUCCESS if not no_quote else WARN)

        if warnings:
            messagebox.showwarning("Warnings", "\n".join(warnings[:10]))
        # If we're on the step-1 screen, add the NEXT button; otherwise advance directly.
        if hasattr(self, "_step1_nav") and not getattr(self, "_step1_next_added", False):
            self._btn(self._step1_nav, "NEXT  →  ASSIGN MEDIA",
                      self._step2, color=ACCENT).pack(side="right")
            self._step1_next_added = True
        else:
            self.after(0, self._step2)

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

        # ── Nav pinned to bottom first so it's always visible ─────────────────
        cache_row = tk.Frame(self.body, bg=BG)
        cache_row.pack(side="bottom", fill="x", pady=(4,0))
        self._btn(cache_row, "CLEAR TRANSCRIPTS", lambda: self._clear_cache("transcripts"),
                  small=True).pack(side="right", padx=(0, 4))
        self._btn(cache_row, "CLEAR RESULTS", lambda: self._clear_cache("results"),
                  small=True).pack(side="right", padx=(0, 8))

        nav = tk.Frame(self.body, bg=BG)
        nav.pack(side="bottom", fill="x", pady=(8,0))
        self._btn(nav, "RECONCILE  →",   self._start_reconcile,
                  color=ACCENT).pack(side="right")

        # ── Background mode toggle ────────────────────────────────────────────
        if not hasattr(self, "_reconcile_bg_mode"):
            self._reconcile_bg_mode = tk.BooleanVar(value=False)
        bg_ck2 = tk.Label(nav,
                          text="\u2611" if self._reconcile_bg_mode.get() else "\u2610",
                          font=(_SANS, 15), bg=BG,
                          fg=ACCENT if self._reconcile_bg_mode.get() else SUB,
                          cursor="hand2", padx=4)
        bg_ck2.pack(side="right", padx=(0, 6))
        bg_lbl2 = tk.Label(nav, text="Background mode", font=FB, bg=BG, fg=SUB,
                           cursor="hand2")
        bg_lbl2.pack(side="right", padx=(0, 2))

        def _toggle_reconcile_bg(lbl=bg_ck2):
            self._reconcile_bg_mode.set(not self._reconcile_bg_mode.get())
            _on = self._reconcile_bg_mode.get()
            lbl.config(text="\u2611" if _on else "\u2610",
                       fg=ACCENT if _on else SUB)

        bg_ck2.bind("<Button-1>",  lambda e: _toggle_reconcile_bg())
        bg_lbl2.bind("<Button-1>", lambda e: _toggle_reconcile_bg())

        # ── Scrollable content area fills remaining space ─────────────────────
        sf = self._scroll_frame(self.body)

        self._pool = MediaPool(sf, self.tokens, self.parts,
                              aaf_mode=(self.workflow in ("script_aaf", "script_session")))
        self._pool.pack(fill="x", pady=(0,10), padx=2)
        self._btn(self._pool._hdr, "\u27f3 REFRESH", self._refresh_pool,
                  small=True).pack(side="left", padx=(10, 0))

        # Pre-load any files dropped at Step 1
        for p in getattr(self, "_prefetch_media", []):
            self._pool._add(p)

        # Restore setup if user came back via "Redo" from step 4
        if getattr(self, "_redo_setup", None):
            redo = self._redo_setup
            self._pool._bulk_loading = True
            try:
                for path, token in redo.get("assignments", []):
                    if os.path.isfile(path):
                        self._pool._add(path)
                        self._pool._rows[-1]["var"].set(token)
                for tok, sp in redo.get("transcript_sources", {}).items():
                    if tok in self._pool._src_vars and sp:
                        self._pool._src_vars[tok].set(basename(sp))
            finally:
                self._pool._bulk_loading = False
            self._pool._rebuild_src_dropdowns()
            self._pool._refresh_count()
            del self._redo_setup

        # Apply pending setup from _open_session (home page "Open Session" card)
        if getattr(self, "_pending_setup", None):
            self._apply_setup_data(self._pending_setup)
            del self._pending_setup

        # If results were saved and loaded, skip straight to Step 4
        if getattr(self, "_pending_results", None) is not None:
            self.results = self._pending_results
            del self._pending_results
            self.after(50, self._step4)

    def _setup_sidecar_path(self):
        if not hasattr(self, '_script_path') or not self._script_path:
            return None
        base = os.path.splitext(self._script_path)[0]
        return base + "_setup.json"

    def _pb_cache_dir(self):
        """Return the .pb_cache directory next to the script, creating it if needed."""
        if not hasattr(self, '_script_path') or not self._script_path:
            return None
        d = os.path.join(os.path.dirname(self._script_path), ".pb_cache")
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        return d

    def _step4_state_path(self):
        cache = self._pb_cache_dir()
        if not cache:
            return None
        script_name = os.path.splitext(os.path.basename(self._script_path))[0]
        return os.path.join(cache, script_name + "_step4state.json")

    def _results_sidecar_path(self):
        cache = self._pb_cache_dir()
        if not cache:
            return None
        script_name = os.path.splitext(os.path.basename(self._script_path))[0]
        return os.path.join(cache, script_name + "_results.json")

    def _save_results(self):
        """Persist self.results to a sidecar JSON so open can restore to Step 4."""
        path = self._results_sidecar_path()
        if not path or not getattr(self, 'results', None):
            return
        try:
            import json as _json
            class _Enc(_json.JSONEncoder):
                def default(self, obj):
                    try:
                        import numpy as _np
                        if isinstance(obj, _np.integer):  return int(obj)
                        if isinstance(obj, _np.floating): return float(obj)
                        if isinstance(obj, _np.ndarray):  return obj.tolist()
                    except ImportError:
                        pass
                    return super().default(obj)
            with open(path, "w", encoding="utf-8") as f:
                _json.dump(self.results, f, cls=_Enc, indent=2)
        except Exception:
            pass

    # ── Diagnostic ────────────────────────────────────────────────────────────

    def _diagnostic_path(self):
        cache = self._pb_cache_dir()
        if not cache:
            return None
        stem = os.path.splitext(os.path.basename(self._script_path))[0]
        return os.path.join(cache, stem + "_diagnostic.json")

    def _build_diagnostic(self, results=None):
        """Compute a diagnostic dict from the current (or supplied) results list.

        Returns a dict with per-clip rows and aggregate summary statistics useful
        for algorithm tuning — what the algo decided vs. what the user actually did.
        """
        import datetime
        results = results or getattr(self, "results", None) or []

        clips = []
        for r in results:
            algo_status  = r.get("_original_status") or r.get("status", "")
            final_status = r.get("status", "")
            accepted     = bool(r.get("_s4_accepted"))
            ignored      = bool(r.get("_s4_ignored"))

            if ignored:
                user_action = "ignored"
            elif accepted and final_status == "manual":
                user_action = "adjusted"   # opened editor and changed IN/OUT
            elif accepted:
                user_action = "accepted"   # accepted algo result as-is
            else:
                user_action = "unreviewed"

            _mt = (r.get("matched_text") or "").strip()
            clips.append({
                "order":        r.get("order", 0),
                "token":        r.get("token", ""),
                "is_vo":        bool(r.get("is_vo")),
                "algo_status":  algo_status,
                "final_status": final_status,
                "confidence":   round(float(r.get("confidence") or 0), 4),
                "user_action":  user_action,
                "delta_in_s":   round(float(r.get("delta_in")  or 0), 3),
                "delta_out_s":  round(float(r.get("delta_out") or 0), 3),
                "rec_in_s":     round(float(r.get("rec_in_s")  or 0), 3),
                "rec_out_s":    round(float(r.get("rec_out_s") or 0), 3),
                "matched_text": _mt[:120] if _mt else "",
                "n_segments":   len(r.get("segments") or []),
            })

        # ── Cross-table: algo_status × user_action ────────────────────────────
        ALGO_STATUSES = ["ok", "direct", "low_confidence", "no_match",
                         "error", "no_file", "not_run", "cancelled"]
        ACTIONS = ["accepted", "adjusted", "ignored", "unreviewed"]
        cross = {}
        for st in ALGO_STATUSES:
            cross[st] = {a: 0 for a in ACTIONS}
        cross["_other"] = {a: 0 for a in ACTIONS}
        for c in clips:
            bucket = c["algo_status"] if c["algo_status"] in ALGO_STATUSES else "_other"
            cross[bucket][c["user_action"]] += 1

        # ── Confidence calibration ────────────────────────────────────────────
        buckets = [
            ("90-100%", 0.90, 1.01),
            ("70-89%",  0.70, 0.90),
            ("50-69%",  0.50, 0.70),
            ("<50%",    0.00, 0.50),
        ]
        conf_cal = {}
        for label, lo, hi in buckets:
            group = [c for c in clips if lo <= c["confidence"] < hi
                     and c["algo_status"] not in ("no_file", "not_run", "cancelled")]
            n = len(group)
            used = sum(1 for c in group if c["user_action"] in ("accepted", "adjusted"))
            conf_cal[label] = {
                "n": n,
                "used": used,
                "rate": round(used / n, 3) if n else None,
            }

        # ── Position accuracy (clips where algo found a match) ────────────────
        matched_clips = [c for c in clips
                         if c["algo_status"] in ("ok", "direct", "low_confidence")
                         and c["user_action"] in ("accepted", "adjusted")]
        if matched_clips:
            abs_di = [abs(c["delta_in_s"])  for c in matched_clips]
            abs_do = [abs(c["delta_out_s"]) for c in matched_clips]
            pos_acc = {
                "n":                   len(matched_clips),
                "mean_abs_delta_in_s": round(sum(abs_di) / len(abs_di), 3),
                "mean_abs_delta_out_s":round(sum(abs_do) / len(abs_do), 3),
                "clips_over_1s_off":   sum(1 for d in abs_di if d > 1.0),
                "clips_over_2s_off":   sum(1 for d in abs_di if d > 2.0),
            }
        else:
            pos_acc = {}

        # ── Ordering violations — rec_in_s going backward within a token ────────
        # When the matched interview position decreases across consecutive script
        # pulls from the same speaker, it almost always means the algo picked the
        # wrong instance of a repeated phrase.
        ordering_violations = []
        _tok_seq = {}   # token → list of (order, rec_in_s)
        for c in clips:
            if c["rec_in_s"] > 0 and c["algo_status"] in (
                    "ok", "direct", "low_confidence", "manual"):
                _tok_seq.setdefault(c["token"], []).append(
                    (c["order"], c["rec_in_s"], c["matched_text"]))
        for tok, seq in _tok_seq.items():
            seq.sort(key=lambda x: x[0])   # sort by script order
            prev_in = -1.0
            for order, rec_in, mtext in seq:
                if rec_in < prev_in:
                    ordering_violations.append({
                        "token":        tok,
                        "order":        order,
                        "rec_in_s":     rec_in,
                        "prev_rec_in_s": round(prev_in, 3),
                        "went_back_s":  round(prev_in - rec_in, 3),
                        "matched_text": mtext[:80],
                    })
                prev_in = rec_in

        # ── Per-token breakdown ───────────────────────────────────────────────
        by_tok = {}
        for c in clips:
            tok = c["token"] or "(unassigned)"
            if tok not in by_tok:
                by_tok[tok] = {"total": 0, "accepted": 0, "adjusted": 0,
                               "ignored": 0, "unreviewed": 0,
                               "algo_ok": 0, "algo_low": 0, "algo_miss": 0,
                               "ordering_violations": 0}
            e = by_tok[tok]
            e["total"]      += 1
            e[c["user_action"]] += 1
            if c["algo_status"] in ("ok", "direct"):   e["algo_ok"]  += 1
            elif c["algo_status"] == "low_confidence": e["algo_low"] += 1
            elif c["algo_status"] in ("no_match", "error"): e["algo_miss"] += 1
        for ov in ordering_violations:
            if ov["token"] in by_tok:
                by_tok[ov["token"]]["ordering_violations"] += 1

        # ── Per-token sync offsets (AAF workflow only) ────────────────────────
        # _aaf_source_offset_vars maps basename → StringVar with detected offset.
        # Map source files back to tokens via pool assignments where possible.
        aaf_offsets = {}
        _offset_vars = getattr(self, "_aaf_source_offset_vars", {})
        _pool = getattr(self, "_pool", None)
        if _offset_vars and _pool:
            try:
                assignments = _pool.get_assignments()   # {token: [paths]}
                for tok, paths in assignments.items():
                    for p in paths:
                        base = os.path.splitext(os.path.basename(p))[0]
                        ov = _offset_vars.get(base)
                        if ov:
                            try:
                                aaf_offsets.setdefault(tok, []).append(
                                    float(ov.get()))
                            except (ValueError, TypeError):
                                pass
            except Exception:
                pass

        total  = len(clips)
        n_acc  = sum(1 for c in clips if c["user_action"] == "accepted")
        n_adj  = sum(1 for c in clips if c["user_action"] == "adjusted")
        n_ign  = sum(1 for c in clips if c["user_action"] == "ignored")
        n_unr  = sum(1 for c in clips if c["user_action"] == "unreviewed")

        return {
            "generated":  datetime.datetime.now().isoformat(timespec="seconds"),
            "script":     getattr(self, "_script_path", ""),
            "clips":      clips,
            "summary": {
                "total":          total,
                "by_user_action": {
                    "accepted":   n_acc,
                    "adjusted":   n_adj,
                    "ignored":    n_ign,
                    "unreviewed": n_unr,
                },
                "automation_rate":        round((n_acc + n_adj) / total, 3) if total else 0,
                "cross_table":            cross,
                "confidence_calibration": conf_cal,
                "position_accuracy":      pos_acc,
                "ordering_violations":    ordering_violations,
                "by_token":               by_tok,
                "aaf_sync_offsets":       aaf_offsets,
            },
        }

    def _write_diagnostic(self):
        """Write diagnostic JSON to .pb_cache/. Called after export."""
        path = self._diagnostic_path()
        if not path:
            return
        try:
            diag = self._build_diagnostic()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(diag, f, indent=2)
        except Exception:
            pass

    def _show_diagnostic(self):
        """Pop up a human-readable diagnostic summary for algorithm tuning."""
        try:
            diag = self._build_diagnostic()
        except Exception as ex:
            messagebox.showerror("Diagnostic error", str(ex))
            return

        s     = diag["summary"]
        total = s["total"]
        ba    = s["by_user_action"]
        ct    = s["cross_table"]
        cc    = s["confidence_calibration"]
        pa    = s.get("position_accuracy", {})
        bt    = s["by_token"]

        dlg = tk.Toplevel(self)
        dlg.title("Session Diagnostic")
        dlg.configure(bg=BG)
        dlg.resizable(True, True)

        # ── Scrollable body ───────────────────────────────────────────────────
        outer = tk.Frame(dlg, bg=BG)
        outer.pack(fill="both", expand=True, padx=16, pady=12)

        def _section(txt):
            tk.Frame(outer, bg=ACCENT, height=1).pack(fill="x", pady=(14, 4))
            tk.Label(outer, text=txt, font=FL, bg=BG, fg=ACCENT).pack(anchor="w")

        def _row(label, value, fg=TEXT):
            f = tk.Frame(outer, bg=BG)
            f.pack(fill="x", pady=1)
            tk.Label(f, text=label, font=FB, bg=BG, fg=SUB,
                     width=34, anchor="w").pack(side="left")
            tk.Label(f, text=str(value), font=FB, bg=BG, fg=fg).pack(side="left")

        def _table(headers, rows, col_widths):
            f = tk.Frame(outer, bg=SURF,
                         highlightbackground=BORDER, highlightthickness=1)
            f.pack(fill="x", pady=(4, 0))
            hf = tk.Frame(f, bg=SURF2)
            hf.pack(fill="x")
            for h, w in zip(headers, col_widths):
                tk.Label(hf, text=h, font=FL, bg=SURF2, fg=ACCENT,
                         width=w, anchor="w", padx=6).pack(side="left")
            for ri, row_vals in enumerate(rows):
                rf = tk.Frame(f, bg=SURF if ri % 2 == 0 else SURF2)
                rf.pack(fill="x")
                for val, w in zip(row_vals, col_widths):
                    tk.Label(rf, text=str(val), font=FB, bg=rf["bg"], fg=TEXT,
                             width=w, anchor="w", padx=6).pack(side="left")

        # ── Overview ──────────────────────────────────────────────────────────
        _section("OVERVIEW")
        _row("Total clips:", total)
        _row("Accepted as-is:",
             "{} ({:.0%})".format(ba["accepted"], ba["accepted"]/total if total else 0),
             SUCCESS)
        _row("Accepted with edits:",
             "{} ({:.0%})".format(ba["adjusted"], ba["adjusted"]/total if total else 0),
             WARN)
        _row("Ignored:",
             "{} ({:.0%})".format(ba["ignored"],  ba["ignored"] /total if total else 0),
             ERR)
        _row("Unreviewed:",
             "{} ({:.0%})".format(ba["unreviewed"], ba["unreviewed"]/total if total else 0),
             SUB)
        automation = s.get("automation_rate", 0)
        _row("Automation rate (used without edits):",
             "{:.0%}".format(ba["accepted"]/total if total else 0),
             SUCCESS if ba["accepted"]/total >= 0.7 else WARN)

        # ── Cross-table ───────────────────────────────────────────────────────
        _section("ALGO VERDICT  →  USER ACTION")
        DISPLAY_STATUSES = [
            ("ok / direct",      ["ok", "direct"]),
            ("low confidence",   ["low_confidence"]),
            ("no match",         ["no_match"]),
            ("error / no file",  ["error", "no_file", "not_run"]),
        ]
        ACTIONS = ["accepted", "adjusted", "ignored", "unreviewed"]
        ct_rows = []
        for label, statuses in DISPLAY_STATUSES:
            acc = sum(ct.get(st, {}).get("accepted",   0) for st in statuses)
            adj = sum(ct.get(st, {}).get("adjusted",   0) for st in statuses)
            ign = sum(ct.get(st, {}).get("ignored",    0) for st in statuses)
            unr = sum(ct.get(st, {}).get("unreviewed", 0) for st in statuses)
            row_tot = acc + adj + ign + unr
            ct_rows.append((label, row_tot, acc, adj, ign, unr))
        _table(
            ["ALGO VERDICT", "TOTAL", "ACCEPTED", "ADJUSTED", "IGNORED", "UNREVIEWED"],
            ct_rows,
            [18,  7,  10,  10,  9,  12],
        )

        # ── Confidence calibration ────────────────────────────────────────────
        _section("CONFIDENCE CALIBRATION")
        cc_rows = []
        for label, data in cc.items():
            n    = data["n"]
            used = data["used"]
            rate = data["rate"]
            bar  = ("█" * int((rate or 0) * 10)).ljust(10)
            cc_rows.append((label, n, used,
                            "{:.0%}  {}".format(rate, bar) if rate is not None else "—"))
        _table(
            ["CONFIDENCE", "CLIPS", "USED", "USAGE RATE"],
            cc_rows,
            [12, 7, 6, 22],
        )

        # ── Position accuracy ─────────────────────────────────────────────────
        if pa:
            _section("POSITION ACCURACY  (algo vs script timecode, accepted clips)")
            _row("Mean |Δ IN|:",  "{:.2f}s".format(pa["mean_abs_delta_in_s"]))
            _row("Mean |Δ OUT|:", "{:.2f}s".format(pa["mean_abs_delta_out_s"]))
            _row("Clips >1s off on IN:", str(pa["clips_over_1s_off"]))
            _row("Clips >2s off on IN:", str(pa["clips_over_2s_off"]), ERR if pa["clips_over_2s_off"] > 0 else TEXT)

        # ── Ordering violations ───────────────────────────────────────────────
        ovs = s.get("ordering_violations", [])
        if ovs:
            _section("ORDERING VIOLATIONS  ({} clips matched out of sequence)".format(len(ovs)))
            tk.Label(outer,
                     text="These clips matched a position in the interview earlier than the "
                          "previous pull from the same speaker — likely a wrong instance of "
                          "a repeated phrase.",
                     font=FB, bg=BG, fg=SUB,
                     wraplength=660, justify="left").pack(anchor="w", pady=(0, 4))
            ov_rows = []
            for ov in ovs:
                ov_rows.append((
                    ov["token"],
                    "#{:03d}".format(ov["order"]),
                    "{:.1f}s".format(ov["rec_in_s"]),
                    "-{:.1f}s".format(ov["went_back_s"]),
                    (ov.get("matched_text") or "")[:40],
                ))
            _table(
                ["TOKEN", "ORDER", "MATCHED AT", "WENT BACK", "MATCHED TEXT"],
                ov_rows,
                [14, 7, 11, 10, 42],
            )

        # ── Per-token ─────────────────────────────────────────────────────────
        _section("PER TOKEN")
        tok_rows = []
        for tok, e in sorted(bt.items(), key=lambda x: -x[1]["total"]):
            n   = e["total"]
            ok  = e["accepted"] + e["adjusted"]
            pct = "{:.0%}".format(ok/n) if n else "—"
            n_ov = e.get("ordering_violations", 0)
            tok_rows.append((
                tok, n,
                "{}/{}".format(e["algo_ok"], e["total"]),
                e["accepted"], e["adjusted"], e["ignored"], pct,
                n_ov if n_ov else "—",
            ))
        _table(
            ["TOKEN", "CLIPS", "ALGO OK/TOTAL", "ACCEPTED", "ADJUSTED", "IGNORED", "USED%", "SEQ?"],
            tok_rows,
            [16, 6, 14, 10, 10, 8, 7, 5],
        )

        # ── Footer ────────────────────────────────────────────────────────────
        dp = self._diagnostic_path()
        if dp:
            tk.Frame(outer, bg=BORDER, height=1).pack(fill="x", pady=(14, 4))
            tk.Label(outer, text="JSON: {}".format(dp),
                     font=("Courier New", 8), bg=BG, fg=SUB,
                     wraplength=700, justify="left").pack(anchor="w")

        self._btn(outer, "CLOSE", dlg.destroy, small=True).pack(pady=(12, 0))

        # ── Size and centre ───────────────────────────────────────────────────
        dlg.update_idletasks()
        w, h = max(680, dlg.winfo_reqwidth()), min(820, dlg.winfo_reqheight() + 20)
        sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
        dlg.geometry("{}x{}+{}+{}".format(w, h, (sw-w)//2, (sh-h)//2))

    def _s4_snapshot(self):
        """Return a serialisable snapshot of the current Step 4 card states."""
        snap = {}
        for e in getattr(self, "_rv", []):
            order = e["res"]["order"]
            r = e["res"]
            entry = {
                "segments":   r.get("segments", []),
                "rec_in_tc":  r.get("rec_in_tc",  ""),
                "rec_out_tc": r.get("rec_out_tc", ""),
                "rec_in_s":   r.get("rec_in_s",   0.0),
                "rec_out_s":  r.get("rec_out_s",  0.0),
                "status":     r.get("status", ""),
                "ignored":    e["skip_var"].get(),
                "accepted":   e.get("accepted_flag", [False])[0],
            }
            if "gap_after_s" in r:
                entry["gap_after_s"] = r["gap_after_s"]
            # Persist VO takes_data so edited segments survive save/load cycles
            if r.get("is_vo") and r.get("takes_data"):
                entry["takes_data"] = r["takes_data"]
            snap[str(order)] = entry
        return snap

    def _s4_save(self):
        """Persist the current Step 4 state to a sidecar JSON file."""
        path = self._step4_state_path()
        if not path:
            return
        try:
            import json as _json
            with open(path, "w", encoding="utf-8") as f:
                _json.dump(self._s4_snapshot(), f, indent=2)
        except Exception:
            pass

    def _s4_load(self):
        """Return saved Step 4 state dict, or {} if none exists."""
        path = self._step4_state_path()
        if path and not os.path.isfile(path):
            # Legacy fallback: sidecar was written beside the script before this change
            if hasattr(self, '_script_path') and self._script_path:
                legacy = os.path.splitext(self._script_path)[0] + "_step4state.json"
                if os.path.isfile(legacy):
                    path = legacy
        if not path or not os.path.isfile(path):
            return {}
        try:
            import json as _json
            with open(path, "r", encoding="utf-8") as f:
                return _json.load(f)
        except Exception:
            return {}

    def _s4_apply_state(self, snap):
        """Apply a snapshot dict to the live _rv card list."""
        inc = getattr(self, "_s4_increment_confirmed", lambda s="": None)
        dec = getattr(self, "_s4_decrement_confirmed", lambda s="": None)
        for e in getattr(self, "_rv", []):
            order = str(e["res"]["order"])
            if order not in snap:
                continue
            entry = snap[order]
            r = e["res"]

            # Restore segment data directly into the result dict
            if "segments" in entry:
                r["segments"]  = entry["segments"]
            if "rec_in_tc"  in entry:
                r["rec_in_tc"]  = entry["rec_in_tc"]
            if "rec_out_tc" in entry:
                r["rec_out_tc"] = entry["rec_out_tc"]
            if "rec_in_s"  in entry:
                r["rec_in_s"]  = entry["rec_in_s"]
            if "rec_out_s" in entry:
                r["rec_out_s"] = entry["rec_out_s"]
            if "status" in entry:
                r["status"] = entry["status"]
            if "gap_after_s" in entry:
                r["gap_after_s"] = entry["gap_after_s"]
            elif "gap_after_s" in r:
                del r["gap_after_s"]   # restore to "use global default"
            # Restore VO takes_data (keeps edited per-take segments in sync)
            if "takes_data" in entry and r.get("is_vo"):
                r["takes_data"] = entry["takes_data"]

            was_ignored  = e["skip_var"].get()
            was_accepted = e.get("accepted_flag", [False])[0]
            snap_ignored  = entry.get("ignored",  False)
            snap_accepted = entry.get("accepted", False)

            # Apply ignore state (toggle_ignore_fn toggles, so only call when mismatch)
            if snap_ignored != was_ignored:
                e.get("toggle_ignore_fn", lambda **kw: None)(from_restore=True)

            # Apply accepted state and update tally counters
            if snap_accepted and not was_accepted:
                e["accepted_flag"][0] = True
                e.get("set_accepted_fn", lambda: None)()
                inc(r.get("status", ""))
            elif not snap_accepted and was_accepted:
                e["accepted_flag"][0] = False
                e.get("set_normal_fn", lambda: None)()
                dec(r.get("status", ""))

    def _s4_push_undo(self):
        """Save current state onto the undo stack before a mutation."""
        stack = getattr(self, "_s4_undo_stack", None)
        if stack is None:
            self._s4_undo_stack = []
            self._s4_redo_stack = []
            stack = self._s4_undo_stack
        stack.append(self._s4_snapshot())
        self._s4_redo_stack.clear()
        # Keep undo history bounded
        if len(stack) > 80:
            stack.pop(0)

    def _s4_undo(self, event=None):
        stack = getattr(self, "_s4_undo_stack", [])
        if not stack:
            return
        self._s4_redo_stack.append(self._s4_snapshot())
        self._s4_apply_state(stack.pop())
        self._s4_save()

    def _s4_redo(self, event=None):
        stack = getattr(self, "_s4_redo_stack", [])
        if not stack:
            return
        self._s4_undo_stack.append(self._s4_snapshot())
        self._s4_apply_state(stack.pop())
        self._s4_save()

    def _build_setup_data(self):
        """Return a serialisable setup dict for the current state."""
        src_data = {}
        for tok in self.tokens:
            sp = self._pool.get_transcript_source(tok) if self._pool else None
            if sp: src_data[tok] = sp
        d = {
            "version":   1,
            "workflow":  getattr(self, "workflow", "script_xml"),
            "script":    getattr(self, "_script_path", ""),
            "assignments": {r["path"]: r["var"].get()
                            for r in self._pool._rows} if self._pool else {},
            "transcript_sources": src_data,
            "pad":       self.pad_var.get(),
            "gap":       self.gap_var.get(),
        }
        # Embed match results so the file is self-contained and restores to Step 4
        if getattr(self, "results", None):
            d["results"] = self.results
        return d

    def _numpy_safe_encoder(self):
        """Return a JSONEncoder subclass that converts numpy scalars/arrays."""
        class _Enc(json.JSONEncoder):
            def default(self, obj):
                try:
                    import numpy as _np
                    if isinstance(obj, _np.integer):  return int(obj)
                    if isinstance(obj, _np.floating): return float(obj)
                    if isinstance(obj, _np.ndarray):  return obj.tolist()
                except ImportError:
                    pass
                return super().default(obj)
        return _Enc

    def _save_setup(self):
        """Save setup to the default sidecar location (no dialog)."""
        if not getattr(self, '_script_path', None) or not getattr(self, '_pool', None):
            return
        path = self._setup_sidecar_path()
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._build_setup_data(), f, cls=self._numpy_safe_encoder(), indent=2)
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    def _build_full_session_data(self):
        """Return a complete session dict: setup + results + Step 4 state."""
        data = self._build_setup_data() if getattr(self, '_pool', None) else {}
        data["script"]   = getattr(self, "_script_path", "")
        data["workflow"] = getattr(self, "workflow", "script_xml")
        if getattr(self, '_rv', None):
            data["step4_state"] = self._s4_snapshot()
        return data

    def _save_as(self):
        """Prompt for a file path, write a complete session JSON, and remember the path."""
        if getattr(self, 'workflow', None) == 'aaf_xml':
            self._aaf_save_setup()
            return
        if not getattr(self, '_script_path', None):
            messagebox.showinfo("Nothing to save", "No session is open yet.")
            return
        init_dir  = (os.path.dirname(self._current_session_file)
                     if self._current_session_file else
                     os.path.dirname(getattr(self, "_script_path", "") or ""))
        init_file = (os.path.basename(self._current_session_file)
                     if self._current_session_file else "session.json")
        path = filedialog.asksaveasfilename(
            title="Save Session As",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
            initialdir=init_dir,
            initialfile=init_file)
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._build_full_session_data(), f,
                          cls=self._numpy_safe_encoder(), indent=2)
            self._current_session_file = path
            messagebox.showinfo("Saved", "Session saved to:\n{}".format(path))
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    def _quick_save(self, btn_ref=None):
        """Save to the current session file; prompt for a path on the first save."""
        if getattr(self, 'workflow', None) == 'aaf_xml':
            self._aaf_save_setup()
            return
        if not getattr(self, '_script_path', None):
            messagebox.showinfo("Nothing to save", "No session is open yet.")
            return
        # First save of this session — behave like Save As
        if not self._current_session_file:
            self._save_as()
            return
        try:
            with open(self._current_session_file, "w", encoding="utf-8") as f:
                json.dump(self._build_full_session_data(), f,
                          cls=self._numpy_safe_encoder(), indent=2)
            b = btn_ref[0] if btn_ref else None
            if b:
                try:
                    b.config(text="SAVED \u2713")
                    self.after(1800, lambda: b.config(text="SAVE"))
                except Exception:
                    pass
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

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

    def _scroll_card_to_top(self, card):
        """After a card expands, scroll so its top is at the top of the Step 4 list."""
        cv = getattr(self, "_s4_scroll_canvas", None)
        if not cv:
            return
        def _do(c=card, canvas=cv):
            try:
                c.update_idletasks()
                bbox = canvas.bbox("all")
                if not bbox or bbox[3] <= 0:
                    return
                # Scroll so that ~2 collapsed cards remain visible above the
                # expanded card for context (each ~44 px when collapsed).
                y = c.winfo_y()
                scroll_y = max(0, y - 88)
                canvas.yview_moveto(scroll_y / bbox[3])
            except Exception:
                pass
        self.after(20, _do)

    def _apply_setup_data(self, data):
        """Apply a setup dict (from _save_setup) to the current pool. Called from _step2."""
        assignments = data.get("assignments", {})
        missing = []
        self._pool._bulk_loading = True
        try:
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
        finally:
            self._pool._bulk_loading = False
        self._pool._rebuild_src_dropdowns()
        self._pool._refresh_count()
        if missing:
            messagebox.showwarning("Missing files",
                "{} file(s) from the saved session were not found:\n{}".format(
                    len(missing), "\n".join(basename(p) for p in missing[:5])))

    # ── Cross-machine session path remapping ──────────────────────────────────

    @staticmethod
    def _collect_session_paths(data):
        """Return every file path stored in a session dict, as a flat set."""
        paths = set()
        sp = data.get("script", "")
        if sp: paths.add(sp)
        for p in data.get("assignments", {}).keys():
            paths.add(p)
        for p in data.get("transcript_sources", {}).values():
            paths.add(p)
        for r in data.get("results", []):
            for key in ("source_audio", "source_video"):
                p = r.get(key, "")
                if p: paths.add(p)
        return paths

    @staticmethod
    def _is_foreign_platform_path(path):
        """Return True if path looks like it was saved on a different OS platform.
        Windows paths (C:\\, F:\\, etc.) look foreign on Mac/Linux, and vice-versa."""
        import re
        if not path:
            return False
        if sys.platform == "win32":
            # On Windows, a Unix-style absolute path is foreign
            return path.startswith("/")
        else:
            # On Mac/Linux, a Windows drive-letter path is foreign
            return bool(re.match(r'^[A-Za-z]:[/\\]', path))

    @staticmethod
    def _build_remap_index(folder):
        """Walk folder recursively and return {lowercase_filename: absolute_path}.
        Skips hidden directories (.pb_cache, .git, etc)."""
        index = {}
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in files:
                key = fn.lower()
                if key not in index:   # first match wins (shallowest path)
                    index[key] = os.path.join(root, fn)
        return index

    @staticmethod
    def _remap_path(old_path, video_index, audio_index):
        """Return the remapped path for old_path, routing to the correct index
        by file type. Falls back to the other index if not found in the primary."""
        fn  = os.path.basename(old_path).lower()
        ext = os.path.splitext(fn)[1]
        if ext in VIDEO_EXTS:
            return video_index.get(fn) or audio_index.get(fn) or old_path
        else:
            return audio_index.get(fn) or video_index.get(fn) or old_path

    @classmethod
    def _remap_session_data(cls, data, video_index, audio_index):
        """Rewrite all path fields in data in-place using separate video/audio indexes."""
        if data.get("script"):
            data["script"] = cls._remap_path(data["script"], video_index, audio_index)

        old_assignments = data.get("assignments", {})
        if old_assignments:
            data["assignments"] = {
                cls._remap_path(p, video_index, audio_index): tok
                for p, tok in old_assignments.items()
            }

        old_ts = data.get("transcript_sources", {})
        if old_ts:
            data["transcript_sources"] = {
                tok: cls._remap_path(p, video_index, audio_index)
                for tok, p in old_ts.items()
            }

        for r in data.get("results", []):
            for key in ("source_audio", "source_video"):
                p = r.get(key, "")
                if p: r[key] = cls._remap_path(p, video_index, audio_index)

    def _open_session(self):
        """Open a saved *_setup.json and jump straight to Step 2 with everything restored."""
        path = filedialog.askopenfilename(
            title="Open Session",
            filetypes=[("JSON files", "*.json"),
                       ("All files", "*.*")])
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            messagebox.showerror("Load failed", str(e))
            return

        # ── AAF→XML setup file — route to the AAF workflow ───────────────────
        if "aaf" in data and "script" not in data:
            aaf_path = data.get("aaf", "")
            if not aaf_path or not os.path.isfile(aaf_path):
                messagebox.showerror(
                    "AAF not found",
                    "The AAF file referenced by this setup could not be found:\n\n"
                    "{}".format(aaf_path or "(none)"))
                return
            self._pending_aaf_setup = data   # _aaf_step2 will restore after init
            self._aaf_load(aaf_path)
            return

        script_path = data.get("script", "")

        # ── Cross-machine path remapping ──────────────────────────────────────
        # Only offer path remapping when the session was created on a different OS
        # (i.e. the saved paths look foreign to the current platform).  If the
        # paths are native-format but the files are simply missing, show a plain
        # error instead — the user just needs to find the files themselves.
        if not script_path or not os.path.isfile(script_path):
            if not self._is_foreign_platform_path(script_path or ""):
                # Same-platform session with missing file — plain error, no remap.
                messagebox.showerror(
                    "Script not found",
                    "The script file referenced by this session could not be found:\n\n"
                    "{}".format(script_path or "(none)"))
                return

            # Foreign-platform session — offer to remap paths.
            all_paths  = self._collect_session_paths(data)
            n_missing  = sum(1 for p in all_paths if p and not os.path.isfile(p))
            n_total    = len([p for p in all_paths if p])

            ans = messagebox.askyesno(
                "Media not found",
                "{} of {} file reference(s) in this session could not be found "
                "at their saved locations.\n\n"
                "This usually means the session was created on a different machine. "
                "Would you like to locate your media folders so PostBridge can "
                "remap all paths automatically?".format(n_missing, n_total))
            if not ans:
                return

            video_folder = filedialog.askdirectory(
                title="Select your VIDEO folder (PostBridge will search subfolders)")
            if not video_folder:
                return

            audio_folder = filedialog.askdirectory(
                title="Select your AUDIO folder (PostBridge will search subfolders)")
            if not audio_folder:
                return

            video_index = self._build_remap_index(video_folder)
            audio_index = self._build_remap_index(audio_folder)
            self._remap_session_data(data, video_index, audio_index)
            script_path = data.get("script", "")

            # Verify the script resolved — it's the one non-negotiable path.
            if not script_path or not os.path.isfile(script_path):
                messagebox.showerror(
                    "Script not found",
                    "Could not find the script file '{}' inside the selected folders.\n\n"
                    "Make sure the script is somewhere inside the Video or Audio "
                    "folder you selected.".format(
                        os.path.basename(data.get("script", "(none)"))))
                return

            # Report how many paths resolved
            all_paths_after = self._collect_session_paths(data)
            still_missing   = [p for p in all_paths_after if p and not os.path.isfile(p)]
            if still_missing:
                messagebox.showwarning(
                    "Some files not found",
                    "{} file(s) could not be matched inside the selected folders "
                    "and will be skipped:\n\n{}".format(
                        len(still_missing),
                        "\n".join(os.path.basename(p) for p in still_missing[:8])
                        + ("\n…" if len(still_missing) > 8 else "")))

        # Migrate legacy workflow keys to the unified script_session workflow.
        # Preserve the old format preference so it pre-selects at Step 5.
        _raw_wf = data.get("workflow", "script_xml")
        if _raw_wf in ("script_aaf", "script_xml"):
            self.workflow    = "script_session"
            self._export_fmt = "aaf" if _raw_wf == "script_aaf" else "xml"
        else:
            self.workflow    = _raw_wf
            self._export_fmt = None
        self._current_session_file = path   # future saves go back to this file
        self._restore_s4           = True   # signal _step4 to restore saved state

        # Stash setup data — _step2 will pick it up after the pool is built
        self._pending_setup = data

        # Stash Step 4 state if embedded in the session file
        self._pending_s4_state = data.get("step4_state") or None

        # Results may be embedded directly in the setup JSON (preferred) or in a sidecar.
        # Check .pb_cache/ first (new location), fall back to legacy path beside the script.
        if data.get("results"):
            self._pending_results = data["results"]
        else:
            _cache_dir  = os.path.join(os.path.dirname(script_path), ".pb_cache")
            _script_stem = os.path.splitext(os.path.basename(script_path))[0]
            results_path = os.path.join(_cache_dir, _script_stem + "_results.json")
            if not os.path.isfile(results_path):
                results_path = os.path.splitext(script_path)[0] + "_results.json"
            if os.path.isfile(results_path):
                try:
                    with open(results_path, encoding="utf-8") as f:
                        self._pending_results = json.load(f)
                except Exception:
                    self._pending_results = None
            else:
                self._pending_results = None

        # Restore pad/gap global values if saved
        if "pad" in data:
            self.pad_var.set(data["pad"])
        if "gap" in data:
            self.gap_var.set(data["gap"])

        self._load_script(script_path)

    def _clear_cache(self, kind="all"):
        """Delete cached files from the .pb_cache directory next to the script.

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
            ".wmv", ".mpg", ".mpeg", ".ts", ".mts", ".m2ts",
            ".flv", ".ogv", ".3gp", ".dv", ".r3d", ".braw", ".ari",
            # Broadcast / studio audio
            ".wav", ".aif", ".aiff",
            # Consumer / archival audio
            ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus",
            ".wma", ".caf",
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
        if self.workflow in ("script_aaf", "script_session"):
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
        if self.workflow in ("script_aaf", "script_session"):
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

        # ── Pre-flight: warn if any VO audio has no pre-transcribed file ─────────
        _vo_uncached = []
        for pi, vb in self.vo_bins.items():
            paths = vb.get_paths()
            takes = engines.pair_takes(paths)
            for _vp, ap in takes:
                if ap:
                    _pb_words, _ = engines.pb_transcript_load(ap)
                    if _pb_words is None and engines.cache_load(ap) is None:
                        _vo_uncached.append(os.path.basename(ap))
        if _vo_uncached:
            _names = "\n".join("  •  {}".format(n) for n in _vo_uncached[:6])
            if len(_vo_uncached) > 6:
                _names += "\n  …and {} more".format(len(_vo_uncached) - 6)
            _msg = (
                "{} VO audio file{} have no pre-transcribed file:\n\n{}\n\n"
                "PostBridge will transcribe them now, which may take several minutes.\n"
                "Or cancel and use the Transcribe Media workflow to pre-transcribe "
                "them first.".format(
                    len(_vo_uncached),
                    "s" if len(_vo_uncached) != 1 else "",
                    _names)
            )
            if not messagebox.askyesno("No VO Transcriptions Found", _msg,
                                       icon="warning"):
                return

        # ── Pre-flight: warn about oversized pull windows ──────────────────────
        suspicious = [p for p in self.pulls
                      if p.get("out_seconds", 0) - p.get("in_seconds", 0) > MAX_EXTRACT_S]
        if suspicious and not self._warn_large_windows(suspicious):
            return

        self._clear()
        self._section("STEP 3 — RECONCILING")

        tk.Label(self.body,
                 text="Processing audio clips and matching to your script.  "
                      "Each clip is extracted and transcribed — this step can take "
                      "several minutes for longer episodes.  The progress bar updates "
                      "as each clip finishes.",
                 font=FB, bg=BG, fg=SUB, wraplength=860).pack(anchor="w", pady=(0,10))

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

        # Animated liveness dots — cycles independently of clip-level progress
        self._dot_lbl = tk.Label(prog_outer, text="", font=FL, bg=SURF, fg=ACCENT,
                                 anchor="w", padx=12, pady=(0))
        self._dot_lbl.pack(anchor="w", pady=(0, 4))
        self._dot_running = True

        def _pulse_dots(step=0):
            if not self._dot_running:
                return
            dots = ["·  ", "·· ", "···", " ··", "  ·", "   "]
            try:
                self._dot_lbl.config(text=dots[step % len(dots)])
            except Exception:
                return
            self.after(300, _pulse_dots, step + 1)

        self.after(0, _pulse_dots)

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

        # Collect transcript source paths here on the main thread — StringVar.get()
        # is not thread-safe and must not be called from the reconcile thread.
        transcript_sources = {
            tok: self._pool.get_transcript_source(tok)
            for tok in self.tokens
        }

        _cpu   = os.cpu_count() or 2
        _n_workers = 1 if getattr(self, "_reconcile_bg_mode",
                                  tk.BooleanVar()).get() else max(1, _cpu - 1)

        threading.Thread(
            target=self._run_reconcile,
            args=(int_assets, transcript_sources, _n_workers),
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

    def _run_reconcile(self, int_assets, transcript_sources, n_workers=None):
        if n_workers is None:
            _cpu = os.cpu_count() or 2
            n_workers = max(1, _cpu - 1)
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

        # Shared counters — protected by a lock because VO parts now transcribe
        # concurrently (one thread per VO part, up to MAX_WORKERS).
        import threading as _th
        _tx_lock = _th.Lock()

        def _transcribe_vo_part(pi, vb):
            """Transcribe all takes for one VO part. Returns list of take tuples."""
            nonlocal _tx_done, _tx_cached, _tx_fresh

            paths = vb.get_paths()
            takes = engines.pair_takes(paths)
            if not takes:
                return []

            self._log_line(
                "Transcribing VO Part {} — {} take{}…".format(
                    pi, len(takes), "s" if len(takes) != 1 else ""), INFO)

            part_takes = []
            for take_i, (vp, ap) in enumerate(takes):
                if self._cancel.is_set():
                    break

                if not ap:
                    self._log_line(
                        "  Part {} Take {}: no audio file, skipping".format(
                            pi, take_i + 1), WARN)
                    continue

                blobs       = None
                audio_words = engines.cache_load(ap)
                if audio_words is not None:
                    blobs = engines.cache_load_blobs(ap)
                    with _tx_lock:
                        _tx_done   += 1
                        _tx_cached += 1
                        _d, _t = _tx_done, _total_tx
                    self._log_line(
                        "  Part {} Take {}: {} words  [cached]".format(
                            pi, take_i + 1, len(audio_words)), INFO)
                    self._set_tx_progress(
                        _d, _t, "VO transcription — {} / {}  [cached]".format(_d, _t))
                elif (_pb_result := engines.pb_transcript_load(ap))[0] is not None:
                    audio_words, blobs = _pb_result
                    with _tx_lock:
                        _tx_done   += 1
                        _tx_cached += 1
                        _d, _t = _tx_done, _total_tx
                    self._log_line(
                        "  Part {} Take {}: {} words  [pre-transcribed]".format(
                            pi, take_i + 1, len(audio_words)), SUCCESS)
                    self._set_tx_progress(
                        _d, _t, "VO transcription — {} / {}  [pre-transcribed]".format(
                            _d, _t))
                else:
                    _t0 = time.perf_counter()
                    if WAVEFORM_CONFORM:
                        chunks = engines.detect_silence_splits(ap)
                        if chunks:
                            with _tx_lock:
                                _chunk_counts.append((pi, take_i + 1, len(chunks)))
                            self._log_line(
                                "  Part {} Take {}: {} chunk{} — transcribing…".format(
                                    pi, take_i + 1, len(chunks),
                                    "s" if len(chunks) != 1 else ""), INFO)
                            audio_words = engines.transcribe_in_chunks(ap, chunks)
                            self._log_line(
                                "  Part {} Take {}: chunked done in {:.1f}s "
                                "({} words)".format(
                                    pi, take_i + 1, time.perf_counter() - _t0,
                                    len(audio_words) if audio_words else 0), INFO)
                        else:
                            self._log_line(
                                "  Part {} Take {}: silence analysis failed — "
                                "falling back to full-file".format(
                                    pi, take_i + 1), WARN)

                    if audio_words is None:
                        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                            tmp_a = tf.name
                        try:
                            ok, err = engines.extract_window(ap, 0, 9999, tmp_a)
                            if not ok:
                                msg = "  Part {} Take {}: audio extract failed".format(
                                    pi, take_i + 1)
                                if err:
                                    msg += " — {}".format(
                                        err[:200] if len(err) > 200 else err)
                                self._log_line(msg, ERR)
                                continue
                            audio_words = engines.transcribe_clip(tmp_a)
                            self._log_line(
                                "  Part {} Take {}: full-file done in {:.1f}s "
                                "({} words)".format(
                                    pi, take_i + 1, time.perf_counter() - _t0,
                                    len(audio_words) if audio_words else 0), INFO)
                        finally:
                            try: os.unlink(tmp_a)
                            except: pass

                    if WAVEFORM_CONFORM and audio_words:
                        _t1 = time.perf_counter()
                        blobs = engines.detect_speech_blobs(ap)
                        self._log_line(
                            "  Part {} Take {}: {} blob{} in {:.1f}s".format(
                                pi, take_i + 1,
                                len(blobs) if blobs else 0,
                                "s" if (blobs and len(blobs) != 1) else "",
                                time.perf_counter() - _t1), INFO)

                    with _tx_lock:
                        _tx_done  += 1
                        _tx_fresh += 1
                        _d, _t = _tx_done, _total_tx
                    self._set_tx_progress(
                        _d, _t, "VO transcription — {} / {}".format(_d, _t))

                    if not audio_words:
                        self._log_line(
                            "  Part {} Take {}: no words transcribed".format(
                                pi, take_i + 1), ERR)
                        continue
                    engines.cache_save(ap, audio_words, blobs=blobs)
                    self._log_line(
                        "  Part {} Take {}: {} words  [saved to cache]".format(
                            pi, take_i + 1, len(audio_words)), INFO)

                v_offset = 0.0
                if vp and not self._is_aaf_mode():
                    v_offset = engines.detect_av_offset(ap, vp)
                    self._log_line(
                        "  Part {} Take {}: {} words  video offset {:+.2f}s".format(
                            pi, take_i + 1, len(audio_words), v_offset), SUCCESS)
                else:
                    self._log_line(
                        "  Part {} Take {}: {} words{}".format(
                            pi, take_i + 1, len(audio_words),
                            "  (no video)" if not vp else "  (AAF — offset skipped)"),
                        SUCCESS)

                part_takes.append((audio_words, v_offset, vp, ap, blobs))

            return part_takes

        # Submit all VO parts concurrently — independent files can transcribe
        # in parallel; n_workers caps the concurrency.
        with ThreadPoolExecutor(max_workers=n_workers) as vo_ex:
            vo_futs = {vo_ex.submit(_transcribe_vo_part, pi, vb): pi
                       for pi, vb in self.vo_bins.items()
                       if not self._cancel.is_set()}
            for fut in _fut_as_completed(vo_futs):
                pi = vo_futs[fut]
                try:
                    part_takes = fut.result()
                    if part_takes:
                        vo_takes_by_part[pi] = part_takes
                except Exception as e:
                    self._log_line(
                        "VO Part {}: transcription error — {}".format(pi, e), ERR)

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

        def process_token_pulls(token, token_pulls):
            """Process all pulls for a single token sequentially with a time cursor.

            Pulls are processed in script order.  After each successful match,
            the cursor advances to rec_out_s so the next pull can only match
            audio that comes *after* the previous one — enforcing the temporal
            ordering guarantee the user confirmed (VO files and interview pulls
            always follow script order in audio time).
            """
            results = []
            cursor  = 0.0
            tsrc    = transcript_sources.get(token)
            pad     = self.pad_var.get()
            for pull in token_pulls:
                if self._cancel.is_set():
                    r = engines._base_result(pull)
                    r["status"] = "cancelled"
                    results.append(r)
                    continue
                r = engines.reconcile_interview_pull(pull, tsrc, pad=pad,
                                                     min_start_s=cursor)
                # For video-sourced clips (no audio transcript), attach the video
                # path so the waveform editor can still open for manual IN/OUT.
                if not tsrc:
                    vid = next((p for p in int_assets.get(token, [])
                                if is_video(p)), None)
                    if vid and not r.get("source_audio"):
                        r["source_video"] = vid
                results.append(r)
                # Advance the cursor past the matched endpoint so subsequent pulls
                # from this token don't re-match earlier audio.
                if r.get("status") in ("ok", "low_confidence", "snapped") \
                        and r.get("rec_out_s", 0.0) > 0.0:
                    cursor = max(cursor, r["rec_out_s"])
            return results

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

        # Group pulls by token and sort each group by script order so the
        # temporal ordering constraint can be applied with a per-token cursor.
        _pulls_by_token = _dd(list)
        for p in pulls:
            _pulls_by_token[p["token"]].append(p)
        for _tok in _pulls_by_token:
            _pulls_by_token[_tok].sort(key=lambda p: p["order"])

        all_items = (
            [("pull_token", (tok, tpulls)) for tok, tpulls in _pulls_by_token.items()] +
            [("vo_part",    (pi, blocks))  for pi, blocks in _vo_by_part.items()]
        )
        # Total expected *results* (one per pull + one per individual VO block)
        total = len(pulls) + len(self.vo_blocks)

        def dispatch(item):
            t0   = time.perf_counter()
            kind, obj = item
            if kind == "pull_token":
                tok, tpulls = obj
                res = process_token_pulls(tok, tpulls)
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
        ex = ThreadPoolExecutor(max_workers=n_workers)
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
                            if kind == "pull_token":
                                _tok, tpulls = obj
                                for pull in tpulls:
                                    r = engines._base_result(pull)
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
                        if kind == "pull_token":
                            _tok, tpulls = obj
                            res_list = []
                            for pull in tpulls:
                                r = engines._base_result(pull)
                                r["status"] = "error"; r["matched_text"] = str(e)
                                res_list.append(r)
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
        self._dot_running = False   # stop the animated dots
        for txt, color in getattr(self, "_pending_summary", []):
            self._log_line(txt, color)
        self._pending_summary = []
        self.after(800, self._step4)

    def _cancel_reconcile(self):
        self._cancel.set()
        self._dot_running = False
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

        # Always restore saved Step 4 edits for this script so that accepted
        # changes survive fresh reconciliation runs.  When a session file was
        # explicitly opened via OPEN, prefer the session-embedded state over
        # the sidecar (the session file may carry newer edits than the sidecar).
        _restore_requested = getattr(self, "_restore_s4", False)
        self._restore_s4   = False   # consume the flag
        if _restore_requested:
            _saved_s4 = (getattr(self, "_pending_s4_state", None)
                         or self._s4_load())
            self._pending_s4_state = None
        else:
            # Fresh reconciliation run — still load the sidecar so the user's
            # previously saved Step 4 edits are not silently discarded.
            _saved_s4 = self._s4_load()
        if _saved_s4:
            for r in self.results:
                entry = _saved_s4.get(str(r["order"]))
                if not entry:
                    continue
                # New format stores float segments directly
                if entry.get("segments"):
                    segs = entry["segments"]
                    r["segments"]  = segs
                    r["rec_in_s"]  = segs[0][0]
                    r["rec_out_s"] = segs[-1][1]
                    r["rec_in_tc"]  = entry.get("rec_in_tc",  secs_tc(segs[0][0]))
                    r["rec_out_tc"] = entry.get("rec_out_tc", secs_tc(segs[-1][1]))
                elif entry.get("segs"):
                    # Legacy format: list of (in_tc_str, out_tc_str)
                    segs_tc = entry["segs"]
                    r["rec_in_tc"]  = segs_tc[0][0]
                    r["rec_out_tc"] = segs_tc[-1][1]
                    try:
                        r["rec_in_s"]  = tc_secs(segs_tc[0][0])
                        r["rec_out_s"] = tc_secs(segs_tc[-1][1])
                        r["segments"]  = [(tc_secs(i), tc_secs(o))
                                          for i, o in segs_tc]
                    except Exception:
                        pass
                if entry.get("status"):
                    r["status"] = entry["status"]
                r["_s4_ignored"]  = entry.get("ignored",  False)
                r["_s4_accepted"] = entry.get("accepted", False)
                if "gap_after_s" in entry:
                    r["gap_after_s"] = entry["gap_after_s"]
                # Restore VO takes_data (keeps edited per-take segments in sync)
                if "takes_data" in entry and r.get("is_vo"):
                    r["takes_data"] = entry["takes_data"]

        # Reset undo/redo stacks for this Step 4 session
        self._s4_undo_stack = []
        self._s4_redo_stack = []

        self._clear()
        self._section("STEP 4 — REVIEW")
        tk.Label(self.body,
                 text="Reconcile log.  "
                      "[~] low confidence    [?] unmatched fallback    "
                      "These are flagged by name in the exported XML.",
                 font=FB, bg=BG, fg=SUB, wraplength=860).pack(anchor="w", pady=(0,6))

        _CLEAN = ("ok", "direct", "no_quote", "cancelled")
        _ATTN  = ("no_match", "error", "low_confidence", "no_file", "not_run")

        ok     = sum(1 for r in self.results if r["status"] in ("ok", "direct"))
        review = sum(1 for r in self.results if r["status"] in
                     ("no_match", "error", "low_confidence", "not_run"))
        skips  = sum(1 for r in self.results if r["status"] in ("no_file", "cancelled"))
        flags  = review + skips   # total items needing attention (used by filter tabs)

        # Live counters — each category decrements as items are confirmed
        _ok_count        = [ok]
        _review_count    = [review]
        _confirmed_count = [0]
        _ignored_count   = [0]
        _ok_var          = tk.StringVar(value=str(ok))
        _review_var      = tk.StringVar(value=str(review))
        _confirmed_var   = tk.StringVar(value="0")
        _ignored_var     = tk.StringVar(value="0")
        _unconfirmed_count = [0]   # mutable; set accurately after card-restore loop

        def _sync_unconfirmed_btn():
            try:
                _fbtns["unconfirmed"].config(
                    text="UNCONFIRMED  {}".format(_unconfirmed_count[0]))
                if _fstate["mode"] == "unconfirmed":
                    _apply_filter()
            except Exception:
                pass

        def _increment_confirmed(status=""):
            """Move an item from its source bucket into confirmed."""
            _confirmed_count[0] += 1
            _confirmed_var.set(str(_confirmed_count[0]))
            _confirmed_num_lbl.config(fg=SUCCESS)
            if status in ("ok", "direct"):
                _ok_count[0] = max(0, _ok_count[0] - 1)
                _ok_var.set(str(_ok_count[0]))
                _ok_num_lbl.config(fg=SUCCESS if _ok_count[0] > 0 else SUB)
            elif status in ("no_match", "error", "low_confidence", "not_run"):
                _review_count[0] = max(0, _review_count[0] - 1)
                _review_var.set(str(_review_count[0]))
                _review_num_lbl.config(fg=WARN if _review_count[0] > 0 else SUB)
                try:
                    _fbtns["attention"].config(
                        text="NEEDS ATTENTION  {}".format(_review_count[0]))
                except Exception:
                    pass
            _unconfirmed_count[0] = max(0, _unconfirmed_count[0] - 1)
            _sync_unconfirmed_btn()

        def _decrement_confirmed(status=""):
            """Return an item from confirmed back to its source bucket (un-accept)."""
            _confirmed_count[0] = max(0, _confirmed_count[0] - 1)
            _confirmed_var.set(str(_confirmed_count[0]))
            _confirmed_num_lbl.config(fg=SUCCESS if _confirmed_count[0] > 0 else SUB)
            if status in ("ok", "direct", "manual"):
                _ok_count[0] += 1
                _ok_var.set(str(_ok_count[0]))
                _ok_num_lbl.config(fg=SUCCESS)
            elif status in ("no_match", "error", "low_confidence", "not_run"):
                _review_count[0] += 1
                _review_var.set(str(_review_count[0]))
                _review_num_lbl.config(fg=WARN)
                try:
                    _fbtns["attention"].config(
                        text="NEEDS ATTENTION  {}".format(_review_count[0]))
                except Exception:
                    pass
            _unconfirmed_count[0] += 1
            _sync_unconfirmed_btn()

        def _increment_ignored():
            _ignored_count[0] += 1
            _ignored_var.set(str(_ignored_count[0]))
            try: _ign_num_lbl.config(fg=ERR)
            except Exception: pass
            _unconfirmed_count[0] = max(0, _unconfirmed_count[0] - 1)
            _sync_unconfirmed_btn()

        def _decrement_ignored():
            _ignored_count[0] = max(0, _ignored_count[0] - 1)
            _ignored_var.set(str(_ignored_count[0]))
            try: _ign_num_lbl.config(fg=ERR if _ignored_count[0] > 0 else SUB)
            except Exception: pass
            _unconfirmed_count[0] += 1
            _sync_unconfirmed_btn()

        srow = tk.Frame(self.body, bg=SURF,
                        highlightbackground=BORDER, highlightthickness=1)
        srow.pack(fill="x", pady=(0,8))

        # ── Stat cell: matched ─────────────────────────────────────────────
        _ok_cell = tk.Frame(srow, bg=SURF)
        _ok_cell.pack(side="left", padx=20, pady=6)
        _ok_num_lbl = tk.Label(_ok_cell, textvariable=_ok_var,
                               font=("Courier New",18,"bold"), bg=SURF,
                               fg=SUCCESS if ok > 0 else SUB)
        _ok_num_lbl.pack()
        tk.Label(_ok_cell, text="matched", font=FB, bg=SURF, fg=SUB).pack()

        # ── Stat cell: need review ─────────────────────────────────────────
        _review_cell = tk.Frame(srow, bg=SURF)
        _review_cell.pack(side="left", padx=20, pady=6)
        _review_num_lbl = tk.Label(_review_cell, textvariable=_review_var,
                                   font=("Courier New",18,"bold"), bg=SURF,
                                   fg=WARN if review > 0 else SUB)
        _review_num_lbl.pack()
        tk.Label(_review_cell, text="need review", font=FB, bg=SURF, fg=SUB).pack()

        # ── Stat cell: no file (static) ────────────────────────────────────
        _nf_cell = tk.Frame(srow, bg=SURF)
        _nf_cell.pack(side="left", padx=20, pady=6)
        tk.Label(_nf_cell, text=str(skips),
                 font=("Courier New",18,"bold"), bg=SURF,
                 fg=ERR if skips else SUB).pack()
        tk.Label(_nf_cell, text="no file", font=FB, bg=SURF, fg=SUB).pack()

        # ── Stat cell: confirmed (live) ────────────────────────────────────
        _conf_cell = tk.Frame(srow, bg=SURF)
        _conf_cell.pack(side="left", padx=20, pady=6)
        _confirmed_num_lbl = tk.Label(_conf_cell, textvariable=_confirmed_var,
                                      font=("Courier New",18,"bold"), bg=SURF, fg=SUB)
        _confirmed_num_lbl.pack()
        tk.Label(_conf_cell, text="confirmed", font=FB, bg=SURF, fg=SUB).pack()

        # ── Stat cell: ignored (live) ──────────────────────────────────────
        _ign_cell = tk.Frame(srow, bg=SURF)
        _ign_cell.pack(side="left", padx=20, pady=6)
        _ign_num_lbl = tk.Label(_ign_cell, textvariable=_ignored_var,
                                font=("Courier New",18,"bold"), bg=SURF, fg=SUB)
        _ign_num_lbl.pack()
        tk.Label(_ign_cell, text="ignored", font=FB, bg=SURF, fg=SUB).pack()

        # ── Filter tabs ───────────────────────────────────────────────────────
        _fstate = {"mode": "all", "sort": "script"}
        _fbtns  = {}
        _sbtns  = {}

        _part_dividers = {}   # part_index → divider Frame (filled during card loop)

        def _apply_filter(mode=None, sort=None):
            if mode is not None:
                _fstate["mode"] = mode
            if sort is not None:
                _fstate["sort"] = sort
            cur_mode = _fstate["mode"]
            cur_sort = _fstate["sort"]

            for m, b in _fbtns.items():
                active = (m == cur_mode)
                b.config(bg=ACCENT if active else SURF2,
                         fg=BG    if active else TEXT)
            for s, b in _sbtns.items():
                active = (s == cur_sort)
                b.config(bg=ACCENT if active else SURF2,
                         fg=BG    if active else TEXT)

            # Freeze scrollregion recalculation during repack
            _s4cv = getattr(self, "_s4_scroll_canvas", None)
            if _s4cv:
                sf.unbind("<Configure>")

            # Unpack everything
            for e in self._rv:
                e["card"].pack_forget()
            for div in _part_dividers.values():
                div.pack_forget()

            # Determine visible entries
            visible = []
            for e in self._rv:
                st = e["res"].get("status", "")
                show = (cur_mode == "all" or
                        (cur_mode == "attention"   and st in _ATTN) or
                        (cur_mode == "matched"     and st in ("ok", "direct")) or
                        (cur_mode == "unconfirmed" and
                         not e["accepted_flag"][0] and not e["skip_var"].get()))
                if show:
                    visible.append(e)

            # Apply sort
            if cur_sort == "confidence":
                def _conf_key(e):
                    c = e["res"].get("confidence", 0) or 0
                    # Items with no confidence (adjusted/manual) sink to the bottom
                    return (1, 0) if c == 0 else (0, c)
                visible.sort(key=_conf_key)
            elif cur_sort == "subclips":
                visible.sort(
                    key=lambda e: len(e["res"].get("segments") or []),
                    reverse=True)
            # "script" order → no sort (already in script order)

            # Repack — only show part dividers in script order
            use_dividers = (cur_sort == "script")
            cur_pi = object()  # sentinel
            for e in visible:
                if use_dividers:
                    pi = e["res"].get("part_index", 0)
                    if pi != cur_pi and pi in _part_dividers:
                        _part_dividers[pi].pack(fill="x", pady=(10, 2), padx=2)
                        cur_pi = pi
                e["card"].pack(fill="x", pady=(0, 4), padx=2)

            # Unfreeze scrollregion — one layout pass covers all repacked cards
            if _s4cv:
                sf.bind("<Configure>",
                        lambda e, c=_s4cv: c.configure(scrollregion=c.bbox("all")))
                sf.update_idletasks()
                _s4cv.configure(scrollregion=_s4cv.bbox("all"))

        frow = tk.Frame(self.body, bg=BG)
        frow.pack(fill="x", pady=(0, 4))
        _unc_initial = sum(
            1 for r in self.results
            if not r.get("_s4_accepted") and not r.get("_s4_ignored")
        )
        for _fm, _fl, _fc in [
            ("all",          "ALL",              len(self.results)),
            ("matched",      "MATCHED",          ok),
            ("attention",    "NEEDS ATTENTION",  flags),
            ("unconfirmed",  "UNCONFIRMED",      _unc_initial),
        ]:
            b = tk.Label(frow, text="{}  {}".format(_fl, _fc), font=FB,
                         bg=SURF2, fg=TEXT, cursor="hand2",
                         padx=14, pady=6, bd=0,
                         highlightbackground=BORDER, highlightthickness=1)
            b.pack(side="left", padx=(0, 4))
            b.bind("<Button-1>", lambda e, m=_fm: _apply_filter(mode=m))
            _fbtns[_fm] = b

        # ── Sort controls ──────────────────────────────────────────────────────
        srow = tk.Frame(self.body, bg=BG)
        srow.pack(fill="x", pady=(0, 8))
        tk.Label(srow, text="SORT:", font=FB, bg=BG, fg=SUB).pack(side="left")
        for _sk, _sl in [
            ("script",     "Script Order"),
            ("confidence", "Confidence"),
            ("subclips",   "Sub-clips"),
        ]:
            b = tk.Label(srow, text=_sl, font=FB,
                         bg=SURF2, fg=TEXT, cursor="hand2",
                         padx=10, pady=4, bd=0,
                         highlightbackground=BORDER, highlightthickness=1)
            b.pack(side="left", padx=(4, 0))
            b.bind("<Button-1>", lambda e, s=_sk: _apply_filter(sort=s))
            _sbtns[_sk] = b

        sf = self._scroll_frame(self.body, height=380)
        self._s4_scroll_canvas = self._last_scroll_canvas
        # Freeze scrollregion updates during card building — rebind after loop
        sf.unbind("<Configure>")
        self._rv   = []
        self._skip = []

        STATUS_COLOR = {
            "ok":             SUCCESS,
            "direct":         SUCCESS,
            "manual":         SUCCESS,
            "low_confidence": WARN,
            "no_match":       ERR,
            "no_quote":       SUB,
            "error":          ERR,
            "no_file":        ERR,
            "not_run":        SUB,
            "cancelled":      SUB,
        }
        STATUS_LABEL = {
            "ok":             "\u2713  matched",
            "direct":         "\u2713  matched",
            "manual":         "\u2713  adjusted",
            "low_confidence": "\u26a0  low confidence",
            "no_match":       "\u2717  no match",
            "no_quote":       "\u2013  no quote text",
            "error":          "\u2717  error",
            "no_file":        "\u2717  no file assigned",
            "not_run":        "\u2013  not run",
            "cancelled":      "\u2013  cancelled",
        }

        # Accordion: only one card body open at a time.
        _open_card   = [None]
        _cur_part_idx = object()   # sentinel — won't match any real part_index

        # Visual stripe colors by card state
        _STRIPE_DEF = BORDER
        _STRIPE_ACC = SUCCESS
        _STRIPE_IGN = ERR

        for res in self.results:
            st  = res.get("status", "")
            sc  = STATUS_COLOR.get(st, SUB)
            sl  = STATUS_LABEL.get(st, st)
            pi  = res.get("part_index", 0)

            # ── Part divider ───────────────────────────────────────────────────
            if pi != _cur_part_idx:
                _cur_part_idx = pi
                part_name = next((p["name"] for p in self.parts
                                  if p["index"] == pi), "Part {}".format(pi))
                _div = tk.Frame(sf, bg=BG)
                _div.pack(fill="x", pady=(10, 2), padx=2)
                tk.Frame(_div, bg=BORDER, height=1).pack(fill="x")
                tk.Label(_div, text="  \u25c6  {}".format(part_name.upper()),
                         font=FL, bg=BG, fg=ACCENT).pack(anchor="w", pady=(2, 0))
                _part_dividers[pi] = _div

            # ── Card shell ─────────────────────────────────────────────────────
            card = tk.Frame(sf, bg=SURF,
                            highlightbackground=BORDER, highlightthickness=1)
            card.pack(fill="x", pady=(0, 4), padx=2)

            # Left color stripe — 4px, reflects accept/ignore/normal state
            _stripe = tk.Frame(card, bg=_STRIPE_DEF, width=4)
            _stripe.pack(side="left", fill="y")

            _inner = tk.Frame(card, bg=SURF)
            _inner.pack(side="left", fill="both", expand=True)

            hdr = tk.Frame(_inner, bg=SURF, cursor="hand2")
            hdr.pack(fill="x", padx=(4, 10), pady=(6, 6))

            _accepted_flag = [False]

            # ── Header left ────────────────────────────────────────────────────
            ord_lbl  = tk.Label(hdr, text="#{:03d}".format(res["order"]),
                                font=FL, bg=SURF, fg=SUB,
                                width=5, anchor="w", cursor="hand2")
            ord_lbl.pack(side="left")
            tok_lbl  = tk.Label(hdr, text=res["token"],
                                font=FL, bg=SURF, fg=ACCENT,
                                width=14, anchor="w", cursor="hand2")
            tok_lbl.pack(side="left")
            stat_lbl = tk.Label(hdr, text=sl, font=FB, bg=SURF, fg=sc,
                                cursor="hand2")
            stat_lbl.pack(side="left", padx=8)

            conf_hdr = res.get("confidence", 0)
            if conf_hdr:
                tk.Label(hdr, text="{:.0%}".format(conf_hdr),
                         font=FS, bg=SURF, fg=sc, cursor="hand2").pack(
                             side="left", padx=(0, 6))


            # Sub-clip count (number of segments) — always created, shown when > 1
            _clips_lbl = tk.Label(hdr, text="", font=FS, bg=SURF, fg=WARN,
                                  cursor="hand2")

            def _refresh_clips_lbl(cl=_clips_lbl, r=res):
                n = len(r.get("segments") or [(0, 0)])
                if n > 1:
                    cl.config(text="{} clips".format(n))
                    cl.pack(side="left", padx=(0, 6))
                else:
                    cl.pack_forget()

            _refresh_clips_lbl()

            # Timecode label — shown right after the status text when accepted
            _tc_lbl = tk.Label(hdr, text="", font=FS, bg=SURF, fg=SUB)
            # initially not packed

            d_in  = res.get("delta_in",  0)
            d_out = res.get("delta_out", 0)
            delta_lbl = None
            if st == "ok" and (abs(d_in) > 0.5 or abs(d_out) > 0.5):
                delta_lbl = tk.Label(hdr,
                            text="Δin:{:+.1f}s  Δout:{:+.1f}s".format(d_in, d_out),
                            font=FB, bg=SURF, fg=INFO, cursor="hand2")
                delta_lbl.pack(side="left", padx=4)
                self._tooltip(delta_lbl,
                    "Δin = matched IN point differs from script timecode by this amount\n"
                    "Δout = matched OUT point differs from script timecode by this amount\n"
                    "Positive = later in file  ·  Negative = earlier in file")

            skip_var = tk.BooleanVar(value=False)

            # ── Header right — state container ─────────────────────────────────
            # Normal: [ADJUST] [IGNORE]  |  Accepted: [✓ ACCEPTED]  |  Ignored: [⊘ IGNORED]
            _hdr_right = tk.Frame(hdr, bg=SURF)
            _hdr_right.pack(side="right")

            _norm_frame    = tk.Frame(_hdr_right, bg=SURF)
            _norm_frame.pack(side="left")

            _acc_state_lbl = tk.Label(_hdr_right, text="\u2713 ACCEPTED",
                                      font=FS, bg=SURF, fg=SUCCESS, cursor="hand2")
            # initially not packed

            _ign_state_lbl = tk.Label(_hdr_right, text="\u2298 IGNORED",
                                      font=FS, bg=SURF, fg=ERR, cursor="hand2")
            # initially not packed

            # source_audio: prefer explicit audio path, fall back to video path
            # so that clips sourced from video assets can still be reviewed.
            _src_audio = (res.get("source_audio", "") or
                          res.get("source_video", "") or "")

            # IGNORE button (only interactive control besides clicking to review)
            ignore_lbl = tk.Label(_norm_frame, text="IGNORE", font=FS,
                                  bg=SURF, fg=SUB, cursor="hand2")
            ignore_lbl.pack(side="right", padx=(4, 0))
            ignore_lbl.bind("<Enter>", lambda e, w=ignore_lbl: w.config(fg=ERR))
            ignore_lbl.bind("<Leave>", lambda e, w=ignore_lbl: w.config(fg=SUB))

            # ── State management helpers ────────────────────────────────────────
            def _set_normal(nf=_norm_frame, al=_acc_state_lbl,
                            il=_ign_state_lbl, tl=_tc_lbl, sw=_stripe, c=card):
                al.pack_forget(); il.pack_forget(); tl.pack_forget()
                nf.pack(side="left")
                sw.config(bg=_STRIPE_DEF)
                c.config(highlightbackground=BORDER, highlightthickness=1)

            def _set_accepted(nf=_norm_frame, al=_acc_state_lbl,
                              il=_ign_state_lbl, tl=_tc_lbl, sw=_stripe,
                              c=card, r=res):
                nf.pack_forget(); il.pack_forget()
                al.pack(side="left")
                in_tc  = r.get("rec_in_tc",  r.get("in_tc",  ""))
                out_tc = r.get("rec_out_tc", r.get("out_tc", ""))
                if in_tc and out_tc:
                    tl.config(text="  {}  \u2192  {}".format(in_tc, out_tc))
                    tl.pack(side="left", padx=(4, 0))
                sw.config(bg=_STRIPE_ACC)
                c.config(highlightbackground=SUCCESS, highlightthickness=2)

            def _set_ignored(nf=_norm_frame, al=_acc_state_lbl,
                             il=_ign_state_lbl, tl=_tc_lbl, sw=_stripe, c=card):
                nf.pack_forget(); al.pack_forget(); tl.pack_forget()
                il.pack(side="left")
                sw.config(bg=_STRIPE_IGN)
                c.config(highlightbackground=ERR, highlightthickness=2)

            def _un_accept(af=_accepted_flag, r=res, ss_n=_set_normal):
                self._s4_push_undo()
                af[0] = False
                r["_s4_accepted"] = False   # clear persisted accepted flag
                ss_n()
                # Use _original_status so we return the item to the right bucket
                eff_status = r.get("_original_status") or r.get("status", "")
                _decrement_confirmed(eff_status)
                self._s4_save()

            def _toggle_ignore(sv=skip_var, ss_n=_set_normal, ss_i=_set_ignored,
                               from_restore=False):
                if not from_restore:
                    self._s4_push_undo()
                sv.set(not sv.get())
                if sv.get():
                    ss_i()
                    _increment_ignored()
                else:
                    ss_n()
                    _decrement_ignored()
                if not from_restore:
                    self._s4_save()

            # ── Open waveform editor — primary card action ──────────────────────
            def _open_review(event=None, r=res, af=_accepted_flag,
                             ss=_set_accepted, sl=stat_lbl,
                             rcl=_refresh_clips_lbl):
                # Compute sa dynamically so session-restored results without
                # source_video still get a second chance via the pool.
                sa = r.get("source_audio") or r.get("source_video") or ""
                if not sa or not os.path.isfile(sa):
                    # Fallback: look for a video file in current pool assets
                    tok    = r.get("token", "")
                    assets = (getattr(self, "_pool", None) and
                              self._pool.get_interview_assets().get(tok, []))
                    vid = next((p for p in (assets or [])
                                if is_video(p) and os.path.isfile(p)), None)
                    if vid:
                        sa = vid
                        r["source_video"] = vid   # cache for future opens
                if not sa or not os.path.isfile(sa):
                    return
                segs_r = r.get("segments") or [(0.0, 30.0)]
                fps    = getattr(self, "_seq_fps", 24.0)

                def _accept(new_segs, r=r, af=af, ss=ss, sl=sl, rcl=rcl):
                    self._s4_push_undo()
                    old_status = r.get("status", "")
                    r["segments"]   = new_segs
                    r["rec_in_s"]   = new_segs[0][0]
                    r["rec_out_s"]  = new_segs[-1][1]
                    r["rec_in_tc"]  = secs_tc(new_segs[0][0])
                    r["rec_out_tc"] = secs_tc(new_segs[-1][1])
                    r["_s4_accepted"] = True   # persist across Step 4 re-entries
                    # For VO clips, build_aaf reads takes_data[best_i]["segments"],
                    # not the top-level segments, so keep them in sync.
                    if r.get("is_vo"):
                        _best_i = r.get("best_take_index", 0)
                        _td = r.get("takes_data") or []
                        if _td and _best_i < len(_td) and isinstance(_td[_best_i], dict):
                            # Matched VO: update the winning take's segments in-place.
                            _td[_best_i] = dict(_td[_best_i], segments=list(new_segs))
                        elif not _td:
                            # No-match VO: takes_data was empty so build_aaf would
                            # skip this clip entirely.  Synthesise a minimal entry
                            # from the audio file the waveform editor just used.
                            r["takes_data"]      = [{"apath": sa, "segments": list(new_segs)}]
                            r["best_take_index"] = 0
                    if old_status not in ("ok", "direct", "manual"):
                        # Remember the original status so restore/un-accept
                        # can decrement the correct tally bucket.
                        r["_original_status"] = old_status
                        r["status"] = "manual"
                        sl.config(text=STATUS_LABEL.get("manual", "\u2713  adjusted"),
                                  fg=STATUS_COLOR.get("manual", SUCCESS))
                    af[0] = True
                    ss()
                    rcl()   # refresh sub-clip count badge
                    _increment_confirmed(old_status)
                    self._s4_save()

                # Gather neighbouring quote text for script context
                _ctx_before = _ctx_after = ""
                _this_order = r.get("order", -1)
                for _ri, _rr in enumerate(self.results):
                    if _rr.get("order") == _this_order:
                        if _ri > 0:
                            _ctx_before = (self.results[_ri - 1]
                                           .get("quote_text", "") or "")
                        if _ri < len(self.results) - 1:
                            _ctx_after  = (self.results[_ri + 1]
                                           .get("quote_text", "") or "")
                        break

                # Words: prefer per-result transcription (interview pulls store
                # their windowed words there); fall back to full-file cache
                # (VO takes are cached against the take file path).
                _words = (r.get("words")
                          or engines.cache_load(sa)
                          or [])

                # Format scripted timecode range for display in the editor
                _stc_in  = r.get("in_tc",  "")
                _stc_out = r.get("out_tc", "")
                _scripted_tc = (
                    "{}  \u2192  {}".format(_stc_in, _stc_out)
                    if _stc_in and _stc_out else ""
                )

                MatchReviewDialog(self, sa, segs_r,
                                  title=r.get("token", ""),
                                  quote_text=r.get("quote_text", ""),
                                  matched_text=r.get("matched_text", ""),
                                  context_before=_ctx_before,
                                  context_after=_ctx_after,
                                  scripted_tc=_scripted_tc,
                                  words=_words,
                                  on_accept=_accept, fps=fps)

            # ── Bind labels ────────────────────────────────────────────────────
            ignore_lbl.bind("<Button-1>",
                            lambda e, f=_toggle_ignore: f())
            _acc_state_lbl.bind("<Button-1>",
                                lambda e, f=_un_accept: f())
            _ign_state_lbl.bind("<Button-1>",
                                lambda e, f=_toggle_ignore: f())

            # ── Card click → open waveform editor ──────────────────────────────
            def _hdr_click(event=None, sv=skip_var, fn=_open_review):
                if not sv.get():   # ignored cards do nothing on click
                    fn()

            for _w in [hdr, card, _inner, ord_lbl, tok_lbl, stat_lbl]:
                _w.bind("<Button-1>", _hdr_click)
            if delta_lbl:
                delta_lbl.bind("<Button-1>", _hdr_click)

            self._rv.append({
                "skip_var": skip_var, "res": res, "card": card,
                "ignore_lbl": ignore_lbl,
                "accepted_flag": _accepted_flag,
                "toggle_ignore_fn": _toggle_ignore,
                "set_accepted_fn": _set_accepted,
                "set_normal_fn":   _set_normal,
            })

            # ── Restore saved state ────────────────────────────────────────────
            if res.get("_s4_ignored") and not skip_var.get():
                _toggle_ignore(from_restore=True)
            elif res.get("_s4_accepted"):
                _accepted_flag[0] = True
                _set_accepted()
                # Use _original_status if available so we decrement the right bucket
                # (items that were low_confidence/no_match get status="manual" after
                # accept; without _original_status they would never clear _review_count)
                eff = res.get("_original_status") or res.get("status", "")
                _increment_confirmed(eff)

        # Store tally callbacks on self so _s4_apply_state can reach them
        self._s4_increment_confirmed = _increment_confirmed
        self._s4_decrement_confirmed = _decrement_confirmed

        # Recompute the UNCONFIRMED count from the live _rv state now that
        # card restoration has run (accepted_flag / skip_var are authoritative).
        _unc = sum(1 for e in self._rv
                   if not e["accepted_flag"][0] and not e["skip_var"].get())
        _unconfirmed_count[0] = _unc
        if "unconfirmed" in _fbtns:
            _fbtns["unconfirmed"].config(text="UNCONFIRMED  {}".format(_unc))

        # Restore scrollregion binding now that all cards are packed, then do
        # one layout pass so the canvas knows the full scroll extent.
        _cv = self._s4_scroll_canvas
        sf.bind("<Configure>",
                lambda e, c=_cv: c.configure(scrollregion=c.bbox("all")))
        sf.update_idletasks()
        _cv.configure(scrollregion=_cv.bbox("all"))

        # Apply default filter and highlight its tab
        _apply_filter(mode="all")

        # Flush the current (fully-restored) state to the sidecar so that if
        # the user clicks ← REDO and re-runs reconciliation, the next Step 4
        # entry can reload the correct positions from the sidecar rather than
        # showing stale cache-reconciliation results.
        self._s4_save()

        nav = tk.Frame(self.body, bg=BG); nav.pack(fill="x", pady=(8,0))
        self._btn(nav, "← REDO", self._step2).pack(side="left")
        self._btn(nav, "VIEW RECONCILE LOG", self._show_reconcile_log,
                  small=True).pack(side="left", padx=(12,0))
        if DEV_DIAGNOSTIC:
            self._btn(nav, "DIAGNOSTIC", self._show_diagnostic,
                      small=True).pack(side="left", padx=(4,0))

        # Undo / Redo buttons
        _undo_btn = self._btn(nav, "↩ UNDO", self._s4_undo, small=True)
        _undo_btn.pack(side="left", padx=(4,0))
        _redo_btn = self._btn(nav, "↪ REDO", self._s4_redo, small=True)
        _redo_btn.pack(side="left", padx=(2,0))

        self._btn(nav, "EXPORT  →", self._step5,
                  color=ACCENT).pack(side="right")

        # Keyboard shortcuts for undo/redo
        self.bind_all("<Control-z>",       self._s4_undo)
        self.bind_all("<Control-Z>",       self._s4_undo)
        self.bind_all("<Control-Shift-z>", self._s4_redo)
        self.bind_all("<Control-Shift-Z>", self._s4_redo)

    # ── Inline card-level audio playback ──────────────────────────────────────

    def _stop_playback(self):
        """Stop any currently playing audio (Windows winsound)."""
        try:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass

    def _play_segment(self, audio_path, start_s, duration_s, status_var=None):
        """
        Asynchronously extract and play a short segment.
        status_var: an optional tk.StringVar to show state ("playing…" / "").
        """
        if not audio_path or not os.path.isfile(audio_path):
            return
        self._stop_playback()
        import tempfile, wave as _wave
        import numpy as _np
        from concurrent.futures import ThreadPoolExecutor
        from engines import extract_audio_segment

        tmp_dir  = getattr(self, "_card_play_tmpdir", None)
        if tmp_dir is None or not os.path.isdir(tmp_dir):
            tmp_dir = tempfile.mkdtemp(prefix="pb_card_")
            self._card_play_tmpdir = tmp_dir
        out_wav = os.path.join(tmp_dir, "_card_play.wav")

        if status_var is not None:
            status_var.set("▶ …")

        def _do():
            extract_audio_segment(audio_path, max(0.0, start_s),
                                  max(0.1, duration_s), out_wav, sample_rate=44100)
            with _wave.open(out_wav, "rb") as wf:
                data = wf.readframes(wf.getnframes())
            arr  = _np.frombuffer(data, _np.int16).astype(_np.float32) / 32768.0
            peak = float(_np.max(_np.abs(arr))) if len(arr) else 0.0
            if peak > 1e-6:
                arr = arr / peak * 0.72
            pcm = (arr * 32767).astype(_np.int16)
            with _wave.open(out_wav, "wb") as wf:
                wf.setnchannels(1); wf.setsampwidth(2)
                wf.setframerate(44100)
                wf.writeframes(pcm.tobytes())
            return out_wav

        def _done(fut):
            try:
                path = fut.result()
            except Exception:
                if status_var is not None:
                    self.after(0, lambda: status_var.set(""))
                return
            def _play():
                try:
                    import winsound
                    winsound.PlaySound(path,
                                       winsound.SND_FILENAME | winsound.SND_ASYNC)
                except Exception:
                    pass
                if status_var is not None:
                    status_var.set("")
            self.after(0, _play)

        ex = ThreadPoolExecutor(max_workers=1)
        ex.submit(_do).add_done_callback(_done)
        ex.shutdown(wait=False)

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
        is_session = (self.workflow == "script_session")

        # Default to AAF for the unified Script → Session workflow
        if is_session and not self._export_fmt:
            self._export_fmt = "aaf"

        is_aaf = self._is_aaf_mode()
        fmt    = "AAF" if is_aaf else "XML"
        self._section("STEP 5 — EXPORT {}".format(fmt))

        # Format switcher row (only shown for script_session — lets user change their mind)
        if is_session:
            _sw_row = tk.Frame(self.body, bg=BG); _sw_row.pack(fill="x", pady=(0, 12))
            tk.Label(_sw_row, text="FORMAT", font=FL, bg=BG, fg=SUB,
                     width=26, anchor="w").pack(side="left")
            def _reswitch(f):
                self._export_fmt = f
                self.out_path.set("")
                self._step5()
            self._btn(_sw_row, "AAF", lambda: _reswitch("aaf"), small=True,
                      color=ACCENT if is_aaf else None).pack(side="left", padx=(0, 4))
            self._btn(_sw_row, "XML", lambda: _reswitch("xml"), small=True,
                      color=ACCENT if not is_aaf else None).pack(side="left")

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
        self._btn(row3, "BROWSE",
                  lambda: self._pick_out(ext), small=True).pack(side="right")
        tk.Entry(row3, textvariable=self.out_path, font=FB,
                 bg=SURF2, fg=TEXT, insertbackground=TEXT,
                 relief="flat", bd=6).pack(side="left", fill="x", expand=True, padx=(0, 8))

        # ── Clip-review panel: shows what will actually be exported ──────────
        # Segments are read live from self.results so any Step 4 edits
        # (via MatchReviewDialog) are reflected here before the build starts.
        _EXPORTABLE = ("ok", "direct", "manual", "low_confidence", "snapped")
        _clip_rows = []
        for _r in getattr(self, "results", []):
            if _r.get("is_vo"):
                continue
            _st = _r.get("status", "")
            if _st not in _EXPORTABLE:
                continue
            _segs = _r.get("segments") or []
            if not _segs:
                continue
            # Check skip flag via _rv
            _rv_e = next((e for e in getattr(self, "_rv", [])
                          if e["res"] is _r), None)
            if _rv_e and _rv_e["skip_var"].get():
                continue
            _clip_rows.append(_r)

        if _clip_rows:
            _cr_hdr = tk.Frame(self.body, bg=BG)
            _cr_hdr.pack(fill="x", pady=(0, 2))
            tk.Label(_cr_hdr, text="CLIP REVIEW  —  verify edits before building",
                     font=FL, bg=BG, fg=SUB).pack(side="left")
            tk.Label(_cr_hdr, text="{} interview clips".format(len(_clip_rows)),
                     font=FS, bg=BG, fg=SUB).pack(side="right")

            _cr_frame = tk.Frame(self.body, bg=SURF,
                                 highlightbackground=BORDER, highlightthickness=1)
            _cr_frame.pack(fill="x", pady=(0, 12))

            _cr_canvas = tk.Canvas(_cr_frame, bg=SURF, height=min(160, len(_clip_rows) * 22 + 8),
                                   highlightthickness=0)
            _cr_sb = _SlimScrollbar(_cr_frame, command=_cr_canvas.yview)
            _cr_inner = tk.Frame(_cr_canvas, bg=SURF)
            _cr_canvas.create_window((0, 0), window=_cr_inner, anchor="nw")
            _cr_canvas.configure(yscrollcommand=_cr_sb.set)

            def _cr_resize(e, cv=_cr_canvas, fr=_cr_inner):
                cv.configure(scrollregion=cv.bbox("all"))
            _cr_inner.bind("<Configure>", _cr_resize)
            _cr_canvas.pack(side="left", fill="both", expand=True)
            _cr_sb.pack(side="right", fill="y")

            for _row_i, _r in enumerate(_clip_rows):
                _bg = SURF if _row_i % 2 == 0 else SURF3
                _segs = _r.get("segments") or []
                _in_s  = _segs[0][0]  if _segs else _r.get("rec_in_s",  0)
                _out_s = _segs[-1][1] if _segs else _r.get("rec_out_s", 0)
                _acc   = _r.get("_s4_accepted", False)
                _acc_col = SUCCESS if _acc else SUB
                _acc_txt = "\u2713" if _acc else "\u25cb"

                _rrow = tk.Frame(_cr_inner, bg=_bg)
                _rrow.pack(fill="x")
                tk.Label(_rrow, text="#{:03d}".format(_r.get("order", 0)),
                         font=FS, bg=_bg, fg=SUB, width=5, anchor="w").pack(side="left", padx=(6,0))
                tk.Label(_rrow, text=_r.get("token", "?"),
                         font=FL, bg=_bg, fg=ACCENT, width=14, anchor="w").pack(side="left")
                tk.Label(_rrow, text=_acc_txt,
                         font=FS, bg=_bg, fg=_acc_col, width=2).pack(side="left")
                tk.Label(_rrow, text="in: {:10.4f}s".format(_in_s),
                         font=("Courier New", 10), bg=_bg, fg=TEXT, anchor="w").pack(side="left", padx=(4,0))
                tk.Label(_rrow, text="out: {:10.4f}s".format(_out_s),
                         font=("Courier New", 10), bg=_bg, fg=TEXT, anchor="w").pack(side="left", padx=(4,0))
                if len(_segs) > 1:
                    tk.Label(_rrow, text="({} segs)".format(len(_segs)),
                             font=FS, bg=_bg, fg=WARN, anchor="w").pack(side="left", padx=(4,0))

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
        # (skip_var, accepted_flag) back to their result dicts.
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
                clean_segs = [(s, e) for s, e in segs if e > s]
            else:
                clean_segs = []

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
        is_aaf   = self._is_aaf_mode()
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

                # ── Write a pre-build diagnostic snapshot (dev only) ─────────
                # Shows exactly what segments will be sent to build_aaf/build_xml.
                # Saved next to the output file as <name>_build_debug.json.
                if DEV_DIAGNOSTIC:
                    _debug_path = os.path.splitext(out_path)[0] + "_build_debug.json"
                    try:
                        _debug_clips = []
                        for _r in edited:
                            _td_dbg = None
                            if _r.get("is_vo"):
                                _td_raw = _r.get("takes_data") or []
                                _bi     = _r.get("best_take_index", 0)
                                _td_dbg = {
                                    "n_takes":   len(_td_raw),
                                    "best_i":    _bi,
                                    "best_apath": (_td_raw[_bi].get("apath") if _bi < len(_td_raw) else None),
                                    "best_segs":  ([list(s) for s in (_td_raw[_bi].get("segments") or [])]
                                                   if _bi < len(_td_raw) else None),
                                }
                            _debug_clips.append({
                                "order":    _r.get("order"),
                                "token":    _r.get("token"),
                                "status":   _r.get("status"),
                                "is_vo":    _r.get("is_vo", False),
                                "rec_in_s": _r.get("rec_in_s"),
                                "rec_out_s":_r.get("rec_out_s"),
                                "segments": [list(s) for s in (_r.get("segments") or [])],
                                "n_segs":   len(_r.get("segments") or []),
                                "takes_data_dbg": _td_dbg,
                            })
                        with open(_debug_path, "w", encoding="utf-8") as _df:
                            json.dump(_debug_clips, _df, indent=2)
                    except Exception:
                        pass

                _set_status("Probing media settings… ({} clips, {} with edits)".format(
                    len(edited),
                    sum(1 for _r in edited if _r.get("status") in ("manual", "ok", "direct"))))
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

                # Write diagnostic snapshot alongside the export (dev only)
                if DEV_DIAGNOSTIC:
                    try:
                        dp = self._diagnostic_path()
                        if dp:
                            _diag = self._build_diagnostic()
                            with open(dp, "w", encoding="utf-8") as _ddf:
                                json.dump(_diag, _ddf, indent=2)
                    except Exception:
                        pass

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
        fmt = "AAF" if self._is_aaf_mode() else "XML"
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
        self._aaf_step1_next_added  = False
        self._section("STEP 1 — LOAD PRO TOOLS AAF EXPORT  (AAF → XML)")

        # ── Nav pinned to bottom first so it's always visible ─────────────────
        nav = tk.Frame(self.body, bg=BG)
        nav.pack(side="bottom", fill="x", pady=(8,0))
        self._btn(nav, "\u2190 HOME", self._home).pack(side="left")
        self._aaf_step1_nav = nav   # NEXT button appended here once AAF is loaded

        # ── Scrollable content area fills remaining space ─────────────────────
        sf = self._scroll_frame(self.body)

        card = tk.Frame(sf, bg=SURF,
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

        # If we already have parsed AAF data (e.g. user pressed Back from Step 2),
        # restore the status label and show the NEXT button immediately — no reload needed.
        if self._aaf_data:
            total_clips = sum(len(t["clips"]) for t in self._aaf_data["tracks"])
            self._aaf_s1.config(
                text="\u2713  {}  \u00b7  {} tracks  \u00b7  {} clips  \u00b7  {} sources".format(
                    self._aaf_data["session_name"],
                    len(self._aaf_data["tracks"]),
                    total_clips,
                    len(getattr(self, "_aaf_sources", []))),
                fg=SUCCESS)
            self._btn(nav, "NEXT  \u2192  ASSIGN VIDEO",
                      self._aaf_step2, color=ACCENT).pack(side="right")
            self._aaf_step1_next_added = True

    def _aaf_load(self, path):
        if not path or not os.path.isfile(path): return
        if hasattr(self, "_aaf_s1"):
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
        self.workflow  = "aaf_xml"   # tells save/load routing which flow is active
        # Point the engine cache dir at a .pb_cache folder next to the AAF so
        # detect_rx_offset results persist across builds of the same session.
        engines._cache_dir = os.path.join(os.path.dirname(self._aaf_path), ".pb_cache")
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
        # Reset column widths so they recompute from new content on next visit
        if hasattr(self, "_aaf_col_widths"):
            del self._aaf_col_widths
        self._aaf_col_widths_vid_count = (-1, -1)
        # Reset hidden sources and sort for new AAF
        self._aaf_hidden_sources = set()
        self._aaf_sort_col = "name"   # default: source clip A→Z (▲ visible from start)
        self._aaf_sort_dir = "asc"

        if hasattr(self, "_aaf_s1"):
            self._aaf_s1.config(
                text="\u2713  {}  \u00b7  {} tracks  \u00b7  {} clips  \u00b7  {} sources".format(
                    parsed["session_name"], len(parsed["tracks"]),
                    total_clips, len(self._aaf_sources)),
                fg=SUCCESS)

        self.seq_name.set(parsed["session_name"])
        # If we're on the step-1 screen, add the NEXT button; otherwise advance directly.
        if hasattr(self, "_aaf_step1_nav") and not getattr(self, "_aaf_step1_next_added", False):
            self._btn(self._aaf_step1_nav, "NEXT  →  ASSIGN VIDEO",
                      self._aaf_step2, color=ACCENT).pack(side="right")
            self._aaf_step1_next_added = True
        else:
            self.after(0, self._aaf_step2)

    def _aaf_step2(self):
        # Preserve pool state so Back → Next doesn't lose work
        _saved_vpaths = list(getattr(self, "_aaf_video_paths", []))
        _saved_apaths = list(getattr(self, "_aaf_audio_paths", []))
        self._clear()
        # Header row: step title on left, UNDO/REDO/CLEAN UP grouped on right
        _hdr = tk.Frame(self.body, bg=BG)
        _hdr.pack(fill="x", pady=(20, 6))
        tk.Frame(_hdr, bg=ACCENT, width=3, height=20).pack(side="left", padx=(0, 10))
        tk.Label(_hdr, text="STEP 2 \u2014 ASSIGN VIDEO MEDIA",
                 font=FL, bg=BG, fg=TEXT).pack(side="left", anchor="w")
        self._btn(_hdr, "CLEAN UP",
                  self._aaf_remove_unassigned, small=True).pack(side="right")
        self._btn(_hdr, "\u21b7 REDO",
                  self._aaf_sync_redo, small=True).pack(side="right", padx=(0, 4))
        self._btn(_hdr, "\u21b6 UNDO",
                  self._aaf_sync_undo, small=True).pack(side="right", padx=(0, 4))
        # Keyboard bindings (idempotent — safe to re-bind each visit)
        self.bind_all("<Control-z>", lambda e: self._aaf_sync_undo())
        self.bind_all("<Control-y>", lambda e: self._aaf_sync_redo())

        total_clips  = sum(len(t["clips"]) for t in self._aaf_data["tracks"])
        tk.Label(self.body,
                 text="{} clips \u00b7 {} tracks \u00b7 {} sources \u2014 "
                      "add video files below, then assign each source to its video.".format(
                          total_clips, len(self._aaf_data["tracks"]),
                          len(self._aaf_sources)),
                 font=FB, bg=BG, fg=SUB).pack(anchor="w", pady=(0, 10))

        # ── Progress bar and nav pinned to bottom so they're always visible ────
        self._aaf_prog_frame = tk.Frame(self.body, bg=BG)
        self._aaf_prog_frame.pack(side="bottom", fill="x", pady=(6, 0))
        self._aaf_prog_lbl = tk.Label(self._aaf_prog_frame, text="", font=FB,
                                      bg=BG, fg=SUB, anchor="w")
        self._aaf_prog_lbl.pack(fill="x", pady=(0, 3))
        self._aaf_prog_bar = _FlatProgressBar(self._aaf_prog_frame, height=4)
        self._aaf_prog_bar.pack(fill="x")
        self._aaf_prog_frame.pack_forget()   # hidden until build starts

        nav = tk.Frame(self.body, bg=BG)
        nav.pack(side="bottom", fill="x", pady=(8,0))
        self._aaf_build_btn = self._btn(nav, "BUILD XML  \u2192", self._aaf_build,
                                        color=ACCENT)
        self._aaf_build_btn.pack(side="right")

        # ── Scrollable content area fills remaining space ─────────────────────
        sf = self._scroll_frame(self.body)

        # ── Video file pool (collapsible) ──────────────────────────────────────
        pool_frame = tk.Frame(sf, bg=SURF,
                              highlightbackground=BORDER, highlightthickness=1)
        pool_frame.pack(fill="x", pady=(0,8), padx=2)

        v_body = tk.Frame(pool_frame, bg=SURF)   # collapsible content
        v_open = [False]

        ph = tk.Frame(pool_frame, bg=SURF, cursor="hand2")
        ph.pack(fill="x", padx=12, pady=(10,4))

        v_arrow = tk.Label(ph, text="\u25b6",
                           font=FB, bg=SURF, fg=ACCENT, cursor="hand2")
        v_arrow.pack(side="left", padx=(0, 6))
        tk.Label(ph, text="VIDEO FILES", font=FL, bg=SURF, fg=ACCENT,
                 cursor="hand2").pack(side="left")
        self._aaf_count_lbl = tk.Label(ph, text="0 files", font=FB, bg=SURF, fg=SUB)
        self._aaf_count_lbl.pack(side="right")
        self._btn(ph, "+ BROWSE FOLDER", self._aaf_browse_folder,
                  small=True).pack(side="right", padx=(0, 4))
        self._btn(ph, "+ BROWSE FILES",  self._aaf_browse_files,
                  small=True).pack(side="right", padx=(0, 4))

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

        # Body starts collapsed
        self._aaf_video_paths = []
        self._aaf_file_list   = tk.Frame(v_body, bg=SURF)
        self._aaf_file_list.pack(fill="x", padx=12)

        self._aaf_status_lbl = tk.Label(v_body, text="", font=FB, bg=SURF, fg=SUB)
        self._aaf_status_lbl.pack(anchor="w", padx=12, pady=(4, 10))

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
        a_open = [False]

        aph = tk.Frame(apool_frame, bg=SURF, cursor="hand2")
        aph.pack(fill="x", padx=12, pady=(10, 4))

        a_arrow = tk.Label(aph, text="\u25b6",
                           font=FB, bg=SURF, fg=ACCENT, cursor="hand2")
        a_arrow.pack(side="left", padx=(0, 6))
        tk.Label(aph, text="REFERENCE AUDIO FILES", font=FL, bg=SURF, fg=ACCENT,
                 cursor="hand2").pack(side="left")
        tk.Label(aph, text="  (camera / scratch audio for dual-system sync \u2014 optional)",
                 font=FB, bg=SURF, fg=SUB).pack(side="left")
        self._aaf_audio_count_lbl = tk.Label(aph, text="0 files", font=FB,
                                             bg=SURF, fg=SUB)
        self._aaf_audio_count_lbl.pack(side="right")
        self._btn(aph, "+ BROWSE FOLDER", self._aaf_browse_audio_folder,
                  small=True).pack(side="right", padx=(0, 4))
        self._btn(aph, "+ BROWSE FILES",  self._aaf_browse_audio_files,
                  small=True).pack(side="right", padx=(0, 4))

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

        # Body starts collapsed
        self._aaf_audio_paths     = []
        self._aaf_audio_file_list = tk.Frame(a_body, bg=SURF)
        self._aaf_audio_file_list.pack(fill="x", padx=12)

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
        ah.pack(fill="x", padx=12, pady=(10, 4))
        # (no redundant label before tabs)

        # ── Initialise page-mode var NOW so _make_tab can read it ─────────────
        if not hasattr(self, "_aaf_page_mode"):
            self._aaf_page_mode = tk.StringVar(value="assign")
        if not hasattr(self, "_aaf_sync_state_vars"):
            self._aaf_sync_state_vars = {}
        if not hasattr(self, "_aaf_sync_dot_labels"):
            self._aaf_sync_dot_labels = {}   # base → dot Label widget
        if not hasattr(self, "_aaf_qa_btns"):
            self._aaf_qa_btns = {}           # base → QA button widget
        if not hasattr(self, "_aaf_qa_playing"):
            self._aaf_qa_playing = None      # base currently being QA-played
        if not hasattr(self, "_aaf_needs_sync_vars"):
            self._aaf_needs_sync_vars = {}   # base → BooleanVar: "show on sync page"
        if not hasattr(self, "_aaf_sync_locked_vars"):
            self._aaf_sync_locked_vars = {}  # base → BooleanVar: sync frozen
        if not hasattr(self, "_aaf_ck_lbl_map"):
            self._aaf_ck_lbl_map = {}        # base → checkbox Label (for drag-select)
        if not hasattr(self, "_aaf_ck_container_map"):
            self._aaf_ck_container_map = {}  # base → row container Frame
        if not hasattr(self, "_aaf_drag_sync"):
            self._aaf_drag_sync = None       # drag state: {"start_idx", "target", "last_idx"}
        if not hasattr(self, "_aaf_confirm_btns"):
            self._aaf_confirm_btns = {}      # base → confirm Label widget
        if not hasattr(self, "_aaf_lock_btns"):
            self._aaf_lock_btns = {}         # base → lock Label widget
        if not hasattr(self, "_aaf_hidden_sources"):
            self._aaf_hidden_sources = set()  # sources hidden by user (reversible)

        # ── Column order (persists across rebuilds; widths computed in _rebuild) ─
        if not hasattr(self, "_aaf_col_order"):
            self._aaf_col_order = ["video", "tracks"]  # Source|Video|Track|Sync|hide

        # ── Page toggle: ASSIGN  /  SYNC ──────────────────────────────────────
        tab_frame = tk.Frame(ah, bg=SURF)
        tab_frame.pack(side="left", padx=(4, 0))
        self._aaf_assign_tab_btn = None
        self._aaf_sync_tab_btn   = None

        def _make_tab(parent, label, mode):
            active  = self._aaf_page_mode.get() == mode
            bg      = ACCENT if active else SURF3
            fg      = BG     if active else SUB
            b = tk.Label(parent, text=label, font=FB, bg=bg, fg=fg,
                         cursor="hand2", padx=10, pady=2, bd=0,
                         highlightbackground=BORDER, highlightthickness=1)
            def _click(e, m=mode):
                self._aaf_page_mode.set(m)
                self._rebuild_aaf_source_rows()
            b.bind("<ButtonRelease-1>", _click)
            return b

        self._aaf_assign_tab_btn = _make_tab(tab_frame, "SOURCE ASSIGN", "assign")
        self._aaf_assign_tab_btn.pack(side="left")
        self._aaf_sync_tab_btn = _make_tab(tab_frame, "SOURCE SYNC", "sync")
        self._aaf_sync_tab_btn.pack(side="left", padx=(2, 0))

        # Undo / Redo — only meaningful on the SYNC page
        if not hasattr(self, "_sync_undo_stack"):
            self._sync_undo_stack = []
            self._sync_redo_stack = []

        # Column headers (rebuilt by _rebuild_aaf_source_rows)
        self._aaf_col_hdr_frame = tk.Frame(assign_frame, bg=SURF)
        self._aaf_col_hdr_frame.pack(fill="x", padx=12, pady=(2, 0))

        self._aaf_assign_frame = tk.Frame(assign_frame, bg=SURF)
        self._aaf_assign_frame.pack(fill="x", padx=12, pady=(0,10))
        # Preserve assignment state across back-navigation; only init on first visit
        if not hasattr(self, "_aaf_source_file_vars"):
            self._aaf_source_file_vars       = {}
        if not hasattr(self, "_aaf_source_sync_vars"):
            self._aaf_source_sync_vars       = {}
        if not hasattr(self, "_aaf_source_syncaudio_vars"):
            self._aaf_source_syncaudio_vars  = {}
        if not hasattr(self, "_aaf_source_audio_disp_vars"):
            self._aaf_source_audio_disp_vars = {}
        if not hasattr(self, "_aaf_source_offset_vars"):
            self._aaf_source_offset_vars     = {}
        if not hasattr(self, "_aaf_source_sync_label_vars"):
            self._aaf_source_sync_label_vars = {}
        self._aaf_sync_btns = {}   # always reset — widget refs are stale after _clear()
        # Close any sync preview dialogs left open from a previous visit
        for _dlg in getattr(self, "_aaf_sync_previews", {}).values():
            try: _dlg.close()
            except Exception: pass
        self._aaf_sync_previews = {}

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

        # ── Sequence settings (preset + editable resolution/fps) ──────────────
        if not hasattr(self, "_aaf_preset_var"):
            self._aaf_preset_var = tk.StringVar(value="Detect from media")
        if not hasattr(self, "_aaf_fps_var"):
            self._aaf_fps_var = tk.StringVar(value="29.97")
        if not hasattr(self, "_aaf_seq_w_var"):
            self._aaf_seq_w_var = tk.StringVar(value="")
        if not hasattr(self, "_aaf_seq_h_var"):
            self._aaf_seq_h_var = tk.StringVar(value="")

        def _on_preset_change(*_):
            name = self._aaf_preset_var.get()
            # Fixed preset?
            if name in _AAF_PRESET_LOOKUP:
                pw, ph, pfps = _AAF_PRESET_LOOKUP[name]
                if pw is not None:
                    self._aaf_seq_w_var.set(str(pw))
                    self._aaf_seq_h_var.set(str(ph))
                    self._aaf_fps_var.set(
                        "{:.3f}".format(pfps).rstrip("0").rstrip("."))
                elif name == "Detect from media":
                    self._aaf_seq_w_var.set("")
                    self._aaf_seq_h_var.set("")
                    self._aaf_schedule_detect_fps()
                # "Custom" — leave fields untouched so user can type freely
                return
            # Detected-from-source entry?
            detected = getattr(self, "_aaf_detected_presets", {})
            if name in detected:
                pw, ph, pfps = detected[name]
                self._aaf_seq_w_var.set(str(pw))
                self._aaf_seq_h_var.set(str(ph))
                self._aaf_fps_var.set(
                    "{:.3f}".format(pfps).rstrip("0").rstrip("."))

        self._aaf_preset_var.trace_add("write", _on_preset_change)

        row_preset = tk.Frame(settings_frame, bg=SURF)
        row_preset.pack(anchor="w", padx=12, pady=(0, 4))
        tk.Label(row_preset, text="Sequence Preset:", font=FB, bg=SURF, fg=TEXT,
                 width=18, anchor="w").pack(side="left")
        self._aaf_preset_dd = _FlatDropdown(row_preset, self._aaf_preset_var,
                                            [p[0] for p in _AAF_SEQ_PRESETS],
                                            font=FB, width=26)
        self._aaf_preset_dd.pack(side="left")

        row_fps = tk.Frame(settings_frame, bg=SURF)
        row_fps.pack(anchor="w", padx=12, pady=(0, 10))
        tk.Label(row_fps, text="Resolution / FPS:", font=FB, bg=SURF, fg=TEXT,
                 width=18, anchor="w").pack(side="left")
        tk.Entry(row_fps, textvariable=self._aaf_seq_w_var,
                 font=FB, bg=SURF2, fg=TEXT, insertbackground=TEXT,
                 relief="flat", width=5).pack(side="left")
        tk.Label(row_fps, text=" × ", font=FB, bg=SURF, fg=SUB).pack(side="left")
        tk.Entry(row_fps, textvariable=self._aaf_seq_h_var,
                 font=FB, bg=SURF2, fg=TEXT, insertbackground=TEXT,
                 relief="flat", width=5).pack(side="left")
        tk.Label(row_fps, text="   FPS:", font=FB, bg=SURF, fg=TEXT).pack(side="left", padx=(10, 4))
        tk.Entry(row_fps, textvariable=self._aaf_fps_var,
                 font=FB, bg=SURF2, fg=TEXT, insertbackground=TEXT,
                 relief="flat", width=8).pack(side="left")
        tk.Label(row_fps, text="  (auto-filled from preset; edit to override)",
                 font=FB, bg=SURF, fg=SUB).pack(side="left")

        # ── Stereo mix (optional full-sequence audio track) ───────────────────
        mix_frame = tk.Frame(sf, bg=SURF,
                             highlightbackground=BORDER, highlightthickness=1)
        mix_frame.pack(fill="x", pady=(0, 8), padx=2)

        mh = tk.Frame(mix_frame, bg=SURF)
        mh.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(mh, text="STEREO MIX", font=FL, bg=SURF, fg=ACCENT).pack(side="left")
        tk.Label(mh, text="  (optional — full-sequence stereo mix that syncs to picture)",
                 font=FB, bg=SURF, fg=SUB).pack(side="left")

        if not hasattr(self, "_aaf_mix_var"):
            self._aaf_mix_var = tk.StringVar(value="")

        mix_body = tk.Frame(mix_frame, bg=SURF)
        mix_body.pack(fill="x", padx=12, pady=(0, 10))

        mix_disp = tk.Label(mix_body, textvariable=self._aaf_mix_var,
                            font=FB, bg=SURF2, fg=TEXT, anchor="w",
                            relief="flat", padx=8, pady=4)
        mix_disp.pack(fill="x", pady=(0, 6))

        mix_btn_row = tk.Frame(mix_body, bg=SURF)
        mix_btn_row.pack(anchor="w")
        self._btn(mix_btn_row, "+ BROWSE MIX FILE", self._aaf_browse_mix,
                  small=True).pack(side="left", padx=(0, 8))
        self._btn(mix_btn_row, "✕ CLEAR", lambda: self._aaf_mix_var.set(""),
                  small=True).pack(side="left")

        if HAS_DND:
            mix_frame.drop_target_register(DND_FILES)
            mix_frame.dnd_bind("<<Drop>>",
                               lambda e: self._aaf_set_mix(engines.parse_dnd(e.data)))

        # ── Camera audio option ───────────────────────────────────────────────
        if not hasattr(self, "_aaf_cam_audio_var"):
            self._aaf_cam_audio_var = tk.BooleanVar(value=False)
        cam_row = tk.Frame(sf, bg=SURF)
        cam_row.pack(fill="x", padx=14, pady=(0, 8))
        tk.Checkbutton(cam_row, text="Include camera audio from video files",
                       variable=self._aaf_cam_audio_var,
                       font=FB, bg=SURF, fg=TEXT, selectcolor=SURF2,
                       activebackground=SURF, activeforeground=TEXT,
                       relief="flat", bd=0).pack(side="left")

        # Restore pool contents: back-navigation takes priority over prefetch
        if _saved_vpaths or _saved_apaths:
            if _saved_vpaths: self._aaf_add_video_batch(_saved_vpaths)
            if _saved_apaths: self._aaf_add_audio_batch(_saved_apaths)
        else:
            prefetch = getattr(self, "_prefetch_aaf_media", [])
            if prefetch:
                vp = [p for p in prefetch if is_video(p)]
                ap = [p for p in prefetch if is_audio(p)]
                if vp: self._aaf_add_video_batch(vp)
                if ap: self._aaf_add_audio_batch(ap)

    # Palette of subtle background tints — one per assigned video file.
    # Unassigned rows use a warm amber tint so they stand out immediately.
    _ASSIGN_PALETTE  = ["#162b16", "#16162b", "#2b1616",
                        "#2b2516", "#162b2b", "#261626"]
    _UNASSIGNED_ROW  = "#2a1a08"

    def _rebuild_aaf_source_rows(self):
        """Rebuild per-source rows; layout depends on current page mode."""
        for w in self._aaf_assign_frame.winfo_children():
            w.destroy()
        self._aaf_sync_btns       = {}   # stale widget refs — repopulated below
        self._aaf_sync_dot_labels = getattr(self, "_aaf_sync_dot_labels", {})
        self._aaf_sync_dot_labels.clear()
        self._aaf_qa_btns = getattr(self, "_aaf_qa_btns", {})
        self._aaf_qa_btns.clear()
        self._aaf_confirm_btns = getattr(self, "_aaf_confirm_btns", {})
        self._aaf_confirm_btns.clear()
        self._aaf_lock_btns = getattr(self, "_aaf_lock_btns", {})
        self._aaf_lock_btns.clear()
        self._aaf_ck_lbl_map = getattr(self, "_aaf_ck_lbl_map", {})
        self._aaf_ck_lbl_map.clear()
        self._aaf_ck_container_map = getattr(self, "_aaf_ck_container_map", {})
        self._aaf_ck_container_map.clear()
        self._aaf_drag_sync = None

        # Ensure vars exist even if called before full page init
        if not hasattr(self, "_aaf_page_mode"):
            self._aaf_page_mode = tk.StringVar(value="assign")
        if not hasattr(self, "_aaf_sync_state_vars"):
            self._aaf_sync_state_vars = {}

        # ── Auto-compute column widths from current content ────────────────────
        # Recomputes whenever _aaf_col_widths is absent OR the video path count has
        # changed (e.g. after loading a setup or browsing files).  Manual sash resizes
        # are preserved as long as the video pool doesn't change.
        _cur_vid_count = len(getattr(self, "_aaf_video_paths", []))
        _cur_aud_count = len(getattr(self, "_aaf_audio_paths", []))
        _cur_sentinel  = (_cur_vid_count, _cur_aud_count)
        if (not hasattr(self, "_aaf_col_widths") or
                getattr(self, "_aaf_col_widths_vid_count", (-1, -1)) != _cur_sentinel):
            # Derive per-character pixel width from the actual screen DPI so column
            # widths are correct at any Windows scaling level (96 → 192+ DPI).
            try:
                _dpi = self.winfo_fpixels('1i')           # pixels per inch
            except Exception:
                _dpi = 96.0
            _ppc = max(9, int(9.0 * _dpi / 96.0 + 0.9))  # px per char, round up

            _sources  = getattr(self, "_aaf_sources", [])
            _name_nat = max((len(b) for b in _sources), default=14) * _ppc + 40
            _trk_strs = [", ".join(sorted(getattr(self, "_aaf_source_tracks", {}).get(b, set())))
                         for b in _sources]
            _trk_nat  = max((len(t) for t in _trk_strs), default=8) * _ppc + 30
            _vid_fns  = [basename(p) for p in getattr(self, "_aaf_video_paths", [])]
            _vid_nat  = max((len(fn) for fn in _vid_fns), default=20) * _ppc + 80
            _aud_fns  = [basename(p) for p in getattr(self, "_aaf_audio_paths", [])]
            _aud_nat  = max((len(fn) for fn in _aud_fns), default=20) * _ppc + 40
            self._aaf_col_widths = {
                "name":   max(140, _name_nat),
                "tracks": min(max(60,  _trk_nat),  300),
                "video":  max(200, _vid_nat),
                "audio":  max(200, _aud_nat),
            }
            self._aaf_col_widths_vid_count = _cur_sentinel

        # ── Apply active column sort ───────────────────────────────────────────
        _sort_col = getattr(self, "_aaf_sort_col", None)
        _sort_dir = getattr(self, "_aaf_sort_dir", "asc")
        if _sort_col and hasattr(self, "_aaf_sources"):
            def _sort_key(b):
                if _sort_col == "name":
                    return b.lower()
                if _sort_col == "video":
                    return self._aaf_source_file_vars.get(
                        b, tk.StringVar(value="")).get().lower()
                if _sort_col == "tracks":
                    return ", ".join(sorted(
                        self._aaf_source_tracks.get(b, set()))).lower()
                return b.lower()
            self._aaf_sources.sort(key=_sort_key, reverse=(_sort_dir == "desc"))

        # ── Update tab button appearance ───────────────────────────────────────
        mode = self._aaf_page_mode.get()
        for btn, m in [(getattr(self, "_aaf_assign_tab_btn", None), "assign"),
                       (getattr(self, "_aaf_sync_tab_btn",   None), "sync")]:
            if btn:
                try:
                    active = (mode == m)
                    btn.config(bg=ACCENT if active else SURF3,
                               fg=BG     if active else SUB)
                except Exception:
                    pass

        # ── Rebuild column headers ─────────────────────────────────────────────
        hdr = getattr(self, "_aaf_col_hdr_frame", None)
        if hdr:
            for w in hdr.winfo_children():
                w.destroy()
            if mode == "assign":
                _col_widths = getattr(self, "_aaf_col_widths",
                                      {"name": 200, "tracks": 110, "video": 200})
                _col_order  = getattr(self, "_aaf_col_order",  ["video", "tracks"])

                # Sort indicator helper
                _sc = getattr(self, "_aaf_sort_col", None)
                _sd = getattr(self, "_aaf_sort_dir", "asc")
                def _sort_lbl(text, sort_key):
                    if _sc == sort_key:
                        return text + (" \u25b2" if _sd == "asc" else " \u25bc")
                    return text

                # SOURCE CLIP (click-to-sort, fixed width from content measurement)
                nf = tk.Frame(hdr, bg=SURF, width=_col_widths["name"], cursor="hand2")
                nf.pack_propagate(False)
                nf.pack(side="left", fill="y")
                _nl = tk.Label(nf, text=_sort_lbl("SOURCE CLIP", "name"),
                               font=FB, bg=SURF, fg=SUB, anchor="w", padx=4,
                               cursor="hand2")
                _nl.pack(fill="both", expand=True)
                for _w in (nf, _nl):
                    _w.bind("<ButtonRelease-1>", lambda e: self._aaf_sort_by("name"))
                self._aaf_col_sash(hdr, "name", SURF)

                # Middle columns (click-to-sort only)
                for _col in _col_order:
                    if _col == "tracks":
                        tf = tk.Frame(hdr, bg=SURF,
                                      width=_col_widths.get("tracks", 80),
                                      cursor="hand2")
                        tf.pack_propagate(False)
                        tf.pack(side="left", fill="y")
                        _th = tk.Label(tf, text=_sort_lbl("TRACK", "tracks"),
                                       font=FB, bg=SURF, fg=SUB,
                                       cursor="hand2", anchor="w", padx=4)
                        _th.pack(fill="both", expand=True)
                        for _w in (tf, _th):
                            _w.bind("<ButtonRelease-1>",
                                    lambda e: self._aaf_sort_by("tracks"))
                        self._aaf_col_sash(hdr, "tracks", SURF)
                    elif _col == "video":
                        vf = tk.Frame(hdr, bg=SURF,
                                      width=_col_widths.get("video", 200),
                                      cursor="hand2")
                        vf.pack_propagate(False)
                        vf.pack(side="left", fill="y")
                        _vh = tk.Label(vf, text=_sort_lbl("VIDEO FILE", "video"),
                                       font=FB, bg=SURF, fg=SUB,
                                       cursor="hand2", anchor="w", padx=4)
                        _vh.pack(fill="both", expand=True)
                        for _w in (vf, _vh):
                            _w.bind("<ButtonRelease-1>",
                                    lambda e: self._aaf_sort_by("video"))
                        self._aaf_col_sash(hdr, "video", SURF)

                # SYNC and HIDE — left-packed immediately after last column
                tk.Label(hdr, text="SYNC", font=FB, bg=SURF, fg=SUB,
                         width=5, anchor="center").pack(side="left", padx=(6, 0))
                tk.Label(hdr, text="HIDE", font=FB, bg=SURF, fg=SUB,
                         width=5, anchor="center").pack(side="left", padx=(2, 0))

            else:
                _cw_s   = getattr(self, "_aaf_col_widths", {"name": 200, "audio": 260})
                _nw_s   = _cw_s.get("name", 200)
                _aw_s   = _cw_s.get("audio", 260)
                _ow_s   = 110   # offset column width — shared with rows

                # Global controls anchor far right (packed right-to-left)
                self._btn(hdr, "SYNC ALL",
                          self._aaf_sync_all, small=True).pack(side="right", padx=(0, 4))
                self._btn(hdr, "RESET ALL",
                          self._aaf_reset_all, small=True).pack(side="right", padx=(0, 4))
                self._btn(hdr, "UNLOCK ALL",
                          self._aaf_unlock_all, small=True).pack(side="right", padx=(0, 4))

                # SOURCE CLIP (left, fixed width)
                _snf = tk.Frame(hdr, bg=SURF, width=_nw_s)
                _snf.pack_propagate(False)
                _snf.pack(side="left", fill="y")
                tk.Label(_snf, text="\u25cf SOURCE CLIP", font=FB, bg=SURF, fg=SUB,
                         anchor="w", padx=4).pack(fill="both", expand=True)
                self._aaf_col_sash(hdr, "name", SURF)

                # REFERENCE AUDIO (left, fixed width)
                af = tk.Frame(hdr, bg=SURF, width=_aw_s)
                af.pack_propagate(False)
                af.pack(side="left", fill="y")
                tk.Label(af, text="REFERENCE AUDIO", font=FB, bg=SURF, fg=SUB,
                         anchor="w", padx=4).pack(fill="both", expand=True)
                self._aaf_col_sash(hdr, "audio", SURF)

                # OFFSET (left, fixed width)
                _sof = tk.Frame(hdr, bg=SURF, width=_ow_s)
                _sof.pack_propagate(False)
                _sof.pack(side="left", fill="y")
                tk.Label(_sof, text="OFFSET", font=FB, bg=SURF, fg=SUB,
                         anchor="w", padx=4).pack(fill="both", expand=True)

                # SYNC (remaining space)
                tk.Label(hdr, text="SYNC", font=FB, bg=SURF, fg=SUB,
                         anchor="w", padx=8).pack(side="left", fill="x", expand=True)

        options      = ["— no video —"] + [basename(p) for p in self._aaf_video_paths]
        aud_options  = ["— no audio —"] + [basename(p) for p in self._aaf_audio_paths]
        aud_by_name  = {basename(p): p for p in self._aaf_audio_paths}

        vid_fns   = [basename(p) for p in self._aaf_video_paths]
        vid_color = {fn: self._ASSIGN_PALETTE[i % len(self._ASSIGN_PALETTE)]
                     for i, fn in enumerate(vid_fns)}

        def _row_bg(sv_val):
            return vid_color.get(sv_val, self._UNASSIGNED_ROW)

        def _apply_color(container, color):
            try:
                if not container.winfo_exists():
                    return
            except Exception:
                return
            container.config(bg=color)
            def _recurse(w):
                if hasattr(w, "set_bg"):
                    w.set_bg(color)
                else:
                    try:    w.config(bg=color)
                    except: pass
                for child in w.winfo_children():
                    _recurse(child)
            _recurse(container)

        # Sync state dot colours
        _STATE_DOT = {"": SUB, "auto": WARN, "manual": SUCCESS}

        for base in self._aaf_sources:
            # ── Persistent vars (survive rebuilds) ────────────────────────────
            if base not in self._aaf_source_file_vars:
                self._aaf_source_file_vars[base]       = tk.StringVar(value="— no video —")
            if base not in self._aaf_source_sync_vars:
                self._aaf_source_sync_vars[base]       = tk.BooleanVar(value=False)
            if base not in self._aaf_source_syncaudio_vars:
                self._aaf_source_syncaudio_vars[base]  = tk.StringVar(value="")
            if base not in self._aaf_source_offset_vars:
                self._aaf_source_offset_vars[base]     = tk.StringVar(value="0.000")
            if base not in self._aaf_source_sync_label_vars:
                self._aaf_source_sync_label_vars[base] = tk.StringVar(value="")
            if base not in self._aaf_sync_state_vars:
                self._aaf_sync_state_vars[base]        = tk.StringVar(value="")
            if base not in self._aaf_needs_sync_vars:
                self._aaf_needs_sync_vars[base]        = tk.BooleanVar(value=False)
            if base not in self._aaf_sync_locked_vars:
                self._aaf_sync_locked_vars[base]       = tk.BooleanVar(value=False)

            sv = self._aaf_source_file_vars[base]
            if sv.get() not in options:
                sv.set("— no video —")

            # ── Audio display var — recreated fresh each rebuild ───────────────
            cur_full  = self._aaf_source_syncaudio_vars[base].get()
            cur_bn    = basename(cur_full) if cur_full else ""
            aud_disp  = tk.StringVar(value=cur_bn if cur_bn else "— no audio —")
            self._aaf_source_audio_disp_vars[base] = aud_disp

            # Skip hidden sources (shown in collapsible section below)
            if base in getattr(self, "_aaf_hidden_sources", set()):
                continue

            # ── Container — accent border when source is marked for sync ──────
            _is_sync = self._aaf_needs_sync_vars[base].get()
            bg = _row_bg(sv.get())
            container = tk.Frame(self._aaf_assign_frame, bg=bg,
                                 highlightbackground=ACCENT if _is_sync else BORDER,
                                 highlightthickness=2 if _is_sync else 1)
            container.pack(fill="x", pady=2)

            row1 = tk.Frame(container, bg=bg)
            row1.pack(fill="x", padx=6, pady=(4, 0))

            if mode == "assign":
                # ── ASSIGN mode: SOURCE | VIDEO | PT TRACK | SYNC | hide ────
                _col_widths = getattr(self, "_aaf_col_widths",
                                      {"name": 200, "tracks": 110, "video": 200})
                _col_order  = getattr(self, "_aaf_col_order",  ["video", "tracks"])

                # Left side: name | video | tracks | SYNC | HIDE
                nf = tk.Frame(row1, bg=bg, width=_col_widths["name"])
                nf.pack_propagate(False)
                nf.pack(side="left", fill="y")
                name_lbl = tk.Label(nf, text=base, font=FB, bg=bg, fg=TEXT,
                                    anchor="w")
                name_lbl.pack(fill="x")
                self._tooltip(name_lbl, base)

                # Middle columns in stored order
                tracks_str = ", ".join(sorted(self._aaf_source_tracks.get(base, set())))
                for _col in _col_order:
                    if _col == "tracks":
                        tf = tk.Frame(row1, bg=bg,
                                      width=_col_widths.get("tracks", 110))
                        tf.pack_propagate(False)
                        tf.pack(side="left", fill="y", padx=(1, 0))
                        tk.Label(tf, text=tracks_str, font=FB, bg=bg, fg=SUB,
                                 anchor="w").pack(fill="x")
                    elif _col == "video":
                        vrf = tk.Frame(row1, bg=bg,
                                       width=_col_widths.get("video", 200))
                        vrf.pack_propagate(False)
                        vrf.pack(side="left", fill="y", padx=(1, 0))
                        _FlatDropdown(vrf, textvariable=sv, values=options,
                                      state="readonly", font=FB).pack(fill="x")

                # SYNC checkbox + HIDE — left-packed immediately after TRACK
                _ck_var = self._aaf_needs_sync_vars[base]
                _ck_lbl = tk.Label(row1,
                                   text="\u2611" if _ck_var.get() else "\u2610",
                                   font=(_SANS, 15), bg=bg,
                                   fg=ACCENT if _ck_var.get() else SUB,
                                   cursor="hand2", padx=4, width=5, anchor="center")
                _ck_lbl.pack(side="left", padx=(6, 0))
                # Register in maps for drag-select hit-testing
                self._aaf_ck_lbl_map[base]       = _ck_lbl
                self._aaf_ck_container_map[base] = container

                def _ck_press(e, b=base):
                    idx    = self._aaf_sources.index(b)
                    target = not self._aaf_needs_sync_vars[b].get()
                    self._aaf_drag_sync = {"start_idx": idx, "target": target,
                                           "last_idx": idx}
                    self._aaf_apply_sync_range(idx, idx, target)

                def _ck_move(e, b=base):
                    if not self._aaf_drag_sync:
                        return
                    cur = self._aaf_ck_hit_test(e.y_root)
                    if cur is None:
                        return
                    try:
                        cur_idx = self._aaf_sources.index(cur)
                    except ValueError:
                        return
                    if cur_idx == self._aaf_drag_sync.get("last_idx"):
                        return
                    self._aaf_drag_sync["last_idx"] = cur_idx
                    lo = min(self._aaf_drag_sync["start_idx"], cur_idx)
                    hi = max(self._aaf_drag_sync["start_idx"], cur_idx)
                    self._aaf_apply_sync_range(lo, hi,
                                               self._aaf_drag_sync["target"])

                def _ck_release(e):
                    self._aaf_drag_sync = None

                _ck_lbl.bind("<ButtonPress-1>",   _ck_press)
                _ck_lbl.bind("<B1-Motion>",        _ck_move)
                _ck_lbl.bind("<ButtonRelease-1>",  _ck_release)

                hide_lbl = tk.Label(row1, text="HIDE", font=FB, bg=bg, fg=SUB,
                                    cursor="hand2", padx=4, width=5, anchor="center")
                hide_lbl.pack(side="left", padx=(2, 0))
                hide_lbl.bind("<Enter>",  lambda e, w=hide_lbl: w.config(fg=WARN))
                hide_lbl.bind("<Leave>",  lambda e, w=hide_lbl: w.config(fg=SUB))
                hide_lbl.bind("<Button-1>",
                              lambda e, b=base: self._aaf_hide_source(b))

                # Remove stale video-change traces then re-add
                for _tr in list(sv.trace_info()):
                    if _tr[0] == "write":
                        try: sv.trace_remove("write", _tr[1])
                        except Exception: pass
                def _on_vid_change(*args, c=container, s=sv, b=base):
                    _apply_color(c, _row_bg(s.get()))
                    # If this source had a locked/completed sync, invalidate it —
                    # the assignment changed so the old sync result is stale.
                    lkv = self._aaf_sync_locked_vars.get(b)
                    was_locked = lkv and lkv.get()
                    ssv = self._aaf_sync_state_vars.get(b, tk.StringVar()).get()
                    if was_locked or ssv:
                        if lkv: lkv.set(False)
                        ov = self._aaf_source_offset_vars.get(b)
                        if ov: ov.set("0.000")
                        sv2 = self._aaf_source_sync_vars.get(b)
                        if sv2: sv2.set(False)
                        lv = self._aaf_source_sync_label_vars.get(b)
                        if lv: lv.set("")
                        self._aaf_set_sync_state(b, "")
                        # Refresh sync-tab row so lock icon updates immediately
                        self.after(0, self._rebuild_aaf_source_rows)
                sv.trace_add("write", _on_vid_change)

                # Pad bottom of assign-mode row
                tk.Frame(container, bg=bg, height=4).pack()

            else:
                # ── SYNC mode: only show sources marked "needs sync" ───────────
                if not self._aaf_needs_sync_vars[base].get():
                    container.destroy()
                    continue

                locked    = self._aaf_sync_locked_vars[base].get()
                state_val = self._aaf_sync_state_vars[base].get()
                _cw_s     = getattr(self, "_aaf_col_widths", {"name": 200, "audio": 260})
                _nw_s     = _cw_s.get("name", 200)
                _aw_s     = _cw_s.get("audio", 260)
                _ow_s     = 110   # offset column — matches sync header

                # ── SOURCE CLIP (left, fixed width) ───────────────────────────
                dot_color  = _STATE_DOT.get(state_val, SUB)
                clip_frame = tk.Frame(row1, bg=bg, width=_nw_s)
                clip_frame.pack_propagate(False)
                clip_frame.pack(side="left", fill="y")
                dot_lbl = tk.Label(clip_frame, text="\u25cf", font=FB, bg=bg,
                                   fg=dot_color, padx=2)
                dot_lbl.pack(side="left")
                self._aaf_sync_dot_labels[base] = dot_lbl
                name_lbl = tk.Label(clip_frame, text=base, font=FB, bg=bg,
                                    fg=TEXT, anchor="w")
                name_lbl.pack(side="left", fill="x", expand=True)
                self._tooltip(name_lbl, base)

                # ── REFERENCE AUDIO (left, fixed width) ───────────────────────
                aud_frame    = tk.Frame(row1, bg=bg, width=_aw_s)
                aud_frame.pack_propagate(False)
                aud_frame.pack(side="left", fill="y")
                aud_dd_state = "disabled" if locked else "readonly"
                aud_dd = _FlatDropdown(aud_frame, textvariable=aud_disp,
                                       values=aud_options,
                                       state=aud_dd_state, font=FB)
                aud_dd.pack(fill="x", expand=True)

                # ── OFFSET (left, fixed width) ─────────────────────────────────
                off_frame = tk.Frame(row1, bg=bg, width=_ow_s)
                off_frame.pack_propagate(False)
                off_frame.pack(side="left", fill="y")
                tk.Label(off_frame,
                         textvariable=self._aaf_source_offset_vars[base],
                         font=FB, bg=bg, fg=ACCENT,
                         anchor="e").pack(side="left", fill="x", expand=True)
                tk.Label(off_frame, text="s", font=FB, bg=bg,
                         fg=SUB).pack(side="left", padx=(2, 4))

                # ── SYNC controls (left, remaining space) ─────────────────────
                # 🔒 Lock toggle
                lock_text = "\U0001f512" if locked else "\U0001f513"
                lock_fg   = WARN if locked else SUB
                lock_btn  = tk.Label(row1, text=lock_text, font=FB, bg=SURF3,
                                     fg=lock_fg, cursor="hand2", padx=6, pady=2,
                                     bd=0, highlightbackground=BORDER,
                                     highlightthickness=1)
                lock_btn.pack(side="left", padx=(8, 2))
                lock_btn.bind("<Enter>",  lambda e, w=lock_btn: w.config(bg=ACCENT))
                lock_btn.bind("<Leave>",  lambda e, w=lock_btn: w.config(bg=SURF3))
                lock_btn.bind("<ButtonRelease-1>",
                              lambda e, b=base: self._aaf_toggle_lock(b))
                self._aaf_lock_btns[base] = lock_btn

                # ✓ ACCEPT
                cfm_text, cfm_fg = self._confirm_btn_style(state_val)
                cfm_btn = tk.Label(row1, text=cfm_text, font=FB, bg=SURF3,
                                   fg=cfm_fg, cursor="hand2", padx=6, pady=2,
                                   bd=0, highlightbackground=BORDER,
                                   highlightthickness=1)
                cfm_btn.pack(side="left", padx=(2, 0))
                cfm_btn.bind("<Enter>",  lambda e, w=cfm_btn: w.config(bg=ACCENT))
                cfm_btn.bind("<Leave>",  lambda e, w=cfm_btn: w.config(bg=SURF3))
                cfm_btn.bind("<ButtonRelease-1>",
                             lambda e, b=base: self._aaf_confirm_sync(b))
                if locked:
                    cfm_btn.config(state="disabled", fg=SUB)
                self._aaf_confirm_btns[base] = cfm_btn

                # ▶ PREVIEW
                prev_btn = tk.Label(row1, text="\u25b6 PREVIEW", font=FB, bg=SURF3,
                                    fg=TEXT, cursor="hand2", padx=6, pady=2, bd=0,
                                    highlightbackground=BORDER, highlightthickness=1)
                prev_btn.pack(side="left", padx=(2, 0))
                prev_btn.bind("<Enter>",  lambda e, w=prev_btn: w.config(bg=ACCENT))
                prev_btn.bind("<Leave>",  lambda e, w=prev_btn: w.config(bg=SURF3))
                prev_btn.bind("<ButtonRelease-1>",
                              lambda e, b=base: self._aaf_qa_toggle(b))
                self._aaf_qa_btns[base] = prev_btn

                # ALIGN…
                align_btn = tk.Label(row1, text="ALIGN\u2026", font=FB, bg=SURF3,
                                     fg=SUB if locked else TEXT,
                                     cursor="" if locked else "hand2",
                                     padx=6, pady=2, bd=0,
                                     highlightbackground=BORDER, highlightthickness=1)
                align_btn.pack(side="left", padx=(2, 0))
                if not locked:
                    align_btn.bind("<Enter>",  lambda e, w=align_btn: w.config(bg=ACCENT))
                    align_btn.bind("<Leave>",  lambda e, w=align_btn: w.config(bg=SURF3))
                    align_btn.bind("<ButtonRelease-1>",
                                   lambda e, b=base: self._aaf_open_align(b))

                # ↺ Reset
                rst_btn = tk.Label(row1, text="\u21ba", font=FB, bg=SURF3,
                                   fg=SUB, cursor="hand2", padx=6, pady=2, bd=0,
                                   highlightbackground=BORDER, highlightthickness=1)
                rst_btn.pack(side="left", padx=(2, 0))
                if locked:
                    rst_btn.config(state="disabled", cursor="")
                else:
                    rst_btn.bind("<Enter>",  lambda e, w=rst_btn: w.config(bg="#5a2020"))
                    rst_btn.bind("<Leave>",  lambda e, w=rst_btn: w.config(bg=SURF3))
                    rst_btn.bind("<ButtonRelease-1>",
                                 lambda e, b=base: self._aaf_reset_sync(b))

                # SYNC button
                sync_btn = self._btn(row1, "SYNC",
                                     lambda b=base: self._aaf_do_sync(b),
                                     small=True)
                sync_btn.pack(side="left", padx=(2, 0))
                if locked:
                    sync_btn.config(state="disabled", fg=SUB)
                self._aaf_sync_btns[base] = sync_btn
                self._aaf_refresh_sync_btn(base, sync_btn)

                # Audio dropdown trace (clear stale sync on ref change)
                def _on_aud_change(*args, b=base, dv=aud_disp):
                    fn   = dv.get()
                    full = {basename(p): p for p in self._aaf_audio_paths}.get(fn, "")
                    self._aaf_source_syncaudio_vars[b].set(full)
                    lv = self._aaf_source_sync_label_vars.get(b)
                    if lv and lv.get():
                        lv.set("")
                        btn_ = self._aaf_sync_btns.get(b)
                        if btn_:
                            self._aaf_refresh_sync_btn(b, btn_)
                aud_disp.trace_add("write", _on_aud_change)

        # Show placeholder when SYNC page has nothing to display
        if mode == "sync":
            visible = [b for b in self._aaf_sources
                       if self._aaf_needs_sync_vars.get(b, tk.BooleanVar()).get()]
            if not visible:
                tk.Label(self._aaf_assign_frame,
                         text="No sources marked for sync.\n"
                              "Go to SOURCE ASSIGN and tick the sync checkbox on each source.",
                         font=FB, bg=SURF, fg=SUB, justify="center",
                         pady=20).pack(fill="x")

        # ── Hidden sources collapsible section (assign mode only) ─────────────
        if mode == "assign":
            _hidden_set = getattr(self, "_aaf_hidden_sources", set())
            _hidden_list = [b for b in self._aaf_sources if b in _hidden_set]
            if _hidden_list:
                if not hasattr(self, "_aaf_hidden_expanded"):
                    self._aaf_hidden_expanded = False
                _exp = self._aaf_hidden_expanded
                _count = len(_hidden_list)

                h_hdr = tk.Frame(self._aaf_assign_frame, bg=SURF3, cursor="hand2")
                h_hdr.pack(fill="x", pady=(10, 0))
                tk.Label(h_hdr,
                         text=("  \u25be " if _exp else "  \u25b8 ") +
                              "HIDDEN  ({})".format(_count),
                         font=FB, bg=SURF3, fg=SUB,
                         anchor="w", pady=4).pack(side="left")
                tk.Label(h_hdr, text="click to expand / collapse",
                         font=FB, bg=SURF3, fg=BORDER,
                         anchor="e", padx=8).pack(side="right")

                h_body = tk.Frame(self._aaf_assign_frame, bg=SURF3)
                if _exp:
                    h_body.pack(fill="x")
                    for _hb in _hidden_list:
                        _hr = tk.Frame(h_body, bg=SURF3)
                        _hr.pack(fill="x", padx=12, pady=1)
                        tk.Label(_hr, text=_hb, font=FB, bg=SURF3, fg=SUB,
                                 anchor="w").pack(side="left", fill="x", expand=True)
                        _show = tk.Label(_hr, text="SHOW", font=FB, bg=SURF3,
                                         fg=ACCENT, cursor="hand2", padx=8)
                        _show.pack(side="right")
                        _show.bind("<Button-1>",
                                   lambda e, b=_hb: self._aaf_unhide_source(b))

                def _toggle_hidden(e, body=h_body, hdr_lbl=h_hdr.winfo_children()[0],
                                   lst=_hidden_list):
                    self._aaf_hidden_expanded = not getattr(self, "_aaf_hidden_expanded", False)
                    ex = self._aaf_hidden_expanded
                    hdr_lbl.config(text=("  \u25be " if ex else "  \u25b8 ") +
                                        "HIDDEN  ({})".format(len(lst)))
                    if ex:
                        body.pack(fill="x")
                        for _hb in lst:
                            _hr = tk.Frame(body, bg=SURF3)
                            _hr.pack(fill="x", padx=12, pady=1)
                            tk.Label(_hr, text=_hb, font=FB, bg=SURF3, fg=SUB,
                                     anchor="w").pack(side="left", fill="x", expand=True)
                            _s = tk.Label(_hr, text="SHOW", font=FB, bg=SURF3,
                                          fg=ACCENT, cursor="hand2", padx=8)
                            _s.pack(side="right")
                            _s.bind("<Button-1>",
                                    lambda ev, b=_hb: self._aaf_unhide_source(b))
                    else:
                        for _w in list(body.winfo_children()):
                            _w.destroy()
                        body.pack_forget()
                h_hdr.bind("<Button-1>", _toggle_hidden)
                for _ch in h_hdr.winfo_children():
                    _ch.bind("<Button-1>", _toggle_hidden)

        self._aaf_auto_match()

        # If opened via the generic Open Session from the home screen, restore
        # any saved setup state that was stashed before navigating here.
        pending = getattr(self, "_pending_aaf_setup", None)
        if pending is not None:
            self._pending_aaf_setup = None
            self.after(0, lambda d=pending: self._aaf_restore_setup(d))

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
                        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
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
                    # Only auto-fill FPS when a fixed preset hasn't been chosen
                    preset = getattr(self, "_aaf_preset_var", None)
                    preset_val = preset.get() if preset else "Detect from media"
                    if preset_val in ("Detect from media", "Custom"):
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

    def _aaf_update_seq_presets(self):
        """Probe loaded video files and add detected (W×H @ fps) options to the
        sequence preset dropdown below a divider, so the user can pick directly
        from their source media without having to look it up."""
        dd = getattr(self, "_aaf_preset_dd", None)
        if dd is None:
            return
        vpaths = list(getattr(self, "_aaf_video_paths", []))

        def _probe():
            seen     = set()
            detected = []
            for vp in vpaths:
                if not os.path.isfile(vp):
                    continue
                try:
                    w, h, fps, _ = engines.probe_media_settings([vp])
                    if w > 0 and h > 0 and fps > 0:
                        key = (w, h, round(fps, 3))
                        if key not in seen:
                            seen.add(key)
                            fps_s = "{:.3f}".format(fps).rstrip("0").rstrip(".")
                            label = "{}×{}  @  {}fps".format(w, h, fps_s)
                            detected.append((w, h, fps, label))
                except Exception:
                    pass
            return detected

        def _apply(detected):
            base_names = [p[0] for p in _AAF_SEQ_PRESETS]
            new_values = list(base_names)
            if detected:
                new_values.append(
                    _FlatDropdown.separator("──── Detected from source ────"))
                for _, _, _, label in detected:
                    new_values.append(label)
            dd.configure(values=new_values)
            self._aaf_detected_presets = {
                label: (w, h, fps) for w, h, fps, label in detected}

        try:
            from concurrent.futures import ThreadPoolExecutor
            ex = ThreadPoolExecutor(max_workers=1)
            fut = ex.submit(_probe)
            fut.add_done_callback(
                lambda f: self.after(0,
                    lambda: _apply([] if f.exception() else f.result())))
            ex.shutdown(wait=False)
        except Exception:
            pass

    def _aaf_auto_match(self):
        """Auto-assign video AND reference audio files to unassigned sources."""
        vid_options = ["— no video —"] + [basename(p) for p in self._aaf_video_paths]
        aud_by_name = {basename(p): p for p in self._aaf_audio_paths}

        for base in self._aaf_sources:
            # Video auto-match (never override a manual assignment)
            sv = self._aaf_source_file_vars.get(base)
            if sv and sv.get() == "— no video —" and self._aaf_video_paths:
                vp = match_source_to_video(base, self._aaf_video_paths)
                if vp:
                    fn = basename(vp)
                    if fn in vid_options:
                        sv.set(fn)

            # Audio auto-match (never override a manual assignment)
            aud_disp = self._aaf_source_audio_disp_vars.get(base)
            full_var = self._aaf_source_syncaudio_vars.get(base)
            if (aud_disp and full_var
                    and aud_disp.get() == "— no audio —"
                    and self._aaf_audio_paths):
                ap = match_source_to_video(base, self._aaf_audio_paths)
                if ap:
                    fn = basename(ap)
                    if fn in aud_by_name:
                        # Set display var; the trace will update the full-path var
                        aud_disp.set(fn)

    # ── Sync checkbox drag-select helpers ────────────────────────────────────

    def _aaf_ck_hit_test(self, y_root):
        """Return the base whose checkbox row's screen rect contains y_root.
        Falls back to the nearest row if none contains it exactly (e.g. gap
        between rows).  Returns None if no checkboxes are registered."""
        best_base = None
        best_dist = float("inf")
        for base, lbl in self._aaf_ck_lbl_map.items():
            try:
                y_top = lbl.winfo_rooty()
                y_bot = y_top + lbl.winfo_height()
                if y_top <= y_root <= y_bot:
                    return base          # exact hit
                dist = min(abs(y_root - y_top), abs(y_root - y_bot))
                if dist < best_dist:
                    best_dist = dist
                    best_base = base
            except Exception:
                pass
        return best_base

    def _aaf_apply_sync_range(self, lo_idx, hi_idx, target):
        """Set needs-sync to *target* for every source in index range [lo, hi].
        Updates the BoolVar, checkbox label, and container highlight in-place
        without triggering a full row rebuild."""
        for i in range(lo_idx, hi_idx + 1):
            if i < 0 or i >= len(self._aaf_sources):
                continue
            base = self._aaf_sources[i]
            v   = self._aaf_needs_sync_vars.get(base)
            lbl = self._aaf_ck_lbl_map.get(base)
            con = self._aaf_ck_container_map.get(base)
            if v:
                v.set(target)
            if lbl:
                try:
                    lbl.config(text="\u2611" if target else "\u2610",
                               fg=ACCENT if target else SUB)
                except Exception:
                    pass
            if con:
                try:
                    con.config(
                        highlightbackground=ACCENT if target else BORDER,
                        highlightthickness=2 if target else 1)
                except Exception:
                    pass

    def _aaf_unlock_all(self):
        """Unlock every locked source on the sync page."""
        sources = [b for b in self._aaf_sources
                   if self._aaf_needs_sync_vars.get(b, tk.BooleanVar()).get()
                   and self._aaf_sync_locked_vars.get(b, tk.BooleanVar()).get()]
        if not sources:
            return
        for base in sources:
            lkv = self._aaf_sync_locked_vars.get(base)
            if lkv:
                lkv.set(False)
        self._rebuild_aaf_source_rows()

    def _aaf_reset_all(self):
        """Reset sync state for every unlocked source on the sync page."""
        sources = [b for b in self._aaf_sources
                   if self._aaf_needs_sync_vars.get(b, tk.BooleanVar()).get()
                   and not self._aaf_sync_locked_vars.get(b, tk.BooleanVar()).get()]
        if not sources:
            return
        if not messagebox.askyesno("Reset All",
                "Reset sync for {} unlocked source{}?\n"
                "This cannot be undone.".format(
                    len(sources), "s" if len(sources) != 1 else "")):
            return
        for base in sources:
            ov = self._aaf_source_offset_vars.get(base)
            if ov: ov.set("0.000")
            sv = self._aaf_source_sync_vars.get(base)
            if sv: sv.set(False)
            lv = self._aaf_source_sync_label_vars.get(base)
            if lv: lv.set("")
            self._aaf_set_sync_state(base, "")
        self._rebuild_aaf_source_rows()

    def _aaf_sync_all(self):
        """Run sync detection for every source marked 'needs sync' that has video + audio."""
        sources_to_sync = [
            base for base in self._aaf_sources
            if (self._aaf_needs_sync_vars.get(base, tk.BooleanVar()).get()
                and self._aaf_source_file_vars.get(base, tk.StringVar()).get() != "— no video —"
                and self._aaf_source_audio_disp_vars.get(base, tk.StringVar()).get() != "— no audio —")
        ]
        if not sources_to_sync:
            messagebox.showwarning("Nothing to sync",
                "Tick the 'sync' checkbox on at least one source that has both a video "
                "and a reference audio file assigned.")
            return
        for base in sources_to_sync:
            self._aaf_do_sync(base)

    def _aaf_hide_source(self, base):
        """Hide a source from the assignment list (reversible — source preserved in AAF data)."""
        self._aaf_push_full_undo()
        if not hasattr(self, "_aaf_hidden_sources"):
            self._aaf_hidden_sources = set()
        self._aaf_hidden_sources.add(base)
        self._rebuild_aaf_source_rows()

    def _aaf_unhide_source(self, base):
        """Make a previously hidden source visible again."""
        self._aaf_push_full_undo()
        if hasattr(self, "_aaf_hidden_sources"):
            self._aaf_hidden_sources.discard(base)
        self._rebuild_aaf_source_rows()

    def _aaf_remove_source(self, base, container):
        """Legacy — now delegates to hide (keeps source in AAF data)."""
        self._aaf_hide_source(base)

    def _aaf_remove_unassigned_sources(self):
        """Remove all sources that have no video file assigned (legacy helper)."""
        self._aaf_remove_unassigned()

    def _aaf_remove_unassigned(self):
        """
        Clean up three categories of unused items:
          1. Source clips with no video assigned
          2. Video files in the pool not assigned to any source
          3. Audio files in the pool not used as sync audio for any source
        Confirms once before doing anything.
        """
        # 1 — unassigned sources
        dead_sources = [b for b in self._aaf_sources
                        if self._aaf_source_file_vars.get(
                            b, tk.StringVar(value="— no video —")).get() == "— no video —"]

        # 2 — video pool files not used by any source
        used_vid_fns = {sv.get() for sv in self._aaf_source_file_vars.values()
                        if sv.get() not in ("— no video —", "")}
        dead_vids = [p for p in self._aaf_video_paths
                     if basename(p) not in used_vid_fns]

        # 3 — audio pool files not used as sync reference by any source
        used_aud_paths = {sv.get() for sv in self._aaf_source_syncaudio_vars.values()
                          if sv.get()}
        dead_auds = [p for p in self._aaf_audio_paths
                     if p not in used_aud_paths]

        if not dead_sources and not dead_vids and not dead_auds:
            messagebox.showinfo("Clean Up", "Nothing to remove — everything is assigned.")
            return

        lines = []
        if dead_sources:
            lines.append("{} unassigned source{}:".format(
                len(dead_sources), "s" if len(dead_sources) != 1 else ""))
            lines += ["  " + b for b in dead_sources[:5]]
            if len(dead_sources) > 5:
                lines.append("  … and {} more".format(len(dead_sources) - 5))
        if dead_vids:
            lines.append("{} unused video file{}".format(
                len(dead_vids), "s" if len(dead_vids) != 1 else ""))
        if dead_auds:
            lines.append("{} unused audio file{}".format(
                len(dead_auds), "s" if len(dead_auds) != 1 else ""))

        if not messagebox.askyesno("Clean Up", "\n".join(lines) + "\n\nHide unassigned sources / remove unused pool files?"):
            return

        self._aaf_push_full_undo()
        if not hasattr(self, "_aaf_hidden_sources"):
            self._aaf_hidden_sources = set()
        self._aaf_hidden_sources.update(dead_sources)

        for p in dead_vids:
            if p in self._aaf_video_paths:
                self._aaf_video_paths.remove(p)
        nv = len(self._aaf_video_paths)
        if hasattr(self, "_aaf_count_lbl"):
            self._aaf_count_lbl.config(
                text="{} file{}".format(nv, "s" if nv != 1 else ""))

        for p in dead_auds:
            if p in self._aaf_audio_paths:
                self._aaf_audio_paths.remove(p)
        na = len(self._aaf_audio_paths)
        if hasattr(self, "_aaf_audio_count_lbl"):
            self._aaf_audio_count_lbl.config(
                text="{} file{}".format(na, "s" if na != 1 else ""))

        self._rebuild_aaf_source_rows()

    def _aaf_col_sash(self, parent, col_key, bg):
        """
        Create a thin drag-handle between column headers.
        Dragging it adjusts _aaf_col_widths[col_key] and triggers a row rebuild.
        """
        sash = tk.Frame(parent, bg=SURF3, width=5, cursor="sb_h_double_arrow")
        sash.pack(side="left", fill="y", padx=0)
        drag = {}
        def _press(e, k=col_key):
            drag["x0"] = e.x_root
            drag["w0"] = getattr(self, "_aaf_col_widths", {}).get(k, 150)
        def _release(e, k=col_key):
            delta = e.x_root - drag.get("x0", e.x_root)
            old   = drag.get("w0", 150)
            new_w = max(60, old + delta)
            if not hasattr(self, "_aaf_col_widths"):
                self._aaf_col_widths = {}
            self._aaf_col_widths[k] = new_w
            self._rebuild_aaf_source_rows()
        sash.bind("<ButtonPress-1>",   _press)
        sash.bind("<ButtonRelease-1>", _release)
        # Visual feedback on hover
        sash.bind("<Enter>",  lambda e, w=sash: w.config(bg=ACCENT))
        sash.bind("<Leave>",  lambda e, w=sash: w.config(bg=SURF3))

    def _aaf_col_reorder(self, clicked_idx, col_name):
        """
        Reorder ASSIGN columns by swapping the clicked column with its neighbour.
        Each click on a column header rotates it one position to the right
        (wraps around).
        """
        order = getattr(self, "_aaf_col_order", ["video", "tracks"])
        if col_name not in order:
            return
        idx = order.index(col_name)
        # Swap with next (wraps to 0)
        next_idx = (idx + 1) % len(order)
        order[idx], order[next_idx] = order[next_idx], order[idx]
        self._aaf_col_order = order
        self._rebuild_aaf_source_rows()

    def _aaf_sort_by(self, col):
        """Sort the source list by col; second click on same col reverses direction."""
        if getattr(self, "_aaf_sort_col", None) == col:
            self._aaf_sort_dir = "desc" if getattr(self, "_aaf_sort_dir", "asc") == "asc" else "asc"
        else:
            self._aaf_sort_col = col
            self._aaf_sort_dir = "asc"
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
            # "verify ref audio!" means a large (suspicious) offset was detected
            clr = "#e05050" if ("no match" in text or "verify ref" in text) else "#e0a030"
            btn.config(text=text, fg=clr)
        else:
            btn.config(text=text, fg=SUB)

    def _aaf_do_sync(self, base):
        """Run sync detection for this source using the selected reference audio."""
        if self._aaf_sync_locked_vars.get(base, tk.BooleanVar()).get():
            return
        self._aaf_push_undo(base)
        btn = self._aaf_sync_btns.get(base)

        # The reference audio is whatever the user has selected in the dropdown.
        # If nothing is selected yet, try one last auto-match.
        audio_var = self._aaf_source_syncaudio_vars.get(base)
        aud_disp  = self._aaf_source_audio_disp_vars.get(base)
        if audio_var and not audio_var.get() and self._aaf_audio_paths:
            ap = match_source_to_video(base, self._aaf_audio_paths)
            if ap:
                audio_var.set(ap)
                if aud_disp:
                    aud_disp.set(basename(ap))

        if not audio_var or not audio_var.get():
            messagebox.showwarning("No Reference Audio",
                "Select a reference audio file for this source using the dropdown,\n"
                "or add audio files to the reference audio pool.")
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

        # Always probe from the start of both files.  Each source is its own
        # video + audio pair, so there's no reason to seek — the beginning of
        # both recordings is where the overlap lives.
        start_offset = 0.0

        # Disable button while running
        if btn:
            btn.config(text="SYNCING\u2026", state="disabled", fg=SUB)

        _ap_basename = basename(ap)

        def _run():
            return detect_sync_offset(vp, ap, probe_duration=300.0,
                                      start_offset=start_offset)

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
                # Very large offsets (>30 s) are almost always wrong-reference-file
                # matches — flag them distinctly regardless of confidence.
                # (10–20 s offsets are normal for dual-system shoots.)
                large = abs(offset) > 30.0
                if large:
                    lbl = "\u26a0 {:.3f}s ({:d}%)  — verify ref audio!".format(offset, pct)
                elif confidence >= 0.50:
                    lbl = "\u2713 {:.3f}s ({:d}%)".format(offset, pct)
                elif confidence >= 0.20:
                    lbl = "\u26a0 {:.3f}s ({:d}%)".format(offset, pct)
                else:
                    lbl = "\u26a0 {:.3f}s ({:d}%)  — low conf".format(offset, pct)

                lv = self._aaf_source_sync_label_vars.get(base)
                if lv: lv.set(lbl)
                if btn:
                    btn.config(state="normal")
                    self._aaf_refresh_sync_btn(base, btn)

                # Mark as auto-synced (amber dot) until manually confirmed
                self._aaf_set_sync_state(base, "auto")

            self.after(0, _apply)

        from concurrent.futures import ThreadPoolExecutor
        ex  = ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(_run)
        fut.add_done_callback(_done)
        ex.shutdown(wait=False)

    # ── Sync Preview ────────────────────────────────────────────────────────

    def _aaf_open_align(self, base):
        """Open the waveform alignment dialog for manual verification/adjustment."""
        if self._aaf_sync_locked_vars.get(base, tk.BooleanVar()).get():
            return
        fn = self._aaf_source_file_vars.get(base, tk.StringVar()).get()
        if fn == "— no video —":
            messagebox.showwarning("No Video",
                "Assign a video file to this source before opening alignment.")
            return
        path_by_fn = {basename(p): p for p in self._aaf_video_paths}
        vp = path_by_fn.get(fn)
        if not vp:
            return
        ap = self._aaf_source_syncaudio_vars.get(base, tk.StringVar()).get()
        if not ap or not os.path.isfile(ap):
            messagebox.showwarning("No Reference Audio",
                "Select a reference audio file for this source before aligning.")
            return
        try:
            offset = float(self._aaf_source_offset_vars[base].get())
        except (KeyError, ValueError):
            offset = 0.0
        self._aaf_open_sync_preview(base, vp, ap, offset)

    def _aaf_open_sync_preview(self, base, video_path, audio_path, offset):
        """Open the waveform sync-preview dialog for manual verification."""
        def _on_accept(accepted_offset):
            # ── Log correction for feedback analysis ──────────────────────
            try:
                import json as _json, datetime as _dt, os as _os
                _cache_dir = _os.path.join(
                    _os.path.dirname(_os.path.abspath(__file__)), ".pb_cache")
                _os.makedirs(_cache_dir, exist_ok=True)
                _correction_ms = round(abs(accepted_offset - offset) * 1000, 1)
                _verdict = ("exact"  if _correction_ms < 50  else
                            "close"  if _correction_ms < 500 else
                            "wrong")
                _entry = {
                    "ts":            _dt.datetime.now().isoformat(timespec="seconds"),
                    "source":        base,
                    "video":         os.path.basename(video_path),
                    "audio":         os.path.basename(audio_path),
                    "auto_T":        round(offset, 4),
                    "accepted_T":    round(accepted_offset, 4),
                    "correction_ms": _correction_ms,
                    "verdict":       _verdict,
                }
                with open(_os.path.join(_cache_dir, "sync_corrections.jsonl"),
                          "a", encoding="utf-8") as _fh:
                    _fh.write(_json.dumps(_entry) + "\n")
            except Exception:
                pass

            # ── Background signal verification ────────────────────────────
            # Run a Stage-3 xcorr at the accepted offset to confirm waveform
            # agreement.  Appends a "verified" entry to sync_corrections.jsonl
            # so future algorithm runs have signal-backed ground truth.
            def _verify_bg(vp=video_path, ap=audio_path,
                           acc=accepted_offset, src=base):
                try:
                    import json as _jv, datetime as _dv, os as _ov
                    _v_T, _v_conf = verify_sync_at_offset(vp, ap, acc)
                    _cache = _ov.path.join(
                        _ov.path.dirname(_ov.path.abspath(__file__)), ".pb_cache")
                    _ve = {
                        "ts":            _dv.datetime.now().isoformat(timespec="seconds"),
                        "source":        src,
                        "video":         _ov.path.basename(vp),
                        "audio":         _ov.path.basename(ap),
                        "accepted_T":    round(acc, 4),
                        "verified_T":    _v_T,
                        "verified_conf": _v_conf,
                        "verdict":       "verified",
                    }
                    with open(_ov.path.join(_cache, "sync_corrections.jsonl"),
                              "a", encoding="utf-8") as _fv:
                        _fv.write(_jv.dumps(_ve) + "\n")
                except Exception:
                    pass
            import threading as _thr
            _thr.Thread(target=_verify_bg, daemon=True).start()
            # ── Apply accepted offset to UI ────────────────────────────────
            self._aaf_push_undo(base)
            ov = self._aaf_source_offset_vars.get(base)
            if ov:
                ov.set("{:.3f}".format(accepted_offset))
            sv = self._aaf_source_sync_vars.get(base)
            if sv:
                sv.set(True)
            lv = self._aaf_source_sync_label_vars.get(base)
            if lv:
                lv.set("\u2713 {:.3f}s (manual)".format(accepted_offset))
            btn = self._aaf_sync_btns.get(base)
            if btn:
                self._aaf_refresh_sync_btn(base, btn)
            # Mark as manually confirmed (green dot) and auto-lock
            self._aaf_set_sync_state(base, "manual")
            self._aaf_apply_lock(base, True)

        # Stop any in-progress sync preview playback before opening ALIGN.
        # The user clicking ALIGN signals they want to inspect — silence first.
        try:
            import winsound as _ws
            _ws.PlaySound(None, _ws.SND_PURGE)
        except Exception:
            pass

        # Close any existing preview for this source before opening a fresh one
        prev = self._aaf_sync_previews.get(base)
        if prev is not None:
            try: prev.close()
            except Exception: pass

        dlg = SyncPreviewDialog(
            self, video_path, audio_path,
            initial_offset=offset,
            on_accept=_on_accept,
            source_name=base)
        self._aaf_sync_previews[base] = dlg

    # ── Sync helpers ─────────────────────────────────────────────────────────

    _STATE_DOT_COLORS = {"": SUB, "auto": WARN, "manual": SUCCESS}

    @staticmethod
    def _confirm_btn_style(state):
        """Return (text, fg) for the ACCEPT confirm button given sync state."""
        if state == "auto":
            return "\u2713 ACCEPT", WARN
        if state == "manual":
            return "\u2713 ACCEPTED", SUCCESS
        return "\u2713 ACCEPT", SUB

    def _aaf_sync_snapshot(self, base):
        """Return a dict capturing the current sync state of *base*."""
        return {
            "offset":  self._aaf_source_offset_vars.get(base, tk.StringVar()).get(),
            "state":   self._aaf_sync_state_vars.get(base, tk.StringVar()).get(),
            "locked":  self._aaf_sync_locked_vars.get(base, tk.BooleanVar()).get(),
            "enabled": self._aaf_source_sync_vars.get(base, tk.BooleanVar()).get(),
            "label":   self._aaf_source_sync_label_vars.get(base, tk.StringVar()).get(),
        }

    def _aaf_push_undo(self, base):
        """Snapshot current state onto the undo stack; clear redo stack."""
        stack = getattr(self, "_sync_undo_stack", None)
        if stack is None:
            self._sync_undo_stack = []
            self._sync_redo_stack = []
            stack = self._sync_undo_stack
        stack.append((base, self._aaf_sync_snapshot(base)))
        if len(stack) > 30:
            stack.pop(0)
        self._sync_redo_stack.clear()

    def _aaf_full_snapshot(self):
        """Capture the complete source list and all state for full undo (e.g. source removal)."""
        snap = {
            "__type__": "full",
            "sources": list(self._aaf_sources),
            "hidden_sources": sorted(getattr(self, "_aaf_hidden_sources", set())),
            "assignments": {b: sv.get()
                            for b, sv in self._aaf_source_file_vars.items()},
            "sync": {},
        }
        for b in self._aaf_sources:
            snap["sync"][b] = {
                "enabled":    self._aaf_source_sync_vars.get(b, tk.BooleanVar()).get(),
                "audio_path": self._aaf_source_syncaudio_vars.get(b, tk.StringVar()).get(),
                "audio_disp": self._aaf_source_audio_disp_vars.get(b, tk.StringVar()).get(),
                "offset":     self._aaf_source_offset_vars.get(b, tk.StringVar(value="0.000")).get(),
                "label":      self._aaf_source_sync_label_vars.get(b, tk.StringVar()).get(),
                "state":      self._aaf_sync_state_vars.get(b, tk.StringVar()).get(),
                "needs_sync": self._aaf_needs_sync_vars.get(b, tk.BooleanVar()).get(),
                "locked":     self._aaf_sync_locked_vars.get(b, tk.BooleanVar()).get(),
            }
        return snap

    def _aaf_push_full_undo(self):
        """Push a full-state snapshot — used before destructive operations like source removal."""
        stack = getattr(self, "_sync_undo_stack", None)
        if stack is None:
            self._sync_undo_stack = []
            self._sync_redo_stack = []
            stack = self._sync_undo_stack
        stack.append(self._aaf_full_snapshot())
        if len(stack) > 30:
            stack.pop(0)
        self._sync_redo_stack.clear()

    def _aaf_full_restore(self, snap):
        """Restore a full-state snapshot captured by _aaf_full_snapshot."""
        saved_sources = snap.get("sources", [])
        assignments   = snap.get("assignments", {})
        sync_data     = snap.get("sync", {})

        # Restore source list order (only sources present in the snapshot)
        self._aaf_sources[:] = [b for b in saved_sources if b in self._aaf_sources
                                 or b in sync_data]
        self._aaf_hidden_sources = set(snap.get("hidden_sources", []))

        # Re-create any vars that were removed
        options = ["— no video —"] + [basename(p) for p in self._aaf_video_paths]
        for b in saved_sources:
            if b not in self._aaf_source_file_vars:
                self._aaf_source_file_vars[b]       = tk.StringVar(value="— no video —")
            if b not in self._aaf_source_sync_vars:
                self._aaf_source_sync_vars[b]       = tk.BooleanVar(value=False)
            if b not in self._aaf_source_syncaudio_vars:
                self._aaf_source_syncaudio_vars[b]  = tk.StringVar(value="")
            if b not in self._aaf_source_audio_disp_vars:
                self._aaf_source_audio_disp_vars[b] = tk.StringVar(value="— no audio —")
            if b not in self._aaf_source_offset_vars:
                self._aaf_source_offset_vars[b]     = tk.StringVar(value="0.000")
            if b not in self._aaf_source_sync_label_vars:
                self._aaf_source_sync_label_vars[b] = tk.StringVar(value="")
            if b not in self._aaf_sync_state_vars:
                self._aaf_sync_state_vars[b]        = tk.StringVar(value="")
            if b not in self._aaf_needs_sync_vars:
                self._aaf_needs_sync_vars[b]        = tk.BooleanVar(value=False)
            if b not in self._aaf_sync_locked_vars:
                self._aaf_sync_locked_vars[b]       = tk.BooleanVar(value=False)

        # Apply saved values
        for b in saved_sources:
            fn = assignments.get(b, "— no video —")
            if fn in options:
                self._aaf_source_file_vars[b].set(fn)
            sd = sync_data.get(b, {})
            self._aaf_source_sync_vars[b].set(sd.get("enabled", False))
            self._aaf_source_syncaudio_vars[b].set(sd.get("audio_path", ""))
            self._aaf_source_audio_disp_vars[b].set(sd.get("audio_disp", "— no audio —"))
            self._aaf_source_offset_vars[b].set(sd.get("offset", "0.000"))
            self._aaf_source_sync_label_vars[b].set(sd.get("label", ""))
            self._aaf_sync_state_vars[b].set(sd.get("state", ""))
            self._aaf_needs_sync_vars[b].set(sd.get("needs_sync", False))
            self._aaf_sync_locked_vars[b].set(sd.get("locked", False))

        self._rebuild_aaf_source_rows()

    def _aaf_sync_restore(self, base, snap):
        """Apply a snapshot dict to *base* and refresh the UI."""
        ov = self._aaf_source_offset_vars.get(base)
        if ov: ov.set(snap.get("offset", "0.000"))
        lv = self._aaf_source_sync_label_vars.get(base)
        if lv: lv.set(snap.get("label", ""))
        sv = self._aaf_source_sync_vars.get(base)
        if sv: sv.set(snap.get("enabled", False))
        lkv = self._aaf_sync_locked_vars.get(base)
        if lkv: lkv.set(snap.get("locked", False))
        self._aaf_set_sync_state(base, snap.get("state", ""))
        btn = self._aaf_sync_btns.get(base)
        if btn:
            self._aaf_refresh_sync_btn(base, btn)
        # If locked state changed, rebuild so button enable/disable updates
        self._rebuild_aaf_source_rows()

    def _aaf_sync_undo(self):
        stack = getattr(self, "_sync_undo_stack", [])
        if not stack:
            return
        entry = stack.pop()
        if isinstance(entry, dict) and entry.get("__type__") == "full":
            redo = self._aaf_full_snapshot()
            getattr(self, "_sync_redo_stack", []).append(redo)
            self._aaf_full_restore(entry)
        else:
            base, snap = entry
            redo_snap = self._aaf_sync_snapshot(base)
            getattr(self, "_sync_redo_stack", []).append((base, redo_snap))
            self._aaf_sync_restore(base, snap)

    def _aaf_sync_redo(self):
        stack = getattr(self, "_sync_redo_stack", [])
        if not stack:
            return
        entry = stack.pop()
        if isinstance(entry, dict) and entry.get("__type__") == "full":
            undo = self._aaf_full_snapshot()
            getattr(self, "_sync_undo_stack", []).append(undo)
            self._aaf_full_restore(entry)
        else:
            base, snap = entry
            undo_snap = self._aaf_sync_snapshot(base)
            getattr(self, "_sync_undo_stack", []).append((base, undo_snap))
            self._aaf_sync_restore(base, snap)

    def _aaf_set_sync_state(self, base, state):
        """Set sync state var and update dot + confirm button colours."""
        ssv = self._aaf_sync_state_vars.get(base)
        if ssv:
            ssv.set(state)
        dot = getattr(self, "_aaf_sync_dot_labels", {}).get(base)
        if dot:
            try:
                dot.config(fg=self._STATE_DOT_COLORS.get(state, SUB))
            except Exception:
                pass
        cfm = getattr(self, "_aaf_confirm_btns", {}).get(base)
        if cfm:
            try:
                text, fg = self._confirm_btn_style(state)
                # Only update colour if not locked (locked items are greyed out)
                locked = self._aaf_sync_locked_vars.get(base, tk.BooleanVar()).get()
                if not locked:
                    cfm.config(text=text, fg=fg)
            except Exception:
                pass

    def _aaf_confirm_sync(self, base):
        """Accept the auto-detected offset, mark as confirmed, and lock."""
        if self._aaf_sync_locked_vars.get(base, tk.BooleanVar()).get():
            return
        state = self._aaf_sync_state_vars.get(base, tk.StringVar()).get()
        if not state:
            return   # nothing to confirm
        self._aaf_qa_stop()
        self._aaf_push_undo(base)
        sv = self._aaf_source_sync_vars.get(base)
        if sv:
            sv.set(True)
        self._aaf_set_sync_state(base, "manual")
        # Auto-lock after accepting so it can't be accidentally changed
        self._aaf_apply_lock(base, True)
        # ── Log acceptance for feedback analysis ──────────────────────────
        try:
            import json as _json, datetime as _dt, os as _os
            _ov = self._aaf_source_offset_vars.get(base)
            _offset = float(_ov.get()) if _ov else 0.0
            _vp = self._aaf_source_file_vars.get(base, tk.StringVar()).get()
            _ap = self._aaf_source_syncaudio_vars.get(base, tk.StringVar()).get()
            _cache_dir = _os.path.join(
                _os.path.dirname(_os.path.abspath(__file__)), ".pb_cache")
            _os.makedirs(_cache_dir, exist_ok=True)
            _entry = {
                "ts":      _dt.datetime.now().isoformat(timespec="seconds"),
                "source":  base,
                "video":   _os.path.basename(_vp) if _vp else "",
                "audio":   _os.path.basename(_ap) if _ap else "",
                "auto_T":  round(_offset, 4),
                "verdict": "accepted",   # user was happy with auto result
            }
            with open(_os.path.join(_cache_dir, "sync_corrections.jsonl"),
                      "a", encoding="utf-8") as _fh:
                _fh.write(_json.dumps(_entry) + "\n")
        except Exception:
            pass

    def _aaf_apply_lock(self, base, locked):
        """Set lock state and refresh lock button + dependent widgets."""
        lkv = self._aaf_sync_locked_vars.get(base)
        if lkv:
            lkv.set(locked)
        lock_btn = getattr(self, "_aaf_lock_btns", {}).get(base)
        if lock_btn:
            try:
                lock_btn.config(text="\U0001f512" if locked else "\U0001f513",
                                fg=WARN if locked else SUB)
            except Exception:
                pass
        # Rebuild so all dependent buttons (SYNC, ALIGN, RESET, apply ck) update
        self._rebuild_aaf_source_rows()

    def _aaf_toggle_lock(self, base):
        """Toggle the sync lock for *base*."""
        current = self._aaf_sync_locked_vars.get(base, tk.BooleanVar()).get()
        self._aaf_push_undo(base)
        self._aaf_apply_lock(base, not current)

    def _aaf_reset_sync(self, base):
        """Reset sync state for *base* back to zero."""
        if self._aaf_sync_locked_vars.get(base, tk.BooleanVar()).get():
            return
        self._aaf_push_undo(base)
        ov = self._aaf_source_offset_vars.get(base)
        if ov: ov.set("0.000")
        sv = self._aaf_source_sync_vars.get(base)
        if sv: sv.set(False)
        lv = self._aaf_source_sync_label_vars.get(base)
        if lv: lv.set("")
        self._aaf_set_sync_state(base, "")
        btn = self._aaf_sync_btns.get(base)
        if btn:
            self._aaf_refresh_sync_btn(base, btn)

    def _aaf_qa_stop(self):
        """Stop any currently-playing QA audio and reset its button."""
        try:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
        prev = getattr(self, "_aaf_qa_playing", None)
        if prev:
            btn = self._aaf_qa_btns.get(prev)
            if btn:
                try:
                    btn.config(text="\u25b6 PREVIEW", fg=TEXT)
                except Exception:
                    pass
        self._aaf_qa_playing = None

    def _aaf_qa_toggle(self, base):
        """Toggle QA playback: start if idle/different source, stop if already playing."""
        if getattr(self, "_aaf_qa_playing", None) == base:
            self._aaf_qa_stop()
            return
        self._aaf_qa_stop()   # stop any other source first
        self._aaf_qa_play(base)

    def _aaf_qa_play(self, base):
        """Extract a 10-second middle segment of the aligned pair and play it."""
        fn = self._aaf_source_file_vars.get(base, tk.StringVar()).get()
        if fn == "— no video —":
            messagebox.showwarning("No Video",
                "Assign a video file to this source before QA playback.")
            return
        path_by_fn = {basename(p): p for p in self._aaf_video_paths}
        vp = path_by_fn.get(fn)
        if not vp:
            return
        ap = self._aaf_source_syncaudio_vars.get(base, tk.StringVar()).get()
        if not ap or not os.path.isfile(ap):
            messagebox.showwarning("No Reference Audio",
                "Select and sync a reference audio file before QA playback.")
            return
        try:
            offset_s = float(self._aaf_source_offset_vars[base].get())
        except (KeyError, ValueError):
            offset_s = 0.0

        import tempfile, wave as _wave
        tmp_dir = getattr(self, "_qa_tmp_dir", None)
        if not tmp_dir or not os.path.isdir(tmp_dir):
            self._qa_tmp_dir = tempfile.mkdtemp(prefix="pb_qa_")
            tmp_dir = self._qa_tmp_dir

        QA_DUR = 10.0
        QA_SR  = 16000

        # Mark as playing and switch button to stop icon
        self._aaf_qa_playing = base
        btn = self._aaf_qa_btns.get(base)
        if btn:
            try:
                btn.config(text="\u25a0 STOP", fg=WARN)
            except Exception:
                pass

        def _run():
            import numpy as _np
            # Probe reference duration; fall back to 60s estimate if probe fails
            ref_dur = engines._probe_duration(ap)
            if ref_dur <= 0:
                ref_dur = 60.0   # assume at least 60 s and try anyway

            # Try to find a speech-rich region near the middle of the file
            # using blob detection; fall back to simple midpoint if it fails.
            mid = ref_dur / 2.0
            start_s = max(0.0, mid - QA_DUR / 2.0)
            try:
                blobs = engines.detect_speech_blobs(ap)
                if blobs:
                    # Pick the blob whose onset is closest to the file midpoint
                    best = min(blobs, key=lambda b: abs(b["onset"] - mid))
                    # Start just before the onset so the speech begins naturally
                    start_s = max(0.0, best["onset"] - 0.5)
            except Exception:
                pass

            vid_start = max(0.0, start_s + offset_s)

            ref_wav = os.path.join(tmp_dir, "_qa_ref.wav")
            vid_wav = os.path.join(tmp_dir, "_qa_vid.wav")
            out_wav = os.path.join(tmp_dir, "_qa_mix.wav")

            engines.extract_audio_segment(ap, start_s,   QA_DUR, ref_wav, sample_rate=QA_SR)
            engines.extract_audio_segment(vp, vid_start, QA_DUR, vid_wav, sample_rate=QA_SR)

            def _rd(path):
                with _wave.open(path, "rb") as w:
                    data = w.readframes(w.getnframes())
                arr = _np.frombuffer(data, _np.int16).astype(_np.float32) / 32768.0
                rms = _np.sqrt(_np.mean(arr ** 2))
                return arr / rms * 0.4 if rms > 1e-6 else arr

            a = _rd(ref_wav); b = _rd(vid_wav)
            n = max(len(a), len(b))
            a = _np.pad(a, (0, n - len(a))); b = _np.pad(b, (0, n - len(b)))
            # Stereo: reference (DAW) → left ear, camera audio → right ear.
            # Interleave L/R samples: [L0, R0, L1, R1, ...]
            stereo = _np.empty(n * 2, dtype=_np.float32)
            stereo[0::2] = _np.clip(a, -1.0, 1.0)   # left  = reference
            stereo[1::2] = _np.clip(b, -1.0, 1.0)   # right = camera
            pcm = (stereo * 32767).astype(_np.int16)
            with _wave.open(out_wav, "wb") as w:
                w.setnchannels(2); w.setsampwidth(2)
                w.setframerate(QA_SR); w.writeframes(pcm.tobytes())
            return out_wav

        def _done(fut):
            try:
                wav = fut.result()
            except Exception as exc:
                def _err():
                    self._aaf_qa_stop()
                    messagebox.showerror("QA Play failed", str(exc))
                self.after(0, _err)
                return
            if not wav:
                self.after(0, self._aaf_qa_stop)
                return

            def _play():
                # Guard: user may have clicked stop while extraction was running
                if getattr(self, "_aaf_qa_playing", None) != base:
                    return
                try:
                    import winsound
                    winsound.PlaySound(wav,
                        winsound.SND_FILENAME | winsound.SND_ASYNC)
                except Exception:
                    pass
                # winsound async gives no completion callback; poll until silent
                self._qa_poll(base, wav)

            self.after(0, _play)

        from concurrent.futures import ThreadPoolExecutor
        ex = ThreadPoolExecutor(max_workers=1)
        ex.submit(_run).add_done_callback(_done)
        ex.shutdown(wait=False)

    def _qa_poll(self, base, wav_path):
        """Poll every 500 ms; reset button when the file has finished playing."""
        if getattr(self, "_aaf_qa_playing", None) != base:
            return   # stopped manually
        import os as _os
        # Heuristic: re-check if winsound is still holding the file open
        # by attempting a no-op rename — not reliable cross-platform, so
        # instead we estimate from file duration.
        try:
            import wave as _wave
            with _wave.open(wav_path, "rb") as w:
                dur_ms = int(w.getnframes() / w.getframerate() * 1000)
        except Exception:
            dur_ms = 10000  # 10 s fallback
        # Schedule the reset slightly after the expected end
        self.after(dur_ms + 200, lambda: self._aaf_qa_stop() if
                   getattr(self, "_aaf_qa_playing", None) == base else None)

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
        exts = sorted(MEDIA_EXTS)
        ext_glob = (" ".join("*" + e for e in exts) + " " +
                    " ".join("*" + e.upper() for e in exts))
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
        # Rebuild source rows so audio dropdowns include the new files
        # and auto-match can populate unassigned rows.
        if hasattr(self, "_aaf_assign_frame"):
            self._rebuild_aaf_source_rows()

    def _aaf_browse_mix(self):
        path = filedialog.askopenfilename(
            title="Select stereo mix file",
            filetypes=[("Audio files", "*.wav *.aiff *.aif *.mp3 *.m4a *.flac"),
                       ("All files", "*.*")])
        if path:
            self._aaf_mix_var.set(path)

    def _aaf_set_mix(self, paths):
        """Accept a DnD drop for the mix file — use the first valid audio path."""
        for p in (paths or []):
            if p and is_audio(p):
                self._aaf_mix_var.set(p)
                return

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
        self._aaf_update_seq_presets()

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
        self._aaf_update_seq_presets()

    def _aaf_browse_files(self):
        from utils import VIDEO_EXTS
        exts = sorted(VIDEO_EXTS)
        ext_glob = (" ".join("*" + e for e in exts) + " " +
                    " ".join("*" + e.upper() for e in exts))
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
        self._aaf_update_seq_presets()

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
                    "state":      self._aaf_sync_state_vars.get(
                                      base, tk.StringVar()).get(),
                    "needs_sync": self._aaf_needs_sync_vars.get(
                                      base, tk.BooleanVar()).get(),
                    "locked":     self._aaf_sync_locked_vars.get(
                                      base, tk.BooleanVar()).get(),
                }
                for base in self._aaf_sources
                if base in self._aaf_source_sync_vars
            },
            "hidden_sources": sorted(getattr(self, "_aaf_hidden_sources", set())),
            "grouping":      self._aaf_group_var.get(),
            "fps":           self._aaf_fps_var.get(),
            "seq_preset":    getattr(self, "_aaf_preset_var",  tk.StringVar(value="Detect from media")).get(),
            "seq_w":         getattr(self, "_aaf_seq_w_var",   tk.StringVar()).get(),
            "seq_h":         getattr(self, "_aaf_seq_h_var",   tk.StringVar()).get(),
            "seq_name":      self.seq_name.get(),
            "mix_path":      getattr(self, "_aaf_mix_var", tk.StringVar()).get(),
            "camera_audio":  getattr(self, "_aaf_cam_audio_var", tk.BooleanVar()).get(),
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
        self._aaf_restore_setup(data)

    def _aaf_restore_setup(self, data):
        """Apply a saved AAF setup dict to the current Step 2 UI state."""
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

        aud_by_name = {basename(p): p for p in self._aaf_audio_paths}
        for base, sd in data.get("sync", {}).items():
            if base in self._aaf_source_sync_vars:
                self._aaf_source_sync_vars[base].set(sd.get("enabled", False))
            if base in self._aaf_source_syncaudio_vars:
                full = sd.get("audio_path", "")
                self._aaf_source_syncaudio_vars[base].set(full)
                # Populate display var so the dropdown shows the filename
                bn = basename(full) if full else ""
                if base not in self._aaf_source_audio_disp_vars:
                    self._aaf_source_audio_disp_vars[base] = tk.StringVar()
                self._aaf_source_audio_disp_vars[base].set(
                    bn if bn in aud_by_name else ("— no audio —" if not bn else bn))
            if base in self._aaf_source_offset_vars:
                self._aaf_source_offset_vars[base].set(sd.get("offset", "0.000"))
            if base in self._aaf_source_sync_label_vars:
                self._aaf_source_sync_label_vars[base].set(sd.get("label", ""))
            if base in self._aaf_sync_state_vars:
                self._aaf_sync_state_vars[base].set(sd.get("state", ""))
            if base in self._aaf_needs_sync_vars:
                self._aaf_needs_sync_vars[base].set(sd.get("needs_sync", False))
            if base in self._aaf_sync_locked_vars:
                self._aaf_sync_locked_vars[base].set(sd.get("locked", False))

        # Restore hidden sources
        self._aaf_hidden_sources = set(data.get("hidden_sources", []))

        # Single rebuild + FPS probe for the entire load
        if hasattr(self, "_aaf_assign_frame"):
            self._rebuild_aaf_source_rows()
        self._aaf_schedule_detect_fps()

        # Restore other settings
        if "grouping" in data:  self._aaf_group_var.set(data["grouping"])
        if "fps"      in data:  self._aaf_fps_var.set(data["fps"])
        if "seq_preset" in data:
            if not hasattr(self, "_aaf_preset_var"):
                self._aaf_preset_var = tk.StringVar()
            self._aaf_preset_var.set(data["seq_preset"])
        if "seq_w" in data:
            if not hasattr(self, "_aaf_seq_w_var"):
                self._aaf_seq_w_var = tk.StringVar()
            self._aaf_seq_w_var.set(str(data.get("seq_w", "")))
        if "seq_h" in data:
            if not hasattr(self, "_aaf_seq_h_var"):
                self._aaf_seq_h_var = tk.StringVar()
            self._aaf_seq_h_var.set(str(data.get("seq_h", "")))
        if "seq_name" in data:  self.seq_name.set(data["seq_name"])
        if "mix_path" in data:
            if not hasattr(self, "_aaf_mix_var"):
                self._aaf_mix_var = tk.StringVar()
            self._aaf_mix_var.set(data["mix_path"])
        if "camera_audio" in data:
            if not hasattr(self, "_aaf_cam_audio_var"):
                self._aaf_cam_audio_var = tk.BooleanVar()
            self._aaf_cam_audio_var.set(bool(data["camera_audio"]))

        # Probe loaded files and populate the "Detected from source" preset entries
        self._aaf_update_seq_presets()

        if missing:
            messagebox.showwarning("Missing files",
                "{} video file(s) from the saved setup were not found:\n{}".format(
                    len(missing), "\n".join(basename(p) for p in missing[:5])))

    def _aaf_build_progress(self, pct, text):
        """Show/update the build progress bar.  pct is 0–100.  Thread-safe."""
        import threading as _threading
        def _upd():
            frame = getattr(self, "_aaf_prog_frame", None)
            if frame is None:
                return
            if not frame.winfo_ismapped():
                frame.pack(side="bottom", fill="x", pady=(6, 0))
            self._aaf_prog_lbl.config(text=text)
            self._aaf_prog_bar.set(pct, 100)
        if _threading.current_thread() is _threading.main_thread():
            _upd()
            self.update_idletasks()
        else:
            self.after(0, _upd)

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
        if not out:
            return

        # Snapshot Tkinter vars before handing off to the worker thread.
        sr       = self._aaf_data.get("sample_rate", 48000)
        try:
            seq_w = int(getattr(self, "_aaf_seq_w_var", tk.StringVar()).get() or "0")
            seq_h = int(getattr(self, "_aaf_seq_h_var", tk.StringVar()).get() or "0")
        except ValueError:
            seq_w, seq_h = 0, 0
        mix_path    = getattr(self, "_aaf_mix_var", None)
        mix_path    = mix_path.get() if mix_path else ""
        cam_audio   = getattr(self, "_aaf_cam_audio_var", None)
        cam_audio_v = bool(cam_audio and cam_audio.get())
        seq_name    = (self.seq_name.get()
                       or self._aaf_data.get("session_name", "PostBridge"))

        # Disable the button while the worker runs.
        try:
            self._aaf_build_btn.config(state="disabled")
        except Exception:
            pass

        # ── Worker thread — all heavy work runs here ───────────────────────────
        def _worker():
            _seq_w, _seq_h = seq_w, seq_h   # local copies (may be updated by probe)
            try:
                # Sequence dimensions
                if (_seq_w <= 0 or _seq_h <= 0) and vpaths:
                    self._aaf_build_progress(55, "Probing media settings\u2026")
                    print("Probing media settings ({} files)…".format(len(vpaths)))
                    _seq_w, _seq_h, _, _ = engines.probe_media_settings(vpaths)
                    print("  -> {}x{}".format(_seq_w, _seq_h))
                if _seq_w <= 0: _seq_w = 1280
                if _seq_h <= 0: _seq_h = 720

                # ── Fragment auto-sync ─────────────────────────────────────
                # When an AAF source's audio file is a short rendered fragment
                # (AudioSuite/RX/bounce) rather than the original full recording,
                # src_in from the AAF is relative to the fragment — not the
                # assigned video.  Detect where the fragment appears in the video
                # via cross-correlation so the video <in> points are correct.
                self._aaf_build_progress(60, "Scanning for audio fragments\u2026")
                print("Scanning {} clips for AudioSuite fragments…".format(
                    len(clips_with_media)))

                fragment_jobs = []
                frag_seen_sf  = set()
                for c in clips_with_media:
                    sf = c.get("source_file", "")
                    if not sf or sf in frag_seen_sf:
                        continue
                    base = get_clip_base_name(c["clip_name"])
                    off  = source_offset.get(base, 0.0)
                    if (not c.get("video_path")
                            or abs(off) >= 0.002
                            or bool(source_syncaudio.get(base))):
                        frag_seen_sf.add(sf)
                        continue
                    if not os.path.isfile(sf):
                        print("  skip (missing): {}".format(os.path.basename(sf)))
                        frag_seen_sf.add(sf)
                        continue
                    src_dur = engines._probe_duration(sf)
                    vid_dur = engines._probe_duration(c["video_path"])
                    if src_dur <= 0 or vid_dur <= 0 or src_dur >= vid_dur * 0.5:
                        print("  skip (dur ratio {:.1f}s / {:.1f}s): {}".format(
                            src_dur, vid_dur, os.path.basename(sf)))
                        frag_seen_sf.add(sf)
                        continue
                    print("  fragment: {} ({:.1f}s) vs {} ({:.1f}s)".format(
                        os.path.basename(sf), src_dur,
                        os.path.basename(c["video_path"]), vid_dur))
                    frag_seen_sf.add(sf)
                    fragment_jobs.append((sf, c["video_path"], src_dur, vid_dur))

                print("{} fragment job(s) queued.".format(len(fragment_jobs)))
                frag_offsets = {}
                n_frags = len(fragment_jobs)
                for i, (sf, vp, _sd, _vd) in enumerate(fragment_jobs, 1):
                    clip_label = os.path.basename(sf)
                    if len(clip_label) > 45:
                        clip_label = clip_label[:42] + "\u2026"
                    is_cached = engines._rx_offset_cache_load(sf, vp) is not None
                    print("  [{}/{}]{} {}".format(
                        i, n_frags, " [cached]" if is_cached else "", clip_label))
                    self._aaf_build_progress(
                        60 + int(9 * i / max(n_frags, 1)),
                        "Fragment sync {}/{}{}: {}".format(
                            i, n_frags,
                            " [cached]" if is_cached else "",
                            clip_label))
                    T = engines.detect_rx_offset(sf, vp)
                    print("    -> offset {:.3f}s{}".format(
                        T, " (cached)" if is_cached else ""))
                    if T != 0.0:
                        frag_offsets[os.path.normpath(sf).lower()] = T

                if frag_offsets:
                    for c in clips_with_media:
                        sf = c.get("source_file", "")
                        if sf:
                            key = os.path.normpath(sf).lower()
                            if key in frag_offsets:
                                c["video_offset_secs"] = frag_offsets[key]

                if DEV_DIAGNOSTIC:
                    self._aaf_build_progress(70, "Writing diagnostic report\u2026")
                    try:
                        diag_path = os.path.splitext(out)[0] + "_diagnostic.txt"
                        engines.write_build_diagnostic(
                            clips_with_media, fps, _seq_w, _seq_h, sr,
                            out_path=diag_path)
                    except Exception:
                        pass

                self._aaf_build_progress(80, "Building XML\u2026")
                xmeml = engines.build_xml_from_pt(
                    clips_with_media,
                    track_names_ordered,
                    seq_name,
                    seq_w=_seq_w, seq_h=_seq_h, seq_fps=fps, seq_sr=sr,
                    mix_path=mix_path or None,
                    include_camera_audio=cam_audio_v)

                self._aaf_build_progress(95, "Writing file\u2026")
                engines.write_xml(xmeml, out)
                engines.clear_rx_cache()

                def _finish():
                    self.out_path.set(out)
                    self._aaf_done(matched, unmatched,
                                   len(clips_with_media), clip_results)
                self.after(0, _finish)

            except Exception as e:
                engines.clear_rx_cache()
                _err = str(e)
                def _show_err():
                    try:
                        self._aaf_build_btn.config(state="normal")
                    except Exception:
                        pass
                    messagebox.showerror("Build Error", _err)
                self.after(0, _show_err)

        import threading as _threading
        _threading.Thread(target=_worker, daemon=True).start()

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
            log_outer.pack(fill="both", expand=True, pady=(0,6))
            log = tk.Text(log_outer,
                          font=("Courier New", 10), bg=SURF, fg=TEXT,
                          relief="flat", bd=0,
                          state="normal", wrap="none")
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

    # ── Transcribe Media workflow ─────────────────────────────────────────────

    def _transcribe_workflow(self):
        """Standalone batch transcription: drop media files, transcribe them,
        save a .pb_transcript.json alongside each one for fast reconcile later.
        Includes a word-search panel for finding specific words with timestamps."""
        self._clear()
        self._section("TRANSCRIBE MEDIA")

        tk.Label(self.body,
                 text="Drop media files below. PostBridge will transcribe each one "
                      "and save a .pb_transcript.json file alongside it. "
                      "Once transcribed, the Script \u2192 Session reconcile will "
                      "load these instantly instead of re-transcribing.",
                 font=FB, bg=BG, fg=SUB, wraplength=700, justify="left"
                 ).pack(anchor="w", pady=(0, 16))

        # ── File list ─────────────────────────────────────────────────────────
        list_outer = tk.Frame(self.body, bg=SURF,
                              highlightbackground=BORDER, highlightthickness=1)
        list_outer.pack(fill="both", expand=True, pady=(0, 12))

        list_hdr = tk.Frame(list_outer, bg=SURF3)
        list_hdr.pack(fill="x")
        tk.Label(list_hdr, text="FILE", font=FL, bg=SURF3, fg=SUB,
                 anchor="w", padx=12, pady=6).pack(side="left")
        tk.Label(list_hdr, text="STATUS", font=FL, bg=SURF3, fg=SUB,
                 anchor="e", padx=12, pady=6).pack(side="right")

        list_canvas = tk.Canvas(list_outer, bg=SURF, highlightthickness=0, height=280)
        list_scroll = tk.Scrollbar(list_outer, orient="vertical",
                                   command=list_canvas.yview)
        list_scroll.pack(side="right", fill="y")
        list_canvas.pack(fill="both", expand=True)
        list_canvas.configure(yscrollcommand=list_scroll.set)

        list_frame = tk.Frame(list_canvas, bg=SURF)
        list_canvas.create_window((0, 0), window=list_frame, anchor="nw")
        list_frame.bind("<Configure>",
                        lambda e: list_canvas.configure(
                            scrollregion=list_canvas.bbox("all")))

        # ── Drop zone ─────────────────────────────────────────────────────────
        drop_frame = tk.Frame(self.body, bg=SURF2,
                              highlightbackground=BORDER, highlightthickness=1)
        drop_frame.pack(fill="x", pady=(0, 12))

        drop_lbl = tk.Label(drop_frame,
                            text="Drop media files here  or  click to browse",
                            font=FB, bg=SURF2, fg=SUB, pady=18)
        drop_lbl.pack(fill="x")

        # ── Progress bar ──────────────────────────────────────────────────────
        prog_frame = tk.Frame(self.body, bg=BG)
        prog_frame.pack(fill="x", pady=(0, 6))
        prog_lbl = tk.Label(prog_frame, text="", font=FB, bg=BG, fg=SUB, anchor="w")
        prog_lbl.pack(anchor="w")
        prog_bar = _FlatProgressBar(prog_frame, height=6)
        prog_bar.pack(fill="x", pady=(4, 0))

        # ── Controls row ──────────────────────────────────────────────────────
        ctrl = tk.Frame(self.body, bg=BG)
        ctrl.pack(fill="x", pady=(0, 8))

        # ── Fast / Background mode toggle ─────────────────────────────────────
        _cpu_fast  = max(1, (os.cpu_count() or 2) - 1)  # all cores minus one
        _bg_mode   = [False]      # False = fast, True = background
        _workers   = [_cpu_fast]  # mutable: current target concurrency
        _cond      = __import__("threading").Condition()  # wakes blocked slots
        _active    = [0]          # mutable: currently running threads

        bg_frame = tk.Frame(ctrl, bg=BG)
        bg_frame.pack(side="left")

        bg_ck = tk.Label(bg_frame, text="\u2610", font=(_SANS, 15),
                         bg=BG, fg=SUB, cursor="hand2", padx=4)
        bg_ck.pack(side="left")
        bg_lbl = tk.Label(bg_frame, text="Background mode", font=FB, bg=BG, fg=SUB,
                          cursor="hand2")
        bg_lbl.pack(side="left")

        def _toggle_bg():
            _bg_mode[0] = not _bg_mode[0]
            _on = _bg_mode[0]
            _workers[0] = 1 if _on else _cpu_fast
            bg_ck.config(text="\u2611" if _on else "\u2610",
                         fg=ACCENT if _on else SUB)
            with _cond:
                _cond.notify_all()

        bg_ck.bind("<Button-1>",  lambda e: _toggle_bg())
        bg_lbl.bind("<Button-1>", lambda e: _toggle_bg())

        # Mutable command slots — _btn captures these dispatchers at bind time;
        # setting the slot to None disables the button without rebinding.
        _tx_cmd  = [None]
        _clr_cmd = [None]

        tx_btn  = self._btn(ctrl, "TRANSCRIBE", lambda: _tx_cmd[0] and _tx_cmd[0](),
                            color=ACCENT)
        tx_btn.pack(side="right")
        clr_btn = self._btn(ctrl, "CLEAR LIST", lambda: _clr_cmd[0] and _clr_cmd[0](),
                            small=True)
        clr_btn.pack(side="right", padx=(0, 8))

        # ── State ─────────────────────────────────────────────────────────────
        _files         = []     # list of abs paths
        _rows          = {}     # path → {"row", "status"}
        _running       = [False]
        _cancel        = [False]
        _selected_file = [None] # path currently loaded in the search panel

        def _fmt_ts(s):
            """Format seconds as HH:MM:SS.f for display."""
            h   = int(s // 3600)
            m   = int((s % 3600) // 60)
            sec = s % 60
            return "{:02d}:{:02d}:{:04.1f}".format(h, m, sec)

        def _status_color(status):
            if status in ("done", "skipped"):  return SUCCESS
            if status == "error":              return ERR
            if status == "transcribing":       return ACCENT
            return SUB

        # ── Search panel (built before _add_files so _select_file can ref it) ──
        tk.Frame(self.body, bg=BORDER, height=1).pack(fill="x", pady=(4, 12))

        search_outer = tk.Frame(self.body, bg=SURF,
                                highlightbackground=BORDER, highlightthickness=1)
        search_outer.pack(fill="both", expand=True, pady=(0, 4))

        search_hdr = tk.Frame(search_outer, bg=SURF3)
        search_hdr.pack(fill="x")
        tk.Label(search_hdr, text="SEARCH TRANSCRIPT", font=FL,
                 bg=SURF3, fg=SUB, anchor="w", padx=12, pady=6).pack(side="left")
        search_file_lbl = tk.Label(search_hdr, text="— click a transcribed file above —",
                                   font=FB, bg=SURF3, fg=SUB, anchor="e", padx=12)
        search_file_lbl.pack(side="right")

        search_entry_row = tk.Frame(search_outer, bg=SURF, pady=8)
        search_entry_row.pack(fill="x", padx=12)
        search_var = tk.StringVar()
        search_entry = tk.Entry(search_entry_row, textvariable=search_var,
                                font=FB, bg=SURF2, fg=TEXT,
                                insertbackground=TEXT, relief="flat",
                                highlightbackground=BORDER, highlightthickness=1,
                                width=28)
        search_entry.pack(side="left", ipady=5, padx=(0, 8))
        search_entry.insert(0, "")
        search_count_lbl = tk.Label(search_entry_row, text="", font=FB,
                                    bg=SURF, fg=SUB)
        search_count_lbl.pack(side="left")

        # Results canvas
        res_canvas = tk.Canvas(search_outer, bg=SURF, highlightthickness=0, height=180)
        res_scroll = tk.Scrollbar(search_outer, orient="vertical",
                                  command=res_canvas.yview)
        res_scroll.pack(side="right", fill="y")
        res_canvas.pack(fill="both", expand=True, padx=(0, 0))
        res_canvas.configure(yscrollcommand=res_scroll.set)
        res_frame = tk.Frame(res_canvas, bg=SURF)
        res_canvas.create_window((0, 0), window=res_frame, anchor="nw")
        res_frame.bind("<Configure>",
                       lambda e: res_canvas.configure(
                           scrollregion=res_canvas.bbox("all")))

        no_results_lbl = tk.Label(res_frame, text="",
                                  font=FB, bg=SURF, fg=SUB,
                                  anchor="w", padx=16, pady=10)
        no_results_lbl.pack(anchor="w")

        def _do_search(*_):
            term = search_var.get().strip().lower()
            # Clear previous results
            for w in list(res_frame.winfo_children()):
                w.destroy()
            no_results_lbl_inner = tk.Label(res_frame, text="",
                                            font=FB, bg=SURF, fg=SUB,
                                            anchor="w", padx=16, pady=10)
            no_results_lbl_inner.pack(anchor="w")

            path = _selected_file[0]
            if not path:
                no_results_lbl_inner.config(
                    text="Select a transcribed file above to search.")
                search_count_lbl.config(text="")
                return
            if not term:
                no_results_lbl_inner.config(text="Type a word to search.")
                search_count_lbl.config(text="")
                return

            words, _ = engines.pb_transcript_load(path)
            if not words:
                no_results_lbl_inner.config(
                    text="No transcript found for this file. Transcribe it first.")
                search_count_lbl.config(text="")
                return

            CONTEXT = 5   # words of context each side
            hits = [i for i, w in enumerate(words)
                    if term in w.get("word", "")]

            search_count_lbl.config(
                text="{} match{}".format(len(hits), "es" if len(hits) != 1 else "")
                if hits else "no matches")

            if not hits:
                no_results_lbl_inner.config(
                    text='No matches for "{}".'.format(term))
                return

            no_results_lbl_inner.destroy()

            for idx in hits:
                w      = words[idx]
                ts     = _fmt_ts(w.get("start", 0))
                before = " ".join(x["word"] for x in words[max(0, idx-CONTEXT):idx])
                after  = " ".join(x["word"] for x in words[idx+1:idx+1+CONTEXT])
                hit_w  = w.get("word", "")

                row = tk.Frame(res_frame, bg=SURF)
                row.pack(fill="x", padx=8, pady=2)

                ts_lbl = tk.Label(row, text=ts, font=(_SANS, 11, "bold"),
                                  bg=SURF, fg=ACCENT, width=11, anchor="w",
                                  cursor="hand2")
                ts_lbl.pack(side="left", padx=(4, 8))
                self._tooltip(ts_lbl, "Click to copy timecode")

                def _copy_ts(t=ts):
                    self.clipboard_clear()
                    self.clipboard_append(t)
                ts_lbl.bind("<Button-1>", lambda e, t=ts: _copy_ts(t))

                ctx_frame = tk.Frame(row, bg=SURF)
                ctx_frame.pack(side="left", fill="x", expand=True)

                if before:
                    tk.Label(ctx_frame, text="…" + before + " ",
                             font=FB, bg=SURF, fg=SUB,
                             anchor="w").pack(side="left")
                tk.Label(ctx_frame, text=hit_w,
                         font=(_SANS, 12, "bold"), bg=SURF, fg=TEXT,
                         anchor="w").pack(side="left")
                if after:
                    tk.Label(ctx_frame, text=" " + after + "…",
                             font=FB, bg=SURF, fg=SUB,
                             anchor="w").pack(side="left")

                tk.Frame(res_frame, bg=BORDER, height=1).pack(fill="x", padx=8)

        search_var.trace_add("write", _do_search)

        def _select_file(path):
            # Deselect previous
            prev = _selected_file[0]
            if prev and prev in _rows:
                _rows[prev]["row"].config(
                    highlightbackground=SURF, highlightthickness=0)

            # Only allow selecting transcribed files
            if path not in _rows:
                return
            status = _rows[path]["status"].cget("text")
            has_pb, _ = engines.pb_transcript_load(path)
            if not has_pb:
                return

            _selected_file[0] = path
            _rows[path]["row"].config(
                highlightbackground=ACCENT, highlightthickness=2)
            search_file_lbl.config(text=os.path.basename(path))
            _do_search()   # re-run with new file

        def _add_files(paths):
            for p in paths:
                p = os.path.abspath(p)
                if p in _rows:
                    continue
                if not is_media(p):
                    continue
                _files.append(p)
                row = tk.Frame(list_frame, bg=SURF,
                               highlightbackground=SURF, highlightthickness=0,
                               cursor="hand2")
                row.pack(fill="x", padx=4, pady=1)

                # Check if a .pb_transcript.json already exists for this file
                existing, _ = engines.pb_transcript_load(p)
                init_status = "transcribed" if existing else "pending"
                init_color  = SUCCESS if existing else SUB

                name_lbl = tk.Label(row, text=os.path.basename(p), font=FB,
                                    bg=SURF, fg=TEXT, anchor="w")
                name_lbl.pack(side="left", fill="x", expand=True, padx=(8, 4))
                status_lbl = tk.Label(row, text=init_status, font=FB,
                                      bg=SURF, fg=init_color, anchor="e", padx=8)
                status_lbl.pack(side="right")

                _rows[p] = {"row": row, "status": status_lbl}

                for w in (row, name_lbl, status_lbl):
                    w.bind("<Button-1>", lambda e, path=p: _select_file(path))
                    w.bind("<Enter>",
                           lambda e, r=row: r.config(bg=SURF2) if _selected_file[0] != p else None)
                    w.bind("<Leave>",
                           lambda e, r=row: r.config(bg=SURF)  if _selected_file[0] != p else None)

            _update_btn_state()

        def _clear_list():
            if _running[0]:
                return
            for w in list(list_frame.winfo_children()):
                w.destroy()
            _files.clear()
            _rows.clear()
            _selected_file[0] = None
            search_file_lbl.config(text="— click a transcribed file above —")
            _do_search()
            _update_btn_state()

        def _update_btn_state():
            has_pending = any(
                _rows[p]["status"].cget("text") in ("pending", "error")
                for p in _files if p in _rows
            )
            can_tx  = has_pending and not _running[0]
            can_clr = not _running[0]
            _tx_cmd[0]  = _start       if can_tx  else None
            _clr_cmd[0] = _clear_list  if can_clr else None
            tx_btn.config( fg=TEXT if can_tx  else SUB)
            clr_btn.config(fg=TEXT if can_clr else SUB)

        def _set_prog(text, done=None, total=None):
            def _upd():
                prog_lbl.config(text=text)
                if done is not None and total:
                    prog_bar.set(done, total)
            self.after(0, _upd)

        def _set_status(path, text):
            def _upd():
                if path not in _rows:
                    return
                _rows[path]["status"].config(
                    text=text, fg=_status_color(text))
                # Auto-select first newly-transcribed file for search
                if text == "done" and _selected_file[0] is None:
                    _select_file(path)
            self.after(0, _upd)

        # ── Browse ────────────────────────────────────────────────────────────
        def _browse(e=None):
            paths = filedialog.askopenfilenames(
                title="Select media files",
                filetypes=[("Media files",
                            " ".join("*" + x for x in sorted(MEDIA_EXTS))),
                           ("All files", "*.*")])
            if paths:
                _add_files(paths)

        drop_lbl.bind("<Button-1>", _browse)
        drop_frame.bind("<Button-1>", _browse)

        try:
            drop_frame.drop_target_register("DND_Files")
            drop_frame.dnd_bind("<<Drop>>",
                lambda e: _add_files(
                    [p.strip().strip("{}") for p in e.data.split()
                     if os.path.isfile(p.strip().strip("{}"))]))
            drop_lbl.drop_target_register("DND_Files")
            drop_lbl.dnd_bind("<<Drop>>",
                lambda e: _add_files(
                    [p.strip().strip("{}") for p in e.data.split()
                     if os.path.isfile(p.strip().strip("{}"))]))
        except Exception:
            pass

        # ── Worker ────────────────────────────────────────────────────────────
        def _run():
            import concurrent.futures as _cf
            _running[0] = True
            _cancel[0]  = False

            def _enter_cancel_mode():
                _tx_cmd[0] = lambda: _cancel.__setitem__(0, True)
                tx_btn.config(text="CANCEL", fg=WARN)
                _clr_cmd[0] = None
                clr_btn.config(fg=SUB)

            self.after(0, _enter_cancel_mode)

            pending = [p for p in _files
                       if p in _rows
                       and _rows[p]["status"].cget("text") in ("pending", "error")]

            n_total = len(pending)
            n_done  = [0]

            # ── Condition-variable gate — supports live worker-count changes ──
            import threading as _th

            def _acquire():
                """Block until a slot is free, then claim it."""
                with _cond:
                    while _active[0] >= _workers[0]:
                        _cond.wait(timeout=0.3)
                        if _cancel[0]:
                            return False
                    _active[0] += 1
                    return True

            def _release():
                with _cond:
                    _active[0] = max(0, _active[0] - 1)
                    _cond.notify_all()

            def _process_one(path):
                if _cancel[0]:
                    _release()
                    return
                name = os.path.basename(path)
                _set_status(path, "transcribing")
                _set_prog("\u23f3  Transcribing {} ({}/{})…".format(
                    name, n_done[0] + 1, n_total),
                    done=n_done[0], total=n_total)

                def _progress_cb(frac, msg):
                    _set_prog("\u23f3  {} — {} ({}/{})".format(
                        msg, name, n_done[0] + 1, n_total),
                        done=n_done[0], total=n_total)

                try:
                    words, blobs = engines.transcribe_file(
                        path, progress_cb=_progress_cb)
                    if words:
                        engines.pb_transcript_save(path, words, blobs)
                        n_done[0] += 1
                        _set_status(path, "done")
                        _set_prog("\u2713  {} done  ({}/{})".format(
                            name, n_done[0], n_total),
                            done=n_done[0], total=n_total)
                    else:
                        _set_status(path, "error")
                        _set_prog("\u26a0  {} — no words detected".format(name),
                                  done=n_done[0], total=n_total)
                except Exception as exc:
                    print("Transcribe error [{}]: {}".format(name, exc))
                    _set_status(path, "error")
                    _set_prog("\u26a0  Error on {}".format(name),
                              done=n_done[0], total=n_total)
                finally:
                    _release()

            # Dispatcher: iterate pending, acquire a slot, spin up a thread.
            # When _workers[0] is 1 the gate allows only one through at a time;
            # increasing it live wakes blocked iterations immediately.
            dispatch_threads = []
            for path in pending:
                if _cancel[0]:
                    break
                if not _acquire():   # returns False on cancel
                    break
                t = _th.Thread(target=_process_one, args=(path,), daemon=True)
                dispatch_threads.append(t)
                t.start()

            # Wait for all launched threads to finish
            for t in dispatch_threads:
                t.join()

            _active[0] = 0

            _running[0] = False
            self.after(0, _on_done)

        def _on_done():
            n_done = sum(1 for p in _files
                         if p in _rows
                         and _rows[p]["status"].cget("text") == "done")
            n_err  = sum(1 for p in _files
                         if p in _rows
                         and _rows[p]["status"].cget("text") == "error")
            summary = "\u2713  {} file{} transcribed".format(
                n_done, "s" if n_done != 1 else "")
            if n_err:
                summary += "  \u00b7  \u26a0 {} error{}".format(
                    n_err, "s" if n_err != 1 else "")
            prog_lbl.config(text=summary, fg=SUCCESS if not n_err else WARN)
            prog_bar.set(n_done, max(n_done + n_err, 1))
            tx_btn.config(text="TRANSCRIBE")
            _update_btn_state()

        def _start():
            import threading
            threading.Thread(target=_run, daemon=True).start()

        _update_btn_state()
        _do_search()   # seed the search panel with its placeholder state

    # ── Script Formatter workflow ─────────────────────────────────────────────

    def _script_formatter(self):
        """Standalone script builder: token chips, @PART/@VO/@PULL block copiers,
        format reference cheat sheet, and AI prompt copier."""
        self._clear()
        self._sf_tokens = []

        # ── Header ────────────────────────────────────────────────────────────
        hdr_row = tk.Frame(self.body, bg=BG)
        hdr_row.pack(fill="x", pady=(16, 4))
        self._btn(hdr_row, "\u2190 Back", self._home, small=True).pack(side="left")
        tk.Label(hdr_row, text="Script Formatter", font=FBT,
                 bg=BG, fg=TEXT).pack(side="left", padx=20)

        sf = self._scroll_frame(self.body)

        # ── Card helper ───────────────────────────────────────────────────────
        def _card(title):
            outer = tk.Frame(sf, bg=SURF,
                             highlightbackground=BORDER, highlightthickness=1)
            outer.pack(fill="x", pady=(0, 14))
            tk.Frame(outer, bg=ACCENT, width=4).pack(side="left", fill="y")
            inner = tk.Frame(outer, bg=SURF)
            inner.pack(fill="x", padx=20, pady=16, side="left", expand=True)
            tk.Label(inner, text=title.upper(),
                     font=("Courier New", 9, "bold"),
                     bg=SURF, fg=SUB).pack(anchor="w", pady=(0, 10))
            return inner

        # ── Copy-with-feedback row helper ──────────────────────────────────
        def _copy_row(parent, label, get_text):
            row = tk.Frame(parent, bg=SURF)
            row.pack(fill="x", pady=(10, 0))
            fb = tk.Label(row, text="", font=FB, bg=SURF, fg=SUCCESS)
            fb.pack(side="right", padx=4)
            def _do():
                t = get_text()
                if not t:
                    return
                self.clipboard_clear()
                self.clipboard_append(t)
                fb.config(text="\u2713 Copied!")
                fb.after(2000, lambda: fb.config(text=""))
            self._btn(row, label, _do, small=True).pack(side="left")

        # Shared @PULL state
        _pull_text    = [""]
        _pull_tok_var = tk.StringVar()

        # ═════════════════════════════════════════════════════════════════════
        # SECTION 0 — Format Reference + AI Prompt
        # ═════════════════════════════════════════════════════════════════════
        ref_inner = _card("Script Format Reference")

        tk.Label(ref_inner,
                 text=("PostBridge reads scripts marked up with special directives. "
                       "If your script isn\u2019t already in this format, use the AI prompt "
                       "to reformat it: click \u201cCopy AI Prompt,\u201d paste it into ChatGPT, "
                       "Claude, or another assistant, then paste your script after it."),
                 font=FB, bg=SURF, fg=SUB,
                 justify="left", wraplength=900, anchor="w").pack(anchor="w",
                                                                  pady=(0, 8))

        tk.Label(ref_inner,
                 text=("--- INTERVIEW SESSIONS START ---    @PART  Part Name\n"
                       "[TOKEN_NAME]                        @PULL  TOKEN [HH:MM:SS-HH:MM:SS]\n"
                       "--- INTERVIEW SESSIONS END ---        Quote text \u2014 no blank lines inside\n"
                       "                                    @VO  PART_N_LABEL\n"
                       "--- EPISODE ASSETS START ---          VO text \u2014 ends at next @, //, or blank\n"
                       "[PART_0_NARRATOR]                   // comment\n"
                       "--- EPISODE ASSETS END ---"),
                 font=("Courier New", 10), bg=SURF, fg=SUB,
                 justify="left", anchor="w").pack(anchor="w", pady=(0, 8))

        _copy_row(ref_inner, "Copy AI Prompt", self._ai_prompt_text)

        # ═════════════════════════════════════════════════════════════════════
        # SECTION 1 — Episode Tokens
        # ═════════════════════════════════════════════════════════════════════
        tok_inner = _card("Episode Tokens")

        tk.Label(tok_inner,
                 text="Token names \u2014 one per line  (e.g. INTERVIEW_A)",
                 font=FB, bg=SURF, fg=SUB).pack(anchor="w")
        tok_text = tk.Text(tok_inner, height=4,
                           font=("Courier New", 11),
                           bg=SURF2, fg=TEXT, bd=0, relief="flat",
                           highlightbackground=BORDER, highlightthickness=1,
                           insertbackground=TEXT, padx=8, pady=6)
        tok_text.pack(fill="x", pady=(4, 10))

        chip_row = tk.Frame(tok_inner, bg=SURF)
        chip_row.pack(fill="x", pady=(0, 10))
        tk.Label(chip_row, text="No tokens yet",
                 font=(_SANS, 10, "italic"),
                 bg=SURF, fg=SUB).pack(side="left")

        tk.Label(tok_inner, text="Asset block for document header",
                 font=FB, bg=SURF, fg=SUB).pack(anchor="w")
        asset_var = tk.StringVar(value="Enter tokens above")
        asset_lbl = tk.Label(tok_inner, textvariable=asset_var,
                             font=("Courier New", 11),
                             bg=SURF2, fg=SUB, justify="left", anchor="nw",
                             padx=10, pady=8)
        asset_lbl.pack(fill="x", pady=(4, 0))

        _copy_row(tok_inner, "Copy Asset Block",
                  lambda: asset_var.get() if self._sf_tokens else "")
        tk.Label(tok_inner,
                 text="Paste this block into the doc once, near the top, "
                      "before any @PART lines.",
                 font=FB, bg=SURF, fg=SUB).pack(anchor="w", pady=(6, 0))

        # ═════════════════════════════════════════════════════════════════════
        # SECTION 2 — @PART
        # ═════════════════════════════════════════════════════════════════════
        part_inner = _card("@PART \u2014 Insert a Part Marker")

        tk.Label(part_inner, text="Part title",
                 font=FB, bg=SURF, fg=SUB).pack(anchor="w")
        part_var = tk.StringVar()
        tk.Entry(part_inner, textvariable=part_var,
                 font=(_SANS, 13),
                 bg=SURF2, fg=TEXT, bd=0, relief="flat",
                 highlightbackground=BORDER, highlightthickness=1,
                 insertbackground=TEXT).pack(fill="x", pady=(4, 8))

        part_preview = tk.Label(part_inner, text="\u2014",
                                font=("Courier New", 12),
                                bg=SURF2, fg=SUB, anchor="w", padx=10, pady=8)
        part_preview.pack(fill="x")

        def _update_part(*_):
            t = part_var.get().strip()
            part_preview.config(
                text="@PART " + t if t else "\u2014",
                fg=TEXT if t else SUB)

        part_var.trace_add("write", _update_part)
        _copy_row(part_inner, "Copy @PART",
                  lambda: ("@PART " + part_var.get().strip())
                  if part_var.get().strip() else "")

        # ═════════════════════════════════════════════════════════════════════
        # SECTION 3 — @VO
        # ═════════════════════════════════════════════════════════════════════
        vo_inner = _card("@VO \u2014 Insert a Voice-Over Marker")

        tk.Label(vo_inner,
                 text=("Copies @VO to your clipboard \u2014 paste it into the doc, "
                       "then type your VO copy on the line below it."),
                 font=FB, bg=SURF, fg=SUB, justify="left").pack(anchor="w",
                                                                pady=(0, 6))
        _copy_row(vo_inner, "Copy @VO", lambda: "@VO")

        # ═════════════════════════════════════════════════════════════════════
        # SECTION 4 — @PULL
        # ═════════════════════════════════════════════════════════════════════
        pull_inner = _card("@PULL \u2014 Insert a Pull Marker")

        tk.Label(pull_inner, text="Token",
                 font=FB, bg=SURF, fg=SUB).pack(anchor="w")
        pull_cb = ttk.Combobox(pull_inner, textvariable=_pull_tok_var,
                               values=["\u2014 select token \u2014"],
                               state="readonly", font=(_SANS, 12))
        pull_cb.set("\u2014 select token \u2014")
        pull_cb.pack(fill="x", pady=(4, 14))

        tc_row = tk.Frame(pull_inner, bg=SURF)
        tc_row.pack(fill="x", pady=(0, 4))

        def _make_tc_group(parent, label_text):
            col = tk.Frame(parent, bg=SURF)
            col.pack(side="left", expand=True, fill="x", padx=(0, 16))
            tk.Label(col, text=label_text, font=FB, bg=SURF, fg=SUB).pack(anchor="w")
            grp = tk.Frame(col, bg=SURF2,
                           highlightbackground=BORDER, highlightthickness=1)
            grp.pack(fill="x", pady=(4, 0))

            def _only_digits(P):
                return P == "" or (P.isdigit() and len(P) <= 2)

            vcmd = (grp.register(_only_digits), "%P")
            entries = []
            for i in range(3):
                if i > 0:
                    tk.Label(grp, text=":", font=("Courier New", 13),
                             bg=SURF2, fg=SUB).pack(side="left")
                e = tk.Entry(grp, width=3, font=("Courier New", 13),
                             bg=SURF2, fg=TEXT, bd=0, relief="flat",
                             insertbackground=TEXT, justify="center",
                             validate="key", validatecommand=vcmd)
                e.pack(side="left",
                       padx=(8 if i == 0 else 0, 8 if i == 2 else 0),
                       pady=7)
                entries.append(e)

            for i, e in enumerate(entries):
                def _on_key(ev, idx=i, en=e):
                    if len(en.get()) == 2 and idx < 2:
                        entries[idx + 1].focus_set()
                        entries[idx + 1].select_range(0, "end")
                    _build_pull()

                def _on_bs(ev, idx=i, en=e):
                    if idx > 0 and not en.get():
                        entries[idx - 1].focus_set()
                        entries[idx - 1].icursor("end")

                e.bind("<KeyRelease>", _on_key)
                e.bind("<BackSpace>", _on_bs)
                e.bind("<FocusIn>", lambda ev, en=e: en.select_range(0, "end"))

            def get_val():
                vals = [en.get() for en in entries]
                if not any(vals):
                    return ""
                return ":".join(v.zfill(2) for v in vals)

            def is_done():
                return all(len(en.get()) == 2 for en in entries)

            def clear_all():
                for en in entries:
                    en.delete(0, "end")

            return get_val, is_done, clear_all

        get_in_tc,  in_done,  clear_in  = _make_tc_group(tc_row, "IN timecode")
        get_out_tc, out_done, clear_out = _make_tc_group(tc_row, "OUT timecode")

        pull_msg = tk.Label(pull_inner, text="", font=FB,
                            bg=SURF, fg=ERR, anchor="w", justify="left")
        pull_msg.pack(anchor="w", pady=(4, 0))

        pull_preview = tk.Label(pull_inner, text="\u2014",
                                font=("Courier New", 12),
                                bg=SURF2, fg=SUB, anchor="w", padx=10, pady=8)
        pull_preview.pack(fill="x", pady=(4, 0))

        def _build_pull(*_):
            tok    = _pull_tok_var.get()
            i_ok   = in_done()
            o_ok   = out_done()
            in_tc  = get_in_tc()  if i_ok else ""
            out_tc = get_out_tc() if o_ok else ""

            pull_msg.config(text="", fg=ERR)
            pull_preview.config(text="\u2014", fg=SUB)
            _pull_text[0] = ""

            if not tok or tok == "\u2014 select token \u2014" \
                    or not i_ok or not o_ok:
                return

            def _valid(tc):
                if len(tc) != 8:
                    return False
                try:
                    parts = tc.split(":")
                    return int(parts[1]) < 60 and int(parts[2]) < 60
                except (ValueError, IndexError):
                    return False

            def _secs(tc):
                h, m, s = (int(x) for x in tc.split(":"))
                return h * 3600 + m * 60 + s

            errs = []
            if not _valid(in_tc):
                errs.append("IN timecode invalid (MM/SS must be < 60)")
            if not _valid(out_tc):
                errs.append("OUT timecode invalid (MM/SS must be < 60)")
            if not errs and _secs(out_tc) <= _secs(in_tc):
                errs.append("OUT must be after IN")

            if errs:
                pull_msg.config(text=";  ".join(errs), fg=ERR)
                return

            line = "@PULL {} [{}-{}]".format(tok, in_tc, out_tc)
            pull_preview.config(text=line, fg=TEXT)
            _pull_text[0] = line

            dur = _secs(out_tc) - _secs(in_tc)
            if dur > 300:
                m_ = dur // 60
                s_ = dur % 60
                dur_s = "{}m {}s".format(m_, s_) if s_ else "{}m".format(m_)
                pull_msg.config(
                    text="\u26a0  Duration is {} \u2014 confirm timing is correct".format(dur_s),
                    fg=WARN)
            else:
                pull_msg.config(text="\u2713 Looks good", fg=SUCCESS)

        _pull_tok_var.trace_add("write", _build_pull)

        pull_btn_row = tk.Frame(pull_inner, bg=SURF)
        pull_btn_row.pack(fill="x", pady=(10, 0))
        pull_fb = tk.Label(pull_btn_row, text="", font=FB, bg=SURF, fg=SUCCESS)
        pull_fb.pack(side="right", padx=4)

        def _copy_pull():
            t = _pull_text[0]
            if not t:
                return
            self.clipboard_clear()
            self.clipboard_append(t)
            pull_fb.config(text="\u2713 Copied!")
            pull_fb.after(2000, lambda: pull_fb.config(text=""))

        def _clear_pull():
            clear_in()
            clear_out()
            pull_cb.set("\u2014 select token \u2014")
            _build_pull()

        self._btn(pull_btn_row, "Copy @PULL", _copy_pull, small=True).pack(side="left")
        tk.Frame(pull_btn_row, bg=SURF, width=8).pack(side="left")
        self._btn(pull_btn_row, "Clear", _clear_pull, small=True).pack(side="left")

        # ═════════════════════════════════════════════════════════════════════
        # Token rebuild — defined after all widgets exist so closures resolve
        # ═════════════════════════════════════════════════════════════════════
        def _refresh_pull_tokens():
            opts = ["\u2014 select token \u2014"] + self._sf_tokens
            pull_cb["values"] = opts
            if _pull_tok_var.get() not in self._sf_tokens:
                pull_cb.set("\u2014 select token \u2014")
            _build_pull()

        def _rebuild_tokens(*_):
            raw = tok_text.get("1.0", "end")
            seen = set()
            toks = []
            for line in raw.splitlines():
                t = "".join(
                    c for c in line.strip().upper()
                    if c.isalnum() or c == "_")
                if t and t not in seen:
                    seen.add(t)
                    toks.append(t)
            self._sf_tokens = toks

            # Rebuild chips
            for w in chip_row.winfo_children():
                w.destroy()
            if not toks:
                tk.Label(chip_row, text="No tokens yet",
                         font=(_SANS, 10, "italic"),
                         bg=SURF, fg=SUB).pack(side="left")
            else:
                for t in toks:
                    tk.Label(chip_row, text=t,
                             font=("Courier New", 10),
                             bg="#1e2636", fg="#5a9fd4",
                             padx=8, pady=2,
                             highlightbackground="#3a6090",
                             highlightthickness=1).pack(side="left", padx=(0, 5))

            # Rebuild asset block
            if toks:
                block = ("--- EPISODE ASSETS START ---\n"
                         + "\n".join("[{}]".format(t) for t in toks)
                         + "\n--- EPISODE ASSETS END ---")
                asset_var.set(block)
                asset_lbl.config(fg=TEXT)
            else:
                asset_var.set("Enter tokens above")
                asset_lbl.config(fg=SUB)

            _refresh_pull_tokens()

        tok_text.bind("<KeyRelease>", _rebuild_tokens)
        tok_text.bind("<<Paste>>",
                      lambda e: tok_text.after(10, _rebuild_tokens))

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