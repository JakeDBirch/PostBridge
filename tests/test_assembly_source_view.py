"""The Assembly window's source view: the waveform editor built into it,
Ctrl+F between it and the track, the playhead kept on the same audio, edits
saved live through Step 4, and the one set of keys for both views.

Built on test_assembly_edit's stand-in app.  Needs tkinter and a display;
skips otherwise.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

tk = pytest.importorskip("tkinter")

from test_assembly_edit import view, _key, _res, WAV  # noqa: E402,F401
import assembly_view  # noqa: E402

SHIFT = 0x0001
CTRL = 0x0004


def _segs(app, order):
    return [tuple(round(v, 4) for v in s) for s in _res(app, order)["segments"]]


def test_ctrl_f_shows_the_recording_behind_the_insertion_point(view):
    app, win = view
    win.set_insert(1.0)                      # pull 1: source 10-14 at 0-4
    _key(app, win.cv, "f", CTRL)
    assert win.view == "source" and win._src_order == 1
    assert win.src._audio_path == WAV
    assert win.src._playhead_s == pytest.approx(11.0, abs=1e-3)
    assert win.src._edited_segments() == [(10.0, 14.0)]
    app.update()
    assert win._src_frame.winfo_ismapped() and not win._track_frame.winfo_ismapped()

    # Back: the insertion point lands where the editor's playhead was.
    win.src._playhead_s = 12.5
    _key(app, win.src._cv, "Return")
    assert win.view == "cut"
    assert win.insert == pytest.approx(2.5, abs=1e-3)
    app.update()
    assert win._track_frame.winfo_ismapped() and not win._src_frame.winfo_ismapped()


def test_enter_comes_back_too_and_the_selected_pull_wins(view):
    app, win = view
    win.select(2)
    _key(app, win.cv, "f", CTRL)
    assert win.view == "source" and win._src_order == 2
    assert win.src._playhead_s == pytest.approx(20.0)   # its first moment
    _key(app, win.src._cv, "Return")
    assert win.view == "cut" and win.insert == pytest.approx(win._clip(2).start)


def test_a_moment_the_cut_skips_lands_where_its_audio_resumes(view):
    app, win = view
    _res(app, 1)["segments"] = [(10.0, 11.0), (12.0, 14.0)]
    win.refresh()
    win.show_source(1)
    win.src._playhead_s = 11.5                 # inside the cut
    win.toggle_view()
    assert win.insert == pytest.approx(1.0, abs=1e-3)


def test_an_edit_in_the_source_view_is_one_step_four_step(view):
    app, win = view
    depth = len(app._s4_undo_stack)
    win.show_source(1)
    win.src._playhead_s = 11.0
    _key(app, win.src._cv, "i")                # SET IN at the playhead
    assert _segs(app, 1) == [(11.0, 14.0)]
    assert len(app._s4_undo_stack) == depth + 1
    assert app._rv[0]["accepted_flag"][0]      # saved as the track's edits are
    win.refresh()
    assert win._clip(1).segments == [(11.0, 14.0)]
    assert win.src._edited_segments() == [(11.0, 14.0)]

    _key(app, win.src._cv, "z", 0x0004)        # Ctrl+Z: Step 4's undo, here too
    win.refresh()
    assert _segs(app, 1) == [(10.0, 14.0)]
    assert win.src._edited_segments() == [(10.0, 14.0)]
    assert win.src._in_s == 10.0


def test_b_splits_and_c_cuts_in_both_views(view):
    app, win = view
    win.show_source(1)
    win.src._playhead_s = 12.0
    _key(app, win.src._cv, "b")
    assert _segs(app, 1) == [(10.0, 12.0), (12.0, 14.0)]
    win.src._playhead_s = 13.0
    _key(app, win.src._cv, "c")
    assert _segs(app, 1) == [(10.0, 12.0), (12.0, 13.0), (13.5, 14.0)]

    _key(app, win.src._cv, "Return")
    win.refresh()
    win.set_insert(win._clip(2).start + 0.5)
    _key(app, win.cv, "c")
    assert _segs(app, 2) == [(20.0, 20.5), (21.0, 22.0)]


def test_a_mark_dropped_in_the_source_view_is_on_the_track(view):
    app, win = view
    win.show_source(1)
    win.src._playhead_s = 11.2
    _key(app, win.src._cv, "m")
    assert _res(app, 1)["markers"] == [{"src": 11.2, "kind": "music_inflection",
                                        "name": ""}]
    win.refresh()
    assert [m["kind"] for m in win.markers] == ["music_inflection"]
    assert win.markers[0]["t"] == pytest.approx(1.2, abs=1e-3)
    # ...and one dropped on the track reaches the source view.
    win.toggle_view()
    win.set_insert(3.0)
    _key(app, win.cv, "m")
    win.refresh()
    assert [m["src"] for m in win.src._marks()] == [11.2, 13.0]


def test_the_arrows_step_pulls_in_the_source_view_too(view):
    app, win = view
    win.show_source(1)
    _key(app, win.src._cv, "Right")
    assert win.view == "source" and win._src_order == 2
    assert win.src._edited_segments() == [(20.0, 22.0)]
    _key(app, win.src._cv, "Left")
    assert win._src_order == 1


def test_shift_f_follows_the_playhead_and_alt_f_the_script(view):
    app, win = view
    follow, script = assembly_view._MONITOR["follow"], assembly_view._MONITOR["script"]
    _key(app, win.cv, "F", SHIFT)
    assert assembly_view._MONITOR["follow"] is (not follow)
    assert assembly_view._MONITOR["script"] is script     # not the script's now
    _key(app, win.cv, "F", SHIFT)
    assert assembly_view._MONITOR["follow"] is follow
    _key(app, win.cv, "s")                        # S is no longer a key
    assert assembly_view._MONITOR["follow"] is follow
    # Alt+F: the script follows the words, and nothing gets a fade.
    undo = len(app._s4_undo_stack)
    win.cv.focus_force()
    app.update()
    win.cv.event_generate("<Alt-f>", when="now")
    app.update()
    assert assembly_view._MONITOR["script"] is (not script)
    win.cv.event_generate("<Alt-f>", when="now")
    app.update()
    assert assembly_view._MONITOR["script"] is script
    assert len(app._s4_undo_stack) == undo
    _key(app, win.cv, "Return")                   # the surrounding audio
    assert assembly_view._MONITOR["script"] is script and win.excised


def test_space_plays_the_view_on_show(view, monkeypatch):
    app, win = view
    win.show_source(1)
    played = []
    monkeypatch.setattr(win.src, "_toggle_play", lambda: played.append(True))
    _key(app, win.src._cv, "space")
    assert played and not win._playing


def test_a_pull_that_leaves_the_cut_gives_way_to_it(view):
    app, win = view
    win.show_source(2)
    app.results = [r for r in app.results if r["order"] != 2]
    app._rv = app._rv[:1]
    win.refresh()
    assert win.view == "cut" and win._src_order is None


def test_the_same_pull_again_only_moves_the_playhead(view, monkeypatch):
    app, win = view
    win.show_source(1)
    loads = []
    monkeypatch.setattr(win.src, "load_clip",
                        lambda *a, **k: loads.append(a))
    win.toggle_view()
    win.set_insert(3.0)
    win.find_in_recording()
    assert not loads and win.src._playhead_s == pytest.approx(13.0, abs=1e-3)


def test_the_panels_name_the_keys(view):
    app, win = view

    def labels(w):
        out = []
        for c in w.winfo_children():
            if isinstance(c, tk.Label):
                out.append(c.cget("text"))
            out += labels(c)
        return out
    shown = labels(win._side)
    for want in ("Show surrounding audio", "Enter", "Find in recording",
                 "Ctrl+F", "Cut ½ s out", "C",
                 "Script follows words", "Shift+F", "Accept & next",
                 "Ctrl+Enter", "Skip edits", "Shift+S"):
        assert want in shown
    assert "Open in editor" not in shown


def test_closing_takes_the_source_view_with_it(view):
    app, win = view
    win.show_source(1)
    src = win.src
    win.close()
    assert not src._alive() and win.src is None


def test_adopting_a_match_from_another_file_reassigns_the_pull(view):
    """ADOPT in the source view's "all pulls" search: the pull takes the
    other token and recording with the match as its one segment, through
    the card's accept, as one undo step; the view then shows that file."""
    app, win = view
    win.show_source(1)
    seen, orig = {}, app._rv[0]["accept_fn"]
    app._rv[0]["accept_fn"] = lambda segs, **kw: (seen.update(kw), orig(segs))
    depth = len(app._s4_undo_stack)
    other = os.path.join(ROOT, "tests", "_no_such_other.wav")
    app._s4_editor_source = lambda r: (other if r["order"] == 1 else WAV, [])
    win.src._xtok_adopt("OTHER", other, 30.0, 33.0)
    assert seen == {"new_token": "OTHER", "new_audio_path": other}
    assert _segs(app, 1) == [(30.0, 33.0)]
    assert len(app._s4_undo_stack) == depth + 1
    win.refresh()
    assert win.src._audio_path == other
    assert win.src._edited_segments() == [(30.0, 33.0)]


