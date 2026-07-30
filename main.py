import os
import sys
import re
import json
import hashlib
import threading
import queue
import subprocess
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

# Streaming-merge diagnostics — when True, each Whisper segment emit
# and each render tick is logged as a JSON line to _pq_stream.log next
# to main.py.  Off by default; flip to True locally to inspect what
# the tiny/main streaming buffers and user-edit spans are doing.
# Never commit this set to True.
_PQ_STREAM_DIAG = False

# Import our custom modules!
from config import *
from config import _SANS
from utils import (basename, secs_tc, tc_secs, is_video, is_audio, is_media,
                   MEDIA_EXTS, VIDEO_EXTS, parse_dnd, run_hidden)
from parsers import (parse_script, parse_pt_session_text, dedupe_pt_tracks,
                     match_pt_clip_to_media, get_clip_base_name,
                     parse_aaf_session, match_source_to_video,
                     detect_sync_offset, verify_sync_at_offset)
from gui_components import VoBin, MediaPool, _SlimScrollbar, _FlatDropdown, _FlatProgressBar
import engines
from sync_preview import SyncPreviewDialog
from match_review import MatchReviewDialog
from aaf_workflow import AafWorkflowMixin


def _tx_count_chars(tx, a, b):
    """Wrap Text.count(a, b, 'chars') so callers always get an int.

    Python 3.14's Tk shipped a behaviour change: Text.count() returns
    None instead of (0,) when the two indices are equal (empty range).
    Older releases returned a 1-tuple unconditionally.  Treat None as
    zero and unwrap the tuple form."""
    try:
        r = tx.count(a, b, "chars")
    except tk.TclError:
        return 0
    if r is None:
        return 0
    if isinstance(r, (tuple, list)):
        return int(r[0]) if r else 0
    return int(r)

def _tx_count_displaylines(tx, a, b):
    """Same wrapper as _tx_count_chars but for display lines (post
    word-wrap).  Used by the streaming render's scroll preservation:
    capture the number of display lines above the viewport, restore
    with yview_scroll so the user stays the same DISTANCE from the
    top regardless of how much content gets appended below.  Returns
    0 on any failure or for empty ranges (e.g. caller was at top)."""
    try:
        r = tx.count(a, b, "displaylines")
    except tk.TclError:
        return 0
    if r is None:
        return 0
    if isinstance(r, (tuple, list)):
        return int(r[0]) if r else 0
    return int(r)


class App(AafWorkflowMixin, TkinterDnD.Tk if HAS_DND else tk.Tk):

    # ── Status display maps (single source of truth) ────────────────
    # Class-level so any code path that renders a status label uses
    # THE SAME copy — no risk of surgical-patch helpers drifting out
    # of sync with _step4's build-time copy.  Colour aliases (SUCCESS,
    # WARN, etc.) live at module scope, imported when this class is
    # defined, so the maps here can reference them safely.
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
        "provisional":    INFO,
        "transcribing":   INFO,
    }
    STATUS_LABEL = {
        "ok":             "✓  matched",
        "direct":         "✓  matched",
        "manual":         "✓  adjusted",
        "low_confidence": "⚠  low confidence",
        "no_match":       "✗  no match",
        "no_quote":       "–  no quote text",
        "error":          "✗  error",
        "no_file":        "✗  no file assigned",
        "not_run":        "–  not run",
        "cancelled":      "–  cancelled",
        "provisional":    "…  provisional",
        "transcribing":   "⧗  transcribing…",
    }

    # ── Status-bucket sets (single source of truth) ─────────────────
    # OK_STATUSES: auto-matched by the pipeline — count in the OK
    #   bucket at the top of Step 4.  Excludes "manual" because the
    #   user's edit changed it from an auto match to a hand-adjusted
    #   one; the OK bucket tracks the pipeline's own hit rate.
    # REVIEW_STATUSES: rows the user needs to look at (matched
    #   nothing, low confidence, errored out, not yet run).
    # SUCCESS_STATUSES: any status representing a resolved row —
    #   used by _make_status_popup to decide whether a right-click
    #   status change should also un-accept the row.  Includes
    #   "manual" because a hand-adjusted row is still resolved.
    # Semantically distinct — keep both.  Previously they were
    # inline tuples that drifted:  _decrement_confirmed included
    # "manual" in the OK bucket while _increment_confirmed didn't,
    # so un-accepting a "manual" row was inflating _ok_count.
    OK_STATUSES      = ("ok", "direct")
    REVIEW_STATUSES  = ("no_match", "error", "low_confidence", "not_run")
    SUCCESS_STATUSES = frozenset(("ok", "direct", "manual"))

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

        # Thread-safe UI queue — Python 3.14 no longer allows self.after() from
        # background threads, so workers post callbacks here and the main thread
        # drains the queue every 20 ms via _pump_ui().
        self._ui_q = queue.Queue()
        self._pump_ui()

        self.seq_name   = tk.StringVar()
        self.out_path   = tk.StringVar()
        self.gap_var    = tk.DoubleVar(value=DEFAULT_GAP)
        self.pad_var    = tk.IntVar(value=PAD_SECS)

        self._load_prefs()
        # Apply the user's saved Whisper model preference (if any) before
        # any transcription kicks off.  Without this, the engine would
        # fall back to the WHISPER_MODEL config default and ignore the
        # choice the user made in Settings on the previous session.
        _saved_model = self._prefs.get("whisper_model")
        if _saved_model:
            engines.set_active_model(_saved_model)
        # Seed bundled tiny into the persistent cache and kick off a
        # background download of base + small.  The installer ships
        # only tiny to stay slim; this populates the practical CPU
        # choices the user can pick from the dropdown without the
        # user paying a wait on their first selection.  Failures
        # (offline, partial download) are silent — faster-whisper
        # will simply retry the next time the user picks one.
        try:
            engines._seed_bundled_models()
        except Exception:
            pass
        # No background prefetch: only tiny is bundled / seeded.
        # Any other model size downloads on demand the first time
        # the user actually picks it in the model picker.
        self._header()
        self.body = tk.Frame(self, bg=BG)
        self.body.pack(fill="both", expand=True, padx=44, pady=(0, 14))

        # Global keyboard shortcuts — active on every screen
        self.bind_all("<Control-s>",       lambda e: self._quick_save(self._footer_save_btn))
        self.bind_all("<Control-S>",       lambda e: self._save_as())
        self.bind_all("<Control-o>",       lambda e: self._open_session())

        # Unsaved-work guard — signature of the last-saved (or freshly
        # loaded) state.  _on_app_close compares it to the live state and
        # prompts to save when they differ.
        self._saved_signature = None
        self.protocol("WM_DELETE_WINDOW", self._on_app_close)

        self._home()

    def _ui(self, cb):
        """Post cb onto the main-thread UI queue. Safe to call from any thread."""
        self._ui_q.put(cb)

    def _pump_ui(self):
        """Drain up to 20 queued callbacks per tick to avoid blocking the event loop."""
        try:
            for _ in range(20):
                cb = self._ui_q.get_nowait()
                try:
                    cb()
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.after(20, self._pump_ui)

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
        bar.pack(fill="x", padx=36, pady=(10, 8))

        # ── Left: persistent action buttons (Home / Save / Save As / Open) ─────
        btn_frame = tk.Frame(bar, bg=BG)
        btn_frame.pack(side="left")
        self._btn(btn_frame, "\u2302 HOME", self._home,
                  small=True).pack(side="left", padx=(0, 10))
        self._footer_save_btn = [None]
        def _hdr_save():
            self._quick_save(self._footer_save_btn)
        _fsb = self._btn(btn_frame, "SAVE", _hdr_save, small=True)
        _fsb.pack(side="left", padx=(0, 4))
        self._footer_save_btn[0] = _fsb
        self._btn(btn_frame, "SAVE AS", self._save_as,
                  small=True).pack(side="left", padx=(0, 4))
        self._btn(btn_frame, "OPEN",    self._open_session,
                  small=True).pack(side="left")

        # ── Right: MeatEater logo ─────────────────────────────────────────────
        # Must be packed before the expanding center frame.
        self._logo_photo = None
        _script_dir = os.path.dirname(os.path.abspath(__file__))
        _assets = os.path.join(_script_dir, "assets")
        logo_frame = tk.Frame(bar, bg=BG)
        logo_frame.pack(side="right")
        for _name in ("ME_HortLogo_OrgWht.png",
                      "meateater_logo.png",
                      "channels4_profile-d0f26706-7f7c-46be-b8be-cd47fd3401bf.png"):
            _path = os.path.join(_assets, _name)
            if os.path.isfile(_path):
                try:
                    from tkinter import PhotoImage
                    self._logo_photo = PhotoImage(file=_path)
                    _h = self._logo_photo.height()
                    if _h > 50:
                        div = max(1, _h // 50)
                        self._logo_photo = self._logo_photo.subsample(div, div)
                    tk.Label(logo_frame, image=self._logo_photo, bg=BG).pack()
                    break
                except Exception:
                    pass
        if self._logo_photo is None:
            tk.Label(logo_frame, text="MEATEATER",
                     font=("Courier New", 9, "bold"),
                     bg=BG, fg=ACCENT).pack()

        # ── Center: PostBridge title + subtitle ───────────────────────────────
        brand_frame = tk.Frame(bar, bg=BG)
        brand_frame.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(brand_frame, bg=BG)
        inner.pack(expand=True)   # centers within the remaining space

        # "POST" orange + "BRIDGE" white, mirroring the MEAT/EATER split
        title_row = tk.Frame(inner, bg=BG)
        title_row.pack()
        tk.Label(title_row, text="POST",
                 font=(_SANS, 18, "bold"), bg=BG, fg=ACCENT, padx=0).pack(side="left")
        tk.Label(title_row, text="BRIDGE",
                 font=(_SANS, 18, "bold"), bg=BG, fg=TEXT, padx=0).pack(side="left")

        tk.Label(inner, text="audio/video post-production bridge",
                 font=(_SANS, 9, "italic"), bg=BG, fg=SUB, pady=0).pack()

        _dep_warnings = []
        if not HAS_WHISPER:
            _dep_warnings.append("pip install faster-whisper")
        if not HAS_DND:
            _dep_warnings.append("pip install tkinterdnd2")
        if _dep_warnings:
            tk.Label(inner,
                     text="  [" + "  \u00b7  ".join(_dep_warnings) + "]",
                     font=("Courier New", 9), bg=BG, fg=WARN).pack(pady=(3, 0))

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

    # ── Model picker (inline dropdown widgets) ─────────────────────────────

    # Static description of every Whisper model.  Surfaced via the "?"
    # button next to each inline picker; previously lived in the modal
    # Settings dialog.  Keeping it here so all three pickers stay in
    # sync — every workflow shows the same table.
    _MODEL_INFO = [
        ("tiny",
         "39 M",   "~32× real-time", "~150 MB",
         "Draft quality.  Useful for fast first-pass on slow CPUs."),
        ("base",
         "74 M",   "~16× real-time", "~250 MB",
         "Fast and reasonably accurate.  Strong CPU choice."),
        ("small",
         "244 M",  "~6× real-time",  "~600 MB",
         "Default.  Balanced speed and accuracy on most hardware."),
        ("medium",
         "769 M",  "~2× real-time",  "~1.5 GB",
         "Higher accuracy.  Slow on CPU; comfortable on GPU."),
        ("large-v3",
         "1.55 B", "~1× real-time",  "~3 GB",
         "Best accuracy.  Practical mainly with GPU acceleration."),
    ]
    _MODEL_SIZES = [m[0] for m in _MODEL_INFO]

    def _build_model_picker(self, parent, bg=None):
        """Return a Frame containing 'Model:' + dropdown + '?' button.

        Wires the dropdown to apply the choice immediately (with the
        busy-transcription gate) and registers the resulting widget on
        self so _refresh_model_indicators() can update it after an
        external change.  Used in PQ project view, PQ session view and
        Script -> Session step 2.
        """
        if bg is None:
            bg = BG
        row = tk.Frame(parent, bg=bg)
        tk.Label(row, text="Model:", font=FB, bg=bg, fg=SUB
                 ).pack(side="left", padx=(0, 4))
        var = tk.StringVar(value=engines.get_active_model_size())
        opt = tk.OptionMenu(row, var, *self._MODEL_SIZES)
        # Fix the dropdown to a single width regardless of which model
        # is selected — without this the picker shrinks for "tiny" and
        # grows for "large-v3", causing the whole row to jitter on
        # change.  9 chars comfortably fits "large-v3" plus the chevron.
        opt.config(font=FB, bg=SURF2, fg=TEXT,
                    activebackground=ACCENT, activeforeground=TEXT,
                    highlightthickness=0, bd=0, padx=8, pady=2,
                    cursor="hand2", width=9, anchor="w",
                    indicatoron=True)
        opt["menu"].config(font=FB, bg=SURF2, fg=TEXT,
                            activebackground=ACCENT,
                            activeforeground=TEXT, bd=0)
        opt.pack(side="left")

        help_lbl = tk.Label(row, text="?", font=FBT,
                             bg=bg, fg=SUB, cursor="hand2",
                             padx=6, pady=2)
        help_lbl.pack(side="left")
        help_lbl.bind("<Enter>",
                       lambda e, w=help_lbl: w.config(fg=ACCENT))
        help_lbl.bind("<Leave>",
                       lambda e, w=help_lbl: w.config(fg=SUB))
        help_lbl.bind("<Button-1>",
                       lambda e: self._show_model_help())

        # Trace the dropdown — every user change runs through the gate.
        # We use a flag to prevent the trace re-firing when we revert
        # the var (e.g. user clicked BACK on the busy-swap dialog).
        guard = {"in_revert": False}

        def _on_change(*_):
            if guard["in_revert"]:
                return
            new_choice = var.get()
            if not new_choice or new_choice == engines.get_active_model_size():
                return
            old_choice = engines.get_active_model_size()
            if self._is_transcription_running():
                decision = self._prompt_busy_model_swap(
                    self, new_choice, old_choice)
                if decision == "back":
                    # Revert dropdown to the still-active model.
                    guard["in_revert"] = True
                    try:
                        var.set(old_choice)
                    finally:
                        guard["in_revert"] = False
                    return
                if decision == "cancel_current":
                    self._request_cancel_active_transcriptions()
            engines.set_active_model(new_choice)
            self._prefs["whisper_model"] = new_choice
            self._save_prefs()
            self._refresh_model_indicators()

        var.trace_add("write", _on_change)

        # Register the var on self so _refresh_model_indicators can
        # sync it after an external change (currently no external
        # changers, but future progressive-transcribe code may flip
        # the model briefly to tiny).
        if not hasattr(self, "_model_picker_vars"):
            self._model_picker_vars = []
        self._model_picker_vars.append(var)
        return row

    def _show_model_help(self):
        """Pop a non-modal-feeling help dialog explaining each Whisper
        model size.  Triggered by the '?' button on every inline picker."""
        win = tk.Toplevel(self)
        win.title("Whisper Models")
        win.configure(bg=BG)
        win.transient(self); win.grab_set()
        win.resizable(False, False)

        hdr = tk.Frame(win, bg=ACCENT, padx=16, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Whisper Model Sizes",
                 font=FBT, bg=ACCENT, fg="#1c1c1c").pack(anchor="w")

        body = tk.Frame(win, bg=BG, padx=20, pady=14)
        body.pack(fill="both", expand=True)

        tk.Label(body,
                 text=("Larger = more accurate but slower.  "
                       "Speeds are CPU; GPU is ~5–15× faster."),
                 font=FB, bg=BG, fg=SUB,
                 justify="left", wraplength=900
                 ).pack(anchor="w", pady=(0, 14))

        tbl = tk.Frame(body, bg=BG)
        tbl.pack(fill="x", pady=(0, 8))
        for col, txt in enumerate(
                ["Model", "Params", "Speed (CPU)",
                 "RAM / VRAM", "Notes"]):
            tk.Label(tbl, text=txt,
                     font=("Courier New", 10, "bold"),
                     bg=BG, fg=SUB, anchor="w"
                     ).grid(row=0, column=col, sticky="w",
                            padx=(0, 14), pady=(0, 6))
        # Generous row padding (10 px each side) keeps the wrapped
        # 2-line notes visually separated from the next row's notes.
        # Without this they butt right against each other and read as
        # one continuous block of text.
        _row_pady = (10, 10)
        for r, (sz, p, sp, mem, note) in enumerate(
                self._MODEL_INFO, start=1):
            tk.Label(tbl, text=sz,
                     font=("Courier New", 11, "bold"),
                     bg=BG, fg=ACCENT, anchor="nw"
                     ).grid(row=r, column=0, sticky="nw",
                            padx=(0, 14), pady=_row_pady)
            for i, val in enumerate([p, sp, mem], start=1):
                tk.Label(tbl, text=val, font=FB, bg=BG, fg=TEXT,
                         anchor="nw"
                         ).grid(row=r, column=i, sticky="nw",
                                padx=(0, 14), pady=_row_pady)
            tk.Label(tbl, text=note, font=FB, bg=BG, fg=SUB,
                     anchor="nw", justify="left", wraplength=480
                     ).grid(row=r, column=4, sticky="nw",
                            pady=_row_pady)

        tk.Label(body,
                 text=("Bundled: tiny, base, small.  "
                       "Medium and large-v3 download on first use."),
                 font=FB, bg=BG, fg=SUB,
                 justify="left", wraplength=900
                 ).pack(anchor="w", pady=(8, 0))

        nav = tk.Frame(win, bg=BG)
        nav.pack(fill="x", padx=20, pady=(0, 14))
        self._btn(nav, "CLOSE", win.destroy, width=14
                  ).pack(side="right")
        win.bind("<Escape>", lambda e: win.destroy())

        # Wider default so the notes column has room to breathe instead
        # of wrapping every line.  Caller can drag tighter if they want.
        self._center_dialog(win, 1080, 420)

    def _is_transcription_running(self):
        """Return True if any transcription is currently in flight.
        Used to gate the Settings dialog model swap — applying a new
        model mid-transcription can segfault the CTranslate2 backend
        (confirmed crash with small -> tiny swap on GPU)."""
        # Pull Quotes: every session carries a _progress.active flag
        # while its Whisper worker is running.
        proj = getattr(self, "_pq_project", None)
        if proj is not None:
            for s in proj.get("sessions", []):
                if (s.get("_progress") or {}).get("active"):
                    return True
        # Script -> Session: tracked by the global busy flag the
        # reconcile launcher sets (see _start_reconcile / _finish_reconcile).
        if getattr(self, "_reconcile_busy", False):
            return True
        return False

    def _prompt_busy_model_swap(self, parent, new_choice, current_choice):
        """Three-button modal shown when the user applies a model change
        while a transcription is in flight.  Returns one of:

            "back"           — don't change anything; close settings
            "apply_next"     — apply on next transcription only; let
                                the current one keep using its model
            "cancel_current" — request cancellation of in-flight work,
                                then apply the new model now
        """
        win = tk.Toplevel(parent)
        win.title("Transcription in Progress")
        win.configure(bg=BG)
        win.transient(parent); win.grab_set()
        win.resizable(False, False)

        hdr = tk.Frame(win, bg=WARN, padx=16, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="⚠   Transcription in Progress",
                 font=FBT, bg=WARN, fg="#1c1c1c").pack(anchor="w")

        body = tk.Frame(win, bg=BG, padx=20, pady=14)
        body.pack(fill="both", expand=True)
        tk.Label(body,
                 text=("A transcription is running with '{}'.\n"
                       "How do you want to switch to '{}'?".format(
                           current_choice, new_choice)),
                 font=FB, bg=BG, fg=TEXT, justify="left",
                 wraplength=540).pack(anchor="w", pady=(0, 12))

        result = {"choice": "back"}

        def _pick(c):
            result["choice"] = c
            win.destroy()

        # All three buttons pack right-aligned in one tight group with
        # uniform 8 px gaps.  Reading from left to right:
        #   BACK   APPLY ON NEXT RUN   CANCEL & APPLY NOW
        nav = tk.Frame(win, bg=BG)
        nav.pack(fill="x", padx=20, pady=(0, 14))
        _W = 18
        self._btn(nav, "CANCEL & APPLY NOW",
                  lambda: _pick("cancel_current"),
                  color=ERR, width=_W
                  ).pack(side="right")
        self._btn(nav, "APPLY ON NEXT RUN",
                  lambda: _pick("apply_next"),
                  color=ACCENT, width=_W
                  ).pack(side="right", padx=(0, 8))
        self._btn(nav, "BACK", lambda: _pick("back"),
                  width=_W).pack(side="right", padx=(0, 8))
        win.bind("<Escape>", lambda e: _pick("back"))

        win.update_idletasks()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        ww = max(620, win.winfo_reqwidth())
        wh = max(320, win.winfo_reqheight())
        win.geometry("{}x{}+{}+{}".format(
            ww, wh,
            px + max(0, (pw - ww) // 2),
            py + max(0, (ph - wh) // 2)))
        self.wait_window(win)
        return result["choice"]

    def _request_cancel_active_transcriptions(self):
        """Best-effort cancellation of every in-flight transcription.

        Script -> Session: signals self._cancel, which the reconcile
        workers poll regularly and bail out within seconds.

        Pull Quotes: sets a per-session threading.Event that's polled
        between Whisper segments inside engines.transcribe_clip_verbatim
        and transcribe_session_per_track.  CTranslate2's C++ decode
        can't be interrupted mid-segment, so the user sees up to ~5-15 s
        of lag (one segment) on cancel — but the GPU is freed and the
        worker exits cleanly with no transcript saved.
        """
        proj = getattr(self, "_pq_project", None)
        if proj is not None:
            for s in proj.get("sessions", []):
                if (s.get("_progress") or {}).get("active"):
                    s["_cancel_requested"] = True
                    ev = s.get("_cancel_event")
                    if ev is not None:
                        try:
                            ev.set()
                        except Exception:
                            pass
        if getattr(self, "_reconcile_busy", False):
            try:
                self._cancel.set()
            except Exception:
                pass

    def _refresh_model_indicators(self):
        """Push the active model name into every visible picker dropdown.
        Safe to call when none exist."""
        name = engines.get_active_model_size()
        for var in getattr(self, "_model_picker_vars", []):
            try:
                if var.get() != name:
                    var.set(name)
            except tk.TclError:
                pass

    def _show_settings_dialog(self):
        """Backwards-compat shim — kept in case anything still calls
        the old modal entry point.  The inline dropdown pickers
        replaced it; this just opens the help popup instead."""
        self._show_model_help()

    def _home(self):
        # Reset session-specific state so a new workflow starts clean.
        # Halt any Pull Quotes playback before tearing down.
        try:
            self._pq_stop_playback()
        except Exception:
            pass
        self._current_session_file = None
        self._export_fmt           = None
        self._restore_s4           = False
        self.workflow              = None
        self._pending_results      = None
        # Clear AAF pool so re-entering the workflow starts blank
        self._aaf_video_paths = []
        self._aaf_audio_paths = []
        self._clear()
        tk.Frame(self.body, bg=BG, height=30).pack()

        tk.Label(self.body, text="Choose a workflow",
                 font=FBT, bg=BG, fg=SUB).pack(pady=(0, 16))

        # ── Resume last project (shown only when a previous script is known) ──
        _last_script = self._prefs.get("last_script", "")
        if _last_script and os.path.isfile(_last_script):
            _ls_name = os.path.splitext(os.path.basename(_last_script))[0]
            resume_row = tk.Frame(self.body, bg=SURF,
                                  highlightbackground=BORDER, highlightthickness=1)
            resume_row.pack(fill="x", padx=20, pady=(0, 16))
            tk.Frame(resume_row, bg=ACCENT, width=4).pack(side="left", fill="y")
            _ri = tk.Frame(resume_row, bg=SURF)
            _ri.pack(fill="x", padx=24, pady=10)
            tk.Label(_ri, text="Resume last project",
                     font=(_SANS, 11, "bold"), bg=SURF, fg=SUB).pack(side="left")
            tk.Label(_ri, text="  —  " + _ls_name,
                     font=(_SANS, 11), bg=SURF, fg=TEXT).pack(side="left")
            _ra = tk.Label(_ri, text="\u2192", font=(_SANS, 16, "bold"),
                           bg=SURF, fg=BORDER, padx=8)
            _ra.pack(side="right")
            _rw = [resume_row, _ri, _ra]
            def _resume_enter(e, ws=_rw, c=resume_row, a=_ra):
                for w in ws: w.config(bg="#303030")
                c.config(highlightbackground=ACCENT); a.config(fg=ACCENT)
            def _resume_leave(e, ws=_rw, c=resume_row, a=_ra):
                for w in ws: w.config(bg=SURF)
                c.config(highlightbackground=BORDER); a.config(fg=BORDER)
            def _resume_click(e, sp=_last_script):
                self.workflow = "script_session"
                self._export_fmt = None
                self._load_script(sp)
            for _lbl in _ri.winfo_children(): _rw.append(_lbl)
            for _w in _rw:
                _w.bind("<Enter>",    _resume_enter)
                _w.bind("<Leave>",    _resume_leave)
                _w.bind("<Button-1>", _resume_click)

        cards_frame = tk.Frame(self.body, bg=BG)
        cards_frame.pack(fill="x", padx=20)

        _TITLE_FONT = (_SANS, 15, "bold")
        _DESC_FONT  = (_SANS, 11)
        _ARR_FONT   = (_SANS, 20, "bold")
        hover_bg    = "#303030"

        workflows = [
            ("Pull Quotes",
             "pull_quotes",
             "Transcribe interview sessions, browse the transcripts, and "
             "copy passages as ready-to-paste @PULL blocks for your script.",
             HAS_WHISPER),
            ("Format Script",
             "script_formatter",
             "Build @PART, @VO, and @PULL blocks and copy them into your script.",
             True),
            ("Reconcile Script \u2192 Session",
             "script_session",
             "Whisper-reconcile a script and export as AAF or XML \u2014 "
             "format is chosen at the export step.",
             HAS_WHISPER),
            ("Convert AAF \u2192 XML",
             "pt_xml",
             "Parse an AAF, match clips to video files by source name, "
             "and generate XML.",
             HAS_AAF),
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
            "Reformat the following script for PostBridge.  Follow these "
            "rules exactly.\n\n"
            "ALL HEADERS USE BRACKETS AT COLUMN 0.  THREE TYPES:\n\n"
            "  [PART <name>]                          ← section marker\n"
            "  [VO <vo_id>]                           ← voice-over block\n"
            "  [<TOKEN> HH:MM:SS-HH:MM:SS]            ← interview pull\n\n"
            "OPTIONAL TOKEN DECLARATION BLOCK at the top of the script:\n\n"
            "  [TOKENS]\n"
            "  JENA\n"
            "  DON\n"
            "  [/TOKENS]\n\n"
            "  • Token names: UPPERCASE letters, digits, underscores "
            "(e.g. JOHN_SMITH).\n"
            "  • Tokens are auto-registered the first time they "
            "appear in a `[…]` header — the [TOKENS] block is optional.\n"
            "  • VO ids: PART_N_NARRATOR (e.g. PART_0_NARRATOR, "
            "PART_1_NARRATOR).\n\n"
            "BODY RULES:\n\n"
            "  • Header lines (anything in `[…]`) MUST be at column 0.\n"
            "  • The text under each header is the block's content.\n"
            "  • BLANK LINES inside a block ARE preserved as paragraph "
            "breaks — feel free to use them.\n"
            "  • The next block starts only when another `[…]` header "
            "appears at column 0.  Indented or non-header text is body.\n"
            "  • Timecodes must be HH:MM:SS (zero-padded).  Use a "
            "single hyphen between in/out (no space).\n"
            "  • Comments: `// comment` from a whitespace-prefixed "
            "`//` to end-of-line is ignored.\n"
            "  • Speaker prefixes like `JORDAN:` inside a pull's body "
            "are decorative — they don't change parsing.\n\n"
            "EXAMPLE:\n\n"
            "Episode Title\n\n"
            "[PART Cold Open]\n\n"
            "[VO PART_0_NARRATOR]\n"
            "Narrator's lead-in.  Standard prose.\n\n"
            "Second paragraph still in the same VO block.\n\n"
            "[JENA 00:01:23-00:01:45]\n"
            "JORDAN: Tell me about the trip.\n"
            "JENA: We went out at four in the morning.  The water was glass.\n\n"
            "Second paragraph still in the same pull, separated by a blank "
            "line.\n\n"
            "[JENA 00:02:14-00:02:30]\n"
            "Next pull from the same session.\n\n"
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
            _last = self._prefs.get("last_script", "")
            _init_dir = os.path.dirname(_last) if _last and os.path.isfile(_last) else None
            path = filedialog.askopenfilename(
                title="Open Script",
                initialdir=_init_dir,
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
        elif key == "pull_quotes":
            self._pq_open_home()

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

    def _btn(self, parent, label, cmd, small=False, color=None,
              width=None):
        # `width` is in characters and gives all buttons in a row the
        # same visual width when their labels differ.  Use it whenever
        # several buttons appear together so they align cleanly instead
        # of each sizing to its own label.
        bg  = color or SURF3
        pad = (8,4) if small else (16,8)
        kw  = dict(text=label,
                    font=FB if small else FBT,
                    bg=bg, fg=TEXT,
                    cursor="arrow", padx=pad[0], pady=pad[1],
                    bd=0, highlightbackground=BORDER, highlightthickness=1,
                    anchor="center")
        if width is not None:
            kw["width"] = width
        w   = tk.Label(parent, **kw)
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

    def _center_dialog(self, win, min_w=560, min_h=420):
        """Size a Toplevel to its requested size (no smaller than
        min_w × min_h) and centre it over the main window."""
        win.update_idletasks()
        pw = self.winfo_width();  ph = self.winfo_height()
        px = self.winfo_rootx();  py = self.winfo_rooty()
        ww = max(min_w, win.winfo_reqwidth())
        wh = max(min_h, win.winfo_reqheight())
        win.geometry("{}x{}+{}+{}".format(
            ww, wh,
            px + max(0, (pw - ww) // 2),
            py + max(0, (ph - wh) // 2)))

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
            # Don't scroll when focus is in a popup (e.g. combobox dropdown).
            # Wrapped in try/except: Tk's focus_get() raises KeyError on
            # internal helper widgets like a ttk.Combobox's "popdown" — the
            # widget name exists in Tk-land but isn't registered in Python
            # children mappings.  We treat that as "not in our toplevel".
            try:
                w = self.focus_get()
                if w is not None and w.winfo_toplevel() != self:
                    return
            except (KeyError, tk.TclError):
                pass
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

    # ── Persistent user prefs (last script dir, etc.) ─────────────────────────
    def _prefs_path(self):
        return os.path.join(os.path.expanduser("~"), ".postbridge_prefs.json")

    def _load_prefs(self):
        try:
            with open(self._prefs_path(), encoding="utf-8") as f:
                self._prefs = json.load(f)
        except Exception:
            self._prefs = {}

    def _save_prefs(self):
        try:
            with open(self._prefs_path(), "w", encoding="utf-8") as f:
                json.dump(self._prefs, f, indent=2)
        except Exception:
            pass

    def _load_script(self, path):
        def _clear_pending_restore():
            # _open_session arms these for THIS load.  Every early-return
            # path below MUST clear them, or they survive into the next
            # script the user opens — restoring AAF#1's saved setup onto
            # AAF#2, or marking the next freshly-opened file falsely
            # clean (mirrors the _aaf_load failure-path fix).
            self._pending_setup       = None
            self._pending_results     = None
            self._pending_s4_state    = None
            self._pending_mark_saved  = False
            # _confirmed_carryover / _rereconcile_src_override are
            # mutating session state — leaving them populated would
            # corrupt the next session's reconcile.
            self._confirmed_carryover     = {}
            self._rereconcile_src_override = {}

        if not path or not os.path.isfile(path):
            _clear_pending_restore()
            return
        self._script_path = os.path.abspath(path)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception as e:
            _clear_pending_restore()
            messagebox.showerror("Error", str(e)); return

        tokens, parts, pulls, vo_blocks, doc_title, warnings = parse_script(text)

        if not pulls:
            _clear_pending_restore()
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

        # Remember this script so the next file-dialog opens in the right place
        # and the home screen can offer a one-click resume.
        self._prefs["last_script"] = self._script_path
        self._save_prefs()

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
            self._ui(self._step2)

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

        # ── Confirmed-carryover banner (shown when coming back via ← BACK) ────
        _carryover = getattr(self, "_confirmed_carryover", None) or {}
        if _carryover:
            _n = len(_carryover)
            _co_frm = tk.Frame(self.body, bg=SURF, pady=6, padx=10)
            _co_frm.pack(fill="x", pady=(0, 10))
            tk.Label(_co_frm,
                     text="ℹ  {} approved edit{} from the previous run will be "
                          "preserved — only untouched rows will be re-reconciled.".format(
                              _n, "s" if _n != 1 else ""),
                     font=FB, bg=SURF, fg=INFO,
                     wraplength=760, justify="left").pack(side="left",
                                                          fill="x", expand=True)
            def _clear_carryover(frm=_co_frm):
                self._confirmed_carryover = {}
                frm.destroy()
            tk.Button(_co_frm, text="Run All Fresh", command=_clear_carryover,
                      bg=SURF3, fg=SUB, font=FB, relief="flat",
                      padx=8, pady=2, cursor="hand2").pack(side="right")

        # ── Nav pinned to bottom first so it's always visible ─────────────────
        cache_row = tk.Frame(self.body, bg=BG)
        cache_row.pack(side="bottom", fill="x", pady=(4,0))
        self._btn(cache_row, "CLEAR TRANSCRIPTS", lambda: self._clear_cache("transcripts"),
                  small=True).pack(side="right", padx=(0, 4))
        self._btn(cache_row, "CLEAR RESULTS", lambda: self._clear_cache("results"),
                  small=True).pack(side="right", padx=(0, 8))

        nav = tk.Frame(self.body, bg=BG)
        nav.pack(side="bottom", fill="x", pady=(8,0))

        # Consistent prev/next everywhere.  BACK to Step 1 on the left.
        # NEXT → REVIEW on the right; jumps into Step 4 IF results
        # already exist from a prior reconcile — otherwise nudges the
        # user to click RECONCILE first instead of silently doing
        # nothing.  RECONCILE stays as its own separate action so
        # peeking at Step 2 between reviews doesn't require re-running
        # a 20-minute pipeline.
        self._btn(nav, "← BACK", self._step1).pack(side="left")

        def _step2_next():
            if getattr(self, "results", None):
                self._step4()
            else:
                messagebox.showinfo(
                    "No results yet",
                    "There are no reconciled results to review.  "
                    "Click RECONCILE first to run the pipeline, then "
                    "come back and click NEXT.",
                    parent=self)
        self._btn(nav, "NEXT  →  REVIEW", _step2_next,
                  color=ACCENT).pack(side="right")

        self._btn(nav, "RECONCILE", self._start_reconcile
                  ).pack(side="right", padx=(0, 8))

        # Inline model picker just left of RECONCILE so the user can
        # change the Whisper size before kicking off the reconcile.
        self._build_model_picker(nav, bg=BG).pack(side="right",
                                                   padx=(0, 12))

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
            finally:
                self._pool._bulk_loading = False
            self._pool._refresh_count()
            del self._redo_setup

        # Apply pending setup from _open_session (home page "Open Session" card)
        if getattr(self, "_pending_setup", None):
            self._apply_setup_data(self._pending_setup)
            del self._pending_setup

        # Snapshot the freshly-applied assignments into _redo_setup so that a
        # future ← BACK from Step 4 can restore them — without this, opening
        # a session that has cached results would jump straight to Step 4,
        # bypass the usual reconcile-time snapshot at line 2622, and lose
        # every token/asset assignment on BACK.
        if (self._pool and self._pool._rows
                and not getattr(self, "_redo_setup", None)):
            self._redo_setup = {
                "assignments": [(r["path"], r["var"].get())
                                for r in self._pool._rows],
            }

        # If results were saved and loaded, skip straight to Step 4
        if getattr(self, "_pending_results", None) is not None:
            self.results = self._pending_results
            del self._pending_results
            self.after(50, self._step4)
        elif getattr(self, "_pending_mark_saved", False):
            # Opened a session that lands at Step 2 (no saved Step 4
            # state).  The restore is now complete and unchanged, so
            # capture it as the clean baseline for the close prompt.
            self._pending_mark_saved = False
            self._mark_saved()

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

    def _conform_baseline_path(self):
        """Sidecar that captures the conform output exactly as it was
        the moment reconcile finished — before any user edits.  Pairs
        with _s4_snapshot() (current state) to produce the user-edits
        diff in _user_edits_diff_path()."""
        cache = self._pb_cache_dir()
        if not cache:
            return None
        script_name = os.path.splitext(
            os.path.basename(self._script_path))[0]
        return os.path.join(cache, script_name + "_conform_baseline.json")

    def _user_edits_diff_path(self):
        """Sidecar holding the per-pull diff between the conform
        baseline (above) and the current Step 4 state.  Regenerated on
        every _s4_save() so a post-session audit shows exactly where
        the editor disagreed with the algorithm."""
        cache = self._pb_cache_dir()
        if not cache:
            return None
        script_name = os.path.splitext(
            os.path.basename(self._script_path))[0]
        return os.path.join(cache, script_name + "_user_edits.json")

    def _write_conform_baseline(self):
        """Snapshot self.results into the conform-baseline sidecar.
        Called exactly once per fresh reconcile, immediately after the
        worker finishes and BEFORE the user has any opportunity to
        edit anything — this is the algorithm's output as ground truth
        for the subsequent diff."""
        path = self._conform_baseline_path()
        if not path:
            return
        snap = {}
        for r in getattr(self, "results", []) or []:
            order = r.get("order")
            if order is None:
                continue
            entry = {
                "segments":   r.get("segments", []),
                "rec_in_tc":  r.get("rec_in_tc",  ""),
                "rec_out_tc": r.get("rec_out_tc", ""),
                "rec_in_s":   r.get("rec_in_s",   0.0),
                "rec_out_s":  r.get("rec_out_s",  0.0),
                "status":     r.get("status",     ""),
                "token":      r.get("token",      ""),
                "is_vo":      bool(r.get("is_vo", False)),
                "confidence": r.get("confidence", None),
                # Fresh reconcile output starts unflagged on both axes.
                "ignored":    False,
                "accepted":   False,
            }
            if "gap_after_s" in r:
                entry["gap_after_s"] = r["gap_after_s"]
            snap[str(order)] = entry
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(snap, f, indent=2)
        except Exception:
            pass

    def _compute_user_edits_diff(self):
        """Compare the current _s4_snapshot() to the conform baseline
        and write a structured diff sidecar.  Each pull where the user
        touched anything (segment boundaries, accept/ignore toggle,
        rec_in/rec_out adjustment) gets a per-field delta entry.

        Also computes summary counters so a quick read shows the
        aggregate scope of editor intervention (how many pulls touched,
        net seconds added/removed via segment edits, etc.).
        """
        bl_path   = self._conform_baseline_path()
        diff_path = self._user_edits_diff_path()
        if not bl_path or not diff_path:
            return
        if not os.path.isfile(bl_path):
            return
        try:
            with open(bl_path, "r", encoding="utf-8") as f:
                baseline = json.load(f)
        except Exception:
            return
        try:
            current = self._s4_snapshot()
        except Exception:
            return

        def _segs_total_dur(segs):
            try:
                return sum(max(0.0, float(s[1]) - float(s[0]))
                           for s in segs)
            except Exception:
                return 0.0

        pulls_changed = []
        for order, cur in current.items():
            base = baseline.get(order)
            if not base:
                continue
            deltas = []
            base_segs = base.get("segments") or []
            cur_segs  = cur.get("segments")  or []
            if base_segs != cur_segs:
                base_dur = _segs_total_dur(base_segs)
                cur_dur  = _segs_total_dur(cur_segs)
                deltas.append({
                    "field":            "segments",
                    "before":           base_segs,
                    "after":            cur_segs,
                    "n_segs_delta":     len(cur_segs) - len(base_segs),
                    "duration_delta_s": round(cur_dur - base_dur, 3),
                })
            for k in ("rec_in_s", "rec_out_s"):
                try:
                    delta_s = float(cur.get(k, 0.0)) - float(
                        base.get(k, 0.0))
                except Exception:
                    delta_s = 0.0
                # Ignore sub-50 ms wiggle (numeric noise from save/load
                # round-trips); only real human-noticeable boundary
                # adjustments count.
                if abs(delta_s) > 0.05:
                    deltas.append({
                        "field":   k,
                        "before":  base.get(k),
                        "after":   cur.get(k),
                        "delta_s": round(delta_s, 3),
                    })
            for k in ("accepted", "ignored"):
                if cur.get(k) != base.get(k):
                    deltas.append({
                        "field":  k,
                        "before": base.get(k),
                        "after":  cur.get(k),
                    })
            if deltas:
                pulls_changed.append({
                    "order":      int(order),
                    "token":      base.get("token", ""),
                    "is_vo":      base.get("is_vo", False),
                    "status":     base.get("status", ""),
                    "confidence": base.get("confidence"),
                    "deltas":     deltas,
                })

        summary = {
            "n_pulls_total":   len(baseline),
            "n_pulls_changed": len(pulls_changed),
            "n_segment_edits": sum(
                1 for p in pulls_changed for d in p["deltas"]
                if d["field"] == "segments"),
            "n_boundary_edits": sum(
                1 for p in pulls_changed for d in p["deltas"]
                if d["field"] in ("rec_in_s", "rec_out_s")),
            "n_accepts": sum(
                1 for p in pulls_changed for d in p["deltas"]
                if d["field"] == "accepted" and d.get("after")),
            "n_ignores": sum(
                1 for p in pulls_changed for d in p["deltas"]
                if d["field"] == "ignored" and d.get("after")),
            "net_duration_delta_s": round(sum(
                d.get("duration_delta_s", 0)
                for p in pulls_changed for d in p["deltas"]
                if d["field"] == "segments"), 3),
        }
        out = {"summary": summary, "pulls": pulls_changed}
        try:
            with open(diff_path, "w", encoding="utf-8") as f:
                json.dump(out, f, indent=2)
        except Exception:
            pass

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
        # Refresh the user-edits diff sidecar so a post-session audit
        # has the latest snapshot of where the editor adjusted the
        # algorithm's output.  Wrapped because we don't want a diff
        # failure to block the primary state save.
        try:
            self._compute_user_edits_diff()
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

    def _s4_x_toggle_ignore(self, event=None):
        """X key — toggle IGNORE on the most-recently-clicked Step 4 card."""
        # Ignore key presses that originate inside a text-entry widget so
        # that typing 'x' in a search box or entry field is never hijacked.
        # focus_get() can raise KeyError on Tk internal subwidgets (e.g. a
        # ttk.Combobox popdown); treat that as "no entry focused".
        try:
            focused = self.focus_get()
        except (KeyError, tk.TclError):
            focused = None
        if isinstance(focused, (tk.Entry, tk.Text)):
            return
        fn = getattr(self, "_s4_active_toggle", None)
        if fn is not None:
            fn()

    def _s4_reassign_dialog(self, res):
        """Open a picker letting the user swap this row's source file
        (and, for interview pulls, its token) from Step 4.  Covers the
        case the cross-token phrase search can't reach — the quote
        isn't findable in any other sidecar (typo, contraction drift,
        or the correct file was never transcribed).

        Interview pulls (default):
          Scope choice — "This pull only" or "Every pull in token".
          On apply: source_audio / token swapped; rec_in/out and segments
          reset to the script's authored TCs; _s4_accepted / _s4_ignored
          cleared; status → "provisional".

        VO blocks:
          No token dropdown (VO is bound to a script part), no scope
          (each VO block is independent).  On apply: takes_data is
          rebuilt as a single-entry list pointing at the new file,
          best_take_index reset to 0, segments cleared so the user
          defines the new range in the waveform editor.
        """
        if res.get("is_vo"):
            self._s4_reassign_vo(res)
            return

        cur_tok  = res.get("token", "")
        cur_file = res.get("source_audio", "") or res.get("source_video", "") or ""

        # ── Collect available tokens and the WHOLE pool of files ───────
        # The file picker shows every audio file in the pool prefixed
        # with its currently-assigned token so the user can pick any of
        # them regardless of what's in the token dropdown — the two
        # controls are independent.  Was previously filtered to the
        # selected token's files only, which hid most of the pool.
        tokens = list(getattr(self, "tokens", []) or [])
        if cur_tok and cur_tok not in tokens:
            tokens = [cur_tok] + tokens
        _pool_entries = []          # (display_str, path) — assigned files first
        _path_by_display = {}       # display_str → path
        for row in getattr(self._pool, "_rows", []) or []:
            _t = row.get("var").get() if row.get("var") else ""
            _p = row.get("path") or ""
            if not _p or is_video(_p):
                continue            # audio-only for interview reassign
            _tok_lbl = _t if (_t and _t != "— unassigned —") else "unassigned"
            _display = "[{}]  {}".format(_tok_lbl, os.path.basename(_p))
            # Disambiguate collisions on basename by tacking on a
            # short parent-dir hint (rare but real for card_A/C0001.wav
            # + card_B/C0001.wav multicam pools).
            if _display in _path_by_display and _path_by_display[_display] != _p:
                _display = "[{}]  {}  ({})".format(
                    _tok_lbl, os.path.basename(_p),
                    os.path.basename(os.path.dirname(_p)))
            _pool_entries.append((_display, _p))
            _path_by_display[_display] = _p
        # Sort: assigned tokens A→Z first, then unassigned; within
        # each group, by filename.
        _pool_entries.sort(key=lambda e: (
            e[0].startswith("[unassigned]"),
            e[0].lower(),
        ))

        dlg = tk.Toplevel(self)
        dlg.title("Reassign source")
        dlg.configure(bg=BG)
        dlg.transient(self); dlg.grab_set()
        dlg.resizable(True, True)
        dlg.minsize(1280, 480)

        tk.Label(dlg, text="Reassign this pull's source file and/or token.",
                 font=FB, bg=BG, fg=SUB, justify="left").pack(
                     anchor="w", padx=20, pady=(18, 4))
        _quote = (res.get("quote_text", "") or "").strip()
        if _quote:
            tk.Label(dlg, text="Quote: “{}”".format(
                        _quote if len(_quote) < 80 else _quote[:77] + "…"),
                     font=FB, bg=BG, fg=TEXT, justify="left",
                     wraplength=520).pack(anchor="w", padx=20, pady=(0, 12))

        # Token dropdown
        tk.Label(dlg, text="Token:", font=FB, bg=BG, fg=TEXT).pack(
            anchor="w", padx=20)
        tok_var = tk.StringVar(value=cur_tok)
        tok_cb  = ttk.Combobox(dlg, textvariable=tok_var, values=tokens,
                               state="readonly", font=FB, width=48)
        tok_cb.pack(padx=20, pady=(2, 12), fill="x")

        # File picker — a proper scrollable Listbox instead of a Combobox
        # so long basenames don't get clipped by widget width, and so it
        # visually resembles the pool's row-per-file layout Jordan sees
        # in Step 2.  Two-column-ish rendering (basename left, token
        # right) via space-padding on a fixed-width font.
        _BROWSE = "📁  Browse for a file not in the pool…"
        tk.Label(dlg, text="Source file  (all audio in the pool):",
                 font=FB, bg=BG, fg=TEXT).pack(anchor="w", padx=20)

        # Compute the pad width so basenames align in a "column".
        _bn_max = max([len(os.path.basename(_p)) for _d, _p in _pool_entries]
                       + [len(os.path.basename(cur_file)) if cur_file else 0,
                          20])
        _bn_pad = min(_bn_max, 60)   # cap so extreme names don't blow layout

        def _fmt_row(path, tok_lbl):
            bn  = os.path.basename(path)
            pad = " " * max(2, _bn_pad + 2 - len(bn))
            return "{}{}[{}]".format(bn, pad, tok_lbl)

        # Rebuild display strings using the two-column format; keep the
        # same _path_by_display mapping (regenerate to match new format).
        _path_by_display = {}
        _display_rows = []
        for _old_disp, _p in _pool_entries:
            # Recover the token label from the old "[TOK]  bn" prefix.
            _tok_lbl = _old_disp.split("]", 1)[0].lstrip("[") if _old_disp.startswith("[") else "?"
            _disp = _fmt_row(_p, _tok_lbl)
            # Disambiguate rare basename collisions by tacking parent-dir
            # onto the token-label side so the basename column stays clean.
            while _disp in _path_by_display and _path_by_display[_disp] != _p:
                _disp += "  · " + os.path.basename(os.path.dirname(_p))
            _display_rows.append((_disp, _p))
            _path_by_display[_disp] = _p

        # Alphabetize by basename so the list is scannable — display
        # strings already start with the basename, so a lower-case
        # sort of the display string sorts by filename directly.
        _display_rows.sort(key=lambda e: e[0].lower())

        # Current file: hoist to the top so Apply just works if the
        # user opened the dialog by accident.
        _cur_display = ""
        for i, (_disp, _p) in enumerate(_display_rows):
            if _p == cur_file:
                _cur_display = _disp
                _display_rows.insert(0, _display_rows.pop(i))
                break
        if cur_file and not _cur_display:
            _cur_display = _fmt_row(cur_file, "external")
            _display_rows.insert(0, (_cur_display, cur_file))
            _path_by_display[_cur_display] = cur_file

        # Scrollable Listbox.  Fixed-width font so the [TOK] column aligns.
        _lb_frame = tk.Frame(dlg, bg=BG)
        _lb_frame.pack(fill="both", expand=True, padx=20, pady=(2, 6))
        _lb = tk.Listbox(_lb_frame, font=("Consolas", 10),
                         activestyle="dotbox", height=12,
                         bg=SURF, fg=TEXT,
                         selectbackground=ACCENT, selectforeground=BG,
                         highlightthickness=1, highlightbackground=BORDER,
                         exportselection=False)
        _lb.pack(side="left", fill="both", expand=True)
        _lb_sb = tk.Scrollbar(_lb_frame, orient="vertical", command=_lb.yview)
        _lb_sb.pack(side="right", fill="y")
        _lb.configure(yscrollcommand=_lb_sb.set)
        for _disp, _p in _display_rows:
            _lb.insert("end", _disp)
        _lb.insert("end", _BROWSE)
        # Consume the mousewheel over the Listbox so it doesn't bubble
        # up to the Step 4 scroll canvas behind the dialog — otherwise
        # scrolling here scrolls the Step 4 review under the modal.
        def _lb_wheel(event, lb=_lb):
            lb.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return "break"
        _lb.bind("<MouseWheel>", _lb_wheel)
        # Preselect the current file's row so Apply just works.
        if _cur_display:
            _lb.selection_set(0)
            _lb.activate(0); _lb.see(0)

        # file_var holds the CURRENTLY-SELECTED display string so the
        # existing apply logic (below) can look it up via _path_by_display.
        file_var = tk.StringVar(value=_cur_display or "")

        def _on_lb_select(_e=None):
            sel = _lb.curselection()
            if not sel:
                return
            v = _lb.get(sel[0])
            if v == _BROWSE:
                from tkinter.filedialog import askopenfilename
                p = askopenfilename(
                    parent=dlg, title="Source file",
                    filetypes=[("Audio", "*.wav *.mp3 *.aif *.aiff *.flac *.m4a"),
                               ("Video", "*.mov *.mp4 *.mkv"),
                               ("All", "*.*")])
                if not p:
                    return
                _disp = _fmt_row(p, "browsed")
                _path_by_display[_disp] = p
                _lb.insert(0, _disp)
                _lb.selection_clear(0, "end")
                _lb.selection_set(0)
                _lb.activate(0)
                file_var.set(_disp)
            else:
                file_var.set(v)
        _lb.bind("<<ListboxSelect>>", _on_lb_select)
        _lb.bind("<Double-Button-1>", lambda e: (_on_lb_select(), _apply()))

        # Scope
        scope_var = tk.StringVar(value="pull")
        sf = tk.Frame(dlg, bg=BG); sf.pack(anchor="w", padx=20, pady=(0, 4))
        tk.Label(sf, text="Scope:", font=FB, bg=BG, fg=TEXT).pack(side="left")
        for _v, _lbl in (("pull",  "This pull only"),
                         ("token", "Every pull in token “{}”".format(cur_tok))):
            tk.Radiobutton(sf, text=_lbl, variable=scope_var, value=_v,
                           font=FB, bg=BG, fg=TEXT, selectcolor=SURF2,
                           activebackground=BG, activeforeground=TEXT
                           ).pack(side="left", padx=(10, 0))
        # Honest note about export vs. review-only reach.  Token-scope
        # reassign registers _rereconcile_src_override so build_aaf uses
        # the new file; pull-scope stops at playback / the waveform
        # editor.  If you need per-pull export overrides, use token
        # scope after moving the pull to its own single-pull token.
        tk.Label(dlg,
                 text="Pull scope affects review/playback only.  "
                      "Token scope also updates the AAF/XML export source.",
                 font=FB, bg=BG, fg=SUB, justify="left",
                 wraplength=520).pack(anchor="w", padx=20, pady=(0, 12))

        result = {"ok": False}
        def _apply():
            _sel = file_var.get()
            if not _sel or _sel == _BROWSE:
                messagebox.showwarning("Pick a file",
                                       "Choose a source file first.",
                                       parent=dlg)
                return
            result["ok"] = True
            dlg.destroy()
        def _cancel():
            dlg.destroy()

        br = tk.Frame(dlg, bg=BG); br.pack(fill="x", padx=20, pady=(0, 16))
        self._btn(br, "Cancel", _cancel, small=True).pack(side="right")
        self._btn(br, "Apply", _apply, color=ACCENT,
                  small=True).pack(side="right", padx=(0, 8))

        dlg.update_idletasks()
        _px, _py = self.winfo_rootx(), self.winfo_rooty()
        _pw, _ph = self.winfo_width(), self.winfo_height()
        _dw, _dh = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        dlg.geometry("+{}+{}".format(_px + (_pw - _dw) // 2,
                                      _py + (_ph - _dh) // 2))
        self.wait_window(dlg)
        if not result["ok"]:
            return

        new_tok  = tok_var.get()
        # Display strings map back to real paths via _path_by_display.
        new_file = _path_by_display.get(file_var.get(), file_var.get())
        scope    = scope_var.get()

        # Identify affected rows
        if scope == "token":
            targets = [r for r in self.results if r.get("token") == cur_tok]
        else:
            targets = [res]

        # No undo push: _s4_snapshot captures TCs/accept-flags but not
        # token/source_audio/matched_text/words, so undoing a reassign
        # would leave the row half-restored and worse than the current
        # state.  Reassigns are terminal for now — a follow-up can
        # widen the snapshot if we want them undoable.

        # Token-scope reassigns propagate to the AAF/XML export via
        # _rereconcile_src_override: the export code pushes that file
        # to the front of int_assets[tok] at build time (main.py:6713)
        # so it becomes the token's primary track.  Pull-scope reassigns
        # only affect playback / the waveform editor — see the note in
        # the dialog when adding per-pull export overrides.
        if scope == "token" and new_tok == cur_tok:
            if not hasattr(self, "_rereconcile_src_override"):
                self._rereconcile_src_override = {}
            self._rereconcile_src_override[cur_tok] = new_file

        for r in targets:
            r["token"]         = new_tok
            r["source_audio"]  = new_file
            r.pop("source_video", None)
            if is_video(new_file):
                r["source_video"] = new_file
            # Reset to script-authored TCs — the previous reconciled
            # window was against a different file and can't be trusted.
            _oi = r.get("orig_in_s",  r.get("rec_in_s",  0.0))
            _oo = r.get("orig_out_s", r.get("rec_out_s", 0.0))
            r["rec_in_s"]  = _oi
            r["rec_out_s"] = _oo
            r["rec_in_tc"]  = r.get("in_tc",  "")
            r["rec_out_tc"] = r.get("out_tc", "")
            r["segments"]  = [(_oi, _oo)]
            r["words"]     = []
            r["matched_text"]  = r.get("quote_text", "")
            r["confidence"]    = 0.0
            r["status"]        = "provisional"
            r.pop("_s4_accepted",   None)
            r.pop("_s4_ignored",    None)
            r.pop("_original_status", None)

        self._s4_save()
        self._step4()

    def _s4_reassign_vo(self, res):
        """Reassign a VO block's source audio file.

        Unlike interview pulls, VO isn't a token-scoped concept — each
        VO block is bound to a specific script part and identified by
        its VO id.  The pool tracks VO audio under "VO: <part name>"
        tokens (see gui_components.py:1419), so the file picker is
        filtered to those rows for the block's part_index.

        On apply the block's takes_data is rebuilt as a single-entry
        list pointing at the new file, best_take_index reset to 0,
        source_audio + segments cleared so the waveform editor opens
        clean and the user defines the new range from scratch.  Flags
        and status reset the same way interview reassigns do.
        """
        pi = res.get("part_index", -1)
        # Look up the human-readable part name from the pool so the
        # dialog can label WHICH VO we're editing.  Falls back to the
        # numeric part_index if pool state isn't queryable.
        part_name = None
        try:
            for _p in getattr(self._pool, "_parts", []) or []:
                if _p.get("index") == pi:
                    part_name = _p.get("name")
                    break
        except Exception:
            pass
        part_label = part_name or ("Part {}".format(pi))

        # Gather EVERY audio file in the pool, labelled by its current
        # assignment.  Files already assigned to this VO part are
        # bubbled to the top, but the user can pick any file — VO
        # blocks that were mis-assigned in the pool need access to the
        # full list.
        _pool_entries = []
        _path_by_display = {}
        _this_vo_key = "VO: " + (part_name or "")
        for row in getattr(self._pool, "_rows", []) or []:
            _t = row.get("var").get() if row.get("var") else ""
            _p = row.get("path") or ""
            if not _p or is_video(_p):
                continue
            _tok_lbl = _t if (_t and _t != "— unassigned —") else "unassigned"
            _display = "[{}]  {}".format(_tok_lbl, os.path.basename(_p))
            if _display in _path_by_display and _path_by_display[_display] != _p:
                _display = "[{}]  {}  ({})".format(
                    _tok_lbl, os.path.basename(_p),
                    os.path.basename(os.path.dirname(_p)))
            _pool_entries.append((_display, _p, _t == _this_vo_key))
            _path_by_display[_display] = _p
        # Files assigned to THIS VO part first, then the rest alpha.
        _pool_entries.sort(key=lambda e: (not e[2], e[0].lower()))

        # Current audio for this VO — takes_data[best_i]["apath"] wins
        # if present; fall back to source_audio; then to nothing.
        _td = res.get("takes_data") or []
        _bi = res.get("best_take_index", 0) or 0
        cur_file = ""
        if 0 <= _bi < len(_td) and isinstance(_td[_bi], dict):
            cur_file = _td[_bi].get("apath", "") or ""
        if not cur_file:
            cur_file = res.get("source_audio", "") or ""

        dlg = tk.Toplevel(self)
        dlg.title("Reassign VO source")
        dlg.configure(bg=BG)
        dlg.transient(self); dlg.grab_set()
        dlg.resizable(True, True)
        dlg.minsize(1280, 480)

        tk.Label(dlg, text="Reassign this VO block's source audio file.",
                 font=FB, bg=BG, fg=SUB, justify="left").pack(
                     anchor="w", padx=20, pady=(18, 4))
        _quote = (res.get("quote_text", "") or "").strip()
        if _quote:
            tk.Label(dlg, text="VO ({}): “{}”".format(
                        part_label,
                        _quote if len(_quote) < 80 else _quote[:77] + "…"),
                     font=FB, bg=BG, fg=TEXT, justify="left",
                     wraplength=520).pack(anchor="w", padx=20, pady=(0, 12))

        _BROWSE = "📁  Browse for a file not in the pool…"
        tk.Label(dlg, text="Source file  (all audio in the pool):",
                 font=FB, bg=BG, fg=TEXT).pack(anchor="w", padx=20)

        # Two-column row layout (basename left, [TOK] right) matching
        # the interview dialog + the Step-2 pool visual.  See the same
        # comment in _s4_reassign_dialog for why we use a Listbox
        # instead of a Combobox.
        _bn_max = max([len(os.path.basename(_p)) for _d, _p, _m in _pool_entries]
                       + [len(os.path.basename(cur_file)) if cur_file else 0,
                          20])
        _bn_pad = min(_bn_max, 60)

        def _fmt_row(path, tok_lbl):
            bn  = os.path.basename(path)
            pad = " " * max(2, _bn_pad + 2 - len(bn))
            return "{}{}[{}]".format(bn, pad, tok_lbl)

        _path_by_display = {}
        _display_rows = []
        for _old_disp, _p, _mine in _pool_entries:
            _tok_lbl = _old_disp.split("]", 1)[0].lstrip("[") if _old_disp.startswith("[") else "?"
            _disp = _fmt_row(_p, _tok_lbl)
            while _disp in _path_by_display and _path_by_display[_disp] != _p:
                _disp += "  · " + os.path.basename(os.path.dirname(_p))
            _display_rows.append((_disp, _p))
            _path_by_display[_disp] = _p

        # Alphabetize by basename.  Display strings start with basename,
        # so a lower-case sort of the display sorts by filename.
        _display_rows.sort(key=lambda e: e[0].lower())

        _cur_display = ""
        for i, (_disp, _p) in enumerate(_display_rows):
            if _p == cur_file:
                _cur_display = _disp
                _display_rows.insert(0, _display_rows.pop(i))
                break
        if cur_file and not _cur_display:
            _cur_display = _fmt_row(cur_file, "external")
            _display_rows.insert(0, (_cur_display, cur_file))
            _path_by_display[_cur_display] = cur_file

        _lb_frame = tk.Frame(dlg, bg=BG)
        _lb_frame.pack(fill="both", expand=True, padx=20, pady=(2, 6))
        _lb = tk.Listbox(_lb_frame, font=("Consolas", 10),
                         activestyle="dotbox", height=12,
                         bg=SURF, fg=TEXT,
                         selectbackground=ACCENT, selectforeground=BG,
                         highlightthickness=1, highlightbackground=BORDER,
                         exportselection=False)
        _lb.pack(side="left", fill="both", expand=True)
        _lb_sb = tk.Scrollbar(_lb_frame, orient="vertical", command=_lb.yview)
        _lb_sb.pack(side="right", fill="y")
        _lb.configure(yscrollcommand=_lb_sb.set)
        for _disp, _p in _display_rows:
            _lb.insert("end", _disp)
        _lb.insert("end", _BROWSE)
        # Consume mousewheel so the Step-4 canvas behind the modal
        # doesn't scroll along with the Listbox.
        def _vo_lb_wheel(event, lb=_lb):
            lb.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return "break"
        _lb.bind("<MouseWheel>", _vo_lb_wheel)
        if _cur_display:
            _lb.selection_set(0)
            _lb.activate(0); _lb.see(0)

        file_var = tk.StringVar(value=_cur_display or "")

        def _on_lb_select(_e=None):
            sel = _lb.curselection()
            if not sel:
                return
            v = _lb.get(sel[0])
            if v == _BROWSE:
                from tkinter.filedialog import askopenfilename
                p = askopenfilename(
                    parent=dlg, title="VO source file",
                    filetypes=[("Audio", "*.wav *.mp3 *.aif *.aiff *.flac *.m4a"),
                               ("All", "*.*")])
                if not p:
                    return
                _disp = _fmt_row(p, "browsed")
                _path_by_display[_disp] = p
                _lb.insert(0, _disp)
                _lb.selection_clear(0, "end")
                _lb.selection_set(0)
                _lb.activate(0)
                file_var.set(_disp)
            else:
                file_var.set(v)
        _lb.bind("<<ListboxSelect>>", _on_lb_select)
        _lb.bind("<Double-Button-1>", lambda e: (_on_lb_select(), _apply()))

        tk.Label(dlg,
                 text="VO segments will be cleared — set the new IN/OUT "
                      "in the waveform editor after applying.",
                 font=FB, bg=BG, fg=SUB, justify="left",
                 wraplength=520).pack(anchor="w", padx=20, pady=(0, 12))

        result = {"ok": False}
        def _apply():
            if not file_var.get() or file_var.get() == _BROWSE:
                messagebox.showwarning("Pick a file",
                                       "Choose a source file first.",
                                       parent=dlg)
                return
            result["ok"] = True
            dlg.destroy()
        def _cancel():
            dlg.destroy()

        br = tk.Frame(dlg, bg=BG); br.pack(fill="x", padx=20, pady=(0, 16))
        self._btn(br, "Cancel", _cancel, small=True).pack(side="right")
        self._btn(br, "Apply", _apply, color=ACCENT,
                  small=True).pack(side="right", padx=(0, 8))

        dlg.update_idletasks()
        _px, _py = self.winfo_rootx(), self.winfo_rooty()
        _pw, _ph = self.winfo_width(), self.winfo_height()
        _dw, _dh = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        dlg.geometry("+{}+{}".format(_px + (_pw - _dw) // 2,
                                      _py + (_ph - _dh) // 2))
        self.wait_window(dlg)
        if not result["ok"]:
            return

        new_file = _path_by_display.get(file_var.get(), file_var.get())

        # Rebuild takes_data with the new single-entry take.  Segments
        # cleared so build_aaf's takes_data[best_i]["segments"] read at
        # engines.py:build_aaf-time skips this clip until the user
        # opens the waveform editor and sets a range.  source_audio
        # also updated because the Step-4 card + editor both read that
        # field first before takes_data (main.py:5860).
        res["takes_data"]      = [{"apath": new_file, "segments": []}]
        res["best_take_index"] = 0
        res["source_audio"]    = new_file
        res.pop("source_video", None)
        if is_video(new_file):
            res["source_video"] = new_file
        res["segments"]        = []
        res["rec_in_s"]        = 0.0
        res["rec_out_s"]       = 0.0
        res["rec_in_tc"]       = "00:00:00"
        res["rec_out_tc"]      = "00:00:00"
        res["matched_text"]    = ""
        res["confidence"]      = 0.0
        res.pop("_s4_accepted",   None)
        res.pop("_s4_ignored",    None)
        res.pop("_original_status", None)

        # Try to reconcile immediately against the new file's cached
        # transcript.  If none exists, kick off a background transcribe
        # + reconcile and mark the row "transcribing" so the user has
        # a visible indicator that work is happening.
        _cached_words, _blobs = engines.pb_transcript_load(new_file)
        if _cached_words is None:
            _cached_words = engines.cache_load(new_file)
            _blobs = None

        if _cached_words:
            # Cache hit — reconcile completes synchronously, and
            # results change enough (TCs, segments, status) that a
            # full rebuild is the honest way to reflect them.
            self._s4_reconcile_vo_row(res, new_file, _cached_words, _blobs)
            res.setdefault("status", "provisional")
            self._s4_save()
            try:
                _cv = getattr(self, "_s4_scroll_canvas", None)
                if _cv is not None:
                    self._s4_pending_scroll_frac = float(_cv.yview()[0])
            except Exception:
                pass
            self._step4()
        else:
            # Bg transcribe path — SKIP the full rebuild here.  A full
            # _step4() at this point produced the "screen blanks and
            # cycles through each row before returning" jitter Jordan
            # reported, because packing 200+ cards is not free.
            # Instead patch this ONE card's status label surgically
            # to "transcribing…"; the eventual completion callback in
            # _s4_transcribe_and_reconcile_vo will do the full rebuild
            # ONCE, when the results actually change.
            res["status"] = "transcribing"
            self._s4_patch_card_status(res)
            self._s4_save()
            threading.Thread(
                target=self._s4_transcribe_and_reconcile_vo,
                args=(res, new_file), daemon=True).start()

    def _s4_patch_card_status(self, res):
        """Surgical single-card status refresh — avoids a full _step4
        rebuild when only ONE row's status changed.  Looks up the
        res in self._rv and updates that entry's stat_lbl text +
        color using the class-level STATUS_LABEL / STATUS_COLOR
        maps (App.STATUS_*) — single source of truth so this can't
        drift out of sync with _step4's build-time rendering."""
        _rv = getattr(self, "_rv", None)
        if not _rv:
            return
        _new_st = res.get("status", "")
        for e in _rv:
            if e.get("res") is res:
                _lbl = e.get("stat_lbl")
                if _lbl:
                    try:
                        _lbl.config(
                            text=self.STATUS_LABEL.get(_new_st, _new_st),
                            fg=self.STATUS_COLOR.get(_new_st, SUB))
                    except tk.TclError:
                        pass
                break

    def _s4_reconcile_vo_row(self, res, audio_file, words, blobs=None):
        """Reconcile a single VO block against a cached transcript.

        Rebuilds a minimal `takes` list with one entry, calls
        engines.reconcile_vo_part on this block alone, then merges the
        winning take's segments / TCs / matched_text back onto `res`.
        Silent on error — the row just stays in whatever pre-call
        state it was in.
        """
        # Find the original VO block dict from self.vo_blocks so the
        # reconciler has the script text + gap flag to match against.
        vo_block = None
        _order = res.get("order")
        for vb in getattr(self, "vo_blocks", []) or []:
            if vb.get("order") == _order:
                vo_block = vb
                break
        if vo_block is None:
            return
        # Take tuple shape mirrors engines.reconcile_vo_part's unpack
        # at engines.py:3044 — (audio_words, v_offset, vpath, apath[, blobs]).
        take = (words, 0.0, None, audio_file,
                blobs if blobs is not None else [])
        try:
            new_results = engines.reconcile_vo_part([vo_block], [take])
        except Exception:
            return
        if not new_results:
            return
        nr = new_results[0]
        # Merge non-underscore keys onto res so _s4_accepted /
        # _s4_ignored / _original_status stay owned by Step 4 state.
        for k, v in nr.items():
            if not k.startswith("_"):
                res[k] = v
        # Guarantee status reflects success; reconcile_vo_part will
        # have already set it to ok / low_confidence / no_match.

    def _s4_transcribe_and_reconcile_vo(self, res, audio_file):
        """Background: transcribe `audio_file` (writing the sidecar so
        future runs are cache-hits), then reconcile this VO row against
        it.  Runs off the main thread; all UI updates route through
        self._ui() so Tk stays single-threaded."""
        try:
            words, blobs = engines.transcribe_file(audio_file)
        except Exception:
            def _fail():
                res["status"] = "error"
                res["matched_text"] = "transcription failed"
                # Surgical status patch — no full rebuild for an
                # error state change on a single row.
                self._s4_patch_card_status(res)
                self._s4_save()
            self._ui(_fail)
            return
        if not words:
            def _empty():
                res["status"] = "no_match"
                res["matched_text"] = "transcription empty"
                self._s4_patch_card_status(res)
                self._s4_save()
            self._ui(_empty)
            return
        # Persist so subsequent opens hit the sidecar cache.
        try:
            engines.pb_transcript_save(audio_file, words, blobs=blobs)
        except Exception:
            pass

        def _finish():
            self._s4_reconcile_vo_row(res, audio_file, words, blobs)
            self._s4_save()
            self._step4()
        self._ui(_finish)

    def _s4_redo_to_step2(self):
        """Go BACK to Step 2 while preserving user-approved Step 4 edits.

        Rows the user confirmed OR ignored are stashed in
        _confirmed_carryover so that _run_reconcile can pass them
        through untouched — both interview pulls (via
        process_token_pulls) and VO blocks (via process_vo_part).
        Only untouched rows will be re-reconciled, letting the user
        fix file assignments for problem tokens without losing any
        approved work.
        """
        carryover = {
            r["order"]: r
            for r in getattr(self, "results", [])
            if r.get("_s4_accepted") or r.get("_s4_ignored")
        }
        self._confirmed_carryover = carryover
        self._step2()

    def _build_setup_data(self):
        """Return a serialisable setup dict for the current state."""
        d = {
            "version":   1,
            "workflow":  getattr(self, "workflow", "script_xml"),
            "script":    getattr(self, "_script_path", ""),
            "assignments": {r["path"]: r["var"].get()
                            for r in self._pool._rows} if self._pool else {},
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

    # ── Unsaved-work guard ─────────────────────────────────────────────
    def _state_signature(self):
        """Hash of the meaningful, savable state for the active workflow,
        or None when a close could lose nothing (home screen, or a
        workflow that already auto-saves everything).

        Comparing this hash to the one captured at the last save/load is
        more robust than scattering dirty-flag updates through every
        mutation site — it can't drift out of sync with the real state.
        """
        wf = getattr(self, "workflow", None)
        try:
            if wf in ("script_session", "script_aaf"):
                if not getattr(self, "_script_path", None):
                    return None
                data = self._build_full_session_data()
            elif wf == "aaf_xml":
                if getattr(self, "_aaf_data", None) is None:
                    return None
                data = self._aaf_build_setup_data()
            elif wf == "pull_quotes":
                proj = getattr(self, "_pq_project", None)
                if not proj:
                    return None
                # Interview sessions auto-save their own JSON files on
                # every edit, so transcript/notes changes are never lost.
                # Only project-level membership (which sessions belong to
                # the project + the title) can be lost on close, so the
                # signature tracks just that — otherwise every transcript
                # keystroke would falsely read as "unsaved project".
                data = {
                    "title": proj.get("title", ""),
                    "sessions": sorted(
                        s.get("_file_path", "")
                        for s in proj.get("sessions", [])),
                    "file_path": proj.get("file_path"),
                }
            else:
                return None
            blob = json.dumps(data, sort_keys=True, default=str)
            return hashlib.md5(blob.encode("utf-8", "ignore")).hexdigest()
        except Exception:
            # Never let a signature failure block the close path; treat
            # an un-hashable state as "not dirty" so close still works.
            return None

    def _mark_saved(self):
        """Capture the current state as the clean baseline.  Call after
        any successful save or load."""
        self._saved_signature = self._state_signature()

    def _is_dirty(self):
        """True when there is meaningful state that differs from the last
        saved/loaded baseline."""
        sig = self._state_signature()
        if sig is None:
            return False
        return sig != getattr(self, "_saved_signature", None)

    def _on_app_close(self):
        """WM_DELETE_WINDOW handler — prompt to save unsaved work."""
        # In-flight work guard.  Transcription / reconcile run on worker
        # threads whose results aren't part of _state_signature until
        # they land, so the dirty-check below can't see them — closing
        # now would silently kill the worker and lose the run.  Warn
        # first.  (Checked before the dirty prompt so the user isn't
        # asked two questions when both apply — abandoning the run is
        # the bigger decision.)
        try:
            running = self._is_transcription_running()
        except Exception:
            running = False
        if running:
            if not messagebox.askyesno(
                    "Work in progress",
                    "A transcription is still running.\n\nClosing now will "
                    "abandon it and lose any results not yet saved.\n\n"
                    "Close anyway?",
                    default="no", icon="warning"):
                return
        try:
            dirty = self._is_dirty()
        except Exception:
            dirty = False
        if not dirty:
            self.destroy()
            return
        # Save / Don't Save / Cancel via the native yes/no/cancel box.
        resp = messagebox.askyesnocancel(
            "Unsaved changes",
            "You have unsaved changes.\n\nSave before closing?",
            default="yes", icon="warning")
        if resp is None:
            return                      # Cancel — keep the app open
        if resp:                        # Yes — save first
            try:
                self._quick_save()
            except Exception as e:
                messagebox.showerror("Save failed", str(e))
                return                  # stay open so work isn't lost
            # If the save was itself cancelled (e.g. user dismissed a
            # first-time Save As dialog), the state is still dirty —
            # don't close and discard the work.
            if self._is_dirty():
                return
        self.destroy()                  # No — discard and close

    def _build_full_session_data(self):
        """Return a complete session dict: setup + results + Step 4 state."""
        data = self._build_setup_data() if getattr(self, '_pool', None) else {}
        data["script"]   = getattr(self, "_script_path", "")
        data["workflow"] = getattr(self, "workflow", "script_xml")
        if getattr(self, '_rv', None):
            data["step4_state"] = self._s4_snapshot()
        # Persist any per-token source overrides set by re-reconcile so that
        # re-opening the session exports against the correct file automatically.
        _rr_ov = getattr(self, "_rereconcile_src_override", None)
        if _rr_ov:
            data["rereconcile_src_override"] = dict(_rr_ov)
        return data

    def _save_as(self):
        """Prompt for a file path, write a complete session JSON, and remember the path."""
        if getattr(self, 'workflow', None) == 'aaf_xml':
            self._aaf_save_setup()
            return
        if getattr(self, 'workflow', None) == 'pull_quotes':
            self._pq_save_episode_project(prompt_path=True)
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
            self._mark_saved()
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    def _quick_save(self, btn_ref=None):
        """Save to the current session file; prompt for a path on the first save."""
        def _flash_saved():
            # Show SAVED \u2713 only when a save actually happened \u2014 a
            # cancelled Save-As dialog must not read as success.
            b = btn_ref[0] if btn_ref else None
            if b:
                try:
                    b.config(text="SAVED \u2713")
                    self.after(1800, lambda: b.config(text="SAVE"))
                except Exception:
                    pass

        if getattr(self, 'workflow', None) == 'aaf_xml':
            if self._aaf_save_setup(prompt=False):
                _flash_saved()
            return
        if getattr(self, 'workflow', None) == 'pull_quotes':
            # Standalone-session view: opened a .pb_session.json directly
            # rather than a .pb_episode.json project, so the wrapper
            # project has no file_path of its own.  Ctrl+S should save
            # the session itself (which already has a _file_path) rather
            # than prompting for a brand-new project file every press.
            _proj = getattr(self, "_pq_project", None) or {}
            _sessions = _proj.get("sessions") or []
            ok = False
            if (not _proj.get("file_path")
                    and len(_sessions) == 1
                    and _sessions[0].get("_file_path")):
                try:
                    ok = bool(self._pq_save_session_file(_sessions[0]))
                except Exception:
                    ok = False
            else:
                ok = bool(self._pq_save_episode_project(prompt_path=False))
            if ok:
                _flash_saved()
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
            self._mark_saved()
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
        finally:
            self._pool._bulk_loading = False
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
        """Walk folder recursively and return {lowercase_filename: [absolute_path, …]}.
        Skips hidden directories (.pb_cache, .git, etc).

        Returns a LIST per basename instead of a single path so that
        multicam folders (where CARD_A/CLIP.MP4 and CARD_B/CLIP.MP4
        coexist) don't get silently collapsed by 'first match wins'.
        _remap_path then picks the best candidate by parent-folder
        suffix similarity to the saved old path."""
        index = {}
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in files:
                key = fn.lower()
                index.setdefault(key, []).append(os.path.join(root, fn))
        return index

    @staticmethod
    def _pick_best_remap(old_path, candidates):
        """Choose the candidate whose parent-folder tail best matches
        old_path's parent-folder tail.  Saved old paths carry enough
        identity (parent dirs) to disambiguate twin basenames; without
        this scoring, basename-only lookup silently mapped two saved
        references to the same physical file (reported when a foreign
        session was opened against a multicam root)."""
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        old_dirs = [d.lower() for d in os.path.normpath(old_path).split(os.sep) if d]
        best_path, best_score = None, -1
        for c in candidates:
            cdirs = [d.lower() for d in os.path.normpath(c).split(os.sep) if d]
            # Longest matching suffix among the *parent* directories
            # (drop the basename — every candidate shares it).
            old_parents = old_dirs[:-1]
            new_parents = cdirs[:-1]
            score = 0
            for a, b in zip(reversed(old_parents), reversed(new_parents)):
                if a == b:
                    score += 1
                else:
                    break
            if score > best_score:
                best_path, best_score = c, score
        return best_path

    @classmethod
    def _remap_path(cls, old_path, video_index, audio_index):
        """Return the remapped path for old_path, routing to the correct
        index by file type, with parent-folder disambiguation when
        multiple candidates share a basename."""
        fn  = os.path.basename(old_path).lower()
        ext = os.path.splitext(fn)[1]
        primary, secondary = (video_index, audio_index) \
            if ext in VIDEO_EXTS else (audio_index, video_index)
        return (cls._pick_best_remap(old_path, primary.get(fn) or [])
                or cls._pick_best_remap(old_path, secondary.get(fn) or [])
                or old_path)

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

        for r in data.get("results", []):
            for key in ("source_audio", "source_video"):
                p = r.get(key, "")
                if p: r[key] = cls._remap_path(p, video_index, audio_index)

    def _open_session(self, path=None):
        """Open a saved *_setup.json and jump straight to Step 2 with everything restored.

        When *path* is None (the default), prompt the user with a file dialog.
        When *path* is provided (e.g. from a CLI arg / Windows file association),
        load that file directly without prompting.
        """
        if path is None:
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
            self._pending_mark_saved = True  # clean baseline after restore
            self._pending_aaf_saved_path = path  # quick-saves return here
            self._aaf_load(aaf_path)
            return

        # ── Pull Quotes — Episode Project / Interview Session ────────────────
        wf = data.get("workflow", "")
        if wf == "episode_project":
            self.workflow = "pull_quotes"
            self._pq_load_project(data, file_path=path)
            return
        if wf == "interview_session":
            self.workflow = "pull_quotes"
            self._pq_open_standalone_session(data, file_path=path)
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
        # One-shot: capture a clean baseline once the (deferred) restore
        # finishes, so opening + closing an unchanged session doesn't
        # falsely prompt "unsaved changes".  Honored at the Step 2 or
        # Step 4 restore-completion point, then cleared.
        self._pending_mark_saved   = True

        # Stash setup data — _step2 will pick it up after the pool is built
        self._pending_setup = data

        # Stash Step 4 state if embedded in the session file
        self._pending_s4_state = data.get("step4_state") or None

        # Restore any re-reconcile source overrides so Step 5 uses the right files
        self._rereconcile_src_override = data.get("rereconcile_src_override") or {}

        # Only auto-jump to Step 4 when the session was explicitly saved there.
        # step4_state is written by _build_full_session_data only when _rv exists
        # (i.e. Step 4 is the current screen).  Sessions saved at Step 2 never
        # carry step4_state, so stale sidecar result files won't cause a jump.
        # Clear any stale carryover from a previous session so it can't
        # bleed into this freshly opened one.
        self._confirmed_carryover = {}

        if data.get("step4_state"):
            if data.get("results"):
                self._pending_results = data["results"]
            else:
                _cache_dir   = os.path.join(os.path.dirname(script_path), ".pb_cache")
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

    def _preflight_memory_check(self):
        """Return True to proceed, False to abort.

        Checks the smaller of (physical RAM free, commit charge headroom)
        and warns the user when allocations are likely to fail.  On
        Windows, commit charge (physical + page file) is the actual
        ceiling for new allocations — it's common to have 15 GB physical
        free while the page file is full and a 300 MB malloc fails."""
        budget = self._available_alloc_bytes()
        if budget is None:
            return True   # can't measure — don't block the user
        budget_gb = budget / (1024 ** 3)
        # 4 GB is the empirically derived "danger zone" — below this,
        # Whisper feature buffers or parallel ffmpeg subprocesses start
        # hitting MemoryError on contiguous allocations.
        if budget_gb >= 4.0:
            return True

        # Build a diagnostic that distinguishes "physical full" from
        # "commit charge exhausted" — they call for different fixes.
        phys = self._available_ram_bytes()
        page = self._available_pagefile_bytes()
        diag = []
        if phys is not None:
            diag.append("Physical RAM free: {:.1f} GB".format(
                phys / (1024 ** 3)))
        if page is not None:
            diag.append("Commit headroom:   {:.1f} GB".format(
                page / (1024 ** 3)))
            if page < 2 * (1024 ** 3):
                diag.append("(commit charge near limit — Windows will "
                            "refuse allocations regardless of physical "
                            "RAM free)")

        msg = (
            "Memory budget is tight — only {:.1f} GB available for new "
            "allocations.\n\n"
            "{}\n\n"
            "Whisper + ffmpeg need contiguous blocks of several hundred "
            "MB at a time.  When commit charge or physical RAM runs low, "
            "those allocations fail with MemoryError mid-run.\n\n"
            "Recommended:\n"
            "  •  Quit other memory-hungry apps (Pro Tools, browsers, "
            "Premiere, Photoshop)\n"
            "  •  If commit charge is the bottleneck, a Windows restart "
            "is the quickest fix\n"
            "  •  Toggle Background mode on the next screen to serialise "
            "the workload\n\n"
            "Continue anyway?"
        ).format(budget_gb, "\n".join("  " + d for d in diag))
        return messagebox.askyesno(
            "Low Memory Warning", msg, icon="warning")

    def _show_resource_help(self):
        """Open a small popup explaining what the resource-monitor numbers
        mean and how to act on them.  Triggered by the ⓘ icon in Step 3."""
        win = tk.Toplevel(self)
        win.title("Resource Monitor — Key")
        win.configure(bg=BG)
        win.transient(self); win.grab_set()
        win.resizable(False, False)

        hdr = tk.Frame(win, bg=ACCENT, padx=16, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Resource Monitor — Key",
                 font=FBT, bg=ACCENT, fg="#1c1c1c").pack(anchor="w")

        body = tk.Frame(win, bg=BG, padx=20, pady=14)
        body.pack(fill="both", expand=True)

        rows = [
            ("GPU 0–5% (steady)",
             "CUDA isn't engaged.  faster-whisper has fallen back to "
             "CPU mode — usually means torch isn't installed or the "
             "CUDA build is wrong for your GPU.  Reconcile still works "
             "but is 5–15× slower."),
            ("GPU 30–100%",
             "Whisper is actively transcribing on the GPU.  The number "
             "tracks how busy the compute units are.  Higher = better "
             "throughput."),
            ("GPU jumps between idle and 90%",
             "Normal.  Audio decoding / chunk boundary work happens "
             "between Whisper calls.  Average matters more than any "
             "single tick."),
            ("VRAM ~600 MB",
             "One Whisper transcribe in flight (small model)."),
            ("VRAM ~1.2 GB",
             "Two parallel transcribes "
             "(FULL_TRANSCRIBE_CONCURRENCY=2)."),
            ("VRAM near total",
             "Out of GPU memory.  Drop FULL_TRANSCRIBE_CONCURRENCY in "
             "config.py, or switch to a smaller Whisper model."),
            ("RAM (amber, <4 GB free)",
             "Memory pressure.  Reconcile + Pro Tools / browser / "
             "Premiere together can crash ffmpeg subprocesses with "
             "MemoryError.  Close something heavy or toggle Background "
             "mode."),
            ("RAM stays high & steady",
             "Fine.  Whisper holds the model + audio buffers in RAM "
             "throughout — total RAM doesn't drop until the run ends."),
        ]
        for sig, meaning in rows:
            row = tk.Frame(body, bg=BG, pady=4)
            row.pack(fill="x")
            tk.Label(row, text=sig, font=FBT, bg=BG, fg=ACCENT,
                     width=30, anchor="nw", justify="left"
                     ).pack(side="left", anchor="n")
            tk.Label(row, text=meaning, font=FB, bg=BG, fg=TEXT,
                     anchor="w", justify="left", wraplength=420
                     ).pack(side="left", anchor="n")

        # Tuning footnote
        foot = tk.Label(body,
            text=("\nTuning: bump FULL_TRANSCRIBE_CONCURRENCY in "
                  "config.py if the GPU sits below ~60% during long "
                  "transcribes.  Drop it if VRAM is tight or you see "
                  "contention warnings."),
            font=FB, bg=BG, fg=SUB, justify="left", wraplength=720)
        foot.pack(anchor="w", pady=(8, 0))

        nav = tk.Frame(win, bg=BG)
        nav.pack(fill="x", padx=20, pady=(0, 14))
        self._btn(nav, "CLOSE", win.destroy, width=10
                  ).pack(side="right")
        win.bind("<Escape>", lambda e: win.destroy())

        self._center_dialog(win, 760, 480)

    def _get_gpu_stats(self):
        """Return (utilization_pct, vram_used_gb, vram_total_gb) for the
        primary NVIDIA GPU, or None if unavailable.

        Shells out to nvidia-smi (always present with the driver).  The
        call typically completes in 50-200 ms — fine for a 2 s poll."""
        try:
            out = run_hidden(
                ["nvidia-smi",
                 "--query-gpu=utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, timeout=2.0,
            ).stdout.decode("utf-8", errors="ignore").strip()
            # nvidia-smi can emit multiple lines for multi-GPU boxes — take
            # the first line (primary GPU).
            first = out.splitlines()[0]
            parts = [p.strip() for p in first.split(",")]
            if len(parts) >= 3:
                util       = int(parts[0])
                used_mb    = int(parts[1])
                total_mb   = int(parts[2])
                return util, used_mb / 1024.0, total_mb / 1024.0
        except Exception:
            return None
        return None

    def _memory_status(self):
        """Return a Windows MEMORYSTATUSEX struct or None.  Used by the
        physical-RAM, commit-charge, and combined-budget helpers below."""
        if sys.platform != "win32":
            return None
        try:
            import ctypes
            class _MEMSTATUS(ctypes.Structure):
                _fields_ = [
                    ("dwLength",                ctypes.c_ulong),
                    ("dwMemoryLoad",            ctypes.c_ulong),
                    ("ullTotalPhys",            ctypes.c_ulonglong),
                    ("ullAvailPhys",            ctypes.c_ulonglong),
                    ("ullTotalPageFile",        ctypes.c_ulonglong),
                    ("ullAvailPageFile",        ctypes.c_ulonglong),
                    ("ullTotalVirtual",         ctypes.c_ulonglong),
                    ("ullAvailVirtual",         ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            stat = _MEMSTATUS()
            stat.dwLength = ctypes.sizeof(_MEMSTATUS)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(
                    ctypes.byref(stat)):
                return None
            return stat
        except Exception:
            return None

    def _available_ram_bytes(self):
        """Physical RAM available (idle) in bytes, or None.
        NOTE: physical RAM free is NOT the cap on new allocations — the
        commit charge ceiling is.  Use _available_alloc_bytes() to know
        whether an allocation will actually succeed."""
        stat = self._memory_status()
        return int(stat.ullAvailPhys) if stat else None

    def _available_pagefile_bytes(self):
        """Commit charge headroom in bytes (physical + page file backed),
        or None.  This is the real cap on new allocations on Windows."""
        stat = self._memory_status()
        return int(stat.ullAvailPageFile) if stat else None

    def _available_alloc_bytes(self):
        """Minimum of physical-free and commit-headroom — the largest
        contiguous block that could plausibly succeed.  Returns None if
        memory state can't be measured."""
        phys = self._available_ram_bytes()
        page = self._available_pagefile_bytes()
        if phys is None and page is None:
            return None
        if phys is None:
            return page
        if page is None:
            return phys
        return min(phys, page)

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

    def _build_provisional_results(self, int_assets):
        """Seed Step-4 result rows from the script's authored timecodes
        so the user can start reviewing before Whisper runs.

        For each interview pull, produce a `_base_result` with the
        pull's `[in..out]` copied verbatim from the script bracket
        and `source_audio` pointing at the first non-video pool file
        for that token — enough for single-side playback in the
        waveform editor.  When reconcile eventually finishes, these
        provisional rows are replaced by the reconciled results,
        UNLESS the user already marked the row confirmed/ignored
        (see the merge in _run_reconcile).

        VO blocks are NOT seeded — they have no authored TC in the
        script and can't be provisionally placed.  They appear in
        Step 4 with status "not_run" until reconcile hands them
        real matches.
        """
        results = []
        for pull in getattr(self, "pulls", []):
            r = engines._base_result(pull)
            r["status"] = "provisional"
            r["confidence"] = 0.0
            r["matched_text"] = pull.get("quote_text", "")
            tok = pull.get("token", "")
            files = [p for p in int_assets.get(tok, []) if not is_video(p)]
            if files:
                r["source_audio"] = files[0]
            results.append(r)
        for vo in getattr(self, "vo_blocks", []) or []:
            results.append(engines._vo_base_result(vo))
        results.sort(key=lambda r: r.get("order", 0))
        return results

    def _start_reconcile(self):
        # A fresh reconcile always produces new results — discard any loaded results.
        self._pending_results = None

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
        def _cache_miss_reason(ap):
            """Return a short string explaining why both VO caches missed for ap."""
            # Check .pb_cache/
            _cp = engines._cache_path(ap) if engines._cache_dir else None
            if not engines._cache_dir:
                pb_reason = "no cache dir"
            elif not _cp or not os.path.exists(_cp):
                pb_reason = "not in .pb_cache"
            else:
                try:
                    with open(_cp, encoding="utf-8") as _f:
                        _d = json.load(_f)
                    if _d.get("path") != os.path.abspath(ap):
                        pb_reason = "path mismatch in cache"
                    elif abs(_d.get("mtime", 0) - os.path.getmtime(ap)) > 1:
                        pb_reason = "mtime changed ({:.0f}s drift)".format(
                            abs(_d.get("mtime", 0) - os.path.getmtime(ap)))
                    elif _d.get("version") != CACHE_VERSION:
                        pb_reason = "cache version mismatch"
                    else:
                        pb_reason = "cache read error"
                except Exception:
                    pb_reason = "cache read error"
            # Check .pb_transcript.json
            _pbt = engines.pb_transcript_path(ap)
            if not os.path.isfile(_pbt):
                pbt_reason = "no .pb_transcript.json"
            else:
                try:
                    with open(_pbt, encoding="utf-8") as _f:
                        _d = json.load(_f)
                    if abs(_d.get("mtime", 0) - os.path.getmtime(ap)) > 2:
                        pbt_reason = "pb_transcript mtime changed"
                    elif not _d.get("words"):
                        pbt_reason = "pb_transcript empty"
                    else:
                        pbt_reason = "pb_transcript load error"
                except Exception:
                    pbt_reason = "pb_transcript read error"
            return "{}  ·  {}".format(pb_reason, pbt_reason)

        _vo_uncached = []   # list of (basename, reason) tuples
        for pi, vb in self.vo_bins.items():
            paths = vb.get_paths()
            takes = engines.pair_takes(paths)
            for _vp, ap in takes:
                if ap:
                    _pb_words, _ = engines.pb_transcript_load(ap)
                    if _pb_words is None and engines.cache_load(ap) is None:
                        _vo_uncached.append((os.path.basename(ap),
                                             _cache_miss_reason(ap)))
        if _vo_uncached:
            _names = "\n".join(
                "  •  {}  ({})".format(n, r) for n, r in _vo_uncached[:6])
            if len(_vo_uncached) > 6:
                _names += "\n  …and {} more".format(len(_vo_uncached) - 6)
            _msg = (
                "{} VO audio file{} have no usable transcription cache:\n\n{}\n\n"
                "PostBridge will transcribe them now, which may take several "
                "minutes.".format(
                    len(_vo_uncached),
                    "s" if len(_vo_uncached) != 1 else "",
                    _names)
            )
            if not messagebox.askyesno("VO Transcription Cache Missing", _msg,
                                       icon="warning"):
                return

        # ── Pre-flight: warn about oversized pull windows ──────────────────────
        suspicious = [p for p in self.pulls
                      if p.get("out_seconds", 0) - p.get("in_seconds", 0) > MAX_EXTRACT_S]
        if suspicious and not self._warn_large_windows(suspicious):
            return

        # ── Pre-flight: warn about low available memory ──────────────────────
        # Reconcile spins up Whisper (~1 GB) plus per-token ffmpeg processes
        # and audio buffers; a parallel Pro Tools session can easily push the
        # box past its RAM ceiling and crash subprocess output readers with
        # MemoryError.  Surface this BEFORE the long-running run starts.
        if not self._preflight_memory_check():
            return

        # ── Provisional-first review: seed self.results with the script's
        # authored timecodes so a → REVIEW NOW jump into Step 4 has
        # something to render before Whisper starts producing real
        # reconciled TCs.  _run_reconcile's final merge preserves any
        # row the user confirmed/ignored while transcription was
        # still running in the background.
        self.results = self._build_provisional_results(int_assets)
        self._bg_reconcile_active = True

        self._clear()
        self._section("STEP 3 — RECONCILING")

        _desc = tk.Label(self.body,
                         text="Extracting and transcribing each interview pull, "
                              "then matching to your script. "
                              "May take several minutes — status updates as each clip finishes.",
                         font=FB, bg=BG, fg=SUB, justify="left", anchor="w")
        _desc.pack(fill="x", pady=(0, 10))
        _desc.bind("<Configure>", lambda e: _desc.config(wraplength=e.width))

        prog_outer = tk.Frame(self.body, bg=SURF,
                              highlightbackground=BORDER, highlightthickness=1)
        prog_outer.pack(fill="x", pady=(0,8))

        # Elapsed-time clock — top row of the progress card
        _clock_row = tk.Frame(prog_outer, bg=SURF)
        _clock_row.pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(_clock_row, text="ELAPSED", font=FB, bg=SURF, fg=SUB).pack(side="left")
        self._elapsed_lbl = tk.Label(_clock_row, text="0:00",
                                     font=FL, bg=SURF, fg=TEXT)
        self._elapsed_lbl.pack(side="left", padx=(6, 0))
        self._elapsed_running = True
        _t_clock_start = time.perf_counter()

        def _tick_elapsed(t0=_t_clock_start):
            if not self._elapsed_running:
                return
            elapsed = int(time.perf_counter() - t0)
            m, s = divmod(elapsed, 60)
            try:
                self._elapsed_lbl.config(text="{}:{:02d}".format(m, s))
            except Exception:
                return
            self.after(1000, _tick_elapsed, t0)

        self.after(1000, _tick_elapsed)

        prog_row = tk.Frame(prog_outer, bg=SURF)
        prog_row.pack(fill="x", padx=12, pady=(6, 4))
        _total_items = len(self.pulls) + len(self.vo_blocks)
        self._prog_lbl = tk.Label(prog_row, text="Starting…",
                                  font=FL, bg=SURF, fg=TEXT, anchor="w")
        self._prog_lbl.pack(side="left")
        self._prog_bar = _FlatProgressBar(prog_row, height=6)
        self._prog_bar.pack(side="left", fill="x", expand=True, padx=(12, 0))
        self._prog_bar.set(0, max(_total_items, 1))

        # Animated liveness dots — cycles independently of clip-level progress
        self._dot_lbl = tk.Label(prog_outer, text="", font=FL, bg=SURF, fg=ACCENT,
                                 anchor="w", padx=12)
        self._dot_lbl.pack(anchor="w", pady=(0, 6))
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

        self._ui(_pulse_dots)

        # ── Live resource monitor ──────────────────────────────────────────
        # Polls GPU utilisation/VRAM via nvidia-smi and available RAM via
        # the Windows GlobalMemoryStatusEx API every 2 seconds.  Surfaces
        # whether CUDA is actually engaged, whether the GPU is saturated,
        # and whether RAM pressure is climbing — all the things a user
        # would otherwise have to alt-tab to Task Manager to learn.
        res_row = tk.Frame(prog_outer, bg=SURF)
        res_row.pack(fill="x", padx=12, pady=(0, 2))
        tk.Label(res_row, text="RESOURCES", font=FB,
                 bg=SURF, fg=SUB).pack(side="left")
        self._res_lbl = tk.Label(res_row, text="…", font=FL,
                                  bg=SURF, fg=TEXT, anchor="w")
        self._res_lbl.pack(side="left", padx=(6, 0))

        # "?" help affordance sits inline next to the values it
        # explains — packed left so it stays close to the readout
        # instead of floating off at the far edge of the row.
        help_lbl = tk.Label(res_row, text=" ⓘ ", font=FB,
                             bg=SURF, fg=SUB, cursor="hand2")
        help_lbl.pack(side="left", padx=(4, 0))
        help_lbl.bind("<Enter>",
                       lambda e, w=help_lbl: w.config(fg=ACCENT))
        help_lbl.bind("<Leave>",
                       lambda e, w=help_lbl: w.config(fg=SUB))
        help_lbl.bind("<Button-1>", lambda e: self._show_resource_help())

        # One-line interpretive hint below the readout — keeps the meaning
        # of the headline numbers visible at a glance without burying the
        # reading itself.  Tap the ⓘ for the full reference.
        hint_lbl = tk.Label(
            prog_outer,
            text=("GPU >30% = CUDA active  ·  "
                  "RAM turns amber below 4 GB free  ·  "
                  "tap ⓘ for full key"),
            font=FB, bg=SURF, fg=SUB, anchor="w", padx=12)
        hint_lbl.pack(fill="x", pady=(0, 8))

        self._res_monitor_running = True
        # Wall-clock anchor so each log sample has an elapsed-seconds
        # stamp.  Lets you correlate utilisation dips with specific
        # transcribe-start / transcribe-done events in the same file.
        self._res_monitor_t0 = time.perf_counter()

        def _tick_resources():
            if not self._res_monitor_running:
                return
            try:
                if not self._res_lbl.winfo_exists():
                    return
            except tk.TclError:
                return

            parts = []
            # Raw sample values for the file log (separate from the
            # styled UI text below).
            log_gpu  = None
            log_vu   = None
            log_vt   = None
            log_ram  = None

            gpu = self._get_gpu_stats()
            if gpu is not None:
                util, vused, vtotal = gpu
                log_gpu, log_vu, log_vt = util, vused, vtotal
                # Highlight in ACCENT when GPU is engaged so the user can
                # see at a glance that CUDA is working.
                gpu_color = ACCENT if util >= 30 else SUB
                parts.append(("GPU {}%".format(util), gpu_color))
                parts.append((" · VRAM {:.1f} / {:.1f} GB".format(
                    vused, vtotal), TEXT))
            else:
                parts.append(("GPU n/a", SUB))

            ram = self._available_ram_bytes()
            if ram is not None:
                ram_gb = ram / (1024 ** 3)
                log_ram = ram_gb
                ram_color = WARN if ram_gb < 4.0 else TEXT
                parts.append(("  ·  RAM {:.1f} GB free".format(ram_gb),
                              ram_color))

            # Single label, multi-color via... actually we can't mix colors
            # in one Label.  Render as plain text with the first segment
            # color (GPU) since that's the most diagnostic.
            try:
                text = "".join(p[0] for p in parts)
                fg   = parts[0][1] if parts else SUB
                self._res_lbl.config(text=text, fg=fg)
            except tk.TclError:
                return

            # Mirror to _debug_run.log so a post-run audit can see the
            # whole utilisation timeline.  Direct file write — bypasses
            # the UI log so we don't spam Jordan with hundreds of [res]
            # lines while she watches a run.
            try:
                bits = ["[res]",
                        "t={:.0f}s".format(
                            time.perf_counter() - self._res_monitor_t0)]
                if log_gpu is not None:
                    bits.append("GPU={}%".format(log_gpu))
                    bits.append("VRAM={:.1f}/{:.1f}GB".format(
                        log_vu, log_vt))
                else:
                    bits.append("GPU=n/a")
                if log_ram is not None:
                    bits.append("RAM={:.1f}GBfree".format(log_ram))
                # Live worker counts come off self — same refs the gate
                # uses inside _run_reconcile, so the readout is accurate
                # whether the user toggles fast/background mid-run.
                _act_ref = getattr(self,
                    "_reconcile_live_active", None)
                _lw_ref  = getattr(self,
                    "_reconcile_live_workers", None)
                bits.append("workers={}/{}".format(
                    _act_ref[0] if _act_ref else "?",
                    _lw_ref[0]  if _lw_ref  else "?"))
                line = "  ".join(bits)
                with self._DEBUG_LOG_LOCK:
                    with open(self._DEBUG_LOG, "a",
                              encoding="utf-8") as _f:
                        _f.write(line + "\n")
            except Exception:
                pass

            self.after(2000, _tick_resources)

        # First tick fires immediately so the user doesn't see "..." for
        # 2 seconds; subsequent ticks fire every 2s via the after loop.
        self.after(100, _tick_resources)

        # ── Transcription tasks panel ─────────────────────────────────
        # Top panel: one row per transcription task (interview token's
        # full-audio pass OR a single VO take).  Rows appear as their
        # task starts, update in place as progress comes in, and freeze
        # with a ✓ once done.  Whole panel collapses (pack_forget) when
        # every task has reached a terminal state, giving the reconcile
        # log all the remaining vertical space.
        xc_hdr = tk.Frame(self.body, bg=BG)
        xc_hdr.pack(fill="x", pady=(0, 2))
        tk.Label(xc_hdr, text="TRANSCRIPTION", font=FL,
                 bg=BG, fg=SUB).pack(side="left", padx=12)
        self._xc_tasks_count_lbl = tk.Label(
            xc_hdr, text="", font=FL, bg=BG, fg=SUB)
        self._xc_tasks_count_lbl.pack(side="left", padx=(8, 0))

        self._xc_tasks_frame = tk.Frame(
            self.body, bg=SURF,
            highlightbackground=BORDER, highlightthickness=1)
        self._xc_tasks_frame.pack(fill="x", pady=(0, 8))
        # Per-task widget refs live here so updates are surgical.
        self._xc_tasks         = {}     # task_id -> state dict
        self._xc_task_order    = []     # insertion order for stable layout
        self._xc_task_widgets  = {}     # task_id -> widget dict
        # Reset on each entry into Step 3.
        self._xc_tasks_header  = xc_hdr
        self._xc_tasks_visible = True

        log_frame = tk.Frame(self.body, bg=SURF3,
                             highlightbackground=BORDER, highlightthickness=1)
        log_frame.pack(fill="both", expand=True)
        tk.Label(log_frame, text="RECONCILIATION", font=FL,
                 bg=SURF3, fg=SUB, anchor="w", padx=8, pady=4
                 ).pack(fill="x")
        log_inner = tk.Frame(log_frame, bg=SURF3)
        log_inner.pack(fill="both", expand=True)
        self._log = tk.Text(log_inner, bg=SURF3, fg=SUB, font=FB,
                            relief="flat", bd=8, state="disabled",
                            height=20, wrap="word")
        sb = _SlimScrollbar(log_inner, command=self._log.yview)
        self._log.configure(yscrollcommand=sb.set)
        self._log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._cancel.clear()
        # Pin the nav to the BOTTOM of the parent so the log_frame
        # (packed above with fill=both+expand=True) can't push it off
        # the visible window.  Without side="bottom" the nav lands
        # below the expanding log — invisible on any window smaller
        # than the log's minimum height + all its siblings.  Which is
        # every window Jordan's seeing.
        nav = tk.Frame(self.body, bg=BG)
        nav.pack(side="bottom", fill="x", pady=(8, 0))
        self._btn(nav, "CANCEL", self._cancel_reconcile).pack(side="left")

        # Provisional-first jump: user can enter Step 4 review while the
        # background transcribe keeps running.  Rows appear with script-
        # authored TCs and single-side playback; confirms made here are
        # preserved when reconcile finishes and merges its real results.
        self._btn(nav, "REVIEW NOW  →", self._step4,
                  color=ACCENT).pack(side="left", padx=(8, 0))

        # Background-mode toggle — works on the fly via shared live-workers ref
        _cpu    = os.cpu_count() or 2
        _n_fast = max(1, _cpu - 1)
        _is_bg  = getattr(self, "_reconcile_bg_mode", tk.BooleanVar()).get()
        if not hasattr(self, "_reconcile_bg_mode"):
            self._reconcile_bg_mode = tk.BooleanVar(value=False)
        _lw     = [1 if _is_bg else _n_fast]
        _cond   = threading.Condition()
        _active = [0]
        self._reconcile_live_workers = _lw
        self._reconcile_live_cond    = _cond
        self._reconcile_live_active  = _active
        self._reconcile_n_fast       = _n_fast

        bg_s3_frame = tk.Frame(nav, bg=BG)
        bg_s3_frame.pack(side="right")
        bg_s3_ck = tk.Label(bg_s3_frame,
                            text="\u2611" if _is_bg else "\u2610",
                            font=(_SANS, 15), bg=BG,
                            fg=ACCENT if _is_bg else SUB,
                            cursor="hand2", padx=4)
        bg_s3_ck.pack(side="left")
        bg_s3_lbl = tk.Label(bg_s3_frame, text="Background mode",
                             font=FB, bg=BG, fg=SUB, cursor="hand2")
        bg_s3_lbl.pack(side="left")

        def _toggle_s3_bg():
            self._reconcile_bg_mode.set(not self._reconcile_bg_mode.get())
            _on = self._reconcile_bg_mode.get()
            bg_s3_ck.config(text="\u2611" if _on else "\u2610",
                            fg=ACCENT if _on else SUB)
            _lw[0] = 1 if _on else _n_fast
            with _cond:
                _cond.notify_all()

        bg_s3_ck.bind("<Button-1>",  lambda e: _toggle_s3_bg())
        bg_s3_lbl.bind("<Button-1>", lambda e: _toggle_s3_bg())

        # Collect all tkinter variable values here on the main thread.
        # IntVar/DoubleVar/StringVar.get() is not thread-safe and must not be
        # called from the reconcile thread (causes deadlocks on Python 3.14+).
        asgn = self._pool.get_assignments()
        token_audio_paths = {
            tok: [p for p in asgn.get(tok, []) if not is_video(p)]
            for tok in self.tokens
        }
        pad_secs = self.pad_var.get()

        # Flag the reconcile as busy so _is_transcription_running()
        # (used by the Settings dialog model-swap gate) reports True.
        self._reconcile_busy = True
        threading.Thread(
            target=self._run_reconcile,
            args=(int_assets, token_audio_paths, pad_secs),
            daemon=True).start()

    @staticmethod
    def _fmt_duration(secs):
        """Return a string like '2m 14s  (134.2s)' for log output."""
        m = int(secs) // 60
        s = secs - m * 60
        if m:
            return "{:d}m {:.0f}s  ({:.1f}s)".format(m, s, secs)
        return "{:.1f}s".format(secs)

    # Fixed-path debug mirror — readable by the dev without knowing the script location.
    _DEBUG_LOG      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_debug_run.log")
    _DEBUG_LOG_LOCK = threading.Lock()   # serialise concurrent thread writes to the log file

    # ── Step 3 transcription-tasks routing ────────────────────────────
    # Regex patterns matched against every _log_line() message.  When
    # a pattern matches, the message is consumed (kept out of the
    # bottom reconciliation log) and routed into the top transcription
    # tasks panel as a structured update.  Engines.py keeps emitting
    # the same human-readable strings; this is purely a UI-side
    # transformation, so engine code stays unchanged.
    _XC_TOKEN_START_RE = re.compile(
        r"^\s*\[(\w+)\]\s+starting\s+(\d+)\s+pull")
    _XC_TOKEN_MIXED_RE = re.compile(
        r"^\s*\[(\w+)\]\s+mixed\s+(\d+)\s+tracks?")
    _XC_TOKEN_CACHED_RE = re.compile(
        r"^\s*\[(\w+)\]\s+using cached full-audio transcript "
        r"\((\d+)\s+words\)")
    _XC_TOKEN_FULL_RE = re.compile(
        r"^\s*\[(\w+)\]\s+\d+\s+pulls\s+\+\s+no cached transcript")
    _XC_TOKEN_SILENCE_RE = re.compile(
        r"^\s*\[(\w+)\]\s+Detecting silence splits")
    _XC_TOKEN_PROG_RE = re.compile(
        r"^\s*\[(\w+)\]\s+Transcribing\s+—\s+"
        r"(\d+)s\s+of\s+(\d+)s\s+\((\d+)%\)")
    _XC_TOKEN_DONE_RE = re.compile(
        r"^\s*\[(\w+)\]\s+full transcript ready\s+"
        r"\((\d+)\s+words,\s+([\d.]+)s\)")
    _XC_TOKEN_PULL_DONE_RE = re.compile(
        r"^\s*\[(\w+)\]\s+done\s+—\s+(\d+)\s+pull")
    _XC_VO_HEADER_RE = re.compile(
        r"^\s*Transcribing VO Part\s+(\d+)\s+—\s+(\d+)\s+take")
    _XC_VO_FRESH_RE = re.compile(
        r"^\s*Part\s+(\d+)\s+Take\s+(\d+):\s+cache miss")
    _XC_VO_CACHED_RE = re.compile(
        r"^\s*Part\s+(\d+)\s+Take\s+(\d+):\s+(\d+)\s+words\s+\[cached\]")
    _XC_VO_CHUNKS_RE = re.compile(
        r"^\s*Part\s+(\d+)\s+Take\s+(\d+):\s+(\d+)\s+chunks\s+—\s+"
        r"transcribing")
    _XC_VO_DONE_RE = re.compile(
        r"^\s*Part\s+(\d+)\s+Take\s+(\d+):\s+chunked done in\s+"
        r"([\d.]+)s\s+\((\d+)\s+words\)")
    # Noise: blob/save/no-video lines that follow VO_DONE — drop entirely.
    _XC_VO_NOISE_RE = re.compile(
        r"^\s*Part\s+\d+\s+Take\s+\d+:\s+(?:\d+\s+blobs|"
        r"\d+\s+words\s+\[saved to cache\]|"
        r"\d+\s+words\s+\(no video\))")

    @staticmethod
    def _xc_phase_icon(phase):
        return {
            "pending":          "⋯",
            "mixing":           "⋯",
            "silence-detect":   "⋯",
            "transcribing":     "▸",
            "done":             "✓",
            "cached":           "⚡",
            "error":            "⚠",
        }.get(phase, "·")

    @staticmethod
    def _xc_phase_color(phase):
        return {
            "pending":          SUB,
            "mixing":           SUB,
            "silence-detect":   SUB,
            "transcribing":     ACCENT,
            "done":             SUCCESS,
            "cached":           SUCCESS,
            "error":            ERR,
        }.get(phase, TEXT)

    def _xc_task_ensure(self, task_id, label, category):
        """Create the row for `task_id` if it doesn't already exist.
        Returns the widget dict.  Idempotent."""
        if task_id in self._xc_task_widgets:
            return self._xc_task_widgets[task_id]
        body = getattr(self, "_xc_tasks_frame", None)
        if body is None:
            return None
        # Panel may have been collapsed after a prior all-done state;
        # re-show it if a new task is starting.  Re-pack frame BEFORE
        # the log container, then header BEFORE the frame, so layout
        # ends up: header → tasks → log (top to bottom).
        if not getattr(self, "_xc_tasks_visible", True):
            try:
                _log_outer = self._log.master.master
                self._xc_tasks_frame.pack(fill="x", pady=(0, 8),
                                          before=_log_outer)
                self._xc_tasks_header.pack(fill="x", pady=(0, 2),
                                           before=self._xc_tasks_frame)
                self._xc_tasks_visible = True
            except tk.TclError:
                pass

        row = tk.Frame(body, bg=SURF)
        row.pack(fill="x", padx=8, pady=2)

        icon_lbl = tk.Label(row, text="⋯", font=FBT,
                             bg=SURF, fg=SUB, width=2, anchor="center")
        icon_lbl.pack(side="left")

        lbl_lbl = tk.Label(row, text=label, font=FBT,
                            bg=SURF, fg=ACCENT,
                            anchor="w", width=16, padx=4)
        lbl_lbl.pack(side="left")

        status_lbl = tk.Label(row, text="queued", font=FB,
                               bg=SURF, fg=SUB, anchor="w")
        status_lbl.pack(side="left", fill="x", expand=True)

        pct_lbl = tk.Label(row, text="", font=FB,
                            bg=SURF, fg=SUB, width=5, anchor="e")
        pct_lbl.pack(side="right", padx=(4, 8))

        bar_holder = tk.Frame(row, bg=SURF, width=120, height=6)
        bar_holder.pack_propagate(False)
        bar_holder.pack(side="right", padx=4)
        bar = _FlatProgressBar(bar_holder, height=6,
                                fill=ACCENT, bg=SURF2)
        bar.pack(fill="both", expand=True)

        widgets = {
            "row":        row,
            "icon_lbl":   icon_lbl,
            "lbl_lbl":    lbl_lbl,
            "status_lbl": status_lbl,
            "pct_lbl":    pct_lbl,
            "bar":        bar,
            "category":   category,
        }
        self._xc_task_widgets[task_id] = widgets
        self._xc_task_order.append(task_id)
        self._xc_tasks[task_id] = {
            "phase":  "pending",
            "pct":    0,
            "detail": "queued",
        }
        self._xc_update_count_label()
        return widgets

    def _xc_task_update(self, task_id, **updates):
        """Update one task's phase / pct / detail and refresh its row."""
        state = self._xc_tasks.get(task_id)
        if state is None:
            return
        state.update(updates)
        w = self._xc_task_widgets.get(task_id)
        if w is None:
            return
        phase  = state.get("phase",  "pending")
        pct    = state.get("pct",    0)
        detail = state.get("detail", "")
        try:
            w["icon_lbl"].config(text=self._xc_phase_icon(phase),
                                  fg=self._xc_phase_color(phase))
            w["status_lbl"].config(text=detail,
                                    fg=self._xc_phase_color(phase))
            if phase in ("done", "cached"):
                w["pct_lbl"].config(text="")
                w["bar"].set(1, 1)
            elif phase == "transcribing" and pct:
                w["pct_lbl"].config(text="{}%".format(int(pct)))
                w["bar"].set(pct, 100)
            elif phase == "transcribing":
                w["pct_lbl"].config(text="")
                w["bar"].set(0, 1)
            else:
                w["pct_lbl"].config(text="")
                w["bar"].set(0, 1)
        except tk.TclError:
            pass
        self._xc_update_count_label()
        self._xc_maybe_collapse()

    def _xc_update_count_label(self):
        """Show 'N tasks · M done' beside the TRANSCRIPTION header."""
        lbl = getattr(self, "_xc_tasks_count_lbl", None)
        if lbl is None:
            return
        n_total = len(self._xc_tasks)
        n_done  = sum(1 for s in self._xc_tasks.values()
                      if s.get("phase") in ("done", "cached"))
        try:
            if n_total:
                lbl.config(text="({} task{} · {} done)".format(
                    n_total, "s" if n_total != 1 else "", n_done))
            else:
                lbl.config(text="")
        except tk.TclError:
            pass

    def _xc_maybe_collapse(self):
        """Hide the transcription panel once every task has reached a
        terminal phase.  Reverses itself in _xc_task_ensure if a new
        task starts (e.g. user kicks off another reconcile run)."""
        if not self._xc_tasks:
            return
        all_done = all(
            s.get("phase") in ("done", "cached", "error")
            for s in self._xc_tasks.values())
        if all_done and getattr(self, "_xc_tasks_visible", True):
            try:
                self._xc_tasks_frame.pack_forget()
                self._xc_tasks_header.pack_forget()
                self._xc_tasks_visible = False
            except tk.TclError:
                pass

    def _xc_reset(self):
        """Clear all task rows + state.  Called at the start of each
        reconcile run so a re-run doesn't accumulate stale rows."""
        for w in list(self._xc_task_widgets.values()):
            try:
                w["row"].destroy()
            except tk.TclError:
                pass
        self._xc_task_widgets = {}
        self._xc_tasks        = {}
        self._xc_task_order   = []
        # Re-show the panel header in case a previous run left it
        # collapsed.  Pack frame first (before log container) then
        # header (before frame) for top-to-bottom ordering.
        if getattr(self, "_xc_tasks_frame", None) is not None:
            try:
                if not self._xc_tasks_visible:
                    _log_outer = self._log.master.master
                    self._xc_tasks_frame.pack(
                        fill="x", pady=(0, 8), before=_log_outer)
                    self._xc_tasks_header.pack(
                        fill="x", pady=(0, 2),
                        before=self._xc_tasks_frame)
                    self._xc_tasks_visible = True
            except tk.TclError:
                pass
        self._xc_update_count_label()

    def _xc_route_log(self, msg, color=None):
        """Try to interpret `msg` as a transcription-task progress
        message and update the top panel.  Returns True when the
        message was consumed (don't echo to the bottom log) and False
        otherwise."""
        if not msg:
            return False
        if not hasattr(self, "_xc_tasks"):
            return False
        # — VO noise (blob counts / save confirmations / no-video) —
        if self._XC_VO_NOISE_RE.match(msg):
            return True

        # — VO take patterns —
        m = self._XC_VO_FRESH_RE.match(msg)
        if m:
            part, take = m.group(1), m.group(2)
            tid = ("vo", part, take)
            self._xc_task_ensure(
                tid, "Part {} T{}".format(part, take), "vo")
            self._xc_task_update(
                tid, phase="pending", detail="cache miss · queued",
                pct=0)
            return True
        m = self._XC_VO_CHUNKS_RE.match(msg)
        if m:
            part, take, nchunks = m.group(1), m.group(2), m.group(3)
            tid = ("vo", part, take)
            self._xc_task_ensure(
                tid, "Part {} T{}".format(part, take), "vo")
            self._xc_task_update(
                tid, phase="transcribing",
                detail="transcribing · {} chunks".format(nchunks),
                pct=0)
            return True
        m = self._XC_VO_CACHED_RE.match(msg)
        if m:
            part, take, nwords = m.group(1), m.group(2), m.group(3)
            tid = ("vo", part, take)
            self._xc_task_ensure(
                tid, "Part {} T{}".format(part, take), "vo")
            self._xc_task_update(
                tid, phase="cached",
                detail="cached · {} words".format(nwords))
            return True
        m = self._XC_VO_DONE_RE.match(msg)
        if m:
            part, take, secs, nwords = m.groups()
            tid = ("vo", part, take)
            self._xc_task_ensure(
                tid, "Part {} T{}".format(part, take), "vo")
            self._xc_task_update(
                tid, phase="done",
                detail="done · {} words · {}s".format(nwords, secs))
            return True
        if self._XC_VO_HEADER_RE.match(msg):
            # Informational ("Transcribing VO Part N — M takes...") —
            # let it pass through to the log so the user sees the
            # high-level grouping.  Don't claim the message.
            return False

        # — Interview-token patterns —
        m = self._XC_TOKEN_PROG_RE.match(msg)
        if m:
            tok, cur_s, tot_s, pct = m.groups()
            tid = ("interview", tok)
            self._xc_task_ensure(tid, "[{}]".format(tok), "interview")
            self._xc_task_update(
                tid, phase="transcribing",
                detail="transcribing · {}s of {}s".format(cur_s, tot_s),
                pct=int(pct))
            return True
        m = self._XC_TOKEN_DONE_RE.match(msg)
        if m:
            tok, nwords, secs = m.groups()
            tid = ("interview", tok)
            self._xc_task_ensure(tid, "[{}]".format(tok), "interview")
            self._xc_task_update(
                tid, phase="done",
                detail="done · {} words · {}s".format(nwords, secs))
            return True
        m = self._XC_TOKEN_CACHED_RE.match(msg)
        if m:
            tok, nwords = m.groups()
            tid = ("interview", tok)
            self._xc_task_ensure(tid, "[{}]".format(tok), "interview")
            self._xc_task_update(
                tid, phase="cached",
                detail="cached · {} words".format(nwords))
            return True
        m = self._XC_TOKEN_FULL_RE.match(msg)
        if m:
            tok = m.group(1)
            tid = ("interview", tok)
            self._xc_task_ensure(tid, "[{}]".format(tok), "interview")
            self._xc_task_update(
                tid, phase="transcribing",
                detail="transcribing full audio…",
                pct=0)
            return True
        m = self._XC_TOKEN_SILENCE_RE.match(msg)
        if m:
            tok = m.group(1)
            tid = ("interview", tok)
            self._xc_task_ensure(tid, "[{}]".format(tok), "interview")
            self._xc_task_update(
                tid, phase="silence-detect",
                detail="detecting silence splits…")
            return True
        m = self._XC_TOKEN_MIXED_RE.match(msg)
        if m:
            tok, n = m.group(1), m.group(2)
            tid = ("interview", tok)
            self._xc_task_ensure(tid, "[{}]".format(tok), "interview")
            self._xc_task_update(
                tid, phase="mixing",
                detail="mixed {} tracks".format(n))
            return True
        m = self._XC_TOKEN_START_RE.match(msg)
        if m:
            tok, n_pulls = m.group(1), m.group(2)
            tid = ("interview", tok)
            self._xc_task_ensure(tid, "[{}]".format(tok), "interview")
            # Only set "queued" if we haven't already advanced past it
            # via mixed/silence/transcribing — multiple messages can
            # land out of order.
            state = self._xc_tasks.get(tid) or {}
            if state.get("phase", "pending") == "pending":
                self._xc_task_update(
                    tid, phase="pending",
                    detail="queued · {} pulls".format(n_pulls))
            return True
        # [TOKEN] done — N pull(s) completed is a RECONCILE-phase
        # message, not transcription.  Let it pass through.
        if self._XC_TOKEN_PULL_DONE_RE.match(msg):
            return False
        return False

    def _log_line(self, msg, color=None):
        # Also write to the run log file if one is open
        if getattr(self, "_run_log_fh", None):
            try:
                self._run_log_fh.write(msg + "\n")
                self._run_log_fh.flush()
            except Exception:
                pass
        # Mirror to the fixed debug log — lock prevents concurrent open() collisions on Windows
        try:
            with self._DEBUG_LOG_LOCK:
                with open(self._DEBUG_LOG, "a", encoding="utf-8") as _dlf:
                    _dlf.write(msg + "\n")
        except Exception:
            pass
        # Everything below touches Tk widgets — the transcription panel
        # (via _xc_route_log → _xc_task_ensure/_update, which create and
        # config rows) AND the bottom log — so it MUST run on the main
        # thread.  _log_line is called from reconcile/VO worker threads,
        # where touching Tk directly is a latent crash.  Route-then-log
        # in ONE marshalled callback so the "consumed → don't double-print"
        # decision and the log write stay ordered and consistent.
        def _do():
            try:
                consumed = self._xc_route_log(msg, color)
            except Exception:
                consumed = False
            if consumed:
                return
            # The log widget lives in Step 3.  When the user navigated to
            # Step 4 (provisional-first flow) it no longer exists — file
            # writes above are the authoritative record; skip the widget
            # write silently.
            _log = getattr(self, "_log", None)
            if _log is None:
                return
            try:
                if not _log.winfo_exists():
                    return
            except tk.TclError:
                return
            _log.configure(state="normal")
            tag = "c{}".format(abs(hash(color or "")))
            if color:
                _log.tag_configure(tag, foreground=color)
            _log.insert("end", msg + "\n", tag if color else "")
            _log.see("end")
            _log.configure(state="disabled")
        self._ui(_do)

    def _set_tx_progress(self, n, total, label=""):
        def _do():
            if not hasattr(self, "_prog_lbl"):
                return
            self._prog_lbl.configure(text=label)
            self._prog_bar.set(n, total)
        self._ui(_do)

    def _run_reconcile(self, int_assets, token_audio_paths, pad_secs=PAD_SECS, n_workers=None):
        # Clear the debug mirror at the start of each run
        try:
            open(self._DEBUG_LOG, "w").close()
        except Exception:
            pass
        # Clear any task rows left over from a prior run on the same
        # Step 3 view (rare but possible if the user cancels and
        # re-runs without leaving the screen).  _run_reconcile is a
        # worker thread and _xc_reset destroys/re-packs Tk widgets, so
        # marshal it onto the main thread (its internal guards handle a
        # torn-down panel).
        self._ui(self._xc_reset)

        # ── Build transcript_sources: mix multi-file tokens ─────────────────
        # Single-file tokens pass through unchanged.  Multi-file tokens are
        # mixed by engines.mix_for_transcript() to a CONTENT-ADDRESSED path
        # under .pb_cache/mix_{md5}.wav — same source files → same path →
        # second reconcile finds the cached mix + its sidecar transcript and
        # skips both the mix pass and Whisper.  See mix_for_transcript().

        # Diagnostic — surface the cache dir and any existing mixes so a
        # cache-miss cascade is visible in the log instead of silent.
        _cd = getattr(engines, "_cache_dir", None) or ""
        _mix_count = 0
        if _cd and os.path.isdir(_cd):
            try:
                _mix_count = sum(1 for f in os.listdir(_cd)
                                 if f.startswith("mix_") and f.endswith(".wav"))
            except OSError:
                pass
        self._log_line(
            "Cache dir: {}  |  cached mixes on disk: {}".format(
                _cd or "(unset)", _mix_count),
            SUB)
        #
        # Older versions used tempfile.mkstemp so mixes had random names and
        # never survived across runs; those old temps are cleaned up here.
        # Cached mixes under _cache_dir are LEFT ALONE so they persist and
        # get cache hits next time.
        _cache_root = getattr(engines, "_cache_dir", None)
        for _old in getattr(self, "_temp_mix_files", []):
            try:
                # Preserve any path that's inside the persistent cache dir;
                # only clear old-style random temp files.
                if (_cache_root and
                        os.path.abspath(_old).startswith(os.path.abspath(_cache_root))):
                    continue
                os.remove(_old)
            except Exception:
                pass
        self._temp_mix_files = []

        # ── Pull Quotes fast path: discover pre-transcribed sessions ────────
        # If the script lives inside a recognised editorial project layout
        # (`02_MEDIA` + `03_AUDIO` siblings somewhere up the tree), we walk
        # for `*.pb_session.json` files.  Any pull whose token matches a
        # discovered session skips the mix-and-transcribe step entirely
        # — its precise Whisper-derived timecodes from Pull Quotes are
        # used directly.  Tokens without a session fall through to the
        # existing Whisper-on-mix path.
        try:
            from parsers import discover_pq_sessions as _discover_pq
        except Exception:
            _discover_pq = None
        pq_session_map = {}        # token → session file path
        pq_session_data = {}       # token → loaded session dict
        if _discover_pq is not None:
            try:
                pq_session_map = _discover_pq(getattr(self, "_script_path", "")) or {}
            except Exception:
                pq_session_map = {}
            for _w in (pq_session_map.pop("__warnings__", []) or []):
                self._log_line("  ! " + _w, WARN)
            for _tok, _sp in pq_session_map.items():
                try:
                    with open(_sp, encoding="utf-8") as _f:
                        _data = json.load(_f)
                    if (_data.get("workflow") == "interview_session"
                            and _data.get("transcript")):
                        _data["_file_path"] = _sp
                        # ── Path relocation ────────────────────────────
                        # audio_cache + media were written as absolute
                        # paths on the machine that made the session
                        # (usually a different user's Downloads folder).
                        # If they don't resolve here, try the session
                        # file's own directory (basename lookup) and
                        # then fall back to the pool's assigned audio
                        # for this token.  Otherwise source_audio ends
                        # up empty and the waveform editor's resolver
                        # picks whichever pool file happens to be first
                        # — often the wrong side of the conversation.
                        _sess_dir  = os.path.dirname(_sp)
                        _pool_here = [p for p in token_audio_paths.get(_tok, [])
                                      if p and os.path.isfile(p) and not is_video(p)]
                        _relocs = 0
                        _cur_ac = _data.get("audio_cache", "") or ""
                        if _cur_ac and not os.path.isfile(_cur_ac):
                            _local = os.path.join(_sess_dir, os.path.basename(_cur_ac))
                            if os.path.isfile(_local):
                                _data["audio_cache"] = _local; _relocs += 1
                            elif _pool_here:
                                _data["audio_cache"] = _pool_here[0]; _relocs += 1
                        _fixed_media = []
                        for _mp in (_data.get("media") or []):
                            if _mp and os.path.isfile(_mp):
                                _fixed_media.append(_mp); continue
                            _local = os.path.join(_sess_dir, os.path.basename(_mp or ""))
                            if _mp and os.path.isfile(_local):
                                _fixed_media.append(_local); _relocs += 1; continue
                            # Try to fuzzy-match a pool file by basename stem
                            _stem = os.path.splitext(os.path.basename(_mp or ""))[0].lower()
                            _hit = next((p for p in _pool_here
                                         if _stem in os.path.basename(p).lower()),
                                        None) if _stem else None
                            if _hit:
                                _fixed_media.append(_hit); _relocs += 1
                        if _fixed_media:
                            _data["media"] = _fixed_media
                        # ── Audio-signature verification ────────────────
                        # For each media file referenced by this session,
                        # check its baked audio_signature against the
                        # local file.  Mismatch → WARN and DROP the
                        # session so the pool-mix path (or fresh
                        # transcribe) takes over.  Legacy sessions
                        # without audio_signatures pass silently.
                        _sigs = _data.get("audio_signatures") or {}
                        _sig_bad = False
                        _sig_msgs = []
                        for _mp in (_data.get("media") or []) + (
                                [_data.get("audio_cache")] if _data.get("audio_cache") else []):
                            if not _mp:
                                continue
                            _sig = _sigs.get(_mp)
                            if not _sig:
                                # Try basename fallback for relocated paths.
                                _bn = os.path.basename(_mp)
                                for _k, _v in _sigs.items():
                                    if os.path.basename(_k) == _bn:
                                        _sig = _v
                                        break
                            if not _sig:
                                continue   # no signature to verify — legacy
                            _ok, _reason = engines.audio_signature_matches(_sig, _mp)
                            if not _ok:
                                _sig_bad = True
                                _sig_msgs.append(
                                    "{}: {}".format(os.path.basename(_mp), _reason))
                        if _sig_bad:
                            self._log_line(
                                "  [{}] pq_session REJECTED — audio signature "
                                "mismatch; falling back to pool mix.  {}".format(
                                    _tok, " | ".join(_sig_msgs)),
                                WARN)
                            continue   # don't populate pq_session_data
                        pq_session_data[_tok] = _data
                        _reloc_note = ("  [{} paths relocated]".format(_relocs)
                                       if _relocs else "")
                        self._log_line(
                            "  [{}] using Pull Quotes transcript ({} words){}".format(
                                _tok, len(_data["transcript"]), _reloc_note),
                            SUCCESS)
                except Exception as _exc:
                    self._log_line(
                        "  [{}] failed to load session JSON: {}".format(
                            _tok, _exc), WARN)
        self._pq_session_data = pq_session_data   # used by process_token_pulls

        transcript_sources   = {}
        for tok, paths in token_audio_paths.items():
            # Pool-mix-wins rule: when the pool has >=2 audio files for
            # this token, the pool is the source of truth — remix + re-
            # transcribe fresh (using the fixed mix algorithm), ignoring
            # any pq_session's stale .pb_audio.wav / transcript that
            # may have been baked from a bad mix.  Single-file tokens
            # (0 or 1 pool files) fall back to the pq_session when one
            # exists, because there's nothing new to mix.
            _pool_audios = [p for p in (paths or []) if not is_video(p)]
            if tok in pq_session_data and len(_pool_audios) < 2:
                transcript_sources[tok] = None
                self._log_line(
                    "  [{}] pq_session wins (pool has {} audio file{})".format(
                        tok, len(_pool_audios),
                        "s" if len(_pool_audios) != 1 else ""),
                    SUB)
                continue
            if tok in pq_session_data and len(_pool_audios) >= 2:
                # Drop the pq_session for this token so the mix path
                # below fires cleanly — otherwise process_token_pulls
                # would still see pq_session_data[tok] and use its
                # transcript.
                pq_session_data.pop(tok, None)
                self._log_line(
                    "  [{}] pool-mix wins ({} audio files); "
                    "pq_session ignored (would be stale)".format(
                        tok, len(_pool_audios)),
                    SUB)
            if not paths:
                transcript_sources[tok] = None
            elif len(paths) == 1:
                transcript_sources[tok] = paths[0]
                self._log_line(
                    "  [{}] source: {}".format(
                        tok, os.path.basename(paths[0])), SUB)
            else:
                try:
                    # Peek at the content-addressed mix path BEFORE
                    # calling — if it already exists, mix_for_transcript
                    # will silently return it and we want the log to
                    # reflect a cache hit rather than pretending we
                    # just re-mixed.  Falls back to False when the
                    # cache dir isn't set, in which case the temp-file
                    # branch always runs the mix.
                    _mix_cache_p = None
                    try:
                        _mix_cache_p = engines._stable_mix_cache_path(paths)
                    except Exception:
                        _mix_cache_p = None
                    _was_cached = bool(
                        _mix_cache_p and os.path.isfile(_mix_cache_p))
                    tmp = engines.mix_for_transcript(paths)
                    transcript_sources[tok] = tmp
                    self._temp_mix_files.append(tmp)
                    if _was_cached:
                        self._log_line(
                            "  [{}] mix cached ({} tracks) → {}".format(
                                tok, len(paths), os.path.basename(tmp)),
                            SUCCESS)
                    else:
                        self._log_line(
                            "  [{}] mixed {} tracks → {}".format(
                                tok, len(paths), os.path.basename(tmp)), SUB)
                except Exception as exc:
                    self._log_line(
                        "  [{}] mix failed ({}); falling back to first track".format(
                            tok, exc), WARN)
                    transcript_sources[tok] = paths[0]
        # Live concurrency control — shared with the UI toggle so Fast↔Background
        # switching takes effect immediately without restarting the run.
        _live_workers = getattr(self, "_reconcile_live_workers", None)
        _cond         = getattr(self, "_reconcile_live_cond",    None)
        _active_ref   = getattr(self, "_reconcile_live_active",  None)
        if _live_workers is None:
            _cpu = os.cpu_count() or 2
            n_workers     = n_workers or max(1, _cpu - 1)
            _live_workers = [n_workers]
            _cond         = threading.Condition()
            _active_ref   = [0]

        def _gate_in():
            with _cond:
                while _active_ref[0] >= _live_workers[0]:
                    _cond.wait(timeout=0.3)
                _active_ref[0] += 1

        def _gate_out():
            with _cond:
                _active_ref[0] -= 1
                _cond.notify_all()

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

        # Log gate concurrency level so we can verify fast vs background mode
        _lw_val = _live_workers[0] if _live_workers else "?"
        self._log_line(
            "Concurrency: {} worker(s)  [{}]".format(
                _lw_val,
                "background" if _lw_val == 1 else "fast"),
            SUB)

        # Pre-load Whisper model before any gated work starts.
        # Without this, the first thread to need it loads the model while holding
        # a gate slot — freezing progress for 60+ seconds with nothing visible.
        if HAS_WHISPER:
            try:
                # Read the ACTIVE model size (picker override or config
                # default) rather than the raw WHISPER_MODEL config
                # constant.  engines.get_model() below picks the same
                # value; without this the log printed "small" even when
                # the user had set the picker to medium (reported), even
                # though the actually-loaded model was medium.
                self._log_line(
                    "Loading model ({})…".format(
                        engines.get_active_model_size()), INFO)
                engines.get_model()
                self._log_line("Model ready.", SUCCESS)
            except Exception as e:
                self._log_line("Model load failed: {}".format(e), ERR)
                self._elapsed_running = False
                # Clear the busy flags the caller set before launching
                # this worker thread — otherwise the spinner dots keep
                # animating and the model picker stays gated behind a
                # warning dialog until app restart.
                self._ui(self._stop_reconcile_runners)
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
                    # ── Log why both caches missed (helps diagnose stale/moved files) ──
                    _why = []
                    _cp = engines._cache_path(ap) if engines._cache_dir else None
                    if not engines._cache_dir:
                        _why.append(".pb_cache: no cache dir")
                    elif not _cp or not os.path.exists(_cp):
                        _why.append(".pb_cache: file not found")
                    else:
                        try:
                            with open(_cp, encoding="utf-8") as _cf:
                                _cd = json.load(_cf)
                            _exp = os.path.abspath(ap)
                            if _cd.get("path") != _exp:
                                _why.append(".pb_cache: path mismatch")
                            elif abs(_cd.get("mtime", 0) - os.path.getmtime(ap)) > 1:
                                _why.append(".pb_cache: mtime stale")
                            elif _cd.get("version") != CACHE_VERSION:
                                _why.append(".pb_cache: version mismatch")
                            else:
                                _why.append(".pb_cache: load error")
                        except Exception:
                            _why.append(".pb_cache: read error")
                    _pbt = engines.pb_transcript_path(ap)
                    if not os.path.isfile(_pbt):
                        _why.append("no .pb_transcript.json")
                    else:
                        try:
                            with open(_pbt, encoding="utf-8") as _pf:
                                _pd = json.load(_pf)
                            _pm = os.path.getmtime(ap)
                            if abs(_pd.get("mtime", 0) - _pm) > 2:
                                _why.append(".pb_transcript: mtime stale")
                            elif not _pd.get("words"):
                                _why.append(".pb_transcript: no words")
                            else:
                                _why.append(".pb_transcript: load error")
                        except Exception:
                            _why.append(".pb_transcript: read error")
                    self._log_line(
                        "  Part {} Take {}: cache miss ({}) — transcribing fresh".format(
                            pi, take_i + 1, "  ·  ".join(_why)),
                        WARN)

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
                    # ALSO write the .pb_transcript.json sidecar next to
                    # the source audio.  The internal cache above is keyed
                    # by md5 in .pb_cache/ so only reconcile can see it;
                    # the sidecar makes the same transcript reachable from
                    # Pull Quotes (USE EXISTING on drop), which is how the
                    # user searches transcript text after reconcile.  The
                    # interview-source path already writes both; this
                    # brings VO takes into parity.
                    try:
                        engines.pb_transcript_save(ap, audio_words, blobs=blobs)
                    except Exception:
                        pass
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

        # ── VO transcription via gated raw threads ────────────────────────────
        # Each thread acquires the live-worker gate before doing heavy work, so
        # Fast↔Background toggling takes effect immediately.  Threads also submit
        # their reconcile future as soon as transcription completes (pipelining).
        _new_recon_q = queue.Queue()   # VO reconcile futures added mid-run

        # Shared progress counter: each individual pull bumps this when it
        # completes.  The stall watchdog (below) treats counter advancement
        # as "alive" so a long-running token batch (e.g. 26 pulls × ~100s)
        # doesn't trip the 5-minute future-level timeout while it's still
        # making per-pull progress.  Mutable list so closures share the int.
        _pull_progress = [0]

        # Auto-promoted full-audio transcribes need to be serialised: the
        # shared WhisperModel + CTranslate2 GPU backend can only really run
        # one transcription at a time, and dispatching 7+ in parallel just
        # causes pathological contention (15+ min with no completions).
        # Pair with the heartbeat thread inside process_token_pulls so
        # tokens waiting on the semaphore don't trip the stall watchdog.
        _full_transcribe_sem = threading.BoundedSemaphore(
            max(1, FULL_TRANSCRIBE_CONCURRENCY))

        _t_rec_start = time.perf_counter()

        def process_token_pulls(token, token_pulls):
            """Process all pulls for a single token sequentially with a time cursor."""
            results = []
            cursor  = 0.0
            tsrc    = transcript_sources.get(token)
            pad     = pad_secs
            # Pulls confirmed in the previous run are passed through as-is so
            # the user can fix problem tokens via ← BACK without re-running
            # work they've already approved.
            _carryover = getattr(self, "_confirmed_carryover", None) or {}

            self._log_line(
                "  [{}] starting {} pull(s)  src={}{}".format(
                    token, len(token_pulls),
                    os.path.basename(tsrc) if tsrc else "none",
                    "  [{} confirmed carried over]".format(
                        sum(1 for p in token_pulls if p.get("order") in _carryover))
                    if _carryover else ""),
                SUB)

            # ── Pick the fastest available path for this whole token ──────────
            # Per-pull Whisper has high fixed overhead (model warmup, audio
            # decode, VAD).  When a token has many pulls, transcribing the
            # full audio once + looking up word ranges is dramatically faster
            # AND produces a cached transcript that subsequent runs reuse for
            # free.  Three sources, in priority order:
            #   1. A Pull Quotes session JSON registered for this token —
            #      best, includes speaker diarisation
            #   2. A .pb_transcript.json sitting next to the audio (from any
            #      prior run, VO transcription, or PQ session)
            #   3. Auto-promote: transcribe the full audio now if pulls ≥
            #      AUTO_FULL_TRANSCRIBE_THRESHOLD and the source exists
            _pq_data = (getattr(self, "_pq_session_data", None) or {}).get(token)
            if _pq_data is None and tsrc:
                _pb_words, _ = engines.pb_transcript_load(tsrc)
                if _pb_words:
                    _pq_data = {
                        "transcript":  _pb_words,
                        "audio_cache": tsrc,
                        "media":       [tsrc],
                        "id":          token,
                        "_file_path":  tsrc,
                    }
                    self._log_line(
                        "  [{}] using cached full-audio transcript ({} words)".format(
                            token, len(_pb_words)),
                        SUCCESS)
                elif (len(token_pulls) >= AUTO_FULL_TRANSCRIBE_THRESHOLD
                      and not self._cancel.is_set()):
                    self._log_line(
                        "  [{}] {} pulls + no cached transcript — transcribing full "
                        "audio once (much faster than {} per-pull calls)".format(
                            token, len(token_pulls), len(token_pulls)),
                        INFO)
                    # Heartbeat: a full-audio transcribe can take 5-15 minutes,
                    # AND tokens queued waiting on _full_transcribe_sem can sit
                    # idle even longer.  Without a heartbeat the stall watchdog
                    # would mark this future abandoned at the 300s timeout.
                    # Bump _pull_progress every 30s so the watchdog sees the
                    # future is alive (whether transcribing or queued).
                    _hb_stop = threading.Event()
                    def _heartbeat():
                        while not _hb_stop.wait(30.0):
                            _pull_progress[0] += 1
                    _hb_thread = threading.Thread(
                        target=_heartbeat, daemon=True)
                    _hb_thread.start()
                    try:
                        # Serialise full-audio transcribes: see comment on
                        # _full_transcribe_sem above for why this matters.
                        _t_wait = time.perf_counter()
                        with _full_transcribe_sem:
                            _wait_s = time.perf_counter() - _t_wait
                            if _wait_s > 5.0:
                                self._log_line(
                                    "  [{}] starting full transcribe after {:.0f}s "
                                    "waiting in queue".format(token, _wait_s),
                                    SUB)
                            _t_full = time.perf_counter()
                            # Throttled progress logger — every 10s emit a
                            # progress line so the user sees "[TOKEN]
                            # transcribing 5:23 of 32:45 (16%)" rather than
                            # silence for 5+ minutes per token.
                            _last_prog_log = [0.0]
                            def _prog_cb(frac, msg, _tok=token):
                                now = time.perf_counter()
                                if now - _last_prog_log[0] < 10.0:
                                    return
                                _last_prog_log[0] = now
                                self._log_line(
                                    "  [{}] {}".format(_tok, msg), SUB)
                            _full_words, _full_blobs = engines.transcribe_file(
                                tsrc, progress_cb=_prog_cb)
                            if _full_words:
                                engines.pb_transcript_save(
                                    tsrc, _full_words, blobs=_full_blobs)
                                _pq_data = {
                                    "transcript":  _full_words,
                                    "audio_cache": tsrc,
                                    "media":       [tsrc],
                                    "id":          token,
                                    "_file_path":  tsrc,
                                }
                                self._log_line(
                                    "  [{}] full transcript ready ({} words, "
                                    "{:.1f}s) — cached as .pb_transcript.json".format(
                                        token, len(_full_words),
                                        time.perf_counter() - _t_full),
                                    SUCCESS)
                    except Exception as e:
                        self._log_line(
                            "  [{}] full-audio transcribe failed ({}); falling back "
                            "to per-pull Whisper".format(token, e),
                            WARN)
                    finally:
                        _hb_stop.set()

            # Log pull result cache status for this token on first pull
            # (checked AFTER reconcile_interview_pull so we don't double-call
            # os.path.getmtime — which can hang indefinitely on inaccessible files)
            _logged_pull_cache = False
            for pull in token_pulls:
                # ── Carry over confirmed results from the previous run ────────
                if pull.get("order") in _carryover:
                    r = _carryover[pull["order"]]
                    results.append(r)
                    if r.get("rec_out_s", 0.0) > 0.0:
                        cursor = max(cursor, r["rec_out_s"])
                    continue

                if self._cancel.is_set():
                    r = engines._base_result(pull)
                    r["status"] = "cancelled"
                    results.append(r)
                    continue
                _pull_t0 = time.perf_counter()
                # Only enforce the temporal ordering cursor when this pull's
                # source in-point is at or after the cursor.  If the script
                # uses clips non-chronologically (narrative order ≠ source
                # file order), the cursor would otherwise filter out ALL words
                # from legitimately earlier parts of the interview.
                _pull_in_s = pull.get("in_seconds", 0.0)
                _effective_cursor = (cursor
                                     if _pull_in_s >= cursor - engines._PULL_ORDERING_LOOKBACK
                                     else 0.0)

                # Fast path: a full transcript is available for this token
                # (Pull Quotes session, cached .pb_transcript.json, or auto-
                # promoted earlier in process_token_pulls).  Script timecodes
                # are precise enough to use directly — just look up the words
                # in the cached transcript.  Otherwise fall back to per-pull
                # Whisper on a padded window.
                if _pq_data is not None:
                    r = engines.reconcile_pull_from_session(pull, _pq_data)
                else:
                    r = engines.reconcile_interview_pull(
                        pull, tsrc, pad=pad,
                        min_start_s=_effective_cursor)
                # source_audio is set to tsrc (the mix WAV) inside
                # reconcile_interview_pull.  We keep it pointing there — the mix
                # file stays alive through Step 4 so the waveform editor shows the
                # full blended audio instead of just one track.
                _pull_elapsed = time.perf_counter() - _pull_t0
                # ── Pull cache diagnostic (once per token, after the call) ──────
                if not _logged_pull_cache:
                    _logged_pull_cache = True
                    if r.get("from_cache"):
                        self._log_line(
                            "  ↳ PULL cache HIT for '{}'".format(token), SUCCESS
                        )
                    else:
                        if not tsrc:
                            _miss = "no transcript source"
                        elif not engines._cache_dir:
                            _miss = ".pb_cache dir not set"
                        else:
                            _miss = "no cached result (status: {})".format(
                                r.get("status", "?"))
                        self._log_line(
                            "  ↳ PULL cache MISS for '{}': {}".format(token, _miss), WARN
                        )
                # Per-pull timing log — helps identify which specific pulls are slow
                _diag = r.get("_diag", "")
                # Surface script-conform success: when alignment produced
                # internal cuts, mention the cut count + alignment ratio.
                if r.get("_script_conformed"):
                    _cuts  = r.get("n_internal_cuts", 0)
                    _ratio = r.get("_conform_ratio")
                    _suffix = (" · script-conformed {:.0%} match, "
                               "{} cut{}".format(
                                   _ratio or 0.0,
                                   _cuts, "" if _cuts == 1 else "s"))
                    _diag = (_diag + _suffix) if _diag else _suffix.lstrip(" ·")
                self._log_line(
                    "    pull #{} '{}' → {}{}  ({:.1f}s)".format(
                        pull.get("order", "?"), token,
                        r.get("status", "?"),
                        "  [{}]".format(_diag) if _diag else "",
                        _pull_elapsed),
                    SUB)
                # ─────────────────────────────────────────────────────────────────
                if not tsrc:
                    vid = next((p for p in int_assets.get(token, [])
                                if is_video(p)), None)
                    if vid and not r.get("source_audio"):
                        r["source_video"] = vid
                results.append(r)
                # Bump the shared progress counter so the stall watchdog
                # in the main reconcile loop knows we're still making
                # progress even though this token's future hasn't returned.
                _pull_progress[0] += 1
                if r.get("status") in ("ok", "low_confidence", "snapped") \
                        and r.get("rec_out_s", 0.0) > 0.0:
                    cursor = max(cursor, r["rec_out_s"])

            self._log_line(
                "  [{}] done — {} pull(s) completed".format(token, len(results)),
                SUB)
            return results

        def process_vo_part(part_index, blocks):
            if self._cancel.is_set():
                return [dict(engines._vo_base_result(b), status="cancelled")
                        for b in blocks]
            takes = vo_takes_by_part.get(part_index, [])
            fresh = engines.reconcile_vo_part(blocks, takes)
            # Same order-keyed passthrough as process_token_pulls above:
            # a VO block the user already confirmed/ignored in a prior
            # Step 4 pass is returned as-is instead of overwriting the
            # user's approved result with fresh algo output.  Without
            # this every confirmed VO row silently reverts on ← BACK.
            _carryover = getattr(self, "_confirmed_carryover", None) or {}
            if not _carryover:
                return fresh
            return [_carryover.get(r.get("order"), r) for r in fresh]

        from collections import defaultdict as _dd
        _vo_by_part = _dd(list)
        for vb in self.vo_blocks:
            _vo_by_part[vb["part_index"]].append(vb)

        _pulls_by_token = _dd(list)
        for p in pulls:
            _pulls_by_token[p["token"]].append(p)
        for _tok in _pulls_by_token:
            _pulls_by_token[_tok].sort(key=lambda p: p["order"])

        # Total expected results (one per pull + one per individual VO block)
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

        def dispatch_gated(item):
            """Dispatch one reconcile item, obeying the live worker gate."""
            _gate_in()
            try:
                return dispatch(item)
            finally:
                _gate_out()

        # ── Reconcile loop ─────────────────────────────────────────────────────
        # Pool is sized to hold all possible items; the gate controls concurrency.
        # Pull-token items are submitted IMMEDIATELY so interview pull reconciliation
        # runs concurrently with VO transcription (pipelining).  VO-part items are
        # submitted by the VO transcription threads as each part completes.
        _total_recon_items = len(_pulls_by_token) + len(_vo_by_part)
        ex = ThreadPoolExecutor(max_workers=max(1, _total_recon_items))
        futures = {}   # fut → item (all futures ever submitted)

        # Submit interview-pull items now — they don't depend on VO transcription
        for tok, tpulls in _pulls_by_token.items():
            if not self._cancel.is_set():
                item = ("pull_token", (tok, tpulls))
                fut  = ex.submit(dispatch_gated, item)
                futures[fut] = item

        # VO transcription: one thread per part with its own semaphore — completely
        # independent from the reconcile gate so stalled reconcile futures cannot
        # block VO transcription from starting.
        _n_fast_ref = getattr(self, "_reconcile_n_fast", MAX_WORKERS)
        _n_vo_concurrent = max(1, min(len(self.vo_bins), (_n_fast_ref + 1) // 2))
        _vo_sem = threading.Semaphore(_n_vo_concurrent)
        _vo_done_event = threading.Event()

        def _vo_thread(pi, vb):
            _vo_sem.acquire()
            try:
                part_takes = _transcribe_vo_part(pi, vb)
            except Exception as e:
                self._log_line(
                    "VO Part {}: transcription error — {}".format(pi, e), ERR)
                part_takes = []
            finally:
                _vo_sem.release()
            if part_takes:
                vo_takes_by_part[pi] = part_takes
            # Submit VO reconcile immediately — takes are populated above
            if not self._cancel.is_set():
                blocks = _vo_by_part.get(pi, [])
                if blocks:
                    item = ("vo_part", (pi, blocks))
                    fut  = ex.submit(dispatch_gated, item)
                    futures[fut] = item
                    _new_recon_q.put(fut)

        _vo_threads = []
        for pi, vb in self.vo_bins.items():
            if not self._cancel.is_set():
                t = threading.Thread(target=_vo_thread, args=(pi, vb), daemon=True)
                t.start()
                _vo_threads.append(t)

        def _watch_vo():
            for t in _vo_threads:
                t.join()
            if _total_tx:
                _tx_total_s = time.perf_counter() - _t_tx_start
                self._set_tx_progress(_total_tx, _total_tx,
                                      "VO transcription complete")
                self._log_line(
                    "VO transcription total: {}  ({} fresh  ·  {} cached)".format(
                        self._fmt_duration(_tx_total_s), _tx_fresh, _tx_cached),
                    INFO)
                if _chunk_counts:
                    _all_n = [n for _, _, n in _chunk_counts]
                    self._log_line(
                        "  Chunks — min: {}  max: {}  avg: {:.1f}  total: {}".format(
                            min(_all_n), max(_all_n),
                            sum(_all_n) / len(_all_n), sum(_all_n)), INFO)
            _vo_done_event.set()
        threading.Thread(target=_watch_vo, daemon=True).start()

        pending       = set(futures.keys())
        _stall_t      = time.perf_counter()
        _stall_logged = set()
        _last_pull_n  = _pull_progress[0]   # baseline for sub-future progress

        try:
            while pending or not _vo_done_event.is_set() or not _new_recon_q.empty():
                # Pick up any VO reconcile futures submitted since last iteration
                while True:
                    try:
                        pending.add(_new_recon_q.get_nowait())
                    except queue.Empty:
                        break

                if self._cancel.is_set():
                    for f in pending:
                        f.cancel()
                    break

                if not pending:
                    # VO still transcribing; no reconcile items queued yet
                    time.sleep(0.3)
                    continue

                # Wait up to 2 s for at least one future to finish
                done_set, pending = _fut_wait(
                    pending, timeout=2.0, return_when=FIRST_COMPLETED)

                # Pick up VO reconcile futures submitted while we waited
                while True:
                    try:
                        pending.add(_new_recon_q.get_nowait())
                    except queue.Empty:
                        break

                # ── Still no completions? log stall milestones ───────────────
                if not done_set:
                    # Sub-future progress also counts as "alive": if any
                    # individual pull inside a still-pending token batch
                    # completed since the last loop, reset the stall clock
                    # so a long-running token doesn't trip the hard timeout
                    # while it's actually making progress one pull at a time.
                    if _pull_progress[0] != _last_pull_n:
                        _last_pull_n  = _pull_progress[0]
                        _stall_t      = time.perf_counter()
                        _stall_logged = set()
                        continue

                    stall_s = time.perf_counter() - _stall_t
                    n_left  = len(pending)

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
                        break   # exit outer loop — proceed to summary

                    continue

                # ── Progress resumed — reset stall clock ────────────────────
                _stall_t      = time.perf_counter()
                _stall_logged = set()
                _last_pull_n  = _pull_progress[0]

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
                            self._prog_bar.set(d, total)
                            self._prog_lbl.config(
                                text="Matching pulls — {} / {}".format(d, total))
                        self._ui(_update)
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

        self._elapsed_running = False

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
            # Same flag-cleanup as the happy path — the user cancelled
            # mid-reconcile, but the spinner dots / resource poller /
            # busy flag must still be released.
            self._ui(self._stop_reconcile_runners)
            self._ui(self._step2)
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
        # Merge with any user-approved edits made in Step 4 while this
        # reconcile was running in the background (provisional-first flow).
        # Rows the user already marked _s4_accepted or _s4_ignored win —
        # their TCs and any manual segments are preserved verbatim.  Every
        # other row is replaced by the fresh reconciled result.
        _prev = getattr(self, "results", None) or []
        _kept = {r["order"]: r for r in _prev
                 if r.get("_s4_accepted") or r.get("_s4_ignored")}
        if _kept:
            sorted_results = [_kept.get(r["order"], r) for r in sorted_results]

        self.results          = sorted_results
        self._vo_takes_by_part = vo_takes_by_part
        self._pending_summary = sum_lines   # consumed by _finish_reconcile

        self._ui(self._finish_reconcile)

    def _stop_reconcile_runners(self):
        """Clear the four flags that gate the spinner / resource poller /
        Settings-model-picker / provisional-first strip.  _finish_reconcile
        clears them on the happy path, but the cancel branch and the
        model-load-failure branch of _run_reconcile used to exit without
        clearing — leaving the dots animating forever and the model picker
        silently blocked behind a warning dialog until the app restarted."""
        self._dot_running         = False
        self._res_monitor_running = False
        self._reconcile_busy      = False
        self._bg_reconcile_active = False

    def _finish_reconcile(self):
        """Called on the main thread after _run_reconcile completes.
        Appends the pre-computed summary lines to the log widget then
        transitions to Step 4.  All Tk operations happen here, safely."""
        self._stop_reconcile_runners()
        for txt, color in getattr(self, "_pending_summary", []):
            self._log_line(txt, color)
        self._pending_summary = []
        self._confirmed_carryover = {}   # consumed; clear for next run
        self._bg_reconcile_active = False   # provisional-first strip auto-hides
        # Mix files (_temp_mix_files) are intentionally kept alive here so the
        # waveform editor can use them during Step 4.  They are cleaned up at the
        # start of the next reconcile run, or when the window closes.
        # Snapshot the conform output BEFORE any user edits so _s4_save
        # can diff against it later — that diff is the telemetry that
        # reveals where the algorithm and the editor systematically
        # disagree.
        try:
            self._write_conform_baseline()
        except Exception:
            pass
        # Rebuild Step 4 to show fully-reconciled rows.  This covers both
        # cases: (a) user is still on Step 3, so this is the normal
        # transition; (b) user jumped to Step 4 via → REVIEW NOW, so the
        # rebuild swaps their provisional rows for the merged real ones
        # (confirmed/ignored preserved — see the merge in _run_reconcile).
        self.after(800, self._step4)

    def _cancel_reconcile(self):
        if not messagebox.askyesno(
                "Cancel reconciliation?",
                "Stop the current reconcile run?\n\n"
                "Work completed so far is already cached and will be skipped on the next run.",
                default="no"):
            return
        self._cancel.set()
        self._dot_running = False
        self._log_line("Cancel requested — stopping after current items finish…", WARN)
        # Mark any in-flight transcription tasks as cancelled so the
        # top panel doesn't keep them pinned as "transcribing…" while
        # the worker winds down.
        try:
            for tid, state in list(self._xc_tasks.items()):
                if state.get("phase") in ("done", "cached", "error"):
                    continue
                self._xc_task_update(
                    tid, phase="error",
                    detail="cancelled")
        except Exception:
            pass
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

        # Step 4 always opens with a clean slate — reconcile is fast (cached)
        # so there is no benefit to carrying over confirmed/ignored state.
        self._restore_s4       = False
        self._pending_s4_state = None

        # Reset undo/redo stacks for this Step 4 session
        self._s4_undo_stack = []
        self._s4_redo_stack = []

        self._clear()
        self._section("STEP 4 — REVIEW")

        # ── Background-reconcile progress strip ─────────────────────────
        # When the user jumped into Step 4 via → REVIEW NOW while
        # transcribe is still running (provisional-first flow), show
        # a small always-visible strip up top counting provisional
        # rows still awaiting a real Whisper match.  Auto-hides when
        # _finish_reconcile fires and clears _bg_reconcile_active.
        if getattr(self, "_bg_reconcile_active", False):
            _bg_strip = tk.Frame(self.body, bg=SURF,
                                 highlightbackground=INFO,
                                 highlightthickness=1)
            _bg_strip.pack(fill="x", pady=(0, 8))
            _spin_lbl = tk.Label(_bg_strip, text="…", font=FL, bg=SURF, fg=INFO,
                                 padx=10, pady=6)
            _spin_lbl.pack(side="left")
            _msg_lbl = tk.Label(_bg_strip,
                                text="Transcribing in background — provisional "
                                     "rows will upgrade as tokens finish.",
                                font=FB, bg=SURF, fg=TEXT, anchor="w")
            _msg_lbl.pack(side="left", padx=(0, 8), fill="x", expand=True)
            _prov_n = sum(1 for r in self.results
                          if r.get("status") == "provisional")
            _count_lbl = tk.Label(
                _bg_strip,
                text="{} provisional".format(_prov_n),
                font=FB, bg=SURF, fg=SUB, padx=10)
            _count_lbl.pack(side="right")
            self._bg_strip_frame = _bg_strip
            self._bg_strip_count_lbl = _count_lbl
            self._bg_strip_msg_lbl   = _msg_lbl
            # Simple dot-cycle spinner tied to the strip's lifetime.
            def _tick_spin(step=0, lbl=_spin_lbl, frm=_bg_strip):
                try:
                    if not frm.winfo_exists():
                        return
                    lbl.config(text=("·  ", "·· ", "···", " ··",
                                     "  ·", "   ")[step % 6])
                except tk.TclError:
                    return
                self.after(300, _tick_spin, step + 1)
            self.after(300, _tick_spin)

        tk.Label(self.body,
                 text="Reconcile log.  "
                      "[~] low confidence    [?] unmatched fallback    "
                      "These are flagged by name in the exported XML.",
                 font=FB, bg=BG, fg=SUB, wraplength=860).pack(anchor="w", pady=(0,6))

        OK_STATUSES     = self.OK_STATUSES
        REVIEW_STATUSES = self.REVIEW_STATUSES
        ok     = sum(1 for r in self.results if r["status"] in OK_STATUSES)
        review = sum(1 for r in self.results if r["status"] in REVIEW_STATUSES)
        skips  = sum(1 for r in self.results if r["status"] in ("no_file", "cancelled"))

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
            if status in OK_STATUSES:
                _ok_count[0] = max(0, _ok_count[0] - 1)
                _ok_var.set(str(_ok_count[0]))
                _ok_num_lbl.config(fg=SUCCESS if _ok_count[0] > 0 else SUB)
            elif status in REVIEW_STATUSES:
                _review_count[0] = max(0, _review_count[0] - 1)
                _review_var.set(str(_review_count[0]))
                _review_num_lbl.config(fg=WARN if _review_count[0] > 0 else SUB)
            _unconfirmed_count[0] = max(0, _unconfirmed_count[0] - 1)
            _sync_unconfirmed_btn()

        def _decrement_confirmed(status=""):
            """Return an item from confirmed back to its source bucket (un-accept)."""
            _confirmed_count[0] = max(0, _confirmed_count[0] - 1)
            _confirmed_var.set(str(_confirmed_count[0]))
            _confirmed_num_lbl.config(fg=SUCCESS if _confirmed_count[0] > 0 else SUB)
            if status in OK_STATUSES:
                _ok_count[0] += 1
                _ok_var.set(str(_ok_count[0]))
                _ok_num_lbl.config(fg=SUCCESS)
            elif status in REVIEW_STATUSES:
                _review_count[0] += 1
                _review_var.set(str(_review_count[0]))
                _review_num_lbl.config(fg=WARN)
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
        # sort_dir: {sort_key: 1 (ascending) or -1 (descending)}.  Clicking
        # the active sort flips its direction; clicking a different sort
        # switches to it in its remembered direction.
        # Persist filter/sort state on self so a mid-review rebuild
        # (e.g. reassign apply, reconcile-merge, status change) doesn't
        # bounce the user back to the default ALL / Script view.
        if not hasattr(self, "_s4_fstate") or not isinstance(
                getattr(self, "_s4_fstate", None), dict):
            self._s4_fstate = {"mode": "all", "sort": "script",
                               "sort_dir": {"script": 1, "alphabetical": 1,
                                            "confidence": 1, "subclips": -1}}
        _fstate = self._s4_fstate
        _fbtns  = {}
        _sbtns  = {}
        _sort_labels = {"script":       "Script",
                        "alphabetical": "Alphabetical",
                        "confidence":   "Confidence",
                        "subclips":     "Sub-clips"}

        _part_dividers = {}   # part_index → divider Frame (filled during card loop)

        def _apply_filter(mode=None, sort=None):
            if mode is not None:
                _fstate["mode"] = mode
            if sort is not None:
                # Same sort clicked again → invert direction.  New sort →
                # switch, keeping that sort's remembered direction.
                if sort == _fstate["sort"]:
                    _fstate["sort_dir"][sort] *= -1
                _fstate["sort"] = sort
            cur_mode = _fstate["mode"]
            cur_sort = _fstate["sort"]
            cur_dir  = _fstate["sort_dir"].get(cur_sort, 1)

            for m, b in _fbtns.items():
                active = (m == cur_mode)
                b.config(bg=ACCENT if active else SURF2,
                         fg=BG    if active else TEXT)
            for s, b in _sbtns.items():
                active = (s == cur_sort)
                arrow  = ("  ↑" if cur_dir > 0 else "  ↓") if active else ""
                b.config(text=_sort_labels[s] + arrow,
                         bg=ACCENT if active else SURF2,
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
                show = (cur_mode == "all" or
                        (cur_mode == "confirmed"   and e["accepted_flag"][0]) or
                        (cur_mode == "unconfirmed" and
                         not e["accepted_flag"][0] and not e["skip_var"].get()))
                if show:
                    visible.append(e)

            # Apply sort.  cur_dir = 1 (asc) or -1 (desc); each branch
            # inverts its own key so ascending is the "natural" direction
            # for that sort (script = top-of-script first, alphabetical =
            # A first, confidence = low first, sub-clips = few first).
            reverse = (cur_dir < 0)
            if cur_sort == "script":
                visible.sort(key=lambda e: e["res"].get("order", 0),
                             reverse=reverse)
            elif cur_sort == "alphabetical":
                def _alpha_key(e):
                    # Sort by token/label so all pulls for a token
                    # group together; secondary key is script order
                    # so within-token rows stay in the order they
                    # appear in the script.
                    return ((e["res"].get("token") or "").strip().lower(),
                            e["res"].get("order", 0))
                visible.sort(key=_alpha_key, reverse=reverse)
            elif cur_sort == "confidence":
                def _conf_key(e):
                    c = e["res"].get("confidence", 0) or 0
                    # Items with no confidence (adjusted/manual) sink to the bottom
                    return (1, 0) if c == 0 else (0, c)
                visible.sort(key=_conf_key, reverse=reverse)
            elif cur_sort == "subclips":
                visible.sort(
                    key=lambda e: len(e["res"].get("segments") or []),
                    reverse=reverse)

            # Repack — only show part dividers in ascending script order
            use_dividers = (cur_sort == "script" and cur_dir > 0)
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
        _conf_initial = sum(1 for r in self.results if r.get("_s4_accepted"))
        for _fm, _fl, _fc in [
            ("all",         "ALL",         len(self.results)),
            ("confirmed",   "CONFIRMED",   _conf_initial),
            ("unconfirmed", "UNCONFIRMED", _unc_initial),
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
        for _sk in ("script", "alphabetical", "confidence", "subclips"):
            b = tk.Label(srow, text=_sort_labels[_sk], font=FB,
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

        # Alias the class-level maps so the existing per-card build code
        # that references bare STATUS_COLOR / STATUS_LABEL keeps working
        # verbatim.  Single source of truth lives at App.STATUS_COLOR /
        # App.STATUS_LABEL now, so surgical patchers (like
        # _s4_patch_card_status) can't drift out of sync.
        STATUS_COLOR = self.STATUS_COLOR
        STATUS_LABEL = self.STATUS_LABEL

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

            # Right-click status → context menu to change/revert it.
            # Solves accidental "adjusted" via stray click in the
            # waveform editor: pick "matched" (or Restore original) and
            # the flag flips back.  Only user-facing statuses are
            # offered — internal/error states aren't picker-safe.
            #
            # A FACTORY, not free-standing defs: previously
            # _change_status / _restore_original were loop-scoped names
            # that got rebound every iteration.  The popup's menu
            # lambdas closed over the ENCLOSING scope's names — which,
            # by the time the user right-clicked, all pointed at the
            # LAST card's version.  So clicking a menu on card N would
            # mutate the last card in self.results.  Wrapping the
            # per-row helpers in a factory rebinds them as locals to
            # each _make_status_popup call, so each stat_lbl gets its
            # own truly captured closures.
            def _make_status_popup(r, sl_w):
                # A "success"-family status means the row belongs in
                # the CONFIRMED bucket if _s4_accepted is True.  Any
                # right-click change AWAY from that family (to a
                # review-y status like low_confidence / no_match /
                # provisional / not_run) implies the user is walking
                # the row BACK to unconfirmed, so drop the accepted
                # flag to keep the state consistent.  Otherwise the
                # row keeps showing in the CONFIRMED tab despite the
                # label saying otherwise.
                _SUCCESS_STATUSES = self.SUCCESS_STATUSES
                def _change(new_status):
                    self._s4_push_undo()
                    if "_original_status" not in r:
                        r["_original_status"] = r.get("status", "")
                    r["status"] = new_status
                    sl_w.config(
                        text=STATUS_LABEL.get(new_status, new_status),
                        fg=STATUS_COLOR.get(new_status, SUB))
                    # If moving off a success status, un-accept so the
                    # CONFIRMED counter drops this row and the card
                    # visual state (stripe + right-side ACCEPTED lbl)
                    # reverts to the default look.  A full rebuild
                    # picks up the new state cleanly.
                    if (new_status not in _SUCCESS_STATUSES
                            and r.get("_s4_accepted")):
                        r.pop("_s4_accepted", None)
                        try:
                            _cv = getattr(self, "_s4_scroll_canvas", None)
                            if _cv is not None:
                                self._s4_pending_scroll_frac = float(_cv.yview()[0])
                        except Exception:
                            pass
                        self._s4_save()
                        self._step4()
                        return
                    self._s4_save()

                def _restore():
                    orig = r.get("_original_status") or ""
                    if not orig or orig == r.get("status"):
                        return
                    self._s4_push_undo()
                    r["status"] = orig
                    r.pop("_original_status", None)
                    sl_w.config(text=STATUS_LABEL.get(orig, orig),
                                fg=STATUS_COLOR.get(orig, SUB))
                    # Same un-accept rule for Restore — if the ORIGINAL
                    # status was a review-y one, don't leave the row
                    # confirmed with a mismatched label.
                    if (orig not in _SUCCESS_STATUSES
                            and r.get("_s4_accepted")):
                        r.pop("_s4_accepted", None)
                        try:
                            _cv = getattr(self, "_s4_scroll_canvas", None)
                            if _cv is not None:
                                self._s4_pending_scroll_frac = float(_cv.yview()[0])
                        except Exception:
                            pass
                        self._s4_save()
                        self._step4()
                        return
                    self._s4_save()

                def _popup(event):
                    m = tk.Menu(self, tearoff=0, bg=SURF, fg=TEXT,
                                activebackground=ACCENT,
                                activeforeground=BG, bd=0)
                    _orig = r.get("_original_status") or ""
                    _cur  = r.get("status", "")
                    if _orig and _orig != _cur:
                        m.add_command(
                            label="Restore original ({})".format(
                                STATUS_LABEL.get(_orig, _orig)
                                    .lstrip("✓✗⚠–… ").strip()),
                            command=_restore)
                        m.add_separator()
                    # Curated status options — user-facing labels only,
                    # skipping internal states (no_quote / error /
                    # no_file / cancelled) that shouldn't be user-settable.
                    for _key in ("ok", "manual", "low_confidence",
                                 "no_match", "provisional", "not_run"):
                        m.add_command(
                            label=STATUS_LABEL.get(_key, _key),
                            command=lambda k=_key: _change(k))
                    try:
                        m.tk_popup(event.x_root, event.y_root)
                    finally:
                        m.grab_release()
                return _popup

            stat_lbl.bind("<Button-3>", _make_status_popup(res, stat_lbl))

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

            # REASSIGN — opens a picker to swap this pull/VO's source
            # file without going through the cross-token phrase search,
            # which only helps when the quote is findable in another
            # already-transcribed sidecar.  Interview and VO share the
            # same entry point; the dialog adapts its layout + apply
            # logic based on res["is_vo"].
            reassign_lbl = tk.Label(_norm_frame, text="REASSIGN", font=FS,
                                    bg=SURF, fg=SUB, cursor="hand2")
            reassign_lbl.pack(side="right", padx=(4, 0))
            reassign_lbl.bind("<Enter>", lambda e, w=reassign_lbl: w.config(fg=ACCENT))
            reassign_lbl.bind("<Leave>", lambda e, w=reassign_lbl: w.config(fg=SUB))
            reassign_lbl.bind(
                "<Button-1>",
                lambda e, r=res: self._s4_reassign_dialog(r))

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
                # Resolve source audio with a fallback ladder so cards
                # from restored sessions / provisional-first / post-
                # reassign flows all open cleanly.  A silent return here
                # was the reason many cards appeared "dead to clicks".
                tried = []
                def _try(path, label):
                    tried.append((label, path or "(unset)"))
                    return path and os.path.isfile(path)

                sa = r.get("source_audio", "") or ""
                if not _try(sa, "source_audio"):
                    sa = r.get("source_video", "") or ""
                    if not _try(sa, "source_video"):
                        sa = ""

                # For VO rows the winning take's apath is the canonical
                # source — try it before any pool guessing.
                if not sa and r.get("is_vo"):
                    _td = r.get("takes_data") or []
                    _bi = r.get("best_take_index", 0) or 0
                    if 0 <= _bi < len(_td) and isinstance(_td[_bi], dict):
                        _apath = _td[_bi].get("apath", "") or ""
                        if _try(_apath, "takes_data[{}].apath".format(_bi)):
                            sa = _apath
                            r["source_audio"] = _apath   # cache

                # Pool fallbacks — prefer audio, then video (existing
                # behavior extended: audio was never tried before).
                if not sa:
                    tok = r.get("token", "")
                    _pool = getattr(self, "_pool", None)
                    if _pool is not None:
                        if r.get("is_vo"):
                            _pi = r.get("part_index", -1)
                            try:
                                _bin = _pool.get_vo_assets().get(_pi, {}) or {}
                            except Exception:
                                _bin = {}
                            _audios = _bin.get("audios", []) or []
                            _videos = _bin.get("videos", []) or []
                        else:
                            _assigned = _pool.get_interview_assets().get(tok, []) or []
                            _audios = [p for p in _assigned if not is_video(p)]
                            _videos = [p for p in _assigned if is_video(p)]

                        _aud = next((p for p in _audios if os.path.isfile(p)), None)
                        if _try(_aud, "pool audio"):
                            sa = _aud
                            r["source_audio"] = _aud   # cache
                        else:
                            _vid = next((p for p in _videos if os.path.isfile(p)), None)
                            if _try(_vid, "pool video"):
                                sa = _vid
                                r["source_video"] = _vid   # cache

                if not sa or not os.path.isfile(sa):
                    # Explain WHY the editor can't open — not silent.
                    _msg  = ("Can't open the waveform editor for #{:03d}  {}\n\n"
                             "No usable source audio/video found.  Sources "
                             "checked:\n\n".format(
                                 r.get("order", 0), r.get("token", "?")))
                    for _lbl, _path in tried:
                        _msg += "  • {:20}  {}\n".format(_lbl, _path)
                    _msg += ("\nUse the REASSIGN button on this card to point "
                             "it at a valid file, or add the missing file to "
                             "the pool via Step 2.")
                    messagebox.showwarning("No source available", _msg, parent=self)
                    return
                segs_r = r.get("segments") or [(0.0, 30.0)]
                fps    = getattr(self, "_seq_fps", 24.0)

                def _accept(new_segs, new_token=None, new_audio_path=None,
                            r=r, af=af, ss=ss, sl=sl, rcl=rcl):
                    self._s4_push_undo()
                    old_status = r.get("status", "")
                    # Cross-token ADOPT from the match-review search: also
                    # reassign this pull to the new token + audio file and
                    # refresh the scripted timecode fields.  segments arg
                    # is a single-hit window (in_s, out_s) from the sidecar.
                    if new_token and new_token != r.get("token"):
                        r["token"] = new_token
                    if new_audio_path:
                        r["source_audio"] = new_audio_path
                        # source_video was a fallback for restored sessions;
                        # once we've reassigned, clear it so future opens
                        # use the new audio path.
                        r.pop("source_video", None)
                        if is_video(new_audio_path):
                            r["source_video"] = new_audio_path
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

                # Build {token: [audio_paths]} from the media pool so the
                # dialog's "🌐 all pulls" search can cross-check the
                # phrase against every OTHER token's transcript sidecar.
                # Used when the assigned token is itself wrong and the
                # quote lives in a completely different audio file.
                _pool_by_token = {}
                try:
                    for _prow in getattr(self._pool, "_rows", []) or []:
                        _tok = _prow["var"].get()
                        _pth = _prow.get("path")
                        if _tok and _pth and _tok != "— unassigned —":
                            _pool_by_token.setdefault(_tok, []).append(_pth)
                except Exception:
                    _pool_by_token = {}

                MatchReviewDialog(self, sa, segs_r,
                                  title=r.get("token", ""),
                                  quote_text=r.get("quote_text", ""),
                                  matched_text=r.get("matched_text", ""),
                                  context_before=_ctx_before,
                                  context_after=_ctx_after,
                                  scripted_tc=_scripted_tc,
                                  words=_words,
                                  on_accept=_accept, fps=fps,
                                  pool_by_token=_pool_by_token)

            # ── Bind labels ────────────────────────────────────────────────────
            ignore_lbl.bind("<Button-1>",
                            lambda e, f=_toggle_ignore: f())
            _acc_state_lbl.bind("<Button-1>",
                                lambda e, f=_un_accept: f())
            _ign_state_lbl.bind("<Button-1>",
                                lambda e, f=_toggle_ignore: f())

            # ── Card click → open waveform editor ──────────────────────────────
            def _hdr_click(event=None, sv=skip_var, fn=_open_review,
                           ti=_toggle_ignore):
                self._s4_active_toggle = ti  # track for X shortcut
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
                # Stashed for surgical single-card updates (see
                # _s4_patch_card_status) so state changes on ONE row
                # don't have to full-rebuild all ~200 cards.
                "stat_lbl":       stat_lbl,
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

        # Re-apply persisted filter mode (persisted via self._s4_fstate);
        # falls back to "all" on first entry.  This is what stops a
        # mid-review rebuild (reassign apply, status change, etc.) from
        # bouncing the user back to the ALL/Script default.
        _apply_filter(mode=self._s4_fstate.get("mode", "all"))

        # Restore scroll fraction requested by the caller (e.g. reassign
        # apply captures yview()[0] pre-rebuild; consumed once here so
        # normal Step 4 entry still opens at the top).
        _pend_scroll = getattr(self, "_s4_pending_scroll_frac", None)
        if _pend_scroll is not None:
            self._s4_pending_scroll_frac = None
            try:
                # Small delay so layout settles before we scroll.
                self.after(30, lambda f=_pend_scroll:
                    self._s4_scroll_canvas.yview_moveto(f))
            except Exception:
                pass

        # If this Step 4 was triggered by a selective re-reconcile, restore the
        # confirmed states for all tokens that were NOT re-reconciled.
        _rr = getattr(self, "_s4_rereconcile_restore", None)
        if _rr is not None:
            self._s4_apply_state(_rr)
            self._s4_rereconcile_restore = None

        # Flush the current (fully-restored) state to the sidecar so that if
        # the user clicks ← BACK and re-runs reconciliation, the next Step 4
        # entry can reload the correct positions from the sidecar rather than
        # showing stale cache-reconciliation results.
        self._s4_save()

        # One-shot clean baseline for an OPENED session that lands at
        # Step 4.  Only fires on open-restore (flag set by _open_session),
        # never on normal forward navigation — so reconciling and landing
        # here still reads as unsaved until the user actually saves.
        if getattr(self, "_pending_mark_saved", False):
            self._pending_mark_saved = False
            self._mark_saved()

        nav = tk.Frame(self.body, bg=BG); nav.pack(side="bottom", fill="x", pady=(8,0))
        self._btn(nav, "← BACK", self._s4_redo_to_step2).pack(side="left")
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

        # Standard Prev/Next/Reconcile trio on the right, matching
        # Step 2's layout.  EXPORT is the "next" step from review.
        # RECONCILE stays available so you can re-run the pipeline
        # without leaving Step 4 — carryover preserves confirmed rows,
        # so a re-run only touches the ones you haven't approved yet.
        self._btn(nav, "NEXT  →  EXPORT", self._step5,
                  color=ACCENT).pack(side="right")
        self._btn(nav, "RECONCILE", self._start_reconcile
                  ).pack(side="right", padx=(0, 8))

        # Keyboard shortcuts for undo/redo
        self.bind_all("<Control-z>",       self._s4_undo)
        self.bind_all("<Control-Z>",       self._s4_undo)
        self.bind_all("<Control-Shift-z>", self._s4_redo)
        self.bind_all("<Control-Shift-Z>", self._s4_redo)

        # X — toggle IGNORE on the most-recently-clicked card
        self.bind_all("<x>", self._s4_x_toggle_ignore)
        self.bind_all("<X>", self._s4_x_toggle_ignore)

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
                    self._ui(lambda: status_var.set(""))
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
            self._ui(_play)

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

        nav = tk.Frame(self.body, bg=BG); nav.pack(side="bottom", fill="x", pady=(8,0))
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

        # If any token was re-reconciled or had its export source fixed, make sure
        # the correct file is first — the pool still points to the originals.
        # The chosen file leads; all other pool files (including the host/Jordan
        # track) are preserved in their original order behind it.
        # Skip any override that points at a transient mix WAV (these live in
        # the OS temp folder and must never reach the export).
        for _tok, _fp in getattr(self, "_rereconcile_src_override", {}).items():
            if not _fp:
                continue
            if os.path.basename(_fp).startswith("_pb_mix_"):
                continue
            existing = int_assets.get(_tok, [])
            others   = [p for p in existing if p != _fp]
            int_assets[_tok] = [_fp] + others

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
        def _confirm_cancel_sync():
            if messagebox.askyesno("Cancel sync detection?",
                                   "Stop sync detection for this source?",
                                   default="no"):
                cancel_evt.set()
        cancel_btn = self._btn(self.body, "CANCEL", _confirm_cancel_sync, small=True)
        cancel_btn.pack(pady=(20, 0))

        def _set_status(msg):
            """Thread-safe status update."""
            self._ui(lambda m=msg: status_lbl.config(text=m))

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
                        # Also dump int_assets and rereconcile_src_override so
                        # we can post-mortem any source-file leak (e.g. a temp
                        # mix WAV ending up in the AAF).
                        _debug_payload = {
                            "clips":      _debug_clips,
                            "int_assets": {tok: list(paths)
                                           for tok, paths in int_assets.items()},
                            "rereconcile_src_override":
                                dict(getattr(self, "_rereconcile_src_override", {}) or {}),
                        }
                        with open(_debug_path, "w", encoding="utf-8") as _df:
                            json.dump(_debug_payload, _df, indent=2)
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
                self._ui(_finish)

            except engines.BuildCancelled:
                def _on_cancel():
                    pbar.stop()
                    _disable_cancel()
                    self._step5()   # return user to config screen
                self._ui(_on_cancel)

            except Exception as exc:
                err = str(exc)

                def _on_err():
                    pbar.stop()
                    _disable_cancel()
                    messagebox.showerror("Build Error", err)
                    self._step5()   # return user to config screen
                self._ui(_on_err)

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
                 text=("[PART Cold Open]                    \u2190 section header\n"
                       "[VO PART_0_NARRATOR]                \u2190 voice-over block\n"
                       "Body text on the next line(s).\n"
                       "Blank lines preserved as paragraph breaks.\n"
                       "\n"
                       "[JENA 00:01:23-00:01:45]            \u2190 interview pull\n"
                       "Quote text below.  Blank lines preserved.\n"
                       "\n"
                       "Second paragraph still in the same pull.\n"
                       "\n"
                       "[JENA 00:02:14-00:02:30]            \u2190 next pull"),
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

        _copy_row(tok_inner, "Copy [TOKENS] Block",
                  lambda: asset_var.get() if self._sf_tokens else "")
        tk.Label(tok_inner,
                 text="Paste this block once near the top of the doc.  "
                      "Tokens are also auto-registered the first time "
                      "they appear in a [TOKEN tc-tc] header, so the "
                      "block is optional.",
                 font=FB, bg=SURF, fg=SUB,
                 wraplength=900, justify="left"
                 ).pack(anchor="w", pady=(6, 0))

        # ── Section 2: [PART name] ────────────────────────────────────
        part_inner = _card("[PART ...] - Insert a Section Header")

        tk.Label(part_inner, text="Part title",
                 font=FB, bg=SURF, fg=SUB).pack(anchor="w")
        part_var = tk.StringVar()
        tk.Entry(part_inner, textvariable=part_var,
                 font=(_SANS, 13),
                 bg=SURF2, fg=TEXT, bd=0, relief="flat",
                 highlightbackground=BORDER, highlightthickness=1,
                 insertbackground=TEXT).pack(fill="x", pady=(4, 8))

        part_preview = tk.Label(part_inner, text="-",
                                font=("Courier New", 12),
                                bg=SURF2, fg=SUB, anchor="w",
                                padx=10, pady=8)
        part_preview.pack(fill="x")

        def _update_part(*_):
            t = part_var.get().strip()
            part_preview.config(
                text=("[PART " + t + "]") if t else "-",
                fg=TEXT if t else SUB)

        part_var.trace_add("write", _update_part)
        _copy_row(part_inner, "Copy [PART ...]",
                  lambda: ("[PART " + part_var.get().strip() + "]")
                  if part_var.get().strip() else "")

        # ── Section 3: [VO id] ────────────────────────────────────────
        vo_inner = _card("[VO ...] - Insert a Voice-Over Header")

        tk.Label(vo_inner, text="VO id (e.g. PART_0_NARRATOR)",
                 font=FB, bg=SURF, fg=SUB).pack(anchor="w")
        vo_var = tk.StringVar(value="PART_0_NARRATOR")
        tk.Entry(vo_inner, textvariable=vo_var,
                 font=(_SANS, 13),
                 bg=SURF2, fg=TEXT, bd=0, relief="flat",
                 highlightbackground=BORDER, highlightthickness=1,
                 insertbackground=TEXT).pack(fill="x", pady=(4, 8))
        vo_preview = tk.Label(vo_inner, text="[VO PART_0_NARRATOR]",
                              font=("Courier New", 12),
                              bg=SURF2, fg=TEXT, anchor="w",
                              padx=10, pady=8)
        vo_preview.pack(fill="x")

        def _upd_vo(*_):
            v = vo_var.get().strip().upper()
            v = re.sub(r"[^A-Z0-9_]+", "_", v).strip("_")
            vo_preview.config(
                text=("[VO " + v + "]") if v else "-",
                fg=TEXT if v else SUB)

        vo_var.trace_add("write", _upd_vo)

        def _copy_vo():
            v = vo_var.get().strip().upper()
            v = re.sub(r"[^A-Z0-9_]+", "_", v).strip("_")
            return ("[VO " + v + "]") if v else ""
        _copy_row(vo_inner, "Copy [VO ...]", _copy_vo)

        # ── Section 4: [TOKEN tc-tc] ──────────────────────────────────
        pull_inner = _card("[TOKEN tc-tc] - Insert an Interview Pull")

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

            line = "[{} {}-{}]".format(tok, in_tc, out_tc)
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

        self._btn(pull_btn_row, "Copy [TOKEN tc-tc]", _copy_pull,
                  small=True).pack(side="left")
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

            # Rebuild [TOKENS] declaration block
            if toks:
                block = "[TOKENS]\n" + "\n".join(toks) + "\n[/TOKENS]"
                asset_var.set(block)
                asset_lbl.config(fg=TEXT)
            else:
                asset_var.set("Enter tokens above")
                asset_lbl.config(fg=SUB)

            _refresh_pull_tokens()

        tok_text.bind("<KeyRelease>", _rebuild_tokens)
        tok_text.bind("<<Paste>>",
                      lambda e: tok_text.after(10, _rebuild_tokens))

    # ── Pull Quotes workflow ──────────────────────────────────────────────────
    # Two-tier model:
    #   • Episode Project (".pb_episode.json")        — list of session paths
    #   • Interview Session (".pb_session.json")      — token + media + transcript
    # Sessions self-save next to their first media file.  Projects are saved
    # explicitly to a user-chosen path.

    def _pq_open_home(self):
        """Entry point: empty in-memory Episode Project workspace."""
        self._pq_project = {
            "title":     "Untitled Episode",
            "sessions":  [],   # list of session dicts (each carries _file_path)
            "file_path": None,
        }
        self._pq_render_project_view()

    def _pq_load_project(self, data, file_path):
        """Load an Episode Project JSON (already-parsed dict)."""
        sessions, missing = [], []
        for sp in data.get("sessions", []) or []:
            loaded = self._pq_load_session_file(sp)
            if loaded is None:
                missing.append(sp)
            else:
                sessions.append(loaded)
        self._pq_project = {
            "title":     data.get("title") or os.path.splitext(
                            os.path.basename(file_path))[0],
            "sessions":  sessions,
            "file_path": file_path,
        }
        if missing:
            messagebox.showwarning(
                "Missing sessions",
                "{} session file(s) referenced by this project could not be "
                "loaded:\n\n{}".format(
                    len(missing),
                    "\n".join("  • " + p for p in missing[:10])))
        self._pq_render_project_view()
        self._mark_saved()   # freshly loaded == clean baseline

    def _pq_open_standalone_session(self, data, file_path):
        """Open a single Interview Session JSON without a project context.
        Wraps it in an unsaved one-session project so the same view applies."""
        session = dict(data)
        session["_file_path"] = file_path
        self._pq_project = {
            "title":     "Untitled Episode",
            "sessions":  [session],
            "file_path": None,
        }
        self._pq_render_project_view()
        self._mark_saved()   # freshly loaded == clean baseline

    def _pq_load_session_file(self, path):
        """Load an Interview Session JSON; return dict or None on error."""
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("workflow") != "interview_session":
                return None
            data["_file_path"] = path
            return data
        except Exception:
            return None

    def _pq_save_session_file(self, session):
        """Write a session dict to its `_file_path`."""
        path = session.get("_file_path")
        if not path:
            return False
        # Stable session ID — generated once per session, future-proof
        # against ambiguous tokens across projects.
        if not session.get("id"):
            import uuid as _uuid
            session["id"] = "ses_" + _uuid.uuid4().hex[:12]
        payload = {
            "version":     3,
            "workflow":    "interview_session",
            "id":          session["id"],
            "token":       session.get("token", ""),
            "media":       list(session.get("media", [])),
            "transcript":  session.get("transcript", []),
        }
        # Path to the playback cache (mix-of-all-sources WAV) is persisted
        # only when it actually exists on disk.
        ac = session.get("audio_cache")
        if ac and os.path.isfile(ac):
            payload["audio_cache"] = ac
        # Per-file audio_signatures (sha256 + duration + sr + channels
        # + size) so cross-machine reconcile can validate the local
        # audio matches what the transcript was made from.  Legacy
        # sessions without this field still load (audio_signature_
        # matches returns True for missing sigs), but new saves start
        # populating it immediately.  Keyed by ABSOLUTE PATH — resolver
        # code that relocates paths at load time strips down to
        # basename lookup when validating, so the map is consulted by
        # basename when abs-path doesn't match.
        _sigs = {}
        for _mp in payload["media"]:
            if _mp and os.path.isfile(_mp):
                _sigs[_mp] = engines.audio_signature(_mp)
        if ac and os.path.isfile(ac) and ac not in _sigs:
            _sigs[ac] = engines.audio_signature(ac)
        if _sigs:
            payload["audio_signatures"] = _sigs
        # Optional per-source speaker label overrides (map of
        # filename → display label).  Only saved when set.
        speakers = session.get("speakers")
        if speakers:
            payload["speakers"] = dict(speakers)
        # Margin notes (Pull Quotes mode = margin)
        notes = session.get("notes")
        if notes:
            payload["notes"] = list(notes)
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            # Mirror the word list as a .pb_transcript.json sidecar next
            # to each source audio file so the Script→Session reconcile
            # workflow can re-use this transcript without re-running
            # Whisper.  No-op if the transcript is empty.
            words = session.get("transcript") or []
            if words:
                for media_path in session.get("media", []):
                    try:
                        if os.path.isfile(media_path):
                            engines.pb_transcript_save(media_path, words)
                    except Exception:
                        pass
            return True
        except Exception as e:
            messagebox.showerror("Save Session failed", str(e))
            return False

    def _pq_default_session_path(self, token, media_paths):
        """Pick the canonical save path for a session JSON.

        Preference order:
          1. `<project_root>/03_AUDIO/00_RAW AUDIO/<TOKEN>.pb_session.json`
             — the editorial template's interview-audio folder.  Walks
             up from the first media file looking for `02_MEDIA` +
             `03_AUDIO` siblings (project root marker).
          2. `<first_media_dir>/<TOKEN>.pb_session.json` — fallback when
             the layout isn't recognised.
        """
        if not media_paths:
            return None
        first = os.path.abspath(media_paths[0])

        # Walk up looking for project root.
        cur = os.path.dirname(first)
        for _ in range(8):
            raw = os.path.join(cur, "03_AUDIO", "00_RAW AUDIO")
            if (os.path.isdir(os.path.join(cur, "02_MEDIA"))
                    and os.path.isdir(os.path.join(cur, "03_AUDIO"))
                    and os.path.isdir(raw)):
                return os.path.join(raw, "{}.pb_session.json".format(token))
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent

        return os.path.join(os.path.dirname(first),
                            "{}.pb_session.json".format(token))

    def _pq_save_episode_project(self, prompt_path=False):
        """Write the Episode Project JSON.  Prompts on first save.
        Returns True only when a file was actually written — callers use
        this to gate "SAVED" feedback (a cancelled dialog is not a save)."""
        project = getattr(self, "_pq_project", None)
        if not project:
            return False
        # Refuse to save until the project actually has sessions on disk.
        if not project["sessions"]:
            messagebox.showinfo("Nothing to save",
                "Add at least one Interview Session before saving the project.")
            return False
        path = project.get("file_path")
        if prompt_path or not path:
            suggested = re.sub(r"[^A-Za-z0-9_\- ]+", "_",
                               project.get("title", "Episode")) + ".pb_episode.json"
            path = filedialog.asksaveasfilename(
                title="Save Episode Project",
                initialfile=suggested,
                defaultextension=".json",
                filetypes=[("Pull Quotes Episode Project", "*.pb_episode.json"),
                           ("JSON", "*.json"),
                           ("All", "*.*")])
            if not path:
                return False
        payload = {
            "version":  1,
            "workflow": "episode_project",
            "title":    project.get("title", ""),
            "sessions": [s["_file_path"] for s in project["sessions"]
                         if s.get("_file_path")],
        }
        # Per-session sha256 so cross-machine consumption can validate
        # the .pb_session.json file itself hasn't drifted from what
        # this episode project was built against.  Cheap — session
        # files are tiny (few KB); no ffprobe involved for this one.
        try:
            import hashlib as _hl
            _session_sigs = {}
            for _sp in payload["sessions"]:
                if _sp and os.path.isfile(_sp):
                    _h = _hl.sha256()
                    with open(_sp, "rb") as _f:
                        for _chunk in iter(lambda: _f.read(1 << 20), b""):
                            _h.update(_chunk)
                    _session_sigs[_sp] = {
                        "sha256": _h.hexdigest(),
                        "size":   os.path.getsize(_sp),
                    }
            if _session_sigs:
                payload["session_signatures"] = _session_sigs
        except Exception:
            pass
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            project["file_path"] = path
            self._mark_saved()
            self._pq_render_project_view()
            return True
        except Exception as e:
            messagebox.showerror("Save Episode Project failed", str(e))
            return False

    # ── Project view ─────────────────────────────────────────────────────────

    def _pq_render_project_view(self):
        import re
        # Stop any session-view playback before tearing the view down,
        # otherwise the audio keeps going while the user is browsing.
        try:
            self._pq_stop_playback()
        except Exception:
            pass
        self._clear()
        # Stepping back into the project means we're no longer focused on a
        # single session — clear the marker so background-completion
        # callbacks know to refresh THIS view rather than the (gone) session
        # view.
        self._pq_current_session = None
        # Reset the row-widget registry; rows will repopulate as we render.
        self._pq_row_widgets = {}

        project = self._pq_project

        # Bottom nav (pinned) — only HOME.  SAVE / SAVE AS live in the
        # persistent header bar (same as every other workflow).
        nav = tk.Frame(self.body, bg=BG)
        nav.pack(side="bottom", fill="x", pady=(8, 0))
        self._btn(nav, "← HOME", self._home).pack(side="left")

        self._section("PULL QUOTES — EPISODE PROJECT")

        # Title + file path metadata
        title_row = tk.Frame(self.body, bg=BG)
        title_row.pack(fill="x", pady=(0, 8))
        tk.Label(title_row, text="EPISODE TITLE", font=FL,
                 bg=BG, fg=SUB, width=18, anchor="w").pack(side="left")
        title_var = tk.StringVar(value=project.get("title", ""))
        title_entry = tk.Entry(title_row, textvariable=title_var, font=FB,
                                bg=SURF2, fg=TEXT, insertbackground=TEXT,
                                relief="flat", bd=6)
        title_entry.pack(side="left", fill="x", expand=True)
        def _on_title_change(*_):
            project["title"] = title_var.get().strip() or "Untitled Episode"
        title_var.trace_add("write", _on_title_change)

        fp_row = tk.Frame(self.body, bg=BG)
        fp_row.pack(fill="x", pady=(0, 12))
        tk.Label(fp_row, text="PROJECT FILE", font=FL,
                 bg=BG, fg=SUB, width=18, anchor="w").pack(side="left")
        fp = project.get("file_path") or "— unsaved (use SAVE in the header) —"
        tk.Label(fp_row, text=fp, font=FB, bg=BG,
                 fg=SUCCESS if project.get("file_path") else SUB,
                 anchor="w").pack(side="left", fill="x", expand=True)

        # Project-level controls row: bg-mode + manage-speakers
        ctrl_row = tk.Frame(self.body, bg=BG)
        ctrl_row.pack(fill="x", pady=(0, 12))
        tk.Label(ctrl_row, text="OPTIONS", font=FL,
                 bg=BG, fg=SUB, width=18, anchor="w").pack(side="left")

        # Background-mode checkbox — shared with the session view via
        # self._pq_bg_mode_var (created here if absent), so flipping
        # either keeps the other in sync.
        if not hasattr(self, "_pq_bg_mode_var"):
            self._pq_bg_mode_var = tk.BooleanVar(value=False)
        bg_frame = tk.Frame(ctrl_row, bg=BG)
        bg_frame.pack(side="left")
        bg_ck = tk.Label(bg_frame,
                          text="☑" if self._pq_bg_mode_var.get() else "☐",
                          font=(_SANS, 14), bg=BG,
                          fg=ACCENT if self._pq_bg_mode_var.get() else SUB,
                          cursor="hand2", padx=4)
        bg_ck.pack(side="left")
        bg_lbl = tk.Label(bg_frame, text="Background mode (all sessions)",
                          font=FB, bg=BG, fg=SUB, cursor="hand2")
        bg_lbl.pack(side="left")
        def _toggle_bg_proj():
            new_val = not self._pq_bg_mode_var.get()
            self._pq_bg_mode_var.set(new_val)
            bg_ck.config(text="☑" if new_val else "☐",
                         fg=ACCENT if new_val else SUB)
        bg_ck.bind("<Button-1>",  lambda e: _toggle_bg_proj())
        bg_lbl.bind("<Button-1>", lambda e: _toggle_bg_proj())

        # Inline model picker so the user can change the Whisper size
        # before opening any session.
        self._build_model_picker(ctrl_row, bg=BG).pack(side="left",
                                                       padx=(24, 0))

        # (MANAGE SPEAKERS removed — speaker labels are renamed inline
        # by double-clicking any "JORDAN:" / "JENA:" header in the
        # transcript view.  The bulk dialog implementation is kept on
        # the App object as `_pq_manage_speakers_dialog` for future use.)

        is_empty = not project["sessions"]

        # Single panel that contains: column header (only when populated),
        # one row per existing session, then the always-last "+ ADD
        # INTERVIEW SESSION" affordance.  Putting the add button inside the
        # panel keeps it visually anchored to the list while remaining the
        # obvious next action — empty or full.
        list_outer = tk.Frame(self.body, bg=SURF,
                              highlightbackground=BORDER, highlightthickness=1)
        list_outer.pack(fill="both", expand=True, pady=(0, 12))

        # Column header — pinned at the top of the panel.
        if not is_empty:
            hdr = tk.Frame(list_outer, bg=SURF3)
            hdr.pack(side="top", fill="x")
            for txt, w_ in [("TOKEN", 16), ("FILES", 8),
                            ("WORDS", 12), ("STATUS", 28)]:
                tk.Label(hdr, text=txt, font=FL, bg=SURF3, fg=SUB,
                         anchor="w", padx=12, pady=6, width=w_
                         ).pack(side="left")

        # "+ ADD INTERVIEW SESSION" — built and pinned to the BOTTOM of the
        # panel BEFORE the (scrollable) row area, so it stays reachable no
        # matter how many sessions the project has.  It used to be packed
        # after the rows with no scroll region, so a project with many
        # sessions pushed it past the bottom of the window with no way to
        # reach it (reported by a user with 12 sessions).
        add_pad = tk.Frame(list_outer, bg=SURF2 if is_empty else SURF)
        add_pad.pack(side="bottom", fill="x")
        add_inner = tk.Frame(add_pad, bg=add_pad.cget("bg"))
        add_inner.pack(padx=14, pady=14, anchor="w")
        self._btn(add_inner,
                  "+ ADD INTERVIEW SESSION",
                  self._pq_add_session_dialog,
                  color=ACCENT).pack(side="left")
        if is_empty:
            tk.Label(add_inner,
                     text="    ← start here",
                     font=FB, bg=add_pad.cget("bg"), fg=SUB
                     ).pack(side="left")

        if not is_empty:
            # Session rows go in a scrollable area filling the space between
            # the pinned header and the pinned add button.  _scroll_frame
            # defaults to the app BG; tint it SURF so empty space below the
            # rows matches the panel instead of showing a dark void.
            rows_host = self._scroll_frame(list_outer)
            try:
                rows_host.config(bg=SURF)
                self._last_scroll_canvas.config(bg=SURF)
            except (AttributeError, tk.TclError):
                pass
            for s in project["sessions"]:
                self._pq_render_session_row(rows_host, s)
        else:
            # Empty-state copy — sits above the add affordance.
            tk.Label(list_outer,
                     text="\n  No interview sessions yet.",
                     font=FBT, bg=SURF, fg=TEXT,
                     anchor="w", padx=20).pack(side="top", anchor="w")
            tk.Label(list_outer,
                     text="  Drop interview media into a session, transcribe it,\n"
                          "  and copy quotes as ready-to-paste @PULL blocks.\n",
                     font=FB, bg=SURF, fg=SUB,
                     anchor="w", padx=20, justify="left",
                     pady=(4)).pack(side="top", anchor="w")

        # Incremental status-cell updates while any session is transcribing.
        # No full re-render → no flicker.
        if any(s.get("_progress", {}).get("active")
               for s in project["sessions"]):
            self.after(600, self._pq_tick_project_status)

    def _pq_update_project_row(self, session):
        """Surgical update of one session row's status + word count.
        Falls back to a full re-render if the row widgets aren't around
        (e.g. project view rebuilt under us)."""
        rows = getattr(self, "_pq_row_widgets", None) or {}
        refs = rows.get(id(session))
        if not refs:
            try:
                self._pq_render_project_view()
            except Exception:
                pass
            return
        try:
            text, fg = self._pq_status_for(session)
            refs["status_lbl"].config(text=text, fg=fg)
            refs["words_lbl"].config(
                text="{:,}".format(len(session.get("transcript", []))))
        except tk.TclError:
            try:
                self._pq_render_project_view()
            except Exception:
                pass

    def _pq_tick_project_status(self):
        """Incrementally refresh the status cell of every session row whose
        progress dict says it's still active.  Avoids the twitch of a full
        re-render under self.after."""
        if getattr(self, "_pq_current_session", None) is not None:
            return  # session view is open; project view isn't visible
        if getattr(self, "_pq_project", None) is None:
            return
        rows = getattr(self, "_pq_row_widgets", None) or {}
        any_active = False
        for sess_id, refs in list(rows.items()):
            try:
                if not refs["status_lbl"].winfo_exists():
                    continue
            except Exception:
                continue
            session = refs["session"]
            text, fg = self._pq_status_for(session)
            try:
                refs["status_lbl"].config(text=text, fg=fg)
                refs["words_lbl"].config(
                    text="{:,}".format(len(session.get("transcript", []))))
            except tk.TclError:
                continue
            if session.get("_progress", {}).get("active"):
                any_active = True

        if any_active:
            self.after(600, self._pq_tick_project_status)

    def _pq_status_for(self, session):
        """Return (status_text, fg_color) for a session row."""
        n_media = len(session.get("media", []))
        n_words = len(session.get("transcript", []))
        prog    = session.get("_progress", {})
        if prog.get("active"):
            phase = prog.get("phase", "transcribing")
            extra = prog.get("extra", "")
            return "● {}{}".format(phase, extra), ACCENT
        if n_media == 0:
            return "no media", WARN
        if n_words == 0:
            return "needs transcription", WARN
        return "✓ transcribed", SUCCESS

    def _pq_render_session_row(self, parent, session):
        """Render one row in the project's session list, storing widget
        references in self._pq_row_widgets so tick updates don't rebuild
        the whole tree."""
        row = tk.Frame(parent, bg=SURF, cursor="hand2")
        row.pack(fill="x")
        n_media = len(session.get("media", []))
        n_words = len(session.get("transcript", []))
        status_text, status_fg = self._pq_status_for(session)

        tok_lbl = tk.Label(row, text=session.get("token", "?"),
                            font=FBT, bg=SURF, fg=ACCENT,
                            anchor="w", padx=12, pady=8, width=16)
        tok_lbl.pack(side="left")
        files_lbl = tk.Label(row, text=str(n_media), font=FB,
                              bg=SURF, fg=TEXT, anchor="w",
                              padx=12, pady=8, width=8)
        files_lbl.pack(side="left")
        words_lbl = tk.Label(row, text="{:,}".format(n_words), font=FB,
                              bg=SURF, fg=TEXT, anchor="w",
                              padx=12, pady=8, width=12)
        words_lbl.pack(side="left")

        # Inline delete affordance (right-edge ✕).  Packed BEFORE the
        # status label so the status fills the rest of the row width.
        rm_lbl = tk.Label(row, text="✕", font=FBT, bg=SURF, fg=SUB,
                           cursor="hand2", padx=12, pady=8)
        rm_lbl.pack(side="right")

        status_lbl = tk.Label(row, text=status_text, font=FB,
                               bg=SURF, fg=status_fg, anchor="w",
                               padx=12, pady=8)
        status_lbl.pack(side="left", fill="x", expand=True)

        if not hasattr(self, "_pq_row_widgets"):
            self._pq_row_widgets = {}
        self._pq_row_widgets[id(session)] = {
            "row":         row,
            "tok_lbl":     tok_lbl,
            "files_lbl":   files_lbl,
            "words_lbl":   words_lbl,
            "status_lbl":  status_lbl,
            "rm_lbl":      rm_lbl,
            "session":     session,
        }

        def _open_handler(sess=session):
            return lambda e: self._pq_open_session_view(sess)
        handler = _open_handler()
        # Click anywhere in the row except the ✕ → open the session.
        row.bind("<Button-1>", handler)
        for child in row.winfo_children():
            if child is rm_lbl:
                continue
            child.bind("<Button-1>", handler)

        def _on_remove(_e=None, sess=session):
            self._pq_remove_session(sess)
        rm_lbl.bind("<Button-1>", _on_remove)
        rm_lbl.bind("<Enter>", lambda e: rm_lbl.config(fg=ERR))
        rm_lbl.bind("<Leave>", lambda e: rm_lbl.config(fg=SUB))

        def _hover_in(e, r=row, rm=rm_lbl):
            r.config(bg=SURF2)
            for c in r.winfo_children():
                if c is rm:
                    rm.config(bg=SURF2)
                else:
                    c.config(bg=SURF2)
        def _hover_out(e, r=row, rm=rm_lbl):
            r.config(bg=SURF)
            for c in r.winfo_children():
                if c is rm:
                    rm.config(bg=SURF)
                else:
                    c.config(bg=SURF)
        row.bind("<Enter>", _hover_in)
        row.bind("<Leave>", _hover_out)

    # ── Add Session dialog (chooser between New / Existing) ─────────────────

    def _pq_add_session_dialog(self):
        win = tk.Toplevel(self)
        win.title("Add Interview Session")
        win.configure(bg=BG)
        win.transient(self); win.grab_set()
        win.resizable(False, False)

        tk.Label(win, text="Add Interview Session", font=FH,
                 bg=BG, fg=TEXT, pady=14, padx=24).pack()

        body = tk.Frame(win, bg=BG)
        body.pack(padx=24, pady=(0, 16))

        def _new():
            win.destroy()
            self._pq_new_session_dialog()

        def _existing():
            win.destroy()
            path = filedialog.askopenfilename(
                title="Pick Interview Session JSON",
                filetypes=[("Pull Quotes Session", "*.pb_session.json"),
                           ("JSON", "*.json"),
                           ("All", "*.*")])
            if not path:
                return
            loaded = self._pq_load_session_file(path)
            if loaded is None:
                messagebox.showerror(
                    "Load failed",
                    "Not a valid Interview Session JSON:\n\n" + path)
                return
            if any(s.get("_file_path") == path
                   for s in self._pq_project["sessions"]):
                messagebox.showinfo("Already added",
                    "This session is already part of the project.")
                return
            self._pq_project["sessions"].append(loaded)
            self._pq_render_project_view()

        self._btn(body, "NEW INTERVIEW SESSION", _new,
                  color=ACCENT).pack(side="left", padx=(0, 12))
        self._btn(body, "ADD EXISTING SESSION", _existing).pack(side="left")

        cancel_row = tk.Frame(win, bg=BG)
        cancel_row.pack(pady=(0, 16))
        self._btn(cancel_row, "CANCEL", win.destroy, small=True).pack()

        win.update_idletasks()
        # Centre on parent
        pw = self.winfo_width(); ph = self.winfo_height()
        px = self.winfo_rootx(); py = self.winfo_rooty()
        ww = win.winfo_width();  wh = win.winfo_height()
        win.geometry("+{}+{}".format(px + (pw - ww) // 2, py + (ph - wh) // 2))
        self.wait_window(win)

    # ── New Session dialog ──────────────────────────────────────────────────

    def _pq_new_session_dialog(self):
        import re
        win = tk.Toplevel(self)
        win.title("New Interview Session")
        win.configure(bg=BG)
        win.transient(self); win.grab_set()
        win.minsize(900, 700)

        # Center on parent BEFORE wait_window blocks
        def _center():
            win.update_idletasks()
            pw = self.winfo_width();  ph = self.winfo_height()
            px = self.winfo_rootx(); py = self.winfo_rooty()
            ww = max(900, win.winfo_reqwidth())
            wh = max(700, win.winfo_reqheight())
            win.geometry("{}x{}+{}+{}".format(
                ww, wh,
                px + max(0, (pw - ww) // 2),
                py + max(0, (ph - wh) // 2)))
        win.after(10, _center)

        tk.Label(win, text="New Interview Session", font=FH,
                 bg=BG, fg=TEXT, padx=24, pady=14).pack(anchor="w")

        # Token entry
        tok_row = tk.Frame(win, bg=BG)
        tok_row.pack(fill="x", padx=24, pady=(0, 10))
        tk.Label(tok_row, text="SESSION TOKEN", font=FL,
                 bg=BG, fg=SUB, width=18, anchor="w").pack(side="left")
        tok_var = tk.StringVar(value="")
        tok_entry = tk.Entry(tok_row, textvariable=tok_var, font=FB,
                              bg=SURF2, fg=TEXT, insertbackground=TEXT,
                              relief="flat", bd=6, width=24)
        tok_entry.pack(side="left")
        tk.Label(tok_row, text="(uppercase letters / digits / underscores)",
                 font=FB, bg=BG, fg=SUB).pack(side="left", padx=(10, 0))

        # Media drop zone
        drop = tk.Frame(win, bg=SURF2,
                        highlightbackground=BORDER, highlightthickness=1)
        drop.pack(fill="both", expand=True, padx=24, pady=(0, 8))

        hdr = tk.Frame(drop, bg=SURF3)
        hdr.pack(fill="x")
        tk.Label(hdr, text="MEDIA FILES", font=FL, bg=SURF3, fg=SUB,
                 anchor="w", padx=12, pady=6).pack(side="left")
        count_lbl = tk.Label(hdr, text="0 files", font=FB,
                              bg=SURF3, fg=SUB, padx=12, pady=6)
        count_lbl.pack(side="right")

        list_canvas = tk.Canvas(drop, bg=SURF2, highlightthickness=0, height=180)
        list_scroll = _SlimScrollbar(drop, command=list_canvas.yview)
        list_scroll.pack(side="right", fill="y")
        list_canvas.pack(fill="both", expand=True)
        list_canvas.configure(yscrollcommand=list_scroll.set)
        list_inner = tk.Frame(list_canvas, bg=SURF2)
        list_canvas.create_window((0, 0), window=list_inner, anchor="nw")
        list_inner.bind("<Configure>",
                        lambda e: list_canvas.configure(
                            scrollregion=list_canvas.bbox("all")))

        media_paths = []
        _user_typed_token = [False]

        def _refresh_list():
            for w_ in list(list_inner.winfo_children()):
                w_.destroy()
            count_lbl.config(text="{} file{}".format(
                len(media_paths), "s" if len(media_paths) != 1 else ""))
            if not media_paths:
                tk.Label(list_inner,
                         text="  Drop files here or click + BROWSE below.",
                         font=FB, bg=SURF2, fg=SUB,
                         padx=8, pady=12).pack(anchor="w")
            else:
                for p in media_paths:
                    rrow = tk.Frame(list_inner, bg=SURF2)
                    rrow.pack(fill="x", padx=8, pady=2)
                    kind = "VIDEO" if is_video(p) else "AUDIO"
                    kc   = (("#1e3a1e","#5aab61") if kind == "VIDEO"
                            else ("#1e2a3a","#5588cc"))
                    tk.Label(rrow, text=kind, font=("Courier New", 9, "bold"),
                             bg=kc[0], fg=kc[1], padx=5, pady=2
                             ).pack(side="left")
                    tk.Label(rrow, text="  " + os.path.basename(p),
                             font=FB, bg=SURF2, fg=TEXT,
                             anchor="w").pack(side="left",
                                              fill="x", expand=True)
                    rm = tk.Label(rrow, text="✕", font=FB,
                                  bg=SURF2, fg=SUB, cursor="hand2", padx=8)
                    rm.pack(side="right")
                    def _remove(_e, _p=p):
                        if _p in media_paths:
                            media_paths.remove(_p)
                        _refresh_list()
                        if not _user_typed_token[0]:
                            tok_var.set(self._pq_suggest_token(media_paths))
                    rm.bind("<Button-1>", _remove)
            if not _user_typed_token[0]:
                tok_var.set(self._pq_suggest_token(media_paths))

        def _add_paths(paths):
            for p in paths:
                p = os.path.abspath(str(p).strip().strip("{}"))
                if not p or not is_media(p) or p in media_paths:
                    continue
                media_paths.append(p)
            _refresh_list()

        def _on_token_typed(*_):
            _user_typed_token[0] = bool(tok_var.get().strip())
        tok_var.trace_add("write", _on_token_typed)

        def _browse():
            files = filedialog.askopenfilenames(
                title="Pick interview media",
                filetypes=[("Media",
                            "*.wav *.aif *.aiff *.bwf *.mp3 *.m4a *.flac "
                            "*.mp4 *.mov *.mxf *.mkv *.avi *.m4v"),
                           ("All", "*.*")])
            if files:
                _add_paths(files)

        if HAS_DND:
            for w_ in (drop, list_canvas, list_inner):
                w_.drop_target_register(DND_FILES)
                w_.dnd_bind("<<Drop>>",
                            lambda e: _add_paths(parse_dnd(e.data)))

        ctl = tk.Frame(win, bg=BG)
        ctl.pack(fill="x", padx=24, pady=(0, 16))
        self._btn(ctl, "+ BROWSE", _browse, small=True).pack(side="left")

        nav = tk.Frame(win, bg=BG)
        nav.pack(fill="x", padx=24, pady=(0, 16))

        def _go():
            token = tok_var.get().strip().upper()
            token = re.sub(r"[^A-Z0-9_]+", "_", token).strip("_")
            if not token:
                messagebox.showwarning("Token required",
                    "Enter a session token name.", parent=win)
                return
            if not media_paths:
                messagebox.showwarning("No media",
                    "Add at least one media file.", parent=win)
                return
            for s in self._pq_project["sessions"]:
                if s.get("token") == token:
                    messagebox.showwarning("Token already in use",
                        "This Episode Project already has a session with "
                        "token '{}'.  Pick a different token.".format(token),
                        parent=win)
                    return

            session_path = self._pq_default_session_path(token, media_paths)
            if not session_path:
                messagebox.showerror("Save path",
                    "Could not derive a session save path.", parent=win)
                return
            session = {
                "version":    1,
                "workflow":   "interview_session",
                "token":      token,
                "media":      list(media_paths),
                "transcript": [],
                "_file_path": session_path,
            }
            self._pq_project["sessions"].append(session)
            self._pq_save_session_file(session)   # write empty-transcript shell
            win.destroy()
            self._pq_open_session_view(session, transcribe_now=True)

        _W = 14
        self._btn(nav, "TRANSCRIBE  →", _go,
                  color=ACCENT, width=_W).pack(side="right")
        self._btn(nav, "CANCEL", win.destroy, width=_W
                  ).pack(side="right", padx=(0, 8))

        _refresh_list()
        tok_entry.focus_set()
        self.wait_window(win)

    def _pq_suggest_token(self, paths):
        """Suggest a token name from filename common content."""
        import re as _re
        if not paths:
            return ""
        names = [os.path.splitext(os.path.basename(p))[0] for p in paths]
        SKIP = {"riverside", "raw", "audio", "video", "session", "interview",
                "speaker", "take", "cfr", "v1", "v2", "v3"}
        def _toks(n):
            n = _re.sub(r"[_\-.]+", " ", n.lower())
            n = _re.sub(r"[^a-z0-9 ]+", " ", n)
            out = []
            for w_ in n.split():
                if w_ in SKIP:               continue
                if w_.startswith("speaker"): continue
                if _re.fullmatch(r"\d+", w_):  continue
                out.append(w_)
            return out
        tok_lists = [_toks(n) for n in names]
        if not any(tok_lists):
            return _re.sub(r"[^A-Z0-9_]+", "_",
                          names[0].upper()).strip("_")
        # Words common to ALL files (in single-file case, that's all of them)
        common = set(tok_lists[0])
        for t in tok_lists[1:]:
            common &= set(t)
        if not common:
            for t in tok_lists:
                if t:
                    return t[0].upper()
            return ""
        # Preserve order from first filename
        ordered = [w_ for w_ in tok_lists[0] if w_ in common]
        return "_".join(ordered).upper()

    # ── Session view ────────────────────────────────────────────────────────

    def _pq_open_session_view(self, session, transcribe_now=False):
        self._clear()
        self._pq_current_session = session
        # Restore any state stashed by an orientation-driven rebuild.
        _stash = getattr(self, "_pq_layout_resize_stash", None)
        self._pq_layout_resize_stash = None

        # ── Bottom bar — consolidates BACK / hints / 📝 notes / PLAY / COPY
        # onto one persistent row, so even short windows never lose the
        # action buttons.  Left side: navigation + hint text.  Right side:
        # primary actions.  No separate action row.
        nav = tk.Frame(self.body, bg=BG)
        nav.pack(side="bottom", fill="x", pady=(8, 0))
        self._btn(nav, "← BACK TO PROJECT",
                  self._pq_render_project_view).pack(side="left")
        # Hint text — small enough to share the row, drops first if cramped.
        tk.Label(nav,
                 text="  Right-click for actions  ·  Space plays  ·  Ctrl+M note",
                 font=FB, bg=BG, fg=SUB).pack(side="left", padx=(12, 0))

        # ── Section title (clickable to collapse the info card) ──────────
        # The section row carries a chevron showing the collapse state of
        # the info card below it.  Clicking either the title or chevron
        # toggles the info card's visibility — gives the transcript more
        # vertical room on short windows without losing access to the
        # MEDIA list / TRANSCRIBE controls.  Match _section's visual
        # treatment (3 px ACCENT stripe on the left).
        section_row = tk.Frame(self.body, bg=BG)
        section_row.pack(fill="x", pady=(20, 6))
        tk.Frame(section_row, bg=ACCENT, width=3, height=20).pack(
            side="left", padx=(0, 10))
        # Persist collapse state across re-renders of the same session.
        # On the very first open, auto-collapse the info card when the
        # window is too short to comfortably show it + transcript.  With
        # the unified bottom bar this only fires on really small windows
        # — the user can always toggle it back manually.
        if not hasattr(self, "_pq_info_collapsed"):
            self.update_idletasks()
            cur_h = self.winfo_height() or 800
            self._pq_info_collapsed = cur_h < 620
        self._pq_info_chevron = tk.Label(
            section_row,
            text="▼" if not self._pq_info_collapsed else "▶",
            font=FB, bg=BG, fg=SUB, cursor="hand2", padx=4)
        self._pq_info_chevron.pack(side="left")
        section_lbl = tk.Label(
            section_row,
            text="INTERVIEW SESSION  —  {}".format(session.get("token", "?")),
            font=FL, bg=BG, fg=TEXT, cursor="hand2")
        section_lbl.pack(side="left", padx=(2, 0))

        # Header info card — can be collapsed by clicking the chevron
        info = tk.Frame(self.body, bg=SURF,
                        highlightbackground=BORDER, highlightthickness=1)
        self._pq_info_card = info
        if not self._pq_info_collapsed:
            info.pack(fill="x", pady=(0, 12))
        inner = tk.Frame(info, bg=SURF)
        inner.pack(fill="x", padx=16, pady=12)

        def _toggle_info(_e=None):
            self._pq_info_collapsed = not self._pq_info_collapsed
            if self._pq_info_collapsed:
                info.pack_forget()
                self._pq_info_chevron.config(text="▶")
            else:
                # Re-pack BEFORE the PanedWindow so it appears above it.
                info.pack(fill="x", pady=(0, 12),
                          before=self._pq_layout_pw)
                self._pq_info_chevron.config(text="▼")
        self._pq_info_chevron.bind("<Button-1>", _toggle_info)
        section_lbl.bind("<Button-1>", _toggle_info)

        tk.Label(inner, text="TOKEN", font=FL,
                 bg=SURF, fg=SUB, width=10,
                 anchor="w").grid(row=0, column=0, sticky="w")
        tk.Label(inner, text=session.get("token", ""),
                 font=FBT, bg=SURF, fg=ACCENT,
                 anchor="w").grid(row=0, column=1, sticky="w")

        tk.Label(inner, text="MEDIA", font=FL,
                 bg=SURF, fg=SUB, width=10,
                 anchor="nw").grid(row=1, column=0, sticky="nw", pady=(8, 0))
        media_box = tk.Frame(inner, bg=SURF)
        media_box.grid(row=1, column=1, sticky="w", pady=(8, 0))
        self._pq_media_box = media_box
        self._pq_render_media_list(session)

        tk.Label(inner, text="FILE", font=FL,
                 bg=SURF, fg=SUB, width=10,
                 anchor="nw").grid(row=2, column=0, sticky="nw", pady=(8, 0))
        tk.Label(inner, text=session.get("_file_path", "(unsaved)"),
                 font=FB, bg=SURF, fg=SUB,
                 anchor="w").grid(row=2, column=1, sticky="w", pady=(8, 0))

        btn_row = tk.Frame(inner, bg=SURF)
        btn_row.grid(row=3, column=1, sticky="w", pady=(10, 0))
        self._btn(btn_row, "+ ADD MEDIA",
                  lambda s=session: self._pq_add_media_to_session(s),
                  small=True).pack(side="left")
        self._pq_retx_btn = self._btn(
            btn_row,
            ("⟳ RE-TRANSCRIBE" if session.get("transcript") else "⟳ TRANSCRIBE"),
            lambda s=session: self._pq_run_transcription(s),
            small=True, color=ACCENT)
        self._pq_retx_btn.pack(side="left", padx=(8, 0))

        # Inline model picker (dropdown + ? help).  Lives right next to
        # TRANSCRIBE so the user can change the model before clicking.
        self._build_model_picker(btn_row, bg=SURF).pack(side="left",
                                                        padx=(16, 0))

        # Background-mode checkbox — same intent as the script→session flow:
        # opting in makes the whisper worker yield so the rest of the system
        # stays responsive at the cost of slower decoding.
        bg_frame = tk.Frame(btn_row, bg=SURF)
        bg_frame.pack(side="left", padx=(16, 0))
        if not hasattr(self, "_pq_bg_mode_var"):
            self._pq_bg_mode_var = tk.BooleanVar(value=False)
        bg_ck = tk.Label(bg_frame,
                         text="☑" if self._pq_bg_mode_var.get() else "☐",
                         font=(_SANS, 14),
                         bg=SURF, fg=ACCENT if self._pq_bg_mode_var.get() else SUB,
                         cursor="hand2", padx=4)
        bg_ck.pack(side="left")
        bg_lbl = tk.Label(bg_frame, text="Background mode", font=FB,
                          bg=SURF, fg=SUB, cursor="hand2")
        bg_lbl.pack(side="left")
        def _toggle_bg():
            new_val = not self._pq_bg_mode_var.get()
            self._pq_bg_mode_var.set(new_val)
            bg_ck.config(text="☑" if new_val else "☐",
                         fg=ACCENT if new_val else SUB)
        bg_ck.bind("<Button-1>",  lambda e: _toggle_bg())
        bg_lbl.bind("<Button-1>", lambda e: _toggle_bg())

        # ── Resizable layout: transcript + notes ─────────────────────────
        # PanedWindow holds the transcript pane and the margin-notes
        # panel.  Orientation flips with the window width:
        #   ≥ PQ_LAYOUT_THRESHOLD px → horizontal (side-by-side)
        #   below                    → vertical (stacked)
        # The user can drag the sash freely between them.
        PQ_LAYOUT_THRESHOLD = 1100
        self.update_idletasks()
        cur_w = self.winfo_width() or 1200
        orient = "horizontal" if cur_w >= PQ_LAYOUT_THRESHOLD else "vertical"
        self._pq_layout_orient = orient
        self._pq_layout_pw = tk.PanedWindow(
            self.body, orient=orient, bg=BG,
            sashwidth=8, sashrelief="flat",
            sashpad=0, bd=0, opaqueresize=False,
            handlepad=0, showhandle=False)
        self._pq_layout_pw.pack(fill="both", expand=True, pady=(0, 8))

        # Drag the sash → glow.  Bind on the PanedWindow so we don't
        # have to know the sash widget id; Tk routes the events.
        self._pq_layout_pw.config(sashrelief="flat")

        # ── Transcript pane (header + search + text) ─────────────────────
        tx_outer = tk.Frame(self._pq_layout_pw, bg=SURF,
                            highlightbackground=BORDER, highlightthickness=1)
        # tx_outer is added to the PanedWindow at the bottom of this
        # function once the notes panel has been built too.

        # Header row: TRANSCRIPT label + search toggle + status
        tx_hdr = tk.Frame(tx_outer, bg=SURF3)
        tx_hdr.pack(fill="x")
        tk.Label(tx_hdr, text="TRANSCRIPT", font=FL,
                 bg=SURF3, fg=SUB, anchor="w",
                 padx=12, pady=6).pack(side="left")
        # Search toggle — the search row consumes a 40-50 px strip;
        # collapse-by-default lets the user reclaim that for the
        # transcript and pop it open with Ctrl+F or this icon.
        if not hasattr(self, "_pq_search_visible"):
            # Collapsed by default — the 🔍 button toggles it.  Saves
            # ~40 px of vertical space for the transcript itself
            # without making search hard to find.  Preference sticks
            # across re-renders via the hasattr check above.
            self._pq_search_visible = False
        self._pq_search_toggle = tk.Label(
            tx_hdr, text="🔍", font=FB,
            bg=SURF3, fg=ACCENT if self._pq_search_visible else SUB,
            cursor="hand2", padx=10, pady=6)
        self._pq_search_toggle.pack(side="right")
        self._pq_status_lbl = tk.Label(tx_hdr, text="", font=FB,
                                        bg=SURF3, fg=SUB,
                                        padx=12, pady=6)
        self._pq_status_lbl.pack(side="right")

        # ── Cursor timecode readout ──────────────────────────────────
        # Shows the source TC of the word under the transcript cursor
        # so the user can quickly adjust a pull's timecode in the
        # script without having to copy-paste to extract the time.
        # Click the readout to copy the TC to the clipboard.
        self._pq_cursor_tc_var = tk.StringVar(value="—")
        self._pq_cursor_tc_lbl = tk.Label(
            tx_hdr, textvariable=self._pq_cursor_tc_var, font=FBT,
            bg=SURF3, fg=ACCENT, cursor="hand2", padx=12, pady=6)
        self._pq_cursor_tc_lbl.pack(side="right")
        tk.Label(tx_hdr, text="AT", font=FB, bg=SURF3, fg=SUB,
                 padx=(0, 0), pady=6).pack(side="right")

        def _copy_cursor_tc(_e=None):
            v = self._pq_cursor_tc_var.get()
            if not v or v == "—":
                return
            try:
                self.clipboard_clear()
                self.clipboard_append(v)
                # Brief visual acknowledgement
                _orig = self._pq_cursor_tc_lbl.cget("fg")
                self._pq_cursor_tc_lbl.config(fg=SUCCESS)
                self.after(500, lambda: self._pq_cursor_tc_lbl.config(fg=_orig))
            except tk.TclError:
                pass
        self._pq_cursor_tc_lbl.bind("<Button-1>", _copy_cursor_tc)

        # Search row — Entry + match counter + ◀ ▶ navigation
        sr = tk.Frame(tx_outer, bg=SURF, padx=12, pady=6)
        if self._pq_search_visible:
            sr.pack(fill="x")
        self._pq_search_row = sr
        tk.Label(sr, text="🔍", font=FB, bg=SURF, fg=SUB
                 ).pack(side="left", padx=(0, 6))
        self._pq_search_var = tk.StringVar()
        sr_entry = tk.Entry(sr, textvariable=self._pq_search_var,
                             font=FB, bg=SURF2, fg=TEXT,
                             insertbackground=TEXT, relief="flat",
                             bd=4, width=32)
        sr_entry.pack(side="left", ipady=2)
        self._pq_search_entry = sr_entry
        self._pq_search_count_lbl = tk.Label(
            sr, text="", font=FB, bg=SURF, fg=SUB)
        self._pq_search_count_lbl.pack(side="left", padx=(8, 0))

        self._btn(sr, "▶", lambda: self._pq_search_step(+1),
                  small=True).pack(side="right", padx=(2, 0))
        self._btn(sr, "◀", lambda: self._pq_search_step(-1),
                  small=True).pack(side="right")
        self._pq_search_var.trace_add(
            "write", lambda *_: self._pq_search_apply())
        sr_entry.bind("<Return>", lambda e: self._pq_search_step(+1))
        sr_entry.bind("<Shift-Return>",
                      lambda e: self._pq_search_step(-1))
        sr_entry.bind("<Escape>", lambda e: self._pq_search_clear())

        def _toggle_search(_e=None):
            self._pq_search_visible = not self._pq_search_visible
            if self._pq_search_visible:
                self._pq_search_row.pack(fill="x", before=tx_frame)
                self._pq_search_toggle.config(fg=ACCENT)
                try:
                    self._pq_search_entry.focus_set()
                except tk.TclError:
                    pass
            else:
                self._pq_search_row.pack_forget()
                self._pq_search_toggle.config(fg=SUB)
                # Clear any active search highlights when hidden.
                try:
                    self._pq_search_clear()
                except Exception:
                    pass
            return "break"
        self._pq_search_toggle.bind("<Button-1>", _toggle_search)
        # Ctrl+F focuses the search box, opening it if collapsed.
        def _focus_search(_e=None):
            if not self._pq_search_visible:
                _toggle_search()
            else:
                try:
                    self._pq_search_entry.focus_set()
                    self._pq_search_entry.selection_range(0, "end")
                except tk.TclError:
                    pass
            return "break"
        # Bind at top-level so it works from anywhere in the session view.
        self.bind("<Control-f>", _focus_search)
        self.bind("<Control-F>", _focus_search)

        # Text widget — explicit padx/pady gives a uniform inset all the
        # way down (the previous bd=8 + scrollbar packing produced the
        # progressively-narrower-margin glitch).
        tx_frame = tk.Frame(tx_outer, bg=SURF2)
        tx_frame.pack(fill="both", expand=True)
        tx_text = tk.Text(tx_frame, bg=SURF2, fg=TEXT,
                           insertbackground=TEXT, wrap="word",
                           relief="flat", bd=0, font=FB,
                           padx=14, pady=12,
                           spacing1=0, spacing2=0, spacing3=0,
                           tabs="",
                           selectbackground=ACCENT,
                           selectforeground=TEXT,
                           # Native undo as a safety net in case any
                           # programmatic insert path slips through.
                           undo=True, autoseparators=True, maxundo=-1)
        # lmargin1/lmargin2/rmargin are tag-only options.  Pin all three
        # to 0 — applied to every chunk of rendered text so wrapped lines
        # stay flush left and right.  lmargincolor is also explicitly
        # transparent (matches widget bg) to rule out any margin-band
        # rendering quirk.
        tx_text.tag_configure("body",
                               lmargin1=0, lmargin2=0, rmargin=0,
                               lmargincolor=SURF2, rmargincolor=SURF2)
        # Source-based fading: words from the tiny draft pass render
        # in muted SUB grey so the user can see at a glance which
        # parts of the transcript are still "draft" and which have
        # been confirmed by the configured (main) pass or replaced
        # by a user edit.  Tag must be raised above "body" so its
        # foreground wins for tagged char ranges.
        tx_text.tag_configure("body_tiny", foreground=SUB)
        tx_text.tag_raise("body_tiny", "body")
        # User-edited words get a thin underline — the conventional
        # "tracked changes" indicator.  Subtle enough not to clash
        # with reading, clear enough that the user always knows
        # which parts they touched (and can right-click to restore
        # the original Whisper text).
        tx_text.tag_configure("body_user", underline=True)
        tx_text.tag_raise("body_user", "body")
        # Playback "karaoke" highlight — the current word under the
        # playhead gets a warm orange-tinted background so the user
        # can follow along visually while audio plays.  Driven by
        # the periodic _pq_playback_highlight_tick poll while
        # _pq_playing.  The colour is ACCENT mixed ~25% into BG so
        # it reads as the same orange family without screaming.
        tx_text.tag_configure("body_playing", background="#4a2a14")
        tx_text.tag_raise("body_playing", "body")
        # Diagnostic on Ctrl+Shift+D — fast version: samples up to 30
        # logical lines spread evenly through the document and reports
        # each one's first display-line dimensions.  Capped iterations
        # so a giant transcript can't freeze the UI.
        def _diag(event=None, t=tx_text):
            try:
                t.update_idletasks()
                ww  = t.winfo_width()
                wh  = t.winfo_height()
                pad_l = int(t.cget("padx"))
                content_w = ww - pad_l * 2
                end_idx = t.index("end-1c")
                last_line = int(end_idx.split('.')[0])
                # Sample up to 30 lines evenly distributed
                if last_line <= 30:
                    sample = list(range(1, last_line + 1))
                else:
                    step = max(1, last_line // 30)
                    sample = list(range(1, last_line + 1, step))[:30]
                print("=" * 60)
                print("PQ-DIAG widget={}x{}  padx={}  content_w={}  "
                      "total_lines={}".format(ww, wh, pad_l,
                                               content_w, last_line))
                widths = []
                for ln in sample:
                    dli = t.dlineinfo("{}.0".format(ln))
                    text = t.get("{}.0".format(ln),
                                  "{}.end".format(ln))
                    text_n = len(text)
                    text_preview = (text[:50] + "…") if text_n > 50 else text
                    if dli is None:
                        print("  line {:>4}: (offscreen)  chars={}  {!r}".format(
                            ln, text_n, text_preview))
                        continue
                    x, y, w, h, _ = dli
                    widths.append(w)
                    print("  line {:>4}: x={:3d} y={:5d} w={:4d}  "
                          "chars={:3d}  {!r}".format(
                              ln, x, y, w, text_n, text_preview))
                if widths:
                    print("  rendered widths — min={} max={} avg={}".format(
                        min(widths), max(widths),
                        round(sum(widths)/len(widths), 1)))
            except Exception as e:
                import traceback
                traceback.print_exc()
            return "break"
        tx_text.bind("<Control-D>", _diag)
        tx_text.bind("<Control-d>", _diag)
        tx_sb = _SlimScrollbar(tx_frame, command=tx_text.yview)
        tx_sb.pack(side="right", fill="y")
        tx_text.configure(yscrollcommand=tx_sb.set)
        tx_text.pack(side="left", fill="both", expand=True)
        self._pq_tx_text = tx_text

        # ── Cursor timecode: keep _pq_cursor_tc_var in sync with the
        # text-widget insertion point.  Fires on click and any key that
        # moves the insertion cursor (arrows, Home/End, etc.).  Uses
        # binary search across _pq_word_index (ordered by abs char
        # position) so lookup stays O(log N) on long transcripts.
        import bisect as _bisect
        def _update_cursor_tc(_e=None, t=tx_text):
            idx = getattr(self, "_pq_word_index", None)
            var = getattr(self, "_pq_cursor_tc_var", None)
            if var is None:
                return
            if not idx:
                var.set("—"); return
            try:
                pos = t.count("1.0", "insert")
                pos_i = int(pos[0]) if pos else 0
            except (tk.TclError, TypeError, ValueError):
                var.set("—"); return
            # bisect on the start-char field; then verify the cursor sits
            # inside that word's range OR pick the nearest neighbour.
            starts = [s for (s, _e, _w) in idx]
            i = _bisect.bisect_right(starts, pos_i) - 1
            if i < 0:
                i = 0
            elif i < len(idx) - 1 and idx[i][1] < pos_i:
                # Cursor is past this word's end; if the NEXT word starts
                # close, snap to it (feels natural when clicking whitespace).
                if idx[i + 1][0] - pos_i < pos_i - idx[i][1]:
                    i += 1
            w = idx[i][2]
            t_s = float(w.get("start", 0.0) or 0.0)
            var.set(secs_tc(t_s).split(".")[0])

        tx_text.bind("<ButtonRelease-1>", _update_cursor_tc, add="+")
        tx_text.bind("<KeyRelease>",       _update_cursor_tc, add="+")

        # Search match highlight tag
        tx_text.tag_configure("pq_match", background="#5a3a10",
                              foreground=TEXT)
        tx_text.tag_configure("pq_match_current", background=ACCENT,
                              foreground=TEXT)

        # Reset search state on view rebuild
        self._pq_search_hits = []      # list of (start_idx, end_idx)
        self._pq_search_cur  = -1

        self._pq_render_transcript_text(session)

        # Bind copy → @PULL
        for keyspec in ("<Control-c>", "<Control-C>",
                        "<Command-c>", "<Command-C>"):
            tx_text.bind(keyspec, self._pq_copy_as_pull)

        # Undo / redo — Ctrl+Z is bound by Tk's Text class when undo=True,
        # but we add explicit redo on Ctrl+Y AND Ctrl+Shift+Z so users
        # get whichever convention they expect.
        def _do_undo(event=None, t=tx_text):
            try: t.edit_undo()
            except tk.TclError: pass   # nothing to undo
            return "break"
        def _do_redo(event=None, t=tx_text):
            try: t.edit_redo()
            except tk.TclError: pass   # nothing to redo
            return "break"
        # Ctrl+Z is already wired by Tk class binding; bind it explicitly
        # too so it always reaches our edit_undo even if some future
        # bind_all swallows it.
        tx_text.bind("<Control-z>",       _do_undo)
        tx_text.bind("<Control-Z>",       _do_undo)
        tx_text.bind("<Control-y>",       _do_redo)
        tx_text.bind("<Control-Y>",       _do_redo)
        tx_text.bind("<Control-Shift-z>", _do_redo)
        tx_text.bind("<Control-Shift-Z>", _do_redo)

        # ── Always-on read-only with right-click context menu ─────────
        # The transcript is permanently read-only.  Editing happens via
        # right-click → "Edit text…" on a selection.  Spacebar always
        # plays the current selection (or cursor-to-end).  Ctrl+M still
        # adds a margin note for the current selection.
        def _add_note(event=None):
            self._pq_add_margin_note()
            return "break"
        tx_text.bind("<Control-m>", _add_note)
        tx_text.bind("<Control-M>", _add_note)
        self.bind("<Control-m>", _add_note)
        self.bind("<Control-M>", _add_note)

        # Spacebar plays the current selection.  Shift+Space stays as a
        # convenience alias.
        def _space_play(event=None):
            self._pq_play_selection()
            return "break"
        tx_text.bind("<space>",               _space_play)
        tx_text.bind("<Key-space>",           _space_play)
        tx_text.bind("<Shift-space>",         _space_play)
        tx_text.bind("<Shift-Key-space>",     _space_play)
        sr_entry.bind("<Shift-space>",        _space_play)
        sr_entry.bind("<Shift-Key-space>",    _space_play)

        # Read-only enforcement — block any keypress that would modify
        # the buffer.  Allow navigation, modifier keys, and Ctrl+key
        # shortcuts (which we wire explicitly elsewhere).
        ALLOWED_KEYSYMS = {
            "Up", "Down", "Left", "Right", "Home", "End", "Prior", "Next",
            "Tab", "Shift_L", "Shift_R", "Control_L", "Control_R",
            "Alt_L", "Alt_R", "Meta_L", "Meta_R", "Caps_Lock",
            "Num_Lock", "Scroll_Lock",
        }
        # Ctrl keys the Text widget binds to BUILT-IN editing (emacs
        # style) that would mutate the buffer: Ctrl+D delete-char,
        # Ctrl+H backspace, Ctrl+K kill-line, Ctrl+O open-line,
        # Ctrl+T transpose.  Everything else with Ctrl is allowed so the
        # wired shortcuts (Ctrl+C copy, Ctrl+A select-all, Ctrl+S save,
        # Ctrl+M margin note, …) keep working.
        CTRL_EDIT_KEYSYMS = {"d", "h", "k", "o", "t"}
        def _maybe_block_edit(event):
            if event.keysym in ALLOWED_KEYSYMS:
                return None
            ctrl_pressed = (event.state & 0x4) != 0
            if ctrl_pressed:
                if event.keysym.lower() in CTRL_EDIT_KEYSYMS:
                    return "break"
                return None
            return "break"
        tx_text.bind("<KeyPress>", _maybe_block_edit)

        # Block clipboard mutations of the read-only transcript.  Typing
        # and Delete/BackSpace are already stopped above, but the Text
        # widget's class-level virtual events still fire: an accidental
        # Ctrl+V (paste) could drop clipboard text over the selection
        # (reported — easy to hit instead of Ctrl+C), and Ctrl+X (cut) /
        # middle-click paste are the same hazard.  Copy and Select-All
        # stay enabled.  Returning "break" from the instance binding
        # preempts the class binding that performs the edit.
        # <<PasteSelection>> covers X11 middle-click paste; the rest cover
        # Ctrl+V / Ctrl+X / Clear on every platform.
        for _mut_evt in ("<<Paste>>", "<<PasteSelection>>",
                         "<<Cut>>", "<<Clear>>"):
            tx_text.bind(_mut_evt, lambda e: "break")

        # Right-click context menu — the only way to mutate the
        # transcript.  Always offers the actions that make sense on
        # the current selection (or graceful no-op when there isn't
        # one).
        def _show_ctx(event):
            try:
                has_sel = bool(tx_text.tag_ranges("sel"))
            except tk.TclError:
                has_sel = False
            # Does the selection (or cursor word, when no selection)
            # touch any user-edited entries with restore data?  Drives
            # whether the "Restore Whisper original" item shows.
            has_restorable = self._pq_selection_has_user_edits()
            menu = tk.Menu(tx_text, tearoff=0,
                            bg=SURF2, fg=TEXT,
                            activebackground=ACCENT,
                            activeforeground=TEXT,
                            bd=0)
            menu.add_command(
                label="Copy as @PULL",
                command=self._pq_copy_as_pull,
                state="normal" if has_sel else "disabled")
            menu.add_command(
                label="Play selection (Space)",
                command=self._pq_play_selection,
                state="normal" if has_sel else "disabled")
            menu.add_command(
                label="Add margin note (Ctrl+M)",
                command=self._pq_add_margin_note,
                state="normal" if has_sel else "disabled")
            menu.add_separator()
            menu.add_command(
                label="Edit selected text…",
                command=self._pq_edit_selection,
                state="normal" if has_sel else "disabled")
            if has_restorable:
                menu.add_command(
                    label="Restore Whisper original",
                    command=self._pq_restore_originals)
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
        tx_text.bind("<Button-3>", _show_ctx)

        # Ctrl+R / F5 → re-render the transcript from the same in-memory
        # data.  Useful when iterating on rendering tweaks: change the
        # constants in _pq_render_transcript_text, restart PostBridge once,
        # open the session, then this binding lets us re-run the renderer
        # without leaving the view.
        def _rerender(event=None, sess=session):
            self._pq_render_transcript_text(sess)
            return "break"
        tx_text.bind("<Control-r>", _rerender)
        tx_text.bind("<Control-R>", _rerender)
        tx_text.bind("<F5>",        _rerender)
        self.bind("<F5>", _rerender)

        # ── Margin notes panel ───────────────────────────────────────
        # Lives inside the PanedWindow alongside the transcript.  When
        # notes exist, the pane is visible; collapse via the ⇲ button
        # in the header (forgets the pane); a 📝 button in the action
        # row brings it back.
        self._pq_notes_outer = tk.Frame(self._pq_layout_pw, bg=SURF,
                                          highlightbackground=BORDER,
                                          highlightthickness=1)
        notes_hdr = tk.Frame(self._pq_notes_outer, bg=SURF3)
        notes_hdr.pack(fill="x")
        tk.Label(notes_hdr, text="MARGIN NOTES", font=FL,
                 bg=SURF3, fg=SUB, anchor="w",
                 padx=12, pady=6).pack(side="left")
        # Collapse button — removes the pane from the PanedWindow.
        self._pq_notes_collapse_btn = tk.Label(
            notes_hdr, text="✕", font=FBT, bg=SURF3, fg=SUB,
            cursor="hand2", padx=10, pady=6)
        self._pq_notes_collapse_btn.pack(side="right")
        self._pq_notes_collapse_btn.bind(
            "<Enter>", lambda e: self._pq_notes_collapse_btn.config(fg=ERR))
        self._pq_notes_collapse_btn.bind(
            "<Leave>", lambda e: self._pq_notes_collapse_btn.config(fg=SUB))
        self._pq_notes_collapse_btn.bind(
            "<Button-1>", lambda e: self._pq_toggle_notes_pane(False))
        self._pq_notes_count_lbl = tk.Label(
            notes_hdr, text="", font=FB, bg=SURF3, fg=SUB,
            padx=8, pady=6)
        self._pq_notes_count_lbl.pack(side="right")

        notes_body = tk.Frame(self._pq_notes_outer, bg=SURF)
        notes_body.pack(fill="both", expand=True)
        self._pq_notes_canvas = tk.Canvas(
            notes_body, bg=SURF, highlightthickness=0)
        notes_sb = _SlimScrollbar(notes_body,
                                   command=self._pq_notes_canvas.yview)
        notes_sb.pack(side="right", fill="y")
        self._pq_notes_canvas.pack(side="left", fill="both", expand=True)
        self._pq_notes_canvas.configure(yscrollcommand=notes_sb.set)
        self._pq_notes_inner = tk.Frame(self._pq_notes_canvas, bg=SURF)
        self._pq_notes_canvas.create_window(
            (0, 0), window=self._pq_notes_inner, anchor="nw")
        self._pq_notes_inner.bind(
            "<Configure>",
            lambda e: self._pq_notes_canvas.configure(
                scrollregion=self._pq_notes_canvas.bbox("all")))
        # When the canvas resizes (panel sash drag, window resize, or
        # layout flip), reflow each note's body text + snippet so the
        # wraplength matches the new available width.
        self._pq_notes_canvas.bind(
            "<Configure>",
            lambda e: self._pq_update_notes_wraplength(e.width))

        # Mouse-wheel routing: a global bind_all handler from
        # _scroll_frame steals wheel events meant for this panel, so
        # bind directly on the panel widgets and return "break" to
        # short-circuit the global chain.  _pq_bind_wheel_to is
        # called again inside _pq_render_notes_panel to cover the
        # row widgets that get rebuilt every refresh.
        self._pq_bind_wheel_to(self._pq_notes_outer)

        # Add panes to the PanedWindow.  Transcript first (it gets the
        # leading position regardless of orient), notes second when any
        # exist.  The notes pane is added/removed dynamically by
        # _pq_render_notes_panel based on note presence.
        self._pq_layout_pw.add(tx_outer, minsize=320, stretch="always")
        self._pq_render_notes_panel()

        # Primary actions live on the same bottom row as BACK TO PROJECT.
        # Right-anchored so they hug the right edge regardless of how much
        # the hint text on the left consumes.
        self._btn(nav, "COPY SELECTION AS @PULL",
                  self._pq_copy_as_pull,
                  color=ACCENT).pack(side="right")
        self._pq_play_btn = self._btn(
            nav, "▶ PLAY SELECTION",
            self._pq_play_selection)
        self._pq_play_btn.pack(side="right", padx=(0, 8))

        # 📝 notes-pane toggle sits between hint text and PLAY/COPY.
        self._pq_notes_toggle_btn = tk.Label(
            nav, text="📝", font=FB, bg=SURF3, fg=TEXT,
            cursor="hand2", padx=10, pady=4)
        self._pq_notes_toggle_btn.pack(side="right", padx=(0, 12))
        self._pq_notes_toggle_btn.bind(
            "<Button-1>",
            lambda e: self._pq_toggle_notes_pane(None))
        self._pq_refresh_notes_toggle_btn()

        # Apply current mode to update badge styling + key behaviour
        self._pq_apply_mode()

        # Restore stashed state from an orient-rebuild, if any.
        if _stash:
            try:
                if "search" in _stash and _stash["search"]:
                    self._pq_search_var.set(_stash["search"])
                if "collapsed" in _stash:
                    self._pq_notes_user_collapsed = bool(_stash["collapsed"])
                    self._pq_render_notes_panel()
                    self._pq_refresh_notes_toggle_btn()
                if "yview" in _stash and _stash["yview"]:
                    self.after(50, lambda y=_stash["yview"]:
                               self._pq_tx_text.yview_moveto(y[0])
                               if hasattr(self, "_pq_tx_text") else None)
            except Exception:
                pass

        # Layout-driven orient switching: when the window is resized
        # across PQ_LAYOUT_THRESHOLD, rebuild the view with the other
        # orient.  Debounced inside _pq_on_layout_resize.
        # Bind on the toplevel root so we catch every Configure event.
        if not getattr(self, "_pq_layout_resize_bound", False):
            self.bind("<Configure>", self._pq_on_layout_resize, add="+")
            self._pq_layout_resize_bound = True

        if transcribe_now:
            self.after(80,
                       lambda s=session: self._pq_run_transcription(s))
        elif session.get("_progress", {}).get("active"):
            # The user opened a session that's already mid-transcription
            # (kicked off earlier and they navigated back).  Resume the
            # animation and show the placeholder pane.
            if (btn := getattr(self, "_pq_retx_btn", None)):
                btn.config(text="TRANSCRIBING…", fg=SUB)
            self._pq_render_progress_placeholder()
            self._pq_animate_progress()

    def _pq_render_media_list(self, session):
        box = getattr(self, "_pq_media_box", None)
        if not box:
            return
        for w_ in list(box.winfo_children()):
            w_.destroy()
        media = session.get("media", [])
        if not media:
            tk.Label(box, text="(no media)", font=FB,
                     bg=SURF, fg=SUB).pack(anchor="w")
            return
        for p in media:
            row = tk.Frame(box, bg=SURF)
            row.pack(anchor="w")
            ok = os.path.isfile(p)
            tk.Label(row, text=("✓ " if ok else "✗ "),
                     font=FB, bg=SURF,
                     fg=SUCCESS if ok else ERR
                     ).pack(side="left")
            tk.Label(row, text=p,
                     font=FB, bg=SURF,
                     fg=TEXT if ok else ERR
                     ).pack(side="left")

    def _pq_capture_text_state(self):
        """Snapshot the text widget's interactive state so a streaming
        re-render can restore it instead of jumping the user to the end.

        Anchors cursor / selection by the id() of the word-dict the
        cursor is currently in, not by character index — character
        indexes can shift slightly across re-renders (paragraph break
        positions move when new context arrives) but the word dicts
        themselves are stable across streaming updates.

        Returns a dict; pair with _pq_restore_text_state().
        """
        tx = getattr(self, "_pq_tx_text", None)
        if tx is None:
            return None
        state = {}
        try:
            if not tx.winfo_exists():
                return None
            word_idx = getattr(self, "_pq_word_index", []) or []
            # Cursor position by word id AND timestamp.  The id lets us
            # restore exactly when the word dict survives the re-render
            # (most ticks).  The timestamp is a fallback for the cases
            # where the merge replaces the cursor's word with a fresh
            # dict at the same audio time (refined pass overwriting
            # tiny words) — without it, the cursor jumps to the end of
            # the buffer mid-stream and feels twitchy.
            try:
                c = _tx_count_chars(tx, "1.0", tx.index("insert"))
                for s, e, w in word_idx:
                    if s <= c <= e:
                        state["insert_word_id"] = id(w)
                        try:
                            state["insert_word_t"] = (
                                float(w.get("start", 0.0))
                                + float(w.get("end", 0.0))) / 2.0
                        except Exception:
                            pass
                        break
            except Exception:
                pass
            # Selection range by word ids
            try:
                f = _tx_count_chars(tx, "1.0", tx.index("sel.first"))
                l = _tx_count_chars(tx, "1.0", tx.index("sel.last"))
                first_id = last_id = None
                for s, e, w in word_idx:
                    if first_id is None and s <= f <= e:
                        first_id = id(w)
                    if s <= l <= e:
                        last_id = id(w)
                if first_id is not None and last_id is not None:
                    state["sel_first_id"] = first_id
                    state["sel_last_id"]  = last_id
            except tk.TclError:
                pass
            # Scroll position — store the absolute DISTANCE from the
            # top, in display lines.  This is invariant to content
            # changes BELOW the viewport (the streaming case): as
            # more words append, the user's distance-from-top stays
            # the same so they keep seeing the same content.  Beats
            # any fraction-based or word-anchored scheme, because
            # the top is a fixed anchor that nothing can shift.
            try:
                state["top_display_lines"] = _tx_count_displaylines(
                    tx, "1.0", "@0,0")
            except Exception:
                pass
            # Was the widget focused?
            try:
                state["had_focus"] = (self.focus_get() is tx)
            except Exception:
                state["had_focus"] = False
        except Exception:
            return None
        return state

    def _pq_restore_text_state(self, state):
        """Restore cursor, selection, scroll, focus after a re-render.
        Pairs with _pq_capture_text_state."""
        if not state:
            return
        tx = getattr(self, "_pq_tx_text", None)
        if tx is None:
            return
        try:
            if not tx.winfo_exists():
                return
            word_idx = getattr(self, "_pq_word_index", []) or []
            # Cursor: try id-match first, then fall back to the word
            # whose audio time straddles the saved cursor timestamp.
            # The fallback keeps the cursor stable during streaming
            # when the refined pass swaps in fresh dicts for the same
            # audio range.
            ins_id = state.get("insert_word_id")
            ins_t  = state.get("insert_word_t")
            placed = False
            if ins_id is not None:
                for s, e, w in word_idx:
                    if id(w) == ins_id:
                        try:
                            tx.mark_set("insert",
                                         "1.0+{}c".format(s))
                            placed = True
                        except tk.TclError:
                            pass
                        break
            if not placed and ins_t is not None:
                best = None
                best_dt = float("inf")
                for s, e, w in word_idx:
                    try:
                        ws = float(w.get("start", 0.0))
                        we = float(w.get("end",   ws))
                    except Exception:
                        continue
                    if ws <= ins_t <= we:
                        best = s
                        best_dt = 0.0
                        break
                    mid = (ws + we) / 2.0
                    dt = abs(mid - ins_t)
                    if dt < best_dt:
                        best_dt = dt
                        best = s
                if best is not None:
                    try:
                        tx.mark_set("insert", "1.0+{}c".format(best))
                    except tk.TclError:
                        pass
            # Selection
            sf = state.get("sel_first_id")
            sl = state.get("sel_last_id")
            if sf is not None and sl is not None:
                new_first = new_last = None
                for s, e, w in word_idx:
                    if id(w) == sf and new_first is None:
                        new_first = "1.0+{}c".format(s)
                    if id(w) == sl:
                        new_last = "1.0+{}c".format(e)
                if new_first and new_last:
                    try:
                        tx.tag_remove("sel", "1.0", "end")
                        tx.tag_add("sel", new_first, new_last)
                    except tk.TclError:
                        pass
            # Scroll restore.  Reset yview to the top, then scroll
            # down by exactly the captured number of display lines.
            # No fraction math, no dlineinfo, no update_idletasks —
            # both calls happen inside the same Tk callback so the
            # widget only paints once with the final scroll state.
            n = state.get("top_display_lines")
            if n is not None:
                try:
                    tx.yview_moveto(0.0)
                    if n > 0:
                        tx.yview_scroll(int(n), "units")
                except (tk.TclError, ValueError):
                    pass
            # Restore focus
            if state.get("had_focus"):
                try:
                    tx.focus_set()
                except tk.TclError:
                    pass
        except Exception:
            pass

    def _pq_render_transcript_text(self, session, placeholder=None,
                                    user_frontier=None):
        """Re-render the transcript pane.

        When `placeholder` is given (non-empty string), show it instead of
        the transcript — used during transcription to display "loading
        model…" / "transcribing…" without flashing a misleading
        "click TRANSCRIBE" prompt.

        `user_frontier` (seconds, optional) hides user-edited words
        whose audio start exceeds it.  Used by the streaming render
        and the pre-stream wipe so preserved edits stay invisible
        until the streaming transcription actually reaches them —
        without removing them from session["transcript"], so they're
        not lost on the next merge tick.
        """
        tx = getattr(self, "_pq_tx_text", None)
        if not tx:
            return
        words = session.get("transcript") or []
        if user_frontier is not None and words:
            words = [w for w in words
                     if w.get("_src") != "user"
                     or float(w.get("start", 0.0)) <= user_frontier]
        tx.configure(state="normal")
        tx.delete("1.0", "end")
        if placeholder:
            tx.insert("1.0", placeholder)
            self._pq_word_index = []
        elif not words:
            tx.insert("1.0",
                      "(no transcript yet — click TRANSCRIBE to generate one)")
            self._pq_word_index = []
        else:
            # ─── Render strategy ───────────────────────────────────────
            # 1. Walk word entries in chronological order.  Whenever
            #    the speaker changes, emit a visible "<SPEAKER>:\n"
            #    label tagged with the "speaker_lbl" style.  Speaker
            #    labels are excluded from word_index so they don't
            #    appear in @PULL clipboard output.
            # 2. Within each speaker run, concatenate words into a
            #    continuous string and run the syntactic paragraph
            #    detector — same logic that handled the single-speaker
            #    case.
            # 3. Insert breaks within speaker runs; speaker turns get a
            #    full blank-line separation regardless.
            tx.tag_configure("speaker_lbl",
                              foreground=ACCENT, font=FBT,
                              spacing1=10, spacing3=4,
                              lmargin1=0, lmargin2=0, rmargin=0)
            # Double-clicking any speaker label opens an inline rename
            # dialog that updates the label across every session row in
            # the project — the global rename Jordan asked for.
            tx.tag_bind(
                "speaker_lbl", "<Double-Button-1>",
                lambda e, t=tx: self._pq_inline_rename_speaker(e, t))

            self._pq_word_index = []

            # Decide whether tiny-tagged words should render faded.
            # Two conditions must hold:
            #   1. A transcription is actively running on this session
            #      (so "tiny" words are a transient draft).
            #   2. The configured model is bigger than tiny (so a
            #      refinement pass is actually coming for them).
            # Without both, tiny-tagged words are the user's final
            # answer and should render solid like any other.
            _prog = session.get("_progress") or {}
            _fade_tiny = (bool(_prog.get("active"))
                          and engines.get_active_model_size() != "tiny")

            # Group consecutive words by speaker
            real_words = [w for w in words
                          if not w.get("break") and (w.get("word") or "").strip()]

            # Detect if any speakers are actually labelled (multi-track)
            speakers_present = any(w.get("speaker") for w in real_words)

            # Filter out cross-track filler/bleed: when one speaker says
            # something brief (e.g. "yeah", "mm-hmm", "right") sandwiched
            # between long stretches of another speaker, that's almost
            # always either bleed picked up by the wrong mic or a
            # listening filler.  Removed at render time so the original
            # transcript is preserved on disk.
            if speakers_present:
                real_words = self._pq_filter_filler_interjections(real_words)

            # Group into [(speaker, [words]), ...]
            groups = []
            cur_speaker = object()
            cur_words   = []
            for w in real_words:
                sp = w.get("speaker") if speakers_present else None
                if sp != cur_speaker:
                    if cur_words:
                        groups.append((cur_speaker, cur_words))
                    cur_speaker = sp
                    cur_words   = []
                cur_words.append(w)
            if cur_words:
                groups.append((cur_speaker, cur_words))

            # Render each group: optional speaker label + paragraphed prose
            for gi, (sp, gwords) in enumerate(groups):
                # Insert speaker label (only when we have speaker info)
                if sp and speakers_present:
                    if gi > 0:
                        tx.insert("end", "\n\n")
                    # Honour per-session speaker-label overrides
                    sess_speakers = session.get("speakers") or {}
                    display = (sess_speakers.get(sp)
                               or sess_speakers.get(os.path.basename(sp))
                               or self._pq_format_speaker(sp))
                    label_text = "{}:\n".format(display)
                    # Insert with both the visual style tag AND a
                    # per-speaker tag carrying the raw key so the
                    # rename handler knows which speaker was clicked.
                    raw_tag = "speaker_raw:{}".format(sp)
                    if raw_tag not in tx.tag_names():
                        tx.tag_configure(raw_tag)   # invisible marker tag
                    tx.insert("end", label_text,
                              ("speaker_lbl", raw_tag))

                # Build continuous text for this speaker's run
                cont_buf  = []
                cont_idx  = []   # (start_in_cont, end_in_cont, word)
                cpos      = 0
                first     = True
                for w in gwords:
                    wt = w.get("word") or ""
                    if not wt.strip():
                        continue
                    if first:
                        wt = wt.lstrip()
                        first = False
                    cont_idx.append((cpos, cpos + len(wt), w))
                    cont_buf.append(wt)
                    cpos += len(wt)
                continuous = "".join(cont_buf)
                if not continuous:
                    continue

                # Syntactic paragraph breaks within this speaker's run
                break_positions = self._pq_detect_paragraph_breaks(continuous)
                parts = []
                last = 0
                for bp in break_positions:
                    parts.append(continuous[last:bp])
                    parts.append("\n\n")
                    last = bp
                parts.append(continuous[last:])
                run_text = "".join(parts)

                # Position in the Text widget where this run starts
                run_start = tx.index("end-1c")
                tx.insert("end", run_text, "body")

                # Record word_index char positions in widget coords.
                # `run_start` is "<line>.<col>" — convert each cont
                # offset into a widget index relative to that.  Use Tk
                # math: run_start + Nc.
                sorted_bps = sorted(break_positions)
                # Convert run_start to absolute char count from "1.0"
                run_start_abs = _tx_count_chars(tx, "1.0", run_start)
                for s, e, w in cont_idx:
                    # Add 2 chars for each break before this word in run
                    n_before = sum(1 for bp in sorted_bps if bp <= s)
                    shift = n_before * 2
                    abs_s = run_start_abs + s + shift
                    abs_e = run_start_abs + e + shift
                    self._pq_word_index.append((abs_s, abs_e, w))
                    # Apply source-based styling.  Tiny-draft words
                    # render faded ONLY while a non-tiny refinement
                    # is actively running — otherwise tiny words are
                    # the user's final choice and should render
                    # solid like any other source.  User edits get
                    # a thin underline (tracked-changes marker) so
                    # the user always knows what they touched.
                    src = w.get("_src")
                    if _fade_tiny and src == "tiny":
                        tx.tag_add(
                            "body_tiny",
                            "1.0 + {}c".format(abs_s),
                            "1.0 + {}c".format(abs_e))
                    elif src == "user":
                        tx.tag_add(
                            "body_user",
                            "1.0 + {}c".format(abs_s),
                            "1.0 + {}c".format(abs_e))

            # Reset undo history — programmatic render is not a user
            # edit, so Ctrl+Z shouldn't revert to "before render".
            try:
                tx.edit_reset()
            except tk.TclError:
                pass
        self._pq_update_status_lbl(session)

    # ── Selection actions ─────────────────────────────────────────────────

    def _pq_apply_mode(self):
        """Vestigial entry point kept so existing callers don't error.
        Edit/Play modes were collapsed into perpetual read-only with a
        right-click context menu — the only remaining responsibility is
        making sure the notes panel is rendered after view setup."""
        tx = getattr(self, "_pq_tx_text", None)
        if tx is not None:
            try:
                tx.config(cursor="xterm")
            except tk.TclError:
                pass
        try:
            self._pq_render_notes_panel()
        except Exception:
            pass

    def _pq_edit_selection(self):
        """Open a dialog letting the user rewrite the words under the
        current selection.  On save, replaces those word entries in
        ``session["transcript"]`` with new entries whose timestamps are
        distributed proportionally across the original time range, then
        re-renders the transcript and persists the session."""
        tx      = getattr(self, "_pq_tx_text", None)
        session = getattr(self, "_pq_current_session", None)
        if tx is None or session is None:
            return

        word_idx = getattr(self, "_pq_word_index", []) or []
        if not word_idx:
            return

        try:
            sel_first = tx.index("sel.first")
            sel_last  = tx.index("sel.last")
        except tk.TclError:
            return

        c0 = _tx_count_chars(tx, "1.0", sel_first)
        c1 = _tx_count_chars(tx, "1.0", sel_last)

        picked = [w for (s, e, w) in word_idx if e > c0 and s < c1]
        if not picked:
            return

        # Locate the picked words in session["transcript"] by identity
        # — they're the same dict objects the renderer captured.  Find
        # the contiguous slice [span_start, span_end) covering them so
        # we can splice in the replacement entries while preserving
        # any non-word entries (breaks, etc.) before/after.
        transcript = session.get("transcript") or []
        picked_ids = {id(w) for w in picked}
        span_start = None
        span_end   = None
        for i, w in enumerate(transcript):
            if id(w) in picked_ids:
                if span_start is None:
                    span_start = i
                span_end = i + 1
        if span_start is None:
            return

        orig_words = transcript[span_start:span_end]
        real_orig  = [w for w in orig_words
                      if (w.get("word") or "").strip() and not w.get("break")]
        if not real_orig:
            return

        range_start = float(real_orig[0].get("start", 0))
        range_end   = float(real_orig[-1].get("end", range_start))
        if range_end <= range_start:
            range_end = range_start + max(0.5, len(real_orig) * 0.3)

        # Most-common speaker among picked words wins (in mixed-track
        # selections this beats arbitrarily picking the first one).
        from collections import Counter
        sp_counter = Counter(w.get("speaker") for w in real_orig
                             if w.get("speaker"))
        common_speaker = (sp_counter.most_common(1)[0][0]
                          if sp_counter else None)

        orig_text = "".join(w.get("word") or "" for w in real_orig).strip()

        win = tk.Toplevel(self)
        win.title("Edit Selection")
        win.configure(bg=BG)
        win.transient(self); win.grab_set()
        win.minsize(560, 320)

        # Bottom buttons first so they survive minimum-height dialogs.
        nav = tk.Frame(win, bg=BG)
        nav.pack(side="bottom", fill="x", padx=20, pady=(8, 14))

        tk.Label(win, text="Edit Transcript Text", font=FH,
                 bg=BG, fg=TEXT, padx=20, pady=14).pack(anchor="w")
        tk.Label(win, text="Time range: {} – {}".format(
                    secs_tc(range_start).split(".")[0],
                    secs_tc(range_end).split(".")[0]),
                 font=FL, bg=BG, fg=SUB, padx=20).pack(anchor="w")
        tk.Label(win,
                 text=("Edited words inherit the original time span; "
                       "timestamps are distributed proportionally.  "
                       "Enter to save · Esc to cancel."),
                 font=FB, bg=BG, fg=SUB, padx=20,
                 wraplength=520, justify="left"
                 ).pack(anchor="w", pady=(0, 8))

        ent = tk.Text(win, font=FB, bg=SURF2, fg=TEXT,
                       insertbackground=TEXT, relief="flat",
                       bd=8, height=3, wrap="word", undo=True)
        ent.pack(fill="both", expand=True, padx=20, pady=(8, 0))
        ent.insert("1.0", orig_text)
        ent.tag_add("sel", "1.0", "end-1c")
        ent.focus_set()

        def _save(_e=None):
            new_text = ent.get("1.0", "end-1c").strip()
            toks = new_text.split() if new_text else []

            # Whisper convention: every word includes any natural
            # leading whitespace (e.g. " Hello,").  Preserve the first
            # ORIGINAL word's leading-space behaviour for the first new
            # word so the new text doesn't fuse to the preceding word.
            # E.g., replacing " world" with "earth" must give " earth"
            # not "earth" — otherwise "Hello world" becomes "Helloearth".
            first_orig_word = real_orig[0].get("word", "") if real_orig else ""
            first_starts_with_space = (
                len(first_orig_word) > 0
                and first_orig_word[0] in (" ", "\t"))

            # Unique id for this edit operation.  Every new entry from
            # this Save shares the same _edit_id, so "Restore Whisper
            # original" can locate the entire group from any one of
            # them.  Time-ns hex is unique within the session and
            # keeps the value short.
            edit_id = "e{:x}".format(time.time_ns())

            # Snapshot the original Whisper entries so a later restore
            # can splice them back exactly as Whisper produced them.
            # dict() to defensively decouple from later mutations of
            # the original entries (e.g. a refine pass updating
            # timestamps in place).
            replaces_blob = [dict(w) for w in orig_words]

            # Empty input (no tokens) deletes the selected words.
            # Useful for cleaning up cases like "ok ay" -> "okay" where
            # the user wants to remove a stray token, or "the the dog"
            # -> "the dog" where Whisper duplicated a word.
            new_entries = []
            if toks:
                n     = len(toks)
                total = max(0.05, range_end - range_start)
                per   = total / n
                for i, tok in enumerate(toks):
                    if i == 0:
                        wt = (" " if first_starts_with_space else "") + tok
                    else:
                        wt = " " + tok
                    entry = {
                        "word":  wt,
                        "start": range_start + i * per,
                        "end":   range_start + (i + 1) * per,
                        # Mark these words as user-authored so a later
                        # progressive-refinement pass preserves them
                        # instead of overwriting with Whisper output.
                        "_src":      "user",
                        "_edit_id":  edit_id,
                        "_replaces": replaces_blob,
                    }
                    if common_speaker:
                        entry["speaker"] = common_speaker
                    new_entries.append(entry)
            else:
                # Deletion: leave a single user-tagged placeholder so
                # the merge layer knows this range is intentionally
                # empty and shouldn't be refilled by a later refine
                # pass.  Otherwise the configured-model output would
                # come in and re-insert the words Jordan just deleted.
                #
                # Implementation: zero-width "user" marker with the
                # original time range.  Renders as nothing visible
                # but takes part in _pq_user_edited_spans so the
                # merge respects the deletion.
                new_entries.append({
                    "word":      "",
                    "start":     range_start,
                    "end":       range_end,
                    "_src":      "user",
                    "_deleted":  True,
                    "_edit_id":  edit_id,
                    "_replaces": replaces_blob,
                })
                if common_speaker:
                    new_entries[0]["speaker"] = common_speaker

            # Re-resolve the splice against the LIVE session transcript.
            # While the dialog was open, streaming ticks may have
            # appended more words OR replaced some via merge — using
            # the captured `transcript` snapshot would silently delete
            # any words added in that window.  Try id() lookup first
            # (the picked dicts are still in transcript when no merge
            # has run); fall back to time-range lookup if a refine
            # pass replaced them with fresh dicts at the same span.
            live = session.get("transcript")
            if live is not None and live is not transcript:
                live_start = None
                live_end   = None
                for i, w in enumerate(live):
                    if id(w) in picked_ids:
                        if live_start is None:
                            live_start = i
                        live_end = i + 1
                if live_start is None:
                    # Refine pass swept the picked dicts.  Splice by
                    # time-range overlap of real (non-user, non-break)
                    # words instead.
                    for i, w in enumerate(live):
                        if w.get("break") or w.get("_src") == "user":
                            continue
                        ws = float(w.get("start", 0.0))
                        we = float(w.get("end",   ws))
                        mid = (ws + we) / 2.0
                        if range_start <= mid <= range_end:
                            if live_start is None:
                                live_start = i
                            live_end = i + 1
                if live_start is None:
                    # Nothing to replace — just insert in time order.
                    live.extend(new_entries)
                    live.sort(
                        key=lambda w: float(w.get("start", 0.0)))
                else:
                    live[live_start:live_end] = new_entries
                session["transcript"] = live
            else:
                transcript[span_start:span_end] = new_entries
                session["transcript"] = transcript
            try:
                self._pq_save_session_file(session)
            except Exception:
                pass
            win.destroy()
            # Snapshot full interactive state (word-anchored scroll)
            # before the re-render, then put the cursor at the END
            # of the new entries (where the user would naturally
            # continue) and replay the scroll anchor.
            tx_after = getattr(self, "_pq_tx_text", None)
            pre_state = self._pq_capture_text_state()
            try:
                self._pq_render_transcript_text(session)
            except Exception:
                pass
            self._pq_restore_text_state(pre_state)
            if tx_after is not None and new_entries:
                last_id = id(new_entries[-1])
                word_idx = getattr(self, "_pq_word_index", []) or []
                for s, e, w in word_idx:
                    if id(w) == last_id:
                        try:
                            tx_after.tag_remove("sel", "1.0", "end")
                            tx_after.mark_set(
                                "insert", "1.0+{}c".format(e))
                        except tk.TclError:
                            pass
                        break
            return "break"

        _W = 8
        self._btn(nav, "SAVE", _save,
                  color=ACCENT, width=_W).pack(side="right")
        self._btn(nav, "CANCEL", win.destroy, width=_W
                  ).pack(side="right", padx=(0, 8))
        # Enter saves.  Multi-line edits aren't useful here (one
        # transcript region is replaced as a flat span of tokens),
        # so we block the newline insertion entirely by returning
        # "break" from the bindings.  Ctrl+Enter still works for
        # muscle-memory compatibility.
        def _save_and_break(_e=None):
            _save()
            return "break"
        for seq in ("<Return>", "<KP_Enter>",
                    "<Control-Return>", "<Control-KP_Enter>"):
            ent.bind(seq, _save_and_break)
            win.bind(seq, _save_and_break)
        win.bind("<Escape>", lambda e: win.destroy())

        self._center_dialog(win, 560, 320)
        self.wait_window(win)

    def _pq_selection_has_user_edits(self):
        """True when the current selection overlaps any user-edited
        entries that carry restore data (_edit_id + _replaces).  Used
        to gate the 'Restore Whisper original' menu item — no point
        showing it when there's nothing to restore."""
        tx      = getattr(self, "_pq_tx_text", None)
        session = getattr(self, "_pq_current_session", None)
        if tx is None or session is None:
            return False
        word_idx = getattr(self, "_pq_word_index", []) or []
        if not word_idx:
            return False
        try:
            sel_first = tx.index("sel.first")
            sel_last  = tx.index("sel.last")
        except tk.TclError:
            return False
        c0 = _tx_count_chars(tx, "1.0", sel_first)
        c1 = _tx_count_chars(tx, "1.0", sel_last)
        for (s, e, w) in word_idx:
            if e <= c0 or s >= c1:
                continue
            if w.get("_src") == "user" and w.get("_edit_id"):
                return True
        return False

    def _pq_restore_originals(self):
        """Right-click action: replace every user-edited word in the
        current selection with the original Whisper entries captured
        at edit time.  Each user entry carries _edit_id + _replaces;
        restoration is grouped by _edit_id so a multi-word edit is
        restored as one atomic block (not piecemeal)."""
        tx      = getattr(self, "_pq_tx_text", None)
        session = getattr(self, "_pq_current_session", None)
        if tx is None or session is None:
            return
        word_idx = getattr(self, "_pq_word_index", []) or []
        if not word_idx:
            return
        try:
            sel_first = tx.index("sel.first")
            sel_last  = tx.index("sel.last")
        except tk.TclError:
            return

        c0 = _tx_count_chars(tx, "1.0", sel_first)
        c1 = _tx_count_chars(tx, "1.0", sel_last)

        # Collect unique _edit_ids touched by the selection that
        # actually have restore data attached.
        touched_ids = []
        seen = set()
        for (s, e, w) in word_idx:
            if e <= c0 or s >= c1:
                continue
            if w.get("_src") != "user":
                continue
            eid = w.get("_edit_id")
            if not eid or not w.get("_replaces"):
                continue
            if eid in seen:
                continue
            seen.add(eid)
            touched_ids.append(eid)

        if not touched_ids:
            return

        transcript = session.get("transcript") or []
        if not transcript:
            return

        # Snapshot the full interactive state before mutating the
        # transcript so the cursor / selection / scroll all survive
        # the re-render that follows.  Using the shared capture
        # helper means the scroll is anchored to the top-visible
        # WORD rather than a yview fraction — robust against the
        # transcript's char count shifting during streaming.
        tx = getattr(self, "_pq_tx_text", None)
        pre_state = self._pq_capture_text_state()

        # For each touched edit_id, find the contiguous slice of
        # entries sharing it and splice in the originals.  An
        # edit_id should always be contiguous in the transcript
        # (the edit dialog inserts new entries as one block) but
        # we don't rely on that — we locate all matching indices
        # and use [min, max+1] as the splice range.  Track the
        # ids of the freshly-spliced dicts so the cursor lands on
        # the first restored word after the re-render.
        first_restored_id = None
        for eid in touched_ids:
            indices = [i for i, w in enumerate(transcript)
                       if w.get("_edit_id") == eid]
            if not indices:
                continue
            lo = min(indices)
            hi = max(indices) + 1
            originals = transcript[lo].get("_replaces") or []
            # Defensive copy so future restores don't share dict
            # identity with what's now back in the transcript.
            restored = [dict(o) for o in originals]
            if restored and first_restored_id is None:
                first_restored_id = id(restored[0])
            transcript[lo:hi] = restored

        session["transcript"] = transcript
        try:
            self._pq_save_session_file(session)
        except Exception:
            pass
        try:
            self._pq_render_transcript_text(session)
        except Exception:
            pass

        # Restore scroll via the shared helper (word-anchored), then
        # place cursor at the first restored word and clear the
        # stale selection (the words the user had selected just
        # ceased to exist as edits).
        self._pq_restore_text_state(pre_state)
        if tx is not None:
            try:
                tx.tag_remove("sel", "1.0", "end")
            except tk.TclError:
                pass
            if first_restored_id is not None:
                word_idx = getattr(self, "_pq_word_index", []) or []
                for s, e, w in word_idx:
                    if id(w) == first_restored_id:
                        try:
                            tx.mark_set("insert", "1.0+{}c".format(s))
                        except tk.TclError:
                            pass
                        break

    def _pq_add_margin_note(self):
        """Margin-mode action: prompt for a note tied to the current
        selection (or to the cursor word if no selection).  Notes are
        persisted on session["notes"] and rendered as small inline
        markers — they never appear in @PULL clipboard output."""
        tx      = getattr(self, "_pq_tx_text", None)
        session = getattr(self, "_pq_current_session", None)
        if tx is None or session is None:
            return

        word_idx = getattr(self, "_pq_word_index", []) or []
        if not word_idx:
            return

        try:
            sel_first = tx.index("sel.first")
            sel_last  = tx.index("sel.last")
        except tk.TclError:
            sel_first = sel_last = None

        if sel_first and sel_last:
            c0 = _tx_count_chars(tx, "1.0", sel_first)
            c1 = _tx_count_chars(tx, "1.0", sel_last)
            picked = [w for (s, e, w) in word_idx if e > c0 and s < c1]
        else:
            c0 = _tx_count_chars(tx, "1.0", tx.index("insert"))
            picked = [w for (s, e, w) in word_idx if s <= c0 < e]

        if not picked:
            return

        # Use the start time of the first selected word as the anchor —
        # notes survive transcript re-renders that way.
        anchor_start = float(picked[0].get("start", 0))
        anchor_end   = float(picked[-1].get("end", anchor_start))

        # Preview shows a snippet of the anchored quote
        snippet = "".join(w.get("word") or "" for w in picked).strip()[:80]

        win = tk.Toplevel(self)
        win.title("Margin Note")
        win.configure(bg=BG)
        win.transient(self); win.grab_set()
        win.minsize(560, 380)

        # Pack the bottom button row FIRST (side="bottom") so it's
        # guaranteed to be visible even when the dialog is the
        # minimum height — otherwise the editor pushes it off-screen.
        nav = tk.Frame(win, bg=BG)
        nav.pack(side="bottom", fill="x", padx=20, pady=(8, 14))

        tk.Label(win, text="Add Margin Note", font=FH,
                 bg=BG, fg=TEXT, padx=20, pady=14).pack(anchor="w")
        tk.Label(win, text="Anchored to:", font=FL,
                 bg=BG, fg=SUB, padx=20).pack(anchor="w")
        tk.Label(win, text='"{}"'.format(snippet),
                 font=FB, bg=BG, fg=ACCENT, padx=20,
                 wraplength=520, justify="left"
                 ).pack(anchor="w", pady=(0, 8))
        tk.Label(win,
                 text="Ctrl+Enter to save · Esc to cancel",
                 font=FB, bg=BG, fg=SUB, padx=20).pack(anchor="w")

        ent = tk.Text(win, font=FB, bg=SURF2, fg=TEXT,
                       insertbackground=TEXT, relief="flat",
                       bd=8, height=6, wrap="word")
        ent.pack(fill="both", expand=True, padx=20, pady=(8, 0))
        ent.focus_set()

        def _save(_e=None):
            text = ent.get("1.0", "end-1c").strip()
            if not text:
                win.destroy(); return "break"
            notes = list(session.get("notes") or [])
            notes.append({
                "anchor_start": anchor_start,
                "anchor_end":   anchor_end,
                "snippet":      snippet,
                "text":         text,
                "ts":           __import__("datetime").datetime.now()
                                  .isoformat(timespec="seconds"),
            })
            session["notes"] = notes
            self._pq_save_session_file(session)
            win.destroy()
            self._pq_render_notes_panel()
            return "break"

        _W = 10
        self._btn(nav, "SAVE NOTE", _save,
                  color=ACCENT, width=_W).pack(side="right")
        self._btn(nav, "CANCEL", win.destroy, width=_W
                  ).pack(side="right", padx=(0, 8))
        # Ctrl+Enter from anywhere in the dialog (incl. inside the Text
        # widget where Tk's class binding for <Return> would normally
        # eat the event).  Bind on both the window and the editor.
        win.bind("<Control-Return>",       _save)
        win.bind("<Control-KP_Enter>",     _save)
        ent.bind("<Control-Return>",       _save)
        ent.bind("<Control-KP_Enter>",     _save)
        win.bind("<Escape>", lambda e: win.destroy())

        self._center_dialog(win, 560, 380)
        self.wait_window(win)

    def _pq_update_notes_wraplength(self, canvas_width):
        """Set wraplength on every note row's body / snippet label so
        the text reflows when the user resizes the notes pane."""
        # Subtract margins: row padding (12) + delete button (~24) +
        # bullet (~20) + safety (~8) ≈ 64 px reserved.
        new_w = max(180, int(canvas_width) - 64)
        for lbl in (getattr(self, "_pq_notes_body_lbls", []) or []):
            try:
                if lbl.winfo_exists():
                    lbl.config(wraplength=new_w)
            except tk.TclError:
                pass

    def _pq_toggle_notes_pane(self, force=None):
        """Show/hide the margin-notes pane.

        force=True  → show.  force=False → hide.  force=None → toggle.
        Tracked via self._pq_notes_user_collapsed so a redraw of the
        notes panel honours the user's preference.
        """
        if force is None:
            cur = bool(getattr(self, "_pq_notes_user_collapsed", False))
            self._pq_notes_user_collapsed = not cur
        else:
            self._pq_notes_user_collapsed = not bool(force)
        self._pq_render_notes_panel()
        self._pq_refresh_notes_toggle_btn()

    def _pq_refresh_notes_toggle_btn(self):
        """Update the 📝 button label + colour based on current state."""
        btn = getattr(self, "_pq_notes_toggle_btn", None)
        session = getattr(self, "_pq_current_session", None)
        if btn is None or session is None:
            return
        try:
            notes = session.get("notes") or []
            collapsed = bool(getattr(self, "_pq_notes_user_collapsed", False))
            if not notes:
                btn.config(text="📝 0 notes", fg=SUB, bg=SURF3)
            elif collapsed:
                btn.config(
                    text="📝 {} note{} ▸".format(
                        len(notes), "s" if len(notes) != 1 else ""),
                    fg=TEXT, bg=SURF3)
            else:
                btn.config(
                    text="📝 {} note{}".format(
                        len(notes), "s" if len(notes) != 1 else ""),
                    fg=ACCENT, bg=SURF3)
        except tk.TclError:
            pass

    def _pq_on_layout_resize(self, event=None):
        """Debounced window-resize callback.  When the window crosses
        the layout threshold, switch PanedWindow orientation by
        rebuilding the session view (cheap; only fires on threshold
        crossings, not on every resize tick)."""
        if getattr(self, "_pq_current_session", None) is None:
            return
        # Debounce: cancel any pending check, schedule fresh one.
        prev = getattr(self, "_pq_layout_resize_after", None)
        if prev is not None:
            try: self.after_cancel(prev)
            except Exception: pass
        self._pq_layout_resize_after = self.after(
            220, self._pq_check_layout_orient)

    def _pq_check_layout_orient(self):
        """If the current window width crosses the threshold for the
        opposite orientation, rebuild the session view with that
        orient."""
        sess = getattr(self, "_pq_current_session", None)
        if sess is None:
            return
        cur_w = self.winfo_width()
        desired = "horizontal" if cur_w >= 1100 else "vertical"
        cur_orient = getattr(self, "_pq_layout_orient", desired)
        if desired == cur_orient:
            return
        # Stash transient state we want to preserve across rebuild
        try:
            stash = {
                "search": (self._pq_search_var.get()
                           if hasattr(self, "_pq_search_var") else ""),
                "yview":  (self._pq_tx_text.yview()
                           if hasattr(self, "_pq_tx_text") else None),
                "collapsed": bool(getattr(
                    self, "_pq_notes_user_collapsed", False)),
            }
        except Exception:
            stash = {}
        self._pq_layout_resize_stash = stash
        self._pq_open_session_view(sess)

    def _pq_wheel_notes(self, event):
        """MouseWheel handler that scrolls the margin-notes canvas.
        Returns "break" so the global bind_all wheel handler from
        _scroll_frame doesn't also fire."""
        canvas = getattr(self, "_pq_notes_canvas", None)
        if canvas is None:
            return
        try:
            if not canvas.winfo_exists():
                return
        except tk.TclError:
            return
        # macOS reports raw notch counts (1/-1); X11 uses Button-4/5;
        # Windows uses delta multiples of 120.
        if sys.platform == "darwin":
            units = int(-1 * event.delta)
        else:
            units = int(-1 * (event.delta / 120)) if event.delta else 0
        if units:
            canvas.yview_scroll(units, "units")
        return "break"

    def _pq_bind_wheel_to(self, widget):
        """Recursively bind MouseWheel events on widget + every
        descendant to the notes-canvas scroller.  Idempotent: re-binding
        the same widget just replaces the handler."""
        if widget is None:
            return
        try:
            widget.bind("<MouseWheel>", self._pq_wheel_notes)
            widget.bind("<Button-4>",
                lambda e: (self._pq_notes_canvas.yview_scroll(-3, "units"),
                           "break")[1])
            widget.bind("<Button-5>",
                lambda e: (self._pq_notes_canvas.yview_scroll( 3, "units"),
                           "break")[1])
        except tk.TclError:
            return
        for c in widget.winfo_children():
            self._pq_bind_wheel_to(c)

    def _pq_render_notes_panel(self):
        """Refresh the MARGIN NOTES side panel.  Shows one row per note
        with snippet (clickable to jump to anchor) + body + delete.
        Hidden when there are no notes AND we're not in MARGIN mode."""
        outer = getattr(self, "_pq_notes_outer", None)
        inner = getattr(self, "_pq_notes_inner", None)
        cnt   = getattr(self, "_pq_notes_count_lbl", None)
        session = getattr(self, "_pq_current_session", None)
        if outer is None or inner is None or session is None:
            return

        notes = session.get("notes") or []

        # Visibility: notes_outer is a pane in the layout PanedWindow.
        # Add it when notes exist + not user-collapsed; remove when no
        # notes or user collapsed it.
        pw = getattr(self, "_pq_layout_pw", None)
        try:
            if pw is not None and pw.winfo_exists():
                in_panes = str(outer) in [str(p) for p in pw.panes()]
                user_collapsed = bool(getattr(
                    self, "_pq_notes_user_collapsed", False))
                want_visible = bool(notes) and not user_collapsed
                if want_visible and not in_panes:
                    # Add as second pane with a sane minimum.  Stretch
                    # "never" so it stays at its preferred width unless
                    # the user explicitly drags the sash.
                    pw.add(outer, minsize=240, stretch="never",
                            sticky="nsew")
                elif not want_visible and in_panes:
                    pw.forget(outer)
        except tk.TclError:
            pass

        # Repopulate — also clear out the body-label registry so we
        # can rebuild it as new note rows are created.
        for w in list(inner.winfo_children()):
            w.destroy()
        self._pq_notes_body_lbls = []

        if cnt is not None:
            try:
                cnt.config(text="{} note{}".format(
                    len(notes), "s" if len(notes) != 1 else ""))
            except tk.TclError:
                pass
        # Keep the action-row toggle button in sync as notes are
        # added/deleted.
        try:
            self._pq_refresh_notes_toggle_btn()
        except Exception:
            pass

        if not notes:
            tk.Label(inner,
                     text=("  No notes yet.  In MARGIN mode (Ctrl+M), "
                           "select text in the transcript and press Enter "
                           "to add one."),
                     font=FB, bg=SURF, fg=SUB, padx=8, pady=12,
                     wraplength=900, justify="left").pack(anchor="w")
            return

        # Sort by anchor_start so notes appear in transcript order
        for note_idx, n in enumerate(sorted(
                notes, key=lambda x: float(x.get("anchor_start", 0)))):
            row = tk.Frame(inner, bg=SURF,
                            highlightbackground=BORDER, highlightthickness=1)
            row.pack(fill="x", padx=6, pady=4)
            inner_row = tk.Frame(row, bg=SURF)
            inner_row.pack(fill="x", padx=8, pady=6)

            ts_s = float(n.get("anchor_start", 0))
            tc_str = secs_tc(ts_s).split(".")[0]

            # Header: timecode + snippet (clickable) + delete
            hdr = tk.Frame(inner_row, bg=SURF)
            hdr.pack(fill="x")
            tc_lbl = tk.Label(hdr, text="▸ " + tc_str, font=FB,
                               bg=SURF, fg=ACCENT, cursor="hand2")
            tc_lbl.pack(side="left")
            snippet = (n.get("snippet") or "").strip()
            if snippet:
                # Snippet (quoted anchor text) stays single-line and
                # never wraps — only the note BODY reflows with panel
                # width.  Truncate to 60 chars; the full anchor lives
                # in the underlying word entries anyway.
                snip_text = snippet[:60] + ("…" if len(snippet) > 60 else "")
                snip_lbl = tk.Label(hdr,
                                     text='  "{}"'.format(snip_text),
                                     font=FB, bg=SURF, fg=SUB,
                                     cursor="hand2", anchor="w")
                snip_lbl.pack(side="left", fill="x", expand=True)
            else:
                snip_lbl = None

            del_lbl = tk.Label(hdr, text="✕", font=FB,
                                bg=SURF, fg=SUB, cursor="hand2",
                                padx=4)
            del_lbl.pack(side="right")
            del_lbl.bind("<Enter>", lambda e, w=del_lbl: w.config(fg=ERR))
            del_lbl.bind("<Leave>", lambda e, w=del_lbl: w.config(fg=SUB))
            def _delete(_e=None, target=n):
                cur = list(session.get("notes") or [])
                # Remove by identity-equivalence on a tuple key
                key = (target.get("anchor_start"),
                       target.get("text"),
                       target.get("ts"))
                session["notes"] = [
                    nn for nn in cur
                    if (nn.get("anchor_start"),
                        nn.get("text"),
                        nn.get("ts")) != key
                ]
                self._pq_save_session_file(session)
                self._pq_render_notes_panel()
            del_lbl.bind("<Button-1>", _delete)

            # Body: the note text.  wraplength is updated dynamically
            # via _pq_update_notes_wraplength based on the current
            # canvas width so the text reflows when the user resizes
            # the panel.
            body_lbl = tk.Label(inner_row, text=(n.get("text") or "").strip(),
                                 font=FB, bg=SURF, fg=TEXT,
                                 anchor="w", justify="left",
                                 wraplength=400)
            body_lbl.pack(anchor="w", fill="x", pady=(4, 0))
            # Only the BODY label wraps with the panel.  The snippet
            # stays single-line (truncated above) — it's a quote
            # reference, not the user's content.
            self._pq_notes_body_lbls = getattr(
                self, "_pq_notes_body_lbls", [])
            self._pq_notes_body_lbls.append(body_lbl)

            # Click-to-jump: scroll the transcript pane to this note's anchor
            def _jump(_e=None, t_s=ts_s):
                self._pq_jump_to_time(t_s)
            tc_lbl.bind("<Button-1>", _jump)
            if snip_lbl is not None:
                snip_lbl.bind("<Button-1>", _jump)

        # After the rows are built, push the current canvas width into
        # each body label so the new rows wrap correctly without
        # waiting for the next <Configure> event.
        try:
            cw = self._pq_notes_canvas.winfo_width()
            if cw > 1:
                self._pq_update_notes_wraplength(cw)
            else:
                # First render before the canvas has a width — defer.
                self.after(50,
                           lambda: self._pq_update_notes_wraplength(
                               self._pq_notes_canvas.winfo_width()))
        except tk.TclError:
            pass

        # Re-bind wheel events on every freshly-created row widget so
        # scrolling works regardless of which child the cursor hovers.
        try:
            self._pq_bind_wheel_to(self._pq_notes_outer)
        except Exception:
            pass

    def _pq_jump_to_time(self, time_s):
        """Scroll the transcript Text widget so the word nearest
        `time_s` is visible.  Used by the notes panel click-to-jump."""
        tx = getattr(self, "_pq_tx_text", None)
        word_idx = getattr(self, "_pq_word_index", []) or []
        if tx is None or not word_idx:
            return
        # Find nearest word by start time
        best = None
        best_dist = float("inf")
        for s, e, w in word_idx:
            ws = float(w.get("start", 0))
            d = abs(ws - time_s)
            if d < best_dist:
                best_dist = d
                best = s
        if best is None:
            return
        try:
            idx = "1.0+{}c".format(best)
            tx.see(idx)
            tx.mark_set("insert", idx)
            tx.focus_set()
            # Brief flash highlight
            end_idx = "1.0+{}c".format(best + 30)
            tx.tag_configure("pq_jump_flash",
                              background="#5a3a10")
            tx.tag_add("pq_jump_flash", idx, end_idx)
            self.after(1200,
                       lambda: tx.tag_remove("pq_jump_flash", "1.0", "end"))
        except tk.TclError:
            pass

    def _pq_manage_speakers_dialog(self):
        """List every speaker raw-tag found across the project's sessions
        and let the user rename any of them.  Renames apply to all
        sessions and persist to disk."""
        project = getattr(self, "_pq_project", None)
        if project is None or not project.get("sessions"):
            messagebox.showinfo("Manage Speakers",
                "No sessions in this project yet.")
            return

        # Gather unique raw speaker tags + their current display labels
        # across every session.  raw → set of sessions that reference it.
        raw_to_sessions = {}
        raw_to_label    = {}
        for sess in project["sessions"]:
            speakers_map = sess.get("speakers") or {}
            for w in (sess.get("transcript") or []):
                raw = w.get("speaker")
                if not raw:
                    continue
                raw_to_sessions.setdefault(raw, []).append(sess)
                # Resolve current label: per-session override OR
                # filename-derived default.
                label = (speakers_map.get(raw)
                         or speakers_map.get(os.path.basename(raw))
                         or self._pq_format_speaker(raw))
                raw_to_label[raw] = label

        if not raw_to_sessions:
            messagebox.showinfo("Manage Speakers",
                "No speaker tags found.  Multi-track sessions get speaker\n"
                "tags automatically when they're transcribed.")
            return

        win = tk.Toplevel(self)
        win.title("Manage Speakers")
        win.configure(bg=BG)
        win.transient(self); win.grab_set()
        win.minsize(640, 360)

        tk.Label(win, text="Manage Speakers", font=FH,
                 bg=BG, fg=TEXT, padx=24, pady=14).pack(anchor="w")
        tk.Label(win,
                 text=("Rename any speaker.  Changes apply to every session\n"
                       "in this project that references the same source."),
                 font=FB, bg=BG, fg=SUB, padx=24, justify="left"
                 ).pack(anchor="w", pady=(0, 12))

        # Table: raw-tag (read-only) + current label (editable)
        body = tk.Frame(win, bg=SURF,
                        highlightbackground=BORDER, highlightthickness=1)
        body.pack(fill="both", expand=True, padx=24, pady=(0, 12))

        hdr = tk.Frame(body, bg=SURF3)
        hdr.pack(fill="x")
        tk.Label(hdr, text="SOURCE", font=FL, bg=SURF3, fg=SUB,
                 anchor="w", padx=12, pady=6, width=44).pack(side="left")
        tk.Label(hdr, text="DISPLAY LABEL", font=FL, bg=SURF3, fg=SUB,
                 anchor="w", padx=12, pady=6).pack(side="left")
        tk.Label(hdr, text="USED IN", font=FL, bg=SURF3, fg=SUB,
                 anchor="e", padx=12, pady=6).pack(side="right")

        entries = {}   # raw → StringVar
        for raw in sorted(raw_to_sessions.keys()):
            row = tk.Frame(body, bg=SURF)
            row.pack(fill="x")
            tk.Label(row, text=raw, font=FB, bg=SURF, fg=SUB,
                     anchor="w", padx=12, pady=6, width=44
                     ).pack(side="left")
            var = tk.StringVar(value=raw_to_label.get(raw, raw))
            entries[raw] = var
            entry = tk.Entry(row, textvariable=var, font=FB,
                              bg=SURF2, fg=TEXT, insertbackground=TEXT,
                              relief="flat", bd=4, width=32)
            entry.pack(side="left", padx=(0, 8), ipady=2)
            n_sessions = len(raw_to_sessions[raw])
            tk.Label(row,
                     text="{} session{}".format(
                         n_sessions, "s" if n_sessions != 1 else ""),
                     font=FB, bg=SURF, fg=SUB,
                     anchor="e", padx=12, pady=6
                     ).pack(side="right")

        nav = tk.Frame(win, bg=BG)
        nav.pack(fill="x", padx=24, pady=(0, 16))

        def _save():
            renamed = 0
            for raw, var in entries.items():
                new_label = var.get().strip()
                if not new_label:
                    continue
                if new_label == raw_to_label.get(raw):
                    continue
                # Apply across every session that references this raw tag
                for sess in raw_to_sessions.get(raw, []):
                    speakers_map = dict(sess.get("speakers") or {})
                    speakers_map[raw] = new_label
                    # Also key by basename in case the renderer falls
                    # back to that lookup form.
                    speakers_map[os.path.basename(raw)] = new_label
                    sess["speakers"] = speakers_map
                    self._pq_save_session_file(sess)
                renamed += 1
            win.destroy()
            # Re-render whatever view we're in so the new labels show.
            # Preserve cursor / selection / scroll — only speaker
            # labels changed; the user's reading position should not.
            if getattr(self, "_pq_current_session", None) is not None:
                _rn_state = self._pq_capture_text_state()
                self._pq_render_transcript_text(self._pq_current_session)
                self._pq_restore_text_state(_rn_state)
            else:
                self._pq_render_project_view()
            messagebox.showinfo("Speakers updated",
                "Renamed {} speaker label{}.".format(
                    renamed, "s" if renamed != 1 else ""))

        _W = 8
        self._btn(nav, "SAVE", _save,
                  color=ACCENT, width=_W).pack(side="right")
        self._btn(nav, "CANCEL", win.destroy, width=_W
                  ).pack(side="right", padx=(0, 8))

        win.update_idletasks()
        pw = self.winfo_width(); ph = self.winfo_height()
        px = self.winfo_rootx(); py = self.winfo_rooty()
        ww = win.winfo_width();  wh = win.winfo_height()
        win.geometry("+{}+{}".format(
            px + max(0, (pw - ww) // 2),
            py + max(0, (ph - wh) // 2)))
        self.wait_window(win)

    def _pq_inline_rename_speaker(self, event, tx):
        """Double-click on a speaker label → rename it everywhere in
        the current Episode Project.  Same effect as the MANAGE
        SPEAKERS dialog, but one-click for the visible label."""
        # Find the raw speaker tag at the click position
        pos = tx.index("@{},{}".format(event.x, event.y))
        raw = None
        for tname in tx.tag_names(pos):
            if tname.startswith("speaker_raw:"):
                raw = tname.split(":", 1)[1]
                break
        if not raw:
            return "break"

        project = getattr(self, "_pq_project", None)
        session = getattr(self, "_pq_current_session", None)
        if not project or not session:
            return "break"

        # Resolve current display label
        sess_speakers = session.get("speakers") or {}
        cur_label = (sess_speakers.get(raw)
                     or sess_speakers.get(os.path.basename(raw))
                     or self._pq_format_speaker(raw))

        # Inline dialog
        win = tk.Toplevel(self)
        win.title("Rename Speaker")
        win.configure(bg=BG)
        win.transient(self); win.grab_set()
        win.resizable(False, False)
        tk.Label(win, text="Rename speaker", font=FH,
                 bg=BG, fg=TEXT, padx=20, pady=14).pack(anchor="w")
        tk.Label(win, text="Source: {}".format(raw), font=FB,
                 bg=BG, fg=SUB, padx=20).pack(anchor="w")
        tk.Label(win,
                 text="(Renaming applies to every session in this "
                      "project that references the same source.)",
                 font=FB, bg=BG, fg=SUB, padx=20, wraplength=420,
                 justify="left").pack(anchor="w", pady=(0, 8))

        var = tk.StringVar(value=cur_label)
        ent = tk.Entry(win, textvariable=var, font=FB,
                        bg=SURF2, fg=TEXT, insertbackground=TEXT,
                        relief="flat", bd=4, width=32)
        ent.pack(padx=20, ipady=2)
        ent.select_range(0, "end")
        ent.focus_set()

        nav = tk.Frame(win, bg=BG)
        nav.pack(fill="x", padx=20, pady=14)

        def _apply():
            new = var.get().strip()
            if not new or new == cur_label:
                win.destroy(); return
            n_changed = 0
            for s in project["sessions"]:
                # Apply only if this session has any word with this raw tag
                touches = any(
                    w.get("speaker") == raw
                    for w in (s.get("transcript") or []))
                if not touches:
                    continue
                m = dict(s.get("speakers") or {})
                m[raw] = new
                m[os.path.basename(raw)] = new
                s["speakers"] = m
                self._pq_save_session_file(s)
                n_changed += 1
            win.destroy()
            # Re-render the open session view.  Preserve cursor /
            # selection / scroll — only the speaker label changed.
            _ir_state = self._pq_capture_text_state()
            self._pq_render_transcript_text(session)
            self._pq_restore_text_state(_ir_state)

        _W = 8
        self._btn(nav, "RENAME", _apply,
                  color=ACCENT, width=_W).pack(side="right")
        self._btn(nav, "CANCEL", win.destroy, width=_W
                  ).pack(side="right", padx=(0, 8))
        win.bind("<Return>", lambda e: _apply())
        win.bind("<Escape>", lambda e: win.destroy())

        win.update_idletasks()
        pw = self.winfo_width(); ph = self.winfo_height()
        px = self.winfo_rootx(); py = self.winfo_rooty()
        ww = win.winfo_width();  wh = win.winfo_height()
        win.geometry("+{}+{}".format(
            px + max(0, (pw - ww) // 2),
            py + max(0, (ph - wh) // 2)))
        self.wait_window(win)
        return "break"

    def _pq_filter_filler_interjections(self, words):
        """Drop cross-track filler interjections.

        A "filler turn" is a short run (≤ FILLER_MAX_WORDS) of one
        speaker's words that's sandwiched between same-speaker turns of
        a *different* speaker, AND the run consists only of common
        backchannel words ("yeah", "mm-hmm", "right", etc.).  These
        almost always come from cross-mic bleed or listener filler that
        adds noise to the read.  Removed at render time only — the raw
        transcript on disk is unchanged so the user can adjust the
        threshold or disable filtering without re-transcribing.

        Configurable knobs:
          self._pq_filler_words      → set of lowercase backchannels
          self._pq_filler_max_words  → max length for a candidate run
        """
        DEFAULT_FILLERS = {
            # Affirmative / negative backchannels
            "yeah", "yep", "yup", "yes", "no", "nope",
            # Approval / agreement
            "right", "okay", "ok", "sure", "totally", "exactly",
            "absolutely", "true", "definitely", "certainly",
            # Vocalisations / hesitations
            "mm", "mmm", "mhm", "mhmm", "mmhmm", "hmm", "hm",
            "uhhuh", "uhuh", "huh", "wow", "oh", "ah", "uh", "um",
            "ugh", "er", "eh", "oof",
            # Single-word filler
            "i", "well", "so", "like",
        }
        fillers = set(getattr(self, "_pq_filler_words", DEFAULT_FILLERS))
        max_w   = int(getattr(self, "_pq_filler_max_words", 3))

        if not words:
            return words

        # Group into consecutive same-speaker runs
        runs = []
        cur_speaker = object()
        cur_words   = []
        for w in words:
            sp = w.get("speaker")
            if sp != cur_speaker:
                if cur_words:
                    runs.append((cur_speaker, cur_words))
                cur_speaker = sp
                cur_words   = []
            cur_words.append(w)
        if cur_words:
            runs.append((cur_speaker, cur_words))

        def _normalise(wt):
            """Lowercase + strip ALL non-alpha (including hyphens like
            in '-hmm.', dashes, periods).  Apostrophes preserved for
            'i'm' / 'don't' style fillers."""
            return re.sub(r"[^a-z']", "", (wt or "").lower())

        def _is_filler_run(run_words):
            if len(run_words) > max_w:
                return False
            saw_text = False
            for w in run_words:
                clean = _normalise(w.get("word") or "")
                if not clean:
                    continue
                saw_text = True
                # Accept hyphenated compounds like "uh-huh" / "mm-hmm"
                # by also testing each piece against the filler set.
                if clean in fillers:
                    continue
                # Try splitting on internal hyphens that survived stripping
                # (shouldn't happen post-_normalise, but defensive).
                parts = [p for p in clean.split("-") if p]
                if parts and all(p in fillers for p in parts):
                    continue
                return False
            return saw_text

        keep = [True] * len(runs)
        for i in range(1, len(runs) - 1):
            sp, rw = runs[i]
            prev_sp = runs[i - 1][0]
            next_sp = runs[i + 1][0]
            # Drop only if the run is sandwiched between same-other-speaker
            # turns AND every word is a filler.
            if (prev_sp == next_sp and prev_sp != sp
                    and prev_sp is not None
                    and _is_filler_run(rw)):
                keep[i] = False

        out = []
        for kept, (_, rw) in zip(keep, runs):
            if kept:
                out.extend(rw)
        return out

    def _pq_format_speaker(self, raw):
        """Tidy a raw speaker tag (filename basename) into a display
        label.  Strips known boilerplate and uppercases."""
        if not raw:
            return ""
        s = str(raw)
        s = re.sub(r"\.[a-zA-Z0-9]+$", "", s)        # ext if any
        s = re.sub(r"^riverside[_\- ]+", "", s, flags=re.I)
        s = re.sub(r"raw[\-_ ]?audio[\-_ ]?", "", s, flags=re.I)
        s = re.sub(r"raw[\-_ ]?video[\-_ ]?", "", s, flags=re.I)
        s = re.sub(r"_blood[_\- ]trails[_\- ]\d+", "", s, flags=re.I)
        s = re.sub(r"[_\-]+", " ", s).strip()
        return s.upper() if s else "?"

    def _pq_detect_paragraph_breaks(self, text):
        """Syntactic paragraph-break detection on already-rendered text.

        Returns a sorted list of character positions in `text`.  At each
        position the renderer should insert a "\\n\\n" — i.e. break the
        paragraph BEFORE the character at that position.

        Heuristics, in order of precedence (any one triggers a break):
          • The sentence we just finished ended with "?"
                — questions in interview prose almost always sit at a
                  paragraph boundary (interviewer asks → interviewee
                  answers, or vice versa).
          • The next sentence starts with a discourse marker
                — "So,", "Well,", "Now,", "Okay,", "Right,", "Anyway,"
                  are reliable paragraph openers in spoken interviews.
          • Length cap: after `SENTENCES_PER_PARA` consecutive sentences
            without any other trigger, force a break so we never end up
            with one runaway paragraph.

        Knobs are exposed as instance attrs (`_pq_sentences_per_para`,
        `_pq_discourse_starters`) so they can be tweaked from a Python
        REPL launched with `python -i` without re-transcribing.
        """
        if not text:
            return []

        SENTENCES_PER_PARA = int(getattr(self, "_pq_sentences_per_para", 4))
        MIN_SENTENCES_BEFORE_BREAK = int(
            getattr(self, "_pq_min_sentences_before_break", 2))
        # Hard char fallback: if we ever go this far without any break,
        # force one at the next sentence boundary regardless of triggers.
        # Catches the "last third runs on" failure mode where Whisper's
        # capitalisation drifts and our other signals stop firing.
        MAX_CHARS_PER_PARA = int(
            getattr(self, "_pq_max_chars_per_para", 700))
        # Only the strong topic-shifters.  "right", "yeah", "alright"
        # were too common as acknowledgments and produced 1-sentence
        # filler paragraphs.
        DEFAULT_STARTERS = {
            "so", "well", "now", "okay", "anyway",
        }
        starters = set(getattr(self, "_pq_discourse_starters",
                                DEFAULT_STARTERS))

        positions = []
        n = len(text)
        i = 0
        sentences_since_break = 0
        last_break_pos = 0
        while i < n:
            ch = text[i]
            if ch in ".?!":
                # Walk past any trailing punctuation cluster (e.g. "?!")
                j = i + 1
                while j < n and text[j] in ".?!":
                    j += 1
                # Skip whitespace
                k = j
                while k < n and text[k].isspace():
                    k += 1
                # Sentence boundary if next char is alphabetic (any case).
                # Relaxed from upper-only because Whisper's capitalisation
                # gets unreliable in long transcripts and we'd otherwise
                # stop detecting boundaries entirely.
                if k < n and text[k].isalpha():
                    sentences_since_break += 1
                    # Read the next leading word(s) (up to 2)
                    m = k
                    while m < n and (text[m].isalpha() or text[m] == "'"):
                        m += 1
                    first_word = text[k:m].lower()
                    # Optional second word for compound starters
                    p = m
                    while p < n and text[p].isspace():
                        p += 1
                    q = p
                    while q < n and (text[q].isalpha() or text[q] == "'"):
                        q += 1
                    second_word = text[p:q].lower()
                    two_word = (first_word + " " + second_word).strip()

                    was_question  = (ch == "?")
                    is_starter    = (first_word in starters
                                     or two_word in starters)
                    para_full     = sentences_since_break >= SENTENCES_PER_PARA
                    long_enough   = sentences_since_break >= MIN_SENTENCES_BEFORE_BREAK
                    char_overflow = (k - last_break_pos) > MAX_CHARS_PER_PARA

                    # `para_full` and `char_overflow` are unconditional
                    # caps (length and character).  Question/discourse
                    # breaks only fire if the paragraph we'd be closing
                    # has at least MIN_SENTENCES_BEFORE_BREAK sentences
                    # — avoids fragmenting prose into 1-sentence filler.
                    if (para_full or char_overflow
                        or ((was_question or is_starter) and long_enough)):
                        positions.append(k)
                        sentences_since_break = 0
                        last_break_pos = k
                    i = k
                    continue
            i += 1

        # Final safety net: if any span between consecutive breaks is
        # still > 2× MAX_CHARS (because no sentence boundaries were
        # detected at all in that region), force breaks at the nearest
        # whitespace to the midpoint.  This handles transcripts where
        # Whisper produced a long stretch with no punctuation.
        TOO_LONG = 2 * MAX_CHARS_PER_PARA
        boundaries = [0] + positions + [n]
        forced = []
        for idx in range(len(boundaries) - 1):
            start = boundaries[idx]
            end   = boundaries[idx + 1]
            span  = end - start
            if span <= TOO_LONG:
                continue
            # Bisect: insert breaks every MAX_CHARS_PER_PARA at nearest spaces
            cur = start + MAX_CHARS_PER_PARA
            while cur < end - MAX_CHARS_PER_PARA // 2:
                bp = cur
                # Find nearest whitespace within ±200 chars
                for off in range(200):
                    if cur - off > start and text[cur - off].isspace():
                        bp = cur - off + 1
                        break
                    if cur + off < end and text[cur + off].isspace():
                        bp = cur + off + 1
                        break
                forced.append(bp)
                cur = bp + MAX_CHARS_PER_PARA
        if forced:
            positions = sorted(set(positions + forced))

        return positions

    def _pq_update_status_lbl(self, session):
        lbl = getattr(self, "_pq_status_lbl", None)
        if not lbl:
            return
        n_words = len(session.get("transcript") or [])
        if not n_words:
            lbl.config(text="not transcribed", fg=WARN)
            return
        last  = session["transcript"][-1]
        dur_s = float(last.get("end", 0))
        h = int(dur_s // 3600)
        m = int((dur_s % 3600) // 60)
        s = int(dur_s % 60)
        lbl.config(text="{:,} words · {:02d}:{:02d}:{:02d}".format(
                       n_words, h, m, s), fg=SUB)

    def _pq_remove_session(self, session):
        if not messagebox.askyesno(
                "Remove from project",
                "Remove '{}' from this Episode Project?\n\n"
                "The session JSON on disk will not be deleted.".format(
                    session.get("token", "?"))):
            return
        self._pq_project["sessions"] = [
            s for s in self._pq_project["sessions"] if s is not session]
        self._pq_render_project_view()

    def _pq_add_media_to_session(self, session):
        files = filedialog.askopenfilenames(
            title="Add media to session",
            filetypes=[("Media",
                        "*.wav *.aif *.aiff *.bwf *.mp3 *.m4a *.flac "
                        "*.mp4 *.mov *.mxf *.mkv *.avi *.m4v"),
                       ("All", "*.*")])
        added_paths = []
        media = list(session.get("media", []))
        for f in files:
            f = os.path.abspath(f)
            if f not in media and is_media(f):
                media.append(f)
                added_paths.append(f)
        if not added_paths:
            return
        session["media"] = media

        # ── Sidecar-adoption path ──────────────────────────────────────
        # Before wiping session["transcript"], probe each newly-added
        # file for a .pb_transcript.json sidecar.  Previously we always
        # cleared the transcript and required the user to click ⟳
        # TRANSCRIBE just to reach the USE EXISTING prompt — an obvious
        # dead-end when they just wanted to search a transcript that
        # already exists.  Now: if any added file has a valid sidecar,
        # route through the same _pq_confirm_retranscribe dialog that
        # the TRANSCRIBE-button flow uses, so USE EXISTING / RE-TRANSCRIBE
        # is offered inline.
        sidecar_paths = []
        for p in added_paths:
            try:
                if engines.pb_transcript_load(p)[0] is not None:
                    sidecar_paths.append(p)
            except Exception:
                pass

        has_session_transcript = bool(session.get("transcript"))
        if sidecar_paths or has_session_transcript:
            choice, preserve_edits = self._pq_confirm_retranscribe(
                session, has_session_transcript, sidecar_paths)
            if choice == "cancel":
                # Roll back the media add so the session stays as it was.
                session["media"] = [p for p in media if p not in added_paths]
                return
            if choice == "use_cached" and sidecar_paths:
                # Load the first sidecar's words directly into the session
                # — mirrors _pq_run_transcription's USE EXISTING handler.
                words, _ = engines.pb_transcript_load(sidecar_paths[0])
                if words:
                    session["transcript"] = list(words)
                    self._pq_save_session_file(session)
                    self._pq_render_media_list(session)
                    self._pq_render_transcript_text(session)
                    if getattr(self, "_pq_retx_btn", None):
                        self._pq_retx_btn.config(text="⟳ RE-TRANSCRIBE")
                    return
            # Fall through: user chose RE-TRANSCRIBE (or use_cached had
            # no words) — proceed with the existing wipe + re-render
            # flow AND kick off the transcription immediately so they
            # don't have to click TRANSCRIBE separately.
            session["transcript"] = []
            self._pq_save_session_file(session)
            self._pq_render_media_list(session)
            self._pq_render_transcript_text(session)
            if getattr(self, "_pq_retx_btn", None):
                self._pq_retx_btn.config(text="⟳ TRANSCRIBE")
            self.after(80,
                       lambda s=session: self._pq_run_transcription(s))
            return

        # No sidecar and no existing transcript — original behaviour:
        # wipe + re-render, wait for the user to click TRANSCRIBE.
        session["transcript"] = []
        self._pq_save_session_file(session)
        self._pq_render_media_list(session)
        self._pq_render_transcript_text(session)
        if getattr(self, "_pq_retx_btn", None):
            self._pq_retx_btn.config(text="⟳ TRANSCRIBE")

    def _pq_confirm_retranscribe(self, session, has_session_transcript,
                                  sidecar_paths):
        """Ask the user what to do when transcript data already exists.

        Returns a tuple (choice, preserve_edits):
            choice         — "transcribe" / "use_cached" / "cancel"
            preserve_edits — bool; when True, any prior user edits
                             (in-memory OR in sidecar) are merged into
                             the new transcription via the per-word
                             _src='user' mechanism.  When False, edits
                             are discarded before the run starts.
                             Always False when no edits exist.
        """
        # Count user edits from both sources.  In-memory session wins
        # if both have content — that's the live state.  Otherwise
        # peek at the most-recent sidecar so a fresh-session
        # re-transcribe surfaces edits that aren't loaded yet.
        session_edits = 0
        if has_session_transcript:
            session_edits = len(
                [w for w in session.get("transcript", [])
                 if w.get("_src") == "user"])

        sidecar_edits = 0
        sidecar_edit_src = None
        if sidecar_paths and not has_session_transcript:
            for sp in sidecar_paths:
                try:
                    words, _ = engines.pb_transcript_load(sp)
                    if words:
                        n = sum(1 for w in words
                                if w.get("_src") == "user")
                        if n:
                            sidecar_edits = n
                            sidecar_edit_src = sp
                            break
                except Exception:
                    pass

        total_edits = session_edits + sidecar_edits

        win = tk.Toplevel(self)
        win.title("Transcript Already Exists")
        win.configure(bg=BG)
        win.transient(self); win.grab_set()
        win.resizable(False, False)

        hdr = tk.Frame(win, bg=WARN, padx=16, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="⚠   Transcript Already Exists",
                 font=FBT, bg=WARN, fg="#1c1c1c").pack(anchor="w")

        body = tk.Frame(win, bg=BG, padx=20, pady=14)
        body.pack(fill="both", expand=True)

        lines = []
        if has_session_transcript:
            wc = len([w for w in session.get("transcript", [])
                      if (w.get("word") or "").strip()])
            lines.append(
                "Session already has a {:,}-word transcript.".format(wc))
        if sidecar_paths:
            n = len(sidecar_paths)
            extra = ""
            if sidecar_edits:
                extra = " (with {} user edit{})".format(
                    sidecar_edits,
                    "s" if sidecar_edits != 1 else "")
            lines.append(
                "{} cached transcript{} on disk{}.".format(
                    n, "s" if n != 1 else "", extra))

        tk.Label(body, text="\n".join(lines), font=FB,
                 bg=BG, fg=TEXT, justify="left",
                 wraplength=520).pack(anchor="w", pady=(0, 8))

        # Preserve-edits checkbox.  Shown only when edits exist
        # somewhere (in-memory session OR in the sidecar that would
        # otherwise be ignored on a fresh-session re-transcribe).
        # Default ON — losing user work silently is the worst default.
        preserve_var = tk.BooleanVar(value=True)
        if total_edits:
            edit_total = max(session_edits, sidecar_edits)
            cb_text = ("Preserve {} edited word{} when "
                       "re-transcribing").format(
                edit_total, "s" if edit_total != 1 else "")
            cb = tk.Checkbutton(
                body, text=cb_text, variable=preserve_var,
                font=FB, bg=BG, fg=TEXT, selectcolor=SURF2,
                activebackground=BG, activeforeground=TEXT,
                highlightthickness=0, bd=0)
            cb.pack(anchor="w", pady=(4, 10))

        result = {"choice": "cancel"}
        # Uniform button sizing — all three buttons same width and height
        # so they align cleanly along the bottom row.
        nav = tk.Frame(win, bg=BG)
        nav.pack(fill="x", padx=20, pady=(0, 14))

        def _pick(c):
            result["choice"] = c
            win.destroy()

        # All buttons pack right-aligned in one tight group with uniform
        # 8 px gaps.  Packing in reverse order so the visual sequence
        # reads CANCEL  RE-TRANSCRIBE  USE EXISTING from left to right.
        _W = 13
        if sidecar_paths:
            self._btn(nav, "USE EXISTING",
                      lambda: _pick("use_cached"),
                      color=ACCENT, width=_W
                      ).pack(side="right")
            self._btn(nav, "RE-TRANSCRIBE",
                      lambda: _pick("transcribe"),
                      width=_W).pack(side="right", padx=(0, 8))
        else:
            self._btn(nav, "RE-TRANSCRIBE",
                      lambda: _pick("transcribe"),
                      color=ACCENT, width=_W
                      ).pack(side="right")
        self._btn(nav, "CANCEL", lambda: _pick("cancel"),
                  width=_W).pack(side="right", padx=(0, 8))

        win.bind("<Escape>", lambda e: _pick("cancel"))

        self._center_dialog(win, 580, 240)
        self.wait_window(win)

        # Re-transcribe ALWAYS visually wipes — Jordan explicitly
        # wanted a clean slate so the draft pass appears against an
        # empty pane rather than fighting stale content.  Two modes:
        #
        #   preserve=True   → keep only the user-edited words; their
        #                     timestamps survive and the worker's
        #                     merge re-inserts them at the right
        #                     positions as the draft / configured
        #                     passes stream in.
        #   preserve=False  → full wipe; nothing carries over.
        #
        # When the only source of edits is a sidecar (new-session
        # re-transcribe), hoist its user-tagged words onto the
        # session first so they participate in the merge.
        preserve = bool(preserve_var.get()) and bool(total_edits)
        if result["choice"] == "transcribe":
            if preserve:
                if has_session_transcript:
                    session["transcript"] = [
                        w for w in session["transcript"]
                        if w.get("_src") == "user"]
                elif sidecar_edits and sidecar_edit_src:
                    try:
                        words, _ = engines.pb_transcript_load(
                            sidecar_edit_src)
                        session["transcript"] = [
                            w for w in (words or [])
                            if w.get("_src") == "user"]
                    except Exception:
                        session["transcript"] = []
                else:
                    session["transcript"] = []
            else:
                session["transcript"] = []
            self._pq_save_session_file(session)

        return result["choice"], preserve

    def _pq_preview_ready(self, session):
        """Called on the main thread when the tiny draft pass completes
        for a session.  Renders the preview transcript and flips the
        status label to 'preview · refining…' so the user knows the
        configured model is still working in the background."""
        if getattr(self, "_pq_current_session", None) is session:
            # Preserve cursor / selection / scroll across this render
            # — without it the user gets bounced to the top of the
            # transcript every time tiny finishes.
            state = self._pq_capture_text_state()
            try:
                self._pq_render_transcript_text(session)
            except Exception:
                pass
            else:
                self._pq_restore_text_state(state)
            lbl = getattr(self, "_pq_status_lbl", None)
            if lbl:
                try:
                    lbl.config(text="preview · refining…", fg=WARN)
                except tk.TclError:
                    pass

    @staticmethod
    def _pq_merge_streams(tiny_words, main_words, current_words):
        """Live merge of two streaming transcript sources plus the
        user-edited words already in the current session transcript.

        Priority:  user > main (configured) > tiny (draft) > backdrop

        Algorithm:
          1. User words from current_words are sacred.  Their time
             spans are computed and every other source's words in those
             spans are dropped.
          2. Main's coverage is tracked via its 'frontier' — the latest
             end-time of any main word emitted so far.  Words before
             the frontier are 'main has decoded this'; words after it
             are 'main hasn't reached yet'.
          3. Tiny words AFTER the main frontier are kept; BEFORE it
             they're dropped (main has them).
          4. BACKDROP — current_words' non-user, non-tiny content (e.g.
             pre-existing transcript on a re-transcribe).  Fills any
             timestamp range still uncovered by main+tiny.  Without
             this, every tick of a re-transcribe (where tiny doesn't
             run) silently deleted everything past main's frontier
             until main caught up.
          5. All surviving words are sorted by start time; duplicates
             at the same timestamp (within a small epsilon) are
             deduped, keeping the higher-priority source.

        Result: the live transcript starts as the existing transcript
        / tiny draft, then progressively gets replaced by main as main
        catches up, with user edits anchored throughout.
        """
        user_words = [w for w in (current_words or [])
                      if w.get("_src") == "user"]
        # IMPORTANT: pass the FULL current_words list, not the filtered
        # user-only list.  _pq_user_edited_spans uses non-user words as
        # run-break markers — feeding it the filtered list collapses
        # every scattered user edit into one giant span from the first
        # edit to the last, which then drops every other word in that
        # range.  (Hit this bug in stream diagnostics: 4 scattered
        # edits caused merged=388 instead of ~1700.)
        user_spans = App._pq_user_edited_spans(current_words)

        def _in_user(t):
            return any(s <= t <= e for (s, e) in user_spans)

        # Main's coverage frontier — latest end time of any main word.
        main_frontier = 0.0
        for w in main_words or []:
            if w.get("break"):
                continue
            we = float(w.get("end", 0.0))
            if we > main_frontier:
                main_frontier = we

        # Tiny's coverage envelope, used to know whether the backdrop
        # should fill a range or defer to tiny.  An empty tiny buffer
        # means tiny isn't running this pass (re-transcribe) so the
        # backdrop should fill everything past main's frontier.
        tiny_start = float("inf")
        tiny_end   = 0.0
        for w in tiny_words or []:
            if w.get("break"):
                continue
            ws = float(w.get("start", 0.0))
            we = float(w.get("end",   ws))
            if ws < tiny_start: tiny_start = ws
            if we > tiny_end:   tiny_end   = we
        has_tiny = (tiny_end > 0.0)

        result = []

        # 1) main words (highest priority)
        for w in main_words or []:
            if w.get("break"):
                t = float(w.get("start", 0.0))
                if not _in_user(t):
                    result.append(w)
                continue
            mid = (float(w.get("start", 0.0))
                   + float(w.get("end", 0.0))) / 2.0
            if not _in_user(mid):
                result.append(w)

        # 2) tiny words past the main frontier
        for w in tiny_words or []:
            if w.get("break"):
                t = float(w.get("start", 0.0))
                if _in_user(t) or t <= main_frontier:
                    continue
                result.append(w)
                continue
            mid = (float(w.get("start", 0.0))
                   + float(w.get("end", 0.0))) / 2.0
            if _in_user(mid) or mid <= main_frontier:
                continue
            result.append(w)

        # 3) backdrop — pre-existing non-user words from current_words.
        #    Skip anything inside a user span, behind main's frontier,
        #    OR (when tiny is active) inside tiny's coverage envelope
        #    — tiny is fresher than the previous render's leftovers.
        for w in (current_words or []):
            src = w.get("_src")
            if src == "user":
                continue   # handled separately
            if w.get("break"):
                t = float(w.get("start", 0.0))
                if _in_user(t) or t <= main_frontier:
                    continue
                if has_tiny and tiny_start <= t <= tiny_end:
                    continue
                result.append(w)
                continue
            ws = float(w.get("start", 0.0))
            we = float(w.get("end",   ws))
            mid = (ws + we) / 2.0
            if _in_user(mid) or mid <= main_frontier:
                continue
            if has_tiny and tiny_start <= mid <= tiny_end:
                continue
            result.append(w)

        # 4) user words — ALL of them, always.  Filtering them by
        # the streaming frontier here would drop edits made during
        # streaming whose audio time hadn't been reached yet, and
        # the next tick wouldn't see them in current_words to put
        # them back.  The "hide until streaming reaches them" visual
        # behaviour is applied at render time instead.
        result.extend(user_words)

        result.sort(key=lambda w: float(w.get("start", 0.0)))
        return result

    @staticmethod
    def _pq_user_edited_spans(words):
        """Walk a transcript and return a list of (start_s, end_s) spans
        covering every contiguous run of words with _src=='user'.  Used
        by the refine-merge logic to keep user edits intact when a
        higher-quality transcription pass overwrites the rest.

        Run-break heuristic: a run ends when EITHER a non-user word
        appears OR the audio-time gap to the next user word exceeds
        GAP_S seconds.  The time-gap check matters during streaming
        re-transcribe when the transcript momentarily contains nothing
        BUT scattered user edits — without it, three edits at 50 / 200
        / 500 s would merge into one (50, 500) span and the merge
        would drop everything inside that range."""
        GAP_S = 2.0
        spans = []
        run = None
        prev_end = None
        for w in words or []:
            if w.get("break"):
                continue
            if w.get("_src") == "user":
                ws = float(w.get("start", 0.0))
                we = float(w.get("end",   ws))
                if run is None:
                    run = [ws, we]
                elif prev_end is not None and (ws - prev_end) > GAP_S:
                    spans.append(tuple(run))
                    run = [ws, we]
                else:
                    run[1] = max(run[1], we)
                prev_end = we
            else:
                if run is not None:
                    spans.append(tuple(run))
                    run = None
                    prev_end = None
        if run is not None:
            spans.append(tuple(run))
        return spans

    @staticmethod
    def _pq_merge_refine(current_words, refine_words):
        """Merge a refined (higher-quality) transcription into the current
        transcript, preserving every word in user-edited time spans.

        Algorithm:
          1. Identify user-edited spans in current_words (_src=='user')
          2. Drop refine_words that fall inside any user span
          3. Splice the surviving user words back into the result at
             their original timestamps
          4. Sort everything by start time

        Returns a new word list — caller assigns to session['transcript']."""
        user_spans = App._pq_user_edited_spans(current_words)

        def _in_user_span(t):
            return any(s <= t <= e for (s, e) in user_spans)

        # Refine words that DON'T overlap user-edited regions
        keep = []
        for w in refine_words or []:
            if w.get("break"):
                # Drop refine breaks too if they fall in a user span;
                # the user's words own that region of audio.
                t = float(w.get("start", 0.0))
                if _in_user_span(t):
                    continue
                keep.append(w)
                continue
            ws = float(w.get("start", 0.0))
            we = float(w.get("end",   ws))
            mid = (ws + we) / 2.0
            if _in_user_span(mid):
                continue
            keep.append(w)

        # User words to splice in
        user_words = [
            w for w in (current_words or [])
            if w.get("_src") == "user"
        ]
        merged = keep + user_words
        merged.sort(key=lambda w: float(w.get("start", 0.0)))
        return merged

    def _pq_run_transcription(self, session):
        """Kick off (or refuse to start) a transcription for `session`.

        State lives on the session dict (`session["_progress"]`) so multiple
        sessions can be transcribing at once — start one, navigate back to
        the project, start the next, etc.  The animation timer reads from
        the *currently visible* session's progress dict.
        """
        # Refuse to start a duplicate run for the same session.
        if session.get("_progress", {}).get("active"):
            return

        media = list(session.get("media", []))
        if not media:
            messagebox.showwarning("No media",
                "Add at least one media file before transcribing.")
            return
        missing = [p for p in media if not os.path.isfile(p)]
        if missing:
            messagebox.showerror("Missing files",
                "These files no longer exist:\n\n" +
                "\n".join("  • " + p for p in missing))
            return

        if not HAS_WHISPER:
            messagebox.showerror("Not installed",
                "faster-whisper is not installed.\n\n"
                "Run:  pip install faster-whisper")
            return

        # Memory pre-flight — same Windows commit-charge check used by
        # Script→Session reconcile.  Catches the case where 15 GB of
        # physical RAM is free but the page file is exhausted and a
        # 300 MB feature buffer allocation will fail.
        if not self._preflight_memory_check():
            return

        # Pre-transcribe alert: existing transcript data on this session OR
        # a .pb_transcript.json sidecar next to any of the media files means
        # the user may not realise they're about to throw work away.  Ask
        # them to confirm overwrite, with a "use cached" option to pull
        # the sidecar transcript into this session without re-running
        # Whisper.
        has_session_transcript = bool(session.get("transcript"))
        sidecar_paths = []
        for p in media:
            try:
                if engines.pb_transcript_load(p)[0] is not None:
                    sidecar_paths.append(p)
            except Exception:
                pass

        if has_session_transcript or sidecar_paths:
            choice, preserve_edits = self._pq_confirm_retranscribe(
                session, has_session_transcript, sidecar_paths)
            if choice == "cancel":
                return
            if choice == "use_cached":
                # Pull the most-recent sidecar transcript onto the session
                # and skip the Whisper run entirely.
                src = sidecar_paths[0] if sidecar_paths else None
                if src:
                    words, _ = engines.pb_transcript_load(src)
                    if words:
                        session["transcript"] = list(words)
                        self._pq_save_session_file(session)
                        if (getattr(self, "_pq_current_session", None)
                                is session):
                            self._pq_render_transcript_text(session)
                        return
            # choice == "transcribe": the dialog has already wiped
            # session["transcript"] and (when preserve was checked)
            # hoisted just the user-edited words back onto it, so the
            # worker starts from a clean slate either way.  Force a
            # re-render here so the visual wipe lands immediately —
            # otherwise stale text lingers until the first stream tick.
            # user_frontier=0 hides any preserved edits at this stage
            # so the pane looks fully empty; streaming will reveal
            # each edit when it arrives at the edit's timestamp.
            if getattr(self, "_pq_current_session", None) is session:
                try:
                    self._pq_render_transcript_text(
                        session, user_frontier=0.0)
                except Exception:
                    pass

        bg_mode = bool(getattr(self, "_pq_bg_mode_var",
                               tk.BooleanVar(value=False)).get())

        # Per-session progress state — concurrent-safe: each session has its
        # own dict, the animation timer reads only the current session's.
        session["_progress"] = {
            "phase":      "starting",
            "extra":      "",
            "active":     True,
            "elapsed":    0,
            "started":    time.perf_counter(),
            "total_secs": 0.0,   # filled in by the worker once it probes
        }

        # Update UI for THIS session only if it's the one on screen.
        is_current = (getattr(self, "_pq_current_session", None) is session)
        if is_current:
            btn = getattr(self, "_pq_retx_btn", None)
            lbl = getattr(self, "_pq_status_lbl", None)
            if btn: btn.config(text="TRANSCRIBING…", fg=SUB)
            if lbl: lbl.config(text="starting…", fg=ACCENT)
            self._pq_render_progress_placeholder()
            self._pq_animate_progress()

        # Background-mode → lower process priority for the duration.
        # Track per-thread so concurrent runs don't fight over priority.
        prev_priority = self._pq_apply_priority(low=bg_mode)

        def _set_phase(phase, extra=""):
            prog = session.get("_progress")
            if not prog or not prog.get("active"):
                return
            prog["phase"] = phase
            prog["extra"] = extra

        def _fmt_dur(secs):
            secs = max(0, int(secs))
            h, r = divmod(secs, 3600)
            m, s = divmod(r, 60)
            return "{:02d}:{:02d}:{:02d}".format(h, m, s)

        # Cancel-event handle the worker passes into the engine.  Set
        # by _request_cancel_active_transcriptions when the user picks
        # "Cancel & apply now" in the model-swap dialog.  Stored on the
        # session dict so external code can find it.
        cancel_event = threading.Event()
        session["_cancel_event"] = cancel_event

        def _worker():
            t0 = time.perf_counter()
            try:
                # Probe total duration upfront so the progress display has
                # context — "of 01:32:15" rather than just "decoded".
                _set_phase("preparing")
                try:
                    durs = []
                    for p in media:
                        d = engines.get_media_duration(p) or 0.0
                        if d:
                            durs.append(float(d))
                    # mix_for_transcript pads to the longest input; for a
                    # single source the duration is just the source itself.
                    total = max(durs) if durs else 0.0
                    if (prog := session.get("_progress")):
                        prog["total_secs"] = total
                except Exception:
                    pass

                # Be explicit when the configured model isn't cached
                # yet — get_model() can sit for several minutes inside
                # faster-whisper's download path with no visible
                # progress.  Surface that we're downloading so the user
                # doesn't think the worker is hung.
                _cfg_size = engines.get_active_model_size()
                if not engines._is_model_cached(_cfg_size):
                    # Rough sizes for the status message.
                    _approx = {
                        "tiny":     "75 MB",
                        "base":     "145 MB",
                        "small":    "485 MB",
                        "medium":   "1.5 GB",
                        "large-v3": "3.1 GB",
                    }.get(_cfg_size, "")
                    _set_phase(
                        "downloading model ({}){}".format(
                            _cfg_size,
                            " — " + _approx if _approx else ""),
                        "")
                else:
                    _set_phase("loading model")
                engines.get_model()

                # Snapshot the pre-run transcript so re-transcribe can
                # preserve user edits via the same merge logic the
                # progressive flow uses.  If the user previously edited
                # any words (right-click → Edit), those entries carry
                # _src='user' and survive every subsequent run.
                pre_run_transcript = list(session.get("transcript") or [])
                pre_run_user_edits = [
                    w for w in pre_run_transcript
                    if w.get("_src") == "user"
                ]

                audios = [p for p in media if not is_video(p)]
                if not audios:
                    audios = list(media)

                tmp_paths = []
                try:
                    def _on_seg_end(audio_s, **kw):
                        prog = session.get("_progress")
                        if not prog or not prog.get("active"):
                            return
                        total = prog.get("total_secs", 0)
                        track_name = kw.get("track_name")
                        n_tracks   = kw.get("n_tracks", 1)
                        idx        = (kw.get("track_index", 0) or 0) + 1
                        prefix = ""
                        if track_name and n_tracks > 1:
                            prefix = " [track {}/{}]".format(idx, n_tracks)
                        if total > 0:
                            pct = max(0, min(100, audio_s * 100.0 / total))
                            prog["extra"] = "{} · {} of {} ({:.0f}%)".format(
                                prefix, _fmt_dur(audio_s),
                                _fmt_dur(total), pct)
                        else:
                            prog["extra"] = "{} · {} decoded".format(
                                prefix, _fmt_dur(audio_s))

                    # ── Streaming word emission ─────────────────────
                    # Fires once per decoded Whisper segment with the
                    # words from THAT segment.  Appends them to a
                    # streaming accumulator and triggers a throttled
                    # main-thread render so the user sees text appear
                    # progressively instead of waiting for the entire
                    # pass to finish.  Throttled to ~1.2 s between
                    # renders so we don't redraw on every short
                    # segment of a busy file.
                    # Two parallel streaming buffers: one filled by the
                    # tiny draft pass, one filled by the configured
                    # ("main") refinement pass.  Both render through
                    # the same throttle.  _pq_merge_streams combines
                    # them so main words win at any timestamp the main
                    # frontier has already passed, falling back to
                    # tiny ahead of the frontier, and user edits
                    # always survive.
                    streaming_words = []          # tiny draft buffer
                    streaming_main_words = []     # configured pass buffer
                    streaming_last_render = [0.0]
                    streaming_render_pending = [False]
                    streaming_tick = [0]
                    # Diagnostic logging — gated behind _PQ_STREAM_DIAG.
                    # When False (default), _pq_stream_log is a no-op and
                    # the log file is never touched.  Flip the flag at
                    # the top of the file when you need to inspect what
                    # the streaming merge is doing.
                    if _PQ_STREAM_DIAG:
                        _pq_stream_log_path = os.path.join(
                            os.path.dirname(os.path.abspath(__file__)),
                            "_pq_stream.log")
                        try:
                            with open(_pq_stream_log_path, "w",
                                      encoding="utf-8") as _f0:
                                _f0.write(
                                  "# Pull Quotes stream diagnostics — "
                                  "one JSON line per render tick.\n")
                        except Exception:
                            pass

                        def _pq_stream_log(event, **fields):
                            try:
                                rec = {"t":  round(
                                            time.perf_counter() - t0, 2),
                                       "ev": event}
                                rec.update(fields)
                                with open(_pq_stream_log_path, "a",
                                          encoding="utf-8") as _f:
                                    _f.write(json.dumps(rec) + "\n")
                            except Exception:
                                pass
                    else:
                        def _pq_stream_log(event, **fields):
                            return

                    def _do_stream_render(s=session):
                        streaming_render_pending[0] = False
                        # Only render if this session is still on screen
                        # AND no full transcript has overwritten the
                        # streaming buffer (which would happen at the
                        # very end of the pass when the worker assigns
                        # the final word list).
                        if getattr(self, "_pq_current_session",
                                   None) is s:
                            # Three-way merge: tiny draft + configured
                            # main + user edits already in the session.
                            # User edits always survive; main wins over
                            # tiny for any region the main pass has
                            # reached; tiny shows ahead of the main
                            # frontier so the user sees a moving
                            # boundary instead of nothing.
                            current = s.get("transcript") or []
                            cur_user = sum(1 for w in current
                                           if w.get("_src") == "user")
                            cur_nonuser = sum(1 for w in current
                                              if (w.get("_src") != "user"
                                                  and not w.get("break")))
                            tiny_snap = list(streaming_words)
                            main_snap = list(streaming_main_words)
                            merged = App._pq_merge_streams(
                                tiny_snap, main_snap, current)
                            streaming_tick[0] += 1
                            _pq_stream_log("render",
                                tick=streaming_tick[0],
                                tiny_buf=len(tiny_snap),
                                main_buf=len(main_snap),
                                cur_total=len(current),
                                cur_user=cur_user,
                                cur_nonuser=cur_nonuser,
                                merged=len(merged),
                                merged_user=sum(
                                    1 for w in merged
                                    if w.get("_src") == "user"))
                            s["transcript"] = merged
                            # Streaming frontier — the further-along of
                            # {main, tiny}.  Hides preserved user
                            # edits past this point at render time so
                            # they appear in place only once streaming
                            # arrives at their audio position.
                            _main_end = 0.0
                            for _w in main_snap:
                                if _w.get("break"): continue
                                _e = float(_w.get("end", 0.0))
                                if _e > _main_end: _main_end = _e
                            _tiny_end_val = 0.0
                            for _w in tiny_snap:
                                if _w.get("break"): continue
                                _e = float(_w.get("end", 0.0))
                                if _e > _tiny_end_val: _tiny_end_val = _e
                            _frontier = max(_main_end, _tiny_end_val)
                            # Capture the interactive state (cursor,
                            # selection, scroll, focus) BEFORE the
                            # re-render so Jordan can edit / scroll /
                            # select during the stream without being
                            # yanked back to the end on every tick.
                            state = self._pq_capture_text_state()
                            try:
                                self._pq_render_transcript_text(
                                    s, user_frontier=_frontier)
                            except Exception:
                                pass
                            else:
                                self._pq_restore_text_state(state)

                    def _schedule_stream_render():
                        now = time.perf_counter()
                        if (now - streaming_last_render[0] < 1.2
                                or streaming_render_pending[0]):
                            return
                        streaming_last_render[0] = now
                        streaming_render_pending[0] = True
                        # Schedule the render on the Tk main thread —
                        # this is called from worker threads.
                        self._ui(_do_stream_render)

                    def _stream_seg_cb_tiny(seg_words):
                        # Log timestamp range of the incoming segment
                        # so we can see exactly what tiny is emitting.
                        _real = [w for w in (seg_words or [])
                                 if not w.get("break")]
                        _pq_stream_log("seg_tiny",
                            n=len(seg_words or []),
                            n_real=len(_real),
                            t0=(float(_real[0]["start"])
                                if _real else None),
                            t1=(float(_real[-1]["end"])
                                if _real else None))
                        streaming_words.extend(seg_words)
                        _schedule_stream_render()

                    def _stream_seg_cb_main(seg_words):
                        _real = [w for w in (seg_words or [])
                                 if not w.get("break")]
                        _pq_stream_log("seg_main",
                            n=len(seg_words or []),
                            n_real=len(_real),
                            t0=(float(_real[0]["start"])
                                if _real else None),
                            t1=(float(_real[-1]["end"])
                                if _real else None))
                        streaming_main_words.extend(seg_words)
                        _schedule_stream_render()

                    # ── Decide whether (and how) to run a tiny draft pass
                    # When the user is actively viewing this session and
                    # their configured model isn't already 'tiny', do a
                    # fast tiny pass first so they see SOMETHING within
                    # ~30 s instead of waiting minutes for the full
                    # configured model.  Audio extraction takes a few
                    # seconds so the "is the user viewing?" check
                    # naturally functions as a dwell filter — drive-by
                    # transcribe-then-leave doesn't pay the tiny cost.
                    #
                    # On GPU we run tiny IN PARALLEL with the configured
                    # model (two CTranslate2 instances, separate threads).
                    # The tiny preview renders the moment it's ready; the
                    # configured pass continues unaffected.  On CPU we
                    # fall back to sequential — both inference loops
                    # contending for the same cores would slow each
                    # other down and the gain disappears.
                    configured_size = engines.get_active_model_size()
                    is_viewing_now = (getattr(self, "_pq_current_session",
                                               None) is session)
                    # Progressive (tiny draft → configured refine) runs
                    # any time the user is viewing AND the configured
                    # model is bigger than tiny.  We used to skip it
                    # when an existing transcript was present so a
                    # re-transcribe wouldn't downgrade quality before
                    # the configured pass caught up — but Jordan
                    # explicitly wanted a draft ASAP on re-transcribe
                    # too, and the merge keeps the prior content as
                    # backdrop / user edits as sacred regardless.
                    do_progressive = (is_viewing_now
                                       and configured_size != "tiny")
                    parallel_ok = engines.is_cuda_available()

                    # Per-pass thread storage when running parallel.
                    tiny_thread = None
                    tiny_words_box = [None]   # mutable container; thread writes

                    def _run_tiny_pass(audios=audios, mix_path_ref=None):
                        """Tiny draft worker.  Writes to tiny_words_box,
                        streams each segment's words into the live
                        transcript pane, and catches its own exceptions
                        so a tiny crash never kills the parent worker's
                        configured run."""
                        try:
                            if len(audios) == 1:
                                tw = engines.transcribe_clip_verbatim(
                                    mix_path_ref, progress_cb=_on_seg_end,
                                    cancel_event=cancel_event,
                                    model_size="tiny",
                                    segment_cb=_stream_seg_cb_tiny)
                            else:
                                speaker_labels = session.get("speakers") or {}
                                tw = engines.transcribe_session_per_track(
                                    audios, speaker_labels=speaker_labels,
                                    progress_cb=_on_seg_end,
                                    cancel_event=cancel_event,
                                    model_size="tiny",
                                    segment_cb=_stream_seg_cb_tiny)
                            tiny_words_box[0] = tw
                            if tw and getattr(
                                    self, "_pq_current_session", None
                                    ) is session:
                                # Final render with the complete tiny
                                # output (streaming may have rendered a
                                # slightly stale view at last throttle).
                                # Three-way merge against user edits
                                # AND any configured-pass words that
                                # may have already streamed in.
                                #
                                # Snapshot inputs on this thread, but
                                # marshal the actual session-state write
                                # onto the main thread.  The main thread
                                # ALSO writes session["transcript"] from
                                # _do_stream_render (line 11136-ish via
                                # _pq_merge_streams); writing this dict
                                # slot from two threads racelessly is
                                # what self._ui is for.  Narrow window
                                # (microseconds between the read and the
                                # assign) but the fix is cheap and
                                # brings the site into parity with the
                                # _ui-marshalled preview-ready call
                                # immediately below.
                                _tw_snap   = list(tw)
                                _smain_snap = list(streaming_main_words)
                                def _commit(s=session, tws=_tw_snap, sms=_smain_snap):
                                    s["transcript"] = App._pq_merge_streams(
                                        tws, sms, s.get("transcript") or [])
                                    self._pq_preview_ready(s)
                                self._ui(_commit)
                        except engines.TranscriptionCancelled:
                            pass
                        except Exception as _e:
                            # Don't let tiny failure block the configured
                            # run — log and move on.
                            print("PQ tiny pass failed:",
                                  _e, file=sys.stderr)

                    if len(audios) == 1:
                        # Single source — existing fast path.
                        with tempfile.NamedTemporaryFile(
                                suffix=".wav", delete=False) as _tf:
                            mix_path = _tf.name
                        tmp_paths.append(mix_path)
                        _set_phase("extracting audio")
                        ok, err = engines.extract_window(
                            audios[0], 0, 9999, mix_path)
                        if not ok:
                            raise RuntimeError(
                                "audio extract failed: " + (err or ""))

                        if do_progressive and parallel_ok:
                            # GPU: launch tiny in a sibling thread, then
                            # run configured here in parallel.  Both
                            # passes stream into their respective
                            # buffers and the merge picks main over
                            # tiny for any timestamp main has reached.
                            _set_phase("draft + refining (parallel)", "")
                            tiny_thread = threading.Thread(
                                target=_run_tiny_pass,
                                kwargs={"mix_path_ref": mix_path},
                                daemon=True)
                            tiny_thread.start()
                            words = engines.transcribe_clip_verbatim(
                                mix_path, progress_cb=_on_seg_end,
                                cancel_event=cancel_event,
                                segment_cb=_stream_seg_cb_main)
                            # Wait for tiny — almost always finished first
                            # but guarantee both done before merge.
                            tiny_thread.join()
                        elif do_progressive:
                            # CPU: run tiny then configured sequentially.
                            _set_phase("draft (tiny)", "")
                            _run_tiny_pass(mix_path_ref=mix_path)
                            _set_phase("refining", "")
                            words = engines.transcribe_clip_verbatim(
                                mix_path, progress_cb=_on_seg_end,
                                cancel_event=cancel_event,
                                segment_cb=_stream_seg_cb_main)
                        else:
                            # Non-progressive: stream the configured
                            # pass directly so the user sees text
                            # appear as it's decoded.
                            _set_phase("transcribing", "")
                            words = engines.transcribe_clip_verbatim(
                                mix_path, progress_cb=_on_seg_end,
                                cancel_event=cancel_event,
                                segment_cb=_stream_seg_cb_main)
                    else:
                        # Multi-track — transcribe each independently so
                        # we know who said what (speaker = source file).
                        speaker_labels = session.get("speakers") or {}

                        if do_progressive and parallel_ok:
                            _set_phase("draft + refining (parallel)")
                            tiny_thread = threading.Thread(
                                target=_run_tiny_pass,
                                daemon=True)
                            tiny_thread.start()
                            words = engines.transcribe_session_per_track(
                                audios, speaker_labels=speaker_labels,
                                progress_cb=_on_seg_end,
                                cancel_event=cancel_event,
                                segment_cb=_stream_seg_cb_main)
                            tiny_thread.join()
                        elif do_progressive:
                            _set_phase("draft (tiny)")
                            _run_tiny_pass()
                            _set_phase("refining {} tracks".format(
                                len(audios)))
                            words = engines.transcribe_session_per_track(
                                audios, speaker_labels=speaker_labels,
                                progress_cb=_on_seg_end,
                                cancel_event=cancel_event,
                                segment_cb=_stream_seg_cb_main)
                        else:
                            # Non-progressive multi-track: stream
                            # configured directly.
                            _set_phase("transcribing {} tracks".format(
                                len(audios)))
                            words = engines.transcribe_session_per_track(
                                audios, speaker_labels=speaker_labels,
                                progress_cb=_on_seg_end,
                                cancel_event=cancel_event,
                                segment_cb=_stream_seg_cb_main)
                        # Build a mix purely for the playback cache so
                        # the user can hear both speakers when auditioning
                        # transcript regions.
                        _set_phase("building playback mix")
                        mix_path = engines.mix_for_transcript(audios)
                        tmp_paths.append(mix_path)

                    # ── Merge with prior user edits ──────────────────
                    # Two scenarios produce a merge:
                    #
                    #  A) Progressive run.  Tiny pre-pass populated
                    #     session['transcript'] with _src='tiny' words;
                    #     user may have edited some of them (now
                    #     _src='user') while the configured model ran.
                    #     Merge against the CURRENT session transcript
                    #     so we pick up those mid-flight edits.
                    #
                    #  B) Re-transcribe.  No tiny pass; the old
                    #     transcript carried _src='user' edits from
                    #     prior sessions.  Merge against the
                    #     pre_run_transcript snapshot we captured
                    #     before the worker started.
                    if do_progressive and session.get("transcript"):
                        words = App._pq_merge_refine(
                            session["transcript"], words)
                    elif pre_run_user_edits:
                        words = App._pq_merge_refine(
                            pre_run_transcript, words)

                    # Persist the mix (or single extracted WAV) as the
                    # session's playback cache.
                    cache_dest = self._pq_audio_cache_path(session)
                    if cache_dest and os.path.isfile(mix_path):
                        try:
                            import shutil as _sh
                            os.makedirs(os.path.dirname(cache_dest),
                                        exist_ok=True)
                            _sh.copyfile(mix_path, cache_dest)
                            session["audio_cache"] = cache_dest
                        except Exception:
                            pass
                finally:
                    for tp in tmp_paths:
                        try: os.unlink(tp)
                        except Exception: pass

                # Late safety net: if cancel was requested but the
                # decode somehow completed before the next segment
                # boundary was checked, still discard the result.  The
                # primary cancellation path is the TranscriptionCancelled
                # exception raised from within the engine — see the
                # exception handler below.
                if session.get("_cancel_requested"):
                    session.pop("_cancel_requested", None)
                    raise engines.TranscriptionCancelled()

                # Defensive final-merge: edits committed in the brief
                # window between the configured-pass merge and this
                # final assignment would otherwise be silently lost.
                # Re-merge against the LATEST session["transcript"]
                # snapshot so any user edits made very late in the
                # run are preserved.  Idempotent when nothing changed.
                _latest = session.get("transcript") or []
                _has_late_user_words = any(
                    w.get("_src") == "user" for w in _latest)
                if _has_late_user_words:
                    words = App._pq_merge_refine(_latest, words)

                session["transcript"] = list(words or [])
                self._pq_save_session_file(session)

                elapsed = round(time.perf_counter() - t0, 1)
                def _done():
                    prog = session.get("_progress")
                    if prog is not None:
                        prog["active"] = False
                    # If the user is still looking at this session, refresh
                    # its view.  Otherwise update the project row in place
                    # (if it's on screen) — no full re-render, no twitch.
                    if getattr(self, "_pq_current_session", None) is session:
                        # Preserve cursor / selection / scroll across
                        # the final render — the user may have been
                        # reading partway through the streaming output
                        # and shouldn't be yanked back to the top when
                        # the run completes.
                        _final_state = self._pq_capture_text_state()
                        self._pq_render_transcript_text(session)
                        self._pq_restore_text_state(_final_state)
                        if (btn := getattr(self, "_pq_retx_btn", None)):
                            btn.config(text="⟳ RE-TRANSCRIBE", fg=TEXT)
                        if (lbl := getattr(self, "_pq_status_lbl", None)):
                            lbl.config(text="✓ done in {}s".format(elapsed),
                                        fg=SUCCESS)
                            self.after(2500,
                                       lambda: self._pq_update_status_lbl(session))
                    elif (getattr(self, "_pq_current_session", None) is None
                          and getattr(self, "_pq_project", None) is not None):
                        self._pq_update_project_row(session)
                self._ui(_done)
            except Exception as e:
                err = str(e)
                is_mem = isinstance(e, MemoryError) or "allocate" in err.lower()
                # User-requested cancel: detected either by the dedicated
                # TranscriptionCancelled exception raised mid-decode from
                # the engine, or by the late-safety-net check above.
                is_cancel = (
                    isinstance(e, engines.TranscriptionCancelled)
                    or "cancelled by user" in err.lower()
                    or "cancelled" in err.lower())
                def _err():
                    prog = session.get("_progress")
                    if prog is not None:
                        prog["active"] = False
                    if getattr(self, "_pq_current_session", None) is session:
                        if (btn := getattr(self, "_pq_retx_btn", None)):
                            btn.config(text="⟳ TRANSCRIBE", fg=TEXT)
                        if (lbl := getattr(self, "_pq_status_lbl", None)):
                            lbl.config(
                                text=("cancelled" if is_cancel else "error"),
                                fg=(SUB if is_cancel else ERR))
                        # Preserve cursor / selection / scroll on the
                        # error-path render too — cancel mid-read
                        # shouldn't jump the user away from where they
                        # were looking.
                        _err_state = self._pq_capture_text_state()
                        self._pq_render_transcript_text(session)
                        self._pq_restore_text_state(_err_state)
                    elif getattr(self, "_pq_current_session", None) is None:
                        self._pq_update_project_row(session)
                    if is_mem:
                        # MemoryError mid-transcribe — almost always a
                        # Windows commit-charge exhaustion.  Pull live
                        # numbers so the user sees what's tight.
                        phys = self._available_ram_bytes()
                        page = self._available_pagefile_bytes()
                        diag = [err, ""]
                        if phys is not None:
                            diag.append("Physical RAM free:  {:.1f} GB".format(
                                phys / (1024 ** 3)))
                        if page is not None:
                            diag.append("Commit headroom:    {:.1f} GB".format(
                                page / (1024 ** 3)))
                            if page < 2 * (1024 ** 3):
                                diag.append("")
                                diag.append("Commit charge is near the limit — "
                                            "Windows refuses allocations even "
                                            "when physical RAM is free.")
                        diag.append("")
                        diag.append("Try:")
                        diag.append("  •  Quit Pro Tools / Premiere / browsers")
                        diag.append("  •  If commit headroom is low, a Windows "
                                    "restart is the fastest fix")
                        diag.append("  •  Use a shorter source clip or split "
                                    "the audio")
                        messagebox.showerror(
                            "Out of Memory ({})".format(
                                session.get("token", "?")),
                            "\n".join(diag))
                    elif is_cancel:
                        # No popup — the user already saw the Settings
                        # dialog and chose to cancel.  Status lbl above
                        # already reads "cancelled".
                        pass
                    else:
                        messagebox.showerror(
                            "Transcription failed ({})".format(
                                session.get("token", "?")), err)
                self._ui(_err)
            finally:
                self._pq_apply_priority(low=False, prev=prev_priority)
                # Clear cancel state regardless of how the worker
                # exited; the next TRANSCRIBE click will recreate it.
                session.pop("_cancel_event", None)
                session.pop("_cancel_requested", None)

        threading.Thread(target=_worker, daemon=True).start()

    def _pq_render_progress_placeholder(self):
        """Show an animated 'transcription in progress' message in the
        transcript pane while a session is decoding AND has no words yet.

        Critical: once a transcript exists (e.g. the tiny preview during
        a progressive run), DO NOT overwrite it — the user is already
        reading and possibly editing.  The placeholder is only useful
        when the pane is otherwise empty.  Status / refining state is
        communicated via the status label instead.
        """
        tx      = getattr(self, "_pq_tx_text", None)
        session = getattr(self, "_pq_current_session", None)
        if not tx or session is None:
            return
        prog = session.get("_progress") or {}
        if not prog.get("active"):
            return
        # Don't clobber an existing transcript (tiny preview or full).
        if session.get("transcript"):
            return
        try:
            if not tx.winfo_exists():
                return
            dots = "." * (1 + (prog.get("elapsed", 0) % 3))
            msg = ("  {}{}{}\n\n  (transcription continues in the background "
                   "— feel free to navigate away or transcribe other "
                   "sessions; results save automatically.)").format(
                       prog.get("phase", "working"),
                       prog.get("extra", ""), dots)
            tx.configure(state="normal")
            tx.delete("1.0", "end")
            tx.insert("1.0", msg)
            self._pq_word_index = []
        except tk.TclError:
            return

    def _pq_animate_progress(self):
        """Tick the dots animation every 600 ms while the currently-visible
        session is transcribing.  Stops automatically when active=False or
        when the user navigates away."""
        session = getattr(self, "_pq_current_session", None)
        if session is None:
            return
        prog = session.get("_progress") or {}
        if not prog.get("active"):
            return
        prog["elapsed"] = prog.get("elapsed", 0) + 1
        lbl = getattr(self, "_pq_status_lbl", None)
        if lbl:
            try:
                if lbl.winfo_exists():
                    phase = prog.get("phase", "working")
                    extra = prog.get("extra", "")
                    lbl.config(text="{}{}{}".format(
                        phase, extra, "." * (1 + (prog["elapsed"] % 3))),
                        fg=ACCENT)
            except tk.TclError:
                pass
        self._pq_render_progress_placeholder()
        self.after(600, self._pq_animate_progress)

    def _pq_apply_priority(self, low, prev=None):
        """Lower (or restore) the host process priority.

        Returns the previous priority handle so it can be restored from a
        finally clause.  Failures are swallowed — priority is best-effort.
        """
        try:
            if sys.platform == "win32":
                import ctypes
                k32 = ctypes.windll.kernel32
                BELOW_NORMAL = 0x00004000
                NORMAL       = 0x00000020
                handle = k32.GetCurrentProcess()
                if prev is not None:
                    k32.SetPriorityClass(handle, prev)
                    return prev
                cur = k32.GetPriorityClass(handle)
                target = BELOW_NORMAL if low else NORMAL
                if cur != target:
                    k32.SetPriorityClass(handle, target)
                return cur
            elif hasattr(os, "nice"):
                # POSIX: relative niceness; we apply +10 when entering low
                # mode, then -10 when restoring.
                if prev is not None:
                    try:
                        os.nice(-int(prev))
                    except Exception:
                        pass
                    return None
                if low:
                    try:
                        os.nice(10)
                        return 10
                    except Exception:
                        return None
                return None
        except Exception:
            return None
        return None

    # ── Search ────────────────────────────────────────────────────────────
    def _pq_search_apply(self):
        """Recompute matches for the current query and refresh highlights.

        Wrapped in a top-level try/except so a malformed query (or any
        Tk quirk) can never crash the app — search just reports the
        error and bails.
        """
        tx = getattr(self, "_pq_tx_text", None)
        var = getattr(self, "_pq_search_var", None)
        lbl = getattr(self, "_pq_search_count_lbl", None)
        if tx is None or var is None:
            return
        try:
            tx.tag_remove("pq_match",         "1.0", "end")
            tx.tag_remove("pq_match_current", "1.0", "end")
        except tk.TclError:
            return
        self._pq_search_hits = []
        self._pq_search_cur  = -1

        term = var.get().strip()
        if not term:
            if lbl: lbl.config(text="", fg=SUB)
            return

        try:
            idx       = "1.0"
            term_len  = len(term)
            count_var = tk.IntVar(self)   # populated by Tk on each match
            # Hard upper bound on iterations so we cannot pathologically
            # loop on a degenerate query — 5000 hits is far more than
            # any user would ever need to see.
            for _ in range(5000):
                pos = tx.search(term, idx, stopindex="end",
                                 nocase=True, count=count_var)
                if not pos:
                    break
                # `count_var` reports the actual match length (handy if
                # we add regex mode later); for plain text it equals
                # `term_len` but reading the var is the safer path.
                hit_len = count_var.get() or term_len
                end = "{}+{}c".format(pos, hit_len)
                tx.tag_add("pq_match", pos, end)
                self._pq_search_hits.append((pos, end))
                # Always advance — if the search returns the same
                # position twice (shouldn't, but be paranoid) we still
                # break the loop.
                new_idx = end
                if new_idx == idx:
                    break
                idx = new_idx
        except (tk.TclError, ValueError) as exc:
            if lbl:
                lbl.config(text="search error: {}".format(str(exc)[:32]),
                           fg=ERR)
            return

        n = len(self._pq_search_hits)
        if lbl:
            lbl.config(
                text=("{} match{}".format(n, "es" if n != 1 else "")
                      if n else "no matches"),
                fg=SUCCESS if n else WARN)
        if n:
            self._pq_search_cur = 0
            self._pq_search_focus_current()

    def _pq_search_step(self, delta):
        n = len(getattr(self, "_pq_search_hits", []))
        if not n:
            # Re-run the search if user hits Enter on a fresh query
            self._pq_search_apply()
            return
        cur = (self._pq_search_cur + delta) % n
        self._pq_search_cur = cur
        self._pq_search_focus_current()

    def _pq_search_focus_current(self):
        tx = getattr(self, "_pq_tx_text", None)
        hits = getattr(self, "_pq_search_hits", [])
        cur  = getattr(self, "_pq_search_cur", -1)
        lbl  = getattr(self, "_pq_search_count_lbl", None)
        if tx is None or not hits or cur < 0:
            return
        try:
            tx.tag_remove("pq_match_current", "1.0", "end")
            start, end = hits[cur]
            tx.tag_add("pq_match_current", start, end)
            tx.see(start)
        except tk.TclError:
            return
        if lbl:
            lbl.config(text="{} of {}".format(cur + 1, len(hits)),
                       fg=SUCCESS)

    def _pq_search_clear(self):
        var = getattr(self, "_pq_search_var", None)
        if var is not None:
            var.set("")

    # ── Audio playback ────────────────────────────────────────────────────
    def _pq_audio_cache_path(self, session):
        """Return the path where the playback cache for this session lives."""
        media = session.get("media") or []
        token = session.get("token") or "SESSION"
        if not media:
            return None
        return os.path.join(os.path.dirname(os.path.abspath(media[0])),
                            "{}.pb_audio.wav".format(token))

    def _pq_play_selection(self):
        """Extract the time range under the current selection (or word at
        cursor) from the persisted session audio and play it via winsound."""
        tx      = getattr(self, "_pq_tx_text", None)
        session = getattr(self, "_pq_current_session", None)
        if tx is None or session is None:
            return

        # If currently playing, the play button doubles as STOP.
        if getattr(self, "_pq_playing", False):
            self._pq_stop_playback()
            return

        # Resolve selected words → time range
        word_idx = getattr(self, "_pq_word_index", []) or []
        if not word_idx:
            messagebox.showinfo("Nothing to play",
                "Transcribe this session first.")
            return

        try:
            sel_first = tx.index("sel.first")
            sel_last  = tx.index("sel.last")
        except tk.TclError:
            sel_first = sel_last = None

        play_to_end = False
        if sel_first and sel_last:
            c0 = _tx_count_chars(tx, "1.0", sel_first)
            c1 = _tx_count_chars(tx, "1.0", sel_last)
            picked = [w for (s, e, w) in word_idx if e > c0 and s < c1]
        else:
            # No selection — play indefinitely from the cursor word to the
            # end of the audio (Shift+Space toggles stop).
            c0 = _tx_count_chars(tx, "1.0", tx.index("insert"))
            picked = [w for (s, e, w) in word_idx if s <= c0 < e]
            if not picked:
                # Cursor is past the last word — start at the next word
                picked = [w for (s, e, w) in word_idx if s >= c0][:1]
            if not picked and word_idx:
                picked = [word_idx[0][2]]
            play_to_end = True

        if not picked:
            messagebox.showinfo("Nothing to play",
                "Click inside a word or select a passage first.")
            return

        in_s = float(picked[0].get("start", 0))
        if play_to_end:
            # Use the cached audio's full duration as the end so we play
            # to the very end without arbitrary truncation.
            out_s = None   # signals open-ended below
        else:
            out_s = float(picked[-1].get("end", in_s))
            if out_s <= in_s:
                out_s = in_s + 5.0

        # Prefer the persisted session audio (matches what was transcribed);
        # fall back to the first source file.
        cache = session.get("audio_cache") or self._pq_audio_cache_path(session)
        if cache and not os.path.isfile(cache):
            cache = None
        src = cache or (session.get("media") or [None])[0]
        if not src or not os.path.isfile(src):
            messagebox.showerror("No audio",
                "Cannot find audio for this session.")
            return

        # Resolve the duration to extract.  For open-ended playback (no
        # selection), probe the audio file once to get its full length.
        if out_s is None:
            try:
                total = engines.get_media_duration(src) or (in_s + 7200)
            except Exception:
                total = in_s + 7200
            duration_s = max(1.0, total - in_s)
        else:
            duration_s = max(0.5, out_s - in_s)

        # Extract → temp WAV → play.  Create the temp file BEFORE the
        # try, so the cleanup branch always knows the path to unlink —
        # the previous code created tf inside the try and the failure
        # branch returned without removing it, leaking a zero-byte WAV
        # into the OS temp dir on every failed playback.
        tf = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tf.close()
        try:
            engines.extract_audio_segment(
                src, in_s, duration_s, tf.name,
                sample_rate=22050)
        except Exception as e:
            try: os.unlink(tf.name)
            except Exception: pass
            messagebox.showerror("Playback failed", str(e))
            return

        # Clean up any prior temp first
        if (prev := getattr(self, "_pq_play_tmp", None)):
            try: os.unlink(prev)
            except Exception: pass
        self._pq_play_tmp = tf.name

        if sys.platform == "win32":
            try:
                import winsound
                # Anchor the wall-clock instant we tell winsound to
                # start, so the highlight tick can compute the
                # playhead position from elapsed time + audio offset.
                self._pq_play_clock_start = time.perf_counter()
                self._pq_play_audio_in_s  = in_s
                winsound.PlaySound(tf.name,
                                   winsound.SND_FILENAME | winsound.SND_ASYNC)
                self._pq_playing = True
                if (b := getattr(self, "_pq_play_btn", None)):
                    b.config(text="■ STOP")
                # Kick off the karaoke highlight loop
                self._pq_playback_highlight_tick()
                # Schedule auto-restore of button text once playback ends
                dur_ms = int(max(500, duration_s * 1000) + 300)
                self.after(dur_ms, self._pq_stop_playback)
            except Exception as e:
                messagebox.showerror("Playback failed", str(e))
        else:
            messagebox.showinfo("Playback unsupported",
                "Audio playback is only wired up for Windows in this build.")

    def _pq_playback_highlight_tick(self):
        """While playback is active, update the 'body_playing'
        highlight to whichever word the playhead is currently inside
        and scroll it into view.  Polls itself every ~60 ms — fast
        enough to feel smooth, slow enough not to thrash Tk."""
        if not getattr(self, "_pq_playing", False):
            return
        tx = getattr(self, "_pq_tx_text", None)
        if tx is None:
            return
        word_idx = getattr(self, "_pq_word_index", []) or []
        start_clock = getattr(self, "_pq_play_clock_start", None)
        audio_in_s  = getattr(self, "_pq_play_audio_in_s",  None)
        if (word_idx and start_clock is not None
                and audio_in_s is not None):
            try:
                playhead = audio_in_s + (
                    time.perf_counter() - start_clock)
                # Linear search is fine — _pq_word_index typically
                # has a few hundred entries and we only run this 16x/s.
                cur = None
                for (s_abs, e_abs, w) in word_idx:
                    ws = float(w.get("start", 0.0))
                    we = float(w.get("end",   ws))
                    if ws <= playhead < we:
                        cur = (s_abs, e_abs)
                        break
                tx.tag_remove("body_playing", "1.0", "end")
                if cur is not None:
                    tx.tag_add(
                        "body_playing",
                        "1.0 + {}c".format(cur[0]),
                        "1.0 + {}c".format(cur[1]))
                    # Keep the playing word on screen without
                    # snapping the user back to it on every tick if
                    # they intentionally scrolled away — tx.see only
                    # scrolls if the index is offscreen.
                    tx.see("1.0 + {}c".format(cur[0]))
            except tk.TclError:
                pass
        # Schedule next tick.  Cancel handle stored so stop can kill
        # the loop immediately rather than waiting for the next poll.
        self._pq_play_hi_after = self.after(
            60, self._pq_playback_highlight_tick)

    def _pq_stop_playback(self):
        if not getattr(self, "_pq_playing", False):
            return
        self._pq_playing = False
        # Cancel the highlight loop and clear any lingering tag.
        if (after_id := getattr(self, "_pq_play_hi_after", None)):
            try: self.after_cancel(after_id)
            except Exception: pass
            self._pq_play_hi_after = None
        tx = getattr(self, "_pq_tx_text", None)
        if tx is not None:
            try:
                tx.tag_remove("body_playing", "1.0", "end")
            except tk.TclError:
                pass
        if sys.platform == "win32":
            try:
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
        if (b := getattr(self, "_pq_play_btn", None)):
            try:
                b.config(text="▶ PLAY SELECTION")
            except tk.TclError:
                pass

    def _pq_copy_as_pull(self, event=None):
        """Convert the current transcript selection into a @PULL block."""
        tx      = getattr(self, "_pq_tx_text", None)
        session = getattr(self, "_pq_current_session", None)
        if tx is None or session is None:
            return None
        try:
            sel_first = tx.index("sel.first")
            sel_last  = tx.index("sel.last")
        except tk.TclError:
            return None   # no selection — let default copy run

        # Convert tk indices to character offsets from "1.0".
        c0 = _tx_count_chars(tx, "1.0", sel_first)
        c1 = _tx_count_chars(tx, "1.0", sel_last)

        word_idx = getattr(self, "_pq_word_index", []) or []
        if not word_idx:
            return "break"

        # All words whose character range overlaps the selection.
        selected = [w for (s_, e_, w) in word_idx if e_ > c0 and s_ < c1]
        if not selected:
            return "break"

        in_s  = float(selected[0].get("start", 0))
        out_s = float(selected[-1].get("end", in_s))
        # PostBridge timecodes are integer seconds (the parser rejects a
        # fractional part).  Round to the NEAREST second instead of
        # truncating: the old secs_tc(...).split(".")[0] floored every
        # value, which pushed the IN point EARLIER than the spoken word —
        # up to a full second (reported) — and clipped the tail at OUT.
        # Nearest rounding removes that systematic early bias on both edges.
        in_tc  = secs_tc(round(in_s)).split(".")[0]
        out_tc = secs_tc(round(out_s)).split(".")[0]

        # Pull body verbatim from the rendered transcript so what the
        # user copies matches what they see — speaker labels, paragraph
        # breaks, line breaks, all preserved.  We expand the selection
        # to whole words at both edges so a partial-word click doesn't
        # truncate mid-token.
        word_idx_map = {id(w): (s, e) for (s, e, w) in word_idx}
        first_s = word_idx_map.get(id(selected[0]),  (None, None))[0]
        last_e  = word_idx_map.get(id(selected[-1]), (None, None))[1]

        if first_s is not None and last_e is not None:
            try:
                # Use the selection bounds expanded to whole words so a
                # partial-word click doesn't truncate mid-token.  The old
                # code also snapped start back via "linestart", intending
                # to grab a leading speaker label — but Tk's "linestart"
                # walks to the LOGICAL line start (the previous newline),
                # and rendered paragraphs have no internal newlines under
                # wrap="word", so the snap captured the ENTIRE paragraph
                # whenever the user selected a sentence inside one
                # (reported).  Drop it: the [TOKEN HH:MM:SS-HH:MM:SS]
                # header already encodes the speaker, so the body
                # doesn't need a leading "BRYAN1:" prefix.
                start_idx = "1.0+{}c".format(first_s)
                end_idx   = "1.0+{}c".format(last_e)
                body = tx.get(start_idx, end_idx)
            except tk.TclError:
                body = tx.get(sel_first, sel_last)
        else:
            body = tx.get(sel_first, sel_last)

        # Tidy: rstrip every line, strip outer whitespace.  Internal
        # blank lines (paragraph breaks) and speaker-label line breaks
        # are preserved.
        body = "\n".join(line.rstrip() for line in body.split("\n")).strip()

        block = "[{} {}-{}]\n{}\n".format(
            session.get("token", "?"),
            in_tc, out_tc,
            body)
        self.clipboard_clear()
        self.clipboard_append(block)
        self.update()

        # Brief flash on the status label
        lbl = getattr(self, "_pq_status_lbl", None)
        if lbl:
            prev_text = lbl.cget("text")
            prev_fg   = lbl.cget("fg")
            lbl.config(
                text="✓ @PULL copied  [{}–{}]".format(in_tc, out_tc),
                fg=SUCCESS)
            self.after(2000,
                       lambda: lbl.config(text=prev_text, fg=prev_fg))
        return "break"

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

    # ── Optional CLI arg: a session JSON to auto-open ─────────────────────────
    # When PostBridge is launched from a Windows file association
    # ("PostBridge.exe %1" or "python main.py %1"), the session path arrives
    # as sys.argv[1].  We schedule the open after the mainloop starts so the
    # window is already visible when any error dialog appears.
    _autoload_path = None
    if len(sys.argv) > 1:
        _candidate = sys.argv[1]
        if os.path.isfile(_candidate):
            _autoload_path = _candidate

    _app = App()
    if _autoload_path:
        _app.after(0, lambda p=_autoload_path: _app._open_session(p))
    _app.mainloop()