"""The hand-off from a writer's script to PostBridge.

Each case here is a real defect from a delivered Blood Trails script.  Script
Check has to name it, and Fix Format has to repair what can be repaired without
touching a word of what the script says.
"""
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from parsers import DEFAULT_DOC_TITLE, parse_script
from script_check import ERROR, WARN, run_checks
from script_normalize import has_loose_pulls, normalize
from utils import suggest_vo_part


# Mike Williams: one [VO] at the top, narration after that running on under the
# pulls, sections and the outro only as loose lines.
SWALLOWED = """[TOKENS]
[CLAY]
[UNUSED]
[/TOKENS]


[PART Intro]


[VO PART_0_NARRATOR]


A duck hunter drives to a lake.


That's next.


—


In December of 2000, Damon felt that tension.


[DAMON 00:25:46-00:26:11]
“I can't do it man.”


Mike was married himself.


[CLAY 00:31:11-00:31:57]
“There's something bad wrong.”


Part 1: The Lake


Scott searched harder than anyone.


[CLAY 00:36:20-00:36:30]
“They found the boat.”


—


Thanks for listening to this episode.
"""

# Season 2 Update: plain prose, one pull in the wrong form, an unmarked quote
# and a placeholder for a clip.
UNMARKED = """Intro


We're taking a break this week.


—


Part 1


Ryan is a prosecutor.


[RYAN_WELDON 00:07:27-00:08:03]
“When you have your initial charge.”


Sherry spoke to the reporter.


“I feel good that I've got justice.”


Part 2


Sheriff Dutton told me about his wife.


Dutton (42:38-42:58): “His wife almost confessed to it.”


His response was less emphatic than I expected.


Chris Interview Clip


Now, obviously, an investigator doesn't show his cards.


Outro


See you next time.
"""

# Bill Richardson: [PART] headers with the numbers on loose lines beneath them,
# a blank title from the token block, and VO ids that carry the real numbering.
NUMBERS_BELOW = """[TOKENS]
HOGAN1
[/TOKENS]


[PART Intro]


[VO PART_0_NARRATOR]
A pigeon flutters.


[PART Bill]


Part 1: Bill


[HOGAN1 00:07:05-00:07:27]
“Bill's been shot.”


[VO PART_1_NARRATOR]
Bill was a hunter.


[PART The Hit Men]


Part 2: The Hit Men


[VO PART_2_NARRATOR]
Police arrived.


[PART Outro]


[VO PART_3_NARRATOR]
Thanks for listening.
"""


def _titles(text, severity=None):
    return [f.title for f in run_checks(text) if severity in (None, f.severity)]


def _words(text):
    """Every word a script says, without its headers, in order."""
    keep = []
    text = re.sub(r"\[TOKENS\].*?\[/TOKENS\]", "", text, flags=re.S)
    for ln in text.lstrip("﻿").split("\n"):
        s = ln.strip()
        if s and not s.startswith("[") and s.strip("—–- "):
            keep.append(s)
    return keep


# ── Script Check ────────────────────────────────────────────────────────────

def test_narration_inside_a_pull_is_an_error():
    errs = _titles(SWALLOWED, ERROR)
    assert any("narration" in t and "inside interview pulls" in t for t in errs)


def test_swallowed_narration_is_not_also_reported_as_short_spans():
    assert not any("too short" in t for t in _titles(SWALLOWED))


def test_narration_after_a_divider_under_a_pull_counts_as_swallowed():
    text = ("Title\n\n[PART One]\n\n[A 00:00:01-00:00:03]\n“Short.”\n\n—\n\n"
            + "Thanks for listening to this episode of the show. " * 20 + "\n")
    assert any("inside interview pulls" in t for t in _titles(text, ERROR))
    assert not any("too short" in t for t in _titles(text))


def test_section_lines_are_an_error():
    assert any("section line" in t for t in _titles(SWALLOWED, ERROR))
    assert any("section line" in t for t in _titles(NUMBERS_BELOW, ERROR))


def test_dividers_are_reported():
    assert any("divider" in t for t in _titles(SWALLOWED, WARN))


