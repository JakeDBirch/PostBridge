import os
from utils import app_state_dir as _app_state_dir
import re
import subprocess
import tempfile
import json
from utils import tc_secs, basename, run_hidden


def _ffmpeg_cmd():
    """The bundled ffmpeg (or system / Homebrew / downloaded copy), never a
    bare "ffmpeg": a Mac without ffmpeg on PATH has only the bundled one,
    and a bare spawn there fails on every call."""
    try:
        from ffmpeg_bundled import get_ffmpeg_cmd
        return get_ffmpeg_cmd()
    except Exception:
        return ["ffmpeg"]


def _ffprobe_cmd():
    try:
        from ffmpeg_bundled import get_ffprobe_cmd
        return get_ffprobe_cmd()
    except Exception:
        return ["ffprobe"]


_vob_starts_cache = {}

def _vob_starts(path):
    """(video_start, audio_start) program-clock seconds of a .VOB's first
    video and audio stream, or None when ffprobe can't say.  Memoised."""
    key = os.path.normcase(os.path.abspath(path))
    if key in _vob_starts_cache:
        return _vob_starts_cache[key]
    starts = []
    for sel in ("v:0", "a:0"):
        try:
            r = run_hidden(
                _ffprobe_cmd() + ["-v", "quiet", "-select_streams", sel,
                                  "-show_entries", "stream=start_time",
                                  "-of", "csv=p=0", path],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=15)
            # csv rows can carry a trailing comma (side-data section).
            first = (r.stdout or "").strip().splitlines()[0]
            starts.append(float(first.split(",")[0]))
        except Exception:
            return None    # unknown — don't cache, the file may be mid-copy
    _vob_starts_cache[key] = tuple(starts)
    return _vob_starts_cache[key]


def vob_av_skew(path):
    """Seconds the first audio packet of a DVD .VOB starts after its first
    video frame (negative = audio first); 0.0 for anything else or when
    ffprobe can't say.

    A VOB is an MPEG program stream: audio and video carry their own
    timestamps and needn't start together.  ffmpeg decodes the audio from
    its own first packet, but the NLE puts frame 0 of the picture at the
    head of the clip, so a sync offset measured on the raw audio would be
    off by exactly this much."""
    from utils import is_vob
    starts = _vob_starts(path) if is_vob(path) else None
    if not starts:
        return 0.0
    skew = starts[1] - starts[0]
    # Anything past a few seconds is a timestamp reset, not a real lead.
    return skew if abs(skew) <= 5.0 else 0.0


def audio_read_args(path, t_start=0.0, t_dur=None, min_seek=0.5):
    """ffmpeg arguments that open *path* and read its audio from *t_start*
    for *t_dur* seconds, everything up to (not including) the output's own
    options such as -ac / -ar / -f.

    Ordinary media: fast input seek (only past *min_seek*, as before).

    DVD .VOB: time is measured from the first video frame — see
    vob_av_skew — so every window agrees with the picture the NLE shows.
    And an input seek in an MPEG program stream lands somewhere that
    depends on how the streams' start times line up, so the same -ss
    reads different audio from one VOB to the next.  So the input seek
    only gets close (10 s short), timestamps are kept on the disc's own
    program clock (-copyts), and the output seek cuts at the exact
    program-clock time of the frame wanted."""
    from utils import is_vob
    if not is_vob(path):
        args = []
        if t_start > min_seek:
            args += ["-ss", "{:.3f}".format(t_start)]
        if t_dur is not None:
            args += ["-t", "{:.3f}".format(max(t_dur, 0.1))]
        return args + ["-i", path]
    t_start = max(0.0, t_start)
    skew    = vob_av_skew(path)
    starts  = _vob_starts(path)
    _NEAR_S = 10.0
    if starts and t_start > _NEAR_S:
        args = ["-ss", "{:.3f}".format(t_start - _NEAR_S), "-copyts",
                "-i", path, "-ss", "{:.4f}".format(starts[0] + t_start)]
    else:
        # Near the top: decode from the start and seek on the output.
        args = ["-i", path]
        aud_t = t_start - skew
        if aud_t > 0.0005:
            args += ["-ss", "{:.4f}".format(aud_t)]
        elif aud_t < -0.0005:
            # The window opens before the audio does: pad with silence.
            args += ["-af", "adelay={:.2f}:all=1".format(-aud_t * 1000.0)]
    if t_dur is not None:
        args += ["-t", "{:.3f}".format(max(t_dur, 0.1))]
    return args

# ── Script parser ───────────────────────────────────────────────────────────────────────────
#
# Bracketed syntax (the only supported form):
#
#     [PART Cold Open]                         ← section header
#
#     [VO PART_0_NARRATOR]                     ← VO block
#     Standard prose.  Blank lines preserved as paragraph breaks.
#
#     [JENA 00:01:23-00:01:45]                 ← interview pull
#     Quote text.  Blank lines preserved.
#
#     Second paragraph still in the same pull.
#
# Token names: uppercase letters, digits, underscores.  The optional
#     [TOKENS]
#     JENA
#     DON
#     [/TOKENS]
# block at the top of the script enumerates them; otherwise tokens are
# auto-registered the first time they appear in a `[…]` header.
#
# Comments: anything from a whitespace-prefixed `//` to end of line is ignored.

# Header line, three forms:
#   [PART <name>]
#   [VO <id>]
#   [<TOKEN> HH:MM:SS-HH:MM:SS]
_HEADER_RE = re.compile(
    r"^\[(?:"
    r"(?:PART\s+(?P<part>[^\]]+))"
    r"|(?:VO\s+(?P<void>[A-Z0-9_]+))"
    r"|(?P<tok>[A-Z][A-Z0-9_]*)\s+"
    r"(?P<intc>\d{1,2}:\d{2}:\d{2})-(?P<outtc>\d{1,2}:\d{2}:\d{2})"
    r")\]$"
)


def _strip_comment(s):
    """Strip a trailing '// comment' but only when '//' is at line start
    or preceded by whitespace (so URLs containing // survive)."""
    if "//" not in s:
        return s
    idx = 0
    while True:
        k = s.find("//", idx)
        if k < 0:
            return s
        if k == 0 or s[k - 1].isspace():
            return s[:k].rstrip()
        idx = k + 2


def _normalize_tc(s):
    """Pad single-digit hours: '1:23:45' → '01:23:45'."""
    parts = s.split(":")
    if len(parts) == 3 and len(parts[0]) == 1:
        return "0" + s
    return s


def parse_script(text):
    """Parse a script in the bracketed syntax.

    Returns:
      tokens   : [str]
      parts    : [{index, name}]
      pulls    : [{order, token, in_tc, out_tc, in_seconds, out_seconds,
                   part_index, quote_text, quote_paragraphs, gap_after}]
      vo_blocks: [{order, id, part_index, text, paragraphs, gap_after}]
      doc_title: str
      warnings : [str]
    """
    tokens, parts, pulls, vo_blocks, warnings = [], [], [], [], []

    # Doc title: first non-empty, non-header, non-comment, non-[TOKENS] line.
    doc_title = "PostBridge Episode"
    for ln in text.split("\n"):
        s = ln.strip()
        if not s:
            continue
        if s.startswith("//"):
            continue
        if s.startswith("[") and s.endswith("]"):
            continue
        if s.upper() in ("[TOKENS]", "[/TOKENS]"):
            continue
        doc_title = s
        break

    # Optional [TOKENS] … [/TOKENS] block (case-insensitive)
    m = re.search(r"\[TOKENS\](.*?)\[/TOKENS\]", text,
                   re.DOTALL | re.IGNORECASE)
    if m:
        for ln in m.group(1).split("\n"):
            clean = _strip_comment(ln).strip().strip("[]")
            tm = re.match(r"^([A-Z][A-Z0-9_]*)$", clean)
            if tm and tm.group(1) not in tokens:
                tokens.append(tm.group(1))

    lines    = text.split("\n")
    cur_part = 0
    order    = 1
    seen     = set()
    i        = 0
    n        = len(lines)

    def _collect_paragraphs(start):
        paragraphs = []
        cur_lines  = []
        last_blank = False
        j          = start
        while j < n:
            raw      = lines[j]
            stripped = _strip_comment(raw).strip()
            # Header at column 0 ends the block
            if raw and not raw[:1].isspace() and _HEADER_RE.match(stripped):
                break
            if not stripped:
                if cur_lines:
                    paragraphs.append(" ".join(cur_lines))
                    cur_lines = []
                last_blank = True
                j += 1
                continue
            last_blank = False
            cur_lines.append(stripped)
            j += 1
        if cur_lines:
            paragraphs.append(" ".join(cur_lines))
        return paragraphs, j, last_blank

    while i < n:
        raw = lines[i]
        ln  = _strip_comment(raw).strip()

        # Headers must be at column 0
        if not ln or (raw and raw[:1].isspace()):
            i += 1
            continue

        m = _HEADER_RE.match(ln)
        if not m:
            i += 1
            continue

        # ── PART ─────────────────────────────────────────────────────────
        if m.group("part") is not None:
            cur_part += 1
            parts.append({"index": cur_part, "name": m.group("part").strip()})
            i += 1
            continue

        # ── VO ───────────────────────────────────────────────────────────
        if m.group("void") is not None:
            vo_id = m.group("void")
            paragraphs, end_i, last_blank = _collect_paragraphs(i + 1)
            i = end_i
            text_joined = " ... ".join(p for p in paragraphs if p).strip()
            if text_joined:
                _next_ln  = lines[i].strip() if i < n else ""
                gap_after = last_blank or not _HEADER_RE.match(_next_ln)
                vo_blocks.append({
                    "order":      order,
                    "id":         vo_id,
                    "part_index": cur_part,
                    "text":       text_joined,
                    "paragraphs": list(paragraphs),
                    "gap_after":  gap_after,
                })
                order += 1
            continue

        # ── PULL ─────────────────────────────────────────────────────────
        tok    = m.group("tok")
        in_tc  = _normalize_tc(m.group("intc"))
        out_tc = _normalize_tc(m.group("outtc"))
        try:
            in_s  = tc_secs(in_tc)
            out_s = tc_secs(out_tc)
        except Exception:
            warnings.append("Bad timecodes in pull header: {}".format(ln))
            i += 1
            continue

        key = (tok, in_s, out_s)
        if key in seen:
            i += 1
            continue
        seen.add(key)
        if tok not in tokens:
            tokens.append(tok)

        paragraphs, end_i, last_blank = _collect_paragraphs(i + 1)
        i = end_i

        # Strip surrounding quote chars from each paragraph
        cleaned = []
        for p in paragraphs:
            p2 = p.strip().strip("“”‘’\"\'")
            if p2:
                cleaned.append(p2)
        quote_text = " ... ".join(cleaned).strip()

        _next_ln  = lines[i].strip() if i < n else ""
        gap_after = last_blank or not _HEADER_RE.match(_next_ln)
        pulls.append({
            "order":            order,
            "token":            tok,
            "in_tc":            in_tc,
            "out_tc":           out_tc,
            "in_seconds":       in_s,
            "out_seconds":      out_s,
            "part_index":       cur_part,
            "quote_text":       quote_text,
            "quote_paragraphs": cleaned,
            "gap_after":        gap_after,
        })
        order += 1

    if vo_blocks and not parts:
        parts.append({"index": 1, "name": "Episode VO"})
        for vb in vo_blocks:
            vb["part_index"] = 1

    return tokens, parts, pulls, vo_blocks, doc_title, warnings


