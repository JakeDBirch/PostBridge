"""
Match Review Dialog — waveform + in/out editor for reconciled interview pulls and VO blocks.

Opens from the Step 4 review panel so the user can visually verify and adjust
the edit points found by the reconciler.  Shows a context window of the source
audio waveform with the matched region highlighted and draggable IN/OUT markers.
"""

import hashlib
import logging
import os
import tempfile
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor

_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "match_review_log.txt")
logging.basicConfig(
    filename=_LOG_PATH,
    filemode="w",
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)s  %(message)s",
)
_log = logging.getLogger("match_review")

from config import (BG, SURF, SURF2, SURF3, BORDER, ACCENT, TEXT, SUB,
                    SUCCESS, WARN, ERR, FB, FBT, FH,
                    SNAP_IN_OFFSET, SNAP_OUT_OFFSET)
from engines import extract_audio_segment, extract_mono_pcm, get_media_duration

# ── Waveform disk cache ────────────────────────────────────────────────────────
# Keyed on file path + mtime + size so stale entries are automatically ignored.
# Stores 8 kHz float32 display samples (normalized to 0.85 peak) + raw src peak.

_WAVE_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "waveform_cache")

def _wave_cache_key(audio_path):
    try:
        st  = os.stat(audio_path)
        raw = "{}|{}|{}".format(os.path.abspath(audio_path),
                                 st.st_size, st.st_mtime)
        return hashlib.md5(raw.encode()).hexdigest()
    except OSError:
        return None

def _wave_cache_load(audio_path):
    """Return (samples float32, src_peak float) from disk cache, or None."""
    key = _wave_cache_key(audio_path)
    if not key:
        return None
    cache_path = os.path.join(_WAVE_CACHE_DIR, key + ".npz")
    if not os.path.exists(cache_path):
        return None
    try:
        import numpy as _np
        data = _np.load(cache_path)
        return data["samples"].astype(_np.float32), float(data["peak"])
    except Exception:
        _log.warning("corrupt waveform cache for %s — ignoring", audio_path)
        return None

def _wave_cache_save(audio_path, samples, peak):
    """Write samples and peak to disk cache (silent on error, runs in bg thread)."""
    key = _wave_cache_key(audio_path)
    if not key:
        return
    try:
        import numpy as _np
        os.makedirs(_WAVE_CACHE_DIR, exist_ok=True)
        cache_path = os.path.join(_WAVE_CACHE_DIR, key + ".npz")
        _np.savez_compressed(cache_path,
                              samples=samples.astype(_np.float32),
                              peak=_np.float32(peak))
        _log.info("waveform cached: %s", os.path.basename(cache_path))
    except Exception:
        _log.warning("failed to cache waveform for %s",
                     audio_path, exc_info=True)

# ── Constants ─────────────────────────────────────────────────────────────────
_SR          = 8000     # waveform display sample rate
_PLAYBACK_SR = 44100    # playback sample rate
_CONTEXT_S   = 6.0      # seconds of context on each side of the match
_MARKER_HIT  = 10       # pixel radius for grabbing IN/OUT markers
_CHUNK_S          = 120.0   # seconds per lazy-load chunk (for scrolling beyond preview)
_TRIGGER_S        = 15.0    # trigger next chunk when viewport is this close to a loaded edge
_AUTO_SNAP_WINDOW = 0.35    # seconds: search radius for automatic boundary snap

# ── Region colours ────────────────────────────────────────────────────────────
#   "kept"    — audio that will be included in the edit
#   "cut"     — gaps between segments (excised audio)
#   "outside" — before global IN or after global OUT

_BG_KEPT     = "#152830"   # kept-segment background (dark teal)
_BG_CUT      = "#2a1010"   # cut-gap background (dark red)
_BG_OUTSIDE  = "#1a1e21"   # outside IN/OUT background (near-black grey)

_WAVE_KEPT_FILL    = "#253a48"   # kept waveform fill
_WAVE_KEPT_LINE    = "#4a8fa8"   # kept waveform outline
_WAVE_CUT_FILL     = "#4a1515"   # cut-gap waveform fill (dark red)
_WAVE_CUT_LINE     = "#bb3535"   # cut-gap waveform outline (red)
_WAVE_OUT_FILL     = "#262c30"   # outside waveform fill (dim grey)
_WAVE_OUT_LINE     = "#3a4a54"   # outside waveform outline (dim grey)

# Legacy aliases kept so any remaining references don't break
_MATCH_FILL    = _BG_KEPT
_CUT_FILL      = _BG_CUT
_WAVE_FILL     = _WAVE_KEPT_FILL
_WAVE_OUTLINE  = _WAVE_KEPT_LINE


