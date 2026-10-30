"""Assembly view -- the reconciled episode as one continuous track.

Step 4 reviews one pull at a time.  This is the other way of looking at the
same data: every placed pull laid end to end, exactly where build_aaf will
put it (engines.layout_timeline is the one source of placement for both),
played back as a single reference track, its text beside the controls
above it.  Every divider drags (narrow the text, size each control
panel), and the layout is kept in the app's prefs.  Ctrl+= goes back to
Step 4 and comes here again, like Pro Tools' Edit / Mix toggle; Ctrl+S
saves the session.

The track is the same "single reference" Step 4 already plays -- an
interview pull sounds from its transcription mix, a VO pull from its take --
so it is a listening and timing view, not a mix.  The AAF still breaks the
cut out by source.

The mouse works like Pro Tools' Smart Tool: on a pull, the top half is
the selector (click places the insertion point, drag highlights a span) and
the bottom half the grabber (click selects the region, drag moves it,
double-click opens the waveform editor); the edges trim.  Every action is
also a button with its key in the panels beside the script, which are the
window's key.

The track is also the editor.  Every edit is the same edit Step 4 makes, committed through the card's own
accept (segments, and seg_gaps -- the silence at their joins) or the
result's gap_after_s, so Step 4's undo, its saved state and the AAF all
see it.  Each of a pull's segments is a region, and its own clip in the
AAF:

  * B splits the region under the insertion point in two there (or at
    both ends of a highlighted span): the same audio, two regions;
  * drag a pull's edge, or either side of a cut or split inside it, to
    trim -- shuffle-style, so everything after it moves up or back;
  * drag a region, or the gap in front of it, to change that gap -- the
    gap after the pull before it, or inside a pull, the silence at that
    join;
  * , and . nudge the selected region (or pull) a frame earlier or later
    the same way (< > by ten), for pacing;
  * highlight part of a pull and press Delete to take it out (a whole
    pull, selected with the grabber: it is IGNOREd);
  * double-click a pull with the grabber (or Enter) to open Step 4's
    waveform editor on it, for hunting the right edit in audio outside the
    pull; double-click with the selector takes the whole region.

It is also where the cut gets spotted.  I and O drop a music cue's in and
out, U a music inflection (a moment the music should answer, in or out to
be decided), M an SFX spot and K a note, at the playhead -- while it plays,
too.  Music and SFX share a lane on the ruler, notes have their own.  Each
mark lives on the pull it lands on (engines.place_markers), so it rides
with that audio through any retiming, and the AAF carries them as the
MUSIC, SFX and NOTES marker tracks beside PARTS.  Drag a flag to move it,
double-click to name it, Delete to remove it; an SFX spot or a note opens
for naming as it lands.

The text beside the controls is the transcript of the cut, and it edits like
the track.  Highlight words and they are the selection, on the track too, for
Space, Delete, B, the spotting keys or the right-click menu, across as many
pulls as you like (one undo step).  Tape that is out of the cut stays in the
text, struck through -- what went from inside a pull, and a little either side
of it -- and H puts highlighted words back, as Pro Tools' heal does.  W swaps
the transcript for the script, which highlights the same way wherever its
words are matched to the audio.  Words from the tiny first pass read faded
while the chosen model works through them (refine.py), and firm up in place.

While the cut plays, the text lights each word as it is said.

Audio is pulled per pull, with a few seconds of handle either side, from a
small thread pool, nearest-the-view first, and composed on demand by
_AssemblyAudio as the player asks for it.  Nothing is rendered to disk.
"""
import bisect
import concurrent.futures
import difflib
import math
import os
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox

import numpy as np

import engines
import match_review
import playback
from config import (BG, SURF, SURF2, SURF3, BORDER, ACCENT, TEXT, SUB,
                    WARN, ERR, FB, FBT, HOST_NAME)
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
CUT_SHOW_S  = 1.0     # tape taken out at a joint before it gets a badge
WORD_HOLD_S = 0.3     # a script word stays lit this far into the pause after it
SCRIPT_HOLD_S = 4.0   # the script stops following this long after you scroll it
SCRIPT_BAND = (0.12, 0.7)   # the lit word may sit anywhere in this band of
SCRIPT_AT   = 0.3           # the pane; outside it, it is scrolled back to here
SEL_PAD     = 0.15    # a highlight's edge sits at most this far outside its word
                      # (halfway to the next word, if that is nearer)
TX_HANDLE_S = 2.0     # tape shown struck through either side of a pull
TX_CUT_MAX  = 30      # a longer run of cut words shows only its ends...
TX_CUT_ENDS = 6       # ...this many at each, and a count for the rest
MAX_WORD_S  = 3.0     # how far ahead of a piece a word it keeps could start

_SMALL      = (FB[0], 9)
_MONO       = ("Consolas", 10)
_TC_FONT    = ("Consolas", 15, "bold")

_C_VO       = "#9d8cf0"   # VO waveform
_C_HOST     = "#8a8a8a"   # host waveform
# Interview tokens, in alphabetical order of token -- clear of the amber
# review flag, the orange accent and the music / SFX marker colours.
_TOKEN_COLOURS = ("#20a080", "#4fc3f7", "#8bc34a", "#e57373", "#ba68c8",
                  "#4db6ac", "#dce775", "#90a4ae", "#a1887f", "#7986cb")
_C_FLAG     = WARN        # a pull Step 4 still flags for review
_C_PLAYHEAD = ERR
_C_PART     = ACCENT
_C_CLIP_BG  = SURF
_C_MUSIC    = "#4f9be0"   # music cue in / out
_C_SFX      = "#e0609a"   # SFX spot
_C_NOTE     = "#c9c9a8"   # a note: anything else worth marking
_C_CUT      = "#6a6a6a"   # words out of the cut, in the text
_C_TXSEL    = "#35597a"   # highlighted words



# Monitoring choices, kept for the run like the playback speed.
_MONITOR = {"follow": True, "preroll_on": False, "preroll_s": 2.0,
            "return_on": False, "script": True,
            "transcript": True, "cut_words": True}
_PREROLLS = (0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 8.0, 10.0)


class _Flow(tk.Frame):
    """Widgets laid out left to right, wrapping at the frame's width -- so
    a control panel can be made as narrow or as wide as you like.  Only a
    change of width re-flows it.  The width is the parent's less `pad`: a
    panel squeezed shut unmaps the flow, which then hears nothing."""

    def __init__(self, parent, pad=0, **kw):
        tk.Frame.__init__(self, parent, **kw)
        self._items = []                 # (widget, wraps to the width)
        self._width = 0
        self._pad = pad
        parent.bind("<Configure>", self._on_configure, add="+")

    def add(self, widget, fill=False):
        self._items.append((widget, fill))
        if self._width:
            self._reflow()
        return widget

    def remove(self, widget):
        self._items = [it for it in self._items if it[0] is not widget]

    def _on_configure(self, e):
        width = e.width - self._pad
        if width != self._width:
            self._width = width
            self._reflow()

    def _reflow(self):
        x = y = row_h = 0
        width = max(1, self._width)
        for w, fill in self._items:
            if fill:
                w.config(wraplength=max(60, width - 4))
            rw, rh = w.winfo_reqwidth(), w.winfo_reqheight()
            if x and x + rw > width:
                x, y, row_h = 0, y + row_h + 4, 0
            w.place(x=x, y=y)
            x += rw + 4
            row_h = max(row_h, rh)
        self.config(height=max(1, y + row_h))


class _KeyButton(object):
    """One entry of the command bar: what it does, and its key -- a button
    and the window's key at once.  A toggle shows its state."""

    def __init__(self, parent, text, key, command, toggle=False):
        self.toggle = toggle
        self.on = False
        self.frame = tk.Frame(parent, bg=BORDER, padx=1, pady=1, cursor="hand2")
        self.lbl = tk.Label(self.frame, text=text, font=(FB[0], 10), bg=SURF3,
                            fg=TEXT, padx=7, pady=2)
        self.lbl.pack(side="left")
        parts = [self.lbl]
        if key:
            self.key = tk.Label(self.frame, text=key, font=("Consolas", 10, "bold"),
                                bg=SURF2, fg=ACCENT, padx=5, pady=2)
            self.key.pack(side="left")
            parts.append(self.key)
        for w in parts:
            w.bind("<Button-1>", lambda e: command())
            w.bind("<Enter>", lambda e: self.lbl.config(bg=BORDER))
            w.bind("<Leave>", lambda e: self._paint())

    def _paint(self):
        self.lbl.config(bg=ACCENT if self.on else SURF3,
                        fg=TEXT)

    def set_on(self, on):
        self.on = bool(on)
        self._paint()

    def set_text(self, text):
        self.lbl.config(text=text)

    def pack(self, **kw):
        self.frame.pack(**kw)
        return self


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
        # Seconds of silence at each join between them (engines.kept_segments).
        gaps = [float(g) for g in entry.get("seg_gaps") or ()]
        self.seg_gaps  = gaps if len(gaps) == len(self.segments) - 1 else \
            [0.0] * max(0, len(self.segments) - 1)
        if self.pieces:
            self.src_lo = min(si for _, _, si in self.pieces)
            self.src_hi = max(si + du for _, du, si in self.pieces)
        else:
            self.src_lo = self.src_hi = 0.0
        self.words = None              # [(tl_start, tl_end, text)] once loaded
        self.load_error = None
        self.tint = _C_VO                # the token's colour (see _layout)

    @property
    def label(self):
        lbl = "#{:03d}  {}".format(self.order, self.res.get("token", "?"))
        n = len(self.res.get("takes_data") or [])
        if self.res.get("monolog") and n > 1:
            lbl += "  ·  reading {}/{}".format(
                (self.res.get("best_take_index") or 0) + 1, n)
        return lbl

    def colour(self):
        return self.tint

    def src_at(self, t):
        """Source time under timeline time t, or None in no segment."""
        k = self.piece_at(t)
        return None if k is None else self.pieces[k][2] + (t - self.pieces[k][0])

    def keeps(self, src):
        """Is source time `src` in the cut, in this pull?"""
        return any(si <= src < si + du for _tl, du, si in self.pieces)

    def piece_at(self, t):
        """Index of the region (segment) under timeline time t, or None."""
        for k, (tl, du, _si) in enumerate(self.pieces):
            if tl <= t < tl + du:
                return k
        return None


def _tidy(segments, gaps=None):
    """Drop the slivers an edit has left, keeping the regions in timeline
    order.  `gaps` is the silence at each join; a sliver's passes to the
    region after it.  Regions that butt stay apart -- a split is two
    regions of continuous audio.  Returns (segments, gaps)."""
    gaps = list(gaps or ())
    out, out_g, carry = [], [], 0.0
    for k, (a, b) in enumerate(segments):
        if 0 < k <= len(gaps):
            carry += gaps[k - 1]
        if b - a < MIN_SEG_S:
            continue
        if out:
            out_g.append(carry)
        out.append((a, b))
        carry = 0.0
    return out, out_g


def _segments_without(clip, a, b):
    """The clip's segments, and the silence at their joins, with timeline
    span [a, b] taken out -- audio and silence alike, shuffle-style."""
    segs, before, pend = [], [], 0.0
    for k, ((s, e), (tl, du, _si)) in enumerate(zip(clip.segments, clip.pieces)):
        if k:
            g = clip.seg_gaps[k - 1]
            pend += max(0.0, g - max(0.0, min(b, tl) - max(a, tl - g)))
        ra, rb = max(a, tl), min(b, tl + du)
        for seg in ([(s, e)] if rb <= ra else
                    [(s, s + (ra - tl)), (s + (rb - tl), e)]):
            segs.append(seg)
            before.append(pend)
            pend = 0.0
    return _tidy(segs, before[1:])


def _segments_with(segments, spans):
    """A pull's segments with source spans put back.  A span that meets a
    segment heals into it; the others stay as they were, butted or not."""
    out = [(float(a), float(b)) for a, b in segments]
    for a, b in spans:
        rest = []
        for s, e in out:
            if s <= b + 1e-3 and a <= e + 1e-3:
                a, b = min(a, s), max(b, e)
            else:
                rest.append((s, e))
        out = sorted(rest + [(a, b)])
    return out


_WORD_INDEX = {}


def _indexed(words):
    """(words in time order, their start times), worked out once per list --
    a pull looks its words up by time, many times a second while they stream."""
    memo = _WORD_INDEX.get(id(words))
    if memo is not None and memo[0] is words:
        return memo[1], memo[2]
    if len(_WORD_INDEX) > 64:
        _WORD_INDEX.clear()
    ordered = sorted((w for w in words or ()
                      if isinstance(w, dict) and "start" in w
                      and not w.get("break") and not w.get("_deleted")),
                     key=lambda w: float(w["start"]))
    starts = [float(w["start"]) for w in ordered]
    _WORD_INDEX[id(words)] = (words, ordered, starts)
    return ordered, starts


def _word_point(s0, s1):
    """Where a word counts as being, for in or out of the cut: two thirds of
    the way through.  Whisper's starts run early, so the first word of a pull
    often "starts" ahead of the edit that has all of it."""
    return s0 + (s1 - s0) * 2.0 / 3.0


