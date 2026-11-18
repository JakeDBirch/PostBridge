"""Script -> Session Step 2: INCLUDE VIDEO.

An AAF for Pro Tools is cut from audio, so with video out the pool keeps only
the video that is all a token has (an interrogation tape).  The rest is
hidden and in nobody's assignments, but stays in the pool and in a save, so
putting video back in brings it back as it was.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

UN = "— unassigned —"
D = os.path.join(ROOT, "no_such_dir")
FILES = {
    "guest_mic.wav":      "GUEST",
    "guest_cam_a.mov":    "GUEST",       # a camera of a token that has audio
    "interrogation.mp4":  "SUSPECT",     # all SUSPECT has
    "narr.wav":           "VO: Open",
    "narr_cam.mp4":       "VO: Open",
    "stray.mov":          UN,            # nobody's yet
    "riverside_bob_raw_audio.wav":            UN,
    "riverside_bob_raw_synced_video_cfr.mp4": UN,   # the same recording
}


def _p(name):
    return os.path.join(D, name)


@pytest.fixture
def pool(monkeypatch):
    tk = pytest.importorskip("tkinter")
    import gui_components as GC
    monkeypatch.setattr(GC, "HAS_DND", False)
    monkeypatch.setattr(GC, "_get_dur_key", lambda path: None)
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    # aaf_mode off: its name-pair prune would take the Riverside video away
    # before this switch is asked about it.
    p = GC.MediaPool(root, ["GUEST", "SUSPECT"], [{"name": "Open", "index": 0}])
    p.pack(fill="both", expand=True)
    p._bulk_loading = True                   # as a restore: no re-pairing by name
    for fn, tok in FILES.items():
        p._add(_p(fn))
        p._rows[-1]["var"].set(tok)
    p._bulk_loading = False
    p._sort_rows()
    p._refresh_count()
    yield p
    root.update()
    root.destroy()


def _shown(pool):
    by_widget = {r["row"]: r for r in pool._rows}
    return {os.path.basename(by_widget[w]["path"])
            for w in pool._table.pack_slaves()}


KEPT = {"guest_mic.wav", "interrogation.mp4", "narr.wav", "stray.mov",
        "riverside_bob_raw_audio.wav"}


def test_video_is_in_until_it_is_switched_off(pool):
    assert _shown(pool) == set(FILES)
    assert pool._count_lbl.cget("text") == "8 files"
    assert _p("guest_cam_a.mov") in pool.get_assignments()["GUEST"]


def test_out_keeps_only_the_video_with_no_audio(pool):
    pool._toggle_video()
    assert _shown(pool) == KEPT
    assert "3 video left out" in pool._count_lbl.cget("text")
    asg = pool.get_assignments()
    assert asg["GUEST"] == [_p("guest_mic.wav")]
    assert asg["SUSPECT"] == [_p("interrogation.mp4")]
    assert pool.get_vo_assets()[0] == {"audios": [_p("narr.wav")], "videos": []}


def test_what_is_left_out_stays_in_the_pool_and_comes_back(pool):
    pool._toggle_video()
    assert len(pool._rows) == len(FILES)
    assert dict(pool.assignment_pairs())[_p("guest_cam_a.mov")] == "GUEST"
    pool._toggle_video()
    assert _shown(pool) == set(FILES)
    assert _p("guest_cam_a.mov") in pool.get_assignments()["GUEST"]


def test_it_follows_the_assignments(pool):
    pool._toggle_video()
    row = {os.path.basename(r["path"]): r for r in pool._rows}
    row["stray.mov"]["var"].set("GUEST")            # now a camera of GUEST
    row["guest_mic.wav"]["var"].set(UN)             # ...whose audio just left
    pool._deferred_sort()
    assert {"stray.mov", "guest_cam_a.mov"} <= _shown(pool)
    row["guest_mic.wav"]["var"].set("GUEST")
    pool._deferred_sort()
    assert not {"stray.mov", "guest_cam_a.mov"} & _shown(pool)


def test_an_unassigned_camera_the_episode_pairs_with_audio_is_out(pool):
    pool.paired_videos = {os.path.normcase(_p("stray.mov"))}
    pool._toggle_video()
    assert "stray.mov" not in _shown(pool)


def test_the_same_length_as_an_audio_file_is_the_same_recording(pool):
    row = {os.path.basename(r["path"]): r for r in pool._rows}
    row["stray.mov"]["link_id"] = row["riverside_bob_raw_audio.wav"]["link_id"] = "1800"
    pool._toggle_video()
    assert "stray.mov" not in _shown(pool)


def test_the_caller_hears_a_click_and_a_restored_choice(pool):
    heard = []
    pool.on_video_toggle = heard.append
    pool._toggle_video()
    pool.restore_video({"assignments": {}})          # an older save: no say
    assert pool.video_off() and heard == [True]
    pool.restore_video({"video_off": False})
    assert not pool.video_off() and heard == [True, False]
    assert pool._video_lbl.cget("text").endswith("INCLUDE VIDEO")
