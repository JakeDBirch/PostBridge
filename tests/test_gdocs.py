"""Google Docs bridge: the planning of edits, without a network.

A Docs API document is a list of paragraphs with character ranges; notes
are inserted right under the line they concern, highest index first so
nothing shifts, each under a named range so the next pass can remove it.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gdocs  # noqa: E402
from script_check import Finding, ERROR, WARN, run_checks  # noqa: E402


def _doc(lines, named=None):
    """A Docs API document for *lines*; each paragraph ends in a newline."""
    content, idx = [], 1
    for ln in lines:
        txt = ln + "\n"
        content.append({"startIndex": idx, "endIndex": idx + len(txt),
                        "paragraph": {"elements": [{"textRun": {"content": txt}}]}})
        idx += len(txt)
    doc = {"revisionId": "r1", "body": {"content": content}}
    if named:
        doc["namedRanges"] = {n: {"namedRanges": [{"ranges": [{"startIndex": s, "endIndex": e}]}]}
                              for n, (s, e) in named.items()}
    return doc


SCRIPT = ["Ep 7", "", "[PART Open]", "[VO PART_0_NARRATOR]", "A quiet morning.",
          "[BUTLER 00:00:05-00:00:20]", "We went out at four."]


def test_paragraphs_and_normalize():
    doc = _doc(SCRIPT)
    paras = gdocs.paragraphs(doc)
    assert [t for t, _s, _e in paras] == SCRIPT
    assert paras[0] == ("Ep 7", 1, 6)                     # "Ep 7\n" is 5 chars
    assert paras[2][1] == paras[1][2]                     # contiguous
    assert gdocs.normalize_text(b"\xef\xbb\xbfA\r\nB\x0bC") == "A\nB\nC"


def test_notes_land_under_their_lines_highest_first():
    doc = _doc(SCRIPT)
    notes = [("[BUTLER 00:00:05-00:00:20]", "ERROR Tokens: BUTLER is not in this episode"),
             ("[PART Open]", "WARN Format: part has no pulls"),
             (None, "INFO: 2 pulls")]
    reqs, unplaced = gdocs.plan_notes(doc, notes)
    assert unplaced == []
    inserts = [r["insertText"] for r in reqs if "insertText" in r]
    paras = {t: e for t, _s, e in gdocs.paragraphs(doc)}
    assert [i["location"]["index"] for i in inserts] == sorted(
        [paras["[BUTLER 00:00:05-00:00:20]"], paras["[PART Open]"], paras["Ep 7"]], reverse=True)
    assert all(i["text"].startswith(gdocs.NOTE_PREFIX) and i["text"].endswith("\n") for i in inserts)
    # Each insert is followed by a style and a named range over exactly its text.
    for k in range(0, len(reqs), 3):
        ins, sty, nr = reqs[k], reqs[k + 1], reqs[k + 2]
        start = ins["insertText"]["location"]["index"]
        end = start + len(ins["insertText"]["text"])
        assert sty["updateTextStyle"]["range"] == {"startIndex": start, "endIndex": end}
        assert nr["createNamedRange"]["range"] == {"startIndex": start, "endIndex": end}
        assert nr["createNamedRange"]["name"].startswith(gdocs.NOTE_RANGE + ":")


def test_unfound_lines_go_to_the_top_not_nowhere():
    doc = _doc(SCRIPT)
    reqs, unplaced = gdocs.plan_notes(doc, [("[JENA 00:01:00-00:01:10]", "ERROR: gone")])
    assert unplaced == ["ERROR: gone"]
    assert reqs[0]["insertText"]["location"]["index"] == gdocs.paragraphs(doc)[0][2]


def test_delete_plan_removes_only_our_notes_highest_first():
    doc = _doc(SCRIPT + [gdocs.NOTE_PREFIX + "old"],
               named={"pbnote:aaa": (10, 20), "pbnote:bbb": (40, 60), "other": (1, 5)})
    reqs = gdocs.plan_delete_notes(doc)
    assert [r["deleteContentRange"]["range"] for r in reqs] == [
        {"startIndex": 40, "endIndex": 60}, {"startIndex": 10, "endIndex": 20}]


def test_findings_become_anchored_notes():
    text = "\n".join(SCRIPT)
    fs = [Finding(ERROR, "Tokens", "Unknown token", "BUTLER is not declared", line=6),
          Finding(WARN, "Format", "No title", line=None),
          Finding(WARN, "Format", "Blank line", "", line=2)]
    notes = gdocs.findings_to_notes(fs, text)
    assert notes[0] == ("[BUTLER 00:00:05-00:00:20]",
                        "ERROR Tokens: Unknown token — BUTLER is not declared")
    assert notes[1] == (None, "WARN Format: No title")
    assert notes[2][0] is None                           # blank line cannot anchor
    # A token-level finding with no line goes under that token's first pull.
    fs = [Finding(WARN, "Assets", "No session found for token BUTLER", "no BUTLER.pb_session.json")]
    assert gdocs.findings_to_notes(fs, text)[0][0] == "[BUTLER 00:00:05-00:00:20]"


def test_findings_that_name_a_pull_land_under_it():
    text = "\n".join(SCRIPT + ["[BUTLER 1:00:00-1:00:09]", "Much later."])
    head = "[BUTLER 00:00:05-00:00:20]"
    fs = [Finding(ERROR, "Transcript", "Timecodes point at the wrong audio", head + "\n“We went”"),
          Finding(WARN, "Transcript", "Span is short", "[BUTLER 01:00:00-01:00:09] is 9s"),
          Finding(WARN, "Transcript", "BUTLER was transcribed with the tiny model", "Most words."),
          Finding(WARN, "Assets", "Token BUTLER is used oddly"),
          Finding(WARN, "Assets", "Session discovery warning", "Duplicate session for token 'BUTLER': F:\\a"),
          Finding(WARN, "Transcript", "Timecodes point at the wrong audio", "[JENA 00:00:01-00:00:02]"),
          Finding(WARN, "Format", "No VO blocks were parsed")]
    assert [a for a, _m in gdocs.findings_to_notes(fs, text)] == [
        head, "[BUTLER 1:00:00-1:00:09]", head, head, head, None, None]


def test_one_line_holds_several_notes_in_order():
    doc = _doc(SCRIPT)
    head = "[BUTLER 00:00:05-00:00:20]"
    reqs, unplaced = gdocs.plan_notes(doc, [(head, "first"), (head, "second")])
    assert unplaced == []
    inserts = [r["insertText"] for r in reqs if "insertText" in r]
    assert {i["location"]["index"] for i in inserts} == {gdocs.paragraphs(doc)[5][2]}
    # Same index: the one inserted last ends up on top.
    assert [i["text"] for i in inserts] == [gdocs.NOTE_PREFIX + "second\n", gdocs.NOTE_PREFIX + "first\n"]


def test_repeated_lines_each_take_a_note():
    doc = _doc(["Title", "Same line.", "Other.", "Same line."])
    reqs, _unplaced = gdocs.plan_notes(doc, [("Same line.", "a"), ("Same line.", "b")])
    paras = gdocs.paragraphs(doc)
    assert [r["insertText"]["location"]["index"] for r in reqs if "insertText" in r] == [
        paras[3][2], paras[1][2]]


def test_plain_script_has_nothing_to_report():
    plain = "Episode 9\nThe first paragraph, read aloud.\n\nThe second one.\n"
    assert run_checks(plain) == []
    # Not plain: a title alone, nothing at all, the obsolete syntax.
    assert any(f.severity == ERROR for f in run_checks("Episode 9\n"))
    assert any(f.severity == ERROR for f in run_checks(""))
    assert any("obsolete" in f.title for f in run_checks("Episode 9\n@VO PART_1\nWords.\n"))


def _applied(lines, reqs):
    """The Doc's lines after *reqs*, applied in order as the Docs API would."""
    text = "\n".join(lines) + "\n"
    for r in reqs:
        if "deleteContentRange" in r:
            rg = r["deleteContentRange"]["range"]
            assert rg["endIndex"] <= len(text)       # never the final newline
            text = text[:rg["startIndex"] - 1] + text[rg["endIndex"] - 1:]
        else:
            i = r["insertText"]["location"]["index"] - 1
            text = text[:i] + r["insertText"]["text"] + text[i:]
    return text