def _word_units(w):
    """A transcript word as it reads: [(start, end, text, glued, model)], the
    word and any figures riding on it (engines.transcribe_clip).  Glued: no
    space in front of it, where Whisper wrote it so."""
    s0 = float(w["start"])
    has_text = "text" in w
    raw = str(w.get("text", w.get("word", "")))
    src = w.get("_src")
    out = []
    if raw.strip():
        out.append((s0, float(w.get("end", s0)), raw.strip(),
                    has_text and not raw[:1].isspace(), src))
    for x in w.get("extras") or ():
        t = str(x.get("text", ""))
        if t.strip():
            xs = float(x.get("start", s0))
            out.append((xs, float(x.get("end", xs)), t.strip(),
                        not t[:1].isspace(), src))
    return out


class _Tok(object):
    """One word of a pull's line in the text.  cs..ce: where it sits in the
    line.  t0..t1: when it plays, if it is in the cut (kept); s0..s1: where it
    is in the source, for the transcript's words.  k: its region."""
    __slots__ = ("cs", "ce", "t0", "t1", "s0", "s1", "kept", "k")

    def __init__(self, cs, ce, t0, t1, s0, s1, kept, k):
        self.cs, self.ce, self.t0, self.t1 = cs, ce, t0, t1
        self.s0, self.s1, self.kept, self.k = s0, s1, kept, k


class _Line(object):
    """One pull's words in the text: the line, its words, and the runs of it
    that take a style (the whole pane is one insert per pull, no tag a word)."""

    def __init__(self, body, tokens, chunks=None, script=False, words=None):
        self.body = body
        self.tokens = tokens
        self.chunks = chunks if chunks is not None else [(body, None)]
        self.script = script             # the script's words, not the transcript's
        self.words = words               # the clip's words it was timed against
        self.starts = [t.cs for t in tokens]
        self.kept = [t for t in tokens if t.kept]
        self.kept_t0 = [t.t0 for t in self.kept]


def _transcript_line(c, words, neighbours=(), show_cut=True, fade=False):
    """The pull as its transcript reads, or None when it holds no word of
    what is in the cut.  With show_cut, the tape it leaves out -- inside it,
    and TX_HANDLE_S either side unless a neighbouring pull plays that -- is
    there too, as words that aren't kept.  fade: the draft's words read dim."""
    ordered, starts = _indexed(words)
    if not ordered or not c.pieces:
        return None
    reach = TX_HANDLE_S if show_cut else 0.0
    i = bisect.bisect_left(starts, c.src_lo - reach - MAX_WORD_S)
    units, any_kept = [], False
    while i < len(ordered) and starts[i] < c.src_hi + reach:
        for s0, s1, text, glued, src in _word_units(ordered[i]):
            at = _word_point(s0, s1)
            k = next((n for n, (_tl, du, si) in enumerate(c.pieces)
                      if si <= at < si + du), None)
            if k is None:
                if not show_cut or not c.src_lo - reach <= at < c.src_hi + reach:
                    continue
                if (at < c.src_lo or at >= c.src_hi) and \
                        any(n.keeps(at) for n in neighbours):
                    continue
            else:
                any_kept = True
            units.append((s0, s1, text, glued, src, k))
        i += 1
    if not any_kept:
        return None

    # A long run of cut tape reads as its two ends and a count.
    shown, j = [], 0
    while j < len(units):
        if units[j][5] is not None:
            shown.append(units[j])
            j += 1
            continue
        e = j
        while e < len(units) and units[e][5] is None:
            e += 1
        run = units[j:e]
        if len(run) > TX_CUT_MAX:
            mid = run[TX_CUT_ENDS:-TX_CUT_ENDS]
            shown.extend(run[:TX_CUT_ENDS])
            shown.append((mid[0][0], mid[-1][1], "[{} words]".format(len(mid)),
                          False, None, None))
            shown.extend(run[-TX_CUT_ENDS:])
        else:
            shown.extend(run)
        j = e

    tokens, chunks, pos, prev_style = [], [], 0, None

    def emit(text, style):
        if chunks and chunks[-1][1] == style:
            chunks[-1][0] += text
        else:
            chunks.append([text, style])

    for s0, s1, text, glued, src, k in shown:
        style = "txcut" if k is None else (
            "txdraft" if fade and src == engines.DRAFT_MODEL else None)
        if tokens and not glued:
            emit(" ", style if style == prev_style else None)
            pos += 1
        emit(text, style)
        if k is None:
            t0 = t1 = None
        else:
            tl, du, si = c.pieces[k]
            t0 = max(tl, tl + s0 - si)
            t1 = max(t0, min(tl + du, tl + s1 - si))
        tokens.append(_Tok(pos, pos + len(text), t0, t1, s0, s1, k is not None, k))
        pos += len(text)
        prev_style = style
    return _Line("".join(x[0] for x in chunks), tokens,
                 [tuple(x) for x in chunks])


_WORD_RE = re.compile(r"\S+")


def _norm_word(w):
    return re.sub(r"[^0-9a-z]", "", w.lower())


def _script_timing(body, words):
    """When each word of a pull's script line is said.

    body  : the line as the script pane shows it.
    words : the pull's transcript words on the timeline, [(t0, t1, text)].

    The line's words are matched to the transcript's in order (difflib); a
    run the transcript has differently ("gonna" for "going to") shares out
    the time between its matched neighbours.  Returns [(t0, t1, char_start,
    char_end)] in order -- empty when nothing matches."""
    toks = [(m.start(), m.end(), _norm_word(m.group()))
            for m in _WORD_RE.finditer(body or "")]
    toks = [x for x in toks if x[2]]
    if not toks or not words:
        return []
    sm = difflib.SequenceMatcher(None, [x[2] for x in toks],
                                 [_norm_word(w[2]) for w in words],
                                 autojunk=False)
    times = [None] * len(toks)
    for i, j, n in sm.get_matching_blocks():
        for d in range(n):
            times[i + d] = (words[j + d][0], words[j + d][1])
    if not any(times):
        return []
    i = 0
    while i < len(toks):
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < len(toks) and times[j] is None:
            j += 1
        if i > 0 and j < len(toks):
            lo, hi = times[i - 1][1], times[j][0]
            if hi - lo >= 0.05 * (j - i):    # else the words were cut out
                step = (hi - lo) / (j - i)
                for n in range(i, j):
                    times[n] = (lo + (n - i) * step, lo + (n - i + 1) * step)
        i = j
    return [(tm[0], tm[1], cs, ce)
            for (cs, ce, _k), tm in zip(toks, times) if tm is not None]


