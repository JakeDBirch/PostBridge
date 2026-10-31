"""The Assembly's text pane: the cut as words, highlight them to edit the audio.

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

from conftest import open_assembly  # noqa: E402
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

    monkeypatch.setitem(assembly_view._MONITOR, "nudge_s", 0.1)
    monkeypatch.setattr(assembly_view.playback, "available", lambda: False)
    monkeypatch.setattr(os.path, "isfile",
                        lambda p, _f=os.path.isfile: p == WAV or _f(p))
    win = open_assembly(app)
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


def _all_text(win):
    return win.text.get("1.0", "end")


def _at(win, order, word):
    """The pixel of a word in the text."""
    line = win._line(order)
    tok = next(k for k in line.tokens if line.body[k.cs:k.ce] == word)
    return _pixel(win, order, tok.cs)


def _pixel(win, order, cs):
    idx = "b{}.first + {} chars".format(order, cs + 1)
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


def _tagged(win, tag):
    rng = win.text.tag_ranges(tag)
    return [win.text.get(a, b) for a, b in zip(rng[::2], rng[1::2])]


def _kept_words(win, order):
    line = win._line(order)
    return [line.body[k.cs:k.ce] for k in line.kept]


# ── What the text shows ──────────────────────────────────────────────────
def test_the_text_is_only_the_cut(view):
    _app, win = view
    assert _body(win, 1) == "We walked the ridge before dawn."
    text = _all_text(win)
    assert "Before that," not in text and "Anyway," not in text   # cut tape
    assert "#001" not in text
    assert not _tagged(win, "txstruck")


def test_marks_are_not_in_the_text(view):
    app, win = view
    win.set_insert(1.0)
    win.drop_marker("music_inflection")
    win.refresh()
    assert len(win.markers) == 1
    assert "Inflection" not in _all_text(win)


def test_a_pull_with_no_words_reads_as_its_script(view):
    _app, win = view
    assert _body(win, 3) == "The last line of the script."
    assert not win._line(3).tokens


def test_a_speaker_is_named_where_the_speaker_changes(view):
    app, win = view
    _res(app, 2)["token"] = "HOST"
    win.refresh()
    assert _all_text(win).split("\n")[1:3] == ["GUEST", "We walked the ridge before dawn."]
    assert "HOST\nNothing moved then." in _all_text(win)
    assert "GUEST\nThe last line" in _all_text(win)      # back again
    # The same speaker on: not named twice.
    _res(app, 2)["token"] = "GUEST"
    win.refresh()
    assert _all_text(win).count("GUEST") == 1


def test_a_narration_has_no_names_or_headings(view):
    app, win = view
    app._monolog = True
    win.refresh()
    assert _all_text(win).split("\n")[0] == "We walked the ridge before dawn."
    assert "GUEST" not in _all_text(win) and "Part" not in _all_text(win)


def _line_from(script, *said, fade=False):
    from assembly_view import _norm_word, _script_ops, _text_line
    units = []
    t = 0.0
    for text in said:
        src = None
        if isinstance(text, tuple):
            text, src = text
        units.append((t, t + 0.3, t, t + 0.3, text, False, src, 0))
        t += 0.4
    ops = _script_ops(script, [_norm_word(u[4]) for u in units])
    return _text_line(script, units, ops, fade)


def _read(line):
    """The line with its marks spelled out: ~~struck~~ and {tinted}."""
    out = ""
    for text, style in line.chunks:
        out += {"txstruck": "~~{}~~", "txsaid": "{{{}}}", "txdraft": "<{}>"}.get(
            style, "{}").format(text)
    return out


def test_script_wording_not_said_is_struck_and_said_wording_tinted():
    line = _line_from("We are going to walk the ridge.",
                      "We're", "gonna", "walk", "the", "ridge.")
    assert _read(line) == "~~We are going to~~ {We're gonna} walk the ridge."
    kept = [line.body[t.cs:t.ce] for t in line.kept]
    assert kept == ["We're", "gonna", "walk", "the", "ridge."]
    struck = [t for t in line.tokens if not t.kept]
    assert [line.body[t.cs:t.ce] for t in struck] == ["We", "are", "going", "to"]
    assert all(t.t0 is None and t.s0 is None for t in struck)


def test_a_word_left_out_and_a_word_added():
    assert _read(_line_from("He rode on alone.", "He", "rode", "alone.")) == \
        "He rode ~~on~~ alone."
    assert _read(_line_from("He rode alone.", "He", "rode", "um", "alone.")) == \
        "He rode {um} alone."


def test_the_same_words_are_not_a_difference():
    # Casing and punctuation.
    assert _read(_line_from("He rode, alone.", "he", "rode", "alone")) == "He rode, alone."
    # Whisper's spelling of a name: the script's stands.
    assert _read(_line_from("Hardin rode on.", "harden", "rode", "on")) == "Hardin rode on."
    # That slack is for names.  A word said differently is a difference.
    assert _read(_line_from("The killings stopped.", "the", "killing", "stopped")) == \
        "The ~~killings~~ {killing} stopped."
    assert _read(_line_from("He will ride on.", "he", "will", "rode", "on")) == \
        "He will ~~ride~~ {rode} on."
    # One word or two.
    line = _line_from("A hyper-violent man.", "a", "hyper", "violent", "man")
    assert _read(line) == "A hyper-violent man."
    assert [line.body[t.cs:t.ce] for t in line.kept] == ["A", "hyper-violent", "man."]
    # ...and the other way about, each word with the time they share.
    line = _line_from("He was hyper violent.", "he", "was", "hyper-violent")
    assert _read(line) == "He was hyper violent."
    assert line.kept[2].t0 == line.kept[3].t0 and line.kept[2].t1 == line.kept[3].t1


def test_the_scripts_paragraphs_and_dashes_stand():
    line = _line_from("One two three.\nFour — five.", "one", "two", "three", "four", "five")
    assert _read(line) == "One two three.\nFour — five."


def test_differences_wait_for_a_better_model_than_the_draft():
    tiny = lambda w: (w, "tiny")
    line = _line_from("We walked the ridge.",
                      tiny("We"), tiny("marched"), tiny("the"), tiny("rich"), fade=True)
    assert _read(line) == "<We> <marched> <the> <rich>"
    # Once the better model has heard it, the same difference gets noted.
    line = _line_from("We walked the ridge.", "We", "marched", "the", "rich", fade=True)
    assert _read(line) == "We ~~walked~~ {marched} the ~~ridge.~~ {rich}"
    # With tiny the model chosen, nothing is coming: it is noted.
    line = _line_from("We walked the ridge.",
                      tiny("We"), tiny("marched"), tiny("the"), tiny("rich"), fade=False)
    assert _read(line) == "We ~~walked~~ {marched} the ~~ridge.~~ {rich}"


def test_figures_are_in_the_comparison(view):
    from assembly_view import _word_units
    w = dict(_w("In", 1.0, 1.2), extras=[{"text": " 1876,", "start": 1.3, "end": 1.9}])
    assert [u[2] for u in _word_units(w)] == ["In", "1876,"]
    assert _read(_line_from("In 1876, he.", "In", "1876,", "he")) == "In 1876, he."


def test_a_pull_with_no_script_reads_as_said_in_paragraphs():
    from assembly_view import TX_PARA_CHARS, _plain_line
    units = [(i * 0.4, i * 0.4 + 0.3, i * 0.4, i * 0.4 + 0.3,
              "sentence{}.".format(i) if i % 10 == 9 else "word{}".format(i),
              False, None, 0) for i in range(400)]
    line = _plain_line(units)
    paras = line.body.split("\n")
    assert len(paras) > 2
    for p in paras[:-1]:
        assert len(p) >= TX_PARA_CHARS and p.endswith(".")
    assert line.body[line.tokens[-1].cs:line.tokens[-1].ce] == "sentence399."


def test_a_line_not_in_the_cut_is_struck(view):
    app, win = view
    app._rv[1]["skip_var"].set(True)
    win.refresh()
    assert _tagged(win, "txstruck") == ["Nothing moved then."]
    assert "(not in the cut)" not in _all_text(win)
    # ...a line never found, too: script wording that wasn't said.
    _res(app, 3)["status"] = "no_match"
    _res(app, 3)["segments"] = []
    win.refresh()
    assert "The last line of the script." in _tagged(win, "txstruck")


def test_deleting_a_word_of_the_script_strikes_it(view):
    app, win = view
    win._on_text_double(_E(*_at(win, 1, "ridge")))
    win.delete_range()
    win.refresh()
    assert _kept_words(win, 1) == ["We", "walked", "the", "before", "dawn."]
    assert _tagged(win, "txstruck") == ["ridge"]
    assert len(_segs(app, 1)) == 2
    app._s4_undo()
    win.refresh()
    assert not _tagged(win, "txstruck")
    assert _segs(app, 1) == [(10.0, 14.0)]


def test_deleting_a_word_not_in_the_script_makes_it_go(view):
    app, win = view
    _res(app, 1)["quote_text"] = "We walked the before dawn."
    win.refresh()
    assert _tagged(win, "txsaid") == ["ridge"]
    win._on_text_double(_E(*_at(win, 1, "ridge")))
    win.delete_range()
    win.refresh()
    assert "ridge" not in _all_text(win)
    assert not _tagged(win, "txsaid")


def test_a_redraw_only_lines_up_the_pull_that_changed(view, monkeypatch):
    import assembly_view
    app, win = view
    calls = []
    real = assembly_view._script_ops
    monkeypatch.setattr(assembly_view, "_script_ops",
                        lambda s, w: (calls.append(s), real(s, w))[1])
    win._render_script()
    assert not calls                           # nothing has changed
    app.words = [dict(w, word="changed", text=" changed") if w["word"] == "ridge" else w
                 for w in app.words]
    win._render_script()
    assert calls == ["We walked the ridge before dawn."]


def test_other_reading_shows_only_when_a_pull_has_one(view):
    app, win = view
    assert win.reading_key is None
    _res(app, 1)["monolog"] = True
    _res(app, 1)["takes_data"] = [{"segments": [(10.0, 14.0)], "apath": WAV}]
    win.refresh()
    assert win.reading_key is None             # a monolog with only the one
    _res(app, 1)["takes_data"].append({"segments": [(40.0, 44.0)], "apath": WAV})
    win.refresh()
    assert win.reading_key is not None
    assert win.reading_key.frame.winfo_exists()
    _res(app, 1)["takes_data"].pop()           # a re-cut changes the answer
    win.refresh()
    assert win.reading_key is None


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
    assert win.insert == pytest.approx(a)     # the insertion point goes to its start
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
    app.words = [dict(w) for w in app.words]   # new words come as a new list
    app.words[7]["start"] = 13.0
    app.words[7]["end"] = 13.5
    win.refresh()
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


def test_a_highlight_goes_across_struck_words(view):
    app, win = view
    _res(app, 1)["quote_text"] = "We walked over the ridge before dawn."
    win.refresh()
    assert _tagged(win, "txstruck") == ["over"]
    _drag(app, win, 1, "walked", 1, "the")
    assert _highlighted(win) == "walked over the"
    assert win._range[2] == pytest.approx(1.15)
    # A struck word alone has no audio: nothing on the track.
    win._on_text_double(_E(*_at(win, 1, "over")))
    assert win._range is None
    assert "not said" in win.status.cget("text")
    # Clicking one goes to where the audio resumes.
    x, y = _at(win, 1, "over")
    win._on_text_press(_E(x, y))
    win._on_text_release(_E(x, y))
    assert win.insert == pytest.approx(0.95)


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


def _labels(menu):
    return [menu.type(i) == "command" and menu.entrycget(i, "label")
            for i in range(menu.index("end") + 1)]


def test_delete_of_every_word_of_a_pull_ignores_it_and_it_can_come_back(view, monkeypatch):
    app, win = view
    win._on_text_triple(_E(*_at(win, 2, "moved")))
    win.delete_range()
    app.update()
    assert app._rv[1]["skip_var"].get()
    assert win._clip(2) is None
    win.refresh()
    assert _tagged(win, "txstruck") == ["Nothing moved then."]

    menus = []
    monkeypatch.setattr(tk.Menu, "tk_popup", lambda self, x, y: menus.append(self))
    win._on_text_menu(_E(*_pixel(win, 2, 2)))
    m = menus[0]
    m.invoke(_labels(m).index("Bring back into the cut"))
    assert not app._rv[1]["skip_var"].get()
    assert win._clip(2) is not None
    assert not _tagged(win, "txstruck")


def test_a_line_never_found_offers_no_bring_back(view, monkeypatch):
    app, win = view
    _res(app, 3)["status"] = "no_match"
    _res(app, 3)["segments"] = []
    win.refresh()
    x, y = _pixel(win, 3, 2)
    win._on_text_press(_E(x, y))
    win._on_text_release(_E(x, y))
    assert "No audio was found" in win.status.cget("text")
    assert win.sel_order == 3
    win.open_selected()
    assert app.opened == [3]
    menus = []
    monkeypatch.setattr(tk.Menu, "tk_popup", lambda self, x, y: menus.append(self))
    win._on_text_menu(_E(x, y))
    assert "Bring back into the cut" not in _labels(menus[0])


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


def test_a_mark_lands_at_the_start_of_the_highlight(view):
    app, win = view
    _drag(app, win, 1, "the", 1, "before")
    a = min(win._range[1:])
    win.drop_marker("music_inflection")
    assert win.markers[0]["t"] == pytest.approx(a, abs=1e-3)


def test_copy_takes_the_highlighted_words(view):
    app, win = view
    _drag(app, win, 1, "the", 2, "Nothing")
    win.copy_words()
    assert win.win.clipboard_get() == "the ridge before dawn.\nNothing"


def test_a_script_of_several_paragraphs_highlights_across_them(view):
    app, win = view
    _res(app, 1)["quote_text"] = "We walked the ridge.\nBefore dawn."
    win.refresh()
    assert _body(win, 1) == "We walked the ridge.\nBefore dawn."
    _drag(app, win, 1, "ridge.", 1, "Before")
    assert _highlighted(win) == "ridge.\nBefore"
    assert win._range[1] == pytest.approx(1.15)
    assert win._range[2] == pytest.approx(2.5)
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
    for want in ("Play from here (Space)", "Delete (Del)", "Split (B)",
                 "Music inflection (M)", "SFX (X)", "Note (N)",
                 "Copy (Ctrl+C)", "Open in editor (Enter)"):
        assert want in seen
    assert not any("Restore" in s or "Music in " in s for s in seen)
    # Right-clicking inside a highlight keeps it.
    _drag(app, win, 1, "walked", 1, "before")
    win._on_text_menu(_E(*_at(win, 1, "ridge")))
    assert _highlighted(win) == "walked the ridge before"


def test_karaoke_lights_the_word(view):
    app, win = view

    def lit():
        rng = win.text.tag_ranges("word")
        return win.text.get(*rng) if rng else None
    win.set_insert(1.4)                        # 11.4 s: "ridge"
    assert lit() == "ridge"


def test_the_retired_keys_do_nothing(view):
    app, win = view
    win._on_text_double(_E(*_at(win, 1, "ridge")))
    win.delete_range()
    win.refresh()
    before = _all_text(win), _segs(app, 1)
    win.win.focus_force()
    win.text.focus_force()
    app.update()
    for k in ("h", "w", "W"):
        win.text.event_generate("<KeyPress>", keysym=k, when="now")
    app.update()
    assert (_all_text(win), _segs(app, 1)) == before


# ── Words arriving ───────────────────────────────────────────────────────
def test_refined_words_come_in_under_the_highlight(view):
    app, win = view
    win._on_text_double(_E(*_at(win, 1, "ridge")))
    before = win._range
    # The refine hears something else at the ridge.
    app.words = [w for w in app.words if w["start"] < 11.0 or w["start"] > 11.8]
    app.words += [_w("the", 11.0, 11.1), _w("ridgeline", 11.2, 11.7)]
    app.words.sort(key=lambda w: w["start"])
    win.transcript_changed(WAV)
    win._tx_render_now()
    app.update()
    assert _tagged(win, "txstruck") == ["ridge"]
    assert _tagged(win, "txsaid") == ["ridgeline"]
    assert win._range == before
    assert _highlighted(win) == "ridgeline"


def test_a_render_waits_for_a_highlight_being_dragged(view):
    app, win = view
    x, y = _at(win, 1, "the")
    win._on_text_press(_E(x, y))
    app.words = [dict(w, word="changed", text=" CHANGED") if w["word"] == "ridge" else w
                 for w in app.words]
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
    assert "We walked the ridge before dawn." in " ".join(_tagged(win, "txdraft"))
    app._refine_running = lambda: False
    win.show_refine_status()
    win._tx_render_now()
    assert not _tagged(win, "txdraft")


# ── The second pass, on screen ───────────────────────────────────────────
def test_the_panel_shows_the_pass_running(view):
    app, win = view
    app._refine_running = lambda: True
    app._refine_status = lambda: "Refining with medium: 40%"
    app._refine_progress = lambda: 0.4
    app._refine_short = lambda: "Refining with medium  40%"
    win.show_refine_status()
    app.update()
    assert win.tx_status.cget("text") == "Refining with medium: 40%"
    assert win.refine_key.lbl.cget("text") == "Cancel refining"
    assert win.refine_key.on
    assert win.refine_bar._pct == pytest.approx(0.4)
    assert "40%" in win.load_lbl.cget("text")          # always in view


def test_the_panels_button_starts_a_pass_and_cancels_a_running_one(view):
    app, win = view
    calls = []
    app._refine_again = lambda: calls.append("again")
    app._refine_cancel = lambda: calls.append("cancel")
    app._refine_running = lambda: False
    win._refine_again()
    app._refine_running = lambda: True
    win._refine_again()
    assert calls == ["again", "cancel"]


def test_the_panel_settles_when_it_finishes_and_offers_the_new_cut(view):
    app, win = view
    app._refine_running = lambda: True
    app._refine_progress = lambda: 0.4
    win.show_refine_status()
    app._refine_running = lambda: False
    app._refine_progress = lambda: None
    app._refine_short = lambda: ""
    app._refine_held = lambda: True
    win.show_refine_status()
    app.update()
    assert win.refine_key.lbl.cget("text") == "Refine with {}".format(
        engines.get_active_model_size())
    assert not win.refine_key.on
    assert win.refine_bar._pct == 0.0
    assert "Refining" not in win.load_lbl.cget("text")
    assert win.recut_key is not None
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


# ── Drawing only what changed ───────────────────────────────────────────────
def _dump(win):
    """The text and every tag on it but the ones that come and go."""
    t = win.text
    tags = {g: [str(i) for i in t.tag_ranges(g)] for g in t.tag_names()
            if g not in ("sel", "word", "cur", "sel_pull", "txsel")}
    return t.get("1.0", "end"), {g: r for g, r in tags.items() if r}


def _watch_inserts(win, monkeypatch):
    calls = []
    t = win.text
    real_ins, real_del = t.insert, t.delete
    monkeypatch.setattr(t, "insert", lambda *a: (calls.append(a), real_ins(*a))[1])
    monkeypatch.setattr(t, "delete", lambda *a: (calls.append(a), real_del(*a))[1])
    return calls


def test_render_with_nothing_new_leaves_the_text_alone(view, monkeypatch):
    _app, win = view
    calls = _watch_inserts(win, monkeypatch)
    assert win._render_script() is False
    assert calls == []


def test_new_words_replace_only_their_own_pull(view, monkeypatch):
    app, win = view
    calls = _watch_inserts(win, monkeypatch)
    app.words = [dict(w) for w in app.words]
    app.words[-1]["text"] = " afterwards."
    assert win._render_script() is True
    assert "afterwards." in _body(win, 2)
    assert "We walked the ridge" in _body(win, 1)
    # One delete and one insert, with pull 1 left where it was.
    assert [len(c) > 2 for c in calls] == [False, True]
    assert win.text.compare(calls[0][0], ">=", win.text.tag_ranges("o1")[-1])


def test_patched_text_is_what_a_full_drawing_gives(view):
    """Text and tags alike, or the words stop lining up with their timings."""
    app, win = view
    app.words = [dict(w) for w in app.words]
    app.words[3]["text"] = " wandered"                 # pull 1
    app.words[-2]["text"] = " shifted"                 # pull 2
    win._render_script()
    patched = _dump(win)
    win._tx_shape = None
    win._render_script()
    assert _dump(win) == patched


def test_a_pull_gaining_its_first_words_redraws_the_shape(view):
    app, win = view
    before = win._tx_shape
    app.results.append(dict(order=4, token="HOST", status="ok", part_index=0,
                            segments=[], quote_text="A new speaker's line."))
    win._render_script()
    assert win._tx_shape != before
    assert "HOST\n" in _all_text(win)


def test_karaoke_and_highlight_survive_a_patch(view):
    app, win = view
    _drag(app, win, 2, "Nothing", 2, "moved")
    win.playhead = win._clip(1).start + 0.8
    win._light_script_word()
    assert win._lit_script is not None
    app.words = [dict(w) for w in app.words]
    app.words[-1]["text"] = " afterwards."
    win._tx_render_now()
    assert _highlighted(win) == "Nothing moved"
    t = win.text
    assert t.tag_ranges("word")                        # lit again, not lost


def test_words_off_screen_do_not_redraw_the_track(view, monkeypatch):
    app, win = view
    drawn = []
    monkeypatch.setattr(win, "redraw", lambda: drawn.append(True))
    win.view0, win.pps = 1000.0, 100.0                 # well past the cut
    app.words = [dict(w) for w in app.words]
    app.words[-1]["text"] = " afterwards."
    win.transcript_changed(win.clips[0].play_path)
    win._tx_render_now()
    assert drawn == []
    win.view0 = 0.0
    app.words = [dict(w) for w in app.words]
    app.words[3]["text"] = " wandered"
    win.transcript_changed(win.clips[0].play_path)
    win._tx_render_now()
    assert drawn == [True]


def test_scrolling_stays_put_when_a_pull_below_changes(view):
    """And with one above it changing, the same words stay at the top."""
    app, win = view
    win.text.config(height=2)
    app.update()
    win.text.yview("o2.first")
    app.update()
    top = win._text_top()
    app.words = [dict(w) for w in app.words]
    app.words[3]["text"] = " wandered off along"       # pull 1, above
    win._render_script()
    app.update()
    assert win._text_top() == top


def test_wheel_notches_make_one_redraw(view, monkeypatch):
    app, win = view
    app.update()
    drawn = []
    monkeypatch.setattr(win, "redraw", lambda: drawn.append(True))

    class _W(object):
        delta, state, x = -120, 0, 10
    for _ in range(6):
        win._on_wheel(_W())
    assert drawn == []
    app.update()
    assert drawn == [True]


def test_overview_strip_is_only_drawn_again_when_the_cut_changes(view):
    app, win = view
    win.pps = 2000.0                                   # zoomed in: room to scroll
    win.redraw()
    box = win.ov.find_withtag("view")
    win.view0 += 1.0
    win.redraw()
    assert win.ov.find_withtag("view") == box          # moved, not remade
    x0 = win.ov.coords("view")[0]
    win.view0 += 1.0
    win.redraw()
    assert win.ov.coords("view")[0] != x0
    _res(app, 1)["segments"] = [(10.0, 13.0)]
    win.refresh()
    assert win.ov.find_withtag("view") != box


def test_closing_takes_every_timer_with_it(view):
    """One left behind fires by a command name a new widget's handler may have
    been given since: the TypeError from _SlimScrollbar's <Configure> lambda."""
    app, win = view
    w = win.win
    job = w.after(60000, lambda: None)
    glide = w.after_idle(lambda: None)
    other = app.after(60000, lambda: None)
    win.close()
    pending = app.tk.splitlist(app.tk.call("after", "info"))
    assert job not in pending and glide not in pending
    assert other in pending
    app.after_cancel(other)
