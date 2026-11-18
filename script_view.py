# -*- coding: utf-8 -*-
"""The script, readable inside PostBridge.

A window with the script as it is on disk -- headers coloured by what they
are -- beside what Script Check makes of it and an outline of its parts.
Clicking a finding or a part goes to its line.  It reads; it does not edit:
the script's home is its file or its Google Doc.

`describe()` is the part with no widgets: the kind of every line, the
outline and the findings, for the window and for tests.
"""
import os
import subprocess
import sys
import tkinter as tk
import tkinter.font as tkfont

from config import *
from config import _SANS
from gui_components import _SlimScrollbar, _token_color
from parsers import _HEADER_RE, _strip_comment, parse_script
import script_check

_MONO = "Consolas" if sys.platform == "win32" else "Menlo"
_BOM = chr(0xFEFF)
_NUM_W = 6                    # characters a line's number takes, with its space


def describe(text, script_path=None):
    """What the window shows of *text*.

    Returns {"kinds": {line: kind}, "tokens": {line: token}, "outline":
    [(line, label)], "findings": [Finding], "counts": (pulls, vo, parts,
    tokens)}.  Lines are 1-based.  kind is one of part, vo, pull, tokens,
    comment; a line with none is body text.
    """
    text = (text or "").lstrip(_BOM)
    kinds, toks, outline = {}, {}, []
    in_tokens = False
    for n, raw in enumerate(text.split("\n"), 1):
        bare = raw.strip()
        s = _strip_comment(raw).strip()
        if not s:
            if bare:
                kinds[n] = "comment"
            continue
        up = s.upper()
        if up == "[TOKENS]":
            in_tokens = True
        if in_tokens:
            kinds[n] = "tokens"
            if up == "[/TOKENS]":
                in_tokens = False
            continue
        m = _HEADER_RE.match(s)
        if not m:
            continue
        if m.group("part") is not None:
            kinds[n] = "part"
            outline.append((n, m.group("part").strip()))
        elif m.group("void") is not None:
            kinds[n] = "vo"
        else:
            kinds[n] = "pull"
            toks[n] = m.group("tok")
    tokens, parts, pulls, vo_blocks, _title, _w = parse_script(text)
    return {"kinds": kinds, "tokens": toks, "outline": outline,
            "findings": script_check.run_checks(text, script_path=script_path),
            "counts": (len(pulls), len(vo_blocks), len(parts), len(tokens))}


def headline(findings):
    """(text, colour) summing up Script Check's findings in a few words."""
    n_err, n_warn, _n_info = script_check.summarize(findings)
    if n_err:
        return "{} blocking issue{}".format(n_err, "" if n_err == 1 else "s"), ERR
    if n_warn:
        return "{} thing{} to check".format(n_warn, "" if n_warn == 1 else "s"), WARN
    return "no problems found", SUCCESS


def open_in_editor(path):
    """Hand the script to whatever the system edits text with."""
    if sys.platform == "win32":
        os.startfile(path)                                  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


