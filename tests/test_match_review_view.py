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
