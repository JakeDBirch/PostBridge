"""Fix Format — repair a script's structure without touching what it says.

A script usually arrives with its structure half marked up: pulls with headers
but narration with none, "Part 1: The Lake" on a line of its own, a dash between
sections.  The parser can't tell any of that from body text, so the narration
quietly becomes part of the quote above it.

`walk()` reads a script the way the parser does and names what each line really
is.  Script Check reports from it and `normalize()` repairs from it, so the two
can't disagree about what is wrong.

normalize() only ever adds or corrects headers, removes dividers and fills in
the token list.  Quote text, narration and timecodes are never changed; anything
it can't settle, such as a placeholder with no timecodes, stays for Script Check
to name.  It is pure, and running it twice changes nothing the second time.
"""

import re

from parsers import (_HEADER_RE, _normalize_tc, _strip_comment, is_plain_script,
                     parse_script)

_OPEN_QUOTE  = ("“", '"')
_CLOSE_QUOTE = ("”", '"')

# "Part 1: The Lake", "Part 1", "Intro", "Outro" alone on a line.
_SECTION_RE = re.compile(
    r"^(?:part\s+(?:\d+|[ivxlc]+)(?:\s*[:\-–—]\s*\S.*)?"
    r"|intro(?:duction)?|outro|cold open)$", re.IGNORECASE)
# A line of nothing but rule characters.
_DIVIDER_RE = re.compile(r"^[—–\-_*•#~=\s]+$")
# "Dutton (42:38-42:58): “In the…”", or a bracketed header that doesn't parse:
# lowercase, MM:SS, an en dash, spaces around the hyphen.
_TC = r"\d{1,2}:\d{2}(?::\d{2})?"
_LOOSE_PULL_RE = re.compile(
    r"^\[?\s*(?P<name>[A-Za-z][\w'’\-]*(?:[ .]+[A-Za-z][\w'’\-]*){0,3})\s*[(\[]?\s*"
    r"(?P<in>" + _TC + r")\s*(?:-|–|—|to)\s*(?P<out>" + _TC + r")\s*[)\]]*\s*:?\s*"
    r"(?P<body>(?:[“\"].*)?)$")
# "Chris Interview Clip": something to go here, with no timecodes to place it.
_PLACEHOLDER_RE = re.compile(
    r"^(?=.{1,50}$)\[?[\w'’. \-]*\b(?:clip|sot|sound ?bite)"
    r"(?:\s+(?:here|goes here|tk))?\]?$", re.IGNORECASE)

_THANKS_RE = re.compile(r"^thanks for listening", re.IGNORECASE)


class Line(object):
    """One non-blank line of a script and what it really is.

    kind: tokens, title, part, vo, pull, body (of a pull), narration (in a VO
    block), comment, swallowed (narration a pull's text would absorb), orphan
    (text in no block), section, divider, loose_pull, placeholder
    """
    __slots__ = ("n", "s", "kind", "pull", "bare_quote")

    def __init__(self, n, s, kind, pull=None, bare_quote=False):
        self.n, self.s, self.kind = n, s, kind
        self.pull = pull                # (token, in_tc, out_tc) it sits under
        self.bare_quote = bare_quote    # all of a narration paragraph is a quote


def looks_like_title(s):
    """A sentence or a section name isn't an episode title."""
    if _SECTION_RE.match(s) or _DIVIDER_RE.match(s):
        return False
    words = len(s.split())
    return words <= 14 and not (words > 6 and s[-1:] in ".?!")


def _pad(tc):
    """'42:38' or '1:02:03' as HH:MM:SS."""
    return _normalize_tc("00:" + tc if tc.count(":") == 1 else tc)


def loose_pull_header(m):
    """The header a _LOOSE_PULL_RE match should have been."""
    token = re.sub(r"[^A-Z0-9_]+", "_", m.group("name").upper()).strip("_")
    return "[{} {}-{}]".format(token, _pad(m.group("in")), _pad(m.group("out")))


