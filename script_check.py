"""Script Check — deterministic pre-flight on a script, before any media is loaded.

Every finding here is provable from the script text (plus, when the script sits
inside a project folder, the `.pb_session.json` files discovered next to it).
Nothing in this module transcribes, matches, or needs the media to exist.

The point is to catch — at load time — the class of defect that otherwise only
shows up after a full transcribe-and-reconcile, or worse, silently never shows
up at all: a script in the obsolete `@VO` syntax loads with 99 valid pulls and
zero narration, and the old pipeline said nothing.

`run_checks()` is pure and returns a list of Finding records.  The GUI renders
them; it does not decide what is wrong.
"""

import os
import re

from parsers import parse_script, discover_pq_sessions, _HEADER_RE

# Severity ordering is also the display ordering.
ERROR, WARN, INFO = "error", "warn", "info"
_SEV_RANK = {ERROR: 0, WARN: 1, INFO: 2}

# Words per second for a conversational read.  Used only to decide whether a
# pull's span could physically contain its own quote; deliberately generous so
# a fast talker doesn't trip it.
_WORDS_PER_SEC = 3.2

_LEGACY_RE = re.compile(r"^@(PULL|VO|PART)\b", re.IGNORECASE)
_BRACKET_LINE_RE = re.compile(r"^\[.*\]$")
_TOKEN_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class Finding(object):
    """One check result.  `line` is 1-indexed, or None when not line-specific."""

    __slots__ = ("severity", "category", "title", "detail", "line")

    def __init__(self, severity, category, title, detail="", line=None):
        self.severity = severity
        self.category = category
        self.title = title
        self.detail = detail
        self.line = line

    def __repr__(self):
        loc = "" if self.line is None else " @%d" % self.line
        return "<%s %s%s: %s>" % (self.severity.upper(), self.category, loc, self.title)


def _tc(seconds):
    return "%02d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60, seconds % 60)


def _strip_bom(text):
    return text[1:] if text[:1] == "﻿" else text


# ── structure ───────────────────────────────────────────────────────────────

def _check_legacy_markers(lines, out):
    hits = [(i, l.strip()) for i, l in enumerate(lines, 1) if _LEGACY_RE.match(l)]
    if not hits:
        return
    kinds = sorted({_LEGACY_RE.match(l).group(1).upper() for _, l in hits})
    out.append(Finding(
        ERROR, "Format",
        "Script uses the obsolete @%s syntax" % " / @".join(kinds),
        "%d marker line%s found.  These are no longer read: every narration block "
        "and section header they mark is dropped without error, while bracketed "
        "pull headers still load — so the script appears to import fine.\n"
        "Fix: Script Formatter → Copy AI Prompt converts a script to the "
        "bracketed form ([PART …] / [VO …] / [TOKEN in-out])."
        % (len(hits), "" if len(hits) == 1 else "s"),
        hits[0][0]))


def _check_shape(parts, pulls, vo_blocks, out, legacy=False):
    if legacy:
        # "no VO blocks" / "no parts" are symptoms of the obsolete syntax,
        # already reported with the fix.  Repeating them is just noise.
        return
    if not pulls and not vo_blocks:
        out.append(Finding(ERROR, "Format", "No pulls and no VO blocks were parsed",
                           "Nothing in this file matched a bracketed header."))
        return
    if not vo_blocks:
        out.append(Finding(ERROR, "Format", "No VO blocks — no narration was parsed",
                           "Narration needs its own [VO <id>] header.  Prose sitting "
                           "under a [PART …] with no [VO …] above it is dropped."))
    if not parts:
        out.append(Finding(WARN, "Format", "No [PART …] section headers were found",
                           "Every item lands in one unnamed section, and VO takes are "
                           "binned per part, so there will be a single VO bin."))


def _check_indented_headers(lines, out):
    for i, raw in enumerate(lines, 1):
        if not raw[:1].isspace():
            continue
        if _HEADER_RE.match(raw.strip()):
            out.append(Finding(
                ERROR, "Structure", "Header is indented and will be ignored",
                "%s\nHeaders must start at column 0.  As written, this line is "
                "treated as body text and its pull/VO block is lost." % raw.strip(),
                i))


def _check_unparsed_brackets(lines, out):
    """Lines that look like a header but aren't one — they become quote text."""
    in_tokens = False
    for i, raw in enumerate(lines, 1):
        s = raw.strip()
        if s.upper() == "[TOKENS]":
            in_tokens = True
            continue
        if s.upper() == "[/TOKENS]":
            in_tokens = False
            continue
        if in_tokens or not s or raw[:1].isspace():
            continue
        if _BRACKET_LINE_RE.match(s) and not _HEADER_RE.match(s):
            out.append(Finding(
                WARN, "Structure", "Bracketed line is not a valid header",
                "%s\nIt will be absorbed into the surrounding block as body text.  "
                "If it is a note or a direction, prefix it with // to make it a "
                "comment." % s,
                i))


