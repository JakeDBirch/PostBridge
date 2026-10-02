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
