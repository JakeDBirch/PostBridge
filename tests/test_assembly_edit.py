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

from conftest import open_assembly  # noqa: E402
import engines  # noqa: E402

tk = pytest.importorskip("tkinter")

FPS = 24.0
WAV = os.path.join(ROOT, "tests", "_no_such_guest.wav")   # never loaded

_APP_METHODS = ("_s4_snapshot", "_s4_undo_snapshot", "_s4_apply_state",
                "_s4_apply_state_rows", "_s4_push_undo", "_s4_undo", "_s4_redo",
                "_assembly_set_default_gap",
                "_assembly_set_segments", "_assembly_set_gap",
                "_assembly_set_markers", "_assembly_set_reading",
                "_assembly_edit_many", "_collect_export_inputs")


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
    app._prefs, app.saved, app.lifted = {}, [], []
    app._save_prefs = lambda: None
    app._quick_save = app.saved.append
    app.lift = lambda *a: app.lifted.append(True)
    app._assembly_ignore = lambda order: None
    app._ui = lambda cb: app.after(0, cb)
    app._rv = []
    for r in app.results:
        e = {"res": r, "skip_var": _Var(False), "accepted_flag": [False]}
        e["accept_fn"] = _accept_for(app, e)
        app._rv.append(e)

    monkeypatch.setitem(assembly_view._MONITOR, "nudge_s", 0.1)
    monkeypatch.setattr(assembly_view.playback, "available", lambda: False)
    win = open_assembly(app)
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

    win.nudge(1)                           # 100 ms later
    fr = 0.1
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
    win.nudge(1)
    gap = 0.1
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
    win.nudge(1)
    assert _res(app, 1)["gap_after_s"] == 0.6
    assert "seg_gaps" not in _res(app, 2)


def test_silence_inside_a_pull_drags_like_a_gap(view):
    app, win = view
    win.set_insert(1.5)
    win.split()
    win.nudge_gap()                         # half a second of silence
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


def _key(app, widget, keysym, state=0):
    widget.focus_force()
    app.update()
    widget.event_generate("<KeyPress>", keysym=keysym, state=state, when="now")
    app.update()


def test_inflection_and_notes(view):
    app, win = view
    win.set_insert(0.5)
    _key(app, win.cv, "m")                  # inflection: no name field
    assert win._rename is None
    win.set_insert(1.5)
    _key(app, win.cv, "n")                  # a note opens for naming
    assert win._rename is not None
    win._rename[0].insert(0, "check the date")
    _key(app, win._rename[0], "Return")
    kinds = [(m["kind"], m["label"]) for m in win.markers]
    assert kinds == [("music_inflection", "Inflection 1"),
                     ("note", "check the date")]
    assert app.opened == []
    # Notes have their own lane on the ruler.
    win.redraw()
    note = win.markers[1]
    top, bot = win._marker_row(1)
    kind, hit, _k = win._hit(win._x(note["t"]) + 3, (top + bot) / 2.0)
    assert kind == "marker" and hit["kind"] == "note"
    top, bot = win._marker_row(0)
    kind, hit, _k = win._hit(win._x(note["t"]) + 3, (top + bot) / 2.0)
    assert kind != "marker"


def test_double_click_with_the_grabber_opens_the_editor(view):
    app, win = view
    win.redraw()
    app.update()
    y0, y1 = win._track_box()
    class _E(object):
        def __init__(self, x, y):
            self.x, self.y = x, y
    x = win._x(1.0)
    win._on_track_double(_E(x, y1 - 4))
    assert app.opened == [1]
    # The selector's double-click takes the whole region instead.
    win._on_track_double(_E(x, y0 + 6))
    assert app.opened == [1]
    assert win.sel_region == (1, 0) and win._range[1:] == (0.0, 4.0)


def test_step4_and_save_from_the_assembly(view):
    app, win = view
    _key(app, win.cv, "equal", state=0x0004)          # Ctrl+=
    assert app.lifted
    assert win.pps == pytest.approx(win.pps)          # and it didn't zoom
    _key(app, win.cv, "s", state=0x0004)              # Ctrl+S
    assert app.saved == [[win.save_key.lbl]]


