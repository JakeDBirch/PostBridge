"""Cameras go to the recordings they were shot with.

Taken from a real episode (Blood Trails, Bill Richardson), where every
camera that went to the wrong recording synced at 0-77 % -- or, worse, at
94 % against footage of a different part -- and every one of them synced
at 100 % against its own camera:

* VO parts recorded as "Richardson Part 5 (1).wav" etc., cameras
  "Richardson Part 5.MP4", running times within a few percent of each
  other.  Discovery took recordings longest first, so Part 2 claimed the
  Part 5 and Part 6 cameras as "angles", and the "(1)" copy suffix read as
  a part number, giving Part 5 the Part 1 camera.
* Three Riverside sessions whose names all reduce to "riverside": each
  replaced the last, and two sessions' cameras ended up nowhere.
* A rescan kept every camera list it found, so none of it could be fixed
  by scanning again.
* Step 2's name matcher then filled the gaps with another session's
  camera ("..._0388" took "..._0453").

Pure Python: no media, no Tk.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import episode as EP                     # noqa: E402
from parsers import match_source_to_video  # noqa: E402

VO, CAM, RAW = "03_AUDIO/01_VO/", "02_MEDIA/0_RAW FOOTAGE/", "03_AUDIO/00_RAW AUDIO/"


def _files():
    f = {}
    def add(rel, kind, secs):
        f[rel] = {"kind": kind, "size": 1, "duration_s": secs,
                  "status": "source", "reason": "", "dup_of": None}
    for name, rec, cam in [("Intro", 276.0, 285.3), ("Part 1 (1)", 380.9, 387.4),
                           ("Part 2 (1)", 449.1, 457.0), ("Part 3 (1)", 277.4, 284.8),
                           ("Part 4", 274.5, 284.3), ("Part 5 (1)", 442.5, 453.5),
                           ("Part 6", 414.3, 421.4), ("Outro (1)", 142.8, 144.6)]:
        add(VO + "Richardson {}.wav".format(name), "audio", rec)
        add(CAM + "Richardson {}.MP4".format(name.replace(" (1)", "")), "video", cam)
    for a, b, secs in [("jordan", "rks", 1676.4), ("jordan", "peter_barth", 8039.2),
                       ("jordan", "lynn_richardson", 3266.2)]:
        n1 = {"rks": 453, "peter_barth": 388, "lynn_richardson": 454}[b]
        n2 = n1 - 1 if b == "rks" else n1 + 1
        add(RAW + "riverside_{}_raw-audio_blood_trails_{:04d}.wav".format(a, n1), "audio", secs)
        add(RAW + "riverside_{}_raw-audio_blood_trails_{:04d}.wav".format(b, n2), "audio", secs + 0.2)
        add(CAM + "riverside_{}_raw-video-cfr_blood_trails_{:04d}.mp4".format(a, n1), "video", secs)
        add(CAM + "riverside_{}_raw-video-cfr_blood_trails_{:04d}.mp4".format(b, n2), "video", secs + 0.3)
    add("INT/Grandmother (Witness) Interview Dec 2022.m4a", "audio", 385.3)
    add("INT/Ashlyn Kuersten Interview Nov 21 - Cold Case Expert.mp4", "video", 382.2)
    return f


def _cams(src):
    return [os.path.basename(v) for v in src["video"]]


def test_every_part_gets_its_own_camera():
    s = EP.propose_sources(_files())
    for part in ("Intro", "Part 1 (1)", "Part 2 (1)", "Part 3 (1)", "Part 4",
                 "Part 5 (1)", "Part 6", "Outro (1)"):
        assert _cams(s["Richardson " + part]) == \
            ["Richardson {}.MP4".format(part.replace(" (1)", ""))], part


def test_sessions_sharing_a_name_all_survive_with_their_cameras():
    s = EP.propose_sources(_files())
    riverside = {n: src for n, src in s.items() if n.startswith("riverside")}
    assert len(riverside) == 3
    for src in riverside.values():
        nums = {os.path.splitext(a)[0][-4:] for a in src["audio"]}
        assert {os.path.splitext(v)[0][-4:] for v in src["video"]} == nums


def test_one_shared_word_does_not_pair_a_camera():
    s = EP.propose_sources(_files())
    assert s["Grandmother (Witness) Interview Dec 2022"]["video"] == []


def _scanned():
    files = _files()
    return {"files": files, "sources": EP.propose_sources(files), "mix": None,
            "sessions": {}}


def _old_episode():
    """The episode as the old scan and a Script -> Session save left it."""
    ep = EP.new_episode("X:/show")
    ep["media"] = _files()
    ep["sources"] = {
        "Richardson Part 2 (1)": {"token": None, "audio": [VO + "Richardson Part 2 (1).wav"],
                                  "video": [CAM + "Richardson Part 2.MP4", CAM + "Richardson Part 5.MP4",
                                            CAM + "Richardson Part 6.MP4"],
                                  "session": None, "vo_part": "Part 2: The Hit Men"},
        "Richardson Part 5 (1)": {"token": None, "audio": [VO + "Richardson Part 5 (1).wav"],
                                  "video": [CAM + "Richardson Part 1.MP4"], "session": None,
                                  "vo_part": "Part 5: Bruce Bass"},
        "riverside": {"token": "RKS", "session": None,
                      "audio": [RAW + "riverside_jordan_raw-audio_blood_trails_0453.wav",
                                RAW + "riverside_rks_raw-audio_blood_trails_0452.wav"],
                      "video": [CAM + "riverside_jordan_raw-video-cfr_blood_trails_0453.mp4",
                                CAM + "riverside_rks_raw-video-cfr_blood_trails_0452.mp4"]},
        # Made by Script -> Session: the user tokened the recordings only.
        "PETER": {"token": "PETER", "session": None, "user_made": True, "video": [],
                  "audio": [RAW + "riverside_jordan_raw-audio_blood_trails_0388.wav",
                            RAW + "riverside_peter_barth_raw-audio_blood_trails_0389.wav"]},
        # Hand-edited: the user chose this camera.
        "Richardson Part 4": {"token": None, "audio": [VO + "Richardson Part 4.wav"],
                              "video": [CAM + "Richardson Intro.MP4"], "session": None,
                              "user_made": True},
    }
    return ep


def test_rescan_corrects_guesses_and_keeps_decisions():
    ep = _old_episode()
    EP.apply_scan(ep, _scanned())
    s = ep["sources"]
    # Proposed cameras nobody chose are proposed afresh...
    assert _cams(s["Richardson Part 2 (1)"]) == ["Richardson Part 2.MP4"]
    assert _cams(s["Richardson Part 5 (1)"]) == ["Richardson Part 5.MP4"]
    # ...and what they were told stays: VO part, token, name.
    assert s["Richardson Part 2 (1)"]["vo_part"] == "Part 2: The Hit Men"
    assert s["riverside"]["token"] == "RKS"
    assert not [n for n in s if n.startswith("riverside_jordan_raw-audio_blood_trails_0453")]
    # A hand edit is the user's: kept, and its camera given to no one else.
    assert _cams(s["Richardson Part 4"]) == ["Richardson Intro.MP4"]
    assert s["Richardson Intro"]["video"] == []
    # The user's source holds its recordings and gains their cameras.
    assert s["PETER"]["token"] == "PETER"
    assert _cams(s["PETER"]) == ["riverside_jordan_raw-video-cfr_blood_trails_0388.mp4",
                                 "riverside_peter_barth_raw-video-cfr_blood_trails_0389.mp4"]
    assert not [n for n in s if n.startswith("riverside_jordan_raw-audio_blood_trails_0388")]
    # No file belongs to two sources.
    seen = {}
    for n, src in s.items():
        for f in src.get("audio", []) + src.get("video", []):
            assert f not in seen, (f, seen.get(f), n)
            seen[f] = n


def test_rescan_twice_changes_nothing():
    ep = _old_episode()
    EP.apply_scan(ep, _scanned())
    first = {n: dict(s) for n, s in ep["sources"].items()}
    EP.apply_scan(ep, _scanned())
    assert ep["sources"] == first


def test_step2_never_guesses_another_session_or_part():
    pool = [CAM + "riverside_jordan_raw-video-cfr_blood_trails_0453.mp4",
            CAM + "Richardson Intro.MP4", CAM + "Richardson Part 1.MP4",
            CAM + "Richardson Part 5.MP4"]
    assert match_source_to_video("riverside_jordan_raw-audio_blood_trails_0388", pool) is None
    assert match_source_to_video("riverside_lynn_richardson_raw-audio_blood_trails_0455", pool) is None
    assert match_source_to_video("Richardson Part 5 (1)", pool).endswith("Richardson Part 5.MP4")
    assert match_source_to_video("Richardson Part 3 (1)", pool) is None
    assert match_source_to_video("Richardson Intro", pool).endswith("Richardson Intro.MP4")
    # Still finds what it always found.
    assert match_source_to_video("2025-05-21~interview~10543", ["a/10543.m4v"]) == "a/10543.m4v"
    assert match_source_to_video("Ep 7 pt 1", ["a/Ep 7 CAM 1 01.mp4"]) == "a/Ep 7 CAM 1 01.mp4"
    assert match_source_to_video("Butler Interview_JORDAN", ["a/Butler Interview.MP4"]) \
        == "a/Butler Interview.MP4"
