import os
import re
from utils import tc_secs, basename

# ── Script parser ──────────────────────────────────────────────────────────────
def parse_script(text):
    """
    Returns:
      tokens   : [str]
      parts    : [{index, name}]
      pulls    : [{order, token, in_tc, out_tc, in_seconds, out_seconds,
                   part_index, quote_text}]
      doc_title: str
      warnings : [str]
    """
    tokens, parts, pulls, vo_blocks, warnings = [], [], [], [], []
    doc_title = "Blood Trails Episode"

    for mk in ["--- INTERVIEW SESSIONS START ---", "--- EPISODE ASSETS START ---"]:
        idx = text.find(mk)
        if idx != -1:
            doc_title = next(
                (l.strip() for l in text[:idx].strip().split("\n") if l.strip()),
                doc_title)
            break

    for sm, em in [
        ("--- INTERVIEW SESSIONS START ---", "--- INTERVIEW SESSIONS END ---"),
        ("--- EPISODE ASSETS START ---",     "--- EPISODE ASSETS END ---"),
    ]:
        m = re.search(re.escape(sm) + r'(.*?)' + re.escape(em), text, re.DOTALL)
        if m:
            for ln in m.group(1).split("\n"):
                clean = re.sub(r'//.*$', '', ln).strip()
                tm = re.match(r'^\[([A-Z0-9_]+)\]$', clean)
                if tm and tm.group(1) not in tokens:
                    tokens.append(tm.group(1))
            break

    # Single pass — shared global order counter for both @PULL and @VO blocks
    # so that build_xml sorts them in true script order.
    lines    = text.split("\n")
    cur_part = 0
    order    = 1        # global sequence counter shared by pulls + VO blocks
    seen     = set()
    i        = 0

    PULL_RE = re.compile(
        r'^@PULL\s+([A-Z0-9_]+)\s+\[(\d{2}:\d{2}:\d{2})-(\d{2}:\d{2}:\d{2})\]')
    VO_RE   = re.compile(r'^@VO\s+(PART_\d+_[A-Z]+)\s*$')

    while i < len(lines):
        ln = lines[i].strip()

        # @PART
        pm = re.match(r'^@PART\s+(.+)$', ln)
        if pm:
            cur_part += 1
            parts.append({"index": cur_part, "name": pm.group(1).strip()})
            i += 1; continue

        # @PULL
        qm = PULL_RE.match(ln)
        if qm:
            tok, in_tc, out_tc = qm.group(1), qm.group(2), qm.group(3)
            in_s, out_s = tc_secs(in_tc), tc_secs(out_tc)
            key = (tok, in_s, out_s)
            if key not in seen:
                seen.add(key)
                if tok not in tokens:
                    warnings.append("Unknown token '{}' in @PULL".format(tok))
                i += 1
                quote_lines     = []
                _last_was_blank = False
                while i < len(lines):
                    nxt = lines[i].strip()
                    if not nxt:
                        _last_was_blank = True
                        if quote_lines: break   # blank after text → end of quote
                        i += 1; continue        # blank before text → skip
                    if nxt.startswith("@") or nxt.startswith("//"):
                        break
                    _last_was_blank = False     # reset when text resumes
                    quote_lines.append(nxt)
                    i += 1
                quote_text = " ".join(quote_lines).strip()
                quote_text = quote_text.strip('\u201c\u201d\u2018\u2019"\'')
                # gap_after=False only when the next @PULL/@VO immediately follows
                # with NO blank line.  _last_was_blank catches the case where the
                # loop consumed blank lines before arriving at the next token.
                _next_ln  = lines[i].strip() if i < len(lines) else ""
                gap_after = _last_was_blank or not (
                    _next_ln.startswith("@PULL") or _next_ln.startswith("@VO"))
                pulls.append({
                    "order":      order,
                    "token":      tok,
                    "in_tc":      in_tc,
                    "out_tc":     out_tc,
                    "in_seconds": in_s,
                    "out_seconds":out_s,
                    "part_index": cur_part,
                    "quote_text": quote_text,
                    "gap_after":  gap_after,
                })
                order += 1
            else:
                i += 1
            continue

        # @VO
        vm = VO_RE.match(ln)
        if vm:
            vo_id = vm.group(1)
            i += 1
            paragraphs      = []   # completed paragraph strings
            para_lines      = []   # lines in the paragraph being built
            _last_was_blank = False
            while i < len(lines):
                nxt = lines[i].strip()
                if not nxt:
                    # Blank line → end of current paragraph
                    if para_lines:
                        paragraphs.append(" ".join(para_lines))
                        para_lines = []
                    _last_was_blank = True
                    i += 1; continue
                if nxt.startswith("@") or nxt.startswith("//"):
                    break
                _last_was_blank = False   # text resumes after blank lines
                para_lines.append(nxt)
                i += 1
            if para_lines:
                paragraphs.append(" ".join(para_lines))
            # Join paragraphs with " ... " so match_segments treats each
            # paragraph as an independent chunk when searching the transcript.
            # A single-paragraph block produces no separators (no change from
            # previous behaviour).
            vo_text = " ... ".join(paragraphs).strip()
            if vo_text:
                _next_ln  = lines[i].strip() if i < len(lines) else ""
                gap_after = _last_was_blank or not (
                    _next_ln.startswith("@PULL") or _next_ln.startswith("@VO"))
                vo_blocks.append({
                    "order":      order,
                    "id":         vo_id,
                    "part_index": cur_part,
                    "text":       vo_text,
                    "gap_after":  gap_after,
                })
                order += 1
            continue

        i += 1

    # ── Robustness: if VO blocks exist but no @PART was declared, auto-create a
    # default part so the MediaPool always offers a "VO: …" assignment option
    # and reconciliation can proceed without requiring @PART in the script. ────
    if vo_blocks and not parts:
        parts.append({"index": 1, "name": "Episode VO"})
        for vb in vo_blocks:
            vb["part_index"] = 1

    return tokens, parts, pulls, vo_blocks, doc_title, warnings

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
    import re as _re

    def _normalize(name):
        name = os.path.splitext(name)[0]
        name = _re.sub(r'\.[LR]$', '', name)
        name = _re.sub(r'-Norm_\d+(-\d+)?$', '', name)
        name = _re.sub(r'-\d{2}$', '', name)
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
            fn_words   = set(_re.findall(r'[a-z0-9]+', fn))
            clip_words = set(_re.findall(r'[a-z0-9]+', clip_base))
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
    import re as _re

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
        for w in _re.findall(r'[a-z0-9]+', text.lower()):
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


