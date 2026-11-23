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
    half = int(0.100 * sr) // 2
    assert got == [("SourceClip", 1 * sr + half),                # no fade in
                   ("Transition", 2 * half),                     # its own crossfade
                   ("SourceClip", int(1.5 * sr) + half),
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


def test_assembly_crossfades_a_butt_join_like_the_aaf():
    # Two pieces butt at 1.0 s; 200 ms crossfade: 100 ms either side.
    audio, lead = _audio([(0, 200), (200, 0)],
                         [(0, 1000, 1000), (1000, 1000, 3000)],
                         [(1.0, 2.0), (3.0, 4.0)])
    out = audio.read(lead, 2000)
    # Constant tape either side: the two sides sum to it all the way across.
    assert np.allclose(out[:2000], 0.5, atol=0.005)
    # ...and each side really is playing past the cut: outgoing alone.
    from assembly_view import _AssemblyAudio  # noqa: F401
    starts, pieces = audio._index
    assert [(p[1], p[4], p[5]) for p in pieces] == [(1100, 0, 200), (1100, 200, 0)]
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
    assert _fade_of(app, 1, "out") == [pytest.approx(0.3)]
    assert _fade_of(app, 1, "in") == [pytest.approx(0.3)]
    c = win._clip(1)
    assert c.shapes[0][1] == ("x", pytest.approx(0.15))
    assert c.shapes[1][0] == ("x", pytest.approx(0.15))


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


def test_dragging_a_crossfade_handle_sets_both_sides(view):  # noqa: F811
    app, win = view
    _res(app, 1)["segments"] = [(10.0, 11.0), (12.0, 14.0)]
    app.fade_var.set(0.100)
    win.refresh()
    win.restore_view(100.0, 0.0, 0.0)
    win.redraw()
    y0, _y1 = win._track_box()
    cut = win._x(1.0)
    hx = win._x(1.0 + 0.05)                     # half of 100 ms, right side
    kind, hc, hk = win._hit(hx, y0 + 6)
    assert (kind, hk) == ("fade", (1, "in"))
    win._on_track_press(_Ev(hx, y0 + 6))
    win._on_track_drag(_Ev(cut + 0.2 * win.pps, y0 + 6))
    win._on_track_release(_Ev(cut + 0.2 * win.pps, y0 + 6))
    assert _fade_of(app, 1, "in") == [pytest.approx(0.4, abs=0.03)]
    assert _fade_of(app, 1, "out") == [pytest.approx(0.4, abs=0.03)]
