"""The episode is the project's one file.

Stepping through the workflows from an episode must ask for no save
location and leave no JSON of its own behind: Script -> Session, AAF -> XML
and Pull Quotes all amend <Title>.pb_episode.json, and come back from it.
An episode from before this reads its old stage files in and leaves them.

The model tests need nothing; the screen tests need ffmpeg, tkinter and a
display, and borrow test_episode_screens' fixtures.
"""
import json
import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import episode as EP  # noqa: E402
from test_episode import project  # noqa: E402,F401  (module-scoped fixture)
from test_episode_screens import app  # noqa: E402,F401


def _bare(tmp_path, title="Ep"):
    root = tmp_path / title
    root.mkdir()
    return EP.new_episode(str(root), title), str(root)


# ── The file ──────────────────────────────────────────────────────────────

def test_stage_paths_are_relative_on_disk_and_follow_a_move(tmp_path):
    ep, root = _bare(tmp_path)
    wav = os.path.join(root, "03_AUDIO", "a.wav")
    # Tk's file dialogs give forward slashes, os.path back slashes.
    fwd = root.replace("\\", "/") + "/03_AUDIO/b.wav"
    outside = str(tmp_path / "elsewhere.wav")
    EP.set_stage(ep, EP.STAGE_S2S, {
        "script": os.path.join(root, "script.txt"),
        "assignments": {wav: "BUTLER", fwd: "CARY", outside: "X"},
        "results": [{"source_audio": wav, "quote_text": "We went out at four.",
                     "words": [{"word": root + "/not-a-path", "start": 0.0}]}]})
    path = EP.save(ep)

    raw = json.load(open(path, encoding="utf-8"))
    st = raw["stages"][EP.STAGE_S2S]
    assert st["script"] == "$EP/script.txt"
    assert set(st["assignments"]) == {"$EP/03_AUDIO/a.wav", "$EP/03_AUDIO/b.wav", outside}
    assert st["results"][0]["source_audio"] == "$EP/03_AUDIO/a.wav"
    # Word lists are left alone.
    assert st["results"][0]["words"][0]["word"].endswith("/not-a-path")

    moved = str(tmp_path / "moved")
    shutil.move(root, moved)
    ep2 = EP.load(os.path.join(moved, os.path.basename(path)))
    st2 = EP.get_stage(ep2, EP.STAGE_S2S)
    assert st2["script"] == os.path.join(moved, "script.txt")
    assert os.path.join(moved, "03_AUDIO", "a.wav") in st2["assignments"]
    assert st2["assignments"][outside] == "X"
    assert not ep2.get("_absorbed")


def test_saved_state_is_a_copy_of_what_was_saved(tmp_path):
    ep, _root = _bare(tmp_path)
    live = {"results": [{"segments": [[1.0, 2.0]]}]}
    EP.set_stage(ep, EP.STAGE_S2S, live)
    live["results"][0]["segments"] = [[9.0, 9.5]]    # unsaved edit on screen
    assert EP.get_stage(ep, EP.STAGE_S2S)["results"][0]["segments"] == [[1.0, 2.0]]
    got = EP.get_stage(ep, EP.STAGE_S2S)
    got["results"].clear()
    assert EP.get_stage(ep, EP.STAGE_S2S)["results"]


def test_numpy_values_save(tmp_path):
    np = pytest.importorskip("numpy")
    ep, _root = _bare(tmp_path)
    EP.set_stage(ep, EP.STAGE_S2S, {"pad": np.float32(0.5), "a": np.arange(2)})
    ep["stages"][EP.STAGE_AAF] = {"n": np.int64(3)}
    raw = json.load(open(EP.save(ep), encoding="utf-8"))
    assert raw["stages"][EP.STAGE_S2S] == {"pad": 0.5, "a": [0, 1]}
    assert raw["stages"][EP.STAGE_AAF] == {"n": 3}


