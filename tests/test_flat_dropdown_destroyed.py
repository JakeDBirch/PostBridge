"""A pick that rebuilds the row destroys the dropdown it came from.

Nothing may touch the dead widget afterwards, and its click-outside binding
has to come off the window, or every later click raises in the console.
"""
import tkinter as tk

import pytest

from gui_components import _FlatDropdown


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        pytest.skip("Tk unavailable: {}".format(exc))
    # Left mapped: a withdrawn window never delivers <<ComboboxSelected>>.
    errors = []
    r.report_callback_exception = lambda *a: errors.append(a)
    r.errors = errors
    yield r
    r.destroy()


def _open(root):
    var = tk.StringVar(value="a")
    dd = _FlatDropdown(root, textvariable=var, values=["a", "b"])
    dd.pack()
    root.update()
    dd._open()
    root.update()
    return dd, var


def _click_bindings(root):
    return [l for l in root.bind("<Button-1>").splitlines() if l.strip()]


def test_pick_that_destroys_the_dropdown(root):
    dd, var = _open(root)
    dd.bind("<<ComboboxSelected>>", lambda e: dd.destroy())
    lb = dd._lb
    lb.selection_clear(0, "end")
    lb.selection_set(1)
    dd._pick(lb)
    root.update()
    assert var.get() == "b"
    assert not dd.winfo_exists()
    assert _click_bindings(root) == []
    assert root.errors == []


def test_destroyed_while_open_then_click(root):
    dd, _ = _open(root)
    dd.destroy()
    root.update()
    root.event_generate("<Button-1>", x=1, y=1)
    root.update()
    assert root.errors == []
    assert _click_bindings(root) == []
    # A second click finds nothing left to run.
    root.event_generate("<Button-1>", x=1, y=1)
    root.update()
    assert root.errors == []


def test_tooltip_after_destroy(root):
    dd, _ = _open(root)
    dd._close()
    dd.destroy()
    dd._tip_show(10, 10, "a")
    dd._tint(False)
    assert root.errors == []
