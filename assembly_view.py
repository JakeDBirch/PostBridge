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
also a button with its key in the panels beside the text, which are the
window's key.

The track is also the editor.  Every edit is the same edit Step 4 makes, committed through the card's own
accept (segments, and seg_gaps -- the silence at their joins) or the
result's gap_after_s, so Step 4's undo, its saved state and the AAF all
see it.  Each of a pull's segments is a region, and its own clip in the
AAF:

  * B splits the region under the insertion point in two there (or at
    both ends of a highlighted span): the same audio, two regions;
  * drag a pull's edge, or either side of a cut or split inside it, to
    trim -- shuffle-style, so everything after it moves up or back.  An edge
    snaps to where speech starts and stops, and to the playhead, as in the
    waveform editor (the ticks on the track); Ctrl places it freely;
  * drag a region, or the gap in front of it, to change that gap -- the
    gap after the pull before it, or inside a pull, the silence at that
    join;
  * , and . nudge the selected region (or pull) earlier or later the same
    way, by the nudge value chosen in the Edit panel; / and Shift+/ nudge it
    later and earlier by the next value up, for pacing;
  * highlight part of a pull and press Delete to take it out (a whole
    pull, selected with the grabber: it is IGNOREd);
  * double-click a pull with the grabber (or Enter) to open Step 4's
    waveform editor on it, for hunting the right edit in audio outside the
    pull; double-click with the selector takes the whole region.

It is also where the cut gets spotted.  M drops a music inflection (a moment
the music should answer), X an SFX spot and N a note, at the playhead -- while
it plays, too.  Music and SFX share a lane on the ruler, notes have their own.
Each mark lives on the pull it lands on (engines.place_markers), so it rides
with that audio through any retiming, and the AAF carries them as the MUSIC,
SFX and NOTES marker tracks beside PARTS.  Drag a flag to move it, double-click
to name it; Delete, Ctrl+click or its right-click menu removes it.  An SFX spot
or a note opens for naming as it lands.  Music in and out marks from earlier
sessions still show and still export.

The text beside the controls is the cut as words, and nothing else: the
script's own wording where it was said.  Only a difference gets a mark: script
wording that wasn't said is struck through, and words said that aren't in the
script are tinted.  It edits like the track.  Highlight words and they are the
selection, on the track too, for Delete, B, the spotting keys or the right-click
menu, across as many pulls as you like (one undo step).  A highlight doesn't
hold playback in: the two are separate.  Words from the tiny first pass read
faded while the chosen model works through them (refine.py), and differences
are only noted once a better model has heard them.

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
import monolog
import playback
from config import (BG, SURF, SURF2, SURF3, BORDER, ACCENT, TEXT, SUB,
                    WARN, ERR, FB, FBT, HOST_NAME)
from gui_components import _FlatProgressBar
from utils import secs_tc

PLAY_SR     = 32000   # playback / display rate -- the whole cut is held in
                      # memory as int16 at this rate (~4 MB a minute)
LAYOUT_SR   = 48000   # sample grid layout_timeline places on, as the AAF
HANDLE_S    = 3.0     # extra source audio loaded either side of each pull,
                      # so a trim can open a pull up without a reload
ENV_HZ      = 400     # waveform envelope resolution (bins per second)
MAX_PPS     = PLAY_SR * 4.0   # closest zoom: 4 px to a sample of that audio
RULER_STEPS = (0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1,
               2, 5, 10, 15, 30, 60, 120, 300, 600)   # seconds between ticks
MIN_CUT_S   = 0.001   # shortest span Delete will take out
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
TX_PARA_CHARS = 600   # a pull with no script starts a new paragraph after this much,
TX_PARA_PAUSE_S = 1.0 # at the next sentence end, or a pause this long without one
MAX_WORD_S  = 3.0     # how far ahead of a piece a word it keeps could start
NUDGES      = (0.01, 0.1, 0.5, 1.0)   # seconds; / nudges by the next one up
LAYOUT_V    = 2       # panels' contents changed: a saved layout is fitted afresh

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
_C_STRUCK   = "#8a8a8a"   # script wording that wasn't said
_C_SAID     = "#e8c98a"   # words said that aren't in the script
_C_TXSEL    = "#35597a"   # highlighted words
_C_SNAP_TICK = "#556070"  # where an edge will snap, as in the waveform editor
_C_SNAPPED  = "#ffaa00"   # an edge that has snapped
SNAP_TICKS_PPS = 10       # no ticks zoomed out past this: they cluster too closely



# Monitoring choices, kept for the run like the playback speed.
_MONITOR = {"follow": False, "preroll_on": True, "preroll_s": 2.0,
            "return_on": True, "script": True, "nudge_s": 0.1}
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


def _fmt_tc(t, places=1):
    """Timeline position as M:SS.s (H:MM:SS.s past the hour), to `places`
    decimals of a second."""
    t = max(0.0, t)
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    sec = "{:0{w}.{p}f}".format(s, w=places + 3 if places else 2, p=places)
    if h:
        return "{:d}:{:02d}:{}".format(int(h), int(m), sec)
    return "{:d}:{}".format(int(m), sec)


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


def _silence_edges(pcm, t0):
    """Where speech starts and stops in a pull's loaded audio, in source time:
    the waveform editor's own snap points, on audio levelled as its is."""
    if not len(pcm):
        return []
    samples = pcm.astype(np.float32) / 32768.0
    peak = float(np.abs(samples).max())
    if peak > 1e-6:
        samples = samples / peak * 0.85
    return match_review.MatchReviewDialog._compute_silence_boundaries_arrays(
        samples, PLAY_SR, t0)[2]


