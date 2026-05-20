"""
Sync Preview Dialog — visual + aural waveform alignment tool.

Opens after auto-sync runs so the user can verify or manually adjust
the offset between a video file's embedded audio and a reference
audio recording.
"""

import logging
import os
import tempfile
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor

# ── Crash log ─────────────────────────────────────────────────────────────────
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "sync_preview_log.txt")
logging.basicConfig(
    filename=_LOG_PATH,
    filemode="w",            # overwrite each session
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)s  %(message)s",
)
_log = logging.getLogger("sync_preview")

from config import (BG, SURF, SURF2, SURF3, BORDER, ACCENT, TEXT, SUB,
                    SUCCESS, WARN, FB, FBT, FH, WAVE_REF, WAVE_VID)
from engines import extract_mono_pcm, extract_audio_segment

# ── Constants ─────────────────────────────────────────────────────────────────
_SR           = 8000       # extraction sample rate for waveform display
_PLAYBACK_SR  = 44100      # extraction sample rate for playback clips
_PLAY_DUR     = 3.0        # playback clip duration (seconds)
_FRAME_S      = 1.0 / 60   # one frame at 60 fps
_NUDGE_SAMP   = int(_FRAME_S * _SR)  # samples per single-frame nudge

# Blended fill colours (tinted towards BG so overlapping regions stay readable)
_REF_FILL = "#1a4038"      # teal tinted dark
_VID_FILL = "#3a2510"      # orange tinted dark



