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

# Silent lead-in prepended to every extracted playback WAV.  Windows'
# audio-session startup applies a smoothing gain-ramp (~50-150 ms) at
# the head of every winsound.PlaySound() to prevent click/pop artefacts;
# on speech that ramp is audible as a fade-in on the very first
# syllable.  Prepending a short silent lead-in gives Windows something
# to ramp over, so the real audio starts at full level.  The playhead
# animation is offset by the same amount so the visual cursor stays
# aligned with what the user hears.
_LEAD_IN_MS = 120
_CONTEXT_S   = 6.0      # seconds of context on each side of the match
_MARKER_HIT  = 10       # pixel radius for grabbing IN/OUT markers
_CUT_HIT     = 18       # wider radius for interior CUT IN / CUT OUT lines —
                        # they're the boundaries between kept segments and
                        # get grabbed constantly during multi-clip cleanup,
                        # so they need to win against the fleur-slip zone
                        # even on a slightly-off click
_SLIP_DRAG_PX = 4       # pixels mouse must move before a body-press becomes a slip
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
                 on_accept=None, fps=24.0,
                 pool_by_token=None):
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
        # Retained for the "copy corrected @PULL header" action in the
        # cross-token search: the current pull's token label and the
        # scripted quote text.
        self._current_token   = title or ""
        self._quote_text      = quote_text or ""
        # Optional {token: [audio_paths]} map so the search bar can
        # cross-check phrases against every OTHER pool file's
        # .pb_transcript.json sidecar — used when the assigned token is
        # itself wrong.  Falls back to the just-this-file default.
        self._pool_by_token   = pool_by_token or {title: [source_audio]}
        self._words           = [w for w in (words or [])
                                  if isinstance(w, dict) and "start" in w]
        # Cached start-time array for O(log N) visible-word slicing in
        # _draw_words.  Long-form interview transcripts run 10-20K
        # words; walking the whole list on every _draw was 50-200 ms
        # per redraw and _draw fires 3-5× during initial open.
        self._word_starts     = [float(w.get("start", 0.0))
                                  for w in self._words]

        # Working copy — we edit the overall span (first in / last out).
        # Interior segment boundaries stay fixed; on accept we shift
        # the first-in and last-out by the same deltas the user applied.
        self._segments   = [list(s) for s in segments]  # mutable copy
        # Sweep zero-duration segments from any prior session's saved
        # state before we compute _in_s / _out_s — otherwise the span
        # inherits an artefactual boundary at the empty segment's
        # position and the visible edit area expands to include it.
        # (Method may not exist yet during first-run tests; be defensive.)
        _fps_for_prune = float(fps) if fps else 24.0
        _eps = 1.0 / max(1.0, _fps_for_prune)
        _clean = [list(s) for s in self._segments
                   if (float(s[1]) - float(s[0])) >= _eps]
        if _clean:
            self._segments = _clean
        self._in_s       = self._segments[0][0]  if self._segments else 0.0
        self._out_s      = self._segments[-1][1] if self._segments else 0.0

        # Load the full source file so the user can scroll to any position
        # (the matched region may be wrong and the true edit points may be far away).
        self._ctx_start = 0.0
        # DON'T probe the real duration on the main thread — was 100-500 ms
        # per open, blocked the whole parent app because __init__ runs in
        # the caller's main loop.  Start with a generous coarse estimate
        # that covers the visible match + plenty of scroll room; the
        # background extraction thread refines it once the real duration
        # is known (see _phase1_worker / _preview_done).
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

        # Word-search state — for hunting a phrase in a transcript that
        # diverges wildly from what the script said.  self._search_hits
        # holds [(word_idx, in_s, out_s), …] for the current query.
        self._search_query  = ""
        self._search_hits   = []
        self._search_idx    = 0   # which hit is currently focussed

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
        # Focus the window so keyboard shortcuts work immediately, but
        # DON'T grab_set() until the preview lands — the multi-second
        # main-thread stalls during Phase-2 silence-boundary computation
        # would leave the user unable to hit Escape.  grab_set fires
        # inside _preview_done once samples are ready.
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
        self._cv.bind("<Motion>",          self._on_hover)

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

        # ZOOM — only FIT stays as a button; wheel-scroll + keyboard
        # shortcuts (+/-, r/t) still zoom, per the keybindings below.
        tk.Label(nrow, text="ZOOM", font=FB, bg=BG, fg=SUB).pack(side="left", padx=(0, 4))
        _zfit = tk.Label(nrow, text="FIT", font=FB, bg=SURF3, fg=TEXT,
                          cursor="hand2", padx=8, pady=2, bd=0,
                          highlightbackground=BORDER, highlightthickness=1)
        _zfit.pack(side="left", padx=1)
        _zfit.bind("<Enter>", lambda e, w=_zfit: w.config(bg=ACCENT))
        _zfit.bind("<Leave>", lambda e, w=_zfit: w.config(bg=SURF3))
        _zfit.bind("<ButtonRelease-1>", lambda e: self._zoom_fit())

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

        # Shift+S = toggle SKIP CUTS (matches the checkbox in the
        # transport row).  Both cases bound because Tk delivers
        # <Shift-S> when caps/shift produces uppercase and <Shift-s>
        # otherwise depending on keyboard layout.
        def _toggle_skip_cuts(_e=None):
            v = self._play_edit_mode
            if v is not None:
                v.set(not v.get())
        win.bind("<Shift-S>", _toggle_skip_cuts)
        win.bind("<Shift-s>", _toggle_skip_cuts)

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

        # ── Transcript search row ─────────────────────────────────────────
        # Type a phrase, prev/next steps through matches on the word
        # timeline and centres the view + playhead on each hit.  Handy
        # when the transcript diverges wildly from the scripted quote
        # and you need to hunt for the phrase's real location.
        srow = tk.Frame(win, bg=BG)
        srow.pack(fill="x", padx=12, pady=(4, 0))
        tk.Label(srow, text="🔎 Search transcript:",
                 font=FB, bg=BG, fg=SUB).pack(side="left")
        self._search_var = tk.StringVar()
        _sent = tk.Entry(srow, textvariable=self._search_var, font=FB,
                         bg=SURF2, fg=TEXT, insertbackground=TEXT,
                         relief="flat", bd=4, width=32)
        _sent.pack(side="left", padx=(6, 4))
        _sent.bind("<Return>",   lambda e: self._search_next())
        _sent.bind("<KP_Enter>", lambda e: self._search_next())
        _sent.bind("<Escape>",   lambda e: self._search_clear())
        # Debounce so the O(N-word) match scan doesn't fire on every
        # single keystroke.  150 ms after the last edit is quick
        # enough to feel live while collapsing "hello" from 5 firings
        # down to 1.
        def _schedule_search(*_):
            _prev = getattr(self, "_search_after_id", None)
            if _prev is not None:
                try: self._win.after_cancel(_prev)
                except Exception: pass
            self._search_after_id = self._win.after(150, self._search_update)
        self._search_var.trace_add("write", _schedule_search)

        def _sb(text, cmd):
            b = tk.Label(srow, text=text, font=FB, bg=SURF3, fg=TEXT,
                         cursor="hand2", padx=8, pady=2, bd=0,
                         highlightbackground=BORDER, highlightthickness=1)
            b.pack(side="left", padx=(0, 4))
            b.bind("<Enter>", lambda e, w=b: w.config(bg=ACCENT, fg=BG))
            b.bind("<Leave>", lambda e, w=b: w.config(bg=SURF3, fg=TEXT))
            b.bind("<ButtonRelease-1>", lambda e, f=cmd: f())
            return b
        _sb("◀ Prev", self._search_prev)
        _sb("Next ▶", self._search_next)
        self._search_count_lbl = tk.Label(srow, text="", font=FB,
                                          bg=BG, fg=SUB)
        self._search_count_lbl.pack(side="left", padx=(6, 0))

        # 🌐 All pulls — search across every other token's transcript
        # sidecars.  When the phrase is nowhere in THIS file (or the
        # user suspects the assigned token was wrong to begin with),
        # the cross-token panel below surfaces hits with their file /
        # token / timecode + a one-click "copy corrected @PULL header"
        # so the user can paste the fix into the script and re-reconcile.
        self._search_all_var = tk.BooleanVar(value=False)
        _all_cb = tk.Checkbutton(srow, text="🌐 all pulls",
                                  variable=self._search_all_var,
                                  font=FB, bg=BG, fg=TEXT,
                                  selectcolor=BG, activebackground=BG,
                                  activeforeground=TEXT, cursor="hand2",
                                  bd=0, padx=4, pady=0,
                                  highlightthickness=0,
                                  command=lambda: self._search_update())
        _all_cb.pack(side="right", padx=(0, 4))

        # Cross-token results panel — packed but empty by default.  Fills
        # in when "all pulls" is on and there are hits in OTHER files.
        self._search_xtok_frame = tk.Frame(win, bg=BG)
        self._search_xtok_frame.pack(fill="x", padx=12, pady=(0, 0))

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
        # side="bottom" so ACCEPT / CANCEL survive when the canvas
        # frame (packed above with fill=both+expand=True) grows to
        # fill the window.  Without this, resizing the editor shorter
        # (or opening on a smaller display) pushes the accept row off
        # the visible area — same failure mode as the Step 3 and
        # Step 4 nav fixes (476483a + this session's).
        bot.pack(side="bottom", fill="x", padx=12, pady=(8, 12))

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
        # Probe the real file duration here on the bg thread — the
        # main thread's __init__ used a coarse estimate to avoid
        # blocking the parent app on ffprobe.  Result gets applied
        # in _preview_done under the Tk main thread.
        try:
            real_dur = float(get_media_duration(self._audio_path) or 0.0)
        except Exception:
            real_dur = 0.0
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
        return arr, start, peak, real_dur

    def _preview_done(self, fut):
        try:
            if not self._win.winfo_exists():
                return
        except Exception:
            return
        try:
            samples, preview_start, src_peak, real_dur = fut.result()
            self._src_peak     = src_peak if src_peak > 1e-6 else None
            self._display_gain = (0.85 / src_peak) if src_peak > 1e-6 else 1.0
            # Refine _ctx_dur from the real probe now that ffprobe has
            # run off-thread.  Falls back to whatever coarse estimate
            # __init__ set if the probe returned 0.
            if real_dur and real_dur > 0:
                self._ctx_dur = real_dur
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
                # Preview + boundaries ready — now safe to grab modal
                # focus.  If a later Phase-2 stall happens, the user
                # can still Escape (or the parent app can dismiss).
                try:
                    self._win.grab_set()
                except tk.TclError:
                    pass
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

        # ── Small helper: turn a text label into a clickable "floating
        # button" by drawing a bordered rectangle behind it, tagged the
        # same so hover + click hit the whole rectangle, not just the
        # text glyphs.  Padding is intentionally generous so the click
        # target comfortably exceeds the text ink.
        def _canvas_button(text_id, tag,
                            fill_bg=SURF2, fill_bg_hover=ERR,
                            ink=SUB, ink_hover=BG,
                            pad=(8, 3)):
            _bb = cv.bbox(text_id)
            if not _bb:
                return
            x1, y1, x2, y2 = _bb
            _rect = cv.create_rectangle(
                x1 - pad[0], y1 - pad[1],
                x2 + pad[0], y2 + pad[1],
                fill=fill_bg, outline=BORDER, width=1,
                tags=(tag,))
            cv.tag_lower(_rect, text_id)   # keep text on top of rect
            cv.itemconfig(text_id, fill=ink)
            cv.tag_bind(tag, "<Enter>",
                        lambda e, r=_rect, t=text_id: (
                            cv.itemconfig(r, fill=fill_bg_hover),
                            cv.itemconfig(t, fill=ink_hover),
                            cv.config(cursor="hand2")))
            cv.tag_bind(tag, "<Leave>",
                        lambda e, r=_rect, t=text_id: (
                            cv.itemconfig(r, fill=fill_bg),
                            cv.itemconfig(t, fill=ink),
                            cv.config(cursor="")))

        # ── Multi-segment interior boundaries — draggable, removable ──────
        # _remove_hit_areas kept as an empty list so _on_press's stale
        # position scan bails cleanly; the button fires via tag_bind
        # on release now.
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
                        _tag = "rm_in_{}".format(i)
                        _rm  = cv.create_text(
                            px + 8, 40, text="\u00d7 merge",
                            fill=SUB,
                            font=("Segoe UI", 8, "bold"), anchor="w",
                            tags=(_tag,))
                        _canvas_button(_rm, _tag)
                        cv.tag_bind(_tag, "<ButtonRelease-1>",
                                    lambda e, ii=i: self._remove_cut(ii))
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
        # _remove_seg_areas empty for the same reason as _remove_hit_areas.
        self._remove_seg_areas = []
        if len(self._segments) > 1:
            for _si, (_seg_in, _seg_out) in enumerate(self._segments):
                _eff_in  = self._in_s  if _si == 0            else _seg_in
                _eff_out = self._out_s if _si == _n_segs - 1  else _seg_out
                _sx = _t_to_px(_eff_in)
                _ex = _t_to_px(_eff_out)
                _seg_w = _ex - _sx
                _cx    = (_sx + _ex) / 2
                # Adapt label to available width so short segments still
                # get a clickable button.  A zero-duration segment can
                # never appear here \u2014 _prune_empty_segments sweeps them
                # on every mutation \u2014 but user-created 200-ms segments
                # (e.g. two cuts placed close together) still need a
                # way out.  Threshold matches the smallest label that
                # renders as a real button (~14px for a single "\u00d7").
                if _seg_w > 14 and 0 <= _cx < w:
                    if _seg_w > 100:
                        _label = "\u00d7 remove segment"
                    elif _seg_w > 44:
                        _label = "\u00d7 remove"
                    else:
                        _label = "\u00d7"
                    _tag = "rm_seg_{}".format(_si)
                    _cy  = mid + 28
                    _rm  = cv.create_text(
                        int(_cx), _cy,
                        text=_label,
                        fill=ERR,
                        font=("Segoe UI", 8, "bold"),
                        anchor="center",
                        tags=(_tag,))
                    _canvas_button(_rm, _tag,
                                   ink=ERR, ink_hover=BG,
                                   fill_bg_hover=ERR)
                    cv.tag_bind(_tag, "<ButtonRelease-1>",
                                lambda e, i=_si: self._remove_segment(i))

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

        # Search-hit backgrounds — one WARN band per hit, ACCENT for the
        # currently-focussed one.  Drawn BEFORE the segment kept/cut
        # rectangles so those still tint over unmatched areas, and
        # BEFORE the word text so the text sits on top.
        for _hi, (_widx, _hin, _hout) in enumerate(
                getattr(self, "_search_hits", [])):
            _hp = self._t_to_px(_hin)
            _hq = self._t_to_px(_hout)
            if _hq >= 0 and _hp <= w:
                _col = ACCENT if _hi == self._search_idx else WARN
                cv.create_rectangle(max(-2, _hp - 2), 0,
                                     min(w + 2, _hq + 2), 36,
                                     fill=_col, outline="", stipple="gray50")

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
        # Bisect the sorted start-time array for the visible time
        # window instead of walking every word.  On a 20K-word
        # transcript this trims per-draw cost from ~50-200 ms to
        # a few ms.  Slight over-scan (± 1s worth of margin) so
        # words whose start is off-screen but whose ink still spills
        # into the viewport get considered.
        import bisect as _bisect
        _t_left  = self._px_to_t(-5)
        _t_right = self._px_to_t(w + 5)
        _lo = _bisect.bisect_left(self._word_starts,  _t_left  - 1.0)
        _hi = _bisect.bisect_right(self._word_starts, _t_right + 1.0)
        cands = []
        for gi in range(_lo, _hi):
            wd = self._words[gi]
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
        # If the press landed on one of our clickable canvas buttons
        # (merge / remove-segment), let its tag-bound <ButtonRelease-1>
        # handle the action — don't start a drag underneath it.
        try:
            _cv    = self._word_cv.master  # the waveform canvas — actually resolve properly
        except Exception:
            _cv = None
        try:
            _wf_cv = getattr(self, "_wf_cv", None) or getattr(self, "_wfcv", None)
        except Exception:
            _wf_cv = None
        # The canvas the event originates from IS event.widget; use it.
        _ew = getattr(event, "widget", None)
        if _ew is not None:
            _cur = _ew.find_withtag("current")
            if _cur:
                _tags = _ew.gettags(_cur[0])
                if any(t.startswith("rm_in_") or t.startswith("rm_seg_")
                        for t in _tags):
                    self._drag = None
                    return
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
            # Check interior segment boundaries before falling through to
            # playhead / slip.  Uses _CUT_HIT (wider than _MARKER_HIT) so
            # a slightly-off click on a cut still grabs the cut instead
            # of triggering slip on the neighbouring segment.  If two
            # cuts are within _CUT_HIT of each other AND both within
            # _CUT_HIT of the click, we prefer the one CLOSER to the
            # cursor (not just "first found") — otherwise the CUT OUT
            # of segment N and CUT IN of segment N+1 sitting a few
            # pixels apart would always resolve to seg_out N even when
            # the user was closer to seg_in N+1.
            n = len(self._segments)
            interior_hit = None
            best_dist    = _CUT_HIT + 1
            for i in range(n):
                if i > 0:
                    px = self._t_to_px(self._segments[i][0])
                    d  = abs(event.x - px)
                    if d <= _CUT_HIT and d < best_dist:
                        interior_hit = ("seg_in", i)
                        best_dist    = d
                if i < n - 1:
                    px = self._t_to_px(self._segments[i][1])
                    d  = abs(event.x - px)
                    if d <= _CUT_HIT and d < best_dist:
                        interior_hit = ("seg_out", i)
                        best_dist    = d
            if interior_hit:
                self._drag = interior_hit
            elif abs(event.x - ph_px) <= _MARKER_HIT:
                # Playhead line: grab from anywhere along its full
                # vertical extent, not just the triangle handle at
                # the top.  Ordered BEFORE the region-body slip
                # check so a click directly on the cursor line wins
                # against slip on whatever segment / gap sits under
                # it.  The bare-ruler variant below (y <= 14) still
                # fires for clicks at the top even off the line
                # itself, so the triangle grabber stays generous.
                self._drag = "playhead"
            elif in_px + _MARKER_HIT < event.x < out_px - _MARKER_HIT and event.y > 14:
                # Inside the overall selected region.  Two possibilities:
                #   (a) blue kept segment  → slip that segment
                #   (b) red interior gap   → slip the gap (both boundaries
                #                            together, keeping gap length)
                # Determine which by checking whether the click's time
                # falls into any segment's [in..out], or into a gap
                # between two segments.
                _click_t = self._px_to_t(event.x)
                _seg_i   = None
                _gap_i   = None   # gap sits BETWEEN seg _gap_i and _gap_i+1
                for _i, (_sin, _sout) in enumerate(self._segments):
                    _eff_in  = self._in_s  if _i == 0                      else _sin
                    _eff_out = self._out_s if _i == len(self._segments) - 1 else _sout
                    if _eff_in <= _click_t <= _eff_out:
                        _seg_i = _i
                        break
                if _seg_i is None and len(self._segments) >= 2:
                    for _i in range(len(self._segments) - 1):
                        if (self._segments[_i][1] <= _click_t
                                <= self._segments[_i + 1][0]):
                            _gap_i = _i
                            break

                if _gap_i is not None:
                    # RED-gap slip: shift seg[i].out and seg[i+1].in
                    # together, keeping the gap duration constant.
                    self._drag             = "gap_slip_pending"
                    self._slip_anchor_x   = event.x
                    self._gap_slip_idx    = _gap_i
                    self._gap_anchor_left  = self._segments[_gap_i][1]
                    self._gap_anchor_right = self._segments[_gap_i + 1][0]
                    self._slip_anchor_segs = [list(s) for s in self._segments]
                else:
                    # BLUE-segment slip — the existing per-segment
                    # behavior, only moves the one segment under the
                    # cursor.  Falls back to seg 0 if the click's
                    # time somehow landed outside every segment (edge
                    # case: click on the boundary marker itself).
                    if _seg_i is None:
                        _seg_i = 0
                    self._drag              = "slip_pending"
                    self._slip_anchor_x    = event.x
                    self._slip_seg_idx     = _seg_i
                    self._slip_anchor_seg  = list(self._segments[_seg_i])
                    self._slip_anchor_in   = self._in_s
                    self._slip_anchor_out  = self._out_s
                    self._slip_anchor_segs = [list(s) for s in self._segments]
            elif event.y <= 14:
                # Ruler strip at the top — grabs the playhead even off
                # the line itself, so the triangle handle stays a
                # generous target for coarse-precision seeking.  The
                # abs(event.x - ph_px) check that used to live here
                # has been hoisted above the region-body slip branch
                # so cursor clicks anywhere on the line win.
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

        Vectorised — was a pure-Python for-loop over ~N/160 windows
        that stalled the main thread for 1-4 s on hour-long sources
        (workflow-verified as the biggest waveform-open lag cause).
        Reshape → per-row RMS in one C-level call → boolean transitions
        via np.diff → convert transition indices to seconds.  Runs
        ~100× faster and drops the freeze to under 50 ms on typical
        interview lengths.
        """
        import numpy as _np
        self._silence_boundaries = []
        self._sil_starts         = []
        self._sil_ends           = []
        if self._samples is None or len(self._samples) == 0:
            return
        sr        = self._sr
        win       = max(1, int(sr * 0.02))    # 20 ms windows
        threshold = 0.02                       # RMS threshold (audio peak-normalised ~0.85)
        min_dur_s = 0.1                        # only silences ≥ 100 ms

        samples = self._samples
        n_full  = (len(samples) // win) * win  # trim ragged tail — we don't need sub-window precision
        if n_full == 0:
            return
        # Row = one 20 ms window; column = sample within window.
        rms = _np.sqrt(_np.mean(
            samples[:n_full].reshape(-1, win).astype(_np.float32) ** 2, axis=1))
        is_sil = rms < threshold

        # Locate silence-region edges: pad boolean array with False on
        # both ends so a run starting at 0 or ending at last window is
        # detected the same way as an interior one.  np.diff on an
        # int8 view gives +1 at rising edges (speech→silence, i.e.
        # silence STARTS) and -1 at falling edges (silence ENDS).
        pad = _np.concatenate(([False], is_sil, [False])).astype(_np.int8)
        d   = _np.diff(pad)
        starts_i = _np.flatnonzero(d ==  1)   # window indices where silence starts
        ends_i   = _np.flatnonzero(d == -1)   # window indices where silence ends
        # Convert window indices → seconds via window * (win / sr).
        w_dur = win / sr
        starts_s = self._ctx_start + starts_i * w_dur
        ends_s   = self._ctx_start + ends_i   * w_dur
        # Filter silences shorter than min_dur_s.
        keep = (ends_s - starts_s) >= min_dur_s
        starts_s = starts_s[keep]
        ends_s   = ends_s[keep]

        self._sil_starts         = starts_s.tolist()
        self._sil_ends           = ends_s.tolist()
        self._silence_boundaries = sorted(self._sil_starts + self._sil_ends)

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
        # Promote (blue-segment) slip_pending → slip only once the mouse
        # has moved enough.  Until then, suppress all motion handling so
        # a simple click doesn't accidentally shift the region.
        if self._drag == "slip_pending":
            if abs(event.x - self._slip_anchor_x) <= _SLIP_DRAG_PX:
                return
            self._drag = "slip"
        # Same promotion for red-gap slip.
        elif self._drag == "gap_slip_pending":
            if abs(event.x - self._slip_anchor_x) <= _SLIP_DRAG_PX:
                return
            self._drag = "gap_slip"
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
        elif self._drag == "slip":
            # Slip ONLY the segment the press landed inside — was
            # previously "slip the whole region" which yanked every
            # sub-clip of a multi-segment pull whenever the user
            # tried to nudge one of them.  Anchored from the initial
            # press to avoid float drift across motion events.
            raw_delta = self._px_to_t(event.x) - self._px_to_t(self._slip_anchor_x)
            i         = self._slip_seg_idx
            n         = len(self._slip_anchor_segs)
            anc_in    = self._slip_anchor_seg[0]
            anc_out   = self._slip_anchor_seg[1]
            dur       = anc_out - anc_in

            # Clamp so this segment can't cross into its neighbors
            # (preserves ordering + prevents overlaps).  First segment
            # is bounded left by 0; last by the file end; interior
            # segments by their neighbor boundaries.
            _left_bound  = (0.0 if i == 0
                            else self._slip_anchor_segs[i - 1][1])
            _right_bound = (self._ctx_dur if i == n - 1
                            else self._slip_anchor_segs[i + 1][0])
            new_in  = max(_left_bound, anc_in + raw_delta)
            new_in  = min(new_in, _right_bound - dur)
            new_out = new_in + dur

            # Update just this segment.  The rest keep their anchored
            # positions so multi-clip pulls stay put around the one
            # segment the user is nudging.
            self._segments = [list(s) for s in self._slip_anchor_segs]
            self._segments[i] = [new_in, new_out]
            # First / last segment slip also drags the global IN / OUT
            # so the visible region matches — interior slips leave
            # IN / OUT untouched.
            if i == 0:
                self._in_s  = new_in
            if i == n - 1:
                self._out_s = new_out
            self._refresh_displays()
        elif self._drag == "gap_slip":
            # Shift a RED interior gap left/right while keeping its
            # DURATION constant — seg[i].out and seg[i+1].in move
            # together by the same delta.  Effect: the gap "walks"
            # across the timeline; kept audio at the tail of seg i
            # gets cut (or restored) and the head of seg i+1 does the
            # opposite.
            raw_delta = self._px_to_t(event.x) - self._px_to_t(self._slip_anchor_x)
            i         = self._gap_slip_idx
            gap_dur   = self._gap_anchor_right - self._gap_anchor_left
            # Clamp: neither boundary can cross into its segment's
            # anchor.  seg[i].out can't go below seg[i].in + 1 frame,
            # seg[i+1].in can't exceed seg[i+1].out - 1 frame.
            _left_min  = self._slip_anchor_segs[i][0]     + self._frame_s
            _right_max = self._slip_anchor_segs[i + 1][1] - self._frame_s
            new_left  = max(_left_min, self._gap_anchor_left + raw_delta)
            new_left  = min(new_left, _right_max - gap_dur)
            new_right = new_left + gap_dur
            self._segments = [list(s) for s in self._slip_anchor_segs]
            self._segments[i][1]     = new_left
            self._segments[i + 1][0] = new_right
            self._refresh_displays()
        else:  # playhead
            self._playhead_s = min(t, self._ctx_dur)
        self._draw()

    def _on_release(self, event):
        if self._drag in ("slip_pending", "gap_slip_pending"):
            # Mouse was pressed in the region body / a red gap but never
            # dragged far enough to become a slip — treat it as a plain
            # playhead click, matching the behaviour of clicking outside
            # the region (including jump-to position during playback).
            new_t = max(0.0, min(self._px_to_t(event.x), self._ctx_dur))
            self._playhead_s = new_t
            was_playing = self._playback_start_wall is not None
            self._draw()
            if was_playing:
                if self._play_edit_mode and self._play_edit_mode.get():
                    self._play_or_edit()
                else:
                    remain = max(0.5, self._ctx_dur - new_t)
                    self._play(new_t, min(remain, 30.0))
                self._pre_play_pos = new_t
        # A drag on a CUT boundary can crush a segment down to zero
        # duration.  Auto-prune so the invisible-sliver-that-won't-
        # remove state (screenshot from Jordan) can't happen.
        _pre = len(self._segments)
        if hasattr(self, "_prune_empty_segments"):
            self._prune_empty_segments()
            if len(self._segments) != _pre:
                self._refresh_displays()
                self._draw()
        self._drag = None

    def _on_hover(self, event):
        """Update the canvas cursor to reflect what a click-drag would do."""
        if self._samples is None:
            return
        in_px  = self._t_to_px(self._in_s)
        out_px = self._t_to_px(self._out_s)
        # Outer IN / OUT handles
        if (abs(event.x - in_px) <= _MARKER_HIT or
                abs(event.x - out_px) <= _MARKER_HIT):
            self._cv.config(cursor="sb_h_double_arrow")
            return
        # Interior segment cut boundaries (CUT IN / CUT OUT lines).
        # Uses _CUT_HIT (wider than _MARKER_HIT for outer IN / OUT) —
        # must match _on_press so cursor feedback and hit behavior
        # agree, otherwise the user sees the fleur cursor over an
        # area that ACTUALLY grabs a cut.
        n = len(self._segments)
        for i in range(n):
            if i > 0:
                px = self._t_to_px(self._segments[i][0])
                if abs(event.x - px) <= _CUT_HIT:
                    self._cv.config(cursor="sb_h_double_arrow")
                    return
            if i < n - 1:
                px = self._t_to_px(self._segments[i][1])
                if abs(event.x - px) <= _CUT_HIT:
                    self._cv.config(cursor="sb_h_double_arrow")
                    return
        # Playhead line: any y within _MARKER_HIT horizontally shows
        # the h-resize cursor so the user knows they can grab the
        # cursor from anywhere along its length (matches the priority
        # order in _on_press).
        ph_px = self._t_to_px(self._playhead_s)
        if abs(event.x - ph_px) <= _MARKER_HIT:
            self._cv.config(cursor="sb_h_double_arrow")
            return
        if in_px + _MARKER_HIT < event.x < out_px - _MARKER_HIT and event.y > 14:
            self._cv.config(cursor="fleur")               # slip / move region
        else:
            self._cv.config(cursor="")                    # default

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

    # ── Transcript search ────────────────────────────────────────────────

    _SEARCH_NORM_RE = None
    @staticmethod
    def _search_norm(s):
        """Lowercase, strip punctuation, normalise smart quotes — matches
        how the reconcile normalises words for text-fallback searches."""
        import re as _re
        s = (s or "").replace("’", "'").replace("‘", "'")
        return _re.sub(r"[^a-z0-9']+", " ", s.lower()).strip()

    def _search_update(self):
        """Trace callback: recompute matches for the current query, jump
        to the first, refresh the count label + redraw word timeline.
        Also refreshes the cross-token results panel when "all pulls"
        is on."""
        q = self._search_norm(self._search_var.get())
        self._search_query = q
        self._search_hits  = []
        self._search_xtok_refresh()   # always clear/refresh the panel
        if not q or not self._words:
            self._search_idx = 0
            if hasattr(self, "_search_count_lbl"):
                try: self._search_count_lbl.config(text="", fg=SUB)
                except tk.TclError: pass
            self._words_draw_key = None  # force _draw_words to redraw
            self._draw_words()
            return
        # Multi-word phrase support: split query into words, then look
        # for a run of consecutive transcript words that match.
        q_words = q.split()
        n_q = len(q_words)
        # Cache the normalised word arrays — self._words doesn't
        # change over the dialog's lifetime, so rebuilding them per
        # keystroke was 20k _search_norm calls + 20k splits for
        # nothing.  Build once, reuse.
        w_first = getattr(self, "_search_w_first", None)
        if w_first is None:
            w_texts = [self._search_norm(w.get("word", "")) for w in self._words]
            w_first = [t.split()[0] if t else "" for t in w_texts]
            self._search_w_first = w_first
        for i in range(len(w_first) - n_q + 1):
            if w_first[i:i + n_q] == q_words:
                in_s  = float(self._words[i].get("start", 0))
                out_s = float(self._words[i + n_q - 1].get("end", in_s))
                self._search_hits.append((i, in_s, out_s))
        self._search_idx = 0
        self._search_update_count_lbl()
        if self._search_hits:
            self._search_focus_current()
        else:
            self._words_draw_key = None
            self._draw_words()

    def _search_update_count_lbl(self):
        if not hasattr(self, "_search_count_lbl"):
            return
        try:
            if not self._search_hits:
                self._search_count_lbl.config(text="no matches", fg=WARN)
            else:
                self._search_count_lbl.config(
                    text="{}/{}".format(self._search_idx + 1, len(self._search_hits)),
                    fg=SUCCESS)
        except tk.TclError:
            pass

    def _search_next(self):
        if not self._search_hits:
            return
        self._search_idx = (self._search_idx + 1) % len(self._search_hits)
        self._search_update_count_lbl()
        self._search_focus_current()

    def _search_prev(self):
        if not self._search_hits:
            return
        self._search_idx = (self._search_idx - 1) % len(self._search_hits)
        self._search_update_count_lbl()
        self._search_focus_current()

    def _search_focus_current(self):
        """Centre the waveform view on the current search hit and park
        the playhead on it so PLAY IN picks it up."""
        if not self._search_hits:
            return
        _, in_s, out_s = self._search_hits[self._search_idx]
        t_centre = (in_s + out_s) / 2.0
        w = self._canvas_w
        window_s = w * self._spp / self._sr
        vs = int((t_centre - window_s * 0.5 - self._ctx_start) * self._sr)
        n  = len(getattr(self, "_samples", []))
        max_vs = max(0, n - w * self._spp) if n else 0
        self._view_start  = max(0, min(max_vs, vs))
        self._playhead_s  = in_s
        self._words_draw_key = None
        self._draw()

    def _search_clear(self):
        try:
            self._search_var.set("")
        except tk.TclError:
            pass

    def _find_phrase_in_words(self, q_words, words):
        """Return [(word_idx, in_s, out_s), …] for every match of the
        phrase (list of already-normalised query words) in a word list.
        Same normalisation as the in-file search so cross-file hits use
        the same matching semantics."""
        if not q_words or not words:
            return []
        n_q = len(q_words)
        w_first = []
        for w in words:
            t = self._search_norm(w.get("word", ""))
            w_first.append(t.split()[0] if t else "")
        hits = []
        for i in range(len(w_first) - n_q + 1):
            if w_first[i:i + n_q] == q_words:
                in_s  = float(words[i].get("start", 0))
                out_s = float(words[i + n_q - 1].get("end", in_s))
                hits.append((i, in_s, out_s, words))
        return hits

    _CTX_WORDS_BEFORE = 15
    _CTX_WORDS_AFTER  = 15

    def _search_xtok_refresh(self):
        """Rebuild the cross-token results panel from the current query.
        For every pool file that isn't this pull's audio, load the
        sidecar and pack one row per phrase match with:
          • [TOKEN] and file name
          • the surrounding transcript sentence (match highlighted)
          • ▶ AUDITION — plays the match's audio without disturbing
            this dialog's own playhead / waveform
          • ADOPT — reassigns this pull to that token + timecode and
            closes the dialog (caller handles the pull-dict update).
        """
        panel = getattr(self, "_search_xtok_frame", None)
        if panel is None:
            return
        for w in panel.winfo_children():
            try: w.destroy()
            except tk.TclError: pass
        if not getattr(self, "_search_all_var", None) or not self._search_all_var.get():
            return
        q = self._search_query
        if not q:
            return
        q_words = q.split()
        try:
            from engines import pb_transcript_load
        except Exception:
            return
        _own = os.path.abspath(self._audio_path or "")
        rows = []   # (tok, audio_path, in_s, out_s, ctx_before, matched, ctx_after)
        for tok, paths in (self._pool_by_token or {}).items():
            for ap in paths:
                if not ap or os.path.abspath(ap) == _own:
                    continue
                try:
                    words, _ = pb_transcript_load(ap)
                except Exception:
                    words = None
                if not words:
                    continue
                for i, in_s, out_s, _ws in self._find_phrase_in_words(q_words, words):
                    n_q  = len(q_words)
                    b0   = max(0, i - self._CTX_WORDS_BEFORE)
                    a1   = min(len(words), i + n_q + self._CTX_WORDS_AFTER)
                    before  = " ".join(w.get("word", "") for w in words[b0:i]).strip()
                    matched = " ".join(w.get("word", "") for w in words[i:i + n_q]).strip()
                    after   = " ".join(w.get("word", "") for w in words[i + n_q:a1]).strip()
                    rows.append((tok, ap, in_s, out_s, before, matched, after))
        if not rows:
            tk.Label(panel, text="  no matches in other tokens",
                     font=FB, bg=BG, fg=SUB).pack(anchor="w", pady=(4, 0))
            return
        tk.Label(panel,
                  text="  matches in other tokens — ▶ audition, then ADOPT "
                       "to reassign this pull:",
                  font=FB, bg=BG, fg=SUB).pack(anchor="w", pady=(4, 2))
        for (tok, ap, in_s, out_s, before, matched, after) in rows[:15]:
            row = tk.Frame(panel, bg=SURF2,
                            highlightbackground=BORDER, highlightthickness=1)
            row.pack(fill="x", padx=6, pady=1)

            # Left header line: token / file / TC / action buttons.
            head = tk.Frame(row, bg=SURF2)
            head.pack(fill="x", padx=6, pady=(3, 0))
            tk.Label(head, text="[{}]".format(tok), font=FBT,
                      bg=SURF2, fg=ACCENT).pack(side="left")
            tk.Label(head, text=os.path.basename(ap), font=FB,
                      bg=SURF2, fg=SUB).pack(side="left", padx=(8, 0))
            tc = "{} — {}".format(self._fmt_tc(in_s), self._fmt_tc(out_s))
            tk.Label(head, text=tc, font=FB, bg=SURF2, fg=TEXT
                     ).pack(side="left", padx=(10, 0))

            # ADOPT button — closes with reassignment.  Placed first (on
            # the right) so it's the visually dominant action.
            adopt = tk.Label(head, text="  ADOPT  ", font=FBT,
                              bg=SUCCESS, fg=TEXT, cursor="hand2",
                              padx=6, pady=1, bd=0,
                              highlightbackground=BORDER, highlightthickness=1)
            adopt.pack(side="right", padx=(6, 0))
            adopt.bind("<Enter>", lambda e, w=adopt: w.config(bg="#4a9a51"))
            adopt.bind("<Leave>", lambda e, w=adopt: w.config(bg=SUCCESS))
            adopt.bind(
                "<ButtonRelease-1>",
                lambda e, t=tok, p=ap, i=in_s, o=out_s: self._xtok_adopt(t, p, i, o))

            # ▶ AUDITION button — plays that segment from the OTHER
            # file without disturbing this dialog's own playhead.
            aud = tk.Label(head, text=" ▶ Audition ", font=FB,
                            bg=SURF3, fg=TEXT, cursor="hand2",
                            padx=6, pady=1, bd=0,
                            highlightbackground=BORDER, highlightthickness=1)
            aud.pack(side="right", padx=(6, 0))
            aud.bind("<Enter>", lambda e, w=aud: w.config(bg=ACCENT, fg=BG))
            aud.bind("<Leave>", lambda e, w=aud: w.config(bg=SURF3, fg=TEXT))
            aud.bind(
                "<ButtonRelease-1>",
                lambda e, p=ap, i=in_s, o=out_s: self._audition_cross(p, i, o))

            # Context body: sentence-ish window with the match tagged in
            # ACCENT so the user can see it in situ.
            ctx = tk.Text(row, height=2, wrap="word", bg=SURF2, fg=TEXT,
                          font=FB, relief="flat", bd=0,
                          padx=6, pady=0, highlightthickness=0)
            ctx.pack(fill="x", padx=6, pady=(2, 4))
            if before:  ctx.insert("end", "… " + before + " ")
            match_start = ctx.index("insert")
            ctx.insert("end", matched)
            match_end   = ctx.index("insert")
            if after:   ctx.insert("end", " " + after + " …")
            ctx.tag_add("match", match_start, match_end)
            ctx.tag_configure("match", foreground=ACCENT, font=FBT)
            ctx.configure(state="disabled")

    def _fmt_tc(self, secs):
        h, r = divmod(max(0.0, float(secs)), 3600)
        m, s = divmod(r, 60)
        return "{:02d}:{:02d}:{:02d}".format(int(h), int(m), int(s))

    def _audition_cross(self, other_path, start_s, duration_or_end_s):
        """Play a short clip from a DIFFERENT file than this pull's audio
        — used by the ▶ Audition buttons on cross-token search hits.
        Deliberately doesn't touch this dialog's own playhead / view /
        segments so switching between auditions doesn't disturb the
        review state you might want to keep if you decide NOT to adopt."""
        # Signature accepts either (start, duration) or (start, end); if the
        # second arg is bigger than start_s it's an END time.
        if duration_or_end_s > start_s:
            duration_s = max(0.4, duration_or_end_s - start_s)
        else:
            duration_s = max(0.4, duration_or_end_s)
        # Give the played clip a bit of tail so short matches (2-3
        # words) are actually audible in context.
        duration_s += 2.0
        import numpy as _np
        import wave as _wave
        out_wav = os.path.join(self._tmp_dir,
                                "_aud_{}.wav".format(int(start_s * 1000)))

        def _do():
            extract_audio_segment(other_path, max(0.0, start_s), duration_s,
                                   out_wav, sample_rate=_PLAYBACK_SR)
            with _wave.open(out_wav, "rb") as wf:
                data = wf.readframes(wf.getnframes())
            arr  = _np.frombuffer(data, _np.int16).astype(_np.float32) / 32768.0
            peak = float(_np.max(_np.abs(arr))) if len(arr) else 0.0
            if peak > 1e-6:
                arr = arr / peak * 0.85
            lead_samps = int(_PLAYBACK_SR * _LEAD_IN_MS / 1000)
            if lead_samps:
                arr = _np.concatenate(
                    [_np.zeros(lead_samps, dtype=_np.float32), arr])
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
                _log.exception("audition extract failed"); return
            def _do_play():
                try:
                    import winsound
                    winsound.PlaySound(None, winsound.SND_PURGE)
                    winsound.PlaySound(path,
                                       winsound.SND_FILENAME | winsound.SND_ASYNC)
                except Exception:
                    _log.exception("audition play failed")
            try: self._win.after(0, _do_play)
            except Exception: pass

        ex = ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(_do)
        fut.add_done_callback(_done)
        ex.shutdown(wait=False)

    def _xtok_adopt(self, new_token, new_audio_path, in_s, out_s):
        """User clicked ADOPT on a cross-token match.  Signals the
        caller by invoking on_accept with the reassignment payload
        (segments = single match window; new_token + new_audio_path
        piggyback in the kwargs) and closes the dialog.  Callers that
        don't care about reassignment (older signature) still get the
        segments arg and simply ignore the rest."""
        self._cleanup()
        if not self._on_accept:
            return
        segs = [(float(in_s), float(out_s))]
        try:
            self._on_accept(segs,
                            new_token=new_token,
                            new_audio_path=new_audio_path)
        except TypeError:
            # Caller uses the old single-arg signature — fall back
            # gracefully; the token change is dropped in that case.
            self._on_accept(segs)

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
        # Short-circuit no-op configures: Tk fires <Configure> repeatedly
        # as widgets settle their sizes on open, and each event was
        # triggering a full waveform + word-timeline redraw.  On real
        # sessions this stacked 5-10 identical redraws per open — most
        # of the "reloading graphics" jitter came from here.
        new_w = max(100, event.width)
        new_h = max(50,  event.height)
        _prev = getattr(self, "_last_resize_size", (0, 0))
        if (new_w, new_h) == _prev and not self._need_initial_zoom:
            return
        self._last_resize_size = (new_w, new_h)
        self._canvas_w = new_w
        self._canvas_h = new_h
        # Initial-zoom logic runs synchronously so the first paint uses
        # the correct dimensions; guarded to fire only once.
        if self._need_initial_zoom and event.width > 100 and self._samples is not None:
            self._need_initial_zoom = False
            pad = 3.0
            view_start_s = max(self._ctx_start, self._in_s - pad)
            view_end_s   = min(self._ctx_start + self._ctx_dur, self._out_s + pad)
            view_dur_s   = max(0.1, view_end_s - view_start_s)
            self._spp        = max(1, int(view_dur_s * self._sr / self._canvas_w))
            self._view_start = int((view_start_s - self._ctx_start) * self._sr)
        # Debounce the redraw: cancel any pending draw and schedule one
        # ~30 ms out.  Rapid Configure bursts collapse into a single
        # final redraw instead of 5-10 stacked.
        _prev_id = getattr(self, "_resize_after_id", None)
        if _prev_id is not None:
            try: self._win.after_cancel(_prev_id)
            except Exception: pass
        self._resize_after_id = self._win.after(30, self._draw)

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
        """Spacebar handler — stop if playing, play/edit if stopped.

        Always plays from the current playhead position — Jordan
        specifically wants "if I've moved the cursor elsewhere,
        playback starts from there".  Playhead defaults to _in_s at
        dialog init (line 229), so a truly-untouched dialog still
        plays from IN.  If a real bug ever shifts the playhead
        during load, fix it at the source rather than snapping here.
        """
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
            # Prepend silent lead-in — see _play() for rationale.  For
            # stitched playback the lead-in goes at the head of the
            # combined stream only (NOT between segments — that would
            # introduce gaps at every cut).
            lead_samps = int(_PLAYBACK_SR * _LEAD_IN_MS / 1000)
            if lead_samps:
                combined = _np.concatenate(
                    [_np.zeros(lead_samps, dtype=_np.float32), combined])
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
                # See _play() — offset the wall-clock start into the
                # future by _LEAD_IN_MS so the cursor doesn't move
                # during the silent lead-in.
                self._playback_start_wall = (
                    _time.perf_counter() + _LEAD_IN_MS / 1000.0)
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

    def _prune_empty_segments(self):
        """Drop any segments whose effective duration is below one frame
        (~40 ms at 24 fps), and merge adjacent segments whose boundaries
        touch (out == in).  Called after every mutation so the editor's
        segment list never falls into the "invisible sliver that can't
        be clicked" state Jordan hit — a zero-duration segment renders
        as no waveform region and doesn't fit a remove-segment button,
        yet still expands the edit area because its endpoints do count
        in the min/max span.

        Preserves the FIRST and LAST segments as sacred (they anchor
        _in_s / _out_s); only interior segments can be pruned by this
        pass.  The global IN/OUT get realigned to whichever kept
        segments now sit at the head/tail.
        """
        _EPS = 1.0 / max(1.0, float(getattr(self, "_fps", 24.0)))
        if len(self._segments) <= 1:
            return
        keep = []
        for s in self._segments:
            try:
                _in, _out = float(s[0]), float(s[1])
            except (TypeError, ValueError, IndexError):
                continue
            if _out - _in >= _EPS:
                keep.append([_in, _out])
        # Coalesce touching segments (out == next.in) so the "merge"
        # button doesn't have to be clicked for redundant boundaries.
        merged = []
        for s in keep:
            if merged and abs(merged[-1][1] - s[0]) < _EPS:
                merged[-1][1] = s[1]
            else:
                merged.append(list(s))
        # Guard: don't reduce to zero segments — leave at least one
        # even if everything was pruned (indicates a degenerate input).
        if not merged and self._segments:
            merged = [list(self._segments[0])]
        if merged != self._segments:
            self._segments = merged
            # Realign global handles to the new head/tail so the
            # displayed IN/OUT don't drift off into pruned space.
            self._in_s  = self._segments[0][0]
            self._out_s = self._segments[-1][1]

    def _remove_cut(self, seg_idx):
        """Merge segment (seg_idx-1) and (seg_idx), removing the cut between them."""
        n = len(self._segments)
        if seg_idx <= 0 or seg_idx >= n:
            return
        self._push_undo()
        # Extend the previous segment's out to the next segment's out, then delete next
        self._segments[seg_idx - 1][1] = self._segments[seg_idx][1]
        del self._segments[seg_idx]
        self._prune_empty_segments()
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
        self._prune_empty_segments()
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
                self._prune_empty_segments()
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
            # Prepend silent lead-in so Windows' PlaySound startup ramp
            # happens over silence and the real audio starts at full level.
            lead_samps = int(_PLAYBACK_SR * _LEAD_IN_MS / 1000)
            if lead_samps:
                arr = _np.concatenate(
                    [_np.zeros(lead_samps, dtype=_np.float32), arr])
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
                # Start playhead animation.  Anchor the wall-clock start
                # _LEAD_IN_MS in the FUTURE so the visual cursor doesn't
                # advance while the silent lead-in is playing — the
                # cursor then reaches start_s exactly when the real
                # audio does.
                self._playhead_s             = start_s
                self._playback_start_wall    = (
                    _time.perf_counter() + _LEAD_IN_MS / 1000.0)
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
        # Clamp elapsed at 0 — the play routines anchor start_wall a bit
        # in the future (by _LEAD_IN_MS) so the visual cursor doesn't
        # slide during the silent lead-in that absorbs Windows' audio
        # startup ramp.
        elapsed = max(0.0,
                      _time.perf_counter() - self._playback_start_wall)
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