def test_a_failed_save_leaves_the_file_as_it_was(tmp_path):
    ep, root = _bare(tmp_path)
    path = EP.save(ep)
    before = open(path, encoding="utf-8").read()
    ep["stages"][EP.STAGE_S2S] = {"bad": object()}
    with pytest.raises(TypeError):
        EP.save(ep)
    assert open(path, encoding="utf-8").read() == before
    assert os.listdir(root) == [os.path.basename(path)]


def test_older_episode_reads_its_stage_files_in_and_leaves_them(tmp_path):
    root = tmp_path / "Old"
    (root / "docs").mkdir(parents=True)
    script = root / "docs" / "script.txt"
    script.write_text("x")
    aaf = root / "cut.aaf"
    aaf.write_text("x")
    sess = root / "docs" / "session.json"
    sess.write_text(json.dumps({"workflow": "script_session", "script": str(script),
                                "results": [{"order": 1}]}))
    setup = root / "docs" / "cut_setup.json"
    setup.write_text(json.dumps({"version": 2, "aaf": str(aaf), "sync": {"a": 1}}))
    pq = root / "docs" / "BUTLER.pb_session.json"
    pq.write_text(json.dumps({"workflow": "interview_session", "token": "BUTLER",
                              "media": [], "transcript": [{"word": " hi"}]}))
    path = root / ("Old" + EP.EPISODE_EXT)
    path.write_text(json.dumps({
        "version": 2, "workflow": "episode", "title": "Old", "media": {}, "excluded": [],
        "sources": {"B": {"token": "BUTLER", "audio": [], "video": [],
                          "session": "docs/BUTLER.pb_session.json"}},
        "links": {"script": "docs/script.txt", "s2s_session": "docs/session.json",
                  "aaf": "cut.aaf", "aaf_setup": "docs/cut_setup.json"},
        "sessions": ["docs/BUTLER.pb_session.json"]}))
    sizes = {p: p.read_bytes() for p in (sess, setup, pq)}

    ep = EP.load(str(path))
    assert ep["_absorbed"]
    assert EP.get_stage(ep, EP.STAGE_S2S)["results"] == [{"order": 1}]
    assert EP.get_stage(ep, EP.STAGE_AAF)["sync"] == {"a": 1}
    held = EP.pq_session_list(ep)
    assert [s["token"] for s in held] == ["BUTLER"] and held[0]["id"]
    assert ep["sources"]["B"]["session"] == held[0]["id"]
    assert "s2s_session" not in ep["links"] and "aaf_setup" not in ep["links"]

    EP.save(ep)
    raw = json.load(open(path, encoding="utf-8"))
    assert raw["version"] == EP.EPISODE_VERSION and "sessions" not in raw
    assert not EP.load(str(path)).get("_absorbed")
    # The old files are left exactly as they were.
    assert {p: p.read_bytes() for p in sizes} == sizes


def test_a_stage_file_written_before_the_project_moved_follows_it(tmp_path):
    """Bill Richardson's session still named F:/Blood Trails/... after the
    project moved into Dropbox: its script and audio must be found at the new
    root, and the good link to the script must not become the dead one."""
    root = tmp_path / "Dropbox" / "Show"
    (root / "docs").mkdir(parents=True)
    (root / "audio").mkdir()
    script = root / "docs" / "script.txt"
    script.write_text("x")
    wav = root / "audio" / "a.wav"
    wav.write_text("x")
    old = "F:/Blood Trails/Show"
    (root / "docs" / "session.json").write_text(json.dumps({
        "script": old + "/docs/script.txt",
        "assignments": {old + "/audio/a.wav": "BUTLER", "E:/library/sting.wav": "X"},
        "results": [{"source_audio": r"F:\Blood Trails\Show\audio\a.wav",
                     "quote_text": "F:/Blood Trails/Show/ is quoted here"}]}))
    path = root / ("Show" + EP.EPISODE_EXT)
    path.write_text(json.dumps({
        "workflow": "episode", "title": "Show",
        "links": {"script": "docs/script.txt", "s2s_session": "docs/session.json"}}))
    ep = EP.load(str(path))
    st = EP.get_stage(ep, EP.STAGE_S2S)
    assert st["script"] == str(script)
    assert str(wav) in st["assignments"] and "E:/library/sting.wav" in st["assignments"]
    assert st["results"][0]["source_audio"] == str(wav)
    assert ep["links"]["script"] == "docs/script.txt"
    EP.save(ep)
    assert EP.get_stage(EP.load(str(path)), EP.STAGE_S2S)["script"] == str(script)


