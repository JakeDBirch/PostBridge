"""The waveform editor's window and zoom, in a real window.  The audio is put in
place by hand: extraction hands back to Tk from a worker, which needs a mainloop.

Needs tkinter and a display; skips otherwise.
"""
import os
import sys
import time

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

tk = pytest.importorskip("tkinter")

import match_review  # noqa: E402

SECS = 120


@pytest.fixture
def dlg(monkeypatch):
    monkeypatch.setattr(match_review.MatchReviewDialog, "_start_extraction",
                        lambda self: None)
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip("Tk unavailable: {}".format(exc))
    root.withdraw()
    d = match_review.MatchReviewDialog(root, "no_such_file.wav", [[50.0, 60.0]],
                                       title="BUTLER")
    settle = time.monotonic() + 0.2
    while time.monotonic() < settle:
        root.update()
    d._need_initial_zoom = False
    d._samples = np.zeros(d._sr * SECS, np.float32)
    d._ctx_start, d._ctx_dur = 0.0, float(SECS)
    d._spp, d._view_start = 400, d._sr * 10
    yield d
    d._on_cancel()
    root.destroy()


def _middle_s(d):
    return (d._view_start + d._canvas_w / 2.0 * d._spp) / d._sr + d._ctx_start


def test_it_opens_full_screen(dlg):
    if sys.platform == "win32":
        assert dlg._win.state() == "zoomed"
    assert dlg._win.winfo_width() > 1000


@pytest.mark.parametrize("factor", [1 / 1.5, 1.5])
@pytest.mark.parametrize("view_s, cursor", [(50.0, 55.0), (0.0, 80.0)],
                         ids=["in sight", "out of sight"])
def test_the_zoom_keys_centre_on_the_cursor(dlg, factor, view_s, cursor):
    dlg._spp = 100
    dlg._view_start = int(view_s * dlg._sr)
    dlg._playhead_s = cursor
    before = dlg._spp
    dlg._zoom_by(factor)
    assert dlg._spp != before
    assert _middle_s(dlg) == pytest.approx(cursor, abs=0.001)


def test_while_it_plays_the_zoom_keys_centre_on_where_you_clicked(dlg):
    dlg._playhead_s, dlg._pre_play_pos = 75.0, 60.0
    dlg._zoom_by(1 / 1.5)
    assert _middle_s(dlg) == pytest.approx(60.0, abs=0.001)
    dlg._pre_play_pos = None


def test_near_the_start_of_the_file_the_view_stops_at_it(dlg):
    dlg._spp = 2000
    dlg._playhead_s = 0.5
    dlg._zoom_by(1.5)
    assert dlg._view_start == 0


def test_the_wheel_keeps_what_is_under_the_mouse_where_it_is(dlg):
    dlg._playhead_s = 40.0
    x = 300
    under = dlg._view_start + x * dlg._spp
    dlg._zoom_by(1 / 1.5, x)
    assert dlg._view_start + x * dlg._spp == pytest.approx(under, abs=1)


def test_the_automatic_snap_does_not_count_as_an_edit(dlg):
    dlg._sil_ends, dlg._sil_starts = [49.8], [60.2]
    dlg._auto_snap_boundaries()
    assert dlg._in_s != 50.0
    assert dlg._edited_segments() == dlg._opened_segs
    dlg._out_s -= 1.0
    assert dlg._edited_segments() != dlg._opened_segs


def _labels(w):
    for c in w.winfo_children():
        if c.winfo_class() == "Label":
            yield c
        yield from _labels(c)


def _next_button(d):
    return next((b for b in _labels(d._win)
                 if b.cget("text").startswith("NEXT CLIP")), None)


def test_the_last_clip_has_no_next_button(dlg):
    assert _next_button(dlg) is None


def test_the_next_button_is_on_screen_and_moves_on(monkeypatch):
    monkeypatch.setattr(match_review.MatchReviewDialog, "_start_extraction",
                        lambda self: None)
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip("Tk unavailable: {}".format(exc))
    root.withdraw()
    went, accepted = [], []
    d = match_review.MatchReviewDialog(root, "no_such_file.wav", [[50.0, 60.0]],
                                       on_accept=accepted.append,
                                       on_next=lambda: went.append(True))
    try:
        settle = time.monotonic() + 0.2
        while time.monotonic() < settle:
            root.update()
        b = _next_button(d)
        assert b.winfo_viewable()
        bottom = b.winfo_rooty() + b.winfo_height()
        assert bottom <= d._win.winfo_rooty() + d._win.winfo_height()
        b.event_generate("<ButtonRelease-1>")
        root.update()
        assert went == [True] and accepted == []
        assert not d._win.winfo_exists()
    finally:
        root.destroy()