# ── Skip cuts, Accept & next, and the script following the source view ───


def test_ctrl_enter_accepts_the_pull_and_shows_the_next(view):
    app, win = view
    win.show_source(1)
    depth = len(app._s4_undo_stack)
    _key(app, win.src._cv, "Return", CTRL)
    assert app._rv[0]["accepted_flag"][0]            # through the card's accept
    assert _segs(app, 1) == [(10.0, 14.0)]           # as it was
    assert len(app._s4_undo_stack) == depth + 1
    assert win.view == "source" and win._src_order == 2
    assert win.src._edited_segments() == [(20.0, 22.0)]


def test_accept_and_next_leaves_an_accepted_pull_alone(view):
    app, win = view
    _res(app, 1)["_s4_accepted"] = True
    win.show_source(1)
    depth = len(app._s4_undo_stack)
    win.accept_next()
    assert len(app._s4_undo_stack) == depth and win._src_order == 2


def test_accept_and_next_on_the_track_selects_the_next_pull(view):
    app, win = view
    win.select(1)
    _key(app, win.cv, "Return", CTRL)
    assert app._rv[0]["accepted_flag"][0]
    assert win.view == "cut" and win.sel_order == 2


def test_shift_s_switches_skip_edits_for_the_source_view(view):
    app, win = view
    was = assembly_view._MONITOR["skip_cuts"]
    win.show_source(1)
    assert win.src.skip_cuts is was
    _key(app, win.src._cv, "S", SHIFT)
    assert assembly_view._MONITOR["skip_cuts"] is (not was)
    assert win.src.skip_cuts is (not was) and win.skip_key.on is (not was)
    _key(app, win.src._cv, "S", SHIFT)
    assert win.src.skip_cuts is was


def test_the_script_follows_the_source_views_playhead(view):
    app, win = view
    c = win._clip(1)
    c.words = win._map_words(c)

    def lit():
        rng = win.text.tag_ranges("word")
        return win.text.get(*rng) if rng else None
    win.show_source(1)
    win.src.on_playhead(11.4)                  # source time: "ridge"
    assert lit() == "ridge"
    win.src.on_playhead(12.9)
    assert lit() == "dawn."
    win.src.on_playhead(30.0)                  # outside the pull: no word
    assert lit() is None
    win.src.on_playhead(11.4)
    win.src.on_playhead(None)                  # stopped
    assert lit() is None


def test_the_source_view_has_no_script_of_its_own(view):
    app, win = view
    win.show_source(1)
    assert not hasattr(win.src, "_script_text")
