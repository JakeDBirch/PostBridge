"""Monolog -- a raw single-voice read cut down to its script.

The Gunfighters shape: one narrator, one long recording (or a few), and a
finished script with no markers in it.  The raw read carries everything
the edit takes out -- false starts, retakes, lines reworded on the fly, and
talk with the producer between reads.  This module lines the whole script
up against the whole read at once and keeps, for every stretch of script,
the last time it was delivered.

  1. Words.  Both sides become plain tokens by the transcript's own rules
     (lower case, contractions expanded, hyphenated words joined, digits
     dropped), so "seventeen-year-old" meets Whisper's "seventeenyearold".
  2. Runs.  Every three words of the read that also occur in the script
     seed a run, grown along the script while the words keep agreeing; a
     single misheard, added or dropped word does not end it.
  3. The read path.  A dynamic programme over the runs, in recording
     order, keeps the chain that accounts for the most script: reading on
     is free, going back a few lines (a restart) costs a little, landing
     somewhere unrelated costs a lot -- so three words of chat that happen
     to quote the script stay out.
  4. Deliveries.  The path breaks wherever the narrator went back, jumped,
     or stopped to talk.  Taken in recording order, each delivery claims
     the script it covers from any earlier one; what an earlier delivery
     loses is cut.  Words read between matches inside a delivery -- a line
     reworded as it was read, an ad-lib -- stay in.
  5. Pulls.  Each surviving stretch of tape becomes a pull, broken at
     paragraphs, in the shape engines.reconcile_vo_part returns, so Step 4,
     the Assembly view and build_aaf take it as they take any VO.  Every
     other delivery of a pull's lines rides along as an alternative take.
"""
import re
from collections import defaultdict
from difflib import SequenceMatcher

import engines
from utils import secs_tc

# The one VO target a monolog's media pool offers: every recording dropped
# on it is searched for the whole script.
NARRATION = {"index": 0, "name": "Narration"}

SEED_N        = 3      # words a run must open with
MAX_SKIP      = 40     # script words a continuing read may pass over
MAX_BACK      = 150    # further back than this is a jump, not a restart
RESTART_COST  = 1.5
RESUME_COST   = 1.0    # back to where the read was, after a pickup
JUMP_COST     = 10.0
MAX_INSERT    = 30     # unmatched words a delivery carries between matches
MAX_PAUSE_S   = 6.0    # longer silence inside a delivery ends it
TALK_PAUSE_S  = 1.5    # ...and a shorter one among a few unmatched words
EXT_PAUSE_S   = 0.8    # a pause that ends a reworded sentence end
MIN_CLAIM     = 6      # matched words a later delivery needs to take over
                       # lines already read (fewer if it ends a sentence)
MIN_SHOW      = 0.6    # coverage under which a pull is flagged for review
ALT_COVER     = 0.6    # coverage an earlier delivery needs to be an alternative
HEAD_S        = 0.15   # lead-in before a word's start, when there is no waveform
PRE_S         = 0.05   # in this far ahead of the voice
TAIL_S        = 0.6    # out this far after it has died away
LONG_PAUSE_S  = 4.0    # a silence inside a reading longer than this...
PAUSE_SIDE_S  = 0.5    # ...is cut down to this either side of it
GUARD_S       = 0.05   # never reach this close to a word that is cut
HEAL_MAX_S    = 6.0    # a removal from a reading this short or shorter...
HEAL_MAX_WORDS = 12    # ...of this many words or fewer goes back in, unless retaken
MIN_LEFT_S    = 0.2    # a new pull's tape, clear of a kept pull's, shorter than this goes

_HYPHEN_RE = re.compile(r"(?<=[a-z])[-‐‑](?=[a-z])")
_SENT_END_RE = re.compile(r"[.!?][\"'”’)\]]*$")


def _tokens(text):
    """Match tokens for a word or a run of text, by the transcript's rules."""
    t = (text or "").lower().replace("’", "'").replace("‘", "'")
    t = _HYPHEN_RE.sub("", t)
    t = engines._normalize_for_match(t)
    return [w.strip("'") for w in re.findall(r"[a-z']+", t) if w.strip("'")]


_SAME_MEMO = {}


def _key(t):
    """A spelling-blind form of a word, for seeding: doubled letters and
    vowels after the first letter go, so "hickock" meets "hickok" and
    "harden" meets "hardin"."""
    t = t.replace("ck", "k").replace("ph", "f")
    t = re.sub(r"(.)\1+", r"\1", t)
    return t[:1] + re.sub(r"[aeiouy]", "", t[1:]) if len(t) > 3 else t


def _same(a, b):
    """Two tokens that are the same word -- or near enough that Whisper
    misspelt it ("harden" for "hardin", "enjoy" for "enjoyed")."""
    if a == b:
        return True
    if len(a) < 4 or len(b) < 4 or a[0] != b[0]:
        return False
    k = (a, b)
    v = _SAME_MEMO.get(k)
    if v is None:
        v = _key(a) == _key(b) or (
            min(len(a), len(b)) >= 5
            and SequenceMatcher(None, a, b).ratio() >= 0.8)
        _SAME_MEMO[k] = v
    return v


