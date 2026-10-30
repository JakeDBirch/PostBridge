"""Long files reach Whisper a window at a time, and the words land in file time."""
import threading
import types
import wave

import numpy as np
import pytest

import engines


def _wav(path, seconds, pauses=()):
    pcm = np.full(int(seconds * 16000), 3000, dtype=np.int16)
    for at in pauses:
        pcm[int(at * 16000):int((at + 1.0) * 16000)] = 0
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(pcm.tobytes())
    return str(path)


class _FakeModel:
    """One word a second, for as long as it is given."""

    def __init__(self):
        self.calls = []

    def transcribe(self, audio, **options):
        self.calls.append((audio, dict(options)))
        secs = len(audio) / 16000 if not isinstance(audio, str) else 5

        def segs():
            for i in range(int(secs)):
                word = types.SimpleNamespace(word=" w{}".format(i),
                                             start=float(i), end=i + 0.5)
                yield types.SimpleNamespace(start=float(i), end=i + 0.5,
                                            text=" w{}".format(i), words=[word])
        return segs(), None


@pytest.fixture
def model(monkeypatch):
    fake = _FakeModel()
    monkeypatch.setattr(engines, "get_model", lambda size=None: fake)
    monkeypatch.setattr(engines, "get_active_model_size", lambda: "small")
    monkeypatch.setattr(engines, "WHISPER_WINDOW_S", 60)
    monkeypatch.setattr(engines, "WHISPER_WINDOW_SEEK_S", 10)
    return fake


def _windows(model):
    return [len(audio) / 16000 for audio, _ in model.calls]


def test_a_short_file_goes_whole(model, tmp_path):
    path = _wav(tmp_path / "a.wav", 30)
    engines.transcribe_clip_verbatim(path)
    assert [audio for audio, _ in model.calls] == [path]


def test_a_wav_whisper_would_resample_goes_whole(model, tmp_path):
    path = tmp_path / "a.wav"
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(48000)
        wf.writeframes(b"\0\0" * 2 * 48000 * 200)
    engines.transcribe_clip_verbatim(str(path))
    assert [audio for audio, _ in model.calls] == [str(path)]


def test_a_long_file_never_hands_whisper_more_than_a_window(model, tmp_path):
    engines.transcribe_clip_verbatim(_wav(tmp_path / "a.wav", 200))
    assert len(model.calls) >= 4
    assert max(_windows(model)) <= 60
    assert sum(_windows(model)) == pytest.approx(200, abs=0.01)


def test_windows_end_in_the_pause(model, tmp_path):
    engines.transcribe_clip_verbatim(_wav(tmp_path / "a.wav", 90, pauses=[54]))
    assert 54 < _windows(model)[0] < 55


@pytest.mark.parametrize("fn", [engines.transcribe_clip,
                                engines.transcribe_clip_verbatim])
def test_times_are_in_file_time_and_keep_going_up(model, tmp_path, fn):
    seen = []
    words = fn(_wav(tmp_path / "a.wav", 200), progress_cb=seen.append)
    starts = [w["start"] for w in words if not w.get("break")]
    assert starts == sorted(starts)
    assert starts[-1] > 190
    assert seen == sorted(seen) and seen[-1] > 190


def test_verbatim_carries_the_sentence_across_the_join(model, tmp_path):
    engines.transcribe_clip_verbatim(_wav(tmp_path / "a.wav", 200))
    assert "initial_prompt" not in model.calls[0][1]
    assert model.calls[1][1]["initial_prompt"].startswith(" w")


def test_matching_transcription_stays_independent_across_the_join(model, tmp_path):
    engines.transcribe_clip(_wav(tmp_path / "a.wav", 200))
    assert all("initial_prompt" not in o for _, o in model.calls)


def test_cancel_stops_at_the_next_segment(model, tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(engines.TranscriptionCancelled):
        engines.transcribe_clip_verbatim(_wav(tmp_path / "a.wav", 200),
                                         cancel_event=cancel)
    assert len(model.calls) == 1


def test_whole_file_extraction_has_no_length_limit(tmp_path, monkeypatch):
    # A sentinel end time used to cut anything past 2:46:39.
    src = _wav(tmp_path / "src.wav", 3)
    out = tmp_path / "out.wav"
    seen = {}
    real = engines._run

    def spy(cmd, **kw):
        seen["cmd"], seen["timeout"] = cmd, kw.get("timeout")
        return real(cmd, **kw)
    monkeypatch.setattr(engines, "_run", spy)
    ok, err = engines.extract_window(src, 0, None, str(out))
    assert ok, err
    assert "-t" not in seen["cmd"]
    assert seen["timeout"] > 60
    with wave.open(str(out)) as wf:
        assert wf.getnframes() == pytest.approx(3 * 16000, abs=2000)
