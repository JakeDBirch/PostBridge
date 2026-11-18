"""Narration above the first [PART], and reading the script inside PostBridge.

Jordan recorded the intro's narration as a file of its own.  The script had
its [VO] block above the first [PART], so it belonged to no part, Step 2 had
no "VO: ..." to assign the recording to, and nothing said why.  And there was
no way to look at the script without leaving the app.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import script_check          # noqa: E402
import script_normalize      # noqa: E402
import script_view           # noqa: E402
from parsers import parse_script  # noqa: E402

SCRIPT = """Ep 7

[VO INTRO_NARRATOR]
Welcome to the show.

[PART Part 1: The Lake]

[VO PART_1_NARRATOR]
It was cold.

[BUTLER 00:00:05-00:00:20]
We went out at four.
"""


def _named(findings, text):
    return [f for f in findings if text in f.title]


def test_narration_above_the_first_part_is_a_blocking_finding():
    found = _named(script_check.run_checks(SCRIPT), "above the first [PART")
    assert len(found) == 1
    f = found[0]
    assert f.severity == script_check.ERROR and f.line == 3
    assert "INTRO_NARRATOR" in f.detail and "[PART Intro]" in f.detail


def test_a_script_whose_narration_all_sits_in_parts_says_nothing():
    sound = SCRIPT.replace("[VO INTRO_NARRATOR]", "[PART Intro]\n\n[VO INTRO_NARRATOR]")
    assert not _named(script_check.run_checks(sound), "above the first [PART")
    # No parts at all is the parser's single unnamed part, already reported.
    bare = "[VO A]\nOne.\n\n[BUTLER 00:00:05-00:00:20]\nTwo.\n"
    assert not _named(script_check.run_checks(bare), "above the first [PART")


def test_fix_format_gives_that_narration_a_part():
    fixed, changes = script_normalize.normalize(SCRIPT, title="Ep 7")
    assert "Added [PART Intro] above the narration that came before the first part" in changes
    _tokens, parts, _pulls, vo, _t, _w = parse_script(fixed)
    assert [p["name"] for p in parts] == ["Intro", "Part 1: The Lake"]
    assert [(v["id"], v["part_index"]) for v in vo] == [("INTRO_NARRATOR", 1),
                                                         ("PART_1_NARRATOR", 2)]
    assert not _named(script_check.run_checks(fixed), "above the first [PART")
    assert script_normalize.normalize(fixed, title="Ep 7") == (fixed, [])   # once is enough


def test_fix_format_names_it_opening_when_intro_is_taken():
    text = SCRIPT.replace("[PART Part 1: The Lake]", "[PART Intro]")
    fixed, _changes = script_normalize.normalize(text, title="Ep 7")
    assert [p["name"] for p in parse_script(fixed)[1]] == ["Opening", "Intro"]


def test_a_cold_open_pull_goes_under_the_new_part_too():
    text = "[BUTLER 00:00:05-00:00:20]\nWe went out at four.\n\n" + SCRIPT.split("\n", 2)[2]
    fixed, _changes = script_normalize.normalize(text, title="Ep 7")
    _tokens, parts, pulls, _vo, _t, _w = parse_script(fixed)
    assert parts[0]["name"] == "Intro" and pulls[0]["part_index"] == 1


def test_describe_names_every_header_and_outlines_the_parts():
    info = script_view.describe(SCRIPT)
    assert info["kinds"] == {3: "vo", 6: "part", 8: "vo", 11: "pull"}
    assert info["tokens"] == {11: "BUTLER"}
    assert info["outline"] == [(6, "Part 1: The Lake")]
    assert info["counts"] == (1, 2, 1, 1)
    text, _colour = script_view.headline(info["findings"])
    assert text.endswith("blocking issue") or "blocking issues" in text


# ── The window ────────────────────────────────────────────────────────────

if True:
    import shutil
    _HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@pytest.mark.skipif(not _HAVE_FFMPEG, reason="the app fixture's project needs ffmpeg")
def test_the_script_opens_in_a_window_and_a_finding_goes_to_its_line(tmp_path, monkeypatch):
    import tkinter as tk
    from test_episode_screens import _app_or_skip, _pump
    app = _app_or_skip()
    try:
        errors = []
        app.report_callback_exception = lambda *x: errors.append(x)
        path = str(tmp_path / "Ep 7.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(SCRIPT)
        win = app._view_script(path)
        _pump(app, 10)
        body = win._text.get("1.0", "end")
        assert "[VO INTRO_NARRATOR]" in body and "We went out at four." in body
        assert "part" in win._text.tag_names("6.8") and "vo" in win._text.tag_names("3.8")
        assert "num_err" in win._text.tag_names("3.1")        # the line Script Check names
        win.goto(3)
        assert win._text.tag_ranges("at")
        # Asking again reuses the window, and RE-READ picks up an edit.
        with open(path, "w", encoding="utf-8") as f:
            f.write(SCRIPT.replace("[VO INTRO_NARRATOR]", "[PART Intro]\n\n[VO INTRO_NARRATOR]"))
        assert app._view_script(path) is win
        _pump(app, 10)
        assert not [f for f in win.info["findings"] if "above the first" in f.title]
        assert not errors, errors
        win.destroy()
    finally:
        app.destroy()
