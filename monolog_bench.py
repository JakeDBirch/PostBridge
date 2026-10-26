"""Score monolog.reconcile against a cut an editor already made.

    python monolog_bench.py --script EP.txt --edit EP_aaf.xml --vo "pt 1.WAV" "pt 2.WAV"

--edit is the AAF -> XML export of the finished Pro Tools session (any FCP7
XML whose audio clips reference the raw VO files): every clip on those
files is a stretch the editor kept.  The raw reads are transcribed the way
Script -> Session transcribes VO (chunks at silences) into --work, once.

Prints how much of the editor's cut PostBridge keeps (recall) and how much
of PostBridge's is in it (precision), by word and by time, how far its edit
points sit from the editor's, and the biggest disagreements as text, with
-v all of them.  Ep 6 of Gunfighters (Oct 2026): words 98.6% / 99.3%,
edit points within 0.04 s at the median.
"""
import argparse
import json
import os
import statistics
import sys
import time
import xml.etree.ElementTree as ET

import engines
import monolog
import parsers


def _keep_regions(xml_path, vo_paths):
    """{vo path: merged [start, end] source regions} the edit uses."""
    names = {os.path.basename(p).lower(): p for p in vo_paths}
    root = ET.parse(xml_path).getroot()
    tb = root.find(".//sequence/rate/timebase")
    ntsc = root.find(".//sequence/rate/ntsc")
    fps = float(tb.text) if tb is not None else 24.0
    if ntsc is not None and ntsc.text.upper() == "TRUE":
        fps = fps * 1000.0 / 1001.0
    segs = {p: [] for p in vo_paths}
    for clip in root.iter("clipitem"):
        p = names.get((clip.findtext("name") or "").lower())
        if p is None or clip.findtext("in") is None:
            continue
        segs[p].append((int(clip.findtext("in")) / fps, int(clip.findtext("out")) / fps))
    out = {}
    for p, ss in segs.items():
        merged = []
        for a, b in sorted(ss):
            if merged and a <= merged[-1][1] + 0.05:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        out[p] = merged
    return out


def _transcript(path, work):
    cache = os.path.join(work, os.path.basename(path) + ".words.json")
    if os.path.isfile(cache):
        with open(cache, encoding="utf-8") as f:
            return json.load(f)
    t0 = time.time()
    print("transcribing", os.path.basename(path), "...", flush=True)
    words = engines.transcribe_in_chunks(path, engines.detect_silence_splits(path))
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(words, f)
    print("  {} words in {:.0f}s".format(len(words), time.time() - t0))
    return words


def _inside(t, regions):
    return any(a <= t <= b for a, b in regions)


def _overlap(A, B):
    return sum(max(0.0, min(b, d) - max(a, c)) for a, b in A for c, d in B)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--script", required=True)
    ap.add_argument("--edit", required=True)
    ap.add_argument("--vo", nargs="+", required=True, help="raw reads, in recording order")
    ap.add_argument("--work", default=os.path.join(os.path.expanduser("~"), ".pb_monolog_bench"))
    ap.add_argument("-v", action="store_true", help="list every disagreement")
    a = ap.parse_args()
    os.makedirs(a.work, exist_ok=True)

    with open(a.script, encoding="utf-8", errors="replace") as f:
        blocks = parsers.parse_plain_script(f.read())[3]
    takes = [(_transcript(p, a.work), 0.0, None, p, None) for p in a.vo]
    truth = _keep_regions(a.edit, a.vo)
    t0 = time.time()
    results = monolog.reconcile(blocks, takes)
    print("reconcile {:.1f}s: {} pulls, {} flagged".format(
        time.time() - t0, len(results),
        sum(1 for r in results if r["status"] != "ok")))

    tw = {"tp": 0, "fp": 0, "fn": 0}
    t_tp = t_pred = t_true = 0.0
    diffs, d_in, d_out = [], [], []
    for (words, _o, _v, path, _b) in takes:
        P = sorted(s for r in results if r.get("source_audio") == path
                   for s in r.get("segments") or [])
        K = truth[path]
        t_tp += _overlap(P, K)
        t_pred += sum(b - a for a, b in P)
        t_true += sum(b - a for a, b in K)
        run = None
        for w in words:
            mid = (w["start"] + w["end"]) / 2.0
            k, p = _inside(mid, K), _inside(mid, P)
            if k and p:
                tw["tp"] += 1
            elif p:
                tw["fp"] += 1
            elif k:
                tw["fn"] += 1
            kind = None if k == p else ("EXTRA" if p else "MISSING")
            if run and run[0] == kind and w["start"] - run[3] < 3:
                run[4].append(w["word"]); run[3] = w["end"]
            else:
                if run and run[0]:
                    diffs.append(run)
                run = [kind, os.path.basename(path), w["start"], w["end"], [w["word"]]]
        if run and run[0]:
            diffs.append(run)
        merged = []
        for s, e in P:
            if merged and s <= merged[-1][1] + 0.01:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        for idx, acc in ((0, d_in), (1, d_out)):
            for reg in K:
                near = min(merged, key=lambda m: abs(m[idx] - reg[idx]), default=None)
                if near and abs(near[idx] - reg[idx]) < 1.5:
                    acc.append(near[idx] - reg[idx])

    print("time:  kept {:.1f} min, editor kept {:.1f} min, both {:.1f} min "
          "-> precision {:.3f}  recall {:.3f}".format(
              t_pred / 60, t_true / 60, t_tp / 60, t_tp / max(t_pred, 1e-9),
              t_tp / max(t_true, 1e-9)))
    print("words: {}  precision {:.3f}  recall {:.3f}".format(
        tw, tw["tp"] / max(1, tw["tp"] + tw["fp"]), tw["tp"] / max(1, tw["tp"] + tw["fn"])))
    if d_in and d_out:
        print("edit points vs the editor's (PostBridge - editor): "
              "in median {:+.2f}s, out median {:+.2f}s".format(
                  statistics.median(d_in), statistics.median(d_out)))
    shown = sorted(diffs, key=lambda d: (d[1], d[2])) if a.v else \
        sorted(diffs, key=lambda d: -len(d[4]))[:25]
    for kind, name, start, _end, ws in shown:
        if a.v or len(ws) >= 3:
            print("  {:<7} {:<16} {:8.1f}  {}".format(kind, name[:16], start, " ".join(ws)[:140]))


if __name__ == "__main__":
    sys.exit(main())