# ── The two sides ─────────────────────────────────────────────────────────────
class _Script(object):
    """The script as one token stream, remembering for each token its
    block (paragraph), its word in that block, and where sentences end."""

    def __init__(self, blocks):
        self.blocks = sorted(blocks, key=lambda b: b["order"])
        self.tok, self.blk, self.word = [], [], []
        self.words = []                   # per block: its words, as written
        self.sent_end = set()             # token index ending a sentence
        self.sent_start = {0}             # ... and starting one
        self.block_start = []             # first token of each block
        for bi, b in enumerate(self.blocks):
            self.block_start.append(len(self.tok))
            ws = (b.get("text") or "").split()
            self.words.append(ws)
            for wi, w in enumerate(ws):
                for t in _tokens(w):
                    self.tok.append(t)
                    self.blk.append(bi)
                    self.word.append(wi)
                if _SENT_END_RE.search(w) and self.tok:
                    self.sent_end.add(len(self.tok) - 1)
            if self.tok:
                self.sent_end.add(len(self.tok) - 1)
        self.block_start.append(len(self.tok))
        self.sent_start.update(e + 1 for e in self.sent_end)

    def sentence_of(self, j):
        """(first, last) token of the sentence holding token j."""
        a = j
        while a > 0 and a not in self.sent_start:
            a -= 1
        b = j
        while b < len(self.tok) - 1 and b not in self.sent_end:
            b += 1
        return a, b

    def text(self, a, b):
        """The script as written, tokens a..b inclusive, its paragraphs on
        lines of their own."""
        out = []
        for bi in range(self.blk[a], self.blk[b] + 1):
            w0 = self.word[a] if bi == self.blk[a] else 0
            w1 = self.word[b] + 1 if bi == self.blk[b] else len(self.words[bi])
            out.append(" ".join(self.words[bi][w0:w1]))
        return "\n".join(out)


class _Read(object):
    """Every take's words as one token stream (None between takes)."""

    def __init__(self, takes):
        self.takes = takes
        self.tok, self.take, self.wi = [], [], []
        for k, tk in enumerate(takes):
            if k:
                self.tok.append(None); self.take.append(k); self.wi.append(-1)
            for wi, w in enumerate(tk["words"]):
                for t in _tokens(w.get("word", "")):
                    self.tok.append(t); self.take.append(k); self.wi.append(wi)

    def word(self, i):
        return self.takes[self.take[i]]["words"][self.wi[i]]


# ── Runs and the read path ────────────────────────────────────────────────────
class _Run(object):
    __slots__ = ("pairs", "i0", "i1", "j0", "j1")

    def __init__(self, pairs):
        self.pairs = pairs
        self.i0, self.j0 = pairs[0]
        self.i1, self.j1 = pairs[-1]


def _grow(S, T, i, j, step):
    """Matched (read, script) pairs from (i, j) on -- forward (step 1) or
    back (step -1) -- while the words agree.  A word written as two
    ("new found" / "newfound") counts once, and one slip -- a word
    misheard, added or dropped -- is stepped over when the two after it
    agree again."""
    n, m = len(T), len(S)

    def ok(a, b):
        return 0 <= a < n and 0 <= b < m and T[a] is not None

    def eq(a, b):
        return ok(a, b) and _same(T[a], S[b])

    def joined_t(a, b):          # two read words make one script word
        a2 = a + step
        return ok(a, b) and ok(a2, b) and \
            (T[a] + T[a2] if step > 0 else T[a2] + T[a]) == S[b]

    def joined_s(a, b):          # one read word for two script words
        b2 = b + step
        return ok(a, b) and ok(a, b2) and \
            (S[b] + S[b2] if step > 0 else S[b2] + S[b]) == T[a]

    s = step
    pairs = []
    while ok(i, j):
        if eq(i, j):
            pairs.append((i, j)); i += s; j += s
        elif joined_t(i, j):
            pairs.append((i, j)); i += 2 * s; j += s
        elif joined_s(i, j):
            pairs.append((i, j)); i += s; j += 2 * s
        elif eq(i + s, j + s) and eq(i + 2 * s, j + 2 * s):
            i += s; j += s
        elif eq(i + s, j) and eq(i + 2 * s, j + s):
            i += s
        elif eq(i, j + s) and eq(i + s, j + 2 * s):
            j += s
        else:
            break
    return pairs


def _runs(S, T):
    index = defaultdict(list)
    SK = [_key(t) for t in S]
    for j in range(len(S) - SEED_N + 1):
        index[tuple(SK[j:j + SEED_N])].append(j)
    TK = [None if t is None else _key(t) for t in T]
    covered = set()
    runs = []
    for i in range(len(T) - SEED_N + 1):
        key = tuple(TK[i:i + SEED_N])
        if None in key:
            continue
        for j in index.get(key, ()):
            if (i, j) in covered:
                continue
            pairs = _grow(S, T, i - 1, j - 1, -1)[::-1] + _grow(S, T, i, j, 1)
            if len(pairs) >= SEED_N:
                covered.update(pairs)
                runs.append(_Run(pairs))
    runs.sort(key=lambda r: (r.i0, r.j0))
    return runs