_SENT_END_RE = re.compile(r"[.!?][\"'”’)\]]*$")
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

    def __init__(self, body, tokens, chunks=None):
        self.body = body
        self.tokens = tokens
        self.chunks = chunks if chunks is not None else [(body, None)]
        self.starts = [t.cs for t in tokens]
        self.kept = [t for t in tokens if t.kept]
        self.kept_t0 = [t.t0 for t in self.kept]


_WORD_RE = re.compile(r"\S+")


def _norm_word(w):
    return re.sub(r"[^0-9a-z]", "", w.lower())


def _kept_units(c, words):
    """The transcript words the pull's segments keep (_word_point), in the
    order they play: [(t0, t1, s0, s1, text, glued, model, region)].  Looked up
    by time: a whole file's words, many pulls."""
    ordered, starts = _indexed(words)
    out = []
    for k, (tl, du, si) in enumerate(c.pieces):
        i = bisect.bisect_left(starts, si - MAX_WORD_S)
        while i < len(ordered) and starts[i] < si + du:
            for s0, s1, text, glued, src in _word_units(ordered[i]):
                if si <= _word_point(s0, s1) < si + du:
                    t0 = max(tl, tl + s0 - si)
                    out.append((t0, max(t0, min(tl + du, tl + s1 - si)), s0, s1,
                                text, glued, src, k))
            i += 1
    out.sort(key=lambda u: u[0])
    return out


class _LineMaker(object):
    """Builds a _Line a piece at a time."""

    def __init__(self):
        self.chunks, self.tokens, self.pos = [], [], 0
        self.last = ""

    def text(self, text, style=None):
        if not text:
            return
        if self.chunks and self.chunks[-1][1] == style:
            self.chunks[-1][0] += text
        else:
            self.chunks.append([text, style])
        self.pos += len(text)
        self.last = text[-1]

    def gap(self, text, style=None):
        """What lies between two words.  A space adds nothing to a space."""
        if "\n" not in text and not text.strip() and (not self.pos or self.last.isspace()):
            return
        self.text(text, style)

    def said(self, text, unit, style=None):
        """A word in the cut, timed from a transcript word."""
        self.tokens.append(_Tok(self.pos, self.pos + len(text), unit[0], unit[1],
                                unit[2], unit[3], True, unit[7]))
        self.text(text, style)

    def struck(self, text):
        """Script wording that wasn't said: in the line, not in the cut."""
        self.tokens.append(_Tok(self.pos, self.pos + len(text), None, None, None,
                                None, False, None))
        self.text(text, "txstruck")

    def line(self):
        return _Line("".join(x[0] for x in self.chunks), self.tokens,
                     [tuple(x) for x in self.chunks])


def _plain_line(units, fade=False):
    """A pull with no script to compare: what was said, in paragraphs."""
    m, para, prev = _LineMaker(), 0, None
    for u in units:
        if prev is not None and not u[5]:
            if para >= TX_PARA_CHARS and (
                    u[2] - prev[3] >= TX_PARA_PAUSE_S or _SENT_END_RE.search(prev[4])):
                m.text("\n")
                para = 0
            else:
                m.text(" ")
        m.said(u[4], u, "txdraft" if fade and u[6] == engines.DRAFT_MODEL else None)
        para += len(u[4]) + 1
        prev = u
    return m.line()


def _script_words(script):
    """[(char start, char end, match form)] for the script's words; a dash on
    its own is not one."""
    out = []
    for w in _WORD_RE.finditer(script or ""):
        n = _norm_word(w.group())
        if n:
            out.append((w.start(), w.end(), n))
    return out


def _script_ops(script, said):
    """How a pull's script lines up with what its cut says (`said`: the match
    forms of its words): difflib's opcodes, with what is the same word left
    out of the differences.  "hyper-violent" / "hyper violent" is one word to
    the eye, and "Hardin" / "Harden" is Whisper's spelling of a name, not a
    change.  Only a name gets that much slack: "killings" / "killing" and
    "rode" / "ride" are differences worth noting."""
    found = _script_words(script)
    words = [w[2] for w in found]
    said = list(said)
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, words, said, autojunk=False).get_opcodes():
        if tag != "replace":
            out.append((tag, i1, i2, j1, j2))
        elif "".join(words[i1:i2]) == "".join(said[j1:j2]):
            out.append(("equal", i1, i2, j1, j2))
        elif i2 - i1 == j2 - j1:
            for d in range(i2 - i1):
                a, b = words[i1 + d], said[j1 + d]
                same = (script[found[i1 + d][0]].isupper() and len(a) >= 4
                        and monolog._key(a) == monolog._key(b))
                out.append(("equal" if same else "replace",
                            i1 + d, i1 + d + 1, j1 + d, j1 + d + 1))
        else:
            out.append((tag, i1, i2, j1, j2))
    return out