def test_a_stage_file_naming_a_file_found_nowhere_leaves_the_link_alone(tmp_path):
    root = tmp_path / "Show"
    root.mkdir()
    (root / "session.json").write_text(json.dumps({"script": "F:/gone/script.txt"}))
    path = root / ("Show" + EP.EPISODE_EXT)
    path.write_text(json.dumps({
        "workflow": "episode", "title": "Show",
        "links": {"script": "script.txt", "s2s_session": "session.json"}}))
    ep = EP.load(str(path))
    assert ep["links"]["script"] == "script.txt"
    assert EP.get_stage(ep, EP.STAGE_S2S)["script"] == "F:/gone/script.txt"


def test_an_unreadable_stage_file_keeps_its_link(tmp_path):
    root = tmp_path / "Old"
    root.mkdir()
    (root / "session.json").write_text("{not json")
    path = root / ("Old" + EP.EPISODE_EXT)
    path.write_text(json.dumps({"workflow": "episode", "title": "Old",
                                "links": {"s2s_session": "session.json"}}))
    ep = EP.load(str(path))
    assert ep["links"]["s2s_session"] == "session.json"
    assert EP.STAGE_S2S not in ep["stages"]


def test_discovery_finds_sessions_in_the_episode_and_they_win(tmp_path):
    import parsers
    import transcript_check
    ep, root = _bare(tmp_path)
    script = os.path.join(root, "script.txt")
    open(script, "w").close()
    # A .pb_session.json left over beside the one the episode holds.
    with open(os.path.join(root, "BUTLER.pb_session.json"), "w") as f:
        json.dump({"workflow": "interview_session", "token": "BUTLER",
                   "media": [], "transcript": [{"word": " stale"}]}, f)
    EP.pq_sessions(ep)["ses_1"] = {
        "workflow": "interview_session", "id": "ses_1", "token": "BUTLER", "media": [],
        "transcript": [{"word": " live", "start": 0.0, "end": 0.2}]}
    path = EP.save(ep)
    found = parsers.discover_pq_sessions(script)
    assert found["BUTLER"] == EP.session_ref(path, "BUTLER")
    assert not found.get("__warnings__")
    assert EP.read_pq_session(found["BUTLER"])["transcript"][0]["word"] == " live"
    ok, n, _detail = transcript_check.pq_session_status(found["BUTLER"], quick=True)
    assert n == 1 and ok


def test_find_episode_above(tmp_path):
    ep, root = _bare(tmp_path)
    path = EP.save(ep)
    deep = os.path.join(root, "05_DOCS", "01_SCRIPT")
    os.makedirs(deep)
    assert EP.find_episode_above(os.path.join(deep, "script.txt")) == path
    assert EP.find_episode_above(str(tmp_path / "elsewhere" / "x.txt")) is None


def test_inside():
    ep = {"root": os.path.abspath("show")}
    assert EP.inside(ep, os.path.join("show", "a", "b.txt"))
    assert not EP.inside(ep, os.path.abspath("other/b.txt"))
    assert not EP.inside(ep, os.path.abspath("show-2/b.txt"))
    assert not EP.inside(ep, None)


# ── Stepping through the workflows ────────────────────────────────────────

def _project_json(project):
    out = []
    for dp, _dn, fn in os.walk(project):
        if ".pb_cache" in dp:
            continue                    # the transcript and telemetry caches
        out += [os.path.relpath(os.path.join(dp, f), project)
                for f in fn if f.endswith(".json") and not f.endswith(".pb_transcript.json")]
    return sorted(out)


