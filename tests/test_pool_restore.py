"""Restoring a saved pool: the rows past the first batch arrive a tick at a
time, so what needs them all waits for them, and a screen that goes away part
way through just stops the restore."""
import tkinter as tk

import pytest

import engines
import gui_components
import main
from gui_components import MediaPool


class _App(tk.Tk):
    _APPLY_SETUP_FIRST_BATCH = main.App._APPLY_SETUP_FIRST_BATCH
    _APPLY_SETUP_BATCH       = main.App._APPLY_SETUP_BATCH
    _apply_setup_data        = main.App._apply_setup_data


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(engines, "_safe_exists", lambda p: True)
    monkeypatch.setattr(gui_components, "HAS_DND", False)
    monkeypatch.setattr(gui_components, "_get_dur_key", lambda p: None)
    try:
        a = _App()
    except tk.TclError as exc:
        pytest.skip("Tk unavailable: {}".format(exc))
    a.withdraw()
    a.errors = []
    a.report_callback_exception = lambda *exc: a.errors.append(exc)
    a._pool = MediaPool(a, ["BUTLER"], [])
    a._pool.pack()
    yield a
    a.destroy()


def _setup(n):
    return {"assignments": {"X:/media/f{:03d}.wav".format(i): "BUTLER"
                            for i in range(n)}}


def _pump(app, ticks=200):
    for _ in range(ticks):
        app.update()


def test_then_waits_for_every_row(app):
    n = app._APPLY_SETUP_FIRST_BATCH + 3 * app._APPLY_SETUP_BATCH
    seen = []
    app._apply_setup_data(_setup(n), then=lambda: seen.append(len(app._pool._rows)))
    assert not seen                            # still streaming
    _pump(app)
    assert seen == [n]
    assert not app._pool._bulk_loading
    assert all(r["var"].get() == "BUTLER" for r in app._pool._rows)
    assert not app.errors


def test_a_short_pool_is_ready_at_once(app):
    seen = []
    app._apply_setup_data(_setup(3), then=lambda: seen.append(len(app._pool._rows)))
    assert seen == [3]


def test_the_screen_going_away_stops_the_restore(app):
    seen = []
    app._apply_setup_data(_setup(60), then=lambda: seen.append(True))
    app._pool.destroy()
    _pump(app)
    assert not seen
    assert not app.errors
