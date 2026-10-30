"""Two passes: tiny first, then the chosen model behind the screen.

The engine half runs a stand-in Whisper; the refine half a stand-in
transcribe_file that hands words over a few at a time.  The app is the real
merge code (main.App's Step 4 methods) on a plain object, without a screen.
"""
import json
import os
import queue
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import engines  # noqa: E402
import monolog  # noqa: E402
from refine import RefineMixin  # noqa: E402


def _seg(words, start=None, end=None):
    ws = [SimpleNamespace(word=w, start=a, end=b) for w, a, b in words]
    return SimpleNamespace(words=ws, start=start if start is not None else words[0][1],
                           end=end if end is not None else words[-1][2],
                           text="".join(w for w, _a, _b in words))


class _Model(object):
    def __init__(self, segments):
        self.segments = segments
        self.calls = []

    def transcribe(self, path, **kw):
        self.calls.append(path)
        return iter(self.segments), None


@pytest.fixture
def whisper(monkeypatch):
    """engines.get_model hands back a stand-in, and remembers the size asked for."""
    asked = []
    model = _Model([_seg([(" In", 1.0, 1.2), (" 1876,", 1.3, 1.9), (" he", 2.0, 2.2)]),
                    _seg([(" Rode", 3.0, 3.3), (" on.", 3.4, 3.8)])])

    def _get(size=None):
        asked.append(size)
        return model
    monkeypatch.setattr(engines, "get_model", _get)
    return SimpleNamespace(asked=asked, model=model)


# ── Engines ──────────────────────────────────────────────────────────────
def test_transcribe_clip_keeps_both_forms_of_a_word(whisper):
    words = engines.transcribe_clip("x.wav", model_size="tiny")
    assert whisper.asked == ["tiny"]
    assert [w["word"] for w in words] == ["in", "he", "rode", "on"]
    assert [w["text"] for w in words] == [" In", " he", " Rode", " on."]
    assert {w["_src"] for w in words} == {"tiny"}
    # A figure means nothing to a matcher; it rides on the word before.
    assert words[0]["extras"] == [{"text": " 1876,", "start": 1.3, "end": 1.9}]


def test_transcribe_clip_streams_and_stops(whisper):
    heard = []
    engines.transcribe_clip("x.wav", segment_cb=heard.append)
    assert [[w["word"] for w in seg] for seg in heard] == [["in", "he"], ["rode", "on"]]
    stop = threading.Event()
    stop.set()
    with pytest.raises(engines.TranscriptionCancelled):
        engines.transcribe_clip("x.wav", cancel_event=stop)


def test_transcribe_in_chunks_puts_words_in_file_time(whisper, monkeypatch):
    monkeypatch.setattr(engines, "extract_window", lambda *a, **k: (True, ""))
    heard = []
    words = engines.transcribe_in_chunks("x.wav", [(100.0, 110.0)], model_size="tiny",
                                         segment_cb=heard.extend)
    assert words[0]["start"] == 101.0
    assert words[0]["extras"][0]["start"] == 101.3
    assert heard[0]["start"] == 101.0 and heard[0]["_src"] == "tiny"


def test_draft_model_is_tiny_unless_tiny_is_chosen(monkeypatch):
    monkeypatch.setattr(engines, "_active_model_override", "medium")
    assert engines.draft_model() == "tiny"
    monkeypatch.setattr(engines, "_active_model_override", "tiny")
    assert engines.draft_model() is None


