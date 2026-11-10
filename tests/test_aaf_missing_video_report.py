"""AAF -> XML: the editor's report of clips without video.

Next to the XML the build writes <name>_missing_video.txt: every clip that
got no picture, at its record timecode, and whether another camera covers
that moment -- so a hole in the cut can be looked up, not hunted for.
"""
import os
import sys
import time
import traceback

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aaf_workflow import _covered_fraction, missing_video_report  # noqa: E402

# A session at 01:00:00:00, 29.97 non-drop: the AAF's start is 108000
# frames, which is 3603.6 real seconds.  Record timecode counts frames the
# way the XML places clips (and Premiere labels them).
T0 = 108000 / 29.97
INFO = {"seq_name": "Ep 5", "xml_path": r"C:\x\Ep5_aaf.xml", "fps": 29.97,
        "drop_frame": False, "start_tc_secs": T0, "total_clips": 10}


def _m(name, base, s, e, track="A1", hidden=False):
    return {"clip_name": name, "base": base, "track": track,
            "start_secs": s, "end_secs": e, "hidden": hidden}


def test_covered_fraction():
    assert _covered_fraction(10, 20, []) == 0.0
    assert _covered_fraction(10, 20, [(0, 30)]) == 1.0
    assert _covered_fraction(10, 20, [(5, 15), (12, 18)]) == pytest.approx(0.8)


def test_report_lists_clips_by_record_timecode():
    txt = missing_video_report(
        [_m("Denise_Part_3.wav-07", "Denise_Part_3", 125.0, 131.5),
         _m("Denise_Part_3.wav-02", "Denise_Part_3", 62.0, 64.0),
         _m("Music_Cue_4.wav-01", "Music_Cue_4", 10.0, 40.0, hidden=True)],
        [(120.0, 140.0)],                         # another camera at 2:00-2:20
        ["Cam B: sync offset +3.000s places the clip outside CamB.mp4"],
        INFO)
    lines = txt.splitlines()
    rows = [l for l in lines if l.strip().startswith("01:0")]
    assert len(rows) == 2
    # Sorted by record time, timecode counts from the sequence start.
    assert rows[0].split()[:4] == ["01:01:01:28", "01:01:03:28", "0:02.0", "none"]
    assert rows[1].split()[:4] == ["01:02:04:26", "01:02:11:11", "0:06.5", "other"]
    assert "2 of 10 clips have no video" in txt
    assert "1 of these leaves the screen empty" in txt
    # The clamped-sync warnings and hidden sources get their own sections,
    # and hidden clips are not in the main list.
    assert "PICTURE THAT MAY NOT MATCH" in txt and "CamB.mp4" in txt
    i = lines.index("LEFT OUT ON PURPOSE")
    assert any("Music_Cue_4" in l for l in lines[i:])
    assert not any("Music_Cue_4" in l for l in rows)


def test_markers_cover_only_the_holes():
    from aaf_workflow import missing_video_markers
    ms = missing_video_markers(
        [_m("D3.wav-01", "Denise_Part_3", 62.0, 64.0),
         _m("D3.wav-02", "Denise_Part_3", 64.5, 66.0),   # 0.5 s on: same spot
         _m("D3.wav-03", "Denise_Part_3", 90.0, 91.0),   # its own spot
         _m("D3.wav-04", "Denise_Part_3", 125.0, 131.5), # other camera covers
         _m("Cue.wav-01", "Music", 10.0, 40.0, hidden=True)],
        [(120.0, 140.0)])
    assert [(m["start_secs"], m["end_secs"]) for m in ms] == [
        (62.0, 66.0), (90.0, 91.0)]
    assert ms[0]["name"] == "NO VIDEO: Denise_Part_3"
    assert "2 clips" in ms[0]["comment"] and "D3.wav-02" in ms[0]["comment"]


