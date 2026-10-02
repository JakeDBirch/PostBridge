import os, re, ntpath, sys, subprocess

# Single point of control for child-process console suppression.  In the
# bundled WINDOWED build there is no parent console, so every subprocess
# (ffmpeg / ffprobe / nvidia-smi) would otherwise spawn its own — a black
# window flashing per call, observed to blank a secondary monitor on one
# user's Intel-graphics setup.  Every media subprocess in the app should
# go through this wrapper.
_CREATE_NO_WINDOW = 0x08000000

def app_state_dir():
    """Writable home for PostBridge's own logs and caches.

    A packaged app must never write inside its install location: on macOS
    any file added to a signed .app breaks its code-signature seal (Apple's
    notary rejected the build for exactly this — the sync-preview log landed
    in Contents/Frameworks), and a downloaded app launched from Downloads
    runs from a read-only translocated copy.  Frozen builds therefore use the
    per-user app-data folder; running from source keeps the old behaviour
    (next to the scripts), which the dev tools such as inspect_cache.py
    read from."""
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            d = os.path.join(os.path.expanduser("~"), "Library",
                             "Application Support", "PostBridge")
        elif sys.platform == "win32":
            d = os.path.join(os.environ.get("APPDATA")
                             or os.path.expanduser("~"), "PostBridge")
        else:
            d = os.path.join(os.environ.get("XDG_DATA_HOME")
                             or os.path.join(os.path.expanduser("~"),
                                             ".local", "share"), "PostBridge")
    else:
        d = os.path.dirname(os.path.abspath(__file__))
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _hidden_startupinfo():
    """STARTUPINFO that hides the child window — belt-and-suspenders
    alongside CREATE_NO_WINDOW.  CREATE_NO_WINDOW alone is normally
    enough to stop a console app (ffmpeg/ffprobe) flashing a window, but
    some Windows/driver combinations still flash without an explicit
    SW_HIDE; setting both is the most robust documented approach."""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si

def run_hidden(cmd, **kwargs):
    """subprocess.run with the child console fully suppressed on Windows."""
    if sys.platform == "win32":
        kwargs.setdefault("creationflags", _CREATE_NO_WINDOW)
        kwargs.setdefault("startupinfo", _hidden_startupinfo())
    return subprocess.run(cmd, **kwargs)

def basename(p):
    return ntpath.basename(p) or os.path.basename(p)

# ── Cloud-placeholder (Dropbox / OneDrive / iCloud) detection ────────────────
# Files synced by a cloud provider in "online-only" mode exist as zero-byte
# reparse-point placeholders: os.path.isfile() is True and st_size reports the
# FULL logical size, so every existence/size check in the app passes.  The
# bytes only arrive when something reads the file, and that read blocks for as
# long as the download takes — minutes for a 700 MB WAV, hours for a 20 GB MP4.
#
# That is indistinguishable from a hang: ffmpeg sits there producing nothing
# until our subprocess timeout fires, and the caller reports a generic failure.
# Detecting it up front lets us say what is actually wrong.
#
# Both APIs used here read metadata only — neither triggers hydration.
_FILE_ATTRIBUTE_OFFLINE               = 0x00001000
_FILE_ATTRIBUTE_RECALL_ON_OPEN        = 0x00040000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
_RECALL_MASK = (_FILE_ATTRIBUTE_OFFLINE
                | _FILE_ATTRIBUTE_RECALL_ON_OPEN
                | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)


def local_fraction(path):
    """Fraction (0.0-1.0) of *path* whose bytes are actually on local disk.

    Returns 1.0 when the file is fully local, when the platform gives us no
    way to tell, or on any error — callers must never block work on a
    negative answer they cannot trust.
    """
    try:
        logical = os.path.getsize(path)
    except OSError:
        return 1.0
    if logical <= 0:
        return 1.0

    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)

            k32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
            k32.GetFileAttributesW.restype  = wintypes.DWORD
            attrs = k32.GetFileAttributesW(path)
            if attrs == 0xFFFFFFFF or not (attrs & _RECALL_MASK):
                # No recall-on-access flag → ordinary local file.  Skip the
                # size probe so genuinely sparse local media isn't flagged.
                return 1.0

            k32.GetCompressedFileSizeW.argtypes = [wintypes.LPCWSTR,
                                                   ctypes.POINTER(wintypes.DWORD)]
            k32.GetCompressedFileSizeW.restype  = wintypes.DWORD
            hi = wintypes.DWORD(0)
            lo = k32.GetCompressedFileSizeW(path, ctypes.byref(hi))
            if lo == 0xFFFFFFFF and ctypes.get_last_error() != 0:
                return 1.0
            on_disk = (hi.value << 32) | lo
        except Exception:
            return 1.0
    else:
        try:
            on_disk = os.stat(path).st_blocks * 512
        except (AttributeError, OSError):
            return 1.0

    return max(0.0, min(1.0, float(on_disk) / float(logical)))


