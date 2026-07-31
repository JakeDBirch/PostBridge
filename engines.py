import os, sys, re, io, json, tempfile, subprocess, wave, hashlib, threading

# All media subprocesses (ffmpeg / ffprobe) go through _run so the child
# console window is suppressed on Windows (see utils.run_hidden — the
# single shared mechanism, also used by parsers / ffmpeg_bundled / main).
from utils import run_hidden as _run


def _safe_net_call(fn, default, _timeout=2.0):
    """
    Run fn() in a daemon thread with a hard timeout.

    On Windows, filesystem calls (getmtime, isfile, exists, …) on paths that
    live on an inaccessible network share (dead SMB connection, sleeping NAS,
    unmounted drive letter) can block INDEFINITELY — the call never raises, it
    just never returns.  Running the call in a daemon thread and joining with a
    timeout ensures these operations always complete promptly even when source
    files are offline.
    """
    result = [default]
    def _worker():
        try:
            result[0] = fn()
        except Exception:
            pass
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=_timeout)
    return result[0]

def _safe_getmtime(path, _timeout=2.0):
    """Return os.path.getmtime(path) with a hard timeout (see _safe_net_call)."""
    return _safe_net_call(lambda: os.path.getmtime(path), 0.0, _timeout)

def _safe_isfile(path, _timeout=2.0):
    """Return os.path.isfile(path) with a hard timeout (see _safe_net_call)."""
    return _safe_net_call(lambda: os.path.isfile(path), False, _timeout)

def _safe_exists(path, _timeout=2.0):
    """Return os.path.exists(path) with a hard timeout (see _safe_net_call)."""
    return _safe_net_call(lambda: os.path.exists(path), False, _timeout)
from xml.etree.ElementTree import Element, SubElement, ElementTree, indent

try:
    from faster_whisper import WhisperModel
    HAS_WHISPER = True
except ImportError:
    HAS_WHISPER = False

try:
    import aaf2
    HAS_AAF = True
except ImportError:
    HAS_AAF = False

from config import *
from utils import *

# ── Sentinel exception ─────────────────────────────────────────────────────────
class BuildCancelled(Exception):
    """Raised inside build_aaf when the caller sets the cancel_event."""

# ── Globals ────────────────────────────────────────────────────────────────────
_cache_dir = None   # Will be set by App when script is loaded
_channel_cache = {}
_model_cache = {}
_model_lock  = threading.Lock()

# User-selected Whisper model size override.  When set, replaces the
# config-level WHISPER_MODEL default for every get_model() call.  The
# settings dialog in main.py calls set_active_model() to persist the
# user's choice across the session.  None = use config default.
_active_model_override = None


def set_active_model(size):
    """Override the Whisper model size used by all get_model() calls.

    ``size`` should be one of the faster-whisper recognised model names
    (``tiny``, ``base``, ``small``, ``medium``, ``large-v3``, plus the
    ``.en`` English-only variants).  Pass ``None`` to revert to the
    config.WHISPER_MODEL default.
    """
    global _active_model_override
    _active_model_override = size or None


def get_active_model_size():
    """Return the model size that get_model() will currently use."""
    return _active_model_override or WHISPER_MODEL


def is_cuda_available():
    """Return True if a CUDA-enabled PyTorch build is installed and the
    current machine actually has a usable GPU.  Used by the Pull Quotes
    worker to decide between sequential (CPU) and parallel (GPU) draft +
    refine transcription."""
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False

# ── Audio extraction (uses bundled ffmpeg if not on PATH) ───────────────────────
def _ffmpeg_cmd():
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


def snap_fps(f):
    for std in (23.976, 24.0, 25.0, 29.97, 30.0, 47.952, 48.0,
                50.0, 59.94, 60.0):
        if abs(f - std) < 0.1:
            return std
    return f if f > 0 else 24.0


def check_ffmpeg():
    """
    Ensure ffmpeg/ffprobe are available (system or auto-downloaded). Return (True, "") if ready.
    """
    try:
        from ffmpeg_bundled import check_ffmpeg as _check
        return _check()
    except Exception as e:
        return False, str(e)


def extract_window(media_path, start_s, end_s, out_path):
    """
    Extract a window of audio from media_path to out_path (WAV).
    Returns (True, None) on success, (False, error_message) on failure.
    """
    duration = max(1.0, end_s - start_s)
    ff = _ffmpeg_cmd()
    cmd = ff + [
        "-y",
        "-ss", str(max(0, start_s)),
        "-t", str(duration),
        "-i", media_path,
        "-ar", "16000",
        "-ac", "1",
        "-vn",
        out_path
    ]
    try:
        r = _run(cmd, capture_output=True, timeout=60, text=True, encoding='utf-8', errors='replace')
        if r.returncode == 0:
            return True, None
        err = (r.stderr or r.stdout or "").strip()
        if not err:
            err = "ffmpeg exited with code {}".format(r.returncode)
        # If default failed with "multiple streams" or similar, try explicit first audio
        if "Stream specifier" in err or "Invalid stream" in err or "does not contain any stream" in err.lower():
            cmd2 = ff + [
                "-y",
                "-ss", str(max(0, start_s)),
                "-t", str(duration),
                "-i", media_path,
                "-map", "0:a:0",
                "-ar", "16000",
                "-ac", "1",
                "-vn",
                out_path
            ]
            r2 = _run(cmd2, capture_output=True, timeout=60, text=True, encoding='utf-8', errors='replace')
            if r2.returncode == 0:
                return True, None
            err = (r2.stderr or r2.stdout or "").strip() or err
        return False, err
    except FileNotFoundError:
        return False, "ffmpeg not found"
    except subprocess.TimeoutExpired:
        return False, "ffmpeg timed out after 60s"
    except Exception as e:
        return False, str(e)