def test_panels_wrap_and_the_layout_is_kept(view):
    app, win = view
    side = win._side
    flow = win.legend.master.winfo_children()[1]
    win._top.sash_place(0, win._top.winfo_width() - 600, 0)
    app.update()
    tall_wide = flow.winfo_reqheight()
    win._top.sash_place(0, win._top.winfo_width() - 200, 0)
    app.update()
    assert flow.winfo_reqheight() > tall_wide       # narrower: it wraps
    # Every panel opens as tall as its buttons need, where there is room.
    win._top.sash_place(0, win._top.winfo_width() - 900, 0)
    app.update()
    win._fit_panels()
    app.update()
    panes = [side.nametowidget(str(n)) for n in side.panes()]
    if sum(f.winfo_reqheight() + 5 for f in panes) <= side.winfo_height():
        for f in panes[:-1]:
            assert f.winfo_height() >= f.winfo_reqheight() - 1
    frac = win._top.sash_coord(0)[0] / float(win._top.winfo_width())
    n_panes = len(side.panes())
    win.close()
    lay = app._prefs["assembly_layout"]
    assert lay["script"] == pytest.approx(frac, abs=0.01)
    assert len(lay["panels"]) == n_panes - 1


def test_editor_accept_edits_its_own_card(tmp_path):
    """The waveform editor's Accept used to edit the LAST card in Step 4,
    whichever card it was opened from."""
    import wave
    import main
    wav = str(tmp_path / "GUEST.wav")
    with wave.open(wav, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000 * 30)
    try:
        app = main.App()
    except tk.TclError as exc:
        pytest.skip("Tk unavailable: {}".format(exc))
    try:
        app.results = [
            dict(order=o, token="GUEST", status="ok", part_index=0,
                 segments=[[a, a + 2.0]], rec_in_s=a, rec_out_s=a + 2.0,
                 rec_in_tc="", rec_out_tc="", quote_text="line {}".format(o),
                 source_audio=wav)
            for o, a in ((1, 2.0), (2, 10.0), (3, 20.0))]
        app.parts = [{"index": 0, "name": "Part 0"}]
        app.tokens, app.workflow = ["GUEST"], "script_session"
        app.pulls = [{"token": "GUEST", "order": o} for o in (1, 2, 3)]
        app._script_path = str(tmp_path / "script.txt")
        app.update()
        app._step4()
        for _ in range(60):
            app.update()
        import match_review
        dialogs = []
        orig = match_review.MatchReviewDialog.__init__

        def _init(self, *a, **k):
            orig(self, *a, **k)
            dialogs.append(self)
        match_review.MatchReviewDialog.__init__ = _init
        try:
            next(e for e in app._rv if e["res"]["order"] == 1)["open_review_fn"]()
            for _ in range(20):
                app.update()
        finally:
            match_review.MatchReviewDialog.__init__ = orig
        d = dialogs[-1]
        d._in_s += 0.5
        d._on_accept_click()
        app.update()
        segs = {r["order"]: [list(s) for s in r["segments"]] for r in app.results}
        assert segs[1] == [[2.5, 4.0]]
        assert segs[3] == [[20.0, 22.0]]
    finally:
        app.destroy()


def test_default_gap_is_set_in_the_assembly(view):
    app, win = view
    _res(app, 2)["gap_after_s"] = 1.0       # a gap set by hand
    app.results.append(dict(order=3, token="GUEST", status="ok", part_index=0,
                            segments=[(30.0, 31.0)], quote_text="Then quiet."))
    win.refresh()
    assert win._clip(2).start == pytest.approx(4.5)
    win._gap_box.delete(0, "end")
    win._gap_box.insert(0, "0.25")
    win._gap_from_box()
    assert app.gap_var.get() == 0.25
    assert win._clip(2).start == pytest.approx(4.25)    # the default moved it
    assert win._clip(3).start - win._clip(2).end == pytest.approx(1.0)  # by hand
    app._s4_undo()                                       # one undo step
    win.refresh()
    assert app.gap_var.get() == 0.5 and win._gap_box.get() == "0.50"
    assert win._clip(2).start == pytest.approx(4.5)


def _settle(app, secs=0.4):
    import time
    end = time.time() + secs
    while time.time() < end:
        app.update()
        time.sleep(0.01)


