"""The waveform editor's window and zoom, in a real window.  The audio is put in
place by hand: extraction hands back to Tk from a worker, which needs a mainloop.

Needs tkinter and a display; skips otherwise.
"""
import os
import sys
import time
from concurrent.futures import Future

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


def test_points_someone_has_set_stay_where_they_are(dlg):
    dlg._auto_snap = False
    dlg._sil_ends, dlg._sil_starts = [50.0], [60.0]
    dlg._auto_snap_boundaries()
    assert (dlg._in_s, dlg._out_s) == (50.0, 60.0)
    assert dlg._edited_segments() == [(50.0, 60.0)]


def _labels(w):
    for c in w.winfo_children():
        if c.winfo_class() == "Label":
            yield c
        yield from _labels(c)


def _button(d, text):
    return next(b for b in _labels(d._win) if text in b.cget("text"))


def _pump(root, secs=0.2):
    settle = time.monotonic() + secs
    while time.monotonic() < settle:
        root.update()


@pytest.fixture
def walking(monkeypatch):
    monkeypatch.setattr(match_review.MatchReviewDialog, "_start_extraction",
                        lambda self: None)
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip("Tk unavailable: {}".format(exc))
    root.withdraw()
    log = []
    d = match_review.MatchReviewDialog(root, "a.wav", [[50.0, 60.0]],
                                       title="BUTLER", quote_text="first line",
                                       on_accept=lambda s: log.append(("accept", s)),
                                       on_next=lambda: log.append("next"),
                                       on_prev=lambda: log.append("prev"))
    _pump(root)
    yield root, d, log
    d._on_cancel()
    root.destroy()


def test_the_buttons_are_on_screen_and_only_accept_and_next_accepts(walking):
    root, d, log = walking
    for text, want in (("PREVIOUS", "prev"), ("NEXT  ", "next")):
        b = _button(d, text)
        assert b.winfo_viewable()
        assert (b.winfo_rooty() + b.winfo_height()
                <= d._win.winfo_rooty() + d._win.winfo_height())
        log.clear()
        b.event_generate("<ButtonRelease-1>")
        root.update()
        assert log == [want]
    log.clear()
    _button(d, "ACCEPT AND NEXT").event_generate("<ButtonRelease-1>")
    root.update()
    assert log == [("accept", [(50.0, 60.0)]), "next"]
    assert d._win.winfo_exists()


def test_with_nothing_that_way_the_button_is_dim_and_does_nothing(walking):
    root, d, log = walking
    d._on_prev = None
    d._sync_step_btns()
    b = _button(d, "PREVIOUS")
    assert b.cget("fg") == match_review.BORDER
    b.event_generate("<ButtonRelease-1>")
    root.update()
    assert log == []
    assert d._win.winfo_exists()


def test_loading_a_clip_replaces_everything_about_the_last_one(walking):
    root, d, log = walking
    d._samples = np.zeros(d._sr * SECS, np.float32)
    d._push_undo()
    d._search_var.set("first")
    d.load_clip("b.wav", [[5.0, 6.0]], title="MILLER", quote_text="second line",
                scripted_tc="00:00:05 -> 00:00:06",
                words=[{"word": "hello", "start": 5.0, "end": 5.4}])
    root.update()
    assert d._win.title().endswith("MILLER")
    assert (d._in_s, d._out_s, d._playhead_s) == (5.0, 6.0, 5.0)
    assert d._samples is None and d._undo_stack == []
    assert d._search_var.get() == ""
    assert "second line" in d._script_text.get("1.0", "end")
    assert "first line" not in d._script_text.get("1.0", "end")
    assert d._scripted_val.cget("text") == "00:00:05 -> 00:00:06"
    assert d._word_frame.winfo_manager() == "pack"
    assert d._on_next is None and d._on_prev is None

    d.load_clip("c.wav", [[7.0, 8.0]])          # no script text, no words
    root.update()
    assert not d._script_frame.winfo_manager()
    assert not d._word_frame.winfo_manager()
    assert d._scripted_head.cget("text") == ""


def test_a_preview_for_the_last_clip_is_dropped(walking):
    root, d, log = walking
    stale = Future()
    stale.set_result((np.ones(800, np.float32), 40.0, 0.5, 100.0))
    gen = d._load_gen
    d.load_clip("b.wav", [[5.0, 6.0]])
    d._preview_done(stale, gen)
    _pump(root, 0.1)
    assert d._samples is None and d._file_dur_s == 0.0


def test_a_whole_file_that_arrives_late_only_lands_on_its_own_file(walking):
    root, d, log = walking
    whole = Future()
    whole.set_result((np.zeros(d._sr * SECS, np.float32), 0.5, True))
    d.load_clip("b.wav", [[5.0, 6.0]])
    d._full_done(whole, "a.wav")
    _pump(root, 0.1)
    assert d._samples is None and not d._full_file
    d._full_done(whole, "b.wav")
    _pump(root, 0.1)
    assert d._full_file and len(d._samples) == d._sr * SECS


def test_another_clip_of_a_file_already_loaded_does_not_load_it_again(walking,
                                                                      monkeypatch):
    root, d, log = walking
    d._samples = samples = np.zeros(d._sr * SECS, np.float32)
    d._full_file, d._ctx_start, d._ctx_dur = True, 0.0, float(SECS)
    started = []
    monkeypatch.setattr(match_review.MatchReviewDialog, "_start_extraction",
                        lambda self: started.append(True))
    d.load_clip("a.wav", [[70.0, 75.0]])
    root.update()
    assert d._samples is samples and started == []
    view_s = d._view_start / d._sr
    assert view_s <= 70.0 and view_s + d._canvas_w * d._spp / d._sr >= 75.0
    d.load_clip("b.wav", [[5.0, 6.0]])
    assert d._samples is None and started == [True]