def _read_path(runs):
    """The runs the read actually went through, in recording order."""
    if not runs:
        return []
    # resumes[r]: an earlier run ended near where r starts in the script,
    # so a jump forward into r is the read going back to where it was --
    # after a pickup of an earlier line, say.
    ends = defaultdict(list)
    for r in runs:
        ends[r.j1].append(r.i1)
    resumes = []
    for r in runs:
        resumes.append(any(
            r.i0 - 4000 < i1 < r.i0
            for j in range(r.j0 - 5, r.j0 + 6) for i1 in ends.get(j, ())))

    def cost(p, r, k):
        d = r.j0 - p.j1 - 1
        if 0 <= d <= 3:
            return 0.25 * d
        if 0 < d <= MAX_SKIP:
            return 1.0 + 0.1 * d
        if d < 0 and -d <= MAX_BACK:
            return RESTART_COST
        if d <= MAX_BACK and resumes[k]:
            return RESUME_COST
        return JUMP_COST

    by_end = sorted(range(len(runs)), key=lambda k: runs[k].i1)
    score = [0.0] * len(runs)
    back  = [None] * len(runs)
    avail = []                         # finished runs, in order of i1
    e = 0
    best_k, best_v = None, float("-inf")
    for k, r in enumerate(runs):
        while e < len(by_end) and runs[by_end[e]].i1 < r.i0:
            q = by_end[e]
            avail.append(q)
            if score[q] > best_v:
                best_k, best_v = q, score[q]
            e += 1
        own = float(len(r.pairs))
        sc, bk = own, None
        if best_k is not None and best_v - JUMP_COST + own > sc:
            sc, bk = best_v - JUMP_COST + own, best_k
        for q in avail[-80:]:
            v = score[q] - cost(runs[q], r, k) + own
            if v > sc:
                sc, bk = v, q
        score[k], back[k] = sc, bk
    k = max(range(len(runs)), key=lambda x: score[x])
    path = []
    while k is not None:
        path.append(runs[k])
        k = back[k]
    return path[::-1]


# ── Deliveries ────────────────────────────────────────────────────────────────
class _Delivery(object):
    """One unbroken reading of a stretch of script.  pairs are its matched
    (read, script) tokens; i_head..i_tail is its tape and cj0..cj1 the
    script it answers for, both stretched over a reworded sentence end."""

    def __init__(self, pairs, take, stumbled=False):
        self.pairs = pairs
        self.i0, self.j0 = self.pairs[0]
        self.i1, self.j1 = self.pairs[-1]
        self.i_head, self.i_tail = self.i0, self.i1
        self.cj0, self.cj1 = self.j0, self.j1
        self.take = take
        self.stumbled = stumbled   # ends where the narrator tripped and went on

    def first_at_or_after(self, j):
        for pi, pj in self.pairs:
            if pj >= j:
                return pi, pj
        return None

    def last_at_or_before(self, j):
        for pi, pj in reversed(self.pairs):
            if pj <= j:
                return pi, pj
        return None

    def matched(self, a, b):
        return sum(1 for _i, j in self.pairs if a <= j <= b)


def _pauses(R, i0, i1):
    """The longest silence between words of the read from token i0 to i1."""
    words = []
    for i in range(i0, i1 + 1):
        if R.tok[i] is not None:
            w = R.word(i)
            if not words or w is not words[-1]:
                words.append(w)
    return max([b["start"] - a["end"] for a, b in zip(words, words[1:])] or [0.0])


def _stumble(R, cur, r):
    """A trip inside a reading: among the words between the last match
    and run r, two that say again what the narrator had just said over
    the last few script words ("both these killing -- both these
    killings").  Returns (x, j): the read token where the line starts
    again and the script token it starts again at."""
    pi1, pj1 = cur[-1]
    T = R.tok
    tail = [(i, j) for i, j in cur if j >= pj1 - 3]
    for x in range(pi1 + 1, r.i0):
        if T[x] is None or T[x + 1] is None:
            continue
        for i, j in tail:
            if (T[i + 1] is not None and _same(T[x], T[i])
                    and _same(T[x + 1], T[i + 1])):
                return x, j
    return None


def _deliveries(path, St, R):
    """Break the read path into deliveries: at every restart or jump, at
    a change of recording, and wherever the words between two matches
    sound like talk -- a long silence, or more than a few words with a
    hesitation among them.  An ad-lib read straight through stays in; a
    stumble the narrator read straight on from starts a fresh delivery."""
    out, cur = [], []                 # cur: (read, script) pairs so far
    for r in path:
        if cur:
            pi1, pj1 = cur[-1]
            d = r.j0 - pj1 - 1
            gap_words = r.i0 - pi1 - 1
            pause = _pauses(R, pi1, r.i0)
            if (d < 0 or d > MAX_SKIP or gap_words > MAX_INSERT
                    or pause > MAX_PAUSE_S
                    or (gap_words >= 4 and pause > TALK_PAUSE_S)
                    or R.take[r.i0] != R.take[pi1]):
                out.append(_Delivery(cur, R.take[pi1])); cur = []
            else:
                st = _stumble(R, cur, r)
                if st is not None:
                    keep = [pq for pq in cur if pq[1] < st[1]]
                    if keep:
                        out.append(_Delivery(keep, R.take[pi1], stumbled=True))
                    cur = [st]
        cur.extend(r.pairs)
    if cur:
        out.append(_Delivery(cur, R.take[cur[-1][0]]))
    return out