def test_saved_transcripts_say_which_model_made_them(tmp_path, monkeypatch):
    wav = tmp_path / "read.wav"
    wav.write_bytes(b"\x00" * 64)
    monkeypatch.setattr(engines, "_cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(engines, "_active_model_override", "medium")
    assert engines.transcript_model(str(wav)) is None
    words = [{"word": "w", "start": 0.0, "end": 0.1}]
    engines.pb_transcript_save(str(wav), words, model="tiny")
    assert engines.transcript_model(str(wav)) == "tiny"
    engines.pb_transcript_save(str(wav), words)            # the active one
    assert engines.transcript_model(str(wav)) == "medium"
    os.remove(engines.pb_transcript_path(str(wav)))
    engines.cache_save(str(wav), words, model="tiny")
    assert engines.transcript_model(str(wav)) == "tiny"
    assert engines.cache_load(str(wav)) == words


def test_a_draft_pull_is_labelled_and_not_cached(whisper, tmp_path, monkeypatch):
    monkeypatch.setattr(engines, "extract_window", lambda *a, **k: (True, ""))
    monkeypatch.setattr(engines, "detect_speech_blobs", lambda *a, **k: None)
    monkeypatch.setattr(engines, "_cache_dir", str(tmp_path))
    monkeypatch.setattr(engines, "_active_model_override", "medium")
    wav = tmp_path / "guest.wav"
    wav.write_bytes(b"\x00" * 64)
    saved = []
    monkeypatch.setattr(engines, "pull_result_cache_save",
                        lambda *a: saved.append(a))
    pull = _pull(1, 5.0, 10.0, "he rode on")
    r = engines.reconcile_interview_pull(pull, str(wav), pad=1.0, model_size="tiny")
    assert r["_tx_model"] == "tiny" and r["status"] == "ok"
    assert r["words"][0]["start"] == pytest.approx(5.0)    # window starts 4 s in
    assert r["words"][0]["extras"][0]["start"] == pytest.approx(5.3)
    assert not saved
    r = engines.reconcile_interview_pull(pull, str(wav), pad=1.0)
    assert r["_tx_model"] == "medium"
    assert saved


def test_a_transcript_keeps_its_label_on_a_machine_with_another_model(tmp_path, monkeypatch):
    """Jordan's words, saved again here with something else chosen."""
    wav = tmp_path / "read.wav"
    wav.write_bytes(b"\x00" * 64)
    monkeypatch.setattr(engines, "_cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(engines, "_active_model_override", "large-v3")
    words = [_word("we", 0.0, 0.1, "small"), _word("rode", 0.2, 0.3, "small"),
             _word("West", 0.4, 0.5, "user")]
    engines.pb_transcript_save(str(wav), words)
    assert engines.transcript_model(str(wav)) == "small"
    os.remove(engines.pb_transcript_path(str(wav)))
    engines.cache_save(str(wav), words)
    assert engines.transcript_model(str(wav)) == "small"


def test_a_session_says_which_model_made_it(tmp_path, monkeypatch):
    from pq_workflow import PqWorkflowMixin
    monkeypatch.setattr(engines, "_active_model_override", "large-v3")
    wav = tmp_path / "read.wav"
    wav.write_bytes(b"\x00" * 64)
    # Saved before sessions were labelled: the words still say.
    old = {"media": [str(wav)], "transcript": [_word("we", 0.0, 0.1, "base")]}
    assert engines.session_model(old) == "base"
    assert PqWorkflowMixin._pq_session_payload(None, old)["model"] == "base"
    PqWorkflowMixin._pq_mirror_transcript(old)
    assert engines.transcript_model(str(wav)) == "base"
    # What the run said beats what the words say.
    old["model"] = "small"
    assert PqWorkflowMixin._pq_session_payload(None, old)["model"] == "small"
    assert "model" not in PqWorkflowMixin._pq_session_payload(None, {"media": []})


def test_the_check_reports_a_sessions_model(tmp_path):
    import transcript_check
    p = tmp_path / "G.pb_session.json"
    p.write_text(json.dumps({"workflow": "interview_session", "model": "base",
                             "transcript": [_word("we", 0.0, 0.1, "base")]}))
    assert transcript_check.pq_session_status(str(p), quick=True) == (
        True, 1, "1 words (model base)")


def test_pull_quotes_drafts_off_screen_and_labels_what_it_saves(tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    import main
    try:
        app = main.App()
    except tk.TclError as exc:
        pytest.skip("Tk unavailable: {}".format(exc))
    try:
        app.withdraw()
        app._preflight_memory_check = lambda: True
        monkeypatch.setattr(engines, "_active_model_override", "medium")
        monkeypatch.setattr(engines, "get_model", lambda size=None: None)
        monkeypatch.setattr(engines, "_is_model_cached", lambda s: True)
        monkeypatch.setattr(engines, "is_cuda_available", lambda: False)
        monkeypatch.setattr(engines, "get_media_duration", lambda p: 10.0)
        monkeypatch.setattr(engines, "extract_window", lambda *a, **k: (True, ""))
        seen_draft = threading.Event()
        sizes = []

        def _verbatim(path, progress_cb=None, cancel_event=None, model_size=None,
                      segment_cb=None):
            sizes.append(model_size)
            size = model_size or "medium"
            words = [_word("we", 1.0, 1.2, size),
                     _word("rode" if model_size else "walked", 1.3, 1.6, size)]
            segment_cb(words)
            if model_size is None:
                assert seen_draft.wait(5)
            return words
        monkeypatch.setattr(engines, "transcribe_clip_verbatim", _verbatim)

        wav = tmp_path / "guest.wav"
        wav.write_bytes(b"\x00" * 64)
        path = tmp_path / "GUEST.pb_session.json"
        session = {"token": "GUEST", "media": [str(wav)], "transcript": [],
                   "_file_path": str(path)}
        app._pq_current_session = None        # not on screen at any point
        app._pq_run_transcription(session)
        end = time.time() + 8
        while session["_progress"]["active"] and time.time() < end:
            app.update()
            time.sleep(0.01)
            if any(w["word"] == "rode" for w in session.get("transcript") or []):
                seen_draft.set()
        assert not session["_progress"]["active"]
        assert sizes == ["tiny", None]        # tiny first, then the chosen one
        assert seen_draft.is_set()            # and its words were there meanwhile
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["model"] == "medium"
        assert [w["word"] for w in saved["transcript"]] == ["we", "walked"]
        assert engines.transcript_model(str(wav)) == "medium"
        assert app._pq_status_for(session)[0] == "✓ transcribed · medium"
    finally:
        app.destroy()


# ── First pass: script order ─────────────────────────────────────────────
def test_the_draft_line_goes_in_script_order():
    from refine import DraftLine
    keys = [(3, "vo", 1), (9, "token", "G"), (20, "vo", 2)]
    line = DraftLine(keys)
    ran = []
    stop = threading.Event()

    def _work(key):
        line.wait(key, stop)
        ran.append(key[0])
        line.done(key)
    threads = [threading.Thread(target=_work, args=(k,), daemon=True)
               for k in reversed(keys)]
    for t in threads:
        t.start()
        time.sleep(0.05)
    for t in threads:
        t.join(3)
    assert ran == [3, 9, 20]


def test_saved_work_holds_nobody_up_and_cancel_lets_everyone_go():
    from refine import DraftLine
    stop = threading.Event()
    line = DraftLine([1, 2, 3])
    line.done(1)                     # the first file had a transcript
    line.wait(2, stop)               # so this needn't wait
    ticks = []
    t = threading.Thread(target=line.wait, args=(3, stop, lambda: ticks.append(1)),
                         daemon=True)
    t.start()
    t.join(0.6)
    assert t.is_alive() and ticks    # waiting ticks, for the stall watchdog
    stop.set()
    t.join(2)
    assert not t.is_alive()


def test_a_wider_line_still_starts_first_things_first():
    from refine import DraftLine
    stop = threading.Event()
    line = DraftLine([1, 2, 3], width=2)
    line.wait(2, stop)
    t = threading.Thread(target=line.wait, args=(3, stop), daemon=True)
    t.start()
    t.join(0.4)
    assert t.is_alive()
    line.done(1)
    t.join(2)
    assert not t.is_alive()


# ── First pass: lines land as it gets past them ──────────────────────────
def test_a_line_not_reached_yet_is_not_struck_as_never_said():
    from assembly_view import AssemblyWindow
    pending = AssemblyWindow._build_line(None, {"quote_text": "We rode.", "status": "not_run"},
                                         None, False)
    assert not pending.tokens
    missing = AssemblyWindow._build_line(None, {"quote_text": "We rode.", "status": "no_match"},
                                         None, False)
    assert missing.tokens


def _vo(order, text):
    return {"order": order, "id": "V{}".format(order), "part_index": 1, "text": text,
            "gap_after": True}


def _spoken(text, start, src="tiny"):
    out, t = [], start
    for w in text.split():
        out.append({"word": w.lower(), "text": " " + w, "start": t, "end": t + 0.3,
                    "_src": src})
        t += 0.4
    return out


_INTRO = "in the spring of that year the riders came down from the northern hills"
_NEXT = "nobody in the valley had seen so many horses since before the long war"
_LAST = "by the end of summer every one of those men was dead or gone for good"


def test_a_vo_line_lands_once_the_pass_is_past_it():
    from refine import lines_past
    blocks = [_vo(1, _INTRO), _vo(2, _NEXT), _vo(3, _LAST)]
    heard = _spoken(_INTRO, 0.0)
    end = heard[-1]["end"]
    # Only just heard: it could still be going on.
    assert lines_past(blocks, [(heard, 0.0, None, "a.wav", None)], "a.wav", end, set()) == []
    heard = heard + _spoken(_NEXT, end + 1.0)
    front = heard[-1]["end"]
    got = lines_past(blocks, [(heard, 0.0, None, "a.wav", None)], "a.wav", front, set())
    assert [r["order"] for r in got] == [1]
    assert got[0]["segments"][0][0] == pytest.approx(0.0, abs=0.3)
    # Nothing lands twice, and the next comes when its time comes.
    heard = heard + _spoken(_LAST, front + 1.0)
    front = heard[-1]["end"]
    got = lines_past(blocks, [(heard, 0.0, None, "a.wav", None)], "a.wav", front, {1})
    assert [r["order"] for r in got] == [2]


def test_only_the_next_few_vo_lines_are_looked_for(monkeypatch):
    from refine import lines_past, LAND_AHEAD
    asked = []

    def _rec(blocks, takes):
        asked.append([b["order"] for b in blocks])
        return []
    monkeypatch.setattr(engines, "reconcile_vo_part", _rec)
    blocks = [_vo(n, "line") for n in range(1, 21)]
    lines_past(blocks, [], "a.wav", 0.0, {1, 2})
    assert asked[0] == list(range(1, 3 + LAND_AHEAD))


def test_a_line_found_in_a_finished_take_needs_no_margin():
    from refine import lines_past
    done = _spoken(_INTRO, 0.0)
    got = lines_past([_vo(1, _INTRO)],
                     [(done, 0.0, None, "take1.wav", None),
                      (_spoken("something else", 0.0), 0.0, None, "take2.wav", None)],
                     "take2.wav", 1.0, set())
    assert [r["source_audio"] for r in got] == ["take1.wav"]


def test_an_interview_pull_lands_once_the_pass_is_past_it():
    from refine import pulls_past
    words = _spoken(_INTRO + " " + _NEXT, 10.0)
    end = words[-1]["end"]
    data = {"transcript": words, "audio_cache": "", "media": [], "id": "G"}
    p1 = _pull(1, 10.0, 15.0, _INTRO)
    p2 = _pull(2, end - 2.0, end, "before the long war")
    bad = _pull(3, 20.0, 20.0, _NEXT)         # no sound timecodes
    got = pulls_past([p1, p2, bad], data, end, set())
    assert [r["order"] for r in got] == [1]
    assert pulls_past([p1, p2, bad], data, end, {1}) == []
    assert [r["order"] for r in pulls_past([p1, p2, bad], data, end + 6.0, {1})] == [2]


def test_the_first_pass_streams_and_then_leaves_it_to_what_is_saved(media):
    app = _make_app()
    view = app._assembly = _View()
    landed = []
    heard = app._draft_listen(media, "tiny", lambda w, f: landed.append((len(w), f)))
    assert app._refine_words(media) == []
    heard([_word("we", 1.0, 1.2, "tiny")])
    assert [w["word"] for w in app._refine_words(media)] == ["we"]
    assert "First pass with tiny: 12%" in app._refine_status()
    assert "First pass with tiny  12%" in app._refine_short()
    assert not landed                    # not time to look yet
    app._draft_finish(media)
    _pump(app, lambda: media in view.changed)
    assert app._refine_words(media) is None
    assert media in view.changed
    assert app._refine_status() == ""


def test_the_first_pass_looks_for_lines_every_so_often(media, monkeypatch):
    import refine
    monkeypatch.setattr(refine, "LAND_EVERY_S", 0.0)
    app = _make_app()
    landed = []
    heard = app._draft_listen(media, "tiny", lambda w, f: landed.append((len(w), f)))
    heard([_word("we", 1.0, 1.2, "tiny")])
    heard([_word("rode", 2.0, 2.4, "tiny")])
    assert landed == [(1, 1.2), (2, 2.4)]


def test_a_landed_line_is_a_rows_first_match_then_the_whole_transcript_has_its_say(media):
    app = _make_app()
    app._bg_reconcile_active = True
    app._draft_to_review = lambda: None
    row = dict(_row(1, 0.0, 0.0, media), status="not_run", segments=[])
    app.results = [row]
    early = dict(_row(1, 1.0, 2.0, media), confidence=0.9)
    app._draft_land([early], media)
    _pump(app, lambda: row.get("_bg_matched"))
    assert row["segments"] == [(1.0, 2.0)] and row["_bg_matched"]
    # The file's finished: the same row, a better idea.
    late = dict(_row(1, 1.0, 2.5, media), confidence=0.95, _refined=True)
    app._draft_land([late], media, more_to_come=False)
    _pump(app, lambda: row["segments"] == [(1.0, 2.5)])
    assert row["segments"] == [(1.0, 2.5)]
    assert "_refined" not in row
    # And the end of the run's own copy doesn't undo anything.
    app._bg_apply_batch([dict(_row(1, 7.0, 8.0, media))])
    assert row["segments"] == [(1.0, 2.5)]


def test_a_landed_line_never_overrides_your_edit(media):
    app = _make_app()
    app._bg_reconcile_active = True
    app._draft_to_review = lambda: None
    row = dict(_row(1, 1.0, 2.0, media), status="not_run", _bg_matched=True,
               _s4_accepted=True)
    app.results = [row]
    app._draft_land([dict(_row(1, 5.0, 6.0, media), _refined=True)], media)
    _pump(app, lambda: not app.q.qsize())
    assert row["segments"] == [(1.0, 2.0)]


def test_step_3_goes_to_step_4_only_for_a_real_match_with_more_to_come(media):
    app = _make_app()
    app._bg_reconcile_active = True
    went = []
    app._draft_to_review = lambda: went.append(1)
    app.results = [dict(_row(1, 0, 0, media), status="not_run", segments=[])]
    app._draft_land([dict(_row(1, 0, 0, media), status="no_file", segments=[])])
    _pump(app, lambda: not app.q.qsize())
    assert not went
    app._draft_land([_row(1, 1.0, 2.0, media)], more_to_come=False)
    _pump(app, lambda: not app.q.qsize())
    assert not went
    app._draft_land([_row(1, 1.0, 2.0, media)])
    _pump(app, lambda: bool(went))
    assert went
    # A monolog has no rows till it's all in.
    went.clear()
    app._bg_reconcile_active = False
    app._draft_land([_row(1, 1.0, 2.0, media)])
    _pump(app, lambda: not app.q.qsize())
    assert not went


def test_the_second_pass_goes_in_script_order_windows_included(media, tmp_path, monkeypatch):
    other = tmp_path / "other.wav"
    other.write_bytes(b"\x00" * 64)
    engines.pb_transcript_save(str(other), _draft_words(), model="tiny")
    stream = _Stream(monkeypatch)
    app = _make_app()
    app.pulls = [_pull(1, 1.0, 2.0, "x")]
    app.results = [_row(1, 1.0, 2.0, str(other), _tx_model="tiny"),
                   _row(2, 1.0, 2.5, media),
                   _row(3, 1.0, 2.0, str(other))]
    paths, windows, first = app._refine_sources(app.results)
    assert first[str(other)] == 1 and first[media] == 2
    jobs = []
    app._refine_enqueue = lambda j, *a: jobs.extend(j)
    app._refine_scan(paths, windows, first, False, "script.txt")
    _pump(app, lambda: bool(jobs))
    assert [(j["kind"], j["path"]) for j in jobs] == [("file", str(other)),
                                                      ("pulls", str(other)),
                                                      ("file", media)]


def test_a_vo_line_moved_by_hand_gets_the_second_pass(media, tmp_path, monkeypatch):
    moved = tmp_path / "pickup.wav"
    moved.write_bytes(b"\x00" * 64)
    engines.pb_transcript_save(str(moved), _draft_words(), model="tiny")
    stream = _Stream(monkeypatch)
    app = _make_app()
    redone = []
    app._s4_reconcile_vo_row = lambda r, p, w, b: redone.append((r["order"], p, len(w)))
    saved = []
    app._s4_save = lambda: saved.append(1)
    app.results = [dict(_row(1, 0.0, 1.0, str(moved)), is_vo=True, part_index=1,
                        _s4_reassigned=True),
                   dict(_row(2, 0.0, 1.0, str(moved)), is_vo=True, part_index=1,
                        _s4_reassigned=True, _s4_accepted=True)]
    app._refine_resume()
    assert _pump(app, lambda: bool(stream.sizes))
    stream.feed.put([_word("we", 1.0, 1.2)])
    stream.feed.put(None)
    assert _pump(app, lambda: bool(redone))
    assert redone == [(1, str(moved), 1)]    # your accepted one stays as it is
    assert saved
    assert engines.transcript_model(str(moved)) == "medium"


# ── A stand-in app ───────────────────────────────────────────────────────
class _Var(object):
    def __init__(self, v):
        self.v = v

    def get(self):
        return self.v


class _Bin(object):
    def __init__(self, paths):
        self.paths = paths

    def get_paths(self):
        return list(self.paths)


class _View(object):
    """What the Assembly shows the app."""

    def __init__(self):
        self.changed, self.statuses, self.refreshed = [], 0, 0
        self.is_busy = False

    def busy(self):
        return self.is_busy

    def capture_place(self):
        return "place"

    def restore_place(self, place):
        assert place == "place"

    def refresh(self):
        self.refreshed += 1

    def transcript_changed(self, path):
        self.changed.append(path)

    def show_refine_status(self):
        self.statuses += 1


def _make_app():
    import main

    class _App(RefineMixin):
        _S4_OWNED_KEYS = main.App._S4_OWNED_KEYS
        _PRE_MATCH_STATUSES = main.App._PRE_MATCH_STATUSES
        _segs_same = staticmethod(main.App._segs_same)
    for n in ("_bg_apply_batch", "_bg_merge_row", "_bg_patch_undo", "_bg_patch_cards",
              "_bg_strip_refresh", "_bg_finish_live"):
        setattr(_App, n, getattr(main.App, n))
    app = _App()
    app.q = queue.Queue()
    app._ui = app.q.put
    app.later = []
    app.after = lambda ms, cb: app.later.append(cb)
    app._s4_is_live = lambda: False
    app._s4_undo_stack, app._s4_redo_stack = [], []
    app._s4_editing_row = None
    app._assembly = None
    app._reconcile_busy = False
    app._script_path = "script.txt"
    app._monolog = False
    app.vo_bins, app.vo_blocks, app.pulls, app.results = {}, [], [], []
    app.pad_var = _Var(1.0)
    app._rv = []
    app.patched = {}
    app._conform_baseline_patch = app.patched.update
    return app


def _pump(app, until, secs=3.0):
    end = time.time() + secs
    while time.time() < end:
        try:
            app.q.get(timeout=0.02)()
        except queue.Empty:
            pass
        if until():
            # Drain what the finish posted.
            while True:
                try:
                    app.q.get_nowait()()
                except queue.Empty:
                    return True
    return False


def _word(text, a, b, src="medium"):
    return {"word": text, "text": " " + text, "start": a, "end": b, "_src": src}


def _draft_words():
    return [_word("we", 1.0, 1.2, "tiny"), _word("waked", 1.3, 1.6, "tiny"),
            _word("the", 1.7, 1.8, "tiny"), _word("rich", 1.9, 2.2, "tiny")]


@pytest.fixture
def media(tmp_path, monkeypatch):
    wav = tmp_path / "guest.wav"
    wav.write_bytes(b"\x00" * 64)
    monkeypatch.setattr(engines, "_cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(engines, "_active_model_override", "medium")
    monkeypatch.setattr(engines, "get_media_duration", lambda p: 10.0)
    monkeypatch.setattr(engines, "_is_model_cached", lambda s: True)
    engines.cache_save(str(wav), _draft_words(), model="tiny")
    engines.pb_transcript_save(str(wav), _draft_words(), model="tiny")
    return str(wav)


class _Stream(object):
    """A stand-in transcribe_file a test feeds a segment at a time."""

    def __init__(self, monkeypatch):
        self.feed = queue.Queue()
        self.sizes = []
        monkeypatch.setattr(engines, "transcribe_file", self)

    def __call__(self, path, model_size=None, segment_cb=None, cancel_event=None,
                 blobs=None, progress_cb=None):
        self.sizes.append(model_size)
        words = []
        while True:
            seg = self.feed.get(timeout=5)
            if cancel_event is not None and cancel_event.is_set():
                raise engines.TranscriptionCancelled()
            if seg is None:
                return words, []
            words.extend(seg)
            segment_cb(seg)


def _pull(order, a, b, quote):
    return {"order": order, "token": "G", "in_seconds": a, "out_seconds": b,
            "quote_text": quote, "in_tc": "", "out_tc": "", "part_index": 0}


def _row(order, a, b, src, **kw):
    return dict(dict(order=order, token="G", status="ok", segments=[(a, b)],
                     source_audio=src, quote_text="q", rec_in_tc="", rec_out_tc=""),
                **kw)


# ── The pass ─────────────────────────────────────────────────────────────
def test_a_draft_is_found_and_refined_with_the_words_streaming_in(media, monkeypatch):
    stream = _Stream(monkeypatch)
    app = _make_app()
    view = app._assembly = _View()
    app.results = [_row(1, 1.0, 2.5, media)]
    app._refine_resume()
    assert _pump(app, lambda: bool(stream.sizes))
    assert stream.sizes == ["medium"]
    assert app._refine_running()
    assert "Refining with medium" in app._refine_status()
    assert [w["word"] for w in app._refine_words(media)] == ["we", "waked", "the", "rich"]

    # First segment: refined up to its end, the draft beyond it.
    stream.feed.put([_word("we", 1.0, 1.2), _word("walked", 1.3, 1.6)])
    assert _pump(app, lambda: len(app._refine_words(media)) and
                 app._refine_words(media)[1]["word"] == "walked")
    assert [(w["word"], w["_src"]) for w in app._refine_words(media)] == [
        ("we", "medium"), ("walked", "medium"), ("the", "tiny"), ("rich", "tiny")]
    assert media in view.changed
    assert "16%" in app._refine_status()

    # Done: saved, labelled, and no longer running.
    stream.feed.put([_word("the", 1.7, 1.8), _word("ridge", 1.9, 2.2)])
    stream.feed.put(None)
    assert _pump(app, lambda: not app._refine_running())
    assert [w["word"] for w in app._refine_words(media)] == ["we", "walked", "the", "ridge"]
    assert engines.transcript_model(media) == "medium"
    assert [w["word"] for w in engines.pb_transcript_load(media)[0]][-1] == "ridge"
    assert [w["word"] for w in engines.cache_load(media)][-1] == "ridge"
    assert "refined with medium" in app._refine_status()


def test_nothing_runs_when_tiny_is_the_model_chosen(media, monkeypatch):
    stream = _Stream(monkeypatch)
    monkeypatch.setattr(engines, "_active_model_override", "tiny")
    app = _make_app()
    app.results = [_row(1, 1.0, 2.5, media)]
    app._refine_resume()
    time.sleep(0.1)
    assert not stream.sizes and not app._refine_running()


def test_a_finished_transcript_is_left_alone(media, monkeypatch):
    stream = _Stream(monkeypatch)
    engines.pb_transcript_save(media, _draft_words(), model="medium")
    app = _make_app()
    app.results = [_row(1, 1.0, 2.5, media)]
    app._refine_resume()
    _pump(app, lambda: False, secs=0.2)
    assert not stream.sizes and not app._refine_running()


def test_refine_again_takes_a_better_model_over_a_finished_transcript(media, monkeypatch):
    stream = _Stream(monkeypatch)
    engines.pb_transcript_save(media, _draft_words(), model="small")
    app = _make_app()
    app.results = [_row(1, 1.0, 2.5, media)]
    app._refine_again()
    assert _pump(app, lambda: bool(stream.sizes))
    stream.feed.put(None)
    assert _pump(app, lambda: not app._refine_running())
    # Already there: says so instead of running.
    engines.pb_transcript_save(media, _draft_words(), model="medium")
    app._refine_again()
    assert _pump(app, lambda: "already transcribed" in app._refine_status())
    assert len(stream.sizes) == 1


def test_stopping_leaves_the_draft_and_it_starts_over_on_resume(media, monkeypatch):
    stream = _Stream(monkeypatch)
    app = _make_app()
    app.results = [_row(1, 1.0, 2.5, media)]
    app._refine_resume()
    assert _pump(app, lambda: bool(stream.sizes))
    stream.feed.put([_word("we", 1.0, 1.2)])
    assert _pump(app, lambda: app._refine_words(media)[0]["_src"] == "medium")
    app._refine_stop()
    stream.feed.put([_word("walked", 1.3, 1.6)])         # wakes it to see the event
    assert not app._refine_running()
    assert app._refine_words(media) is None
    assert engines.transcript_model(media) == "tiny"     # the draft still stands
    app._refine_resume()
    assert _pump(app, lambda: len(stream.sizes) == 2)
    stream.feed.put(None)
    assert _pump(app, lambda: not app._refine_running())


def test_only_the_session_on_screen_is_touched(media, monkeypatch):
    stream = _Stream(monkeypatch)
    app = _make_app()
    app.results = [_row(1, 1.0, 2.5, media)]
    app._refine_resume()
    assert _pump(app, lambda: bool(stream.sizes))
    app._script_path = "other.txt"
    app.results = [_row(1, 1.0, 2.5, "other.wav")]
    app._refine_resume()                 # a new session: the old pass is stopped
    stream.feed.put(None)
    assert _pump(app, lambda: not app._refine_running())


# ── Matching again ───────────────────────────────────────────────────────
def _finish(app, stream, words):
    stream.feed.put(words)
    stream.feed.put(None)
    return _pump(app, lambda: not app._refine_running() and app.q.empty())


def test_script_pulls_take_the_new_match_and_your_work_stays(media, monkeypatch):
    stream = _Stream(monkeypatch)
    app = _make_app()
    view = app._assembly = _View()
    app.pulls = [_pull(1, 1.0, 2.5, "we walked the ridge"),
                 _pull(2, 5.0, 6.0, "then quiet"),
                 _pull(3, 7.0, 8.0, "and on")]
    app.results = [
        _row(1, 1.0, 2.2, media, markers=[{"kind": "sfx", "src": 1.5}], gap_after_s=2.0),
        _row(2, 5.0, 5.5, media, _s4_accepted=True, status="manual"),
        _row(3, 7.0, 7.5, media, status="low_confidence"),
    ]
    app._refine_resume()
    assert _pump(app, lambda: bool(stream.sizes))
    assert _finish(app, stream, [
        _word("we", 1.1, 1.2), _word("walked", 1.3, 1.6), _word("the", 1.7, 1.8),
        _word("ridge", 1.9, 2.4), _word("then", 5.2, 5.3), _word("quiet", 5.4, 5.8),
        _word("and", 7.2, 7.3), _word("on", 7.4, 7.7)])
    by = {r["order"]: r for r in app.results}
    # Untouched: takes the new match, keeps its marks and its gap.
    assert by[1]["segments"][-1][1] == pytest.approx(2.4)
    assert by[1]["markers"] == [{"kind": "sfx", "src": 1.5}]
    assert by[1]["gap_after_s"] == 2.0
    # Accepted: your cut stands, with a note and the better words.
    assert by[2]["segments"] == [(5.0, 5.5)]
    assert by[2]["_s4_accepted"] and "kept your edit" in by[2]["_bg_note"]
    assert [w["word"] for w in by[2]["words"]] == ["then", "quiet"]
    # A row the draft matched poorly comes good.
    assert by[3]["status"] == "ok"
    # They are the algorithm's output now, not the user's edits.
    assert set(app.patched) == {1, 2, 3}
    assert view.refreshed


def test_undo_does_not_bring_the_draft_match_back(media, monkeypatch):
    stream = _Stream(monkeypatch)
    app = _make_app()
    app.pulls = [_pull(1, 1.0, 2.5, "we walked the ridge")]
    app.results = [_row(1, 1.0, 2.2, media)]
    app._s4_undo_stack = [{"1": {"segments": [(1.0, 2.2)], "status": "ok"}}]
    app._refine_resume()
    assert _pump(app, lambda: bool(stream.sizes))
    assert _finish(app, stream, [_word("we", 1.1, 1.2), _word("walked", 1.3, 1.6),
                                 _word("the", 1.7, 1.8), _word("ridge", 1.9, 2.4)])
    assert app._s4_undo_stack[0]["1"]["segments"][-1][1] == pytest.approx(2.4)


def test_a_usable_row_is_never_replaced_by_no_match(media, monkeypatch):
    app = _make_app()
    cur = _row(1, 1.0, 2.2, media)
    assert app._refine_merge_row(cur, dict(order=1, token="G", status="no_match",
                                           segments=[])) == "kept"
    assert cur["segments"] == [(1.0, 2.2)] and cur["status"] == "ok"
    assert "no match" in cur["_bg_note"]


def test_a_reassigned_row_is_left_alone(media):
    app = _make_app()
    cur = _row(1, 1.0, 2.2, media, _s4_reassigned=True)
    assert app._refine_merge_row(cur, dict(_row(1, 3.0, 4.0, media))) == "kept"
    assert cur["segments"] == [(1.0, 2.2)]


def test_a_new_match_waits_while_the_assembly_is_in_use(media, monkeypatch):
    app = _make_app()
    view = app._assembly = _View()
    view.is_busy = True
    app.results = [_row(1, 1.0, 2.2, media)]
    fresh = _row(1, 1.0, 2.4, media)
    app._refine_apply([fresh], media)
    assert app.results[0]["segments"] == [(1.0, 2.2)]
    assert len(app.later) == 1
    view.is_busy = False
    app.later.pop()()
    assert app.results[0]["segments"] == [(1.0, 2.4)]
    assert view.refreshed == 1


def test_pulls_matched_from_their_own_windows_run_again(tmp_path, monkeypatch):
    monkeypatch.setattr(engines, "_active_model_override", "medium")
    wav = str(tmp_path / "guest.wav")
    open(wav, "wb").close()
    runs = []

    def _rip(pull, path, pad=1.0, min_start_s=0.0, model_size=None):
        runs.append((pull["order"], pad, model_size))
        return dict(_row(pull["order"], pull["in_seconds"], pull["out_seconds"] + 1, path),
                    _tx_model="medium", words=[_word("better", 1.0, 1.2)])
    monkeypatch.setattr(engines, "reconcile_interview_pull", _rip)
    app = _make_app()
    app.pulls = [_pull(1, 1.0, 2.0, "a"), _pull(2, 3.0, 4.0, "b")]
    app.results = [_row(1, 1.0, 2.0, wav, _tx_model="tiny"),
                   _row(2, 3.0, 4.0, wav, _tx_model="medium", _s4_accepted=True)]
    app._refine_resume()
    assert _pump(app, lambda: bool(runs) and not app._refine_running())
    assert runs == [(1, 1.0, None)]      # the one still on a draft
    assert app.results[0]["segments"] == [(1.0, 3.0)]
    assert app.results[0]["_tx_model"] == "medium"
    assert app.results[0]["words"][0]["word"] == "better"


def test_a_session_reopened_at_step_4_finds_its_vo_parts_in_the_pool(media, monkeypatch):
    monkeypatch.setattr(engines, "pair_takes", lambda paths: [(None, p) for p in paths])
    stream = _Stream(monkeypatch)
    matched = []

    def _rvp(blocks, takes):
        matched.append([t[3] for t in takes])
        return []
    monkeypatch.setattr(engines, "reconcile_vo_part", _rvp)
    app = _make_app()
    app._pool = SimpleNamespace(
        get_vo_assets=lambda: {0: {"audios": [media], "videos": ["cam.mp4"]}})
    app.vo_blocks = [{"order": 1, "part_index": 0, "id": "v", "text": "t"}]
    app.results = [dict(_row(1, 1.0, 1.5, media), is_vo=True, token="VO_PART_00",
                        part_index=0)]
    app._refine_resume()
    assert _pump(app, lambda: bool(stream.sizes))
    assert _finish(app, stream, [_word("we", 1.0, 1.2)])
    assert matched == [[media]]


def test_a_vo_part_waits_for_all_its_takes(tmp_path, monkeypatch):
    monkeypatch.setattr(engines, "_cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(engines, "_active_model_override", "medium")
    monkeypatch.setattr(engines, "get_media_duration", lambda p: 10.0)
    monkeypatch.setattr(engines, "_is_model_cached", lambda s: True)
    monkeypatch.setattr(engines, "pair_takes", lambda paths: [(None, p) for p in paths])
    paths = []
    for name in ("take1.wav", "take2.wav"):
        p = tmp_path / name
        p.write_bytes(b"\x00" * 64)
        engines.cache_save(str(p), _draft_words(), model="tiny")
        engines.pb_transcript_save(str(p), _draft_words(), model="tiny")
        paths.append(str(p))
    stream = _Stream(monkeypatch)
    matched = []

    def _rvp(blocks, takes):
        matched.append([t[3] for t in takes])
        return [dict(_row(1, 1.0, 2.0, takes[0][3]), is_vo=True, token="VO_PART_00",
                     part_index=0, takes_data=[])]
    monkeypatch.setattr(engines, "reconcile_vo_part", _rvp)
    app = _make_app()
    app.vo_bins = {0: _Bin(paths)}
    app.vo_blocks = [{"order": 1, "part_index": 0, "id": "v", "text": "t"}]
    app.results = [dict(_row(1, 1.0, 1.5, paths[0]), is_vo=True, token="VO_PART_00",
                        part_index=0)]
    app._refine_resume()
    assert _pump(app, lambda: len(stream.sizes) == 1)
    stream.feed.put([_word("we", 1.0, 1.2)])
    stream.feed.put(None)
    assert _pump(app, lambda: len(stream.sizes) == 2)
    assert not matched                   # the second take is still a draft
    stream.feed.put([_word("we", 1.0, 1.2)])
    stream.feed.put(None)
    assert _pump(app, lambda: not app._refine_running() and bool(matched))
    assert matched == [paths]
    assert app._vo_takes_by_part[0][0][3] == paths[0]


# ── A monolog ────────────────────────────────────────────────────────────
def _monolog_app(wav, monkeypatch):
    monkeypatch.setattr(engines, "pair_takes", lambda paths: [(None, p) for p in paths])
    app = _make_app()
    app._monolog = True
    app.vo_bins = {0: _Bin([wav])}
    app.vo_blocks = [{"order": 1, "part_index": 0, "id": "p1", "text": "t"}]
    built = []
    app._step4 = lambda: built.append(list(app.results))
    app.built = built
    app._write_conform_baseline = lambda: None
    app._open_assembly = lambda: None
    return app


def _cut_row(order, span, a, b, wav, **kw):
    return dict(order=order, token="VO_PART_00", status="ok", is_vo=True, part_index=0,
                monolog=True, script_span=span, segments=[(a, b)], source_audio=wav,
                gap_after_s=0.0, quote_text="q", **kw)


def _new_cut(wav):
    return [_cut_row(1, (0, 9), 0.5, 2.0, wav), _cut_row(2, (10, 19), 3.0, 4.0, wav),
            _cut_row(3, (20, 29), 5.0, 6.0, wav)]


def test_an_untouched_monolog_takes_the_new_cut_at_once(media, monkeypatch):
    stream = _Stream(monkeypatch)
    monkeypatch.setattr(monolog, "reconcile", lambda blocks, takes, keep=(): _new_cut(media))
    app = _monolog_app(media, monkeypatch)
    app.results = [_cut_row(1, (0, 29), 0.5, 6.0, media)]
    app._refine_resume()
    assert _pump(app, lambda: bool(stream.sizes))
    assert _finish(app, stream, [_word("we", 1.0, 1.2)])
    assert _pump(app, lambda: len(app.results) == 3)
    assert [r["order"] for r in app.results] == [1, 2, 3]
    assert not app._refine_held()


def test_a_monolog_you_have_edited_waits_to_be_asked(media, monkeypatch):
    stream = _Stream(monkeypatch)
    # The new cut leaves out the lines the edited pull answers for.
    monkeypatch.setattr(monolog, "reconcile", lambda blocks, takes, keep=():
                        [r for r in _new_cut(media) if tuple(r["script_span"]) not in keep])
    app = _monolog_app(media, monkeypatch)
    view = app._assembly = _View()
    view.pps, view.view0, view.playhead = 30.0, 0.0, 0.0
    view.restore_view = lambda *a: None
    edited = _cut_row(1, (0, 9), 0.5, 1.5, media, _s4_accepted=True)
    app.results = [edited, _cut_row(2, (10, 29), 3.0, 6.0, media)]
    app._s4_undo_stack = [{"1": {}}]
    app._refine_resume()
    assert _pump(app, lambda: bool(stream.sizes))
    assert _finish(app, stream, [_word("we", 1.0, 1.2)])
    assert _pump(app, app._refine_held)
    assert len(app.results) == 2                 # nothing moved under you
    assert "update" in app._refine_status()

    app._s4_is_live = lambda: True
    app._refine_update_the_cut()
    assert not app._refine_held()
    assert app.built                             # Step 4's cards built anew
    assert app.results[0] is edited              # your pull stands
    assert [r["order"] for r in app.results] == [1, 2, 3]
    assert [r["script_span"] for r in app.results] == [(0, 9), (10, 19), (20, 29)]


# The read below is one take of these three paragraphs, straight through.
_P = ("The first herds rumbled in during the spring and the Texans were amazed.",
      "By the next year the town had become a boomtown with bars on every corner.",
      "Nobody who lived through that season ever forgot how quickly it all ended.")


def _read_of(*paras):
    t, words = 0.0, []
    for w in " ".join(paras).lower().replace(",", "").replace(".", "").split():
        words.append({"word": w, "start": round(t, 3), "end": round(t + 0.3, 3)})
        t += 0.4
    return [(words, 0.0, None, "take.wav", None)]


def _script_blocks(*paras):
    import parsers
    return parsers.parse_plain_script("Title\n" + "\n".join(paras))[3]


def _played(rows, words):
    out = []
    for r in rows:
        for a, b in r.get("segments") or []:
            out += [w["word"] for w in words if a <= (w["start"] + w["end"]) / 2 <= b]
    return out


def test_update_keeps_a_pull_from_before_spans_existed():
    """Episode 10: 67 pulls worked on, 27 with marks, cut before script_span.
    They were all thrown away, and the dialog had said they would stay."""
    blocks, takes = _script_blocks(*_P), _read_of(*_P)
    old = [dict(order=n + 1, quote_text=p, status="ok", is_vo=True, source_audio="take.wav",
                segments=[(n * 10.0, n * 10.0 + 3.0)], _s4_accepted=True,
                markers=[{"kind": "sfx", "src": n * 10.0, "name": "gun"}])
           for n, p in enumerate(_P)]
    before = [dict(r) for r in old]
    rows = monolog.recut_around(blocks, takes, old, lambda r: r.get("_s4_accepted"))
    assert len(rows) == 3
    for r, b in zip(rows, before):
        assert r["segments"] == b["segments"] and r["markers"] == b["markers"]
    assert [r["order"] for r in rows] == [1, 2, 3]


def test_update_cuts_new_pulls_only_for_the_lines_left_so_nothing_plays_twice():
    """A straight read is one new pull, so an edited pull in the middle of it
    would have had the whole read added around it, repeating its lines."""
    blocks, takes = _script_blocks(*_P), _read_of(*_P)
    words = takes[0][0]
    n0 = len(_P[0].split())
    n1 = n0 + len(_P[1].split())
    middle = dict(order=2, quote_text=_P[1], status="ok", is_vo=True,
                  source_audio="take.wav", _s4_accepted=True,
                  segments=[(words[n0]["start"], words[n1 - 1]["end"])])
    old = [dict(order=1, quote_text=_P[0], status="ok", is_vo=True, source_audio="take.wav",
                segments=[(0.0, words[n0 - 1]["end"])]),
           middle,
           dict(order=3, quote_text=_P[2], status="ok", is_vo=True, source_audio="take.wav",
                segments=[(words[n1]["start"], words[-1]["end"])])]
    rows = monolog.recut_around(blocks, takes, old, lambda r: r.get("_s4_accepted"))
    assert rows[1] is middle and [r["order"] for r in rows] == [1, 2, 3]
    assert not any(r["status"] == "no_match" for r in rows)
    # Each word once, in order, none lost.
    assert _played(rows, words) == [w["word"] for w in words]


def test_update_changes_nothing_if_a_worked_on_pull_cant_be_placed():
    """The script has changed since: losing the pull would be the worst answer."""
    blocks, takes = _script_blocks(*_P), _read_of(*_P)
    old = [dict(order=1, quote_text="words that are no longer in the script at all",
                status="ok", _s4_accepted=True, segments=[(0.0, 2.0)])]
    assert monolog.recut_around(blocks, takes, old, lambda r: r.get("_s4_accepted")) is None
    assert old[0]["segments"] == [(0.0, 2.0)]


def test_a_failed_update_leaves_the_cut_alone_and_says_why(media, monkeypatch):
    import refine
    said = []
    monkeypatch.setattr(refine.messagebox, "showwarning", lambda t, m, **k: said.append(m))
    monkeypatch.setattr(monolog, "recut_around", lambda *a: None)
    app = _monolog_app(media, monkeypatch)
    rows = [_cut_row(1, (0, 9), 0.5, 1.5, media, _s4_accepted=True)]
    app.results = rows
    app._rf().held = [("words", 0.0, None, media, None)]
    app._s4_is_live = lambda: True
    app._refine_update_the_cut()
    assert app.results is rows and not app.built and said
    assert app._refine_held()                    # still on offer


def test_a_swapped_reading_counts_as_worked_on(media, monkeypatch):
    app = _monolog_app(media, monkeypatch)
    assert not app._refine_row_touched(_cut_row(1, (0, 9), 0.5, 1.5, media))
    assert app._refine_row_touched(_cut_row(1, (0, 9), 0.5, 1.5, media, _reading_chosen=True))


def test_the_question_counts_what_stays_and_what_changes(media, monkeypatch):
    app = _monolog_app(media, monkeypatch)
    app.results = [_cut_row(1, (0, 9), 0.5, 1.5, media, _s4_accepted=True),
                   _cut_row(2, (10, 19), 3.0, 4.0, media)]
    assert app._refine_cut_counts() == (1, 1)


def test_monolog_results_say_which_script_they_cover():
    import parsers
    blocks = parsers.parse_plain_script(
        "Title\nThe first line of the script is here.\nAnd now the second line follows it.")[3]
    t, words = 0.0, []
    for w in "the first line of the script is here and now the second line follows it".split():
        words.append({"word": w, "start": t, "end": t + 0.3})
        t += 0.4
    rows = monolog.reconcile(blocks, [(words, 0.0, None, None)])
    assert rows and all(r.get("script_span") for r in rows)
    assert rows[0]["script_span"][0] == 0


def test_the_baseline_takes_the_new_match(tmp_path):
    import main
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"1": {"segments": [[1.0, 2.0]]},
                                "2": {"segments": [[3.0, 4.0]]}}))

    class _A(object):
        _conform_baseline_path = lambda self: str(path)
        _conform_baseline_entry = staticmethod(main.App._conform_baseline_entry)
    main.App._conform_baseline_patch(_A(), {1: dict(segments=[[1.0, 2.5]], status="ok")})
    snap = json.loads(path.read_text())
    assert snap["1"]["segments"] == [[1.0, 2.5]] and snap["1"]["accepted"] is False
    assert snap["2"]["segments"] == [[3.0, 4.0]]


# ── Saying so ────────────────────────────────────────────────────────────
def test_step_4_says_the_cut_stands_on_a_draft(media, monkeypatch):
    tk = pytest.importorskip("tkinter")
    stream = _Stream(monkeypatch)
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip("Tk unavailable: {}".format(exc))
    try:
        root.withdraw()
        app = _make_app()
        app.body = tk.Frame(root)
        tk.Label(app.body, text="STEP 4").pack()
        tk.Label(app.body, text="cards").pack()
        app._s4_is_live = lambda: True
        app.results = [_row(1, 1.0, 2.5, media)]
        app._refine_resume()
        assert _pump(app, lambda: bool(stream.sizes))
        strip = app._rf().strip
        assert strip is not None
        assert "tiny draft" in strip[1].cget("text")
        assert "Refining with medium" in strip[1].cget("text")
        # It sits under the title, above the cards.
        assert app.body.pack_slaves()[1] is strip[0]
        assert _finish(app, stream, [_word("we", 1.0, 1.2)])
        assert "refined with medium" in strip[1].cget("text")
    finally:
        root.destroy()


def test_refine_again_does_not_call_a_finished_transcript_a_draft(media, monkeypatch):
    tk = pytest.importorskip("tkinter")
    stream = _Stream(monkeypatch)
    engines.pb_transcript_save(media, _draft_words(), model="small")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip("Tk unavailable: {}".format(exc))
    try:
        root.withdraw()
        app = _make_app()
        app.body = tk.Frame(root)
        tk.Label(app.body, text="STEP 4").pack()
        app._s4_is_live = lambda: True
        app.results = [_row(1, 1.0, 2.5, media)]
        app._refine_again()
        assert _pump(app, lambda: bool(stream.sizes))
        assert "draft" not in app._rf().strip[1].cget("text")
        stream.feed.put([_word("we", 1.0, 1.2)])
        stream.feed.put(None)
        assert _pump(app, lambda: not app._refine_running())
        assert "refined with medium" in app._rf().strip[1].cget("text")
    finally:
        root.destroy()
