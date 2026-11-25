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
        self.source = None

    def load_source(self, source, t0=0.0):
        self.source = source

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
    """Shown and skipped, the surrounding audio isn't on what plays at all:
    the player is on the finished cut itself (cut_audio), on the cut's own
    time, so a cut plays as its crossfade.  Heard, the track as laid out."""
    app, win = view
    monkeypatch.setitem(assembly_view._MONITOR, "skip_cuts", True)
    win.player = p = _Player()
    _cut_one(app, win)
    win.toggle_excised()
    assert p.source is win.cut_audio and p.segments is None
    # The finished cut: pull 1's two pieces butt at 1.0 s, no cut audio in it.
    starts = [(round(st / win.cut_audio.sr - assembly_view.LEAD_S, 3), o, round(si, 3))
              for st, _du, o, si, *_r in win.cut_audio._index[1]]
    assert all(not (o == 1 and 11.0 < si < 12.0) for _t, o, si in starts)
    assert win._to_player(win._to_open_t(1.5)) == pytest.approx(1.5)
    _key(app, win.cv, "S", SHIFT)
    assert p.source is win.audio and not assembly_view._MONITOR["skip_cuts"]
    # ...and the audio composed now has the cut audio in it.
    assert any(o == 1 and abs(si - 11.0) < 1e-6
               for _st, _du, o, si, *_rest in win.audio._index[1])
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
    win.set_insert(2.2)                           # in the silence at the split
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


# ── Snapping on and off (G) ──────────────────────────────────────────────

def test_g_turns_snapping_off_and_on(view, monkeypatch):
    app, win = view
    monkeypatch.setitem(assembly_view._MONITOR, "snap", True)
    win._sil[1] = [11.02]                         # a speech edge near 11.0
    c = win._clip(1)
    d = {"mode": "out", "order": 1, "k": 0, "segs0": list(c.segments),
         "gaps0": list(c.seg_gaps)}
    assert win._snap_edge(dict(d), 11.0) == pytest.approx(11.02)
    _key(app, win.cv, "g")
    assert not assembly_view._MONITOR["snap"] and not win.snap_key.on
    assert win._snap_edge(dict(d), 11.0) == 11.0
    assert win._snap_timeline(1.0) == 1.0
    _key(app, win.cv, "g")
    assert assembly_view._MONITOR["snap"] and win.snap_key.on


def test_snapping_off_reaches_find_in_recording(view, monkeypatch):
    app, win = view
    monkeypatch.setitem(assembly_view._MONITOR, "snap", True)
    win.show_source(1)
    win.src._silence_boundaries = [11.02]
    win.src._spp = win.src._sr // 100              # 10 ms a pixel: 80 ms reach
    assert win.src._snap_to_silence(11.0) == pytest.approx(11.02)
    win.toggle_snap()
    assert win.src.snap is False and win.src._snap_to_silence(11.0) == 11.0
    win.toggle_snap()


def test_a_cuts_sides_take_one_boundary_and_its_middle_the_whole_cut(view):
    app, win = view
    _cut_one(app, win)                            # the cut: 4.0-5.0 opened up
    win.toggle_excised()
    win.pps = 200.0                               # 200 px of cut
    win.view0 = 3.0
    win.redraw()
    app.update()
    y0, y1 = win._track_box()
    mid = (y0 + y1) / 2.0
    assert win._hit(win._x(4.1), mid)[0::2] == ("out", 0)   # near its left side
    assert win._hit(win._x(4.9), mid)[0::2] == ("in", 1)    # near its right
    kind, _c, g = win._hit(win._x(4.5), mid)
    assert kind == "ghost" and g[3] == "cut"                # the middle
    # A narrow cut keeps a few pixels each side for its boundaries.
    win.pps = 40.0
    win.redraw()
    assert win._hit(win._x(4.0) + 10, mid)[0] == "out"
    assert win._hit(win._x(4.5), mid)[0] == "ghost"


# ── Pre-roll with the surrounding audio shown ────────────────────────────