# ── Pull Quotes session discovery ───────────────────────────────────────────────────────────────────
def discover_pq_sessions(script_path, max_walk_up=6):
    """Locate Pull Quotes session JSONs in the script's project folder.

    Walks UP from `script_path` looking for a project root — a directory
    that has both `02_MEDIA` and `03_AUDIO` siblings (the editorial
    template marker).  Once found, recursively globs for
    `*.pb_session.json` and returns a dict of `{token: abs_path}`.

    If the project root isn't recognised, scans the script's containing
    folder + parents up to `max_walk_up` levels.

    Duplicate-token sessions are listed under the special key
    `__warnings__` in the returned dict so the caller can surface them.
    """
    if not script_path or not os.path.isfile(script_path):
        return {}

    def _is_project_root(d):
        return (os.path.isdir(os.path.join(d, "02_MEDIA"))
                and os.path.isdir(os.path.join(d, "03_AUDIO")))

    cur = os.path.dirname(os.path.abspath(script_path))
    project_root = None
    for _ in range(max_walk_up):
        if _is_project_root(cur):
            project_root = cur
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent

    scan_root = project_root or os.path.dirname(os.path.abspath(script_path))
    result   = {}
    warnings = []
    episode_files = []   # .pb_episode.json paths discovered during walk

    def _register(full, source):
        """Load a candidate session file, register by token if valid.
        `source` is a short label for warning messages (walk / episode)."""
        try:
            with open(full, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        if data.get("workflow") != "interview_session":
            return
        tok = data.get("token")
        if not tok:
            return
        if tok in result and result[tok] != full:
            warnings.append(
                "Duplicate session for token '{}' [{}]: {} vs {}".format(
                    tok, source, result[tok], full))
            return
        result[tok] = full

    for dirpath, dirnames, filenames in os.walk(scan_root):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".")
                       and d.lower() not in
                       ("__pycache__", "node_modules", "build", "dist")]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if fn.endswith(".pb_session.json"):
                _register(full, "walk")
            elif fn.endswith(".pb_episode.json"):
                episode_files.append(full)

    # ── .pb_episode.json awareness ────────────────────────────────────
    # An Episode Project is an authoritative list of session paths, but
    # those paths are absolute (written by the machine that made the
    # project — usually not the current user's).  So for each entry
    # that fails to resolve, try relocating it to the episode file's
    # own directory (Jordan's convention: drop the session next to
    # the raw audio, next to the episode file).  This is the
    # "belt-and-braces" mechanism next to the walker so users with a
    # curated episode project don't depend on directory layout to be
    # discoverable.
    for ep_path in episode_files:
        try:
            with open(ep_path, encoding="utf-8") as f:
                ep = json.load(f)
        except Exception:
            continue
        ep_dir = os.path.dirname(ep_path)
        for sp in ep.get("sessions", []) or []:
            if not sp:
                continue
            _cand = sp if os.path.isfile(sp) else None
            if _cand is None:
                # Relocate: try the episode file's directory first.
                _local = os.path.join(ep_dir, os.path.basename(sp))
                if os.path.isfile(_local):
                    _cand = _local
            if _cand is None:
                warnings.append(
                    "Episode session not found (nor relocatable): {}".format(sp))
                continue
            _register(_cand, "episode")

    if warnings:
        result["__warnings__"] = warnings
    return result


# ── Pro Tools Session Text parser ──────────────────────────────────────────────
def parse_pt_session_text(text):
    """
    Parse a Pro Tools 'Export Session Text' file.
    """
    lines = text.split("\n")
    result = {
        "session_name": "",
        "sample_rate":  48000,
        "fps":          30.0,
        "tracks":       [],
    }

    i = 0
    # ── Parse header ──────────────────────────────────────────────────────────
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("SESSION NAME:"):
            result["session_name"] = line.split(":", 1)[1].strip()
        elif line.startswith("SAMPLE RATE:"):
            try: result["sample_rate"] = int(float(line.split(":", 1)[1].strip()))
            except: pass
        elif line.startswith("TIMECODE FORMAT:"):
            tc_fmt = line.split(":", 1)[1].strip()
            try: result["fps"] = float(tc_fmt.split()[0])
            except: pass
        elif line.startswith("TRACK NAME:"):
            break
        i += 1

    # ── Parse track EDLs ──────────────────────────────────────────────────────
    while i < len(lines):
        line = lines[i].strip()
        if not line.startswith("TRACK NAME:"):
            if "M A R K E R S" in line:
                break
            i += 1
            continue

        track_name = line.split(":", 1)[1].strip()
        track = {"name": track_name, "clips": []}
        i += 1

        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("CHANNEL"):
                i += 1
                break
            i += 1

        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue
            if line.startswith("TRACK NAME:"):
                break
            if "M A R K E R S" in line:
                break
            parts = line.split("\t")
            parts = [p.strip() for p in parts if p.strip()]
            if len(parts) >= 6:
                try:
                    ch    = int(parts[0])
                    event = int(parts[1])
                    clip  = parts[2]
                    start = parts[3]
                    end   = parts[4]
                    dur   = parts[5]
                    state = parts[6] if len(parts) > 6 else "Unmuted"
                    track["clips"].append({
                        "channel":   ch,
                        "event":     event,
                        "clip_name": clip,
                        "start_tc":  start,
                        "end_tc":    end,
                        "duration":  dur,
                        "state":     state,
                    })
                except (ValueError, IndexError):
                    pass
            i += 1

        if track["clips"]:
            result["tracks"].append(track)

    return result

def pt_tc_to_secs(tc_str, fps=30.0):
    """
    Convert PT timecode string 'HH:MM:SS:FF.sub' to seconds.
    """
    tc_str = tc_str.strip()
    main, _, sub = tc_str.partition(".")
    parts = main.split(":")
    if len(parts) == 4:
        h, m, s, f = [int(p) for p in parts]
        secs = h * 3600 + m * 60 + s + f / fps
        if sub:
            try: secs += int(sub) / (fps * 100)
            except: pass
        return secs
    elif len(parts) == 3:
        h, m, s = [int(p) for p in parts]
        return h * 3600 + m * 60 + s
    return 0.0

def dedupe_pt_tracks(parsed, channel=1):
    """
    De-duplicate stereo tracks: keep only channel 1 clips.
    """
    fps = parsed.get("fps", 30.0)
    out = []
    for track in parsed["tracks"]:
        for clip in track["clips"]:
            if clip["channel"] != channel:
                continue
            out.append({
                "track_name": track["name"],
                "clip_name":  clip["clip_name"],
                "start_secs": pt_tc_to_secs(clip["start_tc"], fps),
                "end_secs":   pt_tc_to_secs(clip["end_tc"],   fps),
                "dur_secs":   pt_tc_to_secs(clip["duration"],  fps),
                "state":      clip["state"],
            })
    return out

def match_pt_clip_to_media(clip_name, media_paths):
    """
    Match a PT clip name back to a media file in the pool.
    """
    def _normalize(name):
        name = os.path.splitext(name)[0]
        name = re.sub(r'\.[LR]$', '', name)
        name = re.sub(r'-Norm_\d+(-\d+)?$', '', name)
        name = re.sub(r'-\d{2}$', '', name)
        return name.lower().strip()

    clip_base = _normalize(clip_name)

    best_path  = None
    best_score = 0.0

    for path in media_paths:
        fn = os.path.splitext(basename(path))[0].lower().strip()
        if fn in clip_base or clip_base in fn:
            score = min(len(fn), len(clip_base)) / max(len(fn), len(clip_base))
            if score > best_score:
                best_score = score
                best_path  = path
        else:
            fn_words   = set(re.findall(r'[a-z0-9]+', fn))
            clip_words = set(re.findall(r'[a-z0-9]+', clip_base))
            if fn_words and clip_words:
                overlap = len(fn_words & clip_words) / max(len(fn_words), len(clip_words))
                if overlap > best_score:
                    best_score = overlap
                    best_path  = path

    return best_path if best_score > 0.3 else None


def match_source_to_video(source_base, video_paths):
    """
    Match an AAF source base name to the best video file in the pool.

    Uses two complementary strategies:
    • For short video filenames (≤3 significant tokens): coverage —
      what fraction of the video filename's tokens appear in the source name.
      Handles e.g. '10543.m4v' matching '2025-05-21~...~10543'.
    • For longer video filenames: Jaccard word overlap (same as
      match_pt_clip_to_media), good for descriptive names like 'Eddie_Alday'.

    Significant tokens are:
    • Any alphanumeric token of 4+ characters
    • Pure numeric tokens (1, 2, 3 …) — distinguishes "Take 1" from "Take 2"
    • Roman-numeral tokens i–xii — distinguishes "Part I" from "Part III"

    Returns the best matching path, or None if no confident match found.
    """
    # Standalone roman numerals that are worth distinguishing as ordinals.
    # Single-char 'i', 'v', 'x' are included because in file names they almost
    # always appear as ordinals ("Part I", "Episode V") rather than as letters.
    _ROMAN = frozenset({
        'i', 'ii', 'iii', 'iv', 'v', 'vi', 'vii', 'viii', 'ix',
        'x', 'xi', 'xii',
    })

    def _sig_words(text):
        """Lowercase alphanumeric tokens that are 4+ chars, pure digits, or roman numerals."""
        result = set()
        for w in re.findall(r'[a-z0-9]+', text.lower()):
            if len(w) >= 4 or w.isdigit() or w in _ROMAN:
                result.add(w)
        return result

    src_sig = _sig_words(source_base)
    if not src_sig:
        return None

    best_path  = None
    best_score = 0.0

    for path in video_paths:
        fn      = os.path.splitext(basename(path))[0]
        fn_sig  = _sig_words(fn)
        if not fn_sig:
            continue

        overlap  = len(fn_sig & src_sig)
        # Coverage: fraction of the video filename covered by the source.
        # Favoured when the video filename is short (e.g. a 5-digit clip ID).
        coverage = overlap / len(fn_sig)
        # Jaccard: symmetric overlap, better for longer descriptive names.
        jaccard  = overlap / max(len(fn_sig), len(src_sig))

        score = coverage if len(fn_sig) <= 3 else jaccard

        if score > best_score:
            best_score = score
            best_path  = path

    # Coverage threshold is 0.5 (at least half the video name tokens matched);
    # Jaccard threshold stays at 0.3 for longer names.
    return best_path if best_score >= 0.5 else None


