"""REIMPORT AAF and Step 2 revisits must keep per-source sync state.

Step 2 empties its pool lists on every build (the row widgets go with the
old page) and refilled them only at the end.  Its first row rebuild ran
against that empty pool, reset every assignment to "no video", and the
assignment-change trace then wiped the source's sync (offset, state,
lock).  Auto-match re-assigned the videos by name afterwards, so Back from
Step 1 -- and REIMPORT AAF, which rebuilds the page -- looked like they
lost "only" the sync.  REIMPORT from a Step-1-first visit also raised a
TclError on the destroyed Step 1 status label.

Needs tkinter and a display (CI runs it under xvfb); skips otherwise.
"""
import os
import sys
import time
import traceback

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

NO_VIDEO = "— no video —"


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


def _pump(app, n=40):
    # _pump_ui drains the UI queue from a 20 ms after() timer; update()
    # only fires timers that are due, so give them real time.
    for _ in range(n):
        app.update()
        time.sleep(0.025)


def _fake_parser(aud_a, aud_b):
    def clip(name, sf, t0):
        return {"clip_name": name, "start_secs": t0, "end_secs": t0 + 5,
                "src_in_secs": 10.0, "src_out_secs": 15.0, "state": "Unmuted",
                "source_file": sf, "fade_in_secs": 0.0, "fade_out_secs": 0.0}

    def parse(path):
        return {"session_name": os.path.splitext(os.path.basename(path))[0],
                "fps": 29.97, "sample_rate": 48000, "markers": [],
                "start_tc_secs": 0.0, "start_tc_frames": 0, "tc_fps": 29.97,
                "tracks": [{"name": "A1", "clips": [clip("CamA.wav-01", aud_a, 0),
                                                    clip("CamA.wav-02", aud_a, 10)]},
                           {"name": "A2", "clips": [clip("CamB.wav-01", aud_b, 0)]}]}
    return parse


class _Fixture:
    def __init__(self, tmp_path, monkeypatch):
        import aaf_workflow
        self.tmp = tmp_path
        self.vid_a = self._touch("CamA.mp4")
        self.vid_b = self._touch("CamB.mp4")
        aud_a = self._touch("CamA.wav")
        aud_b = self._touch("CamB.wav")
        self.aaf1 = self._touch("show_v1.aaf")
        self.aaf2 = self._touch("show_v2.aaf")
        monkeypatch.setattr(aaf_workflow, "parse_aaf_session",
                            _fake_parser(aud_a, aud_b))
        self.dialogs = []
        for name in ("showinfo", "showwarning", "showerror"):
            monkeypatch.setattr(
                aaf_workflow.messagebox, name,
                lambda title, msg, *a, _n=name, **k: self.dialogs.append((_n, title)))
        self.errors = []
        self.app = _app_or_skip()
        self.app.report_callback_exception = (
            lambda *a: self.errors.append("".join(traceback.format_exception(*a))))

    def _touch(self, name):
        p = self.tmp / name
        p.write_bytes(b"")
        return str(p)

    def enter_step2(self, via):
        app = self.app
        if via == "step1":
            app._aaf_step1(); _pump(app, 5)
            app._aaf_load(self.aaf1); _pump(app, 5)
            app._aaf_step2(); _pump(app, 10)
        else:   # Open Session: _aaf_load with no Step 1 screen ever built
            app._aaf_load(self.aaf1); _pump(app, 15)
        app._aaf_add_video_batch([self.vid_a, self.vid_b]); _pump(app, 10)

    def set_confirmed_sync(self, base, offset="1.234"):
        import tkinter as tk
        app = self.app
        app._aaf_source_sync_vars[base].set(True)
        app._aaf_source_offset_vars[base].set(offset)
        app._aaf_source_sync_label_vars.setdefault(base, tk.StringVar()).set("auto 0.98")
        app._aaf_sync_state_vars[base].set("confirmed")
        app._aaf_needs_sync_vars[base].set(True)
        app._aaf_sync_locked_vars[base].set(True)

    def sync_snapshot(self):
        import tkinter as tk
        app = self.app
        out = {}
        for b in app._aaf_sources:
            out[b] = dict(
                video=app._aaf_source_file_vars[b].get(),
                enabled=app._aaf_source_sync_vars[b].get(),
                audio=os.path.basename(app._aaf_source_syncaudio_vars[b].get()),
                offset=app._aaf_source_offset_vars[b].get(),
                label=app._aaf_source_sync_label_vars.get(b, tk.StringVar()).get(),
                state=app._aaf_sync_state_vars.get(b, tk.StringVar()).get(),
                needs=app._aaf_needs_sync_vars.get(b, tk.BooleanVar()).get(),
                locked=app._aaf_sync_locked_vars.get(b, tk.BooleanVar()).get())
        return out


