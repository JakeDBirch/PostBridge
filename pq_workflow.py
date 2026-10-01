"""Pull Quotes workflow — extracted from main.py as a mixin.

Every method here runs with ``self`` bound to the App instance, so shared
helpers (_btn, _ui, _center_dialog, _mark_saved, _clear, _home, _reset,
_section, _scroll_frame, _tooltip) and shared App state resolve normally via
inheritance.  App is declared ``class App(AafWorkflowMixin, PqWorkflowMixin, ...)``.

Pure code-move from main.py: every method body is identical to its original,
with the sole exception that the three staticmethod self-calls that used
``App._pq_*`` are rewritten to ``PqWorkflowMixin._pq_*`` because App is not
in scope inside this module (importing it would be a circular import).
"""
import os
from utils import app_state_dir as _app_state_dir
import re
import sys
import json
import bisect
import threading
import tempfile
import time
import ctypes
import traceback
from collections import Counter
try:
    import winsound
except ImportError:
    winsound = None

import tkinter as tk
from tkinter import filedialog, messagebox

# Optional drag-and-drop, detected per-module so this file never imports
# main.py (which would be a circular import).
try:
    from tkinterdnd2 import DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False
    DND_FILES = None

# faster_whisper availability — matches main.py's lazy check without
# importing the heavyweight package at module load.
import importlib.util as _importlib_util
HAS_WHISPER = _importlib_util.find_spec("faster_whisper") is not None

from config import *
from config import _SANS
from utils import basename, is_video, is_media, secs_tc, parse_dnd
from gui_components import _SlimScrollbar
import engines


# Module-scope diagnostic flag + log helpers, same shape as main.py.
_PQ_STREAM_DIAG = False

def _pq_stream_log_path():
    import tempfile as _t
    return os.path.join(_t.gettempdir(), "_pq_stream.log")

