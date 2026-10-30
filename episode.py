# -*- coding: utf-8 -*-
"""Episode: one project file shared by every workflow.

An episode lives at the root of a show's project folder as
``<Title>.pb_episode.json`` and holds what the three workflows used to each
keep for themselves: which files are the interview recordings and the
camera footage, which recordings belong together as one *source* (a
speaker, a narrator session), which footage covers each source, where the
script and the AAF are -- and, under ``stages``, each workflow's own saved
work: the Script -> Session reconcile, the AAF -> XML setup and the Pull
Quotes sessions with their transcripts.  It is the one file a project is
saved to.  Every path is stored relative to the root, so the folder can
move between machines.

Episodes from before ``stages`` kept that work in files of their own and
linked to them.  ``load`` reads those files in and leaves them where they
are, unused; nothing writes to them again.

Discovery (``scan``) looks through the chosen folder once and sorts what it
finds.  Project folders are noisy: Pro Tools keeps its own copies of every
imported recording under ``Audio Files``, music and effects libraries sit
beside the interviews, and renders, bounces, caches and backups all look
like media.  The scan folds duplicates onto one canonical file, puts music,
effects and short clips aside with a reason, and proposes sources with
their footage from file names and running times.  Everything it decides is
shown for review; nothing it excludes is deleted or hidden for good.

No Tk in this module: it is used by the screens in episode_workflow.py and
by tests.
"""
import hashlib
import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from utils import is_audio, is_video, is_media

EPISODE_EXT     = ".pb_episode.json"
EPISODE_VERSION = 3
WORKFLOW        = "episode"

# What each workflow saves into the episode, under "stages".
STAGE_S2S = "script_session"     # setup + results + Step 4 state
STAGE_AAF = "aaf_xml"            # setup
STAGE_PQ  = "pull_quotes"        # {"sessions": {session id: session}}

# A path under the root, inside stage data on disk.
_ROOT_MARK = "$EP/"
# Word lists: long, and never a path.
_NO_PATH_KEYS = frozenset(("transcript", "words"))

# Recordings and footage shorter than this are effects, stings, idents or
# test clips, not sources.  Reviewable: a short file can be included by hand.
MIN_SOURCE_SECS = 60.0

# Folder names (any component of the relative path) that hold things an
# episode never cuts from: libraries, renders, caches, Pro Tools housekeeping.
_NOISE_DIR_RE = re.compile(
    r"^(?:\d+[_\s-]*)?(?:"
    r"music|mx|score|sfx|fx|sound[\s_-]*effects|stings?|beds?|library|"
    r"foley|ambien(?:ce|t)|atmos|stems?|bounces?|bounced[\s_-]*files|"
    r"renders?|rendered[\s_-]*files|exports?|deliverables?|"
    r"final[\s_-]*assets?|approval|z?[\s_-]*temp(?:orary)?[\s_-]*.*|tmp|"
    r"cache|\.pb_cache|backups?|session[\s_-]*file[\s_-]*backups?|"
    r"fade[\s_-]*files|clip[\s_-]*groups|waveform[\s_-]*cache|"
    r"trash|proxies|proxy|graphics|gfx|motion|titles?"
    r")$", re.IGNORECASE)

# Pro Tools' own copy of every imported recording.
_PT_AUDIO_DIR_RE = re.compile(r"^audio[\s_-]*files$", re.IGNORECASE)

# Library music and effects by name, wherever they sit: APM-style catalogue
# codes (CEZ_CEZ_4163_01701_...), stems, "_APM", "_60_" edits, SFX_ and
# Freesound prefixes.  A Pro Tools session folder is full of these.
_LIBRARY_NAME_RE = re.compile(
    r"(?:^[A-Z]{2,6}_[A-Z0-9]{2,8}_\d{3,5}_\d{4,6}_)|_STEM_\(|_APM\b|__\d{2,3}__|"
    r"^SFX[\s_-]|^Freesound\b|^FX[\s_-]|^MX[\s_-]", re.IGNORECASE)

# A full-programme mix: the file the editor cuts to.
_MIX_NAME_RE = re.compile(r"for[\s_-]*vid|mix|bounce|master", re.IGNORECASE)
_MIX_DIR_RE  = re.compile(r"mix", re.IGNORECASE)