def _reach(R, i, step, rest, used, n_rest):
    """From read token i, the words the narrator went on saying (step 1)
    or had just said (-1) without a pause, that no run matched.  A short
    stretch set off by a pause is taken whole; otherwise -- or when it runs
    straight into another delivery's words, so it is that delivery's
    talk -- only up to the last word that is one of the script's `rest`."""
    hit, last, n = None, None, 0
    clean = True                      # stopped at a pause or the end
    prev = R.word(i)
    i += step
    while 0 <= i < len(R.tok) and R.tok[i] is not None:
        w = R.word(i)
        if w is not prev:
            gap = w["start"] - prev["end"] if step > 0 else prev["start"] - w["end"]
            if gap > EXT_PAUSE_S:
                break
            n += 1
        if i in used or n > MAX_INSERT:
            clean = False
            break
        if any(_same(R.tok[i], t) for t in rest):
            hit = i
        last = i
        prev = w
        i += step
    if clean and last is not None and n <= n_rest + 3:
        return last
    return hit if (hit is not None and clean or step > 0) else None


def _extend(dels, S, R):
    """A delivery that stops matching before its sentence ends (or starts
    matching after it begins) was usually reworded there, not cut short:
    stretch its tape over the words it went on to say, and let it answer
    for the whole sentence."""
    used = {i for d in dels for i, _j in d.pairs}
    for d in dels:
        if d.j1 not in S.sent_end and not d.stumbled:
            _a, b = S.sentence_of(d.j1)
            hit = _reach(R, d.i1, 1, set(S.tok[d.j1 + 1:b + 1]), used, b - d.j1)
            if hit is not None:
                d.i_tail, d.cj1 = hit, b
        if d.j0 not in S.sent_start:
            a, _b = S.sentence_of(d.j0)
            hit = _reach(R, d.i0, -1, set(S.tok[a:d.j0]), used, d.j0 - a)
            if hit is not None:
                d.i_head, d.cj0 = hit, a


def _claims(dels, S):
    """owner[j]: the delivery that keeps script token j -- the last to read
    it, unless that reading was too slight to trust over an earlier one."""
    owner = [None] * len(S.tok)
    for k, d in enumerate(dels):
        strong = (len(d.pairs) >= MIN_CLAIM
                  or (len(d.pairs) >= SEED_N and d.j1 in S.sent_end))
        for j in range(d.cj0, d.cj1 + 1):
            if owner[j] is None or strong:
                owner[j] = k
    return owner


# ── Pulls ─────────────────────────────────────────────────────────────────────
class _Piece(object):
    """A stretch of tape to keep: delivery k reading script a..b (never
    across a paragraph) as read tokens i_a..i_b."""
    __slots__ = ("k", "a", "b", "i_a", "i_b", "joined", "segs")

    def __init__(self, k, a, b, i_a, i_b):
        self.k, self.a, self.b, self.i_a, self.i_b = k, a, b, i_a, i_b
        self.joined = False      # runs on in the tape from the piece before
        self.segs = []


def _pieces(dels, owner, S, R):
    out = []
    j, m = 0, len(S.tok)
    while j < m:
        k = owner[j]
        if k is None:
            j += 1
            continue
        a = j
        while j + 1 < m and owner[j + 1] == k and S.blk[j + 1] == S.blk[a]:
            j += 1
        b = j
        j += 1
        d = dels[k]
        fa, lb = d.first_at_or_after(a), d.last_at_or_before(b)
        if not fa or not lb or fa[1] > b or lb[1] < a:
            continue
        pc = _Piece(k, a, b, d.i_head if a <= d.cj0 else fa[0],
                    d.i_tail if b >= d.cj1 else lb[0])
        prev = out[-1] if out else None
        if prev is not None and prev.k == k and prev.b == a - 1 and prev.i_b < pc.i_a:
            # One reading across a paragraph break: the words between the
            # paragraphs (an ad-lib) stay, with the paragraph they follow.
            prev.i_b = pc.i_a - 1
            pc.joined = True
        out.append(pc)
    return out


def _said_again(x, nearby):
    """Are the removed tokens x, for the most part, what was said beside them?
    That is what tells a retake from a line reworded as it was read."""
    if not x or not nearby:
        return False
    sm = SequenceMatcher(None, [_key(t) for t in x],
                         [_key(t) for t in nearby if t is not None], autojunk=False)
    return sum(m.size for m in sm.get_matching_blocks()) * 2 > len(x)