def _consumed_map(lines):
    """Walk the file the way parse_script does and mark which prose lines land
    inside a block.  Returns a set of 1-indexed line numbers that do NOT."""
    orphans = set()
    open_block = False
    in_tokens = False
    title_taken = False
    for i, raw in enumerate(lines, 1):
        s = raw.strip()
        if not s:
            continue
        if s.startswith("//"):
            continue
        if s.upper() == "[TOKENS]":
            in_tokens = True
            continue
        if s.upper() == "[/TOKENS]":
            in_tokens = False
            continue
        if in_tokens:
            continue
        if not raw[:1].isspace():
            m = _HEADER_RE.match(s)
            if m:
                # [PART …] does not open a text block; [VO …] and pulls do.
                open_block = m.group("part") is None
                continue
        if _BRACKET_LINE_RE.match(s):
            continue                    # reported by _check_unparsed_brackets
        if not open_block:
            if not title_taken:
                title_taken = True      # first loose line becomes doc_title
                continue
            orphans.add(i)
    return orphans


def _check_orphan_prose(lines, out):
    orphans = sorted(_consumed_map(lines))
    if not orphans:
        return
    # Group consecutive line numbers so a dropped paragraph reports once.
    groups, run = [], [orphans[0]]
    for n in orphans[1:]:
        if n - run[-1] <= 2:
            run.append(n)
        else:
            groups.append(run)
            run = [n]
    groups.append(run)
    for g in groups:
        preview = lines[g[0] - 1].strip()
        out.append(Finding(
            ERROR, "Structure", "Text belongs to no block and will be dropped",
            "%s%s\nThis sits outside any [VO …] or pull header, so parse_script "
            "never collects it.  Give it a [VO <id>] header, or make it a // comment."
            % (preview[:160], "…" if len(preview) > 160 else ""),
            g[0]))


def _check_doc_title(doc_title, tokens, out):
    t = (doc_title or "").strip()
    if not t:
        return
    bad = (t.startswith("---") or t.upper() in ("[TOKENS]", "[/TOKENS]")
           or t in tokens or _BRACKET_LINE_RE.match(t))
    if bad:
        out.append(Finding(
            WARN, "Structure", "Episode title looks wrong: %r" % t[:60],
            "The title is taken from the first loose line in the file, and it also "
            "names the sequence.  Put the episode title on its own line at the top."))


def _check_empty_blocks(pulls, out):
    empties = [p for p in pulls if not (p.get("quote_text") or "").strip()]
    if empties:
        out.append(Finding(
            INFO, "Structure",
            "%d pull%s have no quote text" % (len(empties), "" if len(empties) == 1 else "s"),
            "These cannot be matched against a transcript, so they keep the "
            "timecodes written in the script.  That is correct for non-speech "
            "inserts; for an interview pull it usually means the quote is missing.\n"
            + "\n".join("  [%s %s-%s]" % (p["token"], p["in_tc"], p["out_tc"])
                        for p in empties[:8])))


# ── timecodes ───────────────────────────────────────────────────────────────

def _check_duplicate_headers(lines, out):
    """Duplicate (token, in, out) pulls must be found in the raw text: the
    parser silently drops them, so by the time we have `pulls` they are gone."""
    seen = {}
    for i, raw in enumerate(lines, 1):
        if raw[:1].isspace():
            continue
        m = _HEADER_RE.match(raw.split("//")[0].strip())
        if not m or m.group("tok") is None:
            continue
        key = (m.group("tok"), m.group("intc"), m.group("outtc"))
        if key in seen:
            out.append(Finding(
                WARN, "Timecode", "Duplicate pull is dropped without warning",
                "[%s %s-%s] also appears on line %d.  The parser keeps only the "
                "first, so the second block of quote text never reaches the "
                "timeline." % (key[0], key[1], key[2], seen[key]),
                i))
        else:
            seen[key] = i


