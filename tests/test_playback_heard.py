"""The live player's position is the moment being HEARD.

The engine renders each buffer before the device plays it -- about 0.2 s
before on Windows' default driver -- so a playhead read from the render
cursor runs ahead of the sound, by more the faster the playback.  No
audio device is opened here: a stand-in stream supplies the clock.
"""
import numpy as np

import playback

SR, BLOCK, LATENCY = 32000, 2048, 0.192


class _Stream(object):
    def __init__(self):
        self.time, self.latency, self.active = 100.0, LATENCY, True
        self.aborted = self.stopped = 0

    def abort(self):
        self.aborted += 1

    def stop(self):
        self.stopped += 1


class _Info(object):
    def __init__(self, dac, now):
        self.outputBufferDacTime, self.currentTime = dac, now


def _player(rate=1.0, seconds=60):
    p = playback.LivePlayer(samplerate=SR, blocksize=BLOCK)
    rng = np.random.default_rng(0)
    p.load(rng.standard_normal(SR * seconds).astype(np.float32) * 0.1)
    p.set_rate(rate)
    p._stream = _Stream()
    with p._lock:
        p._pos, p._anchor, p._playing, p._finished = 10.0, 10.0, True, False
    return p


def _run(p, blocks, dac_known=True):
    """Feed the device `blocks` buffers in real time: each is rendered
    LATENCY before it sounds."""
    out = np.zeros((BLOCK, 1), dtype=np.float32)
    for _ in range(blocks):
        now = p._stream.time
        p._callback(out, BLOCK, _Info(now + LATENCY if dac_known else 0.0, now),
                    None)
        p._stream.time = now + BLOCK / float(SR)


def test_position_is_what_is_heard_not_what_is_rendered():
    for rate in (1.0, 1.5, 3.0):
        p = _player(rate)
        _run(p, 40)
        played = 40 * BLOCK / float(SR)             # real seconds since play
        heard = 10.0 + (played - LATENCY) * rate
        assert abs(p.position() - heard) < 0.03 * rate, rate
        # The render cursor is the latency further on, in source time.
        assert p._pos - p.position() > LATENCY * rate * 0.8, rate


def test_nothing_heard_yet_reports_the_start():
    p = _player(1.5)
    _run(p, 1)                                      # first buffer still queued
    assert p.position() == 10.0


def test_host_without_dac_time_uses_stream_latency():
    p = _player(1.5)
    _run(p, 40, dac_known=False)
    heard = 10.0 + (40 * BLOCK / float(SR) - LATENCY) * 1.5
    assert abs(p.position() - heard) < 0.05


def test_stop_by_hand_lands_on_the_heard_moment_and_cuts_the_queue():
    p = _player(1.5)
    _run(p, 40)
    heard = p.position()
    p.stop()
    assert p._stream.aborted == 1 and p._stream.stopped == 0
    assert abs(p.position() - heard) < 1e-9
    assert not p.is_playing()


def test_running_out_plays_the_queued_tail():
    p = _player(1.0)
    with p._lock:
        p._end = 10.5
    _run(p, 40)
    assert not p.is_playing()
    p.stop()
    assert p._stream.stopped == 1 and p._stream.aborted == 0
    assert abs(p.position() - 10.5) < 0.05


def test_seek_reports_the_new_place_at_once():
    p = _player(1.5)
    _run(p, 40)
    p.seek(30.0)
    assert p.position() == 30.0
    _run(p, 40)
    heard = 30.0 + (40 * BLOCK / float(SR) - LATENCY) * 1.5
    assert abs(p.position() - heard) < 0.05