def _heal(pieces, dels, R, kept=frozenset()):
    """A cut is only earned by a retake.  Where a read runs on past the script
    -- reworded, or an ad-lib -- the matcher ends one stretch and starts the
    next, and cutting between them lost words nobody said twice ("a memoir he
    [published years later in 1912.] Siringo claimed").  So tape between two
    stretches goes back when it is short, was read straight on from the words
    before it, and is not said again beside them.  The two stretches are then
    one, across a paragraph too: nothing was taken out, so nothing to mark.
    Never across `kept` lines, which another pull answers for, nor over tape
    another piece plays (a pickup of a later line): it would play twice."""
    T = R.tok
    for prev, pc in zip(pieces, pieces[1:]):
        if pc.joined or dels[prev.k].take != dels[pc.k].take \
                or pc.i_a <= prev.i_b or not kept.isdisjoint(range(prev.b + 1, pc.a)):
            continue
        if any(p.i_a < pc.i_a and p.i_b > prev.i_b
               for p in pieces if p is not prev and p is not pc):
            continue
        x = [t for t in T[prev.i_b + 1:pc.i_a] if t is not None]
        span = R.word(pc.i_a)["start"] - R.word(prev.i_b)["end"]
        retaken = (_said_again(x, T[pc.i_a:pc.i_a + len(x) + 4])
                   or _said_again(x, T[max(0, prev.i_b - len(x) - 3):prev.i_b + 1]))
        if (span <= HEAL_MAX_S and len(x) <= HEAL_MAX_WORDS and not retaken
                and _pauses(R, prev.i_b, pc.i_a - 1) <= TALK_PAUSE_S):
            prev.i_b = pc.i_a - 1
            pc.joined = True


class _Envelope(object):
    """A recording's loudness, 10 ms a frame, for placing edits where the
    voice starts and stops -- Whisper's word times run early or late by a
    few hundred milliseconds, the waveform does not."""
    HOP = 0.01

    def __init__(self, path):
        self.db = None
        try:
            import numpy as np
            # 8 kHz keeps the hiss of an "s" or "h" -- the quiet front of
            # many words -- that a lower rate would lose.
            sr = 8000
            r = engines._run(engines._ffmpeg_cmd() + [
                "-v", "quiet", "-i", path, "-ac", "1", "-ar", str(sr),
                "-f", "s16le", "pipe:1"], capture_output=True, timeout=600)
            pcm = np.frombuffer(r.stdout, dtype=np.int16)
            n = int(sr * self.HOP)
            nf = len(pcm) // n
            db = np.empty(nf, dtype=np.float32)
            step = 6000                       # frames per slice (a minute)
            for f0 in range(0, nf, step):
                fr = pcm[f0 * n:min(nf, f0 + step) * n].reshape(-1, n) / 32768.0
                db[f0:f0 + len(fr)] = 20.0 * np.log10(
                    np.sqrt((fr ** 2).mean(1)) + 1e-9)
            if len(db):
                self.db = db
                # Loud = well clear of the room: 20 dB over the quietest tenth.
                self.thr = float(np.percentile(db, 10)) + 20.0
        except Exception:
            self.db = None

    def loud(self, k):
        return 0 <= k < len(self.db) and self.db[k] > self.thr

    def onset(self, t, lo):
        """Where the voice starts for a word Whisper puts at t: the first
        sustained loud frame from a little before t, never before lo."""
        h = self.HOP
        k0 = max(int(lo / h), int((t - 0.4) / h))
        for k in range(k0, int((t + 0.5) / h)):
            if self.loud(k) and self.loud(k + 1):
                # Back over a soft start -- a quiet consonant, or a quiet
                # little word ahead of the loud one -- but not much before
                # Whisper heard the word begin, so a breath stays out.
                soft = self.thr - 12.0
                stop = max(k0, int((t - 0.1) / h))
                j, quiet = k, 0
                x = k - 1
                while x >= stop and quiet <= 8:
                    if self.db[x] > soft:
                        j, quiet = x, 0
                    else:
                        quiet += 1
                    x -= 1
                return j * h
        return None

    def offset(self, t, hi):
        """Where the voice dies away after a word Whisper ends at t: the
        end of the loud stretch running on from t (over words the
        transcript has none for -- digits), never past hi."""
        h = self.HOP
        k, last, quiet = int((t - 0.1) / h), None, 0
        while k * h <= hi and k < len(self.db):
            if self.db[k] > self.thr:
                last, quiet = k, 0
            else:
                quiet += 1
                if quiet >= 25:
                    break
            k += 1
        return None if last is None else (last + 1) * h

    def next_sound(self, t, hi):
        h = self.HOP
        for k in range(int(t / h), min(int(hi / h), len(self.db) - 1)):
            if self.loud(k) and self.loud(k + 1):
                return k * h
        return hi

    def last_sound(self, t, lo):
        h = self.HOP
        for k in range(int(t / h), max(int(lo / h), 0), -1):
            if self.loud(k) and self.loud(k - 1):
                return (k + 1) * h
        return lo


_ENV_MEMO = {}


def _envelope(path):
    if not path:
        return None
    key = (path, engines._safe_getmtime(path))
    if key not in _ENV_MEMO:
        if len(_ENV_MEMO) >= 8:           # an episode's takes, not every file
            _ENV_MEMO.clear()
        _ENV_MEMO[key] = _Envelope(path)
    return _ENV_MEMO[key]