def _event_diff_offset(video_path, audio_path, probe_duration=300.0,
                        start_offset=0.0, max_lag_s=30.0):
    """Onset-event time-difference histogram sync.

    Detect speech-onset events (rising energy edges) in each signal,
    then histogram the time difference of every cross-pair within
    ±max_lag.  The true offset accumulates a vote from every matching
    onset pair across the whole file, so it forms a dominant peak even
    when the two mics have very different dynamics (e.g. a loud camera
    mic vs a quiet VO mic) — the case where amplitude cross-correlation
    fails because it multiplies the two envelopes and is dominated by
    the amplitude mismatch.

    This is a fingerprinting-style alignment: it uses onset TIMES only,
    discarding amplitude, which is exactly why it is robust to the
    mic-dynamics differences that defeat the envelope detector.

    Validated against 10 ground-truth files across 4 different shoots
    (offsets −1.6 s … −5.6 s): rank-1 with a ≈2× vote margin on every
    file and every parameter setting tested, including 5 cases the
    envelope detector got flatly wrong.

    Returns ``(T_seconds, margin, votes)`` — margin is the dominant
    peak's vote count divided by the next non-adjacent peak's (a
    confidence proxy; ≥1.5 means a clear winner) — or ``None`` on any
    failure.  Sign convention matches the module: positive T = video
    leads audio.
    """
    try:
        import numpy as np
    except Exception:
        return None

    SR  = 8000
    HZ  = 50
    HOP = SR // HZ          # 160 samples = 20 ms frames
    THR = 88               # onset percentile gate (robust across 80–90)

    def _grab(path):
        with tempfile.NamedTemporaryFile(suffix=".raw", delete=False) as _fh:
            tmp = _fh.name
        try:
            cmd = (_ffmpeg_cmd() + ["-y", "-v", "quiet"]
                   + audio_read_args(path, start_offset, probe_duration)
                   + ["-ac", "1", "-ar", str(SR), "-f", "f32le", tmp])
            r = run_hidden(cmd, capture_output=True, timeout=120)
            if r.returncode != 0:
                return None
            with open(tmp, "rb") as _f:
                raw = _f.read()
            if not raw:
                return None
            return np.frombuffer(raw, dtype=np.float32).copy()
        except Exception:
            return None
        finally:
            try: os.unlink(tmp)
            except Exception: pass

    def _onset_times(sig):
        n = (len(sig) // HOP) * HOP
        if n < HOP * 8:
            return None
        rms = np.sqrt(np.mean(sig[:n].reshape(-1, HOP) ** 2, axis=1))
        # Onset strength = positive first difference of the RMS envelope
        # (energy rising = a speech attack after a pause).
        diff = np.diff(rms, prepend=rms[0])
        onset = np.maximum(0.0, diff)
        if np.max(onset) < 1e-9:
            return None
        thr = np.percentile(onset, THR)
        return np.where(onset > thr)[0] / float(HZ)

    try:
        sig_v = _grab(video_path)
        sig_a = _grab(audio_path)
        if sig_v is None or sig_a is None:
            return None
        tv = _onset_times(sig_v)
        ta = _onset_times(sig_a)
        if tv is None or ta is None or len(tv) < 8 or len(ta) < 8:
            return None
        # Pairwise time-difference voting (positive = video onset later
        # than audio onset = video leads).  Vectorised per video event.
        diffs = []
        for t in tv:
            d = t - ta
            d = d[np.abs(d) <= max_lag_s]
            if len(d):
                diffs.extend(d.tolist())
        if len(diffs) < 20:
            return None
        diffs = np.array(diffs)
        bins = np.arange(-max_lag_s, max_lag_s + 0.1, 0.1)
        hist, edges = np.histogram(diffs, bins=bins)
        order = np.argsort(-hist)
        top_i = int(order[0])
        top_c = (edges[top_i] + edges[top_i + 1]) / 2.0
        top_v = int(hist[top_i])
        # Margin = top peak vs the strongest peak at least 0.5 s away.
        second = 0
        for i in order[1:]:
            c = (edges[i] + edges[i + 1]) / 2.0
            if abs(c - top_c) >= 0.5:
                second = int(hist[i])
                break
        margin = (top_v / second) if second > 0 else 99.0
        return float(round(top_c, 3)), float(round(margin, 3)), top_v
    except Exception:
        return None


def detect_sync_offset(video_path, audio_path, probe_duration=300.0,
                        sample_rate=8000, start_offset=0.0,
                        n_probes=3, return_candidates=False,
                        has_slate=False):
    """Multi-window sync detection wrapper.

    Real-world video/audio files have setup/teardown noise at the head
    and tail (mic checks, slating, room handling, gear bumps).  Probing
    only the first 300 s — the previous behaviour — drops false peaks
    into the cross-correlation that are uncorrelated with the true
    speech rhythm, and the algorithm commits to whichever one wins.

    This wrapper picks N evenly-spaced probe positions through the file,
    runs the full multi-stage detector at each, and arbitrates by
    consistency: the true offset is the same regardless of where in the
    file you measure, false peaks aren't.

    n_probes=1 reproduces the legacy single-window behaviour (used for
    short files or when explicit start_offset is supplied).

    When ``return_candidates`` is True the function returns a 3-tuple
    ``(T, conf, candidates)``.  ``candidates`` is a list of up to ~5
    ``(T_alt, conf_alt)`` tuples representing runner-up sync hypotheses
    surfaced by Stage 1 — used by the sync-preview dialog to let the
    user audition alternatives when the auto pick is wrong.
    """

    def _augment(T, conf, alts):
        """Cross-check the envelope detector's pick against the onset
        event-difference histogram and correct it when they disagree
        and the histogram has a clear winner.

        Decision (validated end-to-end on 8 ground-truth files):
          • agree within 0.5 s  → keep the envelope pick verbatim
            (its sub-ms Stage-3 value is more precise than the
            histogram's 0.1 s bins) — zero change on working syncs.
          • disagree + margin ≥ 1.5 → trust the histogram: refine its
            peak to sub-ms via the Stage-3 verifier, promote it to the
            primary, and demote the old envelope pick to an audition
            candidate.  This is exactly the failure mode where the
            envelope detector locks a spurious peak.
          • disagree + weak margin → leave the pick alone but surface
            the histogram peak as the first audition candidate.

        Cost note: the cross-check adds two ffmpeg PCM decodes per
        sync even when the detectors agree.  A confidence gate (skip
        when conf ≥ 0.95) was evaluated and REJECTED against the full
        sync_corrections.jsonl dataset: wrong auto-picks exist at
        final_conf = 1.0 (Blyth Part 2, Dustin Intro/Part 5), so no
        confidence level safely exempts the envelope detector from
        the cross-check.  The decode cost is the price of the
        accuracy win — do not re-add a gate without new evidence.
        """
        try:
            ev = _event_diff_offset(video_path, audio_path,
                                     probe_duration=probe_duration,
                                     start_offset=start_offset)
        except Exception:
            ev = None
        if not ev:
            return T, conf, alts
        T_ev, margin, _votes = ev
        alts = list(alts or [])

        if abs(T_ev - T) <= 0.5:
            return T, conf, alts            # agreement — no change

        if margin >= 1.5:
            # Refine the 0.1 s-resolution histogram peak to sub-sample
            # accuracy with the existing precision verifier, guarding
            # against it wandering to a different nearby peak.
            T_ref = T_ev
            try:
                _tr, _vc = verify_sync_at_offset(
                    video_path, audio_path, T_ev)
                if abs(_tr - T_ev) <= 0.5:
                    T_ref = _tr
            except Exception:
                pass
            # Demote the old envelope pick to a (high-priority) candidate.
            if T != 0.0 and not any(abs(T - x[0]) <= 0.5 for x in alts):
                alts.insert(0, (round(T, 4), round(min(1.0, conf), 4)))
            new_conf = min(0.97, 0.80 + 0.05 * min(margin, 3.0))
            return round(T_ref, 6), round(new_conf, 4), alts[:6]

        # Weak margin: surface but don't override.
        if not any(abs(T_ev - x[0]) <= 0.5 for x in alts):
            alts.insert(0, (round(T_ev, 4), round(min(0.6, 0.4 * margin), 4)))
        return T, conf, alts[:6]

    def _rerank(T, conf, alts):
        """Re-rank the pick against its runner-up candidates with the
        Stage-3 precision verifier (2 ms envelope, ±0.5 s).

        The 0.1 s envelope stages can't tell apart offsets that differ by
        a whole period of a repeating loudness pattern (steady music,
        a metronomic delivery): each alias scores about the same, and the
        true offset can end up as a runner-up.  The 2 ms fine structure
        only lines up at the true offset, so a candidate the verifier
        clearly prefers takes over and the old pick becomes a candidate.
        """
        alts = list(alts or [])
        if not alts:
            return T, conf, alts
        try:
            _t0, v0 = verify_sync_at_offset(video_path, audio_path, T)
        except Exception:
            return T, conf, alts
        best = None
        for t_alt, _c in alts[:4]:
            if abs(t_alt - T) <= 0.5:
                continue
            try:
                tv, vc = verify_sync_at_offset(video_path, audio_path, t_alt)
            except Exception:
                continue
            if abs(tv - t_alt) > 0.5:
                continue
            if best is None or vc > best[1]:
                best = (tv, vc, t_alt)
        if best is None or best[1] < 0.6 or best[1] - v0 < 0.3:
            return T, conf, alts
        tv, vc, t_alt = best
        alts = [x for x in alts if abs(x[0] - t_alt) > 0.5]
        alts.insert(0, (round(T, 4), round(min(1.0, conf), 4)))
        return round(tv, 6), round(max(conf, min(0.97, vc)), 4), alts[:6]

    def _emit(T, conf, alts):
        T, conf, alts = _augment(T, conf, alts)
        T, conf, alts = _rerank(T, conf, alts)
        if return_candidates:
            return T, conf, alts
        return T, conf

    # Legacy single-window path — preserve the original behaviour when
    # the caller specifies an explicit start_offset or asks for one probe.
    if n_probes <= 1 or start_offset != 0.0:
        T, conf, alts = _detect_sync_offset_at(
            video_path, audio_path,
            probe_duration=probe_duration,
            sample_rate=sample_rate,
            start_offset=start_offset,
            has_slate=has_slate)
        return _emit(T, conf, alts)

    # Probe file duration so we can place windows away from head/tail.
    # Lazy import to avoid circular dependency on engines.
    try:
        from engines import get_media_duration as _gmd
        total = _gmd(audio_path) or 0.0
    except Exception:
        total = 0.0

    # If we can't measure the file or it's too short for multi-window
    # probing to add value, fall back to the legacy single-window run.
    if total < probe_duration * 1.5:
        T, conf, alts = _detect_sync_offset_at(
            video_path, audio_path,
            probe_duration=probe_duration,
            sample_rate=sample_rate,
            start_offset=0.0,
            has_slate=has_slate)
        return _emit(T, conf, alts)

    # Probe centres at ~25 %, ~50 %, ~75 % of the file.  Each probe
    # consumes probe_duration seconds; clamp so we don't fall off the end.
    fractions = [0.25, 0.50, 0.75][:max(1, n_probes)]
    probe_starts = []
    for frac in fractions:
        centre = total * frac
        s = max(0.0, centre - probe_duration / 2.0)
        s = min(s, max(0.0, total - probe_duration))
        probe_starts.append(s)
    # De-dupe to avoid running the same window twice on short files.
    probe_starts = sorted(set(round(s, 1) for s in probe_starts))
    if len(probe_starts) < 2:
        T, conf, alts = _detect_sync_offset_at(
            video_path, audio_path,
            probe_duration=probe_duration,
            sample_rate=sample_rate,
            start_offset=probe_starts[0] if probe_starts else 0.0,
            has_slate=has_slate)
        return _emit(T, conf, alts)

    # Run probes sequentially — each call is already CPU-bound on FFT
    # work; parallelising them would just fight for the same cores and
    # produce the same wall-clock with worse contention.
    results = []          # list of (T, conf, start, alts)
    for s in probe_starts:
        try:
            T, conf, alts = _detect_sync_offset_at(
                video_path, audio_path,
                probe_duration=probe_duration,
                sample_rate=sample_rate,
                start_offset=s,
                has_slate=has_slate)
            results.append((T, conf, s, alts))
        except Exception:
            pass

    # A probe that measured nothing (unreadable audio, failed extraction)
    # comes back as (0.0, conf 0.0).  It must not vote: three failed
    # probes all "agree" on 0.000 s, and the agreement floor below then
    # promoted that to 100 % confidence — every source on a Mac whose
    # ffmpeg could not be found read "✓ 0.000s (100%)".
    results = [r for r in results if r[1] > 0.0]

    if not results:
        return _emit(0.0, 0.0, [])
    if len(results) == 1:
        return _emit(results[0][0], results[0][1], results[0][3])

    # Cluster offsets that agree within ±AGREE_S.  True offset → every
    # probe lands inside the same cluster.  False peak → probes scatter
    # across the file's noisy regions.
    AGREE_S = 0.5
    best_cluster = []
    for i, ri in enumerate(results):
        cluster = [ri]
        for j, rj in enumerate(results):
            if i == j:
                continue
            if abs(rj[0] - ri[0]) <= AGREE_S:
                cluster.append(rj)
        if len(cluster) > len(best_cluster):
            best_cluster = cluster

    # Build the merged candidate list for audition: each probe's main
    # answer (if not the cluster winner) PLUS each probe's alternatives.
    # Sort by confidence and dedup within ±0.5 s.
    if len(best_cluster) >= 2:
        total_w = sum(r[1] for r in best_cluster) or float(len(best_cluster))
        avg_T = sum(r[0] * (r[1] or 1.0) for r in best_cluster) / total_w
        max_inner = max(r[1] for r in best_cluster)
        agree_floor = 0.92 if len(best_cluster) == 2 else 1.0
        final_conf = min(1.0, max(max_inner, agree_floor))
        final_T = round(avg_T, 6)
        final_conf = round(final_conf, 4)
    else:
        best = max(results, key=lambda r: r[1])
        final_T    = round(best[0], 6)
        final_conf = round(min(best[1], 0.45), 4)

    # Aggregate alternatives across all probes.  Include each probe's
    # own main answer as a candidate too (it might be the correct one
    # the wrapper rejected via clustering).
    _DEDUP_S = 0.5
    merged = []
    pool = []
    for T_p, conf_p, _s, alts_p in results:
        pool.append((T_p, conf_p))
        pool.extend(alts_p or [])
    pool.sort(key=lambda x: -x[1])
    for t, c in pool:
        if abs(t - final_T) <= _DEDUP_S:
            continue
        if any(abs(t - x[0]) <= _DEDUP_S for x in merged):
            continue
        merged.append((round(t, 4), round(min(1.0, c), 4)))
        if len(merged) >= 5:
            break

    return _emit(final_T, final_conf, merged)


def _detect_sync_offset_at(video_path, audio_path, probe_duration=300.0,
                            sample_rate=8000, start_offset=0.0,
                            has_slate=False):
    """
    Hierarchical multi-scale sync detection.

    Returns (offset_secs: float, confidence: float 0..1)

    offset_secs  — add this to video src_in to get the aligned camera position.
                   Positive = camera rolled before DAW; negative = camera came in late.
    confidence   — 0..1; values ≥ 0.4 are reliable.

    Algorithm: three progressively finer stages, each narrowing the search
    window around the previous estimate.  Uses RMS-envelope cross-correlation
    at every stage so spectral differences between the camera mic and the DAW
    reference recording do not matter — only the amplitude pattern (talk /
    silence rhythm) needs to be similar, which it always is.

    Stage 1  –  1 Hz RMS envelope, full probe window  → accuracy ±0.5 s
    Stage 2  – 50 Hz RMS envelope, ±10 s search       → accuracy ±10 ms
    Stage 3  – 2 ms RMS envelope,  ±0.5 s search      → accuracy ±1 ms
    """
    try:
        import numpy as _np
    except ImportError:
        raise RuntimeError(
            "numpy is required for sync detection.\n"
            "Run:  pip install numpy")

    # ── Low-level helpers ──────────────────────────────────────────────────

    def _extract(path, t_start, t_dur, out_sr):
        """Extract mono float32 audio as numpy array; returns None on failure."""
        with tempfile.NamedTemporaryFile(suffix=".raw", delete=False) as tf:
            tmp = tf.name
        try:
            cmd = (_ffmpeg_cmd() + ["-y", "-v", "quiet"]
                   + audio_read_args(path, t_start, t_dur)
                   + ["-ac", "1",
                      "-ar", str(int(out_sr)),
                      "-f",  "f32le",
                      tmp])
            r = run_hidden(cmd, capture_output=True, timeout=120)
            if r.returncode != 0:
                return None
            with open(tmp, "rb") as _fh:
                raw = _fh.read()
            if not raw:
                return None
            data = _np.frombuffer(raw, dtype=_np.float32).copy()
            rms = float(_np.sqrt(_np.mean(data ** 2)))
            if rms > 1e-9:
                data /= rms
            return data
        except Exception:
            return None
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    def _extract_pair(path_a, t_a, path_b, t_b, t_dur, out_sr):
        """Run two _extract calls concurrently; returns (result_a, result_b).

        Both ffmpeg subprocesses are I/O-bound and independent, so running
        them in parallel threads roughly halves Stage-1 wall-clock time.
        """
        from concurrent.futures import ThreadPoolExecutor as _TPE2
        with _TPE2(max_workers=2) as _pex:
            _fa = _pex.submit(_extract, path_a, t_a, t_dur, out_sr)
            _fb = _pex.submit(_extract, path_b, t_b, t_dur, out_sr)
            return _fa.result(), _fb.result()

    def _peak_env(sig, win_samples, top_pct=20):
        """
        Peak-selective RMS envelope.

        Compute per-window RMS, then zero out every window below the
        (100 - top_pct) percentile — keeping only the loudest top_pct %
        of frames.  Zeroes are then zero-meaned and unit-std normalised.

        This is the numerical equivalent of what you do visually: ignore the
        noise floor, match only the loud events (speech bursts, transients).
        It is completely noise-floor independent because the threshold is
        derived from each signal's own peak distribution, not an absolute level.

        top_pct=15  → keep the loudest 15 % of windows  (coarse pass, high SNR)
        top_pct=25  → keep the loudest 25 %             (medium pass)
        top_pct=35  → keep the loudest 35 %             (fine pass, more events)
        """
        n = (len(sig) // win_samples) * win_samples
        if n < win_samples:
            return None
        rms = _np.sqrt(_np.mean(sig[:n].reshape(-1, win_samples) ** 2, axis=1))
        if len(rms) < 4:
            return None
        gate = float(_np.percentile(rms, 100 - top_pct))
        env  = _np.maximum(0.0, rms - gate)
        env -= _np.mean(env)
        std  = float(_np.std(env))
        if std < 1e-9:
            return None
        return env / std

    def _xcorr_bounded(a_sig, b_sig, max_lag_samp, phat=False):
        """
        Cross-correlate a_sig and b_sig.
        C[L] = sum_t  a_sig[t] * b_sig[t + L]

        Peaks at L means b_sig leads a_sig by L samples (positive = b ahead).
        Search restricted to |L| ≤ max_lag_samp to avoid false peaks.
        Returns (signed_lag_samples_float, confidence_0_to_1).
        Lag is a float thanks to parabolic sub-sample interpolation.

        phat=True applies GCC-PHAT weighting: the cross-spectrum is divided by
        its magnitude before the IFFT.  This whitens the frequency content so
        that dominant periodic components (e.g. speech rhythm harmonics) no
        longer bias the xcorr peak, yielding a sharper, less ambiguous result
        for signals that would otherwise produce multiple competing peaks.
        """
        na, nb = len(a_sig), len(b_sig)
        n   = na + nb - 1
        N   = 1 << int(_np.ceil(_np.log2(max(n, 1))))
        cross = _np.fft.rfft(b_sig, N) * _np.conj(_np.fft.rfft(a_sig, N))
        if phat:
            mag = _np.abs(cross)
            mag[mag < 1e-10] = 1e-10
            cross = cross / mag
        C   = _np.fft.irfft(cross, N)[:n]
        C_abs = _np.abs(C)

        # Restrict to ±max_lag in the circular-index layout:
        #   positive lags: C[0 .. max_lag]
        #   negative lags: C[n-max_lag .. n-1]
        ml = min(max_lag_samp, n // 2 - 1)
        pos_idx = list(range(0, ml + 1))
        neg_idx = list(range(n - ml, n)) if ml > 0 else []
        win_idx = pos_idx + neg_idx

        best_i  = int(max(win_idx, key=lambda i: C_abs[i]))
        lag_int = best_i if best_i <= n // 2 else best_i - n

        # Parabolic interpolation for sub-sample accuracy.
        # Fits a parabola through (best_i-1, best_i, best_i+1) and finds
        # the true peak fractional position, clamped to ±0.5 samples.
        lag = float(lag_int)
        if 0 < best_i < n - 1:
            y0 = float(C_abs[best_i - 1])
            y1 = float(C_abs[best_i])
            y2 = float(C_abs[best_i + 1])
            denom = y0 - 2.0 * y1 + y2
            if abs(denom) > 1e-12:
                frac = 0.5 * (y0 - y2) / denom
                lag += max(-0.5, min(0.5, frac))

        vals        = C_abs[_np.array(win_idx)]
        noise_mean  = float(_np.mean(vals))
        noise_std   = float(_np.std(vals))
        z           = (float(C_abs[best_i]) - noise_mean) / max(noise_std, 1e-9)
        conf        = min(1.0, z / 8.0)
        return lag, conf

    def _xcorr_top_n(a_sig, b_sig, max_lag_samp, n_peaks=3, min_gap=5,
                      phat=False):
        """
        Like _xcorr_bounded but returns the top-N (lag, conf) pairs,
        each at least min_gap lag-samples apart.  Peaks are found by
        repeatedly zeroing out the neighbourhood around each winner and
        scanning again; confidence is always relative to the same global
        noise floor so values are comparable across calls.

        Used for multi-candidate Stage 1: instead of committing to the
        single strongest 1 Hz peak, we keep the top 3 and let Stage 2
        arbitrate by running a separate ±20 s search from each.

        ``phat=True`` applies GCC-PHAT weighting: the cross-spectrum is
        normalised by its magnitude before inverse-FFT, whitening the
        result so impulsive events (claps, slate transients) produce
        sharp peaks instead of broad smears.  Slate-aware sync detection
        uses this.
        """
        na, nb = len(a_sig), len(b_sig)
        n  = na + nb - 1
        N  = 1 << int(_np.ceil(_np.log2(max(n, 1))))
        A = _np.fft.rfft(a_sig, N)
        B = _np.fft.rfft(b_sig, N)
        X = B * _np.conj(A)
        if phat:
            X = X / (_np.abs(X) + 1e-12)
        C  = _np.fft.irfft(X, N)[:n]
        C_abs = _np.abs(C)

        ml      = min(max_lag_samp, n // 2 - 1)
        pos_idx = _np.arange(0, ml + 1)
        neg_idx = _np.arange(n - ml, n) if ml > 0 else _np.array([], dtype=int)
        win_idx = _np.concatenate([pos_idx, neg_idx])

        # Noise floor: fixed across all peak iterations so confidences
        # are comparable (dividing by the same baseline each time).
        vals       = C_abs[win_idx]
        noise_mean = float(_np.mean(vals))
        noise_std  = float(_np.std(vals))

        C_work  = C_abs.copy()
        results = []
        win_lags = _np.where(win_idx <= n // 2,
                             win_idx.astype(int),
                             win_idx.astype(int) - n)   # signed lags for gap test

        for _ in range(n_peaks):
            remaining = C_work[win_idx]
            if float(_np.max(remaining)) < 1e-12:
                break

            local_best = int(_np.argmax(remaining))
            best_i     = int(win_idx[local_best])
            lag_int    = best_i if best_i <= n // 2 else best_i - n

            lag = float(lag_int)
            if 0 < best_i < n - 1:
                y0 = float(C_abs[best_i - 1])
                y1 = float(C_abs[best_i])
                y2 = float(C_abs[best_i + 1])
                denom = y0 - 2.0 * y1 + y2
                if abs(denom) > 1e-12:
                    lag += max(-0.5, min(0.5, 0.5 * (y0 - y2) / denom))

            z    = (float(C_abs[best_i]) - noise_mean) / max(noise_std, 1e-9)
            conf = min(1.0, z / 8.0)
            results.append((lag, conf))

            # Null out ±min_gap around this peak in lag-space so the next
            # iteration cannot re-select it or an immediate neighbour.
            mask = _np.abs(win_lags - lag_int) < min_gap
            C_work[win_idx[mask]] = 0.0

        return results if results else [(0.0, 0.0)]

    # ── Stage 1: two-pass coarse search ──────────────────────────────────
    #
    # WHY TWO PASSES
    # ──────────────
    # A single 50 Hz envelope over 300 s with ±150 s max_lag finds massive
    # false peaks at 22–146 s for interview recordings.  Speech periodicity
    # at 20 ms resolution produces many spurious correlation peaks spread
    # across the full ±150 s window; those distant false peaks outcompete
    # the true ±1–5 s offset every time.
    #
    # A single 5 Hz envelope (200 ms windows, 300 s probe, ±150 s) is
    # reliable for large offsets (>10 s) because the coarse windows average
    # out phoneme/syllable periodicity.  But a 2 s offset is only 10 samples
    # at 5 Hz — indistinguishable from zero or from the nearest speech-rhythm
    # peak.
    #
    # Solution: extract once, compute two envelopes, run two xcorrs.
    #
    #   Pass A — 50 Hz envelope, FIRST 60 s of audio only, ±30 s max_lag.
    #            60 s of speech has few repetitions, and the ±30 s cap
    #            eliminates the 22–146 s false peaks entirely.
    #            Overlap at the true ±3 s lag:  57/60 = 95 %.
    #            Overlap at max lag (±30 s):     30/60 = 50 %.
    #            → true peak dominates when real offset < 30 s.
    #
    #   Pass B — 5 Hz envelope, full 300 s probe, ±120 s max_lag.
    #            Same coarse search that worked well for large-offset
    #            productions (Blyth, etc.).  Blind for offsets < 5 s.
    #
    # Selection: if Pass A reports conf ≥ 0.30, use it (small-offset case).
    #            Otherwise fall back to Pass B (large-offset case).
    #
    # arg order in xcorr: audio (b_sig) first, video (a_sig) second.
    # C[L] = sum_t VIDEO[t+L] * AUDIO[t].  Peak at L>0 → VIDEO leads AUDIO
    # (camera started early) → T positive.

    _SR1_EXTR = 8000
    DUR1      = min(probe_duration, 300.0)

    # Single extraction — both passes reuse these raw arrays.
    raw_vid1, raw_aud1 = _extract_pair(video_path, start_offset,
                                        audio_path, start_offset,
                                        DUR1, _SR1_EXTR)
    if raw_vid1 is None or raw_aud1 is None:
        # 3-tuple, not 2 — every caller unpacks (T, conf, alts).  Returning
        # a bare (0.0, 0.0) here raised "not enough values to unpack" out of
        # detect_sync_offset, which the SYNC button surfaced as "⚠ error"
        # instead of the honest "no confident match" result this path means.
        return 0.0, 0.0, []

    # ── Pass A: 50 Hz, first 60 s, ±30 s ─────────────────────────────────
    _WIN_A   = _SR1_EXTR // 50          # 160 samples = 20 ms
    _DUR_A   = int(60.0 * _SR1_EXTR)   # first 60 s in raw samples
    _raw_v_a = raw_vid1[:_DUR_A]
    _raw_a_a = raw_aud1[:_DUR_A]
    # For slate-marked content the clapper transient is the dominant
    # sync signal — keep more of the loud frames so it survives the
    # percentile gate.  Otherwise stay on the standard top-20 % gate.
    _A_TOP   = 50 if has_slate else 20
    _env_v_a = _peak_env(_raw_v_a, _WIN_A, top_pct=_A_TOP) if len(_raw_v_a) >= _WIN_A else None
    _env_a_a = _peak_env(_raw_a_a, _WIN_A, top_pct=_A_TOP) if len(_raw_a_a) >= _WIN_A else None
    T1A, conf1A = 0.0, 0.0
    if _env_v_a is not None and _env_a_a is not None and len(_env_v_a) >= 4:
        _MAX_LAG_A  = int(30.0 * 50)              # ±30 s at 50 Hz = 1500 samples
        _MIN_GAP_A  = max(1, int(50 * 2))         # ≥2 s between candidates
        # GCC-PHAT whitens the cross-spectrum so impulsive events (clap
        # boards, sync slates) produce sharp peaks instead of broad
        # smears.  Standard speech rhythm correlation works better
        # without it, so only opt in when the user flagged a slate.
        _cands_a    = _xcorr_top_n(_env_a_a, _env_v_a, _MAX_LAG_A,
                                    n_peaks=3, min_gap=_MIN_GAP_A,
                                    phat=has_slate)
        T1A    = float(_cands_a[0][0]) / 50.0
        conf1A = _cands_a[0][1]

    # ── Pass B: 5 Hz, full 300 s, ±120 s ─────────────────────────────────
    _WIN_B  = _SR1_EXTR // 5            # 1600 samples = 200 ms
    _env_v_b = _peak_env(raw_vid1, _WIN_B, top_pct=15)
    _env_a_b = _peak_env(raw_aud1, _WIN_B, top_pct=15)
    T1B, conf1B = 0.0, 0.0
    if _env_v_b is not None and _env_a_b is not None and len(_env_v_b) >= 4:
        _MAX_LAG_B = min(int(120.0 * 5), len(_env_v_b) // 2)  # ±120 s at 5 Hz = 600 samp
        _MIN_GAP_B = max(1, int(5 * 5))                         # ≥5 s between candidates
        _cands_b   = _xcorr_top_n(_env_a_b, _env_v_b, _MAX_LAG_B,
                                   n_peaks=3, min_gap=_MIN_GAP_B)
        T1B    = float(_cands_b[0][0]) / 5.0
        conf1B = _cands_b[0][1]

    # Select pass
    if conf1A >= 0.30:
        T1, conf1 = T1A, conf1A
        _s1_pass  = "A"
    else:
        T1, conf1 = T1B, conf1B
        _s1_pass  = "B"

    # ── Stage 2: 50 Hz RMS envelope, ±5 s search around T1 ───────────────
    # Accuracy: ±10 ms.
    SR2      = 8000
    WIN2     = SR2 // 50       # 20 ms RMS windows → 50 effective Hz
    PROBE2   = 60.0            # seconds to extract
    SEARCH2  = 5.0             # ±5 s search (Stage 1 seed is ≤ 100 ms accurate)

    sr2_eff  = SR2 // WIN2                       # 50 Hz effective
    max_lag2 = int(SEARCH2 * sr2_eff)

    # When T1 is negative the video file started after the audio.
    # Anchor the extraction at video frame 0 and advance audio start by the
    # same amount so we're always correlating the same speech content.
    # (Clamping video to 0 while keeping audio at start_offset would align
    # mismatched windows and produce a garbage Stage-2 result.)
    _v2_ideal = start_offset + T1
    if _v2_ideal < 0.0:
        v_start2 = 0.0
        a_start2 = start_offset + (-_v2_ideal)   # skip audio that predates camera
    else:
        v_start2 = _v2_ideal
        a_start2 = start_offset

    # Run both Stage-2 extractions concurrently; they are independent.
    raw_a2, raw_v2 = _extract_pair(audio_path, a_start2,
                                    video_path, v_start2, PROBE2, SR2)

    T2, conf2 = T1, conf1   # fallback if extraction fails entirely
    T1_best   = T1
    if raw_a2 is not None and raw_v2 is not None:
        env_a2 = _peak_env(raw_a2, WIN2, top_pct=25)
        env_v2 = _peak_env(raw_v2, WIN2, top_pct=25)
        if env_a2 is not None and env_v2 is not None:
            lag2, conf2 = _xcorr_bounded(env_a2, env_v2, max_lag2)
            T2      = (v_start2 - a_start2) + float(lag2) / sr2_eff
            T1_best = T1

    # ── Stage 2M: Mirror – test -T1 to recover negative offsets ──────────
    # When Stage 1 returns a false positive T1 > 0 and the true offset is
    # negative (e.g. camera started late), anchoring Stage 2M at -T1 places
    # the true peak within ±SEARCH2M.
    # Proof: T2_M = (v_start2M − a_start2M) + L_residual = −|true_T|.
    # For large positive true offsets (e.g. Blyth +15 s), conf2M < conf2
    # because the true peak sits >20 s outside the search window → ignored.
    _used_mirror = False
    T2M, conf2M  = T2, 0.0
    if abs(T1) >= 0.5:
        T1_M      = -T1
        _v2M_ideal = start_offset + T1_M
        if _v2M_ideal < 0.0:
            v_start2M = 0.0
            a_start2M = start_offset + (-_v2M_ideal)
        else:
            v_start2M = _v2M_ideal
            a_start2M = start_offset
        # Adaptive Mirror search width: when Stage 2 returned a mediocre
        # confidence, the true offset is more likely to be far from -T1.
        # Widen the search window so we catch ≥-5 s true offsets that
        # would otherwise fall outside ±20 s of the mirror anchor.
        SEARCH2M  = 20.0 if conf2 >= 0.85 else 60.0
        max_lag2M = int(SEARCH2M * sr2_eff)
        raw_a2M, raw_v2M = _extract_pair(audio_path, a_start2M,
                                          video_path,  v_start2M, PROBE2, SR2)
        if raw_a2M is not None and raw_v2M is not None:
            env_a2M = _peak_env(raw_a2M, WIN2, top_pct=25)
            env_v2M = _peak_env(raw_v2M, WIN2, top_pct=25)
            if env_a2M is not None and env_v2M is not None:
                lag2M, conf2M = _xcorr_bounded(env_a2M, env_v2M, max_lag2M)
                T2M = (v_start2M - a_start2M) + float(lag2M) / sr2_eff
                # ── Same-sign check: mirror found original false peak again ──────
                # If T2M ≈ T1 in both sign and magnitude the wide search looped
                # back to the same spurious peak.  Retry with a tight ±5 s window
                # on the already-extracted signals; the true (negative) peak is
                # typically <1 s from the mirror anchor so it survives, while the
                # false peak (which is |T1| + |true_T| away) gets excluded.
                if conf2M > 0 and abs(T2M - T1) < 1.0:
                    max_lag2M_narrow = int(5.0 * sr2_eff)
                    lag2M_n, conf2M_n = _xcorr_bounded(env_a2M, env_v2M,
                                                        max_lag2M_narrow)
                    T2M_n = (v_start2M - a_start2M) + float(lag2M_n) / sr2_eff
                    # Accept narrow result only if it escapes the same-sign trap
                    if conf2M_n > 0 and abs(T2M_n - T1) >= 1.0:
                        T2M, conf2M = T2M_n, conf2M_n
                    else:
                        conf2M = 0.0   # mirror entirely failed, don't use it
                if conf2M > conf2:
                    T2           = T2M
                    conf2        = conf2M
                    T1_best      = T2M   # spread → 0 so t12 penalty vanishes
                    _used_mirror = True

    # ── Stage 3: 2 ms RMS envelope, ±0.5 s search around T2 ──────────────
    # Accuracy: ±1 ms (sub-frame).
    SR3      = 8000
    WIN3     = SR3 // 500      # 2 ms RMS windows → 500 effective Hz
    PROBE3   = 10.0            # seconds to extract
    SEARCH3  = 0.5             # ±0.5 s search

    # Anchor at 30 % into probe2 — settled speech, away from mic-handling noise
    # and setup chaos at the very start of the recording.  The beginning of the
    # file is the worst region for clean correlation: camera operators are still
    # getting rolling, talent is adjusting mics, and transients there are random
    # rather than matched between camera and VO recordings.
    # Same anchor logic as Stage 2: when T2 is negative advance audio start
    # rather than clamping video to 0.
    _a_start3_base = a_start2 + PROBE2 * 0.3
    _v3_ideal      = _a_start3_base + T2
    if _v3_ideal < 0.0:
        v_start3 = 0.0
        a_start3 = _a_start3_base + (-_v3_ideal)
    else:
        v_start3 = _v3_ideal
        a_start3 = _a_start3_base

    raw_a3, raw_v3 = _extract_pair(audio_path, a_start3,
                                    video_path,  v_start3, PROBE3, SR3)

    stage3_ran = False
    T3, conf3  = T2, conf2   # fallback
    if raw_a3 is not None and raw_v3 is not None:
        env_a3 = _peak_env(raw_a3, WIN3, top_pct=35)
        env_v3 = _peak_env(raw_v3, WIN3, top_pct=35)
        if env_a3 is not None and env_v3 is not None:
            sr3_eff   = SR3 // WIN3                      # 500 Hz effective
            max_lag3  = int(SEARCH3 * sr3_eff)
            lag3, conf3 = _xcorr_bounded(env_a3, env_v3, max_lag3)
            T3         = (v_start3 - a_start3) + float(lag3) / sr3_eff
            stage3_ran = True

    # ── Stage 3R: Rescue – Stage-3 anchored at T1_best ────────────────────
    # When the winning Stage-2 result still disagrees with its own Stage-1
    # seed by ≥ 3 s, Stage 2 likely latched onto a false peak inside its
    # ±20 s search window.  Run a fine Stage-3 check anchored at T1_best;
    # if that yields higher confidence than the current best, use it instead.
    _t12_spread_pre = abs(T2 - T1_best)
    rescue_ran  = False
    T3R         = T2
    conf3R_raw  = 0.0

    if _t12_spread_pre >= 3.0:
        _sr3r_eff  = SR3 // WIN3
        _max_lag3r = int(SEARCH3 * _sr3r_eff)
        # Anchor audio at the same 30 % point Stage 3 uses; anchor video on T1_best.
        # Apply the same negative-offset correction: advance audio rather than clamping.
        _a_start3r_base = a_start2 + PROBE2 * 0.3
        _v3r_ideal      = _a_start3r_base + T1_best
        if _v3r_ideal < 0.0:
            _v_start3r = 0.0
            _a_start3r = _a_start3r_base + (-_v3r_ideal)
        else:
            _v_start3r = _v3r_ideal
            _a_start3r = _a_start3r_base
        _raw_a3r, _raw_v3r = _extract_pair(audio_path, _a_start3r,
                                            video_path,  _v_start3r, PROBE3, SR3)
        if _raw_a3r is not None and _raw_v3r is not None:
            _env_a3r = _peak_env(_raw_a3r, WIN3, top_pct=35)
            _env_v3r = _peak_env(_raw_v3r, WIN3, top_pct=35)
            if _env_a3r is not None and _env_v3r is not None:
                _lag3r, _conf3r = _xcorr_bounded(_env_a3r, _env_v3r, _max_lag3r)
                T3R        = (_v_start3r - _a_start3r) + float(_lag3r) / _sr3r_eff
                conf3R_raw = _conf3r
                rescue_ran = True

    # ── Return finest reliable result ──────────────────────────────────────
    # Two independent confidence signals are combined:
    #
    # 1. T2/T3 SPREAD (cross-stage agreement within Stage 2's region)
    #    Stage 3 is anchored at T2 and searches ±0.5 s.  If it lands
    #    somewhere different, Stage 2's peak was ambiguous.
    #      spread < 50 ms  → agree = 1.0
    #      spread < 500 ms → linear 1.0 → 0.0
    #      spread ≥ 500 ms → agree = 0.0
    #
    # 2. T1/T2 SPREAD (cross-stage agreement between coarse and fine)
    #    Stage 1 (5 Hz, full probe) and Stage 2 (50 Hz, ±20 s window) are
    #    fully independent.  When they disagree by more than 1 s, at least
    #    one found a false peak — and T3 cannot arbitrate because it is
    #    anchored at T2.  Empirical data confirms: every wrong detection had
    #    |T1-T2| > 2 s; every clean detection had |T1-T2| < 1 s.
    #      |T1-T2| < 1 s → t12_factor = 1.0   (no penalty)
    #      |T1-T2| < 5 s → linear 1.0 → 0.0
    #      |T1-T2| ≥ 5 s → t12_factor = 0.0   (maximum penalty)
    #
    #    The t12_factor scales the base confidence: multiplier = 0.35 + 0.65×f
    #    so even a fully-penalised result stays above 0 (avoids hiding
    #    detections that are still worth user inspection).

    t12_spread = abs(T2 - T1_best)
    if t12_spread < 1.0:
        t12_factor = 1.0
    elif t12_spread < 5.0:
        t12_factor = 1.0 - (t12_spread - 1.0) / 4.0
    else:
        t12_factor = 0.0
    t12_mult = 0.35 + 0.65 * t12_factor   # range [0.35, 1.0]

    if stage3_ran and conf3 >= 0.05:
        spread = abs(T3 - T2)
        if spread < 0.05:
            agree = 1.0
        elif spread < 0.5:
            agree = 1.0 - (spread - 0.05) / 0.45
        else:
            agree = 0.0
        base_conf  = agree * 0.70 + conf3 * 0.30
        t3_conf    = round(min(1.0, base_conf * t12_mult), 4)
        # Only accept Stage 3 result if it actually improves on Stage 2.
        # When Stage 3 drifts far from T2 with low conf3, T2 (which has
        # conf2 * t12_mult applied) can be a more reliable answer.
        t2_conf    = round(min(1.0, conf2 * t12_mult), 4)
        if t3_conf >= t2_conf:
            final_T    = round(T3, 6)
            final_conf = t3_conf
        else:
            spread     = abs(T3 - T2)   # keep for logging
            final_T    = round(T2, 6)
            final_conf = t2_conf
    elif conf2 >= 0.05:
        spread     = abs(T3 - T2) if stage3_ran else None
        final_T    = round(T2, 6)
        final_conf = round(min(1.0, conf2 * t12_mult), 4)
    else:
        spread     = None
        final_T    = round(T1, 4)
        final_conf = round(conf1, 4)

    # ── Apply rescue if Stage-3-at-T1 is more reliable ────────────────────
    _used_rescue = False
    if rescue_ran and conf3R_raw > final_conf:
        final_T      = round(T3R, 6)
        final_conf   = round(min(1.0, conf3R_raw), 4)
        _used_rescue = True

    # ── Calibration offset ─────────────────────────────────────────────────
    # Empirically measured from sync_corrections.jsonl across 58 "exact" user
    # acceptances: the raw algorithm output is on average ~6 ms LATER than the
    # user's accepted ground truth.  The previous -46 ms global was over-
    # correcting by ~40 ms — see _CAL_GLOBAL_LEGACY below for the historical
    # value.  All log entries from the legacy era have auto_T = raw_T - 0.046,
    # so per-file lookups must back that out before re-applying the new global.
    _T_CAL_GLOBAL        = -0.006   # current value, written into new log entries
    _T_CAL_GLOBAL_LEGACY = -0.046   # value used for entries before the fix
    _T_CAL_S             = _T_CAL_GLOBAL

    # Per-file learned calibration.
    #
    # Two design changes from the previous version:
    #
    #   (1) Use the FIRST (chronologically earliest) good correction for this
    #       audio file rather than the LAST.  When the user accepts a sync
    #       unchanged, that produces a "delta = 0" entry — using the LAST
    #       entry would make _T_CAL_S collapse back to _T_CAL_GLOBAL on every
    #       run after the first acceptance, defeating the per-file learning.
    #       The FIRST entry captures the calibration jump from raw → truth
    #       and stays stable across subsequent unchanged acceptances.
    #
    #   (2) Honour the per-entry "cal_global" field so we can change
    #       _T_CAL_GLOBAL safely in future without invalidating the cache.
    #       Old entries (no field) are interpreted with _T_CAL_GLOBAL_LEGACY.
    #
    # Math — entry was logged as auto_T = raw_T + cal_at_that_time, and the
    # user accepted accepted_T (the truth).  We want final_T == accepted_T:
    #
    #   final_T = raw_T + _T_CAL_S
    #           = (auto_T - cal_at_that_time) + _T_CAL_S
    #   set     = accepted_T
    #   ⇒ _T_CAL_S = accepted_T - auto_T + cal_at_that_time
    #             = delta + cal_at_that_time
    #
    # "wrong" / "verified" verdicts are skipped so a mis-found file never
    # corrupts the cache.
    try:
        _cal_cache = os.path.join(_app_state_dir(), ".pb_cache")
        _corr_path = os.path.join(_cal_cache, "sync_corrections.jsonl")
        if os.path.exists(_corr_path):
            _audio_name = os.path.basename(audio_path)
            _first_good = None
            with open(_corr_path, "r", encoding="utf-8") as _fc:
                for _line in _fc:
                    try:
                        _ce = json.loads(_line)
                        # Only entries that actually carry both auto_T AND
                        # accepted_T can teach us anything about per-file
                        # calibration.  "accepted" verdicts have only
                        # auto_T (user kept it as-is) and previously
                        # poisoned _T_CAL_S because accepted_T defaulted
                        # to 0.0, making delta = -auto_T and wiping out
                        # the offset on every subsequent run.  "wrong"
                        # and "verified" verdicts are kept out for the
                        # original reasons (algorithm grabbed wrong
                        # reference; no correction data).
                        if (_ce.get("audio") == _audio_name and
                                _ce.get("verdict") in ("exact", "close")):
                            _first_good = _ce
                            break   # first match wins
                    except Exception:
                        pass
            if _first_good is not None:
                _auto_T_logged     = _first_good.get("auto_T", 0.0)
                _accepted_T_logged = _first_good.get("accepted_T",
                                                      _auto_T_logged)
                _delta             = _accepted_T_logged - _auto_T_logged
                _cal_at_log = _first_good.get("cal_global",
                                              _T_CAL_GLOBAL_LEGACY)
                _T_CAL_S = _delta + _cal_at_log
    except Exception:
        pass

    final_T = round(final_T + _T_CAL_S, 6)

    # ── Write feedback log ─────────────────────────────────────────────────
    # .pb_cache/sync_runs.jsonl accumulates one entry per detection run.
    # At session start these can be read to understand algorithm behaviour.
    try:
        import datetime as _dt
        _cache_dir = os.path.join(_app_state_dir(), ".pb_cache")
        os.makedirs(_cache_dir, exist_ok=True)
        _entry = {
            "ts":            _dt.datetime.now().isoformat(timespec="seconds"),
            "video":         os.path.basename(video_path),
            "audio":         os.path.basename(audio_path),
            "s1_pass":       _s1_pass,           "s1_hz": 50 if _s1_pass == "A" else 5,
            "T1A":           round(T1A, 4),      "conf1A": round(conf1A, 4),
            "T1B":           round(T1B, 4),      "conf1B": round(conf1B, 4),
            "T1":            round(T1, 4),       "conf1":  round(conf1, 4),
            "T1_best":       round(T1_best, 4),
            "T2":            round(T2, 4),       "conf2":  round(conf2, 4),
            "T3":            round(T3, 6)        if stage3_ran else None,
            "conf3":         round(conf3, 4)     if stage3_ran else None,
            "t12_spread_ms": round(t12_spread * 1000, 1),
            "t12_factor":    round(t12_factor, 3),
            "spread_ms":     round(spread * 1000, 1) if spread is not None else None,
            "stage3_ran":    stage3_ran,
            "T2M":           round(T2M, 4),       "conf2M": round(conf2M, 4),
            "used_mirror":   _used_mirror,
            "rescue_T3R":    round(T3R, 6)       if rescue_ran else None,
            "rescue_conf":   round(conf3R_raw, 4) if rescue_ran else None,
            "used_rescue":   _used_rescue,
            "final_T":       final_T,
            "final_conf":    final_conf,
        }
        with open(os.path.join(_cache_dir, "sync_runs.jsonl"),
                  "a", encoding="utf-8") as _fh:
            _fh.write(json.dumps(_entry) + "\n")
    except Exception:
        pass

    # ── Alternative-offset candidates for sync-preview audition ──────────
    # Auto-sync sometimes commits to the wrong xcorr peak (e.g. short
    # files where the true peak isn't strongest).  Surface the runner-up
    # peaks from Stage 1 so the user can audition them in the sync
    # preview dialog instead of guessing offsets manually.
    #
    # Sources:
    #   • Stage 1 Pass A top-3 peaks (50 Hz envelope, first 60 s)
    #   • Mirror of each Pass A peak (in case the true offset has the
    #     opposite sign)
    #   • Stage 1 Pass B top peak (5 Hz, full probe — catches large offsets)
    #
    # Apply the same calibration shift as final_T so candidates are on
    # the same scale.  Dedup vs final_T and against each other (within
    # 0.5 s).  Return up to 3 alternatives, ranked by confidence.
    _alts_raw = []
    try:
        for _lag_a, _c_a in (_cands_a or []):
            _t = float(_lag_a) / 50.0 + _T_CAL_S
            _alts_raw.append((_t, float(_c_a), "PassA"))
            if abs(_t - _T_CAL_S) >= 0.5:
                _alts_raw.append((-_t + 2 * _T_CAL_S,
                                  float(_c_a) * 0.7, "PassA-mirror"))
        if _cands_b:
            _t = float(_cands_b[0][0]) / 5.0 + _T_CAL_S
            _alts_raw.append((_t, float(_cands_b[0][1]), "PassB"))
    except Exception:
        pass

    _alts = []
    _DEDUP_S = 0.5
    for _t, _c, _src in sorted(_alts_raw, key=lambda x: -x[1]):
        if abs(_t - final_T) <= _DEDUP_S:
            continue
        if any(abs(_t - x[0]) <= _DEDUP_S for x in _alts):
            continue
        _alts.append((round(_t, 4), round(min(1.0, _c), 4)))
        if len(_alts) >= 3:
            break

    return final_T, final_conf, _alts


def verify_sync_at_offset(video_path, audio_path, offset, start_offset=0.0):
    """
    Verify waveform agreement at a known/accepted sync offset.

    Runs a Stage-3 style precision xcorr (2 ms RMS envelope, ±0.5 s search)
    centred on `offset`.  Returns (verified_T, verified_conf) where
    verified_conf ≥ 0.4 indicates the waveforms genuinely agree there.

    Used to build a ground-truth dataset: after a user manually accepts an
    offset, this function measures the actual signal correlation at that point
    so future algorithm tuning has signal-verified ground truth rather than
    relying solely on user judgement.

    Returns (offset, 0.0) on any failure so callers can always unpack safely.
    """
    try:
        import numpy as _np2
    except ImportError:
        return offset, 0.0

    def _vx_extract(path, t_start, t_dur, out_sr):
        with tempfile.NamedTemporaryFile(suffix=".raw", delete=False) as tf:
            tmp = tf.name
        try:
            cmd = (_ffmpeg_cmd() + ["-y", "-v", "quiet"]
                   + audio_read_args(path, t_start, t_dur)
                   + ["-ac", "1", "-ar", str(int(out_sr)),
                      "-f", "f32le", tmp])
            r = run_hidden(cmd, capture_output=True, timeout=60)
            if r.returncode != 0:
                return None
            with open(tmp, "rb") as _fh:
                raw = _fh.read()
            if not raw:
                return None
            data = _np2.frombuffer(raw, dtype=_np2.float32).copy()
            rms = float(_np2.sqrt(_np2.mean(data ** 2)))
            if rms > 1e-9:
                data /= rms
            return data
        except Exception:
            return None
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    def _vx_peak_env(sig, win_samples):
        n = (len(sig) // win_samples) * win_samples
        if n < win_samples:
            return None
        rms = _np2.sqrt(_np2.mean(sig[:n].reshape(-1, win_samples) ** 2, axis=1))
        if len(rms) < 4:
            return None
        gate = float(_np2.percentile(rms, 65))   # keep top 35 %
        env  = _np2.maximum(0.0, rms - gate)
        env -= _np2.mean(env)
        std  = float(_np2.std(env))
        if std < 1e-9:
            return None
        return env / std

    def _vx_xcorr(a_sig, b_sig, max_lag_samp):
        na, nb = len(a_sig), len(b_sig)
        n  = na + nb - 1
        N  = 1 << int(_np2.ceil(_np2.log2(max(n, 1))))
        C  = _np2.fft.irfft(
                 _np2.fft.rfft(b_sig, N) * _np2.conj(_np2.fft.rfft(a_sig, N)),
                 N)[:n]
        C_abs = _np2.abs(C)
        ml = min(max_lag_samp, n // 2 - 1)
        pos_idx = list(range(0, ml + 1))
        neg_idx = list(range(n - ml, n)) if ml > 0 else []
        win_idx = pos_idx + neg_idx
        best_i  = int(max(win_idx, key=lambda i: C_abs[i]))
        lag_int = best_i if best_i <= n // 2 else best_i - n
        lag = float(lag_int)
        if 0 < best_i < n - 1:
            y0, y1, y2 = (float(C_abs[best_i + d]) for d in (-1, 0, 1))
            denom = y0 - 2.0 * y1 + y2
            if abs(denom) > 1e-12:
                lag += max(-0.5, min(0.5, 0.5 * (y0 - y2) / denom))
        vals       = C_abs[_np2.array(win_idx)]
        noise_mean = float(_np2.mean(vals))
        noise_std  = float(_np2.std(vals))
        z          = (float(C_abs[best_i]) - noise_mean) / max(noise_std, 1e-9)
        return lag, min(1.0, z / 8.0)

    SR   = 8000
    WIN  = SR // 500    # 2 ms windows → 500 Hz effective
    DUR  = 10.0
    SRCH = 0.5          # ±0.5 s search

    # Compensate extraction start positions so the lag at the true offset is ≈0
    if offset >= 0:
        a_s = start_offset
        v_s = max(0.0, start_offset + offset)
    else:
        a_s = max(0.0, start_offset - offset)
        v_s = start_offset

    raw_a = _vx_extract(audio_path, a_s, DUR, SR)
    raw_v = _vx_extract(video_path,  v_s, DUR, SR)
    if raw_a is None or raw_v is None:
        return offset, 0.0

    env_a = _vx_peak_env(raw_a, WIN)
    env_v = _vx_peak_env(raw_v, WIN)
    if env_a is None or env_v is None:
        return offset, 0.0

    sr_eff  = SR // WIN
    max_lag = int(SRCH * sr_eff)
    lag, conf = _vx_xcorr(env_a, env_v, max_lag)
    T = (v_s - a_s) + float(lag) / sr_eff
    return round(T, 6), round(conf, 4)


def detect_slate_offset(video_path, audio_path, search_secs=10.0, sample_rate=8000):
    """
    Detect sync offset using a slate clap transient.

    Looks for the first sharp onset (loud, brief transient) in both the video's
    embedded audio and the reference audio file within the first `search_secs`.
    The offset is the time difference between those two peaks.

    Returns (offset_secs: float, confidence: float 0..1)

    offset_secs: same sign convention as detect_sync_offset — add to src_in.
    confidence:  ratio of the slate peak to the surrounding RMS (0..1).
                 Values ≥ 0.6 are reliable; < 0.4 means no clean slate found.

    Requires ffmpeg in PATH and numpy.
    """
    import wave as _wave

    try:
        import numpy as _np
    except ImportError:
        raise RuntimeError("numpy is required.\nRun:  pip install numpy")

    def _extract(src, dst):
        r = run_hidden(
            _ffmpeg_cmd() + ["-y"]
            + audio_read_args(src, 0.0, search_secs)
            + ["-ac", "1", "-ar", str(sample_rate),
             "-acodec", "pcm_s16le", "-vn", dst],
            capture_output=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(
                "ffmpeg failed on {}:\n{}".format(
                    basename(src), r.stderr.decode(errors="replace")[-300:]))

    def _read_wav(path):
        with _wave.open(path, "rb") as w:
            data = w.readframes(w.getnframes())
        return _np.frombuffer(data, _np.int16).astype(_np.float32) / 32768.0

    def _find_onset(sig, sr):
        """
        Find the first major transient using an onset envelope.
        Returns (peak_sample, confidence) where confidence is peak/rms ratio.
        """
        # Onset envelope: rectified first-order diff (detects sudden energy jump)
        env = _np.abs(_np.diff(sig, prepend=sig[:1]))
        # Smooth lightly (5ms window) to remove single-sample noise
        w = max(1, int(sr * 0.005))
        env = _np.convolve(env, _np.ones(w) / w, mode='same')

        # Skip the first 100ms (mic click / pre-roll noise)
        skip = int(sr * 0.1)
        env_search = env[skip:]
        if len(env_search) == 0:
            return 0, 0.0

        pk_rel = int(_np.argmax(env_search))
        pk = pk_rel + skip
        peak_val = float(env[pk])
        rms = float(_np.sqrt(_np.mean(env ** 2))) + 1e-9
        confidence = min(1.0, peak_val / (rms * 6.0))  # 6× RMS = full confidence
        return pk, confidence

    with tempfile.TemporaryDirectory() as tmpdir:
        vid_wav = os.path.join(tmpdir, "vid.wav")
        ref_wav = os.path.join(tmpdir, "ref.wav")
        _extract(video_path, vid_wav)
        _extract(audio_path, ref_wav)
        a = _read_wav(vid_wav)
        b = _read_wav(ref_wav)

    pk_a, conf_a = _find_onset(a, sample_rate)   # onset in video audio
    pk_b, conf_b = _find_onset(b, sample_rate)   # onset in reference audio

    confidence = (conf_a + conf_b) / 2.0
    # offset = time_of_slate_in_video - time_of_slate_in_reference
    # Adding this to src_in aligns video slate with reference slate
    offset = (pk_a - pk_b) / sample_rate
    return offset, confidence


# ── AAF helpers ─────────────────────────────────────────────────────────────────
def get_clip_base_name(clip_name):
    """
    Strip Pro Tools copy-number suffix, processing identifiers, channel
    designations, and file extensions from a clip name, returning the
    logical source identifier used for video assignment grouping.

    Examples:
      'Blyth Cold Open Take 1.wav-03'          -> 'Blyth Cold Open Take 1'
      '12.12.2021_SW_2-02.L'                   -> '12.12.2021_SW_2'
      '12.12.2021_SW_2-02.R'                   -> '12.12.2021_SW_2'  (same)
      '2025-05-21~...~10543.m4v-RX10Dk_43-01' -> '2025-05-21~...~10543'
      'Eddie Alday.wav-39' / '-76'             -> 'Eddie Alday'  (same)
      '(fade in)'                              -> None  (skip)
      'Fade 1' / 'Fade 107'                   -> None  (skip rendered fades)
      'JORDAN_INTERVIEW'                       -> 'JORDAN_INTERVIEW'
    """
    if not clip_name or clip_name.startswith('('):
        return None
    # Skip Pro Tools rendered fade regions ("Fade N")
    if re.match(r'^Fade\s+\d+$', clip_name, re.IGNORECASE):
        return None
    # Strip rightmost copy-number suffix -NN (2+ digits) with optional .L/.R
    # Discard .L/.R entirely — L and R tracks consolidate to the same source entry
    m = re.match(r'^(.+)-(\d{2,})(\.[LR])?$', clip_name)
    name = m.group(1) if m else clip_name
    # Strip iZotope RX processing suffix: -RX10Dk_43, -RX8Dn_01, etc.
    name = re.sub(r'-RX\d+[A-Za-z]+_\d+', '', name)
    # Strip any remaining trailing .L/.R channel designator
    name = re.sub(r'\.[LR]$', '', name)
    # Strip common audio/video file extensions
    name = re.sub(r'\.(wav|aif|aiff|mp3|m4v|mp4|mxf|mov|wmv|avi|mkv)$', '', name,
                  flags=re.IGNORECASE)
    return name or None


def parse_aaf_session(aaf_path):
    """
    Parse a Pro Tools AAF export.

    Returns:
      {
        session_name  : str,
        fps           : float,        # always 29.97 — audio AAF has no FPS slot
        sample_rate   : int,
        start_tc_secs : float,        # session start timecode, in seconds
        start_tc_frames: int,         # ...and in frames at tc_fps
        tc_fps        : float,        # rate the timecode slot counts at
        drop_frame    : bool,         # True when the session counts drop-frame
        tracks        : [
            { name: str,
              clips : [
                  { clip_name   : str,
                    start_secs  : float,   # timeline in-point
                    end_secs    : float,   # timeline out-point
                    src_in_secs : float,   # source in-point
                    src_out_secs: float,   # source out-point
                    state       : "Unmuted" | "Muted",
                  }
              ]
            }
        ],
        markers       : [ { name: str, position_secs: float } ],
      }
    """
    _fade_pat = re.compile(r'^Fade\s+\d+$', re.IGNORECASE)

    try:
        import aaf2
    except ImportError:
        raise RuntimeError(
            "aaf2 library is required for AAF parsing. Run: pip install pyaaf2")

    def _url_to_path(url):
        """Convert file:///... URL to a local file path."""
        if not url:
            return ""
        try:
            from urllib.parse import unquote, urlparse
            parsed = urlparse(url)
            path = unquote(parsed.path)
            # Windows: file:///C:/... → C:/...
            if len(path) > 2 and path[0] == '/' and path[2] == ':':
                path = path[1:]
            return path.replace('/', os.sep)
        except Exception:
            return ""

    result = {
        "session_name":    os.path.splitext(os.path.basename(aaf_path))[0],
        "fps":             29.97,
        "sample_rate":     48000,
        "tracks":          [],
        "markers":         [],
        "start_tc_secs":   0.0,
        "start_tc_frames": 0,
        "tc_fps":          0.0,
        "drop_frame":      False,
    }

    def _cn(obj):
        try:    return obj.class_name
        except: return type(obj).__name__

    def _rate(slot):
        try:
            er = slot.edit_rate
            return float(er.numerator) / float(er.denominator)
        except Exception:
            return float(result["sample_rate"])

    with aaf2.open(aaf_path, "r") as f:
        mobs = list(f.content.mobs)
        mob_index = {str(mob.mob_id): mob for mob in mobs}

        # Find the CompositionMob with the most slots (= the main timeline)
        comp_mob   = None
        best_count = -1
        for mob in mobs:
            if _cn(mob) != 'CompositionMob':
                continue
            try:    count = sum(1 for _ in mob.slots)
            except: count = 0
            if count > best_count:
                best_count = count
                comp_mob   = mob

        if comp_mob is None:
            return result

        try:
            if comp_mob.name:
                result["session_name"] = comp_mob.name
        except Exception:
            pass

        # ── Session start timecode ────────────────────────────────────────────
        # Every clip position below is accumulated from the composition's zero.
        # That zero is only meaningful next to the session's start timecode,
        # which lives in a separate Timecode slot.  Without it the XML claims
        # the sequence starts at 00:00:00:00 and the whole conform lands off by
        # exactly the session start (typically one hour).
        def _read_tc(component):
            """Pull (start_frames, fps, drop) off a Timecode component."""
            def _get(name):
                try:    return component[name].value
                except Exception: pass
                try:    return getattr(component, name.lower())
                except Exception: return None
            start = _get('Start')
            tcfps = _get('FPS')
            drop  = _get('Drop')
            if start is None:
                return None
            return (int(start),
                    float(tcfps) if tcfps else 0.0,
                    bool(drop))

        def _find_tc(mob):
            """First Timecode component on a mob, direct or inside a Sequence."""
            try:    slots = list(mob.slots)
            except Exception: return None
            for sl in slots:
                try:    sseg = sl.segment
                except Exception: continue
                if _cn(sseg) == 'Timecode':
                    got = _read_tc(sseg)
                    if got:
                        return got + (_rate(sl),)
                elif _cn(sseg) == 'Sequence':
                    try:    subs = list(sseg.components)
                    except Exception: continue
                    for sub in subs:
                        if _cn(sub) == 'Timecode':
                            got = _read_tc(sub)
                            if got:
                                return got + (_rate(sl),)
            return None

        found = _find_tc(comp_mob)
        if found is None:
            # Some exporters hang the timecode off a source mob instead.
            for mob in mobs:
                if mob is comp_mob:
                    continue
                found = _find_tc(mob)
                if found and found[0]:
                    break
                found = None

        if found:
            start_frames, tc_fps, drop, slot_rate = found
            # Prefer the Timecode component's own FPS; fall back to the slot's
            # edit rate, which for a timecode slot is the frame rate.
            if not tc_fps or tc_fps <= 0:
                # _rate() falls back to the sample rate when a slot has no edit
                # rate — only trust it if it looks like a frame rate.
                tc_fps = slot_rate if (slot_rate and 1.0 <= slot_rate <= 120.0) else 0.0
            # Drop-frame sessions store a 30 (not 29.97) nominal rate; the real
            # counting rate is 30000/1001.  Same for 60 -> 59.94.
            eff_fps = tc_fps
            if drop and tc_fps in (30.0, 60.0):
                eff_fps = tc_fps * 1000.0 / 1001.0
            result["start_tc_frames"] = start_frames
            result["tc_fps"]          = tc_fps
            result["drop_frame"]      = drop
            result["start_tc_secs"]   = (start_frames / eff_fps) if eff_fps else 0.0

        for slot in comp_mob.slots:
            rate      = _rate(slot)
            slot_name = ''
            try: slot_name = slot.name or ''
            except: pass

            seg    = slot.segment
            seg_cn = _cn(seg)
            if seg_cn != 'Sequence':
                continue

            track_clips     = []
            is_marker_slot  = False
            timeline_pos    = 0
            pending_fade_in = 0.0   # fade duration queued for next real clip's head

            for comp in seg.components:
                ccn = _cn(comp)
                try:    comp_len = comp.length
                except: comp_len = 0

                # ── Unwrap OperationGroup ──────────────────────────────────────
                # Pro Tools AAF exports wrap every clip in an OperationGroup.
                # Extract the inner SourceClip; keep comp_len from the outer group
                # since that is the authoritative timeline duration.
                sc = comp
                if ccn == 'OperationGroup':
                    try:
                        for iseg in comp.segments:
                            if _cn(iseg) == 'SourceClip':
                                sc  = iseg
                                ccn = 'SourceClip'
                                break
                    except Exception:
                        pass

                # ── Marker components ──────────────────────────────────────────
                if ccn in ('CommentMarker', 'DescriptiveMarker', 'DescriptiveClip'):
                    is_marker_slot = True
                    mname = ''
                    try:    mname = comp['Comment'].value
                    except:
                        try: mname = comp.name or ''
                        except: pass
                    result["markers"].append({
                        "name":          mname,
                        "position_secs": timeline_pos / rate if rate else 0.0,
                    })
                    timeline_pos += comp_len
                    continue

                # ── Gap ────────────────────────────────────────────────────────
                if ccn == 'Filler':
                    # Intentional silence: reset any pending fade-in so it isn't
                    # incorrectly attached to the clip that follows the gap.
                    pending_fade_in = 0.0
                    timeline_pos += comp_len
                    continue

                # ── Clip ───────────────────────────────────────────────────────
                if ccn == 'SourceClip':
                    clip_name   = ''
                    src_in_secs = timeline_pos / rate if rate else 0.0  # fallback

                    # SourceID is required — skip clip if missing
                    try:
                        src_mob_id = str(sc['SourceID'].value)
                    except Exception:
                        timeline_pos += comp_len
                        continue

                    # SourceSlotID and StartTime are optional — Pro Tools omits
                    # SourceSlotID on OperationGroup-wrapped inner SourceClips
                    try:    src_slot_id = sc['SourceSlotID'].value
                    except: src_slot_id = None   # will use first slot in MasterMob

                    try:    tc_offset = sc['StartTime'].value
                    except: tc_offset = 0

                    # Resolve MasterMob → clip name and physical source in-point
                    source_file = ""
                    master = mob_index.get(src_mob_id)
                    if master is not None:
                        try: clip_name = master.name or ''
                        except: pass

                        for mslot in master.slots:
                            # If SourceSlotID is known, match exactly;
                            # otherwise accept the first slot we find
                            if src_slot_id is not None and mslot.slot_id != src_slot_id:
                                continue
                            try:
                                mseg = mslot.segment
                                if _cn(mseg) == 'SourceClip':
                                    master_src_in = mseg['StartTime'].value
                                    src_in_units  = master_src_in + tc_offset
                                    mrate         = _rate(mslot)
                                    src_in_secs   = src_in_units / mrate if mrate else 0.0

                                    # Follow to FileSourceMob for the on-disk file path
                                    try:
                                        file_mob_id = str(mseg['SourceID'].value)
                                        file_mob = mob_index.get(file_mob_id)
                                        if file_mob and hasattr(file_mob, 'descriptor'):
                                            desc = file_mob.descriptor
                                            if desc:
                                                for loc in desc.locator:
                                                    try:
                                                        url = loc['URLString'].value
                                                        source_file = _url_to_path(url)
                                                    except Exception:
                                                        pass
                                                    break
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                            break   # first matching slot is enough

                    # ── Fade clips — record duration, don't emit a track clip ──
                    if get_clip_base_name(clip_name) is None:
                        fade_dur = comp_len / rate if rate else 0.0
                        if fade_dur > 0:
                            # Annotate the preceding real clip's tail (fade-out /
                            # crossfade outgoing side).
                            if track_clips:
                                track_clips[-1]["fade_out_secs"] = (
                                    track_clips[-1].get("fade_out_secs", 0.0) + fade_dur)
                            # Queue for the next real clip's head (fade-in /
                            # crossfade incoming side).
                            pending_fade_in = fade_dur
                        timeline_pos += comp_len
                        continue

                    start_secs   = timeline_pos / rate if rate else 0.0
                    dur_secs     = comp_len     / rate if rate else 0.0
                    end_secs     = start_secs + dur_secs
                    src_out_secs = src_in_secs + dur_secs

                    track_clips.append({
                        "clip_name":     clip_name,
                        "start_secs":    start_secs,
                        "end_secs":      end_secs,
                        "src_in_secs":   src_in_secs,
                        "src_out_secs":  src_out_secs,
                        "state":         "Unmuted",
                        "source_file":   source_file,
                        "fade_in_secs":  pending_fade_in,
                        "fade_out_secs": 0.0,
                    })
                    pending_fade_in = 0.0

                timeline_pos += comp_len

            if not is_marker_slot and track_clips:
                # Fall back to "Track N" when Pro Tools didn't export a slot name
                display_name = slot_name or "Track {}".format(
                    len(result["tracks"]) + 1)
                result["tracks"].append({
                    "name":  display_name,
                    "clips": track_clips,
                })

    return result