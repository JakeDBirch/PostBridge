"""The waveform editor built into another window as a view (host=), the way
the Assembly window holds it as its source view.

Needs tkinter and a display; skips otherwise.
"""
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

tk = pytest.importorskip("tkinter")

import match_review  # noqa: E402


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setattr(match_review.MatchReviewDialog, "_start_extraction",
                        lambda self: None)
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip("Tk unavailable: {}".format(exc))
    root.withdraw()
    top = tk.Toplevel(root)
    frame = tk.Frame(top)
    frame.pack(fill="both", expand=True)
    d = match_review.MatchReviewDialog(root, "no_such_file.wav", [[50.0, 60.0]],
                                       title="NARRATOR", host=frame)
    settle = time.monotonic() + 0.2
    while time.monotonic() < settle:
        root.update()
    yield d, top, frame, root
    d.destroy()
    root.destroy()


def _labels(w):
    out = []
    for c in w.winfo_children():
        if isinstance(c, tk.Label):
            out.append(c.cget("text"))
        out += _labels(c)
    return out


def test_it_builds_into_the_frame_it_is_given(hosted):
    d, top, frame, _root = hosted
    assert d._win is top
    assert d._body.master is frame
    assert d._cv.winfo_toplevel() is top


def test_the_keys_are_the_hosts(hosted):
    d, top, _frame, _root = hosted
    assert top.bind() == ()


def test_there_is_no_accept_or_step_row(hosted):
    d, _top, frame, _root = hosted
    shown = _labels(frame)
    for gone in ("ACCEPT EDIT POINTS", "ACCEPT AND NEXT", "CANCEL"):
        assert gone not in shown
    assert d._next_btn is None
    d._sync_step_btns()                      # nothing to dim, and no error


def test_going_takes_the_view_and_leaves_the_window(hosted):
    d, top, frame, root = hosted
    d.destroy()
    root.update()
    assert not d._alive()
    assert top.winfo_exists() and frame.winfo_exists()


def test_the_window_title_is_left_alone(hosted):
    d, top, _frame, _root = hosted
    top.title("Assembly")
    d.load_clip("no_such_file.wav", [[70.0, 75.0]], title="OTHER")
    assert top.title() == "Assembly"
