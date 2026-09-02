"""Pre-flight: will reconcile actually REUSE my transcripts, or re-do the work?

Run this BEFORE Step 3 on a script whose media has already been transcribed
(Pull Quotes sessions and/or .pb_transcript.json sidecars).  It answers the
only question that matters at that point: for each token, which path will
reconcile take -- instant lookup, or minutes of Whisper?

Usage:
    python check_transcripts.py "F:\\path\\to\\script.txt" [media_dir ...]
    python check_transcripts.py "script.txt" --quick     # skip sha256 hashing

With no media_dir, scans the project root discovered from the script (the
folder holding 02_MEDIA + 03_AUDIO), else the script's own folder.

Nothing is written and no audio is decoded -- this is read-only.
"""
import os, sys, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engines
from parsers import parse_script, discover_pq_sessions
from utils import MEDIA_EXTS, is_video
from config import AUTO_FULL_TRANSCRIBE_THRESHOLD

SKIP_DIRS = {".pb_cache", ".git", "__pycache__", "waveform_cache"}


def pause(msg="\nPress Enter to close..."):
    try: input(msg)
    except EOFError: pass


def find_project_root(script_path, max_walk_up=6):
    cur = os.path.dirname(os.path.abspath(script_path))
    for _ in range(max_walk_up):
        if (os.path.isdir(os.path.join(cur, "02_MEDIA"))
                and os.path.isdir(os.path.join(cur, "03_AUDIO"))):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def scan_media(roots):
    found, seen = [], set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d not in SKIP_DIRS]
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() not in MEDIA_EXTS:
                    continue
                p = os.path.abspath(os.path.join(dirpath, fn))
                if p.lower() not in seen:
                    seen.add(p.lower())
                    found.append(p)
    return sorted(found)


def sidecar_status(media_path, quick=False):
    """Return (ok, detail) mirroring engines.pb_transcript_load's own rules."""
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


def main():
    argv  = sys.argv[1:]
    quick = "--quick" in argv
    args  = [a for a in argv if not a.startswith("--")]

    script = args[0] if args else None
    if not script:
        try:
            script = input("Path to your PostBridge script.txt: ").strip().strip('"')
        except EOFError:
            script = ""
    if not script or not os.path.isfile(script):
        print("Script file not found:", script or "(none)")
        pause(); return

    script = os.path.abspath(script)
    with open(script, encoding="utf-8", errors="replace") as f:
        tokens, parts, pulls, vo_blocks, doc_title, warnings = parse_script(f.read())

    pulls_per_token = {}
    for p in pulls:
        pulls_per_token[p["token"]] = pulls_per_token.get(p["token"], 0) + 1

    root        = find_project_root(script)
    media_roots = args[1:] or [root or os.path.dirname(script)]

    print("\nscript:       {}".format(script))
    print("project root: {}".format(
        root or "(not detected - PQ discovery falls back to the script folder)"))
    print("media scan:   {}".format(", ".join(media_roots)))
    print("tokens:       {}   pulls: {}   VO blocks: {}".format(
        len(tokens), len(pulls), len(vo_blocks)))
    for w in warnings[:5]:
        print("  ! {}".format(w))

    # -- Pull Quotes sessions ---------------------------------------------
    print("\n- PULL QUOTES SESSIONS -")
    pq = discover_pq_sessions(script) or {}
    for w in (pq.pop("__warnings__", []) or []):
        print("  ! {}".format(w))
    if not pq:
        print("  none found")
    pq_ok = set()
    for tok in sorted(pq):
        sp = pq[tok]
        try:
            with open(sp, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print("  [{}] UNREADABLE: {}".format(tok, e))
            continue
        nw    = len(data.get("transcript") or [])
        sigs  = data.get("audio_signatures") or {}
        media = [m for m in (data.get("media") or []) if m]
        if data.get("audio_cache"):
            media.append(data["audio_cache"])
        bad, missing = [], []
        for mp in media:
            if not os.path.isfile(mp):
                local = os.path.join(os.path.dirname(sp), os.path.basename(mp))
                if os.path.isfile(local):
                    mp = local              # reconcile relocates the same way
                else:
                    missing.append(os.path.basename(mp))
                    continue
            sig = sigs.get(mp) or next(
                (v for k, v in sigs.items()
                 if os.path.basename(k) == os.path.basename(mp)), None)
            if not sig:
                continue                    # legacy session - passes silently
            s = dict(sig)
            if quick:
                s.pop("sha256", None)
            ok, reason = engines.audio_signature_matches(s, mp)
            if not ok:
                bad.append("{}: {}".format(os.path.basename(mp), reason))
        if bad:
            print("  [{}] REJECTED - signature mismatch: {}".format(
                tok, " | ".join(bad)))
        else:
            pq_ok.add(tok)
            note = ("  (unresolved media: {})".format(", ".join(missing))
                    if missing else "")
            print("  [{}] OK - {} words{}".format(tok, nw, note))
        if tok not in pulls_per_token:
            print("       (no pulls in this script use this token)")

    # -- Sidecars ----------------------------------------------------------
    print("\n- .pb_transcript.json SIDECARS -")
    media_files = scan_media(media_roots)
    if not media_files:
        print("  no media found under the scan roots")
    hits = {}
    for mp in media_files:
        ok, detail = sidecar_status(mp, quick=quick)
        if ok:
            hits[mp] = detail
        print("  {}  {:<44}  {}".format(
            "HIT " if ok else "MISS", os.path.basename(mp)[:44], detail))

    # -- Per-token forecast ------------------------------------------------
    print("\n- FORECAST PER TOKEN -"
          "\n  (media matched to tokens by filename - verify against your real pool)")
    cost = 0
    for tok in sorted(pulls_per_token, key=lambda t: -pulls_per_token[t]):
        n       = pulls_per_token[tok]
        matched = [m for m in media_files
                   if tok.lower() in os.path.basename(m).lower()]
        audios  = [m for m in matched if not is_video(m)]
        covered = [m for m in audios if m in hits]

        if tok in pq_ok and len(audios) >= 2:
            verdict = ("POOL-MIX WINS - PQ session will be DISCARDED and the {} "
                       "pool files remixed + re-transcribed".format(len(audios)))
            cost += 1
        elif tok in pq_ok:
            verdict = "instant - Pull Quotes transcript"
        elif covered:
            verdict = "instant - sidecar on {}".format(os.path.basename(covered[0]))
        elif len(audios) >= 2:
            verdict = ("mix {} tracks, then transcribe the mix once (a mix that "
                       "hasn't been built yet can't have a sidecar)".format(len(audios)))
            cost += 1
        elif not audios:
            verdict = "no audio matched by filename - can't forecast"
        elif n >= AUTO_FULL_TRANSCRIBE_THRESHOLD:
            verdict = "FULL-AUDIO TRANSCRIBE ({} pulls, no cache)".format(n)
            cost += 1
        else:
            verdict = "{} per-pull Whisper calls".format(n)
            cost += 1
        print("  [{:<14}] {:>3} pull{}  |  {}".format(
            tok, n, "s" if n != 1 else " ", verdict))

    print("\n- SUMMARY -")
    print("  {} / {} tokens will hit an existing transcript.".format(
        len(pulls_per_token) - cost, len(pulls_per_token)))
    if cost:
        print("  {} token(s) will spend Whisper time - see the forecast above."
              .format(cost))
    else:
        print("  Nothing to re-transcribe.  Reconcile should be near-instant.")
    pause()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback; traceback.print_exc()
        pause("An error occurred. Press Enter to close...")