def test_preroll_is_the_finished_cut_ahead_not_a_skipped_handle(view, monkeypatch):
    app, win = view
    monkeypatch.setitem(assembly_view._MONITOR, "preroll_on", True)
    monkeypatch.setitem(assembly_view._MONITOR, "preroll_s", 2.0)
    monkeypatch.setitem(assembly_view._MONITOR, "skip_cuts", True)
    _cut_one(app, win)
    win.toggle_excised()
    # Pull 2 starts at 3.5 s of the cut: its 2 s of pre-roll is the end of
    # pull 1 (2.5-3.0) and the gap, not pull 2's own handle.
    o2 = win._clip(2).start
    start = win._preroll_start(o2)
    assert win._to_cut_t(start) == pytest.approx(1.5)
    assert win._clip(1).src_at(start) == pytest.approx(12.5)
    # Inside pull 1, across its opened-up cut.
    o = win._to_open_t(1.5)                      # 12.5 s of the recording
    assert win._to_cut_t(win._preroll_start(o)) == pytest.approx(0.0)
    # Hearing the surrounding audio, the pre-roll is what is on the track.
    monkeypatch.setitem(assembly_view._MONITOR, "skip_cuts", False)
    assert win._preroll_start(o2) == pytest.approx(o2 - 2.0)


def test_the_speeds_are_three_quarter_one_one_and_a_half_two_three(view, monkeypatch):
    app, win = view
    monkeypatch.setattr(assembly_view.match_review, "_LAST_PLAY_RATE", 1.0)
    win._rate = 1.0
    seen = []
    for _ in range(4):
        win.step_rate(1)
        seen.append(win._rate)
    assert seen == [1.5, 2.0, 3.0, 3.0]
    for _ in range(5):
        win.step_rate(-1)
    assert win._rate == 0.75
    win._rate = 1.25                     # a speed from before: the nearest one up
    win.step_rate(1)
    assert win._rate in (1.5, 2.0)


# ── Clicking a speech start / stop tick ──────────────────────────────────

def test_clicking_a_tick_puts_the_insertion_point_on_it(view, monkeypatch):
    app, win = view
    win._sil[1] = [11.234]                        # a speech start, 1.234 s in
    win.pps = 200.0
    win.view0 = 0.0
    win.redraw()
    app.update()
    y0, y1 = win._track_box()
    top = y0 + win._ls + 8 + 3                    # in the tick row
    x = win._x(1.234) + 4                         # a little off it
    assert win._hit(x, top)[0] == "tick"
    win._on_track_press(_Ev(x, top))
    win._on_track_release(_Ev(x, top))
    assert win.insert == pytest.approx(1.234)
    # The same click in the middle of the lane is a click, not the tick.
    mid = (y0 + y1) / 2.0
    assert win._hit(x, mid)[0] != "tick"
    # Ticks stay clickable with snapping off.
    monkeypatch.setitem(assembly_view._MONITOR, "snap", False)
    assert win._hit(x, top)[0] == "tick"


# ── Showing the surrounding audio doesn't stop playback ──────────────────

class _Playing(_Player):
    def __init__(self, at):
        _Player.__init__(self)
        self.at, self.seeks = at, []

    def position(self):
        return self.at

    def seek(self, t):
        self.seeks.append(t)
        self.at = t

    def is_playing(self):
        return True


def test_enter_while_playing_carries_on_from_the_same_moment(view):
    app, win = view
    _cut_one(app, win)
    win.player = p = _Playing(1.5)                # 12.5 s of the recording
    win._playing = True
    try:
        win.toggle_excised()
        assert win._playing                       # not stopped
        # On the finished cut's own time: the same moment, 1.5 s in.
        assert p.at == pytest.approx(1.5)
        assert win.playhead == pytest.approx(5.5)
        assert win._clip(1).src_at(win.playhead) == pytest.approx(12.5)
        win.toggle_excised()
        assert win._playing and p.at == pytest.approx(1.5)
    finally:
        win._playing = False
        win.player = None


def test_h_far_from_any_join_changes_nothing(view):
    app, win = view
    _cut_one(app, win)                            # join at 1.0
    win.pps = 100.0
    win.set_insert(2.5)                           # 150 px from it
    win.heal()
    assert [tuple(s) for s in _res(app, 1)["segments"]] == [(10.0, 11.0), (12.0, 14.0)]
    assert "Nothing to heal here" in win.status.cget("text")


