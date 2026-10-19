"""Episode screens, headless: choose a folder, scan, review, save, reopen,
and launch Script -> Session and AAF -> XML already linked.

Builds the same small real-media project as test_episode.py.  Needs
tkinter, a display and ffmpeg; skips otherwise.
"""
import json
import os
import shutil
import sys
import time
import traceback

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
    pytest.skip("ffmpeg/ffprobe not on PATH", allow_module_level=True)

from test_episode import project  # noqa: E402,F401  (module-scoped fixture)
import episode as EP  # noqa: E402


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
    for _ in range(n):
        app.update()
        time.sleep(0.025)


def _wait(app, cond, secs=60):
    t0 = time.time()
    while time.time() - t0 < secs:
        _pump(app, 4)
        if cond():
            return True
    return False


@pytest.fixture
def app(monkeypatch):
    import episode_workflow, aaf_workflow
    a = _app_or_skip()
    errors = []
    a.report_callback_exception = (
        lambda *x: errors.append("".join(traceback.format_exception(*x))))
    a._errors = errors
    import main as main_mod, pq_workflow
    for mod in (episode_workflow, aaf_workflow, main_mod, pq_workflow):
        for name in ("showinfo", "showwarning", "showerror"):
            monkeypatch.setattr(mod.messagebox, name,
                                lambda title, msg, *x, **k: errors.append("{}: {}".format(title, msg)))
        # A native file dialog would hang a headless run; make any
        # unexpected one a recorded failure instead.
        for name in ("askopenfilename", "asksaveasfilename", "askdirectory"):
            monkeypatch.setattr(mod.filedialog, name,
                                lambda **k: (errors.append("unexpected dialog: " + repr(k)) or ""))
    def _ask(title, msg, **k):
        errors.append("unexpected question: " + title); return False
    for mod in (main_mod, aaf_workflow, pq_workflow):
        monkeypatch.setattr(mod.messagebox, "askyesno", _ask)
        monkeypatch.setattr(mod.messagebox, "askyesnocancel", _ask)
    try:
        yield a
    finally:
        a.destroy()


def _open_fresh(app, monkeypatch, project, reset=True):
    """Choose the project folder.  The project fixture is shared across the
    module, so an earlier test's episode file is removed first unless the
    test wants to find it."""
    import episode_workflow
    if reset:
        for n in os.listdir(project):
            if n.endswith(EP.EPISODE_EXT):
                os.remove(os.path.join(project, n))
        # Sessions other tests made (the fixture's own BUTLER one stays).
        for dp, _dn, fns in os.walk(project):
            for fn in fns:
                if fn.endswith(".pb_session.json") and fn != "BUTLER.pb_session.json":
                    os.remove(os.path.join(dp, fn))
    monkeypatch.setattr(episode_workflow.filedialog, "askdirectory", lambda **k: project)
    app._select_workflow("episode")
    assert _wait(app, lambda: bool(getattr(app, "_ep", None) and app._ep.get("media"))), \
        "scan never finished"
    _pump(app, 10)
    assert not app._errors, "\n".join(app._errors)


def test_choose_folder_scan_save_reopen(app, monkeypatch, project):
    _open_fresh(app, monkeypatch, project)
    ep = app._ep
    assert set(ep["sources"]) == {"Ep 7 pt 1", "Ep 7 pt 2", "Butler Interview"}
    assert ep["sources"]["Butler Interview"]["token"] == "BUTLER"
    assert app.workflow == "episode" and app._is_dirty()

    # Edit through the model the way the screen does, then save with the
    # header SAVE (quick save), which must land at the project root.
    ep["sources"]["Ep 7 pt 1"]["token"] = "NARR1"
    app._quick_save()
    path = os.path.join(project, "Ep 7" + EP.EPISODE_EXT)
    assert os.path.isfile(path) and not app._is_dirty()

    # Put a camera aside and rescan: it stays aside, the token survives.
    app._ep_exclude("02_MEDIA/RAW/Ep 7 CAM 2 01.mp4")
    app._ep_scan()
    assert _wait(app, lambda: "02_MEDIA/RAW/Ep 7 CAM 2 01.mp4" in app._ep["media"]
                 and app._ep["media"]["02_MEDIA/RAW/Ep 7 CAM 2 01.mp4"]["status"] == "excluded")
    assert app._ep["sources"]["Ep 7 pt 1"]["token"] == "NARR1"
    assert app._ep["sources"]["Ep 7 pt 1"]["video"] == ["02_MEDIA/RAW/Ep 7 CAM 1 01.mp4"]
    app._ep_save()

    # Reopen through the generic OPEN router.
    app._ep = None
    app._home(); _pump(app, 3)
    app._open_session(path); _pump(app, 10)
    assert app.workflow == "episode" and app._ep["title"] == "Ep 7"
    assert app._ep["sources"]["Ep 7 pt 1"]["token"] == "NARR1"
    # And the folder dialog finds the existing file next time.
    app._ep = None
    _open_fresh(app, monkeypatch, project, reset=False)
    assert app._ep["sources"]["Ep 7 pt 1"]["token"] == "NARR1"
    assert not app._errors, "\n".join(app._errors)