def walk(text):
    """Every non-blank line of `text`, classified in order."""
    text = text.lstrip("\ufeff")
    out = []
    block, quote, pull = None, None, None   # quote: None, "open", "closed", "none"
    in_tokens = seen_header = False

    def _body(s):
        """Advance the quote state over a line of pull text."""
        nonlocal quote
        if quote in (None, "closed") and s.startswith(_OPEN_QUOTE):
            quote = "open"
        elif quote is None:
            quote = "none"
        if quote == "open" and s.endswith(_CLOSE_QUOTE) and len(s) > 1:
            quote = "closed"

    for n, raw in enumerate(text.split("\n"), 1):
        s = _strip_comment(raw).strip()
        if not s:
            if raw.strip():
                out.append(Line(n, raw.strip(), "comment"))
            continue
        up = s.upper()
        if up == "[TOKENS]":
            in_tokens = True
        if in_tokens:
            out.append(Line(n, s, "tokens"))
            if up == "[/TOKENS]":
                in_tokens = False
            continue

        m = _HEADER_RE.match(s)
        if m:
            seen_header = True
            if m.group("part") is not None:
                block, pull = None, None
                out.append(Line(n, s, "part"))
            elif m.group("void") is not None:
                block, quote, pull = "vo", None, None
                out.append(Line(n, s, "vo"))
            else:
                block, quote = "pull", None
                pull = (m.group("tok"), _normalize_tc(m.group("intc")),
                        _normalize_tc(m.group("outtc")))
                out.append(Line(n, s, "pull", pull))
            continue

        if _SECTION_RE.match(s):
            seen_header = True
            block, pull = None, None
            out.append(Line(n, s, "section"))
            continue
        if _DIVIDER_RE.match(s):
            # `pull` stays: to the parser, what follows is still under that pull.
            block = None
            out.append(Line(n, s, "divider"))
            continue
        lp = _LOOSE_PULL_RE.match(s)
        if lp:
            seen_header = True
            block, quote = "pull", None
            pull = (loose_pull_header(lp), None, None)
            out.append(Line(n, s, "loose_pull", pull))
            if lp.group("body"):
                _body(lp.group("body"))
            continue
        if _PLACEHOLDER_RE.match(s) and quote != "open":
            out.append(Line(n, s, "placeholder"))
            continue

        if block == "pull":
            if quote == "closed" and not s.startswith(_OPEN_QUOTE):
                # The quote is over: this is narration.
                out.append(Line(n, s, "swallowed", pull))
                block, quote, pull = "vo", None, None
                continue
            _body(s)
            out.append(Line(n, s, "body", pull))
        elif block == "vo":
            bare = (s.startswith("“") and s.endswith("”")
                    and "“" not in s[1:] and pull is None)
            out.append(Line(n, s, "narration", pull, bare))
        elif not seen_header and looks_like_title(s):
            seen_header = True
            out.append(Line(n, s, "title"))
        else:
            seen_header = True
            out.append(Line(n, s, "swallowed" if pull else "orphan", pull))
            block, quote, pull = "vo", None, None
    return out


def has_loose_pulls(text):
    """Pulls written without a proper header, which a plain script can't have."""
    return any(l.kind == "loose_pull" for l in walk(text))


def _section_header(s, prev_part=None):
    """The [PART …] for a loose section line, keeping an existing part's name
    when the line only adds a number: [PART Bill] + "Part 1" = "Part 1: Bill"."""
    name = s if s.lower().startswith("part") else s.capitalize()
    if prev_part and re.match(r"^part\s+\w+$", s, re.IGNORECASE):
        name = "{}: {}".format(s, prev_part)
    return "[PART {}]".format(name)


