"""Fades in the AAF export (engines.build_aaf, fade_secs).

A fade is an AAF Transition between two components, which overlap by its
length.  Whatever fades a track carries, every clip must sit and sound
exactly where it does in the export without them.
"""
import wave

import pytest

import engines

aaf2 = pytest.importorskip("aaf2")
S = 48000
FADE = 0.010
F = int(FADE * S)


def _wav(tmp_path, seconds=10):
    path = str(tmp_path / "guest.wav")
    with wave.open(path, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(S)
        w.writeframes(b"\x00\x00" * S * seconds)
    return path


def _pull(order, segs, **kw):
    r = {"order": order, "token": "GUEST", "status": "ok", "part_index": 0,
         "segments": [list(s) for s in segs], "gap_after": True}
    r.update(kw)
    return r


def _track(path, name="GUEST"):
    """[(kind, timeline start, length, source start, cutpoint)] for a track,
    with each transition overlapping its neighbours."""
    out, pos = [], 0
    with aaf2.open(path, "r") as f:
        comp = next(f.content.toplevel())
        slot = next(sl for sl in comp.slots if sl.name == name)
        for c in slot.segment.components:
            kind = type(c).__name__
            if kind == "Transition":
                pos -= c.length
                op = c["OperationGroup"].value
                assert op.operation.name == "Mono Audio Dissolve"
                assert op.length == c.length
                out.append((kind, pos, c.length, None, c.cutpoint))
            else:
                out.append((kind, pos, c.length,
                            c.start if kind == "SourceClip" else None, None))
                pos += c.length
    return out, pos


def _sounding(track):
    """Where each clip's own audio is: (timeline, source) of the cut points
    it would have without fades, taking the crossfade handles back off."""
    out = []
    for i, (kind, pos, length, src, _c) in enumerate(track):
        if kind != "SourceClip":
            continue
        lead = trail = 0
        if i and track[i - 1][0] == "Transition" and i > 1 \
                and track[i - 2][0] == "SourceClip":
            lead = track[i - 1][2] // 2
        if i + 2 < len(track) and track[i + 1][0] == "Transition" \
                and track[i + 2][0] == "SourceClip":
            trail = track[i + 1][2] // 2
        out.append((pos + lead, length - lead - trail, src + lead))
    return out


def _build(tmp_path, results, fade, name="cut.aaf", wav=None):
    out = str(tmp_path / name)
    engines.build_aaf(results, {"GUEST": [wav or _wav(tmp_path)]}, {}, [], "cut",
                      0.5, seq_sr=S, out_path=out, fade_secs=fade)
    return out


def _results():
    return [_pull(1, [(1.0, 2.0), (2.5, 4.0)]),          # a cut inside a pull
            _pull(2, [(5.0, 6.0)]),                      # after the gap
            _pull(3, [(6.0, 6.5), (7.0, 7.4)], seg_gaps=[0.3])]


def test_no_fades_by_default(tmp_path):
    track, _ = _track(_build(tmp_path, _results(), 0.0))
    assert not any(k == "Transition" for k, *_ in track)


def test_fades_leave_every_clip_where_it_was(tmp_path):
    wav = _wav(tmp_path)
    plain, end0 = _track(_build(tmp_path, _results(), 0.0, "plain.aaf", wav))
    faded, end1 = _track(_build(tmp_path, _results(), FADE, "faded.aaf", wav))
    assert _sounding(faded) == _sounding(plain)
    assert end1 >= end0                                  # a last fade-out's filler


def test_each_join_gets_its_kind_of_fade(tmp_path):
    track, _ = _track(_build(tmp_path, _results(), FADE))
    kinds = [k for k, *_ in track]
    # in, [clip x clip], out . in, clip, out . in, clip, out . in, clip, out
    assert kinds == ["Filler", "Transition", "SourceClip", "Transition",
                     "SourceClip", "Transition",
                     "Filler", "Transition", "SourceClip", "Transition",
                     "Filler", "Transition", "SourceClip", "Transition",
                     "Filler", "Transition", "SourceClip", "Transition",
                     "Filler"]
    trans = [t for t in track if t[0] == "Transition"]
    # Fade in: the cut at its start.  Crossfade: centred.  Fade out: at its end.
    assert trans[0][2:] == (F, None, 0)
    assert trans[1][2:] == (F // 2 * 2, None, F // 2)
    assert trans[2][2:] == (F, None, F)
    # The crossfade starts half its length before the cut, at 1.0 s.
    assert trans[1][1] == 1 * S - F // 2


def test_crossfade_plays_tape_either_side_of_the_cut(tmp_path):
    track, _ = _track(_build(tmp_path, [_pull(1, [(1.0, 2.0), (2.5, 4.0)])], FADE))
    a, b = [t for t in track if t[0] == "SourceClip"]
    h = F // 2
    assert (a[2], a[3]) == (1 * S + h, 1 * S)            # runs on past its out
    assert (b[2], b[3]) == (int(1.5 * S) + h, int(2.5 * S) - h)   # starts early


def test_a_join_with_no_tape_to_cross_stays_a_cut(tmp_path):
    # The second piece starts at the very top of the file: nothing before it.
    track, _ = _track(_build(tmp_path, [_pull(1, [(1.0, 2.0), (0.0, 1.0)])], FADE))
    kinds = [k for k, *_ in track]
    assert kinds == ["Filler", "Transition", "SourceClip", "SourceClip",
                     "Transition", "Filler"]


def test_a_fade_takes_no_more_than_a_quarter_of_a_short_clip(tmp_path):
    track, _ = _track(_build(tmp_path, [_pull(1, [(1.0, 1.02)])], FADE))
    clip = next(t for t in track if t[0] == "SourceClip")
    assert all(t[2] <= clip[2] // 4 for t in track if t[0] == "Transition")