@pytest.fixture
def stage_app(app, monkeypatch, project, tmp_path):
    """The app on a fresh episode, and a script for it."""
    from test_episode_screens import _open_fresh
    _open_fresh(app, monkeypatch, project)
    # Each test starts from its own episode file.
    ep_file = os.path.join(project, "Ep 7" + EP.EPISODE_EXT)
    if os.path.exists(ep_file):
        os.remove(ep_file)
    app._ep.pop("file_path", None)
    app._ep["stages"].pop(EP.STAGE_S2S, None)
    app._ep["stages"].pop(EP.STAGE_AAF, None)
    app._ep["links"].pop("script", None)
    app._ep["links"].pop("aaf", None)
    script = os.path.join(project, "05_SCRIPT", "Ep 7.txt")
    os.makedirs(os.path.dirname(script), exist_ok=True)
    with open(script, "w", encoding="utf-8") as f:
        f.write("[PART Open]\n[VO PART_0_NARRATOR]\nA quiet morning.\n"
                "[BUTLER 00:00:05-00:00:20]\nWe went out at four.\n")
    app._ep["links"]["script"] = EP.rel(app._ep, script)
    app._ep_save()
    yield app, ep_file
    if os.path.exists(ep_file):
        os.remove(ep_file)


def _s2s_up(app):
    from test_episode_screens import _wait, _pump
    app._ep_launch_s2s()
    assert _wait(app, lambda: getattr(app, "_pool", None) is not None
                 and len(app._pool._rows) >= 9, secs=30)
    _pump(app, 10)


def test_script_session_saves_into_the_episode_with_no_dialog(stage_app, project):
    from test_episode_screens import _pump
    app, ep_file = stage_app
    before = _project_json(project)
    _s2s_up(app)
    app._pool._rows[0]["var"].set("BUTLER")

    assert app._ep_stage_showing() == EP.STAGE_S2S
    app._quick_save(app._footer_save_btn)      # would record "unexpected dialog"
    _pump(app, 3)
    assert not app._errors, "\n".join(app._errors)
    assert app._footer_save_btn[0].cget("text") == "SAVED ✓"
    assert not app._is_dirty()
    assert _project_json(project) == before    # no session.json beside it

    saved = EP.get_stage(EP.load(ep_file), EP.STAGE_S2S)
    assert saved["script"].endswith("Ep 7.txt")
    assert saved["assignments"][app._pool._rows[0]["path"]] == "BUTLER"


def test_script_session_comes_back_from_the_episode(stage_app, project):
    from test_episode_screens import _pump, _wait
    app, ep_file = stage_app
    _s2s_up(app)
    path0 = app._pool._rows[0]["path"]
    app._pool._rows[0]["var"].set("BUTLER")
    # Going back to the episode keeps the work, without a save being pressed.
    app._ep_enter(); _pump(app, 5)
    assert app.workflow == "episode"
    assert EP.get_stage(EP.load(ep_file), EP.STAGE_S2S)["assignments"][path0] == "BUTLER"
    assert not app._is_dirty()

    # Reopen the whole project from the one file.
    app._ep = None
    app._home(); _pump(app, 3)
    app._open_session(ep_file); _pump(app, 5)
    assert app.workflow == "episode"
    app._ep_launch_s2s()
    assert _wait(app, lambda: getattr(app, "_pool", None) is not None
                 and any(r["path"] == path0 and r["var"].get() == "BUTLER"
                         for r in app._pool._rows), secs=30)
    assert not app._errors, "\n".join(app._errors)


def test_going_home_from_a_stage_keeps_its_work(stage_app):
    from test_episode_screens import _pump
    app, ep_file = stage_app
    _s2s_up(app)
    path0 = app._pool._rows[0]["path"]
    app._pool._rows[0]["var"].set("BUTLER")
    app._home(); _pump(app, 3)
    assert EP.get_stage(EP.load(ep_file), EP.STAGE_S2S)["assignments"][path0] == "BUTLER"


