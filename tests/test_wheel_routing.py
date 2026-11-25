"""The mouse wheel scrolls the scrolling frame the pointer is over.

There used to be one app-wide handler per App._scroll_frame call, each
replacing the last: the script window's list of findings took the wheel
away from the screen behind it (the Episode screen would not scroll until
it was rebuilt), and a screen would not scroll while another window had
keyboard focus.
"""
import os
import sys
import tkinter as tk

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Ev(object):
    """A wheel notch with the pointer over `widget`.  Which widget is under
    a point on the screen is the window manager's answer (and the test
    windows all open on one spot), so the tests give it."""
    def __init__(self, widget, delta=-120):
        self.x_root, self.y_root, self.delta = widget, None, delta


@pytest.fixture
def app():
    import main

    class _App(tk.Tk):
        pass
    for n in ("_scroll_frame", "_wheel_register", "_wheel_frame_at",
              "_wheel_frame_of", "_wheel_route"):
        setattr(_App, n, getattr(main.App, n))
    at = _App._wheel_frame_at
    _App._wheel_frame_at = lambda self, x, y: (
        self._wheel_frame_of(str(x)) if isinstance(x, tk.Misc) else at(self, x, y))
    try:
        a = _App()
    except tk.TclError as exc:
        pytest.skip("Tk unavailable: {}".format(exc))
    a.geometry("400x300")
    yield a
    a.destroy()


def _tall(app, parent):
    sf = app._scroll_frame(parent)
    for i in range(80):
        tk.Label(sf, text="row {}".format(i)).pack(anchor="w")
    return sf, app._last_scroll_canvas


def _settle(app):
    for _ in range(4):
        app.update_idletasks()
        app.update()


def test_the_wheel_scrolls_the_frame_under_the_pointer(app):
    body = tk.Frame(app)
    body.pack(fill="both", expand=True)
    _sf, main_cv = _tall(app, body)
    # A second window with its own scrolling frame, as the script window has.
    top = tk.Toplevel(app)
    top.geometry("300x200")
    side = tk.Frame(top)
    side.pack(fill="both", expand=True)
    sf2, top_cv = _tall(app, side)
    top.focus_force()                       # focus is in the OTHER window
    _settle(app)
    assert main_cv.yview()[0] == 0.0 and top_cv.yview()[0] == 0.0

    app._wheel_route(_Ev(main_cv))          # pointer over the main screen
    _settle(app)
    assert main_cv.yview()[0] > 0.0 and top_cv.yview()[0] == 0.0

    # Over a row inside the second window's frame: that one scrolls.
    app._wheel_route(_Ev(sf2.winfo_children()[0]))
    _settle(app)
    assert top_cv.yview()[0] > 0.0

    # The second window goes; the main screen still scrolls.
    was = main_cv.yview()[0]
    top.destroy()
    _settle(app)
    app._wheel_route(_Ev(main_cv))
    _settle(app)
    assert main_cv.yview()[0] > was


def test_a_flick_is_one_scroll_and_nothing_under_the_pointer_is_nothing(app):
    body = tk.Frame(app)
    body.pack(side="left", fill="both", expand=True)
    plain = tk.Frame(app, width=120)        # no scrolling frame here
    plain.pack(side="right", fill="y")
    _sf, cv = _tall(app, body)
    _settle(app)
    app._wheel_route(_Ev(plain))
    _settle(app)
    assert cv.yview()[0] == 0.0
    for _ in range(5):                      # five notches before Tk is idle
        app._wheel_route(_Ev(cv))
    assert cv.yview()[0] == 0.0             # not yet: coalesced
    _settle(app)
    five = cv.yview()[0]
    cv.yview_moveto(0.0)
    cv.yview_scroll(5, "units")
    assert five == pytest.approx(cv.yview()[0])


def test_a_dropdown_list_is_left_to_scroll_itself(app):
    body = tk.Frame(app)
    body.pack(fill="both", expand=True)
    _sf, cv = _tall(app, body)
    assert app._wheel_frame_of(str(cv) + ".!frame.!combobox.popdown.f.l") is None
    assert app._wheel_frame_of(str(cv) + ".!frame.!label")["canvas"] is cv
    assert app._wheel_frame_of(".!toplevel.!label") is None


def test_the_real_event_reaches_the_router(app):
    body = tk.Frame(app)
    body.pack(fill="both", expand=True)
    sf, cv = _tall(app, body)
    # Really under the pointer's point: above whatever else is on that screen.
    app.attributes("-topmost", True)
    app.lift()
    _settle(app)
    row = sf.winfo_children()[0]
    if str(app.tk.call("winfo", "containing", row.winfo_rootx() + 5,
                       row.winfo_rooty() + 5)) != str(row):
        pytest.skip("another window covers the test window")
    row.event_generate("<MouseWheel>", delta=-120, rootx=row.winfo_rootx() + 5,
                       rooty=row.winfo_rooty() + 5, when="now")
    _settle(app)
    assert cv.yview()[0] > 0.0
