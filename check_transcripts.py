"""Pre-flight: will reconcile actually REUSE my transcripts, or re-do the work?

The command-line front end for `transcript_check`.  Inside the app the
same report is a button -- Step 2 -> CHECK TRANSCRIPTS -- and that
version is more accurate because it reads your real pool assignments
instead of guessing them from filenames.  Use this one when you want the
answer without opening PostBridge.

Usage:
    python check_transcripts.py "C:/path/to/script.txt" [media_dir ...]
    python check_transcripts.py "script.txt" --quick     # skip sha256 hashing

With no media_dir, scans the project root discovered from the script (the
folder holding 02_MEDIA + 03_AUDIO), else the script's own folder.

Nothing is written and no audio is decoded -- this is read-only.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engines
from parsers import parse_script, discover_pq_sessions
from utils import MEDIA_EXTS, is_video
from config import AUTO_FULL_TRANSCRIBE_THRESHOLD
from transcript_check import sidecar_status, pq_session_status, token_forecast

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
        ok, nw, detail = pq_session_status(pq[tok], quick=quick)
        if ok:
            pq_ok.add(tok)
            print("  [{}] OK - {}".format(tok, detail))
        else:
            print("  [{}] REJECTED - {}".format(tok, detail))
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
          "\n  (media matched to tokens by filename - the in-app CHECK "
          "TRANSCRIPTS button uses your real pool assignments instead)")
    cost = 0
    for tok in sorted(pulls_per_token, key=lambda t: -pulls_per_token[t]):
        n       = pulls_per_token[tok]
        matched = [m for m in media_files
                   if tok.lower() in os.path.basename(m).lower()]
        audios  = [m for m in matched if not is_video(m)]
        if not audios:
            verdict = "no audio matched by filename - can't forecast"
            free    = True
        else:
            covered   = [m for m in audios if m in hits]
            pooled_ok = (len(audios) > 1 and len(covered) == len(audios)
                         and engines.pooled_transcript_load(audios)[0] is not None)
            free, verdict = token_forecast(
                n, audios, tok in pq_ok, covered,
                AUTO_FULL_TRANSCRIBE_THRESHOLD, pooled_ok=pooled_ok)
        if not free:
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
