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
import os, json, re

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


def token_forecast(n_pulls, audio_paths, has_pq, covered, full_threshold,
                   pooled_ok=False):
    """Predict reconcile's path for one interview token.

    Returns (free, verdict); `free` is True when no Whisper time is spent.
    `covered` is the subset of `audio_paths` with a usable sidecar,
    `pooled_ok` whether those sidecars all carry the same transcript (so
    a mix can reuse them), and `has_pq` whether a verified Pull Quotes
    session exists for the token.

    Branch order follows reconcile's, which turns on how many audio files
    the token has: with two or more it MIXES them and transcribes the mix,
    so what matters is whether the mix can inherit a transcript -- not
    whether any individual track happens to have one.
    """
    audios = [p for p in audio_paths if not is_video(p)]

    if not audios:
        return True, "no audio assigned - nothing to transcribe"

    if len(audios) >= 2:
        # Pool-mix-wins: a PQ session is discarded here as possibly stale,
        # so it cannot save this token.  What can is every track carrying
        # the same mirrored transcript, which pooled_transcript_load hands
        # to the mix.
        if pooled_ok and len(covered) == len(audios):
            return True, ("instant - the same transcript is mirrored beside "
                          "all {} sources{}".format(
                              len(audios),
                              " (the PQ session is bypassed, but this covers "
                              "it)" if has_pq else ""))
        pq_note = ("the PQ session will be DISCARDED as possibly stale, and "
                   if has_pq else "")
        if covered:
            return False, ("{}only {} of {} tracks carry a sidecar - they must "
                           "ALL agree for the mix to reuse them".format(
                               pq_note, len(covered), len(audios)))
        return False, ("{}these {} tracks get mixed and the mix transcribed "
                       "once".format(pq_note, len(audios)))

    if has_pq:
        return True, "instant - Pull Quotes transcript"
    if covered:
        return True, "instant - sidecar on {}".format(
            os.path.basename(covered[0]))
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

    # Filenames are not reliable: the same recording routinely arrives as a
    # re-encode under a different name (Riverside's "Alex.mp3" exported as
    # "riverside_alex_raw-audio_….wav"), and no stem rule bridges that.  For
    # whatever is still unmatched, pair on CONTENT instead — a transcript
    # belongs to the audio whose duration its last word lands against.
    unmatched = [mp for mp in media_paths if mp not in out]
    if unmatched:
        out.update(_pair_by_duration(pool, unmatched, taken=set(out.values())))
    return out


def transcript_end(path):
    """Last word end-time in a transcript file, or None if unreadable."""
    words, _blobs, err = load_transcript_words(path)
    if err or not words:
        return None
    try:
        return max(float(w.get("end") or 0.0) for w in words)
    except (TypeError, ValueError):
        return None


# A transcript's last word lands at or just before the end of its audio.
_DUR_TOL   = 2.0    # how close "lands against the end" has to be
_DUR_CLEAR = 10.0   # how much worse the runner-up must be to call it unambiguous


def _pair_by_duration(pool, media_paths, taken=()):
    """Pair transcripts to media by where the transcript ends vs how long the
    audio runs.  Deliberately refuses to guess: a transcript is only claimed
    when it fits one file closely AND fits every other candidate far worse, so
    two similar-length interviews are left for the user to pair by hand rather
    than silently crossed over."""
    ends = {}
    for _stem, p in pool:
        if p in taken:
            continue
        e = transcript_end(p)
        if e is not None:
            ends[p] = e
    if not ends:
        return {}

    out, used = {}, set(taken)
    for mp in media_paths:
        try:
            dur = float(engines.get_media_duration(mp) or 0.0)
        except Exception:
            dur = 0.0
        if dur <= 0:
            continue
        scored = []
        for p, e in ends.items():
            if p in used:
                continue
            # Same sanity envelope adopt_warning uses, so anything this pairs
            # would not then be challenged on adoption.
            if e > dur + 30.0:
                continue
            if dur > 120.0 and e < dur * 0.5:
                continue
            scored.append((abs(dur - e), p))
        if not scored:
            continue
        scored.sort()
        close = [(d, p) for d, p in scored if d <= _DUR_TOL]

        if not close:
            continue
        if len(close) == 1:
            # Unambiguous on duration alone.
            runner_up = scored[1][0] if len(scored) > 1 else None
            if runner_up is None or runner_up >= close[0][0] + _DUR_CLEAR:
                out[mp] = close[0][1]
                used.add(close[0][1])
            continue

        # Several transcripts fit the duration — the normal case for a
        # Riverside session, where the guest track and the "Jordan (Guest)"
        # track are the same length.  Break the tie on the names: a stem whose
        # words all appear in the media's name is a better claim than one that
        # only half matches.
        best = _best_by_name(mp, [p for _d, p in close])
        if best is not None:
            out[mp] = best
            used.add(best)
    return out


def _name_tokens(path):
    stem = os.path.basename(path)
    for sfx in TRANSCRIPT_SUFFIXES:
        if stem.lower().endswith(sfx):
            stem = stem[:-len(sfx)]
            break
    else:
        stem = os.path.splitext(stem)[0]
    return set(t for t in re.split(r"[^a-z0-9]+", stem.lower()) if t)


def _best_by_name(media_path, candidates):
    """Pick the candidate whose name is most specifically about this media.

    Scored as the share of the candidate's own words that appear in the media
    filename, so "Eddleman" (1/1) beats "Jordan (Eddleman)" (1/2) for the
    guest track, while the interviewer's file still prefers the latter.
    Returns None when nothing wins outright.
    """
    mtok = _name_tokens(media_path)
    if not mtok:
        return None
    ranked = []
    for p in candidates:
        ctok = _name_tokens(p)
        if not ctok:
            continue
        hits = len(ctok & mtok)
        if not hits:
            continue
        ranked.append((hits / float(len(ctok)), hits, p))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    top = [r for r in ranked if r[:2] == ranked[0][:2]]
    if len(top) == 1:
        return top[0][2]

    # Equal names usually means the SAME transcript in two forms — a session
    # and its exported sidecar, or a "… (1)" re-transcribe.  When the contents
    # match there is nothing to choose between them, so pick deterministically
    # (session first: it carries the token) rather than bailing out.
    sigs = {p: _transcript_sig(p) for _r, _h, p in top}
    first = sigs[top[0][2]]
    if first is not None and all(s == first for s in sigs.values()):
        return sorted((p for _r, _h, p in top),
                      key=lambda p: (not p.lower().endswith(".pb_session.json"),
                                     len(os.path.basename(p)),
                                     os.path.basename(p).lower()))[0]
    return None                          # genuinely different — user chooses


def _transcript_sig(path):
    """(last word end, word count) — enough to tell two files apart."""
    words, _blobs, err = load_transcript_words(path)
    if err or not words:
        return None
    try:
        return (round(max(float(w.get("end") or 0.0) for w in words), 2), len(words))
    except (TypeError, ValueError):
        return None


def remove_transcript(media_path):
    """Delete the sidecar beside `media_path`, undoing an adoption.

    Adoption is a manual override and manual overrides go wrong, so there has
    to be a way back.  Only the sidecar is removed — the media and whatever
    file the transcript was adopted from are untouched.
    Returns (ok, message).
    """
    p = engines.pb_transcript_path(media_path)
    if not p or not os.path.isfile(p):
        return False, "no transcript is assigned to this file"
    try:
        os.remove(p)
    except OSError as e:
        return False, "could not remove sidecar ({})".format(e)
    return True, "removed {}".format(os.path.basename(p))