@pytest.fixture
def fx(tmp_path, monkeypatch):
    f = _Fixture(tmp_path, monkeypatch)
    try:
        yield f
    finally:
        f.app.destroy()


@pytest.mark.parametrize("via", ["open", "step1"])
def test_reimport_keeps_sync_and_save_path(fx, via):
    app = fx.app
    fx.enter_step2(via)
    assert app._aaf_source_file_vars["CamA"].get() == "CamA.mp4"   # auto-matched
    fx.set_confirmed_sync("CamA")
    setup_path = str(fx.tmp / "show_setup.json")
    app._aaf_saved_path = setup_path
    before = fx.sync_snapshot()

    app._aaf_reimport(fx.aaf2)
    _pump(app, 60)

    assert not fx.errors, "\n".join(fx.errors)
    assert fx.sync_snapshot() == before
    assert app._aaf_path == os.path.abspath(fx.aaf2)
    assert app._aaf_saved_path == setup_path, "quick-save must still target the setup file"
    assert ("showinfo", "Reimport complete") in fx.dialogs
    assert not [d for d in fx.dialogs if d[0] == "showerror"], fx.dialogs
    # The setup on disk still names the old AAF, so the baseline is dirty.
    assert app._is_dirty()


def test_back_navigation_keeps_sync_and_manual_assignment(fx):
    app = fx.app
    fx.enter_step2("step1")
    # A manual assignment auto-match would never produce: CamA -> CamB.mp4.
    app._aaf_source_file_vars["CamA"].set("CamB.mp4")
    _pump(app, 5)
    fx.set_confirmed_sync("CamA")
    before = fx.sync_snapshot()
    assert before["CamA"]["video"] == "CamB.mp4"

    app._aaf_step1(); _pump(app, 5)      # Back
    app._aaf_step2(); _pump(app, 20)     # Next

    assert not fx.errors, "\n".join(fx.errors)
    assert fx.sync_snapshot() == before
    assert set(app._aaf_video_paths) == {fx.vid_a, fx.vid_b}


def test_back_from_export_report_keeps_setup(fx):
    """The export-complete screen's BACK TO SETUP returns to Step 2 with
    pool, assignments and syncs intact."""
    app = fx.app
    fx.enter_step2("open")
    fx.set_confirmed_sync("CamA")
    before = fx.sync_snapshot()
    app.out_path.set(str(fx.tmp / "out.xml"))
    app._aaf_done(2, 0, 3, [("CamA.wav-01", "CamA.mp4", "#00ff00")]); _pump(app, 5)
    assert not fx.errors, "
".join(fx.errors)
    app._aaf_step2(); _pump(app, 20)
    assert not fx.errors, "
".join(fx.errors)
    assert fx.sync_snapshot() == before
    assert set(app._aaf_video_paths) == {fx.vid_a, fx.vid_b}


def test_open_session_restore_still_applies(fx):
    """The deferred restore REIMPORT now shares with Open Session."""
    app = fx.app
    fx.enter_step2("open")
    fx.set_confirmed_sync("CamA", offset="2.500")
    saved = app._aaf_build_setup_data()
    before = fx.sync_snapshot()

    # Mirror _open_session's AAF branch on a fresh app.
    app.destroy()
    fx.app = app = _app_or_skip()
    app.report_callback_exception = (
        lambda *a: fx.errors.append("".join(traceback.format_exception(*a))))
    setup_path = str(fx.tmp / "show_setup.json")
    app._pending_aaf_setup = saved
    app._pending_mark_saved = True
    app._pending_aaf_saved_path = setup_path
    app._aaf_load(fx.aaf1)
    _pump(app, 60)

    assert not fx.errors, "\n".join(fx.errors)
    assert fx.sync_snapshot() == before
    assert app._aaf_saved_path == setup_path
    assert not app._is_dirty()