def test_pull_quotes_session_registers_a_source(app, monkeypatch, project):
    _open_fresh(app, monkeypatch, project)
    app._ep_launch_pq(); _pump(app, 5)
    assert app.workflow == "pull_quotes"
    assert app._pq_project["episode_file"] == os.path.join(project, "Ep 7" + EP.EPISODE_EXT)
    # Jordan makes a session for the narrator recording.
    sess_path = os.path.join(project, "03_AUDIO", "01_VO", "NARR1.pb_session.json")
    session = {"token": "NARR1", "_file_path": sess_path,
               "media": [os.path.join(project, "03_AUDIO", "01_VO", "Ep 7 pt 1.wav")],
               "transcript": []}
    assert app._pq_save_session_file(session)
    saved = EP.load(os.path.join(project, "Ep 7" + EP.EPISODE_EXT))
    src = saved["sources"]["Ep 7 pt 1"]
    assert src["token"] == "NARR1" and src["session"] == "03_AUDIO/01_VO/NARR1.pb_session.json"
    assert sess_path in saved["sessions"]
    # The open episode saw it too.
    assert app._ep["sources"]["Ep 7 pt 1"]["token"] == "NARR1"
    # A session for a recording no source owns makes a new source.
    other = {"token": "CORY", "_file_path": os.path.join(project, "03_AUDIO", "CORY.pb_session.json"),
             "media": [os.path.join(project, "03_AUDIO", "sting.wav")], "transcript": []}
    assert app._pq_save_session_file(other)
    saved = EP.load(os.path.join(project, "Ep 7" + EP.EPISODE_EXT))
    assert saved["sources"]["CORY"]["audio"] == ["03_AUDIO/sting.wav"]
    # And the project save merges into the episode file instead of replacing it.
    app._pq_project["sessions"] = [dict(session), dict(other)]
    assert app._pq_save_episode_project(prompt_path=False)
    saved = EP.load(os.path.join(project, "Ep 7" + EP.EPISODE_EXT))
    assert saved["workflow"] == "episode" and len(saved["sources"]) == 4
    assert not app._errors, "\n".join(app._errors)

    # Sessions straight from the episode's sources: Ep 7 pt 1 is covered
    # already; pt 2 and the Butler interview (token known) are offered.
    app._pq_render_project_view(); _pump(app, 3)
    offered = {n: tok for n, tok, _m in app._pq_episode_sources_without_session()}
    assert set(offered) == {"Ep 7 pt 2", "Butler Interview"}
    assert offered["Butler Interview"] == "BUTLER"
    made = app._pq_create_session_from_source("Ep 7 pt 2", "narr 2", open_view=False)
    assert made and made["token"] == "NARR_2"
    assert [os.path.basename(m) for m in made["media"]] == ["Ep 7 pt 2.wav"]
    assert os.path.isfile(made["_file_path"])
    saved = EP.load(os.path.join(project, "Ep 7" + EP.EPISODE_EXT))
    assert saved["sources"]["Ep 7 pt 2"]["token"] == "NARR_2"
    assert "Ep 7 pt 2" not in {n for n, _t, _m in app._pq_episode_sources_without_session()}
    assert not app._errors, "\n".join(app._errors)


def test_launch_script_session_prefills_the_pool(app, monkeypatch, project):
    _open_fresh(app, monkeypatch, project)
    ep = app._ep
    script = os.path.join(project, "05_SCRIPT", "Ep 7.txt")
    os.makedirs(os.path.dirname(script), exist_ok=True)
    with open(script, "w", encoding="utf-8") as f:
        f.write("[PART Open]\n[VO PART_0_NARRATOR]\nA quiet morning.\n"
                "[BUTLER 00:00:05-00:00:20]\nWe went out at four.\n")
    ep["links"]["script"] = EP.rel(ep, script)
    app._ep_save()
    app._ep_launch_s2s()
    ok = _wait(app, lambda: getattr(app, "_pool", None) is not None
               and len(app._pool._rows) >= 9, secs=30)
    _pump(app, 10)
    _rows = getattr(getattr(app, "_pool", None), "_rows", None)
    assert ok, "workflow={} rows={} errors={}".format(
        app.workflow, None if _rows is None else [os.path.basename(r["path"]) for r in _rows],
        app._errors)
    rows = {os.path.basename(r["path"]): r["var"].get() for r in app._pool._rows}
    assert rows["Butler Interview_JORDAN.wav"] == "BUTLER"
    assert rows["Butler Interview_CARY.wav"] == "BUTLER"
    assert rows["Butler Interview.MP4"] == "BUTLER"
    # One narrated part, narrator sources carry no token: assigned to it.
    assert rows["Ep 7 pt 1.wav"] == "VO: Open"
    assert rows["Ep 7 pt 2.wav"] == "VO: Open"
    assert "Ep 7 CAM 1 01.mp4" in rows and "bed.wav" not in rows
    assert not app._errors, "\n".join(app._errors)


