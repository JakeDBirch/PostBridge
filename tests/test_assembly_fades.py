"""Fades set in the Assembly view: the cut's default and a cut's own, as
they are kept on a pull, laid out, heard in the Assembly's playback, and
written to the AAF.
"""
import os
import sys
import wave

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engines  # noqa: E402

from test_assembly_edit import view, _res  # noqa: E402,F401  (the fixture)


# ── A pull's own fades ───────────────────────────────────────────────────────

def test_an_edge_takes_the_default_until_it_has_its_own():
    r = {"order": 1}
    assert engines.edge_fade(r, "in", 10.0, 0.010) == 0.010
    assert engines.set_edge_fade(r, "in", 10.0, 0.050) is True
    assert engines.edge_fade(r, "in", 10.0, 0.010) == 0.050
    assert engines.edge_fade(r, "out", 10.0, 0.010) == 0.010     # the other side
    assert engines.edge_fade(r, "in", 20.0, 0.010) == 0.010      # another edge
    # Set to nothing is still its own: a hard cut, whatever the default.
    engines.set_edge_fade(r, "in", 10.0, 0.0)
    assert engines.edge_fade(r, "in", 10.0, 0.010) == 0.0
    # Handed back to the default, and nothing is left on the pull.
    assert engines.set_edge_fade(r, "in", 10.0, None) is True
    assert "fades" not in r
    assert engines.set_edge_fade(r, "in", 10.0, None) is False


def test_a_fade_rides_with_its_edge_through_a_trim():
    r = {"order": 1}
    engines.set_edge_fade(r, "out", 14.0, 0.080)
    assert engines.edge_fade(r, "out", 13.9, 0.010) == 0.080     # trimmed a little
    assert engines.edge_fade(r, "out", 14.0 + engines.FADE_PIN_S + 0.05,
                             0.010) == 0.010                     # a different cut
    # Setting it again where the edge now is moves it; there is still one.
    engines.set_edge_fade(r, "out", 13.9, 0.040)
    assert r["fades"] == [{"side": "out", "src": 13.9, "len": 0.04}]


def test_layout_gives_every_piece_its_fades():
    sr = 48000
    res = [{"order": 1, "token": "GUEST", "status": "ok", "part_index": 0,
            "segments": [[1.0, 2.0], [2.5, 4.0]], "gap_after": True}]
    engines.set_edge_fade(res[0], "in", 2.5, 0.100)
    placed, _s, _p = engines.layout_timeline(
        res, 0.5, sr, {"GUEST": ["guest.wav"]}, fade_secs=0.010)
    assert placed[0]["fades"] == [(480, 480), (4800, 480)]
    placed, _s, _p = engines.layout_timeline(res, 0.5, sr, {"GUEST": ["guest.wav"]})
    assert placed[0]["fades"] == [(0, 0), (4800, 0)]             # no default


