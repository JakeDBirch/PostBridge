"""Assembly view groundwork: the shared timeline layout and the on-demand
audio source the player reads the whole cut from.

layout_timeline is what build_aaf places clips with, so these pin the
placement rules the Assembly view draws and plays: a pull's segments butt
together, its gap follows the whole pull, gap_after_s overrides the
sequence gap, and rows with nothing to place take up no time.
"""
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import engines  # noqa: E402

SR = 1000   # one sample per millisecond keeps the arithmetic readable


def _int(order, segs, **kw):
    return dict(dict(order=order, token="GUEST", status="ok", segments=segs), **kw)


def _vo(order, segs, apath="vo.wav", **kw):
    return dict(order=order, token="VO_PART_01", status="ok", is_vo=True,
                segments=segs, takes_data=[{"apath": apath, "segments": segs}],
                best_take_index=0, **kw)


ASSETS = {"GUEST": ["guest.wav"]}


def test_segments_butt_and_gap_follows_the_pull():
    placed, skipped, _ = engines.layout_timeline(
        [_int(1, [(10.0, 12.0), (20.0, 21.5)]), _int(2, [(30.0, 31.0)])],
        0.5, SR, ASSETS)
    a, b = placed
    assert a["pieces"] == [(0, 2000, 10000), (2000, 1500, 20000)]
    assert (a["end"], a["gap"]) == (3500, 500)
    assert b["start"] == 4000 and b["pieces"] == [(4000, 1000, 30000)]
    assert not skipped


def test_gap_overrides_and_skips():
    placed, skipped, _ = engines.layout_timeline(
        [_int(1, [(0.0, 1.0)], gap_after_s=2.0),
         _int(2, [(0.0, 1.0)], gap_after=False),
         _int(3, [(0.0, 1.0)], status="no_match"),     # skipped, no time
         _int(4, [(5.0, 6.0)])],
        0.5, SR, ASSETS)
    assert [e["res"]["order"] for e in placed] == [1, 2, 4]
    assert [e["start"] for e in placed] == [0, 3000, 4000]
    assert [r["order"] for r in skipped] == [3]


def test_vo_without_audio_still_holds_its_gap():
    placed, _, _ = engines.layout_timeline(
        [_vo(1, [(1.0, 2.0)], apath=None), _vo(2, [(1.0, 2.0)])],
        0.5, SR, {})
    assert placed[0]["pieces"] == [] and placed[0]["gap"] == 500
    assert placed[1]["start"] == 500


def test_interview_with_no_files_is_skipped():
    placed, skipped, _ = engines.layout_timeline(
        [_int(1, [(0.0, 1.0)])], 0.5, SR, {"GUEST": ["_pb_mix_x.wav"]})
    assert placed == [] and len(skipped) == 1


def test_assembly_audio_composes_the_cut():
    from assembly_view import _AssemblyAudio, _Clip, LEAD_S
    sr = 100
    entry = {"start": 0, "end": 300, "gap": 0,                 # at 100 Hz:
             "segments": [(1.0, 2.0), (3.0, 5.0)],
             "pieces": [(0, 100, 100), (100, 200, 300)]}       # 1 s + 2 s
    c = _Clip(entry, {"order": 7}, 100, "int", False, "x.wav")
    audio = _AssemblyAudio(sr)
    audio.set_clips([c], c.end)
    lead = int(LEAD_S * sr)
    assert len(audio) == lead + 4 * sr
    # Nothing loaded yet: silence, not an error.
    assert not audio.read(lead, 300).any()
    # Source buffer from 0.5 s to 6 s, each sample holding its own index.
    pcm = np.arange(550, dtype=np.int16)
    audio.put(7, "x.wav", 0.5, pcm)
    out = audio.read(lead, 300) * 32768.0
    # Piece 1 plays source 1.0-2.0 s, piece 2 plays source 3.0-5.0 s.
    assert np.allclose(out[:100], np.arange(50, 150))
    assert np.allclose(out[100:300], np.arange(250, 450))
    # The lead-in before 0:00 is silent.
    assert not audio.read(0, lead).any()


def test_tidy_joins_butted_segments_and_drops_slivers():
    from assembly_view import _tidy
    assert _tidy([(5.0, 6.0), (1.0, 2.0), (2.0, 3.0), (7.0, 7.01)]) == \
        [(1.0, 3.0), (5.0, 6.0)]


def test_segments_without_takes_a_span_out_of_the_cut():
    from assembly_view import _Clip, _segments_without
    # Two segments: source 10-12 s at 0-2 s, source 20-23 s at 2-5 s.
    entry = {"start": 0, "end": 500, "gap": 0,
             "segments": [(10.0, 12.0), (20.0, 23.0)],
             "pieces": [(0, 200, 1000), (200, 300, 2000)]}
    c = _Clip(entry, {"order": 1}, 100, "int", False, "x.wav")
    # A pause inside the second segment.
    assert _segments_without(c, 3.0, 3.5) == \
        [(10.0, 12.0), (20.0, 21.0), (21.5, 23.0)]
    # Across the cut: the tail of one and the head of the next.
    assert _segments_without(c, 1.5, 2.5) == [(10.0, 11.5), (20.5, 23.0)]
    # All of it.
    assert _segments_without(c, 0.0, 5.0) == []
