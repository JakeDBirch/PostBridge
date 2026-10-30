"""Monolog: a plain narration script cut from a raw single-voice read.

The reads here are synthetic word lists (no audio, so edges fall back to
Whisper's word times).  Each test is one thing a narrator does to a raw
read -- a false start, a restart, a pickup, a reworded line, a stumble,
talk between takes -- and checks which words the cut keeps.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import monolog  # noqa: E402
import parsers  # noqa: E402

WORD_S, GAP_S = 0.3, 0.1


class Read(object):
    """A take built up phrase by phrase: say() appends words, pause() silence."""

    def __init__(self):
        self.words, self.t = [], 0.0

    def say(self, text):
        for w in text.lower().replace(",", "").replace(".", "").split():
            self.words.append({"word": w, "start": round(self.t, 3),
                               "end": round(self.t + WORD_S, 3)})
            self.t += WORD_S + GAP_S
        return self

    def pause(self, s):
        self.t += s
        return self


def _blocks(*paras):
    text = "Title\n" + "\n".join(paras)
    return parsers.parse_plain_script(text)[3]


def _kept(results, read, apath="take1.wav"):
    """The words of `read` inside the cut, in cut order."""
    out = []
    for r in results:
        if r.get("source_audio") != apath:
            continue
        for a, b in r.get("segments") or []:
            out += [w["word"] for w in read.words
                    if a <= (w["start"] + w["end"]) / 2 <= b]
    return " ".join(out)


def _run(blocks, *reads):
    takes = [(rd.words, 0.0, None, "take{}.wav".format(k + 1), None)
             for k, rd in enumerate(reads)]
    return monolog.reconcile(blocks, takes)


P1 = ("The first herds rumbled in during the spring. The Texans were amazed "
      "to find a hotel waiting for them.")
P2 = ("By the next year the town had become a boomtown with bars and dance "
      "halls on every corner.")


# ── The plain script ──────────────────────────────────────────────────────────
def test_plain_script_paragraphs_and_doc_comments_no_parts():
    text = ("John Wesley Hardin vs Wild Bill Hickok\n"
            "First paragraph, read aloud.\n"
            "Second paragraph that wraps\n"
            "onto a second line.\n"
            "________________\n\n\n"
            "Third paragraph with a comment anchor gasped[a]. The end.\n\n"
            "[a]SFX not VO\n")
    assert parsers.is_plain_script(text)
    tokens, parts, pulls, blocks, title, warnings = parsers.parse_plain_script(text)
    assert title == "John Wesley Hardin vs Wild Bill Hickok"
    assert [b["text"] for b in blocks] == [
        "First paragraph, read aloud.",
        "Second paragraph that wraps onto a second line.",
        "Third paragraph with a comment anchor gasped. The end."]
    # The underscore rule only ends a paragraph: no parts, so the export
    # carries no PARTS markers.
    assert parts == [] and {b["part_index"] for b in blocks} == {1}
    assert all(b["plain"] for b in blocks) and not pulls and not warnings


def test_bracketed_script_is_not_plain():
    assert not parsers.is_plain_script("Title\n[VO PART_1]\nSome words.\n")


# ── The cut ───────────────────────────────────────────────────────────────────
def test_false_start_and_talk_are_cut_last_full_read_kept():
    rd = (Read().say("the first herds rumbled").pause(1.5)
          .say("hang on let me get some water okay we are rolling").pause(3)
          .say(P1).pause(0.8).say(P2).pause(2)
          .say("yeah that was good i think we got it"))
    res = _run(_blocks(P1, P2), rd)
    assert [r["status"] for r in res] == ["ok"]
    kept = _kept(res, rd)
    assert kept == " ".join(w["word"] for w in Read().say(P1).say(P2).words)
    assert "water" not in kept and "good" not in kept


def test_a_read_straight_across_a_paragraph_is_one_region():
    """Nothing was taken out at the paragraph break, so nothing marks it."""
    rd = Read().say(P1).pause(0.8).say(P2)
    res = _run(_blocks(P1, P2), rd)
    assert len(res) == 1 and len(res[0]["segments"]) == 1
    assert res[0]["quote_text"] == P1 + "\n" + P2
    assert res[0]["status"] == "ok" and res[0]["n_internal_cuts"] == 0


def test_a_retake_across_a_paragraph_still_starts_a_new_pull():
    rd = (Read().say(P1).pause(0.8)
          .say("by the next year the town had become").pause(7)
          .say("okay again").pause(3).say(P2))
    res = _run(_blocks(P1, P2), rd)
    assert _kept(res, rd) == " ".join(w["word"] for w in Read().say(P1).say(P2).words)
    assert sum(len(r["segments"]) for r in res) == 2


def test_a_line_reworded_as_it_was_read_is_not_cut_out():
    """Ep 10: script "a memoir from 1912. Siringo claimed", read "a memoir he
    published years later in nineteen twelve. Siringo claimed".  Cutting the
    rewording left "a memoir he Siringo claimed"."""
    para = ("The second version comes from the famed range detective Charles "
            "Siringo in a memoir from that year. Siringo claimed he was one of the "
            "Texans who had been recruited to provide backup.")
    rd = (Read().say("the second version comes from the famed range detective "
                     "charles siringo in a memoir he published years later in "
                     "nineteen twelve").pause(1.8)
          .say("siringo claimed he was one of the texans who had been recruited "
               "to provide backup"))
    res = _run(_blocks(para), rd)
    assert sum(len(r["segments"]) for r in res) == 1
    assert "he published years later in nineteen twelve siringo claimed" in _kept(res, rd)


def test_a_few_words_added_mid_sentence_stay_in():
    """Ep 10: script "because he was like this super evil guy", read "because
    Clay Allison was this super evil guy": the cut left "because Clay this"."""
    para = ("There was an argument he was sure and it is not because he was like "
            "this super evil guy either what clay allison was was unpredictable.")
    rd = Read().say("there was an argument he was sure and it is not because clay "
                    "allison was this super evil guy either what clay allison was "
                    "was unpredictable")
    res = _run(_blocks(para), rd)
    assert sum(len(r["segments"]) for r in res) == 1
    assert "because clay allison was this super evil" in _kept(res, rd)


