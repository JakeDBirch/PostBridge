"""Shared transcript-coverage logic for the pre-flight checker.

One implementation, two front ends: `check_transcripts.py` (CLI) and the
CHECK TRANSCRIPTS button in Step 2.  Everything here mirrors the rules
reconcile itself applies at run time -- when those rules change, change
them here too or the forecast starts lying.

Read-only except for `adopt_transcript`, which is the one place that
writes: it re-stamps an existing transcript onto a media file so a
sidecar that reconcile was rejecting (or that never sat beside the
media at all) starts being reused.
"""
import os, json

import engines
from utils import is_video

# Suffixes we recognise as "a transcript for this media".
TRANSCRIPT_SUFFIXES = (".pb_transcript.json", ".pb_session.json")


# ── Status probes ──────────────────────────────────────────────────────────────

def sidecar_status(media_path, quick=False):
    """Return (ok, detail) for the .pb_transcript.json beside `media_path`.

    Mirrors engines.pb_transcript_load's acceptance rules exactly, but
    reports WHY a sidecar would be rejected instead of just returning
    None.  `quick` drops the sha256 leg of signature verification --
    size + duration only, which is instant on huge files.
    """
    p = engines.pb_transcript_path(media_path)
    if not os.path.isfile(p):
        return False, "no .pb_transcript.json sidecar"
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return False, "sidecar unreadable ({})".format(e)

    words = data.get("words")
    if not words:
        return False, "sidecar has no words"
    n     = len(words)
    model = data.get("model", "?")
    try:
        mtime = os.path.getmtime(media_path)
    except OSError:
        return False, "media unreadable"

    drift = abs(data.get("mtime", 0) - mtime)
    if drift <= 2:
        return True, "{} words (model {}) - mtime match".format(n, model)

    sig = dict(data.get("audio_signature") or {})
    if not sig:
        return False, ("mtime drifted {:.0f}s and sidecar has NO audio_signature"
                       " - will re-transcribe".format(drift))
    if quick:
        sig.pop("sha256", None)
    ok, reason = engines.audio_signature_matches(sig, media_path)
    if ok:
        return True, "{} words (model {}) - mtime drifted, signature verified{}".format(
            n, model, " (quick)" if quick else "")
    return False, "signature MISMATCH ({}) - will re-transcribe".format(reason)