def detect_sync_offset(video_path, audio_path, probe_duration=45.0, sample_rate=8000,
                       start_offset=0.0):
    """
    Detect the sync offset between a video file's embedded audio and a reference
    audio file using FFT-based cross-correlation.

    Returns (offset_secs: float, confidence: float 0..1)

    offset_secs: add this to the video's src_in to align it with the audio timeline.
      Negative → video started after the recorder (camera came in late).
      Positive → video started before the recorder (camera rolled early).

    confidence: z-score of the correlation peak relative to the noise floor,
      scaled so that ~20 sigma = 1.0.  Values ≥ 0.5 are reliable.

    start_offset: seek both files to this position (seconds) before probing.
      Use this for clips that are deep into a long recording so the cross-
      correlation is run against the correct section of the file.

    Requires ffmpeg in PATH and numpy.
    """
    import subprocess as _sp
    import tempfile
    import wave as _wave

    try:
        import numpy as _np
    except ImportError:
        raise RuntimeError(
            "numpy is required for sync detection.\n"
            "Run:  pip install numpy")

    # Build a safe seek prefix: seek slightly before the target to avoid
    # landing on a keyframe boundary, then let ffmpeg trim to exact position.
    seek_s = max(0.0, start_offset - 2.0)

    with tempfile.TemporaryDirectory() as tmpdir:
        vid_wav = os.path.join(tmpdir, "vid.wav")
        ref_wav = os.path.join(tmpdir, "ref.wav")

        # Force 16-bit signed PCM output so _read_wav always gets the same format
        for src, dst in ((video_path, vid_wav), (audio_path, ref_wav)):
            cmd = ["ffmpeg", "-y"]
            if seek_s > 0:
                cmd += ["-ss", "{:.3f}".format(seek_s)]
            cmd += [
                "-t", str(probe_duration + max(0.0, start_offset - seek_s) + 2.0),
                "-i", src,
                "-ac", "1", "-ar", str(sample_rate),
                "-acodec", "pcm_s16le",
                "-vn", dst,
            ]
            r = _sp.run(cmd, capture_output=True, timeout=120)
            if r.returncode != 0:
                raise RuntimeError(
                    "ffmpeg failed extracting audio from {}:\n{}".format(
                        basename(src),
                        r.stderr.decode(errors="replace")[-400:]))

        def _read_wav(path):
            with _wave.open(path, "rb") as w:
                data = w.readframes(w.getnframes())
            return _np.frombuffer(data, _np.int16).astype(_np.float32) / 32768.0

        a_full = _read_wav(vid_wav)   # video embedded audio
        b_full = _read_wav(ref_wav)   # reference (session / recorder) audio

    # When a start_offset was requested, trim the pre-roll we extracted so
    # both arrays start at the requested offset position.
    trim = int((start_offset - seek_s) * sample_rate)
    a = a_full[trim:] if trim > 0 else a_full
    b = b_full[trim:] if trim > 0 else b_full

    # Keep only probe_duration worth of samples
    n_probe = int(probe_duration * sample_rate)
    a = a[:n_probe]
    b = b[:n_probe]

    # Skip the first 2 s of pre-roll silence (same window on both signals so
    # the relative lag is unaffected)
    skip = min(int(2 * sample_rate), len(a) // 8, len(b) // 8)
    a = a[skip:];  b = b[skip:]

    # Zero-mean
    a -= _np.mean(a);  b -= _np.mean(b)

    # Crude high-pass via first-order differencing: kills DC and sub-80 Hz
    # rumble (camera handling noise, HVAC) that differs strongly between mics
    # and would otherwise swamp the speech-band correlation.
    a = _np.diff(a, prepend=a[:1])
    b = _np.diff(b, prepend=b[:1])

    # Re-normalise to unit variance after high-pass
    std_a = _np.std(a);  std_b = _np.std(b)
    if std_a < 1e-6 or std_b < 1e-6:
        return 0.0, 0.0   # one signal is essentially silent
    a /= std_a;  b /= std_b

    # --- Helper: FFT cross-correlation + z-score confidence -----------------
    def _xcorr(x, y):
        """Return (signed_lag_in_samples, confidence_0_to_1)."""
        n_ = len(x) + len(y) - 1
        N_ = 1 << int(_np.ceil(_np.log2(max(n_, 1))))
        C_ = _np.fft.irfft(
            _np.fft.rfft(y, N_) * _np.conj(_np.fft.rfft(x, N_)), N_)[:n_]
        pk_ = int(_np.argmax(_np.abs(C_)))
        lag_ = pk_ if pk_ < n_ // 2 else pk_ - n_
        C_abs_     = _np.abs(C_)
        noise_mean = float(_np.mean(C_abs_))
        noise_std  = float(_np.std(C_abs_))
        z_ = (float(C_abs_[pk_]) - noise_mean) / max(noise_std, 1e-9)
        return lag_, min(1.0, z_ / 20.0)

    # --- Method 1: Waveform cross-correlation (best for matched mics) -----
    wf_lag, wf_conf = _xcorr(a, b)
    wf_offset = -wf_lag / sample_rate

    # --- Method 2: Envelope cross-correlation (robust to mic differences) --
    # Compares RMS amplitude envelopes instead of raw waveforms.  This strips
    # away spectral differences between mics and captures only the timing of
    # speech energy (talk vs. silence), which is identical regardless of mic
    # quality, proximity, or room acoustics.
    env_win = int(0.015 * sample_rate)  # 15 ms RMS window (sub-frame at 60 fps)
    env_conf = 0.0
    env_offset = 0.0
    na = (len(a) // env_win) * env_win
    nb = (len(b) // env_win) * env_win
    if na >= env_win and nb >= env_win:
        a_env = _np.sqrt(_np.mean(a[:na].reshape(-1, env_win) ** 2, axis=1))
        b_env = _np.sqrt(_np.mean(b[:nb].reshape(-1, env_win) ** 2, axis=1))
        a_env -= _np.mean(a_env);  b_env -= _np.mean(b_env)
        std_ae = _np.std(a_env);   std_be = _np.std(b_env)
        if std_ae > 1e-9 and std_be > 1e-9:
            a_env /= std_ae;  b_env /= std_be
            env_lag, env_conf = _xcorr(a_env, b_env)
            env_offset = -env_lag * env_win / sample_rate

    # Return whichever method found a stronger match.
    if env_conf > wf_conf:
        return env_offset, env_conf
    return wf_offset, wf_conf


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
    import subprocess as _sp
    import tempfile
    import wave as _wave

    try:
        import numpy as _np
    except ImportError:
        raise RuntimeError("numpy is required.\nRun:  pip install numpy")

    def _extract(src, dst):
        r = _sp.run(
            ["ffmpeg", "-y",
             "-t", str(search_secs),
             "-i", src,
             "-ac", "1", "-ar", str(sample_rate),
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
    import re as _re
    if not clip_name or clip_name.startswith('('):
        return None
    # Skip Pro Tools rendered fade regions ("Fade N")
    if _re.match(r'^Fade\s+\d+$', clip_name, _re.IGNORECASE):
        return None
    # Strip rightmost copy-number suffix -NN (2+ digits) with optional .L/.R
    # Discard .L/.R entirely — L and R tracks consolidate to the same source entry
    m = _re.match(r'^(.+)-(\d{2,})(\.[LR])?$', clip_name)
    name = m.group(1) if m else clip_name
    # Strip iZotope RX processing suffix: -RX10Dk_43, -RX8Dn_01, etc.
    name = _re.sub(r'-RX\d+[A-Za-z]+_\d+', '', name)
    # Strip any remaining trailing .L/.R channel designator
    name = _re.sub(r'\.[LR]$', '', name)
    # Strip common audio/video file extensions
    name = _re.sub(r'\.(wav|aif|aiff|mp3|m4v|mp4|mxf|mov)$', '', name,
                   flags=_re.IGNORECASE)
    return name or None


def parse_aaf_session(aaf_path):
    """
    Parse a Pro Tools AAF export.

    Returns:
      {
        session_name  : str,
        fps           : float,        # always 29.97 — audio AAF has no FPS slot
        sample_rate   : int,
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
    import re as _re
    _fade_pat = _re.compile(r'^Fade\s+\d+$', _re.IGNORECASE)

    try:
        import aaf2
    except ImportError:
        raise RuntimeError(
            "aaf2 library is required for AAF parsing. Run: pip install pyaaf2")

    result = {
        "session_name": os.path.splitext(os.path.basename(aaf_path))[0],
        "fps":          29.97,
        "sample_rate":  48000,
        "tracks":       [],
        "markers":      [],
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

        for slot in comp_mob.slots:
            rate      = _rate(slot)
            slot_name = ''
            try: slot_name = slot.name or ''
            except: pass

            seg    = slot.segment
            seg_cn = _cn(seg)
            if seg_cn != 'Sequence':
                continue

            track_clips    = []
            is_marker_slot = False
            timeline_pos   = 0

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
                            except Exception:
                                pass
                            break   # first matching slot is enough

                    # Skip fades and any clip get_clip_base_name can't resolve
                    if get_clip_base_name(clip_name) is None:
                        timeline_pos += comp_len
                        continue

                    start_secs   = timeline_pos / rate if rate else 0.0
                    dur_secs     = comp_len     / rate if rate else 0.0
                    end_secs     = start_secs + dur_secs
                    src_out_secs = src_in_secs + dur_secs

                    track_clips.append({
                        "clip_name":    clip_name,
                        "start_secs":   start_secs,
                        "end_secs":     end_secs,
                        "src_in_secs":  src_in_secs,
                        "src_out_secs": src_out_secs,
                        "state":        "Unmuted",
                    })

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