class SyncPreviewDialog:
    """
    Toplevel waveform-overlay dialog for manual sync adjustment.

    Parameters
    ----------
    parent : tk.Tk
        Main application window.
    video_path : str
        Path to video file (camera recording with embedded audio).
    audio_path : str
        Path to reference audio file (studio / DAW recording).
    initial_offset : float
        Starting offset in seconds (from auto-sync or previous value).
    on_accept : callable(float)
        Called with the accepted offset in seconds when the user clicks Accept.
    source_name : str
        Display name shown in the title bar.
    """

    def __init__(self, parent, video_path, audio_path, initial_offset=0.0,
                 on_accept=None, source_name="", candidates=None):
        self._parent = parent
        self._vp = video_path
        self._ap = audio_path
        self._on_accept = on_accept
        self._sr = _SR

        # Auto-sync alternative candidates ((T_secs, conf), ...) the user
        # can audition when the auto pick is wrong.  The "Try next candidate"
        # button cycles through this list.  An entry for the initial offset
        # is prepended so the user can come back to it.
        self._candidates = [(float(initial_offset), 1.0)] + [
            (float(t), float(c))
            for (t, c) in (candidates or [])
            if abs(float(t) - float(initial_offset)) > 0.01
        ]
        self._candidate_idx = 0

        # Offset state (in samples at _SR).
        # Internally stored negated relative to the semantic v_offset so that
        # the display formula  vid_vs = vs - _offset_samples  shows the correct
        # visual alignment: positive v_offset (camera ahead of DAW) → video
        # waveform shifted left (earlier video content on screen).
        self._offset_samples = -int(round(initial_offset * _SR))

        # Waveform data (populated by background thread)
        self._vid_samples = None
        self._ref_samples = None

        # View state
        self._view_start = 0          # first visible sample (in ref coords)
        self._samples_per_px = 1      # set after extraction
        self._canvas_w = 850
        self._canvas_h = 280

        # Drag state
        self._drag_x0 = None
        self._drag_offset0 = 0

        # Temp dir for playback clips
        self._tmp_dir = tempfile.mkdtemp(prefix="pb_sync_")

        # ── Build the window ──────────────────────────────────────────────
        self._win = tk.Toplevel(parent)
        title = "Sync Preview"
        if source_name:
            title += " \u2014 " + source_name
        self._win.title(title)
        self._win.configure(bg=BG)
        self._win.resizable(True, False)
        self._win.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self._win.bind("<Escape>", lambda e: self._on_cancel())

        # Centre on parent
        self._win.update_idletasks()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        px, py = parent.winfo_x(), parent.winfo_y()
        w, h = 920, 520
        x = px + (pw - w) // 2
        y = py + (ph - h) // 2
        self._win.geometry("{}x{}+{}+{}".format(w, h, max(0, x), max(0, y)))

        self._build_ui()
        # Capture all input while the dialog is open so scroll events don't
        # bleed through to the underlying panel.
        self._win.grab_set()
        # Block MouseWheel on the whole dialog window so that scrolling over
        # non-canvas widgets (buttons, labels) doesn't reach the parent panel.
        self._win.bind("<MouseWheel>", lambda e: "break")
        self._start_extraction()

    # ── UI Construction ───────────────────────────────────────────────────

    def _build_ui(self):
        win = self._win

        # Legend row
        leg = tk.Frame(win, bg=BG)
        leg.pack(fill="x", padx=12, pady=(10, 2))
        tk.Label(leg, text="\u25a0", fg=WAVE_VID, bg=BG,
                 font=FB).pack(side="left")
        tk.Label(leg, text="video mic", fg=SUB, bg=BG,
                 font=FB).pack(side="left", padx=(2, 14))
        tk.Label(leg, text="\u25a0", fg=WAVE_REF, bg=BG,
                 font=FB).pack(side="left")
        tk.Label(leg, text="reference audio", fg=SUB, bg=BG,
                 font=FB).pack(side="left", padx=(2, 0))

        # Canvas
        cf = tk.Frame(win, bg=BORDER, bd=1, relief="solid")
        cf.pack(fill="both", expand=True, padx=12, pady=4)
        self._cv = tk.Canvas(cf, bg=SURF, highlightthickness=0,
                             height=self._canvas_h)
        self._cv.pack(fill="both", expand=True)

        # Loading label (shown until waveforms are ready)
        self._loading_lbl = tk.Label(self._cv, text="Extracting waveforms\u2026",
                                     font=FBT, fg=SUB, bg=SURF)
        self._loading_lbl.place(relx=0.5, rely=0.5, anchor="center")

        # Canvas events (bound now, but only effective after data loaded)
        self._cv.bind("<ButtonPress-1>", self._on_drag_start)
        self._cv.bind("<B1-Motion>", self._on_drag_motion)
        self._cv.bind("<ButtonRelease-1>", self._on_drag_end)
        self._cv.bind("<MouseWheel>", self._on_scroll)
        self._cv.bind("<Configure>", self._on_canvas_resize)

        # Zoom + offset row
        ctrl = tk.Frame(win, bg=BG)
        ctrl.pack(fill="x", padx=12, pady=(6, 2))

        tk.Label(ctrl, text="Zoom:", fg=SUB, bg=BG, font=FB).pack(side="left")
        for sym, fn in [("−", self._zoom_out), ("+", self._zoom_in)]:
            b = tk.Label(ctrl, text=sym, font=FBT, bg=SURF3, fg=TEXT,
                         cursor="hand2", padx=10, pady=2, bd=0,
                         highlightbackground=BORDER, highlightthickness=1)
            b.pack(side="left", padx=(4, 0))
            b.bind("<Enter>", lambda e, w=b: w.config(bg=ACCENT))
            b.bind("<Leave>", lambda e, w=b: w.config(bg=SURF3))
            b.bind("<ButtonRelease-1>", lambda e, f=fn: f())

        # Spacer
        tk.Frame(ctrl, bg=BG, width=20).pack(side="left")

        tk.Label(ctrl, text="Offset:", fg=SUB, bg=BG, font=FB).pack(side="left")
        self._offset_var = tk.StringVar(value=self._fmt_offset())
        tk.Label(ctrl, textvariable=self._offset_var, fg=TEXT, bg=BG,
                 font=FBT, width=12, anchor="w").pack(side="left", padx=(4, 8))

        # Nudge buttons
        for sym, delta in [("\u25c0", -1), ("\u25b6", 1)]:
            b = tk.Label(ctrl, text=sym, font=FB, bg=SURF3, fg=TEXT,
                         cursor="hand2", padx=6, pady=2, bd=0,
                         highlightbackground=BORDER, highlightthickness=1)
            b.pack(side="left", padx=1)
            b.bind("<Enter>", lambda e, w=b: w.config(bg=ACCENT))
            b.bind("<Leave>", lambda e, w=b: w.config(bg=SURF3))
            b.bind("<ButtonRelease-1>",
                   lambda e, d=delta: self._nudge(d))

        # Keyboard nudge
        win.bind("<Left>", lambda e: self._nudge(-1))
        win.bind("<Right>", lambda e: self._nudge(1))
        win.bind("<Shift-Left>", lambda e: self._nudge(-10))
        win.bind("<Shift-Right>", lambda e: self._nudge(10))

        # Playback row
        play_row = tk.Frame(win, bg=BG)
        play_row.pack(fill="x", padx=12, pady=(6, 2))

        for label, cmd in [("\u25b6 PLAY MIX", self._play_mix),
                           ("\u25b6 REF ONLY", self._play_ref),
                           ("\u25b6 VID ONLY", self._play_vid),
                           ("\u25a0 STOP", self._stop)]:
            b = tk.Label(play_row, text=label, font=FB, bg=SURF3, fg=TEXT,
                         cursor="hand2", padx=10, pady=4, bd=0,
                         highlightbackground=BORDER, highlightthickness=1)
            b.pack(side="left", padx=(0, 6))
            b.bind("<Enter>", lambda e, w=b: w.config(bg=ACCENT))
            b.bind("<Leave>", lambda e, w=b: w.config(bg=SURF3))
            b.bind("<ButtonRelease-1>", lambda e, f=cmd: f())

        # \u2500\u2500 Candidate audition row \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        # When auto-sync returned alternative offset candidates, surface a
        # "Try next" button that cycles through them.  Useful when the
        # primary auto pick is wrong but a runner-up xcorr peak is right \u2014
        # the user can quickly audition each candidate visually + aurally
        # instead of nudging a slider.
        if len(self._candidates) > 1:
            cand_row = tk.Frame(win, bg=BG)
            cand_row.pack(fill="x", padx=12, pady=(6, 2))
            tk.Label(cand_row,
                     text="Auto-sync had {} candidates:".format(
                         len(self._candidates)),
                     font=FB, bg=BG, fg=SUB).pack(side="left")
            self._cand_lbl = tk.Label(
                cand_row,
                text=self._candidate_label_text(),
                font=FB, bg=BG, fg=TEXT)
            self._cand_lbl.pack(side="left", padx=(4, 12))

            nxt = tk.Label(cand_row, text="TRY NEXT \u25b6", font=FBT,
                            bg=SURF3, fg=TEXT, cursor="hand2",
                            padx=10, pady=4, bd=0,
                            highlightbackground=BORDER,
                            highlightthickness=1)
            nxt.pack(side="left")
            nxt.bind("<Enter>", lambda e, w=nxt: w.config(bg=ACCENT))
            nxt.bind("<Leave>", lambda e, w=nxt: w.config(bg=SURF3))
            nxt.bind("<ButtonRelease-1>",
                     lambda e: self._cycle_candidate())
        else:
            self._cand_lbl = None

    def _candidate_label_text(self):
        """Format the candidate-row readout: 'showing #i of N \u00b7 -5.42 s
        (conf 62%)'.  The auto-pick at index 0 is labelled 'auto' instead
        of a confidence to avoid confusion (its conf is synthetic 1.0)."""
        i = self._candidate_idx
        n = len(self._candidates)
        t, c = self._candidates[i]
        if i == 0:
            tail = "auto pick"
        else:
            tail = "{:+.3f} s   conf {:d}%".format(t, int(round(c * 100)))
        return "  showing #{} of {}   \u00b7   {}".format(i + 1, n, tail)

        # Accept / Cancel row
        bot = tk.Frame(win, bg=BG)
        bot.pack(fill="x", padx=12, pady=(10, 12))

        # Both buttons pack right-aligned with an 8 px gap so they read
        # as a single tight action group instead of CANCEL hugging the
        # left edge and ACCEPT floating on the right.
        _W = 13
        accept = tk.Label(bot, text="ACCEPT OFFSET", font=FBT, bg=SUCCESS,
                          fg=TEXT, cursor="hand2", padx=16, pady=6, bd=0,
                          width=_W, anchor="center",
                          highlightbackground=BORDER, highlightthickness=1)
        accept.pack(side="right")
        accept.bind("<Enter>", lambda e: accept.config(bg="#4a9a51"))
        accept.bind("<Leave>", lambda e: accept.config(bg=SUCCESS))
        accept.bind("<ButtonRelease-1>", lambda e: self._on_accept_click())

        cancel = tk.Label(bot, text="CANCEL", font=FBT, bg=SURF3, fg=TEXT,
                          cursor="hand2", padx=16, pady=6, bd=0,
                          width=_W, anchor="center",
                          highlightbackground=BORDER, highlightthickness=1)
        cancel.pack(side="right", padx=(0, 8))
        cancel.bind("<Enter>", lambda e: cancel.config(bg="#5a2020"))
        cancel.bind("<Leave>", lambda e: cancel.config(bg=SURF3))
        cancel.bind("<ButtonRelease-1>", lambda e: self._on_cancel())

    # ── Waveform Extraction (background) ──────────────────────────────────

    def _start_extraction(self):
        ex = ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(self._extract)
        fut.add_done_callback(self._extraction_done)
        ex.shutdown(wait=False)

    def _extract(self):
        _log.info("extracting waveforms: vid=%s  ref=%s", self._vp, self._ap)
        import numpy as _np
        vid = extract_mono_pcm(self._vp, sample_rate=_SR)
        ref = extract_mono_pcm(self._ap, sample_rate=_SR)
        _log.info("extracted  vid=%d samples  ref=%d samples", len(vid), len(ref))
        # Peak-normalise each waveform independently so neither clips visually.
        # (RMS normalisation with speech crest factors of 10-15 dB causes the
        # peaks to shoot well past the canvas height.)
        peak_v = float(_np.max(_np.abs(vid))) if len(vid) else 0.0
        peak_r = float(_np.max(_np.abs(ref))) if len(ref) else 0.0
        if peak_v > 1e-6:
            vid = vid / peak_v * 0.85
        if peak_r > 1e-6:
            ref = ref / peak_r * 0.85
        return vid, ref

    def _extraction_done(self, fut):
        # Guard against callbacks firing after the window was closed
        try:
            if not self._win.winfo_exists():
                return
        except Exception:
            return

        try:
            vid, ref = fut.result()
        except Exception as exc:
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
                self._vid_samples = vid
                self._ref_samples = ref
                self._loading_lbl.destroy()
                # Initial zoom: show the full reference file
                self._canvas_w = max(100, self._cv.winfo_width())
                self._canvas_h = max(50, self._cv.winfo_height())
                n_ref = len(ref)
                self._samples_per_px = max(1, n_ref // self._canvas_w)
                self._view_start = 0
                self._draw()
            except Exception:
                pass
        self._win.after(0, _ready)

    # ── Waveform Rendering ────────────────────────────────────────────────

    @staticmethod
    def _compute_bins(samples, width, view_start, view_end):
        """Downsample samples[view_start:view_end] to *width* min/max bins.

        view_start / view_end may extend outside [0, len(samples)].
        Out-of-bounds regions are zero-padded so the waveform holds its
        shape and position rather than stretching to fill the canvas.
        """
        import numpy as _np
        view_start = int(view_start)
        view_end   = int(view_end)
        total      = view_end - view_start
        if total <= 0 or width <= 0:
            return _np.zeros(width), _np.zeros(width)

        clamp_s = max(0, view_start)
        clamp_e = min(len(samples), view_end)

        mins = _np.zeros(width)
        maxs = _np.zeros(width)

        if clamp_s >= clamp_e:          # entirely out of range
            return mins, maxs

        # Pixel positions of the valid sample region within the output array
        px_start = int(round((clamp_s - view_start) / total * width))
        px_end   = min(width, int(round((clamp_e - view_start) / total * width)))
        px_w     = px_end - px_start
        if px_w <= 0:
            return mins, maxs

        chunk = samples[clamp_s:clamp_e]
        n = len(chunk)
        if n == 0:
            return mins, maxs
        if n <= px_w:
            mins[px_start:px_start + n] = chunk
            maxs[px_start:px_start + n] = chunk
        else:
            bin_size = n // px_w
            trim     = bin_size * px_w
            reshaped = chunk[:trim].reshape(px_w, bin_size)
            mins[px_start:px_end] = reshaped.min(axis=1)
            maxs[px_start:px_end] = reshaped.max(axis=1)

        return mins, maxs

    def _draw(self):
        if self._ref_samples is None:
            return
        cv = self._cv
        cv.delete("all")
        w = self._canvas_w
        h = self._canvas_h
        mid = h // 2
        spp = self._samples_per_px
        vs = self._view_start
        ve = vs + w * spp

        # Scale factor: amplitude → pixels (±half height, leave 10px margin)
        scale = (mid - 10)

        # ── Helper: build a waveform polygon from min/max bins ─────────────
        def _wave_poly(bins_min, bins_max):
            """Return a flat coordinate list tracing max L→R then min R→L."""
            pts = []
            for i in range(w):
                pts.append(i)
                pts.append(int(mid - bins_max[i] * scale))
            for i in range(w - 1, -1, -1):
                pts.append(i)
                pts.append(int(mid - bins_min[i] * scale))
            return pts

        # ── Reference waveform (fixed) ────────────────────────────────────
        r_min, r_max = self._compute_bins(self._ref_samples, w, vs, ve)
        ref_poly = _wave_poly(r_min, r_max)
        if len(ref_poly) >= 6:
            cv.create_polygon(ref_poly, fill=_REF_FILL, outline=WAVE_REF,
                              width=1)

        # ── Video waveform (shifted by offset) ────────────────────────────
        vid_vs = vs - self._offset_samples
        vid_ve = vid_vs + w * spp
        v_min, v_max = self._compute_bins(self._vid_samples, w, vid_vs, vid_ve)
        vid_poly = _wave_poly(v_min, v_max)
        if len(vid_poly) >= 6:
            cv.create_polygon(vid_poly, fill=_VID_FILL, outline=WAVE_VID,
                              width=1)

        # ── Centre line ───────────────────────────────────────────────────
        cv.create_line(0, mid, w, mid, fill=BORDER, dash=(2, 4))

        # ── Time axis ─────────────────────────────────────────────────────
        total_view_s = (w * spp) / self._sr
        # Choose a nice tick interval
        for tick_s in [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300]:
            if total_view_s / tick_s <= 20:
                break
        start_s = vs / self._sr
        first_tick = int(start_s / tick_s) * tick_s
        t = first_tick
        while True:
            px = int((t * self._sr - vs) / spp)
            if px > w:
                break
            if px >= 0:
                cv.create_line(px, h - 18, px, h - 12, fill=SUB)
                minutes = int(t) // 60
                seconds = t - minutes * 60
                if tick_s >= 1:
                    lbl = "{}:{:02d}".format(minutes, int(seconds))
                else:
                    lbl = "{}:{:04.1f}".format(minutes, seconds)
                cv.create_text(px, h - 8, text=lbl, fill=SUB,
                               font=("Segoe UI", 8), anchor="s")
            t += tick_s

    # ── Interaction ───────────────────────────────────────────────────────

    def _on_canvas_resize(self, event):
        self._canvas_w = max(100, event.width)
        self._canvas_h = max(50, event.height)
        self._draw()

    def _on_drag_start(self, event):
        self._drag_x0 = event.x
        self._drag_offset0 = self._offset_samples

    def _on_drag_motion(self, event):
        if self._drag_x0 is None or self._ref_samples is None:
            return
        dx_px = event.x - self._drag_x0
        dx_samp = dx_px * self._samples_per_px
        self._offset_samples = self._drag_offset0 + int(dx_samp)
        self._offset_var.set(self._fmt_offset())
        self._draw()

    def _on_drag_end(self, event):
        self._drag_x0 = None

    def _on_scroll(self, event):
        """MouseWheel — pan normally; hold Ctrl to zoom centred on cursor."""
        if event.state & 0x4:          # Ctrl held → zoom
            self._zoom_by(1.0 / 1.5 if event.delta > 0 else 1.5,
                          mouse_x=event.x)
        else:
            self._on_pan(event)
        return "break"  # prevent propagation to parent window

    def _on_pan(self, event):
        if self._ref_samples is None:
            return
        delta = -event.delta // 120 * self._samples_per_px * 50
        n_ref = len(self._ref_samples)
        max_start = max(0, n_ref - self._canvas_w * self._samples_per_px)
        self._view_start = max(0, min(max_start, self._view_start + delta))
        self._draw()

    def _zoom_in(self):
        """Zoom in by 2×, keeping the view centred."""
        self._zoom_by(0.5)

    def _zoom_out(self):
        """Zoom out by 2×, keeping the view centred."""
        self._zoom_by(2.0)

    def _zoom_by(self, factor, mouse_x=None):
        if self._ref_samples is None:
            return
        n_ref  = len(self._ref_samples)
        w      = max(1, self._canvas_w)
        old    = self._samples_per_px
        new    = max(1, int(old * factor))
        max_sp = max(1, n_ref // w)
        min_sp = max(1, int(self._sr // w))    # ~1 s minimum span
        new    = max(min_sp, min(max_sp, new))
        if new == old:
            return
        if mouse_x is not None:
            # Keep the sample under the mouse cursor fixed in place
            anchor_samp = self._view_start + mouse_x * old
            self._view_start = max(0, int(anchor_samp - mouse_x * new))
        else:
            # Keep view centre fixed (used by +/- buttons)
            centre           = self._view_start + (w * old) // 2
            self._view_start = max(0, centre - (w * new) // 2)
        self._samples_per_px = new
        self._draw()

    def _nudge(self, frames):
        """Shift offset by *frames* video frames (1/60 s each)."""
        if self._ref_samples is None:
            return
        self._offset_samples += frames * _NUDGE_SAMP
        self._offset_var.set(self._fmt_offset())
        self._draw()

    def _cycle_candidate(self):
        """Advance to the next auto-sync candidate offset (cycles back to
        the start when the list is exhausted).  Updates the waveform view
        and the offset readout so the user can audition the alternative
        visually + via the playback row."""
        if len(self._candidates) <= 1:
            return
        self._stop()
        self._candidate_idx = (self._candidate_idx + 1) % len(self._candidates)
        t_secs, _conf = self._candidates[self._candidate_idx]
        self._offset_samples = -int(round(t_secs * _SR))
        if hasattr(self, "_offset_var"):
            try:
                self._offset_var.set(self._fmt_offset())
            except Exception:
                pass
        if self._cand_lbl is not None:
            try:
                self._cand_lbl.config(text=self._candidate_label_text())
            except Exception:
                pass
        self._draw()

    # ── Playback ──────────────────────────────────────────────────────────

    def _play_mix(self):
        self._play(mix=True)

    def _play_ref(self):
        self._play(ref_only=True)

    def _play_vid(self):
        self._play(vid_only=True)

    def _play(self, mix=False, ref_only=False, vid_only=False):
        """Extract + play a short clip centred on the current view."""
        if self._ref_samples is None:
            return
        self._stop()
        _log.info("play requested  mix=%s ref=%s vid=%s", mix, ref_only, vid_only)

        # Centre of current view in ref-time seconds
        centre_s = (self._view_start +
                    self._canvas_w * self._samples_per_px // 2) / self._sr
        ref_start = max(0.0, centre_s - _PLAY_DUR / 2)
        vid_start = max(0.0, ref_start - self._offset_samples / self._sr)

        def _extract_and_play():
            try:
                import numpy as _np
                import wave as _wave

                ref_wav = os.path.join(self._tmp_dir, "_ref.wav")
                vid_wav = os.path.join(self._tmp_dir, "_vid.wav")
                out_wav = os.path.join(self._tmp_dir, "_play.wav")

                _log.info("extracting segments  ref_start=%.2f  vid_start=%.2f",
                          ref_start, vid_start)
                if not vid_only:
                    extract_audio_segment(self._ap, ref_start, _PLAY_DUR,
                                          ref_wav, sample_rate=_PLAYBACK_SR)
                if not ref_only:
                    extract_audio_segment(self._vp, vid_start, _PLAY_DUR,
                                          vid_wav, sample_rate=_PLAYBACK_SR)

                # Read WAVs into arrays
                def _read(path):
                    with _wave.open(path, "rb") as w:
                        data = w.readframes(w.getnframes())
                    return _np.frombuffer(data, _np.int16).astype(_np.float32) / 32768.0

                def _norm(arr, target_peak=0.72):
                    """Peak-normalise to a consistent listening level.
                    Peak normalisation never clips regardless of crest factor,
                    which eliminates distortion on high-dynamic speech.
                    RMS normalisation caused clipping when speech peaks were
                    4-6× the RMS (typical crest factor 12-15 dB)."""
                    peak = float(_np.max(_np.abs(arr))) if len(arr) else 0.0
                    if peak < 1e-6:
                        return arr
                    return arr / peak * target_peak

                if mix:
                    a = _norm(_read(ref_wav))
                    b = _norm(_read(vid_wav))
                    if len(a) < len(b):
                        a = _np.pad(a, (0, len(b) - len(a)))
                    elif len(b) < len(a):
                        b = _np.pad(b, (0, len(a) - len(b)))
                    mixed = _np.clip(a * 0.5 + b * 0.5, -1.0, 1.0)
                    pcm = (mixed * 32767).astype(_np.int16)
                elif ref_only:
                    pcm = (_norm(_read(ref_wav)) * 32767).astype(_np.int16)
                else:
                    pcm = (_norm(_read(vid_wav)) * 32767).astype(_np.int16)

                with _wave.open(out_wav, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(_PLAYBACK_SR)
                    w.writeframes(pcm.tobytes())

                _log.info("WAV written: %s  (%d bytes)", out_wav,
                          os.path.getsize(out_wav))
                return out_wav
            except Exception:
                _log.exception("_extract_and_play failed")
                raise

        def _done(fut):
            try:
                wav_path = fut.result()
            except Exception:
                _log.exception("extraction future failed")
                return

            def _do_play():
                try:
                    _log.info("starting playback: %s", wav_path)
                    import winsound
                    winsound.PlaySound(
                        wav_path,
                        winsound.SND_FILENAME | winsound.SND_ASYNC)
                    _log.info("playback started OK (winsound)")
                except Exception:
                    _log.exception("winsound playback failed")
            try:
                self._win.after(0, _do_play)
            except Exception:
                _log.exception("self._win.after() failed (window closed?)")

        ex = ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(_extract_and_play)
        fut.add_done_callback(_done)
        ex.shutdown(wait=False)

    def _stop(self):
        try:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass

    # ── Accept / Cancel ───────────────────────────────────────────────────

    def close(self):
        """Close this dialog programmatically (e.g. when the user re-syncs)."""
        self._cleanup()

    def _on_accept_click(self):
        offset_s = -self._offset_samples / self._sr
        self._cleanup()
        if self._on_accept:
            self._on_accept(offset_s)

    def _on_cancel(self):
        self._cleanup()

    def _cleanup(self):
        self._stop()
        # Remove temp files
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

    def _fmt_offset(self):
        s = -self._offset_samples / self._sr
        return "{:+.3f}s".format(s)
