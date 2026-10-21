"""Assembly view -- the reconciled episode as one continuous track.

Step 4 reviews one pull at a time.  This is the other way of looking at the
same data: every placed pull laid end to end, exactly where build_aaf will
put it (engines.layout_timeline is the one source of placement for both),
played back as a single reference track with the script running above it.

The track is the same "single reference" Step 4 already plays -- an
interview pull sounds from its transcription mix, a VO pull from its take --
so it is a listening and timing view, not a mix.  The AAF still breaks the
cut out by source.

The track is also the editor -- there is no second window to step into.
Every edit is the same edit Step 4 makes, committed through the card's own
accept (segments) or the result's gap_after_s, so Step 4's undo, its saved
state and the AAF all see it:

  * drag a pull's edge, or either side of a cut inside it, to trim --
    shuffle-style, so everything after it moves up or back;
  * drag a pull, or the gap in front of it, to change that gap;
  * Shift-drag across part of a pull and press Delete to take it out
    (all of it: the pull is IGNOREd);
  * , and . nudge the gap before the selected pull by a frame (< > by ten).

It is also where the cut gets spotted.  I and O drop a music cue's in and
out, M an SFX spot, at the playhead -- while it plays, too.  Each mark
lives on the pull it lands on (engines.place_markers), so it rides with
that audio through any retiming, and the AAF carries them as the MUSIC and
SFX marker tracks beside PARTS.  Drag a flag to move it, double-click to
name it, Delete to remove it.

Audio is pulled per pull, with a few seconds of handle either side, from a
small thread pool, nearest-the-view first, and composed on demand by
_AssemblyAudio as the player asks for it.  Nothing is rendered to disk.
"""
import bisect
import concurrent.futures
import math
import os
import threading
import tkinter as tk
import tkinter.font as tkfont

import numpy as np

import engines
import match_review
import playback
from config import (BG, SURF, SURF2, SURF3, BORDER, ACCENT, TEXT, SUB,
                    WARN, ERR, FB, FBT, HOST_NAME, WAVE_REF)
from utils import secs_tc

PLAY_SR     = 32000   # playback / display rate -- the whole cut is held in
                      # memory as int16 at this rate (~4 MB a minute)
LAYOUT_SR   = 48000   # sample grid layout_timeline places on, as the AAF
HANDLE_S    = 3.0     # extra source audio loaded either side of each pull,
                      # so a trim can open a pull up without a reload
ENV_HZ      = 400     # waveform envelope resolution (bins per second)
LEAD_S      = 0.5     # silence ahead of 0:00 so playback can start at the top
LOAD_WORKERS = 3
MIN_SEG_S   = 0.04    # shortest segment a trim or cut may leave
DRAG_PX     = 4       # movement before a press on a pull becomes a drag

_SMALL      = (FB[0], 9)
_MONO       = ("Consolas", 10)
_TC_FONT    = ("Consolas", 15, "bold")

_C_VO       = "#9d8cf0"   # VO waveform
_C_INT      = WAVE_REF    # interview waveform
_C_HOST     = "#8a8a8a"   # host waveform
_C_FLAG     = WARN        # a pull Step 4 still flags for review
_C_PLAYHEAD = ERR
_C_PART     = ACCENT
_C_CLIP_BG  = SURF
_C_MUSIC    = "#4f9be0"   # music cue in / out
_C_SFX      = "#e0609a"   # SFX spot



def _fmt_tc(t):
    """Timeline position as M:SS.s (H:MM:SS.s past the hour)."""
    t = max(0.0, t)
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    if h:
        return "{:d}:{:02d}:{:04.1f}".format(int(h), int(m), s)
    return "{:d}:{:04.1f}".format(int(m), s)


class _Clip(object):
    """One placed pull, in timeline seconds."""

    def __init__(self, entry, res, sr, kind, flagged, play_path):
        self.order     = res["order"]
        self.res       = res
        self.kind      = kind          # "vo" | "int" | "host"
        self.flagged   = flagged
        self.play_path = play_path
        self.start     = entry["start"] / float(sr)
        self.end       = entry["end"] / float(sr)
        self.gap       = entry["gap"] / float(sr)
        # (timeline start, duration, source in) per segment
        self.pieces    = [(st / float(sr), du / float(sr), si / float(sr))
                          for st, du, si in entry["pieces"]]
        # The exact source ranges behind the pieces -- what an edit
        # rewrites (the pieces are rounded to the layout's sample grid).
        self.segments  = [(float(a), float(b)) for a, b in entry["segments"]]
        if self.pieces:
            self.src_lo = min(si for _, _, si in self.pieces)
            self.src_hi = max(si + du for _, du, si in self.pieces)
        else:
            self.src_lo = self.src_hi = 0.0
        self.words = None              # [(tl_start, tl_end, text)] once loaded
        self.load_error = None

    @property
    def label(self):
        return "#{:03d}  {}".format(self.order, self.res.get("token", "?"))

    def colour(self):
        if self.flagged:
            return _C_FLAG
        return {"vo": _C_VO, "host": _C_HOST}.get(self.kind, _C_INT)

    def src_at(self, t):
        """Source time under timeline time t, or None in no segment."""
        for tl, du, si in self.pieces:
            if tl <= t < tl + du:
                return si + (t - tl)
        return None


def _tidy(segments):
    """Drop slivers and join segments a trim has butted together."""
    out = []
    for a, b in sorted(segments):
        if b - a < MIN_SEG_S:
            continue
        if out and a - out[-1][1] <= 1e-3:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _segments_without(clip, a, b):
    """The clip's segments with timeline span [a, b] taken out."""
    out = []
    for (s, e), (tl, du, _si) in zip(clip.segments, clip.pieces):
        ra, rb = max(a, tl), min(b, tl + du)
        if rb <= ra:
            out.append((s, e))
            continue
        out.append((s, s + (ra - tl)))
        out.append((s + (rb - tl), e))
    return _tidy(out)


class _AssemblyAudio(object):
    """The cut as a playback.LivePlayer source: composes any window of the
    timeline from the per-pull buffers on demand.  Index 0 is LEAD_S
    before 0:00 (the player refuses to start inside its first window).

    Swapped wholesale rather than mutated, so the audio callback never sees
    a half-updated layout."""

    def __init__(self, sr):
        self.sr     = sr
        self._lead  = int(LEAD_S * sr)
        self._bufs  = {}                 # order -> (path, t0_s, int16 pcm)
        self._index = ([], [])           # (starts, pieces)
        self._len   = self._lead

    def __len__(self):
        return self._len

    def set_clips(self, clips, total_s):
        pieces = []
        for c in clips:
            for tl, du, si in c.pieces:
                pieces.append((int(round(tl * self.sr)) + self._lead,
                               int(round(du * self.sr)), c.order, si))
        pieces.sort()
        self._index = ([p[0] for p in pieces], pieces)
        # A second of tail so the stretcher can play the last word out.
        self._len = self._lead + int((total_s + 1.0) * self.sr)

    def buffer(self, order):
        return self._bufs.get(order)

    def put(self, order, path, t0, pcm):
        bufs = dict(self._bufs)
        bufs[order] = (path, t0, pcm)
        self._bufs = bufs

    def keep_only(self, orders):
        self._bufs = {o: b for o, b in self._bufs.items() if o in orders}

    def read(self, i, n):
        starts, pieces = self._index
        bufs = self._bufs
        out  = np.zeros(n, dtype=np.float32)
        end  = i + n
        j = max(0, bisect.bisect_right(starts, i) - 1)
        while j < len(pieces):
            st, du, order, si = pieces[j]
            j += 1
            if st >= end:
                break
            a, b = max(st, i), min(st + du, end)
            buf = bufs.get(order)
            if b <= a or buf is None:
                continue
            _, t0, pcm = buf
            off = int(round((si - t0) * self.sr)) + (a - st)
            k0, k1 = max(off, 0), min(off + (b - a), len(pcm))
            if k1 > k0:
                o = a - i + (k0 - off)
                out[o:o + (k1 - k0)] = pcm[k0:k1] * (1.0 / 32768.0)
        return out