def _cut_in(words, w, env, pre):
    """Source time to come in on word index w, `pre` ahead of the voice."""
    t = words[w]["start"]
    lo = words[w - 1]["end"] + GUARD_S if w > 0 else 0.0
    if env is not None and env.db is not None:
        on = env.onset(t, lo)
        if on is not None:
            lo = max(lo, env.last_sound(on - 0.02, lo) + GUARD_S)
            return max(lo, on - pre)
    return max(lo, t - HEAD_S)


def _cut_out(words, w, env, tail):
    """Source time to go out after word index w, `tail` after the voice."""
    t = words[w]["end"]
    hi = words[w + 1]["end"] if w + 1 < len(words) else t + 5.0
    if env is not None and env.db is not None:
        off = env.offset(t, hi)
        if off is not None:
            hi = min(hi, env.next_sound(off, hi) - GUARD_S)
            return max(t, min(off + tail, hi))
    if w + 1 < len(words):
        hi = words[w + 1]["start"] - GUARD_S
    return max(t, min(t + tail, hi))


def _edges(R, i_a, i_b, tk):
    """Source segments for read tokens i_a..i_b: in just ahead of the
    voice, out after it has died away, and any silence over LONG_PAUSE_S
    inside taken down to about a second."""
    t = tk[R.take[i_a]]
    words, env = t["words"], t.get("env")
    wa, wb = R.wi[i_a], R.wi[i_b]
    segs = [[_cut_in(words, wa, env, PRE_S), None]]
    for w in range(wa, wb):
        if words[w + 1]["start"] - words[w]["end"] > LONG_PAUSE_S:
            out = _cut_out(words, w, env, PAUSE_SIDE_S)
            back = _cut_in(words, w + 1, env, PAUSE_SIDE_S)
            if back > out:
                segs[-1][1] = out
                segs.append([back, None])
    segs[-1][1] = _cut_out(words, wb, env, TAIL_S)
    return [s for s in segs if s[1] > s[0]]


def _join(segs):
    out = []
    for a, b in segs:
        if out and a <= out[-1][1] + 1e-3:
            out[-1][1] = max(out[-1][1], b)
        elif b > a:
            out.append([a, b])
    return out


def reconcile(vo_blocks, takes, keep=()):
    """Cut the raw read down to the script.

    vo_blocks : the plain script's paragraphs (parsers.parse_plain_script).
    takes     : as for engines.reconcile_vo_part -- (words, v_offset,
                vpath, apath[, blobs]) per recording, in recording order.
    keep      : script token spans (first, last) that pulls already in the
                cut answer for.  Those lines are left alone: no tape is cut
                for them and they aren't reported missing.

    Returns VO results, one per kept stretch of tape (a paragraph read in
    one go is one pull) plus a no_match row for any part of the script that
    was never read, numbered 1.. in script order.
    """
    tk = []
    for entry in takes:
        words, v_offset, vpath, apath = entry[:4]
        tk.append({"words": [w for w in (words or []) if not w.get("break")],
                   "v_offset": v_offset, "vpath": vpath, "apath": apath,
                   "env": _envelope(apath)})
    S = _Script(vo_blocks)
    R = _Read(tk)
    dels = _deliveries(_read_path(_runs(S.tok, R.tok)), S.tok, R)
    _extend(dels, S, R)
    owner = _claims(dels, S)
    kept = {j for a, b in keep for j in range(a, b + 1)}
    for j in kept:
        if j < len(owner):
            owner[j] = None
    pieces = _pieces(dels, owner, S, R)
    _heal(pieces, dels, R, kept)

    for pc in pieces:
        pc.segs = _edges(R, pc.i_a, pc.i_b, tk)
    for prev, pc in zip(pieces, pieces[1:]):
        if pc.joined and prev.segs and pc.segs:
            mid = (R.word(prev.i_b)["end"] + R.word(pc.i_a)["start"]) / 2.0
            prev.segs[-1][1] = pc.segs[0][0] = mid

    # Group into pulls: same recording and forward in it, so each pull plays
    # as one stretch of one take.  A paragraph break starts a new one only
    # where the tape breaks there too: a read that carries straight on is one
    # region, however many paragraphs of the script it covers.
    pulls = []
    for pc in pieces:
        if pulls:
            last = pulls[-1][-1]
            if ((pc.joined or S.blk[pc.a] == S.blk[last.a])
                    and kept.isdisjoint(range(last.b + 1, pc.a))
                    and dels[pc.k].take == dels[last.k].take
                    and pc.i_a > last.i_b):
                pulls[-1].append(pc)
                continue
        pulls.append([pc])

    covered = set(kept)
    for pl in pulls:
        for pc in pl:
            covered.update(range(pc.a, pc.b + 1))
    results = []
    order = [0]

    def _base(block, text):
        order[0] += 1
        r = engines._vo_base_result(dict(block, order=order[0], text=text))
        r["vo_id"] = block["id"]
        r["monolog"] = True
        r["gap_after"] = True
        r["gap_after_s"] = 0.0
        return r

    gap_from = [0]

    def _missing_before(upto):
        """no_match rows for the script before token `upto` nobody read."""
        j = gap_from[0]
        while j < upto:
            if j in covered:
                j += 1
                continue
            a = j
            while j + 1 < upto and j + 1 not in covered and S.blk[j + 1] == S.blk[a]:
                j += 1
            if j - a + 1 >= 4:            # a word or three is not a missing line
                r = _base(S.blocks[S.blk[a]], S.text(a, j))
                r["script_span"] = (a, j)
                r["status"] = "no_match"
                r["diag"] = "not found in the recording"
                if tk:
                    r["source_audio"] = tk[0]["apath"]
                results.append(r)
            j += 1
        gap_from[0] = max(gap_from[0], upto)

    for pl in pulls:
        a0, b1 = pl[0].a, pl[-1].b
        _missing_before(a0)
        take_i = dels[pl[0].k].take
        segs = [s for pc in pl for s in pc.segs]
        for s, nxt in zip(segs, segs[1:]):
            if nxt[0] < s[1]:
                s[1] = nxt[0] = (s[1] + nxt[0]) / 2.0
        segs = _join(segs)
        r = _base(S.blocks[S.blk[a0]], S.text(a0, b1))
        r["script_span"] = (a0, b1)
        n_tok = b1 - a0 + 1
        cover = sum(dels[pc.k].matched(pc.a, pc.b) for pc in pl) / float(n_tok)
        heard = " ".join(R.word(i)["word"] for i in range(pl[0].i_a, pl[-1].i_b + 1)
                         if R.tok[i] is not None and (i == pl[0].i_a or R.wi[i] != R.wi[i - 1]))
        t = tk[take_i]
        chosen = {"segments": [tuple(s) for s in segs], "vpath": t["vpath"],
                  "apath": t["apath"], "v_offset": t["v_offset"], "kept": True}
        takes_data = _alternatives(dels, pl, R, tk, a0, b1) + [chosen]
        takes_data.sort(key=lambda d: (tk_index(tk, d["apath"]), d["segments"][0][0]))
        r.update({
            "segments":        [tuple(s) for s in segs],
            "rec_in_s":        segs[0][0],
            "rec_out_s":       segs[-1][1],
            "rec_in_tc":       secs_tc(segs[0][0]),
            "rec_out_tc":      secs_tc(segs[-1][1]),
            "in_tc":           secs_tc(segs[0][0]),
            "out_tc":          secs_tc(segs[-1][1]),
            "confidence":      round(cover, 3),
            "matched_text":    heard,
            "n_internal_cuts": len(segs) - 1,
            "takes_data":      takes_data,
            "best_take_index": takes_data.index(chosen),
            "source_audio":    t["apath"],
            "status":          "ok" if cover >= MIN_SHOW else "low_confidence",
        })
        if cover < MIN_SHOW:
            r["diag"] = "read differs from the script -- check the wording"
        results.append(r)
    _missing_before(len(S.tok))
    results.sort(key=lambda r: r["order"])
    return results