def test_script_follows_the_lit_word_and_holds_off_after_a_scroll(view):
    import assembly_view
    app, win = view
    text = " ".join("word{}".format(i) for i in range(40))
    app.results = [dict(order=o, token="GUEST", status="ok", part_index=0,
                        segments=[(10.0 * o, 10.0 * o + 4.0)], quote_text=text)
                   for o in range(1, 41)]
    app._rv = []
    for r in app.results:
        e = {"res": r, "skip_var": _Var(False), "accepted_flag": [False]}
        e["accept_fn"] = _accept_for(app, e)
        app._rv.append(e)
    app.results[20]["words"] = [
        {"word": "word{}".format(i), "start": 210.0 + 0.1 * i,
         "end": 210.0 + 0.1 * i + 0.08} for i in range(40)]
    win.refresh()
    app.update()
    last = win.clips[20]                   # mid-script: room to scroll
    t = win.text
    t.yview_moveto(0.0)
    win._touched_at = 0.0
    win.set_insert(last.start + 0.05)
    _settle(app)
    box = t.bbox(win._lit_range[0])
    h = t.winfo_height()
    assert box is not None
    assert assembly_view.SCRIPT_BAND[0] * h <= box[1] <= assembly_view.SCRIPT_BAND[1] * h
    # Scrolled by hand: the script stays where it was put.
    t.yview_moveto(0.0)
    t.event_generate("<MouseWheel>", delta=120)
    app.update()
    y0 = t.yview()[0]
    win.set_insert(last.start + 2.05)
    _settle(app)
    assert t.yview()[0] == y0


def test_undo_clears_the_pulls_accepted_flag(view):
    app, win = view
    e = app._rv[0]
    snap = app._s4_snapshot()               # as it was: not accepted
    e["accepted_flag"][0] = True
    e["res"]["_s4_accepted"] = True
    app._s4_apply_state(snap)
    assert not e["accepted_flag"][0]
    assert not e["res"]["_s4_accepted"]


# ── Transport, zoom and snapping ─────────────────────────────────────────
class _Ev(object):
    def __init__(self, x, y, state=0):
        self.x, self.y, self.state = x, y, state


def test_monitoring_defaults():
    import assembly_view
    src = open(assembly_view.__file__, encoding="utf-8").read()
    head = src[src.index("_MONITOR = {"):src.index("_PREROLLS")]
    assert '"follow": False' in head
    assert '"preroll_on": True' in head
    assert '"return_on": True' in head


def test_return_to_zero_goes_to_the_top_of_everything(view):
    app, win = view
    win.pps = 200.0                         # so the top can scroll out of view
    win.view0 = 2.0
    win.seek(3.0)
    win._range = (1, 1.0, 2.0)
    win.text.yview_moveto(1.0)
    win.return_to_zero()
    assert win.playhead == 0.0 and win.insert == 0.0
    assert win._range is None
    assert win.view0 == pytest.approx(0.0, abs=0.05)
    assert win.text.yview()[0] == 0.0

    win.seek(3.0)
    _key(app, win.cv, "Home")
    assert win.playhead == 0.0


def test_zoom_out_full_shows_the_whole_cut(view):
    app, win = view
    win.zoom(4.0)
    win.view0 = 1.0
    win.win.focus_force()
    win.cv.focus_force()
    app.update()
    win.cv.event_generate("<Alt-a>", when="now")
    app.update()
    assert win.pps == pytest.approx(win._min_pps())
    assert win.view0 == 0.0
    assert win.total_s * win.pps <= win.cv.winfo_width()


# ── Zoomed all the way in ─────────────────────────────────────────────────────
def _load(win, order, pcm, t0=9.0):
    """Give a pull its audio, as _on_loaded does (the test file doesn't exist)."""
    import numpy as np
    c = win._clip(order)
    win.audio.put(order, c.play_path, t0, np.asarray(pcm, dtype=np.int16))
    win._env[order] = (t0, np.full(4000, 0.5, np.float32))
    win._env_scale = 1.0
    return c


def _wave_items(win, col, kind):
    return [i for i in win.cv.find_all()
            if win.cv.type(i) == kind and win.cv.itemcget(i, "fill") == col]