def test_launch_aaf_prefills_setup(app, monkeypatch, project):
    import parsers, aaf_workflow
    _open_fresh(app, monkeypatch, project)
    ep = app._ep
    aaf = os.path.join(project, "01_PROJECT FILES", "Ep 7.aaf")
    open(aaf, "wb").close()

    def clip(name, sf, t0):
        return {"clip_name": name, "start_secs": t0, "end_secs": t0 + 5,
                "src_in_secs": 10.0, "src_out_secs": 15.0, "state": "Unmuted",
                "source_file": sf, "fade_in_secs": 0.0, "fade_out_secs": 0.0}
    pt1 = os.path.join(project, "03_AUDIO", "01_VO", "Ep 7 pt 1.wav")
    bj = os.path.join(project, "03_AUDIO", "00_RAW AUDIO", "Butler Interview_JORDAN.wav")
    def fake_parse(path):
        return {"session_name": "Ep 7", "fps": 23.976, "sample_rate": 48000, "markers": [],
                "start_tc_secs": 0.0, "start_tc_frames": 0, "tc_fps": 23.976,
                "tracks": [{"name": "VO", "clips": [clip("Ep 7 pt 1.wav-01", pt1, 0)]},
                           {"name": "INT", "clips": [clip("Butler Interview_JORDAN.wav-03", bj, 10)]}]}
    monkeypatch.setattr(parsers, "parse_aaf_session", fake_parse)
    monkeypatch.setattr(aaf_workflow, "parse_aaf_session", fake_parse)
    ep["links"]["aaf"] = EP.rel(ep, aaf)
    app._ep_save()
    app._ep_launch_aaf()
    assert _wait(app, lambda: app.workflow == "aaf_xml"
                 and "Ep 7 pt 1" in getattr(app, "_aaf_source_file_vars", {})
                 and app._aaf_source_file_vars["Ep 7 pt 1"].get() == "Ep 7 CAM 1 01.mp4", secs=30)
    _pump(app, 10)
    assert app._aaf_source_slot_counts["Ep 7 pt 1"] == 2
    assert [v.get() for v in app._aaf_source_extra_vars["Ep 7 pt 1"]] == ["Ep 7 CAM 2 01.mp4"]
    assert app._aaf_source_syncaudio_vars["Ep 7 pt 1"].get() == pt1
    assert app._aaf_source_file_vars["Butler Interview_JORDAN"].get() == "Butler Interview.MP4"
    assert app._aaf_source_syncaudio_vars["Butler Interview_JORDAN"].get() == bj
    assert app._aaf_mix_var.get().endswith("Ep 7_FOR VID.wav")
    assert app._aaf_group_var.get() == "custom"
    assert not app._errors, "\n".join(app._errors)

    # Sync a source, save the setup: the episode remembers the setup file
    # AND the sync itself.
    app._aaf_source_offset_vars["Ep 7 pt 1"].set("1.234")
    app._aaf_source_sync_vars["Ep 7 pt 1"].set(True)
    app._aaf_sync_state_vars["Ep 7 pt 1"].set("manual")
    app._aaf_sync_locked_vars["Ep 7 pt 1"].set(True)
    app._aaf_set_drift("Ep 7 pt 1", {"ppm": 8.4, "t_ref": 50.0})
    app._aaf_saved_path = os.path.join(project, "01_PROJECT FILES", "Ep 7_setup.json")
    assert app._aaf_save_setup(prompt=False)
    saved = EP.load(os.path.join(project, "Ep 7" + EP.EPISODE_EXT))
    assert saved["links"]["aaf_setup"] == "01_PROJECT FILES/Ep 7_setup.json"
    st = saved["sources"]["Ep 7 pt 1"]["aaf_sync"]
    assert st["offset"] == "1.234" and st["state"] == "manual" and st["locked"]
    assert st["video"] == "02_MEDIA/RAW/Ep 7 CAM 1 01.mp4" and st["audio"] == "03_AUDIO/01_VO/Ep 7 pt 1.wav"
    assert st["drift"]["ppm"] == 8.4

    # A fresh AAF of the same session, with no setup file: opens synced.
    app._ep["links"].pop("aaf_setup", None)
    app._ep_save()
    app._home(); _pump(app, 3)
    app._ep_launch_aaf()
    assert _wait(app, lambda: app.workflow == "aaf_xml"
                 and app._aaf_source_offset_vars.get("Ep 7 pt 1") is not None
                 and app._aaf_source_offset_vars["Ep 7 pt 1"].get() == "1.234", secs=30)
    assert app._aaf_sync_state_vars["Ep 7 pt 1"].get() == "manual"
    assert app._aaf_sync_locked_vars["Ep 7 pt 1"].get()
    assert app._aaf_get_drift("Ep 7 pt 1")["ppm"] == 8.4
    assert not app._errors, "\n".join(app._errors)


