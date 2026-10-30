"""Episode discovery: fold duplicates, put noise aside, propose sources.

Builds a small project folder with real (tiny) media so running times are
genuine: two sessions with two cameras each, a mic-per-speaker pair, a Pro
Tools copy of a recording, a music bed, a sting, a programme mix, and a
Pull Quotes session that names one recording.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
    pytest.skip("ffmpeg/ffprobe not on PATH", allow_module_level=True)

import episode  # noqa: E402


def _ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True,
                   capture_output=True, timeout=300)


def _wav(path, secs, seed=1):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _ff("-f", "lavfi", "-i", "anoisesrc=d={}:c=pink:r=8000:a=0.2:seed={}".format(secs, seed),
        "-ac", "1", path)


def _mp4(path, secs, seed):
    # Each camera carries its own scratch audio, so two cameras on one
    # shoot are different files (byte-identical ones fold as duplicates).
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _ff("-f", "lavfi", "-i", "testsrc=size=64x48:rate=5:duration={}".format(secs),
        "-f", "lavfi", "-i", "anoisesrc=d={}:c=pink:r=8000:a=0.2:seed={}".format(secs, seed),
        "-t", str(secs), "-c:v", "mpeg4", "-q:v", "31", "-c:a", "aac", "-shortest", path)


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    root = tmp_path_factory.mktemp("show") / "Ep 7"
    root.mkdir()
    r = str(root)
    # Day 1: 100 s recording, two cameras ~100 s.  Day 2: 70 s, two cameras.
    _wav(os.path.join(r, "03_AUDIO", "01_VO", "Ep 7 pt 1.wav"), 100, 1)
    _wav(os.path.join(r, "03_AUDIO", "01_VO", "Ep 7 pt 2.wav"), 70, 2)
    _mp4(os.path.join(r, "02_MEDIA", "RAW", "Ep 7 CAM 1 01.mp4"), 99, 11)
    _mp4(os.path.join(r, "02_MEDIA", "RAW", "Ep 7 CAM 2 01.mp4"), 99, 12)
    _mp4(os.path.join(r, "02_MEDIA", "RAW", "Ep 7 p2 CAM 1 01.mp4"), 71, 13)
    _mp4(os.path.join(r, "02_MEDIA", "RAW", "Ep 7 p2 CAM 2 01.mp4"), 71, 14)
    # Interview with a mic per speaker, 80 s each, one camera.
    _wav(os.path.join(r, "03_AUDIO", "00_RAW AUDIO", "Butler Interview_JORDAN.wav"), 80, 3)
    _wav(os.path.join(r, "03_AUDIO", "00_RAW AUDIO", "Butler Interview_CARY.wav"), 80, 4)
    _mp4(os.path.join(r, "02_MEDIA", "RAW", "Butler Interview.MP4"), 80, 15)
    # A library cue and a Pro Tools-only recording in the session folder.
    pt = os.path.join(r, "01_PROJECT FILES", "PRO TOOLS", "Ep 7", "Audio Files")
    _wav(os.path.join(pt, "KPM_KPM_2259_00301_Tijuana_Waltz__a__APM.wav"), 100, 8)
    _wav(os.path.join(pt, "Cory.wav"), 90, 9)
    # Pro Tools copy of a recording, a music bed, a sting, a mix.
    os.makedirs(pt, exist_ok=True)
    shutil.copy(os.path.join(r, "03_AUDIO", "01_VO", "Ep 7 pt 1.wav"), os.path.join(pt, "Ep 7 pt 1.wav"))
    _wav(os.path.join(r, "03_AUDIO", "05_MUSIC", "bed.wav"), 90, 5)
    _wav(os.path.join(r, "03_AUDIO", "sting.wav"), 4, 6)
    _wav(os.path.join(r, "03_AUDIO", "04_MIXED AUDIO", "Ep 7_FOR VID.wav"), 120, 7)
    # A Pull Quotes session naming the Butler recordings.
    sess = os.path.join(r, "03_AUDIO", "00_RAW AUDIO", "BUTLER.pb_session.json")
    with open(sess, "w", encoding="utf-8") as f:
        json.dump({"version": 3, "workflow": "interview_session", "token": "BUTLER",
                   "media": [os.path.join(r, "03_AUDIO", "00_RAW AUDIO", "Butler Interview_JORDAN.wav"),
                             os.path.join(r, "03_AUDIO", "00_RAW AUDIO", "Butler Interview_CARY.wav")],
                   "transcript": []}, f)
    return r


def test_scan_sorts_the_folder(project):
    res = episode.scan(project)
    f = res["files"]
    st = {r: rec["status"] for r, rec in f.items()}
    assert st["03_AUDIO/01_VO/Ep 7 pt 1.wav"] == "source"
    assert st["03_AUDIO/01_VO/Ep 7 pt 2.wav"] == "source"
    assert st["01_PROJECT FILES/PRO TOOLS/Ep 7/Audio Files/Ep 7 pt 1.wav"] == "duplicate"
    assert f["01_PROJECT FILES/PRO TOOLS/Ep 7/Audio Files/Ep 7 pt 1.wav"]["dup_of"] == \
        "03_AUDIO/01_VO/Ep 7 pt 1.wav"
    assert st["03_AUDIO/05_MUSIC/bed.wav"] == "noise"
    assert f["03_AUDIO/05_MUSIC/bed.wav"]["duration_s"] is None   # never probed
    assert st["03_AUDIO/sting.wav"] == "short"
    assert st["03_AUDIO/04_MIXED AUDIO/Ep 7_FOR VID.wav"] == "mix"
    assert res["mix"] == "03_AUDIO/04_MIXED AUDIO/Ep 7_FOR VID.wav"
    assert all(st[v] == "source" for v in st if v.startswith("02_MEDIA/"))
    # Library cues are noise by name wherever they sit; a recording that
    # only ever lived in the Pro Tools folder is offered, not assumed.
    pt = "01_PROJECT FILES/PRO TOOLS/Ep 7/Audio Files/"
    assert st[pt + "KPM_KPM_2259_00301_Tijuana_Waltz__a__APM.wav"] == "noise"
    assert st[pt + "Cory.wav"] == "pt_copy"
    assert f[pt + "Cory.wav"]["duration_s"] == pytest.approx(90, abs=0.1)


def test_sources_group_mics_and_attach_cameras(project):
    res = episode.scan(project)
    s = res["sources"]
    assert set(s) == {"Ep 7 pt 1", "Ep 7 pt 2", "Butler Interview"}
    assert s["Ep 7 pt 1"]["video"] == ["02_MEDIA/RAW/Ep 7 CAM 1 01.mp4",
                                       "02_MEDIA/RAW/Ep 7 CAM 2 01.mp4"]
    assert s["Ep 7 pt 2"]["video"] == ["02_MEDIA/RAW/Ep 7 p2 CAM 1 01.mp4",
                                       "02_MEDIA/RAW/Ep 7 p2 CAM 2 01.mp4"]
    b = s["Butler Interview"]
    assert b["audio"] == ["03_AUDIO/00_RAW AUDIO/Butler Interview_CARY.wav",
                          "03_AUDIO/00_RAW AUDIO/Butler Interview_JORDAN.wav"]
    assert b["video"] == ["02_MEDIA/RAW/Butler Interview.MP4"]
    assert b["token"] == "BUTLER"
    assert b["session"] == "03_AUDIO/00_RAW AUDIO/BUTLER.pb_session.json"
    assert s["Ep 7 pt 1"]["token"] is None


def test_episode_round_trip_is_relative(project, tmp_path):
    ep = episode.new_episode(project, "Ep 7")
    episode.apply_scan(ep, episode.scan(project))
    # A user exclusion and a hand edit survive a rescan.
    ep["excluded"].append("02_MEDIA/RAW/Ep 7 CAM 2 01.mp4")
    ep["sources"]["Ep 7 pt 1"]["token"] = "NARR1"
    episode.apply_scan(ep, episode.scan(project, excluded=ep["excluded"]))
    assert ep["media"]["02_MEDIA/RAW/Ep 7 CAM 2 01.mp4"]["status"] == "excluded"
    assert ep["sources"]["Ep 7 pt 1"]["token"] == "NARR1"
    path = episode.save(ep)
    assert path == os.path.join(project, "Ep 7" + episode.EPISODE_EXT)
    raw = json.load(open(path, encoding="utf-8"))
    assert raw["workflow"] == "episode" and raw["links"]["mix"].startswith("03_AUDIO/")
    assert "sessions" not in raw
    held = list(raw["stages"][episode.STAGE_PQ]["sessions"].values())
    assert [s["token"] for s in held] == ["BUTLER"]
    assert all(m.startswith("$EP/") for m in held[0]["media"])
    assert "\\" not in json.dumps(raw["media"])

    # Move the whole folder: everything still resolves.
    moved = str(tmp_path / "moved")
    shutil.copytree(project, moved)
    ep2 = episode.load(os.path.join(moved, "Ep 7" + episode.EPISODE_EXT))
    assert ep2["root"] == moved
    assert os.path.isfile(episode.absp(ep2, ep2["links"]["mix"]))
    media = episode.pq_session_list(ep2)[0]["media"]
    assert media and all(m.startswith(moved) and os.path.isfile(m) for m in media)
    assert episode.find_episode_file(moved) == os.path.join(moved, "Ep 7" + episode.EPISODE_EXT)
    sm = episode.summary(ep2)
    assert sm["sources"] == 3 and sm["audio"] == 4
    assert sm["video"] == 4          # five cameras, one put aside above


def test_classify_path_rules():
    c = episode.classify_path
    assert c("03_AUDIO/05_MUSIC/bed.wav") == ("noise", "in '05_MUSIC'")
    assert c("01_PROJECT FILES/PT/Audio Files/x.wav")[0] == "pt_copy"
    assert c("00_FINAL_ASSETS/Z_TEMP_FOR_APPROVAL/ep.mp3")[0] == "noise"
    assert c("03_AUDIO/04_MIXED AUDIO/Ep_v2_FOR VID.wav")[0] == "mix"
    assert c("03_AUDIO/00_RAW AUDIO/BUTLER.pb_audio.wav")[0] == "noise"
    assert c("02_MEDIA/0_RAW FOOTAGE/cam.mp4") == (None, "")
    assert c("02_MEDIA/Session File Backups/x.wav")[0] == "noise"
    assert c("03_AUDIO/CEZ_CEZ_4163_01701_Ringo_el_Gringo_STEM_(PERC)_APM.wav")[0] == "noise"
    assert c("03_AUDIO/SFX_GUNRif_Rifle shots battle.wav")[0] == "noise"
    assert c("03_AUDIO/01_VO/Ep 6 pt 1.WAV") == (None, "")