def test_xml_carries_the_markers():
    import engines
    x = engines.build_xml_from_pt(
        [], [], "Ep 5", seq_fps=29.97, seq_start_secs=T0,
        markers=[{"name": "NO VIDEO: D3", "comment": "c",
                  "start_secs": 62.0, "end_secs": 66.0}])
    mk = x.find("sequence/marker")
    assert mk.find("name").text == "NO VIDEO: D3"
    # Same frame rounding as the clips: round(secs * fps).
    assert (mk.find("in").text, mk.find("out").text) == (
        str(round(62.0 * 29.97)), str(round(66.0 * 29.97)))


def test_report_when_nothing_is_missing():
    txt = missing_video_report([], [(0, 10)], [], INFO)
    assert "Every clip in the AAF has video." in txt


# ── The build writes it ──────────────────────────────────────────────────

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


def test_build_writes_the_report_next_to_the_xml(tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        pytest.skip("no display")
    import aaf_workflow
    import engines

    def touch(name):
        p = tmp_path / name
        p.write_bytes(b"")
        return str(p)
    cam_a = touch("CamA.mp4")
    aud = {n: touch(n + ".wav") for n in ("CamA", "CamB", "Denise_Part_3")}
    aaf = touch("show.aaf")

    def clip(name, s, e):
        return {"clip_name": name + ".wav-01", "start_secs": s, "end_secs": e,
                "src_in_secs": 10.0, "src_out_secs": 10.0 + e - s,
                "state": "Unmuted", "source_file": aud[name],
                "fade_in_secs": 0.0, "fade_out_secs": 0.0}

    def parse(path):
        return {"session_name": "show", "fps": 29.97, "sample_rate": 48000,
                "markers": [], "start_tc_secs": T0, "start_tc_frames": 108000,
                "tc_fps": 29.97,
                "tracks": [{"name": "A1", "clips": [clip("CamA", 0, 30)]},
                           {"name": "A2", "clips": [clip("Denise_Part_3", 40, 50)]},
                           {"name": "A3", "clips": [clip("CamB", 60, 70)]}]}
    monkeypatch.setattr(aaf_workflow, "parse_aaf_session", parse)
    monkeypatch.setattr(aaf_workflow, "offline_media", lambda paths: [])
    out = str(tmp_path / "Ep5_aaf.xml")
    monkeypatch.setattr(aaf_workflow.filedialog, "asksaveasfilename",
                        lambda **k: out)
    built = {}
    monkeypatch.setattr(engines, "build_xml_from_pt",
                        lambda *a, **k: built.update(k) or "<xmeml/>")
    monkeypatch.setattr(engines, "write_xml",
                        lambda x, p: open(p, "w").write(x))
    monkeypatch.setattr(engines, "channels_unknown", lambda x: [])
    monkeypatch.setattr(engines, "_probe_duration", lambda p: 0.0)
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
    try:
        app._aaf_load(aaf); _pump(app, 15)
        app._aaf_add_video_batch([cam_a]); _pump(app, 10)
        for b in app._aaf_sources:                 # only CamA has video
            app._aaf_source_file_vars[b].set(
                app._aaf_vid_label(cam_a) if b == "CamA" else "— no video —")
        app._aaf_hidden_sources = {"CamB"}         # CamB left out on purpose
        app._aaf_seq_w_var.set("1920"); app._aaf_seq_h_var.set("1080")
        app._aaf_fps_var.set("29.97")
        app._aaf_build()
        report = str(tmp_path / "Ep5_aaf_missing_video.txt")
        assert _wait(app, lambda: os.path.isfile(report)), (dialogs, errors)
        _pump(app, 5)
        txt = open(report, encoding="utf-8").read()
        assert "1 of 2 clips have no video" in txt
        row = next(l for l in txt.splitlines() if "Denise_Part_3.wav-01" in l)
        assert row.split()[:4] == ["01:00:39:29", "01:00:49:28", "0:10.0", "none"]
        assert "LEFT OUT ON PURPOSE" in txt and "CamB" in txt
        # One marker, over Denise's hole; none for the hidden CamB.
        assert [(m["name"], m["start_secs"], m["end_secs"])
                for m in built["markers"]] == [
            ("NO VIDEO: Denise_Part_3", 40.0, 50.0)]
        assert not errors, "\n".join(errors)
        assert not [d for d in dialogs if d[0] == "showerror"], dialogs
    finally:
        app.destroy()
