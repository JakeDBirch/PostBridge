"""ffprobe 9 lists a stream twice when it belongs to a stream group, which a
timecode track makes it do.  Single-value probes have to survive that."""
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import engines


def _answers(monkeypatch, stdout, returncode=0):
    monkeypatch.setattr(
        engines, "_run",
        lambda *a, **k: SimpleNamespace(stdout=stdout, returncode=returncode))
    engines._channel_cache.clear()
    engines._codec_cache.clear()


def test_channel_count_listed_twice_is_still_a_count(monkeypatch):
    _answers(monkeypatch, "2\n\n2\n")
    assert engines.probe_audio_channels("cam.mp4") == 2


def test_codec_listed_twice_is_one_codec(monkeypatch):
    _answers(monkeypatch, "aac\n\naac\n")
    assert engines._probe_codec("cam.mp4", "a:0") == "aac"


def test_no_stream_is_empty_and_failure_is_none(monkeypatch):
    _answers(monkeypatch, "\n")
    assert engines._ffprobe_csv("cam.mp4", "stream=channels") == ""
    _answers(monkeypatch, "", returncode=1)
    assert engines._ffprobe_csv("cam.mp4", "stream=channels") is None


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_real_stereo_file_with_timecode_track(tmp_path):
    clip = tmp_path / "cam.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", "color=c=black:s=64x64:r=24:d=0.5",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
         "-shortest", "-timecode", "01:00:00:00", "-c:v", "libx264",
         "-c:a", "aac", str(clip)],
        check=True)
    engines._channel_cache.clear()
    assert engines.probe_audio_channels(str(clip)) == 2


def test_camera_file_declares_its_channels_in_the_xml(monkeypatch):
    _answers(monkeypatch, "2\n\n2\n")
    ci = engines._make_clipitem("clip-1", "file-1", "F:/cam.mp4", 0, 24, 0, 24,
                                24.0, True, True, 1920, 1080, 48000)
    assert ci.find("file/media/audio/channelcount").text == "2"