def extract_mono_pcm(media_path, sample_rate=8000):
    """
    Extract full mono audio from *media_path* as a numpy float32 array
    normalised to [-1, 1].  Pipes raw PCM from ffmpeg — no temp file needed.

    Returns a 1-D numpy float32 array, or raises RuntimeError on failure.
    """
    import numpy as _np
    cmd = _ffmpeg_cmd() + [
        "-i", media_path,
        "-ac", "1", "-ar", str(sample_rate),
        "-f", "s16le", "-acodec", "pcm_s16le",
        "-vn", "pipe:1",
    ]
    r = _run(cmd, capture_output=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(
            "ffmpeg failed extracting PCM from {}:\n{}".format(
                os.path.basename(media_path),
                r.stderr.decode(errors="replace")[-400:]))
    return _np.frombuffer(r.stdout, _np.int16).astype(_np.float32) / 32768.0


def extract_audio_segment(media_path, start_s, duration_s, out_wav_path,
                          sample_rate=16000):
    """
    Extract a short audio segment to a WAV file for playback preview.

    Returns True on success, raises RuntimeError on failure.

    Uses ffmpeg's "double-seek" pattern (fast -ss BEFORE -i, accurate
    -ss AFTER -i) so the extract is sample-accurate at the target time
    without the decoder-warmup transient that pure fast-seek produces —
    which was audible in the match-review playback as a fade-in at the
    start of every clip.  The pre-seek keeps the operation fast even
    on long source files by getting the demuxer close before the
    accurate seek does the last leg.
    """
    start_s = max(0.0, start_s)
    _PRE_BUFFER_S = 2.0    # fast-seek land this far before the target
    if start_s > _PRE_BUFFER_S:
        pre_seek  = start_s - _PRE_BUFFER_S
        post_seek = _PRE_BUFFER_S
        seek_args = (["-ss", "{:.3f}".format(pre_seek), "-i", media_path,
                      "-ss", "{:.3f}".format(post_seek)])
    else:
        # Near the start of the file — just accurate-seek from 0.
        seek_args = (["-i", media_path, "-ss", "{:.3f}".format(start_s)])
    cmd = _ffmpeg_cmd() + ["-y"] + seek_args + [
        "-t", "{:.3f}".format(duration_s),
        "-ar", str(sample_rate), "-ac", "1",
        "-acodec", "pcm_s16le",
        "-vn", out_wav_path,
    ]
    r = _run(cmd, capture_output=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(
            "ffmpeg failed extracting segment from {}:\n{}".format(
                os.path.basename(media_path),
                r.stderr.decode(errors="replace")[-400:]))
    return True


def _ffprobe_csv(path, entries, select=None, timeout=10):
    """Run a single-value `-of csv=p=0` ffprobe query and return the
    stripped stdout, or None on any failure (missing file, timeout,
    non-zero exit).  Centralises the -v quiet / encoding / errors /
    timeout boilerplate every scalar probe used to repeat verbatim.

    None (failure) is kept distinct from "" (probe ran, value empty)
    so callers like _probe_codec can avoid caching a transient miss."""
    cmd = _ffprobe_cmd() + ["-v", "quiet"]
    if select:
        cmd += ["-select_streams", select]
    cmd += ["-show_entries", entries, "-of", "csv=p=0", path]
    try:
        r = _run(cmd, capture_output=True, text=True,
                 encoding="utf-8", errors="replace", timeout=timeout)
        return (r.stdout or "").strip()
    except Exception:
        return None


def get_media_duration(path):
    out = _ffprobe_csv(path, "format=duration")
    try:
        return float(out)
    except (TypeError, ValueError):
        return None

def get_audio_channels(path):
    out = _ffprobe_csv(path, "stream=channels", select="a:0")
    return int(out) if out and out.isdigit() else 2

# ── Split-clip join (gapless file-size splits → one continuous file) ─────────
def _concat_stream_sig(path):
    """Return a (video_sig, audio_sig) tuple describing a file's stream
    format, used to verify two files are safe to stream-copy concat.
    Each sig is a tuple of the format-defining fields; None if no stream."""
    def _probe(stream, fields):
        try:
            r = _run(
                _ffprobe_cmd() + ["-v", "quiet",
                 "-select_streams", stream,
                 "-show_entries", "stream=" + ",".join(fields),
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=15)
            vals = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
            return tuple(vals) if vals else None
        except Exception:
            return None
    v = _probe("v:0", ["codec_name", "width", "height", "pix_fmt"])
    a = _probe("a:0", ["codec_name", "sample_rate", "channels"])
    return v, a


def probe_concat_compat(paths):
    """Check whether `paths` can be losslessly stream-copy concatenated.

    Gapless file-size splits of one recording share identical stream
    parameters, so `-c copy` joins them perfectly.  Files from different
    cameras / settings do NOT, and a stream-copy concat of those would
    produce a broken file.  Returns (compatible: bool, reason: str).
    """
    if len(paths) < 2:
        return False, "select at least two files to join"
    sigs = [_concat_stream_sig(p) for p in paths]
    first_v, first_a = sigs[0]
    if first_v is None:
        return False, "first file has no readable video stream"
    for p, (v, a) in zip(paths[1:], sigs[1:]):
        if v != first_v:
            return (False,
                    "{} has a different video format — these aren't "
                    "splits of the same recording".format(basename(p)))
        if a != first_a:
            return (False,
                    "{} has a different audio format — these aren't "
                    "splits of the same recording".format(basename(p)))
    return True, ""


_codec_cache = {}   # (path, stream) → codec name; codecs never change mid-session

def _probe_codec(path, stream):
    """Codec name of the given stream, memoised per (path, stream).
    The memo collapses the duplicate spawn between join_output_ext
    (the dialog's default-name probe) and concat_video_files."""
    key = (path, stream)
    if key in _codec_cache:
        return _codec_cache[key]
    out = _ffprobe_csv(path, "stream=codec_name", select=stream, timeout=15)
    if out is None:
        return ""    # transient failure — do NOT cache, retry next call
    out = out.lower()
    _codec_cache[key] = out
    return out


def _probe_start_tc_secs(path, fps):
    """File's embedded start timecode in seconds, or None."""
    for ent in ("format_tags=timecode", "stream_tags=timecode"):
        try:
            r = _run(
                _ffprobe_cmd() + ["-v", "quiet", "-show_entries", ent,
                 "-of", "default=nw=1:nk=1", path],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=15)
            for line in (r.stdout or "").splitlines():
                m = re.match(r"\s*(\d+):(\d+):(\d+)[:;](\d+)", line)
                if m:
                    h, mi, s, ff = map(int, m.groups())
                    return h * 3600 + mi * 60 + s + (
                        ff / fps if fps and fps > 0 else 0.0)
        except Exception:
            pass
    return None


def _probe_creation_epoch(path):
    """File's creation_time as a unix epoch (seconds), or None."""
    try:
        r = _run(
            _ffprobe_cmd() + ["-v", "quiet",
             "-show_entries", "format_tags=creation_time",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15)
        lines = [l for l in (r.stdout or "").splitlines() if l.strip()]
        if not lines:
            return None
        import datetime
        txt = lines[0].strip().replace("Z", "+00:00")
        try:
            return datetime.datetime.fromisoformat(txt).timestamp()
        except Exception:
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})",
                         txt)
            if m:
                return datetime.datetime(*map(int, m.groups())).timestamp()
    except Exception:
        pass
    return None


def _fmt_hms(secs):
    secs = max(0, int(secs or 0))
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    if h:
        return "{}h {:02d}m {:02d}s".format(h, m, s)
    return "{}m {:02d}s".format(m, s)


def probe_join_continuity(paths):
    """Check that successive split pieces are time-contiguous before a
    join, using embedded timecode (preferred) or creation_time.

    Catches the common "incomplete join" mistakes: a missing MIDDLE
    piece (gap), and wrong order / duplicates (overlap).  A missing
    FIRST piece can't be detected from the files alone — but the
    returned summary surfaces the first piece's start timecode and the
    joined length so the user can sanity-check the front.

    Returns (status, message, summary):
      status  : 'ok' | 'gap' | 'unknown'
      message : human-readable gap/order details ('' when ok)
      summary : 'start TC HH:MM:SS · joined length …'
    """
    if len(paths) < 2:
        return "ok", "", ""
    info = []
    for p in paths:
        dur = get_media_duration(p) or 0.0
        try:
            _, _, fps = _probe_file_fps(p)
        except Exception:
            fps = 0.0
        info.append({
            "p":   p,
            "dur": dur,
            "tc":  _probe_start_tc_secs(p, fps),
            "cr":  _probe_creation_epoch(p),
        })

    use_tc = all(i["tc"] is not None for i in info)
    use_cr = (not use_tc) and all(i["cr"] is not None for i in info)
    key    = "tc" if use_tc else ("cr" if use_cr else None)

    total_dur = sum(i["dur"] for i in info)
    if use_tc:
        h = int((info[0]["tc"] or 0) // 3600)
        m = int(((info[0]["tc"] or 0) % 3600) // 60)
        s = int((info[0]["tc"] or 0) % 60)
        summary = "start TC {:02d}:{:02d}:{:02d} · joined length {}".format(
            h, m, s, _fmt_hms(total_dur))
    else:
        summary = "joined length {}".format(_fmt_hms(total_dur))

    if key is None:
        return "unknown", "", summary

    TOL  = 2.0   # seconds of slack
    gaps = []
    for a, b in zip(info, info[1:]):
        delta = b[key] - (a[key] + a["dur"])
        if delta > TOL:
            gaps.append("• {} gap before {} — a piece may be missing".format(
                _fmt_hms(delta), basename(b["p"])))
        elif delta < -TOL:
            gaps.append(
                "• {} overlaps the previous piece by {} "
                "(wrong order or duplicate?)".format(
                    basename(b["p"]), _fmt_hms(-delta)))
    if gaps:
        return "gap", "\n".join(gaps), summary
    return "ok", "", summary


def join_output_ext(paths):
    """Recommended output extension for a join.  Professional cameras
    often wrap PCM audio (pcm_s16be/le) in a .MP4 file, but the MP4/isom
    container has no tag for PCM — remuxing it to .mp4 yields a broken
    file.  .MOV holds h264/hevc + PCM cleanly, so PCM sources must join
    to .mov.  Anything else keeps the source's own extension."""
    a = _probe_codec(paths[0], "a:0") if paths else ""
    if a.startswith("pcm"):
        return ".mov"
    ext = os.path.splitext(paths[0])[1].lower() if paths else ".mp4"
    return ext or ".mp4"


def concat_video_files(paths, out_path, progress_cb=None):
    """Losslessly join `paths` (in order) into a single continuous file —
    no re-encode.  Uses the ffmpeg concat demuxer with stream copy.

    Two real-world gotchas, both handled here (verified against pro
    interview footage — h264 + pcm_s16be + a timecode data track):
      • PCM audio can't live in an .mp4/isom container, so the output
        container is forced to .mov for PCM sources (see
        join_output_ext).  Without this the audio is silently lost or
        the file is unreadable.
      • the camera's timecode DATA stream breaks the remux, so only the
        video + audio streams are mapped (the data track is dropped —
        harmless for editorial).

    `progress_cb(fraction_0_to_1)` is called once at finish (the copy is
    a single fast pass).  Returns (ok: bool, message: str, out_path: str)
    — out_path is the ACTUAL path written, which may differ from the
    requested one when the extension was corrected to .mov.
    """
    if len(paths) < 2:
        return False, "need at least two files", out_path

    # Probe the first file's audio codec ONCE — it determines both the
    # container (PCM must go to .mov, see join_output_ext) and whether
    # an audio stream gets mapped at all.
    acodec    = _probe_codec(paths[0], "a:0")
    has_audio = bool(acodec)
    if acodec.startswith("pcm"):
        want_ext = ".mov"
    else:
        want_ext = os.path.splitext(paths[0])[1].lower() or ".mp4"
    root, ext = os.path.splitext(out_path)
    if ext.lower() != want_ext:
        out_path = root + want_ext

    listf = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", suffix=".txt", delete=False, encoding="utf-8") as f:
            listf = f.name
            for p in paths:
                # concat demuxer: forward slashes (backslash is an escape
                # char), single-quoted, embedded quotes escaped.
                ap = os.path.abspath(p).replace("\\", "/").replace(
                    "'", "'\\''")
                f.write("file '{}'\n".format(ap))

        # Map only video + audio (drop the camera's timecode/data track,
        # which otherwise corrupts the output).  '?' = optional so a
        # missing stream doesn't abort the join.
        cmd = _ffmpeg_cmd() + ["-y", "-f", "concat", "-safe", "0",
                               "-i", listf, "-map", "0:v:0?"]
        if has_audio:
            cmd += ["-map", "0:a:0?"]
        cmd += ["-c", "copy", out_path]
        r = _run(cmd, capture_output=True, timeout=3600)
        if progress_cb:
            try: progress_cb(1.0)
            except Exception: pass

        if r.returncode != 0:
            tail = (r.stderr.decode("utf-8", "ignore")[-400:]
                    if r.stderr else "")
            return False, "ffmpeg failed:\n" + tail, out_path
        if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
            return False, "output file was not created", out_path

        # Verify the result is actually well-formed: a readable duration
        # AND (when the source had audio) a surviving audio stream.
        out_dur = get_media_duration(out_path) or 0
        if out_dur <= 0.5:
            return (False,
                    "join produced an unreadable file (duration could "
                    "not be read) — these clips may need a different "
                    "container", out_path)
        if has_audio and not _probe_codec(out_path, "a:0"):
            return (False,
                    "join dropped the audio track — these clips may need "
                    "a different container", out_path)

        # Duration sanity: a stream-copy concat's length should match the
        # sum of the inputs to within a few frames.  If the output is
        # materially short, a segment was silently dropped by the demuxer
        # (a codec/parameter mismatch ffmpeg skipped rather than aborted
        # on) — exactly the "joined clip is much shorter than expected"
        # failure the user hit, where one piece never made it into the
        # bin.  Surface it as a caution (ok stays True — the file is
        # valid, just possibly incomplete) so the caller can warn.
        warn = ""
        in_durs = [get_media_duration(p) or 0 for p in paths]
        if all(d > 0 for d in in_durs):
            expected = sum(in_durs)
            shortfall = expected - out_dur
            # Tolerate a couple of seconds of container/edit-list slack
            # and a 1 % relative wobble before crying foul.
            if shortfall > max(2.0, 0.01 * expected):
                warn = ("joined length {} is {} shorter than the sum of "
                        "the inputs ({}) — a segment may have been dropped; "
                        "check that every piece is present".format(
                            secs_tc(out_dur), secs_tc(shortfall),
                            secs_tc(expected)))
        return True, warn, out_path
    except Exception as e:
        return False, str(e), out_path
    finally:
        if listf:
            try: os.unlink(listf)
            except Exception: pass

# ── Transcript cache ───────────────────────────────────────────────────────────
def _cache_path(media_path):
    if not _cache_dir:
        return None
    key = hashlib.md5(os.path.abspath(media_path).encode()).hexdigest()[:16]
    return os.path.join(_cache_dir, "{}.json".format(key))

def _cache_key_meta(media_path):
    mtime = _safe_getmtime(media_path)
    return {
        "path":    os.path.abspath(media_path),
        "mtime":   mtime,
        "version": CACHE_VERSION,
    }

def cache_load(media_path):
    cp = _cache_path(media_path)
    if not cp or not os.path.exists(cp):
        return None
    try:
        with open(cp, encoding="utf-8") as f:
            data = json.load(f)
        meta = _cache_key_meta(media_path)
        if data.get("path") != meta["path"]:       return None
        if abs(data.get("mtime",0) - meta["mtime"]) > 1: return None
        if data.get("version") != meta["version"]: return None
        return data["words"]
    except Exception:
        return None

def cache_save(media_path, words, blobs=None):
    cp = _cache_path(media_path)
    if not cp:
        return
    try:
        os.makedirs(os.path.dirname(cp), exist_ok=True)
        meta = _cache_key_meta(media_path)
        meta["words"] = words
        if blobs is not None:
            meta["blobs"] = blobs
        with open(cp, "w", encoding="utf-8") as f:
            json.dump(meta, f)
    except Exception:
        pass

def cache_load_blobs(media_path):
    """Return cached blob list for media_path, or None if not cached / unavailable."""
    cp = _cache_path(media_path)
    if not cp or not os.path.exists(cp):
        return None
    try:
        with open(cp, encoding="utf-8") as f:
            data = json.load(f)
        meta = _cache_key_meta(media_path)
        if data.get("path") != meta["path"]:       return None
        if abs(data.get("mtime",0) - meta["mtime"]) > 1: return None
        if data.get("version") != meta["version"]: return None
        return data.get("blobs")   # None if this cache entry pre-dates blob detection
    except Exception:
        return None

# ── Standalone transcript file (.pb_transcript.json alongside media) ───────────
# These live next to the media file itself (not in .pb_cache/) so they travel
# with the asset across machines and remain valid regardless of project location.

def pb_transcript_path(media_path):
    """Return the .pb_transcript.json path that lives alongside media_path."""
    base = os.path.splitext(os.path.abspath(media_path))[0]
    return base + ".pb_transcript.json"

def pb_transcript_load(media_path):
    """Load a .pb_transcript.json from alongside media_path.
    Returns (words, blobs) or (None, None) if absent, stale, or invalid."""
    p = pb_transcript_path(media_path)
    if not _safe_isfile(p):
        return None, None
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        mtime = _safe_getmtime(media_path)
        # Reject if the media file was replaced since transcription
        if abs(data.get("mtime", 0) - mtime) > 2:
            return None, None
        words = data.get("words")
        if not words:
            return None, None
        return words, data.get("blobs")
    except Exception:
        return None, None

def pb_transcript_save(media_path, words, blobs=None):
    """Write a .pb_transcript.json alongside media_path."""
    p = pb_transcript_path(media_path)
    mtime = _safe_getmtime(media_path)
    data = {"version": 1, "model": WHISPER_MODEL, "mtime": mtime, "words": words}
    if blobs is not None:
        data["blobs"] = blobs
    # Bake audio_signature so cross-machine reconcile can verify the
    # local audio is bit-identical to what the transcript was made
    # from.  mtime alone is fragile — file copies change mtime while
    # keeping content, and re-saves change mtime with no real edit.
    try:
        _sig = audio_signature(media_path)
        if _sig:
            data["audio_signature"] = _sig
    except Exception:
        pass
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

def transcribe_file(media_path, progress_cb=None):
    """Transcribe an entire media file using the same chunked pipeline as VO
    reconcile.  Returns (words, blobs).  progress_cb(fraction, status_str) is
    called periodically if provided."""
    def _prog(frac, msg):
        if progress_cb:
            try: progress_cb(frac, msg)
            except Exception: pass

    words = None
    blobs = None

    # Probe total duration so the segment-level progress callback can
    # report "X% (HH:MM:SS of HH:MM:SS)" to the caller's UI.
    total_audio_s = 0.0
    try:
        total_audio_s = float(get_media_duration(media_path) or 0.0)
    except Exception:
        pass

    def _seg_cb(audio_s):
        if progress_cb is None:
            return
        try:
            if total_audio_s > 0:
                frac = max(0.0, min(0.95, 0.10 + 0.75 * (audio_s / total_audio_s)))
                pct  = int(round(audio_s * 100.0 / total_audio_s))
                progress_cb(frac, "Transcribing — {:.0f}s of {:.0f}s ({}%)".format(
                    audio_s, total_audio_s, pct))
            else:
                progress_cb(0.5, "Transcribing — {:.0f}s decoded".format(audio_s))
        except Exception:
            pass

    if WAVEFORM_CONFORM:
        _prog(0.05, "Detecting silence splits…")
        chunks = detect_silence_splits(media_path)
        if chunks:
            _prog(0.10, "Transcribing ({} chunks)…".format(len(chunks)))
            words = transcribe_in_chunks(media_path, chunks,
                                          progress_cb=_seg_cb)

    if words is None:
        # Either WAVEFORM_CONFORM is off or silence split failed — full-file path
        _prog(0.10, "Extracting audio…")
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tmp_a = tf.name
        try:
            ok, _ = extract_window(media_path, 0, 9999, tmp_a)
            if not ok:
                _prog(1.0, "Audio extraction failed")
                return None, None
            _prog(0.30, "Transcribing…")
            words = transcribe_clip(tmp_a, progress_cb=_seg_cb)
        finally:
            try: os.unlink(tmp_a)
            except Exception: pass

    if not words:
        _prog(1.0, "No words detected")
        return None, None

    if WAVEFORM_CONFORM:
        _prog(0.85, "Detecting speech boundaries…")
        blobs = detect_speech_blobs(media_path)

    _prog(1.0, "Done")
    return words, blobs

# ── Pull-result cache ───────────────────────────────────────────────────────────
# Caches the final reconciliation result for a pull so that re-runs after a
# crash or cancel don't need to re-transcribe already-matched clips.
# Only "ok" results are stored — low_confidence / no_match always retry so the
# user can fix assignments or the script and get a fresh attempt.

def _pull_result_cache_path(pull, transcript_path, pad):
    """Return the cache file path for a specific pull computation, or None."""
    if not _cache_dir or not transcript_path:
        return None
    mtime = round(_safe_getmtime(transcript_path), 2)
    key_obj = {
        "transcript": os.path.abspath(transcript_path),
        "mtime":      mtime,
        "in_s":       pull["in_seconds"],
        "out_s":      pull["out_seconds"],
        "quote":      pull.get("quote_text", "").strip(),
        "pad":        pad,
        "version":    CACHE_VERSION,
    }
    digest = hashlib.md5(
        json.dumps(key_obj, sort_keys=True).encode()
    ).hexdigest()[:16]
    return os.path.join(_cache_dir, "pull_{}.json".format(digest))

def pull_result_cache_load(pull, transcript_path, pad):
    """Return a cached reconciliation result dict, or None on miss."""
    cp = _pull_result_cache_path(pull, transcript_path, pad)
    if not cp or not os.path.exists(cp):
        return None
    try:
        with open(cp, encoding="utf-8") as f:
            data = json.load(f)
        result = data.get("result")
        if result:
            result["from_cache"] = True
        return result
    except Exception:
        return None

def pull_result_cache_save(pull, transcript_path, pad, result):
    """Persist an ok-status reconciliation result for future runs."""
    cp = _pull_result_cache_path(pull, transcript_path, pad)
    if not cp:
        return
    try:
        os.makedirs(os.path.dirname(cp), exist_ok=True)
        # Strip transient flag before writing
        payload = {k: v for k, v in result.items() if k != "from_cache"}
        with open(cp, "w", encoding="utf-8") as f:
            json.dump({"result": payload}, f)
    except Exception:
        pass

def pull_result_cache_clear(pull, transcript_path, pad):
    """Delete the cached result for a specific pull, forcing a fresh reconcile on
    the next run regardless of whether the source file has changed."""
    cp = _pull_result_cache_path(pull, transcript_path, pad)
    if cp and os.path.exists(cp):
        try:
            os.remove(cp)
        except Exception:
            pass

# ── Transcription ──────────────────────────────────────────────────────────────
def _bundled_models_root():
    """Return the path to bundled Whisper model weights shipped with
    the executable, else None.  Build scripts pre-download only the
    `tiny` model (~75 MB) into ``models/`` so the installer stays
    small enough to send via chat tools; `base` and `small` are
    fetched in the background after launch.
    """
    # PyInstaller-frozen apps have sys._MEIPASS for read-only data;
    # development runs use the source-tree CWD.
    candidates = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(os.path.join(base, "models"))
    candidates.append(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "models"))
    for c in candidates:
        if os.path.isdir(c):
            return c
    return None


def _persistent_models_dir():
    """Return the writable directory where Whisper weights live across
    runs.  Created on first call.  This is faster-whisper's
    ``download_root`` for every WhisperModel() construction — bundled
    tiny is seeded here from the read-only bundle on first launch,
    and the background downloader writes base/small/medium/large-v3
    here as they're acquired.
    """
    base = os.path.join(os.path.expanduser("~"), ".postbridge_models")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        pass
    return base


def _seed_bundled_models():
    """Copy any model directories that ship in the read-only bundle
    into the persistent cache so faster-whisper finds them under
    ``download_root``.  Idempotent: only seeds entries the persistent
    cache doesn't already hold.

    Safe to call repeatedly; the first call does the copy on cold
    install, subsequent calls are essentially no-ops (just a directory
    listing).
    """
    bundled = _bundled_models_root()
    if not bundled:
        return
    persistent = _persistent_models_dir()
    import shutil as _sh
    try:
        for entry in os.listdir(bundled):
            src = os.path.join(bundled, entry)
            dst = os.path.join(persistent, entry)
            if os.path.exists(dst):
                continue
            try:
                if os.path.isdir(src):
                    _sh.copytree(src, dst)
                else:
                    _sh.copy2(src, dst)
            except Exception as e:
                # Don't let a seed failure block startup — the model
                # will just get re-downloaded.
                print("PostBridge: model seed failed for {}: {}".format(
                    entry, e), file=sys.stderr)
    except Exception:
        pass


def _is_model_cached(size):
    """True if the given Whisper size is already in the persistent
    cache (won't trigger a HuggingFace download).  Used by the
    background pre-fetcher to skip work it doesn't need to do."""
    cache = _persistent_models_dir()
    # faster-whisper stores under models--Systran--faster-whisper-<size>/
    expected = os.path.join(
        cache, "models--Systran--faster-whisper-{}".format(size))
    return os.path.isdir(expected) and len(
        os.listdir(expected)) > 0


def prefetch_models_async(sizes=("base", "small")):
    """Spawn a background thread that downloads each of ``sizes`` into
    the persistent cache if it isn't already there.  Returns the
    Thread so callers can join() if they need to.  Failures (no
    network, etc.) are silent — the next time the user picks one of
    these models, faster-whisper will simply re-attempt the download.
    """
    def _run():
        try:
            from faster_whisper.utils import download_model
        except ImportError:
            try:
                from faster_whisper import download_model
            except ImportError:
                return
        cache = _persistent_models_dir()
        for size in sizes:
            if _is_model_cached(size):
                continue
            try:
                download_model(size, cache_dir=cache)
            except Exception as e:
                # Network failure or interrupted download — leave for
                # next attempt.  Don't crash the helper thread.
                print("PostBridge: background prefetch for '{}' "
                      "failed: {}".format(size, e),
                      file=sys.stderr)
                return
    t = threading.Thread(target=_run, daemon=True,
                          name="postbridge-model-prefetch")
    t.start()
    return t


def get_model(size=None):
    # Resolve "active" model size when none was passed:
    #   user override (set via the Settings dialog)  >  config default.
    # Callers that need a specific size (e.g. forced fallback) can still
    # pass it explicitly.
    if size is None:
        size = _active_model_override or WHISPER_MODEL
    # Fast path — no lock needed once the model is loaded.
    if size in _model_cache:
        return _model_cache[size]
    # Slow path — load once, thread-safe via double-checked locking.
    with _model_lock:
        if size not in _model_cache:
            try:
                import torch
                device  = "cuda" if torch.cuda.is_available() else "cpu"
                compute = "float16" if device == "cuda" else "int8"
            except ImportError:
                device = "cpu"; compute = "int8"
            # Seed the bundled tiny model into the persistent cache on
            # first call, then always point faster-whisper at the
            # persistent cache.  This way:
            #   - Bundled tiny (~75 MB in the installer) seeds cache
            #     on first launch and is found instantly thereafter.
            #   - base/small downloaded by the background prefetcher
            #     land in the same cache and are found by name.
            #   - medium/large-v3 download on demand the first time
            #     the user picks them.
            _seed_bundled_models()
            # num_workers > 1 lets concurrent transcribe() calls share
            # the GPU instead of serialising in CTranslate2 — see
            # config.WHISPER_NUM_WORKERS for the rationale.  Critical
            # for reconcile runs where one full-audio interview pass
            # is happening alongside several VO chunked transcribes.
            _model_cache[size] = WhisperModel(
                size, device=device, compute_type=compute,
                download_root=_persistent_models_dir(),
                num_workers=WHISPER_NUM_WORKERS)
        return _model_cache[size]

def _clip_rms_db(wav_path):
    """Return the mean RMS volume of a WAV clip in dBFS, or -99.0 on failure."""
    try:
        cmd = _ffmpeg_cmd() + [
            "-i", wav_path,
            "-af", "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level",
            "-f", "null", "-"
        ]
        r = _run(cmd, capture_output=True, timeout=15,
                           text=True, encoding="utf-8", errors="replace")
        for line in (r.stdout + r.stderr).splitlines():
            if "RMS_level" in line and "=" in line:
                val = line.split("=")[-1].strip()
                if val not in ("-inf", ""):
                    return float(val)
    except Exception:
        pass
    return -99.0

def _transcribe_raw(wav_path):
    """
    Transcribe wav_path with no VAD filter and a permissive no-speech threshold.
    Used only for diagnostics when the normal transcription returns nothing.
    Returns the raw text string (may be empty or hallucinated).
    """
    try:
        model = get_model()
        segments, _ = model.transcribe(
            wav_path,
            word_timestamps=False,
            language="en",
            beam_size=5,
            condition_on_previous_text=False,
            vad_filter=False,
            no_speech_threshold=0.95,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()
    except Exception as e:
        return "error: {}".format(e)

def transcribe_clip(wav_path, progress_cb=None):
    """Transcribe a WAV file into a flat list of word dicts (lowercase,
    alphanumeric-stripped).  Used by the reconcile / matching path.

    ``progress_cb``, if provided, is called once per Whisper segment with
    the audio offset (seconds) of that segment's end — same shape as
    ``transcribe_clip_verbatim`` so the same UI hook works for both."""
    model = get_model()
    # condition_on_previous_text=False  — each segment decoded independently;
    #   prevents cascading hallucination loops (the #1 cause of infinite hangs).
    # vad_filter=True                   — skip silence/noise regions before
    #   decoding so the model never tries to transcribe an empty or noisy clip.
    # no_speech_threshold=0.6           — treat segments with high no-speech
    #   probability as silence and discard them without running beam search.
    segments, _ = model.transcribe(
        wav_path,
        word_timestamps=True,
        language="en",
        beam_size=5,
        condition_on_previous_text=False,
        vad_filter=True,
        no_speech_threshold=0.6,
    )
    words = []
    for seg in segments:
        if seg.words:
            for w in seg.words:
                words.append({
                    "word":  re.sub(r"[^a-z']", "", w.word.lower()),
                    "start": w.start,
                    "end":   w.end,
                })
        else:
            for w in re.findall(r"[a-z']+", seg.text.lower()):
                words.append({"word": w, "start": seg.start, "end": seg.end})
        if progress_cb is not None:
            try:
                progress_cb(float(seg.end))
            except Exception:
                pass
    return [w for w in words if w["word"]]


class TranscriptionCancelled(Exception):
    """Raised by transcribe_clip_verbatim / transcribe_session_per_track
    when the caller-supplied cancel_event is set between segments.  The
    exception carries any words decoded so far on .partial_words in case
    the caller wants to preserve them; the Pull Quotes worker discards
    them and treats the run as a clean cancel."""

    def __init__(self, partial_words=None):
        super().__init__("transcription cancelled by user")
        self.partial_words = partial_words or []


def transcribe_clip_verbatim(wav_path, progress_cb=None, cancel_event=None,
                              model_size=None, segment_cb=None):
    """Whisper transcription that preserves the model's natural punctuation,
    casing, and segment boundaries — for human-readable display (Pull Quotes).

    Returns a flat list of entries:
        {"word": "<text>", "start": float, "end": float, "_src": "<size>"}
        {"word": "\\n\\n",   "start": ts,    "end": ts, "break": True,
         "_src": "<size>"}

    The "_src" field records which Whisper size produced each word — used
    by the progressive transcription merge logic to distinguish tiny-pass
    drafts from configured-model refinements and from user edits (which
    carry _src="user").

    The "break" entries are paragraph separators inserted between Whisper
    segments so the rendered transcript reads as paragraphed prose.

    `progress_cb`, if provided, receives one floating-point seconds value per
    segment as decoding progresses (the end time of the last segment seen).

    `cancel_event`, if provided (threading.Event), is polled between each
    decoded segment.  When set, raises TranscriptionCancelled carrying the
    words decoded so far.  CTranslate2's C++ decode can't be interrupted
    mid-segment, so the user sees up to ~5-15 s of lag (one segment
    duration) on a cancel click — far better than waiting for the whole
    file to finish.

    `model_size`, if provided, forces a specific Whisper size (e.g. "tiny"
    for a fast draft pass) and stamps every emitted word with that size
    in the `_src` field.  When None, uses the active model.

    `segment_cb`, if provided, fires once per decoded segment with a
    single argument: the list of new word dicts produced by THAT segment
    (the same dicts that have been appended to the return value).  The
    callback can use these for streaming UI updates — appending words
    to a visible transcript as they're decoded.  The break-marker
    entry inserted before each non-first segment is NOT included in
    the segment_cb payload; only "real" words.
    """
    effective_size = model_size or get_active_model_size()
    model = get_model(size=effective_size)
    segments, info = model.transcribe(
        wav_path,
        word_timestamps=True,
        language="en",
        beam_size=5,
        # condition_on_previous_text=True biases each segment with the prior
        # text — gives noticeably better punctuation and capitalisation in
        # exchange for slightly higher hallucination risk.  For a finished
        # interview that's a good trade.
        condition_on_previous_text=True,
        vad_filter=True,
        no_speech_threshold=0.6,
    )
    out = []
    first_segment = True
    prev_end = 0.0
    for seg in segments:
        # Cancel check at the segment boundary.  CTranslate2 has just
        # finished decoding one segment and is about to start the next;
        # this is the natural point to bail out.
        if cancel_event is not None and cancel_event.is_set():
            raise TranscriptionCancelled(partial_words=out)
        if not first_segment:
            # Record the silence gap so the renderer can choose between a
            # within-paragraph line break (short gap) and a paragraph break
            # (long gap).
            gap = max(0.0, float(seg.start) - prev_end)
            out.append({
                "word":  "\n",   # placeholder; renderer decides single vs double
                "start": float(seg.start),
                "end":   float(seg.start),
                "break": True,
                "gap":   gap,
                "_src":  effective_size,
            })
        first_segment = False

        # Track this segment's words so we can hand them to segment_cb
        # at end-of-segment as a discrete payload.
        seg_words = []

        if seg.words:
            for w in seg.words:
                # Whisper's per-word `.word` includes the natural leading
                # whitespace (e.g. " Hello,") — keep it verbatim so direct
                # concatenation reproduces the segment text without
                # heuristic spacing.
                wt = w.word
                if wt is None or not wt.strip():
                    continue
                entry = {
                    "word":  wt,
                    "start": float(w.start),
                    "end":   float(w.end),
                    "_src":  effective_size,
                }
                out.append(entry)
                seg_words.append(entry)
        else:
            # Fall back to segment text if word-level timing is missing.
            text = (seg.text or "").strip()
            if text:
                entry = {
                    "word":  (" " if out and not out[-1].get("break") else "") + text,
                    "start": float(seg.start),
                    "end":   float(seg.end),
                    "_src":  effective_size,
                }
                out.append(entry)
                seg_words.append(entry)

        if callable(segment_cb) and seg_words:
            try:
                segment_cb(seg_words)
            except Exception:
                # Streaming UI hook should never break decoding.
                pass

        prev_end = float(seg.end)
        if callable(progress_cb):
            try:
                progress_cb(prev_end)
            except Exception:
                pass

    return out


def transcribe_session_per_track(audio_paths, speaker_labels=None,
                                   progress_cb=None, cancel_event=None,
                                   model_size=None, segment_cb=None):
    """Transcribe each audio file separately and merge by timestamp.

    Each track is transcribed in isolation with `transcribe_clip_verbatim`,
    then every word is tagged with its source-file basename (or with the
    matching label from `speaker_labels` if provided) and the union of
    all tracks' words is sorted chronologically.

    Returns a flat list of word dicts:
        {"word": "...", "start": float, "end": float, "speaker": "..."}

    Whisper's per-track break entries are dropped — speaker turns are
    a stronger structural signal than within-track silence gaps, and
    the renderer derives layout from speaker changes + syntactic cues.

    `speaker_labels`: optional dict mapping basename → display label.
    Falls back to the basename (without extension) for any file not
    explicitly mapped.
    """
    speaker_labels = speaker_labels or {}
    all_words = []
    n = len(audio_paths)
    for i, src in enumerate(audio_paths):
        # Check between tracks too — a cancel mid-multi-track session
        # bails before we even start the next track's audio extraction.
        if cancel_event is not None and cancel_event.is_set():
            raise TranscriptionCancelled(partial_words=all_words)
        if not src or not os.path.isfile(src):
            continue
        # Tag for this track
        base = os.path.splitext(os.path.basename(src))[0]
        speaker = speaker_labels.get(os.path.basename(src),
                                       speaker_labels.get(base, base))

        # Extract a 16 kHz mono WAV (Whisper's preferred input).  Each
        # track is a single mic, so the source itself is mono-ish; we
        # still run extract_window to normalise format and trim.
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tmp = tf.name
        try:
            ok, err = extract_window(src, 0, 9999, tmp)
            if not ok:
                raise RuntimeError(
                    "audio extract failed for {}: {}".format(
                        os.path.basename(src), err or "unknown"))

            # Per-track callback wraps the global one with a track-aware
            # progress message via a closure.
            def _wrap_cb(audio_s, _i=i, _n=n, _src=src):
                if callable(progress_cb):
                    progress_cb(audio_s, track_index=_i, n_tracks=_n,
                                track_name=os.path.basename(_src))

            # Wrap segment_cb so the per-track speaker label rides
            # along with each streamed word — the PQ render layer
            # uses speaker tags to group/colorise paragraphs.
            inner_segcb = None
            if callable(segment_cb):
                def inner_segcb(seg_words, _sp=speaker):
                    for w in seg_words:
                        w["speaker"] = _sp
                    segment_cb(seg_words)
            try:
                words = transcribe_clip_verbatim(
                    tmp, progress_cb=_wrap_cb,
                    cancel_event=cancel_event,
                    model_size=model_size,
                    segment_cb=inner_segcb)
            except TranscriptionCancelled as _tc:
                # Propagate, but include words gathered from any
                # previously-completed tracks plus the partial words
                # from the current one (callers can choose to keep
                # them or discard them).
                for w in _tc.partial_words or []:
                    if w.get("break"):
                        continue
                    w["speaker"] = speaker
                    all_words.append(w)
                raise TranscriptionCancelled(partial_words=all_words)
        finally:
            try: os.unlink(tmp)
            except Exception: pass

        for w in words:
            if w.get("break"):
                continue
            w["speaker"] = speaker
            all_words.append(w)

    # Merge by start time.  Stable sort preserves intra-track order
    # when timestamps are identical.
    all_words.sort(key=lambda w: float(w.get("start", 0.0)))
    return all_words


# ── Multi-track mix-for-transcript ────────────────────────────────────────────
#
# When a token has more than one audio file, we mix them to a single temp WAV
# before handing off to Whisper.  Each track gets its own adaptive noise gate
# (threshold derived from its own noise floor), speech-weighted level matching
# so both participants are roughly equal in the mix, then a dynaudnorm pass to
# smooth out dynamic speakers.  The caller receives a temp file path and is
# responsible for deleting it after reconciliation.

# Statuses the render pipelines (AAF + WAV) treat as terminal: skip
# the row at build time.  no_match / error rows carry unverified
# script timecodes; the others were never fully processed.  If a user
# manually accepts such a row in Step 4, _accept() promotes it to
# "manual" and it drops off this list.  Kept as a module-level
# constant so build_aaf and the WAV builder can't drift — adding a
# new terminal status here updates both call sites at once.
_TERMINAL_SKIP_STATUSES = frozenset((
    "extract_failed", "no_transcript", "cancelled", "no_file",
    "no_match", "error",
))

_MIX_SR              = 16_000
_MIX_FRAME_SAMP      = _MIX_SR * 20 // 1000   # 20 ms frames = 320 samples
_MIX_NOISE_PCT       = 10
_MIX_THR_MULT        = 3.0
_MIX_THR_MIN         = 0.002
_MIX_THR_MAX         = 0.30
_MIX_GATE_CLOSED     = 0.005
_MIX_ATTACK_FR       = 5
_MIX_HOLD_FR         = 15
_MIX_RELEASE_FR      = 40
_MIX_PEAK_TARGET     = 0.90
_MIX_MAX_BOOST_DB    = 20.0
_MIX_DYN_FRAME_MS    = 500
_MIX_DYN_GAUSS      = 31
_MIX_DYN_PEAK        = 0.95
_MIX_DYN_MAX_GAIN    = 5.0


def _mix_decode(path):
    """Decode any audio file to 16 kHz mono float32 PCM via ffmpeg pipe."""
    import numpy as np
    cmd = _ffmpeg_cmd() + [
        "-y", "-i", path,
        "-ar", str(_MIX_SR), "-ac", "1", "-f", "s16le", "pipe:1",
    ]
    r = _run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError(
            "mix decode failed for {!r}: {}".format(
                os.path.basename(path), r.stderr.decode(errors="replace"))
        )
    return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def _mix_frame_rms(pcm):
    import numpy as np
    n = len(pcm) // _MIX_FRAME_SAMP
    if n == 0:
        return np.array([0.0], dtype=np.float32)
    return np.sqrt(
        np.mean(pcm[: n * _MIX_FRAME_SAMP].reshape(n, _MIX_FRAME_SAMP) ** 2, axis=1)
    ).astype(np.float32)


def _mix_gate_envelope(pcm, threshold):
    import numpy as np
    rms  = _mix_frame_rms(pcm)
    n    = len(rms)
    fg   = np.empty(n, dtype=np.float32)
    g, hold = 0.0, 0
    for i, r in enumerate(rms):
        if r >= threshold:
            hold = _MIX_HOLD_FR
            g    = min(1.0, g + 1.0 / _MIX_ATTACK_FR)
        elif hold > 0:
            hold -= 1
            g    = min(1.0, g + 1.0 / _MIX_ATTACK_FR)
        else:
            g    = max(_MIX_GATE_CLOSED, g - 1.0 / _MIX_RELEASE_FR)
        fg[i] = g
    body = np.repeat(fg, _MIX_FRAME_SAMP)
    tail = len(pcm) - len(body)
    if tail > 0:
        body = np.concatenate([body, np.full(tail, fg[-1], dtype=np.float32)])
    return body


def _mix_speech_rms(pcm, envelope):
    """RMS measured only over frames where the gate is substantially open."""
    import numpy as np
    n = len(pcm) // _MIX_FRAME_SAMP
    if n == 0:
        return 1e-9
    p    = pcm[:n * _MIX_FRAME_SAMP].reshape(n, _MIX_FRAME_SAMP)
    e    = envelope[:n * _MIX_FRAME_SAMP].reshape(n, _MIX_FRAME_SAMP)
    mask = e.mean(axis=1) > 0.5
    if not np.any(mask):
        return 1e-9
    return float(np.sqrt(np.mean(p[mask] ** 2)))


def _mix_dynaudnorm(path):
    """Apply dynaudnorm to path in-place via ffmpeg."""
    tmp = path + "._mix_tmp.wav"
    cmd = _ffmpeg_cmd() + [
        "-y", "-i", path,
        "-af", "dynaudnorm=f={}:g={}:p={}:m={}".format(
            _MIX_DYN_FRAME_MS, _MIX_DYN_GAUSS, _MIX_DYN_PEAK, _MIX_DYN_MAX_GAIN),
        tmp,
    ]
    r = _run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError(
            "dynaudnorm failed: " + r.stderr.decode(errors="replace")
        )
    os.replace(tmp, path)


_MIX_ALG_VER = 2   # bump when mix_for_transcript's algorithm changes

def _stable_mix_cache_path(paths):
    """Content-addressed path in the transcript cache for the mixed
    result of `paths`.  Key: md5(sorted (abspath, mtime, size) + alg_ver).
    Returns None when no cache directory is set (falls back to temp).

    _MIX_ALG_VER is included in the hash so a bug fix to the mix
    algorithm gets a fresh cache path instead of silently reusing a
    stale bad mix.  Old mix WAVs stay on disk (harmless; cleaned by
    normal cache eviction) but stop being consulted."""
    if not _cache_dir or not paths:
        return None
    try:
        parts = []
        for p in paths:
            ap = os.path.abspath(p)
            try:
                st = os.stat(ap)
                mt = round(st.st_mtime, 2)
                sz = st.st_size
            except OSError:
                mt, sz = 0, 0
            parts.append((ap.lower(), mt, sz))
        parts.sort()
        payload = {"paths": parts, "alg": _MIX_ALG_VER}
        digest = hashlib.md5(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
        return os.path.join(_cache_dir, "mix_{}.wav".format(digest))
    except Exception:
        return None


def mix_for_transcript(paths):
    """
    Mix a list of audio file paths into a single 16 kHz mono WAV
    suitable for Whisper transcription.

    Each track receives:
      1. An adaptive noise gate keyed to its own measured noise floor.
      2. Speech-weighted level matching so all participants are roughly equal.
      3. A dynaudnorm pass on the final mix to even out dynamic speakers.

    CACHE: writes to a CONTENT-ADDRESSED path in .pb_cache/ keyed by the
    sorted (abspath, mtime, size) of the inputs.  Second and subsequent
    reconciles of the same token with the same source files find the
    cached mix and return early — no re-mix, and the .pb_transcript.json
    sidecar next to it means Whisper is skipped too.  If any input's
    mtime or size changes, or a new file is added, the mix key changes
    and a fresh mix is generated.

    Returns the mix WAV path.  Callers should NOT delete the file when
    the return path lives under `engines._cache_dir` (it's persistent);
    the temp-file fallback (when the cache dir is unset) is still the
    caller's responsibility.
    """
    import numpy as np

    # ── Fast path: mix already cached and inputs unchanged ────────────────
    cached_path = _stable_mix_cache_path(paths)
    if cached_path and os.path.isfile(cached_path):
        try:
            if os.path.getsize(cached_path) > 44:   # >= a WAV header
                return cached_path
        except OSError:
            pass

    track_data = []
    for path in paths:
        pcm      = _mix_decode(path)
        # Noise-floor estimate.  The fixed 10th-percentile assumption
        # breaks for a continuously-talking speaker (typical interview
        # guest at 80-95% duty cycle): the 10th percentile lands INSIDE
        # the speech distribution, `thr = nf_rms * 3` closes the gate
        # on their own voice, and the mixed track that reaches Whisper
        # is missing that side of the conversation.  Cap nf_rms against
        # the MEDIAN frame RMS so a chatty track can't be its own noise
        # floor — a speech-dominated track's median IS speech, so
        # `median / 8` (~-18 dB below speech) sits reliably below every
        # real utterance.  Silent-heavy tracks (host between questions)
        # stay unaffected because their 10th percentile is already the
        # smaller value.
        _rms   = _mix_frame_rms(pcm)
        _p10   = float(np.percentile(_rms, _MIX_NOISE_PCT))
        _p50   = float(np.percentile(_rms, 50))
        nf_rms = min(_p10, _p50 / 8.0)
        thr      = float(np.clip(nf_rms * _MIX_THR_MULT, _MIX_THR_MIN, _MIX_THR_MAX))
        envelope = _mix_gate_envelope(pcm, thr)
        sp_rms   = _mix_speech_rms(pcm, envelope)
        track_data.append({"pcm": pcm, "envelope": envelope, "sp_rms": sp_rms})

    # Level matching: scale each track so speech RMS matches the median
    valid   = [td["sp_rms"] for td in track_data if td["sp_rms"] > 1e-6]
    target  = float(np.median(valid)) if valid else 1.0
    max_sc  = 10.0 ** (_MIX_MAX_BOOST_DB / 20.0)
    for td in track_data:
        sp = td["sp_rms"]
        td["scale"] = min(target / sp, max_sc) if sp > 1e-6 else 1.0

    # ── Apply gate + scale, then mix ─────────────────────────────────────
    # Accumulate into ONE preallocated buffer.  This used to build a
    # `gated` list of N full-length np.pad copies and then call
    # np.sum(gated, axis=0) — and np.sum over a LIST first stacks it
    # into an (N, maxlen) array, so at that moment it held N padded
    # copies PLUS an N x maxlen stack PLUS the decoded tracks and their
    # envelopes, all live at once.
    #
    # For four 3.75-hour tracks (216M samples each = 864 MB per float32
    # array) that measured ~14.6 GB peak against ~4.4 GB of headroom on
    # this machine — guaranteed page-file thrash, in the step
    # immediately before transcription.
    #
    # Folding each track in place drops peak to roughly the decoded
    # tracks plus one output buffer, and removes the np.pad copies
    # entirely (padding was pure waste — a shorter track just stops
    # contributing past its own length).
    maxlen  = max(len(td["pcm"]) for td in track_data)
    mixed   = np.zeros(maxlen, dtype=np.float32)
    for td in track_data:
        pcm = td["pcm"]
        n   = len(pcm)
        # Gate and level-match in place — no new full-length array.
        # Both operands are float32 (see _mix_decode / _mix_gate_envelope)
        # and scale is a Python float, so the result stays float32,
        # matching the old explicit .astype(np.float32).
        pcm *= td["envelope"]
        pcm *= td["scale"]
        mixed[:n] += pcm
        # Release this track's buffers now so they are reclaimable while
        # the remaining tracks are still being folded in.
        td["pcm"] = td["envelope"] = None

    peak  = np.max(np.abs(mixed))
    if peak > 1e-9:
        mixed *= _MIX_PEAK_TARGET / peak

    # Prefer the content-addressed cache path when a cache dir is set;
    # fall back to a random temp file otherwise (old behaviour).
    if cached_path:
        out_path = cached_path
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
    else:
        fd, out_path = tempfile.mkstemp(suffix=".wav", prefix="_pb_mix_")
        os.close(fd)
    pcm16 = (np.clip(mixed, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_MIX_SR)
        wf.writeframes(pcm16.tobytes())

    # Dynamic normalization pass
    _mix_dynaudnorm(out_path)

    return out_path


def mix_reference_audio(paths, out_path, sample_rate=48000):
    """Sum time-synchronous audio files into one reference WAV for
    dual-system sync.

    Use case: a 4-person interview where an on-camera GROUP mic matches
    the SUM of the individual lav tracks far better than any single lav.
    Syncing each camera against this aggregate locks cleanly (every
    speaker's onsets are present in both the camera mic and the mix).

    Straight aligned sum: each input is decoded to mono at `sample_rate`,
    padded to the longest, summed from sample 0, then peak-normalised.
    Assumes the inputs START at the same instant — true for a
    synchronous multitrack recording (the normal case).  Returns
    (ok: bool, message: str).
    """
    if len(paths) < 2:
        return False, "select at least two tracks to mix"
    try:
        # numpy import lives INSIDE the try so a missing install returns
        # (False, msg) per the contract instead of raising into the
        # dialog's worker thread (which would die silently and leave the
        # Mix dialog stuck on "Mixing…").
        import numpy as np
        # Accumulate incrementally — decode one track, add it into the
        # running sum, free it.  Holding every decoded track at once
        # costs ~11.5 MB per track-minute (4 one-hour lavs ≈ 2.8 GB);
        # this keeps peak memory at one track + the accumulator.
        mix = np.zeros(0, dtype=np.float64)
        for p in paths:
            cmd = _ffmpeg_cmd() + ["-v", "quiet", "-i", p, "-ac", "1",
                                   "-ar", str(int(sample_rate)),
                                   "-f", "f32le", "-"]
            r = _run(cmd, capture_output=True, timeout=1800)
            if r.returncode != 0 or not r.stdout:
                return False, "could not decode " + basename(p)
            arr = np.frombuffer(r.stdout, dtype=np.float32)
            if len(arr) == 0:
                return False, basename(p) + " decoded to empty audio"
            if len(arr) > len(mix):
                mix = np.concatenate(
                    [mix, np.zeros(len(arr) - len(mix), dtype=np.float64)])
            mix[:len(arr)] += arr
            del r, arr
        peak = float(np.max(np.abs(mix))) if len(mix) else 0.0
        if peak > 1e-9:
            mix = mix / peak * 0.9      # headroom, never clips
        pcm = (np.clip(mix, -1.0, 1.0) * 32767).astype(np.int16)
        with wave.open(out_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(int(sample_rate))
            w.writeframes(pcm.tobytes())
        if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
            return False, "output file was not created"
        return True, ""
    except Exception as e:
        return False, str(e)


def detect_sync_via_whisper(video_path, audio_path, probe_duration=120.0):
    """
    Find the sync offset between a video file's embedded camera audio and a
    reference audio file by transcribing both with Whisper and aligning
    matching word sequences.

    This is substantially more robust than FFT cross-correlation for
    dual-system recordings because Whisper normalises away microphone
    frequency response, room acoustics, and gain differences — the same
    speech produces the same text regardless of which mic captured it.

    Returns (offset_secs: float, confidence: float 0..1)
        offset_secs: ref_time - vid_time — add to the video's position to
                     get the corresponding position in the reference timeline.
        confidence:  fraction of matching word sequences that agree on the
                     returned offset, weighted by how many matches were found.
                     ≥ 0.5 is reliable; < 0.2 indicates insufficient speech.
    """
    if not HAS_WHISPER:
        raise RuntimeError("faster-whisper is not installed")

    try:
        import numpy as _np
    except ImportError:
        return 0.0, 0.0

    # Extract the probe window from both sources as 16 kHz mono WAVs.
    # 16 kHz matches Whisper's native rate (no resampling artefacts).
    def _extract(src, dst):
        cmd = _ffmpeg_cmd() + [
            "-t", "{:.3f}".format(probe_duration + 2),
            "-i", src,
            "-ac", "1", "-ar", "16000",
            "-acodec", "pcm_s16le", "-vn", dst,
        ]
        r = _run(cmd, capture_output=True, timeout=180)
        if r.returncode != 0:
            raise RuntimeError(
                "ffmpeg failed extracting probe from {}:\n{}".format(
                    os.path.basename(src),
                    r.stderr.decode(errors="replace")[-300:]))

    with tempfile.TemporaryDirectory() as tmpdir:
        vid_wav = os.path.join(tmpdir, "vid.wav")
        ref_wav = os.path.join(tmpdir, "ref.wav")
        _extract(video_path, vid_wav)
        _extract(audio_path, ref_wav)
        vid_words = transcribe_clip(vid_wav)
        ref_words = transcribe_clip(ref_wav)

    if not vid_words or not ref_words:
        return 0.0, 0.0   # no speech detected in one or both sources

    # Build a lookup table of 4-word sequences (N-grams) from the reference,
    # keyed by the tuple of words, valued by the start time of the first word.
    # N=4 is long enough to be unique but short enough to find matches even
    # when Whisper makes the occasional substitution in one recording.
    N = 4
    ref_ngrams = {}
    for i in range(len(ref_words) - N + 1):
        ngram = tuple(w["word"] for w in ref_words[i:i + N])
        # Skip N-grams that contain very short tokens (fillers, punctuation)
        if any(len(t) < 2 for t in ngram):
            continue
        ref_ngrams.setdefault(ngram, []).append(ref_words[i]["start"])

    # Walk the video transcription and collect offsets wherever an N-gram
    # from the video also appears in the reference.
    offsets = []
    for i in range(len(vid_words) - N + 1):
        ngram = tuple(w["word"] for w in vid_words[i:i + N])
        if any(len(t) < 2 for t in ngram):
            continue
        for ref_t in ref_ngrams.get(ngram, []):
            vid_t = vid_words[i]["start"]
            offsets.append(ref_t - vid_t)

    if not offsets:
        return 0.0, 0.0   # no matching word sequences found

    offsets = _np.array(offsets)
    median_offset = float(_np.median(offsets))

    # Confidence: fraction of matches within ±0.5 s of the median, scaled by
    # how many total matches we found (more matches → higher ceiling).
    agreement = float(_np.mean(_np.abs(offsets - median_offset) < 0.5))
    confidence = min(1.0, agreement * min(1.0, len(offsets) / 5.0))

    return median_offset, confidence


# ── Quote matching ─────────────────────────────────────────────────────────────
def _overlap(query, candidate):
    if not query: return 0.0
    cset = set(candidate)
    return sum(1 for w in query if w in cset) / len(query)

def _find_chunk_bounds(cwords, word_list, search_from=0):
    ANCHOR_N = 4
    MIN_SCORE = 0.45

    n = len(word_list)
    if search_from >= n or not cwords:
        return None

    first_anchor = cwords[:ANCHOR_N]
    last_anchor  = cwords[-ANCHOR_N:] if len(cwords) >= ANCHOR_N else cwords

    best_start       = None
    best_start_score = 0.0
    fa_set           = set(first_anchor)

    for i in range(search_from, n):
        if word_list[i] not in fa_set:
            continue
        window = word_list[i:i + len(first_anchor) + 3]
        score  = _overlap(first_anchor, window)
        if score > best_start_score:
            best_start_score = score
            best_start       = i
        if best_start_score >= 0.9:
            break

    if best_start is None or best_start_score < 0.3:
        return None

    min_end    = best_start + max(1, len(cwords) - 4)
    max_end    = min(best_start + len(cwords) * 2 + 8, n)
    la_set     = set(last_anchor)
    best_end   = min(best_start + len(cwords) - 1, n - 1)
    best_end_s = 0.0

    for j in range(best_start, max_end):
        if word_list[j] not in la_set:
            continue
        end_candidate = j
        for k in range(j, min(j + len(last_anchor) + 3, max_end)):
            if word_list[k] in la_set:
                end_candidate = k
        window = word_list[end_candidate - len(last_anchor) + 1:end_candidate + 1]
        score  = _overlap(last_anchor, window)
        if score > best_end_s:
            best_end_s = score
            best_end   = end_candidate
        if best_end_s >= 0.9:
            break

    span  = word_list[best_start:best_end + 1]
    score = _overlap(cwords, span)

    if score < MIN_SCORE:
        return None

    return (best_start, best_end, round(score, 3))

def _split_on_gaps(seg_words, gap_thresh):
    if not seg_words:
        return []
    chunks      = []
    chunk_start = seg_words[0]
    prev        = seg_words[0]
    for w in seg_words[1:]:
        if w["start"] - prev["end"] > gap_thresh:
            chunks.append((chunk_start["start"], prev["end"]))
            chunk_start = w
        prev = w
    chunks.append((chunk_start["start"], prev["end"]))
    return chunks

def match_segments(quote_text, words, gap_thresh=GAP_THRESH):
    if not words or not quote_text.strip():
        return None

    chunks = re.split(r'[.]{2,}|[\u2026]', quote_text)
    chunks = [c.strip().strip('\u201c\u201d\u2018\u2019"\'') for c in chunks]
    chunks = [c for c in chunks if c.strip()]
    if not chunks:
        return None

    word_list     = [w["word"] for w in words]
    search_from   = 0
    all_segs      = []
    all_scores    = []
    matched_parts = []
    n_gap_cuts    = 0

    for chunk_text in chunks:
        cwords = clean_words(chunk_text)
        if not cwords:
            continue

        hit = _find_chunk_bounds(cwords, word_list, search_from)
        if hit is None:
            continue

        s_idx, e_idx, score = hit
        all_scores.append(score)

        span = words[s_idx:e_idx + 1]
        matched_parts.append(" ".join(w["word"] for w in span))

        gap_segs    = _split_on_gaps(span, gap_thresh)
        all_segs.extend(gap_segs)
        n_gap_cuts += max(0, len(gap_segs) - 1)

        search_from = e_idx + 1

    if not all_segs:
        return None

    return {
        "segments":        all_segs,
        "confidence":      round(sum(all_scores) / len(all_scores), 3),
        "matched_text":    " \u2026 ".join(matched_parts),
        "n_internal_cuts": max(0, len(chunks) - 1),
        "n_gap_cuts":      n_gap_cuts,
    }

def _match_last(quote_text, words, gap_thresh=GAP_THRESH, conf_floor=0.35):
    """
    Find the last (final-take) occurrence of quote_text in words.

    Walks forward through the word list, keeping each new match that meets
    conf_floor.  Returns the last match so that false starts / flubs that
    appear earlier in the recording are skipped in favour of the final take.
    Falls back to the first match if only one is found (normal case).
    Returns None if no match meets conf_floor.
    """
    if not words or not quote_text.strip():
        return None

    best     = None
    word_idx = 0   # start of the next search window (index into words)

    while word_idx < len(words):
        candidate = match_segments(quote_text, words[word_idx:], gap_thresh)
        if candidate is None or not candidate["segments"]:
            break
        # Advance past the end of this match (+1.5 s gap ensures we skip over
        # any trailing silence and don't re-match the same take again).
        last_end_s = candidate["segments"][-1][1]
        advance = next(
            (i for i, w in enumerate(words[word_idx:])
             if w.get("start", 0) > last_end_s + 1.5),
            None,
        )

        # Only keep this match if it meets the confidence floor.  We still
        # advance past it regardless so a severely-truncated false start
        # (conf < floor) doesn't block discovery of the correct final take.
        if candidate["confidence"] >= conf_floor:
            best = candidate

        if advance is None:
            break   # nothing further in the recording
        word_idx += advance

    return best


def _match_best(quote_text, words, gap_thresh=GAP_THRESH, conf_floor=0.35):
    """
    Find the best (highest-confidence) occurrence of quote_text in words.

    Scans the full word list for every candidate occurrence and returns the
    one with the highest confidence score.  On near-equal confidence (within
    0.05) the later occurrence wins, matching _match_last semantics so the
    final-take delivery is preferred over an equally-good earlier rehearsal.

    Critically different from _match_last: when match_segments returns None
    (the false start was so badly truncated that _find_chunk_bounds scored it
    below its own MIN_SCORE floor), this function advances to the next
    occurrence of an anchor word and tries again rather than giving up
    immediately.  This lets severely-truncated false starts and mid-recording
    slates be skipped so the correct full take is still found.
    """
    if not words or not quote_text.strip():
        return None

    # Pre-compute first-chunk anchor words for manual advance on None returns.
    _raw = re.split(r'[.]{2,}|[\u2026]', quote_text)
    _raw = [c.strip().strip('\u201c\u201d\u2018\u2019"\'') for c in _raw]
    _raw = [c for c in _raw if c.strip()]
    anchor_words = clean_words(_raw[0])[:4] if _raw else []
    anchor_set   = set(anchor_words)

    word_list = [w["word"] for w in words]
    best      = None
    word_idx  = 0

    while word_idx < len(words):
        candidate = match_segments(quote_text, words[word_idx:], gap_thresh)

        if candidate is not None and candidate["segments"]:
            # Advance past this match (+1.5 s gap to clear trailing silence)
            last_end_s = candidate["segments"][-1][1]
            advance = next(
                (i for i, w in enumerate(words[word_idx:])
                 if w.get("start", 0) > last_end_s + 1.5),
                None,
            )
            if candidate["confidence"] >= conf_floor:
                # Keep highest confidence; on near-tie (≤0.05) prefer the
                # later delivery so the final recorded take wins.
                if (best is None
                        or candidate["confidence"] > best["confidence"] + 0.05
                        or abs(candidate["confidence"] - best["confidence"]) <= 0.05):
                    best = candidate
            if advance is None:
                break
            word_idx += advance

        else:
            # match_segments returned None — the current position's false start
            # or slate scored below _find_chunk_bounds MIN_SCORE.  Advance to
            # the next occurrence of an anchor word and retry from there.
            if not anchor_set:
                break
            nxt = next(
                (word_idx + 1 + i
                 for i, wrd in enumerate(word_list[word_idx + 1:])
                 if wrd in anchor_set),
                None,
            )
            if nxt is None:
                break
            word_idx = nxt   # try match_segments from this new anchor position

    return best


# ── Per-pull reconciler helpers ────────────────────────────────────────────────
def _clamp(val, lo, hi):
    return max(lo, min(hi, val))

def _snap_to_speech(words, clip_start, orig_in, orig_out, pad):
    """
    Return (snapped_in, snapped_out) by finding word boundaries in the
    transcript that are closest to the original timecodes, within ±pad seconds.
    Used when no quote text is available to guide a text-match.
    """
    abs_starts = [clip_start + w.get("start", 0.0) for w in words]
    abs_ends   = [clip_start + w.get("end",   0.0) for w in words]

    in_candidates = [
        (abs(t - orig_in), t) for t in abs_starts
        if orig_in - pad <= t <= orig_in + pad
    ]
    out_candidates = [
        (abs(t - orig_out), t) for t in abs_ends
        if orig_out - pad <= t <= orig_out + pad
    ]

    snapped_in  = min(in_candidates,  key=lambda x: x[0])[1] if in_candidates  else orig_in
    snapped_out = min(out_candidates, key=lambda x: x[0])[1] if out_candidates else orig_out
    return round(snapped_in, 3), round(snapped_out, 3)

# ── Per-pull reconciler ────────────────────────────────────────────────────────
def _base_result(pull):
    orig_in  = pull["in_seconds"]
    orig_out = pull["out_seconds"]
    return {
        "order":          pull["order"],
        "token":          pull["token"],
        "in_tc":          pull["in_tc"],
        "out_tc":         pull["out_tc"],
        "part_index":     pull["part_index"],
        "quote_text":     pull.get("quote_text", ""),
        "orig_in_s":      orig_in,
        "orig_out_s":     orig_out,
        "rec_in_s":       orig_in,
        "rec_out_s":      orig_out,
        "rec_in_tc":      pull["in_tc"],
        "rec_out_tc":     pull["out_tc"],
        "confidence":     1.0,
        "matched_text":   "",
        "delta_in":       0.0,
        "delta_out":      0.0,
        "segments":       [(orig_in, orig_out)],
        "n_internal_cuts":0,
        "n_gap_cuts":     0,
        "status":         "direct",
        "gap_after":      pull.get("gap_after", True),
    }

_PULL_ORDERING_LOOKBACK = 10.0  # seconds of slack before the cursor for interview pulls

# Common contractions + spoken-vs-written equivalents.  Applied to BOTH
# the script quote_text and each transcript word's lowercase form before
# tokenization, so "I've" / "I have", "gonna" / "going to", "they're" /
# "they are" all line up regardless of which form the script editor
# used and which Whisper produced.  This reliably lifts the alignment
# ratio on conversational interview content by a few percentage points
# and recovers cases where a single contraction was the only blocker
# to a clean script-conform match.
#
# Order matters: longer forms first so "wouldn't've" doesn't get
# half-eaten by "wouldn't" before the trailing "'ve" is handled.
_CONTRACTION_EXPANSIONS = [
    ("won't",     "will not"),
    ("can't",     "cannot"),
    ("shan't",    "shall not"),
    ("ain't",     "is not"),
    ("y'all",     "you all"),
    ("gonna",     "going to"),
    ("wanna",     "want to"),
    ("gotta",     "got to"),
    ("kinda",     "kind of"),
    ("sorta",     "sort of"),
    ("lemme",     "let me"),
    ("gimme",     "give me"),
    ("dunno",     "do not know"),
    ("'tis",      "it is"),
    ("o'clock",   "oclock"),    # collapse so "o" + "clock" don't split
    # — n't endings —
    ("n't",       " not"),
    # — 're / 'll / 've / 'd / 's / 'm —
    ("'re",       " are"),
    ("'ll",       " will"),
    ("'ve",       " have"),
    ("'d",        " would"),
    ("'m",        " am"),
    # "'s" is intentionally LEFT ALONE — it's ambiguous between "is"
    # ("she's tall") and possessive ("Dr. Young's house").  Trying
    # to expand it hurts more than it helps.
]

def _normalize_for_match(text):
    """Apply spoken/written contraction equivalents to `text` (already
    lowercased) so the tokenizer downstream produces aligned token
    sequences for either form.  See _CONTRACTION_EXPANSIONS for the
    rules and rationale."""
    if not text:
        return text
    for src, dst in _CONTRACTION_EXPANSIONS:
        text = text.replace(src, dst)
    return text


def fuzzy_locate_quote(transcript, quote_text, min_ratio=0.5):
    """Find where ``quote_text`` lives inside ``transcript`` (a list of
    word dicts) using fuzzy word-sequence matching.  Returns
    ``(start_s, end_s, confidence, matched_word_dicts)`` or ``None`` if
    no acceptable match is found.

    Used as a fallback when a pull's timecodes are wrong/missing but the
    script still carries the quoted text — Whisper's wording can drift
    from the script (filler words, punctuation, contractions), so this
    is intentionally forgiving.

    ``min_ratio`` is the fraction of target words that must appear in
    the matched region for the match to count.  0.5 means "at least
    half the words line up" — tight enough to avoid false positives,
    loose enough to handle script paraphrasing.
    """
    if not quote_text or not transcript:
        return None
    # Normalize contractions on the script side so "I've" lines up with
    # transcript "I have" (and vice versa) — same logic applied to the
    # transcript side below.
    target = re.findall(
        r"[a-z']+",
        _normalize_for_match(quote_text.lower()))
    # Strip very short tokens that produce noise (single letters, common stopwords
    # contribute little signal at this scale).  Below 4 tokens is too short
    # to fuzzy-match safely.
    if len(target) < 4:
        return None

    # Build a parallel array of clean transcript words + their source dicts.
    # A single contraction word ("I've") expands to multiple tokens
    # ("i", "have") all pointing at the same source dict, so a match
    # block that spans those still resolves to one word's timestamp.
    clean = []
    for w in transcript:
        if w.get("break"):
            continue
        raw = _normalize_for_match((w.get("word") or "").lower())
        for tok in re.findall(r"[a-z']+", raw):
            clean.append((tok, w))
    if len(clean) < len(target):
        return None
    clean_words = [c[0] for c in clean]

    # difflib finds the longest contiguous block of matches; combined matches
    # across blocks give us the full match span.
    from difflib import SequenceMatcher
    sm = SequenceMatcher(None, target, clean_words, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size > 0]
    if not blocks:
        return None
    matched_words = sum(b.size for b in blocks)
    ratio = matched_words / float(len(target))
    if ratio < min_ratio:
        return None

    # Span the transcript region from the first to last matched position.
    b_start = min(b.b for b in blocks)
    b_end   = max(b.b + b.size for b in blocks)
    if b_end <= b_start:
        return None

    matched_dicts = [clean[i][1] for i in range(b_start, b_end)]
    start_s = float(matched_dicts[0].get("start", 0.0))
    end_s   = float(matched_dicts[-1].get("end",   start_s))
    if end_s <= start_s:
        return None
    return start_s, end_s, ratio, matched_dicts


def script_conform_segments(transcript_words, quote_text,
                              min_run=None, merge_gap_s=None,
                              min_ratio=None):
    """Align a script ``quote_text`` against a list of ``transcript_words``
    (the dicts that fall inside the matched range) and return a list of
    ``(start_s, end_s)`` segments representing what the editor should
    *keep* — gaps in the script that exist in the tape become cuts.

    Returns ``(segments, ratio, n_cuts, matched_dicts)`` or ``None`` when
    the alignment is too poor to trust (caller should fall back to the
    single-segment match instead of producing nonsense cuts).

    The algorithm:
      1. Tokenize both inputs (lowercase, alphanumeric only) preserving
         a parallel array of source dicts for the transcript side
      2. SequenceMatcher.get_matching_blocks() — runs of consecutive
         matching tokens
      3. Drop blocks shorter than ``min_run`` (coincidental matches)
      4. Sort by transcript position; emit one segment per surviving
         block using the first/last transcript word's timestamps
      5. Merge segments whose inter-cut gap < ``merge_gap_s`` — those
         are just natural pauses, not real cuts to make
      6. Sanity-check: total aligned ratio must clear ``min_ratio``,
         otherwise return None
    """
    if min_run is None:
        min_run = SCRIPT_CONFORM_MIN_RUN
    if merge_gap_s is None:
        merge_gap_s = SCRIPT_CONFORM_MERGE_GAP_S
    if min_ratio is None:
        min_ratio = SCRIPT_CONFORM_MIN_RATIO

    if not quote_text or not transcript_words:
        return None

    # Same contraction-normalisation as fuzzy_locate_quote — "I've" /
    # "I have", "gonna" / "going to" all line up regardless of which
    # form the script editor or Whisper used.  See _CONTRACTION_EXPANSIONS.
    script_tokens = re.findall(
        r"[a-z']+",
        _normalize_for_match(quote_text.lower()))
    if len(script_tokens) < 4:
        return None

    # Parallel arrays: clean tokens + source dicts.  Skip "break" markers.
    # Contraction words expand to multiple tokens, each pointing at the
    # SAME source dict — block matches that span them still resolve to
    # the original word's timestamps.
    trans = []
    for w in transcript_words:
        if w.get("break"):
            continue
        raw = _normalize_for_match((w.get("word") or "").lower())
        for tok in re.findall(r"[a-z']+", raw):
            trans.append((tok, w))
    if len(trans) < min_run:
        return None
    trans_tokens = [t[0] for t in trans]

    from difflib import SequenceMatcher
    sm = SequenceMatcher(None, script_tokens, trans_tokens, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks()
              if b.size >= min_run]
    if not blocks:
        return None

    # Total aligned ratio drives the trust gate.
    matched = sum(b.size for b in blocks)
    ratio   = matched / float(len(script_tokens))
    if ratio < min_ratio:
        return None

    # Sort by transcript position so segments come out in playback order.
    blocks.sort(key=lambda b: b.b)

    # Build raw segments + the matched-word dicts that make them up.
    raw_segments = []
    matched_dicts = []
    for b in blocks:
        first = trans[b.b][1]
        last  = trans[b.b + b.size - 1][1]
        seg_start = float(first.get("start", 0.0))
        seg_end   = float(last.get("end", seg_start))
        if seg_end <= seg_start:
            continue
        raw_segments.append([seg_start, seg_end])
        matched_dicts.extend(trans[i][1] for i in range(b.b, b.b + b.size))

    if not raw_segments:
        return None

    # Merge segments whose inter-cut gap is shorter than merge_gap_s —
    # Whisper's natural sentence pauses shouldn't become cuts.
    merged = [raw_segments[0]]
    for seg in raw_segments[1:]:
        gap = seg[0] - merged[-1][1]
        if gap < merge_gap_s:
            merged[-1][1] = seg[1]
        else:
            merged.append(seg)

    segments = [(round(s[0], 4), round(s[1], 4)) for s in merged]
    n_cuts   = max(0, len(segments) - 1)
    return segments, ratio, n_cuts, matched_dicts


def reconcile_pull_from_session(pull, session_data):
    """Pull-Quotes fast path: use a pre-transcribed Interview Session
    JSON to resolve a pull *without* running Whisper.

    The script timecodes are precise (Whisper word-level start/end —
    exactly what Pull Quotes wrote), so we just look up the words that
    fall in [in_seconds, out_seconds] in the cached transcript and
    build the result directly.

    `session_data`: parsed contents of a `*.pb_session.json` (dict).
    Must contain a `transcript` list of word dicts with `start` / `end`.

    Returns the same result-dict shape as `reconcile_interview_pull`
    so callers (build_aaf / build_xml) can treat both paths identically.
    """
    in_s   = float(pull.get("in_seconds", 0.0))
    out_s  = float(pull.get("out_seconds", in_s))
    result = _base_result(pull)

    transcript = session_data.get("transcript") or []
    if not transcript:
        result["status"] = "no_transcript"
        result["_diag"]  = "session JSON had no transcript"
        return result

    # Resolve a "primary" media path on the session for downstream
    # waveform-editor use.  Prefer the audio_cache (mix WAV next to the
    # session JSON) so the waveform editor opens to the same audio
    # Pull Quotes used.  Fall back to the first non-video file.
    primary_audio = session_data.get("audio_cache") or ""
    if not primary_audio or not _safe_isfile(primary_audio):
        for p in session_data.get("media") or []:
            if p and _safe_isfile(p) and not is_video(p):
                primary_audio = p
                break
    result["source_audio"] = primary_audio

    # Pick the words whose [start, end] overlaps the pull's range.
    # We're forgiving on edges: a word counts if its midpoint falls
    # inside the range, or if it overlaps either edge.
    overlapping = []
    for w in transcript:
        if w.get("break"):
            continue
        ws = float(w.get("start", 0.0))
        we = float(w.get("end",   ws))
        if we < in_s or ws > out_s:
            continue
        overlapping.append(w)

    # Detect invalid timecodes (in > out, or out unreasonably far past audio
    # end) so we can fall back to the text-search path instead of silently
    # returning no_match.
    invalid_tcs = (out_s <= in_s)

    if invalid_tcs or not overlapping:
        # Text-fallback: a full transcript is available — try to locate the
        # quote by its words alone.  Tolerates script vs. Whisper wording
        # drift (filler words, contractions, light paraphrasing).
        quote_text = pull.get("quote_text") or ""
        fb = fuzzy_locate_quote(transcript, quote_text, min_ratio=0.5)
        if fb is not None:
            fb_start, fb_end, ratio, matched_dicts = fb
            overlapping = matched_dicts
            in_s, out_s = fb_start, fb_end
            result["_diag"] = (
                "TC-fallback text match ({:.0%} word overlap; "
                "ignored bad TCs in={:.1f} out={:.1f})".format(
                    ratio,
                    float(pull.get("in_seconds", 0.0)),
                    float(pull.get("out_seconds", 0.0)))
            )
            result["_text_fallback"] = True
        else:
            result["status"] = "no_match"
            if invalid_tcs:
                result["_diag"] = (
                    "invalid TCs (in={:.1f} > out={:.1f}) and quote text "
                    "didn't fuzzy-match the transcript".format(in_s, out_s))
            else:
                result["_diag"] = "no words in [{:.2f}, {:.2f}]".format(
                    in_s, out_s)
            return result

    # ── Mis-pointed TCs guard ────────────────────────────────────────
    # The previous block only catches in>out and out-of-range timecodes.
    # It misses a third failure mode: TCs that ARE valid (in<out, words
    # in range) but point at the WRONG audio — most commonly a
    # duplicate timecode copy-pasted across two different pulls in the
    # script.  Detect it by scoring how well the script quote fits the
    # words actually inside the TC window.  If the local fit is poor
    # AND the full transcript has a clearly better match elsewhere,
    # snap the pull to that location and flag it as text-fallback so
    # it lands in "needs review" with the right audio.
    #
    # Thresholds:
    #   LOCAL_OK   — at/above this, trust the TCs even if global beats them
    #   MARGIN     — global must beat local by at least this much
    if not result.get("_text_fallback"):
        quote_text_probe = pull.get("quote_text") or ""
        if quote_text_probe:
            LOCAL_OK = 0.30
            MARGIN   = 0.20
            local_fit = fuzzy_locate_quote(
                overlapping, quote_text_probe, min_ratio=0.0)
            local_ratio = local_fit[2] if local_fit else 0.0
            if local_ratio < LOCAL_OK:
                global_fit = fuzzy_locate_quote(
                    transcript, quote_text_probe, min_ratio=0.5)
                if (global_fit is not None
                        and global_fit[2] >= local_ratio + MARGIN):
                    fb_start, fb_end, ratio, matched_dicts = global_fit
                    overlapping = matched_dicts
                    in_s, out_s = fb_start, fb_end
                    result["_diag"] = (
                        "TCs pointed at wrong audio "
                        "(local fit {:.0%} at [{:.1f},{:.1f}]); "
                        "snapped via text-fallback "
                        "({:.0%} overlap at [{:.1f},{:.1f}])".format(
                            local_ratio,
                            float(pull.get("in_seconds", 0.0)),
                            float(pull.get("out_seconds", 0.0)),
                            ratio, fb_start, fb_end)
                    )
                    result["_text_fallback"] = True

    # Snap to the actual word boundaries we found — gives Whisper-precise
    # in/out points instead of the (possibly rounded) script timecodes.
    snapped_in  = float(overlapping[0].get("start", in_s))
    snapped_out = float(overlapping[-1].get("end",   out_s))

    # ── Script-conform: align the script text against the matched range
    # and emit one segment per aligned block, with gaps in the script
    # that exist in the tape becoming cuts.  Falls back to the paragraph-
    # distribution scheme below when alignment is too poor to trust.
    paragraphs   = pull.get("quote_paragraphs") or []
    quote_text   = pull.get("quote_text") or " ".join(paragraphs)
    conform_used = False
    conform_ratio = None
    segments     = None
    conform_words = None

    if SCRIPT_CONFORM_ENABLED and quote_text:
        if len(paragraphs) >= 2:
            # Per-paragraph: align each paragraph independently within a
            # rough time slice of the matched range.  This honours the
            # user's editorial paragraph structure AND keeps coincidental
            # token matches inside one paragraph from locking onto the
            # wrong place in another.
            n_para     = len(paragraphs)
            range_dur  = max(0.001, snapped_out - snapped_in)
            slice_dur  = range_dur / n_para
            all_segs   = []
            all_words  = []
            ratios     = []
            ok         = True
            for i, para in enumerate(paragraphs):
                slice_in  = snapped_in + i       * slice_dur
                slice_out = snapped_in + (i + 1) * slice_dur
                # Generous edges: take everything in the slice + a small
                # overlap into adjacent slices so a word straddling the
                # boundary still aligns.
                pad = min(2.0, 0.5 * slice_dur)
                para_words = [
                    w for w in overlapping
                    if (float(w.get("end", 0)) > slice_in - pad
                        and float(w.get("start", 0)) < slice_out + pad)
                ]
                conf = script_conform_segments(para_words, para)
                if conf is None:
                    ok = False
                    break
                segs, r, _cuts, mwords = conf
                all_segs.extend(segs)
                all_words.extend(mwords)
                ratios.append(r)
            if ok and all_segs:
                segments      = all_segs
                conform_words = all_words
                conform_ratio = sum(ratios) / max(1, len(ratios))
                conform_used  = True
        else:
            conf = script_conform_segments(overlapping, quote_text)
            if conf is not None:
                segs, r, _cuts, mwords = conf
                segments      = segs
                conform_words = mwords
                conform_ratio = r
                conform_used  = True

    if not conform_used:
        # Existing behaviour: one paragraph = one even time slice within
        # the matched range.  Approximate but matches the script
        # structure.  Single-paragraph pulls collapse to one segment.
        if len(paragraphs) >= 2:
            n_para  = len(paragraphs)
            seg_dur = (snapped_out - snapped_in) / n_para
            segments = [(round(snapped_in + i * seg_dur, 4),
                         round(snapped_in + (i + 1) * seg_dur, 4))
                        for i in range(n_para)]
            segments[-1] = (segments[-1][0], snapped_out)
        else:
            segments = [(snapped_in, snapped_out)]

    # Re-derive snapped_in/out from segments so any conform-driven
    # trim is reflected in the headline rec_in/out timecodes.
    if segments:
        snapped_in  = segments[0][0]
        snapped_out = segments[-1][1]

    # Words to attach to the result — use the conform-matched subset
    # when available (those are the words actually inside segments),
    # otherwise the full overlapping range.
    words_for_result = conform_words if conform_used else overlapping

    # Synthesise the matched-text from the words we kept.
    matched_text = " ".join(
        (w.get("word") or "").strip() for w in words_for_result
    ).strip()

    # Confidence: 1.0 for direct TC matches, 0.75 for text-fallback so
    # those land in NEEDS ATTENTION.  Script-conformed cuts get a small
    # bonus toward 1.0 based on alignment ratio (still ≥0.75 floor).
    if result.get("_text_fallback"):
        confidence = 0.75
    elif conform_used and conform_ratio is not None:
        confidence = min(1.0, 0.85 + 0.15 * conform_ratio)
    else:
        confidence = 1.0

    result.update({
        "segments":        segments,
        "rec_in_s":        snapped_in,
        "rec_out_s":       snapped_out,
        "rec_in_tc":       secs_tc(snapped_in),
        "rec_out_tc":      secs_tc(snapped_out),
        "delta_in":        round(snapped_in  - in_s,  3),
        "delta_out":       round(snapped_out - out_s, 3),
        "confidence":      confidence,
        "matched_text":    matched_text,
        "n_internal_cuts": max(0, len(segments) - 1),
        "n_gap_cuts":      0,
        "status":          "ok",
        "words":           list(words_for_result),
        "_pq_session":     session_data.get("id") or session_data.get("_file_path"),
        "_script_conformed": conform_used,
        "_conform_ratio":  conform_ratio,
    })
    return result


def reconcile_interview_pull(pull, transcript_file, pad=PAD_SECS, min_start_s=0.0):
    """
    Reconcile a single @PULL against its source transcript.

    Always transcribes the padded window so the audio conforms to the script
    regardless of whether the script contains quote text:

      - Quote text present  → Whisper match snaps in/out to actual speech
        boundaries and detects any internal editorial cuts.
      - No quote text (or match failed)  → Whisper word timestamps snap in/out
        to the nearest speech boundary within ±pad of each timecode.

    In both cases, when WAVEFORM_CONFORM is enabled a waveform blob pass runs
    on the same extracted clip to push edges to ms / sample precision — the
    same refinement applied to VO blocks.
    """
    orig_in  = pull["in_seconds"]
    orig_out = pull["out_seconds"]
    result   = _base_result(pull)
    result["source_audio"] = transcript_file or ""

    if not transcript_file:
        return result

    # ── Pull-result cache hit ───────────────────────────────────────────────────
    cached = pull_result_cache_load(pull, transcript_file, pad)
    if cached is not None:
        # Validate cached result against temporal ordering constraint.
        # If the cached match landed before the cursor, it's a stale result
        # from a previous run without the constraint — re-run without cache.
        if min_start_s <= 0.0 or cached.get("rec_in_s", 0.0) >= min_start_s - _PULL_ORDERING_LOOKBACK:
            return cached

    clip_start = max(0.0, orig_in - pad)
    clip_end   = orig_out + pad

    # Guard against sentinel out-points (e.g. 99:59:59 meaning "end of file").
    # Capping to MAX_EXTRACT_S prevents Whisper from transcribing an entire
    # multi-hour recording just to find a 30-second quote.
    if clip_end - clip_start > MAX_EXTRACT_S:
        clip_end = clip_start + MAX_EXTRACT_S

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        tmp_wav = tf.name
    try:
        ok, extract_err = extract_window(transcript_file, clip_start, clip_end, tmp_wav)
        if not ok:
            result["_diag"] = "extract_failed: {}".format(extract_err or "unknown")
            return result

        words = transcribe_clip(tmp_wav)
        if not words:
            # ── Diagnostic: re-run without VAD/no-speech filtering to see if
            # Whisper would find ANYTHING in this clip when restrictions are off
            raw_text = _transcribe_raw(tmp_wav)
            rms_db   = _clip_rms_db(tmp_wav)
            diag_str = "PULL_DIAG  order={}  token={}  tc={}-{}  rms={:.1f}dB  raw={}".format(
                result.get("order", "?"),
                result.get("token", "?"),
                result.get("in_tc",  "?"),
                result.get("out_tc", "?"),
                rms_db,
                repr(raw_text[:200]) if raw_text else "''")
            result["_diag"] = diag_str
            # Also write directly to the debug log so it appears regardless
            # of how the result dict is handled upstream.
            try:
                _dlog = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "_debug_run.log")
                with open(_dlog, "a", encoding="utf-8") as _f:
                    _f.write(diag_str + "\n")
            except Exception:
                pass
            return result

        # ── Temporal ordering constraint ────────────────────────────────────
        # If min_start_s is set (a cursor from the previous pull of the same
        # token), exclude any words that fall before it.  We keep a lookback
        # window so a match that begins slightly before the cursor boundary
        # (due to padding or a long silence) still resolves correctly.
        if min_start_s > 0.0:
            min_clip_s = max(0.0, min_start_s - _PULL_ORDERING_LOOKBACK - clip_start)
            words = [w for w in words if w.get("start", 0.0) >= min_clip_s]
            if not words:
                return result  # entire transcription window is before the cursor

        # Offset word timestamps from clip-relative → absolute file time
        # and store on the result so the waveform editor can display them.
        abs_words = [
            {**w, "start": round(w["start"] + clip_start, 4),
                  "end":   round(w["end"]   + clip_start, 4)}
            for w in words
        ]
        result["words"] = abs_words

        # ── Waveform blob detection for ms-level edge snapping ──────────────
        # Run on the same extracted clip window.  Blob timestamps come back
        # clip-relative (0 … clip_duration), so offset them into absolute
        # file time so refine_segment_endpoints can compare against the
        # absolute-time segments produced below.
        blobs = None
        if WAVEFORM_CONFORM:
            raw_blobs = detect_speech_blobs(tmp_wav)
            if raw_blobs:
                blobs = [
                    {**b,
                     "raw_start": b["raw_start"] + clip_start,
                     "raw_end":   b["raw_end"]   + clip_start,
                     "onset":     b["onset"]      + clip_start}
                    for b in raw_blobs
                ]

        quote   = pull.get("quote_text", "").strip()
        matched = False

        if quote:
            match = match_segments(quote, words)
            if match is not None and match["segments"]:
                matched  = True
                abs_segs = [
                    (round(clip_start + s, 3), round(clip_start + e, 3))
                    for s, e in match["segments"]
                ]
                # Always snap endpoints to matched speech boundaries.
                # Clamp within ±pad of the original timecodes so a poor
                # match can't drift the clip far from its intended position.
                new_in  = _clamp(abs_segs[0][0],  orig_in  - pad, orig_in  + pad)
                new_out = _clamp(abs_segs[-1][1], orig_out - pad, orig_out + pad)
                abs_segs[0]  = (new_in,  abs_segs[0][1])
                abs_segs[-1] = (abs_segs[-1][0], new_out)
                abs_segs = [(s, e) for s, e in abs_segs if s < e]
                if not abs_segs:
                    abs_segs = [(orig_in, orig_out)]

                if blobs:
                    abs_segs = refine_segment_endpoints(abs_segs, blobs)

                result.update({
                    "segments":        abs_segs,
                    "rec_in_s":        abs_segs[0][0],
                    "rec_out_s":       abs_segs[-1][1],
                    "rec_in_tc":       secs_tc(abs_segs[0][0]),
                    "rec_out_tc":      secs_tc(abs_segs[-1][1]),
                    "confidence":      match["confidence"],
                    "matched_text":    match["matched_text"],
                    "n_internal_cuts": match["n_internal_cuts"],
                    "n_gap_cuts":      match["n_gap_cuts"],
                    "delta_in":        round(abs_segs[0][0]  - orig_in,  3),
                    "delta_out":       round(abs_segs[-1][1] - orig_out, 3),
                    "status":          "ok" if match["confidence"] >= MATCH_THRESH
                                       else "low_confidence",
                })

        if not matched:
            # No quote text, or quote didn't match — snap to nearest speech
            # boundary within ±pad via Whisper word timestamps, then refine
            # to the waveform edge if blobs are available.
            snapped_in, snapped_out = _snap_to_speech(
                words, clip_start, orig_in, orig_out, pad)
            abs_segs = [(snapped_in, snapped_out)]
            if blobs:
                abs_segs = refine_segment_endpoints(abs_segs, blobs)

            result.update({
                "rec_in_s":   abs_segs[0][0],
                "rec_out_s":  abs_segs[-1][1],
                "rec_in_tc":  secs_tc(abs_segs[0][0]),
                "rec_out_tc": secs_tc(abs_segs[-1][1]),
                "delta_in":   round(abs_segs[0][0]  - orig_in,  3),
                "delta_out":  round(abs_segs[-1][1] - orig_out, 3),
                "segments":   abs_segs,
                "status":     "snapped",
            })

        # ── Cache ok results so re-runs skip transcription for matched pulls ────
        if result["status"] == "ok":
            pull_result_cache_save(pull, transcript_file, pad, result)
    finally:
        try: os.unlink(tmp_wav)
        except Exception: pass

    return result

# ── Waveform blob detection ─────────────────────────────────────────────────────

def detect_speech_blobs(audio_path,
                        silence_db=None,
                        min_speech_ms=None,
                        min_silence_ms=None):
    """
    Detect speech blobs (non-silent regions) in an audio file via RMS energy.

    Returns a list of dicts:
        {"raw_start": float, "raw_end": float, "onset": float}
    where raw_start/raw_end are file-seconds and onset is the first frame of
    sustained speech within the blob (skips breath/rustle at the blob head).

    Returns None if numpy is unavailable, ffmpeg fails, or no blobs are found.
    """
    if silence_db   is None: silence_db   = BLOB_SILENCE_DB
    if min_speech_ms  is None: min_speech_ms  = BLOB_MIN_SPEECH_MS
    if min_silence_ms is None: min_silence_ms = BLOB_MIN_SILENCE_MS

    try:
        import numpy as np
    except ImportError:
        return None

    SR     = 4000       # Hz — enough resolution for energy detection
    WIN_MS = 20         # ms per RMS window
    WIN_N  = int(SR * WIN_MS / 1000)   # samples per window = 80

    # ── Extract raw PCM via ffmpeg (pipe to avoid temp file) ──────────────────
    cmd = _ffmpeg_cmd() + [
        "-y", "-v", "quiet",
        "-i", audio_path,
        "-ac", "1",
        "-ar", str(SR),
        "-f", "f32le",
        "pipe:1",
    ]
    try:
        r = _run(cmd, capture_output=True, timeout=300)
        if r.returncode != 0:
            return None
        data = np.frombuffer(r.stdout, dtype=np.float32)
    except Exception:
        return None

    if len(data) < WIN_N * 2:
        return None

    # ── RMS per window ────────────────────────────────────────────────────────
    n_wins  = len(data) // WIN_N
    frames  = data[:n_wins * WIN_N].reshape(n_wins, WIN_N)
    rms     = np.sqrt(np.mean(frames ** 2, axis=1))
    rms_db  = 20.0 * np.log10(np.maximum(rms, 1e-9))
    is_sp   = rms_db >= silence_db   # bool array, True = speech

    min_sp_frames  = max(1, int(min_speech_ms  / WIN_MS))
    min_sil_frames = max(1, int(min_silence_ms / WIN_MS))

    def frame_to_s(f):
        return round(f * WIN_MS / 1000.0, 4)

    # ── Build run-length list: [(start_frame, end_frame, is_speech)] ──────────
    transitions = np.where(np.diff(is_sp.astype(np.int8)) != 0)[0] + 1
    bounds      = np.concatenate([[0], transitions, [n_wins]])
    runs = [(int(bounds[i]), int(bounds[i+1]), bool(is_sp[bounds[i]]))
            for i in range(len(bounds) - 1)]

    # ── Pass 1: merge short silences between speech regions ───────────────────
    merged = []
    i = 0
    while i < len(runs):
        s, e, speech = runs[i]
        if (not speech
                and (e - s) < min_sil_frames
                and merged and merged[-1][2]          # preceded by speech
                and i + 1 < len(runs) and runs[i+1][2]):  # followed by speech
            # Absorb: extend previous speech run through this silence and the next
            prev_s, _, _ = merged[-1]
            _, next_e, _ = runs[i + 1]
            merged[-1] = (prev_s, next_e, True)
            i += 2
        else:
            merged.append((s, e, speech))
            i += 1

    # ── Pass 2: keep only speech runs long enough ─────────────────────────────
    blobs = []
    for s, e, speech in merged:
        if speech and (e - s) >= min_sp_frames:
            blobs.append({"raw_start": frame_to_s(s), "raw_end": frame_to_s(e)})

    if not blobs:
        return None

    # ── Find onset within each blob (first sustained speech, skip breath) ─────
    ONSET_CONSEC = 3   # consecutive above-threshold windows = real speech start
    for blob in blobs:
        start_f = max(0, int(blob["raw_start"] / (WIN_MS / 1000.0)))
        end_f   = min(n_wins, int(blob["raw_end"]   / (WIN_MS / 1000.0)))
        onset_f = start_f
        consec  = 0
        for fi in range(start_f, end_f):
            if is_sp[fi]:
                consec += 1
                if consec >= ONSET_CONSEC:
                    onset_f = fi - (ONSET_CONSEC - 1)
                    break
            else:
                consec = 0
        blob["onset"] = frame_to_s(onset_f)

    return blobs


def transcribe_with_blobs(audio_path, blobs):
    """
    Stitch all speech blobs into one WAV, transcribe once, map timestamps back.
    Calling the Whisper encoder once avoids MKL memory exhaustion that occurs
    when transcribe_clip is called per-blob in a loop.
    """
    if not blobs:
        return []

    tmp_clips      = []   # paths to per-blob WAV clips (cleaned up in finally)
    mapping        = []   # {stitched_start, stitched_end, file_offset}
    stitched_path  = None
    cursor         = 0.0

    try:
        # ── 1. Extract each blob to a temp WAV ─────────────────────────────────
        for blob in blobs:
            tf = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tf.close()
            ok, _ = extract_window(audio_path, blob["raw_start"], blob["raw_end"], tf.name)
            if not ok:
                try: os.unlink(tf.name)
                except Exception: pass
                continue
            duration = blob["raw_end"] - blob["raw_start"]
            tmp_clips.append(tf.name)
            mapping.append({
                "stitched_start": cursor,
                "stitched_end":   cursor + duration,
                "file_offset":    blob["raw_start"],
            })
            cursor += duration

        if not tmp_clips:
            return []

        # ── 2. Stitch clips into one WAV via Python's wave module ───────────────
        if len(tmp_clips) == 1:
            stitched_path = tmp_clips.pop()   # reuse; nothing extra to clean up
        else:
            tf_out = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tf_out.close()
            stitched_path = tf_out.name
            with wave.open(stitched_path, "wb") as out_w:
                params_written = False
                for p in tmp_clips:
                    with wave.open(p, "rb") as in_w:
                        if not params_written:
                            out_w.setparams(in_w.getparams())
                            params_written = True
                        out_w.writeframes(in_w.readframes(in_w.getnframes()))

        # ── 3. Single model call ────────────────────────────────────────────────
        raw_words = transcribe_clip(stitched_path)

    finally:
        for p in tmp_clips:
            try: os.unlink(p)
            except Exception: pass
        if stitched_path:
            try: os.unlink(stitched_path)
            except Exception: pass

    # ── 4. Map stitched timestamps back to original file positions ──────────────
    all_words = []
    for w in raw_words:
        ws = w["start"]
        for m in mapping:
            if m["stitched_start"] - 0.05 <= ws < m["stitched_end"] + 0.05:
                offset = m["file_offset"] - m["stitched_start"]
                all_words.append({
                    "word":  w["word"],
                    "start": round(ws + offset, 4),
                    "end":   round(w["end"] + offset, 4),
                })
                break
    return all_words


def detect_silence_splits(audio_path, silence_db=None,
                          min_silence_s=None, max_chunk_s=None):
    """
    Analyse audio_path for silence regions >= min_silence_s and return a list
    of (chunk_start, chunk_end) tuples that tile the entire file end-to-end.

    Chunks are split at the midpoint of each qualifying silence gap so that
    every chunk begins and ends in silence, giving Whisper clean context on
    both ends.  Any chunk longer than max_chunk_s is force-split at its
    midpoint regardless of silence content.

    Returns list of (start_s, end_s) floats, or None on failure.
    """
    if silence_db    is None: silence_db    = BLOB_SILENCE_DB
    if min_silence_s is None: min_silence_s = CHUNK_SILENCE_S
    if max_chunk_s   is None: max_chunk_s   = CHUNK_MAX_S

    try:
        import numpy as np
    except ImportError:
        return None

    SR     = 4000
    WIN_MS = 20
    WIN_N  = int(SR * WIN_MS / 1000)

    cmd = _ffmpeg_cmd() + [
        "-y", "-v", "quiet",
        "-i", audio_path,
        "-ac", "1", "-ar", str(SR), "-f", "f32le", "pipe:1",
    ]
    try:
        r = _run(cmd, capture_output=True, timeout=300)
        if r.returncode != 0:
            return None
        data = np.frombuffer(r.stdout, dtype=np.float32)
    except Exception:
        return None

    if len(data) < WIN_N * 2:
        return None

    n_wins  = len(data) // WIN_N
    total_s = round(n_wins * WIN_MS / 1000.0, 3)
    frames  = data[:n_wins * WIN_N].reshape(n_wins, WIN_N)
    rms     = np.sqrt(np.mean(frames ** 2, axis=1))
    rms_db  = 20.0 * np.log10(np.maximum(rms, 1e-9))
    is_sil  = rms_db < silence_db

    def frame_to_s(f):
        return round(f * WIN_MS / 1000.0, 3)

    min_sil_frames = max(1, int(min_silence_s * 1000 / WIN_MS))

    # Find silence runs >= min_silence_s and collect their midpoints as splits
    transitions = np.where(np.diff(is_sil.astype(np.int8)) != 0)[0] + 1
    bounds      = np.concatenate([[0], transitions, [n_wins]])
    split_points = []
    for i in range(len(bounds) - 1):
        s, e = int(bounds[i]), int(bounds[i + 1])
        if is_sil[s] and (e - s) >= min_sil_frames:
            split_points.append(frame_to_s((s + e) // 2))

    # Build initial chunk list from split midpoints
    edges  = [0.0] + split_points + [total_s]
    chunks = [(round(edges[i], 3), round(edges[i + 1], 3))
              for i in range(len(edges) - 1)
              if edges[i + 1] - edges[i] >= 1.0]   # skip sub-second slivers

    # Force-split any chunk that exceeds max_chunk_s
    final = []
    for cs, ce in chunks:
        while ce - cs > max_chunk_s:
            mid = round(cs + (ce - cs) / 2.0, 3)
            final.append((cs, mid))
            cs = mid
        final.append((cs, ce))

    return final if final else None


def transcribe_in_chunks(audio_path, chunks, progress_cb=None):
    """
    Transcribe audio_path in independent segments defined by `chunks`
    (list of (start_s, end_s) pairs from detect_silence_splits).

    Each chunk is extracted to a temp WAV and transcribed by Whisper
    separately, then word timestamps are offset back to absolute file
    position.  Returns a complete, gap-free word list for the whole file.

    ``progress_cb``, if provided, is called as ``progress_cb(audio_s)``
    where ``audio_s`` is the absolute audio offset of the last completed
    segment.  Used by the Script→Session UI to show "of HH:MM:SS (NN%)".
    """
    all_words = []
    for chunk_start, chunk_end in chunks:
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                tmp = tf.name
            ok, _ = extract_window(audio_path, chunk_start, chunk_end, tmp)
            if not ok:
                continue
            # Wrap the inner callback so absolute audio offset is reported
            # (transcribe_clip emits offsets relative to its chunk only).
            inner_cb = None
            if progress_cb is not None:
                def inner_cb(seg_end_local, _co=chunk_start):
                    try:
                        progress_cb(seg_end_local + _co)
                    except Exception:
                        pass
            words = transcribe_clip(tmp, progress_cb=inner_cb)
            for w in words:
                all_words.append({
                    "word":  w["word"],
                    "start": round(w["start"] + chunk_start, 4),
                    "end":   round(w["end"]   + chunk_start, 4),
                })
        finally:
            if tmp:
                try: os.unlink(tmp)
                except Exception: pass
    return all_words


def refine_segment_endpoints(segments, blobs, snap_thresh=None):
    """
    Snap matched segment edges to waveform blob boundaries when they are close:
      - in-points  → snapped to blob["onset"]   (first sustained speech)
      - out-points → snapped to blob["raw_end"]  (waveform silence start)

    Only snaps when the Whisper-derived edge falls within snap_thresh seconds
    of a blob boundary AND inside that blob's range.

    Returns a new segment list (same length as input).
    """
    if snap_thresh is None:
        snap_thresh = BLOB_SNAP_THRESH
    if not blobs or not segments:
        return segments

    def containing_blob(ts):
        for b in blobs:
            if b["raw_start"] <= ts <= b["raw_end"]:
                return b
        return None

    refined = []
    for seg_in, seg_out in segments:
        new_in  = seg_in
        new_out = seg_out

        b_in = containing_blob(seg_in)
        if b_in and abs(seg_in - b_in["onset"]) < snap_thresh:
            new_in = b_in["onset"]

        b_out = containing_blob(seg_out)
        if b_out and abs(seg_out - b_out["raw_end"]) < snap_thresh:
            new_out = b_out["raw_end"]

        refined.append((new_in, new_out))
    return refined


def _vo_base_result(vo_block):
    """Build the skeleton result dict for a VO block."""
    return {
        "order":          vo_block["order"],
        "vo_id":          vo_block["id"],
        "token":          "VO_PART_{:02d}".format(vo_block["part_index"]),
        "part_index":     vo_block["part_index"],
        "quote_text":     vo_block["text"],
        "orig_in_s":      0.0,
        "orig_out_s":     0.0,
        "rec_in_s":       0.0,
        "rec_out_s":      0.0,
        "rec_in_tc":      "00:00:00",
        "rec_out_tc":     "00:00:00",
        "in_tc":          "00:00:00",
        "out_tc":         "00:00:00",
        "confidence":     0.0,
        "matched_text":   "",
        "delta_in":       0.0,
        "delta_out":      0.0,
        "segments":       [],
        "n_internal_cuts":0,
        "n_gap_cuts":     0,
        "takes_data":     [],
        "status":         "not_run",
        "is_vo":          True,
        "gap_after":      vo_block.get("gap_after", True),
    }


def reconcile_vo_part(vo_blocks, takes):
    """
    Reconcile all VO blocks for one script part in sequence, maintaining a
    per-take time cursor so each block only searches the portion of the audio
    that follows the previous match.  This prevents the matcher from getting
    lost inside very long takes (e.g. 2000+ word continuous sessions).

    Selection rule: among all takes, prefer the match with the latest start
    timestamp — i.e. the *last* recorded iteration of a line wins, so false
    starts and warm-up reads are naturally discarded in favour of the final
    delivery.

    Returns a list of result dicts (one per vo_block, in the same order as
    the input list).
    """
    _LOOKBACK_S = 15.0   # seconds before cursor to include as safety margin

    # Unpack takes once
    unpacked = []
    for entry in takes:
        if len(entry) == 5:
            audio_words, v_offset, vpath, apath, blobs = entry
        else:
            audio_words, v_offset, vpath, apath = entry
            blobs = None
        unpacked.append((audio_words, v_offset, vpath, apath, blobs))

    # Per-take cursor: seconds — don't search before (cursor - LOOKBACK)
    cursors = [0.0] * len(unpacked)

    results = []
    for vo_block in sorted(vo_blocks, key=lambda b: b["order"]):
        base = _vo_base_result(vo_block)
        text = vo_block.get("text", "").strip()

        if not text:
            base["status"] = "no_quote"
            results.append(base)
            continue

        if not unpacked:
            base["status"] = "no_transcript"
            results.append(base)
            continue

        best_match      = None
        best_segs       = None
        best_take_index = 0
        best_start_s    = -1.0
        best_confidence = -1.0
        best_v_offset   = 0.0
        best_vpath      = None
        best_apath      = None
        takes_data      = []

        for ti, (audio_words, v_offset, vpath, apath, blobs) in enumerate(unpacked):
            if not audio_words:
                takes_data.append(None)
                continue

            # Window: only search words at or after (cursor - lookback)
            window_start = max(0.0, cursors[ti] - _LOOKBACK_S)
            window = [w for w in audio_words if w.get("start", 0.0) >= window_start]
            if not window:
                takes_data.append(None)
                continue

            # _match_best finds the highest-confidence occurrence within
            # this window.  It also advances past failed anchors (truncated
            # false starts, slates) instead of giving up on a None return,
            # so multi-take files with intros/retakes are handled correctly.
            match = _match_best(text, window)
            if match is None or not match["segments"]:
                takes_data.append(None)
                continue

            segs = refine_segment_endpoints(match["segments"], blobs)
            seg_start  = segs[0][0] if segs else 0.0
            match_conf = match.get("confidence", 0.0)

            takes_data.append({
                "segments": segs,
                "vpath":    vpath,
                "apath":    apath,
                "v_offset": v_offset,
            })

            # Winner selection: confidence is the primary key so that a
            # high-quality match in any take beats a spurious match in another
            # file with a later timestamp.  When confidence is within 0.05 of
            # the current best (same-quality, typical for multiple takes of the
            # same content) fall back to latest-start so the final delivery wins.
            is_better = (
                match_conf > best_confidence + 0.05 or
                (abs(match_conf - best_confidence) <= 0.05 and
                 seg_start > best_start_s)
            )
            if is_better:
                best_start_s    = seg_start
                best_confidence = match_conf
                best_match      = match
                best_segs       = segs
                best_take_index = ti
                best_v_offset   = v_offset
                best_vpath      = vpath
                best_apath      = apath

        # Filter out None placeholders for takes_data stored on result
        takes_data_clean = [td for td in takes_data if td is not None]

        if best_match is None or best_segs is None:
            base["status"] = "no_match"
            # Attach the first available take's audio so the waveform editor
            # can open and the user can set IN/OUT points manually.
            if unpacked:
                base["source_audio"] = unpacked[0][3]
            # Diagnostic: record why the match failed for each take
            diag_parts = []
            for ti, (aw, _vo, _vp, ap, _bl) in enumerate(unpacked):
                cursor_s     = cursors[ti]
                window_start = max(0.0, cursor_s - _LOOKBACK_S)
                in_window    = [w for w in (aw or []) if w.get("start", 0) >= window_start]
                note = "take {}: cursor={:.1f}s  window_start={:.1f}s  words_in_window={}".format(
                    ti, cursor_s, window_start, len(in_window))
                if not aw:
                    note += "  [NO TRANSCRIPTION]"
                elif not in_window:
                    note += "  [WINDOW EMPTY — cursor past end of audio]"
                diag_parts.append(note)
            base["diag"] = " | ".join(diag_parts)
            results.append(base)
            continue

        # Merge any overlapping/duplicate segments (can arise from multi-chunk matches)
        merged = []
        for seg in sorted(best_segs, key=lambda s: s[0]):
            if merged and seg[0] < merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], seg[1])
            else:
                merged.append(list(seg))
        best_segs = merged

        # Advance cursor for the winning take so the next block starts here.
        # Cursor is advanced to the actual last word end — before the tail —
        # so the next block doesn't search unnecessarily far ahead.
        cursors[best_take_index] = best_segs[-1][1]

        # Extend the last segment by VO_TAIL_SECS so Whisper's last-word
        # timestamp doesn't abruptly cut off natural decay / room tone.
        # Doing this at reconcile time (rather than silently at export) means
        # the tail is visible and editable in the waveform editor, so manual
        # adjustments are respected absolutely at export.
        best_segs[-1][1] += VO_TAIL_SECS

        abs_in  = best_segs[0][0]
        abs_out = best_segs[-1][1]
        conf    = round(best_match["confidence"], 3)

        best_apath = unpacked[best_take_index][3] if best_take_index < len(unpacked) else ""
        base.update({
            "takes_data":      takes_data_clean,
            "best_take_index": best_take_index,
            "segments":        best_segs,
            "rec_in_s":        abs_in,
            "rec_out_s":       abs_out,
            "rec_in_tc":       secs_tc(abs_in),
            "rec_out_tc":      secs_tc(abs_out),
            "in_tc":           secs_tc(abs_in),
            "out_tc":          secs_tc(abs_out),
            "confidence":      conf,
            "matched_text":    best_match["matched_text"],
            "n_internal_cuts": best_match["n_internal_cuts"],
            "n_gap_cuts":      best_match["n_gap_cuts"],
            "status":          "ok" if conf >= MATCH_THRESH else "low_confidence",
            "source_audio":    best_apath,
        })
        results.append(base)

    return results


def reconcile_vo_block(vo_block, takes):
    """Single-block wrapper kept for error-path / legacy callers."""
    results = reconcile_vo_part([vo_block], takes)
    return results[0] if results else _vo_base_result(vo_block)

# ── XML builder ────────────────────────────────────────────────────────────────
def f2fr(secs, fps):
    return round(secs * fps)

def make_rate(el, fps):
    _NTSC_MAP = {
        23.976: 24, 47.952: 48,
        29.97:  30, 59.94:  60,
    }
    ntsc  = "FALSE"
    base  = fps
    for frac, integer in _NTSC_MAP.items():
        if abs(fps - frac) < 0.01:
            base = integer
            ntsc = "TRUE"
            break
    r = SubElement(el, "rate")
    SubElement(r, "timebase").text = str(int(round(base)))
    SubElement(r, "ntsc").text     = ntsc

def make_tc(el, fps):
    tc = SubElement(el, "timecode")
    make_rate(tc, fps)
    SubElement(tc, "string").text        = "00:00:00:00"
    SubElement(tc, "frame").text         = "0"
    SubElement(tc, "displayformat").text = "NDF"

def detect_av_offset(audio_path, video_path, search_secs=60, sr=1000):
    if not audio_path or not video_path:
        return 0.0
    try:
        import numpy as np
    except ImportError:
        return 0.0

    def extract_mono(src_path, duration, out_sr):
        with tempfile.NamedTemporaryFile(suffix='.raw', delete=False) as tf:
            tmp = tf.name
        try:
            cmd = _ffmpeg_cmd() + [
                '-y', '-v', 'quiet',
                '-i', src_path,
                '-t', str(duration),
                '-ac', '1',
                '-ar', str(out_sr),
                '-f', 'f32le',
                tmp
            ]
            r = _run(cmd, capture_output=True, timeout=60)
            if r.returncode != 0:
                return None
            with open(tmp, 'rb') as _f:
                data = np.frombuffer(_f.read(), dtype=np.float32)
            rms = np.sqrt(np.mean(data ** 2))
            if rms > 0:
                data = data / rms
            return data
        except Exception:
            return None
        finally:
            try: os.unlink(tmp)
            except Exception: pass

    a_data = extract_mono(audio_path, search_secs, sr)
    v_data = extract_mono(video_path, search_secs, sr)

    if a_data is None or v_data is None:
        return 0.0
    if len(a_data) < sr or len(v_data) < sr:
        return 0.0

    n      = len(a_data) + len(v_data) - 1
    n_fft  = 1 << (n - 1).bit_length()
    A      = np.fft.rfft(a_data, n=n_fft)
    V      = np.fft.rfft(v_data, n=n_fft)
    corr   = np.fft.irfft(V * np.conj(A), n=n_fft)

    peak   = int(np.argmax(corr))
    if peak > n_fft // 2:
        peak -= n_fft
    offset = round(peak / sr, 3)

    if abs(offset) > search_secs * 0.9:
        return 0.0

    return offset

def _extract_mono(src_path, duration, out_sr):
    """Extract mono audio from any media file as float32 numpy array."""
    import numpy as np
    with tempfile.NamedTemporaryFile(suffix='.raw', delete=False) as tf:
        tmp = tf.name
    try:
        cmd = _ffmpeg_cmd() + [
            '-y', '-v', 'quiet',
            '-i', src_path,
            '-t', str(duration),
            '-ac', '1',
            '-ar', str(out_sr),
            '-f', 'f32le',
            tmp
        ]
        r = _run(cmd, capture_output=True, timeout=300,
                           encoding='utf-8', errors='replace')
        if r.returncode != 0:
            return None
        with open(tmp, 'rb') as _f:
            data = np.frombuffer(_f.read(), dtype=np.float32)
        rms = np.sqrt(np.mean(data ** 2))
        if rms > 0:
            data = data / rms
        return data
    except Exception:
        return None
    finally:
        try: os.unlink(tmp)
        except Exception: pass


_duration_cache = {}   # normpath.lower() → (mtime, duration_s)

# ── Audio signature (cross-machine PQ consumption safety) ─────────────
# Bake a per-file fingerprint into every PQ artifact (session, episode,
# transcript sidecar) so reconciliation on a different machine can
# confirm the local audio is the same one the transcript was made from.
# Cheap checks first; sha256 is authoritative but only run when the
# fast checks all pass.  ~1 s per 100 MB of audio.

def audio_signature(path):
    """Return a stable fingerprint dict for `path`, or {} on IO error.
    Never raises.  Keys are optional — callers must tolerate any subset.

    Keys:
      size:        file size in bytes
      duration_s:  audio duration, seconds (3-decimal rounded)
      sample_rate: Hz (0 if probe failed)
      channels:    channel count (0 if probe failed)
      sha256:      hex SHA-256 of the entire file
    """
    if not path or not os.path.isfile(path):
        return {}
    sig = {}
    try:
        sig["size"] = os.path.getsize(path)
    except OSError:
        return sig
    try:
        sig["duration_s"] = round(float(get_media_duration(path) or 0.0), 3)
    except Exception:
        sig["duration_s"] = 0.0
    try:
        _v, a = _concat_stream_sig(path)
        if a and len(a) >= 3:
            sig["sample_rate"] = int(a[1])
            sig["channels"]    = int(a[2])
    except Exception:
        pass
    try:
        h = hashlib.sha256()
        with open(path, "rb") as _f:
            for _chunk in iter(lambda: _f.read(1 << 20), b""):
                h.update(_chunk)
        sig["sha256"] = h.hexdigest()
    except OSError:
        pass
    return sig


def audio_signature_matches(sig, path):
    """Verify `sig` against `path`'s current content.
    Returns (ok: bool, reason: str).  Cheap checks first — short-circuits
    on any mismatch.  A missing / empty `sig` returns (True, "no
    signature") so legacy PQ files without baked signatures still work.
    """
    if not sig:
        return True, "no stored signature"
    if not path or not os.path.isfile(path):
        return False, "local file not found"
    try:
        actual_size = os.path.getsize(path)
    except OSError:
        return False, "local file unreadable"
    if sig.get("size") and int(sig["size"]) != actual_size:
        return False, "size mismatch ({} stored vs {} local)".format(
            sig["size"], actual_size)
    if sig.get("duration_s"):
        try:
            actual_dur = round(float(get_media_duration(path) or 0.0), 3)
        except Exception:
            actual_dur = 0.0
        # 100 ms tolerance absorbs tiny transcode-driven drift.
        if abs(float(sig["duration_s"]) - actual_dur) > 0.1:
            return False, ("duration mismatch ({:.2f}s stored vs "
                           "{:.2f}s local)".format(
                               float(sig["duration_s"]), actual_dur))
    if sig.get("sample_rate") or sig.get("channels"):
        try:
            _v, a = _concat_stream_sig(path)
        except Exception:
            a = None
        if a and len(a) >= 3:
            if sig.get("sample_rate") and int(a[1]) != int(sig["sample_rate"]):
                return False, "sample-rate mismatch ({} stored vs {} local)".format(
                    sig["sample_rate"], a[1])
            if sig.get("channels") and int(a[2]) != int(sig["channels"]):
                return False, "channel-count mismatch ({} stored vs {} local)".format(
                    sig["channels"], a[2])
    if sig.get("sha256"):
        try:
            h = hashlib.sha256()
            with open(path, "rb") as _f:
                for _chunk in iter(lambda: _f.read(1 << 20), b""):
                    h.update(_chunk)
            actual_hash = h.hexdigest()
        except OSError:
            return False, "local file unreadable during hash"
        if actual_hash != sig["sha256"]:
            return False, "sha256 mismatch (bytes differ)"
    return True, "match"


def _probe_duration(path):
    """Return file duration in seconds, or 0 on failure.  Result is cached in
    memory so repeated calls for the same file skip the ffprobe subprocess."""
    key = os.path.normpath(path).lower()
    mtime = _safe_getmtime(path)
    cached = _duration_cache.get(key)
    if cached is not None and abs(cached[0] - mtime) < 1:
        return cached[1]
    # Same probe as get_media_duration but with a caching layer and a
    # 0-on-failure contract (callers here do arithmetic on the result).
    dur = get_media_duration(path) or 0
    _duration_cache[key] = (mtime, dur)
    return dur


# Cache for extracted video audio (keyed by video path) — in-memory, cleared after build
_video_audio_cache = {}   # path → (vid_data_np, vid_dur)


# ── Persistent disk cache for detect_rx_offset results ─────────────────────────
def _rx_offset_cache_path(rx_audio_path, video_path):
    """Return the disk-cache path for a detect_rx_offset result, or None."""
    if not _cache_dir:
        return None
    rx_mtime  = round(_safe_getmtime(rx_audio_path), 2)
    vid_mtime = round(_safe_getmtime(video_path),    2)
    key_obj = {
        "rx":      os.path.abspath(rx_audio_path),
        "rx_mt":   rx_mtime,
        "vid":     os.path.abspath(video_path),
        "vid_mt":  vid_mtime,
        "version": CACHE_VERSION,
    }
    digest = hashlib.md5(
        json.dumps(key_obj, sort_keys=True).encode()
    ).hexdigest()[:16]
    return os.path.join(_cache_dir, "rxoff_{}.json".format(digest))

def _rx_offset_cache_load(rx_audio_path, video_path):
    cp = _rx_offset_cache_path(rx_audio_path, video_path)
    if not cp or not os.path.exists(cp):
        return None
    try:
        with open(cp, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("offset")   # float or None
    except Exception:
        return None

def _rx_offset_cache_save(rx_audio_path, video_path, offset):
    cp = _rx_offset_cache_path(rx_audio_path, video_path)
    if not cp:
        return
    try:
        os.makedirs(os.path.dirname(cp), exist_ok=True)
        with open(cp, "w", encoding="utf-8") as f:
            json.dump({"offset": float(offset)}, f)
    except Exception:
        pass


def detect_rx_offset(rx_audio_path, video_path, sr=1000):
    """Detect where an AudioSuite-rendered (RX) audio clip appears in a video.

    Returns the offset T (seconds) such that position 0 in the RX audio
    corresponds to position T in the video.  The caller should set
    v_offset = -T  so that  v_src_in = src_in_s - (-T) = src_in_s + T.

    Falls back to 0.0 on any failure.
    """
    if not rx_audio_path or not video_path:
        return 0.0
    if not _safe_isfile(rx_audio_path) or not _safe_isfile(video_path):
        return 0.0

    # ── Disk cache hit — skip the entire cross-correlation ─────────────────
    _cached = _rx_offset_cache_load(rx_audio_path, video_path)
    if _cached is not None:
        return float(_cached)

    try:
        import numpy as np
    except ImportError:
        return 0.0

    # Get the RX clip's actual duration
    rx_dur = _probe_duration(rx_audio_path)
    if rx_dur <= 0:
        rx_dur = 30
    rx_dur += 1  # small margin

    # Extract RX audio
    rx_data = _extract_mono(rx_audio_path, rx_dur, sr)
    if rx_data is None or len(rx_data) < sr // 2:
        return 0.0

    # Extract video audio (with cache for multiple RX clips from same video)
    vid_key = os.path.normpath(video_path).lower()
    if vid_key in _video_audio_cache:
        vid_data, vid_dur = _video_audio_cache[vid_key]
    else:
        vid_dur = _probe_duration(video_path)
        if vid_dur <= 0:
            vid_dur = 7200
        vid_dur += 1
        vid_data = _extract_mono(video_path, vid_dur, sr)
        if vid_data is not None and len(vid_data) >= sr:
            _video_audio_cache[vid_key] = (vid_data, vid_dur)
    if vid_data is None or len(vid_data) < sr:
        return 0.0

    # Cross-correlate: find where rx_data appears in vid_data.
    # We only search positive lags [0, len(vid_data) - 1]: the fragment must
    # appear somewhere inside the video, never before it.  Using n_fft that is
    # strictly >= len(rx_data) + len(vid_data) - 1 avoids circular aliasing so
    # every lag [0..len(vid_data)-1] maps to its true linear-correlation value.
    n_lin = len(rx_data) + len(vid_data) - 1
    n_fft = 1 << (n_lin - 1).bit_length()   # next power-of-2 ≥ n_lin
    R     = np.fft.rfft(rx_data,  n=n_fft)
    V     = np.fft.rfft(vid_data, n=n_fft)
    corr  = np.fft.irfft(V * np.conj(R), n=n_fft)

    # Only search [0, len(vid_data)] — valid positive lags.
    # Do NOT apply the n_fft/2 wrap-around correction: that correction is for
    # two-sided searches and would incorrectly map lags > n_fft/2 to negative
    # values, causing fragments in the second half of a long video to return 0.
    valid_len = min(len(vid_data), n_fft)
    peak      = int(np.argmax(corr[:valid_len]))
    offset_s  = round(peak / sr, 3)

    # Sanity: offset must be a plausible positive position within the video
    if offset_s < 0 or offset_s > vid_dur:
        return 0.0

    _rx_offset_cache_save(rx_audio_path, video_path, offset_s)
    return offset_s


def clear_rx_cache():
    """Clear the video audio cache after a build completes."""
    _video_audio_cache.clear()


def pair_takes(paths):
    videos = sorted([p for p in paths if p and is_video(p)])
    audios = sorted([p for p in paths if p and not is_video(p)])
    n = max(len(videos), len(audios))
    takes = []
    for i in range(n):
        vp = videos[i] if i < len(videos) else None
        ap = audios[i] if i < len(audios) else None
        takes.append((vp, ap))
    return takes

def _make_clipitem(cid, fid, filepath, start_fr, end_fr,
                   src_in_fr, src_out_fr, fps,
                   is_video, is_first_use,
                   seq_w=1280, seq_h=720, seq_sr=44100,
                   channels=None, name_prefix="", enabled=True,
                   audio_source_track=None, file_audio_channels=None):
    _ = channels
    ci = Element("clipitem", id=cid)
    SubElement(ci, "name").text     = name_prefix + basename(filepath)
    SubElement(ci, "enabled").text  = "TRUE" if enabled else "FALSE"
    SubElement(ci, "duration").text = str(src_out_fr - src_in_fr)
    make_rate(ci, fps)
    SubElement(ci, "start").text    = str(start_fr)
    SubElement(ci, "end").text      = str(end_fr)
    SubElement(ci, "in").text       = str(src_in_fr)
    SubElement(ci, "out").text      = str(src_out_fr)

    f_el = SubElement(ci, "file", id=fid)
    if is_first_use:
        SubElement(f_el, "name").text    = basename(filepath)
        SubElement(f_el, "pathurl").text = pathurl(filepath)
        make_rate(f_el, fps)
        if is_video:
            mc = SubElement(f_el, "media")
            vc = SubElement(mc, "video")
            vc_ch = SubElement(vc, "samplecharacteristics")
            make_rate(vc_ch, fps)
            SubElement(vc_ch, "width").text  = str(seq_w)
            SubElement(vc_ch, "height").text = str(seq_h)
            ac = SubElement(mc, "audio")
            ac_ch = SubElement(ac, "samplecharacteristics")
            SubElement(ac_ch, "depth").text      = "16"
            SubElement(ac_ch, "samplerate").text = str(seq_sr)
            SubElement(ac, "channelcount").text  = "2"
        else:
            mc = SubElement(f_el, "media")
            ac = SubElement(mc, "audio")
            ac_ch = SubElement(ac, "samplecharacteristics")
            SubElement(ac_ch, "depth").text      = "16"
            SubElement(ac_ch, "samplerate").text = str(seq_sr)
            if file_audio_channels is not None:
                SubElement(ac, "channelcount").text = str(file_audio_channels)

    # Tell the NLE which stream to read (needed when a video file is used on an
    # audio track so it reads the audio channel instead of defaulting to video).
    if audio_source_track is not None:
        st = SubElement(ci, "sourcetrack")
        SubElement(st, "mediatype").text  = "audio"
        SubElement(st, "trackindex").text = str(audio_source_track)

    return ci

def probe_media_settings(all_paths):
    best_w   = 0
    best_h   = 0
    best_fps = 0.0
    best_sr  = 0

    for path in all_paths:
        if not path or not os.path.exists(path):
            continue
        try:
            for line in (_ffprobe_csv(
                    path, "stream=width,height,r_frame_rate",
                    select="v:0") or "").splitlines():
                parts = [p.strip() for p in line.split(",") if p.strip()]
                if len(parts) >= 3:
                    try:
                        w = int(parts[0]); h = int(parts[1])
                        num, den = parts[2].split("/")
                        fps = round(int(num) / int(den), 3)
                        if w * h > best_w * best_h:
                            best_w, best_h = w, h
                        if fps > best_fps:
                            best_fps = fps
                    except Exception:
                        pass
            for line in (_ffprobe_csv(
                    path, "stream=sample_rate", select="a:0") or "").splitlines():
                try:
                    sr = int(line.strip())
                    if sr > best_sr:
                        best_sr = sr
                except Exception:
                    pass
        except Exception:
            pass

    return (
        best_w   if best_w  > 0 else 1280,
        best_h   if best_h  > 0 else 720,
        snap_fps(best_fps),
        best_sr  if best_sr > 0 else 44100,
    )

def build_xml(results, int_assets, vo_bins, parts, seq_name, gap_secs,
              seq_w=1280, seq_h=720, seq_fps=24, seq_sr=44100,
              vo_takes_with_offset=None):
    fps    = seq_fps
    gap_fr = round(gap_secs * fps)
    defined = set()
    ctr     = 0

    # ── Defensive filter: strip transient mix-for-transcript files ────────────
    # mix_for_transcript() writes temp WAVs (prefix "_pb_mix_") used solely as
    # the Whisper input for multi-track tokens.  They must never reach the XML.
    def _is_temp_mix_path(p):
        return bool(p) and os.path.basename(p).startswith("_pb_mix_")
    int_assets = {
        _tok: [p for p in _paths if not _is_temp_mix_path(p)]
        for _tok, _paths in (int_assets or {}).items()
    }
    for _r in (results or []):
        for _td in (_r.get("takes_data") or []):
            if isinstance(_td, dict) and _is_temp_mix_path(_td.get("apath")):
                _td["apath"] = None

    from collections import defaultdict
    clip_pos = defaultdict(int)

    max_vo_takes = 0
    vo_takes_by_part = {}
    if vo_takes_with_offset:
        for pi, takes_list in vo_takes_with_offset.items():
            vo_takes_by_part[pi] = [(e[1], e[2], e[3]) for e in takes_list]
            max_vo_takes = max(max_vo_takes, len(takes_list))
    else:
        for pi, vb in vo_bins.items():
            paths = vb.get_paths() if hasattr(vb, "get_paths") else []
            takes = pair_takes(paths)
            vo_takes_by_part[pi] = [(0.0, vp, ap) for vp, ap in takes]
            max_vo_takes = max(max_vo_takes, len(takes))

    HOST_LC  = HOST_NAME.lower()

    # Allocate enough interview tracks to fit the largest per-token source
    # count.  Slot 0 is reserved for the host across all tokens; subsequent
    # slots hold non-host files, one track per file.  Default is 2 (host +
    # 1 guest) for the typical Riverside duo.
    def _is_host_file(p):
        return HOST_LC in os.path.basename(p).lower()
    _max_guest_v = 1
    _max_guest_a = 1
    for _paths in (int_assets or {}).values():
        _gvps = [p for p in _paths if p and is_video(p) and not _is_host_file(p)]
        _gaps = [p for p in _paths if p and not is_video(p) and not _is_host_file(p)]
        _max_guest_v = max(_max_guest_v, len(_gvps))
        _max_guest_a = max(_max_guest_a, len(_gaps))
    max_iv = 1 + _max_guest_v   # 1 host slot + N guest slots
    max_ia = 1 + _max_guest_a

    total_v = max_iv + max(max_vo_takes, 1)
    total_a = max_ia + max(max_vo_takes, 1)

    v_tracks = [[] for _ in range(total_v)]
    a_tracks = [[] for _ in range(total_a)]

    cursor    = 0
    skipped   = []
    part_start = {}

    _vo_anchors = {}
    _vo_orders_by_part = {}
    for _r in results:
        if _r.get("is_vo"):
            _pi  = _r.get("part_index", 0)
            _ord = _r.get("order", 0)
            _vo_orders_by_part.setdefault(_pi, []).append(_ord)
            if _r.get("takes_data") and _r.get("rec_out_s", 0) > _r.get("rec_in_s", 0):
                _vo_anchors[(_pi, _ord)] = (
                    _r.get("rec_in_s", 0.0), _r.get("rec_out_s", 0.0))
    for _pi in _vo_orders_by_part:
        _vo_orders_by_part[_pi].sort()

    def _vo_fallback_span(pi, order, vb):
        orders = _vo_orders_by_part.get(pi, [])
        try:   i2 = orders.index(order)
        except ValueError: i2 = -1
        prev_out = next((
            _vo_anchors[(pi, o)][1]
            for o in reversed(orders[:i2])
            if (pi, o) in _vo_anchors), None)
        next_in = next((
            _vo_anchors[(pi, o)][0]
            for o in (orders[i2+1:] if i2 >= 0 else [])
            if (pi, o) in _vo_anchors), None)
        ap  = vb.get_audio() if hasattr(vb, "get_audio") else None
        vp  = vb.get_video() if hasattr(vb, "get_video") else None
        dur = get_media_duration(ap or vp)
        s   = prev_out if prev_out is not None else 0.0
        e   = next_in  if next_in  is not None else (dur or 0.0)
        if e <= s:
            s, e = 0.0, (dur or 0.0)
        return s, e

    for res in sorted(results, key=lambda r: r["order"]):
        is_vo = res.get("is_vo", False)
        in_s  = res.get("rec_in_s", 0.0)
        out_s = res.get("rec_out_s", 0.0)
        st    = res.get("status", "")

        if st in _TERMINAL_SKIP_STATUSES:
            skipped.append(res); continue
        if not is_vo and in_s >= out_s:
            skipped.append(res); continue

        segments = res.get("segments") or [(in_s, out_s)]
        pi       = res.get("part_index", 0)

        if is_vo:
            takes    = vo_takes_by_part.get(pi, [])
            takes_data = res.get("takes_data", [])

            if not takes_data:
                vb = vo_bins.get(pi)
                if not vb:
                    skipped.append(res); continue
                fb_in, fb_out = _vo_fallback_span(pi, res.get("order", 0), vb)
                if fb_out <= fb_in:
                    skipped.append(res); continue
                part_takes = takes
                if not part_takes:
                    vp = vb.get_video() if hasattr(vb, "get_video") else None
                    ap = vb.get_audio() if hasattr(vb, "get_audio") else None
                    part_takes = [(0.0, vp, ap)]
                takes_data = []
                for _v_off, _vp, _ap in part_takes:
                    takes_data.append({
                        "segments":    [(fb_in, fb_out)],
                        "vpath":       _vp,
                        "apath":       _ap,
                        "v_offset":    _v_off,
                        "is_fallback": True,
                    })

            if pi not in part_start:
                part_start[pi] = cursor

            max_dur_fr = 0
            for td in takes_data:
                td_segs = td.get("segments") or segments
                if not td_segs: continue
                total_fr = sum(
                    f2fr(e, fps) - f2fr(s, fps)
                    for s, e in td_segs if e > s
                )
                max_dur_fr = max(max_dur_fr, total_fr)

            if max_dur_fr == 0:
                skipped.append(res); continue

            vo_st = res.get("status", "ok")
            vo_prefix = ""
            if any(td.get("is_fallback") for td in takes_data):
                vo_prefix = "[?] "
            elif vo_st == "low_confidence":
                vo_prefix = "[~] "

            for take_i, td in enumerate(takes_data):
                tv_idx = max_iv + take_i
                ta_idx = max_ia + take_i
                td_segs = td.get("segments") or segments
                vp      = td.get("vpath")
                ap      = td.get("apath")
                v_off   = td.get("v_offset", 0.0)

                take_cursor = cursor

                for si, (seg_in_s, seg_out_s) in enumerate(td_segs):
                    if seg_in_s >= seg_out_s: continue
                    seg_in_fr  = f2fr(seg_in_s, fps)
                    seg_out_fr = f2fr(seg_out_s, fps)
                    dur        = seg_out_fr - seg_in_fr
                    sf = take_cursor
                    ef = take_cursor + dur
                    is_last = (si == len(td_segs) - 1)

                    group = []

                    if vp and tv_idx < len(v_tracks):
                        v_in_fr  = f2fr(seg_in_s  + v_off, fps)
                        v_out_fr = f2fr(seg_out_s + v_off, fps)
                        fid  = "file-vo-v-{}-{}".format(pi, take_i)
                        cid  = "clip-{}".format(ctr); ctr += 1
                        first = fid not in defined
                        if first: defined.add(fid)
                        clip_pos[("v", tv_idx+1)] += 1
                        ci = _make_clipitem(cid, fid, vp, sf, ef,
                                            v_in_fr, v_out_fr, fps,
                                            True, first, seq_w, seq_h, seq_sr,
                                            name_prefix=vo_prefix,
                                            enabled=(take_i == 0))
                        v_tracks[tv_idx].append(ci)
                        group.append({"el":ci,"cid":cid,"mediatype":"video",
                                      "track_idx":tv_idx+1,
                                      "clip_pos":clip_pos[("v",tv_idx+1)]})

                    if ap and ta_idx < len(a_tracks):
                        fid  = "file-vo-a-{}-{}".format(pi, take_i)
                        cid  = "clip-{}".format(ctr); ctr += 1
                        first = fid not in defined
                        if first: defined.add(fid)
                        clip_pos[("a", ta_idx+1)] += 1
                        ci = _make_clipitem(cid, fid, ap, sf, ef,
                                            seg_in_fr, seg_out_fr, fps,
                                            False, first, seq_w, seq_h, seq_sr,
                                            name_prefix=vo_prefix,
                                            enabled=(take_i == 0))
                        a_tracks[ta_idx].append(ci)
                        group.append({"el":ci,"cid":cid,"mediatype":"audio",
                                      "track_idx":ta_idx+1,
                                      "clip_pos":clip_pos[("a",ta_idx+1)]})

                    if len(group) > 1:
                        for item in group:
                            for ref in group:
                                lk = SubElement(item["el"], "link")
                                SubElement(lk, "linkclipref").text = ref["cid"]
                                SubElement(lk, "mediatype").text   = ref["mediatype"]
                                SubElement(lk, "trackindex").text  = str(ref["track_idx"])
                                SubElement(lk, "clipindex").text   = str(ref["clip_pos"])

                    take_cursor = ef + (0 if not is_last else 0)

            _vo_gap_fr = round(res["gap_after_s"] * fps) if "gap_after_s" in res else gap_fr
            cursor += max_dur_fr + (_vo_gap_fr if res.get("gap_after", True) else 0)

        else:
            tok    = res["token"]
            paths  = int_assets.get(tok, [])

            host_vpaths  = [p for p in paths if p and is_video(p)     and _is_host_file(p)]
            guest_vpaths = [p for p in paths if p and is_video(p)     and not _is_host_file(p)]
            host_apaths  = [p for p in paths if p and not is_video(p) and _is_host_file(p)]
            guest_apaths = [p for p in paths if p and not is_video(p) and not _is_host_file(p)]

            # Slot 0 holds the host (if any).  Each non-host file gets its own
            # slot (1, 2, 3, …) so multi-source tokens (3+ files, or anonymised
            # speaker-0 / speaker-1 Riverside exports) export every track.
            participants = []
            if host_vpaths or host_apaths:
                participants.append((
                    0,
                    host_vpaths[0] if host_vpaths else None,
                    host_apaths[0] if host_apaths else None,
                ))
            _n_guests = max(len(guest_vpaths), len(guest_apaths))
            for _gi in range(_n_guests):
                participants.append((
                    1 + _gi,
                    guest_vpaths[_gi] if _gi < len(guest_vpaths) else None,
                    guest_apaths[_gi] if _gi < len(guest_apaths) else None,
                ))

            iv_st     = res.get("status", "ok")
            iv_prefix = "[~] " if iv_st == "low_confidence" else ""

            if pi not in part_start:
                part_start[pi] = cursor

            for si, (seg_in_s, seg_out_s) in enumerate(segments):
                if seg_in_s >= seg_out_s: continue

                seg_in_fr  = f2fr(seg_in_s,  fps)
                seg_out_fr = f2fr(seg_out_s, fps)
                dur        = seg_out_fr - seg_in_fr
                sf, ef     = cursor, cursor + dur
                is_last    = (si == len(segments) - 1)
                group      = []

                for slot, vp, ap in participants:
                    if vp and slot < max_iv:
                        fid   = "file-v-{}-{}".format(tok, slot)
                        cid   = "clip-{}".format(ctr); ctr += 1
                        first = fid not in defined
                        if first: defined.add(fid)
                        clip_pos[("v", slot+1)] += 1
                        ci = _make_clipitem(cid, fid, vp, sf, ef,
                                            seg_in_fr, seg_out_fr, fps,
                                            True, first, seq_w, seq_h, seq_sr,
                                            name_prefix=iv_prefix)
                        v_tracks[slot].append(ci)
                        group.append({"el":ci,"cid":cid,"mediatype":"video",
                                      "track_idx":slot+1,
                                      "clip_pos":clip_pos[("v",slot+1)]})

                    if ap and slot < max_ia:
                        fid   = "file-a-{}-{}".format(tok, slot)
                        cid   = "clip-{}".format(ctr); ctr += 1
                        first = fid not in defined
                        if first: defined.add(fid)
                        clip_pos[("a", slot+1)] += 1
                        ci = _make_clipitem(cid, fid, ap, sf, ef,
                                            seg_in_fr, seg_out_fr, fps,
                                            False, first, seq_w, seq_h, seq_sr,
                                            name_prefix=iv_prefix)
                        a_tracks[slot].append(ci)
                        group.append({"el":ci,"cid":cid,"mediatype":"audio",
                                      "track_idx":slot+1,
                                      "clip_pos":clip_pos[("a",slot+1)]})

                if len(group) > 1:
                    for item in group:
                        for ref in group:
                            lk = SubElement(item["el"], "link")
                            SubElement(lk, "linkclipref").text = ref["cid"]
                            SubElement(lk, "mediatype").text   = ref["mediatype"]
                            SubElement(lk, "trackindex").text  = str(ref["track_idx"])
                            SubElement(lk, "clipindex").text   = str(ref["clip_pos"])

                _int_gap_fr = round(res["gap_after_s"] * fps) if "gap_after_s" in res else gap_fr
                cursor = ef + (_int_gap_fr if (is_last and res.get("gap_after", True)) else 0)

    total = cursor

    xmeml = Element("xmeml", version="4")
    seq   = SubElement(xmeml, "sequence", id="sequence-1")
    SubElement(seq, "name").text     = seq_name
    SubElement(seq, "duration").text = str(total)
    make_rate(seq, fps); make_tc(seq, fps)

    for part in parts:
        psf = part_start.get(part["index"], 0)
        mk  = SubElement(seq, "marker")
        SubElement(mk, "name").text    = part["name"]
        SubElement(mk, "in").text      = str(psf)
        SubElement(mk, "out").text     = "-1"
        SubElement(mk, "comment").text = ""

    media = SubElement(seq, "media")
    vel   = SubElement(media, "video")
    vf    = SubElement(vel, "format")
    vc    = SubElement(vf, "samplecharacteristics")
    make_rate(vc, fps)
    SubElement(vc, "width").text            = str(seq_w)
    SubElement(vc, "height").text           = str(seq_h)
    SubElement(vc, "anamorphic").text       = "FALSE"
    SubElement(vc, "pixelaspectratio").text = "square"
    SubElement(vc, "fielddominance").text   = "none"
    SubElement(vc, "colordepth").text       = "32"

    for clips in v_tracks:
        t = SubElement(vel, "track")
        for c in clips: t.append(c)

    ael = SubElement(media, "audio")
    af  = SubElement(ael, "format")
    ac  = SubElement(af, "samplecharacteristics")
    SubElement(ac, "depth").text        = "32"
    SubElement(ac, "samplerate").text   = str(seq_sr)
    SubElement(ac, "channelcount").text = "2"

    for clips in a_tracks:
        t = SubElement(ael, "track")
        for c in clips: t.append(c)

    return xmeml, skipped

def write_xml(xmeml, out_path):
    try: indent(xmeml)
    except Exception: pass
    buf = io.BytesIO()
    ElementTree(xmeml).write(buf, encoding="utf-8", xml_declaration=False)
    with open(out_path, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n')
        f.write(buf.getvalue())

def _probe_file_fps(path):
    """Return (r_frame_rate_str, avg_frame_rate_str, fps_float) for a video file."""
    if not path or not os.path.isfile(path):
        return ("n/a", "n/a", 0.0)
    out = _ffprobe_csv(path, "stream=r_frame_rate,avg_frame_rate",
                       select="v:0")
    if out is None:
        return ("error", "error", 0.0)
    parts = [p.strip() for p in out.split(",") if p.strip()]
    rfr = parts[0] if len(parts) >= 1 else "n/a"
    afr = parts[1] if len(parts) >= 2 else "n/a"
    try:
        num, den = rfr.split("/")
        fps_f = int(num) / int(den)
    except Exception:
        fps_f = 0.0
    return (rfr, afr, fps_f)


def write_build_diagnostic(clips_with_media, seq_fps, seq_w, seq_h, seq_sr,
                           out_path=None):
    """Write a diagnostic report of all data flowing into build_xml_from_pt."""
    import datetime
    if out_path is None:
        out_path = os.path.join(os.path.expanduser("~"), "Downloads",
                                "postbridge_build_diagnostic.txt")
    fps = seq_fps
    lines = []
    lines.append("=" * 80)
    lines.append("PostBridge Build Diagnostic Report")
    lines.append("Generated: {}".format(datetime.datetime.now().isoformat()))
    lines.append("=" * 80)
    lines.append("")
    lines.append("SEQUENCE SETTINGS")
    lines.append("  fps:         {}".format(fps))
    lines.append("  resolution:  {}x{}".format(seq_w, seq_h))
    lines.append("  sample_rate: {}".format(seq_sr))
    lines.append("")

    # Collect unique video and audio files
    video_files = sorted(set(c.get("video_path", "") for c in clips_with_media if c.get("video_path")))
    audio_files = sorted(set(c.get("audio_path", "") for c in clips_with_media if c.get("audio_path")))

    lines.append("VIDEO FILES (native fps)")
    lines.append("-" * 80)
    for vp in video_files:
        rfr, afr, fps_f = _probe_file_fps(vp)
        snapped = snap_fps(fps_f) if fps_f > 0 else "n/a"
        match = "OK" if abs(fps_f - fps) < 0.05 else "** MISMATCH **"
        lines.append("  {}".format(os.path.basename(vp)))
        lines.append("    r_frame_rate:   {} ({:.4f} fps)".format(rfr, fps_f))
        lines.append("    avg_frame_rate: {}".format(afr))
        lines.append("    snapped:        {}".format(snapped))
        lines.append("    vs sequence:    {} (seq={})".format(match, fps))
    lines.append("")

    lines.append("AUDIO FILES (reference)")
    lines.append("-" * 80)
    for ap in audio_files:
        lines.append("  {}".format(os.path.basename(ap)))
    lines.append("")

    # Per-source summary
    lines.append("PER-SOURCE SYNC OFFSETS")
    lines.append("-" * 80)
    seen_sources = {}
    for clip in clips_with_media:
        base = clip.get("source_base", clip.get("clip_name", "?"))
        if base in seen_sources:
            continue
        seen_sources[base] = True
        v_offset = clip.get("video_offset_secs", 0.0)
        vp = clip.get("video_path", "")
        ap = clip.get("audio_path", "")
        sf = clip.get("source_file", "")
        # Detect fragment: source audio much shorter than assigned video
        is_fragment = False
        if sf and vp and os.path.isfile(sf) and os.path.isfile(vp):
            sd = _probe_duration(sf)
            vd = _probe_duration(vp)
            if sd > 0 and vd > 0 and sd < vd * 0.5:
                is_fragment = True
        lines.append("  Source: {}{}".format(base, "  [fragment]" if is_fragment else ""))
        lines.append("    video:      {}".format(os.path.basename(vp) if vp else "NONE"))
        lines.append("    ref audio:  {}".format(os.path.basename(ap) if ap else "NONE"))
        if sf:
            lines.append("    source file: {}".format(os.path.basename(sf)))
        lines.append("    v_offset:   {:.6f} s{}".format(
            v_offset, "  (auto-detected)" if is_fragment and v_offset != 0 else ""))
    lines.append("")

    # First 20 clips detail
    lines.append("CLIP DETAIL (first 50 clips)")
    lines.append("-" * 80)
    sorted_clips = sorted(clips_with_media, key=lambda c: c["start_secs"])
    for i, clip in enumerate(sorted_clips[:50]):
        base     = clip.get("source_base", clip.get("clip_name", "?"))
        v_offset = clip.get("video_offset_secs", 0.0)
        src_in_s = clip.get("src_in_secs", clip["start_secs"])
        src_out_s = clip.get("src_out_secs", src_in_s + (clip["end_secs"] - clip["start_secs"]))
        a_src_in  = f2fr(src_in_s, fps)
        v_src_in  = max(0, f2fr(src_in_s + v_offset, fps))
        a_src_out = f2fr(src_out_s, fps)
        v_src_out = max(0, f2fr(src_out_s + v_offset, fps))
        vp = clip.get("video_path", "")
        ap = clip.get("audio_path", "")

        lines.append("  [{:3d}] {}".format(i, base))
        lines.append("        timeline:     {:.3f}s - {:.3f}s".format(
            clip["start_secs"], clip["end_secs"]))
        lines.append("        src_in:       {:.6f}s  (audio frame {})".format(src_in_s, a_src_in))
        lines.append("        v_offset:     {:.6f}s".format(v_offset))
        lines.append("        v_src_in:     {:.6f}s  (video frame {})".format(
            src_in_s + v_offset, v_src_in))
        lines.append("        a_src_in fr:  {}   v_src_in fr: {}   delta fr: {}".format(
            a_src_in, v_src_in, v_src_in - a_src_in))
        lines.append("        video file:   {}".format(os.path.basename(vp) if vp else "NONE"))
        lines.append("        audio file:   {}".format(os.path.basename(ap) if ap else "NONE"))
        if vp:
            _, _, vfps = _probe_file_fps(vp)
            lines.append("        video native: {:.4f} fps  (seq: {})".format(vfps, fps))
    lines.append("")
    lines.append("=" * 80)
    lines.append("END OF REPORT")

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return out_path


def build_xml_from_pt(clips_with_media, track_names, seq_name,
                      seq_w=1280, seq_h=720, seq_fps=30.0, seq_sr=48000,
                      mix_path=None, include_camera_audio=False,
                      warnings_out=None):
    fps        = seq_fps
    defined    = set()
    link_pairs = []      # (video_element, cam_audio_element, track_name)
    ctr     = 0

    # Cache each video file's length in frames so a wildly-wrong sync
    # offset (which would push a clip's in/out past the end of the
    # media — Premiere rejects those as "invalid start/end" and SILENTLY
    # DROPS the clip) can be clamped into range and reported instead.
    _vfc_cache = {}
    def _video_frame_count(vp):
        if vp in _vfc_cache:
            return _vfc_cache[vp]
        d  = _probe_duration(vp) if vp else 0.0
        fr = f2fr(d, fps) if d and d > 0 else None
        _vfc_cache[vp] = fr
        return fr
    _oor_seen = set()   # source bases already warned (dedupe per source)

    v_tracks   = {tn: [] for tn in track_names}
    a_tracks   = {tn: [] for tn in track_names}
    cam_tracks = {tn: [] for tn in track_names}  # camera audio from video file

    # ── Pre-pass: detect crossfades and halve fade durations ─────────────────
    # For a crossfade the AAF has one Fade clip whose duration equals both the
    # outgoing fade-out and the incoming fade-in.  We want the two clips to
    # meet in the middle rather than having the outgoing clip hold all the way
    # through.  Detection: consecutive clips on the same track where
    # fade_out_secs ≈ fade_in_secs ≈ the gap between them.
    frame_s = 1.0 / fps if fps else 1.0   # one frame in seconds (tolerance)
    sorted_clips = sorted(clips_with_media, key=lambda c: c["start_secs"])
    for i in range(len(sorted_clips) - 1):
        ca, cb = sorted_clips[i], sorted_clips[i + 1]
        if ca["track_name"] != cb["track_name"]:
            continue
        fo  = ca.get("fade_out_secs", 0.0)
        fi  = cb.get("fade_in_secs",  0.0)
        gap = cb["start_secs"] - ca["end_secs"]
        if fo > 0 and fi > 0 and abs(gap - fo) <= frame_s:
            ca["fade_out_secs"] = fo / 2.0
            cb["fade_in_secs"]  = fi / 2.0

    # Track per-track previous clip end (after fade extension) so head extensions
    # don't cause overlap with the preceding clip.
    track_prev_end = {}   # tn → end_fr after extension

    for clip in sorted_clips:
        tn         = clip["track_name"]
        orig_start = f2fr(clip["start_secs"], fps)
        orig_end   = f2fr(clip["end_secs"],   fps)
        v_offset   = clip.get("video_offset_secs", 0.0)
        clip_dur_s = clip["end_secs"] - clip["start_secs"]
        src_in_s   = clip.get("src_in_secs", clip["start_secs"])
        src_out_s  = clip.get("src_out_secs", src_in_s + clip_dur_s)

        # ── Exact fade extensions from AAF fade-clip durations ────────────────
        # fade_in_secs / fade_out_secs are set by parse_aaf_session from the
        # actual "Fade N" clip lengths adjacent to this clip.  Using them here
        # fills the precise gap rather than guessing.
        fi_fr = max(0, f2fr(clip.get("fade_in_secs",  0.0), fps))
        fo_fr = max(0, f2fr(clip.get("fade_out_secs", 0.0), fps))

        # Head: extend backward, clamped so we don't overlap the previous clip.
        prev_end  = track_prev_end.get(tn, 0)
        start_fr  = max(prev_end, orig_start - fi_fr)
        head_ext  = orig_start - start_fr      # actual frames added at head

        # Tail: extend forward by the fade-out frames.
        end_fr    = orig_end + fo_fr
        track_prev_end[tn] = end_fr

        dur_fr = orig_end - orig_start        # clip's own (non-extended) duration
        if dur_fr <= 0:
            continue

        # Audio uses the reference-audio src position directly (no camera offset).
        # Video ADDS v_offset: positive offset = camera started before DAW (common),
        # negative offset = audio started before camera (both are valid).
        a_src_in  = max(0, f2fr(src_in_s,          fps) - head_ext)
        a_src_out = f2fr(src_out_s, fps) + fo_fr
        v_src_in  = max(0, f2fr(src_in_s + v_offset, fps) - head_ext)
        v_src_out = max(0, f2fr(src_out_s + v_offset, fps)) + fo_fr
        # If clamping collapsed the video window, push out by clip duration
        if v_src_out <= v_src_in:
            v_src_out = v_src_in + (end_fr - start_fr)

        vp = clip.get("video_path")
        ap = clip.get("audio_path")

        # ── Out-of-range guard ────────────────────────────────────────
        # When the sync offset is badly wrong (e.g. the joined footage is
        # incomplete, or a clip that shouldn't have been synced), the
        # video in/out can land outside the file.  Slide the window back
        # into [0, file_length] — preserving its length — so the clip
        # stays VISIBLE (and fixable) instead of being dropped on import,
        # and record a one-per-source warning.
        #
        # CONTENT vs FADE-EXTENSION: v_src_in/out above already include
        # head_ext (fade-in extension) and fo_fr (fade-out extension)
        # baked in.  Those extensions request additional source room
        # AROUND the user's authored content so Premiere can render the
        # fade envelope cleanly — they don't represent content that
        # has to be there.  A clip whose AUTHORED CONTENT ends at the
        # video boundary and has, say, a 4 s authored fade-out will
        # have v_src_out push 4 s past _vfc; Premiere will just render
        # the last bit of fade against silence, which is benign.
        # Measure overshoot on the CONTENT (pre-extension) bounds so
        # we only warn when the authored range really doesn't fit.
        # Above 12 frames (~400 ms) is the threshold for warning; real
        # sync misses overshoot by tens to thousands of frames.
        _OOR_TOL_FR = 12
        if vp:
            _vfc = _video_frame_count(vp)
            if _vfc and _vfc > 0 and (v_src_in < 0 or v_src_out > _vfc):
                # Content-only bounds (strip the fade extensions back off).
                _content_in  = f2fr(src_in_s + v_offset,  fps)
                _content_out = f2fr(src_out_s + v_offset, fps)
                _content_overshoot = max(
                    -_content_in,                  # head before video frame 0
                    _content_out - _vfc,           # tail past video end
                    0)
                _win = max(1, v_src_out - v_src_in)
                v_src_in  = max(0, min(v_src_in, _vfc - _win))
                v_src_out = min(_vfc, v_src_in + _win)
                if v_src_out <= v_src_in:
                    v_src_in, v_src_out = 0, min(_vfc, _win)
                _base = clip.get("source_base") or os.path.basename(vp)
                if (warnings_out is not None
                        and _content_overshoot > _OOR_TOL_FR
                        and _base not in _oor_seen):
                    _oor_seen.add(_base)
                    # When |v_offset| is below one frame the warning
                    # text shouldn't blame the sync offset — that
                    # produced the misleading "sync offset -0.0s places
                    # the clip outside …" popup users saw on audio-
                    # derived-from-video sources.  Pick the message
                    # based on what the data actually says.
                    _ov_s    = _content_overshoot / fps if fps else 0.0
                    _vbn     = os.path.basename(vp)
                    _frame_s = 1.0 / fps if fps else 1.0
                    if abs(v_offset) < _frame_s:
                        warnings_out.append(
                            "{}: authored clip range extends {:+.3f}s past "
                            "the end of {} — last {} frames clamped (no "
                            "sync offset applied; the audio's source range "
                            "doesn't fit inside the video — trim the clip "
                            "in Pro Tools or use a video that covers the "
                            "full range).".format(
                                _base, _ov_s, _vbn, _content_overshoot))
                    else:
                        warnings_out.append(
                            "{}: sync offset {:+.3f}s places the clip outside "
                            "{} — clamped into range (re-sync needed).".format(
                                _base, v_offset, _vbn))

        if vp and tn in v_tracks:
            fid   = "file-v-{}".format(os.path.basename(vp).replace(" ", "_"))
            cid   = "clip-{}".format(ctr); ctr += 1
            first = fid not in defined
            if first: defined.add(fid)
            ci = _make_clipitem(cid, fid, vp, start_fr, end_fr,
                                v_src_in, v_src_out, fps,
                                True, first, seq_w, seq_h, seq_sr)
            v_tracks[tn].append(ci)

            if include_camera_audio and tn in cam_tracks:
                # Reuse the video clip's fid (just a reference, no redefinition).
                # Premiere only honours <links> when both the video and audio
                # clipitem reference the *same* fid — a separate file-cam-... fid
                # silently prevents linking even when clip IDs cross-reference.
                # <sourcetrack> is still needed to tell Premiere which stream to read.
                cam_cid = "clip-{}".format(ctr); ctr += 1
                cam_ci  = _make_clipitem(cam_cid, fid, vp, start_fr, end_fr,
                                         v_src_in, v_src_out, fps,
                                         False, False, seq_w, seq_h, seq_sr,
                                         audio_source_track=1)
                cam_tracks[tn].append(cam_ci)
                # Record for cross-link patching after track indices are known.
                # fid is included so the patching pass can set <masterclipid>.
                link_pairs.append((ci, cam_ci, tn, fid))

        if ap and tn in a_tracks:
            fid   = "file-a-{}".format(os.path.basename(ap).replace(" ", "_"))
            cid   = "clip-{}".format(ctr); ctr += 1
            first = fid not in defined
            if first: defined.add(fid)
            ci = _make_clipitem(cid, fid, ap, start_fr, end_fr,
                                a_src_in, a_src_out, fps,
                                False, first, seq_w, seq_h, seq_sr)
            a_tracks[tn].append(ci)

    total = 0
    for clips in list(v_tracks.values()) + list(a_tracks.values()) + list(cam_tracks.values()):
        for c in clips:
            e = c.find("end")
            if e is not None and e.text:
                total = max(total, int(e.text))

    xmeml = Element("xmeml", version="4")
    seq   = SubElement(xmeml, "sequence", id="sequence-1")
    SubElement(seq, "name").text     = seq_name
    SubElement(seq, "duration").text = str(total)
    make_rate(seq, fps); make_tc(seq, fps)

    media = SubElement(seq, "media")
    vel   = SubElement(media, "video")
    vf    = SubElement(vel, "format")
    vc    = SubElement(vf, "samplecharacteristics")
    make_rate(vc, fps)
    SubElement(vc, "width").text            = str(seq_w)
    SubElement(vc, "height").text           = str(seq_h)
    SubElement(vc, "anamorphic").text       = "FALSE"
    SubElement(vc, "pixelaspectratio").text = "square"
    SubElement(vc, "fielddominance").text   = "none"
    SubElement(vc, "colordepth").text       = "32"

    # ── Video tracks — skip completely empty tracks ───────────────────────────
    for tn in track_names:
        clips = v_tracks.get(tn, [])
        if not clips:
            continue
        t = SubElement(vel, "track")
        for c in clips:
            t.append(c)

    ael = SubElement(media, "audio")
    af  = SubElement(ael, "format")
    ac  = SubElement(af, "samplecharacteristics")
    SubElement(ac, "depth").text        = "32"
    SubElement(ac, "samplerate").text   = str(seq_sr)
    SubElement(ac, "channelcount").text = "2"

    # ── AAF audio tracks — skip empty tracks, count what's written ───────────
    n_audio_written = 0
    for tn in track_names:
        clips = a_tracks.get(tn, [])
        if not clips:
            continue
        t = SubElement(ael, "track")
        for c in clips:
            t.append(c)
        n_audio_written += 1

    # ── Camera audio tracks — skip empty tracks, count what's written ─────────
    n_cam_written = 0
    if include_camera_audio:
        for tn in track_names:
            clips = cam_tracks.get(tn, [])
            if not clips:
                continue
            t = SubElement(ael, "track")
            for c in clips:
                t.append(c)
            n_cam_written += 1

    # ── Stereo mix track (full-file, not clamped to AAF length) ──────────────
    if mix_path and os.path.isfile(mix_path):
        mix_dur_s  = get_media_duration(mix_path) or 0.0
        mix_total  = f2fr(mix_dur_s, fps) if mix_dur_s > 0 else total
        if mix_total > total:
            total = mix_total
            seq_dur_el = seq.find("duration")
            if seq_dur_el is not None:
                seq_dur_el.text = str(total)

        fid      = "file-mix-{}".format(os.path.basename(mix_path).replace(" ", "_"))
        mix_name = os.path.basename(mix_path)

        # Two linked mono tracks (L + R) — the only approach that reliably
        # imports as a stereo pair in Premiere.  <link> elements (direct children
        # of each clipitem, no wrapper) cross-reference the two clips so Premiere
        # treats them as locked/stereo rather than independent mono tracks.
        mix_L_tidx = n_audio_written + n_cam_written + 1
        mix_R_tidx = n_audio_written + n_cam_written + 2

        def _mix_ci(cid, chan_idx, include_file):
            el = Element("clipitem", id=cid)
            SubElement(el, "masterclipid").text = fid
            SubElement(el, "name").text         = mix_name
            SubElement(el, "enabled").text      = "TRUE"
            SubElement(el, "duration").text     = str(mix_total)
            SubElement(el, "channelcount").text = "1"
            make_rate(el, fps)
            SubElement(el, "start").text = "0"
            SubElement(el, "end").text   = str(mix_total)
            SubElement(el, "in").text    = "0"
            SubElement(el, "out").text   = str(mix_total)
            if include_file:
                f_el = SubElement(el, "file", id=fid)
                SubElement(f_el, "name").text    = mix_name
                SubElement(f_el, "pathurl").text = pathurl(mix_path)
                make_rate(f_el, fps)
                mc2   = SubElement(f_el, "media")
                ac2   = SubElement(mc2, "audio")
                ac_ch = SubElement(ac2, "samplecharacteristics")
                SubElement(ac_ch, "depth").text      = "16"
                SubElement(ac_ch, "samplerate").text = str(seq_sr)
                SubElement(ac2, "channelcount").text = "2"
            else:
                SubElement(el, "file", id=fid)   # reference only, no redefinition
            st = SubElement(el, "sourcetrack")
            SubElement(st, "mediatype").text  = "audio"
            SubElement(st, "trackindex").text = str(chan_idx)
            return el

        ci_L = _mix_ci("clip-mix-L", 1, True)
        ci_R = _mix_ci("clip-mix-R", 2, False)

        # Cross-links — direct <link> children, no wrapper element
        for target_ci in (ci_L, ci_R):
            for ref_cid, ref_tidx in (("clip-mix-L", mix_L_tidx),
                                      ("clip-mix-R", mix_R_tidx)):
                lk = SubElement(target_ci, "link")
                SubElement(lk, "linkclipref").text = ref_cid
                SubElement(lk, "mediatype").text   = "audio"
                SubElement(lk, "trackindex").text  = str(ref_tidx)
                SubElement(lk, "clipindex").text   = "1"

        mix_track_L = SubElement(ael, "track")
        mix_track_L.append(ci_L)
        SubElement(mix_track_L, "outputchannelindex").text = "1"

        mix_track_R = SubElement(ael, "track")
        mix_track_R.append(ci_R)
        SubElement(mix_track_R, "outputchannelindex").text = "2"

    # ── Patch video↔camera-audio links ───────────────────────────────────────
    # Now that we know which tracks are non-empty (and their 1-based indices),
    # add <links> to each matched video/camera-audio clip pair so Premiere
    # shows them as linked (lock-step move, sync indicators, etc.).
    if link_pairs:
        # 1-based index of each non-empty video track in the <video> section
        v_track_idx = {}
        vi = 0
        for tn in track_names:
            if v_tracks.get(tn):
                vi += 1
                v_track_idx[tn] = vi

        # 1-based index of each non-empty cam track in the <audio> section
        # (comes after the non-empty a_tracks)
        cam_track_idx = {}
        ci_base = n_audio_written
        for tn in track_names:
            if cam_tracks.get(tn):
                ci_base += 1
                cam_track_idx[tn] = ci_base

        for v_el, ca_el, tn, pair_fid in link_pairs:
            vtidx  = v_track_idx.get(tn, 1)
            catidx = cam_track_idx.get(tn, 1)
            v_cid  = v_el.get("id")
            ca_cid = ca_el.get("id")

            # <masterclipid> (inserted at position 0) is what Premiere uses to
            # recognise that a video and audio clipitem come from the same source.
            # Without it, <link> cross-references alone do not produce linking.
            for el in (v_el, ca_el):
                mc_el = Element("masterclipid")
                mc_el.text = pair_fid
                el.insert(0, mc_el)

            # 1-based position of each clip within its own track list
            v_pos  = v_tracks[tn].index(v_el)  + 1
            ca_pos = cam_tracks[tn].index(ca_el) + 1

            # <link> elements are direct children of clipitem (no wrapper).
            # Both sides carry the full pair so Premiere locks them together.
            link_entries = [
                (v_cid,  "video", vtidx,  v_pos),
                (ca_cid, "audio", catidx, ca_pos),
            ]
            for target_el in (v_el, ca_el):
                for ref_cid, ref_media, ref_tidx, ref_pos in link_entries:
                    lk = SubElement(target_el, "link")
                    SubElement(lk, "linkclipref").text = ref_cid
                    SubElement(lk, "mediatype").text   = ref_media
                    SubElement(lk, "trackindex").text  = str(ref_tidx)
                    SubElement(lk, "clipindex").text   = str(ref_pos)

    return xmeml

# ── AAF builder ────────────────────────────────────────────────────────────────
def build_aaf(results, int_assets, vo_bins, parts, seq_name, gap_secs,
              seq_fps=24, seq_sr=48000, vo_takes_with_offset=None,
              out_path="output.aaf", progress_cb=None, cancel_event=None):
    """Build a multi-track AAF where:
      • Each guest token gets its own audio track.
      • All VO parts share a single "VO" track.
      • The host (HOST_NAME) gets a single shared track regardless of
        how many interview segments they appear in.
    Clips are placed at absolute timeline positions so gaps appear correctly
    in Pro Tools when the session is opened.
    """
    if not HAS_AAF:
        raise RuntimeError("pyaaf2 is not installed. Run: pip install pyaaf2")

    import struct as _struct
    from urllib.parse import quote as _url_quote

    def _wave_summary(n_ch, s_rate, n_bits):
        """Build a minimal RIFF/WAVE header for WAVEDescriptor.Summary.
        This is exactly the format Premiere Pro writes, which Pro Tools expects."""
        blk   = n_ch * (n_bits // 8)
        brate = s_rate * blk
        fmt   = _struct.pack('<HHIIHH', 1, n_ch, s_rate, brate, blk, n_bits)
        hdr   = (b'RIFF' + _struct.pack('<I', 36) +
                 b'WAVE' +
                 b'fmt ' + _struct.pack('<I', 16) + fmt +
                 b'data' + _struct.pack('<I', 0))
        return list(hdr)

    def _file_uri(path):
        """Build a percent-encoded file:// URI matching Premiere Pro's format.
        e.g. F:\\My Files\\x.wav  →  file:///F%3a/My%20Files/x.wav"""
        fwd = os.path.abspath(path).replace('\\', '/')
        encoded = '/'.join(_url_quote(p, safe='') for p in fwd.split('/'))
        return 'file:///' + encoded

    def _prog(msg):
        if callable(progress_cb):
            progress_cb(msg)

    def _check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise BuildCancelled("Build cancelled by user")

    sr          = seq_sr
    sample_rate = aaf2.rational.AAFRational("{}/1".format(sr))
    gap_sa      = round(gap_secs * sr)
    HOST_LC     = HOST_NAME.lower()

    def s2sa(secs):
        return round(secs * sr)

    # ── Defensive filter: strip transient mix-for-transcript files ────────────
    # mix_for_transcript() writes temp WAVs (prefix "_pb_mix_") used solely as
    # the Whisper input for multi-track tokens.  They are 16 kHz mono and may
    # be windowed/cleaned up by the time export runs, so they must NEVER appear
    # in the AAF.  Filter them out of int_assets and any takes_data.apath here.
    def _is_temp_mix_path(p):
        if not p:
            return False
        return os.path.basename(p).startswith("_pb_mix_")

    _filtered_int_assets = {}
    _stripped_count = 0
    for _tok, _paths in (int_assets or {}).items():
        _kept = [p for p in _paths if not _is_temp_mix_path(p)]
        _stripped_count += len(_paths) - len(_kept)
        _filtered_int_assets[_tok] = _kept
    int_assets = _filtered_int_assets
    if _stripped_count:
        _prog("WARNING: stripped {} transient mix WAV path(s) from int_assets — "
              "AAF will use original assets only".format(_stripped_count))

    # Same defensive filter for VO takes_data on each interview/VO result
    _vo_stripped = 0
    for _r in (results or []):
        for _td in (_r.get("takes_data") or []):
            if isinstance(_td, dict) and _is_temp_mix_path(_td.get("apath")):
                _td["apath"] = None
                _vo_stripped += 1
    if _vo_stripped:
        _prog("WARNING: cleared {} transient mix WAV path(s) from "
              "takes_data.apath — VO export will skip these takes".format(_vo_stripped))

    # Diagnostic: list each token's audio sources going into the build
    _src_lines = []
    for _tok in sorted(int_assets):
        _aps = [p for p in int_assets[_tok] if p and not is_video(p)]
        _vps = [p for p in int_assets[_tok] if p and is_video(p)]
        _src_lines.append("  [{}] {} audio + {} video".format(
            _tok, len(_aps), len(_vps)))
        for _p in _aps:
            _src_lines.append("       audio: {}".format(os.path.basename(_p)))
        for _p in _vps:
            _src_lines.append("       video: {}".format(os.path.basename(_p)))
    if _src_lines:
        _prog("AAF source manifest:\n" + "\n".join(_src_lines))

    vo_takes_by_part = {}
    if vo_takes_with_offset:
        for pi, takes_list in vo_takes_with_offset.items():
            vo_takes_by_part[pi] = [(e[1], e[2], e[3]) for e in takes_list]
    else:
        for pi, vb in vo_bins.items():
            paths = vb.get_paths() if hasattr(vb, "get_paths") else []
            takes = pair_takes(paths)
            vo_takes_by_part[pi] = [(0.0, vp, ap) for vp, ap in takes]

    cursor      = 0  # samples — advances identically for all tracks
    skipped     = []
    # slots: (filepath, start_sa, dur_sa, src_in_sa, track_name)
    slots       = []
    part_starts = {}  # part_index → sample position of first content in that part

    for res in sorted(results, key=lambda r: r["order"]):
        _check_cancel()
        is_vo    = res.get("is_vo", False)
        st       = res.get("status", "")
        segments = res.get("segments") or []

        if st in _TERMINAL_SKIP_STATUSES:
            skipped.append(res); continue
        if not is_vo and not segments:
            skipped.append(res); continue

        # ── Guard: cap clips whose source range is unreasonably long ──────────
        # A segment spanning > 30 minutes almost always means a typo in the
        # script @PULL timecodes.  Instead of skipping the clip entirely,
        # cap each over-long segment to _CAP_SECS from its declared start so
        # the clip still appears at the right spot in the timeline.
        _MAX_SEG_SECS = 1800   # 30 minutes
        _CAP_SECS     = 10.0   # fallback duration when a segment is over-long
        _check_segs   = segments
        if is_vo:
            _td0 = (res.get("takes_data") or [{}])[0]
            _check_segs = _td0.get("segments") or segments
        if any((e - s) > _MAX_SEG_SECS for s, e in _check_segs if e > s):
            _max_dur = max((e - s) for s, e in _check_segs if e > s)
            _prog("WARNING: capping {} @ {} — source span {:.0f}s → {}s placeholder".format(
                res.get("token", "?"), res.get("in_tc", "?"),
                _max_dur, int(_CAP_SECS)))
            # Cap non-VO segments directly
            segments = [
                (s, s + _CAP_SECS) if (e - s) > _MAX_SEG_SECS else (s, e)
                for s, e in segments if e > s
            ]
            # Cap VO takes_data segments
            if is_vo:
                _new_td = []
                for _td in (res.get("takes_data") or []):
                    _capped = [
                        (s, s + _CAP_SECS) if (e - s) > _MAX_SEG_SECS else (s, e)
                        for s, e in (_td.get("segments") or []) if e > s
                    ]
                    _new_td.append(dict(_td, segments=_capped))
                res = dict(res, takes_data=_new_td, segments=segments)
            # Fall through — clip is placed with capped duration

        # Record where this part first starts in the timeline
        _pi = res.get("part_index", 0)
        if _pi not in part_starts:
            part_starts[_pi] = cursor

        # ── Track assignment ──────────────────────────────────────────────────
        if is_vo:
            track = "VO"
        elif res.get("token", "").lower() == HOST_LC:
            # All host lines share a single track (e.g. all JORDAN questions)
            track = res["token"].upper()
        else:
            track = res.get("token", "UNKNOWN")

        if is_vo:
            takes_data = res.get("takes_data", [])
            if not takes_data:
                skipped.append(res); continue
            # Use the take that produced the best match, not necessarily [0].
            best_i  = res.get("best_take_index", 0)
            td      = takes_data[best_i] if best_i < len(takes_data) else takes_data[0]
            # Prefer the top-level segments — these reflect any waveform-editor
            # edits the user made in Step 4.  The take's own segments are the
            # original Whisper output and would override manual adjustments.
            # VO_TAIL_SECS is already baked into top-level segments at reconcile
            # time, so no additional buffer is added here.
            td_segs = segments or td.get("segments")
            ap      = td.get("apath")
            if ap:
                for seg_in_s, seg_out_s in td_segs:
                    if seg_in_s >= seg_out_s: continue
                    dur_sa = s2sa(seg_out_s - seg_in_s)
                    slots.append((ap, cursor, dur_sa, s2sa(seg_in_s), track))
                    cursor += dur_sa
            _vo_gap = s2sa(res["gap_after_s"]) if "gap_after_s" in res else gap_sa
            cursor += (_vo_gap if res.get("gap_after", True) else 0)
        else:
            tok    = res["token"]
            paths  = int_assets.get(tok, [])
            # Prefer pure-audio files; fall back to video files whose embedded
            # audio will be extracted by ffmpeg (-vn) during the WAV conversion.
            apaths = [p for p in paths if p and is_audio(p)]
            if not apaths:
                apaths = [p for p in paths if p and is_video(p)]
            if not apaths:
                skipped.append(res); continue

            # Identify the host side by HOST_NAME appearing in the filename.
            # Host files share a single track across all tokens; every other
            # file gets its own dedicated track so no source is ever dropped
            # — important for setups where Riverside exports anonymised
            # "speaker-0" / "speaker-1" filenames or where a token has 3+
            # parallel sources.
            host_paths  = [p for p in apaths
                           if HOST_LC in os.path.basename(p).lower()]
            guest_paths = [p for p in apaths
                           if HOST_LC not in os.path.basename(p).lower()]

            for si, (seg_in_s, seg_out_s) in enumerate(segments):
                if seg_in_s >= seg_out_s: continue
                dur_sa  = s2sa(seg_out_s - seg_in_s)
                is_last = (si == len(segments) - 1)
                src_in  = s2sa(seg_in_s)

                # Each non-host file → its own track.  Primary file uses the
                # token name; additional files spill to TOKEN_2, TOKEN_3, …
                for gi, gp in enumerate(guest_paths):
                    track_name = track if gi == 0 else "{}_{}".format(track, gi + 1)
                    slots.append((gp, cursor, dur_sa, src_in, track_name))
                # All host-named files → shared JORDAN track
                for hp in host_paths:
                    slots.append((hp, cursor, dur_sa, src_in,
                                  HOST_NAME.upper()))

                _int_gap = s2sa(res["gap_after_s"]) if "gap_after_s" in res else gap_sa
                cursor += dur_sa + (_int_gap if (is_last and res.get("gap_after", True)) else 0)

    _prog("Analysing {} clips across {} tracks…".format(len(slots),
          len(set(s[4] for s in slots))))

    # ── Determine track order: VO first, host second, guests alphabetically ────
    seen_tracks = list(dict.fromkeys(s[4] for s in slots))  # preserve first-seen order

    def _track_sort_key(t):
        if t == "VO":             return (0, t)
        if t.lower() == HOST_LC:  return (1, t)
        return (2, t)

    track_order = sorted(seen_tracks, key=_track_sort_key)

    # ── Create audio output directory ─────────────────────────────────────────
    # All converted WAVs land in "{aaf_stem} Audio Files/" next to the AAF.
    # Files are named after the SourceMob's UMID (SMPTE ID) — the same
    # convention Premiere/Avid uses — so Pro Tools can resolve them
    # automatically without a manual relink step.
    aaf_stem    = os.path.splitext(os.path.basename(out_path))[0]
    audio_dir   = os.path.join(os.path.dirname(os.path.abspath(out_path)),
                               aaf_stem + " Audio Files")
    os.makedirs(audio_dir, exist_ok=True)
    ff          = _ffmpeg_cmd()
    unique_srcs = list(dict.fromkeys(s[0] for s in slots))

    # 2-second silent WAV used as source for PARTS marker clips.
    # Must be at least ~0.5 s so Pro Tools renders the clip wide enough to
    # show its name label in the track.
    marker_dur_sa   = sr * 2         # 2 seconds in samples
    marker_wav_path = os.path.join(audio_dir, "_part_markers.wav")
    try:
        with wave.open(marker_wav_path, "w") as _wf:
            _wf.setnchannels(1); _wf.setsampwidth(3); _wf.setframerate(sr)
            _wf.writeframes(b"\x00" * 3 * marker_dur_sa)   # 2 seconds of silence
    except Exception:
        marker_wav_path = None

    with aaf2.open(out_path, "w") as f:
        comp_mob = f.create.CompositionMob(seq_name)
        comp_mob.usage = "Usage_TopLevel"
        f.content.mobs.append(comp_mob)

        # ── One timeline slot per track ───────────────────────────────────────
        track_seqs    = {}   # track_name → Sequence
        track_lengths = {}   # track_name → samples written so far (O(1) gap calc)
        for track_name in track_order:
            seq        = f.create.Sequence(media_kind="sound")
            comp_slot  = comp_mob.create_timeline_slot(sample_rate)
            comp_slot.segment = seq
            comp_slot.name    = track_name   # becomes the Pro Tools track label
            track_seqs[track_name]    = seq
            track_lengths[track_name] = 0

        # ── PARTS marker track ────────────────────────────────────────────────
        parts_seq = None
        parts_cur = 0
        if parts and part_starts and marker_wav_path:
            parts_seq  = f.create.Sequence(media_kind="sound")
            parts_slot = comp_mob.create_timeline_slot(sample_rate)
            parts_slot.segment = parts_seq
            parts_slot.name    = "PARTS"

        # ── One SourceMob + MasterMob per unique source file ─────────────────
        # The SourceMob is created FIRST so its MobID (UMID) is known before
        # we convert the WAV.  The WAV is then saved as "{UMID}.wav", which
        # is exactly the filename Pro Tools looks for when opening a linked AAF
        # (the same convention used by Premiere when exporting AAF + audio).
        file_mobs = {}
        n_srcs    = len(unique_srcs)

        for src_i, src in enumerate(unique_srcs):
            _check_cancel()

            # Preserve the source channel layout up to stereo.  Anything wider
            # (5.1, 7.1, etc.) is folded to stereo so Pro Tools doesn't choke.
            # Mono sources stay mono — no upmix.
            src_chans = min(get_audio_channels(src), 2)

            # Deterministic WAV name: SHA-1 of absolute source path + channel
            # count + sample rate.  Including conversion parameters means a
            # change (e.g. mono→stereo on rebuild) forces a fresh conversion
            # rather than silently reusing the wrong file.
            src_hash = hashlib.sha1(
                "{}|{}|{}".format(
                    os.path.abspath(src), src_chans, sr
                ).encode()
            ).hexdigest()[:32].upper()
            wav_name = src_hash + ".wav"
            wav_path = os.path.join(audio_dir, wav_name)

            if os.path.isfile(wav_path):
                _prog("File {} of {}  —  {}  [skipping — already converted]".format(
                    src_i + 1, n_srcs, os.path.basename(src)))
            else:
                ch_label = "stereo" if src_chans == 2 else "mono"
                _prog("Converting file {} of {}  —  {}  ({})".format(
                    src_i + 1, n_srcs, os.path.basename(src), ch_label))
                cmd = ff + [
                    "-y", "-i", src,
                    "-vn",                      # audio only
                    "-ac", str(src_chans),      # preserve mono/stereo; fold >2ch
                    "-ar", str(sr),             # resample to session rate
                    "-c:a", "pcm_s24le",        # 24-bit PCM
                    wav_path,
                ]
                try:
                    r = _run(cmd, capture_output=True, timeout=600)
                    if r.returncode != 0:
                        wav_path = src   # fall back to original on ffmpeg error
                except Exception:
                    wav_path = src       # fall back on timeout / missing ffmpeg

            # ── SourceMob (physical file) ─────────────────────────────────────
            src_mob = f.create.SourceMob()
            f.content.mobs.append(src_mob)
            src_mob.name = basename(src)   # human-readable name shown in Pro Tools

            n_chan      = src_chans
            file_dur_s  = get_media_duration(wav_path) or 7200.0
            file_dur_sa = s2sa(file_dur_s)
            bits        = 24
            blk         = n_chan * (bits // 8)

            # SourceMob slot: null SourceClip (end-of-chain) — matches Premiere
            src_slot         = src_mob.create_timeline_slot(sample_rate)
            src_slot.name    = "A1"
            sc               = f.create.SourceClip(media_kind="sound",
                                                   length=file_dur_sa)
            src_slot.segment = sc

            # WAVEDescriptor with embedded RIFF header — what Pro Tools expects
            desc = f.create.WAVEDescriptor()
            desc["Summary"].value    = _wave_summary(n_chan, sr, bits)
            desc["SampleRate"].value = sample_rate
            desc.length              = file_dur_sa
            try:
                desc["ContainerFormat"].value = f.dictionary.lookup_containerdef(
                    "ContainerDef_AAFKLV")
            except Exception:
                pass
            src_mob.descriptor = desc

            # NetworkLocator — percent-encoded URI matching Premiere's format
            # NOTE: desc.locator returns a new throwaway list each access;
            # desc["Locator"].append() is the only API that actually persists.
            loc = f.create.NetworkLocator()
            loc["URLString"].value = _file_uri(wav_path)
            src_mob.descriptor["Locator"].append(loc)

            # ── MasterMob (clip mob) ──────────────────────────────────────────
            master      = f.create.MasterMob(basename(src))  # original filename = clip name in PT
            master_slot = master.create_timeline_slot(sample_rate)
            master_slot.segment = src_mob.create_source_clip(
                slot_id=src_slot.slot_id, media_kind="sound")
            f.content.mobs.append(master)
            file_mobs[src] = (master, master_slot)

        _prog("Writing AAF timeline ({} clips)…".format(len(slots)))

        for filepath, start_sa, dur_sa, src_in_sa, track_name in slots:
            master, master_slot = file_mobs[filepath]
            seq     = track_seqs[track_name]
            cur_len = track_lengths[track_name]

            # Pad this track to the correct timeline position
            if start_sa > cur_len:
                seq.components.append(
                    f.create.Filler(media_kind="sound",
                                    length=start_sa - cur_len))

            clip = master.create_source_clip(
                slot_id=master_slot.slot_id, length=dur_sa, start=src_in_sa)
            seq.components.append(clip)
            track_lengths[track_name] = start_sa + dur_sa

        # ── Part markers (PARTS track) ────────────────────────────────────────
        # DescriptiveMarker/EventMobSlot objects are not surfaced by Pro Tools
        # on AAF import.  Instead we create a dedicated "PARTS" audio track
        # with one named 1-sample clip per part boundary.  Pro Tools shows the
        # clip names inline on the track, giving clear visual part markers.
        if parts_seq is not None and part_starts:
            # SourceMob for the silent marker WAV
            mk_src = f.create.SourceMob()
            mk_src.name = "_part_markers"
            f.content.mobs.append(mk_src)
            mk_src_slot      = mk_src.create_timeline_slot(sample_rate)
            mk_src_slot.name = "A1"
            mk_src_slot.segment = f.create.SourceClip(media_kind="sound",
                                                       length=marker_dur_sa)
            mk_desc = f.create.WAVEDescriptor()
            mk_desc["Summary"].value    = _wave_summary(1, sr, 24)
            mk_desc["SampleRate"].value = sample_rate
            mk_desc.length              = marker_dur_sa
            try:
                mk_desc["ContainerFormat"].value = f.dictionary.lookup_containerdef(
                    "ContainerDef_AAFKLV")
            except Exception:
                pass
            mk_src.descriptor = mk_desc
            mk_loc = f.create.NetworkLocator()
            mk_loc["URLString"].value = _file_uri(marker_wav_path)
            mk_src.descriptor["Locator"].append(mk_loc)

            part_map = {p["index"]: p["name"] for p in parts}
            for pi, pos_sa in sorted(part_starts.items()):
                pname = part_map.get(pi, "Part {}".format(pi + 1))
                # MasterMob whose name becomes the clip label in Pro Tools
                mk_master      = f.create.MasterMob(pname)
                mk_master_slot = mk_master.create_timeline_slot(sample_rate)
                mk_master_slot.name    = "A1"
                mk_master_slot.segment = mk_src.create_source_clip(
                    slot_id=mk_src_slot.slot_id, media_kind="sound")
                f.content.mobs.append(mk_master)
                # Pad PARTS track up to this part's position
                if pos_sa > parts_cur:
                    parts_seq.components.append(
                        f.create.Filler(media_kind="sound",
                                        length=pos_sa - parts_cur))
                clip = mk_master.create_source_clip(
                    slot_id=mk_master_slot.slot_id, length=marker_dur_sa, start=0)
                parts_seq.components.append(clip)
                parts_cur = pos_sa + marker_dur_sa

    return out_path, skipped

def generate_report(results, skipped, parts, seq_name, xml_path):
    import datetime
    W = 72

    def hr(ch="─"): return ch * W
    def section(title):
        return "\n{}\n  {}\n{}".format(hr("═"), title.upper(), hr("═"))
    def subsection(title):
        return "\n  {}\n  {}".format(title, hr("─")[2:])
    def dur(in_s, out_s):
        d = max(0, (out_s or 0) - (in_s or 0))
        return "{:d}s".format(int(d))

    lines = []
    now   = datetime.datetime.now().strftime("%Y-%m-%d  %H:%M")

    lines += [
        hr("═"),
        "  {} — RECONCILE REPORT".format(seq_name),
        "  Generated: {}".format(now),
        "  XML: {}".format(xml_path),
        hr("═"),
    ]

    all_results = results + skipped
    iv_results  = [r for r in all_results if not r.get("is_vo")]
    vo_results  = [r for r in all_results if r.get("is_vo")]

    status_counts = {}
    for r in all_results:
        st = r.get("status", "unknown")
        status_counts[st] = status_counts.get(st, 0) + 1

    ok_iv    = sum(1 for r in iv_results if r.get("status") == "ok")
    direct   = sum(1 for r in iv_results if r.get("status") == "direct")
    low_iv   = sum(1 for r in iv_results if r.get("status") == "low_confidence")
    miss_iv  = sum(1 for r in iv_results if r.get("status") in ("no_match","no_quote","no_transcript","error"))
    skip_iv  = sum(1 for r in skipped    if not r.get("is_vo"))

    ok_vo    = sum(1 for r in vo_results if r.get("status") in ("ok","direct"))
    low_vo   = sum(1 for r in vo_results if r.get("status") == "low_confidence")
    fall_vo  = sum(1 for r in vo_results if r.get("status") == "no_match")
    skip_vo  = sum(1 for r in skipped    if r.get("is_vo"))

    total_in_timeline = len(results)
    total_skipped     = len(skipped)

    lines += ["", "  SUMMARY", "  " + hr()]
    lines += ["  {:<30} {}".format(k, v) for k, v in [
        ("Sequence",                seq_name),
        ("Parts / markers",         len(parts)),
        ("",                        ""),
        ("Interview pulls placed",  ok_iv + direct + low_iv),
        ("  ✓  Clean match",        ok_iv),
        ("  ✓  Direct (no whisper)",direct),
        ("  ⚠  Low confidence",     low_iv),
        ("  ✕  Failed / missing",   miss_iv + skip_iv),
        ("",                        ""),
        ("VO blocks placed",        ok_vo + low_vo + fall_vo),
        ("  ✓  Matched",            ok_vo),
        ("  ⚠  Low confidence",     low_vo),
        ("  ?  Fallback span used", fall_vo),
        ("  ✕  Skipped",            skip_vo),
        ("",                        ""),
        ("Total clips in timeline", total_in_timeline),
        ("Total skipped",           total_skipped),
    ] if k or v]

    lines += [section("Part-by-part breakdown")]

    part_map = {p["index"]: p["name"] for p in parts}
    part_indices = sorted(part_map)

    for pi in part_indices:
        pname    = part_map[pi]
        p_iv     = [r for r in all_results if not r.get("is_vo") and r.get("part_index") == pi]
        p_vo     = [r for r in all_results if r.get("is_vo")     and r.get("part_index") == pi]

        lines += [subsection("PART {}  —  {}".format(pi + 1, pname))]

        if p_iv:
            lines.append("")
            lines.append("  Interview pulls:")
            for r in sorted(p_iv, key=lambda x: x.get("order", 0)):
                st      = r.get("status", "?")
                tok     = r.get("token", "?")
                in_tc   = r.get("in_tc", "?")
                segs    = r.get("segments") or []
                in_s    = r.get("rec_in_s")
                out_s   = r.get("rec_out_s")
                conf    = r.get("confidence")
                quote   = (r.get("quote_text") or "").strip()[:60]

                if st == "ok":
                    flag = "✓"
                elif st == "direct":
                    flag = "✓ direct"
                elif st == "low_confidence":
                    flag = "⚠ LOW CONF"
                elif st in ("no_match",):
                    flag = "✕ NO MATCH"
                elif st == "no_transcript":
                    flag = "✕ NO TRANSCRIPT"
                elif st == "no_quote":
                    flag = "✕ NO QUOTE"
                elif st in ("error", "extract_failed"):
                    flag = "✕ ERROR"
                elif st in ("cancelled", "no_file"):
                    flag = "✕ SKIPPED"
                elif st == "too_long":
                    segs = r.get("segments") or []
                    max_dur = max((e-s) for s,e in segs if e>s) if segs else 0
                    flag = "✕ TOO LONG ({:.0f}s)".format(max_dur)
                else:
                    flag = "? {}".format(st)

                conf_str = "  conf={:.0%}".format(conf) if conf is not None else ""
                dur_str  = "  ({})".format(dur(in_s, out_s)) if in_s is not None else ""
                segs_str = "  {} seg{}".format(len(segs), "s" if len(segs) != 1 else "") if segs else ""

                lines.append("    [{flag:<12}] {tok:<12} @{in_tc}{conf}{dur}{segs}".format(
                    flag=flag, tok=tok, in_tc=in_tc,
                    conf=conf_str, dur=dur_str, segs=segs_str))
                if quote:
                    lines.append("                       '{}'".format(quote))

        if p_vo:
            lines.append("")
            lines.append("  VO blocks:")
            for r in sorted(p_vo, key=lambda x: x.get("order", 0)):
                st         = r.get("status", "?")
                takes_data = r.get("takes_data") or []
                n_takes    = len(takes_data)
                fallback   = any(td.get("is_fallback") for td in takes_data)
                quote      = (r.get("quote_text") or "").strip()[:60]

                if fallback:
                    flag = "?  FALLBACK"
                elif st in ("ok", "direct"):
                    flag = "✓ ({} take{})".format(n_takes, "s" if n_takes != 1 else "")
                elif st == "low_confidence":
                    flag = "⚠ LOW CONF ({} takes)".format(n_takes)
                elif st == "no_match":
                    flag = "✕ NO MATCH"
                elif st == "not_run":
                    flag = "– NOT RUN"
                else:
                    flag = "? {}".format(st)

                lines.append("    [{flag:<14}]  '{quote}'".format(
                    flag=flag,
                    quote=quote if quote else "(no text)"))

    issues = []
    for r in all_results:
        st  = r.get("status", "")
        tok = r.get("token", "")
        pi  = r.get("part_index", 0)
        pnm = part_map.get(pi, "Part {}".format(pi+1))
        itc = r.get("in_tc", "")
        qt  = (r.get("quote_text") or "").strip()[:50]

        if st == "low_confidence":
            conf = r.get("confidence")
            cs   = " ({:.0%})".format(conf) if conf is not None else ""
            issues.append(("⚠  Low confidence{}".format(cs),
                           tok, pnm, itc, qt))
        elif st == "no_match":
            if r.get("is_vo"):
                issues.append(("?  VO fallback span used",
                               tok, pnm, itc, qt))
            else:
                issues.append(("✕  No match found",
                               tok, pnm, itc, qt))
        elif st in ("no_quote", "no_transcript", "error", "extract_failed"):
            issues.append(("✕  {}".format(st.replace("_"," ").title()),
                           tok, pnm, itc, qt))

    if issues:
        lines += [section("Issues requiring attention  ({})".format(len(issues)))]
        lines.append("  These clips need manual review in the timeline.\n")
        for flag, tok, pnm, itc, qt in issues:
            lines.append("  {}".format(flag))
            lines.append("    Session : {}".format(tok))
            lines.append("    Part    : {}".format(pnm))
            if itc:
                lines.append("    Script TC: {}".format(itc))
            if qt:
                lines.append("    Quote   : '{}'".format(qt))
            lines.append("")
    else:
        lines += [section("Issues requiring attention"), "",
                  "  None — all clips matched cleanly.", ""]

    if skipped:
        lines += [section("Skipped  ({} clips NOT in timeline)".format(len(skipped)))]
        lines.append("  These were excluded entirely.\n")
        for r in skipped:
            st  = r.get("status", "?")
            tok = r.get("token", "?")
            itc = r.get("in_tc", "")
            qt  = (r.get("quote_text") or "").strip()[:55]
            lines.append("  ✕ [{:<16}] {:<14} @{}".format(st, tok, itc))
            if qt:
                lines.append("      '{}'".format(qt))
        lines.append("")

    lines += ["", hr("═"),
              "  End of report  —  {}".format(now),
              hr("═"), ""]

    text = "\n".join(lines)

    report_path = os.path.splitext(xml_path)[0] + "_report.txt"
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        report_path = None

    return text, report_path