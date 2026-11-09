"""Spotting marks in the waveform editor, in a real window.  The marks are the
pull's own res["markers"] -- the list the Assembly track edits and the AAF
exports -- read and written through get_markers / set_markers, as Step 4
passes them.  The audio is put in place by hand, as in test_match_review_view.

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


class _Ev(object):
    def __init__(self, x, y, state=0, widget=None):
        self.x, self.y, self.state, self.widget = x, y, state, widget
        self.x_root = self.y_root = 0


@pytest.fixture
def ed(monkeypatch):
    """(dialog, the pull's result, the writes it was sent)."""
    monkeypatch.setattr(match_review.MatchReviewDialog, "_start_extraction",
                        lambda self: None)
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip("Tk unavailable: {}".format(exc))
    root.withdraw()
    res = {"order": 3, "segments": [(50.0, 60.0)]}
    writes = []

    def _set(marks):
        writes.append(marks)
        if marks:
            res["markers"] = list(marks)
        else:
            res.pop("markers", None)
    d = match_review.MatchReviewDialog(
        root, "no_such_file.wav", [[50.0, 60.0]], title="NARRATOR",
        get_markers=lambda: res.get("markers"), set_markers=_set)
    settle = time.monotonic() + 0.2
    while time.monotonic() < settle:
        root.update()
    d._need_initial_zoom = False
    d._samples = np.zeros(d._sr * SECS, np.float32)
    d._ctx_start, d._ctx_dur = 0.0, float(SECS)
    d._spp, d._view_start = 100, d._sr * 48
    d._loaded_start_s, d._loaded_end_s = 0.0, float(SECS)
    d._draw()
    yield d, res, writes, root
    d._on_cancel()
    root.destroy()


def _key(root, widget, keysym, state=0):
    widget.focus_force()
    root.update()
    widget.event_generate("<KeyPress>", keysym=keysym, state=state, when="now")
    root.update()


def _flag(d, i):
    m = d._marks()[i]
    top, bot = d._mark_row(d._mark_lane(m))
    return _Ev(d._t_to_px(d._mark_t(m)) + 3, (top + bot) / 2.0, widget=d._cv)


def _drawn_labels(d):
    return [d._cv.itemcget(i, "text") for i in d._cv.find_withtag("mark")
            if d._cv.type(i) == "text"]


def test_m_drops_an_inflection_on_the_pull_at_the_playhead(ed):
    d, res, writes, root = ed
    d._playhead_s = 52.5
    _key(root, d._cv, "m")
    assert res["markers"] == [{"src": 52.5, "kind": "music_inflection",
                               "name": ""}]
    assert len(writes) == 1
    assert d._mark_rename is None             # an inflection isn't named
    assert "Inflection" in _drawn_labels(d)


def test_x_and_n_open_for_naming_and_enter_keeps_the_name(ed):
    d, res, writes, root = ed
    d._playhead_s = 55.0
    _key(root, d._cv, "x")
    assert d._mark_rename is not None
    entry = d._mark_rename[0]
    entry.insert(0, "rifle shot")
    _key(root, entry, "Return")
    assert d._mark_rename is None
    assert res["markers"][0]["name"] == "rifle shot"
    assert d._win.winfo_exists()              # not the window's accept
    d._playhead_s = 56.0
    _key(root, d._cv, "n")
    _key(root, d._mark_rename[0], "Escape")
    assert [m["kind"] for m in res["markers"]] == ["sfx", "note"]
    assert d._win.winfo_exists()              # not the window's close
    assert "rifle shot" in _drawn_labels(d)


def test_typing_in_a_field_drops_nothing(ed):
    d, res, _w, root = ed
    def _all(w):
        for c in w.winfo_children():
            yield c
            yield from _all(c)
    entries = [w for w in _all(d._win) if isinstance(w, tk.Entry)]
    _key(root, entries[-1], "m")
    assert "markers" not in res


def test_a_flag_drags_to_a_new_moment_as_one_undo_step(ed):
    d, res, writes, _root = ed
    res["markers"] = [{"src": 52.0, "kind": "sfx", "name": "door"}]
    d._draw()
    ev = _flag(d, 0)
    d._on_press(ev)
    assert d._sel_mark == 0 and d._drag == "mark"
    x = d._t_to_px(57.0)
    d._on_drag_motion(_Ev(x, ev.y, 0x0004))   # Ctrl: no snap
    assert res["markers"][0]["src"] == 52.0   # written once, on release
    d._on_release(_Ev(x, ev.y))
    assert res["markers"] == [{"src": pytest.approx(57.0, abs=0.02),
                               "kind": "sfx", "name": "door"}]
    assert len(writes) == 1
    d._undo_wf()
    assert res["markers"][0]["src"] == 52.0
    d._redo_wf()
    assert res["markers"][0]["src"] == pytest.approx(57.0, abs=0.02)


def test_a_click_on_a_flag_only_chooses_it(ed):
    d, res, writes, _root = ed
    res["markers"] = [{"src": 52.0, "kind": "sfx", "name": ""}]
    d._draw()
    ev = _flag(d, 0)
    d._on_press(ev)
    d._on_release(ev)
    assert d._sel_mark == 0 and not writes
    assert d._playhead_s == 50.0              # the playhead stayed put


def test_delete_removes_the_chosen_mark_and_ctrl_click_any(ed):
    d, res, _w, root = ed
    res["markers"] = [{"src": 52.0, "kind": "sfx", "name": ""},
                      {"src": 54.0, "kind": "note", "name": ""}]
    d._draw()
    d._on_press(_flag(d, 0))
    d._on_release(_flag(d, 0))
    _key(root, d._cv, "Delete")
    assert [m["kind"] for m in res["markers"]] == ["note"]
    d._on_press(_Ev(_flag(d, 0).x, _flag(d, 0).y, 0x0004, d._cv))
    assert "markers" not in res
    d._undo_wf()
    assert [m["kind"] for m in res["markers"]] == ["note"]


def test_right_click_on_a_flag_offers_name_and_delete(ed, monkeypatch):
    d, res, _w, _root = ed
    res["markers"] = [{"src": 52.0, "kind": "music_inflection", "name": ""}]
    d._draw()
    menus = []
    monkeypatch.setattr(tk.Menu, "tk_popup",
                        lambda self, x, y: menus.append(self))
    d._on_mark_menu(_flag(d, 0))
    m = menus[0]
    labels = [m.entrycget(i, "label") for i in range(m.index("end") + 1)]
    assert labels == ["Name…", "Delete marker"]
    m.invoke(1)
    assert "markers" not in res
    d._on_mark_menu(_Ev(d._t_to_px(55.0), 30))     # off a flag: no menu
    assert len(menus) == 1


def test_double_click_names_a_flag(ed):
    d, res, _w, root = ed
    res["markers"] = [{"src": 52.0, "kind": "sfx", "name": ""}]
    d._draw()
    d._on_double(_flag(d, 0))
    entry = d._mark_rename[0]
    entry.insert(0, "hoofbeats")
    _key(root, entry, "Return")
    assert res["markers"][0]["name"] == "hoofbeats"


def test_marks_changed_elsewhere_show_here(ed):
    d, res, _w, _root = ed
    res["markers"] = [{"src": 53.0, "kind": "note", "name": "pickup"}]
    d.markers_changed()
    assert "pickup" in _drawn_labels(d)
    # One the Assembly track left in the gap after the pull sits at OUT.
    res["markers"] = [{"tail": 0.3, "kind": "sfx", "name": "gap"}]
    d.markers_changed()
    assert d._mark_t(d._marks()[0]) == d._out_s
    assert d._mark_hit(_flag(d, 0).x, _flag(d, 0).y) == 0


def test_another_clip_shows_its_own_marks(ed):
    d, res, _w, _root = ed
    res["markers"] = [{"src": 52.0, "kind": "sfx", "name": "first"}]
    other = {"markers": [{"src": 81.0, "kind": "note", "name": "second"}]}
    d._draw()
    d._on_press(_flag(d, 0))
    d._on_release(_flag(d, 0))
    d.load_clip("no_such_file.wav", [[80.0, 85.0]], title="NARRATOR",
                get_markers=lambda: other.get("markers"),
                set_markers=lambda m: other.update(markers=m))
    assert d._sel_mark is None
    assert [m["name"] for m in d._marks()] == ["second"]


def test_without_a_pull_to_mark_the_keys_do_nothing(ed, monkeypatch):
    d, res, _w, root = ed
    d._set_markers = None
    _key(root, d._cv, "m")
    assert "markers" not in res
