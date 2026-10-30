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
