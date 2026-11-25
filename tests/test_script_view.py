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
    # (and the part title the narrator reads, ahead of part 1's narration)
    assert [(v["id"], v["part_index"]) for v in vo if not v.get("part_title")] == [
        ("INTRO_NARRATOR", 1), ("PART_1_NARRATOR", 2)]
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


# ── Fetching from the same window, and the review keeping up ──────────────

if _HAVE_FFMPEG:
    from test_episode import project  # noqa: E402,F401  (module-scoped fixture)
    from test_episode_screens import app, _open_fresh, _wait, _pump, _click  # noqa: E402,F401


def _labels(w):
    import tkinter as tk
    out = []
    for c in w.winfo_children():
        if isinstance(c, tk.Label):
            out.append(c.cget("text"))
        out += _labels(c)
    return out


@pytest.mark.skipif(not _HAVE_FFMPEG, reason="the app fixture's project needs ffmpeg")
def test_the_script_is_fetched_where_it_is_read_and_the_review_follows(
        app, monkeypatch, project):
    """FETCH & CHECK used to be a button on the review and a dialog of its
    own, and the review's line about the script stayed as it was read before
    the fetch: an issue fixed in the Doc still showed as blocking."""
    import gdocs
    _open_fresh(app, monkeypatch, project)
    fixed = SCRIPT.replace("[VO INTRO_NARRATOR]", "[PART Intro]\n\n[VO INTRO_NARRATOR]")
    doc = {"text": SCRIPT}
    monkeypatch.setattr(gdocs, "is_configured", lambda: True)
    monkeypatch.setattr(gdocs, "is_ready", lambda: True)
    monkeypatch.setattr(gdocs, "list_tabs", lambda creds, doc_id, docs=None: [("t.0", "Tab 1")])
    monkeypatch.setattr(gdocs, "get_credentials", lambda interactive=True: object())
    monkeypatch.setattr(gdocs, "fetch_text", lambda creds, doc_id, tab=None: (
        doc["text"], {"name": "Ep 7 intro", "version": "3", "modifiedTime": "t"}))
    app._ep["links"]["gdoc"] = {"id": "doc123", "name": "Ep 7 intro"}
    app._ep_view()
    _pump(app, 5)
    assert "FETCH & CHECK" not in _labels(app.body)          # one way in

    # Nothing fetched yet: VIEW SCRIPT brings the Doc in and shows it.
    assert _click(app, app.body, "VIEW SCRIPT")
    assert _wait(app, lambda: getattr(app, "_ep_fetch_count", 0) == 1, secs=10)
    _pump(app, 5)
    win = app._ep_findings_win
    assert [f for f in win.info["findings"] if "above the first" in f.title]
    assert any("fetched just now" in t for t in _labels(win))
    assert any(t.startswith("\u00b7  script:") and "blocking" in t for t in _labels(app.body))

    # Fixed in the Doc; fetched from the window it is read in.
    doc["text"] = fixed
    assert _click(app, win, "\u27f3 FETCH LATEST")
    assert _wait(app, lambda: app._ep_fetch_count == 2, secs=10)
    _pump(app, 5)
    assert app._ep_findings_win is win and win.winfo_exists()
    assert "[PART Intro]" in win._text.get("1.0", "end")
    assert not [f for f in win.info["findings"] if "above the first" in f.title]
    # ...and the review no longer calls it blocking.
    assert not any("blocking" in t for t in _labels(app.body) if "script:" in t)

    # Opened again just to read: the copy as last fetched, with a way to fetch.
    _click(app, win, "CLOSE")
    assert _click(app, app.body, "VIEW SCRIPT")
    _pump(app, 5)
    assert app._ep_fetch_count == 2
    assert any("as last fetched" in t for t in _labels(app._ep_findings_win))
    assert "\u27f3 FETCH LATEST" in _labels(app._ep_findings_win)
    app._ep_findings_win.destroy()
    assert not app._errors, "\n".join(app._errors)


@pytest.mark.skipif(not _HAVE_FFMPEG, reason="the app fixture's project needs ffmpeg")
def test_a_loose_file_is_attached_by_token(app, monkeypatch, project):
    """"Attach to" listed the sources by name; the script, Step 2 and Pro
    Tools all know them by token."""
    _open_fresh(app, monkeypatch, project)
    ep = app._ep
    ep["sources"]["Ep 7 pt 1"]["vo_part"] = "Open"
    cam = ep["sources"]["Ep 7 pt 2"]["video"][0]
    rec = ep["sources"]["Ep 7 pt 2"]["audio"][0]
    ep["sources"]["Ep 7 pt 2"]["video"].remove(cam)
    choices = app._ep_attach_choices()
    assert list(choices.items()) == [("BUTLER", "Butler Interview"),
                                     ("VO: Open", "Ep 7 pt 1"),
                                     ("Ep 7 pt 2  (no token)", "Ep 7 pt 2")]
    app._ep_view()
    _pump(app, 5)
    assert "NOT YET IN A SOURCE" in _labels(app.body)
    app._ep_attach(choices["BUTLER"], "video", cam)
    assert cam in ep["sources"]["Butler Interview"]["video"]
    # Two sources under one token are told apart by name.
    ep["sources"]["Ep 7 pt 2"]["token"] = "BUTLER"
    assert set(app._ep_attach_choices()) == {
        "BUTLER  \u00b7  Butler Interview", "BUTLER  \u00b7  Ep 7 pt 2", "VO: Open"}
    assert rec in ep["sources"]["Ep 7 pt 2"]["audio"]
    assert not app._errors, "\n".join(app._errors)