def pending_rows(vo_blocks):
    """The read's stand-in cut while the first pass is hearing it: one row per
    paragraph of the script.  The real pulls are numbered by the real cut, which
    only exists once the whole read is heard, and can be retaken up to the last
    minute; these are there to work in until then, and paragraphs_past fills
    them."""
    S = _Script(vo_blocks)
    rows = []
    for bi, block in enumerate(S.blocks):
        r = engines._vo_base_result(dict(block, order=bi + 1))
        r["vo_id"] = block["id"]
        r["monolog"] = True
        r["gap_after"] = True
        r["gap_after_s"] = 0.0
        r["script_span"] = (S.block_start[bi], S.block_start[bi + 1] - 1)
        rows.append(r)
    return rows


def paragraphs_past(vo_blocks, takes, path, frontier, landed, ahead=3, margin=5.0):
    """pending_rows' rows the first pass has got far enough past to trust:
    each paragraph cut against the read so far, where `path` is the recording
    still coming in (None once all are heard), heard as far as `frontier`.
    `landed` holds the orders already filled; only the next `ahead` paragraphs
    are looked for (None: every one left).  A paragraph goes in once `margin`
    seconds are past it and most of it has been read."""
    rows = pending_rows(vo_blocks)
    done = [i for i, r in enumerate(rows) if r["order"] in landed]
    todo = rows if ahead is None else rows[(done[-1] + 1 if done else 0):][:ahead]
    out = []
    for row in todo:
        if row["order"] in landed:
            continue
        first, last = row["script_span"]
        block = dict(next(b for b in sorted(vo_blocks, key=lambda b: b["order"])
                          if b["id"] == row["vo_id"]))
        pulls = [r for r in reconcile([block], takes) if r.get("segments")]
        if not pulls:
            continue
        src = pulls[-1]["source_audio"]      # where it was read last
        pulls = [r for r in pulls if r["source_audio"] == src]
        n_tok = last - first + 1
        said = sum((r["script_span"][1] - r["script_span"][0] + 1) * r["confidence"]
                   for r in pulls)
        segs = _join([list(s) for r in pulls for s in r["segments"]])
        # Part of a paragraph may yet be read while any of the read is to come.
        if path is not None and said < MIN_SHOW * n_tok:
            continue
        if src == path and segs[-1][1] + margin > frontier:
            continue
        chosen = dict(pulls[-1]["takes_data"][pulls[-1]["best_take_index"]],
                      segments=[tuple(s) for s in segs])
        filled = dict(row)
        filled.update({
            "segments":        [tuple(s) for s in segs],
            "rec_in_s":        segs[0][0],
            "rec_out_s":       segs[-1][1],
            "rec_in_tc":       secs_tc(segs[0][0]),
            "rec_out_tc":      secs_tc(segs[-1][1]),
            "in_tc":           secs_tc(segs[0][0]),
            "out_tc":          secs_tc(segs[-1][1]),
            "confidence":      round(said / float(n_tok), 3),
            "matched_text":    " ".join(r["matched_text"] for r in pulls),
            "n_internal_cuts": len(segs) - 1,
            "takes_data":      [chosen],
            "best_take_index": 0,
            "source_audio":    src,
            "status":          "ok" if said >= MIN_SHOW * n_tok else "low_confidence",
        })
        out.append(filled)
    return out