@pytest.mark.parametrize("old,new", [
    (["Title", "Part 1", "Words.", "---", "More."], ["Title", "[PART Part 1]", "Words.", "More."]),
    (["Title", "Words."], ["Title", "[VO A]", "Words."]),
    (["Title", "Words."], ["Title", "Words.", "[VO A]", "End."]),      # past the last line
    (["Title", "Words.", "---"], ["Title", "Words."]),                 # the last line goes
    (["Title", "Part 1"], ["Title", "[PART Part 1]"]),                 # the last line changes
    (["Title", "Words.", "", ""], ["Title", "[VO A]", "Words."]),      # blanks at the end stay
    (["Title", "", "Words."], ["Title", "", "", "Words."]),            # a blank line is a line
])
def test_text_edits_reach_the_new_script(old, new):
    reqs = gdocs.plan_text_edits(_doc(old), "\n".join(new) + "\n")
    assert _applied(old, reqs).rstrip("\n").split("\n") == new


def test_text_edits_leave_matching_lines_alone():
    old = ["Title", "Part 1", "Words.", "More words.  "]     # trailing spaces are no change
    reqs = gdocs.plan_text_edits(_doc(old), "Title\n[PART Part 1]\nWords.\nMore words.\n")
    para = gdocs.paragraphs(_doc(old))[1]
    assert reqs == [
        {"deleteContentRange": {"range": {"startIndex": para[1], "endIndex": para[2]}}},
        {"insertText": {"location": {"index": para[1]}, "text": "[PART Part 1]\n"}}]


