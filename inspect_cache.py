"""Print what's in the PostBridge cache next to your script.

Usage:
    python inspect_cache.py "F:\\path\\to\\your\\script.txt"
    (or double-click and paste the script path when prompted)

Tells you:
  • whether the .pb_cache directory exists next to the script
  • how many cached mixes (mix_*.wav) exist and their sizes
  • how many transcript entries (both .pb_transcript.json sidecars
    next to source files AND md5-keyed entries in .pb_cache/)
  • how many pull-reconciliation caches, bucketed by result.token
That's enough to diagnose whether the content-addressed mix cache
is actually being written and reused."""
import os, sys, json, glob


def pause(msg="\nPress Enter to close…"):
    try: input(msg)
    except EOFError: pass


def main():
    script = sys.argv[1] if len(sys.argv) > 1 else None
    if not script:
        try:
            script = input("Path to your PostBridge script.txt: ").strip().strip('"')
        except EOFError:
            script = ""
    if not script or not os.path.isfile(script):
        print("Script file not found:", script or "(none)")
        pause(); return
    cache_dir = os.path.join(os.path.dirname(os.path.abspath(script)), ".pb_cache")
    print(f"\nscript:    {script}")
    print(f"cache dir: {cache_dir}")
    print(f"exists:    {os.path.isdir(cache_dir)}")
    if not os.path.isdir(cache_dir):
        print("\n(no cache directory — first reconcile after opening this script "
              "will create it and build all caches)")
        pause(); return

    files = os.listdir(cache_dir)
    mixes = sorted(f for f in files if f.startswith("mix_") and f.endswith(".wav"))
    transcripts = sorted(f for f in files
                         if f.endswith(".json") and not f.startswith("pull_")
                         and not f.startswith(os.path.basename(script)[:20]))
    pull_caches = sorted(f for f in files if f.startswith("pull_") and f.endswith(".json"))

    print(f"\ncached mixes ({len(mixes)}):")
    for f in mixes:
        p = os.path.join(cache_dir, f)
        try: sz_mb = os.path.getsize(p) / 1_000_000
        except OSError: sz_mb = 0
        print(f"  {f:32}  {sz_mb:6.1f} MB")

    print(f"\ncached transcripts in .pb_cache/ ({len(transcripts)}):")
    for f in transcripts[:20]:
        p = os.path.join(cache_dir, f)
        try:
            d = json.load(open(p, encoding="utf-8"))
            src = os.path.basename(d.get("path", "?"))
            nw  = len(d.get("words", []))
            print(f"  {f:24}  {nw:>5} words   src: {src}")
        except Exception:
            print(f"  {f}   (parse err)")
    if len(transcripts) > 20:
        print(f"  … and {len(transcripts) - 20} more")

    # Sidecars next to source media (walk the media folders)
    print(f"\npull-result caches by token ({len(pull_caches)} total):")
    from collections import Counter
    tokens = Counter()
    for f in pull_caches:
        try:
            d = json.load(open(os.path.join(cache_dir, f), encoding="utf-8"))
            tokens[d.get("result", {}).get("token", "?")] += 1
        except Exception: pass
    for tok, n in sorted(tokens.items(), key=lambda x: -x[1]):
        print(f"  {tok:16}  {n:>4}")

    print(f"\n─ SUMMARY ─")
    print(f"  {len(mixes)} cached mixes, {len(transcripts)} transcripts, "
          f"{len(pull_caches)} pull results ({len(tokens)} tokens)")
    if len(mixes) == 0:
        print("  ⚠ No mix files cached — either you haven't run a reconcile since "
              "installing the content-addressed mix cache, or something is "
              "invalidating them.  Check the reconcile log for 'mix cached' vs "
              "'mixed N tracks' lines.")
    pause()


if __name__ == "__main__":
    try: main()
    except Exception:
        import traceback; traceback.print_exc()
        pause("An error occurred. Press Enter to close…")
