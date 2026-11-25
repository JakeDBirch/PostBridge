# -*- coding: utf-8 -*-
"""Episode screens: choose the project folder once, review what discovery
found, and launch each workflow already linked.

Mixed into main.App beside the AAF and Pull Quotes mixins; relies on the
App helpers (_btn, _section, _scroll_frame, _clear, _ui, _mark_saved) and
on episode.py for everything that is not a widget.
"""
import json
import os
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

from config import *
from config import _SANS
from gui_components import _FlatDropdown
import episode as EP
import gdocs
import script_normalize


class EpisodeWorkflowMixin:

    # ── Entry points ─────────────────────────────────────────────────────

    def _ep_enter(self):
        """Home card: continue the open episode, else choose a folder."""
        self._ep_amend_stage()
        if getattr(self, "_ep", None):
            self.workflow = "episode"
            self._ep_view()
        else:
            self._ep_start()

    def _ep_start(self):
        root = filedialog.askdirectory(title="Choose the episode's project folder")
        if not root:
            if not getattr(self, "_ep", None):
                self.workflow = None
            return
        existing = EP.find_episode_file(root)
        if existing:
            self._ep_open_file(existing)
            return
        self._ep = EP.new_episode(root)
        self.workflow = "episode"
        self._ep_scan()

    def _ep_open_file(self, path):
        try:
            self._ep = EP.load(path)
        except Exception as e:
            messagebox.showerror("Open episode failed", str(e))
            return
        # An older episode's stage files were just read in: the episode is
        # the one file from here on.  They stay on disk, untouched.
        if self._ep.get("_absorbed"):
            try:
                EP.save(self._ep)
            except Exception as e:
                messagebox.showerror("Save episode failed", str(e))
        self.workflow = "episode"
        self._ep_view()
        self._mark_saved()

    def _ep_save(self, prompt=False):
        ep = getattr(self, "_ep", None)
        if not ep:
            return False
        self._ep_save_flush()
        path = ep.get("file_path")
        if not path and not prompt:
            # The episode file lives at the project root; no dialog needed.
            path = EP.episode_file_for(ep["root"], ep["title"])
        if prompt:
            path = filedialog.asksaveasfilename(
                title="Save episode", initialdir=ep["root"],
                initialfile=os.path.basename(EP.episode_file_for(ep["root"], ep["title"])),
                defaultextension=EP.EPISODE_EXT,
                filetypes=[("PostBridge episode", "*" + EP.EPISODE_EXT), ("All", "*.*")])
            if not path:
                return False
        try:
            EP.save(ep, path)
        except Exception as e:
            messagebox.showerror("Save episode failed", str(e))
            return False
        # Saved under a new name: Pull Quotes follows the episode there.
        proj = getattr(self, "_pq_project", None)
        if prompt and proj and proj.get("episode_file"):
            proj["episode_file"] = ep["file_path"]
        # The baseline belongs to the active workflow: saving the episode
        # from under a stage (closing from it) must not mark that stage's
        # own unsaved work clean.
        if self.workflow == "episode":
            self._mark_saved()
        return True

    @staticmethod
    def _ep_comparable(data):
        """What decides whether an episode is saved: everything save()
        writes but its root, which load() takes from where the file now is
        (a moved folder is not an unsaved change), and its stages, which only
        change by being written (_ep_store_stage)."""
        return {k: v for k, v in data.items()
                if k not in ("root", "file_path", "stages")}

    def _ep_saved_form(self):
        """The open episode as it would be saved, for _state_signature."""
        return self._ep_comparable(EP.disk_form(self._ep))

    def _ep_file_signature(self):
        """_state_signature of the open episode's file as it is on disk, or
        None when it has none yet."""
        ep = getattr(self, "_ep", None)
        path = ep.get("file_path") if ep else None
        if not path:
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        return self._signature_of(self._ep_comparable(data))

    def _ep_unsaved(self):
        """True when the open episode differs from its file -- or has never
        been saved -- whatever screen is showing."""
        if not getattr(self, "_ep", None):
            return False
        return self._signature_of(self._ep_saved_form()) != self._ep_file_signature()

    # ── Stages save into the episode ─────────────────────────────────────

    def _ep_holds(self, ep, key, path):
        """*path* is the episode's script or AAF: linked, or in its folder.
        One from anywhere else is unrelated work, with a file of its own."""
        return bool(path) and (self._ep_owns(ep, key, path) or EP.inside(ep, path))

    def _ep_stage_showing(self):
        """The stage of the open episode on screen (EP.STAGE_*), or None when
        the screen is the episode's own, or work that isn't this episode's."""
        ep = getattr(self, "_ep", None)
        if not ep:
            return None
        wf = getattr(self, "workflow", None)
        if wf == "pull_quotes":
            f = (getattr(self, "_pq_project", None) or {}).get("episode_file")
            mine = ep.get("file_path")
            if f and mine and os.path.normcase(f) == os.path.normcase(mine):
                return EP.STAGE_PQ
            return None
        if wf == "aaf_xml":
            aaf = getattr(self, "_aaf_path", None)
            return EP.STAGE_AAF if self._ep_holds(ep, "aaf", aaf) else None
        if wf in ("script_session", "script_aaf", "script_xml"):
            script = getattr(self, "_script_path", None)
            return EP.STAGE_S2S if self._ep_holds(ep, "script", script) else None
        return None

    def _ep_find_for_stage(self):
        """No episode open, and a first save of a script or AAF about to ask
        where to put it: when the project it sits in has an episode, that is
        where it goes."""
        if getattr(self, "_ep", None):
            return
        wf = getattr(self, "workflow", None)
        if wf == "aaf_xml":
            path = None if getattr(self, "_aaf_saved_path", None) \
                else getattr(self, "_aaf_path", None)
        elif wf in ("script_session", "script_aaf", "script_xml"):
            path = None if getattr(self, "_current_session_file", None) \
                else getattr(self, "_script_path", None)
        else:
            path = None
        found = EP.find_episode_above(path) if path else None
        if found:
            try:
                self._ep = EP.load(found)
            except Exception:
                self._ep = None

    def _ep_store_stage(self, quiet=False):
        """Amend the episode file with the stage on screen -- the one place a
        stage launched from an episode saves to.  True when it was written."""
        stage = self._ep_stage_showing()
        if stage is None:
            return False
        ep = self._ep
        try:
            if stage == EP.STAGE_PQ:
                return bool(self._pq_save_episode_project(prompt_path=False))
            if stage == EP.STAGE_AAF:
                data = self._aaf_build_setup_data()
                ep.setdefault("links", {})["aaf"] = EP.rel(ep, self._aaf_path)
                self._ep_absorb_aaf(data)
            else:
                data = self._build_full_session_data()
                ep.setdefault("links", {})["script"] = EP.rel(ep, self._script_path)
                self._ep_absorb_s2s()
            EP.set_stage(ep, stage, data)
            EP.save(ep)
        except Exception as e:
            if not quiet:
                messagebox.showerror("Save episode failed", str(e))
            return False
        self._mark_saved()
        return True

    def _ep_amend_stage(self):
        """Stepping on from a stage (back to the episode, to Home, a reconcile
        landing): its work goes into the episode without being asked for."""
        try:
            if self._ep_stage_showing() and self._is_dirty():
                self._ep_store_stage(quiet=True)
        except Exception:
            pass

    def _ep_embed_session(self, session, payload):
        """Pull Quotes saved an interview session: it goes into the episode,
        and the file follows shortly -- sessions save on every edit, and a
        transcript pass saves from its worker thread."""
        if threading.current_thread() is not threading.main_thread():
            self._ui(lambda: self._ep_embed_session(session, payload))
            return
        proj = getattr(self, "_pq_project", None) or {}
        ep = self._ep_bound(session.get("_ep_file") or proj.get("episode_file"))
        if not ep:
            return
        EP.pq_sessions(ep)[payload["id"]] = payload
        session.pop("_file_path", None)   # an added file now lives in here
        try:
            self._ep_register_session(session)
        except Exception:
            pass
        self._ep_save_pending = ep
        pending = getattr(self, "_ep_save_after", None)
        if pending is not None:
            self.after_cancel(pending)
        self._ep_save_after = self.after(800, self._ep_save_flush)

    def _ep_save_flush(self):
        """Write a pending session save now.  Safe when nothing is pending."""
        pending = getattr(self, "_ep_save_after", None)
        if pending is not None:
            try:
                self.after_cancel(pending)
            except Exception:
                pass
            self._ep_save_after = None
        ep = getattr(self, "_ep_save_pending", None)
        self._ep_save_pending = None
        if ep:
            try:
                EP.save(ep)
            except Exception as e:
                messagebox.showerror("Save episode failed", str(e))

    # ── Discovery ────────────────────────────────────────────────────────

    def _ep_scan(self):
        ep = self._ep
        self._clear()
        self._section("EPISODE — {}".format(ep["title"]))
        tk.Label(self.body, text=ep["root"], font=FB, bg=BG, fg=SUB).pack(anchor="w")
        tk.Frame(self.body, bg=BG, height=20).pack()
        status = tk.Label(self.body, text="Looking through the folder…",
                          font=FBT, bg=BG, fg=TEXT)
        status.pack(anchor="w")
        detail = tk.Label(self.body, text="", font=FB, bg=BG, fg=SUB)
        detail.pack(anchor="w", pady=(4, 0))

        def _progress(done, total, name):
            self._ui(lambda: detail.config(
                text="Measuring {} of {}: {}".format(done, total, name)))

        def _back():
            # Only if this screen is still up: the user may have moved on while
            # it looked, and this would wipe wherever they are now.
            if self._ep is ep and status.winfo_exists():
                self._ep_view()

        def _run():
            try:
                res = EP.scan(ep["root"], progress=_progress,
                              excluded=ep.get("excluded"))
            except Exception as e:
                msg = str(e)    # `e` is unbound by the time the lambda runs
                self._ui(lambda: messagebox.showerror("Scan failed", msg))
                self._ui(_back)
                return
            def _done():
                EP.apply_scan(ep, res)
                _back()
            self._ui(_done)

        threading.Thread(target=_run, daemon=True).start()

    # ── Review / dashboard ───────────────────────────────────────────────

    def _ep_view(self):
        ep = self._ep
        self._clear()
        # This screen is the episode workflow: the header SAVE and Ctrl+S
        # (both _quick_save) save the episode from here.  It used to rebind
        # Ctrl+S to the episode for good, so every stage opened afterwards
        # saved the episode on Ctrl+S instead of its own file.
        self.workflow = "episode"
        # Unsaved means different from the file.  The stages write into the
        # episode as they save, so the file -- not the last Save pressed
        # here -- is what an edit is measured against.
        self._saved_signature = self._ep_file_signature()

        # Header: title, root, actions
        hdr = tk.Frame(self.body, bg=BG)
        hdr.pack(fill="x", pady=(20, 6))
        self._ep_review_mark = hdr          # the review is up while this is
        tk.Frame(hdr, bg=ACCENT, width=3, height=20).pack(side="left", padx=(0, 10))
        tk.Label(hdr, text="EPISODE", font=FL, bg=BG, fg=TEXT).pack(side="left")
        self._ep_title_var = tk.StringVar(value=ep["title"])
        ent = tk.Entry(hdr, textvariable=self._ep_title_var, font=FBT, bg=SURF2,
                       fg=TEXT, insertbackground=TEXT, relief="flat", width=40)
        ent.pack(side="left", padx=(12, 0))
        self._ep_title_var.trace_add(
            "write", lambda *_: ep.__setitem__("title", self._ep_title_var.get().strip() or ep["title"]))
        save_btn = [None]
        save_btn[0] = self._btn(hdr, "SAVE EPISODE",
                                lambda: self._quick_save(save_btn),
                                small=True, color=ACCENT)
        save_btn[0].pack(side="right")
        self._btn(hdr, "↻ RESCAN FOLDER", self._ep_scan, small=True
                  ).pack(side="right", padx=(0, 6))
        self._btn(hdr, "CHANGE FOLDER…", self._ep_start, small=True
                  ).pack(side="right", padx=(0, 6))
        tk.Label(self.body, text=ep["root"], font=FB, bg=BG, fg=SUB).pack(anchor="w")
        sm = EP.summary(ep)
        tk.Label(self.body,
                 text="{} source{}  ·  {} recording{}  ·  {} camera file{}  "
                      "·  {} file{} put aside".format(
                          sm["sources"], "" if sm["sources"] == 1 else "s",
                          sm["audio"], "" if sm["audio"] == 1 else "s",
                          sm["video"], "" if sm["video"] == 1 else "s",
                          sm["aside"], "" if sm["aside"] == 1 else "s"),
                 font=FB, bg=BG, fg=SUB).pack(anchor="w", pady=(2, 8))

        sf = self._scroll_frame(self.body)
        self._ep_render_stages(sf)
        self._ep_render_sources(sf)
        self._ep_render_unassigned(sf)
        self._ep_render_aside(sf)

    # -- cards ------------------------------------------------------------

    def _ep_card(self, parent, title, note=None):
        card = tk.Frame(parent, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x", pady=(0, 8), padx=2)
        h = tk.Frame(card, bg=SURF)
        h.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(h, text=title, font=FL, bg=SURF, fg=ACCENT).pack(side="left")
        if note:
            tk.Label(h, text="  " + note, font=FB, bg=SURF, fg=SUB).pack(side="left")
        body = tk.Frame(card, bg=SURF)
        body.pack(fill="x", padx=12, pady=(0, 10))
        return body

    def _ep_render_stages(self, parent):
        ep = self._ep
        body = self._ep_card(parent, "STAGES",
                             "each one opens already linked to the sources below")
        links = ep.get("links", {})
        n_sessions = len(EP.pq_session_list(ep))
        rows = [
            ("PULL QUOTES", "{} interview session{}".format(
                n_sessions, "" if n_sessions == 1 else "s"),
             self._ep_launch_pq),
            ("SCRIPT → SESSION",
             os.path.basename(links.get("script") or "")
             or "choose the script when you open it",
             self._ep_launch_s2s),
            ("AAF → XML",
             os.path.basename(links.get("aaf") or "")
             or "choose the AAF when you open it",
             self._ep_launch_aaf),
        ]
        for label, note, cmd in rows:
            r = tk.Frame(body, bg=SURF)
            r.pack(fill="x", pady=2)
            b = self._btn(r, label, cmd, small=True, width=18)
            b.pack(side="left")
            if cmd == self._ep_launch_aaf:
                self._ep_aaf_btn = b      # says "opening" while the AAF is read
            tk.Label(r, text=note, font=FB, bg=SURF, fg=SUB).pack(side="left", padx=(12, 0))
            if cmd == self._ep_launch_s2s:
                # The script can live in Google Docs: link it once, then
                # every launch fetches the latest text and checks it.
                gd = links.get("gdoc") or {}
                if gd.get("id"):
                    tk.Label(r, text="\u00b7  Google Doc: {}{}".format(
                                 gd.get("name", ""),
                                 "  \u203a  " + gd["tab_name"] if gd.get("tab_name") else ""),
                             font=FB, bg=SURF, fg=SUCCESS).pack(side="left", padx=(12, 0))
                    if gd.get("tab_name"):
                        self._btn(r, "CHANGE TAB\u2026", self._ep_gdoc_change_tab, small=True
                                  ).pack(side="right", padx=(6, 0))
                    self._btn(r, "CHANGE DOC\u2026", self._ep_gdoc_link, small=True
                              ).pack(side="left", padx=(6, 0))
                else:
                    self._btn(r, "LINK GOOGLE DOC\u2026", self._ep_gdoc_link, small=True
                              ).pack(side="left", padx=(12, 0))
                if not links.get("script"):
                    self._btn(r, "CHOOSE SCRIPT\u2026", self._ep_choose_script, small=True
                              ).pack(side="left", padx=(6, 0))
                if links.get("script") or gd.get("id"):
                    # Script Check, in a few words, without opening anything:
                    # a script with a blocking issue is worth reading first.
                    # VIEW SCRIPT is where it is read, fetched again from
                    # its Doc, checked and fixed.
                    state = self._ep_script_state()
                    if state:
                        tk.Label(r, text="\u00b7  " + state[0], font=FB, bg=SURF,
                                 fg=state[1]).pack(side="left", padx=(12, 0))
                    self._btn(r, "VIEW SCRIPT", self._ep_view_script, small=True
                              ).pack(side="left", padx=(12, 0))

    def _ep_render_sources(self, parent):
        ep = self._ep
        body = self._ep_card(parent, "SOURCES",
                             "a speaker or a session: its recordings, its cameras, its token")
        if not ep["sources"]:
            tk.Label(body, text="No sources yet. Use a recording below as a source.",
                     font=FB, bg=SURF, fg=SUB).pack(anchor="w")
        free_video = self._ep_free_media("video")
        free_audio = self._ep_free_media("audio")
        shared = EP.shared_files(ep)
        # With a script linked, a source's token is one of the script's:
        # the clear ones are filled in, the rest are a dropdown.
        tokens, part_names = self._ep_script_names()
        auto = self.__dict__.setdefault("_ep_auto_tokens", set())
        if tokens:
            auto.update(EP.assign_script_tokens(ep, tokens))
        # Which sources are open.  One with many files starts closed.
        opened = self.__dict__.setdefault("_ep_open", {})
        if len(ep["sources"]) > 1:
            bar = tk.Frame(body, bg=SURF)
            bar.pack(fill="x")
            def _all(state):
                for n in ep["sources"]:
                    opened[n] = state
                self._ep_view()
            self._btn(bar, "COLLAPSE ALL", lambda: _all(False), small=True).pack(side="right")
            self._btn(bar, "EXPAND ALL", lambda: _all(True), small=True
                      ).pack(side="right", padx=(0, 6))
            if tokens:
                tk.Label(bar, text="tokens from {}".format(os.path.basename(
                             ep.get("links", {}).get("script") or "")),
                         font=FB, bg=SURF, fg=SUB).pack(side="left")
        for name in sorted(ep["sources"]):
            src = ep["sources"][name]
            n_rec, n_cam = len(src.get("audio", [])), len(src.get("video", []))
            is_open = opened.setdefault(name, n_rec + n_cam <= 4)
            row = tk.Frame(body, bg=SURF2, highlightbackground=BORDER, highlightthickness=1)
            row.pack(fill="x", pady=3)
            top = tk.Frame(row, bg=SURF2)
            top.pack(fill="x", padx=10, pady=(6, 6))
            arrow = tk.Label(top, text="\u25bc" if is_open else "\u25b6", font=FB,
                             bg=SURF2, fg=ACCENT, cursor="hand2")
            arrow.pack(side="left", padx=(0, 6))
            title = tk.Label(top, text=name, font=FBT, bg=SURF2, fg=TEXT, cursor="hand2")
            title.pack(side="left")
            count = tk.Label(top, text="   {} recording{} \u00b7 {} camera{}".format(
                                 n_rec, "" if n_rec == 1 else "s",
                                 n_cam, "" if n_cam == 1 else "s"),
                             font=FB, bg=SURF2, fg=SUB, cursor="hand2")
            count.pack(side="left")
            det = tk.Frame(row, bg=SURF2)
            if is_open:
                det.pack(fill="x")
            def _toggle(_e=None, n=name, d=det, a=arrow):
                opened[n] = not opened.get(n)
                if opened[n]:
                    d.pack(fill="x")
                else:
                    d.pack_forget()
                a.config(text="\u25bc" if opened[n] else "\u25b6")
            for w in (arrow, title, count):
                w.bind("<Button-1>", _toggle)
            tk.Label(top, text="   token", font=FB, bg=SURF2, fg=SUB).pack(side="left")
            if tokens:
                none = "\u2014 no token \u2014"
                cur = ("VO: " + src["vo_part"]) if src.get("vo_part") and not src.get("token") \
                    else (src.get("token") or none)
                opts = [none] + list(tokens)
                if src.get("token") and src["token"] not in tokens:
                    opts.append(src["token"])          # typed before the script was linked
                opts += ["VO: " + p for p in part_names]
                if cur not in opts:
                    opts.append(cur)
                tv = tk.StringVar(value=cur)
                _FlatDropdown(top, tv, opts, font=FB, width=20).pack(side="left", padx=(6, 0))
                hint = tk.Label(top, text="  from the script" if name in auto else "",
                                font=FB, bg=SURF2, fg=SUB)
                hint.pack(side="left")
                def _pick(*_a, n=name, v=tv, h=hint, none=none):
                    s = ep["sources"][n]
                    val = v.get()
                    if val.startswith("VO: "):
                        s["token"], s["vo_part"] = None, val[4:]
                    else:
                        s["token"] = None if val == none else val
                        s.pop("vo_part", None)
                    auto.discard(n)
                    h.config(text="")
                tv.trace_add("write", _pick)
            else:
                tv = tk.StringVar(value=src.get("token") or "")
                te = tk.Entry(top, textvariable=tv, font=FB, bg=SURF, fg=TEXT,
                              insertbackground=TEXT, relief="flat", width=14)
                te.pack(side="left", padx=(6, 0))
                def _tok(*_a, n=name, v=tv):
                    t = "".join(ch for ch in v.get().upper() if ch.isalnum() or ch == "_")
                    ep["sources"][n]["token"] = t or None
                tv.trace_add("write", _tok)
                if src.get("vo_part"):
                    tk.Label(top, text="   narrator \u00b7 VO: {}".format(src["vo_part"]),
                             font=FB, bg=SURF2, fg=SUB).pack(side="left")
            if src.get("session"):
                tk.Label(top, text="   transcribed", font=FB, bg=SURF2, fg=SUCCESS
                         ).pack(side="left")
            self._btn(top, "REMOVE SOURCE", lambda n=name: self._ep_remove_source(n),
                      small=True).pack(side="right")
            others = [o for o in sorted(ep["sources"]) if o != name]
            if others:
                cv = tk.StringVar(value="— combine with —")
                _FlatDropdown(top, cv, ["— combine with —"] + others, font=FB, width=22
                              ).pack(side="right", padx=(0, 8))
                cv.trace_add("write", lambda *_a, n=name, v=cv: (
                    self._ep_combine(n, v.get()) if v.get() in ep["sources"] else None))
            spans = EP.source_spans(ep, name)
            # Recordings
            for a in src.get("audio", []):
                self._ep_media_line(det, a, "REC", lambda p=a, n=name: self._ep_detach(n, "audio", p),
                                    span=spans.get(a), also=[o for o in shared.get(a, []) if o != name])
            # Cameras
            for v in src.get("video", []):
                self._ep_media_line(det, v, "CAM", lambda p=v, n=name: self._ep_detach(n, "video", p),
                                    span=spans.get(v), also=[o for o in shared.get(v, []) if o != name])
            # One side of the session in a single file while this source is
            # one part of the other side: that file is shared, not moved.
            theirs = [(k, r) for o in others for k in ("audio", "video")
                      for r in ep["sources"][o].get(k, []) if r not in src.get(k, [])]
            if theirs:
                shr = tk.Frame(det, bg=SURF2)
                shr.pack(fill="x", padx=10, pady=(2, 0))
                tk.Label(shr, text="+ share from another source", font=FB, bg=SURF2, fg=SUB
                         ).pack(side="left")
                hv = tk.StringVar(value="\u2014 choose \u2014")
                labels = {}
                for k, r in theirs:
                    labels.setdefault("{}  {}".format("REC" if k == "audio" else "CAM",
                                                      os.path.basename(r)), (k, r))
                _FlatDropdown(shr, hv, ["\u2014 choose \u2014"] + sorted(labels),
                              font=FB, width=44).pack(side="left", padx=(8, 0))
                def _share(*_a, n=name, v=hv, lab=labels):
                    if v.get() in lab:
                        self._ep_share(n, *lab[v.get()])
                hv.trace_add("write", _share)
            if free_audio:
                addr = tk.Frame(det, bg=SURF2)
                addr.pack(fill="x", padx=10, pady=(2, 0))
                tk.Label(addr, text="+ recording", font=FB, bg=SURF2, fg=SUB).pack(side="left")
                rv = tk.StringVar(value="— choose —")
                _FlatDropdown(addr, rv, ["— choose —"] + [os.path.basename(a) for a in free_audio],
                              font=FB, width=34).pack(side="left", padx=(8, 0))
                def _attach_rec(*_a, n=name, v=rv, fa=free_audio):
                    for p in fa:
                        if os.path.basename(p) == v.get():
                            self._ep_attach(n, "audio", p)
                            return
                rv.trace_add("write", _attach_rec)
            if free_video:
                add = tk.Frame(det, bg=SURF2)
                add.pack(fill="x", padx=10, pady=(2, 6))
                tk.Label(add, text="+ camera", font=FB, bg=SURF2, fg=SUB).pack(side="left")
                av = tk.StringVar(value="— choose —")
                _FlatDropdown(add, av, ["— choose —"] + [os.path.basename(v) for v in free_video],
                              font=FB, width=34).pack(side="left", padx=(8, 0))
                def _attach(*_a, n=name, v=av, fv=free_video):
                    pick = v.get()
                    for p in fv:
                        if os.path.basename(p) == pick:
                            self._ep_attach(n, "video", p)
                            return
                av.trace_add("write", _attach)
            else:
                tk.Frame(det, bg=SURF2, height=4).pack()

    def _ep_script_names(self):
        """(tokens, part names) of the linked script, in script order;
        ([], []) with no script to read.  Read again only when it changes."""
        ep = self._ep
        path = EP.absp(ep, ep.get("links", {}).get("script"))
        try:
            key = (path, os.path.getmtime(path)) if path else None
        except OSError:
            key = None
        if key is None:
            return [], []
        cache = getattr(self, "_ep_script_cache", None)
        if cache and cache[0] == key:
            return cache[1]
        tokens, part_names = [], []
        try:
            from parsers import parse_script, is_plain_script
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            toks, parts, pulls, vo_blocks, _t, _w = parse_script(text)
            tokens = list(dict.fromkeys(toks or []))
            part_names = list(dict.fromkeys(p["name"] for p in parts))
            if not pulls and not vo_blocks and is_plain_script(text):
                import monolog
                part_names = [monolog.NARRATION["name"]]
        except Exception:
            pass
        self._ep_script_cache = (key, (tokens, part_names))
        return tokens, part_names

    def _ep_script_state(self):
        """(text, colour) of Script Check's verdict on the linked script, or
        None with no script to read.  Worked out again only when it changes."""
        ep = self._ep
        path = EP.absp(ep, ep.get("links", {}).get("script"))
        try:
            key = (path, os.path.getmtime(path)) if path else None
        except OSError:
            key = None
        if key is None:
            return None
        cache = getattr(self, "_ep_state_cache", None)
        if cache and cache[0] == key:
            return cache[1]
        try:
            import script_check, script_view
            with open(path, encoding="utf-8", errors="replace") as f:
                state = script_view.headline(
                    script_check.run_checks(f.read(), script_path=path))
            state = ("script: " + state[0], state[1])
        except Exception:
            state = None
        self._ep_state_cache = (key, state)
        return state

    def _ep_view_script(self):
        self._ep_script_window()

    def _ep_choose_script(self):
        """Link the episode's script from the review, so the sources can
        take its tokens before Script -> Session is ever opened."""
        ep = self._ep
        path = filedialog.askopenfilename(
            title="Open Script", initialdir=ep["root"],
            filetypes=[("Text", "*.txt"), ("All", "*.*")])
        if not path:
            return
        ep.setdefault("links", {})["script"] = EP.rel(ep, path)
        self._ep_view()

    def _ep_media_line(self, parent, relpath, tag, detach_cmd, span=None, also=()):
        """*span* and *also*: for a file shared with other sources, where
        this source's own recording begins in it, and who else holds it."""
        ep = self._ep
        rec = ep["media"].get(relpath, {})
        r = tk.Frame(parent, bg=SURF2)
        r.pack(fill="x", padx=10, pady=1)
        tk.Label(r, text=tag, font=("Courier New", 9, "bold"), bg="#2a3d2a" if tag == "CAM" else "#2a2a3d",
                 fg=SUCCESS if tag == "CAM" else "#8888ff", padx=5, pady=2).pack(side="left")
        d = rec.get("duration_s")
        txt = "{}   {}".format(os.path.basename(relpath), self._ep_fmt_dur(d))
        tk.Label(r, text=txt, font=FB, bg=SURF2, fg=TEXT, anchor="w").pack(side="left", padx=(8, 0))
        if also:
            import engines
            tk.Label(r, text="shared with {} \u00b7 from {}".format(
                         ", ".join(also), engines.format_start((span or (0.0,))[0])),
                     font=FB, bg=SURF2, fg=ACCENT).pack(side="left", padx=(10, 0))
        tk.Label(r, text=os.path.dirname(relpath), font=(_SANS, 10), bg=SURF2, fg=SUB
                 ).pack(side="left", padx=(10, 0))
        x = tk.Label(r, text=" ✕ ", font=FB, bg=SURF2, fg=SUB, cursor="hand2")
        x.pack(side="right")
        x.bind("<Button-1>", lambda e: detach_cmd())

    @staticmethod
    def _ep_fmt_dur(d):
        if not d:
            return ""
        d = int(round(d))
        return "{}:{:02d}:{:02d}".format(d // 3600, (d % 3600) // 60, d % 60)

    def _ep_render_unassigned(self, parent):
        ep = self._ep
        audio = self._ep_free_media("audio")
        video = self._ep_free_media("video")
        if not audio and not video:
            return
        body = self._ep_card(parent, "NOT YET IN A SOURCE",
                             "recordings and cameras the scan kept but could not place")
        choices = self._ep_attach_choices()
        for a in audio:
            r = tk.Frame(body, bg=SURF)
            r.pack(fill="x", pady=1)
            self._btn(r, "USE AS SOURCE", lambda p=a: self._ep_make_source(p),
                      small=True).pack(side="left")
            tk.Label(r, text="{}   {}   {}".format(os.path.basename(a), self._ep_fmt_dur(
                ep["media"][a].get("duration_s")), os.path.dirname(a)),
                     font=FB, bg=SURF, fg=TEXT).pack(side="left", padx=(10, 0))
            self._btn(r, "PUT ASIDE", lambda p=a: self._ep_exclude(p), small=True
                      ).pack(side="right")
            self._ep_attach_picker(r, "audio", a, choices)
        for v in video:
            r = tk.Frame(body, bg=SURF)
            r.pack(fill="x", pady=1)
            tk.Label(r, text="CAM", font=("Courier New", 9, "bold"), bg="#2a3d2a",
                     fg=SUCCESS, padx=5, pady=2).pack(side="left")
            tk.Label(r, text="{}   {}   {}".format(os.path.basename(v), self._ep_fmt_dur(
                ep["media"][v].get("duration_s")), os.path.dirname(v)),
                     font=FB, bg=SURF, fg=TEXT).pack(side="left", padx=(8, 0))
            self._btn(r, "PUT ASIDE", lambda p=v: self._ep_exclude(p), small=True
                      ).pack(side="right")
            self._ep_attach_picker(r, "video", v, choices)

    def _ep_attach_choices(self):
        """What a loose file can be attached to, as it is offered: {label:
        source name}.  A source goes by its token -- that is what the script
        and every later step call it -- or by its VO part; one with neither
        yet goes by its name.  Tokens first, then VO parts, then the rest."""
        ep = self._ep
        rows = []
        for name in sorted(ep["sources"]):
            src = ep["sources"][name]
            if src.get("token"):
                rows.append((0, src["token"], name))
            elif src.get("vo_part"):
                rows.append((1, "VO: " + src["vo_part"], name))
            else:
                rows.append((2, "{}  (no token)".format(name), name))
        seen = {}
        for _rank, label, _name in rows:
            seen[label] = seen.get(label, 0) + 1
        out = {}
        for _rank, label, name in sorted(rows, key=lambda t: (t[0], t[1].lower(), t[2])):
            # Two sources under one token: say which is which.
            out[label if seen[label] == 1 else "{}  ·  {}".format(label, name)] = name
        return out

    def _ep_attach_picker(self, row, kind, relpath, choices):
        """The "attach to" dropdown on a loose file's row."""
        if not choices:
            return
        none = "— attach to —"
        sv = tk.StringVar(value=none)
        _FlatDropdown(row, sv, [none] + list(choices), font=FB, width=26
                      ).pack(side="right", padx=(0, 8))
        sv.trace_add("write", lambda *_a: (
            self._ep_attach(choices[sv.get()], kind, relpath)
            if sv.get() in choices else None))

    def _ep_render_aside(self, parent):
        ep = self._ep
        aside = [(r, rec) for r, rec in ep["media"].items() if rec["status"] not in ("source", "mix")]
        if not aside:
            return
        body = self._ep_card(parent, "PUT ASIDE",
                             "{} files, with the reason; nothing here is deleted".format(len(aside)))
        order = {"excluded": 0, "pt_copy": 1, "duplicate": 2, "short": 3, "noise": 4}
        titles = {"excluded": "Put aside by you", "pt_copy": "Pro Tools session audio",
                  "duplicate": "Copies of a recording already listed",
                  "short": "Too short to be a recording", "noise": "Library, renders, caches"}
        groups = {}
        for r, rec in aside:
            groups.setdefault(rec["status"], []).append((r, rec))
        for status in sorted(groups, key=lambda s: order.get(s, 9)):
            items = sorted(groups[status], key=lambda t: t[0].lower())
            g = tk.Frame(body, bg=SURF)
            g.pack(fill="x", pady=(6, 0))
            hdr = tk.Frame(g, bg=SURF, cursor="hand2")
            hdr.pack(fill="x")
            arrow = tk.Label(hdr, text="▶", font=FB, bg=SURF, fg=ACCENT)
            arrow.pack(side="left", padx=(0, 6))
            tk.Label(hdr, text="{}  ({})".format(titles.get(status, status), len(items)),
                     font=FBT, bg=SURF, fg=TEXT).pack(side="left")
            lst = tk.Frame(g, bg=SURF)
            shown = [False]
            def _toggle(_e=None, lst=lst, arrow=arrow, shown=shown, items=items, status=status):
                if shown[0]:
                    lst.pack_forget(); arrow.config(text="▶"); shown[0] = False
                    return
                if not lst.winfo_children():
                    for r, rec in items[:300]:
                        self._ep_aside_line(lst, r, rec, status)
                    if len(items) > 300:
                        tk.Label(lst, text="… and {} more".format(len(items) - 300),
                                 font=FB, bg=SURF, fg=SUB).pack(anchor="w")
                lst.pack(fill="x", padx=(20, 0)); arrow.config(text="▼"); shown[0] = True
            for w in (hdr, arrow):
                w.bind("<Button-1>", _toggle)
            if status == "excluded":
                _toggle()

    def _ep_aside_line(self, parent, relpath, rec, status):
        r = tk.Frame(parent, bg=SURF)
        r.pack(fill="x", pady=1)
        d = rec.get("duration_s")
        txt = "{}   {}   {}".format(os.path.basename(relpath), self._ep_fmt_dur(d), rec.get("reason", ""))
        tk.Label(r, text=txt, font=FB, bg=SURF, fg=TEXT if status != "noise" else SUB,
                 anchor="w").pack(side="left")
        tk.Label(r, text=os.path.dirname(relpath), font=(_SANS, 10), bg=SURF, fg=SUB
                 ).pack(side="left", padx=(10, 0))
        if status == "excluded":
            self._btn(r, "RESTORE", lambda p=relpath: self._ep_restore(p), small=True).pack(side="right")
        elif rec["kind"] == "audio" and (d or 0) >= EP.MIN_SOURCE_SECS and status in ("pt_copy", "noise"):
            self._btn(r, "USE AS SOURCE", lambda p=relpath: self._ep_make_source(p), small=True
                      ).pack(side="right")
        elif rec["kind"] == "video" and status in ("noise", "short", "pt_copy"):
            self._btn(r, "KEEP", lambda p=relpath: self._ep_restore(p), small=True).pack(side="right")

    # -- edits ------------------------------------------------------------

    def _ep_free_media(self, kind):
        ep = self._ep
        used = set()
        for src in ep["sources"].values():
            used.update(src.get("audio", [])); used.update(src.get("video", []))
        return sorted(r for r, rec in ep["media"].items()
                      if rec["kind"] == kind and rec["status"] == "source" and r not in used)

    def _ep_make_source(self, relpath):
        ep = self._ep
        rec = ep["media"][relpath]
        rec["status"] = "source"; rec["reason"] = ""; rec["dup_of"] = None
        if relpath in ep["excluded"]:
            ep["excluded"].remove(relpath)
        name = EP._stem(relpath)
        base, i = name, 2
        while name in ep["sources"]:
            name = "{} ({})".format(base, i); i += 1
        ep["sources"][name] = {"token": None, "audio": [relpath], "video": [],
                               "session": None, "user_made": True}
        self._ep_view()

    def _ep_combine(self, name, other):
        """Two sources are one session (each mic was given its own): *other*
        folds into *name*."""
        EP.merge_sources(self._ep, name, other)
        self._ep_view()

    def _ep_remove_source(self, name):
        self._ep["sources"].pop(name, None)
        self._ep_view()

    def _ep_attach(self, name, kind, relpath):
        src = self._ep["sources"].get(name)
        if src is None:
            return
        for other in self._ep["sources"].values():
            if relpath in other.get(kind, []):
                other[kind].remove(relpath)
        src.setdefault(kind, []).append(relpath)
        src[kind].sort()
        src["user_made"] = True
        self._ep_view()

    def _ep_share(self, name, kind, relpath):
        """*relpath* joins source *name* and stays where it is."""
        EP.share_file(self._ep, name, kind, relpath)
        self._ep["sources"][name]["user_made"] = True
        self._ep_view()

    def _ep_detach(self, name, kind, relpath):
        src = self._ep["sources"].get(name)
        if src and relpath in src.get(kind, []):
            src[kind].remove(relpath)
            src["user_made"] = True
            if not src.get("audio") and not src.get("video"):
                self._ep["sources"].pop(name, None)
        self._ep_view()

    def _ep_exclude(self, relpath):
        ep = self._ep
        for src in ep["sources"].values():
            for kind in ("audio", "video"):
                if relpath in src.get(kind, []):
                    src[kind].remove(relpath)
        ep["media"][relpath]["status"] = "excluded"
        ep["media"][relpath]["reason"] = "put aside"
        if relpath not in ep["excluded"]:
            ep["excluded"].append(relpath)
        self._ep_view()

    def _ep_restore(self, relpath):
        ep = self._ep
        if relpath in ep["excluded"]:
            ep["excluded"].remove(relpath)
        rec = ep["media"][relpath]
        rec["status"] = "source"; rec["reason"] = ""
        self._ep_view()

    # ── Google Docs ───────────────────────────────────────────────────────

    def _ep_launch_s2s_fetched(self):
        """Continue a Script -> Session launch after the Doc was fetched."""
        self._ep_s2s_fetched = True
        try:
            self._ep_launch_s2s()
        finally:
            self._ep_s2s_fetched = False

    def _ep_gdoc_script_path(self, name=None):
        ep = self._ep
        gd = ep.get("links", {}).get("gdoc") or {}
        stem = re.sub(r'[\\/:*?"<>|]+', "_", name or gd.get("name") or ep["title"]).strip() or "script"
        return os.path.join(ep["root"], stem + ".txt")

    def _ep_gdoc_bg(self, work, done):
        """Run *work()* off the UI thread (sign-in, network); hand its
        result to *done(result)* on the UI thread, or show the error."""
        def _run():
            try:
                res = work()
            except Exception as e:
                msg = str(e)
                self._ui(lambda: messagebox.showerror("Google Docs", msg))
                return
            self._ui(lambda: done(res))
        threading.Thread(target=_run, daemon=True).start()

    def _gdocs_ready(self):
        """True when Google Docs is set up on this machine; otherwise walk
        through choosing the two set-up files, once."""
        if gdocs.is_ready():
            return True
        need = [n for n, ok in (("the OAuth client file (a .json)", gdocs.is_configured()),
                                ("the Picker API key (a .txt)", bool(gdocs.picker_key())))
                if not ok]
        if not messagebox.askokcancel("Set up Google Docs", "\n".join(
                ["Google Docs needs your team's set-up files on this machine, once:", ""]
                + ["• " + n for n in need]
                + ["", "Whoever made the team's Google Cloud project has them. "
                   "Choose them now?"]), parent=self):
            return False
        client = key = None
        if not gdocs.is_configured():
            client = filedialog.askopenfilename(
                parent=self, title="Choose the OAuth client file",
                filetypes=[("Google client file", "*.json"), ("All files", "*.*")])
            if not client:
                return False
        if not gdocs.picker_key():
            key = filedialog.askopenfilename(
                parent=self, title="Choose the Picker API key file",
                filetypes=[("Text file", "*.txt"), ("All files", "*.*")])
            if not key:
                return False
        try:
            gdocs.install_setup(client, key)
        except (ValueError, OSError) as e:
            messagebox.showerror("Set up Google Docs", str(e), parent=self)
            return False
        return gdocs.is_ready()

    def _ep_gdoc_link(self):
        """Pick the episode's script Doc through Google's picker."""
        if not self._ep_ready():
            return
        if not self._gdocs_ready():
            return
        ep = self._ep

        def _work():
            creds = gdocs.get_credentials()
            return gdocs.pick_document(creds)

        def _done(pick):
            if not pick:
                return
            ep.setdefault("links", {})["gdoc"] = {"id": pick["id"], "name": pick["name"]}
            EP.save(ep)
            self._ep_gdoc_with_tab(ep, self._ep_view)
        self._ep_gdoc_bg(_work, _done)

    def _ep_gdoc_change_tab(self):
        gd = self._ep.get("links", {}).get("gdoc") or {}
        gd.pop("tab", None)
        gd.pop("tab_name", None)
        self._ep_gdoc_with_tab(self._ep, self._ep_view)

    def _ep_gdoc_with_tab(self, ep, then):
        """Run *then()* once it is known which tab of the linked Doc holds
        the script.  A Doc with several tabs (notes, dramatizations) asks,
        once; everything PostBridge reads or writes then uses only that tab."""
        gd = ep.get("links", {}).get("gdoc") or {}
        if "tab" in gd:
            then()
            return

        def _keep(tab_id, title=None):
            gd["tab"] = tab_id
            if title:
                gd["tab_name"] = title
            EP.save(ep)
            then()

        def _got(tabs):
            if len(tabs) <= 1:
                _keep(tabs[0][0] if tabs else None)
                return
            win = tk.Toplevel(self)
            win.title("Which tab is the script?")
            win.configure(bg=BG)
            win.transient(self)
            self._ep_tab_win = win
            tk.Label(win, text="Which tab holds the script?", font=FH, bg=BG, fg=TEXT
                     ).pack(anchor="w", padx=20, pady=(14, 2))
            tk.Label(win, text="{} has {} tabs. PostBridge reads, checks and writes only "
                     "this one.".format(gd.get("name", "The Doc"), len(tabs)),
                     font=FB, bg=BG, fg=SUB, wraplength=520, justify="left"
                     ).pack(anchor="w", padx=20, pady=(0, 10))
            likely = next((tid for tid, title in tabs if "script" in title.lower()), tabs[0][0])

            def _pick(tid, title):
                win.destroy()
                _keep(tid, title)
            for tid, title in tabs:
                self._btn(win, title, lambda i=tid, n=title: _pick(i, n), small=True,
                          color=ACCENT if tid == likely else None
                          ).pack(fill="x", padx=20, pady=2)
            self._btn(win, "CANCEL", win.destroy, small=True).pack(anchor="e", padx=20, pady=(10, 14))
            win.bind("<Escape>", lambda e: win.destroy())
            self._center_dialog(win, min_w=460, min_h=200)
        self._ep_gdoc_bg(lambda: gdocs.list_tabs(gdocs.get_credentials(), gd["id"]), _got)

    def _ep_gdoc_fetch(self, then=None):
        """Fetch the linked Doc as the episode's script, run Script Check,
        show the findings; *then()* runs when the user continues."""
        ep = self._ep
        gd = (ep.get("links", {}).get("gdoc") or {})
        if not gd.get("id"):
            return
        if not self._gdocs_ready():
            return
        if "tab" not in gd:
            # Linked before tabs were asked about: settle the tab first.
            self._ep_gdoc_with_tab(ep, lambda: self._ep_gdoc_fetch(then))
            return
        prev_text = ""
        prev_path = EP.absp(ep, ep["links"].get("script"))
        if prev_path and os.path.isfile(prev_path):
            try:
                with open(prev_path, encoding="utf-8", errors="replace") as f:
                    prev_text = f.read()
            except OSError:
                pass

        def _work():
            creds = gdocs.get_credentials()
            text, meta = gdocs.fetch_text(creds, gd["id"], gd.get("tab"))
            return text, meta

        def _done(res):
            text, meta = res
            # FIX FORMAT IN PROJECT COPY: for a Doc this account can't write,
            # Fix Format runs on what is saved here instead, on every fetch.
            # The Doc stays as it is.
            local_fixes = []
            if gd.get("fix_local"):
                text, local_fixes = script_normalize.normalize(text, title=meta.get("name"))
            path = self._ep_gdoc_script_path(meta.get("name"))
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            links = ep.setdefault("links", {})
            links["script"] = EP.rel(ep, path)
            links["gdoc"] = dict(gd, name=meta.get("name", gd.get("name", "")),
                                 version=meta.get("version"), modified=meta.get("modifiedTime"))
            EP.save(ep)
            import script_check
            findings = script_check.run_checks(text, script_path=path)
            diff = gdocs.diff_pulls(prev_text, text) if prev_text else None
            # The review's own line about the script was read before this.
            self._ep_review_refresh()
            self._ep_show_findings(findings, text, meta, diff, then, local_fixes)
        self._ep_gdoc_bg(_work, _done)

    def _ep_show_findings(self, findings, text, meta, diff, then=None, local_fixes=()):
        """A fetched Doc, shown: the script window on the project copy, with
        what the fetch found and what can be written back to the Doc.
        *local_fixes*: what Fix Format changed in the project copy."""
        self._ep_script_window(then, fetched={
            "findings": findings, "text": text, "meta": meta, "diff": diff,
            "local_fixes": list(local_fixes)})

    def _ep_review_refresh(self):
        """Redraw the review if it is what is on screen: its line about the
        script, and the tokens it offers, are read from the script."""
        mark = getattr(self, "_ep_review_mark", None)
        try:
            if mark is not None and mark.winfo_exists():
                self._ep_view()
        except tk.TclError:
            pass

    def _ep_script_window(self, then=None, fetched=None):
        """The episode's script in the script window, and everything done to
        it from there: fetch the Google Doc again, write Script Check's notes
        into it, fix its format.  *fetched* is what a fetch just brought
        (see _ep_show_findings); *then()* makes it a step on the way
        somewhere, with CONTINUE in place of CLOSE."""
        import script_view
        ep = self._ep
        links = ep.setdefault("links", {})
        gd = links.get("gdoc") or {}
        path = EP.absp(ep, links.get("script"))
        if not (path and os.path.isfile(path)):
            if gd.get("id"):
                self._ep_gdoc_fetch(then)       # nothing here yet: bring it in
            else:
                messagebox.showinfo("View script", "There is no script file to show yet.")
            return None
        win = script_view.show(self, path)
        self._ep_findings_win = win
        self._ep_fetch_count = getattr(self, "_ep_fetch_count", 0) + (1 if fetched else 0)
        win.on_change = self._ep_review_refresh

        def _close():
            try:
                win.destroy()
            except tk.TclError:
                pass
            if then is not None:
                then()
        right = [("CONTINUE  →", _close, True)] if then is not None \
            else [("CLOSE", _close, False)]

        if not gd.get("id"):
            win.set_actions([], right)
            return win

        doc = "Google Doc: {}{}".format(
            gd.get("name", ""), "  ›  " + gd["tab_name"] if gd.get("tab_name") else "")
        fetch = ("⟳ FETCH LATEST", lambda: self._ep_gdoc_fetch(then), fetched is None)
        if not fetched:
            # Opened to read: this is the copy from the last fetch.
            win.set_status(doc + "   ·   the project copy, as last fetched.  "
                           "FETCH LATEST brings in the Doc as it is now, and checks it.")
            win.set_actions([fetch], right)
            return win

        findings, text, meta = fetched["findings"], fetched["text"], fetched["meta"]
        status = doc + "   ·   fetched just now"
        if fetched["diff"]:
            a, r, c = fetched["diff"]
            status += "   ·   since the last fetch: {} pull{} added, {} removed, {} changed".format(
                a, "" if a == 1 else "s", r, c)
        if fetched["local_fixes"]:
            status += "\nFixed in the project copy (the Doc is unchanged):  " + \
                ";  ".join(fetched["local_fixes"])
        win.set_status(status)

        def _write():
            notes = gdocs.findings_to_notes(findings, text)
            def _work():
                return gdocs.write_notes(gdocs.get_credentials(), gd["id"], notes, tab=gd.get("tab"))
            def _done(res):
                placed, unplaced = res
                msg = "{} note{} written into the Doc.".format(placed, "" if placed == 1 else "s")
                if unplaced:
                    msg += "  {} could not find their line and sit at the top.".format(len(unplaced))
                messagebox.showinfo("Google Docs", msg)
            self._ep_gdoc_bg(_work, _done)

        def _clear():
            def _work():
                return gdocs.clear_notes(gdocs.get_credentials(), gd["id"], tab=gd.get("tab"))
            self._ep_gdoc_bg(_work, lambda n: messagebox.showinfo(
                "Google Docs", "{} note{} removed from the Doc.".format(n, "" if n == 1 else "s")))

        def _fix():
            title = meta.get("name")
            changes = script_normalize.normalize(text, title=title)[1]
            if not messagebox.askyesno("Fix format in the Doc", "\n".join(
                    ["This changes the Google Doc for everyone who has it open:", ""]
                    + ["• " + c for c in changes]
                    + ["• Removes PostBridge's notes", "",
                       "Wording and timecodes stay as they are. "
                       "File → Version history in the Doc can undo it."])):
                return
            def _work():
                return gdocs.fix_format(gdocs.get_credentials(), gd["id"], title=title, tab=gd.get("tab"))
            # Then check what the Doc says now.
            self._ep_gdoc_bg(_work, lambda _changes: self._ep_gdoc_fetch(then))

        name = meta.get("name", "the Doc")
        fix_local = bool(gd.get("fix_local"))
        local_changes = script_normalize.normalize(text, title=meta.get("name"))[1]

        def _set_fix_local(on):
            if on and not messagebox.askyesno("Fix format in the project copy", "\n".join(
                    ["“{}” is view only, so it stays as it is.  The script "
                     "saved in the project folder gets these fixes, now and every "
                     "time it is fetched:".format(name), ""]
                    + ["• " + c for c in local_changes]
                    + ["", "Wording and timecodes stay as they are."]), parent=win):
                return
            link = self._ep.setdefault("links", {}).setdefault("gdoc", {})
            if on:
                link["fix_local"] = True
            else:
                link.pop("fix_local", None)
            EP.save(self._ep)
            self._ep_gdoc_fetch(then)      # fetch again, fixed (or not) this time

        left, notice = [fetch], None
        if not gdocs.can_edit(meta):
            # Shared view-only: it fetches and checks, but nothing can be
            # written back -- say so instead of offering buttons Google
            # would refuse.  The project copy can still be fixed.
            owner = gdocs.owner_name(meta) or "its owner"
            notice = ("View only: “{}” belongs to {}, and this Google "
                      "account can't edit it, so nothing can be written into the "
                      "Doc.  Ask {} to share it with you as an Editor{}.").format(
                          name, owner, owner,
                          "" if fix_local or not local_changes
                          else ", or fix the format in the project copy")
            if not fix_local and local_changes:
                left.append(("FIX FORMAT IN PROJECT COPY", lambda: _set_fix_local(True), True))
        else:
            left.append(("CLEAR NOTES IN DOC", _clear, False))
            if findings:
                left.append(("WRITE NOTES INTO DOC", _write, True))
            if local_changes:
                left.append(("FIX FORMAT IN DOC", _fix, True))
        if fix_local:
            # Turned off, the next fetch saves the Doc exactly as it is.
            left.append(("STOP FIXING THE COPY", lambda: _set_fix_local(False), False))
        win.set_actions(left, right, notice)
        return win

    # ── Write-backs from the stages ───────────────────────────────────────

    def _ep_bound(self, ep_file):
        """The episode a stage was launched from: the open one when it is
        that file, else loaded fresh (and not made current)."""
        ep = getattr(self, "_ep", None)
        if ep and ep.get("file_path") and ep_file and \
                os.path.normcase(ep["file_path"]) == os.path.normcase(ep_file):
            return ep
        if ep_file and os.path.isfile(ep_file):
            try:
                return EP.load(ep_file)
            except Exception:
                return None
        return None

    def _ep_owns(self, ep, key, path):
        return bool(path) and ep.get("links", {}).get(key) == EP.rel(ep, path)

    @staticmethod
    def _ep_ensure_media(ep, r, kind):
        """A media record for a file a stage linked that the scan never saw
        (outside the root, or added since)."""
        if r in ep["media"]:
            return
        full = EP.absp(ep, r)
        try:
            size = os.path.getsize(full)
        except OSError:
            size = 0
        ep["media"][r] = {"kind": kind, "size": size, "duration_s": None,
                          "status": "source", "reason": "", "dup_of": None}

    @staticmethod
    def _ep_source_for(ep, rel_paths, kind="audio"):
        for name, src in ep["sources"].items():
            if set(src.get(kind, [])) & set(rel_paths):
                return name
        return None

    def _ep_register_session(self, session):
        """Pull Quotes saved an interview session while opened from an
        episode: its token and media become (or update) a source."""
        proj = getattr(self, "_pq_project", None) or {}
        ep = self._ep_bound(session.get("_ep_file") or proj.get("episode_file"))
        if not ep or not session.get("token"):
            return
        token = session["token"]
        media = [EP.rel(ep, m) for m in session.get("media", []) if m]
        name  = self._ep_source_for(ep, media)
        if name is None:
            for n, src in ep["sources"].items():
                if src.get("token") == token:
                    name = n
                    break
        if name is None:
            name, i = token, 2
            while name in ep["sources"]:
                name = "{} ({})".format(token, i); i += 1
            ep["sources"][name] = {"token": token, "audio": [], "video": [],
                                   "session": None, "user_made": True}
        src = ep["sources"][name]
        src["token"] = token
        src["session"] = session.get("id")
        for m in media:
            self._ep_ensure_media(ep, m, "audio")
            if m not in src["audio"]:
                src["audio"].append(m)
        src["audio"].sort()

    def _ep_absorb_s2s(self):
        """Script -> Session saving into the episode: take the pool's token
        assignments into the sources -- a camera assigned to a token joins
        that token's source (leaving any other), a recording assigned to a
        token gives its source that token."""
        ep = self._ep
        pool = getattr(self, "_pool", None)
        rows = [(r["path"], r["var"].get()) for r in getattr(pool, "_rows", [])]
        from utils import is_video
        # A file with more than one row is shared between tokens: it joins
        # each token's source and leaves none of them.
        _n = {}
        for path, tok in rows:
            if tok and not tok.startswith("\u2014"):
                _n[path] = _n.get(path, 0) + 1
        multi = {EP.rel(ep, p) for p, n in _n.items() if n > 1}
        # Recordings first: a camera assigned to a VO part follows the
        # recording that narrates it, so every part must be placed before
        # any camera looks for it.
        rows.sort(key=lambda row: is_video(row[0]))
        for path, tok in rows:
            if not tok or tok.startswith("\u2014"):
                continue
            r = EP.rel(ep, path)
            kind = "video" if is_video(path) else "audio"
            if tok.startswith("VO:"):
                # A narrator take: the source owning the file remembers the
                # part, so the next script version opens assigned.  A
                # camera for a part goes to the source narrating that part
                # -- never the other way round: the camera's current source
                # may be a scan's wrong guess, and stamping the part on it
                # relabelled that source's narration.
                part = tok[3:].strip()
                if kind == "video":
                    name = next((n for n, s in ep["sources"].items()
                                 if s.get("vo_part") == part), None)
                    if name is None:
                        continue
                else:
                    name = self._ep_source_for(ep, [r], kind)
                    if name is None:
                        name, i = EP._stem(r), 2
                        while name in ep["sources"]:
                            name = "{} ({})".format(EP._stem(r), i); i += 1
                        ep["sources"][name] = {"token": None, "audio": [], "video": [],
                                               "session": None, "user_made": True}
                    ep["sources"][name]["vo_part"] = part
                src = ep["sources"][name]
                self._ep_ensure_media(ep, r, kind)
                if r not in src.setdefault(kind, []):
                    for other_name, other in ep["sources"].items():
                        if other_name != name and r in other.get(kind, []) \
                                and r not in multi:
                            other[kind].remove(r)
                    src[kind].append(r)
                    src[kind].sort()
                    src["user_made"] = True     # the user placed it: a rescan keeps it
                continue
            name = None
            for n, src in ep["sources"].items():
                if src.get("token") == tok:
                    name = n
                    break
            if name is None:
                # No source carries this token yet: the recording's own
                # source takes it, or a new source is made for the file.
                name = self._ep_source_for(ep, [r], kind)
                if name is None:
                    name, i = tok, 2
                    while name in ep["sources"]:
                        name = "{} ({})".format(tok, i); i += 1
                    ep["sources"][name] = {"token": tok, "audio": [], "video": [],
                                           "session": None, "user_made": True}
                ep["sources"][name]["token"] = tok
            src = ep["sources"][name]
            self._ep_ensure_media(ep, r, kind)
            if r not in src.setdefault(kind, []):
                for other_name, other in ep["sources"].items():
                    if other_name != name and r in other.get(kind, []) \
                            and r not in multi:
                        other[kind].remove(r)
                src[kind].append(r)
                src[kind].sort()
                src["user_made"] = True         # the user placed it: a rescan keeps it

    def _ep_absorb_aaf(self, data):
        """AAF -> XML saving into the episode: keep each source's cameras,
        sync and track row with the source, so a fresh AAF export of the same
        session opens synced.  *data* is the setup being saved."""
        ep = self._ep
        by_label = {self._aaf_vid_label(p): p for p in self._aaf_video_paths}
        by_stem = {}
        for name, src in ep["sources"].items():
            for a in src.get("audio", []):
                by_stem[EP._stem_key(a)] = name
        for base in getattr(self, "_aaf_sources", []):
            name = by_stem.get(EP._stem_key(base + ".x"))
            if name is None:
                continue
            src = ep["sources"][name]
            labels = [data["assignments"].get(base)] + list(
                data.get("extra_assignments", {}).get(base, []))
            vids = [EP.rel(ep, by_label[l]) for l in labels if l in by_label]
            if vids:
                for v in vids:
                    self._ep_ensure_media(ep, v, "video")
                if set(vids) != set(src.get("video", [])):
                    # The user picked these cameras in Step 2: they
                    # leave any other source, and a rescan keeps them.
                    _both = EP.shared_files(ep)     # a shared camera stays shared
                    for other_name, other in ep["sources"].items():
                        if other_name != name:
                            other["video"] = [v for v in other.get("video", [])
                                              if v not in vids or v in _both]
                    src["user_made"] = True
                src["video"] = vids
            sd = data.get("sync", {}).get(base, {})
            src["aaf_sync"] = {
                "video":      vids[0] if vids else None,
                "audio":      EP.rel(ep, sd.get("audio_path")) if sd.get("audio_path") else None,
                "offset":     sd.get("offset", "0.000"),
                "enabled":    sd.get("enabled", False),
                "state":      sd.get("state", ""),
                "locked":     sd.get("locked", False),
                "label":      sd.get("label", ""),
                "needs_sync": sd.get("needs_sync", False),
                "drift":      sd.get("drift"),
            }
            tm = data.get("track_map", {}).get(base)
            if tm:
                src["track_map"] = tm
        if data.get("mix_path"):
            ep["links"]["mix"] = EP.rel(ep, data["mix_path"])

    # ── Stage launchers ───────────────────────────────────────────────────

    def _ep_ready(self):
        """The episode must be on disk before a workflow is launched from
        it, so links can be written back."""
        ep = self._ep
        if not ep.get("file_path"):
            if not self._ep_save():
                return False
        return True

    def _ep_launch_pq(self):
        if not self._ep_ready():
            return
        ep = self._ep
        self.workflow = "pull_quotes"
        # A session still transcribing comes back as itself, not as a copy
        # from the file: the run reports to that dict, and a copy would show
        # neither the run nor its transcript, then save over the result.
        running = {s.get("id"): s for s in self._pq_running_sessions()}
        sessions = [running.get(s.get("id"), s) for s in EP.pq_session_list(ep)]
        for s in sessions:
            s["_ep_file"] = ep["file_path"]
        # Saves go back into the episode file, not over it.
        self._pq_load_project({"title": ep["title"], "episode_file": ep["file_path"],
                               "sessions": sessions}, None)

    def _ep_launch_s2s(self):
        if not self._ep_ready():
            return
        ep = self._ep
        links = ep.setdefault("links", {})
        saved = EP.get_stage(ep, EP.STAGE_S2S)
        script = (saved or {}).get("script") or ""
        if saved and (os.path.isfile(script)
                      or self._is_foreign_platform_path(script)):
            # The saved Step 2 opens as it was left, except where a source
            # has since been given a token in the review: that is the newer
            # word on whose file it is.
            if os.path.isfile(script):
                self._ep_s2s_carry_tokens(saved, self._ep_source_tokens(script))
            self._open_session_data(saved)
            return
        # A linked Google Doc is fetched (and checked) first, so the script
        # that reconciles is the one Jordan has now.
        if (links.get("gdoc") or {}).get("id") and gdocs.is_configured() \
                and not getattr(self, "_ep_s2s_fetched", False):
            self._ep_gdoc_fetch(then=self._ep_launch_s2s_fetched)
            return
        self._ep_s2s_fetched = False
        script = EP.absp(ep, links.get("script"))
        if not (script and os.path.isfile(script)):
            script = filedialog.askopenfilename(
                title="Open Script", initialdir=ep["root"],
                filetypes=[("Text", "*.txt"), ("All", "*.*")])
            if not script:
                return
            links["script"] = EP.rel(ep, script)
            EP.save(ep)
        # Pre-fill Step 2: every source's recordings and cameras, assigned to
        # the source's token when the script knows it.
        assignments, shared = {}, []
        for val, _chosen, files in self._ep_source_tokens(script):
            for p in files:
                if p in assignments:
                    # A file more than one source holds: a second row for
                    # it in Step 2, under this source's token.
                    shared.append({"path": p, "token": val, "twin": True})
                else:
                    assignments[p] = val
        self._pending_setup = {"assignments": assignments, "shared": shared}
        self.workflow = "script_session"
        self._load_script(script)

    def _ep_source_tokens(self, script):
        """What Step 2 assigns each source's files to, for *script*:
        [(value, chosen, [files])] in source order.  value is a token, "VO:
        part" or unassigned; chosen is False when nobody picked it -- the
        source has no token the script knows, and the value is a default."""
        ep = self._ep
        known, part_names, vo_parts = set(), set(), set()
        try:
            from parsers import parse_script, is_plain_script
            with open(script, encoding="utf-8", errors="replace") as f:
                text = f.read()
            tokens, parts, _pulls, vo_blocks, _t, _w = parse_script(text)
            known = set(tokens or [])
            by_index = {p["index"]: p["name"] for p in parts}
            part_names = set(by_index.values())
            vo_parts = {by_index[v["part_index"]] for v in vo_blocks
                        if v.get("part_index") in by_index}
            if not _pulls and not vo_blocks and is_plain_script(text):
                # A plain narration script is cut as a monolog: its one
                # VO target takes every narrator recording.
                import monolog
                part_names = vo_parts = {monolog.NARRATION["name"]}
        except Exception:
            pass
        rows = []
        for _name in sorted(ep["sources"]):
            src = ep["sources"][_name]
            tok = src.get("token")
            vo = src.get("vo_part")
            chosen = True
            if tok and tok in known:
                val = tok
            elif vo and vo in part_names:
                # The part this narrator recording was assigned to last time.
                val = "VO: " + vo
            elif not tok and not src.get("session") and len(vo_parts) == 1:
                # No interview token, no transcript session: a narrator
                # recording.  With one narrated part there is no choice.
                val, chosen = "VO: " + next(iter(vo_parts)), False
            else:
                val, chosen = "— unassigned —", False
            rows.append((val, chosen, [EP.absp(ep, r) for r in
                                       src.get("audio", []) + src.get("video", [])]))
        return rows

    @staticmethod
    def _ep_s2s_carry_tokens(saved, rows):
        """Bring the sources' chosen tokens into a saved Step 2 setup, in
        place.  A file goes to its source's token; one that several sources
        hold goes to each of theirs (a second row per extra token), keeping
        any start typed for a token it already had.  Files of a source with
        no chosen token, and files in no source, stay as saved.  Returns
        the files whose assignment changed."""
        none = "— unassigned —"
        norm = lambda p: os.path.normcase(os.path.normpath(p))
        asg = saved.setdefault("assignments", {})
        key_of = {norm(p): p for p in asg}
        was = {}                     # file -> its saved tokens, own row first
        for p, t in asg.items():
            was.setdefault(norm(p), []).append(t)
        for it in saved.get("shared") or []:
            if it.get("twin") and it.get("path"):
                was.setdefault(norm(it["path"]), []).append(it.get("token"))
        want, holders, path_of = {}, {}, {}
        for val, chosen, files in rows:
            for p in files:
                if not p:
                    continue
                k = norm(p)
                holders[k] = holders.get(k, 0) + 1
                path_of.setdefault(k, p)
                if chosen:
                    want.setdefault(k, []).append(val)
        changed = []
        for k, toks in want.items():
            toks = list(dict.fromkeys(toks))
            old = [t for t in was.get(k, []) if t and t != none]
            # A source holding it that chose nothing keeps what was saved.
            target = toks + ([t for t in old if t not in toks]
                             if holders[k] > len(want[k]) else [])
            if set(target) == set(old):
                continue
            path = key_of.get(k, path_of[k])
            starts = {it.get("token"): it["start"] for it in saved.get("shared") or []
                      if it.get("path") and norm(it["path"]) == k
                      and it.get("start") is not None}
            asg[path] = target[0]
            shared = [it for it in saved.get("shared") or []
                      if not (it.get("path") and norm(it["path"]) == k)]
            if len(target) > 1:
                for i, t in enumerate(target):
                    item = {"path": path, "token": t, "twin": i > 0}
                    if t in starts:
                        item["start"] = starts[t]
                    shared.append(item)
            saved["shared"] = shared
            changed.append(path)
        return changed

    def _ep_paired_videos(self):
        """The cameras of every source that also has a recording, normcased:
        video Step 2 can leave out, since audio covers it, whatever the
        files are called."""
        ep = getattr(self, "_ep", None)
        out = set()
        for src in ((ep or {}).get("sources") or {}).values():
            if src.get("audio"):
                for r in src.get("video", []):
                    p = EP.absp(ep, r)
                    if p:
                        out.add(os.path.normcase(p))
        return out

    def _ep_launch_aaf(self):
        if not self._ep_ready():
            return
        ep = self._ep
        links = ep.setdefault("links", {})
        saved = EP.get_stage(ep, EP.STAGE_AAF)
        if saved and os.path.isfile(saved.get("aaf") or ""):
            self._open_session_data(saved)
            return
        aaf = EP.absp(ep, links.get("aaf"))
        if not (aaf and os.path.isfile(aaf)):
            aaf = filedialog.askopenfilename(
                title="Open AAF", initialdir=ep["root"],
                filetypes=[("AAF", "*.aaf"), ("All", "*.*")])
            if not aaf:
                return
            links["aaf"] = EP.rel(ep, aaf)
            EP.save(ep)
        self._ep_open_aaf(aaf)

    def _ep_open_aaf(self, aaf):
        """Pre-fill the setup from the sources and open Step 2 on *aaf*.

        Both need the AAF read, and a large Pro Tools export takes a couple
        of seconds to read -- long enough for a frozen window to look hung.
        So the first read happens off the UI thread; it is cached, and the
        pre-fill and Step 2 then reuse it."""
        if getattr(self, "_ep_aaf_opening", False):
            return                        # already on its way
        self._ep_aaf_opening = True
        btn = getattr(self, "_ep_aaf_btn", None)
        try:
            label = btn.cget("text")
            btn.config(text="OPENING AAF…")
        except (AttributeError, tk.TclError):
            btn = None

        def _read():
            from parsers import parse_aaf_session
            try:
                parse_aaf_session(aaf)
            except Exception:
                pass                      # _aaf_load reads it again and says why
            self._ui(_open)

        def _open():
            self._ep_aaf_opening = False
            if btn is not None:
                try:
                    if not btn.winfo_exists():
                        return            # the user left the episode meanwhile
                    btn.config(text=label)
                except tk.TclError:
                    return
            self._pending_aaf_setup = self._ep_aaf_setup_for(aaf)
            self._pending_aaf_saved_path = None
            self._pending_mark_saved = False
            self._aaf_load(aaf)

        threading.Thread(target=_read, daemon=True).start()

    def _ep_aaf_setup_for(self, aaf_path):
        """An AAF setup built from the sources: each Pro Tools source (a clip
        base name, which is the recording's file stem) gets the cameras of
        the episode source that owns that recording, and that recording as
        its reference audio; the episode's mix is the stereo mix."""
        from parsers import parse_aaf_session, get_clip_base_name
        ep = self._ep
        bases = []
        try:
            parsed = parse_aaf_session(aaf_path)
            for tr in parsed.get("tracks", []):
                for cl in tr.get("clips", []):
                    b = get_clip_base_name(cl.get("clip_name", ""))
                    if b and b not in bases:
                        bases.append(b)
        except Exception:
            parsed = None
        by_stem = {}
        for name, src in ep["sources"].items():
            for a in src.get("audio", []):
                by_stem[EP._stem_key(a)] = (name, src, a)
        video_paths, audio_paths = [], []
        assignments, slot_counts, extra, sync, track_map = {}, {}, {}, {}, {}
        for b in bases:
            hit = by_stem.get(EP._stem_key(b + ".x"))
            if not hit:
                continue
            name, src, rec = hit
            vids = [EP.absp(ep, v) for v in src.get("video", [])]
            for v in vids:
                if v not in video_paths:
                    video_paths.append(v)
            ref = EP.absp(ep, rec)
            if ref not in audio_paths:
                audio_paths.append(ref)
            if vids:
                assignments[b] = os.path.basename(vids[0])
                if len(vids) > 1:
                    slot_counts[b] = len(vids)
                    extra[b] = [os.path.basename(v) for v in vids[1:]]
            sync[b] = {"enabled": False, "audio_path": ref, "offset": "0.000",
                       "label": "", "state": "", "needs_sync": True, "locked": False}
            # A sync measured on an earlier AAF of the same session still
            # holds when the same camera and the same recording are in
            # play: the offset is between those two files.
            st = src.get("aaf_sync") or {}
            same_cam = bool(vids) and st.get("video") == src.get("video", [None])[0]
            same_ref = st.get("audio") == rec
            if st and same_cam and same_ref and (st.get("state") or st.get("enabled")):
                sync[b].update({k: st.get(k, sync[b].get(k)) for k in
                                ("offset", "enabled", "state", "locked", "label",
                                 "needs_sync", "drift")})
            if src.get("track_map"):
                track_map[b] = list(src["track_map"])
        mix = EP.absp(ep, ep.get("links", {}).get("mix"))
        if mix and os.path.isfile(mix) and mix not in audio_paths:
            audio_paths.append(mix)
        return {"version": 2, "aaf": os.path.abspath(aaf_path),
                "video_paths": video_paths, "audio_paths": audio_paths,
                "assignments": assignments, "slot_counts": slot_counts,
                "extra_assignments": extra, "sync": sync,
                "mix_path": mix if mix and os.path.isfile(mix) else "",
                "track_map": track_map,
                "grouping": "custom" if any(n > 1 for n in slot_counts.values()) else "by_source"}
