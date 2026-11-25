"""A recording shared between tokens.

Riverside lost one participant's recording part way through a session and
gave back two consecutive sets of files for that side, while the host's side
came as one unbroken file.  Each part is its own token, and the host's file
belongs to both: under the second token it joins from where that part began.
Step 2 allowed a file one token only, so the host's side could go to one part
and was missing from the other.
"""
import math
import os
import struct
import tkinter as tk
import wave

import pytest

import engines
import episode as EP
import gui_components
from gui_components import MediaPool

SR = 1000


# ── Where a part starts ───────────────────────────────────────────────────

def test_the_last_part_ends_with_the_shared_file():
    # 60 minutes of host; the guest's recording broke at 40:00 and came back
    # 12.5 seconds later.
    starts = engines.shared_starts(3600.0, [
        ("B", 3600.0 - 2412.5, "riverside_guest_raw-audio_show_0457.wav"),
        ("A", 2400.0, "riverside_guest_raw-audio_show_0456.wav")])
    assert starts == {"A": 0.0, "B": 2412.5}


def test_parts_in_the_middle_follow_the_one_before():
    starts = engines.shared_starts(100.0, [
        ("c", 20.0, "take 10.wav"), ("a", 30.0, "take 2.wav"), ("b", 40.0, "take 3.wav")])
    assert starts == {"a": 0.0, "b": 30.0, "c": 80.0}       # "take 10" is last


def test_parts_longer_than_the_file_never_overlap():
    assert engines.shared_starts(50.0, [("a", 30.0, "1.wav"), ("b", 30.0, "2.wav")]) \
        == {"a": 0.0, "b": 30.0}


def test_a_file_one_token_holds_starts_at_zero():
    assert engines.shared_starts(100.0, [("a", 40.0, "1.wav")]) == {"a": 0.0}
    assert engines.shared_starts(100.0, [("a", None, ""), ("b", 40.0, "2.wav")]) \
        == {"a": 0.0, "b": 0.0}


def test_starts_read_and_write_as_timecode():
    assert engines.format_start(2412.5) == "0:40:12.500"
    assert engines.parse_start("0:40:12.500") == 2412.5
    assert engines.parse_start("40:12.5") == 2412.5
    assert engines.parse_start("90") == 90.0
    assert engines.parse_start("") == 0.0
    assert engines.parse_start("soon") is None


# ── The mix a token is transcribed from ───────────────────────────────────

