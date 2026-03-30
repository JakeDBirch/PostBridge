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
    doc_title = "PostBridge Episode"

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


def detect_sync_offset(video_path, audio_path, probe_duration=300.0, sample_rate=8000,
                       start_offset=0.0):
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
    import subprocess as _sp
    import tempfile

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
            cmd = ["ffmpeg", "-y", "-v", "quiet"]
            if t_start > 0.5:
                cmd += ["-ss", "{:.3f}".format(t_start)]
            cmd += ["-t",  "{:.3f}".format(max(t_dur, 0.1)),
                    "-i",  path,
                    "-ac", "1",
                    "-ar", str(int(out_sr)),
                    "-f",  "f32le",
                    tmp]
            r = _sp.run(cmd, capture_output=True, timeout=120)
            if r.returncode != 0:
                return None
            raw = open(tmp, "rb").read()
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

    def _xcorr_bounded(a_sig, b_sig, max_lag_samp):
        """
        Cross-correlate a_sig and b_sig.
        C[L] = sum_t  a_sig[t] * b_sig[t + L]

        Peaks at L means b_sig leads a_sig by L samples (positive = b ahead).
        Search restricted to |L| ≤ max_lag_samp to avoid false peaks.
        Returns (signed_lag_samples_float, confidence_0_to_1).
        Lag is a float thanks to parabolic sub-sample interpolation.
        """
        na, nb = len(a_sig), len(b_sig)
        n   = na + nb - 1
        N   = 1 << int(_np.ceil(_np.log2(max(n, 1))))
        C   = _np.fft.irfft(
                  _np.fft.rfft(b_sig, N) * _np.conj(_np.fft.rfft(a_sig, N)),
                  N)[:n]
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

    # ── Stage 1: 1 Hz RMS envelope, full probe window ─────────────────────
    # One RMS sample per second.  Handles offsets up to ±probe_duration/2 s.
    # Accuracy: ±0.5 s.
    SR1   = 1          # 1 Hz
    DUR1  = min(probe_duration, 600.0)
    WIN1  = 8000       # ffmpeg extract SR before computing 1-sample/s envelope

    raw_a1 = _extract(video_path, start_offset, DUR1, WIN1)
    raw_b1 = _extract(audio_path, start_offset, DUR1, WIN1)

    if raw_a1 is None or raw_b1 is None:
        return 0.0, 0.0

    env_a1 = _peak_env(raw_a1, WIN1, top_pct=15)   # 1 sample per second, loudest 15 %
    env_b1 = _peak_env(raw_b1, WIN1, top_pct=15)

    if env_a1 is None or env_b1 is None or len(env_a1) < 4 or len(env_b1) < 4:
        return 0.0, 0.0

    # audio first, video second — C[L] = sum_t AUDIO[t] * VIDEO[t+L]
    # Peak at L=+10 means VIDEO leads AUDIO by 10s (camera started 10s early) → T1=+10
    # Consistent with Stage 2 and Stage 3 argument order.
    lag1, conf1 = _xcorr_bounded(env_b1, env_a1, len(env_b1) // 2)
    # Both extractions started at start_offset; v_start = a_start, so:
    T1 = float(lag1) / SR1   # seconds; positive = camera started before DAW

    # ── Stage 2: 50 Hz RMS envelope, ±10 s search around T1 ──────────────
    # Accuracy: ±10 ms.
    SR2      = 8000
    WIN2     = SR2 // 50       # 20 ms RMS windows → 50 effective Hz
    PROBE2   = 60.0            # seconds to extract
    SEARCH2  = 20.0            # ±20 s search (wider catches Stage-1 errors up to 20 s)

    a_start2 = start_offset
    # Centre the video extraction on the Stage-1 estimate.
    # v_start2 - a_start2 encodes the expected offset so the lag is small.
    v_start2 = max(0.0, start_offset + T1)

    raw_a2 = _extract(audio_path, a_start2, PROBE2, SR2)
    raw_v2 = _extract(video_path,  v_start2, PROBE2, SR2)

    T2, conf2 = T1, conf1   # fallback
    if raw_a2 is not None and raw_v2 is not None:
        env_a2 = _peak_env(raw_a2, WIN2, top_pct=25)
        env_v2 = _peak_env(raw_v2, WIN2, top_pct=25)
        if env_a2 is not None and env_v2 is not None:
            sr2_eff  = SR2 // WIN2                       # 50 Hz effective
            max_lag2 = int(SEARCH2 * sr2_eff)
            lag2, conf2 = _xcorr_bounded(env_a2, env_v2, max_lag2)
            T2 = (v_start2 - a_start2) + float(lag2) / sr2_eff

    # ── Stage 3: 2 ms RMS envelope, ±0.5 s search around T2 ──────────────
    # Accuracy: ±1 ms (sub-frame).
    SR3      = 8000
    WIN3     = SR3 // 500      # 2 ms RMS windows → 500 effective Hz
    PROBE3   = 10.0            # seconds to extract
    SEARCH3  = 0.5             # ±0.5 s search

    # Anchor at 30 % into probe2 to use a different region than stage 2
    a_start3 = a_start2 + PROBE2 * 0.3
    v_start3 = max(0.0, a_start3 + T2)

    raw_a3 = _extract(audio_path, a_start3, PROBE3, SR3)
    raw_v3 = _extract(video_path,  v_start3, PROBE3, SR3)

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
    #    Stage 1 (1 Hz, full probe) and Stage 2 (50 Hz, ±20 s window) are
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

    t12_spread = abs(T2 - T1)
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
        final_T    = round(T3, 6)
        final_conf = round(min(1.0, base_conf * t12_mult), 4)
    elif conf2 >= 0.05:
        spread     = abs(T3 - T2) if stage3_ran else None
        final_T    = round(T2, 6)
        final_conf = round(min(1.0, conf2 * t12_mult), 4)
    else:
        spread     = None
        final_T    = round(T1, 4)
        final_conf = round(conf1, 4)

    # ── Write feedback log ─────────────────────────────────────────────────
    # .pb_cache/sync_runs.jsonl accumulates one entry per detection run.
    # At session start these can be read to understand algorithm behaviour.
    try:
        import json as _json
        import datetime as _dt
        _cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   ".pb_cache")
        os.makedirs(_cache_dir, exist_ok=True)
        _entry = {
            "ts":            _dt.datetime.now().isoformat(timespec="seconds"),
            "video":         os.path.basename(video_path),
            "audio":         os.path.basename(audio_path),
            "T1":            round(T1, 4),    "conf1": round(conf1, 4),
            "T2":            round(T2, 4),    "conf2": round(conf2, 4),
            "T3":            round(T3, 6) if stage3_ran else None,
            "conf3":         round(conf3, 4) if stage3_ran else None,
            "t12_spread_ms": round(t12_spread * 1000, 1),
            "t12_factor":    round(t12_factor, 3),
            "spread_ms":     round(spread * 1000, 1) if spread is not None else None,
            "stage3_ran":    stage3_ran,
            "final_T":       final_T,
            "final_conf":    final_conf,
        }
        with open(os.path.join(_cache_dir, "sync_runs.jsonl"),
                  "a", encoding="utf-8") as _fh:
            _fh.write(_json.dumps(_entry) + "\n")
    except Exception:
        pass

    return final_T, final_conf


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
    name = _re.sub(r'\.(wav|aif|aiff|mp3|m4v|mp4|mxf|mov|wmv|avi|mkv)$', '', name,
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