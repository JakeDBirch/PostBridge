"""Step 2's REFRESH: it asks before it changes the pool, and what it adds
comes in unassigned.  It used to add every media file in the pool's folders
and auto-assign each by its name, which flooded a session with tracks the
export then carried, and saved over the pool it had."""
import os
import tkinter as tk

import pytest

import gui_components
import main
from gui_components import MediaPool

UN = "— unassigned —"


class _App(tk.Tk):
    _refresh_pool = main.App._refresh_pool
    workflow = "script_session"


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setattr(gui_components, "HAS_DND", False)
    monkeypatch.setattr(gui_components, "_get_dur_key", lambda p: None)
    try:
        a = _App()
    except tk.TclError as exc:
        pytest.skip("Tk unavailable: {}".format(exc))
    a.withdraw()
    a.asked, a.answer = [], True
    monkeypatch.setattr(main.messagebox, "askyesno",
                        lambda title, msg, **k: (a.asked.append(msg), a.answer)[1])
    monkeypatch.setattr(main.messagebox, "showinfo",
                        lambda title, msg, **k: a.asked.append(msg))
    a._pool = MediaPool(a, ["SCOTT", "CLAY"], [])
    a._pool.pack()
    for n in ("riverside_jordan_0459.wav", "riverside_scott_0460.wav",
              "clay.wav", "scott_old.mp3", "notes.txt"):
        (tmp_path / n).write_bytes(b"x")
    a._pool._add(str(tmp_path / "riverside_jordan_0459.wav"))
    a._pool._rows[-1]["var"].set("SCOTT")
    a._pool._add(str(tmp_path / "gone.wav"))         # not on disk
    a._pool._rows[-1]["var"].set("CLAY")
    a.dir = tmp_path
    yield a
    a.destroy()


def _pool(app):
    return {os.path.basename(r["path"]): r["var"].get() for r in app._pool._rows}


def test_it_asks_and_adds_unassigned(app):
    app._refresh_pool()
    msg = app.asked[-1]
    assert "gone.wav" in msg and "no longer on disk" in msg
    assert "Add 3 files" in msg and "unassigned" in msg
    assert _pool(app) == {"riverside_jordan_0459.wav": "SCOTT",
                          "riverside_scott_0460.wav": UN,
                          "clay.wav": UN, "scott_old.mp3": UN}
    assert not app._pool._auto_assigned


def test_no_leaves_the_pool_as_it_was(app):
    before = _pool(app)
    app.answer = False
    app._refresh_pool()
    assert _pool(app) == before


def test_nothing_new_says_so_and_changes_nothing(app):
    app._pool._remove(next(r for r in app._pool._rows if r["path"].endswith("gone.wav")))
    for n in ("riverside_scott_0460.wav", "clay.wav", "scott_old.mp3"):
        os.remove(str(app.dir / n))
    app._refresh_pool()
    assert "current" in app.asked[-1]
    assert _pool(app) == {"riverside_jordan_0459.wav": "SCOTT"}
