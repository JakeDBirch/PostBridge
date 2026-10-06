"""Tk screens must build on current Tk.

Tk 8.6.14 stopped accepting a (left, right) pair for a widget's own
padx/pady — "bad screen distance".  One such Label in the Pull Quotes
session view aborted the whole screen on Windows builds made with a
current Python, so every transcript showed up blank.  A pair is only
valid in pack()/grid().

The static scan needs nothing; the smoke test needs tkinter and a display
(CI runs it under xvfb) and skips otherwise."""
import ast
import glob
import json
import os
import sys
import traceback

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_WIDGETS = {"Label", "Frame", "Button", "Text", "Canvas", "Entry",
            "Checkbutton", "Radiobutton", "Listbox", "Scale", "Message",
            "LabelFrame", "Toplevel", "Spinbox", "Menubutton", "Scrollbar",
            "PanedWindow", "config", "configure"}


def test_no_widget_gets_a_padding_pair():
    bad = []
    for path in sorted(glob.glob(os.path.join(ROOT, "*.py"))):
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name not in _WIDGETS:
                continue
            for kw in node.keywords:
                if (kw.arg in ("padx", "pady")
                        and isinstance(kw.value, (ast.Tuple, ast.List))):
                    bad.append("{}:{}  {}({}={})".format(
                        os.path.basename(path), node.lineno, name, kw.arg,
                        ast.unparse(kw.value)))
    assert not bad, ("A widget's own padx/pady takes ONE distance; pairs "
                     "belong in pack()/grid():\n  " + "\n  ".join(bad))


def _app_or_skip():
    tk = pytest.importorskip("tkinter")
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        pytest.skip("no display")
    try:
        import main
        app = main.App()
    except tk.TclError as e:
        pytest.skip("Tk unavailable: {}".format(e))
    return app


def test_pull_quotes_session_view_shows_transcript(tmp_path):
    media = tmp_path / "Butler.mp3"
    media.write_bytes(b"")
    words = []
    for i, w in enumerate(("Well I think the hunt started early that "
                           "morning and we walked the ridge. ").split() * 20):
        words.append({"word": " " + w, "start": i * 0.35,
                      "end": i * 0.35 + 0.3, "speaker": str(media)})
    sess_path = tmp_path / "BUTLER.pb_session.json"
    sess_path.write_text(json.dumps({
        "version": 3, "workflow": "interview_session", "id": "ses_test",
        "token": "BUTLER", "media": [str(media)], "transcript": words}))
    proj = {"workflow": "pq_project", "title": "Test",
            "sessions": [str(sess_path)]}
    proj_path = tmp_path / "Test.pb_episode.json"
    proj_path.write_text(json.dumps(proj))

    app = _app_or_skip()
    errors = []
    app.report_callback_exception = (
        lambda *a: errors.append("".join(traceback.format_exception(*a))))
    try:
        app.workflow = "pull_quotes"
        app._pq_load_project(proj, str(proj_path))
        app._pq_open_session_view(app._pq_project["sessions"][0])
        for _ in range(20):
            app.update()
        tx = getattr(app, "_pq_tx_text", None)
        assert tx is not None, "session view never built its transcript pane"
        text = tx.get("1.0", "end")
        assert "hunt started early" in text, text[:200]
        assert not errors, "\n".join(errors)
    finally:
        app.destroy()