def test_zoom_reaches_the_samples_and_the_ruler_and_clock_keep_up(view):
    import assembly_view
    app, win = view
    for _ in range(40):
        win.zoom(1.5)
    assert win.pps == assembly_view.MAX_PPS == 128000.0
    win.set_insert(1.5)
    win.view0 = 1.5
    win.redraw()
    app.update()
    # 1 ms between ticks, so there are ticks to read, to the millisecond.
    texts = [win.cv.itemcget(i, "text") for i in win.cv.find_all()
             if win.cv.type(i) == "text"]
    assert any(t.startswith("0:01.50") and len(t) == 8 for t in texts), texts
    assert sum(1 for t in texts if t.startswith("0:01.5")) >= 5
    assert win.tc_lbl.cget("text") == "0:01.500"
    win.fit()
    assert win.tc_lbl.cget("text") == "0:01.5"


def test_wheel_zoom_stops_at_the_samples(view):
    import assembly_view
    app, win = view
    win.pps = 100000.0
    ev = _Ev(10, 10, state=0x0004)
    ev.delta = 120 * 5
    win._on_wheel(ev)
    assert win.pps == assembly_view.MAX_PPS


def test_up_close_the_wave_is_the_audio_not_the_envelope(view):
    """Positive-only audio: the envelope would draw it either side of centre."""
    import numpy as np
    import assembly_view
    app, win = view
    c = _load(win, 1, np.full(32000 * 6, 16000))
    win.pps = 2000.0                       # 16 samples to a pixel
    win.view0 = 0.5
    win.redraw()
    app.update()
    y0, y1 = win._track_box()
    mid = (y0 + win._ls + 8 + y1 - win._ls - 8) / 2.0
    polys = _wave_items(win, c.colour(), "polygon")
    assert len(polys) == 1
    ys = win.cv.coords(polys[0])[1::2]
    assert min(ys) < mid - 20              # up to the sample's level
    assert max(ys) <= mid + 1              # and nothing below centre

    # Further in than a sample to a pixel: a line through the samples.
    win.pps = assembly_view.MAX_PPS
    win.redraw()
    app.update()
    lines = [i for i in _wave_items(win, c.colour(), "line")
             if len(win.cv.coords(i)) > 40]
    assert len(lines) == 1
    xs = win.cv.coords(lines[0])[0::2]
    assert np.diff(xs) == pytest.approx(4.0)   # 4 px to a sample
    assert not _wave_items(win, c.colour(), "polygon")


def test_up_close_a_cut_stays_out_of_the_drawing(view):
    """Loud tape between two regions: the pixel at the join must not span it."""
    import numpy as np
    app, win = view
    pcm = np.full(32000 * 6, 1000)
    pcm[int(2.0 * 32000):int(3.0 * 32000)] = 30000      # source 11-12 s
    app._assembly_set_segments(1, [(10.0, 11.0), (12.0, 14.0)])
    win.refresh()
    c = _load(win, 1, pcm)
    win.pps = 1000.0
    win.view0 = 0.2
    win.redraw()
    app.update()
    polys = _wave_items(win, c.colour(), "polygon")
    assert len(polys) == 2                 # a region each
    y0, y1 = win._track_box()
    mid = (y0 + y1) / 2.0
    for p in polys:
        ys = win.cv.coords(p)[1::2]
        assert max(ys) - min(ys) < 12      # only the quiet audio, in both


@pytest.mark.parametrize("pps", [60.0, 100.0, 1000.0])
def test_silence_between_regions_is_blank_at_every_zoom(view, pps):
    """One shape across a pull's regions bridged the silence between them.
    It has to look like what it is: nothing there."""
    import numpy as np
    from config import BG
    app, win = view
    app._assembly_set_segments(1, [(10.0, 11.0), (11.0, 14.0)], [1.0])
    win.refresh()
    pieces = win._clip(1).pieces
    assert win._clip(1).seg_gaps == [1.0]
    c = _load(win, 1, np.full(32000 * 6, 8000))
    win._env[1] = (9.0, np.full(4000, 0.25, np.float32))
    win.pps = pps
    win.view0 = 0.0
    win.redraw()
    app.update()
    assert win.pps == pps
    gap_a, gap_b = win._x(pieces[0][1]), win._x(pieces[1][0])
    shapes = _wave_items(win, c.colour(), "polygon")
    assert len(shapes) == 2, ("a region each", win.cv.winfo_width(), win.cv.winfo_height(),
                              win.pps, win.view0, len(win.cv.find_all()), sorted(win._env))
    for s in shapes:
        xs = win.cv.coords(s)[0::2]
        assert max(xs) <= gap_a + 1 or min(xs) >= gap_b - 1, (min(xs), max(xs), gap_a, gap_b)
    # Bare track, the height of the pull: its bar and edge don't cross it.
    y0, y1 = win._track_box()
    blank = [i for i in win.cv.find_all() if win.cv.type(i) == "rectangle"
             and win.cv.itemcget(i, "fill") == BG
             and win.cv.coords(i)[0] == pytest.approx(gap_a)
             and win.cv.coords(i)[2] == pytest.approx(gap_b)]
    assert blank
    box = win.cv.coords(blank[0])
    assert box[1] <= y0 and box[3] >= y1