def _click(app, win, text):
    """Press the app-style button labelled *text* inside *win*."""
    import tkinter as tk
    stack = [win]
    while stack:
        w = stack.pop()
        try:
            if isinstance(w, tk.Label) and w.cget("text") == text:
                # A withdrawn window may not deliver generated mouse events;
                # the app's buttons expose their action directly.
                w._pb_cmd()
                return True
        except tk.TclError:
            pass
        stack.extend(w.winfo_children())
    return False


def test_google_doc_link_fetch_notes_and_launch(app, monkeypatch, project):
    import gdocs
    _open_fresh(app, monkeypatch, project)
    ep = app._ep
    doc_text = ("[PART Open]\n[VO PART_0_NARRATOR]\nA quiet morning.\n"
                "[BUTLER 00:00:05-00:00:20]\nWe went out at four.\n"
                "[JENA 00:00:05-00:00:20]\nNo such session.\n")
    calls = {}
    monkeypatch.setattr(gdocs, "is_configured", lambda: True)
    monkeypatch.setattr(gdocs, "get_credentials", lambda interactive=True: object())
    monkeypatch.setattr(gdocs, "pick_document",
                        lambda creds, timeout=300: {"id": "doc123", "name": "Ep 7 script"})
    monkeypatch.setattr(gdocs, "fetch_text", lambda creds, doc_id: (
        doc_text, {"name": "Ep 7 script", "version": "3", "modifiedTime": "t"}))
    monkeypatch.setattr(gdocs, "write_notes", lambda creds, doc_id, notes, docs=None: (
        calls.update(doc_id=doc_id, notes=notes) or (len(notes), [])))

    # Link through the (faked) picker.
    app._ep_gdoc_link()
    assert _wait(app, lambda: (app._ep["links"].get("gdoc") or {}).get("id") == "doc123", secs=10)

    # Fetch: the Doc becomes the episode's script, version-stamped, checked.
    app._ep_gdoc_fetch()
    assert _wait(app, lambda: getattr(app, "_ep_findings_win", None) is not None, secs=10)
    _pump(app, 3)
    script = os.path.join(project, "Ep 7 script.txt")
    assert os.path.isfile(script)
    assert open(script, encoding="utf-8").read() == doc_text
    assert ep["links"]["script"] == "Ep 7 script.txt"
    assert ep["links"]["gdoc"]["version"] == "3"

    # Write the findings into the Doc: the unknown-session finding is
    # anchored to its header line.
    assert _click(app, app._ep_findings_win, "WRITE NOTES INTO DOC")
    assert _wait(app, lambda: "notes" in calls, secs=10)
    assert calls["doc_id"] == "doc123"
    anchors = [a for a, _m in calls["notes"]]
    assert "[JENA 00:00:05-00:00:20]" in anchors, calls["notes"]
    # The confirmation is an info box, which the fixture records like any
    # dialog; it is the expected outcome here.
    assert any("notes written" in e for e in app._errors), app._errors
    app._errors[:] = [e for e in app._errors if "notes written" not in e]
    _click(app, app._ep_findings_win, "CLOSE")
    app._ep_findings_win = None

    # Launching Script -> Session fetches first, then continues into Step 2
    # with the sources assigned.
    app._ep_launch_s2s()
    assert _wait(app, lambda: getattr(app, "_ep_findings_win", None) is not None, secs=10)
    _pump(app, 3)
    assert _click(app, app._ep_findings_win, "CONTINUE  →")
    assert _wait(app, lambda: getattr(app, "_pool", None) is not None
                 and len(app._pool._rows) >= 9, secs=30)
    _pump(app, 10)
    rows = {os.path.basename(r["path"]): r["var"].get() for r in app._pool._rows}
    assert rows["Butler Interview.MP4"] == "BUTLER"
    assert rows["Ep 7 pt 1.wav"] == "VO: Open"
    assert not app._errors, "\n".join(app._errors)


def test_send_to_script_needs_a_selection_and_a_doc(app, monkeypatch, project):
    _open_fresh(app, monkeypatch, project)
    app._ep_launch_pq(); _pump(app, 5)
    # No session view open: nothing to send; the guard says so.
    app._pq_send_to_script()
    assert any("Select the words" in e for e in app._errors)
    app._errors.clear()
