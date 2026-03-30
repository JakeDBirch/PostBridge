"""
Match Review Dialog — waveform + in/out editor for reconciled interview pulls and VO blocks.

Opens from the Step 4 review panel so the user can visually verify and adjust
the edit points found by the reconciler.  Shows a context window of the source
audio waveform with the matched region highlighted and draggable IN/OUT markers.
"""

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
                    SUCCESS, WARN, FB, FBT, FH)
from engines import extract_audio_segment

# ── Constants ─────────────────────────────────────────────────────────────────
_SR          = 8000     # waveform display sample rate
_PLAYBACK_SR = 44100    # playback sample rate
_CONTEXT_S   = 6.0      # seconds of context on each side of the match
_MARKER_HIT  = 10       # pixel radius for grabbing IN/OUT markers

_MATCH_FILL    = "#152830"   # matched region background
_WAVE_FILL     = "#253a48"   # waveform fill polygon
_WAVE_OUTLINE  = "#4a8fa8"   # waveform outline


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
                 quote_text="", on_accept=None, fps=24.0):
        self._parent      = parent
        self._audio_path  = source_audio
        self._on_accept   = on_accept
        self._fps         = fps
        self._frame_s     = 1.0 / max(1.0, fps)
        self._sr          = _SR

        # Working copy — we edit the overall span (first in / last out).
        # Interior segment boundaries stay fixed; on accept we shift
        # the first-in and last-out by the same deltas the user applied.
        self._segments   = [list(s) for s in segments]  # mutable copy
        self._in_s       = self._segments[0][0]  if self._segments else 0.0
        self._out_s      = self._segments[-1][1] if self._segments else 0.0
        self._orig_in_s  = self._in_s
        self._orig_out_s = self._out_s

        # Context window extracted for display
        self._ctx_start  = max(0.0, self._in_s - _CONTEXT_S)
        self._ctx_dur    = (self._out_s + _CONTEXT_S) - self._ctx_start
        self._samples    = None   # numpy array populated by background thread

        # View state
        self._canvas_w   = 880
        self._canvas_h   = 200
        self._view_start = 0      # first visible sample (within ctx window)
        self._spp        = 1      # samples per pixel

        # Drag state: None, "in", or "out"
        self._drag = None

        # Temp dir for playback clips
        self._tmp_dir = tempfile.mkdtemp(prefix="pb_match_")

        # ── Build window ──────────────────────────────────────────────────
        self._win = tk.Toplevel(parent)
        disp_title = "MATCH REVIEW"
        if title:
            disp_title += "  \u2014  " + title
        self._win.title(disp_title)
        self._win.configure(bg=BG)
        self._win.resizable(True, False)
        self._win.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self._win.bind("<Escape>", lambda e: self._on_cancel())

        self._win.update_idletasks()
        pw = parent.winfo_width();  ph = parent.winfo_height()
        px = parent.winfo_x();      py = parent.winfo_y()
        w, h = 960, 520
        self._win.geometry("{}x{}+{}+{}".format(
            w, h, max(0, px + (pw - w) // 2), max(0, py + (ph - h) // 2)))

        self._build_ui(quote_text)
        self._win.grab_set()
        self._win.bind("<MouseWheel>", lambda e: "break")
        self._start_extraction()

    # ── UI ────────────────────────────────────────────────────────────────

    def _build_ui(self, quote_text):
        win = self._win

        # ── Quote text (scrollable if long) ───────────────────────────────
        if quote_text:
            qf = tk.Frame(win, bg=SURF2,
                          highlightbackground=BORDER, highlightthickness=1)
            qf.pack(fill="x", padx=12, pady=(10, 2))
            q = quote_text if len(quote_text) <= 140 else quote_text[:137] + "\u2026"
            tk.Label(qf, text=q, font=FB, bg=SURF2, fg=SUB,
                     anchor="w", wraplength=900, justify="left",
                     padx=8, pady=4).pack(fill="x")

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
        self._refresh_displays()

        # ── Nudge row ─────────────────────────────────────────────────────
        nrow = tk.Frame(win, bg=BG)
        nrow.pack(fill="x", padx=12, pady=(4, 2))

        def _nudge_btn(parent, label, side, delta):
            b = tk.Label(parent, text=label, font=FB, bg=SURF3, fg=TEXT,
                         cursor="hand2", padx=8, pady=2, bd=0,
                         highlightbackground=BORDER, highlightthickness=1)
            b.pack(side="left", padx=1)
            b.bind("<Enter>", lambda e: b.config(bg=ACCENT))
            b.bind("<Leave>", lambda e: b.config(bg=SURF3))
            b.bind("<ButtonRelease-1>",
                   lambda e, s=side, d=delta: self._nudge(s, d))

        tk.Label(nrow, text="IN", font=FB, bg=BG, fg=SUCCESS,
                 width=4).pack(side="left")
        _nudge_btn(nrow, "\u25c0", "in", -1)
        _nudge_btn(nrow, "\u25b6", "in",  1)

        tk.Frame(nrow, bg=BG, width=16).pack(side="left")
        tk.Label(nrow, text="OUT", font=FB, bg=BG, fg=WARN,
                 width=4).pack(side="left")
        _nudge_btn(nrow, "\u25c0", "out", -1)
        _nudge_btn(nrow, "\u25b6", "out",  1)

        tk.Label(nrow, text="  (1 frame = {:.2f}ms)".format(self._frame_s * 1000),
                 font=FB, bg=BG, fg=SUB).pack(side="left", padx=(8, 0))

        # Keyboard nudge: ←/→ moves IN, Shift-←/→ moves OUT
        win.bind("<Left>",        lambda e: self._nudge("in",  -1))
        win.bind("<Right>",       lambda e: self._nudge("in",   1))
        win.bind("<Shift-Left>",  lambda e: self._nudge("out", -1))
        win.bind("<Shift-Right>", lambda e: self._nudge("out",  1))

        # ── Playback row ──────────────────────────────────────────────────
        prow = tk.Frame(win, bg=BG)
        prow.pack(fill="x", padx=12, pady=(4, 2))

        for label, cmd in [
            ("\u25b6 PLAY MATCH",   self._play_match),
            ("\u25b6 PLAY CONTEXT", self._play_context),
            ("\u25a0 STOP",         self._stop),
        ]:
            b = tk.Label(prow, text=label, font=FB, bg=SURF3, fg=TEXT,
                         cursor="hand2", padx=10, pady=4, bd=0,
                         highlightbackground=BORDER, highlightthickness=1)
            b.pack(side="left", padx=(0, 6))
            b.bind("<Enter>", lambda e, w=b: w.config(bg=ACCENT))
            b.bind("<Leave>", lambda e, w=b: w.config(bg=SURF3))
            b.bind("<ButtonRelease-1>", lambda e, f=cmd: f())

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

    def _start_extraction(self):
        ex = ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(self._extract)
        fut.add_done_callback(self._extraction_done)
        ex.shutdown(wait=False)

    def _extract(self):
        import numpy as _np
        import wave as _wave
        _log.info("extracting context: %s  start=%.2f  dur=%.2f",
                  self._audio_path, self._ctx_start, self._ctx_dur)
        wav = os.path.join(self._tmp_dir, "_ctx.wav")
        extract_audio_segment(self._audio_path, self._ctx_start,
                               self._ctx_dur, wav, sample_rate=_SR)
        with _wave.open(wav, "rb") as wf:
            data = wf.readframes(wf.getnframes())
        arr = _np.frombuffer(data, _np.int16).astype(_np.float32) / 32768.0
        peak = float(_np.max(_np.abs(arr))) if len(arr) else 0.0
        if peak > 1e-6:
            arr = arr / peak * 0.85
        return arr

    def _extraction_done(self, fut):
        try:
            if not self._win.winfo_exists():
                return
        except Exception:
            return
        try:
            samples = fut.result()
        except Exception as exc:
            _log.exception("extraction failed")
            def _err():
                try:
                    self._loading_lbl.config(
                        text="Extraction failed: {}".format(exc))
                except Exception:
                    pass
            self._win.after(0, _err)
            return

        def _ready():
            try:
                if not self._win.winfo_exists():
                    return
                self._samples = samples
                self._loading_lbl.destroy()
                self._canvas_w = max(100, self._cv.winfo_width())
                self._canvas_h = max(50,  self._cv.winfo_height())
                n = len(samples)
                self._spp = max(1, n // self._canvas_w)
                self._view_start = 0
                self._draw()
            except Exception:
                _log.exception("_ready failed")
        self._win.after(0, _ready)

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

        # ── Matched region background ──────────────────────────────────────
        in_px  = _t_to_px(self._in_s)
        out_px = _t_to_px(self._out_s)
        if in_px < w and out_px > 0:
            cv.create_rectangle(max(0, in_px), 0, min(w, out_px), h,
                                 fill=_MATCH_FILL, outline="")

        # ── Waveform polygon ───────────────────────────────────────────────
        b_min, b_max = _waveform_bins(w, vs, ve)
        pts = []
        for i in range(w):
            pts.append(i);     pts.append(int(mid - b_max[i] * scale))
        for i in range(w - 1, -1, -1):
            pts.append(i);     pts.append(int(mid - b_min[i] * scale))
        if len(pts) >= 6:
            cv.create_polygon(pts, fill=_WAVE_FILL, outline=_WAVE_OUTLINE, width=1)

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

        # ── Multi-segment interior boundaries (read-only guides) ──────────
        if len(self._segments) > 1:
            for i, (seg_in, seg_out) in enumerate(self._segments):
                if i > 0:
                    px = _t_to_px(seg_in)
                    if 0 <= px <= w:
                        cv.create_line(px, 0, px, h, fill=ACCENT,
                                       width=1, dash=(4, 4))
                if i < len(self._segments) - 1:
                    px = _t_to_px(seg_out)
                    if 0 <= px <= w:
                        cv.create_line(px, 0, px, h, fill=ACCENT,
                                       width=1, dash=(4, 4))

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
        in_px  = self._t_to_px(self._in_s)
        out_px = self._t_to_px(self._out_s)
        if abs(event.x - in_px) <= _MARKER_HIT:
            self._drag = "in"
        elif abs(event.x - out_px) <= _MARKER_HIT:
            self._drag = "out"
        else:
            self._drag = None

    def _on_drag_motion(self, event):
        if not self._drag or self._samples is None:
            return
        t = max(0.0, self._px_to_t(event.x))
        if self._drag == "in":
            self._in_s = min(t, self._out_s - self._frame_s)
        else:
            self._out_s = max(t, self._in_s + self._frame_s)
        self._refresh_displays()
        self._draw()

    def _on_release(self, event):
        self._drag = None

    def _on_scroll(self, event):
        if event.state & 0x4:
            self._zoom_by(1.0 / 1.5 if event.delta > 0 else 1.5, event.x)
        else:
            self._pan(event)
        return "break"

    def _pan(self, event):
        if self._samples is None:
            return
        delta    = -event.delta // 120 * self._spp * 50
        n        = len(self._samples)
        max_vs   = max(0, n - self._canvas_w * self._spp)
        self._view_start = max(0, min(max_vs, self._view_start + delta))
        self._draw()

    def _zoom_by(self, factor, mouse_x=None):
        if self._samples is None:
            return
        n   = len(self._samples)
        w   = max(1, self._canvas_w)
        old = self._spp
        new = max(1, int(old * factor))
        new = max(1, min(max(1, n // w), new))
        if new == old:
            return
        if mouse_x is not None:
            anchor = self._view_start + mouse_x * old
            self._view_start = max(0, int(anchor - mouse_x * new))
        else:
            centre = self._view_start + (w * old) // 2
            self._view_start = max(0, centre - (w * new) // 2)
        self._spp = new
        self._draw()

    def _on_resize(self, event):
        self._canvas_w = max(100, event.width)
        self._canvas_h = max(50, event.height)
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
            self._in_s = max(0.0, min(t, self._out_s - self._frame_s))
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
        self._dur_var.set("{:.3f}s  \u2014  {:.3f}s  ({:.3f}s)".format(
            self._in_s, self._out_s, dur))

    # ── Playback ──────────────────────────────────────────────────────────

    def _play_match(self):
        """Play the matched region with a short pre-roll tail."""
        start = max(0.0, self._in_s - 0.2)
        dur   = (self._out_s - start) + 0.2
        self._play(start, dur)

    def _play_context(self):
        """Play 2.5 s before IN through 2.5 s after OUT."""
        pre   = 2.5
        start = max(0.0, self._in_s - pre)
        dur   = (self._out_s - self._in_s) + pre * 2
        self._play(start, dur)

    def _play(self, start_s, duration_s):
        self._stop()
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
                arr = arr / peak * 0.72
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
            try:
                self._win.after(0, _do_play)
            except Exception:
                pass

        ex = ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(_do)
        fut.add_done_callback(_done)
        ex.shutdown(wait=False)

    def _stop(self):
        try:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass

    # ── Accept / Cancel ───────────────────────────────────────────────────

    def _on_accept_click(self):
        # Apply delta to first-in and last-out; interior boundaries unchanged.
        d_in  = self._in_s  - self._orig_in_s
        d_out = self._out_s - self._orig_out_s
        new_segs = [list(s) for s in self._segments]
        new_segs[0][0]  += d_in
        new_segs[-1][1] += d_out
        # Clamp to ensure no segment inverts
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