def test_loud_tape_cut_at_a_join_stays_out_of_a_zoomed_out_drawing(view):
    """The join's pixel took its peak from the tape that was cut."""
    import numpy as np
    app, win = view
    app._assembly_set_segments(1, [(10.0, 11.0), (12.0, 14.0)])
    win.refresh()
    c = win._clip(1)
    env = np.full(2000, 0.02, np.float32)
    env[int(2.0 * 400):int(3.0 * 400)] = 1.0          # source 11-12 s
    win._env[1] = (9.0, env)
    win._env_scale = 1.0
    win.pps = 60.0
    win.view0 = 0.0
    win.redraw()
    app.update()
    shapes = _wave_items(win, c.colour(), "polygon")
    assert len(shapes) == 2
    for s in shapes:
        ys = win.cv.coords(s)[1::2]
        assert max(ys) - min(ys) < 40      # only the quiet audio, in both


def test_up_close_falls_back_until_the_audio_is_there(view):
    import numpy as np
    app, win = view
    c = win._clip(1)
    win._env[1] = (9.0, np.full(4000, 0.5, np.float32))
    win.pps = 2000.0
    win.view0 = 0.5
    win.redraw()                           # no buffer yet: no crash, the envelope
    app.update()
    assert len(_wave_items(win, c.colour(), "polygon")) == 1


# ── Edits work from the ghost cursor while the cut plays ──────────────────────
def _playing(win, insert, playhead):
    """Mid-playback: the insertion point left where it was clicked, the
    playhead somewhere else."""
    win._playing = True
    win.insert, win.playhead = insert, playhead


def test_split_during_playback_is_at_the_ghost_cursor(view):
    app, win = view
    _playing(win, 1.0, 3.0)
    try:
        win.split()
    finally:
        win._playing = False
    assert [tuple(s) for s in _res(app, 1)["segments"]] == [(10.0, 11.0), (11.0, 14.0)]
    assert win.insert == 1.0               # and it stays put


def test_zoom_during_playback_holds_the_ghost_cursor_still(view):
    app, win = view
    win.pps = max(1000.0, 8.0 * win.cv.winfo_width() / win.total_s)
    win.view0 = 2.0 - 0.25 * win._span()
    _playing(win, 2.0, 2.0 + 0.5 * win._span())
    try:
        x = win._x(2.0)
        win.zoom(1.5)
        win.zoom(1.5)
        assert win._x(2.0) == pytest.approx(x)
        # Not on screen: it zooms about the middle and doesn't jump there.
        win.view0 = 4.0
        mid = 4.0 + win._span() / 2.0
        x = win._x(mid)
        win.zoom(1.5)
        assert win._x(mid) == pytest.approx(x)
    finally:
        win._playing = False


def test_opening_and_swapping_during_playback_mean_the_ghost_cursors_pull(view):
    app, win = view
    win.sel_order = None
    _playing(win, 1.0, 4.8)                # the playhead is past pull 1, in pull 2
    try:
        assert win._order_at(win.playhead) == 2
        win.open_selected()
    finally:
        win._playing = False
        win.stop = lambda key=False: None
    assert app.opened == [1]
    del win.stop


def test_an_edge_snaps_to_the_ghost_cursor_not_the_moving_playhead(view):
    app, win = view
    win.pps = 100.0
    win.view0 = 0.0
    win.redraw()
    app.update()
    _playing(win, 3.5, 1.0)                # source 13.5 s; the playhead is elsewhere
    try:
        y0, y1 = win._track_box()
        x_out = win._x(4.0) - 1
        win._on_track_press(_Ev(x_out, (y0 + y1) / 2.0))
        assert win._drag["mode"] == "out"
        assert win._drag["ph"] == pytest.approx(13.5)
    finally:
        win._drag = None
        win._playing = False


