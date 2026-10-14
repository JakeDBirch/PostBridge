"""Custom track map: each camera (source x slot) names its video track.

One narrator shot on three cameras over two days is two AAF sources; the
per-source layout gives each day its own stack.  With the map, both days'
first camera land on Video 1 and so on, and the audio tracks collapse with
them because the build keys clip audio and camera audio by the same name.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from test_aaf_reimport import fx, _pump  # noqa: E402,F401  (fixture)


def test_default_map_shares_tracks_by_slot(fx):
    app = fx.app
    fx.enter_step2("open")
    # Two sources, one camera each: both default to Video 1.
    for base in ("CamA", "CamB"):
        assert app._aaf_track_name_for("custom", base, fx.vid_a, 0, "A1") == "Video 1"
        assert app._aaf_track_name_for("custom", base, fx.vid_a, 1, "A1") == "Video 2"
    # The other modes are untouched.
    assert app._aaf_track_name_for("single", "CamA", fx.vid_a, 0, "A1") == "_consolidate_"
    assert app._aaf_track_name_for("by_track", "CamA", fx.vid_a, 1, "A1") == "A1  \u00b7  ANGLE 2"
    assert app._aaf_track_name_for("by_source", "CamA", fx.vid_a, 0, "A1") == "CamA.mp4"


def test_map_edits_survive_save_and_restore(fx):
    app = fx.app
    fx.enter_step2("open")
    app._aaf_group_var.set("custom"); _pump(app, 5)
    assert app._aaf_track_map_frame.winfo_ismapped()
    app._aaf_track_map_var("CamB", 0).set("Video 3")
    data = app._aaf_build_setup_data()
    assert data["grouping"] == "custom"
    assert data["track_map"] == {"CamA": ["Video 1"], "CamB": ["Video 3"]}

    app._aaf_track_map_var("CamB", 0).set("Video 1")
    app._aaf_group_var.set("single"); _pump(app, 3)
    assert not app._aaf_track_map_frame.winfo_ismapped()
    app._aaf_restore_setup(data); _pump(app, 10)
    assert app._aaf_group_var.get() == "custom"
    assert app._aaf_track_name_for("custom", "CamB", fx.vid_b, 0, "A2") == "Video 3"
    assert not fx.errors, "\n".join(fx.errors)


def test_track_order_is_numeric():
    from aaf_workflow import AafWorkflowMixin as M
    assert M._aaf_sort_track_names(["Video 3", "Video 1", "Video 10", "Video 2"]) == \
        ["Video 1", "Video 2", "Video 3", "Video 10"]
    assert M._aaf_sort_track_names(["Video 2", "Other", "Video 1"]) == \
        ["Video 1", "Video 2", "Other"]