def _check_timecodes(pulls, out):
    for p in pulls:
        if p["out_seconds"] <= p["in_seconds"]:
            out.append(Finding(
                ERROR, "Timecode", "Out-point is not after in-point",
                "[%s %s-%s]" % (p["token"], p["in_tc"], p["out_tc"])))

    # A span that cannot physically hold its own quote.
    for p in pulls:
        words = len((p.get("quote_text") or "").split())
        if words < 4:
            continue
        span = p["out_seconds"] - p["in_seconds"]
        needed = words / _WORDS_PER_SEC
        if span > 0 and span < needed * 0.6:
            out.append(Finding(
                WARN, "Timecode", "Span is too short for the quote",
                "[%s %s-%s] is %ds but the quote is %d words (≈%ds of speech).  "
                "Check the out-point."
                % (p["token"], p["in_tc"], p["out_tc"], span, words, round(needed))))

    # Overlapping spans on the same token.
    by_token = {}
    for p in pulls:
        by_token.setdefault(p["token"], []).append(p)
    for tok, ps in sorted(by_token.items()):
        ps = sorted(ps, key=lambda x: x["in_seconds"])
        for a, b in zip(ps, ps[1:]):
            if b["in_seconds"] < a["out_seconds"]:
                same_out = a["out_seconds"] == b["out_seconds"]
                out.append(Finding(
                    ERROR if same_out else INFO, "Timecode",
                    "Two %s pulls share an out-point" % tok if same_out
                    else "%s pulls overlap" % tok,
                    "[%s %s-%s] and [%s %s-%s]%s"
                    % (tok, a["in_tc"], a["out_tc"], tok, b["in_tc"], b["out_tc"],
                       "\nTwo different quotes cannot occupy the same span — one "
                       "of these timecodes was copied from the other."
                       if same_out else
                       "\nFine if the shorter pull is material elided from the longer "
                       "one; otherwise check them.")))


# ── tokens and assets ───────────────────────────────────────────────────────

def _declared_tokens(text):
    """Tokens written in the [TOKENS] block, split into valid and rejected."""
    m = re.search(r"\[TOKENS\](.*?)\[/TOKENS\]", text, re.DOTALL | re.IGNORECASE)
    if not m:
        return [], []
    valid, rejected = [], []
    for ln in m.group(1).split("\n"):
        s = ln.split("//")[0].strip().strip("[]").strip()
        if not s:
            continue
        (valid if _TOKEN_NAME_RE.match(s) else rejected).append(s)
    return valid, rejected


def _edit_distance_1(a, b):
    if abs(len(a) - len(b)) > 1:
        return False
    if a == b:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    for i in range(len(long_)):
        if long_[:i] + long_[i + 1:] == short:
            return True
    return False


def _check_tokens(text, tokens, pulls, out):
    declared, rejected = _declared_tokens(text)
    used = []
    for p in pulls:
        if p["token"] not in used:
            used.append(p["token"])

    for bad in rejected:
        out.append(Finding(
            ERROR, "Assets", "Invalid token name in [TOKENS]: %r" % bad,
            "Token names must be uppercase letters, digits and underscores "
            "(e.g. JOHN_SMITH).  This line is ignored, so the asset is never "
            "registered."))

    for d in declared:
        if d not in used:
            out.append(Finding(
                WARN, "Assets", "Token %s is declared but never used" % d,
                "No pull references it.  Either a pull is missing, or the "
                "declaration is stale."))

    for u in used:
        if declared and u not in declared:
            out.append(Finding(
                INFO, "Assets", "Token %s is used but not declared" % u,
                "It auto-registers on first use, so this is legal — but if the "
                "[TOKENS] block is meant to be the asset list, it is incomplete."))

    names = sorted(set(declared) | set(used))
    reported = set()
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if _edit_distance_1(a, b) and (a, b) not in reported:
                reported.add((a, b))
                out.append(Finding(
                    WARN, "Assets", "Token names differ by one character: %s / %s" % (a, b),
                    "Almost certainly a typo for the same speaker.  One of them will "
                    "never find its media."))


def _check_sessions(script_path, tokens, pulls, out):
    """Cross-check the script's tokens against .pb_session.json files on disk."""
    if not script_path or not os.path.isfile(script_path):
        return
    try:
        found = discover_pq_sessions(script_path)
    except Exception as exc:                       # discovery is best-effort
        out.append(Finding(INFO, "Assets", "Could not scan for session files", str(exc)))
        return

    for w in (found.pop("__warnings__", None) or []):
        out.append(Finding(WARN, "Assets", "Session discovery warning", str(w)))

    if not found:
        out.append(Finding(
            INFO, "Assets", "No .pb_session.json files found near this script",
            "Transcripts could not be cross-referenced.  This is expected if the "
            "interviews have not been transcribed yet."))
        return

    used = {p["token"] for p in pulls}
    for tok in sorted(used):
        if tok not in found:
            out.append(Finding(
                INFO, "Assets", "No session found for token %s" % tok,
                "Pulls reference it, but no %s.pb_session.json was discovered." % tok))
    for tok in sorted(found):
        if tok not in used:
            out.append(Finding(
                INFO, "Assets", "Session %s is not used by the script" % tok,
                "A transcribed session exists but no pull references it."))

    _check_session_media(found, out)
    _check_transcript_quality(found, pulls, out)
    _check_against_transcripts(found, pulls, out)