def test_a_pull_written_loose_is_an_error():
    assert any("no valid header" in t for t in _titles(UNMARKED, ERROR))


@pytest.mark.parametrize("line", [
    "[dutton 00:42:38-00:42:58]",
    "[DUTTON 42:38-42:58]",
    "[DUTTON 00:42:38 - 00:42:58]",
    "[DUTTON 00:42:38–00:42:58]",
])
def test_malformed_pull_headers_are_errors(line):
    text = "Title\n\n[PART One]\n\n[VO PART_0_NARRATOR]\nHello.\n\n%s\n“A quote.”\n" % line
    assert any("no valid header" in t for t in _titles(text, ERROR))


def test_a_placeholder_is_an_error():
    assert any("Chris Interview Clip" in t for t in _titles(UNMARKED, ERROR))


def test_a_quote_with_no_header_is_flagged():
    assert any("is a quote" in t for t in _titles(UNMARKED, WARN))


def test_a_clip_waiting_on_timecodes_is_an_error():
    text = ("Title\n\n[PART One]\n\n[VO PART_0_NARRATOR]\nHello.\n\n"
            "// NEEDS TIMECODES: the Sherry clip\n\n[A 00:00:01-00:00:05]\n“Hi there.”\n")
    assert any("timecodes" in t for t in _titles(text, ERROR))


def test_second_recordings_are_not_called_typos():
    text = ("Title\n\n[KATHY 00:00:01-00:00:05]\n“One.”\n\n"
            "[KATHY2 00:00:01-00:00:05]\n“Two.”\n\n[VO PART_0_NARRATOR]\nHi.\n\n[PART One]\n")
    assert not any("one character" in t for t in _titles(text))


def test_real_token_typos_are_still_caught():
    text = ("Title\n\n[JOHN 00:00:01-00:00:05]\n“One.”\n\n"
            "[JOHM 00:00:06-00:00:09]\n“Two.”\n\n[VO PART_0_NARRATOR]\nHi.\n\n[PART One]\n")
    assert any("one character" in t for t in _titles(text))


def test_a_clean_script_has_no_structure_findings():
    fixed, _ = normalize(NUMBERS_BELOW, title="Bill")
    assert not [f for f in run_checks(fixed)
                if f.category == "Structure" and f.severity != "info"]


# ── Parser ──────────────────────────────────────────────────────────────────

def test_token_names_are_not_the_title():
    assert parse_script(NUMBERS_BELOW)[4] == DEFAULT_DOC_TITLE


def test_narration_is_not_the_title():
    assert parse_script(SWALLOWED)[4] == DEFAULT_DOC_TITLE


def test_a_loose_line_above_the_tokens_is_the_title():
    assert parse_script("My Episode\n\n" + NUMBERS_BELOW)[4] == "My Episode"


def test_a_bom_stays_out_of_the_title():
    assert parse_script("﻿My Episode\n\n[PART One]\n")[4] == "My Episode"


def test_a_bom_does_not_hide_a_first_line_header():
    assert parse_script("﻿[PART One]\n")[1][0]["name"] == "One"


DUP = """Title

[A 00:11:22-00:11:33]
“And then he starts crying.”

[VO PART_0_NARRATOR]
Then.

[A 00:11:22-00:11:33]
“Denise, he was going to kill you.”
"""


def test_a_different_quote_on_the_same_timecodes_is_kept_and_named():
    pulls = parse_script(DUP)[2]
    assert [p["quote_text"] for p in pulls] == ["And then he starts crying.",
                                                "Denise, he was going to kill you."]
    assert any("same timecodes" in t for t in _titles(DUP, ERROR))


def test_an_exact_repeat_is_still_dropped_and_named():
    text = DUP.replace("Denise, he was going to kill you.", "And then he starts crying.")
    assert len(parse_script(text)[2]) == 1
    assert any("exact repeat" in t for t in _titles(text, WARN))


# ── VO file matching ────────────────────────────────────────────────────────

def _parts(text):
    return parse_script(text)[1]