def _pq_stream_log(msg):
    if not _PQ_STREAM_DIAG:
        return
    try:
        with open(_pq_stream_log_path(), "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


# ── Small text-widget helpers that PQ methods use.  Kept here rather
# than in main.py so the mixin is self-contained; main.py's own copies
# are still needed by other code and stay where they are.
def _tx_count_chars(tx, a, b):
    """Return the number of characters between two Tk Text indexes."""
    try:
        return int(tx.count(a, b, "chars")[0])
    except (tk.TclError, IndexError, TypeError):
        return 0

def _tx_count_displaylines(tx, a, b):
    """Return the number of display lines (wrap-aware) between two indexes."""
    try:
        return int(tx.count(a, b, "displaylines")[0])
    except (tk.TclError, IndexError, TypeError):
        return 0


class PqWorkflowMixin:
    def _pq_open_home(self):
        """Entry point: empty in-memory Episode Project workspace."""
        self._pq_project = {
            "title":     "Untitled Episode",
            "sessions":  [],   # list of session dicts (each carries _file_path)
            "file_path": None,
        }
        self._pq_render_project_view()

    def _pq_load_project(self, data, file_path):
        """Load an Episode Project JSON (already-parsed dict)."""
        sessions, missing = [], []
        for sp in data.get("sessions", []) or []:
            loaded = self._pq_load_session_file(sp)
            if loaded is None:
                missing.append(sp)
            else:
                sessions.append(loaded)
        self._pq_project = {
            "title":     data.get("title") or os.path.splitext(
                            os.path.basename(file_path))[0],
            "sessions":  sessions,
            "file_path": file_path,
        }
        if missing:
            messagebox.showwarning(
                "Missing sessions",
                "{} session file(s) referenced by this project could not be "
                "loaded:\n\n{}".format(
                    len(missing),
                    "\n".join("  • " + p for p in missing[:10])))
        self._pq_render_project_view()
        self._mark_saved()   # freshly loaded == clean baseline

    def _pq_open_standalone_session(self, data, file_path):
        """Open a single Interview Session JSON without a project context.
        Wraps it in an unsaved one-session project so the same view applies."""
        session = dict(data)
        session["_file_path"] = file_path
        self._pq_project = {
            "title":     "Untitled Episode",
            "sessions":  [session],
            "file_path": None,
        }
        self._pq_render_project_view()
        self._mark_saved()   # freshly loaded == clean baseline

    def _pq_load_session_file(self, path):
        """Load an Interview Session JSON; return dict or None on error."""
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("workflow") != "interview_session":
                return None
            data["_file_path"] = path
            return data
        except Exception:
            return None

    def _pq_save_session_file(self, session):
        """Write a session dict to its `_file_path`."""
        path = session.get("_file_path")
        if not path:
            return False
        # Stable session ID — generated once per session, future-proof
        # against ambiguous tokens across projects.
        if not session.get("id"):
            import uuid as _uuid
            session["id"] = "ses_" + _uuid.uuid4().hex[:12]
        payload = {
            "version":     3,
            "workflow":    "interview_session",
            "id":          session["id"],
            "token":       session.get("token", ""),
            "media":       list(session.get("media", [])),
            "transcript":  session.get("transcript", []),
        }
        # Path to the playback cache (mix-of-all-sources WAV) is persisted
        # only when it actually exists on disk.
        ac = session.get("audio_cache")
        if ac and os.path.isfile(ac):
            payload["audio_cache"] = ac
        # Per-file audio_signatures (sha256 + duration + sr + channels
        # + size) so cross-machine reconcile can validate the local
        # audio matches what the transcript was made from.  Legacy
        # sessions without this field still load (audio_signature_
        # matches returns True for missing sigs), but new saves start
        # populating it immediately.  Keyed by ABSOLUTE PATH — resolver
        # code that relocates paths at load time strips down to
        # basename lookup when validating, so the map is consulted by
        # basename when abs-path doesn't match.
        _sigs = {}
        for _mp in payload["media"]:
            if _mp and os.path.isfile(_mp):
                _sigs[_mp] = engines.audio_signature(_mp)
        if ac and os.path.isfile(ac) and ac not in _sigs:
            _sigs[ac] = engines.audio_signature(ac)
        if _sigs:
            payload["audio_signatures"] = _sigs
        # Optional per-source speaker label overrides (map of
        # filename → display label).  Only saved when set.
        speakers = session.get("speakers")
        if speakers:
            payload["speakers"] = dict(speakers)
        # Margin notes (Pull Quotes mode = margin)
        notes = session.get("notes")
        if notes:
            payload["notes"] = list(notes)
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            # Mirror the word list as a .pb_transcript.json sidecar next
            # to each source audio file so the Script→Session reconcile
            # workflow can re-use this transcript without re-running
            # Whisper.  No-op if the transcript is empty.
            words = session.get("transcript") or []
            if words:
                for media_path in session.get("media", []):
                    try:
                        if os.path.isfile(media_path):
                            engines.pb_transcript_save(media_path, words)
                    except Exception:
                        pass
            return True
        except Exception as e:
            messagebox.showerror("Save Session failed", str(e))
            return False

    def _pq_default_session_path(self, token, media_paths):
        """Pick the canonical save path for a session JSON.

        Preference order:
          1. `<project_root>/03_AUDIO/00_RAW AUDIO/<TOKEN>.pb_session.json`
             — the editorial template's interview-audio folder.  Walks
             up from the first media file looking for `02_MEDIA` +
             `03_AUDIO` siblings (project root marker).
          2. `<first_media_dir>/<TOKEN>.pb_session.json` — fallback when
             the layout isn't recognised.
        """
        if not media_paths:
            return None
        first = os.path.abspath(media_paths[0])

        # Walk up looking for project root.
        cur = os.path.dirname(first)
        for _ in range(8):
            raw = os.path.join(cur, "03_AUDIO", "00_RAW AUDIO")
            if (os.path.isdir(os.path.join(cur, "02_MEDIA"))
                    and os.path.isdir(os.path.join(cur, "03_AUDIO"))
                    and os.path.isdir(raw)):
                return os.path.join(raw, "{}.pb_session.json".format(token))
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent

        return os.path.join(os.path.dirname(first),
                            "{}.pb_session.json".format(token))

    def _pq_save_episode_project(self, prompt_path=False):
        """Write the Episode Project JSON.  Prompts on first save.
        Returns True only when a file was actually written — callers use
        this to gate "SAVED" feedback (a cancelled dialog is not a save)."""
        project = getattr(self, "_pq_project", None)
        if not project:
            return False
        # Refuse to save until the project actually has sessions on disk.
        if not project["sessions"]:
            messagebox.showinfo("Nothing to save",
                "Add at least one Interview Session before saving the project.")
            return False
        path = project.get("file_path")
        if prompt_path or not path:
            suggested = re.sub(r"[^A-Za-z0-9_\- ]+", "_",
                               project.get("title", "Episode")) + ".pb_episode.json"
            path = filedialog.asksaveasfilename(
                title="Save Episode Project",
                initialfile=suggested,
                defaultextension=".json",
                filetypes=[("Pull Quotes Episode Project", "*.pb_episode.json"),
                           ("JSON", "*.json"),
                           ("All", "*.*")])
            if not path:
                return False
        payload = {
            "version":  1,
            "workflow": "episode_project",
            "title":    project.get("title", ""),
            "sessions": [s["_file_path"] for s in project["sessions"]
                         if s.get("_file_path")],
        }
        # Per-session sha256 so cross-machine consumption can validate
        # the .pb_session.json file itself hasn't drifted from what
        # this episode project was built against.  Cheap — session
        # files are tiny (few KB); no ffprobe involved for this one.
        try:
            import hashlib as _hl
            _session_sigs = {}
            for _sp in payload["sessions"]:
                if _sp and os.path.isfile(_sp):
                    _h = _hl.sha256()
                    with open(_sp, "rb") as _f:
                        for _chunk in iter(lambda: _f.read(1 << 20), b""):
                            _h.update(_chunk)
                    _session_sigs[_sp] = {
                        "sha256": _h.hexdigest(),
                        "size":   os.path.getsize(_sp),
                    }
            if _session_sigs:
                payload["session_signatures"] = _session_sigs
        except Exception:
            pass
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            project["file_path"] = path
            self._mark_saved()
            self._pq_render_project_view()
            return True
        except Exception as e:
            messagebox.showerror("Save Episode Project failed", str(e))
            return False

    # ── Project view ─────────────────────────────────────────────────────────

    def _pq_render_project_view(self):
        import re
        # Stop any session-view playback before tearing the view down,
        # otherwise the audio keeps going while the user is browsing.
        try:
            self._pq_stop_playback()
        except Exception:
            pass
        self._clear()
        # Stepping back into the project means we're no longer focused on a
        # single session — clear the marker so background-completion
        # callbacks know to refresh THIS view rather than the (gone) session
        # view.
        self._pq_current_session = None
        # Reset the row-widget registry; rows will repopulate as we render.
        self._pq_row_widgets = {}

        project = self._pq_project

        # Bottom nav (pinned) — only HOME.  SAVE / SAVE AS live in the
        # persistent header bar (same as every other workflow).
        nav = tk.Frame(self.body, bg=BG)
        nav.pack(side="bottom", fill="x", pady=(8, 0))
        self._btn(nav, "← HOME", self._home).pack(side="left")

        self._section("PULL QUOTES — EPISODE PROJECT")

        # Title + file path metadata
        title_row = tk.Frame(self.body, bg=BG)
        title_row.pack(fill="x", pady=(0, 8))
        tk.Label(title_row, text="EPISODE TITLE", font=FL,
                 bg=BG, fg=SUB, width=18, anchor="w").pack(side="left")
        title_var = tk.StringVar(value=project.get("title", ""))
        title_entry = tk.Entry(title_row, textvariable=title_var, font=FB,
                                bg=SURF2, fg=TEXT, insertbackground=TEXT,
                                relief="flat", bd=6)
        title_entry.pack(side="left", fill="x", expand=True)
        def _on_title_change(*_):
            project["title"] = title_var.get().strip() or "Untitled Episode"
        title_var.trace_add("write", _on_title_change)

        fp_row = tk.Frame(self.body, bg=BG)
        fp_row.pack(fill="x", pady=(0, 12))
        tk.Label(fp_row, text="PROJECT FILE", font=FL,
                 bg=BG, fg=SUB, width=18, anchor="w").pack(side="left")
        fp = project.get("file_path") or "— unsaved (use SAVE in the header) —"
        tk.Label(fp_row, text=fp, font=FB, bg=BG,
                 fg=SUCCESS if project.get("file_path") else SUB,
                 anchor="w").pack(side="left", fill="x", expand=True)

        # Project-level controls row: bg-mode + manage-speakers
        ctrl_row = tk.Frame(self.body, bg=BG)
        ctrl_row.pack(fill="x", pady=(0, 12))
        tk.Label(ctrl_row, text="OPTIONS", font=FL,
                 bg=BG, fg=SUB, width=18, anchor="w").pack(side="left")

        # Background-mode checkbox — shared with the session view via
        # self._pq_bg_mode_var (created here if absent), so flipping
        # either keeps the other in sync.
        if not hasattr(self, "_pq_bg_mode_var"):
            self._pq_bg_mode_var = tk.BooleanVar(value=False)
        bg_frame = tk.Frame(ctrl_row, bg=BG)
        bg_frame.pack(side="left")
        bg_ck = tk.Label(bg_frame,
                          text="☑" if self._pq_bg_mode_var.get() else "☐",
                          font=(_SANS, 14), bg=BG,
                          fg=ACCENT if self._pq_bg_mode_var.get() else SUB,
                          cursor="hand2", padx=4)
        bg_ck.pack(side="left")
        bg_lbl = tk.Label(bg_frame, text="Background mode (all sessions)",
                          font=FB, bg=BG, fg=SUB, cursor="hand2")
        bg_lbl.pack(side="left")
        def _toggle_bg_proj():
            new_val = not self._pq_bg_mode_var.get()
            self._pq_bg_mode_var.set(new_val)
            bg_ck.config(text="☑" if new_val else "☐",
                         fg=ACCENT if new_val else SUB)
        bg_ck.bind("<Button-1>",  lambda e: _toggle_bg_proj())
        bg_lbl.bind("<Button-1>", lambda e: _toggle_bg_proj())

        # Inline model picker so the user can change the Whisper size
        # before opening any session.
        self._build_model_picker(ctrl_row, bg=BG).pack(side="left",
                                                       padx=(24, 0))

        # (MANAGE SPEAKERS removed — speaker labels are renamed inline
        # by double-clicking any "JORDAN:" / "JENA:" header in the
        # transcript view.  The bulk dialog implementation is kept on
        # the App object as `_pq_manage_speakers_dialog` for future use.)

        is_empty = not project["sessions"]

        # Single panel that contains: column header (only when populated),
        # one row per existing session, then the always-last "+ ADD
        # INTERVIEW SESSION" affordance.  Putting the add button inside the
        # panel keeps it visually anchored to the list while remaining the
        # obvious next action — empty or full.
        list_outer = tk.Frame(self.body, bg=SURF,
                              highlightbackground=BORDER, highlightthickness=1)
        list_outer.pack(fill="both", expand=True, pady=(0, 12))

        # Column header — pinned at the top of the panel.
        if not is_empty:
            hdr = tk.Frame(list_outer, bg=SURF3)
            hdr.pack(side="top", fill="x")
            for txt, w_ in [("TOKEN", 16), ("FILES", 8),
                            ("WORDS", 12), ("STATUS", 28)]:
                tk.Label(hdr, text=txt, font=FL, bg=SURF3, fg=SUB,
                         anchor="w", padx=12, pady=6, width=w_
                         ).pack(side="left")

        # "+ ADD INTERVIEW SESSION" — built and pinned to the BOTTOM of the
        # panel BEFORE the (scrollable) row area, so it stays reachable no
        # matter how many sessions the project has.  It used to be packed
        # after the rows with no scroll region, so a project with many
        # sessions pushed it past the bottom of the window with no way to
        # reach it (reported by a user with 12 sessions).
        add_pad = tk.Frame(list_outer, bg=SURF2 if is_empty else SURF)
        add_pad.pack(side="bottom", fill="x")
        add_inner = tk.Frame(add_pad, bg=add_pad.cget("bg"))
        add_inner.pack(padx=14, pady=14, anchor="w")
        self._btn(add_inner,
                  "+ ADD INTERVIEW SESSION",
                  self._pq_add_session_dialog,
                  color=ACCENT).pack(side="left")
        if is_empty:
            tk.Label(add_inner,
                     text="    ← start here",
                     font=FB, bg=add_pad.cget("bg"), fg=SUB
                     ).pack(side="left")

        if not is_empty:
            # Session rows go in a scrollable area filling the space between
            # the pinned header and the pinned add button.  _scroll_frame
            # defaults to the app BG; tint it SURF so empty space below the
            # rows matches the panel instead of showing a dark void.
            rows_host = self._scroll_frame(list_outer)
            try:
                rows_host.config(bg=SURF)
                self._last_scroll_canvas.config(bg=SURF)
            except (AttributeError, tk.TclError):
                pass
            for s in project["sessions"]:
                self._pq_render_session_row(rows_host, s)
        else:
            # Empty-state copy — sits above the add affordance.
            tk.Label(list_outer,
                     text="\n  No interview sessions yet.",
                     font=FBT, bg=SURF, fg=TEXT,
                     anchor="w", padx=20).pack(side="top", anchor="w")
            tk.Label(list_outer,
                     text="  Drop interview media into a session, transcribe it,\n"
                          "  and copy quotes as ready-to-paste @PULL blocks.\n",
                     font=FB, bg=SURF, fg=SUB,
                     anchor="w", padx=20, justify="left",
                     pady=(4)).pack(side="top", anchor="w")

        # Incremental status-cell updates while any session is transcribing.
        # No full re-render → no flicker.
        if any(s.get("_progress", {}).get("active")
               for s in project["sessions"]):
            self.after(600, self._pq_tick_project_status)

    def _pq_update_project_row(self, session):
        """Surgical update of one session row's status + word count.
        Falls back to a full re-render if the row widgets aren't around
        (e.g. project view rebuilt under us)."""
        rows = getattr(self, "_pq_row_widgets", None) or {}
        refs = rows.get(id(session))
        if not refs:
            try:
                self._pq_render_project_view()
            except Exception:
                pass
            return
        try:
            text, fg = self._pq_status_for(session)
            refs["status_lbl"].config(text=text, fg=fg)
            refs["words_lbl"].config(
                text="{:,}".format(len(session.get("transcript", []))))
        except tk.TclError:
            try:
                self._pq_render_project_view()
            except Exception:
                pass

    def _pq_tick_project_status(self):
        """Incrementally refresh the status cell of every session row whose
        progress dict says it's still active.  Avoids the twitch of a full
        re-render under self.after."""
        if getattr(self, "_pq_current_session", None) is not None:
            return  # session view is open; project view isn't visible
        if getattr(self, "_pq_project", None) is None:
            return
        rows = getattr(self, "_pq_row_widgets", None) or {}
        any_active = False
        for sess_id, refs in list(rows.items()):
            try:
                if not refs["status_lbl"].winfo_exists():
                    continue
            except Exception:
                continue
            session = refs["session"]
            text, fg = self._pq_status_for(session)
            try:
                refs["status_lbl"].config(text=text, fg=fg)
                refs["words_lbl"].config(
                    text="{:,}".format(len(session.get("transcript", []))))
            except tk.TclError:
                continue
            if session.get("_progress", {}).get("active"):
                any_active = True

        if any_active:
            self.after(600, self._pq_tick_project_status)

    def _pq_status_for(self, session):
        """Return (status_text, fg_color) for a session row."""
        n_media = len(session.get("media", []))
        n_words = len(session.get("transcript", []))
        prog    = session.get("_progress", {})
        if prog.get("active"):
            phase = prog.get("phase", "transcribing")
            extra = prog.get("extra", "")
            return "● {}{}".format(phase, extra), ACCENT
        if n_media == 0:
            return "no media", WARN
        if n_words == 0:
            return "needs transcription", WARN
        return "✓ transcribed", SUCCESS

    def _pq_render_session_row(self, parent, session):
        """Render one row in the project's session list, storing widget
        references in self._pq_row_widgets so tick updates don't rebuild
        the whole tree."""
        row = tk.Frame(parent, bg=SURF, cursor="hand2")
        row.pack(fill="x")
        n_media = len(session.get("media", []))
        n_words = len(session.get("transcript", []))
        status_text, status_fg = self._pq_status_for(session)

        tok_lbl = tk.Label(row, text=session.get("token", "?"),
                            font=FBT, bg=SURF, fg=ACCENT,
                            anchor="w", padx=12, pady=8, width=16)
        tok_lbl.pack(side="left")
        files_lbl = tk.Label(row, text=str(n_media), font=FB,
                              bg=SURF, fg=TEXT, anchor="w",
                              padx=12, pady=8, width=8)
        files_lbl.pack(side="left")
        words_lbl = tk.Label(row, text="{:,}".format(n_words), font=FB,
                              bg=SURF, fg=TEXT, anchor="w",
                              padx=12, pady=8, width=12)
        words_lbl.pack(side="left")

        # Inline delete affordance (right-edge ✕).  Packed BEFORE the
        # status label so the status fills the rest of the row width.
        rm_lbl = tk.Label(row, text="✕", font=FBT, bg=SURF, fg=SUB,
                           cursor="hand2", padx=12, pady=8)
        rm_lbl.pack(side="right")

        status_lbl = tk.Label(row, text=status_text, font=FB,
                               bg=SURF, fg=status_fg, anchor="w",
                               padx=12, pady=8)
        status_lbl.pack(side="left", fill="x", expand=True)

        if not hasattr(self, "_pq_row_widgets"):
            self._pq_row_widgets = {}
        self._pq_row_widgets[id(session)] = {
            "row":         row,
            "tok_lbl":     tok_lbl,
            "files_lbl":   files_lbl,
            "words_lbl":   words_lbl,
            "status_lbl":  status_lbl,
            "rm_lbl":      rm_lbl,
            "session":     session,
        }

        def _open_handler(sess=session):
            return lambda e: self._pq_open_session_view(sess)
        handler = _open_handler()
        # Click anywhere in the row except the ✕ → open the session.
        row.bind("<Button-1>", handler)
        for child in row.winfo_children():
            if child is rm_lbl:
                continue
            child.bind("<Button-1>", handler)

        def _on_remove(_e=None, sess=session):
            self._pq_remove_session(sess)
        rm_lbl.bind("<Button-1>", _on_remove)
        rm_lbl.bind("<Enter>", lambda e: rm_lbl.config(fg=ERR))
        rm_lbl.bind("<Leave>", lambda e: rm_lbl.config(fg=SUB))

        def _hover_in(e, r=row, rm=rm_lbl):
            r.config(bg=SURF2)
            for c in r.winfo_children():
                if c is rm:
                    rm.config(bg=SURF2)
                else:
                    c.config(bg=SURF2)
        def _hover_out(e, r=row, rm=rm_lbl):
            r.config(bg=SURF)
            for c in r.winfo_children():
                if c is rm:
                    rm.config(bg=SURF)
                else:
                    c.config(bg=SURF)
        row.bind("<Enter>", _hover_in)
        row.bind("<Leave>", _hover_out)

    # ── Add Session dialog (chooser between New / Existing) ─────────────────

    def _pq_add_session_dialog(self):
        win = tk.Toplevel(self)
        win.title("Add Interview Session")
        win.configure(bg=BG)
        win.transient(self); win.grab_set()
        win.resizable(False, False)

        tk.Label(win, text="Add Interview Session", font=FH,
                 bg=BG, fg=TEXT, pady=14, padx=24).pack()

        body = tk.Frame(win, bg=BG)
        body.pack(padx=24, pady=(0, 16))

        def _new():
            win.destroy()
            self._pq_new_session_dialog()

        def _existing():
            win.destroy()
            path = filedialog.askopenfilename(
                title="Pick Interview Session JSON",
                filetypes=[("Pull Quotes Session", "*.pb_session.json"),
                           ("JSON", "*.json"),
                           ("All", "*.*")])
            if not path:
                return
            loaded = self._pq_load_session_file(path)
            if loaded is None:
                messagebox.showerror(
                    "Load failed",
                    "Not a valid Interview Session JSON:\n\n" + path)
                return
            if any(s.get("_file_path") == path
                   for s in self._pq_project["sessions"]):
                messagebox.showinfo("Already added",
                    "This session is already part of the project.")
                return
            self._pq_project["sessions"].append(loaded)
            self._pq_render_project_view()

        self._btn(body, "NEW INTERVIEW SESSION", _new,
                  color=ACCENT).pack(side="left", padx=(0, 12))
        self._btn(body, "ADD EXISTING SESSION", _existing).pack(side="left")

        cancel_row = tk.Frame(win, bg=BG)
        cancel_row.pack(pady=(0, 16))
        self._btn(cancel_row, "CANCEL", win.destroy, small=True).pack()

        win.update_idletasks()
        # Centre on parent
        pw = self.winfo_width(); ph = self.winfo_height()
        px = self.winfo_rootx(); py = self.winfo_rooty()
        ww = win.winfo_width();  wh = win.winfo_height()
        win.geometry("+{}+{}".format(px + (pw - ww) // 2, py + (ph - wh) // 2))
        self.wait_window(win)

    # ── New Session dialog ──────────────────────────────────────────────────

    def _pq_new_session_dialog(self):
        import re
        win = tk.Toplevel(self)
        win.title("New Interview Session")
        win.configure(bg=BG)
        win.transient(self); win.grab_set()
        win.minsize(900, 700)

        # Center on parent BEFORE wait_window blocks
        def _center():
            win.update_idletasks()
            pw = self.winfo_width();  ph = self.winfo_height()
            px = self.winfo_rootx(); py = self.winfo_rooty()
            ww = max(900, win.winfo_reqwidth())
            wh = max(700, win.winfo_reqheight())
            win.geometry("{}x{}+{}+{}".format(
                ww, wh,
                px + max(0, (pw - ww) // 2),
                py + max(0, (ph - wh) // 2)))
        win.after(10, _center)

        tk.Label(win, text="New Interview Session", font=FH,
                 bg=BG, fg=TEXT, padx=24, pady=14).pack(anchor="w")

        # Token entry
        tok_row = tk.Frame(win, bg=BG)
        tok_row.pack(fill="x", padx=24, pady=(0, 10))
        tk.Label(tok_row, text="SESSION TOKEN", font=FL,
                 bg=BG, fg=SUB, width=18, anchor="w").pack(side="left")
        tok_var = tk.StringVar(value="")
        tok_entry = tk.Entry(tok_row, textvariable=tok_var, font=FB,
                              bg=SURF2, fg=TEXT, insertbackground=TEXT,
                              relief="flat", bd=6, width=24)
        tok_entry.pack(side="left")
        tk.Label(tok_row, text="(uppercase letters / digits / underscores)",
                 font=FB, bg=BG, fg=SUB).pack(side="left", padx=(10, 0))

        # Media drop zone
        drop = tk.Frame(win, bg=SURF2,
                        highlightbackground=BORDER, highlightthickness=1)
        drop.pack(fill="both", expand=True, padx=24, pady=(0, 8))

        hdr = tk.Frame(drop, bg=SURF3)
        hdr.pack(fill="x")
        tk.Label(hdr, text="MEDIA FILES", font=FL, bg=SURF3, fg=SUB,
                 anchor="w", padx=12, pady=6).pack(side="left")
        count_lbl = tk.Label(hdr, text="0 files", font=FB,
                              bg=SURF3, fg=SUB, padx=12, pady=6)
        count_lbl.pack(side="right")

        list_canvas = tk.Canvas(drop, bg=SURF2, highlightthickness=0, height=180)
        list_scroll = _SlimScrollbar(drop, command=list_canvas.yview)
        list_scroll.pack(side="right", fill="y")
        list_canvas.pack(fill="both", expand=True)
        list_canvas.configure(yscrollcommand=list_scroll.set)
        list_inner = tk.Frame(list_canvas, bg=SURF2)
        list_canvas.create_window((0, 0), window=list_inner, anchor="nw")
        list_inner.bind("<Configure>",
                        lambda e: list_canvas.configure(
                            scrollregion=list_canvas.bbox("all")))

        media_paths = []
        _user_typed_token = [False]

        def _refresh_list():
            for w_ in list(list_inner.winfo_children()):
                w_.destroy()
            count_lbl.config(text="{} file{}".format(
                len(media_paths), "s" if len(media_paths) != 1 else ""))
            if not media_paths:
                tk.Label(list_inner,
                         text="  Drop files here or click + BROWSE below.",
                         font=FB, bg=SURF2, fg=SUB,
                         padx=8, pady=12).pack(anchor="w")
            else:
                for p in media_paths:
                    rrow = tk.Frame(list_inner, bg=SURF2)
                    rrow.pack(fill="x", padx=8, pady=2)
                    kind = "VIDEO" if is_video(p) else "AUDIO"
                    kc   = (("#1e3a1e","#5aab61") if kind == "VIDEO"
                            else ("#1e2a3a","#5588cc"))
                    tk.Label(rrow, text=kind, font=("Courier New", 9, "bold"),
                             bg=kc[0], fg=kc[1], padx=5, pady=2
                             ).pack(side="left")
                    tk.Label(rrow, text="  " + os.path.basename(p),
                             font=FB, bg=SURF2, fg=TEXT,
                             anchor="w").pack(side="left",
                                              fill="x", expand=True)
                    rm = tk.Label(rrow, text="✕", font=FB,
                                  bg=SURF2, fg=SUB, cursor="hand2", padx=8)
                    rm.pack(side="right")
                    def _remove(_e, _p=p):
                        if _p in media_paths:
                            media_paths.remove(_p)
                        _refresh_list()
                        if not _user_typed_token[0]:
                            tok_var.set(self._pq_suggest_token(media_paths))
                    rm.bind("<Button-1>", _remove)
            if not _user_typed_token[0]:
                tok_var.set(self._pq_suggest_token(media_paths))

        def _add_paths(paths):
            for p in paths:
                p = os.path.abspath(str(p).strip().strip("{}"))
                if not p or not is_media(p) or p in media_paths:
                    continue
                media_paths.append(p)
            _refresh_list()

        def _on_token_typed(*_):
            _user_typed_token[0] = bool(tok_var.get().strip())
        tok_var.trace_add("write", _on_token_typed)

        def _browse():
            files = filedialog.askopenfilenames(
                title="Pick interview media",
                filetypes=[("Media",
                            "*.wav *.aif *.aiff *.bwf *.mp3 *.m4a *.flac "
                            "*.mp4 *.mov *.mxf *.mkv *.avi *.m4v"),
                           ("All", "*.*")])
            if files:
                _add_paths(files)

        if HAS_DND:
            for w_ in (drop, list_canvas, list_inner):
                w_.drop_target_register(DND_FILES)
                w_.dnd_bind("<<Drop>>",
                            lambda e: _add_paths(parse_dnd(e.data)))

        ctl = tk.Frame(win, bg=BG)
        ctl.pack(fill="x", padx=24, pady=(0, 16))
        self._btn(ctl, "+ BROWSE", _browse, small=True).pack(side="left")

        nav = tk.Frame(win, bg=BG)
        nav.pack(fill="x", padx=24, pady=(0, 16))

        def _go():
            token = tok_var.get().strip().upper()
            token = re.sub(r"[^A-Z0-9_]+", "_", token).strip("_")
            if not token:
                messagebox.showwarning("Token required",
                    "Enter a session token name.", parent=win)
                return
            if not media_paths:
                messagebox.showwarning("No media",
                    "Add at least one media file.", parent=win)
                return
            for s in self._pq_project["sessions"]:
                if s.get("token") == token:
                    messagebox.showwarning("Token already in use",
                        "This Episode Project already has a session with "
                        "token '{}'.  Pick a different token.".format(token),
                        parent=win)
                    return

            session_path = self._pq_default_session_path(token, media_paths)
            if not session_path:
                messagebox.showerror("Save path",
                    "Could not derive a session save path.", parent=win)
                return
            session = {
                "version":    1,
                "workflow":   "interview_session",
                "token":      token,
                "media":      list(media_paths),
                "transcript": [],
                "_file_path": session_path,
            }
            self._pq_project["sessions"].append(session)
            self._pq_save_session_file(session)   # write empty-transcript shell
            win.destroy()
            self._pq_open_session_view(session, transcribe_now=True)

        _W = 14
        self._btn(nav, "TRANSCRIBE  →", _go,
                  color=ACCENT, width=_W).pack(side="right")
        self._btn(nav, "CANCEL", win.destroy, width=_W
                  ).pack(side="right", padx=(0, 8))

        _refresh_list()
        tok_entry.focus_set()
        self.wait_window(win)

    def _pq_suggest_token(self, paths):
        """Suggest a token name from filename common content."""
        import re as _re
        if not paths:
            return ""
        names = [os.path.splitext(os.path.basename(p))[0] for p in paths]
        SKIP = {"riverside", "raw", "audio", "video", "session", "interview",
                "speaker", "take", "cfr", "v1", "v2", "v3"}
        def _toks(n):
            n = _re.sub(r"[_\-.]+", " ", n.lower())
            n = _re.sub(r"[^a-z0-9 ]+", " ", n)
            out = []
            for w_ in n.split():
                if w_ in SKIP:               continue
                if w_.startswith("speaker"): continue
                if _re.fullmatch(r"\d+", w_):  continue
                out.append(w_)
            return out
        tok_lists = [_toks(n) for n in names]
        if not any(tok_lists):
            return _re.sub(r"[^A-Z0-9_]+", "_",
                          names[0].upper()).strip("_")
        # Words common to ALL files (in single-file case, that's all of them)
        common = set(tok_lists[0])
        for t in tok_lists[1:]:
            common &= set(t)
        if not common:
            for t in tok_lists:
                if t:
                    return t[0].upper()
            return ""
        # Preserve order from first filename
        ordered = [w_ for w_ in tok_lists[0] if w_ in common]
        return "_".join(ordered).upper()

    # ── Session view ────────────────────────────────────────────────────────

    def _pq_open_session_view(self, session, transcribe_now=False):
        self._clear()
        self._pq_current_session = session
        # Restore any state stashed by an orientation-driven rebuild.
        _stash = getattr(self, "_pq_layout_resize_stash", None)
        self._pq_layout_resize_stash = None

        # ── Bottom bar — consolidates BACK / hints / 📝 notes / PLAY / COPY
        # onto one persistent row, so even short windows never lose the
        # action buttons.  Left side: navigation + hint text.  Right side:
        # primary actions.  No separate action row.
        nav = tk.Frame(self.body, bg=BG)
        nav.pack(side="bottom", fill="x", pady=(8, 0))
        self._btn(nav, "← BACK TO PROJECT",
                  self._pq_render_project_view).pack(side="left")
        # Hint text — small enough to share the row, drops first if cramped.
        tk.Label(nav,
                 text="  Right-click for actions  ·  Space plays  ·  Ctrl+M note",
                 font=FB, bg=BG, fg=SUB).pack(side="left", padx=(12, 0))

        # ── Section title (clickable to collapse the info card) ──────────
        # The section row carries a chevron showing the collapse state of
        # the info card below it.  Clicking either the title or chevron
        # toggles the info card's visibility — gives the transcript more
        # vertical room on short windows without losing access to the
        # MEDIA list / TRANSCRIBE controls.  Match _section's visual
        # treatment (3 px ACCENT stripe on the left).
        section_row = tk.Frame(self.body, bg=BG)
        section_row.pack(fill="x", pady=(20, 6))
        tk.Frame(section_row, bg=ACCENT, width=3, height=20).pack(
            side="left", padx=(0, 10))
        # Persist collapse state across re-renders of the same session.
        # On the very first open, auto-collapse the info card when the
        # window is too short to comfortably show it + transcript.  With
        # the unified bottom bar this only fires on really small windows
        # — the user can always toggle it back manually.
        if not hasattr(self, "_pq_info_collapsed"):
            # winfo_height() reflects the LAST completed layout — no forced
            # flush needed.  The old code called self.update_idletasks()
            # here just to make the reading fresh, which forced a full
            # app-wide layout+paint MID-BUILD of the PQ session view.
            # That is why the session screen used to paint half-built and
            # then fill in.  Reading the pre-render value is fine: on
            # first open the app is already sized (either the maximised
            # zoom on Windows or the _center default on macOS/Linux), and
            # this branch runs only when the collapse flag doesn't exist.
            cur_h = self.winfo_height() or 800
            self._pq_info_collapsed = cur_h < 620
        self._pq_info_chevron = tk.Label(
            section_row,
            text="▼" if not self._pq_info_collapsed else "▶",
            font=FB, bg=BG, fg=SUB, cursor="hand2", padx=4)
        self._pq_info_chevron.pack(side="left")
        section_lbl = tk.Label(
            section_row,
            text="INTERVIEW SESSION  —  {}".format(session.get("token", "?")),
            font=FL, bg=BG, fg=TEXT, cursor="hand2")
        section_lbl.pack(side="left", padx=(2, 0))

        # Header info card — can be collapsed by clicking the chevron
        info = tk.Frame(self.body, bg=SURF,
                        highlightbackground=BORDER, highlightthickness=1)
        self._pq_info_card = info
        if not self._pq_info_collapsed:
            info.pack(fill="x", pady=(0, 12))
        inner = tk.Frame(info, bg=SURF)
        inner.pack(fill="x", padx=16, pady=12)

        def _toggle_info(_e=None):
            self._pq_info_collapsed = not self._pq_info_collapsed
            if self._pq_info_collapsed:
                info.pack_forget()
                self._pq_info_chevron.config(text="▶")
            else:
                # Re-pack BEFORE the PanedWindow so it appears above it.
                info.pack(fill="x", pady=(0, 12),
                          before=self._pq_layout_pw)
                self._pq_info_chevron.config(text="▼")
        self._pq_info_chevron.bind("<Button-1>", _toggle_info)
        section_lbl.bind("<Button-1>", _toggle_info)

        tk.Label(inner, text="TOKEN", font=FL,
                 bg=SURF, fg=SUB, width=10,
                 anchor="w").grid(row=0, column=0, sticky="w")
        tk.Label(inner, text=session.get("token", ""),
                 font=FBT, bg=SURF, fg=ACCENT,
                 anchor="w").grid(row=0, column=1, sticky="w")

        tk.Label(inner, text="MEDIA", font=FL,
                 bg=SURF, fg=SUB, width=10,
                 anchor="nw").grid(row=1, column=0, sticky="nw", pady=(8, 0))
        media_box = tk.Frame(inner, bg=SURF)
        media_box.grid(row=1, column=1, sticky="w", pady=(8, 0))
        self._pq_media_box = media_box
        self._pq_render_media_list(session)

        tk.Label(inner, text="FILE", font=FL,
                 bg=SURF, fg=SUB, width=10,
                 anchor="nw").grid(row=2, column=0, sticky="nw", pady=(8, 0))
        tk.Label(inner, text=session.get("_file_path", "(unsaved)"),
                 font=FB, bg=SURF, fg=SUB,
                 anchor="w").grid(row=2, column=1, sticky="w", pady=(8, 0))

        btn_row = tk.Frame(inner, bg=SURF)
        btn_row.grid(row=3, column=1, sticky="w", pady=(10, 0))
        self._btn(btn_row, "+ ADD MEDIA",
                  lambda s=session: self._pq_add_media_to_session(s),
                  small=True).pack(side="left")
        self._pq_retx_btn = self._btn(
            btn_row,
            ("⟳ RE-TRANSCRIBE" if session.get("transcript") else "⟳ TRANSCRIBE"),
            lambda s=session: self._pq_run_transcription(s),
            small=True, color=ACCENT)
        self._pq_retx_btn.pack(side="left", padx=(8, 0))

        # Inline model picker (dropdown + ? help).  Lives right next to
        # TRANSCRIBE so the user can change the model before clicking.
        self._build_model_picker(btn_row, bg=SURF).pack(side="left",
                                                        padx=(16, 0))

        # Background-mode checkbox — same intent as the script→session flow:
        # opting in makes the whisper worker yield so the rest of the system
        # stays responsive at the cost of slower decoding.
        bg_frame = tk.Frame(btn_row, bg=SURF)
        bg_frame.pack(side="left", padx=(16, 0))
        if not hasattr(self, "_pq_bg_mode_var"):
            self._pq_bg_mode_var = tk.BooleanVar(value=False)
        bg_ck = tk.Label(bg_frame,
                         text="☑" if self._pq_bg_mode_var.get() else "☐",
                         font=(_SANS, 14),
                         bg=SURF, fg=ACCENT if self._pq_bg_mode_var.get() else SUB,
                         cursor="hand2", padx=4)
        bg_ck.pack(side="left")
        bg_lbl = tk.Label(bg_frame, text="Background mode", font=FB,
                          bg=SURF, fg=SUB, cursor="hand2")
        bg_lbl.pack(side="left")
        def _toggle_bg():
            new_val = not self._pq_bg_mode_var.get()
            self._pq_bg_mode_var.set(new_val)
            bg_ck.config(text="☑" if new_val else "☐",
                         fg=ACCENT if new_val else SUB)
        bg_ck.bind("<Button-1>",  lambda e: _toggle_bg())
        bg_lbl.bind("<Button-1>", lambda e: _toggle_bg())

        # ── Resizable layout: transcript + notes ─────────────────────────
        # PanedWindow holds the transcript pane and the margin-notes
        # panel.  Orientation flips with the window width:
        #   ≥ PQ_LAYOUT_THRESHOLD px → horizontal (side-by-side)
        #   below                    → vertical (stacked)
        # The user can drag the sash freely between them.
        PQ_LAYOUT_THRESHOLD = 1100
        self.update_idletasks()
        cur_w = self.winfo_width() or 1200
        orient = "horizontal" if cur_w >= PQ_LAYOUT_THRESHOLD else "vertical"
        self._pq_layout_orient = orient
        self._pq_layout_pw = tk.PanedWindow(
            self.body, orient=orient, bg=BG,
            sashwidth=8, sashrelief="flat",
            sashpad=0, bd=0, opaqueresize=False,
            handlepad=0, showhandle=False)
        self._pq_layout_pw.pack(fill="both", expand=True, pady=(0, 8))

        # Drag the sash → glow.  Bind on the PanedWindow so we don't
        # have to know the sash widget id; Tk routes the events.
        self._pq_layout_pw.config(sashrelief="flat")

        # ── Transcript pane (header + search + text) ─────────────────────
        tx_outer = tk.Frame(self._pq_layout_pw, bg=SURF,
                            highlightbackground=BORDER, highlightthickness=1)
        # tx_outer is added to the PanedWindow at the bottom of this
        # function once the notes panel has been built too.

        # Header row: TRANSCRIPT label + search toggle + status
        tx_hdr = tk.Frame(tx_outer, bg=SURF3)
        tx_hdr.pack(fill="x")
        tk.Label(tx_hdr, text="TRANSCRIPT", font=FL,
                 bg=SURF3, fg=SUB, anchor="w",
                 padx=12, pady=6).pack(side="left")
        # Search toggle — the search row consumes a 40-50 px strip;
        # collapse-by-default lets the user reclaim that for the
        # transcript and pop it open with Ctrl+F or this icon.
        if not hasattr(self, "_pq_search_visible"):
            # Collapsed by default — the 🔍 button toggles it.  Saves
            # ~40 px of vertical space for the transcript itself
            # without making search hard to find.  Preference sticks
            # across re-renders via the hasattr check above.
            self._pq_search_visible = False
        self._pq_search_toggle = tk.Label(
            tx_hdr, text="🔍", font=FB,
            bg=SURF3, fg=ACCENT if self._pq_search_visible else SUB,
            cursor="hand2", padx=10, pady=6)
        self._pq_search_toggle.pack(side="right")
        self._pq_status_lbl = tk.Label(tx_hdr, text="", font=FB,
                                        bg=SURF3, fg=SUB,
                                        padx=12, pady=6)
        self._pq_status_lbl.pack(side="right")

        # ── Cursor timecode readout ──────────────────────────────────
        # Shows the source TC of the word under the transcript cursor
        # so the user can quickly adjust a pull's timecode in the
        # script without having to copy-paste to extract the time.
        # Click the readout to copy the TC to the clipboard.
        self._pq_cursor_tc_var = tk.StringVar(value="—")
        self._pq_cursor_tc_lbl = tk.Label(
            tx_hdr, textvariable=self._pq_cursor_tc_var, font=FBT,
            bg=SURF3, fg=ACCENT, cursor="hand2", padx=12, pady=6)
        self._pq_cursor_tc_lbl.pack(side="right")
        tk.Label(tx_hdr, text="AT", font=FB, bg=SURF3, fg=SUB,
                 padx=(0, 0), pady=6).pack(side="right")

        def _copy_cursor_tc(_e=None):
            v = self._pq_cursor_tc_var.get()
            if not v or v == "—":
                return
            try:
                self.clipboard_clear()
                self.clipboard_append(v)
                # Brief visual acknowledgement
                _orig = self._pq_cursor_tc_lbl.cget("fg")
                self._pq_cursor_tc_lbl.config(fg=SUCCESS)
                self.after(500, lambda: self._pq_cursor_tc_lbl.config(fg=_orig))
            except tk.TclError:
                pass
        self._pq_cursor_tc_lbl.bind("<Button-1>", _copy_cursor_tc)

        # Search row — Entry + match counter + ◀ ▶ navigation
        sr = tk.Frame(tx_outer, bg=SURF, padx=12, pady=6)
        if self._pq_search_visible:
            sr.pack(fill="x")
        self._pq_search_row = sr
        tk.Label(sr, text="🔍", font=FB, bg=SURF, fg=SUB
                 ).pack(side="left", padx=(0, 6))
        self._pq_search_var = tk.StringVar()
        sr_entry = tk.Entry(sr, textvariable=self._pq_search_var,
                             font=FB, bg=SURF2, fg=TEXT,
                             insertbackground=TEXT, relief="flat",
                             bd=4, width=32)
        sr_entry.pack(side="left", ipady=2)
        self._pq_search_entry = sr_entry
        self._pq_search_count_lbl = tk.Label(
            sr, text="", font=FB, bg=SURF, fg=SUB)
        self._pq_search_count_lbl.pack(side="left", padx=(8, 0))

        self._btn(sr, "▶", lambda: self._pq_search_step(+1),
                  small=True).pack(side="right", padx=(2, 0))
        self._btn(sr, "◀", lambda: self._pq_search_step(-1),
                  small=True).pack(side="right")
        # Debounce the scan.  _pq_search_apply walks the whole Text
        # widget and re-tags every hit; running it per keystroke froze
        # the UI for over a second on a long transcript while the user
        # was still typing.  150 ms matches the waveform editor's
        # search debounce, which had the same problem.
        def _schedule_pq_search(*_):
            _prev = getattr(self, "_pq_search_after_id", None)
            if _prev is not None:
                try: self.after_cancel(_prev)
                except Exception: pass
            self._pq_search_after_id = self.after(150, self._pq_search_apply)

        def _flush_pq_search():
            """Run any pending scan NOW so Enter never steps stale hits."""
            _prev = getattr(self, "_pq_search_after_id", None)
            if _prev is not None:
                try: self.after_cancel(_prev)
                except Exception: pass
                self._pq_search_after_id = None
                self._pq_search_apply()

        self._pq_search_var.trace_add("write", _schedule_pq_search)
        sr_entry.bind("<Return>",
                      lambda e: (_flush_pq_search(), self._pq_search_step(+1)))
        sr_entry.bind("<Shift-Return>",
                      lambda e: (_flush_pq_search(), self._pq_search_step(-1)))
        sr_entry.bind("<Escape>", lambda e: self._pq_search_clear())

        def _toggle_search(_e=None):
            self._pq_search_visible = not self._pq_search_visible
            if self._pq_search_visible:
                self._pq_search_row.pack(fill="x", before=tx_frame)
                self._pq_search_toggle.config(fg=ACCENT)
                try:
                    self._pq_search_entry.focus_set()
                except tk.TclError:
                    pass
            else:
                self._pq_search_row.pack_forget()
                self._pq_search_toggle.config(fg=SUB)
                # Clear any active search highlights when hidden.
                try:
                    self._pq_search_clear()
                except Exception:
                    pass
            return "break"
        self._pq_search_toggle.bind("<Button-1>", _toggle_search)
        # Ctrl+F focuses the search box, opening it if collapsed.
        def _focus_search(_e=None):
            if not self._pq_search_visible:
                _toggle_search()
            else:
                try:
                    self._pq_search_entry.focus_set()
                    self._pq_search_entry.selection_range(0, "end")
                except tk.TclError:
                    pass
            return "break"
        # Bind at top-level so it works from anywhere in the session view.
        self.bind("<Control-f>", _focus_search)
        self.bind("<Control-F>", _focus_search)

        # Text widget — explicit padx/pady gives a uniform inset all the
        # way down (the previous bd=8 + scrollbar packing produced the
        # progressively-narrower-margin glitch).
        tx_frame = tk.Frame(tx_outer, bg=SURF2)
        tx_frame.pack(fill="both", expand=True)
        tx_text = tk.Text(tx_frame, bg=SURF2, fg=TEXT,
                           insertbackground=TEXT, wrap="word",
                           relief="flat", bd=0, font=FB,
                           padx=14, pady=12,
                           spacing1=0, spacing2=0, spacing3=0,
                           tabs="",
                           selectbackground=ACCENT,
                           selectforeground=TEXT,
                           # Native undo as a safety net in case any
                           # programmatic insert path slips through.
                           undo=True, autoseparators=True, maxundo=-1)
        # lmargin1/lmargin2/rmargin are tag-only options.  Pin all three
        # to 0 — applied to every chunk of rendered text so wrapped lines
        # stay flush left and right.  lmargincolor is also explicitly
        # transparent (matches widget bg) to rule out any margin-band
        # rendering quirk.
        tx_text.tag_configure("body",
                               lmargin1=0, lmargin2=0, rmargin=0,
                               lmargincolor=SURF2, rmargincolor=SURF2)
        # Source-based fading: words from the tiny draft pass render
        # in muted SUB grey so the user can see at a glance which
        # parts of the transcript are still "draft" and which have
        # been confirmed by the configured (main) pass or replaced
        # by a user edit.  Tag must be raised above "body" so its
        # foreground wins for tagged char ranges.
        tx_text.tag_configure("body_tiny", foreground=SUB)
        tx_text.tag_raise("body_tiny", "body")
        # User-edited words get a thin underline — the conventional
        # "tracked changes" indicator.  Subtle enough not to clash
        # with reading, clear enough that the user always knows
        # which parts they touched (and can right-click to restore
        # the original Whisper text).
        tx_text.tag_configure("body_user", underline=True)
        tx_text.tag_raise("body_user", "body")
        # Playback "karaoke" highlight — the current word under the
        # playhead gets a warm orange-tinted background so the user
        # can follow along visually while audio plays.  Driven by
        # the periodic _pq_playback_highlight_tick poll while
        # _pq_playing.  The colour is ACCENT mixed ~25% into BG so
        # it reads as the same orange family without screaming.
        tx_text.tag_configure("body_playing", background="#4a2a14")
        tx_text.tag_raise("body_playing", "body")
        # Diagnostic on Ctrl+Shift+D — fast version: samples up to 30
        # logical lines spread evenly through the document and reports
        # each one's first display-line dimensions.  Capped iterations
        # so a giant transcript can't freeze the UI.
        def _diag(event=None, t=tx_text):
            try:
                t.update_idletasks()
                ww  = t.winfo_width()
                wh  = t.winfo_height()
                pad_l = int(t.cget("padx"))
                content_w = ww - pad_l * 2
                end_idx = t.index("end-1c")
                last_line = int(end_idx.split('.')[0])
                # Sample up to 30 lines evenly distributed
                if last_line <= 30:
                    sample = list(range(1, last_line + 1))
                else:
                    step = max(1, last_line // 30)
                    sample = list(range(1, last_line + 1, step))[:30]
                print("=" * 60)
                print("PQ-DIAG widget={}x{}  padx={}  content_w={}  "
                      "total_lines={}".format(ww, wh, pad_l,
                                               content_w, last_line))
                widths = []
                for ln in sample:
                    dli = t.dlineinfo("{}.0".format(ln))
                    text = t.get("{}.0".format(ln),
                                  "{}.end".format(ln))
                    text_n = len(text)
                    text_preview = (text[:50] + "…") if text_n > 50 else text
                    if dli is None:
                        print("  line {:>4}: (offscreen)  chars={}  {!r}".format(
                            ln, text_n, text_preview))
                        continue
                    x, y, w, h, _ = dli
                    widths.append(w)
                    print("  line {:>4}: x={:3d} y={:5d} w={:4d}  "
                          "chars={:3d}  {!r}".format(
                              ln, x, y, w, text_n, text_preview))
                if widths:
                    print("  rendered widths — min={} max={} avg={}".format(
                        min(widths), max(widths),
                        round(sum(widths)/len(widths), 1)))
            except Exception as e:
                import traceback
                traceback.print_exc()
            return "break"
        tx_text.bind("<Control-D>", _diag)
        tx_text.bind("<Control-d>", _diag)
        tx_sb = _SlimScrollbar(tx_frame, command=tx_text.yview)
        tx_sb.pack(side="right", fill="y")
        tx_text.configure(yscrollcommand=tx_sb.set)
        tx_text.pack(side="left", fill="both", expand=True)
        self._pq_tx_text = tx_text

        # ── Cursor timecode: keep _pq_cursor_tc_var in sync with the
        # text-widget insertion point.  Fires on click and any key that
        # moves the insertion cursor (arrows, Home/End, etc.).  Uses
        # binary search across _pq_word_index (ordered by abs char
        # position) so lookup stays O(log N) on long transcripts.
        import bisect as _bisect
        def _update_cursor_tc(_e=None, t=tx_text):
            idx = getattr(self, "_pq_word_index", None)
            var = getattr(self, "_pq_cursor_tc_var", None)
            if var is None:
                return
            if not idx:
                var.set("—"); return
            try:
                pos = t.count("1.0", "insert")
                pos_i = int(pos[0]) if pos else 0
            except (tk.TclError, TypeError, ValueError):
                var.set("—"); return
            # bisect on the start-char field; then verify the cursor sits
            # inside that word's range OR pick the nearest neighbour.
            starts = [s for (s, _e, _w) in idx]
            i = _bisect.bisect_right(starts, pos_i) - 1
            if i < 0:
                i = 0
            elif i < len(idx) - 1 and idx[i][1] < pos_i:
                # Cursor is past this word's end; if the NEXT word starts
                # close, snap to it (feels natural when clicking whitespace).
                if idx[i + 1][0] - pos_i < pos_i - idx[i][1]:
                    i += 1
            w = idx[i][2]
            t_s = float(w.get("start", 0.0) or 0.0)
            var.set(secs_tc(t_s).split(".")[0])

        tx_text.bind("<ButtonRelease-1>", _update_cursor_tc, add="+")
        tx_text.bind("<KeyRelease>",       _update_cursor_tc, add="+")

        # Search match highlight tag
        tx_text.tag_configure("pq_match", background="#5a3a10",
                              foreground=TEXT)
        tx_text.tag_configure("pq_match_current", background=ACCENT,
                              foreground=TEXT)

        # Reset search state on view rebuild
        self._pq_search_hits = []      # list of (start_idx, end_idx)
        self._pq_search_cur  = -1

        self._pq_render_transcript_text(session)

        # Bind copy → @PULL
        for keyspec in ("<Control-c>", "<Control-C>",
                        "<Command-c>", "<Command-C>"):
            tx_text.bind(keyspec, self._pq_copy_as_pull)

        # Undo / redo — Ctrl+Z is bound by Tk's Text class when undo=True,
        # but we add explicit redo on Ctrl+Y AND Ctrl+Shift+Z so users
        # get whichever convention they expect.
        def _do_undo(event=None, t=tx_text):
            try: t.edit_undo()
            except tk.TclError: pass   # nothing to undo
            return "break"
        def _do_redo(event=None, t=tx_text):
            try: t.edit_redo()
            except tk.TclError: pass   # nothing to redo
            return "break"
        # Ctrl+Z is already wired by Tk class binding; bind it explicitly
        # too so it always reaches our edit_undo even if some future
        # bind_all swallows it.
        tx_text.bind("<Control-z>",       _do_undo)
        tx_text.bind("<Control-Z>",       _do_undo)
        tx_text.bind("<Control-y>",       _do_redo)
        tx_text.bind("<Control-Y>",       _do_redo)
        tx_text.bind("<Control-Shift-z>", _do_redo)
        tx_text.bind("<Control-Shift-Z>", _do_redo)

        # ── Always-on read-only with right-click context menu ─────────
        # The transcript is permanently read-only.  Editing happens via
        # right-click → "Edit text…" on a selection.  Spacebar always
        # plays the current selection (or cursor-to-end).  Ctrl+M still
        # adds a margin note for the current selection.
        def _add_note(event=None):
            self._pq_add_margin_note()
            return "break"
        tx_text.bind("<Control-m>", _add_note)
        tx_text.bind("<Control-M>", _add_note)
        self.bind("<Control-m>", _add_note)
        self.bind("<Control-M>", _add_note)

        # Spacebar plays the current selection.  Shift+Space stays as a
        # convenience alias.
        def _space_play(event=None):
            self._pq_play_selection()
            return "break"
        tx_text.bind("<space>",               _space_play)
        tx_text.bind("<Key-space>",           _space_play)
        tx_text.bind("<Shift-space>",         _space_play)
        tx_text.bind("<Shift-Key-space>",     _space_play)
        sr_entry.bind("<Shift-space>",        _space_play)
        sr_entry.bind("<Shift-Key-space>",    _space_play)

        # Read-only enforcement — block any keypress that would modify
        # the buffer.  Allow navigation, modifier keys, and Ctrl+key
        # shortcuts (which we wire explicitly elsewhere).
        ALLOWED_KEYSYMS = {
            "Up", "Down", "Left", "Right", "Home", "End", "Prior", "Next",
            "Tab", "Shift_L", "Shift_R", "Control_L", "Control_R",
            "Alt_L", "Alt_R", "Meta_L", "Meta_R", "Caps_Lock",
            "Num_Lock", "Scroll_Lock",
        }
        # Ctrl keys the Text widget binds to BUILT-IN editing (emacs
        # style) that would mutate the buffer: Ctrl+D delete-char,
        # Ctrl+H backspace, Ctrl+K kill-line, Ctrl+O open-line,
        # Ctrl+T transpose.  Everything else with Ctrl is allowed so the
        # wired shortcuts (Ctrl+C copy, Ctrl+A select-all, Ctrl+S save,
        # Ctrl+M margin note, …) keep working.
        CTRL_EDIT_KEYSYMS = {"d", "h", "k", "o", "t"}
        def _maybe_block_edit(event):
            if event.keysym in ALLOWED_KEYSYMS:
                return None
            ctrl_pressed = (event.state & 0x4) != 0
            if ctrl_pressed:
                if event.keysym.lower() in CTRL_EDIT_KEYSYMS:
                    return "break"
                return None
            return "break"
        tx_text.bind("<KeyPress>", _maybe_block_edit)

        # Block clipboard mutations of the read-only transcript.  Typing
        # and Delete/BackSpace are already stopped above, but the Text
        # widget's class-level virtual events still fire: an accidental
        # Ctrl+V (paste) could drop clipboard text over the selection
        # (reported — easy to hit instead of Ctrl+C), and Ctrl+X (cut) /
        # middle-click paste are the same hazard.  Copy and Select-All
        # stay enabled.  Returning "break" from the instance binding
        # preempts the class binding that performs the edit.
        # <<PasteSelection>> covers X11 middle-click paste; the rest cover
        # Ctrl+V / Ctrl+X / Clear on every platform.
        for _mut_evt in ("<<Paste>>", "<<PasteSelection>>",
                         "<<Cut>>", "<<Clear>>"):
            tx_text.bind(_mut_evt, lambda e: "break")

        # Right-click context menu — the only way to mutate the
        # transcript.  Always offers the actions that make sense on
        # the current selection (or graceful no-op when there isn't
        # one).
        def _show_ctx(event):
            try:
                has_sel = bool(tx_text.tag_ranges("sel"))
            except tk.TclError:
                has_sel = False
            # Does the selection (or cursor word, when no selection)
            # touch any user-edited entries with restore data?  Drives
            # whether the "Restore Whisper original" item shows.
            has_restorable = self._pq_selection_has_user_edits()
            menu = tk.Menu(tx_text, tearoff=0,
                            bg=SURF2, fg=TEXT,
                            activebackground=ACCENT,
                            activeforeground=TEXT,
                            bd=0)
            menu.add_command(
                label="Copy as @PULL",
                command=self._pq_copy_as_pull,
                state="normal" if has_sel else "disabled")
            menu.add_command(
                label="Play selection (Space)",
                command=self._pq_play_selection,
                state="normal" if has_sel else "disabled")
            menu.add_command(
                label="Add margin note (Ctrl+M)",
                command=self._pq_add_margin_note,
                state="normal" if has_sel else "disabled")
            menu.add_separator()
            menu.add_command(
                label="Edit selected text…",
                command=self._pq_edit_selection,
                state="normal" if has_sel else "disabled")
            if has_restorable:
                menu.add_command(
                    label="Restore Whisper original",
                    command=self._pq_restore_originals)
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
        tx_text.bind("<Button-3>", _show_ctx)

        # Ctrl+R / F5 → re-render the transcript from the same in-memory
        # data.  Useful when iterating on rendering tweaks: change the
        # constants in _pq_render_transcript_text, restart PostBridge once,
        # open the session, then this binding lets us re-run the renderer
        # without leaving the view.
        def _rerender(event=None, sess=session):
            self._pq_render_transcript_text(sess)
            return "break"
        tx_text.bind("<Control-r>", _rerender)
        tx_text.bind("<Control-R>", _rerender)
        tx_text.bind("<F5>",        _rerender)
        self.bind("<F5>", _rerender)

        # ── Margin notes panel ───────────────────────────────────────
        # Lives inside the PanedWindow alongside the transcript.  When
        # notes exist, the pane is visible; collapse via the ⇲ button
        # in the header (forgets the pane); a 📝 button in the action
        # row brings it back.
        self._pq_notes_outer = tk.Frame(self._pq_layout_pw, bg=SURF,
                                          highlightbackground=BORDER,
                                          highlightthickness=1)
        notes_hdr = tk.Frame(self._pq_notes_outer, bg=SURF3)
        notes_hdr.pack(fill="x")
        tk.Label(notes_hdr, text="MARGIN NOTES", font=FL,
                 bg=SURF3, fg=SUB, anchor="w",
                 padx=12, pady=6).pack(side="left")
        # Collapse button — removes the pane from the PanedWindow.
        self._pq_notes_collapse_btn = tk.Label(
            notes_hdr, text="✕", font=FBT, bg=SURF3, fg=SUB,
            cursor="hand2", padx=10, pady=6)
        self._pq_notes_collapse_btn.pack(side="right")
        self._pq_notes_collapse_btn.bind(
            "<Enter>", lambda e: self._pq_notes_collapse_btn.config(fg=ERR))
        self._pq_notes_collapse_btn.bind(
            "<Leave>", lambda e: self._pq_notes_collapse_btn.config(fg=SUB))
        self._pq_notes_collapse_btn.bind(
            "<Button-1>", lambda e: self._pq_toggle_notes_pane(False))
        self._pq_notes_count_lbl = tk.Label(
            notes_hdr, text="", font=FB, bg=SURF3, fg=SUB,
            padx=8, pady=6)
        self._pq_notes_count_lbl.pack(side="right")

        notes_body = tk.Frame(self._pq_notes_outer, bg=SURF)
        notes_body.pack(fill="both", expand=True)
        self._pq_notes_canvas = tk.Canvas(
            notes_body, bg=SURF, highlightthickness=0)
        notes_sb = _SlimScrollbar(notes_body,
                                   command=self._pq_notes_canvas.yview)
        notes_sb.pack(side="right", fill="y")
        self._pq_notes_canvas.pack(side="left", fill="both", expand=True)
        self._pq_notes_canvas.configure(yscrollcommand=notes_sb.set)
        self._pq_notes_inner = tk.Frame(self._pq_notes_canvas, bg=SURF)
        self._pq_notes_canvas.create_window(
            (0, 0), window=self._pq_notes_inner, anchor="nw")
        self._pq_notes_inner.bind(
            "<Configure>",
            lambda e: self._pq_notes_canvas.configure(
                scrollregion=self._pq_notes_canvas.bbox("all")))
        # When the canvas resizes (panel sash drag, window resize, or
        # layout flip), reflow each note's body text + snippet so the
        # wraplength matches the new available width.
        self._pq_notes_canvas.bind(
            "<Configure>",
            lambda e: self._pq_update_notes_wraplength(e.width))

        # Mouse-wheel routing: a global bind_all handler from
        # _scroll_frame steals wheel events meant for this panel, so
        # bind directly on the panel widgets and return "break" to
        # short-circuit the global chain.  _pq_bind_wheel_to is
        # called again inside _pq_render_notes_panel to cover the
        # row widgets that get rebuilt every refresh.
        self._pq_bind_wheel_to(self._pq_notes_outer)

        # Add panes to the PanedWindow.  Transcript first (it gets the
        # leading position regardless of orient), notes second when any
        # exist.  The notes pane is added/removed dynamically by
        # _pq_render_notes_panel based on note presence.
        self._pq_layout_pw.add(tx_outer, minsize=320, stretch="always")
        self._pq_render_notes_panel()

        # Primary actions live on the same bottom row as BACK TO PROJECT.
        # Right-anchored so they hug the right edge regardless of how much
        # the hint text on the left consumes.
        self._btn(nav, "COPY SELECTION AS @PULL",
                  self._pq_copy_as_pull,
                  color=ACCENT).pack(side="right")
        self._pq_play_btn = self._btn(
            nav, "▶ PLAY SELECTION",
            self._pq_play_selection)
        self._pq_play_btn.pack(side="right", padx=(0, 8))

        # 📝 notes-pane toggle sits between hint text and PLAY/COPY.
        self._pq_notes_toggle_btn = tk.Label(
            nav, text="📝", font=FB, bg=SURF3, fg=TEXT,
            cursor="hand2", padx=10, pady=4)
        self._pq_notes_toggle_btn.pack(side="right", padx=(0, 12))
        self._pq_notes_toggle_btn.bind(
            "<Button-1>",
            lambda e: self._pq_toggle_notes_pane(None))
        self._pq_refresh_notes_toggle_btn()

        # Apply current mode to update badge styling + key behaviour
        self._pq_apply_mode()

        # Restore stashed state from an orient-rebuild, if any.
        if _stash:
            try:
                if "search" in _stash and _stash["search"]:
                    self._pq_search_var.set(_stash["search"])
                if "collapsed" in _stash:
                    self._pq_notes_user_collapsed = bool(_stash["collapsed"])
                    self._pq_render_notes_panel()
                    self._pq_refresh_notes_toggle_btn()
                if "yview" in _stash and _stash["yview"]:
                    self.after(50, lambda y=_stash["yview"]:
                               self._pq_tx_text.yview_moveto(y[0])
                               if hasattr(self, "_pq_tx_text") else None)
            except Exception:
                pass

        # Layout-driven orient switching: when the window is resized
        # across PQ_LAYOUT_THRESHOLD, rebuild the view with the other
        # orient.  Debounced inside _pq_on_layout_resize.
        # Bind on the toplevel root so we catch every Configure event.
        if not getattr(self, "_pq_layout_resize_bound", False):
            self.bind("<Configure>", self._pq_on_layout_resize, add="+")
            self._pq_layout_resize_bound = True

        if transcribe_now:
            self.after(80,
                       lambda s=session: self._pq_run_transcription(s))
        elif session.get("_progress", {}).get("active"):
            # The user opened a session that's already mid-transcription
            # (kicked off earlier and they navigated back).  Resume the
            # animation and show the placeholder pane.
            if (btn := getattr(self, "_pq_retx_btn", None)):
                btn.config(text="TRANSCRIBING…", fg=SUB)
            self._pq_render_progress_placeholder()
            self._pq_animate_progress()

    def _pq_render_media_list(self, session):
        box = getattr(self, "_pq_media_box", None)
        if not box:
            return
        for w_ in list(box.winfo_children()):
            w_.destroy()
        media = session.get("media", [])
        if not media:
            tk.Label(box, text="(no media)", font=FB,
                     bg=SURF, fg=SUB).pack(anchor="w")
            return
        for p in media:
            row = tk.Frame(box, bg=SURF)
            row.pack(anchor="w")
            ok = os.path.isfile(p)
            tk.Label(row, text=("✓ " if ok else "✗ "),
                     font=FB, bg=SURF,
                     fg=SUCCESS if ok else ERR
                     ).pack(side="left")
            tk.Label(row, text=p,
                     font=FB, bg=SURF,
                     fg=TEXT if ok else ERR
                     ).pack(side="left")

    def _pq_capture_text_state(self):
        """Snapshot the text widget's interactive state so a streaming
        re-render can restore it instead of jumping the user to the end.

        Anchors cursor / selection by the id() of the word-dict the
        cursor is currently in, not by character index — character
        indexes can shift slightly across re-renders (paragraph break
        positions move when new context arrives) but the word dicts
        themselves are stable across streaming updates.

        Returns a dict; pair with _pq_restore_text_state().
        """
        tx = getattr(self, "_pq_tx_text", None)
        if tx is None:
            return None
        state = {}
        try:
            if not tx.winfo_exists():
                return None
            word_idx = getattr(self, "_pq_word_index", []) or []
            # Cursor position by word id AND timestamp.  The id lets us
            # restore exactly when the word dict survives the re-render
            # (most ticks).  The timestamp is a fallback for the cases
            # where the merge replaces the cursor's word with a fresh
            # dict at the same audio time (refined pass overwriting
            # tiny words) — without it, the cursor jumps to the end of
            # the buffer mid-stream and feels twitchy.
            # word_idx is built in ascending start-char order by
            # _pq_render_transcript_text, so locate the containing word
            # by bisect rather than walking all 20-34k entries.  This
            # runs on every streaming tick alongside the re-render.
            def _word_at(pos):
                i = bisect.bisect_right(word_idx, pos,
                                        key=lambda it: it[0]) - 1
                if 0 <= i < len(word_idx):
                    s, e, w = word_idx[i]
                    if s <= pos <= e:
                        return w
                return None

            try:
                c = _tx_count_chars(tx, "1.0", tx.index("insert"))
                w = _word_at(c)
                if w is not None:
                    state["insert_word_id"] = id(w)
                    try:
                        state["insert_word_t"] = (
                            float(w.get("start", 0.0))
                            + float(w.get("end", 0.0))) / 2.0
                    except Exception:
                        pass
            except Exception:
                pass
            # Selection range by word ids
            try:
                f = _tx_count_chars(tx, "1.0", tx.index("sel.first"))
                l = _tx_count_chars(tx, "1.0", tx.index("sel.last"))
                wf = _word_at(f)
                wl = _word_at(l)
                if wf is not None and wl is not None:
                    state["sel_first_id"] = id(wf)
                    state["sel_last_id"]  = id(wl)
            except tk.TclError:
                pass
            # Scroll position — store the absolute DISTANCE from the
            # top, in display lines.  This is invariant to content
            # changes BELOW the viewport (the streaming case): as
            # more words append, the user's distance-from-top stays
            # the same so they keep seeing the same content.  Beats
            # any fraction-based or word-anchored scheme, because
            # the top is a fixed anchor that nothing can shift.
            try:
                state["top_display_lines"] = _tx_count_displaylines(
                    tx, "1.0", "@0,0")
            except Exception:
                pass
            # Was the widget focused?
            try:
                state["had_focus"] = (self.focus_get() is tx)
            except Exception:
                state["had_focus"] = False
        except Exception:
            return None
        return state

    def _pq_restore_text_state(self, state):
        """Restore cursor, selection, scroll, focus after a re-render.
        Pairs with _pq_capture_text_state."""
        if not state:
            return
        tx = getattr(self, "_pq_tx_text", None)
        if tx is None:
            return
        try:
            if not tx.winfo_exists():
                return
            word_idx = getattr(self, "_pq_word_index", []) or []
            # Cursor: try id-match first, then fall back to the word
            # whose audio time straddles the saved cursor timestamp.
            # The fallback keeps the cursor stable during streaming
            # when the refined pass swaps in fresh dicts for the same
            # audio range.
            # One pass to build id -> (start, end); the three lookups
            # below (cursor, selection start, selection end) each used
            # to walk all 20-34k entries independently, on every
            # streaming tick.
            _by_id = {}
            for s, e, w in word_idx:
                _by_id[id(w)] = (s, e)

            ins_id = state.get("insert_word_id")
            ins_t  = state.get("insert_word_t")
            placed = False
            if ins_id is not None:
                _hit = _by_id.get(ins_id)
                if _hit is not None:
                    try:
                        tx.mark_set("insert", "1.0+{}c".format(_hit[0]))
                        placed = True
                    except tk.TclError:
                        pass
            if not placed and ins_t is not None:
                best = None
                best_dt = float("inf")
                for s, e, w in word_idx:
                    try:
                        ws = float(w.get("start", 0.0))
                        we = float(w.get("end",   ws))
                    except Exception:
                        continue
                    if ws <= ins_t <= we:
                        best = s
                        best_dt = 0.0
                        break
                    mid = (ws + we) / 2.0
                    dt = abs(mid - ins_t)
                    if dt < best_dt:
                        best_dt = dt
                        best = s
                if best is not None:
                    try:
                        tx.mark_set("insert", "1.0+{}c".format(best))
                    except tk.TclError:
                        pass
            # Selection
            sf = state.get("sel_first_id")
            sl = state.get("sel_last_id")
            if sf is not None and sl is not None:
                new_first = new_last = None
                _hf = _by_id.get(sf)
                _hl = _by_id.get(sl)
                if _hf is not None:
                    new_first = "1.0+{}c".format(_hf[0])
                if _hl is not None:
                    new_last = "1.0+{}c".format(_hl[1])
                if new_first and new_last:
                    try:
                        tx.tag_remove("sel", "1.0", "end")
                        tx.tag_add("sel", new_first, new_last)
                    except tk.TclError:
                        pass
            # Scroll restore.  Reset yview to the top, then scroll
            # down by exactly the captured number of display lines.
            # No fraction math, no dlineinfo, no update_idletasks —
            # both calls happen inside the same Tk callback so the
            # widget only paints once with the final scroll state.
            n = state.get("top_display_lines")
            if n is not None:
                try:
                    tx.yview_moveto(0.0)
                    if n > 0:
                        tx.yview_scroll(int(n), "units")
                except (tk.TclError, ValueError):
                    pass
            # Restore focus
            if state.get("had_focus"):
                try:
                    tx.focus_set()
                except tk.TclError:
                    pass
        except Exception:
            pass

    def _pq_render_transcript_text(self, session, placeholder=None,
                                    user_frontier=None):
        """Re-render the transcript pane.

        When `placeholder` is given (non-empty string), show it instead of
        the transcript — used during transcription to display "loading
        model…" / "transcribing…" without flashing a misleading
        "click TRANSCRIBE" prompt.

        `user_frontier` (seconds, optional) hides user-edited words
        whose audio start exceeds it.  Used by the streaming render
        and the pre-stream wipe so preserved edits stay invisible
        until the streaming transcription actually reaches them —
        without removing them from session["transcript"], so they're
        not lost on the next merge tick.
        """
        tx = getattr(self, "_pq_tx_text", None)
        if not tx:
            return
        words = session.get("transcript") or []
        if user_frontier is not None and words:
            words = [w for w in words
                     if w.get("_src") != "user"
                     or float(w.get("start", 0.0)) <= user_frontier]
        tx.configure(state="normal")
        tx.delete("1.0", "end")
        if placeholder:
            tx.insert("1.0", placeholder)
            self._pq_word_index = []
        elif not words:
            tx.insert("1.0",
                      "(no transcript yet — click TRANSCRIBE to generate one)")
            self._pq_word_index = []
        else:
            # ─── Render strategy ───────────────────────────────────────
            # 1. Walk word entries in chronological order.  Whenever
            #    the speaker changes, emit a visible "<SPEAKER>:\n"
            #    label tagged with the "speaker_lbl" style.  Speaker
            #    labels are excluded from word_index so they don't
            #    appear in @PULL clipboard output.
            # 2. Within each speaker run, concatenate words into a
            #    continuous string and run the syntactic paragraph
            #    detector — same logic that handled the single-speaker
            #    case.
            # 3. Insert breaks within speaker runs; speaker turns get a
            #    full blank-line separation regardless.
            tx.tag_configure("speaker_lbl",
                              foreground=ACCENT, font=FBT,
                              spacing1=10, spacing3=4,
                              lmargin1=0, lmargin2=0, rmargin=0)
            # Double-clicking any speaker label opens an inline rename
            # dialog that updates the label across every session row in
            # the project — the global rename Jordan asked for.
            tx.tag_bind(
                "speaker_lbl", "<Double-Button-1>",
                lambda e, t=tx: self._pq_inline_rename_speaker(e, t))

            self._pq_word_index = []

            # Decide whether tiny-tagged words should render faded.
            # Two conditions must hold:
            #   1. A transcription is actively running on this session
            #      (so "tiny" words are a transient draft).
            #   2. The configured model is bigger than tiny (so a
            #      refinement pass is actually coming for them).
            # Without both, tiny-tagged words are the user's final
            # answer and should render solid like any other.
            _prog = session.get("_progress") or {}
            _fade_tiny = (bool(_prog.get("active"))
                          and engines.get_active_model_size() != "tiny")

            # Group consecutive words by speaker
            real_words = [w for w in words
                          if not w.get("break") and (w.get("word") or "").strip()]

            # Detect if any speakers are actually labelled (multi-track)
            speakers_present = any(w.get("speaker") for w in real_words)

            # Filter out cross-track filler/bleed: when one speaker says
            # something brief (e.g. "yeah", "mm-hmm", "right") sandwiched
            # between long stretches of another speaker, that's almost
            # always either bleed picked up by the wrong mic or a
            # listening filler.  Removed at render time so the original
            # transcript is preserved on disk.
            if speakers_present:
                real_words = self._pq_filter_filler_interjections(real_words)

            # Group into [(speaker, [words]), ...]
            groups = []
            cur_speaker = object()
            cur_words   = []
            for w in real_words:
                sp = w.get("speaker") if speakers_present else None
                if sp != cur_speaker:
                    if cur_words:
                        groups.append((cur_speaker, cur_words))
                    cur_speaker = sp
                    cur_words   = []
                cur_words.append(w)
            if cur_words:
                groups.append((cur_speaker, cur_words))

            # Render each group: optional speaker label + paragraphed prose
            for gi, (sp, gwords) in enumerate(groups):
                # Insert speaker label (only when we have speaker info)
                if sp and speakers_present:
                    if gi > 0:
                        tx.insert("end", "\n\n")
                    # Honour per-session speaker-label overrides
                    sess_speakers = session.get("speakers") or {}
                    display = (sess_speakers.get(sp)
                               or sess_speakers.get(os.path.basename(sp))
                               or self._pq_format_speaker(sp))
                    label_text = "{}:\n".format(display)
                    # Insert with both the visual style tag AND a
                    # per-speaker tag carrying the raw key so the
                    # rename handler knows which speaker was clicked.
                    raw_tag = "speaker_raw:{}".format(sp)
                    if raw_tag not in tx.tag_names():
                        tx.tag_configure(raw_tag)   # invisible marker tag
                    tx.insert("end", label_text,
                              ("speaker_lbl", raw_tag))

                # Build continuous text for this speaker's run
                cont_buf  = []
                cont_idx  = []   # (start_in_cont, end_in_cont, word)
                cpos      = 0
                first     = True
                for w in gwords:
                    wt = w.get("word") or ""
                    if not wt.strip():
                        continue
                    if first:
                        wt = wt.lstrip()
                        first = False
                    cont_idx.append((cpos, cpos + len(wt), w))
                    cont_buf.append(wt)
                    cpos += len(wt)
                continuous = "".join(cont_buf)
                if not continuous:
                    continue

                # Syntactic paragraph breaks within this speaker's run
                break_positions = self._pq_detect_paragraph_breaks(continuous)
                parts = []
                last = 0
                for bp in break_positions:
                    parts.append(continuous[last:bp])
                    parts.append("\n\n")
                    last = bp
                parts.append(continuous[last:])
                run_text = "".join(parts)

                # Position in the Text widget where this run starts
                run_start = tx.index("end-1c")
                tx.insert("end", run_text, "body")

                # Record word_index char positions in widget coords.
                # `run_start` is "<line>.<col>" — convert each cont
                # offset into a widget index relative to that.  Use Tk
                # math: run_start + Nc.
                sorted_bps = sorted(break_positions)
                # Convert run_start to absolute char count from "1.0"
                run_start_abs = _tx_count_chars(tx, "1.0", run_start)

                # ── Literal "line.column" index mapping ──────────────
                # This loop used to tag each word with
                #     "1.0 + {}c".format(abs_s)
                # Tk resolves a "+Nc" index by WALKING FORWARD N chars
                # from the base, so cost is O(N) per call and O(N^2)
                # across the render.  Benchmarked on this box: 5.1 s at
                # 20k words, 15.7 s at 34k — every 1.2 s throughout a
                # streaming transcription.  That is the single biggest
                # source of the "sluggish / re-loading graphics" feel.
                #
                # Emitting a literal "L.C" index instead costs Tk a
                # direct B-tree seek: same 20k words drops to ~87 ms
                # (~58x), and batching the spans into one tag_add call
                # takes it to ~30 ms (~170x).
                #
                # NB batching ALONE does not help — the cost is index
                # resolution, not Tcl round-trips (measured: 5430 ms
                # batched vs 5018 ms looped).  The literal index is the
                # part that matters; batching is a bonus on top.
                _rl, _, _rc = run_start.partition(".")
                _run_line, _run_col = int(_rl), int(_rc)
                # Newline offsets within run_text, for offset -> (line, col).
                _nl, _p = [], run_text.find("\n")
                while _p != -1:
                    _nl.append(_p)
                    _p = run_text.find("\n", _p + 1)

                def _local_index(off, _nl=_nl,
                                 _run_line=_run_line, _run_col=_run_col):
                    """Char offset within run_text -> literal Tk "L.C" index."""
                    k = bisect.bisect_left(_nl, off)   # newlines strictly before off
                    if k == 0:
                        return "{}.{}".format(_run_line, _run_col + off)
                    return "{}.{}".format(_run_line + k, off - _nl[k - 1] - 1)

                _tiny_spans, _user_spans = [], []
                for s, e, w in cont_idx:
                    # Add 2 chars for each break before this word in run.
                    # sorted_bps is sorted and cont_idx ascends by s, so
                    # the old full rescan per word (O(words x breaks),
                    # 294 ms at 20k words) is a bisect.
                    n_before = bisect.bisect_right(sorted_bps, s)
                    shift = n_before * 2
                    abs_s = run_start_abs + s + shift
                    abs_e = run_start_abs + e + shift
                    self._pq_word_index.append((abs_s, abs_e, w))
                    # Apply source-based styling.  Tiny-draft words
                    # render faded ONLY while a non-tiny refinement
                    # is actively running — otherwise tiny words are
                    # the user's final choice and should render
                    # solid like any other source.  User edits get
                    # a thin underline (tracked-changes marker) so
                    # the user always knows what they touched.
                    src = w.get("_src")
                    if _fade_tiny and src == "tiny":
                        _tiny_spans.append(_local_index(s + shift))
                        _tiny_spans.append(_local_index(e + shift))
                    elif src == "user":
                        _user_spans.append(_local_index(s + shift))
                        _user_spans.append(_local_index(e + shift))
                # tag_add takes any number of index PAIRS — one call each.
                if _tiny_spans:
                    tx.tag_add("body_tiny", *_tiny_spans)
                if _user_spans:
                    tx.tag_add("body_user", *_user_spans)

            # Reset undo history — programmatic render is not a user
            # edit, so Ctrl+Z shouldn't revert to "before render".
            try:
                tx.edit_reset()
            except tk.TclError:
                pass
        # Invalidate the derived playback caches (see _pq_playback_caches).
        # Bumped rather than rebuilt because most renders are never
        # followed by playback, and building them costs a full widget
        # read + sort.
        self._pq_index_seq = getattr(self, "_pq_index_seq", 0) + 1
        self._pq_update_status_lbl(session)

    def _pq_playback_caches(self):
        """Return (time_index, line_starts) for the playback highlight,
        rebuilding them only when the transcript has been re-rendered.

        time_index: (word_start_s, word_end_s, abs_s, abs_e) sorted by
            word_start_s.  _pq_word_index itself is ordered by CHARACTER
            offset, and when speaker labels are present the words are
            grouped per speaker — so character order is not time order
            and cannot be bisected on time directly.

        line_starts: absolute char offset of the first character of each
            line, so an absolute offset can be turned into a literal
            "line.column" Tk index.  The tick used to address the
            playing word as "1.0 + {}c", which Tk resolves by walking
            forward from the buffer origin — three such walks per tick
            at 16 ticks/second, each walking ~200k characters on a long
            transcript.

        Both are derived once per render, and only if playback actually
        starts.
        """
        seq = getattr(self, "_pq_index_seq", 0)
        if getattr(self, "_pq_cache_seq", None) == seq:
            return self._pq_time_index, self._pq_line_starts

        word_idx = getattr(self, "_pq_word_index", []) or []
        ti = []
        for s_abs, e_abs, w in word_idx:
            try:
                ws = float(w.get("start", 0.0))
                we = float(w.get("end", ws))
            except Exception:
                continue
            ti.append((ws, we, s_abs, e_abs))
        ti.sort(key=lambda it: it[0])

        ls = [0]
        tx = getattr(self, "_pq_tx_text", None)
        if tx is not None:
            try:
                _txt = tx.get("1.0", "end-1c")
                _p = _txt.find("\n")
                while _p != -1:
                    ls.append(_p + 1)
                    _p = _txt.find("\n", _p + 1)
            except tk.TclError:
                pass

        self._pq_time_index  = ti
        self._pq_line_starts = ls
        self._pq_cache_seq   = seq
        return ti, ls

    def _pq_abs_index(self, n, line_starts):
        """Absolute char offset -> literal Tk "line.column" index."""
        i = bisect.bisect_right(line_starts, n) - 1
        if i < 0:
            i = 0
        return "{}.{}".format(i + 1, n - line_starts[i])

    # ── Selection actions ─────────────────────────────────────────────────

    def _pq_apply_mode(self):
        """Vestigial entry point kept so existing callers don't error.
        Edit/Play modes were collapsed into perpetual read-only with a
        right-click context menu — the only remaining responsibility is
        making sure the notes panel is rendered after view setup."""
        tx = getattr(self, "_pq_tx_text", None)
        if tx is not None:
            try:
                tx.config(cursor="xterm")
            except tk.TclError:
                pass
        try:
            self._pq_render_notes_panel()
        except Exception:
            pass

    def _pq_edit_selection(self):
        """Open a dialog letting the user rewrite the words under the
        current selection.  On save, replaces those word entries in
        ``session["transcript"]`` with new entries whose timestamps are
        distributed proportionally across the original time range, then
        re-renders the transcript and persists the session."""
        tx      = getattr(self, "_pq_tx_text", None)
        session = getattr(self, "_pq_current_session", None)
        if tx is None or session is None:
            return

        word_idx = getattr(self, "_pq_word_index", []) or []
        if not word_idx:
            return

        try:
            sel_first = tx.index("sel.first")
            sel_last  = tx.index("sel.last")
        except tk.TclError:
            return

        c0 = _tx_count_chars(tx, "1.0", sel_first)
        c1 = _tx_count_chars(tx, "1.0", sel_last)

        picked = [w for (s, e, w) in word_idx if e > c0 and s < c1]
        if not picked:
            return

        # Locate the picked words in session["transcript"] by identity
        # — they're the same dict objects the renderer captured.  Find
        # the contiguous slice [span_start, span_end) covering them so
        # we can splice in the replacement entries while preserving
        # any non-word entries (breaks, etc.) before/after.
        transcript = session.get("transcript") or []
        picked_ids = {id(w) for w in picked}
        span_start = None
        span_end   = None
        for i, w in enumerate(transcript):
            if id(w) in picked_ids:
                if span_start is None:
                    span_start = i
                span_end = i + 1
        if span_start is None:
            return

        orig_words = transcript[span_start:span_end]
        real_orig  = [w for w in orig_words
                      if (w.get("word") or "").strip() and not w.get("break")]
        if not real_orig:
            return

        range_start = float(real_orig[0].get("start", 0))
        range_end   = float(real_orig[-1].get("end", range_start))
        if range_end <= range_start:
            range_end = range_start + max(0.5, len(real_orig) * 0.3)

        # Most-common speaker among picked words wins (in mixed-track
        # selections this beats arbitrarily picking the first one).
        from collections import Counter
        sp_counter = Counter(w.get("speaker") for w in real_orig
                             if w.get("speaker"))
        common_speaker = (sp_counter.most_common(1)[0][0]
                          if sp_counter else None)

        orig_text = "".join(w.get("word") or "" for w in real_orig).strip()

        win = tk.Toplevel(self)
        win.title("Edit Selection")
        win.configure(bg=BG)
        win.transient(self); win.grab_set()
        win.minsize(560, 320)

        # Bottom buttons first so they survive minimum-height dialogs.
        nav = tk.Frame(win, bg=BG)
        nav.pack(side="bottom", fill="x", padx=20, pady=(8, 14))

        tk.Label(win, text="Edit Transcript Text", font=FH,
                 bg=BG, fg=TEXT, padx=20, pady=14).pack(anchor="w")
        tk.Label(win, text="Time range: {} – {}".format(
                    secs_tc(range_start).split(".")[0],
                    secs_tc(range_end).split(".")[0]),
                 font=FL, bg=BG, fg=SUB, padx=20).pack(anchor="w")
        tk.Label(win,
                 text=("Edited words inherit the original time span; "
                       "timestamps are distributed proportionally.  "
                       "Enter to save · Esc to cancel."),
                 font=FB, bg=BG, fg=SUB, padx=20,
                 wraplength=520, justify="left"
                 ).pack(anchor="w", pady=(0, 8))

        ent = tk.Text(win, font=FB, bg=SURF2, fg=TEXT,
                       insertbackground=TEXT, relief="flat",
                       bd=8, height=3, wrap="word", undo=True)
        ent.pack(fill="both", expand=True, padx=20, pady=(8, 0))
        ent.insert("1.0", orig_text)
        ent.tag_add("sel", "1.0", "end-1c")
        ent.focus_set()

        def _save(_e=None):
            new_text = ent.get("1.0", "end-1c").strip()
            toks = new_text.split() if new_text else []

            # Whisper convention: every word includes any natural
            # leading whitespace (e.g. " Hello,").  Preserve the first
            # ORIGINAL word's leading-space behaviour for the first new
            # word so the new text doesn't fuse to the preceding word.
            # E.g., replacing " world" with "earth" must give " earth"
            # not "earth" — otherwise "Hello world" becomes "Helloearth".
            first_orig_word = real_orig[0].get("word", "") if real_orig else ""
            first_starts_with_space = (
                len(first_orig_word) > 0
                and first_orig_word[0] in (" ", "\t"))

            # Unique id for this edit operation.  Every new entry from
            # this Save shares the same _edit_id, so "Restore Whisper
            # original" can locate the entire group from any one of
            # them.  Time-ns hex is unique within the session and
            # keeps the value short.
            edit_id = "e{:x}".format(time.time_ns())

            # Snapshot the original Whisper entries so a later restore
            # can splice them back exactly as Whisper produced them.
            # dict() to defensively decouple from later mutations of
            # the original entries (e.g. a refine pass updating
            # timestamps in place).
            replaces_blob = [dict(w) for w in orig_words]

            # Empty input (no tokens) deletes the selected words.
            # Useful for cleaning up cases like "ok ay" -> "okay" where
            # the user wants to remove a stray token, or "the the dog"
            # -> "the dog" where Whisper duplicated a word.
            new_entries = []
            if toks:
                n     = len(toks)
                total = max(0.05, range_end - range_start)
                per   = total / n
                for i, tok in enumerate(toks):
                    if i == 0:
                        wt = (" " if first_starts_with_space else "") + tok
                    else:
                        wt = " " + tok
                    entry = {
                        "word":  wt,
                        "start": range_start + i * per,
                        "end":   range_start + (i + 1) * per,
                        # Mark these words as user-authored so a later
                        # progressive-refinement pass preserves them
                        # instead of overwriting with Whisper output.
                        "_src":      "user",
                        "_edit_id":  edit_id,
                        "_replaces": replaces_blob,
                    }
                    if common_speaker:
                        entry["speaker"] = common_speaker
                    new_entries.append(entry)
            else:
                # Deletion: leave a single user-tagged placeholder so
                # the merge layer knows this range is intentionally
                # empty and shouldn't be refilled by a later refine
                # pass.  Otherwise the configured-model output would
                # come in and re-insert the words Jordan just deleted.
                #
                # Implementation: zero-width "user" marker with the
                # original time range.  Renders as nothing visible
                # but takes part in _pq_user_edited_spans so the
                # merge respects the deletion.
                new_entries.append({
                    "word":      "",
                    "start":     range_start,
                    "end":       range_end,
                    "_src":      "user",
                    "_deleted":  True,
                    "_edit_id":  edit_id,
                    "_replaces": replaces_blob,
                })
                if common_speaker:
                    new_entries[0]["speaker"] = common_speaker

            # Re-resolve the splice against the LIVE session transcript.
            # While the dialog was open, streaming ticks may have
            # appended more words OR replaced some via merge — using
            # the captured `transcript` snapshot would silently delete
            # any words added in that window.  Try id() lookup first
            # (the picked dicts are still in transcript when no merge
            # has run); fall back to time-range lookup if a refine
            # pass replaced them with fresh dicts at the same span.
            live = session.get("transcript")
            if live is not None and live is not transcript:
                live_start = None
                live_end   = None
                for i, w in enumerate(live):
                    if id(w) in picked_ids:
                        if live_start is None:
                            live_start = i
                        live_end = i + 1
                if live_start is None:
                    # Refine pass swept the picked dicts.  Splice by
                    # time-range overlap of real (non-user, non-break)
                    # words instead.
                    for i, w in enumerate(live):
                        if w.get("break") or w.get("_src") == "user":
                            continue
                        ws = float(w.get("start", 0.0))
                        we = float(w.get("end",   ws))
                        mid = (ws + we) / 2.0
                        if range_start <= mid <= range_end:
                            if live_start is None:
                                live_start = i
                            live_end = i + 1
                if live_start is None:
                    # Nothing to replace — just insert in time order.
                    live.extend(new_entries)
                    live.sort(
                        key=lambda w: float(w.get("start", 0.0)))
                else:
                    live[live_start:live_end] = new_entries
                session["transcript"] = live
            else:
                transcript[span_start:span_end] = new_entries
                session["transcript"] = transcript
            try:
                self._pq_save_session_file(session)
            except Exception:
                pass
            win.destroy()
            # Snapshot full interactive state (word-anchored scroll)
            # before the re-render, then put the cursor at the END
            # of the new entries (where the user would naturally
            # continue) and replay the scroll anchor.
            tx_after = getattr(self, "_pq_tx_text", None)
            pre_state = self._pq_capture_text_state()
            try:
                self._pq_render_transcript_text(session)
            except Exception:
                pass
            self._pq_restore_text_state(pre_state)
            if tx_after is not None and new_entries:
                last_id = id(new_entries[-1])
                word_idx = getattr(self, "_pq_word_index", []) or []
                for s, e, w in word_idx:
                    if id(w) == last_id:
                        try:
                            tx_after.tag_remove("sel", "1.0", "end")
                            tx_after.mark_set(
                                "insert", "1.0+{}c".format(e))
                        except tk.TclError:
                            pass
                        break
            return "break"

        _W = 8
        self._btn(nav, "SAVE", _save,
                  color=ACCENT, width=_W).pack(side="right")
        self._btn(nav, "CANCEL", win.destroy, width=_W
                  ).pack(side="right", padx=(0, 8))
        # Enter saves.  Multi-line edits aren't useful here (one
        # transcript region is replaced as a flat span of tokens),
        # so we block the newline insertion entirely by returning
        # "break" from the bindings.  Ctrl+Enter still works for
        # muscle-memory compatibility.
        def _save_and_break(_e=None):
            _save()
            return "break"
        for seq in ("<Return>", "<KP_Enter>",
                    "<Control-Return>", "<Control-KP_Enter>"):
            ent.bind(seq, _save_and_break)
            win.bind(seq, _save_and_break)
        win.bind("<Escape>", lambda e: win.destroy())

        self._center_dialog(win, 560, 320)
        self.wait_window(win)

    def _pq_selection_has_user_edits(self):
        """True when the current selection overlaps any user-edited
        entries that carry restore data (_edit_id + _replaces).  Used
        to gate the 'Restore Whisper original' menu item — no point
        showing it when there's nothing to restore."""
        tx      = getattr(self, "_pq_tx_text", None)
        session = getattr(self, "_pq_current_session", None)
        if tx is None or session is None:
            return False
        word_idx = getattr(self, "_pq_word_index", []) or []
        if not word_idx:
            return False
        try:
            sel_first = tx.index("sel.first")
            sel_last  = tx.index("sel.last")
        except tk.TclError:
            return False
        c0 = _tx_count_chars(tx, "1.0", sel_first)
        c1 = _tx_count_chars(tx, "1.0", sel_last)
        for (s, e, w) in word_idx:
            if e <= c0 or s >= c1:
                continue
            if w.get("_src") == "user" and w.get("_edit_id"):
                return True
        return False

    def _pq_restore_originals(self):
        """Right-click action: replace every user-edited word in the
        current selection with the original Whisper entries captured
        at edit time.  Each user entry carries _edit_id + _replaces;
        restoration is grouped by _edit_id so a multi-word edit is
        restored as one atomic block (not piecemeal)."""
        tx      = getattr(self, "_pq_tx_text", None)
        session = getattr(self, "_pq_current_session", None)
        if tx is None or session is None:
            return
        word_idx = getattr(self, "_pq_word_index", []) or []
        if not word_idx:
            return
        try:
            sel_first = tx.index("sel.first")
            sel_last  = tx.index("sel.last")
        except tk.TclError:
            return

        c0 = _tx_count_chars(tx, "1.0", sel_first)
        c1 = _tx_count_chars(tx, "1.0", sel_last)

        # Collect unique _edit_ids touched by the selection that
        # actually have restore data attached.
        touched_ids = []
        seen = set()
        for (s, e, w) in word_idx:
            if e <= c0 or s >= c1:
                continue
            if w.get("_src") != "user":
                continue
            eid = w.get("_edit_id")
            if not eid or not w.get("_replaces"):
                continue
            if eid in seen:
                continue
            seen.add(eid)
            touched_ids.append(eid)

        if not touched_ids:
            return

        transcript = session.get("transcript") or []
        if not transcript:
            return

        # Snapshot the full interactive state before mutating the
        # transcript so the cursor / selection / scroll all survive
        # the re-render that follows.  Using the shared capture
        # helper means the scroll is anchored to the top-visible
        # WORD rather than a yview fraction — robust against the
        # transcript's char count shifting during streaming.
        tx = getattr(self, "_pq_tx_text", None)
        pre_state = self._pq_capture_text_state()

        # For each touched edit_id, find the contiguous slice of
        # entries sharing it and splice in the originals.  An
        # edit_id should always be contiguous in the transcript
        # (the edit dialog inserts new entries as one block) but
        # we don't rely on that — we locate all matching indices
        # and use [min, max+1] as the splice range.  Track the
        # ids of the freshly-spliced dicts so the cursor lands on
        # the first restored word after the re-render.
        first_restored_id = None
        for eid in touched_ids:
            indices = [i for i, w in enumerate(transcript)
                       if w.get("_edit_id") == eid]
            if not indices:
                continue
            lo = min(indices)
            hi = max(indices) + 1
            originals = transcript[lo].get("_replaces") or []
            # Defensive copy so future restores don't share dict
            # identity with what's now back in the transcript.
            restored = [dict(o) for o in originals]
            if restored and first_restored_id is None:
                first_restored_id = id(restored[0])
            transcript[lo:hi] = restored

        session["transcript"] = transcript
        try:
            self._pq_save_session_file(session)
        except Exception:
            pass
        try:
            self._pq_render_transcript_text(session)
        except Exception:
            pass

        # Restore scroll via the shared helper (word-anchored), then
        # place cursor at the first restored word and clear the
        # stale selection (the words the user had selected just
        # ceased to exist as edits).
        self._pq_restore_text_state(pre_state)
        if tx is not None:
            try:
                tx.tag_remove("sel", "1.0", "end")
            except tk.TclError:
                pass
            if first_restored_id is not None:
                word_idx = getattr(self, "_pq_word_index", []) or []
                for s, e, w in word_idx:
                    if id(w) == first_restored_id:
                        try:
                            tx.mark_set("insert", "1.0+{}c".format(s))
                        except tk.TclError:
                            pass
                        break

    def _pq_add_margin_note(self):
        """Margin-mode action: prompt for a note tied to the current
        selection (or to the cursor word if no selection).  Notes are
        persisted on session["notes"] and rendered as small inline
        markers — they never appear in @PULL clipboard output."""
        tx      = getattr(self, "_pq_tx_text", None)
        session = getattr(self, "_pq_current_session", None)
        if tx is None or session is None:
            return

        word_idx = getattr(self, "_pq_word_index", []) or []
        if not word_idx:
            return

        try:
            sel_first = tx.index("sel.first")
            sel_last  = tx.index("sel.last")
        except tk.TclError:
            sel_first = sel_last = None

        if sel_first and sel_last:
            c0 = _tx_count_chars(tx, "1.0", sel_first)
            c1 = _tx_count_chars(tx, "1.0", sel_last)
            picked = [w for (s, e, w) in word_idx if e > c0 and s < c1]
        else:
            c0 = _tx_count_chars(tx, "1.0", tx.index("insert"))
            picked = [w for (s, e, w) in word_idx if s <= c0 < e]

        if not picked:
            return

        # Use the start time of the first selected word as the anchor —
        # notes survive transcript re-renders that way.
        anchor_start = float(picked[0].get("start", 0))
        anchor_end   = float(picked[-1].get("end", anchor_start))

        # Preview shows a snippet of the anchored quote
        snippet = "".join(w.get("word") or "" for w in picked).strip()[:80]

        win = tk.Toplevel(self)
        win.title("Margin Note")
        win.configure(bg=BG)
        win.transient(self); win.grab_set()
        win.minsize(560, 380)

        # Pack the bottom button row FIRST (side="bottom") so it's
        # guaranteed to be visible even when the dialog is the
        # minimum height — otherwise the editor pushes it off-screen.
        nav = tk.Frame(win, bg=BG)
        nav.pack(side="bottom", fill="x", padx=20, pady=(8, 14))

        tk.Label(win, text="Add Margin Note", font=FH,
                 bg=BG, fg=TEXT, padx=20, pady=14).pack(anchor="w")
        tk.Label(win, text="Anchored to:", font=FL,
                 bg=BG, fg=SUB, padx=20).pack(anchor="w")
        tk.Label(win, text='"{}"'.format(snippet),
                 font=FB, bg=BG, fg=ACCENT, padx=20,
                 wraplength=520, justify="left"
                 ).pack(anchor="w", pady=(0, 8))
        tk.Label(win,
                 text="Ctrl+Enter to save · Esc to cancel",
                 font=FB, bg=BG, fg=SUB, padx=20).pack(anchor="w")

        ent = tk.Text(win, font=FB, bg=SURF2, fg=TEXT,
                       insertbackground=TEXT, relief="flat",
                       bd=8, height=6, wrap="word")
        ent.pack(fill="both", expand=True, padx=20, pady=(8, 0))
        ent.focus_set()

        def _save(_e=None):
            text = ent.get("1.0", "end-1c").strip()
            if not text:
                win.destroy(); return "break"
            notes = list(session.get("notes") or [])
            notes.append({
                "anchor_start": anchor_start,
                "anchor_end":   anchor_end,
                "snippet":      snippet,
                "text":         text,
                "ts":           __import__("datetime").datetime.now()
                                  .isoformat(timespec="seconds"),
            })
            session["notes"] = notes
            self._pq_save_session_file(session)
            win.destroy()
            self._pq_render_notes_panel()
            return "break"

        _W = 10
        self._btn(nav, "SAVE NOTE", _save,
                  color=ACCENT, width=_W).pack(side="right")
        self._btn(nav, "CANCEL", win.destroy, width=_W
                  ).pack(side="right", padx=(0, 8))
        # Ctrl+Enter from anywhere in the dialog (incl. inside the Text
        # widget where Tk's class binding for <Return> would normally
        # eat the event).  Bind on both the window and the editor.
        win.bind("<Control-Return>",       _save)
        win.bind("<Control-KP_Enter>",     _save)
        ent.bind("<Control-Return>",       _save)
        ent.bind("<Control-KP_Enter>",     _save)
        win.bind("<Escape>", lambda e: win.destroy())

        self._center_dialog(win, 560, 380)
        self.wait_window(win)

    def _pq_update_notes_wraplength(self, canvas_width):
        """Set wraplength on every note row's body / snippet label so
        the text reflows when the user resizes the notes pane."""
        # Subtract margins: row padding (12) + delete button (~24) +
        # bullet (~20) + safety (~8) ≈ 64 px reserved.
        new_w = max(180, int(canvas_width) - 64)
        for lbl in (getattr(self, "_pq_notes_body_lbls", []) or []):
            try:
                if lbl.winfo_exists():
                    lbl.config(wraplength=new_w)
            except tk.TclError:
                pass

    def _pq_toggle_notes_pane(self, force=None):
        """Show/hide the margin-notes pane.

        force=True  → show.  force=False → hide.  force=None → toggle.
        Tracked via self._pq_notes_user_collapsed so a redraw of the
        notes panel honours the user's preference.
        """
        if force is None:
            cur = bool(getattr(self, "_pq_notes_user_collapsed", False))
            self._pq_notes_user_collapsed = not cur
        else:
            self._pq_notes_user_collapsed = not bool(force)
        self._pq_render_notes_panel()
        self._pq_refresh_notes_toggle_btn()

    def _pq_refresh_notes_toggle_btn(self):
        """Update the 📝 button label + colour based on current state."""
        btn = getattr(self, "_pq_notes_toggle_btn", None)
        session = getattr(self, "_pq_current_session", None)
        if btn is None or session is None:
            return
        try:
            notes = session.get("notes") or []
            collapsed = bool(getattr(self, "_pq_notes_user_collapsed", False))
            if not notes:
                btn.config(text="📝 0 notes", fg=SUB, bg=SURF3)
            elif collapsed:
                btn.config(
                    text="📝 {} note{} ▸".format(
                        len(notes), "s" if len(notes) != 1 else ""),
                    fg=TEXT, bg=SURF3)
            else:
                btn.config(
                    text="📝 {} note{}".format(
                        len(notes), "s" if len(notes) != 1 else ""),
                    fg=ACCENT, bg=SURF3)
        except tk.TclError:
            pass

    def _pq_on_layout_resize(self, event=None):
        """Debounced window-resize callback.  When the window crosses
        the layout threshold, switch PanedWindow orientation by
        rebuilding the session view (cheap; only fires on threshold
        crossings, not on every resize tick)."""
        if getattr(self, "_pq_current_session", None) is None:
            return
        # Debounce: cancel any pending check, schedule fresh one.
        prev = getattr(self, "_pq_layout_resize_after", None)
        if prev is not None:
            try: self.after_cancel(prev)
            except Exception: pass
        self._pq_layout_resize_after = self.after(
            220, self._pq_check_layout_orient)

    def _pq_check_layout_orient(self):
        """If the current window width crosses the threshold for the
        opposite orientation, rebuild the session view with that
        orient."""
        sess = getattr(self, "_pq_current_session", None)
        if sess is None:
            return
        cur_w = self.winfo_width()
        desired = "horizontal" if cur_w >= 1100 else "vertical"
        cur_orient = getattr(self, "_pq_layout_orient", desired)
        if desired == cur_orient:
            return
        # Stash transient state we want to preserve across rebuild
        try:
            stash = {
                "search": (self._pq_search_var.get()
                           if hasattr(self, "_pq_search_var") else ""),
                "yview":  (self._pq_tx_text.yview()
                           if hasattr(self, "_pq_tx_text") else None),
                "collapsed": bool(getattr(
                    self, "_pq_notes_user_collapsed", False)),
            }
        except Exception:
            stash = {}
        self._pq_layout_resize_stash = stash
        self._pq_open_session_view(sess)

    def _pq_wheel_notes(self, event):
        """MouseWheel handler that scrolls the margin-notes canvas.
        Returns "break" so the global bind_all wheel handler from
        _scroll_frame doesn't also fire."""
        canvas = getattr(self, "_pq_notes_canvas", None)
        if canvas is None:
            return
        try:
            if not canvas.winfo_exists():
                return
        except tk.TclError:
            return
        # macOS reports raw notch counts (1/-1); X11 uses Button-4/5;
        # Windows uses delta multiples of 120.
        if sys.platform == "darwin":
            units = int(-1 * event.delta)
        else:
            units = int(-1 * (event.delta / 120)) if event.delta else 0
        if units:
            canvas.yview_scroll(units, "units")
        return "break"

    def _pq_bind_wheel_to(self, widget):
        """Recursively bind MouseWheel events on widget + every
        descendant to the notes-canvas scroller.  Idempotent: re-binding
        the same widget just replaces the handler."""
        if widget is None:
            return
        try:
            widget.bind("<MouseWheel>", self._pq_wheel_notes)
            widget.bind("<Button-4>",
                lambda e: (self._pq_notes_canvas.yview_scroll(-3, "units"),
                           "break")[1])
            widget.bind("<Button-5>",
                lambda e: (self._pq_notes_canvas.yview_scroll( 3, "units"),
                           "break")[1])
        except tk.TclError:
            return
        for c in widget.winfo_children():
            self._pq_bind_wheel_to(c)

    def _pq_render_notes_panel(self):
        """Refresh the MARGIN NOTES side panel.  Shows one row per note
        with snippet (clickable to jump to anchor) + body + delete.
        Hidden when there are no notes AND we're not in MARGIN mode."""
        outer = getattr(self, "_pq_notes_outer", None)
        inner = getattr(self, "_pq_notes_inner", None)
        cnt   = getattr(self, "_pq_notes_count_lbl", None)
        session = getattr(self, "_pq_current_session", None)
        if outer is None or inner is None or session is None:
            return

        notes = session.get("notes") or []

        # Visibility: notes_outer is a pane in the layout PanedWindow.
        # Add it when notes exist + not user-collapsed; remove when no
        # notes or user collapsed it.
        pw = getattr(self, "_pq_layout_pw", None)
        try:
            if pw is not None and pw.winfo_exists():
                in_panes = str(outer) in [str(p) for p in pw.panes()]
                user_collapsed = bool(getattr(
                    self, "_pq_notes_user_collapsed", False))
                want_visible = bool(notes) and not user_collapsed
                if want_visible and not in_panes:
                    # Add as second pane with a sane minimum.  Stretch
                    # "never" so it stays at its preferred width unless
                    # the user explicitly drags the sash.
                    pw.add(outer, minsize=240, stretch="never",
                            sticky="nsew")
                elif not want_visible and in_panes:
                    pw.forget(outer)
        except tk.TclError:
            pass

        # Repopulate — also clear out the body-label registry so we
        # can rebuild it as new note rows are created.
        for w in list(inner.winfo_children()):
            w.destroy()
        self._pq_notes_body_lbls = []

        if cnt is not None:
            try:
                cnt.config(text="{} note{}".format(
                    len(notes), "s" if len(notes) != 1 else ""))
            except tk.TclError:
                pass
        # Keep the action-row toggle button in sync as notes are
        # added/deleted.
        try:
            self._pq_refresh_notes_toggle_btn()
        except Exception:
            pass

        if not notes:
            tk.Label(inner,
                     text=("  No notes yet.  In MARGIN mode (Ctrl+M), "
                           "select text in the transcript and press Enter "
                           "to add one."),
                     font=FB, bg=SURF, fg=SUB, padx=8, pady=12,
                     wraplength=900, justify="left").pack(anchor="w")
            return

        # Sort by anchor_start so notes appear in transcript order
        for note_idx, n in enumerate(sorted(
                notes, key=lambda x: float(x.get("anchor_start", 0)))):
            row = tk.Frame(inner, bg=SURF,
                            highlightbackground=BORDER, highlightthickness=1)
            row.pack(fill="x", padx=6, pady=4)
            inner_row = tk.Frame(row, bg=SURF)
            inner_row.pack(fill="x", padx=8, pady=6)

            ts_s = float(n.get("anchor_start", 0))
            tc_str = secs_tc(ts_s).split(".")[0]

            # Header: timecode + snippet (clickable) + delete
            hdr = tk.Frame(inner_row, bg=SURF)
            hdr.pack(fill="x")
            tc_lbl = tk.Label(hdr, text="▸ " + tc_str, font=FB,
                               bg=SURF, fg=ACCENT, cursor="hand2")
            tc_lbl.pack(side="left")
            snippet = (n.get("snippet") or "").strip()
            if snippet:
                # Snippet (quoted anchor text) stays single-line and
                # never wraps — only the note BODY reflows with panel
                # width.  Truncate to 60 chars; the full anchor lives
                # in the underlying word entries anyway.
                snip_text = snippet[:60] + ("…" if len(snippet) > 60 else "")
                snip_lbl = tk.Label(hdr,
                                     text='  "{}"'.format(snip_text),
                                     font=FB, bg=SURF, fg=SUB,
                                     cursor="hand2", anchor="w")
                snip_lbl.pack(side="left", fill="x", expand=True)
            else:
                snip_lbl = None

            del_lbl = tk.Label(hdr, text="✕", font=FB,
                                bg=SURF, fg=SUB, cursor="hand2",
                                padx=4)
            del_lbl.pack(side="right")
            del_lbl.bind("<Enter>", lambda e, w=del_lbl: w.config(fg=ERR))
            del_lbl.bind("<Leave>", lambda e, w=del_lbl: w.config(fg=SUB))
            def _delete(_e=None, target=n):
                cur = list(session.get("notes") or [])
                # Remove by identity-equivalence on a tuple key
                key = (target.get("anchor_start"),
                       target.get("text"),
                       target.get("ts"))
                session["notes"] = [
                    nn for nn in cur
                    if (nn.get("anchor_start"),
                        nn.get("text"),
                        nn.get("ts")) != key
                ]
                self._pq_save_session_file(session)
                self._pq_render_notes_panel()
            del_lbl.bind("<Button-1>", _delete)

            # Body: the note text.  wraplength is updated dynamically
            # via _pq_update_notes_wraplength based on the current
            # canvas width so the text reflows when the user resizes
            # the panel.
            body_lbl = tk.Label(inner_row, text=(n.get("text") or "").strip(),
                                 font=FB, bg=SURF, fg=TEXT,
                                 anchor="w", justify="left",
                                 wraplength=400)
            body_lbl.pack(anchor="w", fill="x", pady=(4, 0))
            # Only the BODY label wraps with the panel.  The snippet
            # stays single-line (truncated above) — it's a quote
            # reference, not the user's content.
            self._pq_notes_body_lbls = getattr(
                self, "_pq_notes_body_lbls", [])
            self._pq_notes_body_lbls.append(body_lbl)

            # Click-to-jump: scroll the transcript pane to this note's anchor
            def _jump(_e=None, t_s=ts_s):
                self._pq_jump_to_time(t_s)
            tc_lbl.bind("<Button-1>", _jump)
            if snip_lbl is not None:
                snip_lbl.bind("<Button-1>", _jump)

        # After the rows are built, push the current canvas width into
        # each body label so the new rows wrap correctly without
        # waiting for the next <Configure> event.
        try:
            cw = self._pq_notes_canvas.winfo_width()
            if cw > 1:
                self._pq_update_notes_wraplength(cw)
            else:
                # First render before the canvas has a width — defer.
                self.after(50,
                           lambda: self._pq_update_notes_wraplength(
                               self._pq_notes_canvas.winfo_width()))
        except tk.TclError:
            pass

        # Re-bind wheel events on every freshly-created row widget so
        # scrolling works regardless of which child the cursor hovers.
        try:
            self._pq_bind_wheel_to(self._pq_notes_outer)
        except Exception:
            pass

    def _pq_jump_to_time(self, time_s):
        """Scroll the transcript Text widget so the word nearest
        `time_s` is visible.  Used by the notes panel click-to-jump."""
        tx = getattr(self, "_pq_tx_text", None)
        word_idx = getattr(self, "_pq_word_index", []) or []
        if tx is None or not word_idx:
            return
        # Find nearest word by start time
        best = None
        best_dist = float("inf")
        for s, e, w in word_idx:
            ws = float(w.get("start", 0))
            d = abs(ws - time_s)
            if d < best_dist:
                best_dist = d
                best = s
        if best is None:
            return
        try:
            idx = "1.0+{}c".format(best)
            tx.see(idx)
            tx.mark_set("insert", idx)
            tx.focus_set()
            # Brief flash highlight
            end_idx = "1.0+{}c".format(best + 30)
            tx.tag_configure("pq_jump_flash",
                              background="#5a3a10")
            tx.tag_add("pq_jump_flash", idx, end_idx)
            self.after(1200,
                       lambda: tx.tag_remove("pq_jump_flash", "1.0", "end"))
        except tk.TclError:
            pass

    def _pq_manage_speakers_dialog(self):
        """List every speaker raw-tag found across the project's sessions
        and let the user rename any of them.  Renames apply to all
        sessions and persist to disk."""
        project = getattr(self, "_pq_project", None)
        if project is None or not project.get("sessions"):
            messagebox.showinfo("Manage Speakers",
                "No sessions in this project yet.")
            return

        # Gather unique raw speaker tags + their current display labels
        # across every session.  raw → set of sessions that reference it.
        raw_to_sessions = {}
        raw_to_label    = {}
        for sess in project["sessions"]:
            speakers_map = sess.get("speakers") or {}
            for w in (sess.get("transcript") or []):
                raw = w.get("speaker")
                if not raw:
                    continue
                raw_to_sessions.setdefault(raw, []).append(sess)
                # Resolve current label: per-session override OR
                # filename-derived default.
                label = (speakers_map.get(raw)
                         or speakers_map.get(os.path.basename(raw))
                         or self._pq_format_speaker(raw))
                raw_to_label[raw] = label

        if not raw_to_sessions:
            messagebox.showinfo("Manage Speakers",
                "No speaker tags found.  Multi-track sessions get speaker\n"
                "tags automatically when they're transcribed.")
            return

        win = tk.Toplevel(self)
        win.title("Manage Speakers")
        win.configure(bg=BG)
        win.transient(self); win.grab_set()
        win.minsize(640, 360)

        tk.Label(win, text="Manage Speakers", font=FH,
                 bg=BG, fg=TEXT, padx=24, pady=14).pack(anchor="w")
        tk.Label(win,
                 text=("Rename any speaker.  Changes apply to every session\n"
                       "in this project that references the same source."),
                 font=FB, bg=BG, fg=SUB, padx=24, justify="left"
                 ).pack(anchor="w", pady=(0, 12))

        # Table: raw-tag (read-only) + current label (editable)
        body = tk.Frame(win, bg=SURF,
                        highlightbackground=BORDER, highlightthickness=1)
        body.pack(fill="both", expand=True, padx=24, pady=(0, 12))

        hdr = tk.Frame(body, bg=SURF3)
        hdr.pack(fill="x")
        tk.Label(hdr, text="SOURCE", font=FL, bg=SURF3, fg=SUB,
                 anchor="w", padx=12, pady=6, width=44).pack(side="left")
        tk.Label(hdr, text="DISPLAY LABEL", font=FL, bg=SURF3, fg=SUB,
                 anchor="w", padx=12, pady=6).pack(side="left")
        tk.Label(hdr, text="USED IN", font=FL, bg=SURF3, fg=SUB,
                 anchor="e", padx=12, pady=6).pack(side="right")

        entries = {}   # raw → StringVar
        for raw in sorted(raw_to_sessions.keys()):
            row = tk.Frame(body, bg=SURF)
            row.pack(fill="x")
            tk.Label(row, text=raw, font=FB, bg=SURF, fg=SUB,
                     anchor="w", padx=12, pady=6, width=44
                     ).pack(side="left")
            var = tk.StringVar(value=raw_to_label.get(raw, raw))
            entries[raw] = var
            entry = tk.Entry(row, textvariable=var, font=FB,
                              bg=SURF2, fg=TEXT, insertbackground=TEXT,
                              relief="flat", bd=4, width=32)
            entry.pack(side="left", padx=(0, 8), ipady=2)
            n_sessions = len(raw_to_sessions[raw])
            tk.Label(row,
                     text="{} session{}".format(
                         n_sessions, "s" if n_sessions != 1 else ""),
                     font=FB, bg=SURF, fg=SUB,
                     anchor="e", padx=12, pady=6
                     ).pack(side="right")

        nav = tk.Frame(win, bg=BG)
        nav.pack(fill="x", padx=24, pady=(0, 16))

        def _save():
            renamed = 0
            for raw, var in entries.items():
                new_label = var.get().strip()
                if not new_label:
                    continue
                if new_label == raw_to_label.get(raw):
                    continue
                # Apply across every session that references this raw tag
                for sess in raw_to_sessions.get(raw, []):
                    speakers_map = dict(sess.get("speakers") or {})
                    speakers_map[raw] = new_label
                    # Also key by basename in case the renderer falls
                    # back to that lookup form.
                    speakers_map[os.path.basename(raw)] = new_label
                    sess["speakers"] = speakers_map
                    self._pq_save_session_file(sess)
                renamed += 1
            win.destroy()
            # Re-render whatever view we're in so the new labels show.
            # Preserve cursor / selection / scroll — only speaker
            # labels changed; the user's reading position should not.
            if getattr(self, "_pq_current_session", None) is not None:
                _rn_state = self._pq_capture_text_state()
                self._pq_render_transcript_text(self._pq_current_session)
                self._pq_restore_text_state(_rn_state)
            else:
                self._pq_render_project_view()
            messagebox.showinfo("Speakers updated",
                "Renamed {} speaker label{}.".format(
                    renamed, "s" if renamed != 1 else ""))

        _W = 8
        self._btn(nav, "SAVE", _save,
                  color=ACCENT, width=_W).pack(side="right")
        self._btn(nav, "CANCEL", win.destroy, width=_W
                  ).pack(side="right", padx=(0, 8))

        win.update_idletasks()
        pw = self.winfo_width(); ph = self.winfo_height()
        px = self.winfo_rootx(); py = self.winfo_rooty()
        ww = win.winfo_width();  wh = win.winfo_height()
        win.geometry("+{}+{}".format(
            px + max(0, (pw - ww) // 2),
            py + max(0, (ph - wh) // 2)))
        self.wait_window(win)

    def _pq_inline_rename_speaker(self, event, tx):
        """Double-click on a speaker label → rename it everywhere in
        the current Episode Project.  Same effect as the MANAGE
        SPEAKERS dialog, but one-click for the visible label."""
        # Find the raw speaker tag at the click position
        pos = tx.index("@{},{}".format(event.x, event.y))
        raw = None
        for tname in tx.tag_names(pos):
            if tname.startswith("speaker_raw:"):
                raw = tname.split(":", 1)[1]
                break
        if not raw:
            return "break"

        project = getattr(self, "_pq_project", None)
        session = getattr(self, "_pq_current_session", None)
        if not project or not session:
            return "break"

        # Resolve current display label
        sess_speakers = session.get("speakers") or {}
        cur_label = (sess_speakers.get(raw)
                     or sess_speakers.get(os.path.basename(raw))
                     or self._pq_format_speaker(raw))

        # Inline dialog
        win = tk.Toplevel(self)
        win.title("Rename Speaker")
        win.configure(bg=BG)
        win.transient(self); win.grab_set()
        win.resizable(False, False)
        tk.Label(win, text="Rename speaker", font=FH,
                 bg=BG, fg=TEXT, padx=20, pady=14).pack(anchor="w")
        tk.Label(win, text="Source: {}".format(raw), font=FB,
                 bg=BG, fg=SUB, padx=20).pack(anchor="w")
        tk.Label(win,
                 text="(Renaming applies to every session in this "
                      "project that references the same source.)",
                 font=FB, bg=BG, fg=SUB, padx=20, wraplength=420,
                 justify="left").pack(anchor="w", pady=(0, 8))

        var = tk.StringVar(value=cur_label)
        ent = tk.Entry(win, textvariable=var, font=FB,
                        bg=SURF2, fg=TEXT, insertbackground=TEXT,
                        relief="flat", bd=4, width=32)
        ent.pack(padx=20, ipady=2)
        ent.select_range(0, "end")
        ent.focus_set()

        nav = tk.Frame(win, bg=BG)
        nav.pack(fill="x", padx=20, pady=14)

        def _apply():
            new = var.get().strip()
            if not new or new == cur_label:
                win.destroy(); return
            n_changed = 0
            for s in project["sessions"]:
                # Apply only if this session has any word with this raw tag
                touches = any(
                    w.get("speaker") == raw
                    for w in (s.get("transcript") or []))
                if not touches:
                    continue
                m = dict(s.get("speakers") or {})
                m[raw] = new
                m[os.path.basename(raw)] = new
                s["speakers"] = m
                self._pq_save_session_file(s)
                n_changed += 1
            win.destroy()
            # Re-render the open session view.  Preserve cursor /
            # selection / scroll — only the speaker label changed.
            _ir_state = self._pq_capture_text_state()
            self._pq_render_transcript_text(session)
            self._pq_restore_text_state(_ir_state)

        _W = 8
        self._btn(nav, "RENAME", _apply,
                  color=ACCENT, width=_W).pack(side="right")
        self._btn(nav, "CANCEL", win.destroy, width=_W
                  ).pack(side="right", padx=(0, 8))
        win.bind("<Return>", lambda e: _apply())
        win.bind("<Escape>", lambda e: win.destroy())

        win.update_idletasks()
        pw = self.winfo_width(); ph = self.winfo_height()
        px = self.winfo_rootx(); py = self.winfo_rooty()
        ww = win.winfo_width();  wh = win.winfo_height()
        win.geometry("+{}+{}".format(
            px + max(0, (pw - ww) // 2),
            py + max(0, (ph - wh) // 2)))
        self.wait_window(win)
        return "break"

    def _pq_filter_filler_interjections(self, words):
        """Drop cross-track filler interjections.

        A "filler turn" is a short run (≤ FILLER_MAX_WORDS) of one
        speaker's words that's sandwiched between same-speaker turns of
        a *different* speaker, AND the run consists only of common
        backchannel words ("yeah", "mm-hmm", "right", etc.).  These
        almost always come from cross-mic bleed or listener filler that
        adds noise to the read.  Removed at render time only — the raw
        transcript on disk is unchanged so the user can adjust the
        threshold or disable filtering without re-transcribing.

        Configurable knobs:
          self._pq_filler_words      → set of lowercase backchannels
          self._pq_filler_max_words  → max length for a candidate run
        """
        DEFAULT_FILLERS = {
            # Affirmative / negative backchannels
            "yeah", "yep", "yup", "yes", "no", "nope",
            # Approval / agreement
            "right", "okay", "ok", "sure", "totally", "exactly",
            "absolutely", "true", "definitely", "certainly",
            # Vocalisations / hesitations
            "mm", "mmm", "mhm", "mhmm", "mmhmm", "hmm", "hm",
            "uhhuh", "uhuh", "huh", "wow", "oh", "ah", "uh", "um",
            "ugh", "er", "eh", "oof",
            # Single-word filler
            "i", "well", "so", "like",
        }
        fillers = set(getattr(self, "_pq_filler_words", DEFAULT_FILLERS))
        max_w   = int(getattr(self, "_pq_filler_max_words", 3))

        if not words:
            return words

        # Group into consecutive same-speaker runs
        runs = []
        cur_speaker = object()
        cur_words   = []
        for w in words:
            sp = w.get("speaker")
            if sp != cur_speaker:
                if cur_words:
                    runs.append((cur_speaker, cur_words))
                cur_speaker = sp
                cur_words   = []
            cur_words.append(w)
        if cur_words:
            runs.append((cur_speaker, cur_words))

        def _normalise(wt):
            """Lowercase + strip ALL non-alpha (including hyphens like
            in '-hmm.', dashes, periods).  Apostrophes preserved for
            'i'm' / 'don't' style fillers."""
            return re.sub(r"[^a-z']", "", (wt or "").lower())

        def _is_filler_run(run_words):
            if len(run_words) > max_w:
                return False
            saw_text = False
            for w in run_words:
                clean = _normalise(w.get("word") or "")
                if not clean:
                    continue
                saw_text = True
                # Accept hyphenated compounds like "uh-huh" / "mm-hmm"
                # by also testing each piece against the filler set.
                if clean in fillers:
                    continue
                # Try splitting on internal hyphens that survived stripping
                # (shouldn't happen post-_normalise, but defensive).
                parts = [p for p in clean.split("-") if p]
                if parts and all(p in fillers for p in parts):
                    continue
                return False
            return saw_text

        keep = [True] * len(runs)
        for i in range(1, len(runs) - 1):
            sp, rw = runs[i]
            prev_sp = runs[i - 1][0]
            next_sp = runs[i + 1][0]
            # Drop only if the run is sandwiched between same-other-speaker
            # turns AND every word is a filler.
            if (prev_sp == next_sp and prev_sp != sp
                    and prev_sp is not None
                    and _is_filler_run(rw)):
                keep[i] = False

        out = []
        for kept, (_, rw) in zip(keep, runs):
            if kept:
                out.extend(rw)
        return out

    def _pq_format_speaker(self, raw):
        """Tidy a raw speaker tag (filename basename) into a display
        label.  Strips known boilerplate and uppercases."""
        if not raw:
            return ""
        s = str(raw)
        s = re.sub(r"\.[a-zA-Z0-9]+$", "", s)        # ext if any
        s = re.sub(r"^riverside[_\- ]+", "", s, flags=re.I)
        s = re.sub(r"raw[\-_ ]?audio[\-_ ]?", "", s, flags=re.I)
        s = re.sub(r"raw[\-_ ]?video[\-_ ]?", "", s, flags=re.I)
        s = re.sub(r"_blood[_\- ]trails[_\- ]\d+", "", s, flags=re.I)
        s = re.sub(r"[_\-]+", " ", s).strip()
        return s.upper() if s else "?"

    def _pq_detect_paragraph_breaks(self, text):
        """Syntactic paragraph-break detection on already-rendered text.

        Returns a sorted list of character positions in `text`.  At each
        position the renderer should insert a "\\n\\n" — i.e. break the
        paragraph BEFORE the character at that position.

        Heuristics, in order of precedence (any one triggers a break):
          • The sentence we just finished ended with "?"
                — questions in interview prose almost always sit at a
                  paragraph boundary (interviewer asks → interviewee
                  answers, or vice versa).
          • The next sentence starts with a discourse marker
                — "So,", "Well,", "Now,", "Okay,", "Right,", "Anyway,"
                  are reliable paragraph openers in spoken interviews.
          • Length cap: after `SENTENCES_PER_PARA` consecutive sentences
            without any other trigger, force a break so we never end up
            with one runaway paragraph.

        Knobs are exposed as instance attrs (`_pq_sentences_per_para`,
        `_pq_discourse_starters`) so they can be tweaked from a Python
        REPL launched with `python -i` without re-transcribing.
        """
        if not text:
            return []

        SENTENCES_PER_PARA = int(getattr(self, "_pq_sentences_per_para", 4))
        MIN_SENTENCES_BEFORE_BREAK = int(
            getattr(self, "_pq_min_sentences_before_break", 2))
        # Hard char fallback: if we ever go this far without any break,
        # force one at the next sentence boundary regardless of triggers.
        # Catches the "last third runs on" failure mode where Whisper's
        # capitalisation drifts and our other signals stop firing.
        MAX_CHARS_PER_PARA = int(
            getattr(self, "_pq_max_chars_per_para", 700))
        # Only the strong topic-shifters.  "right", "yeah", "alright"
        # were too common as acknowledgments and produced 1-sentence
        # filler paragraphs.
        DEFAULT_STARTERS = {
            "so", "well", "now", "okay", "anyway",
        }
        starters = set(getattr(self, "_pq_discourse_starters",
                                DEFAULT_STARTERS))

        positions = []
        n = len(text)
        i = 0
        sentences_since_break = 0
        last_break_pos = 0
        while i < n:
            ch = text[i]
            if ch in ".?!":
                # Walk past any trailing punctuation cluster (e.g. "?!")
                j = i + 1
                while j < n and text[j] in ".?!":
                    j += 1
                # Skip whitespace
                k = j
                while k < n and text[k].isspace():
                    k += 1
                # Sentence boundary if next char is alphabetic (any case).
                # Relaxed from upper-only because Whisper's capitalisation
                # gets unreliable in long transcripts and we'd otherwise
                # stop detecting boundaries entirely.
                if k < n and text[k].isalpha():
                    sentences_since_break += 1
                    # Read the next leading word(s) (up to 2)
                    m = k
                    while m < n and (text[m].isalpha() or text[m] == "'"):
                        m += 1
                    first_word = text[k:m].lower()
                    # Optional second word for compound starters
                    p = m
                    while p < n and text[p].isspace():
                        p += 1
                    q = p
                    while q < n and (text[q].isalpha() or text[q] == "'"):
                        q += 1
                    second_word = text[p:q].lower()
                    two_word = (first_word + " " + second_word).strip()

                    was_question  = (ch == "?")
                    is_starter    = (first_word in starters
                                     or two_word in starters)
                    para_full     = sentences_since_break >= SENTENCES_PER_PARA
                    long_enough   = sentences_since_break >= MIN_SENTENCES_BEFORE_BREAK
                    char_overflow = (k - last_break_pos) > MAX_CHARS_PER_PARA

                    # `para_full` and `char_overflow` are unconditional
                    # caps (length and character).  Question/discourse
                    # breaks only fire if the paragraph we'd be closing
                    # has at least MIN_SENTENCES_BEFORE_BREAK sentences
                    # — avoids fragmenting prose into 1-sentence filler.
                    if (para_full or char_overflow
                        or ((was_question or is_starter) and long_enough)):
                        positions.append(k)
                        sentences_since_break = 0
                        last_break_pos = k
                    i = k
                    continue
            i += 1

        # Final safety net: if any span between consecutive breaks is
        # still > 2× MAX_CHARS (because no sentence boundaries were
        # detected at all in that region), force breaks at the nearest
        # whitespace to the midpoint.  This handles transcripts where
        # Whisper produced a long stretch with no punctuation.
        TOO_LONG = 2 * MAX_CHARS_PER_PARA
        boundaries = [0] + positions + [n]
        forced = []
        for idx in range(len(boundaries) - 1):
            start = boundaries[idx]
            end   = boundaries[idx + 1]
            span  = end - start
            if span <= TOO_LONG:
                continue
            # Bisect: insert breaks every MAX_CHARS_PER_PARA at nearest spaces
            cur = start + MAX_CHARS_PER_PARA
            while cur < end - MAX_CHARS_PER_PARA // 2:
                bp = cur
                # Find nearest whitespace within ±200 chars
                for off in range(200):
                    if cur - off > start and text[cur - off].isspace():
                        bp = cur - off + 1
                        break
                    if cur + off < end and text[cur + off].isspace():
                        bp = cur + off + 1
                        break
                forced.append(bp)
                cur = bp + MAX_CHARS_PER_PARA
        if forced:
            positions = sorted(set(positions + forced))

        return positions

    def _pq_update_status_lbl(self, session):
        lbl = getattr(self, "_pq_status_lbl", None)
        if not lbl:
            return
        n_words = len(session.get("transcript") or [])
        if not n_words:
            lbl.config(text="not transcribed", fg=WARN)
            return
        last  = session["transcript"][-1]
        dur_s = float(last.get("end", 0))
        h = int(dur_s // 3600)
        m = int((dur_s % 3600) // 60)
        s = int(dur_s % 60)
        lbl.config(text="{:,} words · {:02d}:{:02d}:{:02d}".format(
                       n_words, h, m, s), fg=SUB)

    def _pq_remove_session(self, session):
        if not messagebox.askyesno(
                "Remove from project",
                "Remove '{}' from this Episode Project?\n\n"
                "The session JSON on disk will not be deleted.".format(
                    session.get("token", "?"))):
            return
        self._pq_project["sessions"] = [
            s for s in self._pq_project["sessions"] if s is not session]
        self._pq_render_project_view()

    def _pq_add_media_to_session(self, session):
        files = filedialog.askopenfilenames(
            title="Add media to session",
            filetypes=[("Media",
                        "*.wav *.aif *.aiff *.bwf *.mp3 *.m4a *.flac "
                        "*.mp4 *.mov *.mxf *.mkv *.avi *.m4v"),
                       ("All", "*.*")])
        added_paths = []
        media = list(session.get("media", []))
        for f in files:
            f = os.path.abspath(f)
            if f not in media and is_media(f):
                media.append(f)
                added_paths.append(f)
        if not added_paths:
            return
        session["media"] = media

        # ── Sidecar-adoption path ──────────────────────────────────────
        # Before wiping session["transcript"], probe each newly-added
        # file for a .pb_transcript.json sidecar.  Previously we always
        # cleared the transcript and required the user to click ⟳
        # TRANSCRIBE just to reach the USE EXISTING prompt — an obvious
        # dead-end when they just wanted to search a transcript that
        # already exists.  Now: if any added file has a valid sidecar,
        # route through the same _pq_confirm_retranscribe dialog that
        # the TRANSCRIBE-button flow uses, so USE EXISTING / RE-TRANSCRIBE
        # is offered inline.
        sidecar_paths = []
        for p in added_paths:
            try:
                if engines.pb_transcript_load(p)[0] is not None:
                    sidecar_paths.append(p)
            except Exception:
                pass

        has_session_transcript = bool(session.get("transcript"))
        if sidecar_paths or has_session_transcript:
            choice, preserve_edits = self._pq_confirm_retranscribe(
                session, has_session_transcript, sidecar_paths)
            if choice == "cancel":
                # Roll back the media add so the session stays as it was.
                session["media"] = [p for p in media if p not in added_paths]
                return
            if choice == "use_cached" and sidecar_paths:
                # Load the first sidecar's words directly into the session
                # — mirrors _pq_run_transcription's USE EXISTING handler.
                words, _ = engines.pb_transcript_load(sidecar_paths[0])
                if words:
                    session["transcript"] = list(words)
                    self._pq_save_session_file(session)
                    self._pq_render_media_list(session)
                    self._pq_render_transcript_text(session)
                    if getattr(self, "_pq_retx_btn", None):
                        self._pq_retx_btn.config(text="⟳ RE-TRANSCRIBE")
                    return
            # Fall through: user chose RE-TRANSCRIBE (or use_cached had
            # no words) — proceed with the existing wipe + re-render
            # flow AND kick off the transcription immediately so they
            # don't have to click TRANSCRIBE separately.
            session["transcript"] = []
            self._pq_save_session_file(session)
            self._pq_render_media_list(session)
            self._pq_render_transcript_text(session)
            if getattr(self, "_pq_retx_btn", None):
                self._pq_retx_btn.config(text="⟳ TRANSCRIBE")
            self.after(80,
                       lambda s=session: self._pq_run_transcription(s))
            return

        # No sidecar and no existing transcript — original behaviour:
        # wipe + re-render, wait for the user to click TRANSCRIBE.
        session["transcript"] = []
        self._pq_save_session_file(session)
        self._pq_render_media_list(session)
        self._pq_render_transcript_text(session)
        if getattr(self, "_pq_retx_btn", None):
            self._pq_retx_btn.config(text="⟳ TRANSCRIBE")

    def _pq_confirm_retranscribe(self, session, has_session_transcript,
                                  sidecar_paths):
        """Ask the user what to do when transcript data already exists.

        Returns a tuple (choice, preserve_edits):
            choice         — "transcribe" / "use_cached" / "cancel"
            preserve_edits — bool; when True, any prior user edits
                             (in-memory OR in sidecar) are merged into
                             the new transcription via the per-word
                             _src='user' mechanism.  When False, edits
                             are discarded before the run starts.
                             Always False when no edits exist.
        """
        # Count user edits from both sources.  In-memory session wins
        # if both have content — that's the live state.  Otherwise
        # peek at the most-recent sidecar so a fresh-session
        # re-transcribe surfaces edits that aren't loaded yet.
        session_edits = 0
        if has_session_transcript:
            session_edits = len(
                [w for w in session.get("transcript", [])
                 if w.get("_src") == "user"])

        sidecar_edits = 0
        sidecar_edit_src = None
        if sidecar_paths and not has_session_transcript:
            for sp in sidecar_paths:
                try:
                    words, _ = engines.pb_transcript_load(sp)
                    if words:
                        n = sum(1 for w in words
                                if w.get("_src") == "user")
                        if n:
                            sidecar_edits = n
                            sidecar_edit_src = sp
                            break
                except Exception:
                    pass

        total_edits = session_edits + sidecar_edits

        win = tk.Toplevel(self)
        win.title("Transcript Already Exists")
        win.configure(bg=BG)
        win.transient(self); win.grab_set()
        win.resizable(False, False)

        hdr = tk.Frame(win, bg=WARN, padx=16, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="⚠   Transcript Already Exists",
                 font=FBT, bg=WARN, fg="#1c1c1c").pack(anchor="w")

        body = tk.Frame(win, bg=BG, padx=20, pady=14)
        body.pack(fill="both", expand=True)

        lines = []
        if has_session_transcript:
            wc = len([w for w in session.get("transcript", [])
                      if (w.get("word") or "").strip()])
            lines.append(
                "Session already has a {:,}-word transcript.".format(wc))
        if sidecar_paths:
            n = len(sidecar_paths)
            extra = ""
            if sidecar_edits:
                extra = " (with {} user edit{})".format(
                    sidecar_edits,
                    "s" if sidecar_edits != 1 else "")
            lines.append(
                "{} cached transcript{} on disk{}.".format(
                    n, "s" if n != 1 else "", extra))

        tk.Label(body, text="\n".join(lines), font=FB,
                 bg=BG, fg=TEXT, justify="left",
                 wraplength=520).pack(anchor="w", pady=(0, 8))

        # Preserve-edits checkbox.  Shown only when edits exist
        # somewhere (in-memory session OR in the sidecar that would
        # otherwise be ignored on a fresh-session re-transcribe).
        # Default ON — losing user work silently is the worst default.
        preserve_var = tk.BooleanVar(value=True)
        if total_edits:
            edit_total = max(session_edits, sidecar_edits)
            cb_text = ("Preserve {} edited word{} when "
                       "re-transcribing").format(
                edit_total, "s" if edit_total != 1 else "")
            cb = tk.Checkbutton(
                body, text=cb_text, variable=preserve_var,
                font=FB, bg=BG, fg=TEXT, selectcolor=SURF2,
                activebackground=BG, activeforeground=TEXT,
                highlightthickness=0, bd=0)
            cb.pack(anchor="w", pady=(4, 10))

        result = {"choice": "cancel"}
        # Uniform button sizing — all three buttons same width and height
        # so they align cleanly along the bottom row.
        nav = tk.Frame(win, bg=BG)
        nav.pack(fill="x", padx=20, pady=(0, 14))

        def _pick(c):
            result["choice"] = c
            win.destroy()

        # All buttons pack right-aligned in one tight group with uniform
        # 8 px gaps.  Packing in reverse order so the visual sequence
        # reads CANCEL  RE-TRANSCRIBE  USE EXISTING from left to right.
        _W = 13
        if sidecar_paths:
            self._btn(nav, "USE EXISTING",
                      lambda: _pick("use_cached"),
                      color=ACCENT, width=_W
                      ).pack(side="right")
            self._btn(nav, "RE-TRANSCRIBE",
                      lambda: _pick("transcribe"),
                      width=_W).pack(side="right", padx=(0, 8))
        else:
            self._btn(nav, "RE-TRANSCRIBE",
                      lambda: _pick("transcribe"),
                      color=ACCENT, width=_W
                      ).pack(side="right")
        self._btn(nav, "CANCEL", lambda: _pick("cancel"),
                  width=_W).pack(side="right", padx=(0, 8))

        win.bind("<Escape>", lambda e: _pick("cancel"))

        self._center_dialog(win, 580, 240)
        self.wait_window(win)

        # Re-transcribe ALWAYS visually wipes — Jordan explicitly
        # wanted a clean slate so the draft pass appears against an
        # empty pane rather than fighting stale content.  Two modes:
        #
        #   preserve=True   → keep only the user-edited words; their
        #                     timestamps survive and the worker's
        #                     merge re-inserts them at the right
        #                     positions as the draft / configured
        #                     passes stream in.
        #   preserve=False  → full wipe; nothing carries over.
        #
        # When the only source of edits is a sidecar (new-session
        # re-transcribe), hoist its user-tagged words onto the
        # session first so they participate in the merge.
        preserve = bool(preserve_var.get()) and bool(total_edits)
        if result["choice"] == "transcribe":
            if preserve:
                if has_session_transcript:
                    session["transcript"] = [
                        w for w in session["transcript"]
                        if w.get("_src") == "user"]
                elif sidecar_edits and sidecar_edit_src:
                    try:
                        words, _ = engines.pb_transcript_load(
                            sidecar_edit_src)
                        session["transcript"] = [
                            w for w in (words or [])
                            if w.get("_src") == "user"]
                    except Exception:
                        session["transcript"] = []
                else:
                    session["transcript"] = []
            else:
                session["transcript"] = []
            self._pq_save_session_file(session)

        return result["choice"], preserve

    def _pq_preview_ready(self, session):
        """Called on the main thread when the tiny draft pass completes
        for a session.  Renders the preview transcript and flips the
        status label to 'preview · refining…' so the user knows the
        configured model is still working in the background."""
        if getattr(self, "_pq_current_session", None) is session:
            # Preserve cursor / selection / scroll across this render
            # — without it the user gets bounced to the top of the
            # transcript every time tiny finishes.
            state = self._pq_capture_text_state()
            try:
                self._pq_render_transcript_text(session)
            except Exception:
                pass
            else:
                self._pq_restore_text_state(state)
            lbl = getattr(self, "_pq_status_lbl", None)
            if lbl:
                try:
                    lbl.config(text="preview · refining…", fg=WARN)
                except tk.TclError:
                    pass

    @staticmethod
    def _pq_merge_streams(tiny_words, main_words, current_words):
        """Live merge of two streaming transcript sources plus the
        user-edited words already in the current session transcript.

        Priority:  user > main (configured) > tiny (draft) > backdrop

        Algorithm:
          1. User words from current_words are sacred.  Their time
             spans are computed and every other source's words in those
             spans are dropped.
          2. Main's coverage is tracked via its 'frontier' — the latest
             end-time of any main word emitted so far.  Words before
             the frontier are 'main has decoded this'; words after it
             are 'main hasn't reached yet'.
          3. Tiny words AFTER the main frontier are kept; BEFORE it
             they're dropped (main has them).
          4. BACKDROP — current_words' non-user, non-tiny content (e.g.
             pre-existing transcript on a re-transcribe).  Fills any
             timestamp range still uncovered by main+tiny.  Without
             this, every tick of a re-transcribe (where tiny doesn't
             run) silently deleted everything past main's frontier
             until main caught up.
          5. All surviving words are sorted by start time; duplicates
             at the same timestamp (within a small epsilon) are
             deduped, keeping the higher-priority source.

        Result: the live transcript starts as the existing transcript
        / tiny draft, then progressively gets replaced by main as main
        catches up, with user edits anchored throughout.
        """
        user_words = [w for w in (current_words or [])
                      if w.get("_src") == "user"]
        # IMPORTANT: pass the FULL current_words list, not the filtered
        # user-only list.  _pq_user_edited_spans uses non-user words as
        # run-break markers — feeding it the filtered list collapses
        # every scattered user edit into one giant span from the first
        # edit to the last, which then drops every other word in that
        # range.  (Hit this bug in stream diagnostics: 4 scattered
        # edits caused merged=388 instead of ~1700.)
        user_spans = PqWorkflowMixin._pq_user_edited_spans(current_words)

        def _in_user(t):
            return any(s <= t <= e for (s, e) in user_spans)

        # Main's coverage frontier — latest end time of any main word.
        main_frontier = 0.0
        for w in main_words or []:
            if w.get("break"):
                continue
            we = float(w.get("end", 0.0))
            if we > main_frontier:
                main_frontier = we

        # Tiny's coverage envelope, used to know whether the backdrop
        # should fill a range or defer to tiny.  An empty tiny buffer
        # means tiny isn't running this pass (re-transcribe) so the
        # backdrop should fill everything past main's frontier.
        tiny_start = float("inf")
        tiny_end   = 0.0
        for w in tiny_words or []:
            if w.get("break"):
                continue
            ws = float(w.get("start", 0.0))
            we = float(w.get("end",   ws))
            if ws < tiny_start: tiny_start = ws
            if we > tiny_end:   tiny_end   = we
        has_tiny = (tiny_end > 0.0)

        result = []

        # 1) main words (highest priority)
        for w in main_words or []:
            if w.get("break"):
                t = float(w.get("start", 0.0))
                if not _in_user(t):
                    result.append(w)
                continue
            mid = (float(w.get("start", 0.0))
                   + float(w.get("end", 0.0))) / 2.0
            if not _in_user(mid):
                result.append(w)

        # 2) tiny words past the main frontier
        for w in tiny_words or []:
            if w.get("break"):
                t = float(w.get("start", 0.0))
                if _in_user(t) or t <= main_frontier:
                    continue
                result.append(w)
                continue
            mid = (float(w.get("start", 0.0))
                   + float(w.get("end", 0.0))) / 2.0
            if _in_user(mid) or mid <= main_frontier:
                continue
            result.append(w)

        # 3) backdrop — pre-existing non-user words from current_words.
        #    Skip anything inside a user span, behind main's frontier,
        #    OR (when tiny is active) inside tiny's coverage envelope
        #    — tiny is fresher than the previous render's leftovers.
        for w in (current_words or []):
            src = w.get("_src")
            if src == "user":
                continue   # handled separately
            if w.get("break"):
                t = float(w.get("start", 0.0))
                if _in_user(t) or t <= main_frontier:
                    continue
                if has_tiny and tiny_start <= t <= tiny_end:
                    continue
                result.append(w)
                continue
            ws = float(w.get("start", 0.0))
            we = float(w.get("end",   ws))
            mid = (ws + we) / 2.0
            if _in_user(mid) or mid <= main_frontier:
                continue
            if has_tiny and tiny_start <= mid <= tiny_end:
                continue
            result.append(w)

        # 4) user words — ALL of them, always.  Filtering them by
        # the streaming frontier here would drop edits made during
        # streaming whose audio time hadn't been reached yet, and
        # the next tick wouldn't see them in current_words to put
        # them back.  The "hide until streaming reaches them" visual
        # behaviour is applied at render time instead.
        result.extend(user_words)

        result.sort(key=lambda w: float(w.get("start", 0.0)))
        return result

    @staticmethod
    def _pq_user_edited_spans(words):
        """Walk a transcript and return a list of (start_s, end_s) spans
        covering every contiguous run of words with _src=='user'.  Used
        by the refine-merge logic to keep user edits intact when a
        higher-quality transcription pass overwrites the rest.

        Run-break heuristic: a run ends when EITHER a non-user word
        appears OR the audio-time gap to the next user word exceeds
        GAP_S seconds.  The time-gap check matters during streaming
        re-transcribe when the transcript momentarily contains nothing
        BUT scattered user edits — without it, three edits at 50 / 200
        / 500 s would merge into one (50, 500) span and the merge
        would drop everything inside that range."""
        GAP_S = 2.0
        spans = []
        run = None
        prev_end = None
        for w in words or []:
            if w.get("break"):
                continue
            if w.get("_src") == "user":
                ws = float(w.get("start", 0.0))
                we = float(w.get("end",   ws))
                if run is None:
                    run = [ws, we]
                elif prev_end is not None and (ws - prev_end) > GAP_S:
                    spans.append(tuple(run))
                    run = [ws, we]
                else:
                    run[1] = max(run[1], we)
                prev_end = we
            else:
                if run is not None:
                    spans.append(tuple(run))
                    run = None
                    prev_end = None
        if run is not None:
            spans.append(tuple(run))
        return spans

    @staticmethod
    def _pq_merge_refine(current_words, refine_words):
        """Merge a refined (higher-quality) transcription into the current
        transcript, preserving every word in user-edited time spans.

        Algorithm:
          1. Identify user-edited spans in current_words (_src=='user')
          2. Drop refine_words that fall inside any user span
          3. Splice the surviving user words back into the result at
             their original timestamps
          4. Sort everything by start time

        Returns a new word list — caller assigns to session['transcript']."""
        user_spans = PqWorkflowMixin._pq_user_edited_spans(current_words)

        def _in_user_span(t):
            return any(s <= t <= e for (s, e) in user_spans)

        # Refine words that DON'T overlap user-edited regions
        keep = []
        for w in refine_words or []:
            if w.get("break"):
                # Drop refine breaks too if they fall in a user span;
                # the user's words own that region of audio.
                t = float(w.get("start", 0.0))
                if _in_user_span(t):
                    continue
                keep.append(w)
                continue
            ws = float(w.get("start", 0.0))
            we = float(w.get("end",   ws))
            mid = (ws + we) / 2.0
            if _in_user_span(mid):
                continue
            keep.append(w)

        # User words to splice in
        user_words = [
            w for w in (current_words or [])
            if w.get("_src") == "user"
        ]
        merged = keep + user_words
        merged.sort(key=lambda w: float(w.get("start", 0.0)))
        return merged

    def _pq_run_transcription(self, session):
        """Kick off (or refuse to start) a transcription for `session`.

        State lives on the session dict (`session["_progress"]`) so multiple
        sessions can be transcribing at once — start one, navigate back to
        the project, start the next, etc.  The animation timer reads from
        the *currently visible* session's progress dict.
        """
        # Refuse to start a duplicate run for the same session.
        if session.get("_progress", {}).get("active"):
            return

        media = list(session.get("media", []))
        if not media:
            messagebox.showwarning("No media",
                "Add at least one media file before transcribing.")
            return
        missing = [p for p in media if not os.path.isfile(p)]
        if missing:
            messagebox.showerror("Missing files",
                "These files no longer exist:\n\n" +
                "\n".join("  • " + p for p in missing))
            return

        if not HAS_WHISPER:
            messagebox.showerror("Not installed",
                "faster-whisper is not installed.\n\n"
                "Run:  pip install faster-whisper")
            return

        # Memory pre-flight — same Windows commit-charge check used by
        # Script→Session reconcile.  Catches the case where 15 GB of
        # physical RAM is free but the page file is exhausted and a
        # 300 MB feature buffer allocation will fail.
        if not self._preflight_memory_check():
            return

        # Pre-transcribe alert: existing transcript data on this session OR
        # a .pb_transcript.json sidecar next to any of the media files means
        # the user may not realise they're about to throw work away.  Ask
        # them to confirm overwrite, with a "use cached" option to pull
        # the sidecar transcript into this session without re-running
        # Whisper.
        has_session_transcript = bool(session.get("transcript"))
        sidecar_paths = []
        for p in media:
            try:
                if engines.pb_transcript_load(p)[0] is not None:
                    sidecar_paths.append(p)
            except Exception:
                pass

        if has_session_transcript or sidecar_paths:
            choice, preserve_edits = self._pq_confirm_retranscribe(
                session, has_session_transcript, sidecar_paths)
            if choice == "cancel":
                return
            if choice == "use_cached":
                # Pull the most-recent sidecar transcript onto the session
                # and skip the Whisper run entirely.
                src = sidecar_paths[0] if sidecar_paths else None
                if src:
                    words, _ = engines.pb_transcript_load(src)
                    if words:
                        session["transcript"] = list(words)
                        self._pq_save_session_file(session)
                        if (getattr(self, "_pq_current_session", None)
                                is session):
                            self._pq_render_transcript_text(session)
                        return
            # choice == "transcribe": the dialog has already wiped
            # session["transcript"] and (when preserve was checked)
            # hoisted just the user-edited words back onto it, so the
            # worker starts from a clean slate either way.  Force a
            # re-render here so the visual wipe lands immediately —
            # otherwise stale text lingers until the first stream tick.
            # user_frontier=0 hides any preserved edits at this stage
            # so the pane looks fully empty; streaming will reveal
            # each edit when it arrives at the edit's timestamp.
            if getattr(self, "_pq_current_session", None) is session:
                try:
                    self._pq_render_transcript_text(
                        session, user_frontier=0.0)
                except Exception:
                    pass

        bg_mode = bool(getattr(self, "_pq_bg_mode_var",
                               tk.BooleanVar(value=False)).get())

        # Per-session progress state — concurrent-safe: each session has its
        # own dict, the animation timer reads only the current session's.
        session["_progress"] = {
            "phase":      "starting",
            "extra":      "",
            "active":     True,
            "elapsed":    0,
            "started":    time.perf_counter(),
            "total_secs": 0.0,   # filled in by the worker once it probes
        }

        # Update UI for THIS session only if it's the one on screen.
        is_current = (getattr(self, "_pq_current_session", None) is session)
        if is_current:
            btn = getattr(self, "_pq_retx_btn", None)
            lbl = getattr(self, "_pq_status_lbl", None)
            if btn: btn.config(text="TRANSCRIBING…", fg=SUB)
            if lbl: lbl.config(text="starting…", fg=ACCENT)
            self._pq_render_progress_placeholder()
            self._pq_animate_progress()

        # Background-mode → lower process priority for the duration.
        # Track per-thread so concurrent runs don't fight over priority.
        prev_priority = self._pq_apply_priority(low=bg_mode)

        def _set_phase(phase, extra=""):
            prog = session.get("_progress")
            if not prog or not prog.get("active"):
                return
            prog["phase"] = phase
            prog["extra"] = extra

        def _fmt_dur(secs):
            secs = max(0, int(secs))
            h, r = divmod(secs, 3600)
            m, s = divmod(r, 60)
            return "{:02d}:{:02d}:{:02d}".format(h, m, s)

        # Cancel-event handle the worker passes into the engine.  Set
        # by _request_cancel_active_transcriptions when the user picks
        # "Cancel & apply now" in the model-swap dialog.  Stored on the
        # session dict so external code can find it.
        cancel_event = threading.Event()
        session["_cancel_event"] = cancel_event

        def _worker():
            t0 = time.perf_counter()
            try:
                # Probe total duration upfront so the progress display has
                # context — "of 01:32:15" rather than just "decoded".
                _set_phase("preparing")
                try:
                    durs = []
                    for p in media:
                        d = engines.get_media_duration(p) or 0.0
                        if d:
                            durs.append(float(d))
                    # mix_for_transcript pads to the longest input; for a
                    # single source the duration is just the source itself.
                    total = max(durs) if durs else 0.0
                    if (prog := session.get("_progress")):
                        prog["total_secs"] = total
                except Exception:
                    pass

                # Be explicit when the configured model isn't cached
                # yet — get_model() can sit for several minutes inside
                # faster-whisper's download path with no visible
                # progress.  Surface that we're downloading so the user
                # doesn't think the worker is hung.
                _cfg_size = engines.get_active_model_size()
                if not engines._is_model_cached(_cfg_size):
                    # Rough sizes for the status message.
                    _approx = {
                        "tiny":     "75 MB",
                        "base":     "145 MB",
                        "small":    "485 MB",
                        "medium":   "1.5 GB",
                        "large-v3": "3.1 GB",
                    }.get(_cfg_size, "")
                    _set_phase(
                        "downloading model ({}){}".format(
                            _cfg_size,
                            " — " + _approx if _approx else ""),
                        "")
                else:
                    _set_phase("loading model")
                engines.get_model()

                # Snapshot the pre-run transcript so re-transcribe can
                # preserve user edits via the same merge logic the
                # progressive flow uses.  If the user previously edited
                # any words (right-click → Edit), those entries carry
                # _src='user' and survive every subsequent run.
                pre_run_transcript = list(session.get("transcript") or [])
                pre_run_user_edits = [
                    w for w in pre_run_transcript
                    if w.get("_src") == "user"
                ]

                audios = [p for p in media if not is_video(p)]
                if not audios:
                    audios = list(media)

                tmp_paths = []
                try:
                    def _on_seg_end(audio_s, **kw):
                        prog = session.get("_progress")
                        if not prog or not prog.get("active"):
                            return
                        total = prog.get("total_secs", 0)
                        track_name = kw.get("track_name")
                        n_tracks   = kw.get("n_tracks", 1)
                        idx        = (kw.get("track_index", 0) or 0) + 1
                        prefix = ""
                        if track_name and n_tracks > 1:
                            prefix = " [track {}/{}]".format(idx, n_tracks)
                        if total > 0:
                            pct = max(0, min(100, audio_s * 100.0 / total))
                            prog["extra"] = "{} · {} of {} ({:.0f}%)".format(
                                prefix, _fmt_dur(audio_s),
                                _fmt_dur(total), pct)
                        else:
                            prog["extra"] = "{} · {} decoded".format(
                                prefix, _fmt_dur(audio_s))

                    # ── Streaming word emission ─────────────────────
                    # Fires once per decoded Whisper segment with the
                    # words from THAT segment.  Appends them to a
                    # streaming accumulator and triggers a throttled
                    # main-thread render so the user sees text appear
                    # progressively instead of waiting for the entire
                    # pass to finish.  Throttled to ~1.2 s between
                    # renders so we don't redraw on every short
                    # segment of a busy file.
                    # Two parallel streaming buffers: one filled by the
                    # tiny draft pass, one filled by the configured
                    # ("main") refinement pass.  Both render through
                    # the same throttle.  _pq_merge_streams combines
                    # them so main words win at any timestamp the main
                    # frontier has already passed, falling back to
                    # tiny ahead of the frontier, and user edits
                    # always survive.
                    streaming_words = []          # tiny draft buffer
                    streaming_main_words = []     # configured pass buffer
                    streaming_last_render = [0.0]
                    streaming_render_pending = [False]
                    streaming_tick = [0]
                    # Diagnostic logging — gated behind _PQ_STREAM_DIAG.
                    # When False (default), _pq_stream_log is a no-op and
                    # the log file is never touched.  Flip the flag at
                    # the top of the file when you need to inspect what
                    # the streaming merge is doing.
                    if _PQ_STREAM_DIAG:
                        _pq_stream_log_path = os.path.join(
                            _app_state_dir(), "_pq_stream.log")
                        try:
                            with open(_pq_stream_log_path, "w",
                                      encoding="utf-8") as _f0:
                                _f0.write(
                                  "# Pull Quotes stream diagnostics — "
                                  "one JSON line per render tick.\n")
                        except Exception:
                            pass

                        def _pq_stream_log(event, **fields):
                            try:
                                rec = {"t":  round(
                                            time.perf_counter() - t0, 2),
                                       "ev": event}
                                rec.update(fields)
                                with open(_pq_stream_log_path, "a",
                                          encoding="utf-8") as _f:
                                    _f.write(json.dumps(rec) + "\n")
                            except Exception:
                                pass
                    else:
                        def _pq_stream_log(event, **fields):
                            return

                    def _do_stream_render(s=session):
                        streaming_render_pending[0] = False
                        # Only render if this session is still on screen
                        # AND no full transcript has overwritten the
                        # streaming buffer (which would happen at the
                        # very end of the pass when the worker assigns
                        # the final word list).
                        if getattr(self, "_pq_current_session",
                                   None) is s:
                            # Three-way merge: tiny draft + configured
                            # main + user edits already in the session.
                            # User edits always survive; main wins over
                            # tiny for any region the main pass has
                            # reached; tiny shows ahead of the main
                            # frontier so the user sees a moving
                            # boundary instead of nothing.
                            current = s.get("transcript") or []
                            cur_user = sum(1 for w in current
                                           if w.get("_src") == "user")
                            cur_nonuser = sum(1 for w in current
                                              if (w.get("_src") != "user"
                                                  and not w.get("break")))
                            tiny_snap = list(streaming_words)
                            main_snap = list(streaming_main_words)
                            merged = PqWorkflowMixin._pq_merge_streams(
                                tiny_snap, main_snap, current)
                            streaming_tick[0] += 1
                            _pq_stream_log("render",
                                tick=streaming_tick[0],
                                tiny_buf=len(tiny_snap),
                                main_buf=len(main_snap),
                                cur_total=len(current),
                                cur_user=cur_user,
                                cur_nonuser=cur_nonuser,
                                merged=len(merged),
                                merged_user=sum(
                                    1 for w in merged
                                    if w.get("_src") == "user"))
                            s["transcript"] = merged
                            # Streaming frontier — the further-along of
                            # {main, tiny}.  Hides preserved user
                            # edits past this point at render time so
                            # they appear in place only once streaming
                            # arrives at their audio position.
                            _main_end = 0.0
                            for _w in main_snap:
                                if _w.get("break"): continue
                                _e = float(_w.get("end", 0.0))
                                if _e > _main_end: _main_end = _e
                            _tiny_end_val = 0.0
                            for _w in tiny_snap:
                                if _w.get("break"): continue
                                _e = float(_w.get("end", 0.0))
                                if _e > _tiny_end_val: _tiny_end_val = _e
                            _frontier = max(_main_end, _tiny_end_val)
                            # Capture the interactive state (cursor,
                            # selection, scroll, focus) BEFORE the
                            # re-render so Jordan can edit / scroll /
                            # select during the stream without being
                            # yanked back to the end on every tick.
                            state = self._pq_capture_text_state()
                            try:
                                self._pq_render_transcript_text(
                                    s, user_frontier=_frontier)
                            except Exception:
                                pass
                            else:
                                self._pq_restore_text_state(state)

                    def _schedule_stream_render():
                        now = time.perf_counter()
                        if (now - streaming_last_render[0] < 1.2
                                or streaming_render_pending[0]):
                            return
                        streaming_last_render[0] = now
                        streaming_render_pending[0] = True
                        # Schedule the render on the Tk main thread —
                        # this is called from worker threads.
                        self._ui(_do_stream_render)

                    def _stream_seg_cb_tiny(seg_words):
                        # Log timestamp range of the incoming segment
                        # so we can see exactly what tiny is emitting.
                        _real = [w for w in (seg_words or [])
                                 if not w.get("break")]
                        _pq_stream_log("seg_tiny",
                            n=len(seg_words or []),
                            n_real=len(_real),
                            t0=(float(_real[0]["start"])
                                if _real else None),
                            t1=(float(_real[-1]["end"])
                                if _real else None))
                        streaming_words.extend(seg_words)
                        _schedule_stream_render()

                    def _stream_seg_cb_main(seg_words):
                        _real = [w for w in (seg_words or [])
                                 if not w.get("break")]
                        _pq_stream_log("seg_main",
                            n=len(seg_words or []),
                            n_real=len(_real),
                            t0=(float(_real[0]["start"])
                                if _real else None),
                            t1=(float(_real[-1]["end"])
                                if _real else None))
                        streaming_main_words.extend(seg_words)
                        _schedule_stream_render()

                    # ── Decide whether (and how) to run a tiny draft pass
                    # When the user is actively viewing this session and
                    # their configured model isn't already 'tiny', do a
                    # fast tiny pass first so they see SOMETHING within
                    # ~30 s instead of waiting minutes for the full
                    # configured model.  Audio extraction takes a few
                    # seconds so the "is the user viewing?" check
                    # naturally functions as a dwell filter — drive-by
                    # transcribe-then-leave doesn't pay the tiny cost.
                    #
                    # On GPU we run tiny IN PARALLEL with the configured
                    # model (two CTranslate2 instances, separate threads).
                    # The tiny preview renders the moment it's ready; the
                    # configured pass continues unaffected.  On CPU we
                    # fall back to sequential — both inference loops
                    # contending for the same cores would slow each
                    # other down and the gain disappears.
                    configured_size = engines.get_active_model_size()
                    is_viewing_now = (getattr(self, "_pq_current_session",
                                               None) is session)
                    # Progressive (tiny draft → configured refine) runs
                    # any time the user is viewing AND the configured
                    # model is bigger than tiny.  We used to skip it
                    # when an existing transcript was present so a
                    # re-transcribe wouldn't downgrade quality before
                    # the configured pass caught up — but Jordan
                    # explicitly wanted a draft ASAP on re-transcribe
                    # too, and the merge keeps the prior content as
                    # backdrop / user edits as sacred regardless.
                    do_progressive = (is_viewing_now
                                       and configured_size != "tiny")
                    parallel_ok = engines.is_cuda_available()

                    # Per-pass thread storage when running parallel.
                    tiny_thread = None
                    tiny_words_box = [None]   # mutable container; thread writes

                    def _run_tiny_pass(audios=audios, mix_path_ref=None):
                        """Tiny draft worker.  Writes to tiny_words_box,
                        streams each segment's words into the live
                        transcript pane, and catches its own exceptions
                        so a tiny crash never kills the parent worker's
                        configured run."""
                        try:
                            if len(audios) == 1:
                                tw = engines.transcribe_clip_verbatim(
                                    mix_path_ref, progress_cb=_on_seg_end,
                                    cancel_event=cancel_event,
                                    model_size="tiny",
                                    segment_cb=_stream_seg_cb_tiny)
                            else:
                                speaker_labels = session.get("speakers") or {}
                                tw = engines.transcribe_session_per_track(
                                    audios, speaker_labels=speaker_labels,
                                    progress_cb=_on_seg_end,
                                    cancel_event=cancel_event,
                                    model_size="tiny",
                                    segment_cb=_stream_seg_cb_tiny)
                            tiny_words_box[0] = tw
                            if tw and getattr(
                                    self, "_pq_current_session", None
                                    ) is session:
                                # Final render with the complete tiny
                                # output (streaming may have rendered a
                                # slightly stale view at last throttle).
                                # Three-way merge against user edits
                                # AND any configured-pass words that
                                # may have already streamed in.
                                #
                                # Snapshot inputs on this thread, but
                                # marshal the actual session-state write
                                # onto the main thread.  The main thread
                                # ALSO writes session["transcript"] from
                                # _do_stream_render (line 11136-ish via
                                # _pq_merge_streams); writing this dict
                                # slot from two threads racelessly is
                                # what self._ui is for.  Narrow window
                                # (microseconds between the read and the
                                # assign) but the fix is cheap and
                                # brings the site into parity with the
                                # _ui-marshalled preview-ready call
                                # immediately below.
                                _tw_snap   = list(tw)
                                _smain_snap = list(streaming_main_words)
                                def _commit(s=session, tws=_tw_snap, sms=_smain_snap):
                                    s["transcript"] = PqWorkflowMixin._pq_merge_streams(
                                        tws, sms, s.get("transcript") or [])
                                    self._pq_preview_ready(s)
                                self._ui(_commit)
                        except engines.TranscriptionCancelled:
                            pass
                        except Exception as _e:
                            # Don't let tiny failure block the configured
                            # run — log and move on.
                            print("PQ tiny pass failed:",
                                  _e, file=sys.stderr)

                    if len(audios) == 1:
                        # Single source — existing fast path.
                        with tempfile.NamedTemporaryFile(
                                suffix=".wav", delete=False) as _tf:
                            mix_path = _tf.name
                        tmp_paths.append(mix_path)
                        _set_phase("extracting audio")
                        ok, err = engines.extract_window(
                            audios[0], 0, 9999, mix_path)
                        if not ok:
                            raise RuntimeError(
                                "audio extract failed: " + (err or ""))

                        if do_progressive and parallel_ok:
                            # GPU: launch tiny in a sibling thread, then
                            # run configured here in parallel.  Both
                            # passes stream into their respective
                            # buffers and the merge picks main over
                            # tiny for any timestamp main has reached.
                            _set_phase("draft + refining (parallel)", "")
                            tiny_thread = threading.Thread(
                                target=_run_tiny_pass,
                                kwargs={"mix_path_ref": mix_path},
                                daemon=True)
                            tiny_thread.start()
                            words = engines.transcribe_clip_verbatim(
                                mix_path, progress_cb=_on_seg_end,
                                cancel_event=cancel_event,
                                segment_cb=_stream_seg_cb_main)
                            # Wait for tiny — almost always finished first
                            # but guarantee both done before merge.
                            tiny_thread.join()
                        elif do_progressive:
                            # CPU: run tiny then configured sequentially.
                            _set_phase("draft (tiny)", "")
                            _run_tiny_pass(mix_path_ref=mix_path)
                            _set_phase("refining", "")
                            words = engines.transcribe_clip_verbatim(
                                mix_path, progress_cb=_on_seg_end,
                                cancel_event=cancel_event,
                                segment_cb=_stream_seg_cb_main)
                        else:
                            # Non-progressive: stream the configured
                            # pass directly so the user sees text
                            # appear as it's decoded.
                            _set_phase("transcribing", "")
                            words = engines.transcribe_clip_verbatim(
                                mix_path, progress_cb=_on_seg_end,
                                cancel_event=cancel_event,
                                segment_cb=_stream_seg_cb_main)
                    else:
                        # Multi-track — transcribe each independently so
                        # we know who said what (speaker = source file).
                        speaker_labels = session.get("speakers") or {}

                        if do_progressive and parallel_ok:
                            _set_phase("draft + refining (parallel)")
                            tiny_thread = threading.Thread(
                                target=_run_tiny_pass,
                                daemon=True)
                            tiny_thread.start()
                            words = engines.transcribe_session_per_track(
                                audios, speaker_labels=speaker_labels,
                                progress_cb=_on_seg_end,
                                cancel_event=cancel_event,
                                segment_cb=_stream_seg_cb_main)
                            tiny_thread.join()
                        elif do_progressive:
                            _set_phase("draft (tiny)")
                            _run_tiny_pass()
                            _set_phase("refining {} tracks".format(
                                len(audios)))
                            words = engines.transcribe_session_per_track(
                                audios, speaker_labels=speaker_labels,
                                progress_cb=_on_seg_end,
                                cancel_event=cancel_event,
                                segment_cb=_stream_seg_cb_main)
                        else:
                            # Non-progressive multi-track: stream
                            # configured directly.
                            _set_phase("transcribing {} tracks".format(
                                len(audios)))
                            words = engines.transcribe_session_per_track(
                                audios, speaker_labels=speaker_labels,
                                progress_cb=_on_seg_end,
                                cancel_event=cancel_event,
                                segment_cb=_stream_seg_cb_main)
                        # Build a mix purely for the playback cache so
                        # the user can hear both speakers when auditioning
                        # transcript regions.
                        _set_phase("building playback mix")
                        mix_path = engines.mix_for_transcript(audios)
                        tmp_paths.append(mix_path)

                    # ── Merge with prior user edits ──────────────────
                    # Two scenarios produce a merge:
                    #
                    #  A) Progressive run.  Tiny pre-pass populated
                    #     session['transcript'] with _src='tiny' words;
                    #     user may have edited some of them (now
                    #     _src='user') while the configured model ran.
                    #     Merge against the CURRENT session transcript
                    #     so we pick up those mid-flight edits.
                    #
                    #  B) Re-transcribe.  No tiny pass; the old
                    #     transcript carried _src='user' edits from
                    #     prior sessions.  Merge against the
                    #     pre_run_transcript snapshot we captured
                    #     before the worker started.
                    if do_progressive and session.get("transcript"):
                        words = PqWorkflowMixin._pq_merge_refine(
                            session["transcript"], words)
                    elif pre_run_user_edits:
                        words = PqWorkflowMixin._pq_merge_refine(
                            pre_run_transcript, words)

                    # Persist the mix (or single extracted WAV) as the
                    # session's playback cache.
                    cache_dest = self._pq_audio_cache_path(session)
                    if cache_dest and os.path.isfile(mix_path):
                        try:
                            import shutil as _sh
                            os.makedirs(os.path.dirname(cache_dest),
                                        exist_ok=True)
                            _sh.copyfile(mix_path, cache_dest)
                            session["audio_cache"] = cache_dest
                        except Exception:
                            pass
                finally:
                    for tp in tmp_paths:
                        try: os.unlink(tp)
                        except Exception: pass

                # Late safety net: if cancel was requested but the
                # decode somehow completed before the next segment
                # boundary was checked, still discard the result.  The
                # primary cancellation path is the TranscriptionCancelled
                # exception raised from within the engine — see the
                # exception handler below.
                if session.get("_cancel_requested"):
                    session.pop("_cancel_requested", None)
                    raise engines.TranscriptionCancelled()

                # Defensive final-merge: edits committed in the brief
                # window between the configured-pass merge and this
                # final assignment would otherwise be silently lost.
                # Re-merge against the LATEST session["transcript"]
                # snapshot so any user edits made very late in the
                # run are preserved.  Idempotent when nothing changed.
                _latest = session.get("transcript") or []
                _has_late_user_words = any(
                    w.get("_src") == "user" for w in _latest)
                if _has_late_user_words:
                    words = PqWorkflowMixin._pq_merge_refine(_latest, words)

                session["transcript"] = list(words or [])
                self._pq_save_session_file(session)

                elapsed = round(time.perf_counter() - t0, 1)
                def _done():
                    prog = session.get("_progress")
                    if prog is not None:
                        prog["active"] = False
                    # If the user is still looking at this session, refresh
                    # its view.  Otherwise update the project row in place
                    # (if it's on screen) — no full re-render, no twitch.
                    if getattr(self, "_pq_current_session", None) is session:
                        # Preserve cursor / selection / scroll across
                        # the final render — the user may have been
                        # reading partway through the streaming output
                        # and shouldn't be yanked back to the top when
                        # the run completes.
                        _final_state = self._pq_capture_text_state()
                        self._pq_render_transcript_text(session)
                        self._pq_restore_text_state(_final_state)
                        if (btn := getattr(self, "_pq_retx_btn", None)):
                            btn.config(text="⟳ RE-TRANSCRIBE", fg=TEXT)
                        if (lbl := getattr(self, "_pq_status_lbl", None)):
                            lbl.config(text="✓ done in {}s".format(elapsed),
                                        fg=SUCCESS)
                            self.after(2500,
                                       lambda: self._pq_update_status_lbl(session))
                    elif (getattr(self, "_pq_current_session", None) is None
                          and getattr(self, "_pq_project", None) is not None):
                        self._pq_update_project_row(session)
                self._ui(_done)
            except Exception as e:
                err = str(e)
                is_mem = isinstance(e, MemoryError) or "allocate" in err.lower()
                # User-requested cancel: detected either by the dedicated
                # TranscriptionCancelled exception raised mid-decode from
                # the engine, or by the late-safety-net check above.
                is_cancel = (
                    isinstance(e, engines.TranscriptionCancelled)
                    or "cancelled by user" in err.lower()
                    or "cancelled" in err.lower())
                def _err():
                    prog = session.get("_progress")
                    if prog is not None:
                        prog["active"] = False
                    if getattr(self, "_pq_current_session", None) is session:
                        if (btn := getattr(self, "_pq_retx_btn", None)):
                            btn.config(text="⟳ TRANSCRIBE", fg=TEXT)
                        if (lbl := getattr(self, "_pq_status_lbl", None)):
                            lbl.config(
                                text=("cancelled" if is_cancel else "error"),
                                fg=(SUB if is_cancel else ERR))
                        # Preserve cursor / selection / scroll on the
                        # error-path render too — cancel mid-read
                        # shouldn't jump the user away from where they
                        # were looking.
                        _err_state = self._pq_capture_text_state()
                        self._pq_render_transcript_text(session)
                        self._pq_restore_text_state(_err_state)
                    elif getattr(self, "_pq_current_session", None) is None:
                        self._pq_update_project_row(session)
                    if is_mem:
                        # MemoryError mid-transcribe — almost always a
                        # Windows commit-charge exhaustion.  Pull live
                        # numbers so the user sees what's tight.
                        phys = self._available_ram_bytes()
                        page = self._available_pagefile_bytes()
                        diag = [err, ""]
                        if phys is not None:
                            diag.append("Physical RAM free:  {:.1f} GB".format(
                                phys / (1024 ** 3)))
                        if page is not None:
                            diag.append("Commit headroom:    {:.1f} GB".format(
                                page / (1024 ** 3)))
                            if page < 2 * (1024 ** 3):
                                diag.append("")
                                diag.append("Commit charge is near the limit — "
                                            "Windows refuses allocations even "
                                            "when physical RAM is free.")
                        diag.append("")
                        diag.append("Try:")
                        diag.append("  •  Quit Pro Tools / Premiere / browsers")
                        diag.append("  •  If commit headroom is low, a Windows "
                                    "restart is the fastest fix")
                        diag.append("  •  Use a shorter source clip or split "
                                    "the audio")
                        messagebox.showerror(
                            "Out of Memory ({})".format(
                                session.get("token", "?")),
                            "\n".join(diag))
                    elif is_cancel:
                        # No popup — the user already saw the Settings
                        # dialog and chose to cancel.  Status lbl above
                        # already reads "cancelled".
                        pass
                    else:
                        messagebox.showerror(
                            "Transcription failed ({})".format(
                                session.get("token", "?")), err)
                self._ui(_err)
            finally:
                self._pq_apply_priority(low=False, prev=prev_priority)
                # Clear cancel state regardless of how the worker
                # exited; the next TRANSCRIBE click will recreate it.
                session.pop("_cancel_event", None)
                session.pop("_cancel_requested", None)

        threading.Thread(target=_worker, daemon=True).start()

    def _pq_render_progress_placeholder(self):
        """Show an animated 'transcription in progress' message in the
        transcript pane while a session is decoding AND has no words yet.

        Critical: once a transcript exists (e.g. the tiny preview during
        a progressive run), DO NOT overwrite it — the user is already
        reading and possibly editing.  The placeholder is only useful
        when the pane is otherwise empty.  Status / refining state is
        communicated via the status label instead.
        """
        tx      = getattr(self, "_pq_tx_text", None)
        session = getattr(self, "_pq_current_session", None)
        if not tx or session is None:
            return
        prog = session.get("_progress") or {}
        if not prog.get("active"):
            return
        # Don't clobber an existing transcript (tiny preview or full).
        if session.get("transcript"):
            return
        try:
            if not tx.winfo_exists():
                return
            dots = "." * (1 + (prog.get("elapsed", 0) % 3))
            msg = ("  {}{}{}\n\n  (transcription continues in the background "
                   "— feel free to navigate away or transcribe other "
                   "sessions; results save automatically.)").format(
                       prog.get("phase", "working"),
                       prog.get("extra", ""), dots)
            tx.configure(state="normal")
            tx.delete("1.0", "end")
            tx.insert("1.0", msg)
            self._pq_word_index = []
        except tk.TclError:
            return

    def _pq_animate_progress(self):
        """Tick the dots animation every 600 ms while the currently-visible
        session is transcribing.  Stops automatically when active=False or
        when the user navigates away."""
        session = getattr(self, "_pq_current_session", None)
        if session is None:
            return
        prog = session.get("_progress") or {}
        if not prog.get("active"):
            return
        prog["elapsed"] = prog.get("elapsed", 0) + 1
        lbl = getattr(self, "_pq_status_lbl", None)
        if lbl:
            try:
                if lbl.winfo_exists():
                    phase = prog.get("phase", "working")
                    extra = prog.get("extra", "")
                    lbl.config(text="{}{}{}".format(
                        phase, extra, "." * (1 + (prog["elapsed"] % 3))),
                        fg=ACCENT)
            except tk.TclError:
                pass
        self._pq_render_progress_placeholder()
        self.after(600, self._pq_animate_progress)

    def _pq_apply_priority(self, low, prev=None):
        """Lower (or restore) the host process priority.

        Returns the previous priority handle so it can be restored from a
        finally clause.  Failures are swallowed — priority is best-effort.
        """
        try:
            if sys.platform == "win32":
                import ctypes
                k32 = ctypes.windll.kernel32
                BELOW_NORMAL = 0x00004000
                NORMAL       = 0x00000020
                handle = k32.GetCurrentProcess()
                if prev is not None:
                    k32.SetPriorityClass(handle, prev)
                    return prev
                cur = k32.GetPriorityClass(handle)
                target = BELOW_NORMAL if low else NORMAL
                if cur != target:
                    k32.SetPriorityClass(handle, target)
                return cur
            elif hasattr(os, "nice"):
                # POSIX: relative niceness; we apply +10 when entering low
                # mode, then -10 when restoring.
                if prev is not None:
                    try:
                        os.nice(-int(prev))
                    except Exception:
                        pass
                    return None
                if low:
                    try:
                        os.nice(10)
                        return 10
                    except Exception:
                        return None
                return None
        except Exception:
            return None
        return None

    # ── Search ────────────────────────────────────────────────────────────
    def _pq_search_apply(self):
        """Recompute matches for the current query and refresh highlights.

        Wrapped in a top-level try/except so a malformed query (or any
        Tk quirk) can never crash the app — search just reports the
        error and bails.
        """
        tx = getattr(self, "_pq_tx_text", None)
        var = getattr(self, "_pq_search_var", None)
        lbl = getattr(self, "_pq_search_count_lbl", None)
        if tx is None or var is None:
            return
        try:
            tx.tag_remove("pq_match",         "1.0", "end")
            tx.tag_remove("pq_match_current", "1.0", "end")
        except tk.TclError:
            return
        self._pq_search_hits = []
        self._pq_search_cur  = -1

        term = var.get().strip()
        if not term:
            if lbl: lbl.config(text="", fg=SUB)
            return

        # A single character matches essentially every position in the
        # transcript, so the loop below instantly saturates its 5000-hit
        # cap — measured at 220 ms to 1.17 s of frozen UI on a 34k-word
        # transcript, on the very first keystroke.  Nothing useful is
        # shown at that point anyway, so wait for a second character.
        if len(term) < 2:
            if lbl: lbl.config(text="keep typing…", fg=SUB)
            return

        try:
            idx       = "1.0"
            term_len  = len(term)
            # One reusable Tk variable for the life of the App.  This
            # used to be `tk.IntVar(self)` per call — every keystroke
            # registered a fresh variable in the Tcl interpreter that
            # was never explicitly freed.
            count_var = getattr(self, "_pq_search_count_var", None)
            if count_var is None:
                count_var = self._pq_search_count_var = tk.IntVar(self)
            _spans = []
            # Hard upper bound on iterations so we cannot pathologically
            # loop on a degenerate query — 5000 hits is far more than
            # any user would ever need to see.
            for _ in range(5000):
                pos = tx.search(term, idx, stopindex="end",
                                 nocase=True, count=count_var)
                if not pos:
                    break
                # `count_var` reports the actual match length (handy if
                # we add regex mode later); for plain text it equals
                # `term_len` but reading the var is the safer path.
                hit_len = count_var.get() or term_len
                end = "{}+{}c".format(pos, hit_len)
                _spans.append(pos)
                _spans.append(end)
                self._pq_search_hits.append((pos, end))
                # Always advance — if the search returns the same
                # position twice (shouldn't, but be paranoid) we still
                # break the loop.
                new_idx = end
                if new_idx == idx:
                    break
                idx = new_idx
            # One tag_add for every hit instead of one per hit.
            if _spans:
                tx.tag_add("pq_match", *_spans)
        except (tk.TclError, ValueError) as exc:
            if lbl:
                lbl.config(text="search error: {}".format(str(exc)[:32]),
                           fg=ERR)
            return

        n = len(self._pq_search_hits)
        if lbl:
            lbl.config(
                text=("{} match{}".format(n, "es" if n != 1 else "")
                      if n else "no matches"),
                fg=SUCCESS if n else WARN)
        if n:
            self._pq_search_cur = 0
            self._pq_search_focus_current()

    def _pq_search_step(self, delta):
        n = len(getattr(self, "_pq_search_hits", []))
        if not n:
            # Re-run the search if user hits Enter on a fresh query
            self._pq_search_apply()
            return
        cur = (self._pq_search_cur + delta) % n
        self._pq_search_cur = cur
        self._pq_search_focus_current()

    def _pq_search_focus_current(self):
        tx = getattr(self, "_pq_tx_text", None)
        hits = getattr(self, "_pq_search_hits", [])
        cur  = getattr(self, "_pq_search_cur", -1)
        lbl  = getattr(self, "_pq_search_count_lbl", None)
        if tx is None or not hits or cur < 0:
            return
        try:
            tx.tag_remove("pq_match_current", "1.0", "end")
            start, end = hits[cur]
            tx.tag_add("pq_match_current", start, end)
            tx.see(start)
        except tk.TclError:
            return
        if lbl:
            lbl.config(text="{} of {}".format(cur + 1, len(hits)),
                       fg=SUCCESS)

    def _pq_search_clear(self):
        var = getattr(self, "_pq_search_var", None)
        if var is not None:
            var.set("")

    # ── Audio playback ────────────────────────────────────────────────────
    def _pq_audio_cache_path(self, session):
        """Return the path where the playback cache for this session lives."""
        media = session.get("media") or []
        token = session.get("token") or "SESSION"
        if not media:
            return None
        return os.path.join(os.path.dirname(os.path.abspath(media[0])),
                            "{}.pb_audio.wav".format(token))

    def _pq_play_selection(self):
        """Extract the time range under the current selection (or word at
        cursor) from the persisted session audio and play it via winsound."""
        tx      = getattr(self, "_pq_tx_text", None)
        session = getattr(self, "_pq_current_session", None)
        if tx is None or session is None:
            return

        # If currently playing, the play button doubles as STOP.
        if getattr(self, "_pq_playing", False):
            self._pq_stop_playback()
            return

        # Resolve selected words → time range
        word_idx = getattr(self, "_pq_word_index", []) or []
        if not word_idx:
            messagebox.showinfo("Nothing to play",
                "Transcribe this session first.")
            return

        try:
            sel_first = tx.index("sel.first")
            sel_last  = tx.index("sel.last")
        except tk.TclError:
            sel_first = sel_last = None

        play_to_end = False
        if sel_first and sel_last:
            c0 = _tx_count_chars(tx, "1.0", sel_first)
            c1 = _tx_count_chars(tx, "1.0", sel_last)
            picked = [w for (s, e, w) in word_idx if e > c0 and s < c1]
        else:
            # No selection — play indefinitely from the cursor word to the
            # end of the audio (Shift+Space toggles stop).
            c0 = _tx_count_chars(tx, "1.0", tx.index("insert"))
            picked = [w for (s, e, w) in word_idx if s <= c0 < e]
            if not picked:
                # Cursor is past the last word — start at the next word
                picked = [w for (s, e, w) in word_idx if s >= c0][:1]
            if not picked and word_idx:
                picked = [word_idx[0][2]]
            play_to_end = True

        if not picked:
            messagebox.showinfo("Nothing to play",
                "Click inside a word or select a passage first.")
            return

        in_s = float(picked[0].get("start", 0))
        if play_to_end:
            # Use the cached audio's full duration as the end so we play
            # to the very end without arbitrary truncation.
            out_s = None   # signals open-ended below
        else:
            out_s = float(picked[-1].get("end", in_s))
            if out_s <= in_s:
                out_s = in_s + 5.0

        # Prefer the persisted session audio (matches what was transcribed);
        # fall back to the first source file.
        cache = session.get("audio_cache") or self._pq_audio_cache_path(session)
        if cache and not os.path.isfile(cache):
            cache = None
        src = cache or (session.get("media") or [None])[0]
        if not src or not os.path.isfile(src):
            messagebox.showerror("No audio",
                "Cannot find audio for this session.")
            return

        # Resolve the duration to extract.  For open-ended playback (no
        # selection), probe the audio file once to get its full length.
        if out_s is None:
            try:
                total = engines.get_media_duration(src) or (in_s + 7200)
            except Exception:
                total = in_s + 7200
            duration_s = max(1.0, total - in_s)
        else:
            duration_s = max(0.5, out_s - in_s)

        # Extract → temp WAV → play.  Create the temp file BEFORE the
        # try, so the cleanup branch always knows the path to unlink —
        # the previous code created tf inside the try and the failure
        # branch returned without removing it, leaking a zero-byte WAV
        # into the OS temp dir on every failed playback.
        tf = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tf.close()
        try:
            engines.extract_audio_segment(
                src, in_s, duration_s, tf.name,
                sample_rate=22050)
        except Exception as e:
            try: os.unlink(tf.name)
            except Exception: pass
            messagebox.showerror("Playback failed", str(e))
            return

        # Clean up any prior temp first
        if (prev := getattr(self, "_pq_play_tmp", None)):
            try: os.unlink(prev)
            except Exception: pass
        self._pq_play_tmp = tf.name

        if sys.platform == "win32":
            try:
                import winsound
                # Anchor the wall-clock instant we tell winsound to
                # start, so the highlight tick can compute the
                # playhead position from elapsed time + audio offset.
                self._pq_play_clock_start = time.perf_counter()
                self._pq_play_audio_in_s  = in_s
                winsound.PlaySound(tf.name,
                                   winsound.SND_FILENAME | winsound.SND_ASYNC)
                self._pq_playing = True
                if (b := getattr(self, "_pq_play_btn", None)):
                    b.config(text="■ STOP")
                # Kick off the karaoke highlight loop
                self._pq_playback_highlight_tick()
                # Schedule auto-restore of button text once playback ends
                dur_ms = int(max(500, duration_s * 1000) + 300)
                self.after(dur_ms, self._pq_stop_playback)
            except Exception as e:
                messagebox.showerror("Playback failed", str(e))
        else:
            messagebox.showinfo("Playback unsupported",
                "Audio playback is only wired up for Windows in this build.")

    def _pq_playback_highlight_tick(self):
        """While playback is active, update the 'body_playing'
        highlight to whichever word the playhead is currently inside
        and scroll it into view.  Polls itself every ~60 ms — fast
        enough to feel smooth, slow enough not to thrash Tk."""
        if not getattr(self, "_pq_playing", False):
            return
        tx = getattr(self, "_pq_tx_text", None)
        if tx is None:
            return
        word_idx = getattr(self, "_pq_word_index", []) or []
        start_clock = getattr(self, "_pq_play_clock_start", None)
        audio_in_s  = getattr(self, "_pq_play_audio_in_s",  None)
        if (word_idx and start_clock is not None
                and audio_in_s is not None):
            try:
                playhead = audio_in_s + (
                    time.perf_counter() - start_clock)
                # Bisect a time-sorted index instead of walking every
                # word.  The old comment here claimed _pq_word_index
                # "typically has a few hundred entries" — a real 3.75-hour
                # session has 20-34k, so this was 5-9 ms of linear scan
                # 16 times a second for the whole playback.
                time_idx, line_starts = self._pq_playback_caches()
                cur = None
                if time_idx:
                    j = bisect.bisect_right(
                        time_idx, playhead, key=lambda it: it[0]) - 1
                    if 0 <= j < len(time_idx):
                        ws, we, s_abs, e_abs = time_idx[j]
                        if ws <= playhead < we:
                            cur = (s_abs, e_abs)
                tx.tag_remove("body_playing", "1.0", "end")
                if cur is not None:
                    # Literal "line.column" indices — "1.0 + Nc" makes Tk
                    # walk forward N chars from the buffer origin, and
                    # this ran three times per tick.
                    _i0 = self._pq_abs_index(cur[0], line_starts)
                    _i1 = self._pq_abs_index(cur[1], line_starts)
                    tx.tag_add("body_playing", _i0, _i1)
                    # Keep the playing word on screen without
                    # snapping the user back to it on every tick if
                    # they intentionally scrolled away — tx.see only
                    # scrolls if the index is offscreen.
                    tx.see(_i0)
            except tk.TclError:
                pass
        # Schedule next tick.  Cancel handle stored so stop can kill
        # the loop immediately rather than waiting for the next poll.
        self._pq_play_hi_after = self.after(
            60, self._pq_playback_highlight_tick)

    def _pq_stop_playback(self):
        if not getattr(self, "_pq_playing", False):
            return
        self._pq_playing = False
        # Cancel the highlight loop and clear any lingering tag.
        if (after_id := getattr(self, "_pq_play_hi_after", None)):
            try: self.after_cancel(after_id)
            except Exception: pass
            self._pq_play_hi_after = None
        tx = getattr(self, "_pq_tx_text", None)
        if tx is not None:
            try:
                tx.tag_remove("body_playing", "1.0", "end")
            except tk.TclError:
                pass
        if sys.platform == "win32":
            try:
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
        if (b := getattr(self, "_pq_play_btn", None)):
            try:
                b.config(text="▶ PLAY SELECTION")
            except tk.TclError:
                pass

    def _pq_copy_as_pull(self, event=None):
        """Convert the current transcript selection into a @PULL block."""
        tx      = getattr(self, "_pq_tx_text", None)
        session = getattr(self, "_pq_current_session", None)
        if tx is None or session is None:
            return None
        try:
            sel_first = tx.index("sel.first")
            sel_last  = tx.index("sel.last")
        except tk.TclError:
            return None   # no selection — let default copy run

        # Convert tk indices to character offsets from "1.0".
        c0 = _tx_count_chars(tx, "1.0", sel_first)
        c1 = _tx_count_chars(tx, "1.0", sel_last)

        word_idx = getattr(self, "_pq_word_index", []) or []
        if not word_idx:
            return "break"

        # All words whose character range overlaps the selection.
        selected = [w for (s_, e_, w) in word_idx if e_ > c0 and s_ < c1]
        if not selected:
            return "break"

        in_s  = float(selected[0].get("start", 0))
        out_s = float(selected[-1].get("end", in_s))
        # PostBridge timecodes are integer seconds (the parser rejects a
        # fractional part).  Round to the NEAREST second instead of
        # truncating: the old secs_tc(...).split(".")[0] floored every
        # value, which pushed the IN point EARLIER than the spoken word —
        # up to a full second (reported) — and clipped the tail at OUT.
        # Nearest rounding removes that systematic early bias on both edges.
        in_tc  = secs_tc(round(in_s)).split(".")[0]
        out_tc = secs_tc(round(out_s)).split(".")[0]

        # Pull body verbatim from the rendered transcript so what the
        # user copies matches what they see — speaker labels, paragraph
        # breaks, line breaks, all preserved.  We expand the selection
        # to whole words at both edges so a partial-word click doesn't
        # truncate mid-token.
        word_idx_map = {id(w): (s, e) for (s, e, w) in word_idx}
        first_s = word_idx_map.get(id(selected[0]),  (None, None))[0]
        last_e  = word_idx_map.get(id(selected[-1]), (None, None))[1]

        if first_s is not None and last_e is not None:
            try:
                # Use the selection bounds expanded to whole words so a
                # partial-word click doesn't truncate mid-token.  The old
                # code also snapped start back via "linestart", intending
                # to grab a leading speaker label — but Tk's "linestart"
                # walks to the LOGICAL line start (the previous newline),
                # and rendered paragraphs have no internal newlines under
                # wrap="word", so the snap captured the ENTIRE paragraph
                # whenever the user selected a sentence inside one
                # (reported).  Drop it: the [TOKEN HH:MM:SS-HH:MM:SS]
                # header already encodes the speaker, so the body
                # doesn't need a leading "BRYAN1:" prefix.
                start_idx = "1.0+{}c".format(first_s)
                end_idx   = "1.0+{}c".format(last_e)
                body = tx.get(start_idx, end_idx)
            except tk.TclError:
                body = tx.get(sel_first, sel_last)
        else:
            body = tx.get(sel_first, sel_last)

        # Tidy: rstrip every line, strip outer whitespace.  Internal
        # blank lines (paragraph breaks) and speaker-label line breaks
        # are preserved.
        body = "\n".join(line.rstrip() for line in body.split("\n")).strip()

        block = "[{} {}-{}]\n{}\n".format(
            session.get("token", "?"),
            in_tc, out_tc,
            body)
        self.clipboard_clear()
        self.clipboard_append(block)
        self.update()

        # Brief flash on the status label
        lbl = getattr(self, "_pq_status_lbl", None)
        if lbl:
            prev_text = lbl.cget("text")
            prev_fg   = lbl.cget("fg")
            lbl.config(
                text="✓ @PULL copied  [{}–{}]".format(in_tc, out_tc),
                fg=SUCCESS)
            self.after(2000,
                       lambda: lbl.config(text=prev_text, fg=prev_fg))
        return "break"