# Whisper models small enough that they drop words outright.  A transcript
# built from one of these resolves pulls badly and is cheap to redo.
_WEAK_MODELS = ("tiny", "base")

# A silence this long between consecutive words is not a pause — it is speech
# the transcriber missed.
_HOLE_SECS = 8.0


def _check_transcript_quality(found, pulls, out):
    """Flag sessions that will resolve badly because the transcript itself is
    thin: built with a weak model, or missing chunks of speech outright.

    A hole matters when a pull is written across it — the matcher then reaches
    far outside the span hunting for words that were never transcribed."""
    for tok, path in sorted(found.items()):
        data = _load_session(path)
        if not data:
            continue
        words = data.get("transcript") or []
        if not words:
            continue

        srcs = {}
        for w in words:
            s = (w.get("_src") or "").lower()
            srcs[s] = srcs.get(s, 0) + 1
        weak = sum(n for s, n in srcs.items() if s in _WEAK_MODELS)
        if weak and weak >= len(words) * 0.5:
            worst = sorted((s for s in srcs if s in _WEAK_MODELS))
            out.append(Finding(
                WARN, "Transcript",
                "%s was transcribed with the %s model" % (tok, "/".join(worst)),
                "%d of %d words came from a model that drops speech.  Pulls over "
                "those regions resolve to the wrong span, and re-transcribing the "
                "session at a higher quality is far cheaper than fixing the clips "
                "by hand." % (weak, len(words))))

        holes, prev = [], None
        for w in words:
            s = float(w.get("start", 0.0))
            if prev is not None and s - prev > _HOLE_SECS:
                holes.append((prev, s))
            prev = float(w.get("end", s))

        for h_start, h_end in holes:
            hit = [p for p in pulls
                   if p["token"] == tok
                   and p["in_seconds"] < h_end and p["out_seconds"] > h_start]
            if not hit:
                continue
            out.append(Finding(
                WARN, "Transcript",
                "%s transcript has a %ds hole at %s"
                % (tok, round(h_end - h_start), _tc(int(h_start))),
                "No words were transcribed between %s and %s, so speech there is "
                "missing entirely.  %d pull%s written across it:\n%s\n"
                "Re-transcribe this session before reconciling."
                % (_tc(int(h_start)), _tc(int(h_end)), len(hit),
                   "" if len(hit) == 1 else "s",
                   "\n".join("  [%s %s-%s]" % (p["token"], p["in_tc"], p["out_tc"])
                             for p in hit[:6]))))


