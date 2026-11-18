"""The part titles the narrator reads.

"[PART Part 1: Evan]" names a section, and the narrator reads that name out at
the top of the part.  PostBridge knows that without the script saying it: the
parser makes the title the first line of narration in its part, the matcher
finds the reading, and an episode saved before this is renumbered on load so
its Step 4 work stays on the right lines.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import engines  # noqa: E402
import main  # noqa: E402
from parsers import parse_script  # noqa: E402
from script_check import WARN, run_checks  # noqa: E402

SCRIPT = """Ep 7

[PART Intro]

[VO PART_0_NARRATOR]
That's next.

[PART Part 1: Evan]

[SANDRA1 00:00:29-00:00:57]
“He was a very happy person.”

[VO PART_1_NARRATOR]
It is a mother's prerogative.

[PART Part 2 – The Tonto]

[VO PART_2_NARRATOR]
That's Arizona resident Joey Hergatt.

[PART Part 3: Read Already]

[VO PART_3_NARRATOR]
Part three. Read already.

More narration.

[PART Outro]

[VO PART_4_NARRATOR]
Thanks for listening.
"""


def _items(pulls, vos):
    return sorted(pulls + vos, key=lambda x: x["order"])


def test_a_part_title_is_the_first_narration_in_its_part():
    _t, parts, pulls, vos, _d, _w = parse_script(SCRIPT)
    titles = [v for v in vos if v.get("part_title")]
    assert [(v["text"], v["id"]) for v in titles] == [
        ("Part 1: Evan.", "PART_1_NARRATOR"),
        ("Part 2 – The Tonto.", "PART_2_NARRATOR")]
    items = _items(pulls, vos)
    assert [x["order"] for x in items] == list(range(1, len(items) + 1))
    # Part 1 opens with a pull: its title plays ahead of it.
    i = next(n for n, x in enumerate(items) if x.get("token") == "SANDRA1")
    assert items[i - 1] is titles[0]
    assert titles[0]["part_index"] == items[i]["part_index"]


def test_not_for_intro_outro_or_a_title_the_script_already_reads():
    _t, _p, _pulls, vos, _d, _w = parse_script(SCRIPT)
    texts = [v["text"] for v in vos if v.get("part_title")]
    assert not any("Intro" in t or "Outro" in t or "Read Already" in t
                   for t in texts)


def test_script_check_and_fix_format_leave_titles_to_postbridge():
    assert not [f for f in run_checks(SCRIPT)
                if f.severity == WARN and "title" in f.title.lower()]
    import script_normalize
    fixed, changes = script_normalize.normalize(SCRIPT, title="Ep 7")
    assert "Part 1. Evan." not in fixed and "Part 1: Evan.\n" not in fixed


# ── Finding the reading ──────────────────────────────────────────────────

def _words(text, t0=0.0):
    out = []
    for n, w in enumerate(text.split()):
        out.append({"word": " " + w, "start": t0 + n * 0.5, "end": t0 + n * 0.5 + 0.4})
    return out


def test_the_last_whole_reading_wins_and_a_dropped_figure_still_counts():
    # The slate says the part, then two reads of the title; the transcript
    # dropped the "1" from the second.
    w = _words("alright this is arizona part one part one evan part evan "
               "it is a mother's prerogative")
    m = engines.match_part_title("Part 1: Evan.", w)
    assert m["confidence"] == 1.0 and m["matched_text"] == "part evan"
    assert m["segments"] == [(4.5, 5.4)]


def test_a_title_is_the_words_in_a_row_not_scattered():
    w = _words("part two that's arizona resident joey and the tonto")
    m = engines.match_part_title("Part 2: The Tonto.", w)
    assert m["confidence"] == 0.5 and m["matched_text"] == "part two"


def test_spelling_slack_on_the_title_words():
    w = _words("part two the tanto that's arizona")
    assert engines.match_part_title("Part 2: The Tonto.", w)["confidence"] == 1.0


def test_reconcile_finds_a_title_in_its_parts_take():
    _t, _p, _pulls, vos, _d, _w = parse_script(SCRIPT)
    part2 = [v for v in vos if v["id"] == "PART_2_NARRATOR"]
    take = _words("all right this is arizona part two part two the tonto "
                  "that's arizona resident joey hergatt")
    res = engines.reconcile_vo_part(part2, [(take, 0.0, None, "p2.wav")])
    title = res[0]
    assert title["part_title"] and title["status"] == "ok"
    assert title["matched_text"] == "part two the tonto"
    assert title["segments"][0][0] == pytest.approx(3.5)


# ── An episode saved before titles were lines ────────────────────────────

class _App(object):
    _with_part_titles = main.App._with_part_titles
    _s4_reconcile_vo_row = main.App._s4_reconcile_vo_row
    _monolog = False

    def __init__(self, script):
        _t, _p, self.pulls, self.vo_blocks, _d, _w = parse_script(script)
        self.logged = []

    def _log_line(self, m, c=None):
        self.logged.append(m)


def _saved(app):
    """Results as an episode saved before titles were lines: every other
    line, numbered 1..n, each carrying a mark of its own."""
    others = [x for x in _items(app.pulls, app.vo_blocks) if not x.get("part_title")]
    out = []
    for n, x in enumerate(others, 1):
        r = {"order": n, "mine": n, "segments": [[1.0, 2.0]], "_s4_accepted": True}
        if "id" in x:
            r.update(is_vo=True, vo_id=x["id"], part_index=x["part_index"],
                     token="VO_PART_{:02d}".format(x["part_index"]))
        else:
            r.update(token=x["token"], part_index=x["part_index"])
        out.append(r)
    return out


def test_saved_work_is_renumbered_onto_its_own_lines(monkeypatch):
    app = _App(SCRIPT)
    monkeypatch.setattr(engines, "pb_transcript_load", lambda p: (None, None))
    monkeypatch.setattr(engines, "cache_load", lambda p: None)
    saved = _saved(app)
    out = app._with_part_titles(saved)
    items = {x["order"]: x for x in app.pulls + app.vo_blocks}
    assert len(out) == len(saved) + 2
    for r in out:
        x = items[r["order"]]
        assert r.get("vo_id", r.get("token")) == x.get("id", x.get("token"))
        if not r.get("part_title"):
            assert r["_s4_accepted"]
    # Every saved line kept what was saved with it.
    assert sorted(r["mine"] for r in out if "mine" in r) == list(range(1, len(saved) + 1))
    assert [r["status"] for r in out if r.get("part_title")] == ["no_match"] * 2


def test_the_titles_are_matched_in_the_parts_take(monkeypatch):
    app = _App(SCRIPT)
    take = _words("this is part one part one evan it is a mother's prerogative")
    monkeypatch.setattr(engines, "pb_transcript_load",
                        lambda p: (take, None) if p == "p1.wav" else (None, None))
    monkeypatch.setattr(engines, "cache_load", lambda p: None)
    saved = _saved(app)
    for r in saved:
        if r.get("vo_id") == "PART_1_NARRATOR":
            r["source_audio"] = "p1.wav"
    out = app._with_part_titles(saved)
    t1 = next(r for r in out if r.get("part_title") and r["vo_id"] == "PART_1_NARRATOR")
    assert t1["status"] == "ok" and t1["matched_text"] == "part one evan"


def test_saved_work_that_does_not_line_up_is_left_alone(monkeypatch):
    app = _App(SCRIPT)
    saved = _saved(app)
    saved[0]["token"] = "SOMEONE_ELSE"
    saved[0]["vo_id"] = "NOPE"
    assert app._with_part_titles(saved) is saved
    assert any("left as saved" in m for m in app.logged)


def test_results_that_already_have_titles_are_left_alone():
    app = _App(SCRIPT)
    saved = _saved(app) + [{"order": 99, "part_title": True}]
    assert app._with_part_titles(saved) is saved