def test_a_retake_right_beside_it_is_still_cut():
    """Ep 10: "[That fall, he complained,] that fall, he complained to the mayor"."""
    para = ("Kennedy did not take this well. That fall he complained to the mayor "
            "of Dodge City who defended the deputy and warned him to behave.")
    rd = Read().say("kennedy did not take this well that fall he complained that fall "
                    "he complained to the mayor of dodge city who defended the deputy "
                    "and warned him to behave")
    assert _kept(_run(_blocks(para), rd), rd) == (
        "kennedy did not take this well that fall he complained to the mayor of "
        "dodge city who defended the deputy and warned him to behave")


def test_talk_after_a_stop_stays_out_however_short():
    """Words the narrator stopped to say are not a rewording."""
    para = ("The first herds rumbled in during the spring and the Texans were "
            "amazed to find a hotel waiting for them that first long season.")
    rd = (Read().say("the first herds rumbled in during the spring").pause(2)
          .say("sorry give me a second").pause(2)
          .say("and the texans were amazed to find a hotel waiting for them that "
               "first long season"))
    kept = _kept(_run(_blocks(para), rd), rd)
    assert "sorry" not in kept and "second" not in kept


def _heal_case(*pieces_at):
    """Pieces (script a, b, tape i_a, i_b) of one take of twenty words, as
    _pieces would hand them to _heal, in script order."""
    rd = Read().say("the first herds rumbled during spring while texans watched "
                    "hotels rising beside dusty rail yards every morning until summer came")
    words = [{"word": w["word"], "start": w["start"], "end": w["end"]} for w in rd.words]
    R = monolog._Read([{"words": words}])
    pieces = [monolog._Piece(0, a, b, i_a, i_b) for a, b, i_a, i_b in pieces_at]
    dels = [type("D", (), {"take": 0})()]
    monolog._heal(pieces, dels, R)
    return pieces


def test_short_tape_between_two_stretches_goes_back():
    first, second = _heal_case((0, 5, 0, 5), (6, 13, 12, 19))
    assert first.i_b == 11 and second.joined