def _can_merge(app):
    """The stand-in app with the real merge, and an IGNORE that IGNOREs."""
    import main
    app._assembly_merge_pulls = main.App._assembly_merge_pulls.__get__(app)

    def _ignore(order):
        e = next(e for e in app._rv if e["res"]["order"] == order)
        e["skip_var"].set(True)
        e["res"]["_s4_ignored"] = True
    app._assembly_ignore = _ignore


def test_h_where_the_audio_runs_on_makes_the_two_pulls_one(view):
    app, win = view
    _can_merge(app)
    _res(app, 1)["segments"] = [(10.0, 14.0)]
    _res(app, 2)["segments"] = [(14.0, 16.0)]     # straight on from pull 1
    _res(app, 2)["markers"] = [{"kind": "sfx", "src": 15.0, "name": ""}]
    win.refresh()
    win.set_insert(win._clip(2).start)            # on the boundary
    win.heal()
    win.refresh()
    assert [tuple(s) for s in _res(app, 1)["segments"]] == [(10.0, 16.0)]
    assert _res(app, 2)["_merged_into"] == 1
    assert win._clip(2) is None and len(win.clips) == 1   # the boundary is gone
    assert "Nothing moved." in _res(app, 1)["quote_text"]
    assert _res(app, 1)["markers"][-1]["src"] == 15.0
    assert win._clip(1).end == pytest.approx(6.0)
    app._s4_undo()
    assert [tuple(s) for s in _res(app, 1)["segments"]] == [(10.0, 14.0)]
    assert "Nothing moved." not in _res(app, 1)["quote_text"]
    assert "_merged_into" not in _res(app, 2)


def test_h_where_the_audio_doesnt_run_on_changes_nothing(view):
    app, win = view
    _can_merge(app)
    _res(app, 1)["segments"] = [(10.0, 14.0)]     # pull 2 is 20-22: 6 s on
    win.refresh()
    before = [c.start for c in win.clips]
    win.set_insert(win._clip(2).start)
    win.heal()
    assert [tuple(s) for s in _res(app, 1)["segments"]] == [(10.0, 14.0)]
    assert [tuple(s) for s in _res(app, 2)["segments"]] == [(20.0, 22.0)]
    assert "gap_after_s" not in _res(app, 1)
    assert [c.start for c in win.clips] == before
    assert "doesn't run on" in win.status.cget("text")


def test_h_on_an_overlap_changes_nothing_either(view):
    app, win = view
    _can_merge(app)
    _res(app, 1)["segments"] = [(10.0, 20.6)]
    _res(app, 2)["segments"] = [(20.0, 22.0)]
    win.refresh()
    win.set_insert(win._clip(2).start)
    win.heal()
    assert [tuple(s) for s in _res(app, 1)["segments"]] == [(10.0, 20.6)]
    assert "_merged_into" not in _res(app, 2)


# ── Coming back to it as it was left ─────────────────────────────────────

def _reopen(app):
    import time as _time
    from conftest import open_assembly
    win = open_assembly(app)
    end = _time.monotonic() + 0.4                # its restore timers
    while _time.monotonic() < end:
        app.update()
    return win


def test_the_listening_settings_come_back(view, monkeypatch):
    app, win = view
    for k in assembly_view._MONITOR:
        monkeypatch.setitem(assembly_view._MONITOR, k, assembly_view._MONITOR[k])
    monkeypatch.setattr(assembly_view.match_review, "_LAST_PLAY_RATE", 1.5)
    win._rate = 1.5
    win.step_rate(1)                              # 2x
    win.toggle_snap()
    win.toggle_normalize()
    win.step_preroll(1)
    want = dict(assembly_view._MONITOR)
    app._assembly_view_key = lambda: "ep"
    win.close()
    assert app._prefs["assembly_monitor"]["rate"] == 2.0
    # A fresh run: everything at its defaults again.
    assembly_view._MONITOR.update(follow=False, snap=True, normalize=True,
                                  preroll_s=2.0)
    assembly_view.match_review._LAST_PLAY_RATE = 1.5
    win2 = _reopen(app)
    try:
        assert dict(assembly_view._MONITOR) == want
        assert win2._rate == 2.0 and not win2.snap_key.on and not win2.norm_key.on
    finally:
        win2.close()


