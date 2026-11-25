"""The episode page as a media pool: a row per file, a token on each.

The page used to define "sources" -- a named thing of its own, with files
attached to it and sources combined with each other -- beside the tokens the
script, Step 2 and Pro Tools all go by.  It is now the pool Step 2 shows:
every recording and camera, each assigned to a token.
"""
import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import episode as EP  # noqa: E402


def _episode(tmp_path):
    ep = EP.new_episode(str(tmp_path))
    for r, kind, secs in [("a/guest.wav", "audio", 100.0), ("a/jordan.wav", "audio", 100.2),
                          ("v/guest.mp4", "video", 100.1),
                          ("a/vo1.wav", "audio", 50.0), ("v/vo1.mp4", "video", 50.0),
                          ("a/vo2.wav", "audio", 70.0), ("v/loose.mp4", "video", 70.1),
                          ("a/sting.wav", "audio", 9.0)]:
        ep["media"][r] = {"kind": kind, "size": 1, "duration_s": secs,
                          "status": "source", "reason": "", "dup_of": None}
    ep["media"]["a/sting.wav"]["status"] = "short"
    ep["sources"] = {
        "guest": {"token": None, "session": None, "video": ["v/guest.mp4"],
                  "audio": ["a/guest.wav", "a/jordan.wav"]},
        "vo1": {"token": None, "session": None, "audio": ["a/vo1.wav"], "video": ["v/vo1.mp4"]},
        "vo2": {"token": None, "session": None, "audio": ["a/vo2.wav"], "video": []}}
    return ep


def _shown(ep):
    return sorted((r["rel"], r["label"]) for r in EP.pool_rows(ep))


def test_the_pool_lists_every_file_once_with_its_token(tmp_path):
    ep = _episode(tmp_path)
    ep["sources"]["guest"]["token"] = "GUEST"
    ep["sources"]["vo1"]["vo_part"] = "Open"
    assert _shown(ep) == [
        ("a/guest.wav", "GUEST"), ("a/jordan.wav", "GUEST"), ("a/vo1.wav", "VO: Open"),
        ("a/vo2.wav", None), ("v/guest.mp4", "GUEST"),
        ("v/loose.mp4", None),                  # in no group yet, still in the pool
        ("v/vo1.mp4", "VO: Open")]
    assert "a/sting.wav" not in [r["rel"] for r in EP.pool_rows(ep)]    # put aside


def test_files_recorded_together_take_a_token_together(tmp_path):
    ep = _episode(tmp_path)
    EP.assign_file(ep, "a/guest.wav", "audio", "GUEST", "guest")
    assert [l for r, l in _shown(ep) if "guest" in r or "jordan" in r] == ["GUEST"] * 3


def test_once_named_a_file_moves_alone(tmp_path):
    ep = _episode(tmp_path)
    EP.assign_file(ep, "a/guest.wav", "audio", "GUEST", "guest")
    EP.assign_file(ep, "a/jordan.wav", "audio", "HOST", "guest")
    assert dict(_shown(ep))["a/jordan.wav"] == "HOST"
    assert dict(_shown(ep))["a/guest.wav"] == "GUEST"
    assert dict(_shown(ep))["v/guest.mp4"] == "GUEST"
    # ...and back under a token that exists joins it.
    EP.assign_file(ep, "a/jordan.wav", "audio", "GUEST", EP.groups_with(ep, "HOST")[0])
    assert not EP.groups_with(ep, "HOST")
    assert sorted(ep["sources"][EP.groups_with(ep, "GUEST")[0]]["audio"]) == [
        "a/guest.wav", "a/jordan.wav"]


def test_two_reads_of_a_part_stay_two_takes_and_a_camera_finds_its_own(tmp_path):
    ep = _episode(tmp_path)
    EP.assign_file(ep, "a/vo1.wav", "audio", "VO: Open", "vo1")
    EP.assign_file(ep, "a/vo2.wav", "audio", "VO: Open", "vo2")
    assert len(EP.groups_with(ep, "VO: Open")) == 2
    # A loose camera for that part goes with the read it ran beside.
    group = EP.assign_file(ep, "v/loose.mp4", "video", "VO: Open", None)
    assert ep["sources"][group]["audio"] == ["a/vo2.wav"]
    assert ep["sources"][group]["video"] == ["v/loose.mp4"]


def test_unassigning_takes_one_file_out(tmp_path):
    ep = _episode(tmp_path)
    EP.assign_file(ep, "a/guest.wav", "audio", "GUEST", "guest")
    EP.assign_file(ep, "v/guest.mp4", "video", None, "guest")
    shown = dict(_shown(ep))
    assert shown["v/guest.mp4"] is None and shown["a/guest.wav"] == "GUEST"
    # A file alone under its token: the token is simply taken off.
    EP.assign_file(ep, "a/vo2.wav", "audio", "NARR", "vo2")
    EP.assign_file(ep, "a/vo2.wav", "audio", None, "vo2")
    assert dict(_shown(ep))["a/vo2.wav"] is None and "vo2" in ep["sources"]