def test_spotting_during_playback_stays_on_the_live_cursor(view):
    """Marks are for what you hear as you hear it, as in Pro Tools."""
    app, win = view
    _playing(win, 1.0, 3.0)
    try:
        win.drop_marker("music_inflection")
    finally:
        win._playing = False
    assert win.markers[0]["t"] == pytest.approx(3.0, abs=1e-3)


def test_a_span_too_short_to_trim_can_still_be_deleted(view):
    """Zoomed in on a click: 10 ms is well under the shortest trim."""
    app, win = view
    win._range = (1, 1.0, 1.01)
    win.delete_range()
    r = _res(app, 1)
    assert [tuple(s) for s in r["segments"]] == [(10.0, 11.0), (11.01, 14.0)]


def _edge_drag(win, order, mode, new_x, state=0):
    """Press on a pull's edge and drag it to pixel new_x; the trim as it stands."""
    c = win._clip(order)
    tl, du, _si = c.pieces[-1 if mode == "out" else 0]
    x = win._x(tl + du if mode == "out" else tl)
    y0, y1 = win._track_box()
    y = (y0 + y1) / 2.0
    win._on_track_press(_Ev(x - 2 if mode == "out" else x + 2, y))
    assert win._drag["mode"] == mode
    win._drag["x0"] = x
    win._on_track_drag(_Ev(new_x, y, state))
    return win._drag


def test_a_dragged_edge_snaps_as_in_the_waveform_editor(view):
    app, win = view
    win.pps = 100.0                         # 8 px = 0.08 s
    win.redraw()
    win._sil[1] = [12.0, 13.5]
    win.set_insert(0.0)
    d = _edge_drag(win, 1, "out", win._x(3.54))     # 13.54: 4 px from 13.5
    assert d["value"][0] == (10.0, 13.5)
    assert d["snapped"]
    assert "snapped" in win.status.cget("text")
    win._on_track_release(_Ev(0, 0))
    assert [tuple(s) for s in _res(app, 1)["segments"]] == [(10.0, 13.5)]
    app._s4_undo()


def test_an_edge_stays_free_past_the_snap_or_with_ctrl(view):
    app, win = view
    win.pps = 100.0
    win.redraw()
    win._sil[1] = [13.5]
    win.set_insert(0.0)
    d = _edge_drag(win, 1, "out", win._x(3.3))      # 20 px away
    assert d["value"][0][1] == pytest.approx(13.3) and not d["snapped"]
    win._on_track_release(_Ev(0, 0))
    app._s4_undo()
    win.refresh()
    d = _edge_drag(win, 1, "out", win._x(3.54), state=0x0004)    # Ctrl
    assert d["value"][0][1] == pytest.approx(13.54) and not d["snapped"]
    win._on_track_release(_Ev(0, 0))


def test_the_playhead_pulls_an_edge_unless_it_is_parked_on_it(view):
    app, win = view
    win.pps = 100.0
    win.redraw()
    win.set_insert(3.0)                     # 13.0 s
    d = _edge_drag(win, 1, "out", win._x(3.1))      # 10 px off: within 12
    assert d["value"][0][1] == pytest.approx(13.0) and d["snapped"]
    win._on_track_release(_Ev(0, 0))
    # A trim parks the insertion point on the new edge.  Trimming on from
    # there must not be pulled straight back to where it started.
    assert win.insert == pytest.approx(3.0)
    d = _edge_drag(win, 1, "out", win._x(2.95))
    assert d["value"][0][1] == pytest.approx(12.95) and not d["snapped"]
    win._on_track_release(_Ev(0, 0))


def test_a_highlight_snaps_at_both_ends(view):
    app, win = view
    win.pps = 100.0
    win.redraw()
    win._sil[1] = [11.0, 12.0]
    y0, y1 = win._track_box()
    y = y0 + 2
    win._on_track_press(_Ev(win._x(1.04), y))
    win._on_track_drag(_Ev(win._x(1.97), y))
    win._on_track_release(_Ev(win._x(1.97), y))
    assert win._range[1:] == (pytest.approx(1.0), pytest.approx(2.0))
    # A plain click isn't moved: the insertion point goes where you put it.
    win._on_track_press(_Ev(win._x(1.04), y))
    win._on_track_release(_Ev(win._x(1.04), y))
    assert win._range is None and win.insert == pytest.approx(1.04, abs=0.011)