def test_the_place_comes_back_on_the_same_audio(view):
    app, win = view
    app._assembly_view_key = lambda: "ep"
    _cut_one(app, win)
    win.toggle_excised()
    win.pps = 400.0
    c2 = win._clip(2)
    win.set_insert(c2.start + 0.5)                # 20.5 s of pull 2's audio
    win.view0 = win.insert - 300 / win.pps        # 300 px in from the left
    win.select(2)
    win.close()
    # Meanwhile pull 1 got longer: pull 2 has moved along the track.
    _res(app, 1)["segments"] = [(10.0, 11.0), (12.0, 15.0)]
    win2 = _reopen(app)
    try:
        assert win2.excised and win2.pps == pytest.approx(400.0)
        assert win2._clip(2).src_at(win2.insert) == pytest.approx(20.5)
        # 300 px in, as left -- held, as ever, so the cut's end stays on screen.
        span = win2._span()
        want = min(max(-span * 0.02, win2.insert - 300.0 / win2.pps),
                   max(0.0, win2.total_s - span * 0.9))
        assert win2.view0 == pytest.approx(want)
        assert win2.sel_order == 2
    finally:
        win2.close()


def test_an_episode_left_open_opens_it_again(view):
    import main
    app, win = view
    app._assembly_view_key = lambda: "ep"
    for n in ("_keep_assembly_for_next_time", "_reopen_kept_assembly",
              "_open_assembly"):
        setattr(app, n, getattr(main.App, n).__get__(app))
    win.set_insert(1.5)
    app._keep_assembly_for_next_time()            # PostBridge closing
    assert app._assembly is None
    assert app._prefs["assembly_views"]["ep"]["open"] is True
    app._reopen_kept_assembly()
    try:
        assert app._assembly is not None
        import time as _time
        end = _time.monotonic() + 0.4
        while _time.monotonic() < end:
            app.update()
        assert app._assembly.insert == pytest.approx(1.5)
    finally:
        app._assembly.close()
    # Closed by hand: next time it stays closed.
    assert app._prefs["assembly_views"]["ep"]["open"] is False


# ── Up and down zoom while placing or scrubbing ──────────────────────────

def test_dragging_down_on_the_ruler_zooms_in_about_the_pointer(view):
    app, win = view
    base = win._min_pps() * 1.5
    win.pps, win.view0 = base, 0.0
    win.redraw()
    x = 250
    win._on_track_press(_Ev(x, 4))               # the ruler: scrub
    t = win._t(x)
    win._on_track_drag(_Ev(x, 4 + 60))           # 60 px down
    assert win.pps == pytest.approx(base * 1.01 ** 60)
    assert win._t(x) == pytest.approx(t)         # the moment stays put
    assert win.playhead == pytest.approx(t)
    win._on_track_drag(_Ev(x, 4))                # back up: back out
    assert win.pps == pytest.approx(base)
    win._on_track_release(_Ev(x, 4))


def test_a_click_dragged_down_places_the_insertion_point_and_zooms_there(view):
    app, win = view
    base = win._min_pps() * 1.5
    win.pps, win.view0 = base, 0.0
    win.redraw()
    app.update()
    y0, y1 = win._track_box()
    y = y0 + 20                                  # the selector's half
    x = win._x(1.5)
    win._on_track_press(_Ev(x, y))
    win._on_track_drag(_Ev(x + 2, y + 40))       # mostly down
    assert win._drag["mode"] == "vzoom" and win._range is None
    assert win.insert == pytest.approx(1.5, abs=0.02)
    assert win.pps > base
    assert win._x(win.insert) == pytest.approx(x, abs=1)   # zoomed about it
    win._on_track_release(_Ev(x + 2, y + 40))
    assert win._range is None


def test_a_click_dragged_across_still_highlights(view):
    app, win = view
    base = win._min_pps() * 1.5
    win.pps, win.view0 = base, 0.0
    win.redraw()
    app.update()
    y0, y1 = win._track_box()
    y = y0 + 20
    x = win._x(1.0)
    win._on_track_press(_Ev(x, y))
    win._on_track_drag(_Ev(x + 80, y + 10))
    win._on_track_release(_Ev(x + 80, y + 10))
    assert win._range is not None and win.pps == base


# ── Shift+Delete: taken out, the time left ───────────────────────────────

def _starts(win):
    return [round(c.start, 4) for c in win.clips]


