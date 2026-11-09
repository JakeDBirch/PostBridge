"""The waveform editor's zoom, snap and loading, built into a window as the
Assembly's source view is.  The audio is put in place by hand: extraction hands
back to Tk from a worker, which needs a mainloop.

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


def _host(root):
    """A frame in a window of its own, for the view to be built into."""
    top = tk.Toplevel(root)
    frame = tk.Frame(top)
    frame.pack(fill="both", expand=True)
    return frame


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
                                       _host(root), title="BUTLER")
    settle = time.monotonic() + 0.2
    while time.monotonic() < settle:
        root.update()
    d._need_initial_zoom = False
    d._samples = np.zeros(d._sr * SECS, np.float32)
    d._ctx_start, d._ctx_dur = 0.0, float(SECS)
    d._spp, d._view_start = 400, d._sr * 10
    yield d
    d.destroy()
    root.destroy()


def _middle_s(d):
    return (d._view_start + d._canvas_w / 2.0 * d._spp) / d._sr + d._ctx_start


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
    d = match_review.MatchReviewDialog(root, "a.wav", [[50.0, 60.0]], _host(root),
                                       title="BUTLER", quote_text="first line")
    _pump(root)
    yield root, d, log
    d.destroy()
    root.destroy()


def test_loading_a_clip_replaces_everything_about_the_last_one(walking):
    root, d, log = walking
    d._samples = np.zeros(d._sr * SECS, np.float32)
    d._push_undo()
    d._search_var.set("first")
    d.load_clip("b.wav", [[5.0, 6.0]], title="MILLER", quote_text="second line",
                scripted_tc="00:00:05 -> 00:00:06",
                words=[{"word": "hello", "start": 5.0, "end": 5.4}])
    root.update()
    assert (d._in_s, d._out_s, d._playhead_s) == (5.0, 6.0, 5.0)
    assert d._samples is None and d._undo_stack == []
    assert d._search_var.get() == ""
    assert "second line" in d._script_text.get("1.0", "end")
    assert "first line" not in d._script_text.get("1.0", "end")
    assert d._scripted_val.cget("text") == "00:00:05 -> 00:00:06"
    assert d._word_frame.winfo_manager() == "pack"

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


# ── The script lights up as the clip plays ────────────────────────────────

class _Player:
    """Stands in for the live engine: _animate_playhead reads position()."""
    def __init__(self):
        self.t, self.playing = 0.0, True

    def is_playing(self):
        return self.playing

    def position(self):
        return self.t

    def stop(self):
        self.playing = False

    def close(self):
        pass


N_WORDS = 300
WORDS = [{"word": " w{}".format(i), "start": 50.0 + 0.3 * i,
          "end": 50.0 + 0.3 * i + 0.25} for i in range(N_WORDS)]
# What the script says: the read, except one word said differently.
SCRIPT = " ".join("w{}".format(i) if i != 5 else "changed"
                  for i in range(N_WORDS))


@pytest.fixture
def playing(monkeypatch):
    monkeypatch.setattr(match_review.MatchReviewDialog, "_start_extraction",
                        lambda self: None)
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip("Tk unavailable: {}".format(exc))
    root.withdraw()
    d = match_review.MatchReviewDialog(
        root, "a.wav", [[50.0, 50.0 + 0.3 * N_WORDS]], _host(root), title="BUTLER",
        quote_text=SCRIPT, context_before="before\n" * 40,
        words=WORDS, auto_snap=False)
    _pump(root)
    d._samples = np.zeros(d._sr * 200, np.float32)
    d._ctx_start, d._ctx_dur = 0.0, 200.0
    p = d._player = _Player()
    yield root, d, p
    d.destroy()
    root.destroy()


def _lit(d):
    r = d._script_text.tag_ranges("playing")
    return d._script_text.get(r[0], r[1]) if r else None


def _hear(root, d, p, t):
    p.t = t
    d._animate_playhead()
    root.update()


def test_the_script_word_being_heard_lights_up(playing):
    root, d, p = playing
    _hear(root, d, p, 50.0 + 0.3 * 3 + 0.1)
    assert _lit(d) == "w3"
    _hear(root, d, p, 50.0 + 0.3 * 4 + 0.1)
    assert _lit(d) == "w4"
    _hear(root, d, p, 50.0 + 0.3 * 4 + 0.28)     # between two words
    assert _lit(d) is None


def test_a_word_the_script_has_differently_stays_dark(playing):
    root, d, p = playing
    _hear(root, d, p, 50.0 + 0.3 * 5 + 0.1)
    assert _lit(d) is None
    _hear(root, d, p, 50.0 + 0.3 * 6 + 0.1)
    assert _lit(d) == "w6"


def test_the_script_scrolls_to_keep_the_word_in_view(playing):
    root, d, p = playing
    qt = d._script_text
    qt.yview_moveto(0.0)                 # reading the lines before it
    root.update()
    word = qt.search("w3 ", "1.0")
    assert qt.bbox(word) is None
    _hear(root, d, p, 50.0 + 0.3 * 3 + 0.1)
    assert _lit(d) == "w3"
    assert qt.bbox(qt.tag_ranges("playing")[0]) is not None


def test_stopping_clears_it(playing):
    root, d, p = playing
    _hear(root, d, p, 50.0 + 0.3 * 3 + 0.1)
    d._stop()
    root.update()
    assert _lit(d) is None and d._playhead_anim is None


def test_playback_ending_on_its_own_clears_it(playing):
    root, d, p = playing
    _hear(root, d, p, 50.0 + 0.3 * 3 + 0.1)
    p.playing = False
    d._animate_playhead()
    assert _lit(d) is None


def test_another_clip_starts_dark_and_lights_its_own_script(playing):
    root, d, p = playing
    _hear(root, d, p, 50.0 + 0.3 * 3 + 0.1)
    d.load_clip("a.wav", [[50.0 + 0.3 * 10, 50.0 + 0.3 * 14]],
                quote_text="w10 w11 w12 w13", words=WORDS, auto_snap=False)
    assert _lit(d) is None
    d._player = p
    p.playing = True
    _hear(root, d, p, 50.0 + 0.3 * 12 + 0.1)
    assert _lit(d) == "w12"


def test_a_lone_word_far_from_the_clip_is_not_taken_for_it():
    import play_highlight
    words = [{"word": "the", "start": 1.0, "end": 1.2},
             {"word": "dog", "start": 19.0, "end": 19.2},
             {"word": "cat", "start": 20.0, "end": 20.2},
             {"word": "sat", "start": 20.3, "end": 20.5}]
    idx = play_highlight.align_text_to_words("the cat sat", words,
                                             span=(20.0, 20.5))
    assert [(a, b) for _, _, a, b in idx] == [(4, 7), (8, 11)]
