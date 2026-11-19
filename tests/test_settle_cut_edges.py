"""A matched pull's cut edges are moved off sound, into the silence beside
them (engines.settle_cut_edges).

Whisper's word times are good to a tenth of a second or so.  An in-point
that late clips the word's attack; an out-point that late chops the
breath after it.  Both are heard as an edit once the cut plays without
the Assembly view's playback smoothing.
"""
import wave

import numpy as np
import pytest

import engines

SR = 16000


def _write(path, spans, seconds, breaths=()):
    """A mono WAV: speech-level noise over each (start, end) of `spans`,
    quiet breaths (16 dB down) over `breaths`, near-silence elsewhere."""
    rng = np.random.default_rng(1)
    pcm = rng.standard_normal(int(seconds * SR)) * 0.0005
    for (a, b), level in [(s, 0.2) for s in spans] + [(s, 0.03) for s in breaths]:
        i, j = int(a * SR), int(b * SR)
        pcm[i:j] = rng.standard_normal(j - i) * level
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes((np.clip(pcm, -1, 1) * 32767).astype(np.int16).tobytes())
    return str(path)


@pytest.fixture
def tape(tmp_path):
    # "...about it." 2.0-4.0, a breath 4.08-4.40, "Yeah" 4.40-4.75 (left
    # out of the cut), silence, "It's just a shame" 5.50-8.0.
    return _write(tmp_path / "tape.wav",
                  [(2.0, 4.0), (4.40, 4.75), (5.50, 8.0)], 10.0,
                  breaths=[(4.08, 4.40)])


def test_late_in_point_moves_ahead_of_the_attack(tape):
    (a, b), = engines.settle_cut_edges([(5.54, 7.5)], tape)
    assert 5.50 - engines.EDGE_LEAD_S - 0.011 <= a <= 5.50 - 0.02
    assert b == 7.5                                    # in running speech


def test_out_point_on_the_breath_moves_back_to_the_word_end(tape):
    (a, b), = engines.settle_cut_edges([(2.5, 4.18)], tape)
    assert 4.0 <= b <= 4.08                            # after the word, before the breath
    assert a == 2.5


def test_early_out_point_moves_out_to_the_tail(tape):
    (a, b), = engines.settle_cut_edges([(5.6, 7.9)], tape)
    assert 8.0 <= b <= 8.0 + engines.EDGE_TAIL_S + 0.011


def test_edges_already_in_silence_stay(tape):
    segs = [(1.9, 4.05), (5.40, 8.2)]
    assert engines.settle_cut_edges(segs, tape) == segs


def test_running_speech_is_left_alone(tape):
    segs = [(2.6, 3.4)]
    assert engines.settle_cut_edges(segs, tape) == segs


def test_never_brings_back_a_word_the_cut_left_out(tape):
    # The in-point sits at the end of "Yeah", which the cut dropped; the
    # silence before the next word starts after it, so nothing of it returns.
    words = [{"start": 4.40, "end": 4.75, "word": "Yeah."},
             {"start": 5.54, "end": 5.9, "word": "It's"}]
    (a, _b), = engines.settle_cut_edges([(5.54, 7.5)], tape, words)
    assert a >= 4.75
    # ...and an out-point does not run on into one.
    words = [{"start": 3.6, "end": 4.0, "word": "it."},
             {"start": 4.02, "end": 4.75, "word": "Yeah."}]
    (_a, b), = engines.settle_cut_edges([(2.5, 3.9)], tape, words)
    assert b <= 4.02


def test_two_segments_never_cross(tape):
    out = engines.settle_cut_edges([(2.5, 4.18), (5.54, 7.5)], tape)
    assert out[0][1] <= out[1][0]
    assert all(a < b for a, b in out)


def test_unreadable_audio_returns_the_segments(tmp_path):
    segs = [(1.0, 2.0)]
    assert engines.settle_cut_edges(segs, str(tmp_path / "missing.wav")) == segs
    assert engines.settle_cut_edges([], "x.wav") == []


def test_session_match_settles_its_edges(tape):
    """reconcile_pull_from_session: word times in, settled edges out."""
    def w(word, a, b):
        return {"word": word, "start": a, "end": b}
    transcript = [w("weirded", 2.0, 2.8), w("out", 2.8, 3.3), w("about", 3.3, 3.7),
                  w("it.", 3.7, 4.18),                  # ends late, on the breath
                  w("Yeah.", 4.40, 4.75),
                  w("It's", 5.54, 6.0),                 # starts late, in the attack
                  w("just", 6.0, 6.6), w("a", 6.6, 6.9), w("shame.", 6.9, 8.0)]
    pull = {"order": 1, "token": "GUEST", "in_seconds": 2.0, "out_seconds": 8.0,
            "in_tc": "00:00:02", "out_tc": "00:00:08", "part_index": 0,
            "quote_text": "weirded out about it. It's just a shame."}
    r = engines.reconcile_pull_from_session(
        pull, {"transcript": transcript, "audio_cache": tape, "media": [tape]})
    segs = r["segments"]
    assert len(segs) == 2, segs
    assert 4.0 <= segs[0][1] <= 4.08, segs
    assert 5.50 - engines.EDGE_LEAD_S - 0.011 <= segs[1][0] <= 5.48, segs
    assert r["rec_in_s"] == segs[0][0] and r["rec_out_s"] == segs[-1][1]