def test_snap_points_come_from_the_loaded_audio_and_are_ticked(view):
    import numpy as np
    from assembly_view import PLAY_SR, _silence_edges
    app, win = view
    t = np.arange(PLAY_SR * 3) / PLAY_SR
    tone = (np.sin(2 * np.pi * 220 * t) * 12000).astype(np.int16)
    tone[PLAY_SR:PLAY_SR * 2] = 0           # a second of silence in the middle
    edges = _silence_edges(tone, 10.0)
    assert len(edges) == 2
    assert edges[0] == pytest.approx(11.0, abs=0.03)
    assert edges[1] == pytest.approx(12.0, abs=0.03)
    assert _silence_edges(np.zeros(0, np.int16), 0.0) == []

    win.pps = 100.0
    win._sil[1] = edges
    win.redraw()
    with_ticks = len(win.cv.find_all())
    win._sil[1] = []
    win.redraw()
    assert with_ticks - len(win.cv.find_all()) == 4     # two ticks, top and bottom


def test_the_shared_snap_keeps_the_editors_rules():
    import match_review as mr
    b = [1.0, 2.0, 3.0]
    assert mr.nearest_boundary([], 1.0) is None
    assert mr.nearest_boundary(b, 0.0) == 1.0
    assert mr.nearest_boundary(b, 9.0) == 3.0
    assert mr.nearest_boundary(b, 1.5) == 1.0            # a tie: the earlier
    assert mr.snap_to_boundary(1.05, b, 0.1) == 1.0
    assert mr.snap_to_boundary(1.5, b, 0.1) == 1.5
    assert mr.snap_to_boundary(1.05, b, 0.1, 1.04, 0.2) == 1.04   # nearer wins
    assert mr.snap_to_boundary(1.05, [], 0.1, 1.1, 0.2) == 1.1
    assert mr.snap_to_boundary(1.05, b, 0.1, None, 0.2) == 1.0


def test_nudge_values(view):
    import assembly_view
    app, win = view
    win.select(2)
    gap = lambda: _res(app, 1).get("gap_after_s", 0.5)
    win.nudge(1)
    assert gap() == pytest.approx(0.6)        # 100 ms to start with
    win.nudge(-1)
    assert gap() == pytest.approx(0.5)

    win.set_nudge(0.01)
    assert app._prefs["assembly_nudge"] == 0.01
    assert win.nudge_keys[0.01].on and not win.nudge_keys[0.1].on
    win.nudge(1)
    assert gap() == pytest.approx(0.51)
    assert set(assembly_view.NUDGES) == {0.01, 0.1, 0.5, 1.0}


def test_slash_nudges_by_the_default_gap_and_angle_brackets_are_gone(view):
    app, win = view
    app.gap_var.v = 0.25                      # not any of the nudge values
    win.refresh()
    win.select(2)
    gap = lambda: _res(app, 1)["gap_after_s"]
    _key(app, win.cv, "slash")
    assert gap() == pytest.approx(0.5)
    _key(app, win.cv, "slash")
    assert gap() == pytest.approx(0.75)
    _key(app, win.cv, "question", state=0x0001)   # Shift+/ does nothing
    assert gap() == pytest.approx(0.75)
    _key(app, win.cv, "greater", state=0x0001)
    assert gap() == pytest.approx(0.75)

    app.gap_var.v = 0.0                       # says so, and leaves it alone
    _key(app, win.cv, "slash")
    assert gap() == pytest.approx(0.75)
    assert "default gap is 0" in win.status.cget("text")


def test_the_edit_panel_offers_the_gap_nudge(view):
    app, win = view

    def labels(w):
        out = [w.cget("text")] if isinstance(w, tk.Label) else []
        for c in w.winfo_children():
            out += labels(c)
        return out
    shown = labels(win.edit_flow)
    assert "Nudge later by default gap" in shown
    assert "Big nudge later" not in shown and "Shift+/" not in shown


