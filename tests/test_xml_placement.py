"""Picture placement in the Pro Tools -> XML build, and drift from the probes.

Measured on a real 23.976 export: every clip's picture landed 0 to 1.8
frames EARLY against the stereo mix, "one frame early" most often.  Two
causes, both fixed here and pinned by these tests:

* The timeline position and the source in-point were rounded to frames
  separately, so their errors added -- up to a frame per clip, a different
  frame per clip.  Each clip is now rounded once against the session
  clock, so its picture is at most half a frame from where the mix has
  the sound, and the spread is centred on zero.
* The three sync probes measure the camera clock drifting against the
  recorder (tens of ppm) and then averaged it away.  The drift is now
  returned and applied per clip.

No media is needed: build_xml_from_pt is driven with paths that do not
exist (its probes degrade to "unknown"), and the detector is driven with
a fake per-probe function.
"""
import math
import os
import random
import sys
import xml.etree.ElementTree as ET

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import engines  # noqa: E402
import parsers  # noqa: E402

FPS = 24000.0 / 1001.0          # what Premiere plays; the build snaps 23.976 to this
# Picture window: at most PICTURE_LATE_LIMIT_MS late, the rest of the frame
# early (perception tolerates sound late far better than sound early).
LATE  = engines.picture_late_limit_frames(23.976)       # 0.36 fr at 23.976
EARLY = 1.0 - LATE
EPS   = 1e-6


def _in_window(errs):
    return min(errs) >= -LATE - EPS and max(errs) <= EARLY + EPS


def _clip(start, dur, src_in, offset, drift=None, sf="ref.wav"):
    return {"clip_name": "Cam.wav-01", "track_name": "A1",
            "start_secs": start, "end_secs": start + dur,
            "src_in_secs": src_in, "src_out_secs": src_in + dur,
            "state": "Unmuted", "source_file": sf,
            "fade_in_secs": 0.0, "fade_out_secs": 0.0,
            "video_path": "cam.mp4", "audio_path": sf,
            "video_offset_secs": offset, "video_drift": drift,
            "source_base": "Cam"}


def _build(clips, mix_lag=0.0):
    stats = {}
    xmeml = engines.build_xml_from_pt(
        clips, ["A1"], "t", seq_fps=23.976, seq_sr=48000,
        stats_out=stats, mix_lag_secs=mix_lag)
    seq = xmeml.find("sequence")
    vids = seq.findall("media/video/track/clipitem")
    auds = seq.findall("media/audio/track/clipitem")
    return vids, auds, stats


def _picture_error_frames(ci, clip, offset_at_clip, mix_lag):
    """Signed frames between the picture shown at the clip's first timeline
    frame and the camera moment the mix is playing there (+ = early)."""
    start_fr = int(ci.find("start").text)
    v_in     = int(ci.find("in").text)
    session_t  = start_fr / FPS - mix_lag
    cam_should = (clip["src_in_secs"] + (session_t - clip["start_secs"])
                  + offset_at_clip)
    return v_in - cam_should * FPS


@pytest.mark.parametrize("seed", range(4))
def test_picture_within_half_a_frame_and_centred(seed):
    rng = random.Random(seed)
    clips, t = [], 0.3
    for _ in range(60):
        dur = rng.uniform(2.0, 20.0)
        clips.append(_clip(t, dur, rng.uniform(0, 4000), 0.740))
        t += dur + rng.uniform(0.0, 0.5)
    vids, auds, stats = _build(clips)
    assert len(vids) == len(clips) == len(auds)
    errs = [_picture_error_frames(ci, c, 0.740, 0.0) for ci, c in zip(vids, clips)]
    assert _in_window(errs), (min(errs), max(errs))
    # Sits where the window says: mean at the window's centre (+0.14 at
    # 23.976), not at the +0.8 the old build produced here.
    centre = (EARLY - LATE) / 2.0
    assert abs(sum(errs) / len(errs) - centre) < 0.15, sum(errs) / len(errs)
    r = stats["sync_residuals"][0]
    assert r["n_clips"] == len(clips)
    assert r["late_max_fr"] <= LATE + EPS and r["early_max_fr"] <= EARLY + EPS
    assert abs(stats["picture_late_limit_fr"] - LATE) < EPS
    # The source window is exactly as long as the timeline span: no speed
    # change for Premiere to infer.
    for ci in vids + auds:
        assert (int(ci.find("out").text) - int(ci.find("in").text)
                == int(ci.find("end").text) - int(ci.find("start").text))


