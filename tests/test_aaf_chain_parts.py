"""Step 2: split audio parts against one video.

A long recording cut into Part 1/2/3 audio files still has one video.
Each row shows its audio's length, OFFSET can be typed, and CHAIN PARTS
syncs the parts in order, each searched for where the one before it ends.
"""
import os
import sys
import time
import traceback

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aaf_workflow import fmt_duration, natural_key, parse_offset_text  # noqa: E402


@pytest.mark.parametrize("text,want", [
    ("3280.5", 3280.5), ("54:40.5", 3280.5), ("0:54:40.5", 3280.5),
    ("-12", -12.0), ("12s", 12.0), (" 1:00 ", 60.0),
    ("abc", None), ("", None), ("1:75", None), ("1:2:3:4", None),
])
def test_parse_offset_text(text, want):
    assert parse_offset_text(text) == want


def test_fmt_duration():
    assert fmt_duration(1661.54) == "27:41.5"
    assert fmt_duration(4929.0) == "1:22:09.0"
    assert fmt_duration(-3.25) == "-0:03.2"
    assert fmt_duration(None) == "?"


def test_natural_key_orders_part_numbers():
    names = ["Int_Part_10.mp3", "Int_Part_2.mp3", "Int_Part_1.mp3"]
    assert sorted(names, key=natural_key) == [
        "Int_Part_1.mp3", "Int_Part_2.mp3", "Int_Part_10.mp3"]


# ── Tk ────────────────────────────────────────────────────────────────────

def _pump(app, n=10):
    for _ in range(n):
        app.update()
        time.sleep(0.02)


def _wait(app, cond, secs=20):
    t0 = time.time()
    while time.time() - t0 < secs:
        if cond():
            return True
        _pump(app, 2)
    return cond()


LENS = {"Denise_Part_1.mp3": 1600.0, "Denise_Part_2.mp3": 1700.25,
        "Denise_Part_3.mp3": 1661.5}


@pytest.fixture
def fx(tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        pytest.skip("no display")
    import aaf_workflow
    import engines

    def touch(name):
        p = tmp_path / name
        p.write_bytes(b"")
        return str(p)
    vid = touch("Interrogation.mp4")
    auds = [touch(n) for n in LENS]
    aaf = touch("show.aaf")

    def clip(name, sf):
        return {"clip_name": name + "-01", "start_secs": 0, "end_secs": 5,
                "src_in_secs": 10.0, "src_out_secs": 15.0, "state": "Unmuted",
                "source_file": sf, "fade_in_secs": 0.0, "fade_out_secs": 0.0}

    def parse(path):
        return {"session_name": "show", "fps": 29.97, "sample_rate": 48000,
                "markers": [], "start_tc_secs": 0.0, "start_tc_frames": 0,
                "tc_fps": 29.97,
                "tracks": [{"name": "A%d" % i, "clips": [clip(os.path.basename(a), a)]}
                           for i, a in enumerate(auds)]}
    monkeypatch.setattr(aaf_workflow, "parse_aaf_session", parse)
    monkeypatch.setattr(aaf_workflow, "offline_media", lambda paths: [])
    monkeypatch.setattr(engines, "get_media_duration",
                        lambda p: LENS.get(os.path.basename(p)))
    # The real part starts 0.4 s after where the hint says (a sloppy cut),
    # so each part's result must feed the next part's hint.
    hints = []

    def fake_sync(vp, ap, **kw):
        h = kw.get("offset_hint")
        hints.append((os.path.basename(ap), h))
        kw["drift_out"].clear()
        return ((h or 0.0) + 0.4, 0.97, [])
    monkeypatch.setattr(aaf_workflow, "detect_sync_offset", fake_sync)
    dialogs = []
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(
            aaf_workflow.messagebox, name,
            lambda title, msg, *a, _n=name, **k: dialogs.append((_n, title, msg)))

    import main
    try:
        app = main.App()
    except tk.TclError as e:
        pytest.skip("Tk unavailable: {}".format(e))
    app.withdraw()
    errors = []
    app.report_callback_exception = (
        lambda *a: errors.append("".join(traceback.format_exception(*a))))
    app._aaf_ffmpeg_ok = True
    app._aaf_load(aaf); _pump(app, 15)
    app._aaf_add_video_batch([vid]); _pump(app, 10)
    app._aaf_add_audio_batch(auds); _pump(app, 5)
    vlabel = app._aaf_vid_label(vid)
    for b, a in zip(app._aaf_sources, sorted(auds)):
        app._aaf_needs_sync_vars[b].set(True)
        app._aaf_source_file_vars[b].set(vlabel)
        app._aaf_source_syncaudio_vars[b].set(a)
    app._aaf_page_mode.set("sync"); app._rebuild_aaf_source_rows(); _pump(app, 5)
    app.hints, app.dialogs, app.errors = hints, dialogs, errors
    try:
        yield app
    finally:
        app.destroy()


def _row_for(app, audio_name):
    return next(b for b in app._aaf_sources
                if os.path.basename(app._aaf_source_syncaudio_vars[b].get()) == audio_name)


def test_rows_show_audio_lengths(fx):
    app = fx
    assert _wait(app, lambda: all(
        lbl.cget("text") not in ("", "…")
        for lbl in app._aaf_aud_dur_labels.values()))
    b = _row_for(app, "Denise_Part_3.mp3")
    assert app._aaf_aud_dur_labels[b].cget("text") == "27:41.5"
    assert not app.errors, "\n".join(app.errors)


def test_typed_offset_seeds_sync(fx):
    app = fx
    b = _row_for(app, "Denise_Part_2.mp3")
    ev = app._aaf_offset_entry_vars[b]
    ev.set("26:40")                       # typed, never Entered
    app._aaf_do_sync(b)                   # SYNC takes it anyway
    assert _wait(app, lambda: app._aaf_sync_state_vars[b].get() == "auto")
    assert app.hints == [("Denise_Part_2.mp3", 1600.0)]
    assert app._aaf_source_offset_vars[b].get() == "1600.400"
    assert not app.errors, "\n".join(app.errors)


def test_chain_parts_syncs_each_from_the_last(fx):
    app = fx
    app._aaf_chain_parts()
    p3 = _row_for(app, "Denise_Part_3.mp3")
    assert _wait(app, lambda: app._aaf_sync_state_vars[p3].get() == "auto")
    # Part 1 from zero (no hint); Part 2 from Part 1's result + its length;
    # Part 3 from Part 2's result + its length.
    assert app.hints == [("Denise_Part_1.mp3", 0.0),
                         ("Denise_Part_2.mp3", 0.4 + 1600.0),
                         ("Denise_Part_3.mp3", 1600.8 + 1700.25)]
    assert app._aaf_source_offset_vars[p3].get() == "3301.450"
    assert not app.dialogs
    assert not app.errors, "\n".join(app.errors)


def test_chain_keeps_a_locked_part(fx):
    app = fx
    p1 = _row_for(app, "Denise_Part_1.mp3")
    app._aaf_source_offset_vars[p1].set("2.000")
    app._aaf_set_sync_state(p1, "manual")
    app._aaf_apply_lock(p1, True); _pump(app, 3)
    app._aaf_chain_parts()
    p3 = _row_for(app, "Denise_Part_3.mp3")
    assert _wait(app, lambda: app._aaf_sync_state_vars[p3].get() == "auto")
    assert [h[0] for h in app.hints] == ["Denise_Part_2.mp3", "Denise_Part_3.mp3"]
    assert app.hints[0][1] == 1602.0
    assert app._aaf_source_offset_vars[p1].get() == "2.000"
    assert not app.errors, "\n".join(app.errors)
