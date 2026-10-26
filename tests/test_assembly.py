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


def test_tidy_drops_slivers_and_keeps_regions_apart():
    from assembly_view import _tidy
    # Timeline order stands (a pickup can come before what it follows);
    # regions that butt stay two regions -- that is what a split is.
    assert _tidy([(5.0, 6.0), (1.0, 2.0), (2.0, 3.0), (7.0, 7.01)]) == \
        ([(5.0, 6.0), (1.0, 2.0), (2.0, 3.0)], [0.0, 0.0])
    # A sliver's silence passes to the region after it.
    assert _tidy([(1.0, 2.0), (2.0, 2.01), (3.0, 4.0)], [0.25, 0.5]) == \
        ([(1.0, 2.0), (3.0, 4.0)], [0.75])


def test_segments_without_takes_a_span_out_of_the_cut():
    from assembly_view import _Clip, _segments_without
    # Two segments: source 10-12 s at 0-2 s, source 20-23 s at 2-5 s.
    entry = {"start": 0, "end": 500, "gap": 0,
             "segments": [(10.0, 12.0), (20.0, 23.0)],
             "pieces": [(0, 200, 1000), (200, 300, 2000)]}
    c = _Clip(entry, {"order": 1}, 100, "int", False, "x.wav")
    # A pause inside the second segment.
    assert _segments_without(c, 3.0, 3.5) == \
        ([(10.0, 12.0), (20.0, 21.0), (21.5, 23.0)], [0.0, 0.0])
    # Across the cut: the tail of one and the head of the next.
    assert _segments_without(c, 1.5, 2.5) == \
        ([(10.0, 11.5), (20.5, 23.0)], [0.0])
    # All of it.
    assert _segments_without(c, 0.0, 5.0) == ([], [])


def test_segments_without_takes_silence_out_too():
    from assembly_view import _Clip, _segments_without
    # A split region moved a second later: source 10-12 s at 0-2 s, then
    # a second of silence, then source 12-15 s at 3-6 s.
    entry = {"start": 0, "end": 600, "gap": 0, "seg_gaps": [1.0],
             "segments": [(10.0, 12.0), (12.0, 15.0)],
             "pieces": [(0, 200, 1000), (300, 300, 1200)]}
    c = _Clip(entry, {"order": 1}, 100, "int", False, "x.wav")
    # Half the silence: the region after it moves up half a second.
    assert _segments_without(c, 2.0, 2.5) == \
        ([(10.0, 12.0), (12.0, 15.0)], [0.5])
    # The first region: the pull now starts where its audio does.
    assert _segments_without(c, 0.0, 2.0) == ([(12.0, 15.0)], [])
    # The second region: the silence in front of it has nothing to sit in.
    assert _segments_without(c, 3.0, 6.0) == ([(10.0, 12.0)], [])
    # Across the silence: both sides trimmed, the silence gone.
    assert _segments_without(c, 1.5, 3.5) == \
        ([(10.0, 11.5), (12.5, 15.0)], [0.0])


def test_silence_at_a_join_moves_what_comes_after():
    res = [_int(1, [(10.0, 11.5), (11.5, 14.0)], seg_gaps=[0.25]),
           _int(2, [(30.0, 31.0)])]
    placed, _, _ = engines.layout_timeline(res, 0.5, SR, ASSETS)
    a, b = placed
    assert a["pieces"] == [(0, 1500, 10000), (1750, 2500, 11500)]
    assert a["seg_gaps"] == [0.25]
    assert (a["end"], b["start"]) == (4250, 4750)
    # One per join or not at all: segments edited under them, they butt.
    res[0]["seg_gaps"] = [0.25, 0.25]
    placed, _, _ = engines.layout_timeline(res, 0.5, SR, ASSETS)
    assert placed[0]["pieces"][1] == (1500, 2500, 11500)
    # An empty segment passes its silence on, so nothing after it moves.
    res[0].update(segments=[(10.0, 11.5), (11.5, 11.5), (11.5, 14.0)],
                  seg_gaps=[0.25, 0.5])
    placed, _, _ = engines.layout_timeline(res, 0.5, SR, ASSETS)
    assert placed[0]["pieces"] == [(0, 1500, 10000), (2250, 2500, 11500)]
    assert placed[0]["seg_gaps"] == [0.75]


def test_vo_silence_at_a_join():
    placed, _, _ = engines.layout_timeline(
        [_vo(1, [(1.0, 2.0), (2.0, 3.0)], seg_gaps=[0.5])], 0.5, SR, {})
    assert placed[0]["pieces"] == [(0, 1000, 1000), (1500, 1000, 2000)]


def test_realign_seg_gaps_after_an_edit_elsewhere():
    old = [(10.0, 11.5), (11.5, 14.0), (20.0, 22.0)]
    gaps = [0.25, 0.5]
    # A trim keeps every join.
    assert engines.realign_seg_gaps(
        old, gaps, [(10.0, 11.4), (11.5, 14.0), (20.0, 22.0)]) == [0.25, 0.5]
    # A cut inside the last segment: the old joins keep theirs.
    assert engines.realign_seg_gaps(
        old, gaps, [(10.0, 11.5), (11.5, 14.0), (20.0, 20.5), (21.0, 22.0)]) \
        == [0.25, 0.5, 0.0]
    # The joins gone, or nothing left to keep.
    assert engines.realign_seg_gaps(old, gaps, [(10.0, 22.0)]) is None
    assert engines.realign_seg_gaps(old, None, old) is None
    assert engines.realign_seg_gaps(old, [0.25], old) is None   # not per join