def normalize(text, title=None):
    """(new_text, changes) -- `changes` says what was done in plain words, and
    is empty when the script's structure is already sound.

    `title` is used when the script has none, usually the file's name.
    """
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    if re.search(r"(?m)^@(PULL|VO|PART)\b", text):
        return text, []                 # the old syntax needs the AI prompt
    if is_plain_script(text) and not has_loose_pulls(text):
        return text, []                 # a monolog script has no structure to fix

    lines = text.split("\n")
    by_line = {l.n: l for l in walk(text)}
    out = []
    n_vo = n_part = n_merge = n_div = n_pull = 0
    outro_added = False
    has_outro = bool(re.search(r"(?im)^\[PART\s+.*outro.*\]$|^outro$", text))
    vo_num, any_part = 0, False
    after_divider = False

    def _last_part():
        """Index in `out` of a [PART …] with nothing but blanks after it."""
        for k in range(len(out) - 1, -1, -1):
            if out[k].strip():
                return k if out[k].startswith("[PART ") else None
        return None

    def _part_num(name):
        nonlocal vo_num, any_part
        m = re.match(r"^part\s+(\d+)", name, re.IGNORECASE)
        if m:
            vo_num = int(m.group(1))
        elif re.match(r"^(intro|cold open)", name, re.IGNORECASE):
            vo_num = 0
        elif any_part:
            vo_num += 1
        any_part = True

    for n, raw in enumerate(lines, 1):
        l = by_line.get(n)
        if l is None or l.kind in ("comment", "body", "narration", "title",
                                   "pull", "placeholder"):
            if l is not None and l.kind in ("body", "narration", "placeholder", "pull"):
                after_divider = False
            out.append(raw.rstrip() if l is None else raw)
            continue
        if l.kind == "tokens":
            continue                    # rebuilt below
        if l.kind == "part":
            after_divider = False
            out.append(l.s)
            _part_num(_HEADER_RE.match(l.s).group("part").strip())
        elif l.kind == "vo":
            after_divider = False
            out.append(l.s)
            m = re.match(r"^PART_(\d+)_", _HEADER_RE.match(l.s).group("void"))
            if m:
                vo_num = int(m.group(1))
        elif l.kind == "section":
            after_divider = False
            k = _last_part()
            if k is not None:
                prev = _HEADER_RE.match(out[k]).group("part").strip()
                if l.s.lower() != prev.lower():
                    out[k] = _section_header(l.s, prev)
                    if l.s.lower().startswith("part"):
                        _part_num(l.s)
                n_merge += 1
            else:
                header = _section_header(l.s)
                out.append(header)
                _part_num(_HEADER_RE.match(header).group("part").strip())
                n_part += 1
        elif l.kind == "divider":
            n_div += 1
            after_divider = True
        elif l.kind == "loose_pull":
            after_divider = False
            lp = _LOOSE_PULL_RE.match(l.s)
            out.append(loose_pull_header(lp))
            if lp.group("body"):
                out.append(lp.group("body"))
            n_pull += 1
        elif l.kind in ("swallowed", "orphan"):
            if after_divider and not has_outro and _THANKS_RE.match(l.s):
                out += ["[PART Outro]", ""]
                _part_num("Outro")
                has_outro = outro_added = True
            after_divider = False
            out.append("[VO PART_{}_NARRATOR]".format(vo_num))
            out.append(raw.rstrip())
            n_vo += 1

    body = "\n".join(out)
    body = re.sub(r"\n{4,}", "\n\n\n", body).strip("\n") + "\n"

    # The token list: what's written there, then anything used but not listed.
    tokens, _parts, _pulls, _vo, _t, _w = parse_script(body)
    written = []
    m = re.search(r"\[TOKENS\](.*?)\[/TOKENS\]", text, re.DOTALL | re.IGNORECASE)
    if m:
        for ln in m.group(1).split("\n"):
            s = _strip_comment(ln).strip().strip("[]").strip()
            if s and s not in written:
                written.append(s)
    used = [p["token"] for p in _pulls]
    missing = [t for t in dict.fromkeys(used) if t not in written]
    # The list is optional, so a sound script without one needs nothing done.
    if not (m or n_vo or n_part or n_merge or n_div or n_pull or outro_added):
        return text, []
    token_block = "\n".join(["[TOKENS]"] + written + missing + ["[/TOKENS]"])

    # The title goes first, then the token list.
    head = []
    has_title = any(l.kind == "title" for l in by_line.values())
    n_title = 0
    if has_title:
        first, _sep, rest = body.partition("\n")
        # The title stays on its line; blank lines up to the next block follow.
        body = rest.lstrip("\n")
        head = [first, "", ""]
    elif title:
        head = [title, "", ""]
        n_title = 1
    new = "\n".join(head + [token_block, "", "", body])

    changes = []
    if n_title:
        changes.append("Added the episode title: {}".format(title))
    if n_vo:
        changes.append("Added {} [VO] header{} so narration stops running into "
                       "the pull above it".format(n_vo, "" if n_vo == 1 else "s"))
    if n_part:
        changes.append("Turned {} section line{} into [PART] headers"
                       .format(n_part, "" if n_part == 1 else "s"))
    if n_merge:
        changes.append("Folded {} section line{} into the [PART] header above"
                       .format(n_merge, "" if n_merge == 1 else "s"))
    if outro_added:
        changes.append("Added [PART Outro] above “Thanks for listening”")
    if n_pull:
        changes.append("Rewrote {} pull{} as proper headers"
                       .format(n_pull, "" if n_pull == 1 else "s"))
    if n_div:
        changes.append("Removed {} divider line{}".format(n_div, "" if n_div == 1 else "s"))
    if missing:
        changes.append("Added {} to the token list".format(", ".join(missing)))

    if not changes:
        return text, []
    return new, changes

