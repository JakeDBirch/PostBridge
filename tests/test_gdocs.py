"""Google Docs bridge: the planning of edits, without a network.

A Docs API document is a list of paragraphs with character ranges; notes
are inserted right under the line they concern, highest index first so
nothing shifts, each under a named range so the next pass can remove it.
"""
import json
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


def test_pull_goes_where_the_mark_is():
    """Jordan: the pull should come in where the cursor is.  The API cannot
    see a cursor, so an @@ typed in the Doc is the spot."""
    lines = ["[PART Open]", "First words.", "@@", "Last words.", ""]
    doc = _doc(lines)
    assert gdocs.has_mark(doc) and not gdocs.has_mark(_doc(PARTS))
    assert gdocs.plan_insert_at_mark(_doc(PARTS), PULL) is None
    fixed = _applied(lines, gdocs.plan_insert_at_mark(doc, PULL))
    assert fixed == "\n".join(["[PART Open]", "First words."] + PULL.strip("\n").split("\n")
                              + ["Last words.", ""]) + "\n"


def test_a_mark_inside_a_line_splits_it_around_the_pull():
    lines = ["Before \U0001F3A3 @@ after.", "@@"]
    fixed = _applied_u16(lines, gdocs.plan_insert_at_mark(_doc16(lines), PULL))
    assert fixed == "Before \U0001F3A3 \n" + PULL.strip("\n") + "\n after.\n@@\n"


def test_a_mark_in_a_postbridge_note_is_not_one():
    assert not gdocs.has_mark(_doc([gdocs.NOTE_PREFIX + "type @@ here"]))


def _doc16(lines):
    """_doc with indices in UTF-16 units, as the Docs API counts them."""
    content, idx = [], 1
    for ln in lines:
        txt = ln + "\n"
        n = len(txt.encode("utf-16-le")) // 2
        content.append({"startIndex": idx, "endIndex": idx + n, "paragraph": {"elements": [
            {"startIndex": idx, "endIndex": idx + n, "textRun": {"content": txt}}]}})
        idx += n
    return {"revisionId": "r1", "body": {"content": content}}


def _applied_u16(lines, reqs):
    raw = ("\n".join(lines) + "\n").encode("utf-16-le")
    for r in reqs:
        if "deleteContentRange" in r:
            rg = r["deleteContentRange"]["range"]
            raw = raw[:(rg["startIndex"] - 1) * 2] + raw[(rg["endIndex"] - 1) * 2:]
        else:
            i = (r["insertText"]["location"]["index"] - 1) * 2
            raw = raw[:i] + r["insertText"]["text"].encode("utf-16-le") + raw[i:]
    return raw.decode("utf-16-le")


def test_insert_at_mark_redoes_the_plan_and_fails_without_a_mark():
    docs = _Docs(_doc(["[PART Open]", "@@"]))
    gdocs.insert_at_mark(None, "doc1", PULL, docs=docs)
    assert docs.batches[0]["writeControl"] == {"requiredRevisionId": "r1"}
    assert gdocs.send_targets(None, "doc1", docs=docs) == {"mark": True, "parts": ["Open"]}
    with pytest.raises(RuntimeError, match="no longer has"):
        gdocs.insert_at_mark(None, "doc1", PULL, docs=_Docs(_doc(PARTS)))


def test_a_pull_for_the_cursor_waits_at_the_end_under_a_named_range():
    lines = ["[PART Open]", "Words.", ""]
    doc = _doc(lines)
    reqs = gdocs.plan_park(doc, PULL)
    assert _applied(lines, [reqs[0]]) == "\n".join(lines) + "\n" + PULL.strip("\n") + "\n"
    rng = reqs[1]["createNamedRange"]
    assert rng["name"] == gdocs.PENDING_RANGE
    whole = "\n".join(lines) + "\n" + PULL.strip("\n") + "\n"
    assert whole[rng["range"]["startIndex"] - 1:rng["range"]["endIndex"] - 1] == PULL.strip("\n")

    docs = _Docs(doc)
    assert gdocs.park_pull(None, "doc1", PULL, docs=docs) == 0
    assert docs.batches[0]["writeControl"] == {"requiredRevisionId": "r1"}
    waiting = _doc(lines, named={gdocs.PENDING_RANGE: (1, 5)})
    assert gdocs.park_pull(None, "doc1", PULL, docs=_Docs(waiting)) == 1
    # A waiting pull is not one of PostBridge's notes: clearing notes leaves it.
    assert gdocs.existing_notes(waiting) == []


