"""AAF -> XML Step 2 redraws only what changed.

A Step 2 page is hundreds of widgets -- each one a native window on
Windows -- and it used to be torn down and rebuilt whole on every SOURCE
ASSIGN / SOURCE SYNC switch, lock, accept and undo: most of a second each on
a real episode.  Each page is now kept while the other one shows, and a row
is redrawn only when the state it shows changed.  These tests pin that down,
along with what came with it: the AAF is read once per launch, a sync that
finishes after its row was redrawn still lands, error messages are no longer
lost, and the ALIGN waveform draws long recordings from a peak pyramid.

The Tk tests need tkinter and a display (CI runs them under xvfb); they
skip otherwise.
"""
import os
import sys
import threading
import time
import traceback

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ── The AAF is read once per launch ──────────────────────────────────────

def test_parse_is_reused_while_the_aaf_is_unchanged(tmp_path, monkeypatch):
    import parsers
    calls = []

    def fake(path):
        calls.append(path)
        return {"tracks": [{"name": "A1", "clips": [{"clip_name": "x"}]}]}
    monkeypatch.setattr(parsers, "_parse_aaf_session_uncached", fake)
    monkeypatch.setattr(parsers, "_aaf_parse_cache", {})
    aaf = tmp_path / "show.aaf"
    aaf.write_bytes(b"v1")

    first = parsers.parse_aaf_session(str(aaf))
    first["tracks"][0]["clips"].clear()        # a caller's copy is its own
    again = parsers.parse_aaf_session(str(aaf))
    assert len(calls) == 1
    assert again["tracks"][0]["clips"] == [{"clip_name": "x"}]

    st = aaf.stat()                            # re-exported from Pro Tools
    os.utime(aaf, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
    parsers.parse_aaf_session(str(aaf))
    assert len(calls) == 2


# ── ALIGN draws long recordings from peaks ───────────────────────────────

def test_peak_bins_match_the_samples():
    np = pytest.importorskip("numpy")
    import sync_preview as SP
    D = SP.SyncPreviewDialog
    t = np.arange(3_000_000, dtype=np.float32)
    x = (np.sin(t * 0.05) * (0.2 + 0.8 * np.abs(np.sin(t * 2e-5)))).astype(np.float32)
    peaks = D._build_peaks(x)
    w = 900
    fine = (1 << SP._PEAK_SHIFT) * SP._PEAK_SUBDIV
    for view_start, spp in [(0, len(x) // w), (-50_000, 2_000),
                            (len(x) - 300 * 1_500, 1_500), (12_345, fine - 1)]:
        view_end = view_start + w * spp
        raw = D._compute_bins(x, w, view_start, view_end)
        fast = D._compute_bins(x, w, view_start, view_end, peaks)
        for a, b in zip(raw, fast):
            if spp < fine:                     # zoomed in: straight from samples
                assert np.array_equal(a, b)
            else:                              # off by at most a block per pixel
                assert np.abs(a - b).mean() < 0.01


# ── Step 2 rows ──────────────────────────────────────────────────────────

def _app_or_skip():
    tk = pytest.importorskip("tkinter")
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        pytest.skip("no display")
    try:
        import main
        app = main.App()
    except tk.TclError as e:
        pytest.skip("Tk unavailable: {}".format(e))
    app.withdraw()
    return app


def _pump(app, n=20):
    # _pump_ui drains the UI queue from a 20 ms after() timer.
    for _ in range(n):
        app.update()
        time.sleep(0.025)


def _wait(app, cond, secs=20):
    t0 = time.time()
    while time.time() - t0 < secs:
        if cond():
            return True
        _pump(app, 2)
    return cond()


@pytest.fixture
def fx(tmp_path, monkeypatch):
    import aaf_workflow

    def touch(name):
        p = tmp_path / name
        p.write_bytes(b"")
        return str(p)
    vid_a, vid_b = touch("CamA.mp4"), touch("CamB.mp4")
    aud_a, aud_b = touch("CamA.wav"), touch("CamB.wav")
    aaf = touch("show.aaf")

    def clip(name, sf, t0):
        return {"clip_name": name, "start_secs": t0, "end_secs": t0 + 5,
                "src_in_secs": 10.0, "src_out_secs": 15.0, "state": "Unmuted",
                "source_file": sf, "fade_in_secs": 0.0, "fade_out_secs": 0.0}

    def parse(path):
        return {"session_name": "show", "fps": 29.97, "sample_rate": 48000,
                "markers": [], "start_tc_secs": 0.0, "start_tc_frames": 0,
                "tc_fps": 29.97,
                "tracks": [{"name": "A1", "clips": [clip("CamA.wav-01", aud_a, 0)]},
                           {"name": "A2", "clips": [clip("CamB.wav-01", aud_b, 0)]}]}
    monkeypatch.setattr(aaf_workflow, "parse_aaf_session", parse)
    # Empty test files read as cloud placeholders; they are not.
    monkeypatch.setattr(aaf_workflow, "offline_media", lambda paths: [])
    dialogs = []
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(
            aaf_workflow.messagebox, name,
            lambda title, msg, *a, _n=name, **k: dialogs.append((_n, title, msg)))

    app = _app_or_skip()
    errors = []
    app.report_callback_exception = (
        lambda *a: errors.append("".join(traceback.format_exception(*a))))
    app._aaf_ffmpeg_ok = True
    app._aaf_load(aaf); _pump(app, 15)
    app._aaf_add_video_batch([vid_a, vid_b]); _pump(app, 10)
    for b in ("CamA", "CamB"):
        app._aaf_needs_sync_vars[b].set(True)
    app.dialogs, app.errors = dialogs, errors
    try:
        yield app
    finally:
        app.destroy()


def _show(app, mode):
    app._aaf_page_mode.set(mode)
    app._rebuild_aaf_source_rows()
    _pump(app, 5)


def test_switching_back_reuses_the_page(fx):
    app = fx
    _show(app, "sync")
    sync_rows = dict(app._aaf_pages["sync"]["rows"])
    assert set(sync_rows) == {"CamA", "CamB"}
    _show(app, "assign")
    assign_rows = dict(app._aaf_pages["assign"]["rows"])
    _show(app, "sync")
    assert app._aaf_pages["sync"]["rows"] == sync_rows        # same widgets
    assert app._aaf_pages["sync"]["rows_frame"].winfo_manager() == "pack"
    assert app._aaf_pages["assign"]["rows_frame"].winfo_manager() == ""
    _show(app, "assign")
    assert app._aaf_pages["assign"]["rows"] == assign_rows
    assert not app.errors, "\n".join(app.errors)


def test_lock_redraws_only_its_row(fx):
    app = fx
    _show(app, "sync")
    before = dict(app._aaf_pages["sync"]["rows"])
    app._aaf_toggle_lock("CamA"); _pump(app, 5)
    after = app._aaf_pages["sync"]["rows"]
    assert after["CamB"] is before["CamB"]
    assert after["CamA"] is not before["CamA"]
    assert not before["CamA"].winfo_exists()
    assert app._aaf_lock_btns["CamA"].cget("text") == "\U0001f512"
    assert str(app._aaf_sync_btns["CamA"].cget("state")) == "disabled"
    assert not app.errors, "\n".join(app.errors)


def test_change_made_elsewhere_shows_on_return(fx):
    app = fx
    _show(app, "sync")
    before = dict(app._aaf_pages["sync"]["rows"])
    _show(app, "assign")
    app._aaf_source_offset_vars["CamB"].set("2.000")
    app._aaf_source_sync_label_vars["CamB"].set("✓ 2.000s (99%)")
    app._aaf_sync_state_vars["CamB"].set("auto")
    _show(app, "sync")
    rows = app._aaf_pages["sync"]["rows"]
    assert rows["CamA"] is before["CamA"]
    assert rows["CamB"] is not before["CamB"]
    assert app._aaf_sync_btns["CamB"].cget("text") == "✓ 2.000s (99%)"
    # Unticking a source changes which rows the page holds: drawn afresh.
    _show(app, "assign")
    app._aaf_needs_sync_vars["CamB"].set(False)
    _show(app, "sync")
    assert set(app._aaf_pages["sync"]["rows"]) == {"CamA"}
    assert not app.errors, "\n".join(app.errors)


def _blocking_sync(monkeypatch, release, result=None, error=None):
    import aaf_workflow

    def fake(vp, ap, **kw):
        release.wait(10)
        if error:
            raise RuntimeError(error)
        kw["drift_out"].clear()
        return result
    monkeypatch.setattr(aaf_workflow, "detect_sync_offset", fake)


def test_sync_lands_on_a_row_redrawn_while_it_ran(fx, monkeypatch):
    """The row used to hand its SYNC button to the sync; a redraw while it
    ran destroyed that button, applying the result raised on it, and the
    row was left with an offset but no state -- so ACCEPT did nothing."""
    app = fx
    release = threading.Event()
    _blocking_sync(monkeypatch, release, result=(1.5, 0.97, [(2.0, 0.5)]))
    _show(app, "sync")
    app._aaf_do_sync("CamA"); _pump(app, 3)
    assert app._aaf_sync_btns["CamA"].cget("text").startswith("SYNCING")
    app._rebuild_aaf_source_rows(force=True); _pump(app, 3)
    assert app._aaf_sync_btns["CamA"].cget("text").startswith("SYNCING")
    release.set()
    assert _wait(app, lambda: app._aaf_sync_state_vars["CamA"].get() == "auto")
    _pump(app, 3)
    btn = app._aaf_sync_btns["CamA"]
    assert app._aaf_source_offset_vars["CamA"].get() == "1.500"
    assert btn.cget("text").startswith("✓ 1.500s")
    assert str(btn.cget("state")) == "normal"
    assert app._aaf_row_is_current("sync", "CamA")
    assert "CamA" not in app._aaf_syncing
    assert not app.errors, "\n".join(app.errors)


def test_failed_sync_says_why(fx, monkeypatch):
    app = fx
    release = threading.Event()
    release.set()
    _blocking_sync(monkeypatch, release, error="no audio stream in CamA.mp4")
    _show(app, "sync")
    app._aaf_do_sync("CamA")
    assert _wait(app, lambda: ("showerror", "Sync Failed",
                               "no audio stream in CamA.mp4") in app.dialogs)
    assert app._aaf_sync_btns["CamA"].cget("text") == "⚠ error"


def test_align_extraction_failure_says_why(fx, monkeypatch):
    from concurrent.futures import Future
    import sync_preview
    monkeypatch.setattr(sync_preview.SyncPreviewDialog, "_start_extraction",
                        lambda self: None)
    dlg = sync_preview.SyncPreviewDialog(fx, "CamA.mp4", "CamA.wav")
    try:
        fut = Future()
        fut.set_exception(RuntimeError("cannot read CamA.mp4"))
        dlg._extraction_done(fut)
        _pump(fx, 3)
        assert "cannot read CamA.mp4" in dlg._loading_lbl.cget("text")
    finally:
        dlg.close()
