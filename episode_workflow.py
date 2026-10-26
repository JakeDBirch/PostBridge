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


class EpisodeWorkflowMixin:

    # ── Entry points ─────────────────────────────────────────────────────

    def _ep_enter(self):
        """Home card: continue the open episode, else choose a folder."""
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
        self.workflow = "episode"
        self._ep_view()
        self._mark_saved()

    def _ep_save(self, prompt=False):
        ep = getattr(self, "_ep", None)
        if not ep:
            return False
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
        (a moved folder is not an unsaved change)."""
        return {k: v for k, v in data.items() if k not in ("root", "file_path")}

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

    def _ep_note_link(self, key, path, owner=None):
        """A workflow launched from this episode saved its own file: remember
        it so the next visit opens it instead of starting from the sources.
        *owner* = (link key, absolute path) names the file the workflow was
        opened with; the link is only taken when it is the episode's own
        (the same script, the same AAF), never from unrelated work."""
        ep = getattr(self, "_ep", None)
        if not ep or not path:
            return
        if owner is not None:
            okey, opath = owner
            if not opath or ep.get("links", {}).get(okey) != EP.rel(ep, opath):
                return
        ep.setdefault("links", {})[key] = EP.rel(ep, path)
        if ep.get("file_path"):
            try:
                EP.save(ep)
            except Exception:
                pass

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

        def _run():
            try:
                res = EP.scan(ep["root"], progress=_progress,
                              excluded=ep.get("excluded"))
            except Exception as e:
                msg = str(e)    # `e` is unbound by the time the lambda runs
                self._ui(lambda: messagebox.showerror("Scan failed", msg))
                self._ui(self._ep_view)
                return
            def _done():
                EP.apply_scan(ep, res)
                self._ep_view()
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
        rows = [
            ("PULL QUOTES", "{} interview session{}".format(
                len(ep.get("sessions", [])), "" if len(ep.get("sessions", [])) == 1 else "s"),
             self._ep_launch_pq),
            ("SCRIPT → SESSION",
             os.path.basename(links.get("s2s_session") or links.get("script") or "")
             or "choose the script when you open it",
             self._ep_launch_s2s),
            ("AAF → XML",
             os.path.basename(links.get("aaf_setup") or links.get("aaf") or "")
             or "choose the AAF when you open it",
             self._ep_launch_aaf),
        ]
        for label, note, cmd in rows:
            r = tk.Frame(body, bg=SURF)
            r.pack(fill="x", pady=2)
            b = self._btn(r, label, cmd, small=True, width=18)
            b.pack(side="left")
            if cmd is self._ep_launch_aaf:
                self._ep_aaf_btn = b      # says "opening" while the AAF is read
            tk.Label(r, text=note, font=FB, bg=SURF, fg=SUB).pack(side="left", padx=(12, 0))
            if cmd is self._ep_launch_s2s:
                # The script can live in Google Docs: link it once, then
                # every launch fetches the latest text and checks it.
                gd = links.get("gdoc") or {}
                if gd.get("id"):
                    tk.Label(r, text="\u00b7  Google Doc: {}".format(gd.get("name", "")),
                             font=FB, bg=SURF, fg=SUCCESS).pack(side="left", padx=(12, 0))
                    self._btn(r, "FETCH & CHECK", self._ep_gdoc_fetch, small=True
                              ).pack(side="left", padx=(12, 0))
                    self._btn(r, "CHANGE DOC\u2026", self._ep_gdoc_link, small=True
                              ).pack(side="left", padx=(6, 0))
                else:
                    self._btn(r, "LINK GOOGLE DOC\u2026", self._ep_gdoc_link, small=True
                              ).pack(side="left", padx=(12, 0))

    def _ep_render_sources(self, parent):
        ep = self._ep
        body = self._ep_card(parent, "SOURCES",
                             "a speaker or a session: its recordings, its cameras, its token")
        if not ep["sources"]:
            tk.Label(body, text="No sources yet. Use a recording below as a source.",
                     font=FB, bg=SURF, fg=SUB).pack(anchor="w")
        free_video = self._ep_free_media("video")
        for name in sorted(ep["sources"]):
            src = ep["sources"][name]
            row = tk.Frame(body, bg=SURF2, highlightbackground=BORDER, highlightthickness=1)
            row.pack(fill="x", pady=3)
            top = tk.Frame(row, bg=SURF2)
            top.pack(fill="x", padx=10, pady=(6, 2))
            tk.Label(top, text=name, font=FBT, bg=SURF2, fg=TEXT).pack(side="left")
            tk.Label(top, text="   token", font=FB, bg=SURF2, fg=SUB).pack(side="left")
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
            # Recordings
            for a in src.get("audio", []):
                self._ep_media_line(row, a, "REC", lambda p=a, n=name: self._ep_detach(n, "audio", p))
            # Cameras
            for v in src.get("video", []):
                self._ep_media_line(row, v, "CAM", lambda p=v, n=name: self._ep_detach(n, "video", p))
            if free_video:
                add = tk.Frame(row, bg=SURF2)
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
                tk.Frame(row, bg=SURF2, height=4).pack()

    def _ep_media_line(self, parent, relpath, tag, detach_cmd):
        ep = self._ep
        rec = ep["media"].get(relpath, {})
        r = tk.Frame(parent, bg=SURF2)
        r.pack(fill="x", padx=10, pady=1)
        tk.Label(r, text=tag, font=("Courier New", 9, "bold"), bg="#2a3d2a" if tag == "CAM" else "#2a2a3d",
                 fg=SUCCESS if tag == "CAM" else "#8888ff", padx=5, pady=2).pack(side="left")
        d = rec.get("duration_s")
        txt = "{}   {}".format(os.path.basename(relpath), self._ep_fmt_dur(d))
        tk.Label(r, text=txt, font=FB, bg=SURF2, fg=TEXT, anchor="w").pack(side="left", padx=(8, 0))
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
        names = sorted(ep["sources"])
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
            if names:
                sv = tk.StringVar(value="— attach to —")
                _FlatDropdown(r, sv, ["— attach to —"] + names, font=FB, width=24
                              ).pack(side="right", padx=(0, 8))
                sv.trace_add("write", lambda *_a, p=v, s=sv: (
                    self._ep_attach(s.get(), "video", p) if s.get() in ep["sources"] else None))

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

    def _ep_gdoc_link(self):
        """Pick the episode's script Doc through Google's picker."""
        if not self._ep_ready():
            return
        if not gdocs.is_configured():
            messagebox.showinfo("Google Docs", gdocs.setup_message())
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
            self._ep_view()
        self._ep_gdoc_bg(_work, _done)

    def _ep_gdoc_fetch(self, then=None):
        """Fetch the linked Doc as the episode's script, run Script Check,
        show the findings; *then()* runs when the user continues."""
        ep = self._ep
        gd = (ep.get("links", {}).get("gdoc") or {})
        if not gd.get("id"):
            return
        if not gdocs.is_configured():
            messagebox.showinfo("Google Docs", gdocs.setup_message())
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
            text, meta = gdocs.fetch_text(creds, gd["id"])
            return text, meta

        def _done(res):
            text, meta = res
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
            self._ep_show_findings(findings, text, meta, diff, then)
        self._ep_gdoc_bg(_work, _done)

    def _ep_show_findings(self, findings, text, meta, diff, then=None):
        """Script Check results for a fetched Doc, with the Doc actions."""
        import script_check
        n_err, n_warn, n_info = script_check.summarize(findings)
        win = tk.Toplevel(self)
        win.title("Script check")
        win.configure(bg=BG)
        win.transient(self)
        tk.Label(win, text="Script check \u2014 {}".format(meta.get("name", "")),
                 font=FH, bg=BG, fg=TEXT, padx=20).pack(anchor="w", pady=(14, 2))
        head = "{} error{}, {} warning{}, {} note{}".format(
            n_err, "" if n_err == 1 else "s", n_warn, "" if n_warn == 1 else "s",
            n_info, "" if n_info == 1 else "s")
        if diff:
            a, r, c = diff
            head += "   \u00b7   since the last fetch: {} pull{} added, {} removed, {} changed".format(
                a, "" if a == 1 else "s", r, c)
        tk.Label(win, text=head, font=FB, bg=BG, fg=SUB, padx=20).pack(anchor="w", pady=(0, 8))
        box = tk.Frame(win, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        box.pack(fill="both", expand=True, padx=20)
        lst = tk.Text(box, font=FB, bg=SURF, fg=TEXT, relief="flat", bd=0, wrap="word",
                      height=16, width=96)
        lst.pack(fill="both", expand=True, padx=8, pady=8)
        colours = {"error": "#e05050", "warn": WARN, "info": SUB}
        for f in findings:
            tag = f.severity
            lst.tag_configure(tag, foreground=colours.get(tag, TEXT))
            loc = "line {}  ".format(f.line) if f.line else ""
            lst.insert("end", "{}{}  {}\n".format(f.severity.upper().ljust(6), loc, f.title), tag)
            if f.detail:
                lst.insert("end", "        {}\n".format(" ".join(str(f.detail).split())), "info")
        if not findings:
            lst.insert("end", "Nothing to report. The script is ready to reconcile.\n", "info")
        lst.configure(state="disabled")
        self._ep_findings_win = win

        nav = tk.Frame(win, bg=BG)
        nav.pack(fill="x", padx=20, pady=(10, 14))
        gd = self._ep.get("links", {}).get("gdoc") or {}

        def _write():
            notes = gdocs.findings_to_notes(findings, text)
            def _work():
                return gdocs.write_notes(gdocs.get_credentials(), gd["id"], notes)
            def _done(res):
                placed, unplaced = res
                msg = "{} note{} written into the Doc.".format(placed, "" if placed == 1 else "s")
                if unplaced:
                    msg += "  {} could not find their line and sit at the top.".format(len(unplaced))
                messagebox.showinfo("Google Docs", msg)
            self._ep_gdoc_bg(_work, _done)

        def _clear():
            def _work():
                return gdocs.clear_notes(gdocs.get_credentials(), gd["id"])
            self._ep_gdoc_bg(_work, lambda n: messagebox.showinfo(
                "Google Docs", "{} note{} removed from the Doc.".format(n, "" if n == 1 else "s")))

        def _continue():
            try:
                win.destroy()
            except tk.TclError:
                pass
            if then is not None:
                then()

        if then is not None:
            self._btn(nav, "CONTINUE  \u2192", _continue, color=ACCENT).pack(side="right")
        else:
            self._btn(nav, "CLOSE", _continue).pack(side="right")
        self._btn(nav, "CLEAR NOTES IN DOC", _clear, small=True).pack(side="left")
        if findings:
            self._btn(nav, "WRITE NOTES INTO DOC", _write, small=True, color=ACCENT
                      ).pack(side="left", padx=(8, 0))
        self._center_dialog(win, min_w=760, min_h=420)

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
        ep = self._ep_bound(proj.get("episode_file"))
        if not ep or not session.get("token"):
            return
        token = session["token"]
        media = [EP.rel(ep, m) for m in session.get("media", []) if m]
        sess  = EP.rel(ep, session.get("_file_path"))
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
        src["session"] = sess
        for m in media:
            self._ep_ensure_media(ep, m, "audio")
            if m not in src["audio"]:
                src["audio"].append(m)
        src["audio"].sort()
        if session.get("_file_path"):
            ap = os.path.abspath(session["_file_path"])
            if ap not in ep.setdefault("sessions", []):
                ep["sessions"].append(ap)
        try:
            EP.save(ep)
        except Exception:
            pass

    def _ep_absorb_s2s(self, session_path):
        """Script -> Session saved: remember the session, and take the pool's
        token assignments into the sources -- a camera assigned to a token
        joins that token's source (leaving any other), a recording assigned
        to a token gives its source that token."""
        ep = getattr(self, "_ep", None)
        script = getattr(self, "_script_path", None)
        if not ep or not self._ep_owns(ep, "script", script):
            return
        ep.setdefault("links", {})["s2s_session"] = EP.rel(ep, session_path)
        pool = getattr(self, "_pool", None)
        rows = [(r["path"], r["var"].get()) for r in getattr(pool, "_rows", [])]
        from utils import is_video
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
                        if other_name != name and r in other.get(kind, []):
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
                    if other_name != name and r in other.get(kind, []):
                        other[kind].remove(r)
                src[kind].append(r)
                src[kind].sort()
                src["user_made"] = True         # the user placed it: a rescan keeps it
        try:
            EP.save(ep)
        except Exception:
            pass

    def _ep_absorb_aaf(self, setup_path):
        """AAF setup saved: remember it, and keep each source's cameras,
        sync and track row in the episode so a fresh AAF export of the
        same session opens synced even without the setup file."""
        ep = getattr(self, "_ep", None)
        aaf = getattr(self, "_aaf_path", None)
        if not ep or not self._ep_owns(ep, "aaf", aaf):
            return
        ep.setdefault("links", {})["aaf_setup"] = EP.rel(ep, setup_path)
        try:
            data = self._aaf_build_setup_data()
        except Exception:
            data = None
        if data:
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
                        for other_name, other in ep["sources"].items():
                            if other_name != name:
                                other["video"] = [v for v in other.get("video", [])
                                                  if v not in vids]
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
        try:
            EP.save(ep)
        except Exception:
            pass

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
        self._pq_load_project({"title": ep["title"], "sessions": list(ep.get("sessions", []))},
                              None)
        # Saves go back into the episode file (sessions + title), not over it.
        self._pq_project["episode_file"] = ep["file_path"]

    def _ep_launch_s2s(self):
        if not self._ep_ready():
            return
        ep = self._ep
        links = ep.setdefault("links", {})
        sess = EP.absp(ep, links.get("s2s_session"))
        if sess and os.path.isfile(sess):
            self._open_session(sess)
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
        assignments = {}
        for src in ep["sources"].values():
            tok = src.get("token")
            vo = src.get("vo_part")
            if tok and tok in known:
                val = tok
            elif vo and vo in part_names:
                # The part this narrator recording was assigned to last time.
                val = "VO: " + vo
            elif not tok and not src.get("session") and len(vo_parts) == 1:
                # No interview token, no transcript session: a narrator
                # recording.  With one narrated part there is no choice.
                val = "VO: " + next(iter(vo_parts))
            else:
                val = "— unassigned —"
            for r in src.get("audio", []) + src.get("video", []):
                assignments[EP.absp(ep, r)] = val
        self._pending_setup = {"assignments": assignments}
        self.workflow = "script_session"
        self._load_script(script)

    def _ep_launch_aaf(self):
        if not self._ep_ready():
            return
        ep = self._ep
        links = ep.setdefault("links", {})
        setup = EP.absp(ep, links.get("aaf_setup"))
        if setup and os.path.isfile(setup):
            self._open_session(setup)
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
