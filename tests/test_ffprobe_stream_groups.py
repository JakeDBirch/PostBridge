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
    engines._channel_failed.clear()
    engines._codec_cache.clear()


def _xml_for(path):
    root = engines.Element("xmeml")
    root.append(engines._make_clipitem("clip-1", "file-1", path, 0, 24, 0, 24,
                                       24.0, True, True, 1920, 1080, 48000))
    return root


def test_failed_probe_is_named_for_the_warning(monkeypatch):
    _answers(monkeypatch, "", returncode=1)
    assert engines.channels_unknown(_xml_for("F:/cam.mp4")) == ["cam.mp4"]


def test_slow_probe_counts_as_unknown(monkeypatch):
    def _slow(*a, **k):
        assert k["timeout"] >= 60
        raise subprocess.TimeoutExpired("ffprobe", k["timeout"])
    monkeypatch.setattr(engines, "_run", _slow)
    engines._channel_cache.clear()
    engines._channel_failed.clear()
    assert engines.channels_unknown(_xml_for("F:/cam.mp4")) == ["cam.mp4"]


def test_video_only_file_is_not_a_failed_probe(monkeypatch):
    _answers(monkeypatch, "\n")
    root = _xml_for("F:/raw-video.mp4")
    assert engines.probe_audio_channels("F:/raw-video.mp4") == 0
    assert root.find(".//file/media/audio/channelcount") is None
    assert engines.channels_unknown(root) == []


def test_counted_file_clears_an_earlier_failure(monkeypatch):
    _answers(monkeypatch, "", returncode=1)
    engines.probe_audio_channels("F:/cam.mp4")
    monkeypatch.setattr(
        engines, "_run",
        lambda *a, **k: SimpleNamespace(stdout="2\n", returncode=0))
    assert engines.channels_unknown(_xml_for("F:/cam.mp4")) == []


def test_warning_names_the_files():
    text = engines.channels_unknown_warning(["cam.mp4"])
    assert "cam.mp4" in text and "Cannot Link Media" in text


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
