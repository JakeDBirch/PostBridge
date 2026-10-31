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


def test_output_capture_leaves_the_standard_handles_alone(request):
    """Tcl keeps the standard handle numbers it first saw, for life.  With fd
    capture moving those handles, Tk fails to start every so often, in any
    test (see pytest.ini)."""
    assert request.config.getoption("capture") != "fd"


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


def _all_widgets(w):
    yield w
    for c in w.winfo_children():
        yield from _all_widgets(c)


def test_a_crash_on_the_ui_queue_is_reported_and_the_queue_carries_on():
    """A Step 2 that crashed on this queue left a blank screen and not a word
    why: the error was thrown away.  A widget gone with its screen is no fault."""
    tk = pytest.importorskip("tkinter")
    app = _app_or_skip()
    app.withdraw()
    reported, ran = [], []
    app.report_callback_exception = lambda exc, val, tb: reported.append(val)
    gone = tk.Label(app)
    gone.destroy()

    def _crash():
        raise AttributeError("a fault")
    try:
        app._ui(_crash)
        app._ui(lambda: gone.config(text="too late"))
        app._ui(lambda: tk.Label(app, no_such_option=1))
        app._ui(lambda: ran.append(True))
        app._pump_ui()
        assert ran
        assert [type(e) for e in reported] == [AttributeError, tk.TclError], reported
        assert "no_such_option" in str(reported[1])
    finally:
        app.destroy()


def test_the_sequence_takes_the_save_files_name(monkeypatch):
    import main
    tk = pytest.importorskip("tkinter")
    app = _app_or_skip()
    app.withdraw()
    try:
        # The name a script loads with, on Step 5 as the user sees it.
        app.workflow, app.results = "script_session", []
        app.seq_name.set("High Noon in Dodge City (AUTO)")
        app._step5()
        app.update()
        monkeypatch.setattr(main.filedialog, "asksaveasfilename",
                            lambda **k: "F:/Show/01_PROJECT FILES/GF_10 v2.1 for mix.aaf")
        app._pick_out(".aaf")
        assert app.seq_name.get() == "GF_10 v2.1 for mix"
        # It can still be retyped, and a cancelled dialog leaves it be.
        app.seq_name.set("my own name")
        monkeypatch.setattr(main.filedialog, "asksaveasfilename", lambda **k: "")
        app._pick_out(".aaf")
        assert app.seq_name.get() == "my own name"
        # A path typed into the field counts too; clearing it changes nothing.
        field = next(w for w in _all_widgets(app) if isinstance(w, tk.Entry)
                     and str(w.cget("textvariable")) == str(app.out_path))
        # (Called directly: a withdrawn window gets no key events.)
        assert field.bind("<KeyRelease>")
        app.out_path.set("F:\\Show\\typed.aaf")
        app._seq_from_out_path()
        assert app.seq_name.get() == "typed"
        app.out_path.set("")
        app._seq_from_out_path()
        assert app.seq_name.get() == "typed"
    finally:
        app.destroy()


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


def _step4_on_two_parts(tmp_path, focus):
    app = _app_or_skip()
    app._prefs = dict(app._prefs, s4_focus_mode=focus)
    app._save_prefs = lambda: None
    app._refine_resume = lambda: None
    script = tmp_path / "s.txt"
    script.write_text("x", encoding="utf-8")
    app._script_path = str(script)
    app.workflow = "script_session"
    app.parts = [{"index": 0, "name": "One"}, {"index": 1, "name": "Two"},
                 {"index": 2, "name": "Three"}]
    app.tokens = ["GUEST"]
    app.results = [dict(order=i, token="GUEST", status="ok", part_index=i // 2,
                        segments=[(float(i), i + 1.0)], rec_in_s=float(i),
                        rec_out_s=i + 1.0, quote_text="line {}".format(i),
                        confidence=0.9) for i in range(6)]
    app.pulls = [{"token": "GUEST", "order": i} for i in range(6)]
    app._step4()
    app.update()
    return app


def _packed(app):
    return [e["res"]["part_index"] for e in app._rv
            if e["card"].winfo_manager() == "pack"]


def test_step4_with_focus_opens_on_one_part(tmp_path):
    app = _step4_on_two_parts(tmp_path, focus=True)
    try:
        assert app._s4_collapsed == {1, 2}
        assert _packed(app) == [0, 0]
    finally:
        app.destroy()


def test_step4_without_focus_lays_every_card_out(tmp_path):
    app = _step4_on_two_parts(tmp_path, focus=False)
    try:
        assert app._s4_collapsed == set()
        assert _packed(app) == [0, 0, 1, 1, 2, 2]
        # In script order, each part's header above its cards.
        sf = app._rv[0]["card"].master
        kids = sf.pack_slaves()
        assert [k for k in kids if k in [e["card"] for e in app._rv]] == \
            [e["card"] for e in app._rv]
        assert len(kids) == 9
    finally:
        app.destroy()