def _script_line(body, c):
    """The pull's script line, with its words timed from the transcript."""
    tokens = [_Tok(cs, ce, t0, t1, None, None, True, c.piece_at(t0))
              for t0, t1, cs, ce in _script_timing(body, c.words or [])]
    return _Line(body, tokens, script=True, words=c.words)


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
        self.insert     = 0.0            # insertion point: where play starts
        self._playing   = False
        self._play_origin = 0.0          # where the last play started
        self._token_tints = {}
        self.sel_order  = None
        self.sel_region = None           # (order, k): one region, by the grabber
        self._cur_order = None           # pull the playhead is in
        self._word_items = []            # [(tl_start, tl_end, item)] on screen
        self._lit_word  = None
        self._lit_script = None          # (order, char_start, char_end) lit
        self._lit_range = None           # its Text indices
        self._touched_at = 0.0           # when the script was last scrolled
        self._scroll_after = None        # a smooth script scroll under way
        self._env       = {}             # order -> (t0, envelope float32)
        self._env_scale = 0.25           # amplitude that fills the lane
        self._loading   = set()
        self._dirty     = False          # a buffer landed; redraw pending
        self._refresh_after = None
        self._tick_after = None
        self._closed    = False
        self._by_order  = {}
        self._words_memo = {}            # path -> the file's saved transcript
        self._words_lock = threading.Lock()
        self._words_queued = set()
        self._tx_lines  = {}             # order -> its _Line in the text
        self._tx_order  = []             # the orders down the text
        self._tx_seq    = {}             # order -> its place in that
        self._tx_anchor = None           # highlighted words: (order, word) where
        self._tx_head   = None           # it started, and where it reaches
        self._tx_range  = None           # the _range they made
        self._tx_click  = None           # where the last click landed (Shift extends)
        self._tx_painted = None
        self._tx_fading = False          # draft words shown faded
        self._tdrag     = None           # a highlight being dragged
        self._tx_render_after = None
        self._tx_scroll_after = None
        self._file_end  = {}             # path -> length, once a read ran short
        self._inputs    = None           # export inputs, held while dragging
        self._drag      = None           # the gesture in progress
        self._range     = None           # (order, a, b) highlighted span
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
        # Ruler rows: timecodes, Part flags, music / SFX marks, notes.
        self._ruler_h = 4 * self._ls + 10
        self._gap_h   = self._ls + 6           # gap lengths under the track

        self._build_ui()
        self.refresh()
        self.win.after(60, self._fit_initial)

    # ── Construction ─────────────────────────────────────────────────────
    def _build_ui(self):
        """Script on the left, the controls in resizable panels to its
        right, the track across the bottom.  Every divider drags, and the
        layout is kept in the app's prefs for next time."""
        self._outer = tk.PanedWindow(self.win, orient="vertical", bg=BORDER,
                                     sashwidth=6, bd=0, sashrelief="flat")
        self._outer.pack(fill="both", expand=True)
        self._top = tk.PanedWindow(self._outer, orient="horizontal",
                                   bg=BORDER, sashwidth=6, bd=0,
                                   sashrelief="flat")

        # Script pane
        script = tk.Frame(self._top, bg=BG)
        sb = tk.Scrollbar(script, orient="vertical")
        sb.pack(side="right", fill="y")
        self.text = tk.Text(script, bg=BG, fg=TEXT, font=(FB[0], 12),
                            wrap="word", bd=0, highlightthickness=0, padx=24,
                            pady=14, spacing1=2, spacing3=8, cursor="xterm",
                            insertwidth=0, yscrollcommand=sb.set, width=40)
        self.text.pack(side="left", fill="both", expand=True)
        sb.config(command=self.text.yview)
        # Scrolling the script yourself holds its follow off a while.
        for w in (self.text, sb):
            w.bind("<MouseWheel>", self._script_touched, add="+")
        sb.bind("<Button-1>", self._script_touched, add="+")
        sb.bind("<B1-Motion>", self._script_touched, add="+")
        self.text.tag_config("part", font=FBT, foreground=_C_PART,
                             spacing1=16, spacing3=6)
        self.text.tag_config("tok", font=_MONO, foreground=SUB)
        self.text.tag_config("out", foreground="#6a6a6a")
        self.text.tag_config("cur", background=SURF3)
        self.text.tag_config("mk_music", foreground=_C_MUSIC, font=_SMALL)
        self.text.tag_config("mk_sfx", foreground=_C_SFX, font=_SMALL)
        self.text.tag_config("mk_note", foreground=_C_NOTE, font=_SMALL)
        self.text.tag_config("sel_pull", background=SURF2)
        self.text.tag_config("txdraft", foreground=SUB)
        self.text.tag_config("txcut", foreground=_C_CUT, overstrike=True)
        self.text.tag_config("txsel", background=_C_TXSEL)
        self.text.tag_config("word", background=ACCENT, foreground=TEXT)
        self.text.tag_raise("cur")
        self.text.tag_raise("txsel")
        self.text.tag_raise("word")
        self.text.bind("<Button-1>", self._on_text_press)
        self.text.bind("<B1-Motion>", self._on_text_drag)
        self.text.bind("<ButtonRelease-1>", self._on_text_release)
        self.text.bind("<Double-Button-1>", self._on_text_double)
        self.text.bind("<Triple-Button-1>", self._on_text_triple)
        self.text.bind("<Button-3>", self._on_text_menu)
        # Tk's own auto-scan would run the native selection over ours.
        for seq in ("<B1-Leave>", "<B1-Enter>", "<Control-Button-1>"):
            self.text.bind(seq, lambda e: "break")
        self._top.add(script, minsize=200, stretch="always")

        # Control panels, one above the other
        self._side = tk.PanedWindow(self._top, orient="vertical", bg=BORDER,
                                    sashwidth=5, bd=0, sashrelief="flat")
        self._top.add(self._side, minsize=180, stretch="never")
        self._build_panels()

        # Track pane
        bottom = tk.Frame(self._outer, bg=BG)
        self.ov = tk.Canvas(bottom, height=24, bg=SURF2, highlightthickness=0,
                            cursor="hand2")
        self.ov.pack(fill="x", pady=(6, 0))
        self.cv = tk.Canvas(bottom, bg=BG, highlightthickness=0,
                            cursor="arrow")
        self.cv.pack(fill="both", expand=True, pady=(2, 0))
        foot = tk.Frame(bottom, bg=BG)
        foot.pack(fill="x", pady=(0, 4))
        self.load_lbl = tk.Label(foot, text="", font=(FB[0], 10), bg=BG,
                                 fg=SUB, padx=10)
        self.load_lbl.pack(side="right")
        self.status = tk.Label(foot, text="", font=(FB[0], 10), bg=BG,
                               fg=SUB, anchor="w", padx=10)
        self.status.pack(side="left", fill="x", expand=True)

        self._outer.add(self._top, minsize=160, stretch="always")
        self._outer.add(bottom, minsize=240, stretch="always")
        self.win.update_idletasks()
        self._restore_layout()

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

    def _panel(self, title):
        """One control panel: a title and its buttons, wrapping to the
        panel's width."""
        f = tk.Frame(self._side, bg=SURF)
        tk.Label(f, text=title, font=(FB[0], 9, "bold"), bg=SURF, fg=SUB,
                 anchor="w").pack(fill="x", padx=8, pady=(6, 2))
        flow = _Flow(f, pad=16, bg=SURF)
        flow.pack(fill="x", padx=8, pady=(0, 6))
        self._side.add(f, minsize=self._ls + 12, stretch="always")
        return flow

    def _build_panels(self):
        """The window's key: every action as a button labelled with its
        key, in panels the way the work goes -- session, play and monitor,
        edit, spot, look -- then the mouse zones and the speakers'
        colours."""
        def key(flow, text, k, cmd, toggle=False):
            b = _KeyButton(flow, text, k, cmd, toggle)
            flow.add(b.frame)
            return b

        p = self._panel("SESSION")
        key(p, "◂  Step 4", "Ctrl+=", self.to_step4)
        self.save_key = key(p, "Save", "Ctrl+S", self.save)

        p = self._panel("PLAY  ·  MONITOR")
        self.tc_lbl = tk.Label(p, text=_fmt_tc(0), font=_TC_FONT, bg=SURF,
                               fg=TEXT, width=10, anchor="w")
        p.add(self.tc_lbl)
        self.play_key = key(p, "▶  Play", "Space", self.toggle_play)
        key(p, "■  Stop  (again: back to start)", "0",
            lambda: self.stop(key=True))
        key(p, "Slower", "[", lambda: self.step_rate(-1))
        self.rate_lbl = tk.Label(p, text="", font=("Consolas", 12, "bold"),
                                 bg=SURF, fg=TEXT, width=5)
        p.add(self.rate_lbl)
        key(p, "Faster", "]", lambda: self.step_rate(1))
        self.preroll_key = key(p, "", "P", self.toggle_preroll, toggle=True)
        key(p, "−", "", lambda: self.step_preroll(-1))
        key(p, "+", "", lambda: self.step_preroll(1))
        self.return_key = key(p, "Return to start on stop", "N",
                              self.toggle_return, toggle=True)
        self.follow_key = key(p, "Follow playhead", "S", self.toggle_follow,
                              toggle=True)
        self.script_key = key(p, "Script follows words", "Shift+S",
                              self.toggle_script_follow, toggle=True)

        p = self._panel("EDIT")
        key(p, "Delete selection", "Del", self._delete_key)
        key(p, "Split", "B", self.split)
        key(p, "Nudge earlier", ",", lambda: self.nudge(-1))
        key(p, "Nudge later", ".", lambda: self.nudge(1))
        key(p, "Undo", "Ctrl+Z", self.app._s4_undo)
        key(p, "Redo", "Ctrl+Shift+Z", self.app._s4_redo)
        key(p, "Open in editor", "Enter", self.open_selected)
        gap = tk.Frame(p, bg=SURF)
        tk.Label(gap, text="Default gap", font=(FB[0], 10), bg=SURF,
                 fg=TEXT).pack(side="left", padx=(0, 4))
        self._gap_box = tk.Spinbox(
            gap, from_=0.0, to=60.0, increment=0.05, format="%.2f", width=5,
            font=(FB[0], 10), bg=SURF2, fg=TEXT, buttonbackground=SURF3,
            insertbackground=TEXT, relief="flat", command=self._gap_from_box)
        self._gap_box.pack(side="left")
        tk.Label(gap, text="s  (every gap you haven't set)", font=(FB[0], 9),
                 bg=SURF, fg=SUB).pack(side="left", padx=(4, 0))
        self._gap_box.bind("<Return>", self._gap_from_box)
        self._gap_box.bind("<KP_Enter>", self._gap_from_box)
        self._gap_box.bind("<FocusOut>", self._gap_from_box)
        p.add(gap)
        self._show_default_gap()
        if any(res.get("monolog") for res in (self.app.results or ())):
            key(p, "Other reading", "↑ ↓", lambda: self.cycle_reading(1))

        p = self._panel("TRANSCRIPT")
        self.tx_flow = p
        self.tx_key = key(p, "Transcript", "W", self.toggle_transcript, toggle=True)
        self.cut_key = key(p, "Show cut words", "Shift+W", self.toggle_cut_words,
                           toggle=True)
        key(p, "Restore", "H", self.restore)
        self.refine_key = key(p, "", "", self._refine_again)
        self.recut_key = None
        self.tx_status = tk.Label(p, font=(FB[0], 10), bg=SURF, fg=SUB,
                                  justify="left", anchor="w")
        p.add(self.tx_status, fill=True)

        p = self._panel("SPOT")
        key(p, "Music in", "I", lambda: self.drop_marker("music_in"))
        key(p, "Inflection", "U", lambda: self.drop_marker("music_inflection"))
        key(p, "Music out", "O", lambda: self.drop_marker("music_out"))
        key(p, "SFX", "M", lambda: self.drop_marker("sfx"))
        key(p, "Note", "K", lambda: self.drop_marker("note"))

        p = self._panel("VIEW")
        key(p, "Zoom out", "R  −", lambda: self.zoom(1 / 1.5))
        key(p, "Zoom in", "T  +", lambda: self.zoom(1.5))
        key(p, "Fit", "F", self.fit)
        key(p, "Prev pull", "←", lambda: self._neighbour(-1))
        key(p, "Next pull", "→", lambda: self._neighbour(1))

        p = self._panel("MOUSE  ·  SPEAKERS")
        p.add(tk.Label(p, font=(FB[0], 10), bg=SURF, fg=TEXT, justify="left",
                       anchor="w", text=(
            "Top half of a region: select (click = insertion point, drag = "
            "highlight).  Bottom half: grab (click = region, drag = move, "
            "double-click = waveform editor).  Edge: trim.  Gap: drag to "
            "retime.  Flag: drag, or double-click to name.  "
            "Ctrl+wheel: zoom.  In the text: drag to highlight, double-click "
            "a word, triple-click its pull, right-click for everything.")),
            fill=True)
        self.legend = p
        self._legend_items = []
        self._show_rate()
        self._show_monitor()
        self.show_refine_status()

    def _show_legend(self):
        """Swatches for the speakers on the track, refreshed as they change."""
        for w in self._legend_items:
            self.legend.remove(w)
            w.destroy()
        self._legend_items = []
        for name, col in self._token_tints.items():
            f = tk.Frame(self.legend, bg=SURF)
            tk.Label(f, bg=col, width=2, height=1, bd=0).pack(
                side="left", padx=(0, 3), pady=3)
            tk.Label(f, text=name, font=(FB[0], 10), bg=SURF,
                     fg=TEXT).pack(side="left", padx=(0, 8))
            self.legend.add(f)
            self._legend_items.append(f)

    def _layout_prefs(self):
        prefs = getattr(self.app, "_prefs", None)
        return prefs if isinstance(prefs, dict) else None

    def _restore_layout(self, tries=40):
        """Put the dividers where they were last time (fractions of the
        window), or at the defaults -- once the window has its size (a
        zoomed window is a sliver until it is mapped)."""
        if self._closed:
            return
        self.win.update_idletasks()
        if self._outer.winfo_height() < 300 and tries > 0:
            self.win.after(50, lambda: self._restore_layout(tries - 1))
            return
        lay = (self._layout_prefs() or {}).get("assembly_layout") or {}
        try:
            h = max(1, self._outer.winfo_height())
            w = max(1, self._top.winfo_width())
            self._outer.sash_place(0, 0, int(h * lay.get("track", 0.42)))
            self._top.sash_place(0, int(w * lay.get("script", 0.6)), 0)
            self.win.update_idletasks()
            sh = max(1, self._side.winfo_height())
            panels = lay.get("panels") or []
            if len(panels) != len(self._side.panes()) - 1:
                self._fit_panels()
                return
            for i, f in enumerate(panels):
                self._side.sash_place(i, 0, int(sh * f))
        except (tk.TclError, TypeError, ValueError, AttributeError):
            pass

    def _fit_panels(self):
        """Each control panel as tall as its buttons need at this width (a
        pane keeps the height it had when added, before they wrapped)."""
        self.win.update_idletasks()
        y = 0
        for i, name in enumerate(self._side.panes()[:-1]):
            y += self._side.nametowidget(str(name)).winfo_reqheight()
            self._side.sash_place(i, 0, y)
            self.win.update_idletasks()
            y = self._side.sash_coord(i)[1] + int(self._side.cget("sashwidth"))

    def _save_layout(self):
        prefs = self._layout_prefs()
        if prefs is None:
            return
        try:
            h = max(1, self._outer.winfo_height())
            w = max(1, self._top.winfo_width())
            sh = max(1, self._side.winfo_height())
            n = len(self._side.panes())
            prefs["assembly_layout"] = {
                "track":  self._outer.sash_coord(0)[1] / float(h),
                "script": self._top.sash_coord(0)[0] / float(w),
                "panels": [self._side.sash_coord(i)[1] / float(sh)
                           for i in range(n - 1)]}
        except tk.TclError:
            return
        save = getattr(self.app, "_save_prefs", None)
        if callable(save):
            save()

    def to_step4(self):
        """Back to Step 4's cards; Ctrl+= there comes back here, like Pro
        Tools' Edit / Mix toggle."""
        self.stop()
        try:
            self.app.deiconify()
            self.app.lift()
            self.app.focus_force()
        except tk.TclError:
            pass

    def save(self):
        """Save the session, exactly as the header's SAVE (Ctrl+S) does."""
        self.app._quick_save([self.save_key.lbl])

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
        self._sync_region_range()
        self._show_default_gap()
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

        # A colour per speaker: VO and the host keep theirs; interview
        # tokens take the palette in alphabetical order.
        toks = sorted({c.res.get("token", "?") for c in clips
                       if c.kind == "int"})
        tints = {"VO": _C_VO} if any(c.kind == "vo" for c in clips) else {}
        tints.update((t, _TOKEN_COLOURS[i % len(_TOKEN_COLOURS)])
                     for i, t in enumerate(toks))
        if any(c.kind == "host" for c in clips):
            tints[HOST_NAME.upper()] = _C_HOST
        for c in clips:
            c.tint = tints["VO"] if c.kind == "vo" else (
                _C_HOST if c.kind == "host" else tints[c.res.get("token", "?")])
        if tints != self._token_tints:
            self._token_tints = tints
            if hasattr(self, "legend"):
                self._show_legend()

        self.clips   = clips
        self._by_order = {c.order: c for c in clips}
        self._starts = [c.start for c in clips]
        self.joints  = self._find_joints(clips)
        self.total_s = clips[-1].end if clips else 0.0
        names = {p.get("index"): p.get("name", "") for p in (app.parts or [])}
        # A plain (monolog) script has no parts, so nothing to flag.
        self.part_marks = [] if app._monolog else sorted(
            (sa / float(LAYOUT_SR), names.get(pi, ""))
            for pi, sa in part_starts.items())
        self.audio.set_clips(clips, self.total_s)
        self.markers = [dict(m, t=m["pos"] / float(LAYOUT_SR))
                        for m in engines.place_markers(placed, LAYOUT_SR)]
        if self.sel_marker is not None and not any(
                (m["order"], m["index"]) == self.sel_marker for m in self.markers):
            self.sel_marker = None

    @staticmethod
    def _find_joints(clips):
        """Where a monolog's cut skips over tape: every join, inside a pull
        or between two, whose two sides are not continuous in the one
        recording -- the false starts and talk taken out, or a pickup
        brought in from elsewhere in the read."""
        joints, prev = [], None          # prev: (path, source end)
        for c in clips:
            if c.kind != "vo" or not c.res.get("monolog"):
                prev = None
                continue
            for tl, du, si in c.pieces:
                if prev is not None and (prev[0] != c.play_path
                                         or abs(si - prev[1]) >= CUT_SHOW_S):
                    joints.append({"t": tl, "order": c.order, "path": c.play_path,
                                   "a": prev[1], "b": si,
                                   "same": prev[0] == c.play_path})
                prev = (c.play_path, si + du)
        return joints

    def _joint_text(self, j):
        if not j["same"]:
            return "⇄ other file"
        if j["b"] < j["a"]:
            return "↶ pickup"
        return "✂ {}".format(_fmt_tc(j["b"] - j["a"]).split(".")[0]
                                   if j["b"] - j["a"] >= 60
                                   else "{:.1f}s".format(j["b"] - j["a"]))

    def _joint_at(self, x, y):
        y0, y1 = self._track_box()
        if not (y1 <= y <= y1 + self._gap_h + 2):
            return None
        near = min(getattr(self, "joints", ()), default=None,
                   key=lambda j: abs(x - self._x(j["t"])))
        if near is None:
            return None
        half = self._font.measure(self._joint_text(near)) / 2.0 + 4
        return near if abs(x - self._x(near["t"])) <= half else None

    def _show_joint(self, j):
        """The status line for a hovered cut: how much tape went and what
        was said on it."""
        c = self._clip(j["order"])
        if not j["same"]:
            self.status.config(fg=TEXT, text="Joins two recordings here  ·  "
                               "now from {}".format(os.path.basename(j["path"] or "")))
            return
        a, b = sorted((j["a"], j["b"]))
        words = self._raw_words(c) if c is not None else []
        said = " ".join(str(w.get("text", w.get("word", ""))).strip()
                        for w in words or ()
                        if isinstance(w, dict) and a <= float(w.get("start", -1)) < b)
        head = ("Pickup from later in the read ({} → {})".format(
                    secs_tc(b), secs_tc(a)) if j["b"] < j["a"] else
                "Cut here: {:.1f}s of the read ({} → {})".format(
                    b - a, secs_tc(a), secs_tc(b)))
        if j["b"] < j["a"]:
            said = ""
        self.status.config(fg=TEXT, text=head + (
            "  ·  “{}”".format(said[:220] + ("…" if len(said) > 220 else ""))
            if said else ""))

    def _queue_loads(self):
        """Fetch every pull whose loaded audio no longer covers it,
        nearest the middle of the view first."""
        self._queue_words()
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

    def _file_words(self, path, load=True):
        """A whole file's transcript: the second pass's as it comes in, else
        what is saved.  None when that is not parsed yet and `load` is False
        (the UI thread never waits on a parse)."""
        live = getattr(self.app, "_refine_words", None)
        words = live(path) if live is not None else None
        if words:
            return words
        words = self._words_memo.get(path)
        if words is None and load:
            with self._words_lock:
                words = self._words_memo.get(path)
                if words is None:
                    try:
                        words = (engines.cache_load(path)
                                 or engines.pb_transcript_load(path)[0] or [])
                    except Exception:
                        words = []
                    self._words_memo[path] = words
        return words

    def _raw_words(self, c, load=True):
        """The transcript behind a pull: its file's whole transcript when
        there is one (the text shows the tape around the pull as well), else
        the pull's own windowed words, else a Pull Quotes session's.  None
        when none of that is to hand yet and `load` is False."""
        words = self._file_words(c.play_path, load) if c.play_path else []
        if words:
            return words
        own = c.res.get("words")
        if own:
            return own
        if words is None:
            return None
        pq = (getattr(self.app, "_pq_session_data", None) or {}).get(
            c.res.get("token"))
        return (pq or {}).get("transcript") or []

    def _map_words(self, c, load=True):
        """Transcript words the pull's segments keep (_word_point), moved onto
        the timeline.  Looked up by time: a whole file's words, many pulls."""
        words = self._raw_words(c, load)
        if words is None:
            return None
        ordered, starts = _indexed(words)
        out = []
        for tl, du, si in c.pieces:
            i = bisect.bisect_left(starts, si - MAX_WORD_S)
            while i < len(ordered) and starts[i] < si + du:
                for ws, we, text, _glued, _src in _word_units(ordered[i]):
                    if si <= _word_point(ws, we) < si + du:
                        t0 = max(tl, tl + ws - si)
                        out.append((t0, max(t0, min(tl + du, tl + we - si)), text))
                i += 1
        out.sort()
        return out

    def _queue_words(self):
        """Parse each file's transcript ahead of the audio, so the text is
        there at once; the audio takes a while longer."""
        for path in {c.play_path for c in self.clips if c.play_path}:
            if path not in self._words_memo and path not in self._words_queued:
                self._words_queued.add(path)
                self._pool.submit(self._load_words, path)

    def _load_words(self, path):
        """Worker thread."""
        if self._closed:
            return
        self._file_words(path)
        self.app._ui(lambda: self._on_words_parsed(path))

    def _on_words_parsed(self, path):
        if self._closed:
            return
        self._words_queued.discard(path)
        self._remap_words(path)

    def _remap_words(self, path):
        for c in self.clips:
            if c.play_path == path:
                words = self._map_words(c, load=False)
                if words is not None:
                    c.words = words
        self._schedule_tx_render()

    def transcript_changed(self, path):
        """The second pass brought more words for this file (refine.py)."""
        if self._closed:
            return
        self._words_memo.pop(path, None)     # what is saved has changed too
        self._words_queued.discard(path)
        self._remap_words(path)
        self._queue_words()

    def show_refine_status(self):
        """The TRANSCRIPT panel, as the second pass stands."""
        if self._closed:
            return
        app = self.app
        fn = getattr(app, "_refine_status", None)
        text = fn() if fn is not None else ""
        held = getattr(app, "_refine_held", None)
        held = held() if held is not None else False
        running = getattr(app, "_refine_running", None)
        running = running() if running is not None else False
        changed = False
        try:
            self.refine_key.set_text(
                "Refine with {}".format(engines.get_active_model_size()))
            if text != self.tx_status.cget("text"):
                self.tx_status.config(text=text)
                changed = True
            if held and self.recut_key is None:
                self.recut_key = _KeyButton(self.tx_flow, "Update the cut", "",
                                            self.update_the_cut)
                self.tx_flow.add(self.recut_key.frame)
                changed = True
            elif not held and self.recut_key is not None:
                self.tx_flow.remove(self.recut_key.frame)
                self.recut_key.frame.destroy()
                self.recut_key = None
                changed = True
            if changed:
                self.tx_flow._reflow()
        except tk.TclError:
            return
        if running != self._tx_fading:
            self._schedule_tx_render()

    def _refine_again(self):
        fn = getattr(self.app, "_refine_again", None)
        if fn is not None:
            fn()

    def update_the_cut(self):
        """Take the monolog's new cut, from the refined transcript."""
        if not messagebox.askyesno(
                "Update the cut",
                "Cut the read again from the refined transcript?\n\n"
                "Pulls you have edited, marked or ignored stay as they are.  "
                "The rest take the new cut, and the undo history starts over.",
                parent=self.win):
            return
        self.app._refine_update_the_cut()

    def busy(self):
        """Mid-gesture or listening: not the moment for the cut to shift."""
        return bool(self._playing or self._drag is not None
                    or self._tdrag is not None or self._rename is not None)

    def capture_place(self):
        """Where the insertion point, the playhead and the highlight are in
        the audio, for a background change to the cut to keep them on it."""
        def at(t):
            c = self._clip_at(t)
            src = c.src_at(t) if c is not None else None
            return (c.order, src) if src is not None else None
        r = self._range
        return {"insert": (at(self.insert), self.insert),
                "playhead": (at(self.playhead), self.playhead),
                "range": None if r is None else (r[0], (at(r[1]), r[1]), (at(r[2]), r[2])),
                "text": r is not None and r == self._tx_range,
                "view": self.view0 - self.playhead}

    def restore_place(self, place):
        if place is None or self._closed:
            return

        def back(entry):
            anchor, fallback = entry
            if anchor is not None:
                c = self._clip(anchor[0])
                for tl, du, si in (c.pieces if c is not None else ()):
                    if si <= anchor[1] < si + du:
                        return tl + anchor[1] - si
            return max(0.0, min(fallback, self.total_s))
        self.insert = back(place["insert"])
        self.playhead = back(place["playhead"])
        r = place["range"]
        if r is not None and self._clip(r[0]) is not None:
            self._range = (r[0], back(r[1]), back(r[2]))
            if place["text"]:
                self._tx_range = self._range
            self._sync_region_range()
        self.view0 = self.playhead + place["view"]
        self.redraw()

    def restore_view(self, pps, view0, playhead):
        """After the window is opened afresh over a new cut."""
        if self._closed:
            return
        self.pps, self.view0 = pps, view0
        self.playhead = self.insert = max(0.0, min(playhead, self.total_s))
        self.redraw()

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
    def _build_line(self, r, c, neighbours, fade):
        body = (r.get("quote_text") or "").strip()
        if c is None:
            body += "   (not in the cut)"
            return _Line(body, [_Tok(0, len(body), None, None, None, None, False, None)])
        if _MONITOR["transcript"]:
            words = self._raw_words(c, load=False)
            if words:
                line = _transcript_line(c, words, neighbours,
                                        _MONITOR["cut_words"], fade)
                if line is not None:
                    return line
        return _script_line(body, c)

    def _line(self, order):
        """A pull's line; a script line follows its clip's words as they load."""
        line = self._tx_lines.get(order)
        if line is not None and line.script:
            c = self._clip(order)
            if c is not None and line.words is not c.words:
                line = self._tx_lines[order] = _script_line(line.body, c)
        return line

    def _text_top(self):
        """(pull, characters into it) at the top of the pane."""
        t = self.text
        try:
            idx = t.index("@0,0")
            o = next((g for g in t.tag_names(idx)
                      if g[0] == "o" and g[1:].isdigit()), None)
            if o is None:
                return None
            return o, self._chars(t.tag_ranges(o)[0], idx)
        except (tk.TclError, IndexError):
            return None

    def _chars(self, a, b):
        n = self.text.count(a, b, "chars")
        return (n[0] if isinstance(n, tuple) else n) or 0

    def _render_script(self):
        t = self.text
        y0 = t.yview()[0]
        top = self._text_top()
        keep = self._tx_capture()
        t.config(state="normal")
        t.delete("1.0", "end")
        placed = {c.order: c for c in self.clips}
        names  = {p.get("index"): p.get("name", "") for p in (self.app.parts or [])}
        marks = {}
        for m in self.markers:
            marks.setdefault(m["order"], []).append(m)
        near = {}
        for a, b in zip(self.clips, self.clips[1:]):
            if a.play_path == b.play_path:
                near.setdefault(a.order, []).append(b)
                near.setdefault(b.order, []).append(a)
        running = getattr(self.app, "_refine_running", None)
        self._tx_fading = fade = bool(running()) if running is not None else False
        self._tx_lines, self._tx_order, self._tx_seq = {}, [], {}
        cur_part = object()
        for r in sorted(self.app.results, key=lambda r: r.get("order", 0)):
            pi = r.get("part_index")
            if pi != cur_part and not self.app._monolog:
                cur_part = pi
                t.insert("end", (names.get(pi) or "Part {}".format(pi)) + "\n",
                         ("part",))
            o   = r.get("order")
            tag = "o{}".format(o)
            out = () if o in placed else ("out",)
            tok = r.get("token", "")
            t.insert("end", "#{:03d} {}   ".format(o, tok), ("tok", tag) + out)
            line = self._build_line(r, placed.get(o), near.get(o, ()), fade)
            self._tx_seq[o] = len(self._tx_order)
            self._tx_order.append(o)
            self._tx_lines[o] = line
            # b<order>: where the line starts, for finding its words.
            args = []
            for text, style in line.chunks:
                if text:
                    args += [text, (tag, "b{}".format(o)) + out
                             + ((style,) if style else ())]
            if args:
                t.insert("end", *args)
            for m in marks.get(o, ()):
                t.insert("end", "   [{}]".format(m["label"]),
                         (tag, {"sfx": "mk_sfx", "note": "mk_note"}.get(
                             m["kind"], "mk_music"),
                          "mk{}_{}".format(o, m["index"])))
            t.insert("end", "\n", (tag,) + out)
        t.config(state="disabled")
        t.yview_moveto(y0)
        if top is not None:
            try:
                t.yview("{}.first + {} chars".format(*top))
            except tk.TclError:
                pass
        self._cur_order = None
        self._lit_script = self._lit_range = None
        self._tx_restore(keep)
        self._tx_painted = None
        self._highlight_script(self._order_at(self.playhead), see=False)
        self._mark_selected_in_script()

    def _schedule_tx_render(self):
        """More words: draw them once this burst settles, and not under a
        highlight being dragged."""
        if self._closed or self._tx_render_after is not None:
            return
        self._tx_render_after = self.win.after(100, self._tx_render_now)

    def _tx_render_now(self):
        self._tx_render_after = None
        if self._closed:
            return
        if self._tdrag is not None:
            self._tx_render_after = self.win.after(300, self._tx_render_now)
            return
        if _MONITOR["transcript"]:
            self._render_script()
        self.redraw()

    # ── Highlighting the text ────────────────────────────────────────────
    def _tx_capture(self):
        """The highlight's two ends by the audio under them, to survive a
        new drawing of the text (the words may have changed)."""
        if self._tx_anchor is None:
            return None
        out = []
        for o, i in (self._tx_anchor, self._tx_head):
            line = self._tx_lines.get(o)
            if line is None or not line.tokens:
                return None
            tok = line.tokens[min(i, len(line.tokens) - 1)]
            out.append((o, tok.s0 if tok.s0 is not None else tok.t0))
        return out

    def _tx_restore(self, kept):
        if kept is None:
            self._tx_anchor = self._tx_head = None
            return
        ends = []
        for o, at in kept:
            line = self._line(o)
            if line is None or not line.tokens:
                self._tx_anchor = self._tx_head = None
                return
            if at is None:
                ends.append((o, 0))
                continue
            ends.append((o, min(
                range(len(line.tokens)),
                key=lambda n: abs((line.tokens[n].s0 if line.tokens[n].s0 is not None
                                   else (line.tokens[n].t0 or 0.0)) - at))))
        self._tx_anchor, self._tx_head = ends

    def _tx_clear(self):
        self._tx_anchor = self._tx_head = self._tx_range = None

    def _tx_selected(self):
        """[(order, clip or None, line, first word, last word)] for the
        highlighted words, down the text."""
        if self._tx_anchor is None:
            return []
        a, b = self._tx_anchor, self._tx_head
        seq = self._tx_seq
        if a[0] not in seq or b[0] not in seq:
            return []
        if (seq[a[0]], a[1]) > (seq[b[0]], b[1]):
            a, b = b, a
        out = []
        for o in self._tx_order[seq[a[0]]:seq[b[0]] + 1]:
            line = self._line(o)
            if line is None or not line.tokens:
                continue
            last = len(line.tokens) - 1
            out.append((o, self._clip(o), line,
                        min(a[1], last) if o == a[0] else 0,
                        min(b[1], last) if o == b[0] else last))
        return out

    def _word_in(self, c, line, i):
        """Where a highlight starting on this word starts on the timeline:
        halfway back to the word before (SEL_PAD at most), or the region's
        own edge when nothing is said between."""
        tok = line.tokens[i]
        k = tok.k if tok.k is not None else c.piece_at(tok.t0)
        if k is None:
            return tok.t0
        for j in range(i - 1, -1, -1):
            p = line.tokens[j]
            if p.kept:
                if p.k == k:
                    return max((p.t1 + tok.t0) / 2.0, tok.t0 - SEL_PAD)
                break
        return c.pieces[k][0]

    def _word_out(self, c, line, i):
        tok = line.tokens[i]
        k = tok.k if tok.k is not None else c.piece_at(tok.t0)
        if k is None:
            return tok.t1
        for j in range(i + 1, len(line.tokens)):
            n = line.tokens[j]
            if n.kept:
                if n.k == k:
                    return min((tok.t1 + n.t0) / 2.0, tok.t1 + SEL_PAD)
                break
        return c.pieces[k][0] + c.pieces[k][1]

    def _tx_apply(self):
        """The highlighted words become the track's selection."""
        first = last = None
        for _o, c, line, i0, i1 in self._tx_selected():
            if c is None:
                continue
            for i in range(i0, i1 + 1):
                if line.tokens[i].kept:
                    if first is None:
                        first = (c, line, i)
                    last = (c, line, i)
        self.sel_region = self.sel_marker = None
        if first is None:
            self._range = None
        else:
            self._range = (first[0].order, self._word_in(*first), self._word_out(*last))
            if first[0].order != self.sel_order:
                self.sel_order = first[0].order
                self._mark_selected_in_script()
        self._tx_range = self._range
        self.redraw()
        self._status_text_sel()

    def _status_text_sel(self):
        if self._range is not None:
            self._status_range()
            return
        n = sum(1 for _o, c, line, i0, i1 in self._tx_selected()
                for tok in line.tokens[i0:i1 + 1] if not tok.kept)
        if n:
            self.status.config(fg=TEXT, text="{} word{} out of the cut highlighted"
                               "   ·   H puts them back".format(n, "" if n == 1 else "s"))

    def _tx_sel_ends(self):
        """The highlight as ((order, first char), (order, end char)) -- the
        words picked in the text, or those under the track's selection."""
        if self._tx_anchor is not None:
            sel = self._tx_selected()
            if not sel:
                return None
            f, l = sel[0], sel[-1]
            return (f[0], f[2].tokens[f[3]].cs), (l[0], l[2].tokens[l[4]].ce)
        if self._range is None:
            return None
        a, b = sorted(self._range[1:])
        first = last = None
        for c in self._clips_in(a, b):
            line = self._line(c.order)
            for tok in (line.kept if line is not None else ()):
                if a <= (tok.t0 + tok.t1) / 2.0 <= b:
                    if first is None:
                        first = (c.order, tok.cs)
                    last = (c.order, tok.ce)
        return None if first is None else (first, last)

    def _paint_tx_sel(self):
        if self._tx_anchor is not None and self._range != self._tx_range:
            self._tx_anchor = self._tx_head = None   # the track has moved on
        key = (self._tx_anchor, self._tx_head, self._range)
        if key == self._tx_painted:
            return
        self._tx_painted = key
        t = self.text
        t.tag_remove("txsel", "1.0", "end")
        ends = self._tx_sel_ends()
        if ends is not None:
            try:
                t.tag_add("txsel", "b{}.first + {} chars".format(*ends[0]),
                          "b{}.first + {} chars".format(*ends[1]))
            except tk.TclError:
                pass

    def _tx_hit(self, x, y):
        """(order, word, on its #number) under the mouse, or None off any pull."""
        t = self.text
        idx = t.index("@{},{}".format(x, y))
        tags = t.tag_names(idx)
        o = next((int(g[1:]) for g in tags if g[0] == "o" and g[1:].isdigit()), None)
        if o is None:
            return None
        line = self._line(o)
        if line is None or not line.tokens:
            return o, None, "tok" in tags
        if "b{}".format(o) in tags:
            off = self._chars("b{}.first".format(o), idx)
        else:
            off = 0 if "tok" in tags else len(line.body)
        return o, max(0, bisect.bisect_right(line.starts, off) - 1), "tok" in tags

    def _on_text_press(self, e):
        t = self.text
        t.focus_set()
        self._end_rename(commit=True)
        self._script_touched()
        self._tdrag = None
        idx = t.index("@{},{}".format(e.x, e.y))
        for tag in t.tag_names(idx):
            if tag.startswith("mk") and "_" in tag:
                o, i = (int(v) for v in tag[2:].split("_"))
                m = self._marker((o, i))
                if m is not None:
                    self._range = None
                    self._tx_clear()
                    self.select_marker((o, i))
                    self.seek(m["t"], center=True)
                    return "break"
        hit = self._tx_hit(e.x, e.y)
        if hit is None:
            return "break"
        o, i, head = hit
        if head or i is None:
            # The pull's number: the whole pull, for the track and the keys.
            self._range = None
            self._tx_clear()
            c = self._clip(o)
            if c is not None:
                self.select(c.order)
                self.seek(c.start, center=True)
            return "break"
        if e.state & 0x0001 and self._tx_click is not None:
            self._tx_anchor, self._tx_head = self._tx_click, (o, i)
            self._tx_apply()
            self._tdrag = {"from": self._tx_click, "moved": True, "x": e.x, "y": e.y}
            return "break"
        self._tx_click = (o, i)
        self._tdrag = {"from": (o, i), "moved": False, "x": e.x, "y": e.y,
                       "x0": e.x, "y0": e.y}
        return "break"

    def _on_text_drag(self, e):
        d = self._tdrag
        if d is None:
            return "break"
        d["x"], d["y"] = e.x, e.y
        self._tx_extend()
        self._tx_autoscroll()
        return "break"

    def _tx_extend(self):
        d = self._tdrag
        if d is None:
            return
        hit = self._tx_hit(d["x"], d["y"])
        if hit is None or hit[1] is None:
            return
        at = hit[:2]
        if not d["moved"]:
            if at == d["from"] and \
                    abs(d["x"] - d["x0"]) + abs(d["y"] - d["y0"]) < DRAG_PX:
                return
            d["moved"] = True
            self._tx_anchor, self._tx_head = d["from"], None
            self._script_touched()
        if at != self._tx_head:
            self._tx_head = at
            self._tx_apply()

    def _tx_autoscroll(self):
        """Dragged off the top or bottom of the pane: it scrolls under you."""
        d = self._tdrag
        if d is None or self._tx_scroll_after is not None:
            return
        step = -1 if d["y"] < 0 else (1 if d["y"] > self.text.winfo_height() else 0)
        if step:
            self.text.yview_scroll(step, "units")
            self._script_touched()
            self._tx_extend()
            self._tx_scroll_after = self.win.after(60, self._tx_scroll_tick)

    def _tx_scroll_tick(self):
        self._tx_scroll_after = None
        self._tx_autoscroll()

    def _on_text_release(self, _e):
        d, self._tdrag = self._tdrag, None
        if self._tx_scroll_after is not None:
            try:
                self.win.after_cancel(self._tx_scroll_after)
            except tk.TclError:
                pass
            self._tx_scroll_after = None
        if d is None:
            return "break"
        if d["moved"]:
            self._tx_settle()
        else:
            self._tx_place(*d["from"])
        return "break"

    def _tx_settle(self):
        """A highlight finished: the insertion point goes to its start."""
        if self._range is not None:
            self.set_insert(min(self._range[1:]))
        self._status_text_sel()

    def _tx_place(self, o, i):
        """A click on a word: the insertion point, just ahead of it."""
        self._range = self.sel_region = None
        self._tx_clear()
        c = self._clip(o)
        if c is None:
            self.sel_order = None
            self._mark_selected_in_script()
            self.status.config(fg=SUB, text="#{:03d} is not in the cut   ·   "
                               "triple-click it and press H to bring it back".format(o))
            self.redraw()
            return
        line = self._line(o)
        j = i
        while j < len(line.tokens) and not line.tokens[j].kept:
            j += 1                       # a cut word: where the audio resumes
        t = self._word_in(c, line, j) if j < len(line.tokens) else c.end
        self.sel_order = o
        self._mark_selected_in_script()
        self.set_insert(t)
        if not self._playing and not self.view0 <= t <= self.view0 + self._span():
            self.view0 = t - self._span() * 0.1
        self.redraw()

    def _on_text_double(self, e):
        """Double-click: the word (or name a mark)."""
        self._tdrag = None
        idx = self.text.index("@{},{}".format(e.x, e.y))
        for tag in self.text.tag_names(idx):
            if tag.startswith("mk") and "_" in tag:
                o, i = (int(v) for v in tag[2:].split("_"))
                if self._marker((o, i)) is not None:
                    self._begin_rename((o, i))
                    return "break"
        hit = self._tx_hit(e.x, e.y)
        if hit is None or hit[1] is None:
            return "break"
        o, i, head = hit
        if head:
            return self._on_text_triple(e)
        self._tx_click = (o, i)
        self._tx_anchor = self._tx_head = (o, i)
        self._tx_apply()
        self._tx_settle()
        return "break"

    def _on_text_triple(self, e):
        """Triple-click: every word of the pull."""
        self._tdrag = None
        hit = self._tx_hit(e.x, e.y)
        if hit is None or hit[1] is None:
            return "break"
        o = hit[0]
        self._tx_click = (o, 0)
        self._tx_anchor = (o, 0)
        self._tx_head = (o, len(self._line(o).tokens) - 1)
        self._tx_apply()
        self._tx_settle()
        return "break"

    def _tx_in_selection(self, o, i):
        for so, _c, _line, i0, i1 in self._tx_selected():
            if so == o and i0 <= i <= i1:
                return True
        if self._tx_anchor is None and self._range is not None:
            line = self._line(o)
            tok = line.tokens[i] if line is not None else None
            if tok is not None and tok.kept:
                a, b = sorted(self._range[1:])
                return a <= (tok.t0 + tok.t1) / 2.0 <= b
        return False

    def _on_text_menu(self, e):
        """Everything that can be done to the highlighted words."""
        self.text.focus_set()
        self._end_rename(commit=True)
        hit = self._tx_hit(e.x, e.y)
        if hit is not None and hit[1] is not None and not hit[2] \
                and not self._tx_in_selection(hit[0], hit[1]):
            self._tx_click = hit[:2]
            self._tx_anchor = self._tx_head = hit[:2]
            self._tx_apply()
            self._tx_settle()
        has = self._range is not None
        can_heal = any(not tok.kept for _o, _c, line, i0, i1 in self._tx_selected()
                       for tok in line.tokens[i0:i1 + 1])
        on = lambda ok: "normal" if ok else "disabled"
        m = tk.Menu(self.text, tearoff=0, bg=SURF2, fg=TEXT,
                    activebackground=ACCENT, activeforeground=TEXT, bd=0)
        m.add_command(label="Play selection (Space)", command=self.toggle_play,
                      state=on(has))
        m.add_command(label="Delete (Del)", command=self.delete_range, state=on(has))
        m.add_command(label="Restore (H)", command=self.restore, state=on(can_heal))
        m.add_command(label="Split (B)", command=self.split)
        m.add_separator()
        m.add_command(label="Music in (I)", command=lambda: self.drop_marker("music_in"))
        m.add_command(label="Music out (O)", command=lambda: self.drop_marker("music_out"))
        m.add_command(label="Inflection (U)",
                      command=lambda: self.drop_marker("music_inflection"))
        m.add_command(label="SFX (M)", command=lambda: self.drop_marker("sfx"))
        m.add_command(label="Note (K)", command=lambda: self.drop_marker("note"))
        m.add_separator()
        m.add_command(label="Copy (Ctrl+C)", command=self.copy_words,
                      state=on(has or can_heal))
        m.add_command(label="Open in editor (Enter)", command=self.open_selected)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()
        return "break"

    def _selected_text(self):
        """The highlighted words that are in the cut, a line per pull."""
        groups = []
        if self._tx_anchor is not None:
            for _o, _c, line, i0, i1 in self._tx_selected():
                groups.append((line, [t for t in line.tokens[i0:i1 + 1] if t.kept]))
        elif self._range is not None:
            a, b = sorted(self._range[1:])
            for c in self._clips_in(a, b):
                line = self._line(c.order)
                if line is not None:
                    groups.append((line, [t for t in line.kept
                                          if a <= (t.t0 + t.t1) / 2.0 <= b]))
        out = []
        for line, toks in groups:
            text, prev = "", None
            for tok in toks:
                if prev is not None:
                    text += "" if prev.ce == tok.cs else " "
                text += line.body[tok.cs:tok.ce]
                prev = tok
            if text:
                out.append(text)
        return "\n".join(out)

    def copy_words(self):
        text = self._selected_text()
        if text:
            self.win.clipboard_clear()
            self.win.clipboard_append(text)
            self.status.config(fg=SUB, text="Copied {} word{}".format(
                len(text.split()), "" if len(text.split()) == 1 else "s"))

    def _cut_spans(self, c, line, i0, i1):
        """The source tape behind the cut words among i0..i1, one span a run:
        each end meets the kept tape it borders, else stops just short of the
        next word left out."""
        toks, out, i = line.tokens, [], i0
        while i <= i1:
            if toks[i].kept or toks[i].s0 is None:
                i += 1
                continue
            j = i
            while j < i1 and not toks[j + 1].kept:
                j += 1
            a, b = toks[i].s0, toks[j].s1
            p = toks[i - 1] if i > 0 else None
            n = toks[j + 1] if j + 1 < len(toks) else None
            if p is None:
                a -= SEL_PAD
            elif p.kept:
                a = c.segments[p.k][1]
            else:
                a = max((p.s1 + a) / 2.0, a - SEL_PAD)
            if n is None:
                b += SEL_PAD
            elif n.kept:
                b = c.segments[n.k][0]
            else:
                b = min((b + n.s0) / 2.0, b + SEL_PAD)
            b = min(b, self._file_end.get(c.play_path, float("inf")))
            if b > a:
                out.append((max(0.0, a), b))
            i = j + 1
        return out

    def restore(self):
        """Put the highlighted words that are out of the cut back into it, as
        Pro Tools' heal does; a pull IGNOREd out of the cut comes back whole."""
        edits, back, lost = {}, [], False
        for o, c, line, i0, i1 in self._tx_selected():
            if c is None:
                if any(e["res"].get("order") == o and e["skip_var"].get()
                       for e in getattr(self.app, "_rv", ())):
                    back.append(o)
                else:
                    lost = True
                continue
            spans = self._cut_spans(c, line, i0, i1)
            if spans:
                edits[o] = (_segments_with(c.segments, spans), None)
        if not edits and not back:
            self.status.config(fg=SUB, text=(
                "No audio was found for these lines: Enter opens the waveform "
                "editor to look for it." if lost else
                "Highlight words that are struck through to put them back."))
            return
        self._range = self.sel_region = None
        self._tx_clear()
        self.app._assembly_edit_many(edits, (), back)
        self.refresh()

    def toggle_transcript(self):
        _MONITOR["transcript"] = not _MONITOR["transcript"]
        self._tx_clear()
        self._show_monitor()
        self._render_script()
        self.redraw()

    def toggle_cut_words(self):
        _MONITOR["cut_words"] = not _MONITOR["cut_words"]
        self._show_monitor()
        if _MONITOR["transcript"]:
            self._render_script()
            self.redraw()

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
                self._follow_script(rng[0])

    def _script_touched(self, _e=None):
        self._touched_at = time.monotonic()
        if self._scroll_after is not None:
            try:
                self.win.after_cancel(self._scroll_after)
            except tk.TclError:
                pass
            self._scroll_after = None

    def _follow_script(self, index):
        """Keep the script on the words being said: when `index` leaves
        the middle band of the pane, glide it back to a third of the way
        down -- unless the script was scrolled by hand a moment ago.
        Costs one bbox while the word is in the band."""
        if not _MONITOR["script"] or \
                time.monotonic() - self._touched_at < SCRIPT_HOLD_S:
            return
        t = self.text
        h = t.winfo_height()
        if h < 40:
            return
        box = t.bbox(index)
        if box is not None and SCRIPT_BAND[0] * h <= box[1] <= SCRIPT_BAND[1] * h:
            return
        try:
            dy = t.count("@0,0", index, "ypixels")
        except tk.TclError:
            return
        dy = (dy[0] if isinstance(dy, tuple) else dy) or 0
        shift = int(dy - SCRIPT_AT * h)
        if self._scroll_after is not None:
            try:
                self.win.after_cancel(self._scroll_after)
            except tk.TclError:
                pass
            self._scroll_after = None
        if abs(shift) > 1.5 * h:         # a jump (a seek): no glide
            t.yview_scroll(shift, "pixels")
            return
        steps = 6

        def _step(n=steps, left=shift):
            self._scroll_after = None
            d = int(round(left / float(n)))
            if d:
                t.yview_scroll(d, "pixels")
            if n > 1 and left - d:
                self._scroll_after = self.win.after(
                    25, lambda: _step(n - 1, left - d))
        _step()

    def _gap_from_box(self, _e=None):
        try:
            value = float(self._gap_box.get())
        except (ValueError, tk.TclError):
            self._show_default_gap()
            return "break"
        self.app._assembly_set_default_gap(value)
        self.refresh()
        self._show_default_gap()
        return "break"

    def _show_default_gap(self):
        """The default gap as the app holds it (an undo can change it)."""
        try:
            if self.win.focus_get() is self._gap_box:
                return                   # being typed in
        except (KeyError, tk.TclError):
            pass
        try:
            v = "{:.2f}".format(float(self.app.gap_var.get()))
            if self._gap_box.get() != v:
                self._gap_box.delete(0, "end")
                self._gap_box.insert(0, v)
        except (tk.TclError, AttributeError, ValueError):
            pass

    def _mark_selected_in_script(self):
        t = self.text
        t.tag_remove("sel_pull", "1.0", "end")
        if self.sel_order is None:
            return
        rng = t.tag_ranges("o{}".format(self.sel_order))
        if rng:
            t.tag_add("sel_pull", rng[0], rng[-1])

    def _light_script_word(self):
        """Karaoke: light the script word being said at the playhead.  A
        lookup per tick, and two tag calls only when the word changes, so
        it keeps up at playback rate."""
        hit = None
        i = bisect.bisect_right(self._starts, self.playhead) - 1
        c = self.clips[i] if 0 <= i < len(self.clips) else None
        line = self._line(c.order) if c is not None else None
        if line is not None and line.kept:
            j = bisect.bisect_right(line.kept_t0, self.playhead) - 1
            if j >= 0:
                tok = line.kept[j]
                nxt = line.kept_t0[j + 1] if j + 1 < len(line.kept) else c.end
                if self.playhead < max(tok.t1, min(nxt, tok.t1 + WORD_HOLD_S)):
                    hit = (c.order, tok.cs, tok.ce)
        if hit == self._lit_script:
            return
        t = self.text
        if self._lit_range is not None:
            t.tag_remove("word", *self._lit_range)
        self._lit_script, self._lit_range = hit, None
        if hit is None:
            return
        try:
            a = t.index("b{}.first + {} chars".format(hit[0], hit[1]))
            b = t.index("b{}.first + {} chars".format(hit[0], hit[2]))
        except tk.TclError:              # no line for it in the script
            return
        t.tag_add("word", a, b)
        self._lit_range = (a, b)
        self._follow_script(a)

    # ── Geometry ─────────────────────────────────────────────────────────
    def _span(self):
        return max(1, self.cv.winfo_width()) / self.pps

    def _x(self, t):
        return (t - self.view0) * self.pps

    def _t(self, x):
        return self.view0 + x / self.pps

    def _clip(self, order):
        return self._by_order.get(order)

    def _clips_in(self, a, b):
        """The pulls a timeline span touches."""
        i = max(0, bisect.bisect_right(self._starts, a) - 1)
        out = []
        for c in self.clips[i:]:
            if c.start >= b:
                break
            if c.end > a:
                out.append(c)
        return out

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
        self._paint_tx_sel()
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
            cv.create_rectangle(xs, y0, xe, y0 + (5 if c.flagged else 3),
                                fill=_C_FLAG if c.flagged else col, outline="")
            for k, (tl, du, si) in enumerate(c.pieces[1:], 1):
                xc = self._x(tl)       # a cut or split inside the pull
                g = c.seg_gaps[k - 1] if k - 1 < len(c.seg_gaps) else 0.0
                if g > 0:              # a region moved later: open track
                    xg = self._x(tl - g)
                    cv.create_rectangle(xg, y0 + 1, xc, y1 - 1, fill=BG,
                                        outline="")
                    txt = "{:.2f}s".format(g)
                    if self._fits(txt, xc - xg + 20):
                        cv.create_text((xg + xc) / 2.0, y1 + self._gap_h / 2.0,
                                       text=txt, font=self._font, fill=SUB)
                cv.create_line(xc, y0 + 4, xc, y1, fill=BORDER, dash=(1, 3))
            lbl = c.label
            if c.flagged:
                lbl += "  ·  " + c.res.get("status", "")
            lx = max(xs, 0) + 6
            if self._fits(lbl, xe - lx - 4):
                cv.create_text(lx, y0 + 6, text=lbl, anchor="nw",
                               font=self._font, fill=_C_FLAG if c.flagged else SUB)
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
                    # A gap set by hand reads bright; the default, dim.
                    cv.create_text((gx0 + gx1) / 2.0, y1 + self._gap_h / 2.0,
                                   text=txt, font=self._font,
                                   fill=TEXT if "gap_after_s" in c.res else SUB)

        # Where the cut skips tape: a tick on the track, and a badge under
        # it when there is room (hover one for what was taken out).
        free_x = -1e9
        for j in getattr(self, "joints", ()):
            if not t0 <= j["t"] <= t1:
                continue
            jx = self._x(j["t"])
            cv.create_line(jx, y1 - 6, jx, y1 + 3, fill=WARN)
            txt = self._joint_text(j)
            half = self._font.measure(txt) / 2.0
            if jx - half > free_x + 6:
                cv.create_text(jx, y1 + self._gap_h / 2.0, text=txt,
                               font=self._font, fill=WARN if j is getattr(
                                   self, "_hover_joint", None) else SUB)
                free_x = jx + half

        if self._range is not None:
            _o, a, b = self._range
            cv.create_rectangle(self._x(min(a, b)), y0, self._x(max(a, b)), y1,
                                fill=TEXT, stipple="gray25", outline=TEXT)
        if self._playing:
            xi = self._x(self.insert)
            cv.create_line(xi, 0, xi, y1, fill=TEXT, dash=(3, 3), tags=("ins",))
        x = self._x(self.playhead)
        cv.create_line(x, 0, x, y1, fill=_C_PLAYHEAD, width=2, tags=("ph",))
        self.tc_lbl.config(text=_fmt_tc(self.playhead))
        self._light_word()
        self._light_script_word()
        self._draw_overview()

    def _marker_row(self, lane=0):
        """Top and bottom of a marker lane on the ruler: 0 for music and
        SFX, 1 for notes."""
        top = 2 * self._ls + 6 + lane * (self._ls + 2)
        return top, top + self._ls

    @staticmethod
    def _lane(m):
        return 1 if m["kind"] == "note" else 0

    @staticmethod
    def _mark_colour(m):
        return {"sfx": _C_SFX, "note": _C_NOTE}.get(m["kind"], _C_MUSIC)

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
        for lane in (0, 1):
            top, bot = self._marker_row(lane)
            marks = [m for m in self.markers if self._lane(m) == lane]
            for n, m in enumerate(marks):
                x = self._x(m["t"])
                if x < -300 or x > w:
                    continue
                col = self._mark_colour(m)
                on = (m["order"], m["index"]) == self.sel_marker
                cv.create_line(x, bot, x, y1, fill=col, dash=(2, 4))
                edge = dict(outline=TEXT if on else "", width=2 if on else 1)
                if m["kind"] == "music_inflection":
                    # A diamond: a music moment, not yet an in or an out.
                    mid = (top + bot) / 2.0
                    r = (bot - top) / 2.0 - 1
                    cv.create_polygon(x + 3, mid - r, x + 3 + r, mid,
                                      x + 3, mid + r, x + 3 - r, mid,
                                      fill=col, **edge)
                else:
                    cv.create_rectangle(x, top + 1, x + 6, bot - 1, fill=col,
                                        **edge)
                nxt = self._x(marks[n + 1]["t"]) if n + 1 < len(marks) \
                    else w + 400
                if self._fits(m["label"], nxt - x - 16):
                    cv.create_text(x + 10, top, text=m["label"], anchor="nw",
                                   font=self._font, fill=TEXT if on else SUB)
        if self._rename is not None:
            entry, key = self._rename
            m = next((m for m in self.markers
                      if (m["order"], m["index"]) == key), None)
            if m is not None:
                cv.create_window(self._x(m["t"]) + 10,
                                 self._marker_row(self._lane(m))[0] - 2,
                                 anchor="nw", window=entry)

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
                           fill=self._mark_colour(m))
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
        self._light_script_word()

    # ── Status ───────────────────────────────────────────────────────────
    def _update_status(self):
        m = self._marker(self.sel_marker)
        if m is not None:
            c = self._clip(m["order"])
            kind = {"music_in": "music in", "music_out": "music out",
                    "music_inflection": "music inflection",
                    "sfx": "SFX spot", "note": "note"}.get(m["kind"], "mark")
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
        reg = self.sel_region
        if reg is not None and reg[0] == c.order and reg[1] < len(c.segments):
            s, e = c.segments[reg[1]]
            sp = self._space(*reg)
            self.status.config(fg=SUB, text="   ·   ".join(p for p in (
                c.label, "region {} of {}".format(reg[1] + 1, len(c.segments)),
                "{} → {}".format(secs_tc(s), secs_tc(e)),
                "{:.1f}s".format(e - s),
                "{:.2f}s in front".format(sp["value"]) if sp else "",
                ", . nudge it   ·   B splits") if p))
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
        if self.sel_region is not None:
            self._range = None           # the pull replaces the region
        self.sel_region = None
        self.sel_marker = None
        self._mark_selected_in_script()
        self.redraw()
        self._update_status()

    def _select_region(self, order, k):
        """One region of a pull, as the grabber takes it: highlighted, so
        Delete takes it out and Space plays it, and the one , . nudge."""
        self.sel_order  = order
        self.sel_region = (order, k)
        self.sel_marker = None
        self._mark_selected_in_script()
        self._sync_region_range()

    def _sync_region_range(self):
        """Keep the selected region's highlight on it as edits move it."""
        if self.sel_region is None:
            return
        o, k = self.sel_region
        c = self._clip(o)
        if c is None or k >= len(c.pieces):
            self.sel_region = self._range = None
            return
        tl, du, _si = c.pieces[k]
        self._range = (o, tl, tl + du)

    def seek(self, t, center=False):
        """Go to t -- the playhead and the insertion point, and the
        playback too if it is running (the ruler, the script, Home)."""
        self.playhead = self.insert = max(0.0, min(t, self.total_s))
        if self.player is not None and self._playing:
            self.player.seek(self.playhead)
        span = self._span()
        if center or not (self.view0 <= self.playhead <= self.view0 + span):
            self.view0 = self.playhead - span * (0.3 if center else 0.1)
            self.redraw()
        else:
            self._move_playhead()
        self._highlight_script(self._order_at(self.playhead))
        self._update_status()

    def set_insert(self, t):
        """Place the insertion point.  While the cut plays this leaves the
        playback (and the view) alone -- it is where the next play starts,
        and where a stop returns to."""
        self.insert = max(0.0, min(t, self.total_s))
        if self._playing:
            self.redraw()
            return
        self.playhead = self.insert
        self._move_playhead()
        self._highlight_script(self._order_at(self.playhead))
        self._update_status()

    # ── Editing on the track ─────────────────────────────────────────────
    def _hit(self, x, y):
        """What a press at (x, y) would take: ("in" | "out", clip, segment)
        on an edge, ("select", clip, segment) in a region's top half,
        ("grab", clip, segment) in its bottom half, ("gap", clip, segment)
        in the silence in front of a region -- between two pulls, or
        inside one -- ("marker", mark) on a flag, ("ruler",) above the
        track, or ("none",)."""
        y0, y1 = self._track_box()
        reach = max(6, self._ls // 2)
        if y < y0:
            lane = next((ln for ln in (0, 1) if self._marker_row(ln)[0] - 2
                         <= y <= self._marker_row(ln)[1] + 1), None)
            if lane is not None:
                near = min((m for m in self.markers if self._lane(m) == lane),
                           default=None,
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
            k = c.piece_at(t)
            if k is None:
                # Silence inside the pull, where a region was moved later.
                k = next((n for n, (tl, _du, _si) in enumerate(c.pieces)
                          if tl > t), None)
                return ("gap", c, k) if k else ("none", None, None)
            # Pro Tools' Smart Tool split: selector above, grabber below.
            return ("select" if y < (y0 + y1) / 2.0 else "grab", c, k)
        if 0 <= i < len(self.clips) - 1:
            return ("gap", self.clips[i + 1], 0)
        return ("none", None, None)

    def _prev_clip(self, c):
        i = next((n for n, x in enumerate(self.clips) if x.order == c.order), 0)
        return self.clips[i - 1] if i > 0 else None

    def _on_motion(self, e):
        if self._drag is not None:
            return
        j = self._joint_at(e.x, e.y)
        if j is not getattr(self, "_hover_joint", None):
            self._hover_joint = j
            self.redraw()
            if j is not None:
                self._show_joint(j)
            else:
                self._update_status()
        kind, c, k = self._hit(e.x, e.y)
        cur = {"in": "sb_h_double_arrow", "out": "sb_h_double_arrow",
               "gap": "sb_h_double_arrow", "marker": "sb_h_double_arrow",
               "select": "xterm", "grab": "fleur",
               "ruler": "hand2"}.get(kind, "arrow")
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
        self._tx_anchor = self._tx_head = None
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
        if kind in ("in", "out"):
            self._range = None
            self._drag = {"mode": kind, "order": c.order, "k": k, "x0": e.x,
                          "segs0": list(c.segments), "gaps0": list(c.seg_gaps),
                          "moved": False}
            self.select(c.order)
            return
        if kind == "select":
            # Selector: highlight from here; Shift extends the span.
            a = min(max(t, c.start), c.end)
            if e.state & 0x0001 and self._range:
                o, ra, rb = self._range
                anchor = ra if abs(a - rb) < abs(a - ra) else rb
                self._range = (o, anchor, a)
            else:
                self._range = (c.order, a, a)
            self.sel_region = None
            self._drag = {"mode": "range", "x0": e.x, "t": a}
            if c.order != self.sel_order:
                self.sel_order = c.order
                self._mark_selected_in_script()
            self.redraw()
            self._status_range()
            return
        if kind == "grab":
            # Grabber: the region is selected; dragging moves it (and
            # everything after) by changing the silence in front of it.
            self._select_region(c.order, k)
            self._drag = {"mode": "gap", "x0": e.x, "moved": False,
                          "grabbed": (c.order, k),
                          "space": self._space(c.order, k)}
            self.redraw()
            self._status_range()
            return
        self._range = None
        self.sel_region = None
        if kind == "gap":
            self._drag = {"mode": "gap", "x0": e.x, "moved": False,
                          "space": self._space(c.order, k), "t": t}
            return
        self.set_insert(t)
        self.redraw()

    def _on_track_drag(self, e):
        d = self._drag
        if d is None:
            return
        mode = d["mode"]
        if mode == "scrub":
            self.seek(self._t(e.x))
            return
        if mode == "range":
            if self._range is None:
                return
            o, a, _b = self._range
            self._range = (o, a, min(max(self._t(e.x), 0.0), self.total_s))
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
            sp = d["space"]
            if sp is None:
                return
            d["value"] = max(0.0, sp["value"] + dt)
            self._layout(self._space_override(sp, d["value"]))
            self._sync_region_range()
        else:
            d["value"] = self._trimmed(d, dt)
            self._layout({d["order"]: {"segments": d["value"]}})
        self.redraw()
        self._status_drag(d)

    # A "space" is the silence in front of one region, which is what
    # moving that region changes: {"order", "k", "owner", "value"}, and for
    # a join inside a pull the pull's "segments" and "gaps" too.
    def _space(self, order, k):
        """The space in front of region k of a pull: the gap after the
        pull before it (k == 0), else the pull's own silence at that join.
        None in front of the first pull -- nothing there to give or take."""
        c = self._clip(order)
        if c is None or k is None or not 0 <= k < max(1, len(c.pieces)):
            return None
        if k == 0:
            prev = self._prev_clip(c)
            if prev is None:
                return None
            return {"order": order, "k": 0, "owner": prev.order,
                    "value": prev.gap}
        return {"order": order, "k": k, "owner": order,
                "value": c.seg_gaps[k - 1], "gaps": list(c.seg_gaps),
                "segments": list(c.segments)}

    def _space_override(self, sp, value):
        """The space set to `value`, as _layout overrides (a drag)."""
        if sp["k"] == 0:
            return {sp["owner"]: {"gap_after_s": value, "gap_after": True}}
        gaps = list(sp["gaps"])
        gaps[sp["k"] - 1] = value
        return {sp["owner"]: {"seg_gaps": gaps}}

    def _commit_space(self, sp, value):
        """The space set to `value` in Step 4: the gap after the pull in
        front, or the pull's own joins through its card's accept."""
        if sp["k"] == 0:
            self.app._assembly_set_gap(sp["owner"], value)
            return
        gaps = list(sp["gaps"])
        gaps[sp["k"] - 1] = value
        self.app._assembly_set_segments(sp["owner"], sp["segments"], gaps)

    def _space_name(self, sp):
        c = self._clip(sp["order"])
        if sp["k"] == 0:
            o = self._clip(sp["owner"])
            return "gap after {}".format(o.label if o else "")
        return "silence before region {} of {}".format(
            sp["k"] + 1, c.label if c else "")

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
                self._range = None           # a click: the insertion point
                self.set_insert(d["t"])
            else:
                self.set_insert(min(a, b))   # a span starts where it starts
                self._status_range()
            self.redraw()
            return
        if not d.get("moved") or "value" not in d:
            if d["mode"] == "gap" and d.get("grabbed") is not None:
                o, k = d["grabbed"]
                c = self._clip(o)
                if c is not None and k < len(c.pieces):
                    self.set_insert(c.pieces[k][0])
            elif d["mode"] == "gap" and "t" in d:
                self.set_insert(d["t"])
            return
        if d["mode"] == "marker":
            self._move_marker(d["key"], d["value"])
            return
        if d.get("grabbed") is None:
            self._range = None           # a moved region stays selected
        if d["mode"] == "gap":
            self._commit_space(d["space"], d["value"])
        else:
            segs, gaps = _tidy(d["value"], d["gaps0"])
            if segs and (segs, gaps) != _tidy(d["segs0"], d["gaps0"]):
                self.app._assembly_set_segments(d["order"], segs, gaps)
        self.refresh()
        self._park_at_edit(d)

    def _park_at_edit(self, d):
        """After an edit the insertion point sits on the new seam, so
        play (with pre-roll) runs straight into it."""
        if d["mode"] == "gap":
            sp = d["space"]
            c = self._clip(sp["order"]) if sp else None
            if c is not None and sp["k"] < len(c.pieces):
                self.set_insert(c.pieces[sp["k"]][0])
            return
        c = self._clip(d["order"])
        if c is None or not c.pieces:
            return
        tl, du, _si = c.pieces[min(d["k"], len(c.pieces) - 1)]
        self.set_insert(tl if d["mode"] == "in" else tl + du)

    def _status_drag(self, d):
        if d["mode"] == "gap":
            sp = d["space"]
            self.status.config(fg=TEXT, text="{}:  {:.3f}s   ({:+.3f}s)".format(
                self._space_name(sp), d["value"], d["value"] - sp["value"]))
            return
        seg0, seg = d["segs0"][d["k"]], d["value"][d["k"]]
        i = 0 if d["mode"] == "in" else 1
        c = self._clip(d["order"])
        self.status.config(fg=TEXT, text="{}   {} {}   ({:+.3f}s)".format(
            c.label if c else "", "in " if i == 0 else "out",
            secs_tc(seg[i]), seg[i] - seg0[i]))

    def _status_range(self):
        o, a, b = self._range
        hit = self._clips_in(*sorted((a, b)))
        if len(hit) > 1:
            self.status.config(fg=TEXT, text="{} pulls   {:.2f}s selected   ·   "
                               "Space plays it   ·   Delete takes it out   ·   "
                               "B splits at both ends".format(len(hit), abs(b - a)))
            return
        c = self._clip(o)
        whole = c is not None and abs(b - a) >= c.end - c.start - MIN_SEG_S
        reg = self.sel_region
        self.status.config(fg=TEXT, text="{}{}   {:.2f}s selected   ·   "
                           "Space plays it   ·   Delete takes it out{}{}".format(
                               c.label if c else "",
                               "  region {} of {}".format(reg[1] + 1, len(c.pieces))
                               if reg and c and len(c.pieces) > 1 else "",
                               abs(b - a),
                               " (the whole pull: IGNOREs it)" if whole else "",
                               "   ·   , . nudge it" if reg else
                               "   ·   B splits at both ends"))

    def delete_range(self):
        """Take the highlighted span out, from every pull it touches: one
        undo step, and a pull with nothing left is IGNOREd."""
        if self._range is None:
            return
        _o, a, b = self._range
        a, b = min(a, b), max(a, b)
        self._range = self.sel_region = None
        self._tx_clear()
        if b - a < MIN_SEG_S:
            self.redraw()
            return
        edits, gone = {}, []
        for c in self._clips_in(a, b):
            segs, gaps = _segments_without(c, a, b)
            if segs:
                edits[c.order] = (segs, gaps)
            else:
                gone.append(c.order)
        if edits or gone:
            self.app._assembly_edit_many(edits, gone)
        self.refresh()
        self.set_insert(a)

    def split(self):
        """Split the region under the insertion point (the playhead, while
        the cut plays) in two there -- or at both ends of a highlighted
        span, which becomes a region of its own.  The same audio, butted:
        two regions that trim and move on their own, two clips in the AAF.
        The region after the split is selected, ready to nudge."""
        r = self._range
        if r is not None and self.sel_region is None and \
                abs(r[2] - r[1]) >= MIN_SEG_S:
            points = sorted(r[1:])
        else:
            points = [self.playhead if self._playing else self.insert]
        by_clip = {}
        for t in points:
            c = self._clip_at(t)
            if c is not None:
                by_clip.setdefault(c.order, []).append(t)
        if not by_clip:
            self.status.config(fg=SUB, text="Nothing to split here: put the "
                               "insertion point inside a pull.")
            return
        edits, first = {}, None
        for order, pts in by_clip.items():
            c = self._clip(order)
            segs, gaps, low = list(c.segments), list(c.seg_gaps), None
            for t in sorted(pts, reverse=True):   # later first: indices hold
                k = c.piece_at(t)
                if k is None:
                    continue
                tl, du, _si = c.pieces[k]
                if t - tl < MIN_SEG_S or tl + du - t < MIN_SEG_S:
                    continue                       # already a region edge
                s, e = segs[k]
                cut = s + (t - tl)
                segs[k:k + 1] = [(s, cut), (cut, e)]
                gaps.insert(k, 0.0)
                low = k
            if low is not None:
                edits[order] = (segs, gaps)
                first = (order, low + 1)
        if not edits:
            c = self._clip(next(iter(by_clip)))
            self.status.config(fg=SUB, text="{}   ·   already a region edge "
                               "here".format(c.label))
            return
        self._range = None
        self._tx_clear()
        self.app._assembly_edit_many(edits)
        self.refresh()
        if len(edits) == 1:
            self._select_region(*first)
        self.redraw()
        self._update_status()

    def nudge(self, frames):
        """Move the selected region -- or the selected pull -- and
        everything after it `frames` earlier or later: the silence in
        front of it shrinks or grows.  It stops once it butts against
        what plays before it."""
        key = self.sel_region or (
            (self.sel_order, 0) if self.sel_order is not None else None)
        if key is None:
            self.status.config(fg=SUB, text="Select a region to nudge: click "
                               "the bottom half of it.")
            return
        sp = self._space(*key)
        c = self._clip(key[0])
        if sp is None:
            self.status.config(fg=SUB, text="{}   ·   the start of the cut: "
                               "nothing in front of it to move".format(
                                   c.label if c else ""))
            return
        fps = float(getattr(self.app, "_seq_fps", 24.0) or 24.0)
        value = round(max(0.0, sp["value"] + frames / fps), 4)   # as stored
        if abs(value - sp["value"]) < 1e-6:
            self.status.config(fg=SUB, text="{}   ·   already butted against "
                               "what plays before it -- trim to tighten "
                               "further".format(c.label if c else ""))
            return
        self._commit_space(sp, value)
        self.refresh()
        c = self._clip(key[0])
        if c is not None and key[1] < len(c.pieces):
            self.set_insert(c.pieces[key[1]][0])
        self.status.config(fg=TEXT, text="{}:  {:.3f}s   ({:+.3f}s)".format(
            self._space_name(sp), value, value - sp["value"]))

    def _delete_key(self):
        if self.sel_marker is not None:
            self.delete_marker()
        else:
            self.delete_range()

    def cycle_reading(self, d):
        """Swap the selected pull to the next (or previous) reading of its
        lines: a monolog keeps every delivery it found as takes_data."""
        o = self.sel_order if self.sel_order is not None             else self._order_at(self.playhead)
        c = self._clip(o)
        if c is None:
            return
        n = len(c.res.get("takes_data") or [])
        if n < 2:
            self.status.config(fg=SUB, text="{}   ·   no other reading of "
                               "these lines was found".format(c.label))
            return
        self.app._assembly_set_reading(
            o, ((c.res.get("best_take_index") or 0) + d) % n)
        self.sel_order = o
        if self.sel_region is not None:
            self.sel_region = self._range = None   # the reading's own regions
        self.refresh()

    def open_selected(self):
        self.open_card(self.sel_order if self.sel_order is not None
                       else self._order_at(self.playhead))

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
            if src is None:              # silence inside it: where audio resumes
                src = next((si for tl, _du, si in c.pieces if tl > t), None)
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
        the cut plays.  An SFX spot opens straight into naming it: Enter
        keeps the name, Esc leaves it as its count (SFX 7).  With words or
        audio highlighted it goes on the highlight: a music out at its end,
        anything else at its start."""
        at = self.playhead
        if self._range is not None and not self._playing \
                and abs(self._range[2] - self._range[1]) > 1e-3:
            a, b = sorted(self._range[1:])
            at = b if kind == "music_out" else a
        order, anchor = self._anchor_at(at)
        if order is None:
            return
        marks = self._pull_marks(order)
        marks.append(dict(anchor, kind=kind, name=""))
        self.app._assembly_set_markers({order: marks})
        self.sel_marker = (order, len(marks) - 1)
        self.refresh()
        if kind in ("sfx", "note"):
            self._begin_rename(self.sel_marker)

    def select_marker(self, key):
        self.sel_marker = key
        self.sel_order = self.sel_region = None
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
        kind, hit, _k = self._hit(e.x, e.y)
        if kind == "marker":
            self._begin_rename((hit["order"], hit["index"]))
        elif kind in ("grab", "in", "out"):
            self.open_card(hit.order)         # the grabber steps into the editor
        elif kind == "select":
            # The selector takes the whole region, as in Pro Tools.
            self._select_region(hit.order, _k)
            self.redraw()
            self._status_range()

    def open_card(self, order):
        """The pull in Step 4's waveform editor, which shows its whole
        source file -- for finding an edit outside what was pulled."""
        if order is None:
            return
        self.stop()
        self.app._assembly_open_card(order)

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

        # "break": the key is the field's alone.  Without it the same
        # Enter reaches the window's own Enter (open in the editor) once
        # the field has closed itself.
        def _done(commit):
            self._end_rename(commit=commit)
            return "break"
        entry.bind("<Return>", lambda e: _done(True))
        entry.bind("<KP_Enter>", lambda e: _done(True))
        entry.bind("<Escape>", lambda e: _done(False))
        self._rename = (entry, key)
        x = self._x(m["t"])
        if not 0 <= x <= max(1, self.cv.winfo_width()) - 220:
            self.view0 = m["t"] - self._span() * 0.3   # bring the field into view
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
        if self._playing:
            self.stop()
        else:
            self.play()

    def play(self):
        """Play from the insertion point -- or the highlighted span, and
        only that -- with the pre-roll in front of it."""
        if self.player is None:
            self.status.config(text="Playback needs the sounddevice package "
                                    "(pip install sounddevice).", fg=ERR)
            return
        end = None
        if self._range is not None and abs(self._range[2] - self._range[1]) > 1e-3:
            origin, end = sorted(self._range[1:])
        else:
            origin = self.insert
            if origin >= self.total_s - 0.05:
                origin = self.insert = 0.0
        self._play_origin = origin
        start = origin - (_MONITOR["preroll_s"] if _MONITOR["preroll_on"] else 0.0)
        self.player.set_rate(self._rate)
        if not self.player.play(start_s=max(0.0, start), end_s=end):
            self.status.config(text="Couldn't open the audio device.", fg=ERR)
            return
        self._playing = True
        self.play_key.set_text("\u275A\u275A  Stop")
        self.play_key.set_on(True)
        self._tick()

    def stop(self, key=False):
        """Stop.  With Return to start on, the playhead goes back to the
        insertion point; otherwise it stays, and pressing stop again (the
        0 key) takes it back to where play started."""
        was = self._playing
        self._playing = False
        if self.player is not None:
            self.player.stop()
        if self._tick_after is not None:
            try:
                self.win.after_cancel(self._tick_after)
            except tk.TclError:
                pass
            self._tick_after = None
        try:
            self.play_key.set_text("\u25B6  Play")
            self.play_key.set_on(False)
        except tk.TclError:
            return
        if was:
            if _MONITOR["return_on"]:
                self.playhead = self.insert
            else:
                self.insert = self.playhead
        elif key:
            self.playhead = self.insert = self._play_origin
        else:
            return
        if not (self.view0 <= self.playhead <= self.view0 + self._span()):
            self.view0 = self.playhead - self._span() * 0.1
        self.redraw()
        self._highlight_script(self._order_at(self.playhead))
        self._update_status()

    def _tick(self):
        self._tick_after = None
        if self._closed or self.player is None:
            return
        playing = self.player.is_playing()
        self.playhead = max(0.0, min(self.player.position(), self.total_s))
        span = self._span()
        # Follow: page the view along -- never while an edit is in hand.
        if _MONITOR["follow"] and self._drag is None and self._rename is None \
                and (self.playhead > self.view0 + span * 0.92
                     or self.playhead < self.view0):
            self.view0 = self.playhead - span * 0.08
            self.redraw()
        else:
            self._move_playhead()
        order = self._order_at(self.playhead)
        if order != self._cur_order:
            self._highlight_script(order, see=_MONITOR["follow"])
            self._update_status()
        if playing:
            self._tick_after = self.win.after(30, self._tick)
        else:
            self.stop()

    def toggle_follow(self):
        _MONITOR["follow"] = not _MONITOR["follow"]
        self._show_monitor()

    def toggle_script_follow(self):
        _MONITOR["script"] = not _MONITOR["script"]
        self._show_monitor()
        if _MONITOR["script"]:
            self._touched_at = 0.0
            if self._lit_range is not None:
                self._follow_script(self._lit_range[0])

    def toggle_preroll(self):
        _MONITOR["preroll_on"] = not _MONITOR["preroll_on"]
        self._show_monitor()

    def step_preroll(self, d):
        cur = _MONITOR["preroll_s"]
        i = min(range(len(_PREROLLS)), key=lambda k: abs(_PREROLLS[k] - cur))
        _MONITOR["preroll_s"] = _PREROLLS[max(0, min(len(_PREROLLS) - 1, i + d))]
        _MONITOR["preroll_on"] = True
        self._show_monitor()

    def toggle_return(self):
        _MONITOR["return_on"] = not _MONITOR["return_on"]
        self._show_monitor()

    def _show_monitor(self):
        self.preroll_key.set_text("Pre-roll  {:g} s".format(_MONITOR["preroll_s"]))
        self.preroll_key.set_on(_MONITOR["preroll_on"])
        self.return_key.set_on(_MONITOR["return_on"])
        self.follow_key.set_on(_MONITOR["follow"])
        self.script_key.set_on(_MONITOR["script"])
        self.tx_key.set_text("Transcript" if _MONITOR["transcript"] else "Script")
        self.tx_key.set_on(_MONITOR["transcript"])
        self.cut_key.set_on(_MONITOR["cut_words"])

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
        if isinstance(e.widget, (tk.Entry, tk.Spinbox)) or \
                not isinstance(e.widget, tk.Misc):
            # Typing a mark's name -- or the key that just closed the
            # field, which can still arrive here once it is gone (e.widget
            # is then only its name).  Never a shortcut.
            return "break"
        k = e.keysym
        ctrl = bool(e.state & 0x0004)
        if k == "space":
            self.toggle_play()
        elif k == "bracketleft":
            self.step_rate(-1)
        elif k == "bracketright":
            self.step_rate(1)
        elif k in ("plus", "equal", "KP_Add") and not ctrl:
            self.zoom(1.5)
        elif k in ("minus", "underscore", "KP_Subtract"):
            self.zoom(1 / 1.5)
        elif k == "Home":
            self.seek(0.0)
            self._range = self.sel_region = None
            self._tx_clear()
        elif k == "End":
            self.seek(self.total_s)
        elif k == "Right":
            self._neighbour(1)
        elif k == "Left":
            self._neighbour(-1)
        elif k in ("Up", "Down"):
            self.cycle_reading(-1 if k == "Up" else 1)
        elif k in ("Delete", "BackSpace"):
            self._delete_key()
        elif k == "Return":
            self.open_selected()
        elif k in ("0", "KP_0", "KP_Insert"):
            self.stop(key=True)
        elif not ctrl and k.lower() == "p":
            self.toggle_preroll()
        elif not ctrl and k.lower() == "n":
            self.toggle_return()
        elif not ctrl and k == "S":
            self.toggle_script_follow()
        elif not ctrl and k.lower() == "s":
            self.toggle_follow()
        elif not ctrl and k.lower() == "r":
            self.zoom(1 / 1.5)               # Pro Tools: R out, T in
        elif not ctrl and k.lower() == "t":
            self.zoom(1.5)
        elif not ctrl and k.lower() in ("i", "o", "u", "m", "k"):
            self.drop_marker({"i": "music_in", "o": "music_out",
                              "u": "music_inflection", "m": "sfx",
                              "k": "note"}[k.lower()])
        elif ctrl and k in ("equal", "plus"):
            self.to_step4()                  # Pro Tools' Edit / Mix toggle
        elif ctrl and k.lower() == "s":
            self.save()
        elif k.lower() == "b" and not ctrl or k.lower() == "e" and ctrl:
            self.split()                     # Pro Tools: B, or Ctrl+E
        elif k in ("comma", "period"):
            self.nudge(-1 if k == "comma" else 1)
        elif k in ("less", "greater"):
            self.nudge(-10 if k == "less" else 10)
        elif ctrl and k.lower() == "z":
            # Step 4's own history: edits here and on the cards share it.
            (self.app._s4_redo if e.state & 0x0001 else self.app._s4_undo)()
        elif ctrl and k.lower() == "y":
            self.app._s4_redo()
        elif ctrl and k.lower() == "c":
            self.copy_words()
        elif k.lower() == "h":
            self.restore()                   # Pro Tools' heal is Ctrl+H
        elif not ctrl and k == "W":
            self.toggle_cut_words()
        elif not ctrl and k.lower() == "w":
            self.toggle_transcript()
        elif k.lower() == "f" and not ctrl:
            self.fit()
        elif k == "Escape":
            self._range = self.sel_region = None
            self._tx_clear()
            self.sel_marker = None
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
        self._save_layout()
        self._closed = True
        self.stop()
        if self.player is not None:
            self.player.close()
        self._pool.shutdown(wait=False, cancel_futures=True)
        for aid in (self._refresh_after, self._tx_render_after,
                    self._tx_scroll_after):
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