def _in_doc(scenario):
    """What the script that runs inside the Doc does with *scenario*."""
    import json, shutil, subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run([node, os.path.join(ROOT, "tests", "gdocs_cursor_mock.js"),
                          os.path.join(ROOT, gdocs.CURSOR_SCRIPT), json.dumps(scenario)],
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


DOC = ["[PART Open]", "First.", "", "Last.", ""]


def test_in_doc_script_moves_the_pull_to_an_empty_line_at_the_cursor():
    r = _in_doc({"lines": DOC + ["[A 00:00:01-00:00:02]", "Words."], "pending": [[5, 6]], "cursor": 2})
    assert r["lines"] == ["[PART Open]", "First.", "[A 00:00:01-00:00:02]", "Words.", "Last.", ""]
    assert r["result"]["placed"] == ["[A 00:00:01-00:00:02]"] and r["ranges"] == 0
    assert r["cursor"] == 3                      # on the pull's last line, for the next one


def test_in_doc_script_puts_the_pull_below_a_line_with_words():
    r = _in_doc({"lines": DOC + ["[A 1]", "Words."], "pending": [[5, 6]], "cursor": 1})
    assert r["lines"] == ["[PART Open]", "First.", "", "[A 1]", "Words.", "", "Last.", ""]


def test_in_doc_script_keeps_the_order_pulls_were_sent_in():
    r = _in_doc({"lines": DOC + ["[A 1]", "One.", "", "[B 2]", "Two."],
                 "pending": [[5, 6], [8, 9]], "cursor": 2})
    assert r["lines"] == ["[PART Open]", "First.", "[A 1]", "One.", "", "[B 2]", "Two.",
                          "Last.", "", ""]
    assert r["result"]["placed"] == ["[A 1]", "[B 2]"]


def test_in_doc_script_waits_for_a_cursor_and_leaves_a_pull_the_cursor_is_in():
    lines = DOC + ["[A 1]", "Words."]
    r = _in_doc({"lines": lines, "pending": [[5, 6]], "cursor": None})
    assert r["lines"] == lines and r["ranges"] == 1
    assert r["result"]["waiting"] == 1 and "Click in the Doc" in r["result"]["note"]
    r = _in_doc({"lines": lines, "pending": [[5, 6]], "cursor": 6})
    assert r["lines"] == lines and r["ranges"] == 0 and r["result"]["placed"] == ["[A 1]"]
    r = _in_doc({"lines": lines, "pending": [[5, 6]], "cursor": 2, "release": True})
    assert r["lines"] == lines and r["ranges"] == 0 and r["result"] == {"released": 1}


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, str(data)

    def json(self):
        return self._data


class _Session:
    """Fake Apps Script API: answers from a list, records the calls."""
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def request(self, method, url, json=None, timeout=None):
        self.calls.append((method, url.replace(gdocs.SCRIPT_API, ""), json))
        return self.answers.pop(0)


def test_install_makes_the_script_inside_the_doc():
    s = _Session([_Resp(200, {"scriptId": "S1"}), _Resp(200, {})])
    assert gdocs.install_cursor_script(None, "doc1", session=s) == "S1"
    assert s.calls[0] == ("POST", "", {"title": "PostBridge", "parentId": "doc1"})
    method, url, body = s.calls[1]
    assert (method, url) == ("PUT", "/S1/content")
    files = {f["name"]: f for f in body["files"]}
    assert "function pbPlacePending" in files["Code"]["source"]
    assert files["Code"]["type"] == "SERVER_JS" and files["appsscript"]["type"] == "JSON"
    assert "oauthScopes" not in files["appsscript"]["source"]


def test_install_updates_a_past_install_and_remakes_one_that_is_gone():
    s = _Session([_Resp(200, {})])
    assert gdocs.install_cursor_script(None, "doc1", "S1", session=s) == "S1"
    assert [c[:2] for c in s.calls] == [("PUT", "/S1/content")]
    gone = {"error": {"message": "Requested entity was not found."}}
    s = _Session([_Resp(404, gone), _Resp(200, {"scriptId": "S2"}), _Resp(200, {})])
    assert gdocs.install_cursor_script(None, "doc1", "S1", session=s) == "S2"


def test_install_says_when_the_apps_script_api_is_off():
    off = {"error": {"message": "User has not enabled the Apps Script API. Enable it by "
                                "visiting https://script.google.com/home/usersettings then retry."}}
    with pytest.raises(gdocs.ScriptApiOff):
        gdocs.install_cursor_script(None, "doc1", session=_Session([_Resp(403, off)]))
    no = {"error": {"message": "The caller does not have permission"}}
    with pytest.raises(RuntimeError, match="does not have permission"):
        gdocs.install_cursor_script(None, "doc1", session=_Session([_Resp(403, no)]))


def test_a_sign_in_without_the_script_permission_is_done_again(tmp_path, monkeypatch):
    """The usual sign-in holds drive.file only.  It keeps serving everything
    else untouched, and only the install asks Google again."""
    import json
    tok = tmp_path / "google_token.json"
    tok.write_text(json.dumps({"token": "t", "refresh_token": "r", "client_id": "c",
                               "client_secret": "s", "expiry": "2099-01-01T00:00:00Z",
                               "scopes": gdocs.SCOPES}))
    monkeypatch.setattr(gdocs, "token_path", lambda: str(tok))
    monkeypatch.setattr(gdocs, "is_configured", lambda: True)
    assert gdocs._granted_scopes() == gdocs.SCOPES
    assert gdocs.get_credentials(interactive=False, extra=[gdocs.SCRIPT_SCOPE]) is None
    tok.write_text(json.dumps({"token": "t", "refresh_token": "r", "client_id": "c",
                               "client_secret": "s", "expiry": "2099-01-01T00:00:00Z",
                               "scopes": gdocs.SCOPES + [gdocs.SCRIPT_SCOPE]}))
    creds = gdocs.get_credentials(interactive=False, extra=[gdocs.SCRIPT_SCOPE])
    assert creds is not None and gdocs.SCRIPT_SCOPE in creds.scopes
    assert gdocs.get_credentials(interactive=False) is not None      # and for everything else


CLIENT = {"installed": {"client_id": "123-abc.apps.googleusercontent.com",
                        "client_secret": "s", "redirect_uris": ["http://localhost"]}}
KEY = "AIza" + "B" * 35


@pytest.fixture
def setup_dirs(tmp_path, monkeypatch):
    """An empty app-data folder, and no bundled or source copies."""
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    monkeypatch.setattr(gdocs, "app_state_dir", lambda: str(appdata))
    monkeypatch.setattr(gdocs, "_candidates", lambda name: [str(appdata / name)])
    return tmp_path, appdata


def test_install_setup_checks_and_copies_both_files(setup_dirs):
    tmp, appdata = setup_dirs
    assert not gdocs.is_ready()
    client, key = tmp / "client_secret_123.json", tmp / "key.txt"
    client.write_text(json.dumps(CLIENT), encoding="utf-8-sig")     # a BOM is fine
    key.write_text(KEY + "\n", encoding="utf-8")
    gdocs.install_setup(str(client), str(key))
    assert gdocs.is_ready()
    assert json.loads((appdata / gdocs.CLIENT_FILE).read_text(encoding="utf-8")) == CLIENT
    assert gdocs.picker_key() == KEY
    assert gdocs.project_number() == "123"


@pytest.mark.parametrize("content,why", [
    ("not json", "isn't JSON"),
    (json.dumps({"web": CLIENT["installed"]}), "Desktop app"),
    (json.dumps({"installed": {"client_id": "x"}}), "isn't a Google OAuth client"),
])
def test_install_setup_refuses_a_wrong_client_file(setup_dirs, content, why):
    tmp, appdata = setup_dirs
    bad = tmp / "c.json"
    bad.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=why):
        gdocs.install_setup(str(bad), None)
    assert not list(appdata.iterdir())


