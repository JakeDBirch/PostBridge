"""DVD .VOB support: the pool accepts VOBs, sync reads them on the
picture's clock, and a VTS_nn_1/_2 byte-split set joins back into one
playable stream.  Uses whatever ffmpeg is on PATH; numpy is required."""
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import is_dvd_menu_vob, is_video  # noqa: E402


def test_vob_is_video_and_menus_are_spotted():
    assert is_video("F:/DVD/VIDEO_TS/VTS_01_1.VOB")
    assert is_video("clip.vob")
    assert is_dvd_menu_vob("VIDEO_TS.VOB")
    assert is_dvd_menu_vob("/x/VIDEO_TS/vts_03_0.vob")
    assert not is_dvd_menu_vob("VTS_03_1.VOB")
    assert not is_dvd_menu_vob("VTS_03_0.mp4")


np = pytest.importorskip("numpy")
needs_ffmpeg = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe not on PATH")

REF_SRC = ("anoisesrc=d=60:c=pink:r=48000:a=0.3:seed=1,"
           "volume='if(lt(mod(t*t*0.37,1.9),0.9),1,0.15)':eval=frame")


def _ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True,
                   capture_output=True, timeout=120)


def _make_vob(tmp_path, name, cam_offset, audio_lag=0.0):
    """A DVD-style program stream (MPEG-2 + AC-3) carrying the reference
    cam_offset s into the clip.  audio_lag shifts the audio stream's
    timestamps so it starts that much after the picture, as some
    authoring tools do."""
    ref = str(tmp_path / "ref.wav")
    if not os.path.exists(ref):
        _ff("-f", "lavfi", "-i", REF_SRC, "-ac", "1", ref)
    out = str(tmp_path / name)
    _ff("-f", "lavfi", "-i", "testsrc=size=352x240:rate=30000/1001:duration=50",
        "-itsoffset", str(audio_lag), "-i", ref, "-t", "50",
        "-map", "0:v", "-map", "1:a",
        "-af", "adelay={}:all=1".format(int(cam_offset * 1000)),
        "-c:v", "mpeg2video", "-b:v", "1500k", "-c:a", "ac3", "-ar", "48000",
        "-f", "vob", out)
    return out, ref


@needs_ffmpeg
@pytest.mark.parametrize("start", [0.0, 20.0])   # read from the top / seek in
@pytest.mark.parametrize("audio_lag", [0.0, 0.4])
def test_sync_offset_is_measured_from_the_first_frame(tmp_path, audio_lag,
                                                      start):
    from parsers import detect_sync_offset, vob_av_skew
    vob, ref = _make_vob(tmp_path, "VTS_01_1.VOB", 3.5, audio_lag)
    skew = vob_av_skew(vob)
    assert abs(skew - audio_lag) < 0.05
    T, conf, _alts = detect_sync_offset(
        vob, ref, probe_duration=300.0, start_offset=start,
        return_candidates=True, has_slate=False)
    want = 3.5 + skew
    assert abs(T - want) < 0.05, "got {:.3f} s, want {:.3f} s".format(T, want)
    assert conf > 0.5


@needs_ffmpeg
def test_vts_split_set_joins_into_one_stream(tmp_path):
    import engines
    from parsers import detect_sync_offset
    whole, ref = _make_vob(tmp_path, "whole.vob", 3.5)
    data = open(whole, "rb").read()
    cut = (len(data) // 2 // 2048) * 2048        # DVD sector boundary
    parts = [str(tmp_path / "VTS_01_1.VOB"), str(tmp_path / "VTS_01_2.VOB")]
    open(parts[0], "wb").write(data[:cut])
    open(parts[1], "wb").write(data[cut:])

    assert engines.probe_join_continuity(parts)[0] == "ok"
    assert engines.probe_join_continuity(parts[::-1])[0] == "gap"
    assert engines.join_output_ext(parts) == ".vob"

    ok, msg, out = engines.concat_video_files(
        parts, str(tmp_path / "joined.mp4"))
    assert ok, msg
    assert out.endswith(".vob")
    assert abs(engines.get_media_duration(out) - 50.0) < 0.5
    # The audio must still line up past the seam, not just in piece one.
    T, conf, _ = detect_sync_offset(out, ref, start_offset=30.0,
                                    return_candidates=True)
    assert abs(T - 3.5) < 0.05, "got {:.3f} s".format(T)
