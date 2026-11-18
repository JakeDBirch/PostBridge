"""The Assembly track with the surrounding audio shown (Enter): every cut
opened up to show the tape it takes out, dimmed, with a handle of the
recording either side of each pull -- the one track, the same edits, and
the finished cut still what plays unless Skip edits (Shift+S) is off.

Built on test_assembly_edit's stand-in app.  Needs tkinter and a display;
skips otherwise.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

tk = pytest.importorskip("tkinter")

from test_assembly_edit import view, _key, _res  # noqa: E402,F401
import assembly_view  # noqa: E402
from assembly_view import _open_up, _to_open, _to_cut, _kept_spans  # noqa: E402

SHIFT = 0x0001


# ── The layout, as numbers ───────────────────────────────────────────────

def _one_pull():
    # A pull of two regions, source 10-11 and 12-14 (a second cut out), at
    # 0 on a 1-sample-a-second grid.
    return [{"start": 0, "end": 3, "gap": 0, "res": {"order": 1},
             "pieces": [(0, 1, 10), (1, 2, 12)], "segments": [(10, 11), (12, 14)],
             "paths": ["a.wav"]}]


def test_a_cut_opens_up_and_each_pull_gets_its_handles():
    placed, parts, shifts = _open_up(_one_pull(), {0: 0}, 1, handle_s=3)
    e = placed[0]
    assert e["ghosts"] == [(0, 3, 7, "handle", 0), (4, 1, 11, "cut", 1),
                           (7, 3, 14, "handle", 2)]
    assert e["pieces"] == [(3, 1, 10), (5, 2, 12)]
    assert (e["start"], e["end"]) == (3, 7)
    assert parts == {0: 0}                       # the Part flag leads the handle
    assert shifts == [(0.0, 3.0, True), (1.0, 1.0, False), (3.0, 3.0, False)]


def test_finished_cut_time_and_the_opened_up_track_map_both_ways():
    _p, _q, shifts = _open_up(_one_pull(), {}, 1, handle_s=3)
    for cut, opened in ((0.0, 3.0), (0.5, 3.5), (1.0, 4.0), (1.5, 5.5),
                        (3.0, 7.0), (3.25, 10.25)):
        assert _to_open(cut, shifts) == pytest.approx(opened)
        assert _to_cut(opened, shifts) == pytest.approx(cut)
    assert _to_cut(1.5, shifts) == 0.0           # in the handle: where it starts
    assert _to_cut(4.5, shifts) == 1.0           # in the cut: where it resumes


def test_a_long_jump_stays_closed():
    p = _one_pull()
    p[0]["pieces"][1] = (1, 2, 200)              # 189 s on: not opened up
    placed, _q, _s = _open_up(p, {}, 1, handle_s=3)
    assert [g[3] for g in placed[0]["ghosts"]] == ["handle", "handle"]


def test_kept_spans_are_what_the_ghosts_leave():
    ghosts = [(3.0, 1.0, 0, "cut", 1), (0.0, 1.0, 0, "handle", 0)]
    assert _kept_spans(ghosts, -0.5, 6.0) == [(-0.5, 0.0), (1.0, 3.0), (4.0, 6.0)]


# ── On the track ─────────────────────────────────────────────────────────

def _cut_one(app, win):
    _res(app, 1)["segments"] = [(10.0, 11.0), (12.0, 14.0)]
    win.refresh()


def test_enter_opens_the_cut_up_on_the_same_moment(view):
    app, win = view
    _cut_one(app, win)
    win.set_insert(1.5)                           # 12.5 s of the recording
    _key(app, win.cv, "Return")
    assert win.excised and win.view == "cut" and win.view_key.on
    c = win._clip(1)
    assert [(round(g[0], 3), round(g[1], 3), g[3]) for g in c.ghosts] == \
        [(0.0, 3.0, "handle"), (4.0, 1.0, "cut"), (7.0, 3.0, "handle")]
    assert [(round(tl, 3), round(du, 3)) for tl, du, _si in c.pieces] == \
        [(3.0, 1.0), (5.0, 2.0)]
    # Pull 2 sits after pull 1's after-handle, its gap and its own handle.
    assert win._clip(2).start == pytest.approx(3.0 + 0.5 + 3.0 + 1.0 + 3.0 + 3.0)
    assert win.playhead == pytest.approx(5.5) and win.insert == pytest.approx(5.5)
    assert win._clip(1).src_at(win.playhead) == pytest.approx(12.5)
    assert win._tc_text() == "0:01.5"            # the finished cut's time
    _key(app, win.cv, "Return")
    assert not win.excised and win.playhead == pytest.approx(1.5)
    assert all(not c.ghosts for c in win.clips)


def test_the_same_edits_work_opened_up(view):
    app, win = view
    _cut_one(app, win)
    win.toggle_excised()
    win.set_insert(6.0)                           # 13.0 s of the recording
    win.split()
    assert [tuple(s) for s in _res(app, 1)["segments"]] == \
        [(10.0, 11.0), (12.0, 13.0), (13.0, 14.0)]
    assert win.excised


def test_a_click_in_the_cut_audio_is_a_place_to_listen_from(view):
    app, win = view
    _cut_one(app, win)
    win.toggle_excised()
    win.redraw()
    app.update()
    y0, y1 = win._track_box()
    kind, c, g = win._hit(win._x(4.5), (y0 + y1) / 2.0)
    assert kind == "ghost" and c.order == 1 and g[3] == "cut"


def test_double_clicking_the_cut_audio_brings_it_back(view):
    app, win = view
    _cut_one(app, win)
    win.toggle_excised()
    win.redraw()
    app.update()
    y0, y1 = win._track_box()

    class _E(object):
        def __init__(self, x, y):
            self.x, self.y = x, y
    win._on_track_double(_E(win._x(4.5), (y0 + y1) / 2.0))
    assert [tuple(s) for s in _res(app, 1)["segments"]] == [(10.0, 14.0)]
    win.refresh()
    assert [g[3] for g in win._clip(1).ghosts] == ["handle", "handle"]


def test_a_trim_reaches_into_the_dimmed_audio(view):
    app, win = view
    _cut_one(app, win)
    win.toggle_excised()
    c = win._clip(1)
    d = {"mode": "out", "order": 1, "k": 0, "segs0": list(c.segments),
         "gaps0": list(c.seg_gaps), "free": True}
    # The first region's out, half way into the cut audio after it.
    assert win._trimmed(d, 0.5)[0] == (10.0, 11.5)


class _Player(object):
    def __init__(self):
        self.segments = "unset"

    def set_segments(self, s):
        self.segments = s

    def set_rate(self, r):
        pass

    def stop(self):
        pass

    def is_playing(self):
        return False

    def close(self):
        pass


def test_playback_skips_the_surrounding_audio_unless_shift_s_says_hear_it(view, monkeypatch):
    app, win = view
    monkeypatch.setitem(assembly_view._MONITOR, "skip_cuts", True)
    win.player = p = _Player()
    _cut_one(app, win)
    assert p.segments is None                     # nothing on show to skip
    win.toggle_excised()
    spans = p.segments
    assert spans[0] == (-assembly_view.LEAD_S, 0.0)
    assert (3.0, 4.0) in [(round(a, 3), round(b, 3)) for a, b in spans]
    assert not any(a < 4.5 < b for a, b in spans)  # the cut audio is jumped
    _key(app, win.cv, "S", SHIFT)
    assert p.segments is None and not assembly_view._MONITOR["skip_cuts"]
    # ...and the audio composed now has the cut audio in it.
    assert any(o == 1 and abs(si - 11.0) < 1e-6
               for _st, _du, o, si in win.audio._index[1])
    win.player = None


def test_the_words_in_the_cut_audio_show_too(view):
    app, win = view
    _cut_one(app, win)                            # "ridge" (11.2 s) is cut
    win.toggle_excised()
    words = dict((w, t) for t, w in win._ghost_words(win._clip(1)))
    assert words["ridge"] == pytest.approx(4.2)
    assert "dawn." not in words


def test_a_card_opens_its_pull_opened_up(view):
    app, win = view
    win.open_card(2)
    assert win.excised and win.view == "cut" and win.sel_order == 2
    assert win.insert == pytest.approx(win._clip(2).start)


def test_marks_stay_on_their_audio(view):
    app, win = view
    _cut_one(app, win)
    win.set_insert(1.5)                           # 12.5 s of the recording
    win.drop_marker("sfx")
    win._end_rename(commit=True)
    win.refresh()
    win.toggle_excised()
    m = next(m for m in win.markers if m["kind"] == "sfx")
    assert m["t"] == pytest.approx(5.5)


# ── Normalize, and a cut dragged whole ───────────────────────────────────

def test_a_quiet_pull_plays_at_the_level_of_a_loud_one():
    import numpy as np
    sr = assembly_view.PLAY_SR
    t = np.arange(sr) / float(sr)
    quiet = (np.sin(2 * np.pi * 200 * t) * 600).astype(np.int16)
    loud = (np.sin(2 * np.pi * 200 * t) * 20000).astype(np.int16)
    gq, gl = assembly_view._norm_gain(quiet), assembly_view._norm_gain(loud)
    assert gq * 600 == pytest.approx(gl * 20000, rel=0.02)
    assert gq * 600 / 32768.0 == pytest.approx(assembly_view.NORM_LEVEL, rel=0.02)
    assert assembly_view._norm_gain(np.zeros(sr, np.int16)) == 1.0
    hiss = (np.random.RandomState(0).randn(sr) * 2).astype(np.int16)
    assert assembly_view._norm_gain(hiss) == assembly_view.NORM_MAX_GAIN


def test_normalize_is_a_gain_on_what_plays():
    import numpy as np
    a = assembly_view._AssemblyAudio(1000)

    class _C(object):
        order, pieces, ghosts = 1, [(0.0, 1.0, 0.0)], []
    a.set_clips([_C()], 1.0)
    a.put(1, "a.wav", 0.0, np.full(2000, 1000, np.int16))
    lead = int(assembly_view.LEAD_S * 1000)
    plain = a.read(lead, 10)
    a.set_gains({1: 4.0})
    assert a.read(lead, 10) == pytest.approx(plain * 4.0)
    a.set_gains({1: 1000.0})
    assert a.read(lead, 10).max() == pytest.approx(1.0)      # held in, not wrapped


def test_the_normalize_button_switches_it(view, monkeypatch):
    app, win = view
    monkeypatch.setitem(assembly_view._MONITOR, "normalize", True)
    win._gain = {1: 3.0}
    win._apply_gains()
    assert win.audio._gains == {1: 3.0}
    win.toggle_normalize()
    assert win.audio._gains == {} and not win.norm_key.on
    win.toggle_normalize()
    assert win.audio._gains == {1: 3.0} and win.norm_key.on


class _Ev(object):
    def __init__(self, x, y, state=0):
        self.x, self.y, self.state = x, y, state


def test_dragging_a_cut_moves_both_its_edges(view):
    app, win = view
    _cut_one(app, win)                            # 10-11 | cut 11-12 | 12-14
    win.toggle_excised()
    win.redraw()
    app.update()
    y0, y1 = win._track_box()
    mid = (y0 + y1) / 2.0
    x = win._x(4.5)                               # in the cut audio
    win._on_track_press(_Ev(x, mid))
    assert win._drag["mode"] == "roll"
    win._on_track_drag(_Ev(x + 0.5 * win.pps, mid, 0x0004))   # Ctrl: no snap
    win._on_track_release(_Ev(x + 0.5 * win.pps, mid, 0x0004))
    segs = [tuple(round(v, 3) for v in sg) for sg in _res(app, 1)["segments"]]
    assert segs == [(10.0, 11.5), (12.5, 14.0)]   # the same second, later


def test_a_click_in_a_cut_is_still_a_place_to_listen_from(view):
    app, win = view
    _cut_one(app, win)
    win.toggle_excised()
    win.redraw()
    app.update()
    y0, y1 = win._track_box()
    x = win._x(4.5)
    win._on_track_press(_Ev(x, (y0 + y1) / 2.0))
    win._on_track_release(_Ev(x, (y0 + y1) / 2.0))
    assert win.insert == pytest.approx(4.5, abs=0.05)
    assert [tuple(s) for s in _res(app, 1)["segments"]] == [(10.0, 11.0), (12.0, 14.0)]


# ── Heal: a cut or split that shouldn't be there ─────────────────────────

def test_h_heals_the_cut_at_the_insertion_point(view):
    app, win = view
    _cut_one(app, win)                            # 10-11 | 12-14, at 0-1 | 1-3
    win.set_insert(1.0)                           # on the join
    _key(app, win.cv, "h")
    assert [tuple(s) for s in _res(app, 1)["segments"]] == [(10.0, 14.0)]
    assert app._rv[0]["accepted_flag"][0]


def test_h_heals_a_split_too_and_drops_its_silence(view):
    app, win = view
    _res(app, 1)["segments"] = [(10.0, 12.0), (12.0, 14.0)]
    _res(app, 1)["seg_gaps"] = [0.4]
    win.refresh()
    win.set_insert(0.5)                           # inside the first region
    win.heal()
    r = _res(app, 1)
    assert [tuple(s) for s in r["segments"]] == [(10.0, 14.0)]
    assert not r.get("seg_gaps")


def test_h_with_a_highlight_heals_every_join_in_it(view):
    app, win = view
    _res(app, 1)["segments"] = [(10.0, 11.0), (11.5, 12.0), (12.5, 14.0)]
    win.refresh()
    c = win._clip(1)
    win._range = (1, c.start + 0.2, c.end - 0.2)
    win.heal()
    assert [tuple(s) for s in _res(app, 1)["segments"]] == [(10.0, 14.0)]


def test_h_opened_up_in_the_cut_audio(view):
    app, win = view
    _cut_one(app, win)
    win.toggle_excised()
    win.set_insert(4.5)                           # in the dimmed cut
    win.heal()
    assert [tuple(s) for s in _res(app, 1)["segments"]] == [(10.0, 14.0)]


def test_a_join_back_in_the_recording_is_not_healed(view):
    app, win = view
    _res(app, 1)["segments"] = [(12.0, 14.0), (10.0, 11.0)]   # a pickup
    win.refresh()
    win.set_insert(2.0)
    win.heal()
    assert [tuple(s) for s in _res(app, 1)["segments"]] == [(12.0, 14.0), (10.0, 11.0)]
    assert "nothing to heal" in win.status.cget("text")