def test_install_setup_refuses_a_wrong_key_and_copies_nothing(setup_dirs):
    tmp, appdata = setup_dirs
    client, key = tmp / "c.json", tmp / "k.txt"
    client.write_text(json.dumps(CLIENT), encoding="utf-8")
    key.write_text("not a key", encoding="utf-8")
    with pytest.raises(ValueError, match="AIza"):
        gdocs.install_setup(str(client), str(key))
    assert not list(appdata.iterdir())


def test_an_installed_copy_is_found_in_the_bundle(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / gdocs.CLIENT_FILE).write_text(json.dumps(CLIENT), encoding="utf-8")
    (bundle / gdocs.PICKER_FILE).write_text(KEY, encoding="utf-8")
    monkeypatch.setattr(gdocs, "app_state_dir", lambda: str(tmp_path / "empty"))
    monkeypatch.setattr(gdocs.sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(gdocs.os.path, "abspath", lambda p: str(tmp_path / "src" / "gdocs.py"))
    assert gdocs.client_config_path() == str(bundle / gdocs.CLIENT_FILE)
    assert gdocs.picker_key() == KEY


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

    def get(self, documentId, **kw):
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


def _tabbed(**tabs):
    """A Docs API reply with includeTabsContent: one tab per keyword, in
    order, each holding those lines; tab ids are t.<name>."""
    out = []
    for name, lines in tabs.items():
        d = _doc(lines)
        out.append({"tabProperties": {"tabId": "t." + name, "title": name.title()},
                    "documentTab": {"body": d["body"], "namedRanges": d.get("namedRanges", {})}})
    return {"revisionId": "r9", "tabs": out}


TABS = dict(script=["Ep 11", "[PART Open]", "[VO A]", "Words."],
            notes=["Production notes", "Call the archive."],
            dramatizations=["SFX: hooves"])


class _Drive:
    """Fake Drive: files.get only works when asked for all drives, as for a
    Doc on a shared drive."""
    def __init__(self):
        self.asked = []

    def files(self):
        return self

    def export(self, fileId, mimeType):
        raise AssertionError("the export runs every tab together; it must not be used")

    def get(self, fileId, fields, supportsAllDrives=False):
        self.asked.append(supportsAllDrives)
        if not supportsAllDrives:
            raise RuntimeError("HttpError 404: File not found")
        return _Exec(lambda: {"name": "Ep 11", "modifiedTime": "t", "version": "9"})


def test_fetch_reads_one_tab_and_finds_a_shared_drive_doc():
    drive, docs = _Drive(), _Docs(_tabbed(**TABS))
    text, meta = gdocs.fetch_text(None, "doc1", tab="t.script", drive=drive, docs=docs)
    assert text == "Ep 11\n[PART Open]\n[VO A]\nWords.\n"
    assert meta["version"] == "9" and drive.asked == [True]
    # Another tab, chosen: only that tab.
    text, _meta = gdocs.fetch_text(None, "doc1", tab="t.notes", drive=drive, docs=docs)
    assert text == "Production notes\nCall the archive.\n"
    # No tab chosen, or one since deleted: the first tab.
    for tab in (None, "t.gone"):
        assert gdocs.fetch_text(None, "doc1", tab=tab, drive=drive, docs=docs)[0].startswith("Ep 11")


def test_list_tabs_includes_child_tabs():
    d = _tabbed(**TABS)
    d["tabs"][0]["childTabs"] = [{"tabProperties": {"tabId": "t.alt", "title": "Alt take"},
                                  "documentTab": {"body": {"content": []}}}]
    assert gdocs.list_tabs(None, "doc1", docs=_Docs(d)) == [
        ("t.script", "Script"), ("t.alt", "Script › Alt take"),
        ("t.notes", "Notes"), ("t.dramatizations", "Dramatizations")]


def test_writes_go_to_the_chosen_tab():
    docs = _Docs(_tabbed(notes=TABS["notes"], script=TABS["script"]))
    gdocs.write_notes(None, "doc1", [("[PART Open]", "WARN: x")], tab="t.script", docs=docs)
    gdocs.append_to_part(None, "doc1", "[BUTLER 00:00:05-00:00:20]\nWe went out.", "Open",
                         tab="t.script", docs=docs)
    gdocs.append_block(None, "doc1", "[BUTLER 00:00:30-00:00:40]\nLater.", tab="t.script", docs=docs)
    reqs = [r for b in docs.batches for r in b["requests"]]
    assert reqs
    for r in reqs:
        for v in r.values():
            for key in ("location", "range", "endOfSegmentLocation"):
                if key in v:
                    assert v[key]["tabId"] == "t.script", r
    # Indexes are planned on the script tab, not the first one.
    script = gdocs.paragraphs(_doc(TABS["script"]))
    note = next(r["insertText"] for r in docs.batches[0]["requests"] if "insertText" in r)
    assert note["location"]["index"] == script[1][2]
    assert gdocs.list_parts(None, "doc1", tab="t.script", docs=docs) == ["Open"]
    assert gdocs.list_parts(None, "doc1", tab="t.notes", docs=docs) == []


def test_diff_pulls_counts_changes():
    old = "[PART A]\n[BUTLER 00:00:05-00:00:20]\nWe went out.\n[BUTLER 00:01:00-00:01:10]\nLater.\n"
    new = "[PART A]\n[BUTLER 00:00:05-00:00:20]\nWe went out early.\n[JENA 00:02:00-00:02:10]\nNew.\n"
    assert gdocs.diff_pulls(old, new) == (1, 1, 1)