def test_soft_line_breaks_are_lines():
    # Like the plain-text export, so a header held by a soft break is found.
    doc = _doc(["Title", "Part 1\x0bWords."])
    assert [t for t, _s, _e in gdocs.script_lines(doc)] == ["Title", "Part 1", "Words."]
    reqs = gdocs.plan_text_edits(doc, "Title\n[PART Part 1]\nWords.\n")
    assert _applied(["Title", "Part 1\x0bWords."], reqs).rstrip("\n").split("\n") == [
        "Title", "[PART Part 1]", "Words."]
    # A length that doesn't add up (an inline image, an emoji): left whole.
    doc["body"]["content"][1]["endIndex"] += 1
    assert len(gdocs.script_lines(doc)) == 2


def test_fix_format_clears_notes_then_repairs_the_structure():
    lines = ["Ep 7", "Part 1: Open", "[VO PART_1_NARRATOR]", "A quiet morning.", "---",
             "[BUTLER 00:00:05-00:00:20]", "“We went out at four.”", "He came back alone."]
    docs = _Docs(_doc(lines, named={"pbnote:old": (1, 6)}))
    changes = gdocs.fix_format(None, "doc1", docs=docs)
    assert changes
    assert len(docs.batches) == 2
    assert "deleteContentRange" in docs.batches[0]["requests"][0]
    assert docs.batches[1]["writeControl"] == {"requiredRevisionId": "r1"}
    fixed = _applied(lines, docs.batches[1]["requests"])
    assert "[PART Part 1: Open]" in fixed and "\n---\n" not in fixed
    # Nothing the narrator or a speaker says has moved or changed.
    for words in lines[3:4] + lines[6:]:
        assert words in fixed
    assert not [f for f in run_checks(fixed) if f.severity == ERROR]


PARTS = ["Ep 7", "", "[PART Open]", "[VO PART_0_NARRATOR]", "A quiet morning.", "",
         "[PART Part 1: The Lake]", "[VO PART_1_NARRATOR]", "The water was still.", "", ""]
PULL = "[BUTLER 00:00:05-00:00:20]\nWe went out at four.\n"


def test_doc_parts_finds_headers_and_draft_section_lines():
    assert gdocs.doc_parts(_doc(PARTS)) == ["Open", "Part 1: The Lake"]
    assert gdocs.doc_parts(_doc(["Ep 7", "Intro", "Words.", "Part 1: The Lake", "More."])) == [
        "Intro", "Part 1: The Lake"]
    assert gdocs.doc_parts(_doc(["Episode 9", "A paragraph read aloud."])) == []