def _layout_with_markers():
    res = [dict(order=1, token="GUEST", status="ok", part_index=0,
                segments=[(1.0, 4.0), (6.0, 8.0)],
                markers=[{"kind": "music_in", "src": 2.0},
                         {"kind": "sfx", "src": 7.0, "name": "rifle shot"},
                         {"kind": "sfx", "src": 5.0},   # cut out: where audio resumes
                         {"kind": "music_out", "tail": 0.25}]),
           dict(order=2, token="GUEST", status="ok", part_index=1,
                segments=[(0.0, 1.0)],
                markers=[{"kind": "sfx", "src": 0.5}])]
    return res


def test_markers_ride_their_pull():
    placed, _, _ = engines.layout_timeline(
        _layout_with_markers(), 0.5, SR, ASSETS)
    m = engines.place_markers(placed, SR)
    assert [(x["pos"], x["label"]) for x in m] == [
        (1000, "Cue 1 in"), (3000, "SFX 1"), (4000, "rifle shot"),
        (5250, "Cue 1 out"), (6000, "SFX 3")]


def test_aaf_writes_music_and_sfx_marker_tracks(tmp_path):
    import wave
    import pytest
    aaf2 = pytest.importorskip("aaf2")
    wav = str(tmp_path / "guest.wav")
    with wave.open(wav, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
        w.writeframes(b"\x00\x00" * 48000 * 10)
    out = str(tmp_path / "cut.aaf")
    engines.build_aaf(_layout_with_markers(), {"GUEST": [wav]}, {},
                      [{"index": 0, "name": "Cold open"},
                       {"index": 1, "name": "Act 1"}],
                      "cut", 0.5, seq_sr=48000, out_path=out)
    tracks = {}
    with aaf2.open(out, "r") as f:
        comp = next(f.content.toplevel())
        for slot in comp.slots:
            pos, clips = 0, []
            for comp_ in slot.segment.components:
                if isinstance(comp_, aaf2.components.SourceClip):
                    clips.append((pos, comp_.length, comp_.mob.name))
                pos += comp_.length
            tracks[slot.name] = clips
    s = 48000
    assert tracks["PARTS"] == [(0, 2 * s, "Cold open"),
                               (int(5.5 * s), 2 * s, "Act 1")]
    # "Cue 1 in" stops where "Cue 1 out" starts, so the track stays in time.
    assert tracks["MUSIC"] == [(1 * s, 2 * s, "Cue 1 in"),
                               (int(5.25 * s), 2 * s, "Cue 1 out")]
    # A mark closer than two seconds to the next is cut short to fit.
    assert tracks["SFX"] == [(3 * s, 1 * s, "SFX 1"),
                             (4 * s, 2 * s, "rifle shot"),
                             (6 * s, 2 * s, "SFX 3")]
    assert list(tracks)[-3:] == ["PARTS", "MUSIC", "SFX"]


def _guest_wav(tmp_path, secs=40):
    import wave
    wav = str(tmp_path / "guest.wav")
    with wave.open(wav, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
        w.writeframes(b"\x00\x00" * 48000 * secs)
    return wav


def test_aaf_writes_split_regions_as_their_own_clips(tmp_path):
    import pytest
    aaf2 = pytest.importorskip("aaf2")
    wav = _guest_wav(tmp_path)
    out = str(tmp_path / "cut.aaf")
    engines.build_aaf([_int(1, [(1.0, 2.0), (2.0, 4.0)], seg_gaps=[0.5]),
                       _int(2, [(5.0, 6.0)])],
                      {"GUEST": [wav]}, {}, [], "cut", 0.5, seq_sr=48000,
                      out_path=out)
    s = 48000
    with aaf2.open(out, "r") as f:
        comp = next(f.content.toplevel())
        slot = next(sl for sl in comp.slots if sl.name == "GUEST")
        pos, clips = 0, []
        for comp_ in slot.segment.components:
            if isinstance(comp_, aaf2.components.SourceClip):
                clips.append((pos, comp_.length, comp_.start))
            pos += comp_.length
    # The split is two clips of continuous audio, the second half a second
    # later; the next pull follows the moved region.
    assert clips == [(0, 1 * s, 1 * s), (int(1.5 * s), 2 * s, 2 * s),
                     (4 * s, 1 * s, 5 * s)]


def test_xml_places_silence_at_a_join(tmp_path):
    wav = _guest_wav(tmp_path)
    xmeml, _ = engines.build_xml(
        [_int(1, [(1.0, 2.0), (2.0, 4.0)], seg_gaps=[0.5],
              rec_in_s=1.0, rec_out_s=4.0),
         _int(2, [(5.0, 6.0)], rec_in_s=5.0, rec_out_s=6.0)],
        {"GUEST": [wav]}, {}, [], "cut", 0.5, seq_fps=24)
    clips = [(int(ci.find("start").text), int(ci.find("end").text),
              int(ci.find("in").text))
             for tr in xmeml.iter("track") for ci in tr.iter("clipitem")]
    assert clips == [(0, 24, 24), (36, 84, 48), (96, 120, 120)]