def _load_session(path):
    import json
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _check_against_transcripts(found, pulls, out):
    """Resolve every pull against its already-transcribed session.

    This runs the production resolver (`reconcile_pull_from_session`) rather
    than a lookalike, so what it reports is exactly what the pipeline will do
    later — including the mis-pointed-timecode guard, which is what catches a
    span copy-pasted from a neighbouring pull.  No media and no Whisper: the
    word timings already live in the session JSON.
    """
    import engines                      # imported lazily: heavy, and only needed here

    cache = {}
    checked = 0
    for p in pulls:
        path = found.get(p["token"])
        if not path:
            continue
        if path not in cache:
            cache[path] = _load_session(path)
        data = cache[path]
        if not data:
            continue

        try:
            res = engines.reconcile_pull_from_session(dict(p), data)
        except Exception as exc:
            out.append(Finding(
                INFO, "Transcript", "Could not cross-check [%s %s-%s]"
                % (p["token"], p["in_tc"], p["out_tc"]), str(exc)))
            continue
        checked += 1

        status = res.get("status")
        where = "[%s %s-%s]" % (p["token"], p["in_tc"], p["out_tc"])
        quote = (p.get("quote_text") or "").strip()
        quote = (quote[:120] + "…") if len(quote) > 120 else quote

        if status == "no_transcript":
            continue                     # already reported by the asset checks

        if status == "no_match":
            out.append(Finding(
                ERROR, "Transcript", "Quote not found in the %s transcript" % p["token"],
                "%s\n“%s”\nNeither the written timecodes nor a search of the "
                "whole transcript located this quote.  Either the timecodes point at "
                "the wrong session, or the quote was rewritten past recognition.\n%s"
                % (where, quote, res.get("_diag", "")),
                None))
            continue

        # The resolver snaps every pull to word boundaries and sets
        # _text_fallback freely — including when it snaps straight back to the
        # same place.  So the flag on its own proves nothing; compare the spans.
        # Likewise, a loose out-point is something reconcile silently fixes, so
        # it is not worth a warning.  Only report what a human must act on.
        rec_in = float(res.get("rec_in_s") or 0.0)
        rec_out = float(res.get("rec_out_s") or 0.0)
        dec_in, dec_out = p["in_seconds"], p["out_seconds"]
        overlap = min(rec_out, dec_out) - max(rec_in, dec_in)
        rec_dur = max(0.0, rec_out - rec_in)
        dec_dur = max(0.001, dec_out - dec_in)

        if overlap <= 0:
            out.append(Finding(
                ERROR, "Transcript", "Timecodes point at the wrong audio",
                "%s\n“%s”\nThese words are not spoken anywhere in that span — "
                "they are at %s-%s.  A span copied from a neighbouring pull looks "
                "exactly like this.\nLeft as written, the clip is cut from the wrong "
                "place.\n%s"
                % (where, quote, res.get("rec_in_tc"), res.get("rec_out_tc"),
                   res.get("_diag", "")),
                None))
            continue

        if rec_dur > dec_dur * 3 and rec_dur - dec_dur > 30:
            out.append(Finding(
                WARN, "Transcript", "Quote resolves to a far longer span than written",
                "%s is %ds, but matching the quote stretches it to %s-%s (%ds).\n"
                "“%s”\nUsually the transcript is missing words in this region, so "
                "the matcher reaches further to find them.  Check this clip by ear."
                % (where, round(dec_dur), res.get("rec_in_tc"), res.get("rec_out_tc"),
                   round(rec_dur), quote),
                None))
            continue

        if abs(rec_in - dec_in) >= 10:
            out.append(Finding(
                WARN, "Transcript", "In-point is well before the first spoken word",
                "%s\n“%s”\nThe quote does not start until %s (%+.0fs).  The clip "
                "would open on %.0fs of unrelated audio."
                % (where, quote, res.get("rec_in_tc"), rec_in - dec_in, rec_in - dec_in),
                None))

    if checked:
        out.append(Finding(
            INFO, "Transcript",
            "%d pull%s cross-checked against transcripts"
            % (checked, "" if checked == 1 else "s"),
            "Resolved from the session JSONs with no media and no transcription run."))


def _check_session_media(found, out):
    import json
    dead = []
    for tok, path in sorted(found.items()):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        for m in (data.get("media") or []):
            if not os.path.exists(m):
                dead.append((tok, m))
    if dead:
        out.append(Finding(
            ERROR, "Assets",
            "%d session media file%s cannot be found on disk"
            % (len(dead), "" if len(dead) == 1 else "s"),
            "The session files point at paths that do not exist here — typically "
            "because the project moved between machines.  Re-point them before "
            "reconciling.\n"
            + "\n".join("  %s → %s" % (t, p) for t, p in dead[:10])))


# ── entry point ─────────────────────────────────────────────────────────────

def run_checks(text, script_path=None):
    """Return a list of Finding, most severe first.

    `text` is the raw script.  `script_path`, when given, enables the on-disk
    asset checks (session discovery and media paths).
    """
    text = _strip_bom(text or "")
    lines = text.split("\n")
    out = []

    tokens, parts, pulls, vo_blocks, doc_title, warnings = parse_script(text)

    for w in warnings:
        out.append(Finding(WARN, "Format", "Parser warning", str(w)))

    _check_legacy_markers(lines, out)
    _legacy = any(f.category == "Format" and "obsolete" in f.title for f in out)
    _check_shape(parts, pulls, vo_blocks, out, legacy=_legacy)
    _check_indented_headers(lines, out)
    _check_unparsed_brackets(lines, out)
    _check_orphan_prose(lines, out)
    _check_doc_title(doc_title, tokens, out)
    _check_empty_blocks(pulls, out)
    _check_duplicate_headers(lines, out)
    _check_timecodes(pulls, out)
    _check_tokens(text, tokens, pulls, out)
    _check_sessions(script_path, tokens, pulls, out)

    out.sort(key=lambda f: (_SEV_RANK[f.severity], f.category, f.line or 0))
    return out


def summarize(findings):
    """(n_error, n_warn, n_info) for a one-line headline."""
    return (sum(1 for f in findings if f.severity == ERROR),
            sum(1 for f in findings if f.severity == WARN),
            sum(1 for f in findings if f.severity == INFO))
