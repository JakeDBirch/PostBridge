"""A button whose action rebuilds the screen it sits on.

The action runs a tick after the press, from a timer.  When the button
owned that timer, the action destroyed the button mid-call and the timer
then finished by deleting its Tcl command by name -- a name Tk could have
just given to a command on the new screen.  That widget's next teardown
raised "can't delete Tcl command" and left the screen half cleared (seen
on the Episode review's KEEP / RESTORE).

Needs tkinter and a display; skips otherwise.
"""
import os
import sys
import time
import traceback

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def test_button_that_rebuilds_its_screen_leaves_no_dead_commands():
    tk = pytest.importorskip("tkinter")
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        pytest.skip("no display")
    try:
        import main
        app = main.App()
    except tk.TclError as e:
        pytest.skip("Tk unavailable: {}".format(e))
    errors = []
    app.report_callback_exception = (
        lambda *x: errors.append("".join(traceback.format_exception(*x))))
    try:
        app._clear()
        host = tk.Frame(app.body)
        host.pack()

        def build():
            for _ in range(40):
                app._btn(host, "KEEP", lambda: rebuild()).pack()

        rebuilt = []

        def rebuild():
            rebuilt.append(1)
            for c in host.winfo_children():
                c.destroy()
            build()

        def dead():
            return [(c._w, n) for c in host.winfo_children()
                    for n in (c._tclCommands or [])
                    if not app.tk.call("info", "commands", n)]

        build()
        app.update()            # a click needs the buttons realised
        for _ in range(60):
            buttons = host.winfo_children()
            assert len(buttons) == 40
            n = len(rebuilt)
            buttons[5].event_generate("<Button-1>")
            end = time.time() + 5
            while len(rebuilt) == n and time.time() < end:
                app.update()
            app.update()        # the timer's own clean-up, after the action
            assert not dead()
        assert len(rebuilt) == 60
        assert not errors, errors[0]
    finally:
        app.destroy()