def test_a_title_line_under_a_header_is_the_same_section():
    lines = ["Ep 7", "[PART Part 1]", "Part 1: The Shots", "[VO A]", "Words.", "",
             "[PART Part 2]", "Part 2: The Fires", "[VO B]", "More."]
    assert gdocs.doc_parts(_doc(lines)) == ["Part 1: The Shots", "Part 2: The Fires"]
    fixed = _applied(lines, gdocs.plan_append_to_part(_doc(lines), PULL, "Part 1: The Shots"))
    assert fixed.index("Words.") < fixed.index("We went out") < fixed.index("[PART Part 2]")
    # Same name before and after Fix Format, so a send isn't lost to it.
    import script_normalize
    new, _changes = script_normalize.normalize("\n".join(lines))
    assert gdocs.doc_parts(_doc(new.rstrip("\n").split("\n"))) == gdocs.doc_parts(_doc(lines))
    # A plain number takes the header's name, as Fix Format does.
    assert gdocs.doc_parts(_doc(["[PART Bill]", "Part 1", "[VO A]", "Words."])) == ["Part 1: Bill"]
    # The title under the VO header, as many real scripts have it: same section,
    # before and after Fix Format, and the pull lands after the section's text.
    lines = ["Ep 7", "[PART Part 1]", "[VO A]", "Part 1: The Shots", "Words.", "",
             "[PART Part 2]", "[VO B]", "More."]
    assert gdocs.doc_parts(_doc(lines)) == ["Part 1: The Shots", "Part 2"]
    new, _changes = script_normalize.normalize("\n".join(lines))
    assert gdocs.doc_parts(_doc(new.rstrip("\n").split("\n"))) == gdocs.doc_parts(_doc(lines))
    fixed = _applied(lines, gdocs.plan_append_to_part(_doc(lines), PULL, "Part 1: The Shots"))
    assert fixed.index("Words.") < fixed.index("We went out") < fixed.index("[PART Part 2]")
    # Different names under one another are two sections, even if one is empty.
    assert gdocs.doc_parts(_doc(["[PART Open]", "[VO A]", "[PART Close]", "[VO B]", "More."])) == [
        "Open", "Close"]


def test_pull_lands_at_the_end_of_its_section():
    reqs = gdocs.plan_append_to_part(_doc(PARTS), PULL, "Open")
    assert _applied(PARTS, reqs).split("\n")[:10] == [
        "Ep 7", "", "[PART Open]", "[VO PART_0_NARRATOR]", "A quiet morning.", "",
        "[BUTLER 00:00:05-00:00:20]", "We went out at four.", "", "[PART Part 1: The Lake]"]


def test_pull_in_the_last_section_stays_inside_the_doc():
    reqs = gdocs.plan_append_to_part(_doc(PARTS), PULL, "Part 1: The Lake")
    assert _applied(PARTS, reqs).split("\n")[8:12] == [
        "The water was still.", "", "[BUTLER 00:00:05-00:00:20]", "We went out at four."]
    no_blank_end = ["[PART Open]", "[VO A]", "Last."]
    reqs = gdocs.plan_append_to_part(_doc(no_blank_end), PULL, "Open")
    assert _applied(no_blank_end, reqs).endswith(
        "Last.\n\n[BUTLER 00:00:05-00:00:20]\nWe went out at four.\n")


def test_pull_in_an_empty_section_goes_under_its_header():
    lines = ["[PART Open]", "", "[PART Next]", "[VO A]", "Words."]
    reqs = gdocs.plan_append_to_part(_doc(lines), PULL, "Open")
    assert _applied(lines, reqs).startswith("[PART Open]\n\n[BUTLER 00:00:05-00:00:20]")


def test_pull_stays_above_a_divider_and_out_of_a_note():
    lines = ["Ep 7", "Intro", "Words.", "---", "Part 1", "More."]
    reqs = gdocs.plan_append_to_part(_doc(lines), PULL, "Intro")
    assert _applied(lines, reqs).index("We went out") < _applied(lines, reqs).index("---")

    note = gdocs.NOTE_PREFIX + "WARN Format: old"
    lines = ["[PART Open]", "[VO A]", "Words.", note, "", "[PART Next]", "[VO B]", "More."]
    para = gdocs.paragraphs(_doc(lines))
    reqs = gdocs.plan_append_to_part(_doc(lines), PULL, "Open")
    # Inside the line above the note, so the note's named range doesn't grow.
    assert reqs[0]["insertText"]["location"]["index"] == para[2][2] - 1