def pq_session_status(session_path, quick=False):
    """Return (ok, n_words, detail) for a Pull Quotes session JSON.

    Applies the same audio-signature verification reconcile does at
    main.py's pq_session load, including the same basename relocation
    fallback, so a session this reports OK is one reconcile will accept.
    """
    try:
        with open(session_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return False, 0, "unreadable ({})".format(e)

    nw    = len(data.get("transcript") or [])
    sigs  = data.get("audio_signatures") or {}
    media = [m for m in (data.get("media") or []) if m]
    if data.get("audio_cache"):
        media.append(data["audio_cache"])

    bad, missing = [], []
    for mp in media:
        if not os.path.isfile(mp):
            local = os.path.join(os.path.dirname(session_path),
                                 os.path.basename(mp))
            if os.path.isfile(local):
                mp = local                  # reconcile relocates the same way
            else:
                missing.append(os.path.basename(mp))
                continue
        sig = sigs.get(mp) or next(
            (v for k, v in sigs.items()
             if os.path.basename(k) == os.path.basename(mp)), None)
        if not sig:
            continue                        # legacy session - passes silently
        s = dict(sig)
        if quick:
            s.pop("sha256", None)
        ok, reason = engines.audio_signature_matches(s, mp)
        if not ok:
            bad.append("{}: {}".format(os.path.basename(mp), reason))

    if bad:
        return False, nw, "signature mismatch - {}".format(" | ".join(bad))
    if missing:
        return True, nw, "{} words (unresolved media: {})".format(
            nw, ", ".join(missing))
    return True, nw, "{} words".format(nw)


def token_forecast(n_pulls, audio_paths, has_pq, covered, full_threshold):
    """Predict reconcile's path for one interview token.

    Returns (free, verdict).  `free` is True when no Whisper time will be
    spent.  `covered` is the subset of `audio_paths` with a usable
    sidecar; `has_pq` is whether a verified PQ session exists.
    """
    audios = [p for p in audio_paths if not is_video(p)]

    if has_pq and len(audios) >= 2:
        return False, ("POOL-MIX WINS - the PQ session will be DISCARDED and "
                       "these {} pool files remixed + re-transcribed".format(
                           len(audios)))
    if has_pq:
        return True, "instant - Pull Quotes transcript"
    if covered:
        return True, "instant - sidecar on {}".format(
            os.path.basename(covered[0]))
    if len(audios) >= 2:
        return False, ("mix {} tracks, then transcribe the mix once (a mix that "
                       "hasn't been built yet can't have a sidecar)".format(
                           len(audios)))
    if not audios:
        return True, "no audio assigned - nothing to transcribe"
    if n_pulls >= full_threshold:
        return False, "FULL-AUDIO TRANSCRIBE ({} pulls, no cache)".format(n_pulls)
    return False, "{} per-pull Whisper call{}".format(
        n_pulls, "s" if n_pulls != 1 else "")


# ── Adoption: point PostBridge at a transcript it isn't finding ────────────────

def load_transcript_words(source_path):
    """Read a .pb_transcript.json or .pb_session.json.

    Returns (words, blobs, error).  On success `error` is None.  Accepts
    either shape: sidecars store the word list under "words", Pull Quotes
    sessions under "transcript".
    """
    if not source_path or not os.path.isfile(source_path):
        return None, None, "file not found"
    try:
        with open(source_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return None, None, "unreadable ({})".format(e)

    if not isinstance(data, dict):
        return None, None, "not a PostBridge transcript file"
    words = data.get("words") or data.get("transcript")
    if not words:
        return None, None, "no word list inside (looked for 'words' / 'transcript')"
    if not isinstance(words, list) or not isinstance(words[0], dict):
        return None, None, "word list is not in PostBridge's format"
    if "start" not in words[0] or "end" not in words[0]:
        return None, None, "words carry no timings - unusable for reconcile"
    return words, data.get("blobs"), None


def adopt_warning(media_path, words):
    """Return a human-readable warning if `words` looks like it belongs to a
    DIFFERENT recording than `media_path`, else None.

    Adoption is a manual override, so the only guard available is a
    sanity check: a transcript whose last word lands well past the end of
    the audio (or stops far short of it) is almost certainly pointed at
    the wrong file, and adopting it would bake bad timecodes into every
    pull for that token.
    """
    try:
        dur = float(engines.get_media_duration(media_path) or 0.0)
    except Exception:
        dur = 0.0
    if dur <= 0:
        return None                         # can't probe - don't block the user

    try:
        last = max(float(w.get("end") or 0.0) for w in words)
    except Exception:
        return None

    if last > dur + 30.0:
        return ("The transcript runs to {:.0f}s but this audio is only {:.0f}s "
                "long.  That usually means the transcript belongs to a "
                "different (longer) recording.".format(last, dur))
    if dur > 120.0 and last < dur * 0.5:
        return ("The transcript stops at {:.0f}s but this audio runs {:.0f}s.  "
                "It may be a partial transcript, or belong to a different "
                "recording.".format(last, dur))
    return None


def adopt_transcript(media_path, words, blobs=None):
    """Write `words` as the sidecar for `media_path`.

    engines.pb_transcript_save stamps the media's CURRENT mtime and a
    freshly computed audio_signature, which is the whole point: whatever
    made reconcile reject the original -- a stale mtime, or the file
    simply living somewhere else -- is resolved by re-stamping it here.
    Returns (ok, message).
    """
    try:
        engines.pb_transcript_save(media_path, words, blobs=blobs)
    except Exception as e:
        return False, "could not write sidecar ({})".format(e)
    ok, detail = sidecar_status(media_path, quick=True)
    if not ok:
        return False, "sidecar written but still rejected: {}".format(detail)
    return True, "adopted {} words -> {}".format(
        len(words), os.path.basename(engines.pb_transcript_path(media_path)))


def find_candidates_in_folder(folder, media_paths):
    """Match transcript files in `folder` to `media_paths` by filename stem.

    Returns {media_path: transcript_path} for every media file that has a
    same-stem transcript in the folder.  Stem matching is what the user
    means by "the transcripts for these files" -- exact stem first, then
    a stem-prefix match so `INT_JORDAN.wav` still finds
    `INT_JORDAN_transcript.pb_transcript.json`.
    """
    if not folder or not os.path.isdir(folder):
        return {}

    pool = []
    for fn in os.listdir(folder):
        low = fn.lower()
        if not any(low.endswith(sfx) for sfx in TRANSCRIPT_SUFFIXES):
            continue
        stem = fn
        for sfx in TRANSCRIPT_SUFFIXES:
            if low.endswith(sfx):
                stem = fn[:-len(sfx)]
                break
        pool.append((stem.lower(), os.path.join(folder, fn)))

    out = {}
    for mp in media_paths:
        mstem = os.path.splitext(os.path.basename(mp))[0].lower()
        hit = next((p for s, p in pool if s == mstem), None)
        if hit is None:
            hit = next((p for s, p in pool
                        if s.startswith(mstem) or mstem.startswith(s)), None)
        if hit:
            out[mp] = hit
    return out
