"""Assembly view editing in a real window: split and nudge, karaoke in the
script, the SFX name field, and Enter in a text field.

The window talks to Step 4 through main.App's own methods -- commit, undo,
snapshot, export inputs -- borrowed onto a small Tk root with a hand-built
card list, so every edit here goes through the same accept and undo path
it does in the app.  The card's accept closure (main.py `_accept`) is
stood in for by _accept_for, which does the parts these edits rely on.

Needs tkinter and a display; skips otherwise.
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import engines  # noqa: E402

tk = pytest.importorskip("tkinter")

FPS = 24.0
WAV = os.path.join(ROOT, "tests", "_no_such_guest.wav")   # never loaded

_APP_METHODS = ("_s4_snapshot", "_s4_apply_state", "_s4_apply_state_rows",
                "_s4_push_undo", "_s4_undo", "_s4_redo",
                "_assembly_set_segments", "_assembly_set_gap",
                "_assembly_set_markers", "_assembly_set_reading",
                "_collect_export_inputs")


class _Var(object):
    def __init__(self, v):
        self.v = v

    def get(self):
        return self.v

    def set(self, v):
        self.v = v


class _Pool(object):
    def get_interview_assets(self):
        return {"GUEST": [WAV]}


def _accept_for(app, e):
    """The card's accept, as far as these edits go: one undo step, the
    join silences carried over, the segments written, the card accepted."""
    r = e["res"]

    def _accept(new_segs, **_kw):
        app._s4_push_undo()
        gaps = engines.realign_seg_gaps(r.get("segments") or [],
                                        r.get("seg_gaps"), new_segs)
        if gaps:
            r["seg_gaps"] = gaps
        else:
            r.pop("seg_gaps", None)
        r["segments"] = new_segs
        e["accepted_flag"][0] = True
        app._s4_save()
    return _accept


def _results():
    return [
        dict(order=1, token="GUEST", status="ok", part_index=0,
             segments=[(10.0, 14.0)],
             quote_text="We walked the ridge before dawn.",
             words=[{"word": " We", "start": 10.2, "end": 10.4},
                    {"word": " walked", "start": 10.5, "end": 10.9},
                    {"word": " the", "start": 11.0, "end": 11.1},
                    {"word": " ridge", "start": 11.2, "end": 11.7},
                    {"word": " before", "start": 12.0, "end": 12.4},
                    {"word": " dawn.", "start": 12.6, "end": 13.2}]),
        dict(order=2, token="GUEST", status="ok", part_index=0,
             segments=[(20.0, 22.0)], quote_text="Nothing moved."),
    ]


@pytest.fixture
def view(monkeypatch):
    import main
    import assembly_view

    class _App(tk.Tk):
        pass
    for n in _APP_METHODS:
        setattr(_App, n, getattr(main.App, n))

    try:
        app = _App()
    except tk.TclError as exc:
        pytest.skip("Tk unavailable: {}".format(exc))
    app.withdraw()
    app.results = _results()
    app.parts, app._monolog = [], False
    app.gap_var = _Var(0.5)
    app.REVIEW_STATUSES = ("review",)
    app._seq_fps = FPS
    app._pool, app.vo_bins = _Pool(), {}
    app._assembly, app.saves, app.opened = None, 0, []
    app._s4_undo_stack, app._s4_redo_stack = [], []
    app._s4_save = lambda: (setattr(app, "saves", app.saves + 1),
                            app._assembly and app._assembly.schedule_refresh())
    app._assembly_open_card = app.opened.append
    app._assembly_ignore = lambda order: None
    app._ui = lambda cb: app.after(0, cb)
    app._rv = []
    for r in app.results:
        e = {"res": r, "skip_var": _Var(False), "accepted_flag": [False]}
        e["accept_fn"] = _accept_for(app, e)
        app._rv.append(e)

    monkeypatch.setattr(assembly_view.playback, "available", lambda: False)
    win = assembly_view.AssemblyWindow(app)
    app._assembly = win
    for _ in range(5):
        app.update()
    yield app, win
    win.close()
    app.destroy()


def _res(app, order):
    return next(r for r in app.results if r["order"] == order)


def _pieces_s(win, order):
    c = win._clip(order)
    return [(round(tl, 4), round(du, 4)) for tl, du, _si in c.pieces]


def test_split_makes_two_regions_and_nudge_moves_the_second(view):
    app, win = view
    win.set_insert(1.5)                    # 11.5 s into the source
    win.split()
    r1 = _res(app, 1)
    assert [tuple(s) for s in r1["segments"]] == [(10.0, 11.5), (11.5, 14.0)]
    assert "seg_gaps" not in r1            # butted: the same audio, two regions
    assert app._rv[0]["accepted_flag"][0]  # through the card's accept
    assert win.sel_region == (1, 1)        # the second half, ready to nudge
    assert _pieces_s(win, 1) == [(0.0, 1.5), (1.5, 2.5)]

    win.nudge(1)                           # a frame later
    fr = round(1 / FPS, 4)
    assert r1["seg_gaps"] == [fr]
    assert _pieces_s(win, 1) == [(0.0, 1.5), (round(1.5 + fr, 4), 2.5)]
    assert round(win._clip(2).start, 4) == round(4.5 + fr, 4)   # all after moves
    assert win.insert == pytest.approx(1.5 + fr, abs=1e-4)
    assert win._range[1] == pytest.approx(1.5 + fr, abs=1e-4)   # selection rides

    win.nudge(-1)
    assert "seg_gaps" not in r1
    depth = len(app._s4_undo_stack)
    win.nudge(-1)                          # butted: nothing to take up
    assert len(app._s4_undo_stack) == depth
    assert "butted" in win.status.cget("text")

    # Step 4's undo walks it back: the nudge, then the split.
    app._s4_undo()
    assert app.results[0]["seg_gaps"] == [fr]
    app._s4_undo()
    app._s4_undo()
    assert [tuple(s) for s in app.results[0]["segments"]] == [(10.0, 14.0)]
    assert "seg_gaps" not in app.results[0]


def test_split_and_nudge_reach_the_export_and_survive_reopening(view):
    app, win = view
    win.set_insert(1.5)
    win.split()
    win.nudge(10)
    gap = round(10 / FPS, 4)
    edited, int_assets = app._collect_export_inputs()[:2]
    placed, _, _ = engines.layout_timeline(edited, 0.5, 48000, int_assets)
    sa = 48000
    assert placed[0]["pieces"] == [(0, int(1.5 * sa), 10 * sa),
                                   (int(round((1.5 + gap) * sa)),
                                    int(2.5 * sa), int(11.5 * sa))]
    # What the view plays is what the AAF places.
    assert [p for c in win.clips for p in c.pieces] == [
        (st / float(sa), du / float(sa), si / float(sa))
        for e in placed for st, du, si in e["pieces"]]

    # A saved session carries the results as JSON; Step 4's state sidecar
    # carries the snapshot.  Both bring the regions back.
    again = json.loads(json.dumps(app.results))
    p2, _, _ = engines.layout_timeline(again, 0.5, 48000, int_assets)
    assert p2[0]["pieces"] == placed[0]["pieces"]
    snap = json.loads(json.dumps(app._s4_snapshot()))
    assert snap["1"]["seg_gaps"] == [gap]
    app.results[0].pop("seg_gaps")
    app._s4_apply_state(snap)
    assert app.results[0]["seg_gaps"] == [gap]


def test_nudging_a_whole_pull_moves_the_gap_in_front_of_it(view):
    app, win = view
    win.set_insert(1.5)
    win.split()                             # region 2 of pull 1 selected
    win.select(2)
    assert win._range is None               # its highlight goes with it
    win.nudge(2)
    assert _res(app, 1)["gap_after_s"] == round(0.5 + 2 / FPS, 4)
    assert "seg_gaps" not in _res(app, 2)


def test_silence_inside_a_pull_drags_like_a_gap(view):
    app, win = view
    win.set_insert(1.5)
    win.split()
    win.nudge(12)                           # half a second of silence
    win.redraw()
    app.update()
    y0, y1 = win._track_box()
    kind, c, k = win._hit(win._x(1.75), (y0 + y1) / 2.0)
    assert (kind, c.order, k) == ("gap", 1, 1)
    kind, c, k = win._hit(win._x(2.5), y1 - 4)
    assert (kind, c.order, k) == ("grab", 1, 1)
    # A mark dropped in that silence sits where the audio resumes.
    win.playhead = 1.75
    win.drop_marker("music_in")
    m = win.markers[0]
    assert m["t"] == pytest.approx(2.0, abs=1e-3)


def test_deleting_a_region_closes_it_up(view):
    app, win = view
    win.set_insert(1.0)
    win.split()
    win.set_insert(2.0)
    win.split()                             # three regions: 0-1, 1-2, 2-4 s
    win._select_region(1, 1)
    win.delete_range()
    assert [tuple(s) for s in _res(app, 1)["segments"]] == \
        [(10.0, 11.0), (12.0, 14.0)]


def test_sfx_spot_opens_its_name_field_and_enter_only_names_it(view):
    app, win = view
    win.set_insert(0.5)
    win.drop_marker("sfx")
    assert win._rename is not None
    entry = win._rename[0]
    win.win.focus_force()
    entry.focus_force()
    app.update()
    entry.insert(0, "rifle shot")
    entry.event_generate("<KeyPress>", keysym="Return", when="now")
    app.update()
    assert _res(app, 1)["markers"][0]["name"] == "rifle shot"
    assert win._rename is None
    assert app.opened == []                 # Enter did not open the editor

    # Esc keeps the count for a name; a music cue doesn't open a field.
    win.set_insert(2.0)
    win.drop_marker("sfx")
    entry = win._rename[0]
    entry.focus_force()
    app.update()
    entry.event_generate("<KeyPress>", keysym="Escape", when="now")
    app.update()
    assert win._rename is None
    assert [m["label"] for m in win.markers] == ["rifle shot", "SFX 2"]
    win.drop_marker("music_in")
    assert win._rename is None
    assert app.opened == []


def test_enter_on_the_track_still_opens_the_editor(view):
    app, win = view
    win.select(2)
    win.cv.focus_force()
    app.update()
    win.cv.event_generate("<KeyPress>", keysym="Return", when="now")
    app.update()
    assert app.opened == [2]


def test_script_lights_the_word_being_said(view):
    app, win = view
    c = win._clip(1)
    c.words = win._map_words(c)

    def lit():
        rng = win.text.tag_ranges("word")
        return win.text.get(*rng) if rng else None
    win.set_insert(1.4)                     # 11.4 s: "ridge"
    assert lit() == "ridge"
    win.set_insert(2.9)                     # 12.9 s: "dawn."
    assert lit() == "dawn."
    assert len(win.text.tag_ranges("word")) == 2   # one word at a time
    win.set_insert(3.8)                     # past the last word and its hold
    assert lit() is None


def test_script_timing_spreads_words_the_transcript_says_differently():
    from assembly_view import _script_timing
    body = "I'm gonna go there now."
    words = [(0.0, 0.2, "I'm"), (0.2, 0.4, "going"), (0.4, 0.6, "to"),
             (0.6, 0.8, "go"), (0.8, 1.0, "there"), (1.0, 1.2, "now.")]
    spans = _script_timing(body, words)
    got = [(body[cs:ce], round(t0, 3), round(t1, 3)) for t0, t1, cs, ce in spans]
    assert got == [("I'm", 0.0, 0.2), ("gonna", 0.2, 0.6), ("go", 0.6, 0.8),
                   ("there", 0.8, 1.0), ("now.", 1.0, 1.2)]
    assert _script_timing(body, []) == []
    assert _script_timing("Nothing alike", [(0, 1, "different")]) == []