def test_the_aaf_carries_a_cuts_own_fade(tmp_path):
    aaf2 = pytest.importorskip("aaf2")
    sr = 48000
    wav = str(tmp_path / "guest.wav")
    with wave.open(wav, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(b"\x00\x00" * sr * 10)
    res = [{"order": 1, "token": "GUEST", "status": "ok", "part_index": 0,
            "segments": [[1.0, 2.0], [2.5, 4.0]], "gap_after": True}]
    engines.set_edge_fade(res[0], "out", 2.0, 0.100)             # the cut inside
    engines.set_edge_fade(res[0], "in", 1.0, 0.0)                # a hard start
    out = str(tmp_path / "cut.aaf")
    engines.build_aaf(res, {"GUEST": [wav]}, {}, [], "cut", 0.5, seq_sr=sr,
                      out_path=out, fade_secs=0.010)
    with aaf2.open(out, "r") as f:
        comp = next(f.content.toplevel())
        slot = next(sl for sl in comp.slots if sl.name == "GUEST")
        got = [(type(c).__name__, c.length) for c in slot.segment.components]
    # The outgoing edge's own 100 ms sets the crossfade's end after the cut
    # (half of it); the incoming edge's default 10 ms its start before.
    post, pre = int(0.100 * sr) // 2, int(0.010 * sr) // 2
    assert got == [("SourceClip", 1 * sr + post),                # no fade in
                   ("Transition", pre + post),                   # its own crossfade
                   ("SourceClip", int(1.5 * sr) + pre),
                   ("Transition", int(0.010 * sr)),              # the default out
                   ("Filler", int(0.010 * sr))]


# ── What the Assembly plays ──────────────────────────────────────────────────

def _audio(fades, pieces, segments):
    from assembly_view import _AssemblyAudio, _Clip, LEAD_S
    sr = 1000
    entry = {"start": 0, "end": pieces[-1][0] + pieces[-1][1], "gap": 0,
             "segments": segments, "pieces": pieces, "fades": fades}
    c = _Clip(entry, {"order": 7}, sr, "int", False, "x.wav")
    audio = _AssemblyAudio(sr)
    audio.set_clips([c], c.end)
    audio.put(7, "x.wav", 0.0, np.full(10 * sr, 16384, dtype=np.int16))  # 0.5
    return audio, int(LEAD_S * sr)


def test_assembly_plays_a_fade_in_and_out_around_silence():
    # One second of tape, 100 ms in and 200 ms out.
    audio, lead = _audio([(100, 200)], [(0, 1000, 1000)], [(1.0, 2.0)])
    out = audio.read(lead, 1000)
    assert out[0] < 0.01 and abs(out[50] - 0.25) < 0.01          # half way up
    assert np.allclose(out[100:800], 0.5)
    assert abs(out[900] - 0.25) < 0.01 and out[999] < 0.01       # half way down


def test_a_crossfade_over_a_split_ramps_straight():
    # A split: the two pieces are one stretch of tape (2.0 s on into 2.0 s).
    # Both sides are the same sound, so a straight crossfade holds it level.
    audio, lead = _audio([(0, 200), (200, 0)],
                         [(0, 1000, 1000), (1000, 1000, 2000)],
                         [(1.0, 2.0), (2.0, 3.0)])
    out = audio.read(lead, 2000)
    assert np.allclose(out[:2000], 0.5, atol=0.005)


def test_assembly_crossfades_a_butt_join_like_the_aaf():
    # A real cut: 1.0-2.0 s, then 3.0-4.0 s.  200 ms crossfade, 100 ms either
    # side, at equal power: unrelated sounds either side keep their level
    # (each side at sin 45 deg in the middle, not half).
    audio, lead = _audio([(0, 200), (200, 0)],
                         [(0, 1000, 1000), (1000, 1000, 3000)],
                         [(1.0, 2.0), (3.0, 4.0)])
    out = audio.read(lead, 2000)
    assert np.allclose(out[:880], 0.5, atol=0.005)
    assert np.allclose(out[1120:2000], 0.5, atol=0.005)
    # Constant tape both sides: the sum peaks at 2 x 0.5 x sin(45 deg) mid-way.
    assert out[1000] == pytest.approx(0.5 * 2 * np.sin(np.pi / 4), abs=0.01)
    # ...and each side really is playing past the cut: outgoing alone.
    from assembly_view import _AssemblyAudio  # noqa: F401
    starts, pieces = audio._index
    assert [(p[1], p[4], p[5]) for p in pieces] == [(1100, 0, 200), (1100, 200, 0)]
    assert [(p[6], p[7]) for p in pieces] == [(False, True), (True, False)]
    assert pieces[1][0] - lead == 900                            # starts early


def test_no_fades_with_the_surrounding_audio_heard():
    from assembly_view import _AssemblyAudio, _Clip
    sr = 1000
    entry = {"start": 0, "end": 1000, "gap": 0, "segments": [(1.0, 2.0)],
             "pieces": [(0, 1000, 1000)], "fades": [(100, 100)]}
    c = _Clip(entry, {"order": 7}, sr, "int", False, "x.wav")
    audio = _AssemblyAudio(sr)
    audio.set_clips([c], c.end, ghosts=True)
    assert all(p[4] == 0 and p[5] == 0 for p in audio._index[1])


# ── Setting them in the window ───────────────────────────────────────────────

def _type(box, text):
    box.delete(0, "end")
    box.insert(0, text)


def test_default_fade_is_set_in_the_assembly(view):  # noqa: F811
    app, win = view
    assert win._clip(1).fades == [(0.0, 0.0)]
    _type(win._fade_box, "20")
    win._fade_from_box()
    assert app.fade_var.get() == pytest.approx(0.020)
    assert win._clip(1).fades == [pytest.approx((0.020, 0.020))]
    app._s4_undo()                                               # one undo step
    win.refresh()
    assert app.fade_var.get() == 0.0 and win._fade_box.get() == "0"
    assert win._clip(1).fades == [(0.0, 0.0)]


def test_a_cut_gets_its_own_fade_at_the_insertion_point(view):  # noqa: F811
    app, win = view
    app.fade_var.set(0.010)
    win.refresh()
    c = win._clip(1)
    win.seek(c.end - 0.05)                                       # by pull 1's out
    at, edges = win._cut_edges()
    assert at == pytest.approx(c.end) and edges == [(1, "out", 14.0)]
    win._show_cut_fade()
    assert win._cut_fade_box.get() == "10" and "default" in win._cut_fade_lbl.cget("text")
    _type(win._cut_fade_box, "150")
    win._cut_fade_from_box()
    assert _res(app, 1)["fades"] == [{"side": "out", "src": 14.0, "len": 0.15}]
    assert win._clip(1).fades == [pytest.approx((0.010, 0.150))]
    assert "its own" in win._cut_fade_lbl.cget("text")
    # The default changes; this cut keeps its own.
    app._assembly_set_default_fade(0.030)
    win.refresh()
    assert win._clip(1).fades == [pytest.approx((0.030, 0.150))]
    # Back to the default.
    win.seek(win._clip(1).end - 0.05)
    win._cut_fade_set(None)
    assert "fades" not in _res(app, 1)
    assert win._clip(1).fades == [pytest.approx((0.030, 0.030))]
    # Undo brings its own fade back.
    app._s4_undo()
    win.refresh()
    assert _res(app, 1)["fades"] == [{"side": "out", "src": 14.0, "len": 0.15}]


def test_a_cut_inside_a_pull_sets_both_its_sides(view):  # noqa: F811
    app, win = view
    _res(app, 1)["segments"] = [(10.0, 11.0), (12.0, 14.0)]
    win.refresh()
    c = win._clip(1)
    win.seek(c.pieces[1][0])                                     # on the cut
    _at, edges = win._cut_edges()
    assert sorted(edges) == [(1, "in", 12.0), (1, "out", 11.0)]
    _type(win._cut_fade_box, "60")
    win._cut_fade_from_box()
    assert win._clip(1).fades == [pytest.approx((0.0, 0.060)),
                                  pytest.approx((0.060, 0.0))]


# ── F, and the handles on the track ──────────────────────────────────────────

from test_assembly_edit import _Ev, _key  # noqa: E402


def _fade_of(app, order, side):
    return [m["len"] for m in _res(app, order).get("fades", ()) if m["side"] == side]


def test_f_fades_out_a_region_end_under_the_highlight(view):  # noqa: F811
    app, win = view
    win._range = (1, 3.5, 4.2)                  # pull 1 ends at 4.0
    _key(app, win.cv, "f")
    assert _fade_of(app, 1, "out") == [pytest.approx(0.5)]
    assert _fade_of(app, 1, "in") == []
    assert win._clip(1).shapes[0][1] == ("f", pytest.approx(0.5))


def test_f_across_a_butt_cut_makes_a_crossfade_the_highlights_length(view):  # noqa: F811
    app, win = view
    _res(app, 1)["segments"] = [(10.0, 11.0), (12.0, 14.0)]
    win.refresh()
    win._range = (1, 0.9, 1.2)                  # the cut is at 1.0
    win.apply_fade()
    # As highlighted: 0.1 s before the cut and 0.2 s after.
    c = win._clip(1)
    assert c.shapes[0][1] == ("x", pytest.approx(0.1), pytest.approx(0.2))
    assert c.shapes[1][0] == ("x", pytest.approx(0.1), pytest.approx(0.2))
    assert _fade_of(app, 1, "in") == [pytest.approx(0.2)]      # twice the part before
    assert _fade_of(app, 1, "out") == [pytest.approx(0.4)]     # twice the part after


def test_f_across_a_gap_fades_out_and_in_as_one_undo(view):  # noqa: F811
    app, win = view
    win._range = (1, 3.8, 4.8)                  # pull 1 ends 4.0, pull 2 starts 4.5
    win.apply_fade()
    assert _fade_of(app, 1, "out") == [pytest.approx(0.2)]
    assert _fade_of(app, 2, "in") == [pytest.approx(0.3)]
    app._s4_undo()
    assert "fades" not in _res(app, 1) and "fades" not in _res(app, 2)


def test_f_with_nothing_to_fade_changes_nothing(view):  # noqa: F811
    app, win = view
    undo = len(app._s4_undo_stack)
    win._range = None
    win.apply_fade()
    win._range = (1, 1.0, 2.0)                  # inside one region
    win.apply_fade()
    assert "fades" not in _res(app, 1) and len(app._s4_undo_stack) == undo


def test_a_fade_handle_sits_on_each_top_corner_and_drags(view):  # noqa: F811
    app, win = view
    win.restore_view(100.0, 0.0, 0.0) if hasattr(win, "restore_view") else None
    win.redraw()
    y0, _y1 = win._track_box()
    c = win._clip(1)
    x_in = win._x(c.start)
    kind, hc, hk = win._hit(x_in + 4, y0 + 6)
    assert (kind, hc.order, hk) == ("fade", 1, (0, "in"))
    # Further down the same edge is still the trim.
    assert win._hit(x_in + 2, y0 + 60)[0] == "in"
    # Drag the corner handle half a second into the region.
    win._on_track_press(_Ev(x_in + 4, y0 + 6))
    win._on_track_drag(_Ev(x_in + 0.5 * win.pps, y0 + 6))
    assert win._clip(1).shapes[0][0] == ("f", pytest.approx(0.5, abs=0.02))  # live
    assert "fades" not in _res(app, 1)                                    # not yet
    win._on_track_release(_Ev(x_in + 0.5 * win.pps, y0 + 6))
    assert _fade_of(app, 1, "in") == [pytest.approx(0.5, abs=0.02)]
    # The handle is now at the fade's end; dragging it back to the edge
    # takes the fade away again (its own: none).
    hx = win._x(c.start + _fade_of(app, 1, "in")[0])
    assert win._hit(hx, y0 + 6)[0] == "fade"
    win._on_track_press(_Ev(hx, y0 + 6))
    win._on_track_drag(_Ev(x_in - 30, y0 + 6))
    win._on_track_release(_Ev(x_in - 30, y0 + 6))
    assert _fade_of(app, 1, "in") == [0.0]


def test_dragging_a_crossfade_handle_moves_only_its_end(view):  # noqa: F811
    app, win = view
    _res(app, 1)["segments"] = [(10.0, 11.0), (12.0, 14.0)]
    app.fade_var.set(0.100)
    win.refresh()
    win.restore_view(100.0, 0.0, 0.0)
    win.redraw()
    y0, _y1 = win._track_box()
    cut = win._x(1.0)
    hx = win._x(1.0 + 0.05)                     # its end after the cut
    kind, hc, hk = win._hit(hx, y0 + 6)
    assert (kind, hk) == ("fade", (1, "in"))
    win._on_track_press(_Ev(hx, y0 + 6))
    win._on_track_drag(_Ev(cut + 0.2 * win.pps, y0 + 6))
    win._on_track_release(_Ev(cut + 0.2 * win.pps, y0 + 6))
    sh = win._clip(1).shapes[0][1]
    assert sh[0] == "x"
    assert sh[2] == pytest.approx(0.2, abs=0.03)        # its end: 0.2 s after
    assert sh[1] == pytest.approx(0.05, abs=0.005)      # the start: as it was


def test_a_crossfade_between_two_pulls_is_a_whole_x_with_no_handle_boxes(view):  # noqa: F811
    app, win = view
    _res(app, 1)["gap_after"], _res(app, 1)["gap_after_s"] = True, 0.0
    _res(app, 1)["segments"] = [(10.0, 14.0)]
    _res(app, 2)["segments"] = [(14.0, 16.0)]   # butts on: one stretch of tape
    app.fade_var.set(0.400)
    win.refresh()
    win.restore_view(100.0, 0.0, 3.0)
    win.redraw()
    app.update()
    cv = win.cv
    fades = cv.find_withtag("fade")
    assert fades and all(cv.type(i) in ("line", "polygon") for i in fades)
    # Both slopes span the whole crossfade, 3.8 to 4.2 s, top to bottom.
    cut = win._x(4.0)
    lines = [cv.coords(i) for i in fades if cv.type(i) == "line"
             and min(cv.coords(i)[0::2]) < cut < max(cv.coords(i)[0::2])]
    assert len(lines) == 2
    for xy in lines:
        assert sorted(round(v) for v in xy[0::2]) == [round(win._x(3.8)),
                                                      round(win._x(4.2))]
        assert abs(xy[1] - xy[3]) > 100          # top to bottom: an X, not a Y
    # ...drawn over both pulls' ground, so pull 2's doesn't hide half of one.
    x2 = win._x(win._clip(2).start)
    ground2 = [i for i in cv.find_all() if cv.type(i) == "rectangle"
               and abs(cv.coords(i)[0] - x2) < 1 and cv.itemcget(i, "fill")]
    assert ground2 and min(fades) > max(ground2)


def test_a_fade_handle_shows_as_the_pointer(view):  # noqa: F811
    app, win = view
    win.restore_view(100.0, 0.0, 0.0)
    win.redraw()
    app.update()
    y0, _y1 = win._track_box()
    x_in = win._x(win._clip(1).start)
    win._on_motion(_Ev(x_in + 4, y0 + 6))
    assert str(win.cv.cget("cursor")) == "size_ne_sw"
    x_out = win._x(win._clip(1).end)
    win._on_motion(_Ev(x_out - 4, y0 + 6))
    assert str(win.cv.cget("cursor")) == "size_nw_se"


# ── A fade's handle doesn't flip over; Heal seals a fade over its join ─────

def test_a_handle_dragged_past_its_edge_leaves_no_fade(view):  # noqa: F811
    app, win = view
    c = win._clip(1)
    xe = win._x(c.start)
    d = {"edge": c.start, "side": "in", "cross": False, "room": 4.0}
    assert win._fade_dragged(d, xe + 0.3 * win.pps) == pytest.approx(0.3)
    assert win._fade_dragged(d, xe - 0.3 * win.pps) == 0.0      # not 0.3 again
    d = {"edge": 2.0, "side": "in", "cross": True, "room": 2.0}
    xe = win._x(2.0)
    assert win._fade_dragged(d, xe + 0.1 * win.pps) == pytest.approx(0.2)
    assert win._fade_dragged(d, xe - 0.1 * win.pps) == 0.0


def test_heal_seals_a_crossfaded_split_and_its_fades_go(view):  # noqa: F811
    app, win = view
    _res(app, 1)["segments"] = [(10.0, 12.0), (12.0, 14.0)]
    win.refresh()
    win._range = (1, 1.8, 2.2)                  # a 400 ms crossfade at 2.0
    win.apply_fade()
    assert _res(app, 1).get("fades")
    win._range = None
    win.pps = 400.0                             # 80 px either side of the cut
    win.set_insert(2.15)                        # inside the crossfade, 60 px off
    win.heal()
    assert [tuple(s) for s in _res(app, 1)["segments"]] == [(10.0, 14.0)]
    assert not _res(app, 1).get("fades")
    app._s4_undo()                              # one step: both come back
    assert [tuple(s) for s in _res(app, 1)["segments"]] == [(10.0, 12.0), (12.0, 14.0)]
    assert _res(app, 1).get("fades")


def test_heal_merging_two_pulls_takes_the_fades_at_the_join_away(view):  # noqa: F811
    import main
    app, win = view
    app._assembly_merge_pulls = main.App._assembly_merge_pulls.__get__(app)
    app._assembly_ignore = lambda order: next(
        e for e in app._rv if e["res"]["order"] == order)["skip_var"].set(True)
    _res(app, 1)["gap_after"], _res(app, 1)["gap_after_s"] = True, 0.0
    _res(app, 1)["segments"] = [(10.0, 14.0)]
    _res(app, 2)["segments"] = [(14.0, 16.0)]
    _res(app, 1)["fades"] = [{"side": "out", "src": 14.0, "len": 0.3},
                             {"side": "in", "src": 10.0, "len": 0.1}]
    _res(app, 2)["fades"] = [{"side": "in", "src": 14.0, "len": 0.3}]
    win.refresh()
    win.set_insert(4.0)
    win.heal()
    assert [tuple(s) for s in _res(app, 1)["segments"]] == [(10.0, 16.0)]
    assert _res(app, 1)["fades"] == [{"side": "in", "src": 10.0, "len": 0.1}]


def test_a_fades_end_is_marked_top_to_bottom(view):  # noqa: F811
    app, win = view
    _res(app, 1)["segments"] = [(10.0, 12.0), (12.0, 14.0)]
    app.fade_var.set(0.400)                     # crossfade at 2.0; fade in at 0
    win.refresh()
    win.restore_view(100.0, -0.5, 0.0)
    win.redraw()
    app.update()
    cv = win.cv
    y0, y1 = win._track_box()
    ends = sorted(round(cv.coords(i)[0]) for i in cv.find_withtag("fade_end"))
    for t in (0.4, 1.8, 2.2):                   # the fade in's end; the X's two
        assert round(win._x(t)) in ends
    for i in cv.find_withtag("fade_end"):
        x0, ya, x1, yb = cv.coords(i)
        assert x0 == x1 and yb - ya > (y1 - y0) / 2      # a vertical, top to bottom


def test_the_waveform_over_a_fade_is_drawn_as_it_plays(view):  # noqa: F811
    app, win = view
    app.fade_var.set(0.500)                     # 500 ms fade in at pull 1's start
    win.refresh()
    sr = win.audio.sr
    loud = np.full(10 * sr, 20000, dtype=np.int16)
    win.audio.put(1, win._clip(1).play_path, 5.0, loud)
    win._env[1] = (5.0, np.full(10 * 400, 20000 / 32768.0, np.float32))
    win.restore_view(400.0, -0.1, 0.0)
    win.redraw()
    app.update()
    cv = win.cv
    heard = cv.find_withtag("fade_heard")
    polys = [i for i in heard if cv.type(i) == "polygon"]
    assert polys
    xy = cv.coords(polys[0])
    pts = list(zip(xy[0::2], xy[1::2]))
    top_edge = pts[:len(pts) // 2]
    y0, y1 = win._track_box()
    mid = (y0 + win._ls + 8 + y1 - win._ls - 8) / 2.0
    # Near nothing as the fade begins, full height by its end.
    start = min(top_edge, key=lambda p: abs(p[0] - win._x(0.02)))
    end = min(top_edge, key=lambda p: abs(p[0] - win._x(0.48)))
    assert mid - start[1] < 0.15 * (mid - end[1])


# ── F with nothing highlighted: the insertion point to the nearer edge ─────

def test_f_without_a_highlight_fades_in_from_the_start_to_the_cursor(view):  # noqa: F811
    app, win = view
    win._range = None
    win.set_insert(0.3)                         # pull 1 is 0-4 on the track
    _key(app, win.cv, "f")
    assert _fade_of(app, 1, "in") == [pytest.approx(0.3)]
    assert win._range is None


def test_f_without_a_highlight_fades_out_from_the_cursor_to_the_end(view):  # noqa: F811
    app, win = view
    win._range = None
    win.set_insert(3.6)
    win.apply_fade()
    assert _fade_of(app, 1, "out") == [pytest.approx(0.4)]


def test_f_without_a_highlight_by_a_butt_cut_crossfades_either_side(view):  # noqa: F811
    app, win = view
    _res(app, 1)["segments"] = [(10.0, 12.0), (12.0, 14.0)]
    win.refresh()
    win._range = None
    win.set_insert(1.8)                         # 0.2 s before the cut at 2.0
    win.apply_fade()
    assert _fade_of(app, 1, "out") == [pytest.approx(0.4)]
    assert _fade_of(app, 1, "in") == [pytest.approx(0.4)]
    assert win._clip(1).shapes[0][1] == ("x", pytest.approx(0.2), pytest.approx(0.2))


# ── Separate defaults: fade in, fade out, crossfade ──────────────────────

def test_the_three_defaults_go_where_they_belong():
    sr = 1000
    res = [{"order": 1, "token": "GUEST", "status": "ok", "part_index": 0,
            "segments": [[1.0, 2.0], [2.5, 4.0]], "gap_after": True},
           {"order": 2, "token": "GUEST", "status": "ok", "part_index": 0,
            "segments": [[6.0, 7.0]], "gap_after": True}]
    placed, _s, _p = engines.layout_timeline(
        res, 0.5, sr, {"GUEST": ["guest.wav"]}, fade_secs=(0.010, 0.020, 0.050))
    # Pull 1's two pieces butt: the crossfade's; its outer edges and pull 2's,
    # beside silence, the fade in's and fade out's.
    assert placed[0]["fades"] == [(10, 50), (50, 20)]
    assert placed[1]["fades"] == [(10, 20)]


def test_an_edge_of_its_own_keeps_it_at_a_butt_join():
    sr = 1000
    res = [{"order": 1, "token": "GUEST", "status": "ok", "part_index": 0,
            "segments": [[1.0, 2.0], [2.5, 4.0]], "gap_after": True}]
    engines.set_edge_fade(res[0], "in", 2.5, 0.100)
    placed, _s, _p = engines.layout_timeline(
        res, 0.5, sr, {"GUEST": ["guest.wav"]}, fade_secs=(0.010, 0.020, 0.050))
    assert placed[0]["fades"] == [(10, 20), (100, 20)]   # not the crossfade's


def test_the_assembly_sets_each_default_on_its_own(view):  # noqa: F811
    from test_assembly_edit import _Var
    app, win = view
    app.fade_in_var, app.fade_out_var = _Var(0.0), _Var(0.0)
    import main
    app._assembly_set_default_fade = main.App._assembly_set_default_fade.__get__(app)
    win._fade_in_box.delete(0, "end")
    win._fade_in_box.insert(0, "30")
    win._fade_from_box(kind="in")
    win._fade_out_box.delete(0, "end")
    win._fade_out_box.insert(0, "60")
    win._fade_from_box(kind="out")
    assert app.fade_in_var.get() == pytest.approx(0.030)
    assert app.fade_out_var.get() == pytest.approx(0.060)
    assert win._default_fades() == (pytest.approx(0.030), pytest.approx(0.060),
                                    app.fade_var.get())
    c = win._clip(1)
    assert c.fades[0] == (pytest.approx(0.030), pytest.approx(0.060))


def test_a_highlighted_fade_is_as_long_as_the_highlight(view):  # noqa: F811
    app, win = view
    win._range = (1, -0.2, 1.5)                 # 1.5 s into a 4 s pull: not capped
    win.apply_fade()
    assert _fade_of(app, 1, "in") == [pytest.approx(1.5)]
    c = win._clip(1)
    assert c.shapes[0][0] == ("f", pytest.approx(1.5))
    win._range = (1, 2.2, 4.3)                  # 1.8 s off its end
    win.apply_fade()
    assert _fade_of(app, 1, "out") == [pytest.approx(1.8)]