def test_tape_a_pickup_plays_is_not_put_back_as_well():
    """Ep 10: a later line's reading sat in the gap, so "five of them decided"
    went out twice, 3.67 s of it."""
    first, second, pickup = _heal_case((0, 5, 0, 5), (6, 13, 12, 19), (14, 16, 7, 9))
    assert first.i_b == 5 and not second.joined


def test_only_a_long_pause_inside_a_reading_is_trimmed():
    para = ("The first herds rumbled in during the spring and the Texans were "
            "amazed to find a hotel waiting for them that first long season.")
    def _read(pause):
        return (Read().say("the first herds rumbled in during the spring").pause(pause)
                .say("and the texans were amazed to find a hotel waiting for them "
                     "that first long season"))
    assert len(_run(_blocks(para), _read(3.0))[0]["segments"]) == 1
    assert len(_run(_blocks(para), _read(5.0))[0]["segments"]) == 2


def test_restart_mid_paragraph_stitches_the_last_delivery():
    rd = (Read().say("the first herds rumbled in during the spring the texans were "
                     "amazed to fond").pause(2)
          .say("sorry").pause(2)
          .say("the texans were amazed to find a hotel waiting for them"))
    res = _run(_blocks(P1), rd)
    kept = _kept(res, rd)
    assert kept == ("the first herds rumbled in during the spring "
                    "the texans were amazed to find a hotel waiting for them")
    assert "fond" not in kept and "sorry" not in kept


def test_pickup_after_the_paragraph_replaces_its_line():
    para = ("He was the most famous lawman in the West. But Hickok was the first. "
            "To my mind the most impressive thing is that he grew into the legend.")
    rd = (Read().say("he was the most famous lawman in the west but hickok was "
                     "first to my mind the most impressive thing is that he grew "
                     "into the legend").pause(3)
          .say("let me do that one line").pause(2)
          .say("but hickok was the first"))
    res = _run(_blocks(para), rd)
    # The pickup lands in place: one pull up to and including it, then the
    # rest of the paragraph from the first reading.
    assert len(res) == 2
    assert _kept(res, rd) == ("he was the most famous lawman in the west "
                              "but hickok was the first "
                              "to my mind the most impressive thing is that he "
                              "grew into the legend")


def test_reworded_ending_is_kept_with_the_sentence():
    para = "Hardin had spent barely two months in Kansas and killed seven or possibly eight men."
    rd = (Read().say("hardin had spent barely two months in kansas and killed "
                     "depending on how you count it seven or maybe eight men")
          .pause(4).say("yeah"))
    res = _run(_blocks(para), rd)
    assert _kept(res, rd).endswith("killed depending on how you count it seven "
                                   "or maybe eight men")


def test_stumble_read_straight_through_is_cut():
    para = "Hickok shot him in the neck. Both killings, at least according to the coroner, were justified."
    rd = Read().say("hickok shot him in the neck both these killing both these "
                    "killings at least according to the coroner were justified")
    kept = _kept(_run(_blocks(para), rd), rd)
    assert kept == ("hickok shot him in the neck both these killings at least "
                    "according to the coroner were justified")


def test_line_never_read_is_flagged_not_dropped_silently():
    rd = Read().say(P1)
    res = _run(_blocks(P1, P2), rd)
    assert [r["status"] for r in res] == ["ok", "no_match"]
    assert res[1]["quote_text"].startswith("By the next year")
    assert not res[1]["segments"]


def test_later_recording_wins_and_earlier_reading_is_an_alternative():
    first = Read().say(P1).say(P2)
    second = Read().pause(1).say(P2)
    res = _run(_blocks(P1, P2), first, second)
    assert [r["source_audio"] for r in res] == ["take1.wav", "take2.wav"]
    td = res[1]["takes_data"]
    assert len(td) == 2 and {t["apath"] for t in td} == {"take1.wav", "take2.wav"}
    assert td[res[1]["best_take_index"]]["apath"] == "take2.wav"
    # Every pull is numbered in cut order and is a VO row build_aaf can place.
    assert [r["order"] for r in res] == [1, 2]
    assert all(r["is_vo"] and r["monolog"] for r in res)