def offline_media(paths, threshold=0.9):
    """Filter *paths* down to the ones not sufficiently present on local disk.

    Returns a list of ``(path, local_fraction, logical_size_bytes)`` tuples,
    in the order given.  An empty list means everything is ready to read.
    """
    out = []
    for p in paths:
        if not p or not os.path.isfile(p):
            continue
        frac = local_fraction(p)
        if frac < threshold:
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            out.append((p, frac, size))
    return out


def _human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "{:.1f} {}".format(n, unit)
        n /= 1024.0


def offline_media_message(stale, action="read"):
    """Human-readable explanation for a non-empty offline_media() result."""
    lines = ["These files are cloud placeholders — their data is not on local "
             "disk, so any attempt to {} them stalls while the cloud client "
             "downloads them:".format(action), ""]
    pending = 0
    for p, frac, size in stale:
        pending += size * (1.0 - frac)
        lines.append("  • {}  ({}, {:.0f}% local)".format(
            basename(p), _human_bytes(size), frac * 100))
    lines += ["",
              "About {} would have to download first.".format(
                  _human_bytes(pending)),
              "",
              "Make them available offline in your cloud client (Dropbox: "
              "right-click → Make available offline), wait for the download "
              "to finish, then run this again."]
    return chr(10).join(lines)


def pathurl(p):
    """Convert an absolute file path to a file:// URL suitable for FCP/Premiere XMEML."""
    p = os.path.abspath(p)
    # On Windows, convert backslashes and add extra slash for drive letter
    if sys.platform == "win32":
        p = p.replace("\\", "/")
        return "file://localhost/" + p
    else:
        # macOS / Linux: spaces and special chars must be percent-encoded
        import urllib.parse
        return "file://localhost" + urllib.parse.quote(p)

def tc_secs(tc):
    """Parse HH:MM:SS or HH:MM:SS.mmm → float seconds."""
    p = tc.strip().split(":")
    return int(p[0]) * 3600 + int(p[1]) * 60 + float(p[2])