# ── Episode model ─────────────────────────────────────────────────────────

def new_episode(root, title=None):
    root = os.path.abspath(root)
    return {
        "version":  EPISODE_VERSION,
        "workflow": WORKFLOW,
        "title":    title or os.path.basename(root.rstrip("\\/")) or "Episode",
        "root":     root,
        # rel path -> media record (see scan)
        "media":    {},
        # name -> {"token", "audio": [rel], "video": [rel], "session": rel|None}
        "sources":  {},
        # rel paths the user put aside by hand (survives a rescan)
        "excluded": [],
        "links":    {"script": None, "aaf": None, "mix": None},
        # Each workflow's saved work (see STAGE_*).
        "stages":   {},
    }


def episode_file_for(root, title=None):
    ep_title = title or os.path.basename(os.path.abspath(root).rstrip("\\/"))
    safe = re.sub(r'[\\/:*?"<>|]+', "_", ep_title).strip() or "Episode"
    return os.path.join(os.path.abspath(root), safe + EPISODE_EXT)


def find_episode_file(root):
    """The episode file in *root*, or None."""
    try:
        names = sorted(n for n in os.listdir(root) if n.endswith(EPISODE_EXT))
    except OSError:
        return None
    for n in names:
        p = os.path.join(root, n)
        try:
            with open(p, encoding="utf-8") as f:
                if json.load(f).get("workflow") == WORKFLOW:
                    return p
        except Exception:
            continue
    return None


def find_episode_above(path, levels=6):
    """The episode file of the project *path* sits in, or None."""
    d = os.path.dirname(os.path.abspath(path))
    for _ in range(levels):
        found = find_episode_file(d)
        if found:
            return found
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def rel(ep, path):
    """Path relative to the episode root, forward slashes."""
    if not path:
        return None
    try:
        r = os.path.relpath(os.path.abspath(path), ep["root"])
    except ValueError:            # different drive
        return os.path.abspath(path).replace("\\", "/")
    return r.replace("\\", "/")


def absp(ep, r):
    if not r:
        return None
    if os.path.isabs(r):
        return os.path.normpath(r)
    return os.path.normpath(os.path.join(ep["root"], r))


def inside(ep, path):
    """True when *path* is somewhere under the episode's folder."""
    if not path:
        return False
    try:
        r = os.path.relpath(os.path.abspath(path), ep["root"])
    except ValueError:            # different drive
        return False
    return not (r == os.pardir or r.startswith(os.pardir + os.sep))


def _map_paths(obj, fn):
    """A copy of stage data with *fn* applied to every string but the word
    lists: a stage's paths sit in values and in keys (a setup's assignments),
    and each workflow has its own shape, so none of them is named here."""
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, dict):
        return {(fn(k) if isinstance(k, str) else k):
                (v if k in _NO_PATH_KEYS else _map_paths(v, fn))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_map_paths(v, fn) for v in obj]
    return obj


def _rooted(ep):
    """fn for _map_paths: an absolute path under the root -> root-relative."""
    prefix = ep["root"].replace("\\", "/").rstrip("/").lower() + "/"
    n = len(prefix)

    def fn(s):
        if len(s) > n and s[:n].replace("\\", "/").lower() == prefix:
            return _ROOT_MARK + s[n:].replace("\\", "/")
        return s
    return fn


def _unrooted(ep):
    """fn for _map_paths: the reverse of _rooted, at wherever the root is now."""
    root = ep["root"]

    def fn(s):
        if s.startswith(_ROOT_MARK):
            return os.path.normpath(os.path.join(root, s[len(_ROOT_MARK):]))
        return s
    return fn


def _plain(obj):
    """json default: numpy scalars and arrays, which reconcile results hold."""
    if hasattr(obj, "tolist"):
        return obj.tolist()
    raise TypeError("{} is not JSON serialisable".format(type(obj).__name__))


def _copied(data):
    return json.loads(json.dumps(data, default=_plain))


def set_stage(ep, stage, data):
    """Take a copy of a workflow's saved state: what is in the episode is what
    was saved, whatever is edited on screen after it."""
    ep.setdefault("stages", {})[stage] = _copied(data)


def get_stage(ep, stage):
    """A copy of a workflow's saved state, for it to work on, or None."""
    data = ep.get("stages", {}).get(stage)
    return _copied(data) if data else None