class AssemblyWindow(object):
    """The over-under editor: script on top, the cut as one track below."""

    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app)
        self.win.title("Assembly — {}".format(
            app.seq_name.get() if hasattr(app, "seq_name") else ""))
        self.win.configure(bg=BG)
        try:
            self.win.state("zoomed")
        except tk.TclError:
            self.win.geometry("1400x900")
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        self.clips      = []
        self._starts    = []             # clip start times, for bisect
        self.total_s    = 0.0
        self.part_marks = []             # [(t, name)]
        self.view0      = 0.0            # timeline time at the left edge
        self.pps        = 30.0           # pixels per second
        self.playhead   = 0.0
        self.sel_order  = None
        self._cur_order = None           # pull the playhead is in
        self._word_items = []            # [(tl_start, tl_end, item)] on screen
        self._lit_word  = None
        self._env       = {}             # order -> (t0, envelope float32)
        self._env_scale = 0.25           # amplitude that fills the lane
        self._loading   = set()
        self._dirty     = False          # a buffer landed; redraw pending
        self._refresh_after = None
        self._tick_after = None
        self._closed    = False
        self._words_memo = {}
        self._words_lock = threading.Lock()
        self._file_end  = {}             # path -> length, once a read ran short
        self._inputs    = None           # export inputs, held while dragging
        self._drag      = None           # the gesture in progress
        self._range     = None           # (order, a, b) Shift-drag selection
        self.markers    = []             # place_markers' list, plus "t" (s)
        self.sel_marker = None           # (order, index) of the chosen mark
        self._rename    = None           # (entry widget, (order, index))
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=LOAD_WORKERS, thread_name_prefix="pb_assembly")

        self.audio  = _AssemblyAudio(PLAY_SR)
        self.player = None
        if playback.available():
            self.player = playback.LivePlayer(samplerate=PLAY_SR)
            self.player.load_source(self.audio, t0=-LEAD_S)
        self._rate  = match_review._LAST_PLAY_RATE

        # Every vertical measure on the track derives from the small font's
        # line height, so the layout holds at any display scaling.
        self._font = tkfont.Font(family=_SMALL[0], size=_SMALL[1])
        self._ls   = self._font.metrics("linespace")
        # Ruler rows: timecodes, Part flags, then music / SFX marks.
        self._ruler_h = 3 * self._ls + 8
        self._gap_h   = self._ls + 6           # gap lengths under the track

        self._build_ui()
        self.refresh()
        self.win.after(60, self._fit_initial)

    # ── Construction ─────────────────────────────────────────────────────
    def _build_ui(self):
        pw = tk.PanedWindow(self.win, orient="vertical", bg=BORDER,
                            sashwidth=6, bd=0, sashrelief="flat")
        pw.pack(fill="both", expand=True)

        # Script pane
        top = tk.Frame(pw, bg=BG)
        sb = tk.Scrollbar(top, orient="vertical")
        sb.pack(side="right", fill="y")
        self.text = tk.Text(top, bg=BG, fg=TEXT, font=(FB[0], 12), wrap="word",
                            bd=0, highlightthickness=0, padx=24, pady=14,
                            spacing1=2, spacing3=8, cursor="arrow",
                            insertwidth=0, yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.config(command=self.text.yview)
        self.text.tag_config("part", font=FBT, foreground=_C_PART,
                             spacing1=16, spacing3=6)
        self.text.tag_config("tok", font=_MONO, foreground=SUB)
        self.text.tag_config("out", foreground="#6a6a6a")
        self.text.tag_config("cur", background=SURF3)
        self.text.tag_config("mk_music", foreground=_C_MUSIC, font=_SMALL)
        self.text.tag_config("mk_sfx", foreground=_C_SFX, font=_SMALL)
        self.text.tag_config("sel_pull", background=SURF2)
        self.text.tag_raise("cur")
        self.text.bind("<Button-1>", self._on_text_click)

        # Track pane
        bottom = tk.Frame(pw, bg=BG)
        bar = tk.Frame(bottom, bg=SURF, highlightbackground=BORDER,
                       highlightthickness=1)
        bar.pack(fill="x")
        btn = self.app._btn
        self.play_btn = btn(bar, "▶  PLAY", self.toggle_play, small=True,
                            width=9)
        self.play_btn.pack(side="left", padx=(8, 6), pady=6)
        self.tc_lbl = tk.Label(bar, text=_fmt_tc(0), font=_TC_FONT, bg=SURF,
                               fg=TEXT, width=10, anchor="w")
        self.tc_lbl.pack(side="left", padx=(4, 14))
        tk.Label(bar, text="SPEED", font=_SMALL, bg=SURF, fg=SUB
                 ).pack(side="left", padx=(0, 4))
        btn(bar, "−", lambda: self.step_rate(-1), small=True
            ).pack(side="left")
        self.rate_lbl = tk.Label(bar, text="", font=("Consolas", 12, "bold"),
                                 bg=SURF, fg=TEXT, width=5)
        self.rate_lbl.pack(side="left")
        btn(bar, "+", lambda: self.step_rate(1), small=True).pack(side="left")
        tk.Label(bar, text="ZOOM", font=_SMALL, bg=SURF, fg=SUB
                 ).pack(side="left", padx=(18, 4))
        btn(bar, "−", lambda: self.zoom(1 / 1.5), small=True
            ).pack(side="left")
        btn(bar, "FIT", self.fit, small=True).pack(side="left", padx=2)
        btn(bar, "+", lambda: self.zoom(1.5), small=True).pack(side="left")
        tk.Label(bar, font=_SMALL, bg=SURF, fg=SUB,
                 text="drag an edge to trim  ·  drag a pull or gap to retime"
                      "  ·  Shift-drag + Delete cuts  ·  , . nudge"
                      "  ·  I / O music  ·  M sfx"
                 ).pack(side="right", padx=10)
        self.load_lbl = tk.Label(bar, text="", font=_SMALL, bg=SURF, fg=SUB)
        self.load_lbl.pack(side="right", padx=8)
        self._show_rate()

        self.ov = tk.Canvas(bottom, height=24, bg=SURF2, highlightthickness=0,
                            cursor="hand2")
        self.ov.pack(fill="x", pady=(6, 0))
        self.cv = tk.Canvas(bottom, bg=BG, highlightthickness=0,
                            cursor="xterm")
        self.cv.pack(fill="both", expand=True, pady=(2, 0))
        self.status = tk.Label(bottom, text="", font=(FB[0], 10), bg=BG,
                               fg=SUB, anchor="w", padx=10)
        self.status.pack(fill="x", pady=(0, 4))

        pw.add(top, minsize=120, stretch="always")
        pw.add(bottom, minsize=240, stretch="always")
        self.win.update_idletasks()
        try:
            pw.sash_place(0, 0, int(self.win.winfo_height() * 0.42))
        except tk.TclError:
            pass

        self.cv.bind("<Configure>", lambda e: self.redraw())
        self.ov.bind("<Configure>", lambda e: self._draw_overview())
        self.cv.bind("<Button-1>", self._on_track_press)
        self.cv.bind("<Shift-Button-1>", self._on_track_press)
        self.cv.bind("<B1-Motion>", self._on_track_drag)
        self.cv.bind("<ButtonRelease-1>", self._on_track_release)
        self.cv.bind("<Motion>", self._on_motion)
        self.cv.bind("<Double-Button-1>", self._on_track_double)
        self.cv.bind("<MouseWheel>", self._on_wheel)
        self.ov.bind("<Button-1>", self._on_overview)
        self.ov.bind("<B1-Motion>", self._on_overview)
        # Every key is handled (or swallowed) here: Step 4 binds x and
        # Ctrl+Z with bind_all, and those must not fire against a card
        # nobody is looking at while this window has focus.
        self.win.bind("<Key>", self._on_key)

    # ── Model ────────────────────────────────────────────────────────────
    def schedule_refresh(self):
        """Step 4 changed; rebuild once the burst of edits settles."""
        if self._closed:
            return
        if self._refresh_after is not None:
            try:
                self.win.after_cancel(self._refresh_after)
            except tk.TclError:
                pass
        self._refresh_after = self.win.after(250, self.refresh)

    def refresh(self):
        """Re-lay the cut from Step 4's current state, keeping every
        loaded buffer that still covers its pull."""
        if self._refresh_after is not None:
            try:
                self.win.after_cancel(self._refresh_after)
            except tk.TclError:
                pass
        self._refresh_after = None
        self._layout()
        clips = self.clips
        self.audio.keep_only({c.order for c in clips})
        self._env = {o: v for o, v in self._env.items()
                     if any(c.order == o for c in clips)}
        if self.sel_order is not None and \
                not any(c.order == self.sel_order for c in clips):
            self.sel_order = None
        if self._range is not None and self._clip(self._range[0]) is None:
            self._range = None
        self._render_script()
        self._queue_loads()
        self.redraw()
        self._update_status()

    def _layout(self, overrides=None):
        """Lay the cut out from Step 4's state, with an edit that is still
        being dragged (`overrides`: order -> result fields) laid over it.
        A drag reuses the export inputs it started with; layout itself is
        a millisecond, so the track follows the mouse."""
        app = self.app
        if overrides is None or self._inputs is None:
            self._inputs = app._collect_export_inputs()[:2]
        edited, int_assets = self._inputs
        if overrides:
            edited = [dict(r, **overrides[r.get("order")])
                      if r.get("order") in overrides else r for r in edited]
        placed, _skipped, part_starts = engines.layout_timeline(
            edited, app.gap_var.get(), LAYOUT_SR, int_assets)
        by_order = {r.get("order"): r for r in app.results}
        review   = set(app.REVIEW_STATUSES) | {"provisional"}
        host_lc  = HOST_NAME.lower()

        old_by = {o.order: o for o in self.clips}
        clips = []
        for e in placed:
            res = by_order.get(e["res"].get("order"), e["res"])
            if res.get("is_vo"):
                kind, path = "vo", (e["paths"][0] if e["paths"] else None)
            else:
                kind = "host" if res.get("token", "").lower() == host_lc else "int"
                # The same reference Step 4 plays: the transcription mix
                # for a multi-file token, else the token's own file.
                path = next((p for p in (res.get("source_audio"),
                                         res.get("source_video"),
                                         e["paths"][0])
                             if p and os.path.isfile(p)), None)
            c = _Clip(e, res, LAYOUT_SR, kind,
                      res.get("status", "") in review, path)
            old = old_by.get(c.order)
            if old is not None:
                c.load_error = old.load_error
                if old.words is not None:
                    c.words = old.words if old.pieces == c.pieces \
                        else self._map_words(c, load=False)
            clips.append(c)

        self.clips   = clips
        self._starts = [c.start for c in clips]
        self.total_s = clips[-1].end if clips else 0.0
        names = {p.get("index"): p.get("name", "") for p in (app.parts or [])}
        self.part_marks = sorted((sa / float(LAYOUT_SR), names.get(pi, ""))
                                 for pi, sa in part_starts.items())
        self.audio.set_clips(clips, self.total_s)
        self.markers = [dict(m, t=m["pos"] / float(LAYOUT_SR))
                        for m in engines.place_markers(placed, LAYOUT_SR)]
        if self.sel_marker is not None and not any(
                (m["order"], m["index"]) == self.sel_marker for m in self.markers):
            self.sel_marker = None

    def _queue_loads(self):
        """Fetch every pull whose loaded audio no longer covers it,
        nearest the middle of the view first."""
        mid = self.view0 + self._span() / 2.0
        todo = []
        for c in self.clips:
            if not c.play_path or c.order in self._loading:
                continue
            buf = self.audio.buffer(c.order)
            if buf is not None:
                path, t0, pcm = buf
                if path == c.play_path and t0 <= c.src_lo + 1e-6 and \
                        t0 + len(pcm) / float(PLAY_SR) >= c.src_hi - 1e-6:
                    if c.words is None:
                        todo.append(c)
                    continue
            todo.append(c)
        todo.sort(key=lambda c: abs((c.start + c.end) / 2.0 - mid))
        for c in todo:
            self._loading.add(c.order)
            self._pool.submit(self._load_clip, c)
        self._show_loading()

    def _load_clip(self, c):
        """Worker thread: audio, envelope and words for one pull."""
        if self._closed:
            return
        try:
            buf = self.audio.buffer(c.order)
            if buf is not None and buf[0] == c.play_path and \
                    buf[1] <= c.src_lo and \
                    buf[1] + len(buf[2]) / float(PLAY_SR) >= c.src_hi:
                path, t0, pcm = buf
            else:
                path = c.play_path
                t0   = max(0.0, c.src_lo - HANDLE_S)
                want = (c.src_hi + HANDLE_S) - t0
                pcm  = engines.extract_pcm_range(path, t0, want, PLAY_SR)
                if len(pcm) < (want - 0.05) * PLAY_SR:
                    # Ran off the end of the file: a trim stops there.
                    self._file_end[path] = t0 + len(pcm) / float(PLAY_SR)
            blk = PLAY_SR // ENV_HZ
            n   = len(pcm) // blk
            env = (np.abs(pcm[:n * blk].reshape(n, blk)).max(axis=1)
                   .astype(np.float32) / 32768.0) if n else \
                np.zeros(1, np.float32)
            words = self._map_words(c)
        except Exception as exc:
            msg = str(exc).splitlines()[0] if str(exc) else repr(exc)
            self.app._ui(lambda: self._on_load_failed(c, msg))
            return
        self.app._ui(lambda: self._on_loaded(c, path, t0, pcm, env, words))

    def _raw_words(self, c, load=True):
        """The transcript behind a pull: its own windowed words, else the
        source file's cached transcript.  None when that is not loaded yet
        and `load` is False (the UI thread never waits on a parse)."""
        words = c.res.get("words")
        if words or not c.play_path:
            return words or []
        with self._words_lock:
            words = self._words_memo.get(c.play_path)
            if words is None and load:
                try:
                    words = engines.cache_load(c.play_path) or []
                except Exception:
                    words = []
                self._words_memo[c.play_path] = words
        return words

    def _map_words(self, c, load=True):
        """Transcript words that fall inside the pull's segments, moved
        onto the timeline."""
        words = self._raw_words(c, load)
        if words is None:
            return None
        out = []
        for w in words or []:
            if not isinstance(w, dict) or "start" not in w:
                continue
            ws = float(w["start"]); we = float(w.get("end", ws))
            for tl, du, si in c.pieces:
                if si <= ws < si + du:
                    out.append((tl + ws - si, tl + min(we, si + du) - si,
                                str(w.get("word", "")).strip()))
                    break
        out.sort()
        return out

    def _on_loaded(self, c, path, t0, pcm, env, words):
        self._loading.discard(c.order)
        if self._closed:
            return
        live = next((x for x in self.clips if x.order == c.order), None)
        if live is None or live.play_path != path:
            self._show_loading()
            return
        self.audio.put(c.order, path, t0, pcm)
        self._env[c.order] = (t0, env)
        live.words = words if live.pieces == c.pieces else None
        if live.words is None:
            live.words = self._map_words(live)
        # Fill the lane with typical speech, not the loudest transient.
        if len(env):
            p = float(np.percentile(env, 99.5))
            if p > self._env_scale:
                self._env_scale = p
        self._show_loading()
        if not self._dirty:
            self._dirty = True
            self.win.after(120, self._redraw_if_dirty)

    def _on_load_failed(self, c, msg):
        self._loading.discard(c.order)
        live = self._clip(c.order)
        if live is not None:
            live.load_error = msg
        self._show_loading()
        self._dirty = True
        self.win.after(120, self._redraw_if_dirty)

    def _redraw_if_dirty(self):
        if self._dirty and not self._closed:
            self._dirty = False
            self.redraw()

    def _show_loading(self):
        n = len(self._loading)
        self.load_lbl.config(
            text="Loading audio … {} pulls to go".format(n) if n else "")

    # ── Script pane ──────────────────────────────────────────────────────
    def _render_script(self):
        t = self.text
        y0 = t.yview()[0]
        t.config(state="normal")
        t.delete("1.0", "end")
        placed = {c.order for c in self.clips}
        names  = {p.get("index"): p.get("name", "") for p in (self.app.parts or [])}
        marks = {}
        for m in self.markers:
            marks.setdefault(m["order"], []).append(m)
        cur_part = object()
        for r in sorted(self.app.results, key=lambda r: r.get("order", 0)):
            pi = r.get("part_index")
            if pi != cur_part:
                cur_part = pi
                t.insert("end", (names.get(pi) or "Part {}".format(pi)) + "\n",
                         ("part",))
            o   = r.get("order")
            tag = "o{}".format(o)
            out = () if o in placed else ("out",)
            tok = r.get("token", "")
            t.insert("end", "#{:03d} {}   ".format(o, tok), ("tok", tag) + out)
            body = (r.get("quote_text") or "").strip()
            if o not in placed:
                body += "   (not in the cut)"
            t.insert("end", body, (tag,) + out)
            for m in marks.get(o, ()):
                t.insert("end", "   [{}]".format(m["label"]),
                         (tag, "mk_sfx" if m["kind"] == "sfx" else "mk_music",
                          "mk{}_{}".format(o, m["index"])))
            t.insert("end", "\n", (tag,) + out)
        t.config(state="disabled")
        t.yview_moveto(y0)
        self._cur_order = None
        self._highlight_script(self._order_at(self.playhead), see=False)
        self._mark_selected_in_script()

    def _order_from_text_event(self, e):
        idx = self.text.index("@{},{}".format(e.x, e.y))
        for tag in self.text.tag_names(idx):
            if tag.startswith("o") and tag[1:].isdigit():
                return int(tag[1:])
        return None

    def _on_text_click(self, e):
        idx = self.text.index("@{},{}".format(e.x, e.y))
        for tag in self.text.tag_names(idx):
            if tag.startswith("mk") and "_" in tag:
                o, i = (int(v) for v in tag[2:].split("_"))
                m = self._marker((o, i))
                if m is not None:
                    self.select_marker((o, i))
                    self.seek(m["t"], center=True)
                    return "break"
        o = self._order_from_text_event(e)
        c = self._clip(o)
        if c is not None:
            self.select(c.order)
            self.seek(c.start, center=True)
        return "break"

    def _highlight_script(self, order, see=True):
        if order == self._cur_order:
            return
        self._cur_order = order
        t = self.text
        t.tag_remove("cur", "1.0", "end")
        if order is None:
            return
        rng = t.tag_ranges("o{}".format(order))
        if rng:
            t.tag_add("cur", rng[0], rng[-1])
            if see:
                t.see(rng[-1]); t.see(rng[0])

    def _mark_selected_in_script(self):
        t = self.text
        t.tag_remove("sel_pull", "1.0", "end")
        if self.sel_order is None:
            return
        rng = t.tag_ranges("o{}".format(self.sel_order))
        if rng:
            t.tag_add("sel_pull", rng[0], rng[-1])

    # ── Geometry ─────────────────────────────────────────────────────────
    def _span(self):
        return max(1, self.cv.winfo_width()) / self.pps

    def _x(self, t):
        return (t - self.view0) * self.pps

    def _t(self, x):
        return self.view0 + x / self.pps

    def _clip(self, order):
        return next((c for c in self.clips if c.order == order), None)

    def _clip_at(self, t):
        i = bisect.bisect_right(self._starts, t) - 1
        if 0 <= i < len(self.clips) and t < self.clips[i].end:
            return self.clips[i]
        return None

    def _order_at(self, t):
        """The pull at t, or the one whose gap t sits in."""
        i = bisect.bisect_right(self._starts, t) - 1
        return self.clips[i].order if 0 <= i < len(self.clips) else None

    def _clamp_view(self):
        span = self._span()
        hi = max(0.0, self.total_s - span * 0.9)
        self.view0 = min(max(-span * 0.02, self.view0), hi)

    def _track_box(self):
        h = self.cv.winfo_height()
        return self._ruler_h + 4, h - self._gap_h

    def _fits(self, text, room):
        return self._font.measure(text) <= room

    # ── Drawing ──────────────────────────────────────────────────────────
    def redraw(self):
        if self._closed:
            return
        cv = self.cv
        cv.delete("all")
        self._word_items = []
        self._lit_word = None
        w = max(1, cv.winfo_width())
        y0, y1 = self._track_box()
        if y1 - y0 < 40:
            return
        self._clamp_view()
        span = self._span()
        t0, t1 = self.view0, self.view0 + span

        # Ruler: timecodes on the top row, Part flags on the second.
        ls, rh = self._ls, self._ruler_h
        cv.create_rectangle(0, 0, w, rh, fill=SURF, outline="")
        cv.create_line(0, rh, w, rh, fill=BORDER)
        step = next((s for s in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600)
                     if self._font.measure("00:00.0") + 24 <= s * self.pps), 1200)
        k = math.floor(t0 / step) * step
        while k <= t1:
            x = self._x(k)
            cv.create_line(x, 2, x, ls + 2, fill=BORDER)
            if k >= 0:
                cv.create_text(x + 4, 2, text=_fmt_tc(k) if step < 1
                               else _fmt_tc(k).split(".")[0],
                               anchor="nw", font=self._font, fill=SUB)
            k += step
        marks = self.part_marks
        for n, (pt, name) in enumerate(marks):
            x = self._x(pt)
            nxt = self._x(marks[n + 1][0]) if n + 1 < len(marks) else w + 400
            if nxt < 0 or x > w:
                continue
            cv.create_line(x, ls + 4, x, y1, fill=_C_PART, dash=(2, 4))
            cv.create_rectangle(x, ls + 5, x + 6, rh - 3, fill=_C_PART,
                                outline="")
            if self._fits(name, nxt - x - 16):
                cv.create_text(x + 10, ls + 4, text=name, anchor="nw",
                               font=self._font, fill=TEXT)
        self._draw_marks(w, y1)

        # Clips
        i0 = max(0, bisect.bisect_right(self._starts, t0) - 1)
        ls = self._ls
        lane_top, lane_bot = y0 + ls + 8, y1 - ls - 8
        lane_mid = (lane_top + lane_bot) / 2.0
        lane_h   = (lane_bot - lane_top) / 2.0
        show_words = self.pps >= 20
        for c in self.clips[i0:]:
            if c.start > t1:
                break
            if c.end < t0:
                continue
            xs, xe = self._x(c.start), self._x(c.end)
            col = c.colour()
            cv.create_rectangle(xs, y0, xe, y1, fill=_C_CLIP_BG, outline=BORDER)
            cv.create_rectangle(xs, y0, xe, y0 + 3, fill=col, outline="")
            for tl, du, si in c.pieces[1:]:
                xc = self._x(tl)       # an internal cut inside the pull
                cv.create_line(xc, y0 + 4, xc, y1, fill=BORDER, dash=(1, 3))
            lbl = c.label
            if c.flagged:
                lbl += "  ·  " + c.res.get("status", "")
            lx = max(xs, 0) + 6
            if self._fits(lbl, xe - lx - 4):
                cv.create_text(lx, y0 + 5, text=lbl, anchor="nw",
                               font=self._font, fill=SUB)
            self._draw_wave(c, xs, xe, lane_mid, lane_h, col, w)
            if c.load_error and xe - xs > 80:
                cv.create_text(max(xs, 0) + 6, lane_mid, text=c.load_error[:120],
                               anchor="w", font=self._font, fill=ERR)
            if show_words and c.words:
                self._draw_words(c, xs, xe, y1 - 4, w)
            if c.order == self.sel_order:
                cv.create_rectangle(xs + 1, y0 + 1, xe - 1, y1 - 1,
                                    outline=TEXT, width=2)
            # Gap after the pull
            if c.gap > 0:
                gx0, gx1 = xe, self._x(c.end + c.gap)
                txt = "{:.2f}s".format(c.gap)
                if self._fits(txt, gx1 - gx0 + 20):
                    cv.create_text((gx0 + gx1) / 2.0, y1 + self._gap_h / 2.0,
                                   text=txt, font=self._font, fill=SUB)

        if self._range is not None:
            _o, a, b = self._range
            cv.create_rectangle(self._x(min(a, b)), y0, self._x(max(a, b)), y1,
                                fill=TEXT, stipple="gray25", outline=TEXT)
        x = self._x(self.playhead)
        cv.create_line(x, 0, x, y1, fill=_C_PLAYHEAD, width=2, tags=("ph",))
        self.tc_lbl.config(text=_fmt_tc(self.playhead))
        self._light_word()
        self._draw_overview()

    def _marker_row(self):
        """Top and bottom of the ruler row the music / SFX flags sit on."""
        return 2 * self._ls + 6, 3 * self._ls + 6

    def _draw_marks(self, w, y1):
        cv = self.cv
        top, bot = self._marker_row()
        # Cue spans: a bar from each in to the out that closes it.  A cue
        # left open runs to the next one, or to the end of the cut.
        spans, open_t = [], None
        for m in self.markers:
            if m["kind"] == "music_in":
                if open_t is not None:
                    spans.append((open_t, m["t"]))
                open_t = m["t"]
            elif m["kind"] == "music_out" and open_t is not None:
                spans.append((open_t, m["t"]))
                open_t = None
        if open_t is not None:
            spans.append((open_t, self.total_s))
        for ta, tb in spans:
            xa, xb = self._x(ta), self._x(tb)
            if xb > 0 and xa < w:
                cv.create_rectangle(xa, bot, xb, bot + 2, fill=_C_MUSIC,
                                    outline="")
        for n, m in enumerate(self.markers):
            x = self._x(m["t"])
            if x < -300 or x > w:
                continue
            col = _C_SFX if m["kind"] == "sfx" else _C_MUSIC
            on = (m["order"], m["index"]) == self.sel_marker
            cv.create_line(x, bot, x, y1, fill=col, dash=(2, 4))
            cv.create_rectangle(x, top + 1, x + 6, bot - 1, fill=col,
                                outline=TEXT if on else "", width=2 if on else 1)
            nxt = self._x(self.markers[n + 1]["t"]) if n + 1 < len(self.markers) \
                else w + 400
            if self._fits(m["label"], nxt - x - 16):
                cv.create_text(x + 10, top, text=m["label"], anchor="nw",
                               font=self._font, fill=TEXT if on else SUB)
        if self._rename is not None:
            entry, key = self._rename
            m = next((m for m in self.markers
                      if (m["order"], m["index"]) == key), None)
            if m is not None:
                cv.create_window(self._x(m["t"]) + 10, top - 2, anchor="nw",
                                 window=entry)

    def _draw_wave(self, c, xs, xe, mid, half, col, w):
        cv = self.cv
        env = self._env.get(c.order)
        a, b = max(0, int(xs)), min(w, int(xe))
        if b - a < 2:
            return
        if env is None:
            cv.create_line(a, mid, b, mid, fill=BORDER)
            return
        e_t0, e = env
        if not len(e):
            return
        px  = np.arange(a, b, dtype=np.float64)
        tt  = self.view0 + px / self.pps
        src = np.full(len(px), np.nan)
        for tl, du, si in c.pieces:
            m = (tt >= tl) & (tt < tl + du)
            src[m] = si + (tt[m] - tl)
        ok = ~np.isnan(src)
        if not ok.any():
            return
        idx = np.clip(((src[ok] - e_t0) * ENV_HZ).astype(np.int64), 0, len(e) - 1)
        per = max(1, int(math.ceil(ENV_HZ / self.pps)))
        if per > 1 and np.all(np.diff(idx) >= 0):
            # Peak of every bin under each pixel, not one sample of it.
            amp = np.maximum.reduceat(e, idx)
            # reduceat runs each pixel up to the next pixel's bin; the last
            # one would run to the end of the envelope -- trim it.
            amp[-1] = e[idx[-1]:idx[-1] + per].max()
        else:
            amp = e[idx]
        amp = np.minimum(amp / max(self._env_scale, 1e-4), 1.0) * half * 0.95
        xs_ok = px[ok]
        top = np.column_stack((xs_ok, mid - amp - 0.5)).ravel()
        bot = np.column_stack((xs_ok[::-1], (mid + amp + 0.5)[::-1])).ravel()
        pts = np.concatenate((top, bot)).tolist()
        if len(pts) >= 6:
            cv.create_polygon(pts, fill=col, outline="")

    def _draw_words(self, c, xs, xe, y, w):
        """Transcript words along the bottom of the lane, each at its own
        start time; one that would run into the last is left out."""
        cv = self.cv
        last = -1e9
        space = self._font.measure(" ")
        for ws, we, word in c.words:
            x = self._x(ws)
            if x > w:
                break
            if x < last or x < xs or x < -200:
                continue
            ww = self._font.measure(word)
            if x + 2 + ww > xe - 2:
                continue
            item = cv.create_text(x + 2, y, text=word, anchor="sw",
                                  font=self._font, fill=SUB)
            self._word_items.append((ws, we, item))
            last = x + 2 + ww + space

    def _light_word(self):
        """Light the word under the playhead."""
        hit = None
        for ws, we, item in self._word_items:
            if ws <= self.playhead < we:
                hit = item
                break
        if hit == self._lit_word:
            return
        if self._lit_word is not None:
            self.cv.itemconfig(self._lit_word, fill=SUB)
        if hit is not None:
            self.cv.itemconfig(hit, fill=ACCENT)
        self._lit_word = hit

    def _draw_overview(self):
        ov = self.ov
        ov.delete("all")
        w = max(1, ov.winfo_width()); h = max(1, ov.winfo_height())
        if self.total_s <= 0:
            return
        k = w / self.total_s
        for c in self.clips:
            ov.create_rectangle(c.start * k, 7, max(c.start * k + 1, c.end * k),
                                h - 7, fill=c.colour(), outline="")
        for pt, _ in self.part_marks:
            ov.create_line(pt * k, 0, pt * k, h, fill=_C_PART)
        for m in self.markers:
            ov.create_line(m["t"] * k, 0, m["t"] * k, 6,
                           fill=_C_SFX if m["kind"] == "sfx" else _C_MUSIC)
        ov.create_rectangle(self.view0 * k, 1, (self.view0 + self._span()) * k,
                            h - 1, outline=TEXT)
        ov.create_line(self.playhead * k, 0, self.playhead * k, h,
                       fill=_C_PLAYHEAD, width=2, tags=("ph",))

    def _move_playhead(self):
        x = self._x(self.playhead)
        y1 = self._track_box()[1]
        self.cv.coords("ph", x, 0, x, y1)
        if self.total_s > 0:
            k = max(1, self.ov.winfo_width()) / self.total_s
            self.ov.coords("ph", self.playhead * k, 0, self.playhead * k,
                           self.ov.winfo_height())
        self.tc_lbl.config(text=_fmt_tc(self.playhead))
        self._light_word()

    # ── Status ───────────────────────────────────────────────────────────
    def _update_status(self):
        m = self._marker(self.sel_marker)
        if m is not None:
            c = self._clip(m["order"])
            kind = {"music_in": "music in", "music_out": "music out",
                    "sfx": "SFX spot"}[m["kind"]]
            self.status.config(fg=SUB, text="   ·   ".join((
                m["label"], kind, _fmt_tc(m["t"]),
                "on " + (c.label if c else "?"),
                "drag to move, double-click to name, Delete to remove")))
            return
        c = self._clip(self.sel_order) or self._clip(self._order_at(self.playhead))
        if c is None:
            self.status.config(text="{} pulls  ·  {}".format(
                len(self.clips), _fmt_tc(self.total_s)), fg=SUB)
            return
        segs = len(c.pieces)
        parts = ["{}".format(c.label),
                 "{} → {}".format(secs_tc(c.src_lo), secs_tc(c.src_hi)),
                 "{:.1f}s".format(c.end - c.start),
                 "{} segment{}".format(segs, "" if segs == 1 else "s"),
                 "gap after {:.2f}s".format(c.gap),
                 c.res.get("status", "")]
        self.status.config(text="   ·   ".join(p for p in parts if p),
                           fg=SUB)

    # ── Selection / navigation ───────────────────────────────────────────
    def select(self, order):
        self.sel_order = order
        self.sel_marker = None
        self._mark_selected_in_script()
        self.redraw()
        self._update_status()

    def seek(self, t, center=False):
        self.playhead = max(0.0, min(t, self.total_s))
        if self.player is not None and self.player.is_playing():
            self.player.seek(self.playhead)
        span = self._span()
        if center or not (self.view0 <= self.playhead <= self.view0 + span):
            self.view0 = self.playhead - span * (0.3 if center else 0.1)
            self.redraw()
        else:
            self._move_playhead()
        self._highlight_script(self._order_at(self.playhead))
        self._update_status()

    # ── Editing on the track ─────────────────────────────────────────────
    def _hit(self, x, y):
        """What a press at (x, y) would grab: ("in" | "out", clip, segment)
        for an edge, ("body", clip) inside a pull, ("gap", the pull before
        it) between two, ("ruler",) above the track, or ("none",)."""
        y0, y1 = self._track_box()
        reach = max(6, self._ls // 2)
        if y < y0:
            top, bot = self._marker_row()
            if top - 2 <= y <= bot + 4:
                near = min(self.markers, default=None,
                           key=lambda m: abs(x - self._x(m["t"]) - 3))
                if near is not None and \
                        abs(x - self._x(near["t"]) - 3) <= reach:
                    return ("marker", near, None)
            return ("ruler", None, None)
        if y > y1:
            return ("none", None, None)
        t = self._t(x)
        i = bisect.bisect_right(self._starts, t) - 1
        best = None
        for c in self.clips[max(0, i - 1):i + 2]:
            last = len(c.pieces) - 1
            for k, (tl, du, _si) in enumerate(c.pieces):
                xi, xo = self._x(tl), self._x(tl + du)
                # Inside the segment only, so the two sides of a cut stay
                # separate; a pull's outer edges also take a little slack.
                if xi - (3 if k == 0 else 0) <= x <= xi + reach:
                    d = abs(x - xi)
                    if best is None or d < best[0]:
                        best = (d, ("in", c, k))
                if xo - reach <= x <= xo + (3 if k == last else 0):
                    d = abs(x - xo)
                    if best is None or d < best[0]:
                        best = (d, ("out", c, k))
        if best is not None:
            return best[1]
        c = self._clip_at(t)
        if c is not None:
            return ("body", c, None)
        if 0 <= i < len(self.clips) - 1:
            return ("gap", self.clips[i], None)
        return ("none", None, None)

    def _prev_clip(self, c):
        i = next((n for n, x in enumerate(self.clips) if x.order == c.order), 0)
        return self.clips[i - 1] if i > 0 else None

    def _on_motion(self, e):
        if self._drag is not None:
            return
        kind, c, k = self._hit(e.x, e.y)
        cur = {"in": "sb_h_double_arrow", "out": "sb_h_double_arrow",
               "gap": "sb_h_double_arrow", "marker": "sb_h_double_arrow",
               "ruler": "hand2"}.get(kind, "xterm")
        if self.cv.cget("cursor") != cur:
            self.cv.config(cursor=cur)
        self.cv.delete("hover")
        if kind in ("in", "out"):
            tl, du, _si = c.pieces[k]
            hx = self._x(tl if kind == "in" else tl + du)
            y0, y1 = self._track_box()
            self.cv.create_line(hx, y0, hx, y1, fill=ACCENT, width=2,
                                tags=("hover",))

    def _on_track_press(self, e):
        self.cv.focus_set()
        t = self._t(e.x)
        kind, c, k = self._hit(e.x, e.y)
        self._end_rename(commit=True)
        if kind == "marker":
            self._range = None
            self.select_marker((c["order"], c["index"]))
            self._drag = {"mode": "marker", "x0": e.x, "t0": c["t"],
                          "key": (c["order"], c["index"]), "moved": False}
            return
        self.sel_marker = None
        if kind == "ruler":
            self._drag = {"mode": "scrub"}
            self.seek(t)
            return
        if e.state & 0x0001 and kind in ("body", "in", "out"):
            # Shift: mark a span of this pull to take out.
            a = min(max(t, c.start), c.end)
            self._range = (c.order, a, a)
            self._drag = {"mode": "range", "x0": e.x}
            self.select(c.order)
            return
        self._range = None
        if kind in ("in", "out"):
            self._drag = {"mode": kind, "order": c.order, "k": k, "x0": e.x,
                          "segs0": list(c.segments), "moved": False}
            self.select(c.order)
            return
        if kind in ("body", "gap"):
            # The gap belongs to the pull before it; dragging a pull moves
            # it and everything after by changing that gap.
            owner = self._prev_clip(c) if kind == "body" else c
            self._drag = {"mode": "gap", "x0": e.x, "moved": False,
                          "target": owner.order if owner else None,
                          "gap0": owner.gap if owner else 0.0}
            if kind == "body" and c.order != self.sel_order:
                self.sel_order = c.order
                self._mark_selected_in_script()
        self.seek(t)
        self.redraw()
        self._update_status()

    def _on_track_drag(self, e):
        d = self._drag
        if d is None:
            return
        mode = d["mode"]
        if mode == "scrub":
            self.seek(self._t(e.x))
            return
        if mode == "range":
            o, a, _b = self._range
            c = self._clip(o)
            if c is not None:
                self._range = (o, a, min(max(self._t(e.x), c.start), c.end))
                self.redraw()
                self._status_range()
            return
        dx = e.x - d["x0"]
        if not d["moved"]:
            if abs(dx) < DRAG_PX:
                return
            d["moved"] = True
            self.cv.delete("hover")
        dt = dx / self.pps
        if mode == "marker":
            d["value"] = min(max(d["t0"] + dt, 0.0), self.total_s)
            m = self._marker(d["key"])
            if m is not None:
                m["t"] = d["value"]          # just the flag, until release
            self.redraw()
            self.status.config(fg=TEXT, text="{}   {}".format(
                m["label"] if m else "", _fmt_tc(d["value"])))
            return
        if mode == "gap":
            if d["target"] is None:
                return
            d["value"] = max(0.0, d["gap0"] + dt)
            self._layout({d["target"]: {"gap_after_s": d["value"],
                                        "gap_after": True}})
        else:
            d["value"] = self._trimmed(d, dt)
            self._layout({d["order"]: {"segments": d["value"]}})
        self.redraw()
        self._status_drag(d)

    def _trimmed(self, d, dt):
        """The pull's segments with one edge moved by dt seconds, held
        inside the neighbouring segments and the file."""
        segs = [list(s) for s in d["segs0"]]
        k = d["k"]
        a, b = segs[k]
        if d["mode"] == "in":
            lo = segs[k - 1][1] if k > 0 else 0.0
            segs[k][0] = max(min(a + dt, b - MIN_SEG_S), lo)
        else:
            c = self._clip(d["order"])
            hi = segs[k + 1][0] if k + 1 < len(segs) else \
                self._file_end.get(c.play_path if c else None, float("inf"))
            segs[k][1] = min(max(b + dt, a + MIN_SEG_S), hi)
        return [tuple(s) for s in segs]

    def _on_track_release(self, e):
        d, self._drag = self._drag, None
        if d is None:
            return
        if d["mode"] == "range":
            o, a, b = self._range
            if abs(self._x(b) - self._x(a)) < DRAG_PX:
                self._range = None
                self.seek(a)
                self.redraw()
            return
        if not d.get("moved") or "value" not in d:
            return
        if d["mode"] == "marker":
            self._move_marker(d["key"], d["value"])
            return
        if d["mode"] == "gap":
            self.app._assembly_set_gap(d["target"], d["value"])
        else:
            segs = _tidy(d["value"])
            if segs and segs != _tidy(d["segs0"]):
                self.app._assembly_set_segments(d["order"], segs)
        self.refresh()

    def _status_drag(self, d):
        if d["mode"] == "gap":
            o = self._clip(d["target"])
            self.status.config(fg=TEXT, text="gap after {}:  {:.3f}s   ({:+.3f}s)"
                               .format(o.label if o else "", d["value"],
                                       d["value"] - d["gap0"]))
            return
        seg0, seg = d["segs0"][d["k"]], d["value"][d["k"]]
        i = 0 if d["mode"] == "in" else 1
        c = self._clip(d["order"])
        self.status.config(fg=TEXT, text="{}   {} {}   ({:+.3f}s)".format(
            c.label if c else "", "in " if i == 0 else "out",
            secs_tc(seg[i]), seg[i] - seg0[i]))

    def _status_range(self):
        o, a, b = self._range
        c = self._clip(o)
        whole = c is not None and abs(b - a) >= c.end - c.start - MIN_SEG_S
        self.status.config(fg=TEXT, text="{}   {:.2f}s marked   ·   Delete "
                           "takes it out{}".format(
                               c.label if c else "", abs(b - a),
                               " (the whole pull: IGNOREs it)" if whole else ""))

    def delete_range(self):
        if self._range is None:
            return
        o, a, b = self._range
        a, b = min(a, b), max(a, b)
        c = self._clip(o)
        self._range = None
        if c is None or b - a < MIN_SEG_S:
            self.redraw()
            return
        segs = _segments_without(c, a, b)
        if segs:
            self.app._assembly_set_segments(o, segs)
        else:
            self.app._assembly_ignore(o)
        self.refresh()
        self.seek(a)

    # ── Spotting marks ───────────────────────────────────────────────────
    def _marker(self, key):
        return next((m for m in self.markers
                     if (m["order"], m["index"]) == key), None) if key else None

    def _anchor_at(self, t):
        """(pull, anchor) for a mark at timeline time t: a moment of the
        pull's source audio inside a pull, a point in the gap after it
        between two."""
        if not self.clips:
            return None, None
        c = self._clip_at(t)
        if c is not None:
            src = c.src_at(t)
            if src is not None:
                return c.order, {"src": round(src, 4)}
        i = bisect.bisect_right(self._starts, t) - 1
        if i < 0:
            c = self.clips[0]
            return c.order, {"src": round(c.pieces[0][2] if c.pieces else 0.0, 4)}
        c = self.clips[i]
        return c.order, {"tail": round(max(0.0, t - c.end), 4)}

    def _pull_marks(self, order):
        r = next((r for r in self.app.results if r.get("order") == order), None)
        return list((r or {}).get("markers") or [])

    def drop_marker(self, kind):
        """A mark at the playhead -- the spotting keys, so it works while
        the cut plays."""
        order, anchor = self._anchor_at(self.playhead)
        if order is None:
            return
        marks = self._pull_marks(order)
        marks.append(dict(anchor, kind=kind, name=""))
        self.app._assembly_set_markers({order: marks})
        self.sel_marker = (order, len(marks) - 1)
        self.refresh()

    def select_marker(self, key):
        self.sel_marker = key
        self.sel_order = None
        self._mark_selected_in_script()
        self.redraw()
        self._update_status()

    def _move_marker(self, key, t):
        """Re-anchor a dragged mark where it was dropped; it may change
        pulls, which is one undo step across both."""
        order, idx = key
        marks = self._pull_marks(order)
        if not 0 <= idx < len(marks):
            self.refresh()
            return
        new_order, anchor = self._anchor_at(t)
        m = marks[idx]
        moved = dict(anchor, kind=m.get("kind"), name=m.get("name", ""))
        if new_order == order:
            marks[idx] = moved
            changes = {order: marks}
            self.sel_marker = (order, idx)
        else:
            del marks[idx]
            dest = self._pull_marks(new_order) + [moved]
            changes = {order: marks, new_order: dest}
            self.sel_marker = (new_order, len(dest) - 1)
        self.app._assembly_set_markers(changes)
        self.refresh()

    def delete_marker(self):
        if self.sel_marker is None:
            return
        order, idx = self.sel_marker
        marks = self._pull_marks(order)
        self.sel_marker = None
        if 0 <= idx < len(marks):
            del marks[idx]
            self.app._assembly_set_markers({order: marks})
        self.refresh()

    def _on_track_double(self, e):
        kind, m, _k = self._hit(e.x, e.y)
        if kind == "marker":
            self._begin_rename((m["order"], m["index"]))

    def _begin_rename(self, key):
        """Name a mark in place: a field over its flag, Enter to keep,
        Esc to leave it as it was.  An empty name goes back to the count."""
        self._end_rename(commit=False)
        m = self._marker(key)
        if m is None:
            return
        entry = tk.Entry(self.cv, font=self._font, bg=SURF2, fg=TEXT,
                         insertbackground=TEXT, relief="flat", width=28,
                         highlightthickness=1, highlightbackground=ACCENT)
        entry.insert(0, m["name"])
        entry.bind("<Return>", lambda e: self._end_rename(commit=True))
        entry.bind("<Escape>", lambda e: self._end_rename(commit=False))
        self._rename = (entry, key)
        self.redraw()
        entry.focus_set()
        entry.select_range(0, "end")

    def _end_rename(self, commit):
        if self._rename is None:
            return
        entry, (order, idx) = self._rename
        self._rename = None
        name = entry.get().strip()
        entry.destroy()
        marks = self._pull_marks(order)
        if commit and 0 <= idx < len(marks) and \
                name != (marks[idx].get("name") or ""):
            marks[idx] = dict(marks[idx], name=name)
            self.app._assembly_set_markers({order: marks})
            self.refresh()
        else:
            self.redraw()
        self.cv.focus_set()

    def nudge_gap(self, frames):
        """Move the selected pull (and everything after it) by frames."""
        c = self._clip(self.sel_order)
        prev = self._prev_clip(c) if c is not None else None
        if prev is None:
            return
        fps = float(getattr(self.app, "_seq_fps", 24.0) or 24.0)
        self.app._assembly_set_gap(prev.order, max(0.0, prev.gap + frames / fps))
        self.refresh()

    def _on_wheel(self, e):
        notches = e.delta / 120.0
        if e.state & 0x0004:                       # Ctrl: zoom at the mouse
            t = self._t(e.x)
            self.pps = min(max(self.pps * (1.25 ** notches), self._min_pps()), 800.0)
            self.view0 = t - e.x / self.pps
        else:
            self.view0 -= notches * self._span() * 0.15
        self.redraw()

    def _on_overview(self, e):
        if self.total_s <= 0:
            return
        t = e.x / max(1, self.ov.winfo_width()) * self.total_s
        self.view0 = t - self._span() / 2.0
        self.redraw()

    def _min_pps(self):
        return max(1, self.cv.winfo_width()) / max(10.0, self.total_s * 1.02)

    def zoom(self, factor):
        anchor = self.playhead if self.view0 <= self.playhead <= \
            self.view0 + self._span() else self.view0 + self._span() / 2.0
        frac = (anchor - self.view0) / self._span()
        self.pps = min(max(self.pps * factor, self._min_pps()), 800.0)
        self.view0 = anchor - frac * self._span()
        self.redraw()

    def fit(self):
        self.pps = self._min_pps()
        self.view0 = 0.0
        self.redraw()

    def _fit_initial(self):
        # A minute across the screen: close enough to read the timing,
        # wide enough to hear a scene play out.
        if self._closed:
            return
        self.pps = max(self._min_pps(), max(1, self.cv.winfo_width()) / 60.0)
        self.redraw()

    def _neighbour(self, direction):
        """Start of the next / previous pull from the playhead."""
        i = bisect.bisect_right(self._starts, self.playhead + 1e-3) - 1
        j = i + 1 if direction > 0 else (
            i if (0 <= i < len(self.clips) and
                  self.playhead - self.clips[i].start > 0.25) else i - 1)
        if 0 <= j < len(self.clips):
            c = self.clips[j]
            self.select(c.order)
            self.seek(c.start)

    # ── Playback ─────────────────────────────────────────────────────────
    def toggle_play(self):
        if self.player is None:
            self.status.config(text="Playback needs the sounddevice package "
                                    "(pip install sounddevice).", fg=ERR)
            return
        if self.player.is_playing():
            self.stop()
            return
        if self.playhead >= self.total_s - 0.05:
            self.playhead = 0.0
        self.player.set_rate(self._rate)
        if not self.player.play(start_s=self.playhead):
            self.status.config(text="Couldn't open the audio device.", fg=ERR)
            return
        self.play_btn.config(text="❚❚  STOP")
        self._tick()

    def stop(self):
        if self.player is not None:
            self.player.stop()
        if self._tick_after is not None:
            try:
                self.win.after_cancel(self._tick_after)
            except tk.TclError:
                pass
            self._tick_after = None
        try:
            self.play_btn.config(text="▶  PLAY")
        except tk.TclError:
            pass

    def _tick(self):
        self._tick_after = None
        if self._closed or self.player is None:
            return
        playing = self.player.is_playing()
        self.playhead = max(0.0, min(self.player.position(), self.total_s))
        span = self._span()
        if self.playhead > self.view0 + span * 0.92 or self.playhead < self.view0:
            self.view0 = self.playhead - span * 0.08
            self.redraw()
        else:
            self._move_playhead()
        order = self._order_at(self.playhead)
        if order != self._cur_order:
            self._highlight_script(order)
            self._update_status()
        if playing:
            self._tick_after = self.win.after(30, self._tick)
        else:
            self.stop()

    def step_rate(self, d):
        rates = match_review._PLAY_RATES
        i = min(range(len(rates)), key=lambda k: abs(rates[k] - self._rate))
        i = max(0, min(len(rates) - 1, i + d))
        self._rate = rates[i]
        # Shared with the per-pull editor, so a speed picked here is the
        # speed the next card opens at, and the other way round.
        match_review._LAST_PLAY_RATE = self._rate
        if self.player is not None:
            self.player.set_rate(self._rate)
        self._show_rate()

    def _show_rate(self):
        self.rate_lbl.config(text="{:g}×".format(self._rate))

    # ── Keys ─────────────────────────────────────────────────────────────
    def _on_key(self, e):
        if isinstance(e.widget, tk.Entry):
            return "break"                   # typing a mark's name
        k = e.keysym
        ctrl = bool(e.state & 0x0004)
        if k == "space":
            self.toggle_play()
        elif k == "bracketleft":
            self.step_rate(-1)
        elif k == "bracketright":
            self.step_rate(1)
        elif k in ("plus", "equal", "KP_Add"):
            self.zoom(1.5)
        elif k in ("minus", "underscore", "KP_Subtract"):
            self.zoom(1 / 1.5)
        elif k == "Home":
            self.seek(0.0)
        elif k == "End":
            self.seek(self.total_s)
        elif k == "Right":
            self._neighbour(1)
        elif k == "Left":
            self._neighbour(-1)
        elif k in ("Delete", "BackSpace"):
            if self.sel_marker is not None:
                self.delete_marker()
            else:
                self.delete_range()
        elif not ctrl and k.lower() in ("i", "o", "m"):
            self.drop_marker({"i": "music_in", "o": "music_out",
                              "m": "sfx"}[k.lower()])
        elif k in ("comma", "period"):
            self.nudge_gap(-1 if k == "comma" else 1)
        elif k in ("less", "greater"):
            self.nudge_gap(-10 if k == "less" else 10)
        elif ctrl and k.lower() == "z":
            # Step 4's own history: edits here and on the cards share it.
            (self.app._s4_redo if e.state & 0x0001 else self.app._s4_undo)()
        elif ctrl and k.lower() == "y":
            self.app._s4_redo()
        elif ctrl and k.lower() == "c" and e.widget is self.text:
            return None                      # let the script copy
        elif k.lower() == "f" and not ctrl:
            self.fit()
        elif k == "Escape":
            self._range = None
            self.sel_marker = None
            self.stop()
            self.redraw()
            self._update_status()
        return "break"

    # ── Lifetime ─────────────────────────────────────────────────────────
    def lift(self):
        try:
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()
        except tk.TclError:
            pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.stop()
        if self.player is not None:
            self.player.close()
        self._pool.shutdown(wait=False, cancel_futures=True)
        for aid in (self._refresh_after,):
            if aid is not None:
                try:
                    self.win.after_cancel(aid)
                except tk.TclError:
                    pass
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        if getattr(self.app, "_assembly", None) is self:
            self.app._assembly = None