def test_mix_lag_moves_the_target():
    clips = [_clip(10.0 + i * 7.3, 6.0, 100.0 + i * 11.1, 1.5) for i in range(40)]
    for lag in (0.014, -0.02):
        vids, _auds, stats = _build(clips, mix_lag=lag)
        errs = [_picture_error_frames(ci, c, 1.5, lag) for ci, c in zip(vids, clips)]
        assert _in_window(errs), (min(errs), max(errs))
        assert stats["mix_lag_secs"] == lag


def test_drift_applied_per_clip():
    # 60 ppm: over an hour of source the offset grows by 216 ms, five frames.
    drift = {"ppm": 60.0, "t_ref": 1800.0}
    clips = [_clip(5.0 + i * 9.0, 6.0, src_in, 2.0, drift)
             for i, src_in in enumerate(range(0, 3600, 90))]
    vids, _auds, stats = _build(clips)
    errs = [_picture_error_frames(ci, c, 2.0 + 60e-6 * (c["src_in_secs"] - 1800.0), 0.0)
            for ci, c in zip(vids, clips)]
    assert _in_window(errs), (min(errs), max(errs))
    # Against a constant offset the same clips would be off by frames.
    worst = max(abs(_picture_error_frames(ci, c, 2.0, 0.0)) for ci, c in zip(vids, clips))
    assert worst > 2.0
    assert abs(stats["sync_residuals"][0]["drift_ppm"] - 60.0) < 1e-9


def test_picture_window_follows_the_frame_rate():
    # 15 ms is 0.36 fr at 23.976 and 0.45 fr at 29.97; from 50 fps the
    # frame is too short for the limit to matter and rounding is nearest.
    assert abs(engines.picture_late_limit_frames(23.976) - 0.35964) < 1e-4
    assert abs(engines.picture_late_limit_frames(29.97) - 0.44955) < 1e-4
    assert engines.picture_late_limit_frames(59.94) == 0.5
    # Window edges: a target exactly `late` above a frame still rounds down
    # to it; a hair more and it goes up.
    late = engines.picture_late_limit_frames(23.976)
    assert engines.round_picture_frame(100.0 + late, 23.976) == 100          # late edge, inclusive
    assert engines.round_picture_frame(100.0 + late + 1e-6, 23.976) == 101
    assert engines.round_picture_frame(100.0 - (1 - late) + 1e-6, 23.976) == 100   # early edge
    assert engines.round_picture_frame(100.0 - (1 - late) - 1e-6, 23.976) == 99
    assert abs(engines.exact_fps(23.976) - FPS) < 1e-12
    assert engines.exact_fps(25.0) == 25.0


def test_detector_reports_drift_from_the_probes(monkeypatch):
    # Three probes on a line: 10 ppm around a 1-hour file.
    def fake_at(video_path, audio_path, probe_duration=300.0, sample_rate=8000,
                start_offset=0.0, has_slate=False):
        centre = start_offset + probe_duration / 2.0
        return 0.5 + 10e-6 * (centre - 1800.0), 1.0, []
    monkeypatch.setattr(parsers, "_detect_sync_offset_at", fake_at)
    monkeypatch.setattr(engines, "get_media_duration", lambda p: 3600.0)
    monkeypatch.setattr(parsers, "_event_diff_offset", lambda *a, **k: None)
    monkeypatch.setattr(parsers, "verify_sync_at_offset",
                        lambda vp, ap, T, start_offset=0.0: (T, 0.9))
    drift = {}
    T, conf, alts = parsers.detect_sync_offset(
        "cam.mp4", "ref.wav", return_candidates=True, drift_out=drift)
    assert abs(T - 0.5) < 0.002 and conf > 0.9
    assert abs(drift["ppm"] - 10.0) < 0.05
    assert abs(drift["t_ref"] - 1800.0) < 1.0
    assert drift["n_probes"] == 3

    # A probe that locked onto something else is not a drift line.
    def bent(video_path, audio_path, probe_duration=300.0, sample_rate=8000,
             start_offset=0.0, has_slate=False):
        centre = start_offset + probe_duration / 2.0
        T = 0.5 + (0.04 if abs(centre - 1800.0) < 1.0 else 0.0)
        return T, 1.0, []
    monkeypatch.setattr(parsers, "_detect_sync_offset_at", bent)
    drift = {}
    parsers.detect_sync_offset("cam.mp4", "ref.wav", drift_out=drift)
    assert drift == {}
