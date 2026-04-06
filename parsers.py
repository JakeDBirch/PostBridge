import os
import re
import subprocess
import tempfile
import json
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
                # Accept both [TOKEN] and bare TOKEN (no brackets)
                tm = re.match(r'^\[?([A-Z0-9_]+)\]?$', clean)
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
            r = subprocess.run(cmd, capture_output=True, timeout=120)
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

    def _xcorr_top_n(a_sig, b_sig, max_lag_samp, n_peaks=3, min_gap=5):
        """
        Like _xcorr_bounded but returns the top-N (lag, conf) pairs,
        each at least min_gap lag-samples apart.  Peaks are found by
        repeatedly zeroing out the neighbourhood around each winner and
        scanning again; confidence is always relative to the same global
        noise floor so values are comparable across calls.

        Used for multi-candidate Stage 1: instead of committing to the
        single strongest 1 Hz peak, we keep the top 3 and let Stage 2
        arbitrate by running a separate ±20 s search from each.
        """
        na, nb = len(a_sig), len(b_sig)
        n  = na + nb - 1
        N  = 1 << int(_np.ceil(_np.log2(max(n, 1))))
        C  = _np.fft.irfft(
                 _np.fft.rfft(b_sig, N) * _np.conj(_np.fft.rfft(a_sig, N)),
                 N)[:n]
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
        return 0.0, 0.0

    # ── Pass A: 50 Hz, first 60 s, ±30 s ─────────────────────────────────
    _WIN_A   = _SR1_EXTR // 50          # 160 samples = 20 ms
    _DUR_A   = int(60.0 * _SR1_EXTR)   # first 60 s in raw samples
    _raw_v_a = raw_vid1[:_DUR_A]
    _raw_a_a = raw_aud1[:_DUR_A]
    _env_v_a = _peak_env(_raw_v_a, _WIN_A, top_pct=20) if len(_raw_v_a) >= _WIN_A else None
    _env_a_a = _peak_env(_raw_a_a, _WIN_A, top_pct=20) if len(_raw_a_a) >= _WIN_A else None
    T1A, conf1A = 0.0, 0.0
    if _env_v_a is not None and _env_a_a is not None and len(_env_v_a) >= 4:
        _MAX_LAG_A  = int(30.0 * 50)              # ±30 s at 50 Hz = 1500 samples
        _MIN_GAP_A  = max(1, int(50 * 2))         # ≥2 s between candidates
        _cands_a    = _xcorr_top_n(_env_a_a, _env_v_a, _MAX_LAG_A,
                                    n_peaks=3, min_gap=_MIN_GAP_A)
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
        SEARCH2M  = 20.0
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
    # Global fallback: +54.5 ms systematic positive bias measured across
    # correctly-found files (deterministic, from peak-envelope window centroid
    # assignment).  Per-file calibration below overrides this for known files
    # using the accumulated corrections history.
    _T_CAL_GLOBAL = -0.046       # seconds  (≈ -46 ms)
    _T_CAL_S      = _T_CAL_GLOBAL

    # Per-file learned calibration: look up the last non-wrong correction for
    # this audio file in sync_corrections.jsonl.  The auto_T stored there had
    # _T_CAL_GLOBAL applied, so the per-file calibration is:
    #   _T_CAL_S = delta + _T_CAL_GLOBAL
    # where delta = accepted_T - auto_T (0.0 when the user accepted unchanged).
    # Group A files (delta≈0) get the same -46 ms as the global default.
    # Group B files (delta≈+46 ms) end up with cal≈0, which is correct since
    # the algorithm output for those files already lands on the true offset.
    # "wrong" verdicts are skipped so a mis-found file never corrupts the cache.
    try:
        _cal_cache = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  ".pb_cache")
        _corr_path = os.path.join(_cal_cache, "sync_corrections.jsonl")
        if os.path.exists(_corr_path):
            _audio_name = os.path.basename(audio_path)
            _last_good  = None
            with open(_corr_path, "r", encoding="utf-8") as _fc:
                for _line in _fc:
                    try:
                        _ce = json.loads(_line)
                        if (_ce.get("audio") == _audio_name and
                                _ce.get("verdict") not in ("wrong", "verified")):
                            _last_good = _ce
                    except Exception:
                        pass
            if _last_good is not None:
                _delta   = (0.0 if _last_good["verdict"] == "accepted"
                            else (_last_good.get("accepted_T", 0.0)
                                  - _last_good.get("auto_T",     0.0)))
                _T_CAL_S = _delta + _T_CAL_GLOBAL
    except Exception:
        pass

    final_T = round(final_T + _T_CAL_S, 6)

    # ── Write feedback log ─────────────────────────────────────────────────
    # .pb_cache/sync_runs.jsonl accumulates one entry per detection run.
    # At session start these can be read to understand algorithm behaviour.
    try:
        import datetime as _dt
        _cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   ".pb_cache")
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

    return final_T, final_conf


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
            cmd = ["ffmpeg", "-y", "-v", "quiet"]
            if t_start > 0.5:
                cmd += ["-ss", "{:.3f}".format(t_start)]
            cmd += ["-t", "{:.3f}".format(max(t_dur, 0.1)),
                    "-i", path, "-ac", "1", "-ar", str(int(out_sr)),
                    "-f", "f32le", tmp]
            r = subprocess.run(cmd, capture_output=True, timeout=60)
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
        r = subprocess.run(
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