class MatchReviewDialog:
    """
    Waveform + edit-point review dialog for a single reconciled clip.

    Parameters
    ----------
    parent : tk.Tk
        Main application window.
    source_audio : str
        Path to the source audio file (pull transcript or VO take).
    segments : list of (float, float)
        Matched segments as [(in_s, out_s), ...] in source-file seconds.
    title : str
        Display name shown in the title bar (token name or VO id).
    quote_text : str
        The matched text — shown as context above the waveform.
    on_accept : callable(segments)
        Called with the (possibly adjusted) segments list on accept.
    fps : float
        Sequence frame rate — used for nudge step size.
    """

    def __init__(self, parent, source_audio, segments, title="",
                 quote_text="", matched_text="", words=None,
                 context_before="", context_after="",
                 scripted_tc="",
                 on_accept=None, fps=24.0):
        self._parent          = parent
        self._audio_path      = source_audio
        self._on_accept       = on_accept
        self._fps             = fps
        self._frame_s         = 1.0 / max(1.0, fps)
        self._sr              = _SR
        self._matched_text    = matched_text or ""
        self._context_before  = context_before or ""
        self._context_after   = context_after  or ""
        self._scripted_tc     = scripted_tc or ""
        self._words           = [w for w in (words or [])
                                  if isinstance(w, dict) and "start" in w]

        # Working copy — we edit the overall span (first in / last out).
        # Interior segment boundaries stay fixed; on accept we shift
        # the first-in and last-out by the same deltas the user applied.
        self._segments   = [list(s) for s in segments]  # mutable copy
        self._in_s       = self._segments[0][0]  if self._segments else 0.0
        self._out_s      = self._segments[-1][1] if self._segments else 0.0

        # Load the full source file so the user can scroll to any position
        # (the matched region may be wrong and the true edit points may be far away).
        self._ctx_start = 0.0
        _file_dur = get_media_duration(source_audio)
        if _file_dur and _file_dur > 0:
            self._ctx_dur = _file_dur
        else:
            # Fallback: generous window around the match
            self._ctx_dur = (self._out_s + _CONTEXT_S * 4) - self._ctx_start
        self._samples      = None   # numpy array populated by background thread
        self._src_peak     = None   # raw float peak of source file (for playback gain)
        self._display_gain = 1.0    # gain applied to display samples (anchors phase-2 to phase-1 scale)
        self._need_initial_zoom = True  # zoom to match region on first real canvas resize

        # Lazy-load chunk tracking — samples array is a contiguous window that grows
        # outward from the Phase-1 preview as the user scrolls.
        self._file_dur_s     = _file_dur if (_file_dur and _file_dur > 0) else 0.0
        self._loaded_start_s = 0.0   # file-time of samples[0]
        self._loaded_end_s   = 0.0   # file-time of samples[-1]
        self._loading_left   = False  # background chunk fetch in progress (left)
        self._loading_right  = False  # background chunk fetch in progress (right)

        # View state
        self._canvas_w   = 880
        self._canvas_h   = 200
        self._view_start = 0      # first visible sample (within ctx window)
        self._spp        = 1      # samples per pixel

        # Drag state: None, "in", "out", or "playhead"
        self._drag = None

        # Playhead cursor (absolute file time; starts at match IN point)
        self._playhead_s      = self._in_s
        self._playhead_anim   = None   # after() id for cursor animation
        self._playback_start_wall = None   # wall-clock time playback began
        self._playback_start_file = None   # file time playback began
        self._play_edit_mode  = None      # tk.BooleanVar — set in _build_ui
        self._play_edit_segs  = None      # [(start_s, dur_s), ...] stitched anim
        self._remove_hit_areas = []       # [(cx, cy, seg_idx), ...] merge-cut targets
        self._remove_seg_areas = []       # [(cx, cy, seg_idx), ...] delete-segment targets
        self._pre_play_pos    = None      # playhead position saved before playback starts
        self._words_draw_key  = None      # cache key for _draw_words dedup
        self._playback_end_file = None    # file-time at which playback should auto-stop

        # Silence snap
        self._silence_boundaries = []   # sorted list of all silence-edge timestamps (s)
        self._sil_starts         = []   # subset: silence onset times (speech ends here)
        self._sil_ends           = []   # subset: silence offset times (speech starts here)
        self._auto_snapped       = False  # True once auto-snap has run
        self._snap_lbl           = None   # Label widget showing snap status

        # Undo/redo stacks — each entry is deepcopy of (_segments, _in_s, _out_s)
        self._undo_stack      = []
        self._redo_stack      = []
        self._drag_undo_pushed = False    # prevent double-push per drag gesture

        # Temp dir for playback clips
        self._tmp_dir = tempfile.mkdtemp(prefix="pb_match_")

        # ── Build window ──────────────────────────────────────────────────
        self._win = tk.Toplevel(parent)
        disp_title = "MATCH REVIEW"
        if title:
            disp_title += "  \u2014  " + title
        self._win.title(disp_title)
        self._win.configure(bg=BG)
        self._win.resizable(True, True)
        self._win.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self._win.bind("<Escape>", lambda e: self._on_cancel())
        self._win.bind("<Return>",  lambda e: self._on_accept_click())

        self._win.update_idletasks()
        pw = parent.winfo_width();  ph = parent.winfo_height()
        px = parent.winfo_x();      py = parent.winfo_y()
        _sw = parent.winfo_screenwidth()
        _sh = parent.winfo_screenheight()
        w = min(1600, _sw - 60)
        h = min(1200, _sh - 40)
        self._win.geometry("{}x{}+{}+{}".format(
            w, h, max(0, px + (pw - w) // 2), max(0, py + (ph - h) // 2)))

        self._build_ui(quote_text, self._context_before, self._context_after,
                       self._scripted_tc)
        self._win.grab_set()
        self._win.focus_force()
        self._win.bind("<MouseWheel>", lambda e: "break")
        self._start_extraction()

    # ── UI ────────────────────────────────────────────────────────────────

    def _build_ui(self, quote_text, context_before="", context_after="",
                  scripted_tc=""):
        win = self._win

        # ── Script text — scrollable, capped height so controls always visible ──
        _has_any_text = quote_text or context_before or context_after
        if _has_any_text:
            import tkinter.font as _tkfont
            _font_main = _tkfont.Font(family="Segoe UI", size=12)
            _font_ctx  = _tkfont.Font(family="Segoe UI", size=11, slant="italic")

            qf = tk.Frame(win, bg=SURF2,
                          highlightbackground=BORDER, highlightthickness=1)
            qf.pack(fill="x", padx=12, pady=(10, 2))
            qt = tk.Text(qf, font=_font_main, bg=SURF2, fg=TEXT,
                         relief="flat", bd=0, padx=8, pady=6,
                         wrap="word", height=16, state="normal",
                         highlightthickness=0)

            # Tag for context text: dimmer colour, italic, slightly smaller
            import tkinter.font as _tkfont2
            _font_tc = _tkfont2.Font(family="Segoe UI", size=10)
            qt.tag_config("ctx",  font=_font_ctx,  foreground=SUB)
            qt.tag_config("rule", font=_font_ctx,  foreground=BORDER)
            qt.tag_config("main", font=_font_main, foreground=TEXT)
            qt.tag_config("tc",   font=_font_tc,   foreground=SUB)

            # Helper: truncate context to ~300 chars at a word boundary
            def _trunc(text, maxch=300):
                text = text.strip()
                if len(text) <= maxch:
                    return text
                cut = text.rfind(" ", 0, maxch)
                return text[:cut if cut > 0 else maxch] + "\u2026"

            _rule = "\u2500" * 60 + "\n"   # ────────────── divider

            # Scripted timecode header — first line so the user always has
            # a reference for where the edit was originally placed in script.
            if scripted_tc:
                qt.insert("end", "SCRIPTED  {}\n".format(scripted_tc), "tc")
                qt.insert("end", _rule, "rule")
            if context_before:
                qt.insert("end", _trunc(context_before) + "\n", "ctx")
                qt.insert("end", _rule, "rule")
            if quote_text:
                qt.insert("end", quote_text, "main")
            if context_after:
                qt.insert("end", "\n" + _rule, "rule")
                qt.insert("end", _trunc(context_after), "ctx")

            qt.config(state="disabled")
            _qsb = tk.Scrollbar(qf, orient="vertical", command=qt.yview,
                                width=10, bg=SURF2, troughcolor=SURF2,
                                highlightthickness=0, bd=0)
            qt.config(yscrollcommand=_qsb.set)
            _qsb.pack(side="right", fill="y")
            qt.pack(side="left", fill="x", expand=True)

            # Scroll so the main quote text is visible (skip past the before-context)
            if context_before and quote_text:
                try:
                    # "main" tag starts right after the rule — scroll to it
                    ranges = qt.tag_ranges("main")
                    if ranges:
                        qt.see(ranges[0])
                except Exception:
                    pass

        # ── Canvas ────────────────────────────────────────────────────────
        cf = tk.Frame(win, bg=BORDER, bd=1, relief="solid")
        cf.pack(fill="both", expand=True, padx=12, pady=(4, 2))
        self._cv = tk.Canvas(cf, bg=SURF, highlightthickness=0,
                             height=self._canvas_h)
        self._cv.pack(fill="both", expand=True)

        self._loading_lbl = tk.Label(self._cv,
                                     text="Extracting waveform\u2026",
                                     font=FBT, fg=SUB, bg=SURF)
        self._loading_lbl.place(relx=0.5, rely=0.5, anchor="center")

        self._cv.bind("<ButtonPress-1>",   self._on_press)
        self._cv.bind("<B1-Motion>",       self._on_drag_motion)
        self._cv.bind("<ButtonRelease-1>", self._on_release)
        self._cv.bind("<MouseWheel>",      self._on_scroll)
        self._cv.bind("<Configure>",       self._on_resize)

        # ── Word timeline — words positioned by timestamp below waveform ────────
        # Only shown when transcription data is available for this file.
        wf = tk.Frame(win, bg=SURF,
                      highlightbackground=BORDER, highlightthickness=1)
        if self._words:
            wf.pack(fill="x", padx=12, pady=(0, 2))
        self._word_cv = tk.Canvas(wf, bg=SURF, highlightthickness=0, height=36)
        self._word_cv.pack(fill="x")

        # ── Timecode + duration row ───────────────────────────────────────
        tc_row = tk.Frame(win, bg=BG)
        tc_row.pack(fill="x", padx=12, pady=(4, 0))

        tk.Label(tc_row, text="IN", font=FB, bg=BG, fg=SUCCESS,
                 width=4, anchor="w").pack(side="left")
        self._in_var = tk.StringVar(value=self._fmt_tc(self._in_s))
        in_ent = tk.Entry(tc_row, textvariable=self._in_var, font=FBT,
                          bg=SURF2, fg=SUCCESS, insertbackground=SUCCESS,
                          relief="flat", bd=4, width=14)
        in_ent.pack(side="left", padx=(0, 6))
        in_ent.bind("<Return>",   self._commit_in_entry)
        in_ent.bind("<FocusOut>", self._commit_in_entry)

        tk.Label(tc_row, text="OUT", font=FB, bg=BG, fg=WARN,
                 width=4, anchor="w").pack(side="left")
        self._out_var = tk.StringVar(value=self._fmt_tc(self._out_s))
        out_ent = tk.Entry(tc_row, textvariable=self._out_var, font=FBT,
                           bg=SURF2, fg=WARN, insertbackground=WARN,
                           relief="flat", bd=4, width=14)
        out_ent.pack(side="left", padx=(0, 12))
        out_ent.bind("<Return>",   self._commit_out_entry)
        out_ent.bind("<FocusOut>", self._commit_out_entry)

        self._dur_var = tk.StringVar()
        tk.Label(tc_row, textvariable=self._dur_var, font=FB,
                 bg=BG, fg=SUB).pack(side="left")

        self._snap_lbl = tk.Label(tc_row, text="", font=FB,
                                  bg=BG, fg="#5b9bd5")
        self._snap_lbl.pack(side="left", padx=(10, 0))

        # Scripted TC reference — shown on the right side of the same row so
        # the user always has the original script position for comparison.
        if scripted_tc:
            tk.Label(tc_row, text="SCRIPTED", font=FB, bg=BG, fg=SUB,
                     padx=0).pack(side="right")
            tk.Label(tc_row, text=scripted_tc, font=FBT, bg=BG, fg=SUB,
                     padx=6).pack(side="right")

        self._refresh_displays()

        # ── Controls row: zoom, SET IN/OUT  ──────────────────────────────
        nrow = tk.Frame(win, bg=BG)
        nrow.pack(fill="x", padx=12, pady=(4, 2))

        tk.Label(nrow, text="(1 frame = {:.2f}ms)".format(self._frame_s * 1000),
                 font=FB, bg=BG, fg=SUB).pack(side="left", padx=(0, 12))

        tk.Label(nrow, text="ZOOM", font=FB, bg=BG, fg=SUB).pack(side="left", padx=(0, 4))
        for zlbl, zfactor in [("−", 1.5), ("+", 1.0 / 1.5), ("FIT", None)]:
            zb = tk.Label(nrow, text=zlbl, font=FB, bg=SURF3, fg=TEXT,
                          cursor="hand2", padx=8, pady=2, bd=0,
                          highlightbackground=BORDER, highlightthickness=1)
            zb.pack(side="left", padx=1)
            zb.bind("<Enter>", lambda e, w=zb: w.config(bg=ACCENT))
            zb.bind("<Leave>", lambda e, w=zb: w.config(bg=SURF3))
            if zfactor is None:
                zb.bind("<ButtonRelease-1>", lambda e: self._zoom_fit())
            else:
                zb.bind("<ButtonRelease-1>", lambda e, f=zfactor: self._zoom_by(f))

        tk.Frame(nrow, bg=BG, width=20).pack(side="left")
        for slbl, side in [("SET IN", "in"), ("SET OUT", "out")]:
            sb = tk.Label(nrow, text=slbl, font=FB, bg=SURF3, fg=TEXT,
                          cursor="hand2", padx=8, pady=2, bd=0,
                          highlightbackground=BORDER, highlightthickness=1)
            sb.pack(side="left", padx=1)
            sb.bind("<Enter>", lambda e, w=sb: w.config(bg=ACCENT))
            sb.bind("<Leave>", lambda e, w=sb: w.config(bg=SURF3))
            sb.bind("<ButtonRelease-1>",
                    lambda e, s=side: self._set_point_from_cursor(s))

        tk.Frame(nrow, bg=BG, width=20).pack(side="left")

        # ADD CUT — splits segment at playhead position
        ac_btn = tk.Label(nrow, text="ADD CUT", font=FB, bg=SURF3, fg=WARN,
                          cursor="hand2", padx=8, pady=2, bd=0,
                          highlightbackground=BORDER, highlightthickness=1)
        ac_btn.pack(side="left", padx=1)
        ac_btn.bind("<Enter>",          lambda e: ac_btn.config(bg=ACCENT, fg=BG))
        ac_btn.bind("<Leave>",          lambda e: ac_btn.config(bg=SURF3, fg=WARN))
        ac_btn.bind("<ButtonRelease-1>", lambda e: self._add_cut())

        tk.Frame(nrow, bg=BG, width=12).pack(side="left")

        # Undo / Redo buttons
        for ulbl, ucmd in [("\u21a9 UNDO", self._undo_wf),
                           ("\u21aa REDO", self._redo_wf)]:
            ub = tk.Label(nrow, text=ulbl, font=FB, bg=SURF3, fg=TEXT,
                          cursor="hand2", padx=8, pady=2, bd=0,
                          highlightbackground=BORDER, highlightthickness=1)
            ub.pack(side="left", padx=1)
            ub.bind("<Enter>",           lambda e, w=ub: w.config(bg=ACCENT))
            ub.bind("<Leave>",           lambda e, w=ub: w.config(bg=SURF3))
            ub.bind("<ButtonRelease-1>", lambda e, f=ucmd: f())

        # Keyboard nudge still works: ←/→ = IN, Shift-←/→ = OUT
        win.bind("<Left>",        lambda e: self._nudge("in",  -1))
        win.bind("<Right>",       lambda e: self._nudge("in",   1))
        win.bind("<Shift-Left>",  lambda e: self._nudge("out", -1))
        win.bind("<Shift-Right>", lambda e: self._nudge("out",  1))

        # Spacebar = toggle play/stop
        win.bind("<space>", lambda e: self._toggle_play())

        # + / - zoom in / out — viewport centre stays fixed
        win.bind("<plus>",       lambda e: self._zoom_by(1.0/1.5))
        win.bind("<minus>",      lambda e: self._zoom_by(1.5))
        win.bind("<equal>",      lambda e: self._zoom_by(1.0/1.5))
        win.bind("<underscore>", lambda e: self._zoom_by(1.5))

        # I / O — set IN / OUT to playhead position
        win.bind("i", lambda e: self._set_point_from_cursor("in"))
        win.bind("o", lambda e: self._set_point_from_cursor("out"))

        # C — insert a cut at the current playhead position (works during playback)
        win.bind("c", lambda e: self._add_cut())

        # R / T — zoom out / in (Pro Tools convention) — viewport centre stays fixed
        win.bind("r", lambda e: self._zoom_by(1.5))
        win.bind("t", lambda e: self._zoom_by(1.0/1.5))

        # Undo/redo keyboard shortcuts — return "break" to stop propagation
        # to the Step 4 bind_all handlers on the parent window.
        win.bind("<Control-z>", lambda e: (self._undo_wf(), "break")[1])
        win.bind("<Control-Z>", lambda e: (self._redo_wf(), "break")[1])
        win.bind("<Control-Shift-z>", lambda e: (self._redo_wf(), "break")[1])
        win.bind("<Control-Shift-Z>", lambda e: (self._redo_wf(), "break")[1])

        # ── Playback row ──────────────────────────────────────────────────
        prow = tk.Frame(win, bg=BG)
        prow.pack(fill="x", padx=12, pady=(4, 2))

        def _pb(text, cmd, bg=SURF3, fg=TEXT):
            b = tk.Label(prow, text=text, font=FB, bg=bg, fg=fg,
                         cursor="hand2", padx=10, pady=4, bd=0,
                         highlightbackground=BORDER, highlightthickness=1)
            b.pack(side="left", padx=(0, 6))
            b.bind("<Enter>",          lambda e, w=b:       w.config(bg=ACCENT, fg=BG))
            b.bind("<Leave>",          lambda e, w=b, ob=bg, of=fg: w.config(bg=ob, fg=of))
            b.bind("<ButtonRelease-1>", lambda e, f=cmd: f())
            return b

        self._play_edit_mode = tk.BooleanVar(value=True)

        _pb("\u25b6 PLAY", self._play_or_edit)
        _pb("\u25b6 IN",   self._play_in)
        _pb("\u25b6 OUT",  self._play_out)
        _pb("\u25a0 STOP", self._stop)

        # SKIP CUTS checkbox — stitches segments, skipping excised gaps (ON by default)
        # Packed to far right so it's visually separate from transport buttons
        _pe_cb = tk.Checkbutton(prow, text="SKIP CUTS",
                                variable=self._play_edit_mode,
                                font=FB, bg=BG, fg=TEXT,
                                selectcolor=BG, activebackground=BG,
                                activeforeground=TEXT, cursor="hand2",
                                bd=0, padx=4, pady=4,
                                highlightthickness=0)
        _pe_cb.pack(side="right", padx=(6, 0))

        # ── Accept / Cancel ───────────────────────────────────────────────
        bot = tk.Frame(win, bg=BG)
        bot.pack(fill="x", padx=12, pady=(8, 12))

        cancel = tk.Label(bot, text="CANCEL", font=FBT, bg=SURF3, fg=TEXT,
                          cursor="hand2", padx=16, pady=6, bd=0,
                          highlightbackground=BORDER, highlightthickness=1)
        cancel.pack(side="left")
        cancel.bind("<Enter>", lambda e: cancel.config(bg="#5a2020"))
        cancel.bind("<Leave>", lambda e: cancel.config(bg=SURF3))
        cancel.bind("<ButtonRelease-1>", lambda e: self._on_cancel())

        accept = tk.Label(bot, text="ACCEPT EDIT POINTS", font=FBT,
                          bg=SUCCESS, fg=TEXT, cursor="hand2",
                          padx=16, pady=6, bd=0,
                          highlightbackground=BORDER, highlightthickness=1)
        accept.pack(side="right")
        accept.bind("<Enter>", lambda e: accept.config(bg="#4a9a51"))
        accept.bind("<Leave>", lambda e: accept.config(bg=SUCCESS))
        accept.bind("<ButtonRelease-1>", lambda e: self._on_accept_click())

    # ── Extraction ────────────────────────────────────────────────────────
    #
    # Two-phase strategy for fast initial display:
    #   Phase 1 — extract a short window around the edit points (~90 s).
    #             Shows the waveform in ~1 s regardless of file length.
    #   Phase 2 — load the full file in the background so the user can
    #             scroll anywhere.  The viewport is preserved on swap.

    _PREVIEW_S = 90.0   # seconds of audio to show immediately

    def _start_extraction(self):
        # Always run Phase 1 first so the relevant waveform region appears
        # immediately (typically ~1 s) regardless of file length or cache state.
        # Phase 2 (_phase2_worker) runs in the background and will swap in the
        # full file once ready — either from the disk cache or by re-extracting.
        ex = ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(self._extract_preview)
        fut.add_done_callback(self._preview_done)
        ex.shutdown(wait=False)

    # ── Phase 1: small window around the match ──────────────────────────

    def _extract_preview(self):
        import numpy as _np
        import wave as _wave
        pad   = 10.0
        start = max(0.0, self._in_s - pad)
        dur   = min(self._PREVIEW_S, max(0.1, self._ctx_dur - start))
        _log.info("preview extract: %s  start=%.2f  dur=%.2f",
                  self._audio_path, start, dur)
        wav = os.path.join(self._tmp_dir, "_preview.wav")
        extract_audio_segment(self._audio_path, start, dur, wav,
                              sample_rate=_SR)
        with _wave.open(wav, "rb") as wf:
            data = wf.readframes(wf.getnframes())
        arr  = _np.frombuffer(data, _np.int16).astype(_np.float32) / 32768.0
        peak = float(_np.max(_np.abs(arr))) if len(arr) else 0.0
        if peak > 1e-6:
            arr = arr / peak * 0.85
        return arr, start, peak

    def _preview_done(self, fut):
        try:
            if not self._win.winfo_exists():
                return
        except Exception:
            return
        try:
            samples, preview_start, src_peak = fut.result()
            self._src_peak     = src_peak if src_peak > 1e-6 else None
            self._display_gain = (0.85 / src_peak) if src_peak > 1e-6 else 1.0
        except Exception as exc:
            _log.exception("preview extraction failed")
            def _err():
                try:
                    self._loading_lbl.config(
                        text="Extraction failed: {}".format(exc))
                except Exception:
                    pass
            self._win.after(0, _err)
            return

        def _show():
            try:
                if not self._win.winfo_exists():
                    return
                # ctx_start shifts so sample-0 == preview_start in file time
                self._samples        = samples
                self._ctx_start      = preview_start
                self._loaded_start_s = preview_start
                self._loaded_end_s   = preview_start + len(samples) / self._sr
                self._compute_silence_boundaries()
                self._auto_snap_boundaries()
                self._refresh_displays()
                try:
                    self._loading_lbl.destroy()
                except Exception:
                    pass
                cw = self._cv.winfo_width()
                ch = self._cv.winfo_height()
                if cw > 10:
                    self._canvas_w = cw
                    self._canvas_h = max(50, ch)
                    pad = 3.0
                    view_start_s = max(self._ctx_start, self._in_s - pad)
                    view_end_s   = min(self._ctx_start + self._ctx_dur,
                                       self._out_s + pad)
                    view_dur_s   = max(0.1, view_end_s - view_start_s)
                    self._spp        = max(1, int(view_dur_s * self._sr /
                                                  self._canvas_w))
                    self._view_start = int((view_start_s - self._ctx_start) *
                                           self._sr)
                    self._need_initial_zoom = False
                else:
                    self._need_initial_zoom = True
                self._draw()
                # Phase 2: load full file in background (cache hit or re-extract).
                # Lazy chunks serve scroll requests while Phase 2 runs.
                ex2 = ThreadPoolExecutor(max_workers=1)
                fut2 = ex2.submit(self._phase2_worker)
                fut2.add_done_callback(self._full_done)
                ex2.shutdown(wait=False)
                # Kick off lazy expansion for any already-visible unloaded regions
                self._check_and_expand()
            except Exception:
                _log.exception("_show failed")

        self._win.after(0, _show)

    # ── Phase 2: full file (background, for scrolling) ──────────────────
    #
    # _phase2_worker tries the disk cache first; if the file is already cached
    # it returns the samples immediately (no re-extraction).  This means cached
    # files are handled exactly like uncached ones from the user's perspective:
    # Phase 1 preview always appears first, and the full file swaps in quietly.

    def _phase2_worker(self):
        """Try disk cache first; fall back to full file extraction."""
        cached = _wave_cache_load(self._audio_path)
        if cached is not None:
            _log.info("phase2 cache hit: %s", self._audio_path)
            samples, peak = cached
            return samples, peak, True   # True = came from cache, no re-write needed
        import numpy as _np
        _log.info("phase2 full extract: %s", self._audio_path)
        arr  = extract_mono_pcm(self._audio_path, _SR)
        peak = float(_np.max(_np.abs(arr))) if len(arr) else 0.0
        if peak > 1e-6:
            arr = arr / peak * 0.85
        return arr, peak, False          # False = freshly extracted, write cache

    def _full_done(self, fut):
        try:
            if not self._win.winfo_exists():
                return
        except Exception:
            return
        try:
            result     = fut.result()
            samples, src_peak, from_cache = result
        except Exception:
            _log.exception("full extraction failed — staying with preview")
            return

        # Only write to cache when freshly extracted (avoid re-compressing a
        # file we just read back from cache moments ago).
        if not from_cache:
            _cache_ex = ThreadPoolExecutor(max_workers=1)
            _cache_ex.submit(_wave_cache_save, self._audio_path, samples, src_peak)
            _cache_ex.shutdown(wait=False)

        def _swap():
            try:
                if not self._win.winfo_exists():
                    return
                import numpy as _np
                # Preserve the current viewport centre in absolute time
                centre_t = (self._ctx_start +
                            (self._view_start +
                             self._canvas_w * self._spp / 2) / self._sr)

                # Rescale so the visible region doesn't jump amplitude on swap.
                # Phase 1 used gain = 0.85/p1.  Phase 2 used gain = 0.85/p2.
                # samples = raw * (0.85/p2).  Multiply by (p2/p1) → raw * (0.85/p1).
                prev_peak = self._src_peak
                if (src_peak > 1e-6 and prev_peak and prev_peak > 1e-6
                        and abs(src_peak - prev_peak) / prev_peak > 0.05):
                    display_samples = samples * (src_peak / prev_peak)
                else:
                    display_samples = samples

                self._samples   = display_samples
                # Update to whole-file peak for accurate playback gain
                self._src_peak  = src_peak if src_peak > 1e-6 else self._src_peak
                self._ctx_start = 0.0
                self._ctx_dur   = len(samples) / self._sr
                # Mark the full file as loaded so lazy expansion stops
                self._loaded_start_s = 0.0
                self._loaded_end_s   = self._ctx_dur
                self._loading_left   = False
                self._loading_right  = False
                # Reposition view to the same absolute time centre
                n      = len(samples)
                new_vs = int(centre_t * self._sr) - self._canvas_w * self._spp // 2
                max_vs = max(0, n - self._canvas_w * self._spp)
                self._view_start = max(0, min(max_vs, new_vs))
                self._compute_silence_boundaries()
                self._draw()
            except Exception:
                _log.exception("_swap failed")

        self._win.after(0, _swap)

    # ── Lazy chunk loading ────────────────────────────────────────────────
    #
    # After Phase 1 the samples array covers only the preview window.  When
    # the user scrolls (or zooms) toward an unloaded edge, _check_and_expand
    # kicks off a background extraction for the next _CHUNK_S seconds of audio
    # and concatenates it once done.  Phase 2 (full-file background load) runs
    # in parallel and supersedes lazy chunks when it completes.

    def _check_and_expand(self):
        """Trigger a chunk fetch if the viewport is near an unloaded boundary."""
        if self._samples is None:
            return
        # Nothing to expand if full file is already loaded
        if (self._loaded_start_s <= 0.01
                and self._file_dur_s > 0
                and self._loaded_end_s >= self._file_dur_s - 0.5):
            return
        view_s = self._ctx_start + self._view_start / self._sr
        view_e = view_s + self._canvas_w * self._spp / self._sr
        # Left expansion
        if (not self._loading_left
                and self._loaded_start_s > 0.5
                and view_s < self._loaded_start_s + _TRIGGER_S):
            self._fetch_chunk("left")
        # Right expansion
        if (not self._loading_right
                and self._file_dur_s > 0
                and self._loaded_end_s < self._file_dur_s - 0.5
                and view_e > self._loaded_end_s - _TRIGGER_S):
            self._fetch_chunk("right")

    def _fetch_chunk(self, side):
        """Schedule a background extraction for the next chunk on the given side."""
        if side == "left":
            self._loading_left = True
            start = max(0.0, self._loaded_start_s - _CHUNK_S)
            dur   = max(0.0, self._loaded_start_s - start)
        else:
            self._loading_right = True
            start = self._loaded_end_s
            dur   = min(_CHUNK_S, max(0.0, self._file_dur_s - start))
        if dur < 0.1:
            if side == "left":
                self._loading_left  = False
            else:
                self._loading_right = False
            return
        _log.info("lazy chunk %s: start=%.2f  dur=%.2f", side, start, dur)
        ex = ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(self._extract_chunk, start, dur)
        fut.add_done_callback(
            lambda f, s=side, ss=start: self._chunk_done(f, s, ss))
        ex.shutdown(wait=False)

    def _extract_chunk(self, start_s, dur_s):
        """Extract a chunk of the source file and return a float32 display array."""
        import numpy as _np
        import wave as _wave
        wav = os.path.join(self._tmp_dir,
                           "_chunk_{:.0f}.wav".format(start_s * 10))
        extract_audio_segment(self._audio_path, start_s, dur_s, wav,
                              sample_rate=_SR)
        with _wave.open(wav, "rb") as wf:
            data = wf.readframes(wf.getnframes())
        arr  = _np.frombuffer(data, _np.int16).astype(_np.float32) / 32768.0
        # Apply the same display gain used for Phase 1 so amplitude is consistent.
        gain = self._display_gain if self._display_gain else 1.0
        arr  = arr * gain
        return arr

    def _chunk_done(self, fut, side, chunk_start_s):
        """Callback (background thread) — merge the loaded chunk into samples."""
        try:
            if not self._win.winfo_exists():
                return
        except Exception:
            return
        try:
            arr = fut.result()
        except Exception:
            _log.exception("chunk extraction failed (%s)", side)
            if side == "left":
                self._loading_left  = False
            else:
                self._loading_right = False
            return

        def _merge(arr=arr, side=side, chunk_start_s=chunk_start_s):
            try:
                import numpy as _np
                if self._samples is None:
                    return
                if side == "left":
                    # Guard: Phase 2 may have already swapped in the full file.
                    if chunk_start_s >= self._loaded_start_s - 0.1:
                        self._loading_left = False
                        return
                    new_samps = _np.concatenate([arr, self._samples])
                    old_ctx   = self._ctx_start
                    self._ctx_start      = chunk_start_s
                    self._ctx_dur        = len(new_samps) / self._sr
                    # Shift view_start so the visible region stays fixed
                    delta_samps          = int((old_ctx - chunk_start_s) * self._sr)
                    self._view_start     = self._view_start + delta_samps
                    self._samples        = new_samps
                    self._loaded_start_s = chunk_start_s
                    self._loading_left   = False
                else:
                    # Guard: Phase 2 may have already swapped in the full file.
                    if chunk_start_s < self._loaded_end_s - 0.5:
                        self._loading_right = False
                        return
                    self._samples        = _np.concatenate([self._samples, arr])
                    self._ctx_dur        = len(self._samples) / self._sr
                    self._loaded_end_s   = chunk_start_s + len(arr) / self._sr
                    self._loading_right  = False
                self._compute_silence_boundaries()
                self._draw()
                # Check if viewport is already near the next boundary
                self._check_and_expand()
            except Exception:
                _log.exception("chunk merge failed (%s)", side)
                if side == "left":
                    self._loading_left  = False
                else:
                    self._loading_right = False

        self._win.after(0, _merge)

    # ── Drawing ───────────────────────────────────────────────────────────

    def _draw(self):
        if self._samples is None:
            return
        import numpy as _np
        cv = self._cv
        cv.delete("all")
        w   = self._canvas_w
        h   = self._canvas_h
        mid = h // 2
        spp = self._spp
        vs  = self._view_start
        ve  = vs + w * spp
        n   = len(self._samples)
        scale = mid - 10

        # ── Helpers ───────────────────────────────────────────────────────
        def _t_to_px(t_abs):
            """Absolute file-time → canvas pixel."""
            samp = (t_abs - self._ctx_start) * self._sr - vs
            return samp / spp

        def _waveform_bins(px_w, view_s, view_e):
            view_s, view_e = int(view_s), int(view_e)
            total = view_e - view_s
            if total <= 0 or px_w <= 0:
                return _np.zeros(px_w), _np.zeros(px_w)
            cs = max(0, view_s); ce = min(n, view_e)
            mins = _np.zeros(px_w); maxs = _np.zeros(px_w)
            if cs >= ce:
                return mins, maxs
            px_s = int(round((cs - view_s) / total * px_w))
            px_e = min(px_w, int(round((ce - view_s) / total * px_w)))
            pw   = px_e - px_s
            if pw <= 0:
                return mins, maxs
            chunk = self._samples[cs:ce]
            nc = len(chunk)
            if nc <= pw:
                mins[px_s:px_s + nc] = chunk
                maxs[px_s:px_s + nc] = chunk
            else:
                bs   = nc // pw
                trim = bs * pw
                r    = chunk[:trim].reshape(pw, bs)
                mins[px_s:px_e] = r.min(axis=1)
                maxs[px_s:px_e] = r.max(axis=1)
            return mins, maxs

        # ── Build region list: outside / kept / cut ───────────────────────
        # Each entry: (px_lo, px_hi, zone)  where zone ∈ {"outside","kept","cut"}
        _n_segs = len(self._segments)
        _regions = []
        # Left outside (before global IN)
        _regions.append((_t_to_px(self._ctx_start), _t_to_px(self._in_s), "outside"))
        for _si, (_seg_in, _seg_out) in enumerate(self._segments):
            _eff_in  = self._in_s  if _si == 0            else _seg_in
            _eff_out = self._out_s if _si == _n_segs - 1  else _seg_out
            _regions.append((_t_to_px(_eff_in), _t_to_px(_eff_out), "kept"))
            if _si < _n_segs - 1:
                # Gap between this segment's raw out and next segment's raw in
                _regions.append((_t_to_px(_seg_out),
                                  _t_to_px(self._segments[_si + 1][0]), "cut"))
        # Right outside (after global OUT)
        _regions.append((_t_to_px(self._out_s),
                          _t_to_px(self._ctx_start + self._ctx_dur), "outside"))

        _ZONE_BG   = {"kept": _BG_KEPT,  "cut": _BG_CUT,  "outside": _BG_OUTSIDE}
        _ZONE_FILL = {"kept": _WAVE_KEPT_FILL, "cut": _WAVE_CUT_FILL,
                      "outside": _WAVE_OUT_FILL}
        _ZONE_LINE = {"kept": _WAVE_KEPT_LINE, "cut": _WAVE_CUT_LINE,
                      "outside": _WAVE_OUT_LINE}

        # ── Background pass ───────────────────────────────────────────────
        # Start with the "outside" colour, then paint each kept / cut strip.
        cv.create_rectangle(0, 0, w, h, fill=_BG_OUTSIDE, outline="")
        for _px_lo, _px_hi, _zone in _regions:
            if _zone == "outside":
                continue
            _rx0 = max(0, int(_px_lo)); _rx1 = min(w, int(_px_hi) + 1)
            if _rx1 > _rx0:
                cv.create_rectangle(_rx0, 0, _rx1, h,
                                     fill=_ZONE_BG[_zone], outline="")

        # ── Waveform pass — one polygon per region ─────────────────────────
        b_min, b_max = _waveform_bins(w, vs, ve)

        def _region_poly(px_lo, px_hi):
            """Build a closed polygon for pixel columns [px_lo, px_hi)."""
            xi = max(0, int(px_lo))
            xe = min(w, int(px_hi) + 1)
            if xe <= xi:
                return []
            pts = []
            for i in range(xi, xe):
                pts.extend([i, int(mid - b_max[i] * scale)])
            for i in range(xe - 1, xi - 1, -1):
                pts.extend([i, int(mid - b_min[i] * scale)])
            return pts

        for _px_lo, _px_hi, _zone in _regions:
            _pts = _region_poly(_px_lo, _px_hi)
            if len(_pts) >= 6:
                cv.create_polygon(_pts,
                                   fill=_ZONE_FILL[_zone],
                                   outline=_ZONE_LINE[_zone], width=1)

        # ── Silence snap tick marks ────────────────────────────────────────
        # Hide when zoomed out past ~100 ms/pixel — boundaries cluster too closely.
        if self._silence_boundaries and self._spp / self._sr <= 0.1:
            _tick_h = max(6, h // 8)
            for _sb in self._silence_boundaries:
                _spx = _t_to_px(_sb)
                if 0 <= _spx < w:
                    cv.create_line(_spx, 0, _spx, _tick_h,
                                   fill="#556070", width=1)
                    cv.create_line(_spx, h - _tick_h, _spx, h,
                                   fill="#556070", width=1)

        # ── IN / OUT marker lines ──────────────────────────────────────────
        for t_abs, color, label in [
            (self._in_s,  SUCCESS, "IN"),
            (self._out_s, WARN,    "OUT"),
        ]:
            px = _t_to_px(t_abs)
            if -_MARKER_HIT <= px <= w + _MARKER_HIT:
                cv.create_line(px, 0, px, h, fill=color, width=2)
                lbl_x = px + 4
                anchor = "w"
                if px > w - 40:
                    lbl_x = px - 4; anchor = "e"
                cv.create_text(lbl_x, 14, text=label, fill=color,
                               font=("Segoe UI", 8, "bold"), anchor=anchor)

        # ── Multi-segment interior boundaries — draggable, removable ──────
        self._remove_hit_areas = []
        if len(self._segments) > 1:
            for i, (seg_in, seg_out) in enumerate(self._segments):
                if i > 0:
                    px = _t_to_px(seg_in)
                    if -_MARKER_HIT <= px <= w + _MARKER_HIT:
                        cv.create_line(px, 0, px, h, fill=WARN, width=2)
                        cv.create_text(px + 4, 22, text="\u25b6 CUT IN",
                                       fill=WARN,
                                       font=("Segoe UI", 8, "bold"), anchor="w")
                        # "× merge" remove button
                        _rm = cv.create_text(px + 4, 36, text="\u00d7 merge",
                                             fill=SUB,
                                             font=("Segoe UI", 7), anchor="w",
                                             tags=("rm_in_{}".format(i),))
                        cv.tag_bind("rm_in_{}".format(i), "<Enter>",
                                    lambda e, t=_rm: cv.itemconfig(t, fill=ERR))
                        cv.tag_bind("rm_in_{}".format(i), "<Leave>",
                                    lambda e, t=_rm: cv.itemconfig(t, fill=SUB))
                        self._remove_hit_areas.append((px + 4, 36, i))
                if i < len(self._segments) - 1:
                    px = _t_to_px(seg_out)
                    if -_MARKER_HIT <= px <= w + _MARKER_HIT:
                        cv.create_line(px, 0, px, h, fill=WARN, width=2)
                        cv.create_text(px - 4, 22, text="CUT OUT \u25c4",
                                       fill=WARN,
                                       font=("Segoe UI", 8, "bold"), anchor="e")

        # ── Segment delete buttons — one per kept segment ─────────────────
        # Only shown when there is more than one segment (so deleting one
        # still leaves at least one behind).
        self._remove_seg_areas = []
        if len(self._segments) > 1:
            for _si, (_seg_in, _seg_out) in enumerate(self._segments):
                _eff_in  = self._in_s  if _si == 0            else _seg_in
                _eff_out = self._out_s if _si == _n_segs - 1  else _seg_out
                _sx = _t_to_px(_eff_in)
                _ex = _t_to_px(_eff_out)
                _seg_w = _ex - _sx
                _cx    = (_sx + _ex) / 2
                # Only draw if the segment is wide enough to fit the label
                if _seg_w > 80 and 0 <= _cx < w:
                    _tag = "rm_seg_{}".format(_si)
                    _cy  = mid + 28
                    _rm  = cv.create_text(
                        int(_cx), _cy,
                        text="\u00d7 remove segment",
                        fill=ERR,
                        font=("Segoe UI", 8, "bold"),
                        anchor="center",
                        tags=(_tag,))
                    cv.tag_bind(_tag, "<Enter>",
                                lambda e, t=_rm: cv.itemconfig(t, fill="#ff5555"))
                    cv.tag_bind(_tag, "<Leave>",
                                lambda e, t=_rm: cv.itemconfig(t, fill=ERR))
                    cv.tag_bind(_tag, "<ButtonRelease-1>",
                                lambda e, i=_si: self._remove_segment(i))
                    self._remove_seg_areas.append((int(_cx), _cy, _si))

        # ── Playhead cursor ────────────────────────────────────────────────
        ph_px = _t_to_px(self._playhead_s)
        if -2 <= ph_px <= w + 2:
            # Glow amber when a drag is currently snapped to it
            _snap_active = (self._drag and self._drag != "playhead" and
                            abs(self._playhead_s -
                                self._px_to_t(getattr(self, "_last_drag_x", -999)))
                            <= 12 * self._spp / self._sr)
            _ph_col = "#ffaa00" if _snap_active else "#ffffff"
            _ph_w   = 2         if _snap_active else 1
            cv.create_line(ph_px, 0, ph_px, h,
                           fill=_ph_col, width=_ph_w, dash=(3, 3),
                           tags=("playhead",))
            cv.create_polygon(ph_px - 5, 0, ph_px + 5, 0, ph_px, 10,
                               fill=_ph_col, outline="",
                               tags=("playhead",))

        # ── Centre line ────────────────────────────────────────────────────
        cv.create_line(0, mid, w, mid, fill=BORDER, dash=(2, 4))

        # ── Time axis ─────────────────────────────────────────────────────
        total_view_s = (w * spp) / self._sr
        for tick_s in [0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60]:
            if total_view_s / tick_s <= 20:
                break
        abs_start_s  = self._ctx_start + vs / self._sr
        first_tick   = int(abs_start_s / tick_s) * tick_s
        t = first_tick
        while True:
            px = int(((t - self._ctx_start) * self._sr - vs) / spp)
            if px > w:
                break
            if px >= 0:
                cv.create_line(px, h - 18, px, h - 12, fill=SUB)
                m   = int(t) // 60;  sec = t - m * 60
                lbl = ("{}:{:05.2f}".format(m, sec) if tick_s < 1
                       else "{}:{:02d}".format(m, int(sec)))
                cv.create_text(px, h - 8, text=lbl, fill=SUB,
                               font=("Segoe UI", 8), anchor="s")
            t += tick_s

        # ── Unloaded region indicators ─────────────────────────────────────
        # Show a dark overlay with a "loading…" label for portions of the
        # viewport that haven't been fetched yet (lazy chunk in progress).
        _view_s = self._ctx_start + vs / self._sr
        _view_e = self._ctx_start + ve / self._sr
        if _view_s < self._loaded_start_s - 0.1:
            _unl_px = max(0, int(_t_to_px(self._loaded_start_s)))
            if _unl_px > 0:
                cv.create_rectangle(0, 0, _unl_px, h, fill="#111111", outline="")
                cv.create_text(_unl_px // 2, h // 2,
                               text="\u27f3  loading\u2026",
                               fill=SUB, font=("Segoe UI", 9, "italic"))
        if _view_e > self._loaded_end_s + 0.1:
            _unl_px = min(w, max(0, int(_t_to_px(self._loaded_end_s))))
            if _unl_px < w:
                cv.create_rectangle(_unl_px, 0, w, h, fill="#111111", outline="")
                cv.create_text(_unl_px + (w - _unl_px) // 2, h // 2,
                               text="\u27f3  loading\u2026",
                               fill=SUB, font=("Segoe UI", 9, "italic"))

        self._draw_words()
        # Trigger lazy expansion if viewport has scrolled near an unloaded edge
        self._check_and_expand()

    def _draw_words(self):
        """Draw transcribed words on the word-timeline canvas at their time positions."""
        cv = self._word_cv
        if not self._words:
            cv.delete("all")
            return
        _key = (tuple(tuple(s) for s in self._segments),
                self._in_s, self._out_s, self._view_start, self._spp, self._canvas_w)
        if _key == self._words_draw_key:
            return
        self._words_draw_key = _key
        cv.delete("all")
        try:
            w = cv.winfo_width()
        except Exception:
            w = self._canvas_w
        if w < 10:
            w = self._canvas_w

        # Fill with same cut/keep colouring as the waveform
        cv.create_rectangle(0, 0, w, 36, fill=_CUT_FILL, outline="")
        _nw = len(self._segments)
        for _wi, (_win, _wout) in enumerate(self._segments):
            _ei  = self._in_s  if _wi == 0          else _win
            _eo  = self._out_s if _wi == _nw - 1    else _wout
            _wp  = self._t_to_px(_ei);  _wq = self._t_to_px(_eo)
            if _wp < w and _wq > 0:
                cv.create_rectangle(max(0, _wp), 0, min(w, _wq), 36,
                                     fill=_MATCH_FILL, outline="")

        # Measure real character widths using tkinter Font so the overlap
        # check is accurate regardless of DPI or font rendering.
        import tkinter.font as _tkfont
        _FONT    = ("Segoe UI", 8)
        _fnt_obj = _tkfont.Font(family="Segoe UI", size=8)
        _min_gap = 4   # minimum pixel gap between drawn words

        # ── Build candidate list (visible words only) ─────────────────────
        # Each entry: [global_index, px, text_w, text, colour, is_priority]
        cands = []
        for gi, wd in enumerate(self._words):
            t  = wd.get("start", 0)
            px = self._t_to_px(t)
            if px < -5 or px > w + 5:
                continue
            text = wd.get("word", "")
            if not text:
                continue
            cands.append([gi, px, _fnt_obj.measure(text), text,
                          SUCCESS if self._in_s <= t <= self._out_s else SUB,
                          False])   # is_priority filled below

        # ── Mark sentence boundaries as priority ──────────────────────────
        # Sentence-end: word (stripped of trailing quotes) ends with . ! ?
        # Sentence-start: the word immediately following a sentence-end.
        sent_end_gi = {c[0] for c in cands
                       if c[3].strip().rstrip('"\'').endswith(('.', '!', '?'))}
        for c in cands:
            if c[0] in sent_end_gi or (c[0] - 1) in sent_end_gi:
                c[5] = True   # is_priority

        # ── Precompute next-priority pixel for each candidate (lookahead) ─
        # Lets normal words yield space to an upcoming priority word.
        next_prio_px = [float('inf')] * len(cands)
        npp = float('inf')
        for idx in range(len(cands) - 1, -1, -1):
            if cands[idx][5]:
                npp = cands[idx][1]
            next_prio_px[idx] = npp

        # ── Priority-aware greedy placement ───────────────────────────────
        # Priority words: always drawn if they fit.
        # Normal words: only drawn if they won't crowd out the next priority word.
        prev_right = -999.0
        for idx, (gi, px, tw, text, colour, is_prio) in enumerate(cands):
            if px < prev_right + _min_gap:
                continue
            if is_prio:
                cv.create_text(int(px), 18, text=text, fill=colour,
                               font=_FONT, anchor="w")
                prev_right = px + tw
            else:
                # Skip if placing this word would leave no room for the next
                # priority word (i.e. our right edge would overlap its left).
                if next_prio_px[idx] >= px + tw + _min_gap:
                    cv.create_text(int(px), 18, text=text, fill=colour,
                                   font=_FONT, anchor="w")
                    prev_right = px + tw

    def _draw_playhead_only(self):
        """Redraw only the playhead — called every 40 ms during playback.
        Avoids the full delete("all") + redraw cycle for a static waveform."""
        cv = self._cv
        cv.delete("playhead")
        ph_px = self._t_to_px(self._playhead_s)
        w = self._canvas_w
        h = self._canvas_h
        if -2 <= ph_px <= w + 2:
            _snap_active = (self._drag and self._drag != "playhead" and
                            abs(self._playhead_s -
                                self._px_to_t(getattr(self, "_last_drag_x", -999)))
                            <= 12 * self._spp / self._sr)
            _ph_col = "#ffaa00" if _snap_active else "#ffffff"
            _ph_w   = 2         if _snap_active else 1
            cv.create_line(ph_px, 0, ph_px, h,
                           fill=_ph_col, width=_ph_w, dash=(3, 3),
                           tags=("playhead",))
            cv.create_polygon(ph_px - 5, 0, ph_px + 5, 0, ph_px, 10,
                               fill=_ph_col, outline="",
                               tags=("playhead",))

    # ── Interaction ───────────────────────────────────────────────────────

    def _t_to_px(self, t_abs):
        samp = (t_abs - self._ctx_start) * self._sr - self._view_start
        return samp / self._spp

    def _px_to_t(self, px):
        samp = self._view_start + px * self._spp
        return self._ctx_start + samp / self._sr

    def _on_press(self, event):
        if self._samples is None:
            return
        self._drag_undo_pushed = False
        # Check "× merge" buttons first (merge adjacent segments)
        for cx, cy, seg_idx in self._remove_hit_areas:
            if abs(event.x - cx) <= 28 and abs(event.y - cy) <= 8:
                self._remove_cut(seg_idx)
                self._drag = None
                return
        # Check "× remove segment" buttons (delete an entire kept segment)
        for cx, cy, seg_idx in self._remove_seg_areas:
            if abs(event.x - cx) <= 50 and abs(event.y - cy) <= 8:
                self._remove_segment(seg_idx)
                self._drag = None
                return
        in_px  = self._t_to_px(self._in_s)
        out_px = self._t_to_px(self._out_s)
        ph_px  = self._t_to_px(self._playhead_s)
        if abs(event.x - in_px) <= _MARKER_HIT:
            self._drag = "in"
        elif abs(event.x - out_px) <= _MARKER_HIT:
            self._drag = "out"
        else:
            # Check interior segment boundaries before falling through to playhead
            n = len(self._segments)
            interior_hit = None
            for i in range(n):
                if i > 0:
                    px = self._t_to_px(self._segments[i][0])
                    if abs(event.x - px) <= _MARKER_HIT:
                        interior_hit = ("seg_in", i)
                        break
                if i < n - 1:
                    px = self._t_to_px(self._segments[i][1])
                    if abs(event.x - px) <= _MARKER_HIT:
                        interior_hit = ("seg_out", i)
                        break
            if interior_hit:
                self._drag = interior_hit
            elif abs(event.x - ph_px) <= _MARKER_HIT or event.y <= 14:
                self._drag = "playhead"
            else:
                # Click anywhere else: move playhead; if playing, jump to new position
                new_t = max(0.0, min(self._px_to_t(event.x), self._ctx_dur))
                self._playhead_s = new_t
                self._drag = "playhead"
                was_playing = self._playback_start_wall is not None
                self._draw()
                if was_playing:
                    if self._play_edit_mode and self._play_edit_mode.get():
                        self._play_or_edit()
                    else:
                        remain = max(0.5, self._ctx_dur - new_t)
                        self._play(new_t, min(remain, 30.0))
                    # Return point is the clicked position, not the original start
                    self._pre_play_pos = new_t

    def _compute_silence_boundaries(self):
        """Build sorted lists of silence-region edge timestamps for snap points.

        _sil_starts: times where a silence begins (= speech just ended → OUT snap target)
        _sil_ends:   times where a silence ends   (= speech just began → IN snap target)
        _silence_boundaries: combined list used for tick rendering
        """
        import numpy as _np
        self._silence_boundaries = []
        self._sil_starts         = []
        self._sil_ends           = []
        if self._samples is None or len(self._samples) == 0:
            return
        sr        = self._sr
        win       = max(1, int(sr * 0.02))   # 20 ms windows
        threshold = 0.02                      # RMS threshold (audio is peak-normalised ~0.85)
        n         = len(self._samples)
        sil_starts = []
        sil_ends   = []
        in_sil    = False
        sil_start = 0.0
        for i in range(0, n, win):
            chunk = self._samples[i : i + win]
            rms   = float(_np.sqrt(_np.mean(chunk ** 2)))
            t     = self._ctx_start + i / sr
            if rms < threshold and not in_sil:
                sil_start = t
                in_sil    = True
            elif rms >= threshold and in_sil:
                sil_end = t
                if sil_end - sil_start >= 0.1:   # only silences ≥ 100 ms
                    sil_starts.append(sil_start)
                    sil_ends.append(sil_end)
                in_sil = False
        if in_sil:
            sil_end = self._ctx_start + n / sr
            if sil_end - sil_start >= 0.1:
                sil_starts.append(sil_start)
                sil_ends.append(sil_end)
        self._sil_starts         = sorted(sil_starts)
        self._sil_ends           = sorted(sil_ends)
        self._silence_boundaries = sorted(sil_starts + sil_ends)

    def _auto_snap_boundaries(self):
        """Snap IN/OUT to the nearest appropriate silence boundary automatically.

        Runs once after Phase 1 audio loads, before the user sees the waveform.
        Uses directional search so IN snaps to speech onsets and OUT to speech
        offsets, biased to avoid cutting into the clip rather than away from it.

        Does nothing if already run, or if silence data isn't available.
        """
        if self._auto_snapped:
            return
        if not self._sil_ends and not self._sil_starts:
            return

        win  = _AUTO_SNAP_WINDOW
        orig_in  = self._in_s
        orig_out = self._out_s
        new_in   = orig_in
        new_out  = orig_out

        # IN → nearest silence END (= speech onset) in [in-win, in+win/3],
        # then advance by SNAP_IN_OFFSET to land after the initial breath.
        in_cands = [b for b in self._sil_ends
                    if orig_in - win <= b <= orig_in + win / 3]
        if in_cands:
            new_in = min(in_cands, key=lambda b: abs(b - orig_in)) + SNAP_IN_OFFSET

        # OUT → nearest silence START (= speech offset) in [out-win/3, out+win],
        # then advance by SNAP_OUT_OFFSET to include the tail of the last word.
        out_cands = [b for b in self._sil_starts
                     if orig_out - win / 3 <= b <= orig_out + win]
        if out_cands:
            new_out = min(out_cands, key=lambda b: abs(b - orig_out)) + SNAP_OUT_OFFSET

        # Safety: don't let snap collapse or invert the region
        if new_in >= new_out or (new_out - new_in) < 0.1:
            return

        changed = (new_in != orig_in or new_out != orig_out)
        if not changed:
            return

        self._in_s  = new_in
        self._out_s = new_out
        if self._segments:
            s0 = self._segments[0]
            sl = self._segments[-1]
            self._segments[0]  = [new_in,  s0[1]]
            self._segments[-1] = [sl[0],   new_out]

        self._auto_snapped = True
        try:
            if self._snap_lbl and self._snap_lbl.winfo_exists():
                self._snap_lbl.config(text="◈ boundary-snapped")
        except Exception:
            pass

    def _snap_to_silence(self, t):
        """Return t snapped to nearest silence boundary if within ~8 pixels."""
        if not self._silence_boundaries:
            return t
        radius = 8 * self._spp / self._sr   # 8 px at current zoom
        best   = min(self._silence_boundaries, key=lambda b: abs(b - t))
        return best if abs(best - t) <= radius else t

    def _snap(self, t):
        """Snap t to silence boundaries or the playhead, whichever is closer.

        Playhead snap uses a slightly larger radius (12 px) so it feels
        magnetic.  Silence snap runs second so the playhead wins ties.
        """
        radius_sil = 8  * self._spp / self._sr   # pixels → seconds
        radius_ph  = 12 * self._spp / self._sr

        best_t    = t
        best_dist = float("inf")

        # Silence boundaries
        if self._silence_boundaries:
            sb = min(self._silence_boundaries, key=lambda b: abs(b - t))
            d  = abs(sb - t)
            if d <= radius_sil and d < best_dist:
                best_t, best_dist = sb, d

        # Playhead cursor (skip if we're dragging the playhead itself)
        if self._drag != "playhead":
            d = abs(self._playhead_s - t)
            if d <= radius_ph and d < best_dist:
                best_t, best_dist = self._playhead_s, d

        return best_t

    def _on_drag_motion(self, event):
        if not self._drag or self._samples is None:
            return
        # Push undo once per drag gesture (not for playhead moves)
        if not self._drag_undo_pushed and self._drag != "playhead":
            self._push_undo()
            self._drag_undo_pushed = True
        self._last_drag_x = event.x
        t = self._snap(max(0.0, self._px_to_t(event.x)))
        if self._drag == "in":
            self._in_s = min(t, self._out_s - self._frame_s)
            self._refresh_displays()
        elif self._drag == "out":
            self._out_s = max(t, self._in_s + self._frame_s)
            self._refresh_displays()
        elif isinstance(self._drag, tuple):
            kind, idx = self._drag
            n = len(self._segments)
            if kind == "seg_in" and 0 < idx < n:
                prev_out = self._segments[idx - 1][1]
                this_out = self._segments[idx][1]
                self._segments[idx][0] = max(
                    prev_out + self._frame_s,
                    min(t, this_out - self._frame_s))
            elif kind == "seg_out" and 0 <= idx < n - 1:
                this_in  = self._segments[idx][0]
                next_in  = self._segments[idx + 1][0]
                self._segments[idx][1] = max(
                    this_in + self._frame_s,
                    min(t, next_in - self._frame_s))
        else:  # playhead
            self._playhead_s = min(t, self._ctx_dur)
        self._draw()

    def _on_release(self, event):
        self._drag = None

    def _on_scroll(self, event):
        # Scroll wheel always zooms; Shift+scroll pans.
        if event.state & 0x1:   # Shift held — pan
            self._pan(event)
        else:                    # plain scroll — zoom in/out
            self._zoom_by(1.0 / 1.5 if event.delta > 0 else 1.5, event.x)
        return "break"

    def _pan(self, event):
        if self._samples is None:
            return
        delta    = -event.delta // 120 * self._spp * 50
        n        = len(self._samples)
        max_vs   = max(0, n - self._canvas_w * self._spp)
        self._view_start = max(0, min(max_vs, self._view_start + delta))
        self._draw()
        self._check_and_expand()

    def _zoom_by(self, factor, mouse_x=None):
        if self._samples is None:
            return
        n   = len(self._samples)
        w   = max(1, self._canvas_w)
        old = self._spp
        # Use round() to avoid int() flooring keeping the value identical at
        # small spp values (e.g. int(1 * 1.5) == 1 → no movement).
        # Then guarantee at least a 1-step change so every scroll does something.
        new = max(1, round(old * factor))
        if new == old:
            new = old + 1 if factor > 1.0 else max(1, old - 1)
        new = max(1, min(max(1, n // w), new))
        if new == old:
            return
        max_vs = max(0, n - w * new)
        # Always anchor on the explicit pixel passed in (scroll wheel uses the
        # cursor, keyboard / buttons use the canvas centre so whatever is
        # currently visible stays put).
        anchor_px = mouse_x if mouse_x is not None else w / 2
        anchor    = self._view_start + anchor_px * old
        self._view_start = max(0, min(max_vs, int(anchor - anchor_px * new)))
        self._spp = new
        self._draw()
        self._check_and_expand()

    def _zoom_fit(self):
        """Zoom so the IN/OUT region fills the canvas with context padding."""
        if self._samples is None:
            return
        pad = 3.0  # seconds of context on each side of the match
        w   = max(1, self._canvas_w)
        view_start_s = max(self._ctx_start, self._in_s - pad)
        view_end_s   = min(self._ctx_start + self._ctx_dur, self._out_s + pad)
        view_dur_s   = max(0.1, view_end_s - view_start_s)
        self._spp        = max(1, int(view_dur_s * self._sr / w))
        self._view_start = int((view_start_s - self._ctx_start) * self._sr)
        self._draw()

    def _zoom_to(self, t_center, window_s=10.0):
        """Zoom so that window_s seconds is visible, centered on t_center."""
        if self._samples is None:
            return
        w = max(1, self._canvas_w)
        self._spp = max(1, int(window_s * self._sr / w))
        vs = int((t_center - window_s * 0.35 - self._ctx_start) * self._sr)
        n  = len(self._samples)
        self._view_start = max(0, min(n - w * self._spp, vs))
        self._draw()

    def _play_in(self):
        """Zoom to IN; stitched if PLAY EDIT on, else raw from IN."""
        self._commit_in_entry()
        self._zoom_to(self._in_s, window_s=10.0)
        if self._play_edit_mode.get():
            segs = self._build_stitched_segs()
            if segs:
                self._play_stitched(segs)
        else:
            self._play(self._in_s, 6.0)

    def _play_out(self):
        """Zoom to OUT and play up to 3s pre-roll, stopping exactly at the out point."""
        self._commit_out_entry()
        self._zoom_to(self._out_s, window_s=10.0)
        pre = min(3.0, self._out_s)
        self._play(self._out_s - pre, pre)

    def _on_resize(self, event):
        self._canvas_w = max(100, event.width)
        self._canvas_h = max(50, event.height)
        # On the first resize with real dimensions, zoom to the match region.
        # This handles the cache-hit path where winfo_width() was 1 at load time.
        if self._need_initial_zoom and event.width > 100 and self._samples is not None:
            self._need_initial_zoom = False
            pad = 3.0
            view_start_s = max(self._ctx_start, self._in_s - pad)
            view_end_s   = min(self._ctx_start + self._ctx_dur, self._out_s + pad)
            view_dur_s   = max(0.1, view_end_s - view_start_s)
            self._spp        = max(1, int(view_dur_s * self._sr / self._canvas_w))
            self._view_start = int((view_start_s - self._ctx_start) * self._sr)
        self._draw()

    def _nudge(self, side, frames):
        delta = frames * self._frame_s
        if side == "in":
            self._in_s = max(0.0, self._in_s + delta)
            if self._in_s >= self._out_s - self._frame_s:
                self._in_s = self._out_s - self._frame_s
        else:
            self._out_s = max(self._in_s + self._frame_s, self._out_s + delta)
        self._refresh_displays()
        self._draw()

    def _commit_in_entry(self, event=None):
        t = self._parse_tc(self._in_var.get())
        if t is not None:
            self._in_s = max(0.0, t)
            if self._in_s >= self._out_s:
                self._out_s = self._in_s + 2.0   # drag OUT along with IN
            self._refresh_displays()
            self._draw()

    def _commit_out_entry(self, event=None):
        t = self._parse_tc(self._out_var.get())
        if t is not None:
            self._out_s = max(self._in_s + self._frame_s, t)
            self._refresh_displays()
            self._draw()

    def _refresh_displays(self):
        self._in_var.set(self._fmt_tc(self._in_s))
        self._out_var.set(self._fmt_tc(self._out_s))
        dur = self._out_s - self._in_s
        self._dur_var.set("({})".format(self._fmt_tc(dur)))

    # ── Playback ──────────────────────────────────────────────────────────

    def _toggle_play(self):
        """Spacebar handler — stop if playing, play/edit if stopped."""
        if self._playback_start_wall is not None:
            self._stop()
        else:
            self._play_or_edit()

    def _play_or_edit(self):
        """▶ PLAY — stitched from cursor if SKIP CUTS on, else raw from cursor.

        With SKIP CUTS on, even a single segment is bounded by IN/OUT so
        playback never drifts past the edit region.
        """
        if self._play_edit_mode.get():
            cursor = self._playhead_s
            n = len(self._segments)
            segs_to_play = []
            for i, seg in enumerate(self._segments):
                s_in  = self._in_s  if i == 0       else seg[0]
                s_out = self._out_s if i == n - 1   else seg[1]
                if cursor >= s_out:
                    continue
                actual_start = max(cursor, s_in)
                dur = s_out - actual_start
                if dur > 0.01:
                    segs_to_play.append((actual_start, dur))
            if not segs_to_play:
                segs_to_play = self._build_stitched_segs()
            self._play_stitched(segs_to_play)
        else:
            remain = max(0.5, self._ctx_dur - self._playhead_s)
            self._play(self._playhead_s, min(remain, 60.0))

    def _build_stitched_segs(self):
        """Return [(start_s, dur_s), ...] for each kept segment."""
        n = len(self._segments)
        result = []
        for i, seg in enumerate(self._segments):
            s_in  = self._in_s  if i == 0       else seg[0]
            s_out = self._out_s if i == n - 1   else seg[1]
            dur = s_out - s_in
            if dur > 0.01:
                result.append((s_in, dur))
        return result

    def _play_stitched(self, seg_list):
        """Extract seg_list segments, concatenate PCM, play, animate playhead."""
        if not seg_list:
            return
        import time as _time
        self._stop()
        self._pre_play_pos = self._playhead_s   # save AFTER stop so it isn't cleared
        self._playback_end_file = seg_list[-1][0] + seg_list[-1][1]

        def _do():
            import numpy as _np
            import wave as _wave
            chunks = []
            for seg_start, seg_dur in seg_list:
                wav = os.path.join(self._tmp_dir,
                                   "_st{:.3f}.wav".format(seg_start))
                extract_audio_segment(self._audio_path, seg_start, seg_dur,
                                      wav, sample_rate=_PLAYBACK_SR)
                with _wave.open(wav, "rb") as wf:
                    data = wf.readframes(wf.getnframes())
                chunks.append(_np.frombuffer(data, _np.int16)
                               .astype(_np.float32) / 32768.0)
            combined = (_np.concatenate(chunks) if chunks
                        else _np.zeros(0, dtype=_np.float32))
            peak = float(_np.max(_np.abs(combined))) if len(combined) else 0.0
            if peak > 1e-6:
                combined = combined / peak * 0.85
            pcm = (combined * 32767).astype(_np.int16)
            out_wav = os.path.join(self._tmp_dir, "_play_edit.wav")
            with _wave.open(out_wav, "wb") as wf:
                wf.setnchannels(1); wf.setsampwidth(2)
                wf.setframerate(_PLAYBACK_SR)
                wf.writeframes(pcm.tobytes())
            return out_wav

        def _done(fut):
            try:
                path = fut.result()
            except Exception:
                _log.exception("play_stitched failed")
                return
            def _do_play():
                try:
                    import winsound
                    winsound.PlaySound(path,
                                      winsound.SND_FILENAME | winsound.SND_ASYNC)
                except Exception:
                    _log.exception("winsound failed")
                    return
                self._playhead_s          = seg_list[0][0]
                self._playback_start_wall = _time.perf_counter()
                self._playback_start_file = seg_list[0][0]
                self._play_edit_segs      = list(seg_list)
                self._animate_playhead()
            try:
                self._win.after(0, _do_play)
            except Exception:
                pass

        ex = ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(_do)
        fut.add_done_callback(_done)
        ex.shutdown(wait=False)

    def _remove_cut(self, seg_idx):
        """Merge segment (seg_idx-1) and (seg_idx), removing the cut between them."""
        n = len(self._segments)
        if seg_idx <= 0 or seg_idx >= n:
            return
        self._push_undo()
        # Extend the previous segment's out to the next segment's out, then delete next
        self._segments[seg_idx - 1][1] = self._segments[seg_idx][1]
        del self._segments[seg_idx]
        self._draw()

    def _remove_segment(self, seg_idx):
        """Delete an entire kept segment, expanding the surrounding cut gaps.

        If the segment is first or last, the global IN or OUT point is moved
        to the adjacent segment's boundary so no orphaned handle is left.
        """
        n = len(self._segments)
        if n <= 1:
            return   # can't delete the only remaining segment
        self._push_undo()
        if seg_idx == 0:
            # New first segment inherits the global IN
            self._in_s = self._segments[1][0]
        elif seg_idx == n - 1:
            # New last segment inherits the global OUT
            self._out_s = self._segments[n - 2][1]
        del self._segments[seg_idx]
        self._refresh_displays()
        self._draw()

    def _push_undo(self):
        """Save current edit state to the undo stack and clear redo."""
        import copy
        self._undo_stack.append(copy.deepcopy(
            (self._segments, self._in_s, self._out_s)))
        self._redo_stack.clear()

    def _undo_wf(self):
        """Restore previous edit state."""
        if not self._undo_stack:
            return
        import copy
        self._redo_stack.append(copy.deepcopy(
            (self._segments, self._in_s, self._out_s)))
        segs, in_s, out_s = self._undo_stack.pop()
        self._segments = segs
        self._in_s  = in_s
        self._out_s = out_s
        self._refresh_displays()
        self._draw()

    def _redo_wf(self):
        """Re-apply a previously undone edit state."""
        if not self._redo_stack:
            return
        import copy
        self._undo_stack.append(copy.deepcopy(
            (self._segments, self._in_s, self._out_s)))
        segs, in_s, out_s = self._redo_stack.pop()
        self._segments = segs
        self._in_s  = in_s
        self._out_s = out_s
        self._refresh_displays()
        self._draw()

    def _add_cut(self):
        """Split the segment under the playhead, creating a new cut point.

        The cut starts exactly at the playhead position; a 0.5 s gap extends
        forward from there so the two new boundary markers are distinct.
        """
        _GAP = 0.5   # seconds of gap inserted after the cut point
        t = self._playhead_s
        n = len(self._segments)
        for i, seg in enumerate(self._segments):
            eff_in  = self._in_s  if i == 0       else seg[0]
            eff_out = self._out_s if i == n - 1   else seg[1]
            if eff_in < t < eff_out:
                # Cut starts at the playhead; gap extends forward.
                # Clamp so markers stay inside segment boundaries.
                cut_out = max(t,          eff_in  + self._frame_s)
                cut_in  = min(t + _GAP,  eff_out - self._frame_s)
                if cut_in <= cut_out:
                    # Not enough room — zero-gap split at t
                    cut_out = t
                    cut_in  = t
                self._push_undo()
                self._segments[i] = [eff_in, cut_out]
                self._segments.insert(i + 1, [cut_in, eff_out])
                self._draw()
                return

    def _stitched_t_to_file_t(self, elapsed):
        """Map elapsed playback wall-time → file-time position for stitched play."""
        segs = self._play_edit_segs
        if not segs:
            return self._playback_start_file + elapsed
        remaining = elapsed
        for seg_start, seg_dur in segs:
            if remaining <= seg_dur:
                return seg_start + remaining
            remaining -= seg_dur
        last = segs[-1]
        return last[0] + last[1]

    def _set_point_from_cursor(self, side):
        """Set IN or OUT to the current playhead position."""
        t = self._playhead_s
        if side == "in":
            self._in_s = max(0.0, t)
            if self._in_s >= self._out_s:
                self._out_s = self._in_s + 2.0   # drag OUT along with IN
        else:
            self._out_s = max(self._in_s + self._frame_s, t)
        self._refresh_displays()
        self._draw()

    def _play(self, start_s, duration_s):
        import time as _time
        self._stop()
        self._pre_play_pos = self._playhead_s   # save AFTER stop so it isn't cleared
        self._playback_end_file = start_s + duration_s
        import numpy as _np
        import wave as _wave
        out_wav = os.path.join(self._tmp_dir, "_play.wav")

        def _do():
            extract_audio_segment(self._audio_path, start_s, duration_s,
                                   out_wav, sample_rate=_PLAYBACK_SR)
            with _wave.open(out_wav, "rb") as wf:
                data = wf.readframes(wf.getnframes())
            arr  = _np.frombuffer(data, _np.int16).astype(_np.float32) / 32768.0
            peak = float(_np.max(_np.abs(arr))) if len(arr) else 0.0
            if peak > 1e-6:
                arr = arr / peak * 0.85
            pcm = (arr * 32767).astype(_np.int16)
            with _wave.open(out_wav, "wb") as wf:
                wf.setnchannels(1); wf.setsampwidth(2)
                wf.setframerate(_PLAYBACK_SR)
                wf.writeframes(pcm.tobytes())
            return out_wav

        def _done(fut):
            try:
                path = fut.result()
            except Exception:
                _log.exception("play extract failed")
                return
            def _do_play():
                try:
                    import winsound
                    winsound.PlaySound(path,
                                       winsound.SND_FILENAME | winsound.SND_ASYNC)
                except Exception:
                    _log.exception("winsound failed")
                    return
                # Start playhead animation
                self._playhead_s             = start_s
                self._playback_start_wall    = _time.perf_counter()
                self._playback_start_file    = start_s
                self._animate_playhead()
            try:
                self._win.after(0, _do_play)
            except Exception:
                pass

        ex = ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(_do)
        fut.add_done_callback(_done)
        ex.shutdown(wait=False)

    def _animate_playhead(self):
        import time as _time
        if self._playback_start_wall is None:
            return
        elapsed = _time.perf_counter() - self._playback_start_wall
        if self._play_edit_segs:
            self._playhead_s = self._stitched_t_to_file_t(elapsed)
        else:
            self._playhead_s = self._playback_start_file + elapsed
        # Auto-stop when we reach the end of the scheduled playback region
        if (self._playback_end_file is not None and
                self._playhead_s >= self._playback_end_file):
            self._stop()
            return
        self._draw_playhead_only()
        try:
            self._playhead_anim = self._win.after(40, self._animate_playhead)
        except Exception:
            pass

    def _stop(self):
        if self._pre_play_pos is not None:
            self._playhead_s   = self._pre_play_pos
            self._pre_play_pos = None
        self._playback_start_wall = None
        self._play_edit_segs      = None
        self._playback_end_file   = None
        if self._playhead_anim is not None:
            try:
                self._win.after_cancel(self._playhead_anim)
            except Exception:
                pass
            self._playhead_anim = None
        try:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
        try:
            self._draw()
        except Exception:
            pass

    # ── Accept / Cancel ───────────────────────────────────────────────────

    def _on_accept_click(self):
        # _in_s / _out_s hold the user-adjusted outer boundaries.
        # Interior boundaries are stored directly in _segments (modified on drag).
        new_segs = [list(s) for s in self._segments]
        new_segs[0][0]  = self._in_s
        new_segs[-1][1] = self._out_s
        for seg in new_segs:
            if seg[1] <= seg[0]:
                seg[1] = seg[0] + self._frame_s
        self._cleanup()
        if self._on_accept:
            self._on_accept([(s[0], s[1]) for s in new_segs])

    def _on_cancel(self):
        self._cleanup()

    def _cleanup(self):
        self._stop()
        try:
            import shutil
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
        except Exception:
            pass
        try:
            self._win.destroy()
        except Exception:
            pass

    # ── Helpers ───────────────────────────────────────────────────────────

    def _fmt_tc(self, s):
        if s < 0:
            s = 0.0
        h   = int(s) // 3600
        m   = (int(s) % 3600) // 60
        sec = s % 60
        return "{:02d}:{:02d}:{:06.3f}".format(h, m, sec)

    def _parse_tc(self, text):
        """Parse HH:MM:SS.mmm / MM:SS.mmm / SS.mmm.  Returns None on failure."""
        text = text.strip()
        try:
            parts = text.split(":")
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            if len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
            return float(parts[0])
        except Exception:
            return None
