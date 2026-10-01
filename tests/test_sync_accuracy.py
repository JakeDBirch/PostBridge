"""Auto-sync detector accuracy on synthetic clips with a known offset.

Detector quality lives here, on every PR, rather than in the release
build's --self-test (which only checks that sync runs with the bundled
ffmpeg).  Uses whatever ffmpeg is on PATH; numpy is required.

Each case builds a 60 s reference recording (pink noise gated on and off
at irregular intervals, like speech) and a 50 s camera clip carrying that
recording at a known offset, then checks detect_sync_offset finds it.
"""
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("numpy")
if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
    pytest.skip("ffmpeg/ffprobe not on PATH", allow_module_level=True)

from parsers import detect_sync_offset  # noqa: E402

# Irregular gating: the gap between loud stretches changes over time, so
# no two offsets produce the same loudness pattern.
REF_SRC = ("anoisesrc=d=60:c=pink:r=48000:a=0.3:seed={seed},"
           "volume='if(lt(mod(t*t*0.37,1.9),0.9),1,0.15)':eval=frame")


def _ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True,
                   capture_output=True, timeout=120)


def _make_clips(tmp_path, cam_offset, seed):
    """cam_offset > 0: the camera rolled first (reference audio starts
    cam_offset s into the clip).  cam_offset < 0: the camera came in late
    (the clip starts -cam_offset s into the reference)."""
    ref = str(tmp_path / "ref.wav")
    cam = str(tmp_path / "cam.mp4")
    _ff("-f", "lavfi", "-i", REF_SRC.format(seed=seed), "-ac", "1", ref)
    video = ["-f", "lavfi", "-i", "testsrc=size=160x120:rate=24:duration=50"]
    if cam_offset < 0:
        audio = ["-ss", str(-cam_offset), "-i", ref]
        af = []
    else:
        audio = ["-i", ref]
        af = ["-af", "adelay={}:all=1".format(int(cam_offset * 1000))]
    _ff(*video, *audio, "-t", "50", "-map", "0:v", "-map", "1:a", *af,
        "-c:v", "mpeg4", "-c:a", "aac", cam)
    return cam, ref


@pytest.mark.parametrize("offset", [-3.5, 0.0, 3.5])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_finds_known_offset(tmp_path, offset, seed):
    cam, ref = _make_clips(tmp_path, offset, seed)
    T, conf, _alts = detect_sync_offset(
        cam, ref, probe_duration=300.0, start_offset=0.0,
        return_candidates=True, has_slate=False)
    assert abs(T - offset) < 0.05, "got {:.3f} s at {:.0f}%".format(T, conf * 100)
    assert conf > 0.5, "got {:.3f} s at {:.0f}%".format(T, conf * 100)