def test_a_file_can_sit_under_a_second_token(tmp_path):
    ep = _episode(tmp_path)
    EP.assign_file(ep, "a/guest.wav", "audio", "GUEST1", "guest")
    EP.assign_file(ep, "a/vo2.wav", "audio", "GUEST2", "vo2")
    EP.add_label(ep, "a/jordan.wav", "audio", "GUEST2")
    assert [l for r, l in _shown(ep) if r == "a/jordan.wav"] == ["GUEST1", "GUEST2"]
    # A token nobody has yet makes its own.
    EP.add_label(ep, "a/jordan.wav", "audio", "HOST")
    assert [l for r, l in _shown(ep) if r == "a/jordan.wav"] == ["GUEST1", "GUEST2", "HOST"]


def test_a_token_is_renamed_for_all_its_files(tmp_path):
    ep = _episode(tmp_path)
    EP.assign_file(ep, "a/guest.wav", "audio", "GEUST", "guest")
    EP.rename_label(ep, "GEUST", "GUEST")
    assert [l for r, l in _shown(ep) if "guest" in r or "jordan" in r] == ["GUEST"] * 3
    assert EP.clean_token(" lynn richardson-2 ") == "LYNN_RICHARDSON_2"
    assert EP.clean_token("  ") == "" and EP.clean_token(None) == ""


# ── The page ──────────────────────────────────────────────────────────────

if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
    pytest.skip("the app fixture's project needs ffmpeg", allow_module_level=True)

from test_episode import project  # noqa: E402,F401  (module-scoped fixture)
from test_episode_screens import app, _open_fresh, _pump  # noqa: E402,F401


def _labels(w):
    import tkinter as tk
    out = []
    for c in w.winfo_children():
        if isinstance(c, tk.Label):
            out.append(c.cget("text"))
        out += _labels(c)
    return out


def _row(ep, name, label="any"):
    return next(r for r in EP.pool_rows(ep)
                if os.path.basename(r["rel"]) == name and label in ("any", r["label"]))


def test_the_page_is_a_pool_and_a_dropdown_assigns(app, monkeypatch, project):
    _open_fresh(app, monkeypatch, project)
    ep = app._ep
    shown = _labels(app.body)
    assert "MEDIA POOL" in shown and "BUTLER" in shown and app._EP_NONE in shown
    for gone in ("SOURCES", "NOT YET IN A SOURCE", "REMOVE SOURCE"):
        assert gone not in shown, gone
    assert not any("combine with" in t for t in shown)
    assert "Butler Interview_CARY.wav" in " ".join(shown)

    # One narrator read is assigned: its cameras come with it.
    app._ep_pool_set(_row(ep, "Ep 7 pt 1.wav"), "VO: Open")
    _pump(app, 5)
    assert _row(ep, "Ep 7 pt 1.wav")["label"] == "VO: Open"
    assert _row(ep, "Ep 7 CAM 1 01.mp4")["label"] == "VO: Open"
    assert "VO: Open" in _labels(app.body)

    # A typed token.
    import episode_workflow
    monkeypatch.setattr(episode_workflow.simpledialog, "askstring",
                        lambda *a, **k: "cary 2")
    app._ep_pool_set(_row(ep, "Butler Interview_CARY.wav"), app._EP_NEW)
    _pump(app, 5)
    assert _row(ep, "Butler Interview_CARY.wav")["label"] == "CARY_2"
    assert _row(ep, "Butler Interview_JORDAN.wav")["label"] == "BUTLER"

    # The same file under a second token, then taken out from under it.
    app._ep_pool_set(_row(ep, "Butler Interview_JORDAN.wav"), "CARY_2", add=True)
    _pump(app, 5)
    assert sorted(r["label"] for r in EP.pool_rows(ep)
                  if r["rel"].endswith("_JORDAN.wav")) == ["BUTLER", "CARY_2"]
    assert any(t.startswith("also BUTLER") for t in _labels(app.body))
    app._ep_pool_remove(_row(ep, "Butler Interview_JORDAN.wav", "CARY_2"))
    _pump(app, 5)
    assert [r["label"] for r in EP.pool_rows(ep) if r["rel"].endswith("_JORDAN.wav")] == ["BUTLER"]

    # Renaming a token, and putting a file aside.
    monkeypatch.setattr(episode_workflow.simpledialog, "askstring", lambda *a, **k: "CARY")
    app._ep_pool_rename("CARY_2")
    _pump(app, 5)
    assert _row(ep, "Butler Interview_CARY.wav")["label"] == "CARY"
    app._ep_exclude(_row(ep, "Ep 7 pt 2.wav")["rel"])
    _pump(app, 5)
    assert "Ep 7 pt 2.wav" not in [os.path.basename(r["rel"]) for r in EP.pool_rows(ep)]
    assert not app._errors, "\n".join(app._errors)