def test_a_section_that_is_gone_is_an_error_not_a_guess():
    assert gdocs.plan_append_to_part(_doc(PARTS), PULL, "No such part") is None
    with pytest.raises(RuntimeError, match="No such part"):
        gdocs.append_to_part(None, "doc1", PULL, "No such part", docs=_Docs(_doc(PARTS)))


def test_append_to_part_redoes_the_plan_if_the_doc_changed():
    docs = _Docs(_doc(PARTS))
    send = docs.batchUpdate
    tries = []

    def _batch(documentId, body):
        tries.append(body)
        if len(tries) == 1:
            def _fail():
                raise RuntimeError("The required revision ID does not match")
            return _Exec(_fail)
        return send(documentId, body)
    docs.batchUpdate = _batch
    gdocs.append_to_part(None, "doc1", PULL, "Open", docs=docs)
    assert len(tries) == 2 and len(docs.batches) == 1
    assert docs.batches[0]["writeControl"] == {"requiredRevisionId": "r1"}
    assert gdocs.list_parts(None, "doc1", docs=docs) == ["Open", "Part 1: The Lake"]


def test_append_after_marker_or_at_end():
    doc = _doc(SCRIPT)
    reqs = gdocs.plan_append(doc, "[BUTLER 00:00:30-00:00:40]\nAnd then.\n", after_marker="[PART Open]")
    paras = {t: e for t, _s, e in gdocs.paragraphs(doc)}
    assert reqs == [{"insertText": {"location": {"index": paras["[PART Open]"]},
                                    "text": "[BUTLER 00:00:30-00:00:40]\nAnd then.\n"}}]
    reqs = gdocs.plan_append(doc, "[BUTLER 00:00:30-00:00:40]\nAnd then.")
    assert "endOfSegmentLocation" in reqs[0]["insertText"]
    assert reqs[0]["insertText"]["text"].startswith("\n[BUTLER")


class _Docs:
    """Fake Docs service: serves a document, records batchUpdates."""
    def __init__(self, doc):
        self.doc = doc
        self.batches = []

    def documents(self):
        return self

    def get(self, documentId):
        return _Exec(lambda: self.doc)

    def batchUpdate(self, documentId, body):
        self.batches.append(body)
        return _Exec(lambda: {})


class _Exec:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


def test_write_notes_pins_the_revision_and_clears_first():
    docs = _Docs(_doc(SCRIPT, named={"pbnote:old": (1, 6)}))
    placed, unplaced = gdocs.write_notes(None, "doc1", [("[PART Open]", "WARN: x")], docs=docs)
    assert (placed, unplaced) == (1, [])
    assert len(docs.batches) == 2
    assert docs.batches[0]["writeControl"] == {"requiredRevisionId": "r1"}
    assert "deleteContentRange" in docs.batches[0]["requests"][0]
    assert "insertText" in docs.batches[1]["requests"][0]


def test_fetch_text_normalizes_and_returns_meta():
    class _Drive:
        def files(self):
            return self

        def export(self, fileId, mimeType):
            assert mimeType == "text/plain"
            return _Exec(lambda: b"\xef\xbb\xbf[PART Open]\r\nhello\r\n")

        def get(self, fileId, fields):
            return _Exec(lambda: {"name": "Ep 7 script", "modifiedTime": "t", "version": "9"})
    text, meta = gdocs.fetch_text(None, "doc1", drive=_Drive())
    assert text == "[PART Open]\nhello\n" and meta["version"] == "9"


def test_diff_pulls_counts_changes():
    old = "[PART A]\n[BUTLER 00:00:05-00:00:20]\nWe went out.\n[BUTLER 00:01:00-00:01:10]\nLater.\n"
    new = "[PART A]\n[BUTLER 00:00:05-00:00:20]\nWe went out early.\n[JENA 00:02:00-00:02:10]\nNew.\n"
    assert gdocs.diff_pulls(old, new) == (1, 1, 1)
