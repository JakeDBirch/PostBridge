"""Light up the word being heard in a Tk Text, and keep it in view.

Shared by Pull Quotes (its transcript) and the waveform editor (its script).
Each keeps its own clock and calls show() from its own playback tick; what is
shared is finding the word at a moment and putting the highlight on it.

A time index is a list of (start_s, end_s, a, b), sorted by start_s, where
a and b mark the word in the Text: Tk indices, or anything to_index turns
into them.
"""
import bisect
import re
import tkinter as tk

PLAY_BG = "#4a2a14"     # the playing word's background, in both places


def word_at(time_index, t):
    """(a, b) of the word playing at file time t, or None between words."""
    j = bisect.bisect_right(time_index, t, key=lambda it: it[0]) - 1
    if 0 <= j < len(time_index):
        ws, we, a, b = time_index[j]
        if ws <= t < we:
            return a, b
    return None


def show(tx, tag, time_index, t, to_index=None):
    """Put `tag` on the word heard at t and scroll it into view.

    see() only scrolls when the word is off screen, so someone who has
    scrolled to read elsewhere is pulled back only once the word leaves the
    part they can see.  Returns the word's (a, b), or None."""
    cur = word_at(time_index, t) if time_index else None
    try:
        tx.tag_remove(tag, "1.0", "end")
        if cur is not None:
            i0, i1 = ((to_index(cur[0]), to_index(cur[1]))
                      if to_index else cur)
            tx.tag_add(tag, i0, i1)
            tx.see(i0)
    except tk.TclError:
        pass
    return cur


def clear(tx, tag):
    if tx is None:
        return
    try:
        tx.tag_remove(tag, "1.0", "end")
    except tk.TclError:
        pass


# ── Script text that has no timings of its own ────────────────────────────

def _norm(s):
    s = (s or "").replace("’", "'").replace("‘", "'")
    return re.sub(r"[^a-z0-9']+", " ", s.lower()).split()


def align_text_to_words(text, words, span=None):
    """Time index for `text` (script, typed by someone) from `words`
    (transcript words with start/end), as (start_s, end_s, a, b) with a, b
    character offsets into text.

    Script and speech differ, so this is a diff of the two word sequences:
    a script word gets the timing of the transcript word it lines up with,
    and a word the speaker skipped or said differently gets none.  `span`
    (lo_s, hi_s) is where the clip is; a lone word matched outside it is
    dropped, since a single "the" a few seconds away is not evidence."""
    from difflib import SequenceMatcher
    s_tok = []                          # (norm, a, b)
    for m in re.finditer(r"\S+", text or ""):
        for sub in _norm(m.group()):
            s_tok.append((sub, m.start(), m.end()))
    w_tok = []                          # (norm, start_s, end_s)
    for w in words or ():
        try:
            ws = float(w.get("start", 0.0))
            we = float(w.get("end", ws))
        except (TypeError, ValueError, AttributeError):
            continue
        for sub in _norm(w.get("word", "")):
            w_tok.append((sub, ws, we))
    if not s_tok or not w_tok:
        return []
    sm = SequenceMatcher(None, [t[0] for t in s_tok], [t[0] for t in w_tok],
                         autojunk=False)
    by_span = {}
    for i, j, n in sm.get_matching_blocks():
        if n == 0:
            continue
        if n == 1 and span is not None:
            ws = w_tok[j][1]
            if not (span[0] - 1.0 <= ws <= span[1] + 1.0):
                continue
        for k in range(n):
            _, a, b = s_tok[i + k]
            _, ws, we = w_tok[j + k]
            if (a, b) in by_span:       # "well-known" is two words aloud
                ows, owe = by_span[(a, b)]
                ws, we = min(ws, ows), max(we, owe)
            by_span[(a, b)] = (ws, we)
    out = [(ws, we, a, b) for (a, b), (ws, we) in by_span.items()]
    out.sort(key=lambda it: it[0])
    return out