def pq_sessions(ep):
    """Pull Quotes' interview sessions, by session id (the live dict)."""
    return ep.setdefault("stages", {}).setdefault(
        STAGE_PQ, {}).setdefault("sessions", {})


def pq_session_list(ep):
    """Copies of the interview sessions, for Pull Quotes to work on."""
    held = ep.get("stages", {}).get(STAGE_PQ, {}).get("sessions", {})
    return [_copied(s) for s in held.values()]


def session_ref(ep_path, token):
    """What discovery names a session by when it lives in an episode file."""
    return "{}#{}".format(ep_path, token)


def read_pq_session(ref):
    """The interview session a discovery entry names: a .pb_session.json, or
    a session_ref into an episode.  Raises if it can't be read."""
    i = ref.rfind(EPISODE_EXT + "#")
    if i < 0:
        with open(ref, encoding="utf-8") as f:
            return json.load(f)
    path, token = ref[:i + len(EPISODE_EXT)], ref[i + len(EPISODE_EXT) + 1:]
    for s in pq_session_list(load(path)):
        if s.get("token") == token:
            return s
    raise ValueError("no session for {} in {}".format(token, path))


def _new_session_id():
    return "ses_" + uuid.uuid4().hex[:12]


def absorb_session_file(ep, path):
    """A .pb_session.json read into the episode.  A session already in the
    episode for that token stands.  Returns the session's id, or None when
    the file is not one."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if data.get("workflow") != "interview_session":
        return None
    sessions = pq_sessions(ep)
    token = data.get("token")
    for sid, s in sessions.items():
        if s.get("token") == token:
            return sid
    media = data.get("media") or []
    data = _rebased(ep, data, media[0] if media else None)
    data.setdefault("id", _new_session_id())
    sessions[data["id"]] = data
    return data["id"]


def _rebased(ep, data, probe):
    """An old stage file holds absolute paths, which still name wherever the
    project was when it was written.  When *probe*, a file it names, is gone
    but its tail is under the root, the project has moved: every path that
    began where that one did now begins at the root."""
    if not probe or os.path.isfile(probe):
        return data
    parts = probe.replace("\\", "/").split("/")
    for i in range(1, len(parts)):
        if os.path.isfile(os.path.join(ep["root"], *parts[i:])):
            old = "/".join(parts[:i]).lower() + "/"
            n = len(old)

            def fn(s):
                if len(s) > n and s[:n].replace("\\", "/").lower() == old:
                    return os.path.normpath(os.path.join(ep["root"], s[n:]))
                return s
            return _map_paths(data, fn)
    return data


def _absorb_stage_files(ep):
    """Read in the files an older episode linked to.  True if anything
    moved.  The files stay on disk; a link whose file is unreadable stays too,
    rather than quietly forgetting the work behind it."""
    links = ep["links"]
    changed = False
    for key, stage, name in (("s2s_session", STAGE_S2S, "script"),
                             ("aaf_setup", STAGE_AAF, "aaf")):
        p = absp(ep, links.get(key))
        if not p:
            changed = links.pop(key, None) is not None or changed
            continue
        if stage not in ep["stages"] and os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            data = _rebased(ep, data, data.get(name))
            ep["stages"][stage] = data
            # The stage opens from what it saved, so the link follows it --
            # but not to a file that isn't there.
            if os.path.isfile(data.get(name) or ""):
                links[name] = rel(ep, data[name])
        del links[key]
        changed = True
    if "sessions" in ep:
        ids = {}
        for r in ep.pop("sessions") or []:
            sid = absorb_session_file(ep, absp(ep, r))
            if sid:
                ids[rel(ep, absp(ep, r))] = sid
        for src in ep["sources"].values():
            if src.get("session"):
                src["session"] = ids.get(src["session"], src["session"])
        changed = True
    return changed


def load(path):
    with open(path, encoding="utf-8") as f:
        ep = json.load(f)
    if ep.get("workflow") != WORKFLOW:
        raise ValueError("not an episode file: {}".format(path))
    # The root is wherever the file is now: the folder may have moved.
    ep["root"] = os.path.dirname(os.path.abspath(path))
    ep["file_path"] = os.path.abspath(path)
    ep.setdefault("media", {}); ep.setdefault("sources", {})
    ep.setdefault("excluded", []); ep.setdefault("links", {})
    ep["stages"] = _map_paths(ep.get("stages") or {}, _unrooted(ep))
    # True until saved: the file on disk is still the old shape.
    ep["_absorbed"] = _absorb_stage_files(ep)
    return ep


def disk_form(ep):
    """The episode exactly as save() writes it, but for its stages: those
    run to megabytes of transcript, and are only ever changed by being
    written, so nothing needs to compare them."""
    out = {k: v for k, v in ep.items()
           if k not in ("file_path", "stages", "_absorbed")}
    out["version"] = EPISODE_VERSION
    return out


def _replace(tmp, path, tries=10):
    """Put the finished file in place.  Dropbox and virus scanners hold a file
    for a moment after it changes, which Windows refuses to replace."""
    for i in range(tries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.1)


def save(ep, path=None):
    path = path or ep.get("file_path") or episode_file_for(ep["root"], ep.get("title"))
    text = json.dumps(disk_form(ep), indent=2)
    stages = ep.get("stages")
    if stages:
        # Compact, and written after the readable part: the pure-Python
        # encoder behind indent= takes seconds over a few transcripts.
        text = text[:-2] + ',\n  "stages": ' + json.dumps(
            _map_paths(stages, _rooted(ep)), separators=(",", ":"),
            default=_plain) + "\n}"
    # All of a project is in this file: never leave half of it behind.
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        _replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    ep["file_path"] = os.path.abspath(path)
    ep.pop("_absorbed", None)
    return ep["file_path"]


# ── Discovery ─────────────────────────────────────────────────────────────

def _quick_hash(path, size):
    """Head + tail hash: tells identical copies apart from different files
    of the same length without reading gigabytes."""
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            h.update(f.read(1 << 20))
            if size > (2 << 20):
                f.seek(size - (1 << 20))
                h.update(f.read(1 << 20))
    except OSError:
        return None
    return h.hexdigest()


def _duration(path):
    try:
        from engines import get_media_duration
        return float(get_media_duration(path) or 0.0)
    except Exception:
        return 0.0


_ROMAN = frozenset({"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix",
                    "x", "xi", "xii"})


def _sig_words(text):
    """Words that carry identity in a file name: 4+ letters, numbers
    (leading zeros dropped so CAM 01 and CAM 1 agree), roman numerals."""
    out = set()
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w.isdigit():
            out.add(str(int(w)))
        elif len(w) >= 4 or w in _ROMAN:
            out.add(w)
    return out


def _name_match(src_name, video_stem):
    """(fraction of the source's identifying words the camera's name
    carries, how many that is).  A copy suffix on the recording is not part
    of its name: "Richardson Part 5 (1)" is Part 5, not a Part 1."""
    s = _sig_words(re.sub(r"\s*\(\d+\)$", "", src_name))
    v = _sig_words(video_stem)
    if not s or not v:
        return 0.0, 0
    shared = len(s & v)
    return shared / float(len(s)), shared


def _stem(path):
    return os.path.splitext(os.path.basename(path))[0]


def _stem_key(path):
    """Stem with the copy suffixes an app adds to a re-import stripped:
    "Take (1).wav", "Take.1.wav", "Take-01.wav" all key as "take"."""
    s = _stem(path).lower()
    s = re.sub(r"\s*\(\d+\)$", "", s)
    s = re.sub(r"(\.\d+|-\d{2,})$", "", s)
    return s.strip()


def _components(relpath):
    return [c for c in relpath.replace("\\", "/").split("/") if c]


def classify_path(relpath):
    """Folder-based verdict for one relative path: ("noise", reason),
    ("pt_copy", reason), ("mix", reason) or (None, "")."""
    comps = _components(relpath)
    dirs = comps[:-1]
    for d in dirs:
        if d.startswith("."):
            return "noise", "hidden folder"
        if _NOISE_DIR_RE.match(d):
            return "noise", "in '{}'".format(d)
    name = comps[-1] if comps else ""
    if name.lower().endswith(".pb_audio.wav"):
        return "noise", "playback cache"
    if _LIBRARY_NAME_RE.search(_stem(name)):
        return "noise", "library music / effects"
    for d in dirs:
        if _PT_AUDIO_DIR_RE.match(d):
            return "pt_copy", "Pro Tools 'Audio Files' copy"
    if is_audio(name) and (_MIX_NAME_RE.search(_stem(name))
                           or any(_MIX_DIR_RE.search(d) for d in dirs)):
        return "mix", "full-programme mix"
    return None, ""


def scan(root, progress=None, excluded=None, workers=4):
    """Find the media under *root* and sort it.

    Returns {"files": {rel: record}, "sources": {name: {...}},
             "mix": rel|None, "sessions": {token: rel}}.

    A record: {"kind", "size", "duration_s", "status", "reason", "dup_of"}
    with status one of  source | mix | duplicate | pt_copy | noise | short |
    excluded.  *excluded* is the user's own put-aside list; it wins over
    everything.  *progress(done, total, text)* is called as durations are
    probed.
    """
    root = os.path.abspath(root)
    excluded = set(excluded or [])
    files = {}
    sessions = {}
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune hidden folders early; everything else is looked at so the
        # review can show why it was put aside.
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("._"):
                continue
            full = os.path.join(dirpath, fn)
            r = os.path.relpath(full, root).replace("\\", "/")
            if fn.endswith(".pb_session.json"):
                try:
                    with open(full, encoding="utf-8") as f:
                        d = json.load(f)
                    if d.get("workflow") == "interview_session" and d.get("token"):
                        sessions[d["token"]] = {
                            "path": r,
                            "media": [os.path.basename(m) for m in d.get("media", [])]}
                except Exception:
                    pass
                continue
            if not is_media(fn):
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            kind = "video" if is_video(fn) else "audio"
            status, reason = classify_path(r)
            if r in excluded:
                status, reason = "excluded", "put aside"
            files[r] = {"kind": kind, "size": size, "duration_s": None,
                        "status": status, "reason": reason, "dup_of": None}

    # Durations for everything that might matter (noise is never probed:
    # a music library can be thousands of files).
    probe = [r for r, rec in files.items() if rec["status"] in (None, "pt_copy", "mix")]
    total = len(probe)
    done = [0]

    def _one(r):
        full = os.path.join(root, r)
        d = _duration(full)
        h = _quick_hash(full, files[r]["size"])
        return r, d, h

    hashes = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for r, d, h in ex.map(_one, probe):
            files[r]["duration_s"] = round(d, 3)
            hashes[r] = h
            done[0] += 1
            if progress:
                progress(done[0], total, os.path.basename(r))

    # Short clips are not sources.
    for r in probe:
        rec = files[r]
        if rec["status"] is None and rec["duration_s"] is not None \
                and rec["duration_s"] < MIN_SOURCE_SECS:
            rec["status"] = "short"
            rec["reason"] = "{:.0f} s".format(rec["duration_s"])

    # Duplicates: identical bytes first, then the same stem and length
    # (Pro Tools converts on import, so a copy can differ in bytes).
    def _rank(r):
        rec = files[r]
        return (rec["status"] == "pt_copy", len(_components(r)), len(r))
    groups = {}
    for r in probe:
        rec = files[r]
        if rec["status"] not in (None, "pt_copy", "mix"):
            continue
        key = ("h", rec["kind"], rec["size"], hashes.get(r))
        groups.setdefault(key, []).append(r)
    for r in probe:
        rec = files[r]
        if rec["status"] not in (None, "pt_copy", "mix"):
            continue
        key = ("s", rec["kind"], _stem_key(r), round(rec["duration_s"] or 0.0, 0))
        groups.setdefault(key, []).append(r)
    for key, members in groups.items():
        members = [m for m in members if files[m]["dup_of"] is None]
        if len(members) < 2:
            continue
        members.sort(key=_rank)
        canon = members[0]
        for m in members[1:]:
            if m == canon:
                continue
            files[m]["dup_of"] = canon
            files[m]["status"] = "duplicate"
            files[m]["reason"] = "copy of {}".format(os.path.basename(canon))
    # Whatever is left in the Pro Tools folder with no original elsewhere
    # stays listed as a Pro Tools copy: a session folder holds every music
    # cue and effect ever auditioned alongside any recording that only ever
    # lived there, and only a person can tell them apart.  The review shows
    # these with their running time for a one-click include.
    for r in probe:
        rec = files[r]
        if rec["status"] is None:
            rec["status"] = "source"

    mix = _pick_mix(files)
    sources = propose_sources(files, sessions)
    return {"files": files, "sources": sources, "mix": mix, "sessions": sessions}


def _pick_mix(files):
    cands = [(files[r]["duration_s"] or 0.0, r) for r, rec in files.items()
             if rec["status"] == "mix"]
    if not cands:
        return None
    # The longest full-programme mix; "FOR VID" in the name wins ties.
    cands.sort(key=lambda t: (-t[0], 0 if re.search(r"for[\s_-]*vid", t[1], re.I) else 1, t[1]))
    return cands[0][1]


def propose_sources(files, sessions=None):
    """Group the source recordings into sources and attach footage.

    Recordings with the same running time (within a second) that share a
    name are one multi-track session (a mic per speaker).  Each camera goes
    to the one source it matches best: running time (a camera rolled on the
    same session runs within a few percent of the recorder) together with a
    shared name; several cameras can match one source.  A Pull Quotes session whose
    media names match gives the source its token."""
    sessions = sessions or {}
    audio = sorted((r for r, rec in files.items()
                    if rec["kind"] == "audio" and rec["status"] == "source"),
                   key=lambda r: (-(files[r]["duration_s"] or 0.0), r))
    video = [r for r, rec in files.items()
             if rec["kind"] == "video" and rec["status"] == "source"]

    # 1. Group audio into sources.
    sources = []          # list of {"name", "audio": [rel]}
    for r in audio:
        d = files[r]["duration_s"] or 0.0
        placed = False
        for s in sources:
            d0 = files[s["audio"][0]]["duration_s"] or 0.0
            if abs(d - d0) <= 1.0 and len(_sig_words(_stem(r)) & _sig_words(_stem(s["audio"][0]))) >= 2:
                s["audio"].append(r)
                placed = True
                break
        if not placed:
            sources.append({"name": _stem(r), "audio": [r]})
    for s in sources:
        if len(s["audio"]) > 1:
            s["name"] = _common_name(s["audio"])
            s["audio"].sort()
    # Names key the episode's sources, so two must never share one.  Three
    # Riverside sessions all reduce to the prefix "riverside", and each one
    # silently replaced the last -- its recordings and cameras with it.
    # Sessions that share a name all go by their first recording instead.
    counts = {}
    for s in sources:
        counts[s["name"]] = counts.get(s["name"], 0) + 1
    taken = set()
    for s in sources:
        name = s["name"] if counts[s["name"]] == 1 else _stem(s["audio"][0])
        base, i = name, 2
        while name in taken:
            name = "{} ({})".format(base, i); i += 1
        s["name"] = name
        taken.add(name)

    # 2. Footage: by running time first, then by name.
    def _score(s, v):
        d0 = files[s["audio"][0]]["duration_s"] or 0.0
        dv = files[v]["duration_s"] or 0.0
        dur_ok = d0 > 0 and dv > 0 and abs(dv - d0) / max(d0, dv) <= 0.08
        ns, shared = max(_name_match(n, _stem(v))
                         for n in [s["name"]] + [_stem(a) for a in s["audio"]])
        # Running time agreeing counts only alongside a real share of the
        # name: one common word ("Interview") pinned an unrelated camera
        # of the same length on a recording.
        if dur_ok and (ns >= 0.5 or (ns >= 0.25 and shared >= 2)):
            return 2 + ns
        if ns >= 0.6:
            return ns
        return 0.0

    # Each camera goes to the source it matches best.  Taking the sources
    # one at a time, longest first, let a recording claim the cameras of
    # shorter ones it merely resembled: "Part 2" took the Part 5 and Part 6
    # cameras as extra angles, and Parts 5 and 6 fell back to Part 1's and
    # Part 3's.
    best_for = {}          # camera -> (score, index of its source)
    for v in sorted(video):
        for i, s in enumerate(sources):
            sc = _score(s, v)
            if sc > best_for.get(v, (0.0, None))[0]:
                best_for[v] = (sc, i)
    for i, s in enumerate(sources):
        scored = sorted(((sc, v) for v, (sc, j) in best_for.items() if j == i),
                        key=lambda t: (-t[0], t[1]))
        if not scored:
            s["video"] = []
            continue
        best = scored[0][0]
        # Everything close to the best is another angle of the same shoot.
        s["video"] = sorted(v for sc, v in scored if sc >= best - 0.5 and sc >= 1.0 or sc == best)

    # 3. Tokens from Pull Quotes sessions.
    out = {}
    for s in sources:
        names = {os.path.basename(a).lower() for a in s["audio"]}
        token, sess = None, None
        for tok, info in sessions.items():
            if names & {m.lower() for m in info.get("media", [])}:
                token, sess = tok, info["path"]
                break
        out[s["name"]] = {"token": token, "audio": s["audio"],
                          "video": s.get("video", []), "session": sess}
    return out


def _common_name(paths):
    stems = [_stem(p) for p in paths]
    pre = os.path.commonprefix(stems).rstrip(" _-.")
    return pre or stems[0]


def apply_scan(ep, result):
    """Write a scan result into the episode: media records (keeping the
    user's exclusions), sources, the mix link and the Pull Quotes sessions.

    A rescan keeps what was decided and proposes the rest afresh:

    * A source the user made or edited (user_made) stays exactly as it is,
      and its recordings and cameras stay its own.  When a proposal's
      recordings all sit in such a source and it has no cameras yet, it
      takes the cameras proposed for them.
    * Any other source that comes back -- under its name, or as the same
      recordings under a new one -- keeps its name, token, session, VO part
      and stage memory, and takes the cameras proposed now.  Nobody chose
      those, so a better scan may correct them; keeping them froze every
      wrong pairing a scan ever made.
    """
    ep["media"] = {r: dict(rec) for r, rec in result["files"].items()}
    old = ep.get("sources", {})
    new = {n: s for n, s in old.items() if s.get("user_made")}
    owner_of = {}                     # file a user-made source holds -> it
    for n, s in new.items():
        for f in s.get("audio", []) + s.get("video", []):
            owner_of[f] = n
    by_recordings = {frozenset(s["audio"]): n for n, s in old.items()
                     if s.get("audio") and not s.get("user_made")}
    carried = set()
    for name, src in result["sources"].items():
        audio = [a for a in src["audio"] if a not in owner_of]
        video = [v for v in src["video"] if v not in owner_of]
        if not audio:
            owners = {owner_of[a] for a in src["audio"]}
            if len(owners) == 1:
                mine = new[owners.pop()]
                if video and not mine.get("video"):
                    mine["video"] = sorted(video)
            continue
        prev_name = name if name in old and not old[name].get("user_made") \
            else by_recordings.get(frozenset(audio))
        if prev_name in carried:
            prev_name = None
        prev = old.get(prev_name) if prev_name else None
        if prev is not None:
            carried.add(prev_name)
            src = dict(prev, audio=audio, video=video,
                       token=prev.get("token") or src.get("token"),
                       session=prev.get("session") or src.get("session"))
            name = prev_name
        else:
            src = dict(src, audio=audio, video=video)
        base, i = name, 2
        while name in new:
            name = "{} ({})".format(base, i); i += 1
        new[name] = src
    ep["sources"] = new
    ep.setdefault("links", {})
    if result.get("mix") and not ep["links"].get("mix"):
        ep["links"]["mix"] = result["mix"]
    # Session files the scan found come into the episode, and their sources
    # then name the session by id.
    ids = {}
    for info in result["sessions"].values():
        sid = absorb_session_file(ep, absp(ep, info["path"]))
        if sid:
            ids[info["path"]] = sid
    for src in ep["sources"].values():
        if src.get("session"):
            src["session"] = ids.get(src["session"], src["session"])
    return ep


def summary(ep):
    """Counts for the review header."""
    st = {}
    for rec in ep.get("media", {}).values():
        st[rec["status"]] = st.get(rec["status"], 0) + 1
    return {
        "sources":  len(ep.get("sources", {})),
        "audio":    sum(1 for r in ep.get("media", {}).values()
                        if r["kind"] == "audio" and r["status"] == "source"),
        "video":    sum(1 for r in ep.get("media", {}).values()
                        if r["kind"] == "video" and r["status"] == "source"),
        "aside":    sum(n for s, n in st.items() if s not in ("source", "mix")),
        "by_status": st,
    }
