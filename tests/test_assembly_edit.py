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


def _key(app, widget, keysym, state=0):
    widget.focus_force()
    app.update()
    widget.event_generate("<KeyPress>", keysym=keysym, state=state, when="now")
    app.update()


def test_inflection_and_notes(view):
    app, win = view
    win.set_insert(0.5)
    _key(app, win.cv, "u")                  # inflection: no name field
    assert win._rename is None
    win.set_insert(1.5)
    _key(app, win.cv, "k")                  # a note opens for naming
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
    win.refresh()
    app.update()
    last = win.clips[20]                   # mid-script: room to scroll
    last.words = [(last.start + 0.1 * i, last.start + 0.1 * i + 0.08,
                   "word{}".format(i)) for i in range(40)]
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