def test_vo_files_follow_the_scripts_own_numbering():
    parts = _parts(NUMBERS_BELOW)
    assert suggest_vo_part("VO Part 1.wav", parts) == "VO: Bill"
    assert suggest_vo_part("VO Part 2.wav", parts) == "VO: The Hit Men"
    assert suggest_vo_part("VO Intro.wav", parts) == "VO: Intro"


def test_outro_files_match_the_outro():
    assert suggest_vo_part("Williams Outro.wav", _parts(NUMBERS_BELOW)) == "VO: Outro"
    assert suggest_vo_part("Williams_Outro.wav", _parts(NUMBERS_BELOW)) == "VO: Outro"


def test_numbers_in_part_names_win():
    parts = [{"index": 1, "name": "Intro"}, {"index": 2, "name": "Part 1: Bill"}]
    assert suggest_vo_part("Part 1.wav", parts) == "VO: Part 1: Bill"


def test_position_skips_an_intro_when_nothing_else_says():
    parts = [{"index": 1, "name": "Intro"}, {"index": 2, "name": "Bill"},
             {"index": 3, "name": "Hit Men"}]
    assert suggest_vo_part("Part 1.wav", parts) == "VO: Bill"
    assert suggest_vo_part("Part 2.wav", parts) == "VO: Hit Men"


def test_position_without_an_intro_is_unchanged():
    parts = [{"index": 1, "name": "Bill"}, {"index": 2, "name": "Hit Men"}]
    assert suggest_vo_part("Part 1.wav", parts) == "VO: Bill"


# ── Fix Format ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("script", [SWALLOWED, UNMARKED, NUMBERS_BELOW])
def test_fixing_twice_changes_nothing_more(script):
    once, changes = normalize(script, title="Episode")
    assert changes
    again, more = normalize(once, title="Episode")
    assert more == [] and again == once


def test_fix_restores_the_narration_and_parts():
    fixed, _ = normalize(SWALLOWED, title="Duck Hunter")
    tokens, parts, pulls, vo, title, _w = parse_script(fixed)
    assert title == "Duck Hunter"
    assert [p["name"] for p in parts] == ["Intro", "Part 1: The Lake", "Outro"]
    assert [p["quote_text"] for p in pulls] == [
        "I can't do it man.", "There's something bad wrong.", "They found the boat."]
    said = " ".join(v["text"] for v in vo)
    for line in ("Mike was married himself.", "Scott searched harder than anyone.",
                 "Thanks for listening to this episode.", "In December of 2000"):
        assert line in said
    assert "—" not in said
    assert not any(e in t for t in _titles(fixed, ERROR) for e in ("narration", "section"))


def test_fix_keeps_every_vo_block_in_its_part():
    fixed, _ = normalize(SWALLOWED, title="T")
    _t, parts, _p, vo, _d, _w = parse_script(fixed)
    names = {p["index"]: p["name"] for p in parts}
    assert {v["id"]: names[v["part_index"]] for v in vo} == {
        "PART_0_NARRATOR": "Intro", "PART_1_NARRATOR": "Part 1: The Lake",
        "PART_2_NARRATOR": "Outro"}


def test_fix_never_changes_the_words():
    for script in (SWALLOWED, NUMBERS_BELOW):
        fixed, _ = normalize(script, title="T")
        section = ("Part 1: The Lake", "Part 1: Bill", "Part 2: The Hit Men")
        assert ([w for w in _words(script) if w not in section]
                == [w for w in _words(fixed) if w != "T"])


def test_fix_rewrites_a_loose_pull_and_keeps_its_quote():
    fixed, changes = normalize(UNMARKED, title="T")
    assert "[DUTTON 00:42:38-00:42:58]\n“His wife almost confessed to it.”" in fixed
    pulls = parse_script(fixed)[2]
    assert [(p["token"], p["in_tc"]) for p in pulls] == [
        ("RYAN_WELDON", "00:07:27"), ("DUTTON", "00:42:38")]


def test_fix_leaves_what_it_cannot_settle_for_script_check():
    fixed, _ = normalize(UNMARKED, title="T")
    errs = _titles(fixed, ERROR)
    assert any("Chris Interview Clip" in t for t in errs)
    assert any("is a quote" in t for t in _titles(fixed, WARN))