def locate_spans(vo_blocks, rows):
    """Give script_span to pulls cut before spans were recorded: a pull's
    quote_text is a slice of the script, found in order along it.  One that
    can't be found (the script has changed since) is left without."""
    tok = _Script(vo_blocks).tok
    start = 0
    for r in sorted(rows, key=lambda r: r.get("order", 0)):
        if r.get("script_span"):
            start = r["script_span"][1] + 1
            continue
        run = [t for w in (r.get("quote_text") or "").split() for t in _tokens(w)]
        if not run:
            continue
        n = len(run)
        at = next((i for i in list(range(start, len(tok) - n + 1)) + list(range(start))
                   if tok[i:i + n] == run), None)
        if at is not None:
            r["script_span"] = (at, at + n - 1)
            start = at + n


def _clear_of(r, kept):
    """Take from new pull r any tape a kept pull already plays: a kept pull can
    hold more than its lines, an ad-lib say, and the new cut can't know.  False
    when that leaves it nothing, unless it never had any (a line not found)."""
    segs = [tuple(s) for s in r.get("segments") or []]
    if not segs:
        return True
    for k in kept:
        if k.get("source_audio") != r.get("source_audio"):
            continue
        for ka, kb in k.get("segments") or []:
            left = []
            for a, b in segs:
                if b <= ka or a >= kb:
                    left.append((a, b))
                    continue
                if a < ka:
                    left.append((a, ka))
                if b > kb:
                    left.append((kb, b))
            segs = left
    segs = [s for s in segs if s[1] - s[0] >= MIN_LEFT_S]
    if not segs:
        return False
    r["segments"] = segs
    r["rec_in_s"], r["rec_out_s"] = segs[0][0], segs[-1][1]
    r["rec_in_tc"] = r["in_tc"] = secs_tc(segs[0][0])
    r["rec_out_tc"] = r["out_tc"] = secs_tc(segs[-1][1])
    r["n_internal_cuts"] = len(segs) - 1
    for td in r.get("takes_data") or []:
        if td.get("kept"):
            td["segments"] = list(segs)
    return True


def recut_around(vo_blocks, takes, old, touched):
    """The read cut again, keeping exactly as they are the pulls of the old
    cut that touched(row) says were worked on, and cutting new ones only for
    the lines those don't answer for -- so nothing plays twice.  Pulls are
    numbered by the cut, so the rows come back renumbered.

    Returns None, changing nothing, if a worked-on pull can't be placed against
    the script: better no new cut than losing someone's work."""
    locate_spans(vo_blocks, old)
    kept = [r for r in old if touched(r)]
    if any(not r.get("script_span") for r in kept):
        return None
    fresh = reconcile(vo_blocks, takes, keep=[tuple(r["script_span"]) for r in kept])
    rows = kept + [r for r in fresh if _clear_of(r, kept)]
    rows.sort(key=lambda r: tuple(r.get("script_span") or (0, -1)))
    for n, r in enumerate(rows, 1):
        r["order"] = n
    return rows


def tk_index(tk, apath):
    for k, t in enumerate(tk):
        if t["apath"] == apath:
            return k
    return len(tk)


def _alternatives(dels, pl, R, tk, a0, b1):
    """Other deliveries of the pull's lines, as takes_data entries: each
    reading that covers most of a0..b1 by itself."""
    used = {pc.k for pc in pl}
    span = b1 - a0 + 1
    out = []
    for k, d in enumerate(dels):
        if k in used or d.cj1 < a0 or d.cj0 > b1:
            continue
        if d.matched(a0, b1) < ALT_COVER * span:
            continue
        fa, lb = d.first_at_or_after(a0), d.last_at_or_before(b1)
        if not fa or not lb or fa[0] > lb[0]:
            continue
        i_a = d.i_head if a0 <= d.j0 else fa[0]
        i_b = d.i_tail if b1 >= d.j1 else lb[0]
        t = tk[d.take]
        out.append({"segments": [tuple(s) for s in _edges(R, i_a, i_b, tk)],
                    "vpath": t["vpath"],
                    "apath": t["apath"], "v_offset": t["v_offset"]})
    return out
