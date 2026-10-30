"""The Assembly's text pane: highlight the transcript, edit the audio.

The same set-up as test_assembly_edit: a real window on main.App's own commit
and undo methods.  Mouse gestures go in through the pane's own handlers, at
the pixel a word is drawn at.

Needs tkinter and a display; skips otherwise.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import engines  # noqa: E402

tk = pytest.importorskip("tkinter")

WAV = os.path.join(ROOT, "tests", "_no_such_guest.wav")   # never loaded

_APP_METHODS = ("_s4_snapshot", "_s4_undo_snapshot", "_s4_apply_state",
                "_s4_apply_state_rows", "_s4_push_undo", "_s4_undo", "_s4_redo",
                "_assembly_set_default_gap", "_assembly_set_segments",
                "_assembly_set_gap", "_assembly_set_markers",
                "_assembly_set_reading", "_assembly_edit_many",
                "_assembly_ignore", "_assembly_unignore",
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


def _w(text, start, end, src=None):
    d = {"word": text.strip(" ,.").lower(), "text": " " + text,
         "start": start, "end": end}
    if src:
        d["_src"] = src
    return d


def _file_words():
    """A stretch of tape: pull 1 keeps 10-14 s, pull 2 keeps 20-22 s."""
    return [_w("Before", 8.5, 8.9), _w("that,", 9.0, 9.5),
            _w("We", 10.2, 10.4), _w("walked", 10.5, 10.9), _w("the", 11.0, 11.1),
            _w("ridge", 11.2, 11.7), _w("before", 12.0, 12.4), _w("dawn.", 12.6, 13.2),
            _w("Anyway,", 14.3, 14.9),
            _w("Nothing", 20.2, 20.6), _w("moved", 20.7, 21.1), _w("then.", 21.2, 21.7)]


def _results():
    return [
        dict(order=1, token="GUEST", status="ok", part_index=0,
             segments=[(10.0, 14.0)], quote_text="We walked the ridge before dawn."),
        dict(order=2, token="GUEST", status="ok", part_index=0,
             segments=[(20.0, 22.0)], quote_text="Nothing moved then."),
        dict(order=3, token="GUEST", status="ok", part_index=0,
             segments=[(30.0, 32.0)], quote_text="The last line of the script."),
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
    app._seq_fps = 24.0
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
    app._ui = lambda cb: app.after(0, cb)
    app._rv = []
    for r in app.results:
        e = {"res": r, "skip_var": _Var(False), "accepted_flag": [False]}
        e["accept_fn"] = _accept_for(app, e)
        e["toggle_ignore_fn"] = (lambda e=e, **kw: (
            app._s4_push_undo(), e["skip_var"].set(not e["skip_var"].get()),
            app._s4_save()))
        app._rv.append(e)

    # The file's transcript, as the second pass would hand it over.
    app.words = _file_words()
    app._refine_words = lambda path: app.words

    # Monitoring choices are kept for the run; each test starts from the default.
    monkeypatch.setitem(assembly_view._MONITOR, "transcript", True)
    monkeypatch.setitem(assembly_view._MONITOR, "cut_words", True)
    monkeypatch.setattr(assembly_view.playback, "available", lambda: False)
    monkeypatch.setattr(os.path, "isfile",
                        lambda p, _f=os.path.isfile: p == WAV or _f(p))
    win = assembly_view.AssemblyWindow(app)
    app._assembly = win
    for _ in range(5):
        app.update()
    win.refresh()
    app.update()
    yield app, win
    win.close()
    app.destroy()


class _E(object):
    def __init__(self, x, y, state=0):
        self.x, self.y, self.state = x, y, state
        self.x_root, self.y_root = x, y


def _res(app, order):
    return next(r for r in app.results if r["order"] == order)


def _segs(app, order):
    return [tuple(round(v, 3) for v in s) for s in _res(app, order)["segments"]]


def _body(win, order):
    t = win.text
    a, b = t.tag_ranges("b{}".format(order))[:2]
    return t.get(a, b)


def _at(win, order, word):
    """The pixel of a word in the text."""
    line = win._line(order)
    tok = next(k for k in line.tokens if line.body[k.cs:k.ce] == word)
    idx = "b{}.first + {} chars".format(order, tok.cs + 1)
    win.text.see(idx)
    win.text.update_idletasks()
    x, y, _w_, h = win.text.bbox(idx)
    return x, y + h // 2


def _drag(app, win, order_a, word_a, order_b, word_b):
    xa, ya = _at(win, order_a, word_a)
    win._on_text_press(_E(xa, ya))
    xb, yb = _at(win, order_b, word_b)
    win._on_text_drag(_E(xb, yb))
    win._on_text_release(_E(xb, yb))
    app.update()


def _highlighted(win):
    rng = win.text.tag_ranges("txsel")
    return win.text.get(*rng) if rng else None


def _kept_words(win, order):
    line = win._line(order)
    return [line.body[k.cs:k.ce] for k in line.kept]


# ── The text ─────────────────────────────────────────────────────────────
def test_the_text_is_the_transcript_with_the_cut_tape_struck_through(view):
    _app, win = view
    assert _body(win, 1) == "Before that, We walked the ridge before dawn. Anyway,"
    assert _kept_words(win, 1) == ["We", "walked", "the", "ridge", "before", "dawn."]
    struck = win.text.tag_ranges("txcut")
    got = [win.text.get(a, b) for a, b in zip(struck[::2], struck[1::2])]
    assert "Before that," in got and "Anyway," in got


def test_a_pull_with_no_words_reads_as_its_script(view):
    _app, win = view
    assert _body(win, 3) == "The last line of the script."


def test_w_swaps_the_script_in_and_shift_w_hides_the_cut_words(view):
    import assembly_view
    app, win = view
    win.win.focus_force()
    win.text.focus_force()
    app.update()
    win.text.event_generate("<KeyPress>", keysym="W", when="now")
    app.update()
    assert not assembly_view._MONITOR["cut_words"]
    assert _body(win, 1) == "We walked the ridge before dawn."
    win.text.event_generate("<KeyPress>", keysym="W", when="now")
    win.text.event_generate("<KeyPress>", keysym="w", when="now")
    app.update()
    assert not assembly_view._MONITOR["transcript"]
    assert win.tx_key.lbl.cget("text") == "Script"
    assert _body(win, 2) == "Nothing moved then."
    assert win._line(1).script


def test_figures_ride_on_the_word_before_and_read_in_place():
    from assembly_view import _Clip, _transcript_line
    words = [dict(_w("In", 1.0, 1.2), extras=[{"text": " 1876,", "start": 1.3, "end": 1.9}]),
             _w("he", 2.0, 2.2)]
    c = _Clip({"start": 0, "end": 48000 * 3, "gap": 0, "segments": [(0.5, 3.5)],
               "pieces": [(0, 48000 * 3, 24000)]},
              {"order": 1}, 48000, "int", False, None)
    line = _transcript_line(c, words)
    assert line.body == "In 1876, he"
    assert [round(k.t0, 2) for k in line.kept] == [0.5, 0.8, 1.5]


def test_a_long_run_of_cut_tape_shows_its_ends_and_a_count():
    import assembly_view
    from assembly_view import _Clip, _transcript_line
    words = [_w("keep", 0.0, 0.2)] + [
        _w("w{}".format(i), 1.0 + i * 0.3, 1.2 + i * 0.3) for i in range(40)
    ] + [_w("again", 20.0, 20.2)]
    c = _Clip({"start": 0, "end": 48000, "gap": 0,
               "segments": [(0.0, 0.5), (19.9, 20.4)],
               "pieces": [(0, 24000, 0), (24000, 24000, 19.9 * 48000)]},
              {"order": 1}, 48000, "int", False, None)
    line = _transcript_line(c, words)
    cut = [t for t in line.tokens if not t.kept]
    n = assembly_view.TX_CUT_ENDS
    assert len(cut) == 2 * n + 1
    middle = cut[n]
    assert line.body[middle.cs:middle.ce] == "[{} words]".format(40 - 2 * n)
    assert middle.s0 == pytest.approx(1.0 + n * 0.3)
    assert middle.s1 == pytest.approx(1.2 + (39 - n) * 0.3)


def test_a_neighbouring_pull_keeps_its_own_words_out_of_the_handle():
    from assembly_view import _Clip, _transcript_line

    def clip(order, a, b, tl):
        sr = 48000
        return _Clip({"start": int(tl * sr), "end": int((tl + b - a) * sr), "gap": 0,
                      "segments": [(a, b)],
                      "pieces": [(int(tl * sr), int((b - a) * sr), int(a * sr))]},
                     {"order": order}, sr, "vo", False, "same.wav")
    words = [_w("one", 1.0, 1.4), _w("two", 2.1, 2.5)]
    first, second = clip(1, 0.5, 2.0, 0.0), clip(2, 2.0, 3.0, 1.5)
    assert _transcript_line(first, words, [second]).body == "one"
    assert _transcript_line(first, words).body == "one two"


# ── Highlighting ─────────────────────────────────────────────────────────
def test_dragging_over_words_selects_them_on_the_track(view):
    app, win = view
    _drag(app, win, 1, "the", 1, "before")
    assert _highlighted(win) == "the ridge before"
    o, a, b = win._range
    assert o == 1
    # Halfway to the word either side, or SEL_PAD at most.
    assert a == pytest.approx(1.0 - 0.05)     # "walked" ends 0.9, "the" starts 1.0
    assert b == pytest.approx(2.4 + 0.1)      # "before" ends 2.4, "dawn." starts 2.6
    assert win.insert == pytest.approx(a)     # play starts at the highlight
    assert win.sel_order == 1


def test_selection_edges_stop_short_and_reach_the_region_edge(view):
    app, win = view
    _drag(app, win, 1, "We", 1, "We")          # no drag to speak of: a click
    assert win._range is None
    _drag(app, win, 1, "We", 1, "walked")
    assert win._range[1] == pytest.approx(0.0)      # nothing said before it
    _drag(app, win, 1, "before", 1, "dawn.")
    assert win._range[2] == pytest.approx(4.0)      # nothing said after it
    # A long pause: only SEL_PAD of it goes with the word.
    app.words[7]["start"] = 13.0
    app.words[7]["end"] = 13.5
    win.refresh()
    _drag(app, win, 1, "ridge", 1, "ridge")
    win._on_text_double(_E(*_at(win, 1, "before")))
    assert win._range[2] == pytest.approx(2.4 + 0.15)


def test_click_places_the_insertion_point_ahead_of_the_word(view):
    app, win = view
    x, y = _at(win, 2, "moved")
    win._on_text_press(_E(x, y))
    win._on_text_release(_E(x, y))
    app.update()
    clip = win._clip(2)
    assert win._range is None
    assert win.insert == pytest.approx(clip.start + (20.65 - 20.0))
    assert win.sel_order == 2


def test_double_click_takes_the_word_and_triple_the_pull(view):
    app, win = view
    win._on_text_double(_E(*_at(win, 1, "ridge")))
    assert _highlighted(win) == "ridge"
    win._on_text_triple(_E(*_at(win, 2, "moved")))
    assert _highlighted(win) == "Nothing moved then."
    assert win._range[1] == pytest.approx(win._clip(2).start)
    assert win._range[2] == pytest.approx(win._clip(2).end)


def test_shift_click_extends_from_the_last_click(view):
    app, win = view
    x, y = _at(win, 1, "walked")
    win._on_text_press(_E(x, y))
    win._on_text_release(_E(x, y))
    x, y = _at(win, 1, "ridge")
    win._on_text_press(_E(x, y, state=0x0001))
    win._on_text_release(_E(x, y, state=0x0001))
    app.update()
    assert _highlighted(win) == "walked the ridge"


def test_the_track_selection_lights_the_words_and_back(view):
    app, win = view
    win._range = (1, 1.15, 2.45)               # 11.15 - 12.45 s
    win.redraw()
    assert _highlighted(win) == "ridge before"
    win._range = None
    win.redraw()
    assert _highlighted(win) is None
    _drag(app, win, 1, "walked", 1, "the")
    win._on_track_press(_E(win._x(3.5), win._track_box()[0] + 2))
    app.update()
    assert win._tx_anchor is None


def test_track_selector_runs_across_pulls(view):
    app, win = view
    y = win._track_box()[0] + 2
    win._on_track_press(_E(win._x(3.0), y))
    win._on_track_drag(_E(win._x(win._clip(2).start + 1.0), y))
    win._on_track_release(_E(win._x(win._clip(2).start + 1.0), y))
    app.update()
    assert "2 pulls" in win.status.cget("text")


# ── Operations ───────────────────────────────────────────────────────────
def test_delete_across_pulls_is_one_undo_step(view):
    app, win = view
    _drag(app, win, 1, "before", 2, "Nothing")
    assert _highlighted(win).startswith("before dawn.")
    depth = len(app._s4_undo_stack)
    win.delete_range()
    assert len(app._s4_undo_stack) == depth + 1
    assert _segs(app, 1) == [(10.0, 11.85)]
    assert _segs(app, 2) == [(20.65, 22.0)]
    assert win._range is None and win._tx_anchor is None
    app._s4_undo()
    assert _segs(app, 1) == [(10.0, 14.0)]
    assert _segs(app, 2) == [(20.0, 22.0)]


def test_delete_of_every_word_of_a_pull_ignores_it(view):
    app, win = view
    win._on_text_triple(_E(*_at(win, 2, "moved")))
    win.delete_range()
    app.update()
    assert app._rv[1]["skip_var"].get()
    assert win._clip(2) is None
    win.refresh()
    assert _body(win, 2).endswith("(not in the cut)")
    # ...and highlighting that line and pressing H brings it back.
    win._on_text_triple(_E(*_at_ghost(win, 2)))
    win.restore()
    win.refresh()
    assert not app._rv[1]["skip_var"].get()
    assert win._clip(2) is not None


def _at_ghost(win, order):
    idx = "b{}.first + 2 chars".format(order)
    win.text.see(idx)
    win.text.update_idletasks()
    x, y, _w_, h = win.text.bbox(idx)
    return x, y + h // 2


def test_a_deleted_word_stays_struck_through_and_h_puts_it_back(view):
    app, win = view
    win._on_text_double(_E(*_at(win, 1, "ridge")))
    win.delete_range()
    win.refresh()
    assert _kept_words(win, 1) == ["We", "walked", "the", "before", "dawn."]
    assert "ridge" in _body(win, 1)
    assert len(_segs(app, 1)) == 2

    win._on_text_double(_E(*_at(win, 1, "ridge")))
    assert win._range is None                  # nothing of it on the track
    assert "H puts them back" in win.status.cget("text")
    win.restore()
    win.refresh()
    assert _segs(app, 1) == [(10.0, 14.0)]     # healed: one region again
    assert _kept_words(win, 1) == ["We", "walked", "the", "ridge", "before", "dawn."]


def test_h_on_the_handle_opens_the_pull_up(view):
    app, win = view
    win._on_text_double(_E(*_at(win, 1, "Anyway,")))
    win.restore()
    win.refresh()
    segs = _segs(app, 1)
    assert segs[0][0] == 10.0 and segs[-1][1] == pytest.approx(14.9 + 0.15)
    assert "Anyway," in _kept_words(win, 1)


def test_split_across_pulls_is_one_undo_step(view):
    app, win = view
    _drag(app, win, 1, "before", 2, "moved")
    depth = len(app._s4_undo_stack)
    win.split()
    assert len(app._s4_undo_stack) == depth + 1
    assert len(_segs(app, 1)) == 2 and len(_segs(app, 2)) == 2
    assert win._range is None


def test_split_at_a_clicked_word_cuts_in_the_gap_before_it(view):
    app, win = view
    x, y = _at(win, 1, "before")
    win._on_text_press(_E(x, y))
    win._on_text_release(_E(x, y))
    win.split()
    assert _segs(app, 1) == [(10.0, 11.85), (11.85, 14.0)]
    assert win.sel_region == (1, 1)


def test_music_marks_land_on_the_ends_of_the_highlight(view):
    app, win = view
    _drag(app, win, 1, "the", 1, "before")
    a, b = sorted(win._range[1:])
    win.drop_marker("music_in")
    win.drop_marker("music_out")
    got = {m["kind"]: m["t"] for m in win.markers}
    assert got["music_in"] == pytest.approx(a, abs=1e-3)
    assert got["music_out"] == pytest.approx(b, abs=1e-3)


def test_copy_takes_the_highlighted_words(view):
    app, win = view
    _drag(app, win, 1, "the", 2, "Nothing")
    win.copy_words()
    assert win.win.clipboard_get() == "the ridge before dawn.\nNothing"


def test_the_script_view_highlights_through_its_timing(view):
    import assembly_view
    app, win = view
    win.toggle_transcript()
    c = win._clip(1)
    c.words = win._map_words(c)
    assert not assembly_view._MONITOR["transcript"]
    _drag(app, win, 1, "the", 1, "ridge")
    assert _highlighted(win) == "the ridge"
    assert win._range[1] == pytest.approx(0.95)
    win.delete_range()
    assert len(_segs(app, 1)) == 2


def test_right_click_selects_the_word_and_offers_the_operations(view, monkeypatch):
    app, win = view
    seen = []

    def _popup(self, x, y):
        seen.extend(self.entrycget(i, "label") for i in range(self.index("end") + 1)
                    if self.type(i) == "command")
    monkeypatch.setattr(tk.Menu, "tk_popup", _popup)
    win._on_text_menu(_E(*_at(win, 1, "ridge")))
    assert _highlighted(win) == "ridge"
    for want in ("Play selection (Space)", "Delete (Del)", "Restore (H)", "Split (B)",
                 "Music in (I)", "Music out (O)", "Inflection (U)", "SFX (M)",
                 "Note (K)", "Copy (Ctrl+C)", "Open in editor (Enter)"):
        assert want in seen
    # Right-clicking inside a highlight keeps it.
    _drag(app, win, 1, "walked", 1, "before")
    win._on_text_menu(_E(*_at(win, 1, "ridge")))
    assert _highlighted(win) == "walked the ridge before"


def test_karaoke_lights_the_transcript(view):
    app, win = view

    def lit():
        rng = win.text.tag_ranges("word")
        return win.text.get(*rng) if rng else None
    win.set_insert(1.4)                        # 11.4 s: "ridge"
    assert lit() == "ridge"


# ── Words arriving ───────────────────────────────────────────────────────
def test_refined_words_come_in_under_the_highlight_and_the_scroll(view):
    app, win = view
    win._on_text_double(_E(*_at(win, 1, "ridge")))
    before = win._range
    # The refine hears the ridge better, and a word in front of it.
    app.words = [w for w in app.words if w["start"] < 11.0 or w["start"] > 11.8]
    app.words += [_w("craggy", 11.05, 11.15), _w("ridgeline", 11.2, 11.7)]
    app.words.sort(key=lambda w: w["start"])
    win.transcript_changed(WAV)
    win._tx_render_now()
    app.update()
    assert "ridgeline" in _body(win, 1)
    assert win._range == before
    assert _highlighted(win) == "ridgeline"


def test_a_render_waits_for_a_highlight_being_dragged(view):
    app, win = view
    x, y = _at(win, 1, "the")
    win._on_text_press(_E(x, y))
    app.words = [dict(w, text=" CHANGED") if w["word"] == "ridge" else w for w in app.words]
    win.transcript_changed(WAV)
    win._tx_render_now()
    assert "CHANGED" not in _body(win, 1)
    win._on_text_release(_E(x, y))
    win._tx_render_now()
    assert "CHANGED" in _body(win, 1)


def test_draft_words_read_faded_while_the_pass_runs(view):
    app, win = view
    app.words = [dict(w, _src="tiny") for w in app.words]
    app._refine_running = lambda: True
    win.refresh()
    faded = win.text.tag_ranges("txdraft")
    got = " ".join(win.text.get(a, b) for a, b in zip(faded[::2], faded[1::2]))
    assert "We walked the ridge before dawn." in got
    app._refine_running = lambda: False
    win.show_refine_status()
    win._tx_render_now()
    assert not win.text.tag_ranges("txdraft")


def test_the_panel_says_where_the_pass_is_and_offers_the_new_cut(view):
    app, win = view
    app._refine_status = lambda: "Refining with medium: 40%"
    app._refine_held = lambda: True
    win.show_refine_status()
    app.update()
    assert win.tx_status.cget("text") == "Refining with medium: 40%"
    assert win.recut_key is not None
    assert win.refine_key.lbl.cget("text").startswith("Refine with ")
    app._refine_held = lambda: False
    win.show_refine_status()
    assert win.recut_key is None


def test_a_background_change_keeps_the_place_on_the_same_audio(view):
    app, win = view
    win._on_text_double(_E(*_at(win, 2, "moved")))
    src_in = win._clip(2).src_at(win._range[1])
    place = win.capture_place()
    _res(app, 1)["segments"] = [(10.0, 12.0)]  # two seconds shorter ahead of it
    win.refresh()
    win.restore_place(place)
    assert win._clip(2).src_at(win._range[1]) == pytest.approx(src_in)
    assert _highlighted(win) == "moved"
    assert win._tx_anchor is not None          # still the words picked


def test_busy_while_listening_or_mid_gesture(view):
    _app, win = view
    assert not win.busy()
    win._playing = True
    assert win.busy()
    win._playing = False
    x, y = _at(win, 1, "the")
    win._on_text_press(_E(x, y))
    assert win.busy()
    win._on_text_release(_E(x, y))
    assert not win.busy()
