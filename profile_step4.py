"""Measure how heavy YOUR Step 4 review list actually is.

Scroll feel in Step 4 is dominated by how many widgets Tk has to manage,
which is (cards x ~16) — so the number that matters is the card count of
the episode you are actually working on, not a synthetic one.  Point this
at a saved session and it builds the real Step 4 offscreen, scrolls it,
and reports milliseconds per scroll step.

Usage:
    python profile_step4.py "F:\\path\\to\\your_session.json"

Read-only: it never writes to the session, and touches no media.

Reading the result:
    under 10 ms   smooth, ~100 fps
    10-20 ms      fine
    20-40 ms      starting to drag on fast scrolls
    over 40 ms    visibly heavy
"""
import os, sys, json, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def pause(msg="\nPress Enter to close..."):
    try: input(msg)
    except EOFError: pass


def load_results(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    results = data.get("results") or []
    if not results:
        return None, None, "no 'results' in this file — is it a saved session?"
    # Part names aren't always carried in the session; the dividers only
    # need something to label themselves with.
    idxs = sorted({r.get("part_index", 0) for r in results})
    parts = [{"index": i, "name": "Part {}".format(i)} for i in idxs]
    return results, parts, None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = args[0] if args else None
    if not path:
        try:
            path = input("Path to a saved PostBridge session .json: ").strip().strip('"')
        except EOFError:
            path = ""
    if not path or not os.path.isfile(path):
        print("Session file not found:", path or "(none)")
        pause(); return

    results, parts, err = load_results(path)
    if err:
        print(err); pause(); return

    import tkinter as tk
    import main as M

    app = M.App()
    app.results  = results
    app.parts    = parts
    app.tokens   = sorted({r.get("token", "") for r in results if r.get("token")})
    app.pulls    = [{"token": r.get("token", ""), "order": r.get("order", 0)}
                    for r in results]
    app.workflow = "script_session"
    app._script_path = path

    def count_widgets(w):
        n = 1
        for c in w.winfo_children():
            n += count_widgets(c)
        return n

    def bench():
        cv = app._s4_scroll_canvas
        app.update_idletasks()
        sf = None
        for child in cv.winfo_children():
            sf = child
            break
        n_widgets = count_widgets(sf) if sf is not None else 0
        vw, vh = cv.winfo_width(), cv.winfo_height()
        sr = cv.cget("scrollregion").split()
        total = float(sr[3]) if len(sr) == 4 else 0

        print("\n  session : {}".format(os.path.basename(path)))
        print("  cards   : {}   parts: {}".format(len(app._rv), len(parts)))
        print("  widgets : {} in the review list".format(n_widgets))
        print("  viewport: {} x {} px   content: {:.0f} px".format(vw, vh, total))

        print("\n  scroll cost")
        for units, label in ((1, "one wheel notch"),
                             (3, "three notches"),
                             (8, "a fast flick (coalesced)")):
            cv.yview_moveto(0.0); app.update_idletasks()
            y0 = cv.canvasy(0)
            t0 = time.perf_counter(); n = 25
            for _ in range(n):
                cv.yview_scroll(units, "units")
                app.update_idletasks()
            ms = (time.perf_counter() - t0) * 1000 / n
            cv.yview_moveto(0.0); app.update_idletasks()
            y0 = cv.canvasy(0); cv.yview_scroll(units, "units")
            app.update_idletasks()
            px = cv.canvasy(0) - y0
            print("    {:<26} {:>6.1f} ms/step   ({:.0f}px per step)".format(
                label, ms, px))

        # Same list with most of it folded away.  Cost tracks how many cards
        # are LAID OUT, so this is the number that matters when the one above
        # looks heavy -- on a synthetic 240-card list, folding 8 of 10 parts
        # took a scroll step from 51.1ms to 11.1ms.
        try:
            here = sorted({r.get("part_index", 0) for r in results})
            if len(here) > 2:
                for _pi in here[:-2]:
                    app._s4_collapsed.add(_pi)
                app._s4_apply_filter()
                app.update_idletasks()
                cv.yview_moveto(0.0); app.update_idletasks()
                t0 = time.perf_counter()
                for _ in range(25):
                    cv.yview_scroll(3, "units")
                    app.update_idletasks()
                ms = (time.perf_counter() - t0) * 1000 / 25
                print("    {:<26} {:>6.1f} ms/step   (all but the last 2 "
                      "parts collapsed)".format("three notches", ms))
        except Exception:
            pass

        print("\n  Under 10ms is smooth; over 40ms will feel heavy.")
        print("  Cost tracks how many cards are LAID OUT, so collapsing the")
        print("  parts you have finished is the lever -- compare the last two")
        print("  numbers above.")
        app.destroy()

    app.after(600, lambda: (app._step4(), app.after(3000, bench)))
    app.mainloop()
    pause()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback; traceback.print_exc()
        pause("An error occurred. Press Enter to close...")