def test_shift_delete_inside_a_pull_leaves_silence_and_moves_nothing(view):
    app, win = view
    before = (_starts(win), round(win._clip(1).end, 4))
    win._range = (1, 1.0, 1.5)                    # 11.0-11.5 s of the read
    _key(app, win.cv, "Delete", SHIFT)
    win.refresh()
    assert [tuple(s) for s in _res(app, 1)["segments"]] == [(10.0, 11.0), (11.5, 14.0)]
    assert _res(app, 1)["seg_gaps"] == [pytest.approx(0.5)]
    assert (_starts(win), round(win._clip(1).end, 4)) == before


def test_shift_delete_off_a_pulls_end_grows_the_gap_after(view):
    app, win = view
    before = _starts(win)
    win._range = (1, 3.4, 4.0)
    win.delete_range(lift=True)
    win.refresh()
    assert [tuple(s) for s in _res(app, 1)["segments"]] == [(10.0, 13.4)]
    assert _res(app, 1)["gap_after_s"] == pytest.approx(0.5 + 0.6)
    assert _starts(win) == before


def test_shift_delete_off_a_pulls_start_grows_the_gap_in_front(view):
    app, win = view
    s2 = win._clip(2).start
    before_end = win._clip(2).end
    win._range = (2, s2, s2 + 0.5)
    win.delete_range(lift=True)
    win.refresh()
    assert [tuple(s) for s in _res(app, 2)["segments"]] == [(20.5, 22.0)]
    assert _res(app, 1)["gap_after_s"] == pytest.approx(0.5 + 0.5)
    assert win._clip(2).end == pytest.approx(before_end)


def test_plain_delete_still_closes_up(view):
    app, win = view
    s2 = win._clip(2).start
    win._range = (1, 1.0, 1.5)
    _key(app, win.cv, "Delete")
    win.refresh()
    assert win._clip(2).start == pytest.approx(s2 - 0.5)


# ── Caps Lock doesn't change what a key does ─────────────────────────────

class _KeyEv(object):
    def __init__(self, widget, keysym, state):
        self.widget, self.keysym, self.state = widget, keysym, state


CAPS = 0x0002


def test_caps_lock_doesnt_turn_a_key_into_its_shifted_one(view, monkeypatch):
    app, win = view
    monkeypatch.setitem(assembly_view._MONITOR, "skip_cuts", True)
    win.set_insert(1.5)
    # B with Caps Lock on arrives as "B": still a split, not Shift+B.
    win._on_key(_KeyEv(win.cv, "B", CAPS))
    assert [tuple(s) for s in _res(app, 1)["segments"]] == [(10.0, 11.5), (11.5, 14.0)]
    # S with Caps Lock: plain S (nothing), not Shift+S (Skip edits).
    win._on_key(_KeyEv(win.cv, "S", CAPS))
    assert assembly_view._MONITOR["skip_cuts"] is True
    # Shift with Caps Lock arrives as "s": still Shift+S.
    win._on_key(_KeyEv(win.cv, "s", CAPS | SHIFT))
    assert assembly_view._MONITOR["skip_cuts"] is False


def test_a_cut_keeps_its_crossfade_with_the_surrounding_audio_shown(view):
    app, win = view
    app.fade_var.set(0.200)                       # the crossfade default
    _cut_one(app, win)                            # 10-11 | cut | 12-14
    shut = win._clip(1).shapes
    assert shut[0][1][0] == "x" and shut[1][0][0] == "x"
    win.toggle_excised()
    opened = win._clip(1).shapes
    assert opened == shut                         # the same crossfade, not two fades
    # Drawn on each side of the dimmed cut audio: the outgoing slope runs on
    # past its region into the cut audio, the incoming one starts in it.
    marks = {(k, side): m for k, side, *m in win._fade_marks(win._clip(1))}
    edge_out, ht_out, secs_out, cross_out, _own = marks[(0, "out")]
    assert cross_out and secs_out == pytest.approx(0.2)
    # What plays: the finished cut, with the crossfade in it.
    x = [p for p in win.cut_audio._index[1] if p[2] == 1]
    assert len(x) == 2 and x[0][5] > 0 and x[1][4] > 0  # fade out / in ramps
    assert x[1][0] < x[0][0] + x[0][1]                  # the two overlap