class ScriptWindow(tk.Toplevel):
    """One per app: asking for the script again brings this one forward."""

    def __init__(self, app, path):
        super().__init__(app)
        self.app, self.path = app, path
        self.configure(bg=BG)
        self.geometry("1280x820")
        self.minsize(820, 480)

        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=14, pady=(12, 6))
        self._name = tk.Label(top, text="", font=FBT, bg=BG, fg=TEXT)
        self._name.pack(side="left")
        self._counts = tk.Label(top, text="", font=FB, bg=BG, fg=SUB)
        self._counts.pack(side="left", padx=(14, 0))
        app._btn(top, "OPEN IN EDITOR", lambda: open_in_editor(self.path),
                 small=True).pack(side="right")
        app._btn(top, "⟳ RE-READ", self.reload, small=True
                 ).pack(side="right", padx=(0, 6))
        self._path_lbl = tk.Label(self, text=path, font=(_SANS, 10), bg=BG, fg=SUB)
        self._path_lbl.pack(anchor="w", padx=14)
        # Where the script came from and what was just done to it (set_status).
        self._status = tk.Label(self, text="", font=FB, bg=BG, fg=SUB, anchor="w",
                                justify="left", wraplength=1220)

        # What can be done to the script from here (set_actions): along the
        # bottom, packed first so a short window keeps it.
        self._bar = tk.Frame(self, bg=BG)
        self._bar.pack(side="bottom", fill="x", padx=14, pady=(0, 12))
        # Called after the file is read again, so whatever else shows the
        # script's state can follow.
        self.on_change = None

        panes = tk.Frame(self, bg=BG)
        panes.pack(fill="both", expand=True, padx=14, pady=(8, 10))
        self._panes = panes

        side = tk.Frame(panes, bg=SURF, width=400, highlightbackground=BORDER,
                        highlightthickness=1)
        side.pack(side="right", fill="y", padx=(10, 0))
        side.pack_propagate(False)
        self._side = side

        box = tk.Frame(panes, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        box.pack(side="left", fill="both", expand=True)
        self._text = tk.Text(box, font=(_MONO, 11), bg=SURF, fg=TEXT, bd=0,
                             highlightthickness=0, padx=10, pady=10, wrap="word",
                             insertbackground=TEXT, selectbackground=SURF3,
                             spacing1=1, spacing3=1)
        sb = _SlimScrollbar(box, command=self._text.yview)
        sb.pack(side="right", fill="y")
        self._text.pack(side="left", fill="both", expand=True)
        self._text.config(yscrollcommand=sb.set)

        # Every line starts with its number; a wrapped line hangs under its
        # own text, clear of the numbers.
        t = self._text
        hang = tkfont.Font(font=(_MONO, 11)).measure("0" * _NUM_W)
        t.tag_configure("body", lmargin2=hang)
        t.tag_configure("num", foreground=SUB)
        t.tag_configure("num_err", foreground=ERR)
        t.tag_configure("num_warn", foreground=WARN)
        t.tag_configure("part", foreground=ACCENT, font=(_MONO, 12, "bold"),
                        spacing1=12)
        t.tag_configure("vo", foreground="#7aa2e8", font=(_MONO, 11, "bold"))
        t.tag_configure("tokens", foreground=SUB)
        t.tag_configure("comment", foreground=SUB)
        t.tag_configure("at", background="#4a3520")

        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<F5>", lambda e: self.reload())
        self.set_actions()
        self.reload()

    # -- what the opener adds ---------------------------------------------

    def set_status(self, text="", colour=None):
        """A line under the path: where the script came from, what changed."""
        self._status.config(text=text or "", fg=colour or SUB)
        if text:
            self._status.pack(anchor="w", fill="x", padx=14, pady=(2, 0),
                              before=self._panes)
        else:
            self._status.pack_forget()

    def set_actions(self, left=(), right=(), notice=None):
        """The bottom bar.  *left* and *right* are [(label, command, accent)];
        *notice* is said in the bar itself.  With nothing given there is
        CLOSE."""
        for w in self._bar.winfo_children():
            w.destroy()
        for label, cmd, accent in (right or [("CLOSE", self.destroy, False)]):
            self.app._btn(self._bar, label, cmd, color=ACCENT if accent else None
                          ).pack(side="right", padx=(8, 0))
        if notice:
            tk.Label(self._bar, text=notice, font=FB, bg=BG, fg=WARN, anchor="w",
                     justify="left", wraplength=560
                     ).pack(side="left", fill="x", expand=True)
        for label, cmd, accent in left:
            self.app._btn(self._bar, label, cmd, small=True,
                          color=ACCENT if accent else None
                          ).pack(side="left", padx=(0, 8))

    # -- content ----------------------------------------------------------

    def reload(self):
        """Read the file again and redraw everything from it."""
        try:
            with open(self.path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            text = ""
            self._counts.config(text="could not be read: {}".format(e), fg=ERR)
        self.title("Script — {}".format(os.path.basename(self.path)))
        self._name.config(text=os.path.basename(self.path))
        self._path_lbl.config(text=self.path)
        self.text = text
        info = describe(text, script_path=self.path)
        self.info = info
        if text:
            self._counts.config(
                text="{} pulls  ·  {} VO  ·  {} parts  ·  {} tokens".format(
                    *info["counts"]), fg=SUB)

        marks = {}                              # line -> "err" | "warn"
        for f in info["findings"]:
            sev = {script_check.ERROR: "err", script_check.WARN: "warn"}.get(f.severity)
            if f.line and sev and marks.get(f.line) != "err":
                marks[f.line] = sev

        t = self._text
        t.config(state="normal")
        t.delete("1.0", "end")
        lines = text.lstrip(_BOM).split("\n")
        for n, raw in enumerate(lines, 1):
            kind = info["kinds"].get(n)
            tags = ["body"] + ([kind] if kind and kind != "pull" else [])
            if kind == "pull":
                tag = "tok_" + info["tokens"][n]
                t.tag_configure(tag, foreground=_token_color(info["tokens"][n]),
                                font=(_MONO, 11, "bold"))
                tags.append(tag)
            num = "num_" + marks[n] if n in marks else "num"
            t.insert("end", "{:>{}} ".format(n, _NUM_W - 1),
                     ("body", num) + (("part",) if kind == "part" else ()))
            t.insert("end", raw.rstrip("\r") + ("" if n == len(lines) else "\n"),
                     tuple(tags))
        t.tag_raise("at")
        t.config(state="disabled")
        self._fill_side(info)
        if self.on_change is not None:
            self.on_change()

    def _fill_side(self, info):
        for w in self._side.winfo_children():
            w.destroy()
        head, colour = headline(info["findings"])
        tk.Label(self._side, text="SCRIPT CHECK", font=FL, bg=SURF, fg=ACCENT
                 ).pack(anchor="w", padx=12, pady=(10, 0))
        tk.Label(self._side, text=head, font=FBT, bg=SURF, fg=colour
                 ).pack(anchor="w", padx=12, pady=(2, 6))
        # The app's own scrolling frame; it notes the last one made, which
        # belongs to whatever screen is up, so that is put back.
        keep = getattr(self.app, "_last_scroll_canvas", None)
        inner = self.app._scroll_frame(self._side)
        self.app._last_scroll_canvas = keep
        inner.master.configure(bg=SURF)
        inner.configure(bg=SURF)
        chip = {script_check.ERROR: ("ERROR", ERR), script_check.WARN: ("WARN", WARN),
                script_check.INFO: ("NOTE", SUB)}
        for f in info["findings"]:
            label, colour = chip[f.severity]
            card = tk.Frame(inner, bg=SURF2, cursor="hand2" if f.line else "")
            card.pack(fill="x", pady=(0, 5), padx=(0, 4))
            row = tk.Frame(card, bg=SURF2)
            row.pack(fill="x", padx=8, pady=(6, 0))
            tk.Label(row, text=label, font=(_SANS, 10, "bold"), bg=SURF2, fg=colour
                     ).pack(side="left")
            if f.line:
                tk.Label(row, text="line {}".format(f.line), font=(_SANS, 10),
                         bg=SURF2, fg=SUB).pack(side="right")
            tk.Label(card, text=f.title, font=(_SANS, 11, "bold"), bg=SURF2, fg=TEXT,
                     anchor="w", justify="left", wraplength=340
                     ).pack(anchor="w", padx=8)
            if f.detail:
                tk.Label(card, text=f.detail, font=(_SANS, 10), bg=SURF2, fg=SUB,
                         anchor="w", justify="left", wraplength=340
                         ).pack(anchor="w", padx=8, pady=(1, 0))
            tk.Frame(card, bg=SURF2, height=6).pack()
            if f.line:
                self._click(card, lambda _e=None, n=f.line: self.goto(n))
        if info["outline"]:
            tk.Label(inner, text="PARTS", font=FL, bg=SURF, fg=ACCENT
                     ).pack(anchor="w", pady=(10, 2))
            for n, name in info["outline"]:
                lab = tk.Label(inner, text=name, font=FB, bg=SURF, fg=TEXT,
                               anchor="w", cursor="hand2")
                lab.pack(fill="x")
                lab.bind("<Button-1>", lambda _e, n=n: self.goto(n))

    @staticmethod
    def _click(widget, cmd):
        widget.bind("<Button-1>", cmd)
        for c in widget.winfo_children():
            ScriptWindow._click(c, cmd)

    def goto(self, line):
        """Bring *line* into view, a few lines down from the top, and mark it."""
        t = self._text
        t.tag_remove("at", "1.0", "end")
        t.tag_add("at", "{}.0".format(line), "{}.0 lineend +1c".format(line))
        t.yview("{}.0".format(max(1, line - 4)))


def show(app, path):
    """Open the script window on *path*, or bring the open one to it."""
    win = getattr(app, "_script_window", None)
    try:
        alive = win is not None and bool(win.winfo_exists())
    except tk.TclError:
        alive = False
    if alive:
        # Whoever opens it now says what it offers: nothing is left over
        # from the last opener.
        win.path = path
        win.on_change = None
        win.set_status()
        win.set_actions()
        win.reload()
        win.deiconify()
        win.lift()
        return win
    app._script_window = ScriptWindow(app, path)
    return app._script_window