# ── While the first pass is hearing the read ─────────────────────────────
_P1 = "In the spring of that year the riders came down from the northern hills into town."
_P2 = "Nobody in the valley had seen so many horses since before the long and bitter war."
_P3 = "By the end of summer every one of those men was dead or gone for good and all."


def _past(blocks, read, frontier, landed=(), **kw):
    return monolog.paragraphs_past(blocks, [(read.words, 0.0, None, "take1.wav", None)],
                                   "take1.wav", frontier, set(landed), **kw)


def test_the_script_stands_in_for_the_cut_a_paragraph_a_row():
    rows = monolog.pending_rows(_blocks(_P1, "", _P2, "", _P3))
    assert [r["order"] for r in rows] == [1, 2, 3]
    assert [r["status"] for r in rows] == ["not_run"] * 3
    assert all(r["is_vo"] and r["monolog"] and r["segments"] == [] for r in rows)
    assert rows[0]["quote_text"] == _P1
    # Spans run on along the script, as a cut's do.
    assert rows[1]["script_span"][0] == rows[0]["script_span"][1] + 1


def test_a_paragraph_goes_in_once_the_read_is_past_it():
    blocks = _blocks(_P1, "", _P2, "", _P3)
    rd = Read().say(_P1)
    heard_to = rd.t
    assert _past(blocks, rd, heard_to) == []          # might not be finished
    rd.pause(1.0).say(_P2)
    got = _past(blocks, rd, rd.t)
    assert [r["order"] for r in got] == [1]
    assert got[0]["status"] == "ok" and got[0]["segments"]
    assert got[0]["segments"][-1][1] <= heard_to + monolog.TAIL_S + 0.01
    assert got[0]["quote_text"] == _P1
    assert got[0]["script_span"] == monolog.pending_rows(blocks)[0]["script_span"]
    assert got[0]["best_take_index"] == 0 and len(got[0]["takes_data"]) == 1
    # Nothing goes in twice; the next one comes in its turn.
    rd.pause(1.0).say(_P3)
    assert [r["order"] for r in _past(blocks, rd, rd.t, {1})] == [2]


def test_half_a_paragraph_stays_out_even_with_the_read_gone_on_elsewhere():
    blocks = _blocks(_P1, "", _P2)
    rd = Read().say("in the spring of that year")
    rd.pause(3.0).say("sorry can we stop there a moment and get some water please")
    assert _past(blocks, rd, rd.t + 10.0) == []


def test_a_false_start_inside_a_paragraph_stays_cut():
    blocks = _blocks(_P1, "", _P2)
    rd = Read().say("in the spring of that").pause(1.0).say(_P1).pause(1.0).say(_P2)
    got = _past(blocks, rd, rd.t)
    assert got[0]["order"] == 1
    assert _kept(got, rd).split()[:6] == "in the spring of that year".split()
    assert len(_kept(got, rd).split()) == len(_P1.split())


def test_only_the_next_few_paragraphs_are_looked_for():
    paras = []
    for n in "alpha bravo charlie delta echo foxtrot".split():
        paras += ["The {} patrol rode out at dawn and came back long after dark that day.".format(n), ""]
    blocks = _blocks(*paras)
    rd = Read()
    for p in paras:
        if p:
            rd.say(p).pause(1.0)
    assert [r["order"] for r in _past(blocks, rd, rd.t + 10.0, ahead=2)] == [1, 2]
    assert [r["order"] for r in _past(blocks, rd, rd.t + 10.0, {1, 2}, ahead=2)] == [3, 4]


def test_the_real_cut_keeps_a_paragraph_you_worked_on_and_cuts_round_it():
    blocks = _blocks(_P1, "", _P2)
    rd = Read().say(_P1).pause(1.0).say(_P2).pause(10.0)
    takes = [(rd.words, 0.0, None, "take1.wav", None)]
    first = _past(blocks, rd, rd.t)
    assert first
    worked = dict(first[0], _s4_accepted=True, segments=[(0.5, first[0]["segments"][-1][1])])
    rows = monolog.recut_around(blocks, takes, [worked], lambda r: r.get("_s4_accepted"))
    assert rows[0]["_s4_accepted"] and rows[0]["segments"] == worked["segments"]
    assert rows[0]["order"] == 1
    assert _kept(rows[1:], rd).startswith("nobody in the valley")