def test_save_as_from_a_stage_saves_the_episode(stage_app, monkeypatch, project):
    import episode_workflow
    app, ep_file = stage_app
    _s2s_up(app)
    other = os.path.join(project, "Ep 7 copy" + EP.EPISODE_EXT)
    titles = []
    monkeypatch.setattr(episode_workflow.filedialog, "asksaveasfilename",
                        lambda **k: (titles.append(k["title"]), other)[1])
    try:
        app._save_as()
        assert titles == ["Save episode"]
        assert EP.get_stage(EP.load(other), EP.STAGE_S2S)["script"].endswith("Ep 7.txt")
        assert app._ep["file_path"] == other
    finally:
        if os.path.exists(other):
            os.remove(other)


def test_work_from_outside_the_episode_keeps_its_own_file(stage_app, tmp_path, monkeypatch):
    """Unrelated work done with an episode open is not swept into it."""
    import main as main_mod
    app, ep_file = stage_app
    elsewhere = tmp_path / "other.txt"
    elsewhere.write_text("x")
    app.workflow, app._script_path = "script_session", str(elsewhere)
    assert app._ep_stage_showing() is None
    calls = []
    monkeypatch.setattr(main_mod.filedialog, "asksaveasfilename",
                        lambda **k: (calls.append(k["title"]), "")[1])
    app._current_session_file = None
    app._quick_save()
    assert calls == ["Save Session As"]
    assert EP.STAGE_S2S not in EP.load(ep_file)["stages"]


def test_first_save_finds_the_projects_episode(stage_app, project):
    """Script -> Session from Home, no episode open: the project's episode
    is still where its save goes."""
    from test_episode_screens import _wait, _pump
    app, ep_file = stage_app
    script = os.path.join(project, "05_SCRIPT", "Ep 7.txt")
    app._ep = None
    app._home(); _pump(app, 3)
    app.workflow = "script_session"
    app._current_session_file = None
    app._load_script(script)
    assert _wait(app, lambda: getattr(app, "_pool", None) is not None, secs=30)
    _pump(app, 5)
    app._quick_save()
    assert not app._errors, "\n".join(app._errors)
    assert app._ep["file_path"] == ep_file
    assert EP.get_stage(EP.load(ep_file), EP.STAGE_S2S)["script"] == script


def test_pull_quotes_sessions_live_in_the_episode(stage_app, project):
    from test_episode_screens import _pump
    app, ep_file = stage_app
    before = _project_json(project)
    app._ep_launch_pq(); _pump(app, 5)
    assert app._ep_stage_showing() == EP.STAGE_PQ
    n0 = len(app._pq_project["sessions"])
    made = app._pq_create_session_from_source("Ep 7 pt 2", "narr 2", open_view=False)
    made["transcript"] = [{"word": " Hello", "start": 0.0, "end": 0.3}]
    assert app._pq_save_session_file(made)
    assert "_file_path" not in made
    # Sessions save on every edit, so the file follows a moment later.
    app._ep_save_flush()
    held = EP.pq_sessions(EP.load(ep_file))
    assert held[made["id"]]["transcript"][0]["word"] == " Hello"
    assert _project_json(project) == before    # no .pb_session.json for it

    # Back through the episode: it reopens from the one file.
    app._ep_enter(); _pump(app, 3)
    app._ep_launch_pq(); _pump(app, 5)
    again = [s for s in app._pq_project["sessions"] if s["id"] == made["id"]]
    assert again and again[0]["transcript"][0]["word"] == " Hello"
    assert app._pq_project["episode_file"] == ep_file

    # A session taken out of the project goes from the episode on save.
    app._pq_project["sessions"] = [s for s in app._pq_project["sessions"]
                                   if s["id"] != made["id"]]
    assert app._pq_save_episode_project(prompt_path=False)
    saved = EP.load(ep_file)
    assert made["id"] not in EP.pq_sessions(saved)
    assert len(app._pq_project["sessions"]) == n0
    assert saved["sources"]["Ep 7 pt 2"]["session"] is None
    assert not app._errors, "\n".join(app._errors)