def _tone_wav(path, secs, bursts, sr=16000):
    """Silence with half-second 440 Hz bursts starting at each of *bursts*."""
    n = int(secs * sr)
    data = bytearray(2 * n)
    for t0 in bursts:
        for i in range(int(t0 * sr), min(n, int((t0 + 0.5) * sr))):
            struct.pack_into("<h", data, 2 * i,
                             int(12000 * math.sin(2 * math.pi * 440 * i / sr)))
    with wave.open(str(path), "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(bytes(data))
    return str(path)


def _loud_seconds(path):
    """The whole seconds of a WAV that hold sound."""
    np = pytest.importorskip("numpy")
    with wave.open(path) as w:
        sr = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(float)
    return [s for s in range(len(pcm) // sr)
            if np.sqrt(np.mean(pcm[s * sr:(s + 1) * sr] ** 2)) > 300], len(pcm) / sr


def test_a_shared_recording_joins_the_mix_from_its_start(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    if not engines.check_ffmpeg()[0]:
        pytest.skip("ffmpeg unavailable")
    monkeypatch.setattr(engines, "_cache_dir", str(tmp_path / ".pb_cache"))
    host  = _tone_wav(tmp_path / "jordan.wav", 30, [3.0, 22.0])     # one question in each part
    guest = _tone_wav(tmp_path / "guest_2.wav", 10, [5.0])          # second part: from 20 s
    spans = {engines.span_key(host): (20.0, 10.0)}

    whole  = engines._stable_mix_cache_path([guest, host])
    placed = engines._stable_mix_cache_path([guest, host], spans)
    assert whole != placed                       # a different mix, a different name
    assert engines._stable_mix_cache_path(
        [guest, host], {engines.span_key(host): (0.0, None)}) == whole

    mix = engines.mix_for_transcript([guest, host], spans)
    assert mix == placed
    loud, secs = _loud_seconds(mix)
    assert abs(secs - 10.0) < 0.1                # the part's length, not the host's
    assert loud == [2, 5]                        # the host's 22 s question at 2 s


# ── What is exported ──────────────────────────────────────────────────────

def _pull(order, segments, **kw):
    return dict(order=order, token="GUEST2", status="ok", part_index=0,
                segments=segments, **kw)


def test_a_shared_recording_is_cut_further_into_its_file():
    files = {"GUEST2": ["guest_2.wav", "jordan.wav"]}
    starts = {engines.span_key("jordan.wav"): 20.0}
    placed, _, _ = engines.layout_timeline(
        [_pull(1, [(2.0, 4.0)], _starts=starts), _pull(2, [(5.0, 6.0)])], 0.5, SR, files)
    assert placed[0]["paths"] == ["guest_2.wav", "jordan.wav"]
    assert placed[0]["starts"] == [0, 20 * SR]
    assert placed[0]["pieces"] == [(0, 2 * SR, 2 * SR)]       # the token's own clock
    assert placed[1]["starts"] == [0, 0]                      # no start, no shift


def test_the_aaf_takes_the_host_from_where_the_part_began(tmp_path):
    aaf2 = pytest.importorskip("aaf2")
    sr = 48000
    guest, host = str(tmp_path / "guest_2.wav"), str(tmp_path / "jordan.wav")
    for p, secs in ((guest, 10), (host, 30)):
        with wave.open(p, "w") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
            w.writeframes(b"\x00\x00" * sr * secs)
    out = str(tmp_path / "cut.aaf")
    engines.build_aaf(
        [_pull(1, [(2.0, 4.0)], _starts={engines.span_key(host): 20.0})],
        {"GUEST2": [guest, host]}, {}, [], "cut", 0.5, seq_sr=sr, out_path=out)
    clips = {}
    with aaf2.open(out, "r") as f:
        comp = next(f.content.toplevel())
        for slot in comp.slots:
            for c in slot.segment.components:
                if isinstance(c, aaf2.components.SourceClip) and c.mob is not None:
                    clips[slot.name] = (c.start, c.length)
    assert clips["GUEST2"] == (2 * sr, 2 * sr)
    assert clips[engines.HOST_NAME.upper()] == (22 * sr, 2 * sr)


def test_a_mix_made_with_a_start_is_not_a_missing_side(tmp_path, monkeypatch):
    monkeypatch.setattr(engines, "_cache_dir", str(tmp_path / ".pb_cache"))
    host, guest = tmp_path / "jordan.wav", tmp_path / "guest_2.wav"
    host.write_bytes(b"h" * 100)
    guest.write_bytes(b"g" * 200)
    files = {"GUEST2": [str(guest), str(host)]}
    spans = {"GUEST2": {engines.span_key(str(host)): (20.0, 10.0)}}
    rows = [_pull(1, [(0.0, 1.0)], source_audio=engines._stable_mix_cache_path(
        files["GUEST2"], spans["GUEST2"]))]
    assert engines.tokens_short_of_their_match(rows, files, spans) == {}
    assert engines.tokens_short_of_their_match(rows, files) == files


# ── The episode ───────────────────────────────────────────────────────────

def _episode(tmp_path):
    ep = EP.new_episode(str(tmp_path))
    for r, kind, secs in [("a/jordan.wav", "audio", 3600.0), ("v/jordan.mp4", "video", 3600.2),
                          ("a/guest_0456.wav", "audio", 2400.0), ("v/guest_0456.mp4", "video", 2400.1),
                          ("a/guest_0457.wav", "audio", 1187.5), ("v/guest_0457.mp4", "video", 1187.6)]:
        ep["media"][r] = {"kind": kind, "size": 1, "duration_s": secs,
                          "status": "source", "reason": "", "dup_of": None}
    ep["sources"] = {
        "Guest 1": {"token": "GUEST1", "session": None, "audio": ["a/guest_0456.wav", "a/jordan.wav"],
                    "video": ["v/guest_0456.mp4", "v/jordan.mp4"]},
        "Guest 2": {"token": "GUEST2", "session": None, "audio": ["a/guest_0457.wav"],
                    "video": ["v/guest_0457.mp4"]}}
    return ep


def test_sharing_leaves_the_recording_where_it_was(tmp_path):
    ep = _episode(tmp_path)
    assert EP.shared_files(ep) == {} and EP.source_spans(ep, "Guest 2") == {}
    EP.share_file(ep, "Guest 2", "audio", "a/jordan.wav")
    EP.share_file(ep, "Guest 2", "video", "v/jordan.mp4")
    assert ep["sources"]["Guest 1"]["audio"] == ["a/guest_0456.wav", "a/jordan.wav"]
    assert ep["sources"]["Guest 2"]["audio"] == ["a/guest_0457.wav", "a/jordan.wav"]
    assert EP.shared_files(ep) == {"a/jordan.wav": ["Guest 1", "Guest 2"],
                                   "v/jordan.mp4": ["Guest 1", "Guest 2"]}
    # The first part starts with the file; the second ends with it.
    assert EP.source_spans(ep, "Guest 1") == {"a/jordan.wav": (0.0, 2400.0),
                                              "v/jordan.mp4": (0.0, 2400.0)}
    assert EP.source_spans(ep, "Guest 2") == {"a/jordan.wav": (2412.5, 1187.5),
                                              "v/jordan.mp4": (2412.7, 1187.5)}


def test_a_rescan_keeps_a_shared_recording_in_both_sources(tmp_path):
    ep = _episode(tmp_path)
    EP.share_file(ep, "Guest 2", "audio", "a/jordan.wav")
    ep["sources"]["Guest 2"]["user_made"] = True
    files = {r: dict(rec) for r, rec in ep["media"].items()}
    EP.apply_scan(ep, {"files": files, "sources": EP.propose_sources(files),
                       "mix": None, "sessions": {}})
    assert "a/jordan.wav" in ep["sources"]["Guest 1"]["audio"]
    assert "a/jordan.wav" in ep["sources"]["Guest 2"]["audio"]


# ── Step 2 ────────────────────────────────────────────────────────────────

LENGTHS = {"X:/a/jordan.wav": 3600.0, "X:/a/guest_0456.wav": 2400.0,
           "X:/a/guest_0457.wav": 1187.5}


@pytest.fixture
def pool(monkeypatch):
    monkeypatch.setattr(gui_components, "HAS_DND", False)
    monkeypatch.setattr(gui_components, "_get_dur_key", lambda p: None)
    monkeypatch.setattr(engines, "get_media_duration", lambda p: LENGTHS.get(p, 0.0))
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip("Tk unavailable: {}".format(exc))
    root.withdraw()
    root.errors = []
    root.report_callback_exception = lambda *exc: root.errors.append(exc)
    p = MediaPool(root, ["GUEST1", "GUEST2"], [])
    p.pack()
    for path in LENGTHS:
        p._add(path)
    for r in p._rows:
        r["var"].set({"X:/a/guest_0456.wav": "GUEST1", "X:/a/guest_0457.wav": "GUEST2",
                      "X:/a/jordan.wav": "GUEST1"}[r["path"]])
    yield p
    assert not root.errors
    root.destroy()


def _row(pool, path, twin=False):
    return next(r for r in pool._rows if r["path"] == path and bool(r.get("twin")) == twin)


def test_a_file_takes_a_second_token(pool):
    host = "X:/a/jordan.wav"
    assert pool.get_spans() == {} and pool.get_shared() == []
    pool._add_share(_row(pool, host))
    _row(pool, host, twin=True)["var"].set("GUEST2")
    pool._refresh_starts()
    assert {t: sorted(p) for t, p in pool.get_assignments().items()} == {
        "GUEST1": ["X:/a/guest_0456.wav", host], "GUEST2": ["X:/a/guest_0457.wav", host]}
    key = engines.span_key(host)
    assert pool.get_spans() == {"GUEST1": {key: (0.0, 2400.0)},
                                "GUEST2": {key: (2412.5, 1187.5)}}
    assert _row(pool, host, twin=True)["start_var"].get() == "0:40:12.500"
    # What a save keeps: the file's own row as ever, the second row beside it.
    assert dict(pool.assignment_pairs())[host] == "GUEST1"
    assert pool.get_shared() == [{"path": host, "token": "GUEST1", "twin": False},
                                 {"path": host, "token": "GUEST2", "twin": True}]


def test_a_typed_start_wins_and_clearing_it_goes_back(pool):
    host = "X:/a/jordan.wav"
    pool._add_share(_row(pool, host))
    twin = _row(pool, host, twin=True)
    twin["var"].set("GUEST2")
    twin["start_var"].set("40:15")
    pool._commit_start(twin)
    assert twin["start_var"].get() == "0:40:15.000"
    assert pool.get_spans()["GUEST2"][engines.span_key(host)] == (2415.0, 1187.5)
    assert pool.get_shared()[1]["start"] == 2415.0
    twin["start_var"].set("")
    pool._commit_start(twin)
    assert pool.get_spans()["GUEST2"][engines.span_key(host)] == (2412.5, 1187.5)


def test_a_saved_share_comes_back(pool):
    host = "X:/a/jordan.wav"
    pool.apply_shared([{"path": host, "token": "GUEST1", "twin": False},
                       {"path": host, "token": "GUEST2", "twin": True, "start": 2415.0}])
    assert len([r for r in pool._rows if r["path"] == host]) == 2
    assert pool.get_spans()["GUEST2"][engines.span_key(host)] == (2415.0, 1187.5)
    # Taking the file out takes its second row with it.
    pool._remove(_row(pool, host))
    assert sorted(r["path"] for r in pool._rows) == ["X:/a/guest_0456.wav", "X:/a/guest_0457.wav"]
    assert pool.get_spans() == {}


# ── From the episode to Step 2 and back ───────────────────────────────────

from test_episode import project  # noqa: E402,F401  (module-scoped fixture)
from test_episode_screens import app, _open_fresh, _wait, _pump  # noqa: E402,F401


def test_a_shared_recording_opens_step_2_under_both_tokens(app, monkeypatch, project):
    _open_fresh(app, monkeypatch, project)
    ep = app._ep
    host = next(a for a in ep["sources"]["Butler Interview"]["audio"] if "JORDAN" in a)
    ep["sources"]["Ep 7 pt 1"]["token"] = "PARTONE"
    app._ep_share("Ep 7 pt 1", "audio", host)           # redraws the review
    _pump(app, 10)
    assert host in ep["sources"]["Butler Interview"]["audio"]
    assert host in ep["sources"]["Ep 7 pt 1"]["audio"]
    assert not app._errors, "\n".join(app._errors)

    script = os.path.join(project, "05_SCRIPT", "Ep 7 shared.txt")
    os.makedirs(os.path.dirname(script), exist_ok=True)
    with open(script, "w", encoding="utf-8") as f:
        f.write("[PART Open]\n[BUTLER 00:00:05-00:00:20]\nWe went out at four.\n"
                "[PARTONE 00:00:05-00:00:20]\nA quiet morning.\n")
    ep["links"]["script"] = EP.rel(ep, script)
    app._ep_save()
    app._ep_launch_s2s()
    assert _wait(app, lambda: getattr(app, "_pool", None) is not None
                 and any(r.get("twin") for r in app._pool._rows), secs=30), app._errors
    _pump(app, 10)
    mine = sorted((bool(r.get("twin")), r["var"].get()) for r in app._pool._rows
                  if os.path.basename(r["path"]) == os.path.basename(host))
    assert mine == [(False, "BUTLER"), (True, "PARTONE")]
    assert set(app._pool.get_spans()) == {"BUTLER", "PARTONE"}
    # Saving Step 2 back into the episode leaves it in both sources.
    app._ep_absorb_s2s()
    assert host in ep["sources"]["Butler Interview"]["audio"]
    assert host in ep["sources"]["Ep 7 pt 1"]["audio"]
    assert not app._errors, "\n".join(app._errors)


# ── The review: tokens from the script, tokens that fold away ────────────

def test_sources_take_the_script_tokens_their_names_carry(tmp_path):
    ep = EP.new_episode(str(tmp_path))
    ep["sources"] = {
        "Dave Kaufmann": {"token": None, "audio": ["a/Dave Kaufmann.wav"], "video": []},
        "riverside_lynn_richardson": {"token": None, "video": [],
                                      "audio": ["a/riverside_lynn_richardson_raw-audio_0455.wav"]},
        "Guest part 1": {"token": None, "audio": ["a/guest_0456.wav"], "video": []},
        "Guest part 2": {"token": None, "audio": ["a/guest_0457.wav"], "video": []},
        "Narration": {"token": None, "vo_part": "Open", "audio": ["a/vo.wav"], "video": []},
        "Peter": {"token": "PETER", "audio": ["a/peter.wav"], "video": []}}
    tokens = ["PETER", "KAUFMANN", "RICHARDSON", "GUEST"]
    assert EP.assign_script_tokens(ep, tokens) == ["Dave Kaufmann", "riverside_lynn_richardson"]
    s = ep["sources"]
    assert s["Dave Kaufmann"]["token"] == "KAUFMANN"
    assert s["riverside_lynn_richardson"]["token"] == "RICHARDSON"
    # Two sources look like GUEST: neither is guessed, the dropdown asks.
    assert s["Guest part 1"]["token"] is None and s["Guest part 2"]["token"] is None
    assert s["Narration"]["token"] is None and s["Peter"]["token"] == "PETER"
    # A token cleared by hand stays cleared until the script's tokens change.
    s["Dave Kaufmann"]["token"] = None
    assert EP.assign_script_tokens(ep, tokens) == []
    assert EP.assign_script_tokens(ep, tokens + ["NEWCOMER"]) == ["Dave Kaufmann"]


def test_the_review_offers_the_script_tokens_and_folds_them_away(app, monkeypatch, project):
    import tkinter as tk
    _open_fresh(app, monkeypatch, project)
    ep = app._ep
    script = os.path.join(project, "05_SCRIPT", "Ep 7 tokens.txt")
    os.makedirs(os.path.dirname(script), exist_ok=True)
    with open(script, "w", encoding="utf-8") as f:
        f.write("[PART Open]\n[VO PART_0_NARRATOR]\nA quiet morning.\n"
                "[BUTLER 00:00:05-00:00:20]\nWe went out at four.\n"
                "[CARY 00:00:05-00:00:20]\nIt was cold.\n")
    ep["links"]["script"] = EP.rel(ep, script)
    app._ep_view()
    _pump(app, 10)
    assert app._ep_script_names() == (["BUTLER", "CARY"], ["Open"])
    assert not app._errors, "\n".join(app._errors)

    def _texts(w):
        out = [w.cget("text")] if isinstance(w, tk.Label) else []
        for c in w.winfo_children():
            out += _texts(c)
        return out
    shown = _texts(app.body)
    assert "COLLAPSE ALL" in " ".join(shown) and "tokens from Ep 7 tokens.txt" in shown
    # Folding one away keeps it folded through a redraw.
    assert app._ep_open["BUTLER"] is True
    app._ep_open["BUTLER"] = False
    app._ep_view()
    _pump(app, 10)
    assert app._ep_open["BUTLER"] is False
    assert not app._errors, "\n".join(app._errors)


# ── Review tokens reach a Script -> Session that was already saved ────────

def _carry(saved, rows):
    import episode_workflow
    changed = episode_workflow.EpisodeWorkflowMixin._ep_s2s_carry_tokens(saved, rows)
    return sorted(os.path.basename(p) for p in changed)


def test_a_token_chosen_in_the_review_reaches_a_saved_step_2():
    un = "\u2014 unassigned \u2014"
    saved = {"assignments": {"X:/a/guest.wav": un, "X:/a/host.wav": "OLD",
                             "X:/a/vo.wav": "VO: Open", "X:/a/loose.wav": "LOOSE"}}
    rows = [("GUEST", True, ["X:/a/guest.wav", "X:/a/host.wav"]),
            (un, False, ["X:/a/vo.wav"])]            # no token chosen: left alone
    assert _carry(saved, rows) == ["guest.wav", "host.wav"]
    assert saved["assignments"] == {"X:/a/guest.wav": "GUEST", "X:/a/host.wav": "GUEST",
                                    "X:/a/vo.wav": "VO: Open", "X:/a/loose.wav": "LOOSE"}
    assert _carry(saved, rows) == []                 # nothing left to change


def test_a_shared_recording_reaches_a_saved_step_2_under_both_tokens():
    host = "X:/a/jordan.wav"
    saved = {"assignments": {host: "GUEST1", "X:/a/g1.wav": "GUEST1", "X:/a/g2.wav": "GUEST2"}}
    rows = [("GUEST1", True, ["X:/a/g1.wav", host]), ("GUEST2", True, ["X:/a/g2.wav", host])]
    assert _carry(saved, rows) == ["jordan.wav"]
    assert saved["assignments"][host] == "GUEST1"
    assert saved["shared"] == [{"path": host, "token": "GUEST1", "twin": False},
                               {"path": host, "token": "GUEST2", "twin": True}]
    # Already under both, with a start typed for one: untouched.
    saved["shared"][1]["start"] = 2415.0
    assert _carry(saved, rows) == [] and saved["shared"][1]["start"] == 2415.0
    # One part renamed in the review: the other keeps its typed start.
    rows[0] = ("FIRST", True, ["X:/a/g1.wav", host])
    assert _carry(saved, rows) == ["g1.wav", "jordan.wav"]
    assert saved["shared"] == [{"path": host, "token": "FIRST", "twin": False},
                               {"path": host, "token": "GUEST2", "twin": True, "start": 2415.0}]


def test_the_review_token_shows_in_step_2_after_a_save(app, monkeypatch, project):
    _open_fresh(app, monkeypatch, project)
    ep = app._ep
    script = os.path.join(project, "05_SCRIPT", "Ep 7 carry.txt")
    os.makedirs(os.path.dirname(script), exist_ok=True)
    with open(script, "w", encoding="utf-8") as f:
        f.write("[PART Open]\n[VO PART_0_NARRATOR]\nA quiet morning.\n"
                "[BUTLER 00:00:05-00:00:20]\nWe went out at four.\n"
                "[CARY 00:00:05-00:00:20]\nIt was cold.\n")
    ep["links"]["script"] = EP.rel(ep, script)
    app._ep_save()

    def _rows():
        return {os.path.basename(r["path"]): r["var"].get() for r in app._pool._rows}
    app._ep_launch_s2s()
    assert _wait(app, lambda: getattr(app, "_pool", None) is not None
                 and len(app._pool._rows) >= 9, secs=30), app._errors
    _pump(app, 10)
    assert _rows()["Ep 7 pt 2.wav"] == "VO: Open"
    EP.set_stage(ep, EP.STAGE_S2S, app._build_setup_data())     # Step 2, saved

    ep["sources"]["Ep 7 pt 2"]["token"] = "CARY"                # then the review
    app._pool = None
    app._ep_launch_s2s()
    assert _wait(app, lambda: getattr(app, "_pool", None) is not None
                 and len(app._pool._rows) >= 9, secs=30), app._errors
    _pump(app, 10)
    rows = _rows()
    assert rows["Ep 7 pt 2.wav"] == "CARY"
    assert rows["Ep 7 pt 1.wav"] == "VO: Open"                  # as it was saved
    assert rows["Butler Interview_CARY.wav"] == "BUTLER"
    assert not app._errors, "\n".join(app._errors)