def secs_tc(s):
    """Convert float seconds → HH:MM:SS.mmm string (sub-second precision)."""
    s = max(0.0, float(s))
    h = int(s // 3600)
    r = s - h * 3600
    m = int(r // 60)
    sec = r - m * 60
    return "{:02d}:{:02d}:{:06.3f}".format(h, m, sec)

VIDEO_EXTS = {
    ".mp4", ".mov", ".mxf", ".avi", ".mkv", ".m4v",
    ".mpg", ".mpeg", ".ts", ".mts", ".m2ts", ".wmv",
    ".flv", ".webm", ".ogv", ".3gp", ".dv", ".r3d",
    ".braw", ".ari", ".vob",
}

AUDIO_EXTS = {
    ".wav", ".aif", ".aiff", ".bwf", ".rf64",
    ".mp3", ".m4a", ".aac", ".flac", ".ogg",
    ".opus", ".wma", ".caf",
}

# For the reference-audio pool: accept both pure audio AND video (which carries audio)
MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS

def is_video(p):
    return os.path.splitext(p)[1].lower() in VIDEO_EXTS

def is_audio(p):
    return os.path.splitext(p)[1].lower() in AUDIO_EXTS

def is_media(p):
    return os.path.splitext(p)[1].lower() in MEDIA_EXTS

# DVD-Video.  A title's programme is VTS_nn_1.VOB … VTS_nn_9.VOB: byte
# splits (1 GB each) of ONE MPEG program stream.  VIDEO_TS.VOB and
# VTS_nn_0.VOB are the disc's menus — never footage, so folder scans skip
# them (picking one by hand still works).
_DVD_MENU_VOB_RE = re.compile(r"^(video_ts|vts_\d\d_0)\.vob$", re.I)

def is_vob(p):
    return os.path.splitext(p)[1].lower() == ".vob"

def is_dvd_menu_vob(p):
    return bool(_DVD_MENU_VOB_RE.match(os.path.basename(p)))

def clean_words(text):
    """Lowercase, strip punctuation, return list of words.

    Smart/curly apostrophes (U+2018/U+2019) are normalised to a plain ASCII
    apostrophe before matching so that script text written in Word or Google
    Docs ("That\u2019s") aligns correctly with Whisper output ("that's").
    """
    text = text.replace('\u2019', "'").replace('\u2018', "'")
    return re.findall(r"[a-z']+", text.lower())

def suggest_token(filename, tokens):
    fn_raw = os.path.splitext(filename)[0]
    fn_low = fn_raw.lower()
    fn_norm = re.sub(r'[_\-.,;()\[\]{}]+', ' ', fn_low).strip()
    fn_words = set(fn_norm.split())

    fn_no_rs = re.sub(r'^riverside[_ ]', '', fn_norm)
    fn_no_rs = re.split(r'\braw[_ ]', fn_no_rs)[0].strip()
    fn_words |= set(fn_no_rs.split())

    best_tok   = None
    best_score = 0.0

    for tok in tokens:
        tok_low   = tok.lower()
        tok_norm  = re.sub(r'[_\-]+', ' ', tok_low).strip()
        tok_words = set(tok_norm.split())
        score     = 0.0

        if tok_low in fn_low or tok_norm in fn_norm:
            score = max(score, 10.0 + len(tok_low))

        if tok_words and tok_words.issubset(fn_words):
            score = max(score, 8.0 + len(tok_words))

        overlap = 0
        for tw in tok_words:
            if tw in fn_words:
                overlap += 1.0
            elif any(tw in fw for fw in fn_words):
                overlap += 0.7
            elif any(fw in tw for fw in fn_words if len(fw) > 2):
                overlap += 0.5
        if overlap > 0:
            score = max(score, overlap * (overlap / len(tok_words)) if tok_words else 0)

        if score == 0:
            for tw in tok_words:
                if len(tw) >= 3 and tw in fn_low:
                    score = max(score, 0.5 + len(tw) * 0.1)

        if score > best_score:
            best_score = score
            best_tok   = tok

    return best_tok if best_score >= 0.5 else None

_ROMAN_MAP = {
    1:"I",2:"II",3:"III",4:"IV",5:"V",6:"VI",7:"VII",8:"VIII",9:"IX",10:"X",
    11:"XI",12:"XII",13:"XIII",14:"XIV",15:"XV",16:"XVI",17:"XVII",18:"XVIII",
    19:"XIX",20:"XX",
}
_ARABIC_FROM_ROMAN = {v.lower(): k for k, v in _ROMAN_MAP.items()}

def _extract_part_number(text):
    text_low = text.lower().strip()
    if any(kw in text_low for kw in ("intro", "cold open", "opening", "opener")):
        return 0
    m = re.search(r'\bpart\s+(\w+)', text_low)
    if m:
        val = m.group(1)
        if val.isdigit():
            return int(val)
        if val in _ARABIC_FROM_ROMAN:
            return _ARABIC_FROM_ROMAN[val]
    m = re.search(r'(\d+)', text_low)
    if m:
        return int(m.group(1))
    return None

def suggest_vo_part(filename, parts):
    if not parts:
        return None

    fn_num = _extract_part_number(os.path.splitext(filename)[0])
    if fn_num is None:
        return None

    if fn_num == 0:
        for p in parts:
            if any(kw in p["name"].lower() for kw in ("intro", "open")):
                return "VO: {}".format(p["name"])
        return "VO: {}".format(parts[0]["name"])

    for p in parts:
        p_num = _extract_part_number(p["name"])
        if p_num is not None and p_num == fn_num:
            return "VO: {}".format(p["name"])

    if 1 <= fn_num <= len(parts):
        return "VO: {}".format(parts[fn_num - 1]["name"])

    return None

def _strip_take_number(filename):
    name = os.path.splitext(filename)[0].lower().strip()
    name = re.sub(r'[_ -]*(take|tk)[_ -]*\d+\s*$', '', name, flags=re.IGNORECASE)
    return name.strip()

def _strip_media_type(filename):
    name = os.path.splitext(filename)[0]
    name = re.sub(r'[_-]raw[_-]?(audio|synced[_-]video[_-]cfr|video[_-]cfr|video)',
                  '', name, flags=re.IGNORECASE)
    name = re.sub(r'[-_]{2,}', '_', name).strip('_- ')
    return name.lower()

def _name_similarity(a, b):
    a_words = set(re.sub(r'[^a-z0-9]', ' ', a).split())
    b_words = set(re.sub(r'[^a-z0-9]', ' ', b).split())
    if not a_words or not b_words: return 0.0
    return len(a_words & b_words) / max(len(a_words), len(b_words))

def _riverside_person_segment(filename):
    fn = os.path.splitext(os.path.basename(filename))[0].lower()
    fn = re.sub(r"[- ]+", "_", fn)
    if not fn.startswith("riverside_"):
        return None
    fn = fn[len("riverside_"):]
    parts = re.split(r"_raw[_-]", fn, maxsplit=1)
    return parts[0] if len(parts) == 2 else None

def parse_dnd(raw):
    paths=[]; cur=""; brace=False
    for ch in raw:
        if ch=="{": brace=True
        elif ch=="}": brace=False; paths.append(cur.strip()); cur=""
        elif ch==" " and not brace:
            if cur.strip(): paths.append(cur.strip()); cur=""
        else: cur+=ch
    if cur.strip(): paths.append(cur.strip())
    return [p for p in paths if p and os.path.isfile(p)]