def _text_line(script, units, ops, fade=False):
    """The pull as it reads in the text: the script's words where they were
    said, timed from the transcript, so it reads as written.  Script wording
    that wasn't said is struck through; words said that aren't in the script
    are tinted.  In what only the draft has heard (fade), a difference is
    likely the draft's own mishearing, so what it heard just reads faded."""
    words = _script_words(script)
    m = _LineMaker()
    cursor = 0

    def faded(u):
        return "txdraft" if fade and u[6] == engines.DRAFT_MODEL else None

    def said(u, style):
        if m.pos and not m.last.isspace() and not u[5]:
            m.text(" ", style if m.chunks[-1][1] == style else None)
        m.said(u[4], u, style)

    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            n, k = i2 - i1, j2 - j1
            for d in range(n):
                a, b = words[i1 + d][:2]
                m.gap(script[cursor:a])
                first = units[j1 + d * k // n]
                last = units[j1 + ((d + 1) * k - 1) // n]
                m.said(script[a:b],
                       (first[0], last[1], first[2], last[3], "", False, None, first[7]),
                       faded(first))
                cursor = b
            continue
        near = units[max(0, j1 - 1):j2 + 1]
        if fade and any(u[6] == engines.DRAFT_MODEL for u in near):
            if i2 > i1:
                m.gap(script[cursor:words[i1][0]])
                cursor = words[i2 - 1][1]
            for u in units[j1:j2]:
                said(u, "txdraft")
            continue
        for i in range(i1, i2):
            a, b = words[i][:2]
            between = script[cursor:a]
            m.gap(between, "txstruck" if i > i1 and "\n" not in between else None)
            m.struck(script[a:b])
            cursor = b
        for u in units[j1:j2]:
            said(u, "txsaid")
    m.gap(script[cursor:])
    return m.line()


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
        self._sil       = {}             # order -> its audio's snap points
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
        self._align_memo = {}            # order -> (script, said, _script_ops)
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

        saved = (self._layout_prefs() or {}).get("assembly_nudge")
        if saved in NUDGES:
            _MONITOR["nudge_s"] = saved

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
        self.show_refine_status()
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
        self.text.tag_config("cur", background=SURF3)
        self.text.tag_config("sel_pull", background=SURF2)
        self.text.tag_config("txdraft", foreground=SUB)
        self.text.tag_config("txstruck", foreground=_C_STRUCK, overstrike=True)
        self.text.tag_config("txsaid", foreground=_C_SAID)
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
        self.cv.bind("<Button-3>", self._on_track_menu)
        self.cv.bind("<MouseWheel>", self._on_wheel)
        self.ov.bind("<Button-1>", self._on_overview)
        self.ov.bind("<B1-Motion>", self._on_overview)
        # Every key is handled (or swallowed) here: Step 4 binds x and
        # Ctrl+Z with bind_all, and those must not fire against a card
        # nobody is looking at while this window has focus.
        self.win.bind("<Key>", self._on_key)
        # Pro Tools' Zoom to Session.
        for seq in ("<Alt-a>", "<Alt-A>"):
            self.win.bind(seq, lambda e: (self.fit(), "break")[1])

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
        key(p, "|◂  Return to zero", "Home", self.return_to_zero)
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
        self.return_key = key(p, "Return to start on stop", "Shift+N",
                              self.toggle_return, toggle=True)
        self.follow_key = key(p, "Follow playhead", "S", self.toggle_follow,
                              toggle=True)
        self.script_key = key(p, "Script follows words", "Shift+S",
                              self.toggle_script_follow, toggle=True)

        p = self._panel("EDIT")
        self.edit_flow = p
        self.reading_key = None
        key(p, "Delete selection", "Del", self._delete_key)
        key(p, "Split", "B", self.split)
        key(p, "Nudge earlier", ",", lambda: self.nudge(-1))
        key(p, "Nudge later", ".", lambda: self.nudge(1))
        key(p, "Big nudge earlier", "Shift+/", lambda: self.nudge(-1, big=True))
        key(p, "Big nudge later", "/", lambda: self.nudge(1, big=True))
        amount = tk.Frame(p, bg=SURF)
        tk.Label(amount, text="Nudge by", font=(FB[0], 10), bg=SURF,
                 fg=TEXT).pack(side="left", padx=(0, 4))
        self.nudge_keys = {}
        for v in NUDGES:
            b = _KeyButton(amount, "{:g} s".format(v) if v >= 1 else
                           "{:g} ms".format(v * 1000), "",
                           lambda v=v: self.set_nudge(v), toggle=True)
            b.pack(side="left", padx=(0, 2))
            self.nudge_keys[v] = b
        p.add(amount)
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
        self._show_other_reading()

        p = self._panel("TRANSCRIPT")
        self.tx_flow = p
        picker = getattr(self.app, "_build_model_picker", None)
        if picker is not None:
            p.add(picker(p, bg=SURF))
        self.refine_key = key(p, "", "", self._refine_again)
        self.recut_key = None
        # On the panel's title row: it still shows in a panel squeezed thin,
        # and the panel doesn't change height when a pass starts.
        title = p.master.winfo_children()[0]
        x = tkfont.Font(font=title.cget("font")).measure("TRANSCRIPT") + 20
        self.refine_bar = _FlatProgressBar(p.master, height=6, bg=SURF)
        self.refine_bar.place(in_=title, x=x, rely=0.5, anchor="w",
                              relwidth=1.0, width=-(x + 8), height=6)
        self.tx_status = tk.Label(p, font=(FB[0], 10), bg=SURF, fg=SUB,
                                  justify="left", anchor="w")
        p.add(self.tx_status, fill=True)

        p = self._panel("SPOT")
        key(p, "Music inflection", "M", lambda: self.drop_marker("music_inflection"))
        key(p, "SFX", "X", lambda: self.drop_marker("sfx"))
        key(p, "Note", "N", lambda: self.drop_marker("note"))

        p = self._panel("VIEW")
        key(p, "Zoom out", "R  −", lambda: self.zoom(1 / 1.5))
        key(p, "Zoom in", "T  +", lambda: self.zoom(1.5))
        key(p, "Zoom out full", "Alt+A  F", self.fit)
        key(p, "Prev pull", "←", lambda: self._neighbour(-1))
        key(p, "Next pull", "→", lambda: self._neighbour(1))

        p = self._panel("MOUSE  ·  SPEAKERS")
        p.add(tk.Label(p, font=(FB[0], 10), bg=SURF, fg=TEXT, justify="left",
                       anchor="w", text=(
            "Top half of a region: select (click = insertion point, drag = "
            "highlight).  Bottom half: grab (click = region, drag = move, "
            "double-click = waveform editor).  Edge: trim.  Gap: drag to "
            "retime.  Flag: drag, double-click to name, Ctrl+click or "
            "right-click to delete.  "
            "Edges and highlights snap to speech starts and stops; hold Ctrl "
            "to place one freely.  Ctrl+wheel: zoom.  In the text: drag to "
            "highlight, double-click "
            "a word, triple-click its pull, right-click for everything.")),
            fill=True)
        self.legend = p
        self._legend_items = []
        self._show_rate()
        self._show_monitor()

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
            if len(panels) != len(self._side.panes()) - 1 \
                    or lay.get("v") != LAYOUT_V:
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
                "v":      LAYOUT_V,
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
        self._env = {o: v for o, v in self._env.items() if o in self._by_order}
        self._sil = {o: v for o, v in self._sil.items() if o in self._by_order}
        if self.sel_order is not None and \
                not any(c.order == self.sel_order for c in clips):
            self.sel_order = None
        if self._range is not None and self._clip(self._range[0]) is None:
            self._range = None
        self._sync_region_range()
        self._show_default_gap()
        self._show_other_reading()
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
            sil = _silence_edges(pcm, t0)
        except Exception as exc:
            msg = str(exc).splitlines()[0] if str(exc) else repr(exc)
            self.app._ui(lambda: self._on_load_failed(c, msg))
            return
        self.app._ui(lambda: self._on_loaded(c, path, t0, pcm, env, words, sil))

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
        """The transcript words the pull keeps, on the timeline."""
        words = self._raw_words(c, load)
        if words is None:
            return None
        return [(u[0], u[1], u[4]) for u in _kept_units(c, words)]

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
        """The TRANSCRIPT panel, as the second pass stands: the button, a bar
        while it runs, and the line under the track that is always in view."""
        if self._closed:
            return
        app = self.app
        fn = getattr(app, "_refine_status", None)
        text = fn() if fn is not None else ""
        held = getattr(app, "_refine_held", None)
        held = held() if held is not None else False
        running = getattr(app, "_refine_running", None)
        running = running() if running is not None else False
        fn = getattr(app, "_refine_progress", None)
        frac = fn() if fn is not None else None
        changed = False
        try:
            label = "Refining…" if running else \
                "Refine with {}".format(engines.get_active_model_size())
            if label != self.refine_key.lbl.cget("text"):
                self.refine_key.set_text(label)
                changed = True
            self.refine_key.set_on(running)
            self.refine_bar.set(frac or 0.0 if running else 0.0, 1.0)
            self.tx_status.config(fg=TEXT if running or held else SUB)
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
            self._show_loading()
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
        kept, rest = self.app._refine_cut_counts()
        what = ("All {} pulls have been worked on, so they all stay as they "
                "are.  This only adds any lines of the script not in the cut "
                "yet.".format(kept) if not rest else
                "The {} you have edited, accepted, marked or ignored stay exactly "
                "as they are, and the other {} take the new cut.".format(kept, rest))
        if not messagebox.askyesno(
                "Update the cut",
                "Cut the read again from the refined transcript?\n\n{}\n\n"
                "The undo history starts over.".format(what),
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

    def _on_loaded(self, c, path, t0, pcm, env, words, sil=()):
        self._loading.discard(c.order)
        if self._closed:
            return
        live = next((x for x in self.clips if x.order == c.order), None)
        if live is None or live.play_path != path:
            self._show_loading()
            return
        self.audio.put(c.order, path, t0, pcm)
        self._env[c.order] = (t0, env)
        self._sil[c.order] = list(sil)
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
        fn = getattr(self.app, "_refine_short", None)
        refine = fn() if fn is not None else ""
        bits = (["Loading audio … {} pulls to go".format(n)] if n else []) + \
            ([refine] if refine else [])
        self.load_lbl.config(text="   ·   ".join(bits),
                             fg=ACCENT if refine else SUB)

    # ── Script pane ──────────────────────────────────────────────────────
    def _build_line(self, r, c, fade):
        script = (r.get("quote_text") or "").strip()
        if c is None:                    # in the script, not in the cut
            tok = _Tok(0, len(script), None, None, None, None, False, None)
            return _Line(script, [tok] if script else [], [(script, "txstruck")])
        words = self._raw_words(c, load=False)
        units = _kept_units(c, words) if words else []
        if not units:
            return _Line(script, [])     # nothing heard yet to set it against
        if not script:
            return _plain_line(units, fade)
        # Lining them up is the only dear part, and most pulls are the same
        # as last time: whichever word just came in, one pull has changed.
        said = tuple(_norm_word(u[4]) for u in units)
        memo = self._align_memo.get(c.order)
        if memo is None or memo[0] != script or memo[1] != said:
            memo = self._align_memo[c.order] = (script, said, _script_ops(script, said))
        return _text_line(script, units, memo[2], fade)

    def _line(self, order):
        return self._tx_lines.get(order)

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
        running = getattr(self.app, "_refine_running", None)
        self._tx_fading = fade = bool(running()) if running is not None else False
        self._tx_lines, self._tx_order, self._tx_seq = {}, [], {}
        solo = bool(self.app._monolog)   # one narrator: nobody to name
        cur_part, speaker = object(), None
        for r in sorted(self.app.results, key=lambda r: r.get("order", 0)):
            pi = r.get("part_index")
            if pi != cur_part and not solo:
                cur_part, speaker = pi, None
                t.insert("end", (names.get(pi) or "Part {}".format(pi)) + "\n",
                         ("part",))
            o   = r.get("order")
            tag = "o{}".format(o)
            line = self._build_line(r, placed.get(o), fade)
            self._tx_seq[o] = len(self._tx_order)
            self._tx_order.append(o)
            self._tx_lines[o] = line
            if not line.body:
                continue
            who = "VO" if r.get("is_vo") else r.get("token", "")
            if not solo and who and who != speaker:
                t.insert("end", who + "\n", ("tok", tag))
                speaker = who
            # b<order>: where the line starts, for finding its words.
            args = []
            for text, style in line.chunks:
                if text:
                    args += [text, (tag, "b{}".format(o))
                             + ((style,) if style else ())]
            t.insert("end", *args)
            t.insert("end", "\n", (tag,))
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
            i = min(i, len(line.tokens) - 1)
            near = [t for t in line.tokens[i:] + line.tokens[:i][::-1]
                    if t.s0 is not None]
            out.append((o, near[0].s0 if near else None))
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
            timed = [n for n, t in enumerate(line.tokens) if t.s0 is not None]
            ends.append((o, min(timed, key=lambda n: abs(line.tokens[n].s0 - at))
                         if timed and at is not None else 0))
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
        sel = self._tx_selected()
        if any(c is None for _o, c, _l, _a, _b in sel):
            self.status.config(fg=TEXT, text="Not in the cut   ·   "
                               "right-click to bring it back")
        elif sel:
            self.status.config(fg=TEXT, text="In the script, but not said in the "
                               "cut: there is no audio here to edit")

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
        hit = self._tx_hit(e.x, e.y)
        if hit is None:
            return "break"
        o, i, head = hit
        if head or i is None:
            # The speaker's name: the whole pull, for the track and the keys.
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
            self.sel_order = o
            self._mark_selected_in_script()
            self.status.config(fg=SUB, text=(
                "Not in the cut   ·   right-click to bring it back"
                if self._is_ignored(o) else
                "No audio was found for these lines   ·   Enter opens the "
                "waveform editor to look for it"))
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
        """Double-click: the word."""
        self._tdrag = None
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
        order = hit[0] if hit is not None else None
        if order is not None and self._clip(order) is None:
            self.sel_order = order
        has = self._range is not None
        on = lambda ok: "normal" if ok else "disabled"
        m = tk.Menu(self.text, tearoff=0, bg=SURF2, fg=TEXT,
                    activebackground=ACCENT, activeforeground=TEXT, bd=0)
        if order is not None and self._is_ignored(order):
            m.add_command(label="Bring back into the cut",
                          command=lambda: self.bring_back(order))
            m.add_separator()
        m.add_command(label="Play from here (Space)", command=self.toggle_play)
        m.add_command(label="Delete (Del)", command=self.delete_range, state=on(has))
        m.add_command(label="Split (B)", command=self.split)
        m.add_separator()
        m.add_command(label="Music inflection (M)",
                      command=lambda: self.drop_marker("music_inflection"))
        m.add_command(label="SFX (X)", command=lambda: self.drop_marker("sfx"))
        m.add_command(label="Note (N)", command=lambda: self.drop_marker("note"))
        m.add_separator()
        m.add_command(label="Copy (Ctrl+C)", command=self.copy_words, state=on(has))
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

    def _is_ignored(self, order):
        return any(e["res"].get("order") == order and e["skip_var"].get()
                   for e in getattr(self.app, "_rv", ()))

    def bring_back(self, order):
        """A pull IGNOREd out of the cut goes back in, whole."""
        self._range = self.sel_region = None
        self._tx_clear()
        self.app._assembly_edit_many({}, (), [order])
        self.refresh()

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

    def _show_other_reading(self):
        """The button is there only while some pull has another reading to
        swap to: a monolog usually has a few, and a re-cut can change that."""
        has = any(len(r.get("takes_data") or []) > 1
                  for r in self.app.results or ())
        if has and self.reading_key is None:
            self.reading_key = _KeyButton(self.edit_flow, "Other reading", "↑ ↓",
                                          lambda: self.cycle_reading(1))
            self.edit_flow.add(self.reading_key.frame)
        elif not has and self.reading_key is not None:
            self.edit_flow.remove(self.reading_key.frame)
            self.reading_key.frame.destroy()
            self.reading_key = None
            self.edit_flow._reflow()

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
        # As many decimals as the ticks are apart: 0:01.25 at 10 ms.
        def _places(s):
            return next((n for n, u in enumerate((1, 0.1, 0.01, 0.001)) if s >= u), 4)
        step = next((s for s in RULER_STEPS
                     if self._font.measure(_fmt_tc(t1, _places(s))) + 24 <= s * self.pps),
                    1200)
        n = math.floor(t0 / step)
        while n * step <= t1:
            k = n * step                 # not a running sum: it drifts at small steps
            x = self._x(k)
            cv.create_line(x, 2, x, ls + 2, fill=BORDER)
            if k >= 0:
                cv.create_text(x + 4, 2, text=_fmt_tc(k, _places(step)),
                               anchor="nw", font=self._font, fill=SUB)
            n += 1
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
                    # Bare track, top to bottom: the pull's bar and edge too.
                    cv.create_rectangle(xg, y0 - 1, xc, y1 + 1, fill=BG,
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
            self._draw_snap_ticks(c, lane_top, lane_bot, w)
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
        self.tc_lbl.config(text=self._tc_text())
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
        if self.pps >= ENV_HZ and self._draw_samples(c, a, b, mid, half, col):
            return
        # A region at a time: one shape across them all would bridge the
        # silence between them, and the peak of a join's pixel would reach
        # into the tape cut there.
        per = max(1, int(math.ceil(ENV_HZ / self.pps)))
        for tl, du, si in c.pieces:
            pa = max(a, int(math.ceil(self._x(tl))))
            pb = min(b, int(math.ceil(self._x(tl + du))))
            if pb <= pa:
                continue
            px  = np.arange(pa, pb, dtype=np.float64)
            src = si + (self.view0 + px / self.pps - tl)
            idx = np.clip(((src - e_t0) * ENV_HZ).astype(np.int64), 0, len(e) - 1)
            if per > 1:
                # Peak of every bin under each pixel, not one sample of it.
                amp = np.maximum.reduceat(e, idx)
                # reduceat runs each pixel up to the next pixel's bin; the
                # last one would run to the end of the envelope -- trim it.
                amp[-1] = e[idx[-1]:idx[-1] + per].max()
            else:
                amp = e[idx]
            amp = np.minimum(amp / max(self._env_scale, 1e-4), 1.0) * half * 0.95
            if pb - pa < 2:              # a sliver: a shape needs some width
                cv.create_line(pa, mid - amp[0] - 0.5, pa, mid + amp[0] + 0.5, fill=col)
                continue
            top = np.column_stack((px, mid - amp - 0.5)).ravel()
            bot = np.column_stack((px[::-1], (mid + amp + 0.5)[::-1])).ravel()
            cv.create_polygon(np.concatenate((top, bot)).tolist(), fill=col, outline="")

    def _draw_samples(self, c, a, b, mid, half, col):
        """Up close the envelope has no more detail to give: draw the audio
        itself, a region at a time so nothing cut shows through.  Each pixel
        spans its lowest and highest sample; with less than a sample to a
        pixel, a line runs through the samples.  False when the pull's audio
        isn't loaded yet."""
        buf = self.audio.buffer(c.order)
        if buf is None or buf[0] != c.play_path or not len(buf[2]):
            return False
        _path, t0, pcm = buf
        cv = self.cv
        per_px = PLAY_SR / self.pps
        k = half * 0.95 / (max(self._env_scale, 1e-4) * 32768.0)
        last = len(pcm) - 1
        for tl, du, si in c.pieces:
            pa = max(a, int(math.ceil(self._x(tl))))
            pb = min(b, int(math.ceil(self._x(tl + du))))
            if pb - pa < 2:
                continue
            # The sample under this region's first pixel.
            first = (si + self.view0 + pa / self.pps - tl - t0) * PLAY_SR
            if per_px >= 1.0:
                edges = (first + np.arange(pb - pa + 1) * per_px).astype(np.int64)
                edges = np.clip(edges, 0, last)
                run = pcm[edges[0]:edges[-1] + 1]
                off = edges[:-1] - edges[0]
                hi = np.maximum.reduceat(run, off) * k
                lo = np.minimum.reduceat(run, off) * k
                xs = np.arange(pa, pb, dtype=np.float64)
                top = np.column_stack((xs, np.maximum(mid - hi - 0.5, mid - half))).ravel()
                bot = np.column_stack((xs[::-1], np.minimum(mid - lo + 0.5,
                                                            mid + half)[::-1])).ravel()
                cv.create_polygon(np.concatenate((top, bot)).tolist(),
                                  fill=col, outline="")
                continue
            i0 = max(0, int(math.ceil(first)))
            i1 = min(last, int(first + (pb - pa) * per_px))
            if i1 - i0 < 1:
                continue
            idx = np.arange(i0, i1 + 1)
            xs = pa + (idx - first) / per_px
            ys = np.clip(mid - pcm[idx] * k, mid - half, mid + half)
            cv.create_line(np.column_stack((xs, ys)).ravel().tolist(), fill=col)
        return True

    def _draw_snap_ticks(self, c, top, bot, w):
        """Where an edge will snap: short ticks top and bottom of the lane, as
        the waveform editor draws them."""
        sil = self._sil.get(c.order)
        if not sil or self.pps < SNAP_TICKS_PPS:
            return
        h = self._ls
        for tl, du, si in c.pieces:
            lo, hi = bisect.bisect_left(sil, si), bisect.bisect_right(sil, si + du)
            for b in sil[lo:hi]:
                x = self._x(tl + b - si)
                if 0 <= x < w:
                    self.cv.create_line(x, top, x, top + h, fill=_C_SNAP_TICK)
                    self.cv.create_line(x, bot - h, x, bot, fill=_C_SNAP_TICK)

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

    def _tc_text(self):
        """The playhead's time, to the millisecond once zoomed in that far."""
        return _fmt_tc(self.playhead, 3 if self.pps >= ENV_HZ else 1)

    def _move_playhead(self):
        x = self._x(self.playhead)
        y1 = self._track_box()[1]
        self.cv.coords("ph", x, 0, x, y1)
        if self.total_s > 0:
            k = max(1, self.ov.winfo_width()) / self.total_s
            self.ov.coords("ph", self.playhead * k, 0, self.playhead * k,
                           self.ov.winfo_height())
        self.tc_lbl.config(text=self._tc_text())
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
                "drag to move, double-click to name, Ctrl+click or Delete to remove")))
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
            if e.state & 0x0004:             # Ctrl+click deletes it
                self.delete_marker()
                return
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
            # The insertion point pulls the edge to it -- unless it is parked on
            # this edge already (it is, after a trim), or the edge could never
            # leave.  Not the playhead: that is a moving target during playback.
            tl, du, _si = c.pieces[k]
            edge = tl if kind == "in" else tl + du
            near = abs(self.insert - edge) * self.pps <= match_review.SNAP_PLAYHEAD_PX
            self._drag = {"mode": kind, "order": c.order, "k": k, "x0": e.x,
                          "segs0": list(c.segments), "gaps0": list(c.seg_gaps),
                          "moved": False,
                          "ph": None if near else c.src_at(self.insert)}
            self.select(c.order)
            return
        if kind == "select":
            # Selector: highlight from here; Shift extends the span.
            a = min(max(t, c.start), c.end)
            if e.state & 0x0001 and self._range:
                o, ra, rb = self._range
                anchor = ra if abs(a - rb) < abs(a - ra) else rb
                self._range = (o, anchor, a)
                placed = True
            else:
                self._range = (c.order, a, a)
                placed = False
            self.sel_region = None
            self._drag = {"mode": "range", "x0": e.x, "t": a, "placed": placed}
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
            free = bool(e.state & 0x0004)
            b = min(max(self._t(e.x), 0.0), self.total_s)
            if abs(self._x(b) - self._x(a)) >= DRAG_PX and not d["placed"]:
                d["placed"] = True           # a drag, not a click: snap its start
                a = self._snap_timeline(a, free)
            self._range = (o, a, self._snap_timeline(b, free))
            self.redraw()
            self._status_range()
            return
        d["free"] = bool(e.state & 0x0004)
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
        self._show_snapped(d)
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
        """The pull's segments with one edge moved by dt seconds and snapped,
        held inside the neighbouring segments and the file."""
        segs = [list(s) for s in d["segs0"]]
        k = d["k"]
        a, b = segs[k]
        if d["mode"] == "in":
            lo = segs[k - 1][1] if k > 0 else 0.0
            segs[k][0] = max(min(self._snap_edge(d, a + dt), b - MIN_SEG_S), lo)
        else:
            c = self._clip(d["order"])
            hi = segs[k + 1][0] if k + 1 < len(segs) else \
                self._file_end.get(c.play_path if c else None, float("inf"))
            segs[k][1] = min(max(self._snap_edge(d, b + dt), a + MIN_SEG_S), hi)
        return [tuple(s) for s in segs]

    def _snap_edge(self, d, src):
        """Where a dragged edge lands, at source time src: on a speech start
        or stop, or the playhead, if it is near one -- the waveform editor's
        snap, at the same distances on screen."""
        if d.get("free"):
            d["snapped"] = False
            return src
        best = match_review.snap_to_boundary(
            src, self._sil.get(d["order"]) or (),
            match_review.SNAP_PX / self.pps, d.get("ph"),
            match_review.SNAP_PLAYHEAD_PX / self.pps)
        d["snapped"] = best != src
        return best

    def _snap_timeline(self, t, free=False):
        """A highlight's edge at timeline time t, snapped the same way."""
        c = self._clip_at(t)
        k = c.piece_at(t) if c is not None else None
        if free or k is None:
            return t
        tl, du, si = c.pieces[k]
        best = match_review.snap_to_boundary(
            si + t - tl, self._sil.get(c.order) or (),
            match_review.SNAP_PX / self.pps)
        return min(max(tl + best - si, tl), tl + du)

    def _show_snapped(self, d):
        """The edge lights while it is held by a snap, as the editor's does."""
        if not d.get("snapped"):
            return
        c = self._clip(d["order"])
        if c is None or d["k"] >= len(c.pieces):
            return
        tl, du, _si = c.pieces[d["k"]]
        x = self._x(tl if d["mode"] == "in" else tl + du)
        y0, y1 = self._track_box()
        self.cv.create_line(x, y0, x, y1, fill=_C_SNAPPED, width=2)

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
        self.status.config(fg=TEXT, text="{}   {} {}   ({:+.3f}s){}".format(
            c.label if c else "", "in " if i == 0 else "out",
            secs_tc(seg[i]), seg[i] - seg0[i],
            "   ·   snapped  (Ctrl places it freely)" if d.get("snapped") else ""))

    def _status_range(self):
        o, a, b = self._range
        hit = self._clips_in(*sorted((a, b)))
        if len(hit) > 1:
            self.status.config(fg=TEXT, text="{} pulls   {:.2f}s selected   ·   "
                               "Delete takes it out   ·   "
                               "B splits at both ends".format(len(hit), abs(b - a)))
            return
        c = self._clip(o)
        whole = c is not None and abs(b - a) >= c.end - c.start - MIN_SEG_S
        reg = self.sel_region
        self.status.config(fg=TEXT, text="{}{}   {:.2f}s selected   ·   "
                           "Delete takes it out{}{}".format(
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
        if b - a < MIN_CUT_S:
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
        """Split the region under the insertion point in two there -- or at
        both ends of a highlighted span, which becomes a region of its own.
        The same audio, butted: two regions that trim and move on their own,
        two clips in the AAF.  The region after the split is selected, ready
        to nudge.  Edits work from the insertion point, which stays where it
        was put while the cut plays, never from the moving playhead."""
        r = self._range
        if r is not None and self.sel_region is None and \
                abs(r[2] - r[1]) >= MIN_SEG_S:
            points = sorted(r[1:])
        else:
            points = [self.insert]
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

    def set_nudge(self, seconds):
        """The nudge value, kept for next time."""
        _MONITOR["nudge_s"] = seconds
        prefs = self._layout_prefs()
        if prefs is not None:
            prefs["assembly_nudge"] = seconds
            save = getattr(self.app, "_save_prefs", None)
            if callable(save):
                save()
        self._show_monitor()

    def _nudge_step(self, big):
        v = _MONITOR["nudge_s"]
        return NUDGES[min(NUDGES.index(v) + 1, len(NUDGES) - 1)] if big else v

    def nudge(self, direction, big=False):
        """Move the selected region -- or the selected pull -- and everything
        after it earlier (-1) or later (1), by the nudge value or with `big`
        the next one up: the silence in front of it shrinks or grows.  It
        stops once it butts against what plays before it."""
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
        step = self._nudge_step(big)
        value = round(max(0.0, sp["value"] + direction * step), 4)   # as stored
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
        o = self.sel_order if self.sel_order is not None else self._order_at(self.insert)
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
                       else self._order_at(self.insert))

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
        audio highlighted it goes at the start of the highlight."""
        at = self.playhead
        if self._range is not None and not self._playing \
                and abs(self._range[2] - self._range[1]) > 1e-3:
            at = min(self._range[1:])
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

    def _on_track_menu(self, e):
        """Right-click on a flag: what can be done to it."""
        kind, hit, _k = self._hit(e.x, e.y)
        if kind != "marker":
            return
        key = (hit["order"], hit["index"])
        self.select_marker(key)
        m = tk.Menu(self.cv, tearoff=0, bg=SURF2, fg=TEXT,
                    activebackground=ACCENT, activeforeground=TEXT, bd=0)
        m.add_command(label="Name…", command=lambda: self._begin_rename(key))
        m.add_command(label="Delete marker", command=self.delete_marker)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

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
            self.pps = min(max(self.pps * (1.25 ** notches), self._min_pps()), MAX_PPS)
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
        """Zoom on the insertion point, which holds still while the cut plays
        (the middle of the view, when it is out of sight)."""
        anchor = self.insert if self.view0 <= self.insert <= \
            self.view0 + self._span() else self.view0 + self._span() / 2.0
        frac = (anchor - self.view0) / self._span()
        self.pps = min(max(self.pps * factor, self._min_pps()), MAX_PPS)
        self.view0 = anchor - frac * self._span()
        self.redraw()

    def return_to_zero(self):
        """Back to the top of the cut, as Pro Tools' Return to Zero: the
        playhead, the insertion point, the track and the text.  Playing, it
        carries on from there."""
        self._range = self.sel_region = None
        self._tx_clear()
        self.seek(0.0)
        self.view0 = 0.0
        self._script_touched()               # stops a glide under way...
        self._touched_at = 0.0               # ...and doesn't hold the follow off
        self.text.yview_moveto(0.0)
        self.redraw()
        self._update_status()

    def fit(self):
        """Zoom out full: the whole cut across the track."""
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
        """Play from the insertion point, with the pre-roll in front of it,
        and on from there.  A highlight leaves the insertion point at its
        start, but doesn't hold playback in: the two are separate."""
        if self.player is None:
            self.status.config(text="Playback needs the sounddevice package "
                                    "(pip install sounddevice).", fg=ERR)
            return
        origin = self.insert
        if origin >= self.total_s - 0.05:
            origin = self.insert = 0.0
        self._play_origin = origin
        start = origin - (_MONITOR["preroll_s"] if _MONITOR["preroll_on"] else 0.0)
        self.player.set_rate(self._rate)
        if not self.player.play(start_s=max(0.0, start)):
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
        for v, b in self.nudge_keys.items():
            b.set_on(v == _MONITOR["nudge_s"])

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
        shift = bool(e.state & 0x0001)
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
            self.return_to_zero()
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
            if shift:
                self.toggle_return()
            else:
                self.drop_marker("note")
        elif not ctrl and k == "S":
            self.toggle_script_follow()
        elif not ctrl and k.lower() == "s":
            self.toggle_follow()
        elif not ctrl and k.lower() == "r":
            self.zoom(1 / 1.5)               # Pro Tools: R out, T in
        elif not ctrl and k.lower() == "t":
            self.zoom(1.5)
        elif not ctrl and k.lower() == "m":
            self.drop_marker("music_inflection")
        elif not ctrl and k.lower() == "x":
            self.drop_marker("sfx")
        elif ctrl and k in ("equal", "plus"):
            self.to_step4()                  # Pro Tools' Edit / Mix toggle
        elif ctrl and k.lower() == "s":
            self.save()
        elif k.lower() == "b" and not ctrl or k.lower() == "e" and ctrl:
            self.split()                     # Pro Tools: B, or Ctrl+E
        elif k in ("comma", "period"):
            self.nudge(-1 if k == "comma" else 1)
        elif k == "question" or (k == "slash" and shift):
            self.nudge(-1, big=True)
        elif k == "slash":
            self.nudge(1, big=True)
        elif ctrl and k.lower() == "z":
            # Step 4's own history: edits here and on the cards share it.
            (self.app._s4_redo if e.state & 0x0001 else self.app._s4_undo)()
        elif ctrl and k.lower() == "y":
            self.app._s4_redo()
        elif ctrl and k.lower() == "c":
            self.copy_words()
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