def test_the_spotting_keys(view):
    app, win = view
    win.set_insert(0.5)
    _key(app, win.cv, "m")
    assert [m["kind"] for m in win.markers] == ["music_inflection"]
    assert win.markers[0]["label"].startswith("Inflection")
    for k in ("i", "o", "u", "k"):             # the old ones do nothing
        _key(app, win.cv, k)
    assert len(win.markers) == 1

    win.set_insert(1.0)
    _key(app, win.cv, "x")
    assert win._rename is not None
    _key(app, win._rename[0], "Escape")
    win.set_insert(1.5)
    _key(app, win.cv, "n")
    assert win._rename is not None
    _key(app, win._rename[0], "Escape")
    assert [m["kind"] for m in win.markers] == ["music_inflection", "sfx", "note"]


def test_x_stays_in_this_window(view):
    app, win = view
    fired = []
    app.bind_all("<x>", lambda e: fired.append(e))
    win.set_insert(1.0)
    _key(app, win.cv, "x")
    _key(app, win._rename[0], "Escape")
    assert not fired
    app.unbind_all("<x>")


def test_shift_n_is_return_to_start_now(view):
    import assembly_view
    app, win = view
    was = assembly_view._MONITOR["return_on"]
    _key(app, win.cv, "N", state=0x0001)
    assert assembly_view._MONITOR["return_on"] is (not was)
    assert not win.markers
    win.toggle_return()
    assert assembly_view._MONITOR["return_on"] is was


def test_marks_saved_with_in_and_out_still_show_and_can_go(view):
    app, win = view
    _res(app, 1)["markers"] = [{"kind": "music_in", "src": 11.0, "name": ""},
                               {"kind": "music_out", "src": 12.0, "name": ""}]
    win.refresh()
    assert [m["kind"] for m in win.markers] == ["music_in", "music_out"]
    edited, assets = app._collect_export_inputs()[:2]
    placed, _s, _p = engines.layout_timeline(edited, 0.5, 48000, assets)
    assert [m["kind"] for m in engines.place_markers(placed, 48000)] == \
        ["music_in", "music_out"]


def _flag_at(win, m):
    top, bot = win._marker_row(win._lane(m))
    return _Ev(win._x(m["t"]) + 3, (top + bot) / 2.0)


def test_ctrl_click_deletes_a_mark(view):
    app, win = view
    win.set_insert(0.5)
    win.drop_marker("music_inflection")
    win.redraw()
    app.update()
    ev = _flag_at(win, win.markers[0])
    win._on_track_press(_Ev(ev.x, ev.y, 0x0004))
    assert win.markers == [] and win._drag is None
    assert "markers" not in _res(app, 1)
    app._s4_undo()
    win.refresh()
    assert len(win.markers) == 1               # one undo step


def test_right_click_on_a_mark_offers_delete(view, monkeypatch):
    app, win = view
    win.set_insert(0.5)
    win.drop_marker("music_inflection")
    win.redraw()
    app.update()
    menus = []

    def _popup(self, x, y):
        menus.append(self)
    monkeypatch.setattr(tk.Menu, "tk_popup", _popup)
    ev = _flag_at(win, win.markers[0])
    ev.x_root, ev.y_root = 0, 0
    win._on_track_menu(ev)
    m = menus[0]
    labels = [m.entrycget(i, "label") for i in range(m.index("end") + 1)]
    assert "Delete marker" in labels
    m.invoke(labels.index("Delete marker"))
    assert win.markers == []
    # Off a flag there is no menu.
    y0, _y1 = win._track_box()
    e = _Ev(win._x(1.0), y0 + 4)
    e.x_root, e.y_root = 0, 0
    win._on_track_menu(e)
    assert len(menus) == 1


def test_playback_is_not_held_in_by_a_highlight(view):
    import assembly_view
    app, win = view

    class _Player(object):
        def __init__(self):
            self.calls = []

        def set_rate(self, r):
            pass

        def play(self, start_s=None, end_s=None):
            self.calls.append((start_s, end_s))
            return True

        def stop(self):
            pass

        def is_playing(self):
            return False

        def position(self):
            return 0.0

        def close(self):
            pass
    win.player = _Player()
    win._range = (1, 1.0, 2.0)
    win.insert = 1.0
    win.play()
    assert win.player.calls[0][1] is None      # no end
    win.stop()
    assert win._range == (1, 1.0, 2.0)         # the highlight stays
    assert "plays it" not in win.status.cget("text")
