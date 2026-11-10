"""SYNC seeded from the typed offset: audio split into parts, one video.

A long interview was cut into Part 1/2/3 audio files for transcription,
but there is still one video.  Part 2 starts Part 1's length into the
video -- minutes past the detector's own ±2 minute reach -- so SYNC has
to search around the offset the user typed rather than around zero.
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

LEN = 600.0
CUTS = [0.0, 200.0, 400.0]   # where each part starts in the recording
# Pink noise gated by a sum of incommensurate sines: speech-like loudness
# whose rhythm never repeats and never speeds up over a long file.
REF_SRC = ("anoisesrc=d={d}:c=pink:r=48000:a=0.3:seed=7,"
           "volume='if(lt(sin(t*1.3)+sin(t*0.71)+sin(t*0.29),0.3),1,0.15)'"
           ":eval=frame")


def _ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True,
                   capture_output=True, timeout=300)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("split")
    ref = str(d / "whole.wav")
    cam = str(d / "cam.mp4")
    _ff("-f", "lavfi", "-i", REF_SRC.format(d=int(LEN)), "-ac", "1", ref)
    _ff("-f", "lavfi", "-i",
        "testsrc=size=160x120:rate=24:duration={}".format(int(LEN)),
        "-i", ref, "-map", "0:v", "-map", "1:a", "-shortest",
        "-c:v", "mpeg4", "-c:a", "aac", cam)
    parts = []
    for i, start in enumerate(CUTS):
        end = CUTS[i + 1] if i + 1 < len(CUTS) else LEN
        p = str(d / "part{}.wav".format(i + 1))
        _ff("-ss", str(start), "-t", str(end - start), "-i", ref, p)
        parts.append(p)
    return cam, parts


@pytest.mark.parametrize("part,hint", [
    (2, 200.0),    # Part 1's exact length
    (2, 188.0),    # a rough guess, too early
    (2, 211.0),    # a rough guess, too late
    (3, 400.0),
])
def test_finds_later_part_from_hint(media, part, hint):
    cam, parts = media
    T, conf, _alts = detect_sync_offset(
        cam, parts[part - 1], return_candidates=True, offset_hint=hint)
    want = CUTS[part - 1]
    assert abs(T - want) < 0.05, "got {:.3f} s at {:.0f}%".format(T, conf * 100)
    assert conf > 0.5, "got {:.3f} s at {:.0f}%".format(T, conf * 100)


def test_without_hint_later_part_is_out_of_reach(media):
    """Why the hint exists: unseeded, Part 3 (400 s in) can't be found."""
    cam, parts = media
    T, _conf = detect_sync_offset(cam, parts[2])
    assert abs(T - CUTS[2]) > 1.0


def test_small_hint_searches_as_before(media):
    """Part 1 with OFFSET 0 (or anything under a minute) is the ordinary
    search, untouched."""
    cam, parts = media
    T, conf = detect_sync_offset(cam, parts[0], offset_hint=0.0)
    assert abs(T) < 0.05 and conf > 0.5