def test_fix_folds_a_number_into_the_part_above():
    fixed, _ = normalize(NUMBERS_BELOW, title="T")
    names = [p["name"] for p in parse_script(fixed)[1]]
    assert names == ["Intro", "Part 1: Bill", "Part 2: The Hit Men", "Outro"]


def test_fix_lists_tokens_that_were_used_but_not_listed():
    fixed, changes = normalize(SWALLOWED, title="T")
    assert any("DAMON" in c for c in changes)
    block = fixed.split("[TOKENS]")[1].split("[/TOKENS]")[0].split()
    assert block == ["CLAY", "UNUSED", "DAMON"]


def test_fix_leaves_a_monolog_script_alone():
    text = "The Gunfighter\n\nHe rode into town.\n\n____\n\nThe sun was low.\n"
    assert normalize(text, title="T") == (text, [])


def test_fix_leaves_the_old_syntax_for_the_ai_prompt():
    text = "Title\n\n@PART One\n\n@VO x\nHello there.\n\n[A 00:00:01-00:00:02]\n“Hi.”\n"
    assert normalize(text, title="T")[1] == []


def test_loose_pulls_keep_a_script_out_of_monolog_mode():
    assert has_loose_pulls("Title\n\nDutton (42:38-42:58): “His wife.”\n")
    assert not has_loose_pulls("Title\n\nAt 10:50-11:00 the family arrived home.\n")


def test_ordinary_narration_is_not_mistaken_for_structure():
    text = ("Title\n\n[PART One]\n\n[VO PART_1_NARRATOR]\n"
            "Part of the reason he left was money.\n\n"
            "Introduction to the case came later.\n\n"
            "That clip was the last anyone saw of him.\n\n"
            "He said “it's over” and walked out.\n\n[A 00:00:01-00:00:04]\n“Bye now.”\n")
    assert normalize(text, title="T")[1] == []
    assert not [f for f in run_checks(text) if f.severity == ERROR]


def test_a_multi_paragraph_quote_stays_in_its_pull():
    text = ("Title\n\n[LYNN 00:06:58-00:07:16]\n“And he would pop their heads off.\n\n"
            "Oh wow.\n\nYeah, he was good.”\n\n[VO PART_0_NARRATOR]\nNext.\n\n[PART One]\n")
    assert normalize(text, title="T")[1] == []
    assert len(parse_script(text)[2][0]["quote_paragraphs"]) == 3


def test_an_unquoted_pull_keeps_its_paragraphs():
    text = ("Title\n\n[JENA 00:01:23-00:01:45]\nJORDAN: Tell me.\n\nJENA: We went out.\n\n"
            "[VO PART_0_NARRATOR]\nNext.\n\n[PART One]\n")
    assert normalize(text, title="T")[1] == []


# ── The screen ──────────────────────────────────────────────────────────────

def test_fix_format_saves_a_copy_and_opens_it(tmp_path):
    tk = pytest.importorskip("tkinter")
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        pytest.skip("no display")
    try:
        import main
        app = main.App()
    except tk.TclError as e:
        pytest.skip("Tk unavailable: {}".format(e))
    try:
        src = tmp_path / "Episode.txt"
        src.write_text(SWALLOWED, encoding="utf-8")
        app._load_script(str(src), navigate=False)
        assert app.doc_title == "Episode"       # no title in it: the file's name
        app._step_check()
        fixed, changes = normalize(SWALLOWED, title="Episode")
        win = app._fix_format_dialog(fixed, changes)
        win.update_idletasks()
        win.destroy()

        path = app._fix_format_save(fixed)
        assert os.path.basename(path) == "Episode_fixed.txt"
        assert src.read_text(encoding="utf-8") == SWALLOWED
        assert app._script_path == os.path.abspath(path)
        assert len(app.vo_blocks) == 5
        assert os.path.basename(app._fix_format_save(fixed)) == "Episode_fixed_fixed.txt"
    finally:
        app.destroy()
