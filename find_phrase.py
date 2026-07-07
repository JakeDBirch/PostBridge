"""Search across every .pb_transcript.json sidecar under a folder for a phrase.

Use when a script's @PULL points to the wrong token / audio file and you
need to find where the quoted text actually appears in your other
transcripts.

Usage:
    python find_phrase.py "quote text goes here" [--root FOLDER] [--fuzzy]

The phrase is matched as a WORD SEQUENCE (spaces and punctuation
normalised the same way the reconcile does), so contractions,
punctuation, and casing don't have to match exactly.  --fuzzy switches
to an approximate match: reports any transcript whose ratio of
phrase-word hits within a sliding window ≥ 0.7 (useful when you're
paraphrasing what you remember hearing).

Output columns:
    IN-OUT (HH:MM:SS)    audio-file            first 60 chars of context
"""
import argparse, json, os, re, sys, time

def normalise(s):
    s = s.replace("’", "'").replace("‘", "'")
    s = re.sub(r"[^A-Za-z0-9']+", " ", s).lower().strip()
    return s.split()

def _tc(secs):
    h, r = divmod(float(secs), 3600)
    m, s = divmod(r, 60)
    return "{:02d}:{:02d}:{:02d}".format(int(h), int(m), int(s))

def search_transcript(words, phrase, fuzzy=False, ratio=0.7):
    """words: list of {"text","start","end", ...} dicts.
       Returns list of (in_s, out_s, context) hits."""
    norm_words = [normalise((w.get("text") or w.get("word") or ""))[0] if normalise((w.get("text") or w.get("word") or "")) else "" for w in words]
    phrase_words = normalise(phrase)
    if not phrase_words: return []
    n = len(phrase_words)
    hits = []
    if not fuzzy:
        for i in range(len(norm_words) - n + 1):
            if norm_words[i:i+n] == phrase_words:
                hits.append(i)
    else:
        pset = set(phrase_words)
        seen_hit = -10**9
        for i in range(len(norm_words) - n + 1):
            window = norm_words[i:i+n]
            common = sum(1 for w in window if w in pset)
            if common / n >= ratio and i - seen_hit >= n:
                hits.append(i); seen_hit = i
    out = []
    for i in hits:
        j = min(i + n - 1, len(words) - 1)
        in_s  = float(words[i].get("start", 0))
        out_s = float(words[j].get("end",   in_s))
        ctx_words = words[max(0, i-3):min(len(words), i + n + 3)]
        ctx = " ".join((w.get("text") or w.get("word") or "") for w in ctx_words)
        out.append((in_s, out_s, ctx))
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phrase", help="the quote text to hunt for")
    ap.add_argument("--root", default=r"F:\Blood Trails",
                    help="folder to scan recursively for .pb_transcript.json (default: F:\\Blood Trails)")
    ap.add_argument("--fuzzy", action="store_true",
                    help="approximate match (word overlap ratio >= 0.7)")
    args = ap.parse_args()

    if not os.path.isdir(args.root):
        print("root does not exist:", args.root); sys.exit(2)

    t0 = time.time()
    scanned, matched, total_hits = 0, 0, 0
    print("Searching {!r} for: {!r}".format(args.root, args.phrase))
    if args.fuzzy: print("(fuzzy mode — reporting approximate matches too)")
    print("=" * 88)

    for dirpath, dirs, files in os.walk(args.root):
        dirs[:] = [d for d in dirs if not d.startswith(".") or d == ".pb_cache"]
        for fn in files:
            if not fn.endswith(".pb_transcript.json"):
                continue
            p = os.path.join(dirpath, fn)
            scanned += 1
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                print("  [skip] {}: {}".format(fn, e)); continue
            words = data.get("words") or []
            if not words: continue
            hits = search_transcript(words, args.phrase, fuzzy=args.fuzzy)
            if not hits: continue
            matched += 1
            total_hits += len(hits)
            # Show the SOURCE audio file (strip the .pb_transcript.json)
            src = p[:-len(".pb_transcript.json")] + ".*"
            print("\n  {}  ({} hit{})".format(src, len(hits),
                                              "s" if len(hits) != 1 else ""))
            for (in_s, out_s, ctx) in hits:
                print("    [{}  -  {}]  {}{}".format(
                    _tc(in_s), _tc(out_s), ctx[:80],
                    "…" if len(ctx) > 80 else ""))

    print("\n" + "=" * 88)
    print("scanned {} sidecar(s), matched {}, {} total hit(s) in {:.2f}s".format(
        scanned, matched, total_hits, time.time() - t0))
    if matched == 0:
        print("no matches — try --fuzzy for approximate matching, "
              "or shorten the phrase")

if __name__ == "__main__":
    main()
