"""AAF → XML workflow — extracted from main.py as a mixin.

Every method here runs with ``self`` bound to the App instance, so shared
helpers (_btn, _ui, _center_dialog, _mark_saved, _clear, _home, _reset,
_section, _scroll_frame, _tooltip) and shared App state resolve normally via
inheritance.  App is declared ``class App(AafWorkflowMixin, ...)``.

Pure code-move from main.py: every method body is identical to its original.
"""
import os
import re
import json
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

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

from config import *
from config import _SANS
from utils import (basename, is_video, is_audio, is_media,
                   MEDIA_EXTS, VIDEO_EXTS, run_hidden)
from parsers import (get_clip_base_name, parse_aaf_session,
                     match_source_to_video, detect_sync_offset,
                     verify_sync_at_offset)
from gui_components import _FlatDropdown, _FlatProgressBar
from sync_preview import SyncPreviewDialog
import engines

# Sequence preset table: (display_name, width, height, fps)
# width/height/fps = None means "detect from media" or "custom — leave fields as-is"
_AAF_SEQ_PRESETS = [
    ("Detect from media",  None,  None,   None),
    ("1080p  23.976 fps",  1920,  1080,  23.976),
    ("1080p  24 fps",      1920,  1080,  24.0),
    ("1080p  25 fps",      1920,  1080,  25.0),
    ("1080p  29.97 fps",   1920,  1080,  29.97),
    ("1080p  30 fps",      1920,  1080,  30.0),
    ("4K UHD  23.976 fps", 3840,  2160,  23.976),
    ("4K UHD  24 fps",     3840,  2160,  24.0),
    ("4K UHD  25 fps",     3840,  2160,  25.0),
    ("4K UHD  29.97 fps",  3840,  2160,  29.97),
    ("4K UHD  30 fps",     3840,  2160,  30.0),
    ("720p  29.97 fps",    1280,   720,  29.97),
    ("720p  30 fps",       1280,   720,  30.0),
    ("Custom",             None,  None,   None),
]
_AAF_PRESET_LOOKUP = {p[0]: p[1:] for p in _AAF_SEQ_PRESETS}


class AafWorkflowMixin:
    """All AAF → XML workflow methods, mixed into App."""

    def _aaf_step1(self):
        self._clear()
        self._aaf_step1_next_added  = False
        self._section("STEP 1 — LOAD PRO TOOLS AAF EXPORT  (AAF → XML)")

        # ── Nav pinned to bottom first so it's always visible ─────────────────
        nav = tk.Frame(self.body, bg=BG)
        nav.pack(side="bottom", fill="x", pady=(8,0))
        self._btn(nav, "\u2190 HOME", self._home).pack(side="left")
        self._aaf_step1_nav = nav   # NEXT button appended here once AAF is loaded

        # ── Scrollable content area fills remaining space ─────────────────────
        sf = self._scroll_frame(self.body)

        card = tk.Frame(sf, bg=SURF,
                        highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x")
        if HAS_DND:
            card.drop_target_register(DND_FILES)
            card.dnd_bind("<<Drop>>",
                          lambda e: self._aaf_load(e.data.strip().strip("{}")))

        inner = tk.Frame(card, bg=SURF)
        inner.pack(padx=40, pady=30)
        self._aaf_s1 = tk.Label(inner,
                             text="Drop .aaf file here  or  click below"
                                  if HAS_DND else "Click below to open AAF file",
                             font=FB, bg=SURF, fg=SUB,
                             wraplength=720, justify="center")
        self._aaf_s1.pack(pady=(0,14))

        tk.Label(inner,
                 text="Export from Pro Tools: File \u2192 Export \u2192 As AAF/OMF\n"
                      "Format: AAF  \u00b7  Audio: Link to Source Media  \u00b7  Include all (or selected) tracks",
                 font=FB, bg=SURF, fg=SUB, justify="center").pack(pady=(0,14))

        self._btn(inner, "OPEN AAF FILE (.aaf)",
                  lambda: self._aaf_load(
                      filedialog.askopenfilename(
                          filetypes=[("AAF","*.aaf"),("All","*.*")]))).pack()

        # If we already have parsed AAF data (e.g. user pressed Back from Step 2),
        # restore the status label and show the NEXT button immediately — no reload needed.
        if self._aaf_data:
            total_clips = sum(len(t["clips"]) for t in self._aaf_data["tracks"])
            self._aaf_s1.config(
                text="\u2713  {}  \u00b7  {} tracks  \u00b7  {} clips  \u00b7  {} sources".format(
                    self._aaf_data["session_name"],
                    len(self._aaf_data["tracks"]),
                    total_clips,
                    len(getattr(self, "_aaf_sources", []))),
                fg=SUCCESS)
            self._btn(nav, "NEXT  \u2192  ASSIGN VIDEO",
                      self._aaf_step2, color=ACCENT).pack(side="right")
            self._aaf_step1_next_added = True

    def _aaf_load(self, path):
        def _clear_pending_restore():
            # _open_session arms these for THIS load.  If the load fails
            # they must not survive \u2014 a stale _pending_aaf_setup would
            # silently restore the wrong setup onto the next AAF the user
            # opens, and a stale _pending_aaf_saved_path would route that
            # AAF's quick-saves into the previous setup's file.
            self._pending_aaf_setup      = None
            self._pending_aaf_saved_path = None
            self._pending_mark_saved     = False

        if not path or not os.path.isfile(path):
            _clear_pending_restore()
            return
        if hasattr(self, "_aaf_s1"):
            self._aaf_s1.config(text="Parsing AAF\u2026", fg=SUB)
            self.update_idletasks()
        try:
            parsed = parse_aaf_session(path)
        except Exception as e:
            _clear_pending_restore()
            messagebox.showerror("AAF Error", str(e)); return

        if not parsed["tracks"]:
            _clear_pending_restore()
            messagebox.showerror("No Tracks",
                "No audio tracks found in this AAF.\n"
                "Make sure the session has track EDL data and was exported\n"
                "with 'Link to Source Media' (not embedded audio).")
            return

        self._aaf_data = parsed
        self._aaf_path = os.path.abspath(path)
        self.workflow  = "aaf_xml"   # tells save/load routing which flow is active
        # Fresh AAF — no chosen save file yet, so the first Ctrl+S prompts
        # for a location (Save As).  Overwritten by _aaf_restore_setup when
        # opening a previously-saved setup.
        self._aaf_saved_path = None
        # Point the engine cache dir at a .pb_cache folder next to the AAF so
        # detect_rx_offset results persist across builds of the same session.
        engines._cache_dir = os.path.join(os.path.dirname(self._aaf_path), ".pb_cache")
        total_clips  = sum(len(t["clips"]) for t in parsed["tracks"])

        # Collect unique source base names and clip counts
        sources     = {}
        clip_counts = {}
        for track in parsed["tracks"]:
            for clip in track["clips"]:
                base = get_clip_base_name(clip["clip_name"])
                if base is None:
                    continue
                sources.setdefault(base, set()).add(track["name"])
                clip_counts[base] = clip_counts.get(base, 0) + 1
        self._aaf_sources            = sorted(sources.keys())
        self._aaf_source_tracks      = sources     # base_name -> set of track names
        self._aaf_source_clip_counts = clip_counts # base_name -> total clip count

        # Prune per-source state left over from a previously-opened AAF.
        # These dicts are keyed by source base and are DELIBERATELY
        # persistent across row rebuilds / page-mode toggles (so user
        # assignments survive those), so they are NOT in the rebuild
        # reset block — but a NEW AAF load must drop entries for sources
        # that no longer exist, or they leak into this session's saved
        # setup and can mis-restore onto a source the new AAF lacks.
        # Only non-current keys are dropped, so same-AAF reloads are a
        # no-op and current assignments are untouched.
        _live = set(self._aaf_sources)
        for _dname in (
                "_aaf_source_file_vars", "_aaf_source_extra_vars",
                "_aaf_source_sync_vars", "_aaf_source_syncaudio_vars",
                "_aaf_source_offset_vars", "_aaf_source_sync_label_vars",
                "_aaf_source_audio_disp_vars", "_aaf_source_slot_counts",
                "_aaf_source_sync_cand_idx", "_aaf_source_sync_candidates",
                "_aaf_source_has_slate_vars",
                "_aaf_sync_state_vars", "_aaf_needs_sync_vars",
                "_aaf_sync_locked_vars"):
            _d = getattr(self, _dname, None)
            if isinstance(_d, dict):
                for _stale in [k for k in _d if k not in _live]:
                    _d.pop(_stale, None)
        # Reset column widths so they recompute from new content on next visit
        if hasattr(self, "_aaf_col_widths"):
            del self._aaf_col_widths
        self._aaf_col_widths_vid_count = (-1, -1)
        # Reset hidden sources and sort for new AAF
        self._aaf_hidden_sources = set()
        self._aaf_sort_col = "name"   # default: source clip A→Z (▲ visible from start)
        self._aaf_sort_dir = "asc"

        if hasattr(self, "_aaf_s1"):
            self._aaf_s1.config(
                text="\u2713  {}  \u00b7  {} tracks  \u00b7  {} clips  \u00b7  {} sources".format(
                    parsed["session_name"], len(parsed["tracks"]),
                    total_clips, len(self._aaf_sources)),
                fg=SUCCESS)

        self.seq_name.set(parsed["session_name"])
        # If we're on the step-1 screen, add the NEXT button; otherwise advance directly.
        if hasattr(self, "_aaf_step1_nav") and not getattr(self, "_aaf_step1_next_added", False):
            self._btn(self._aaf_step1_nav, "NEXT  →  ASSIGN VIDEO",
                      self._aaf_step2, color=ACCENT).pack(side="right")
            self._aaf_step1_next_added = True
        else:
            self._ui(self._aaf_step2)

    def _aaf_step2(self):
        # Preserve pool state so Back → Next doesn't lose work
        _saved_vpaths = list(getattr(self, "_aaf_video_paths", []))
        _saved_apaths = list(getattr(self, "_aaf_audio_paths", []))
        self._clear()
        # Header row: step title on left, UNDO/REDO/CLEAN UP grouped on right
        _hdr = tk.Frame(self.body, bg=BG)
        _hdr.pack(fill="x", pady=(20, 6))
        tk.Frame(_hdr, bg=ACCENT, width=3, height=20).pack(side="left", padx=(0, 10))
        tk.Label(_hdr, text="STEP 2 \u2014 ASSIGN VIDEO MEDIA",
                 font=FL, bg=BG, fg=TEXT).pack(side="left", anchor="w")
        self._btn(_hdr, "CLEAN UP",
                  self._aaf_remove_unassigned, small=True).pack(side="right")
        self._btn(_hdr, "\u21b7 REDO",
                  self._aaf_sync_redo, small=True).pack(side="right", padx=(0, 4))
        self._btn(_hdr, "\u21b6 UNDO",
                  self._aaf_sync_undo, small=True).pack(side="right", padx=(0, 4))
        # Keyboard bindings (idempotent — safe to re-bind each visit)
        self.bind_all("<Control-z>", lambda e: self._aaf_sync_undo())
        self.bind_all("<Control-y>", lambda e: self._aaf_sync_redo())

        total_clips  = sum(len(t["clips"]) for t in self._aaf_data["tracks"])
        tk.Label(self.body,
                 text="{} clips \u00b7 {} tracks \u00b7 {} sources \u2014 "
                      "add video files below, then assign each source to its video.".format(
                          total_clips, len(self._aaf_data["tracks"]),
                          len(self._aaf_sources)),
                 font=FB, bg=BG, fg=SUB).pack(anchor="w", pady=(0, 10))

        # ── Progress bar and nav pinned to bottom so they're always visible ────
        self._aaf_prog_frame = tk.Frame(self.body, bg=BG)
        self._aaf_prog_frame.pack(side="bottom", fill="x", pady=(6, 0))
        self._aaf_prog_lbl = tk.Label(self._aaf_prog_frame, text="", font=FB,
                                      bg=BG, fg=SUB, anchor="w")
        self._aaf_prog_lbl.pack(fill="x", pady=(0, 3))
        self._aaf_prog_bar = _FlatProgressBar(self._aaf_prog_frame, height=4)
        self._aaf_prog_bar.pack(fill="x")
        self._aaf_prog_frame.pack_forget()   # hidden until build starts

        nav = tk.Frame(self.body, bg=BG)
        nav.pack(side="bottom", fill="x", pady=(8,0))
        self._aaf_build_btn = self._btn(nav, "BUILD XML  \u2192", self._aaf_build,
                                        color=ACCENT)
        self._aaf_build_btn.pack(side="right")

        # ── Scrollable content area fills remaining space ─────────────────────
        sf = self._scroll_frame(self.body)

        # ── Video file pool (collapsible) ──────────────────────────────────────
        pool_frame = tk.Frame(sf, bg=SURF,
                              highlightbackground=BORDER, highlightthickness=1)
        pool_frame.pack(fill="x", pady=(0,8), padx=2)

        v_body = tk.Frame(pool_frame, bg=SURF)   # collapsible content
        v_open = [False]

        ph = tk.Frame(pool_frame, bg=SURF, cursor="hand2")
        ph.pack(fill="x", padx=12, pady=(10,4))

        v_arrow = tk.Label(ph, text="\u25b6",
                           font=FB, bg=SURF, fg=ACCENT, cursor="hand2")
        v_arrow.pack(side="left", padx=(0, 6))
        tk.Label(ph, text="VIDEO FILES", font=FL, bg=SURF, fg=ACCENT,
                 cursor="hand2").pack(side="left")
        self._aaf_count_lbl = tk.Label(ph, text="0 files", font=FB, bg=SURF, fg=SUB)
        self._aaf_count_lbl.pack(side="right")
        self._btn(ph, "\u21bb REFRESH", self._aaf_refresh_pool,
                  small=True).pack(side="right", padx=(0, 4))
        self._btn(ph, "\u26d3 JOIN SPLIT", self._aaf_join_split_dialog,
                  small=True).pack(side="right", padx=(0, 4))
        self._btn(ph, "+ BROWSE FOLDER", self._aaf_browse_folder,
                  small=True).pack(side="right", padx=(0, 4))
        self._btn(ph, "+ BROWSE FILES",  self._aaf_browse_files,
                  small=True).pack(side="right", padx=(0, 4))

        def _toggle_vpool(arr=v_arrow, body=v_body, state=v_open):
            if state[0]:
                body.pack_forget()
                arr.config(text="\u25b6")
                state[0] = False
            else:
                body.pack(fill="x")
                arr.config(text="\u25bc")
                state[0] = True

        for w in (ph, v_arrow):
            w.bind("<Button-1>", lambda e: _toggle_vpool())

        # Body starts collapsed
        self._aaf_video_paths = []
        self._aaf_video_rows  = {}   # full path → row Frame (identity, not label text)
        self._aaf_file_list   = tk.Frame(v_body, bg=SURF)
        self._aaf_file_list.pack(fill="x", padx=12)

        self._aaf_status_lbl = tk.Label(v_body, text="", font=FB, bg=SURF, fg=SUB)
        self._aaf_status_lbl.pack(anchor="w", padx=12, pady=(4, 10))

        if HAS_DND:
            for w in [pool_frame, ph]:
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>",
                           lambda e: self._aaf_add_video_batch(engines.parse_dnd(e.data)))

        # ── Reference audio pool (collapsible) ────────────────────────────────
        apool_frame = tk.Frame(sf, bg=SURF,
                               highlightbackground=BORDER, highlightthickness=1)
        apool_frame.pack(fill="x", pady=(0, 8), padx=2)

        a_body = tk.Frame(apool_frame, bg=SURF)   # collapsible content
        a_open = [False]

        aph = tk.Frame(apool_frame, bg=SURF, cursor="hand2")
        aph.pack(fill="x", padx=12, pady=(10, 4))

        a_arrow = tk.Label(aph, text="\u25b6",
                           font=FB, bg=SURF, fg=ACCENT, cursor="hand2")
        a_arrow.pack(side="left", padx=(0, 6))
        tk.Label(aph, text="REFERENCE AUDIO FILES", font=FL, bg=SURF, fg=ACCENT,
                 cursor="hand2").pack(side="left")
        tk.Label(aph, text="  (camera / scratch audio for dual-system sync \u2014 optional)",
                 font=FB, bg=SURF, fg=SUB).pack(side="left")
        self._aaf_audio_count_lbl = tk.Label(aph, text="0 files", font=FB,
                                             bg=SURF, fg=SUB)
        self._aaf_audio_count_lbl.pack(side="right")
        self._btn(aph, "\u21bb REFRESH", self._aaf_refresh_pool,
                  small=True).pack(side="right", padx=(0, 4))
        self._btn(aph, "\u26d3 MIX TO REF", self._aaf_mix_reference_dialog,
                  small=True).pack(side="right", padx=(0, 4))
        self._btn(aph, "+ BROWSE FOLDER", self._aaf_browse_audio_folder,
                  small=True).pack(side="right", padx=(0, 4))
        self._btn(aph, "+ BROWSE FILES",  self._aaf_browse_audio_files,
                  small=True).pack(side="right", padx=(0, 4))

        def _toggle_apool(arr=a_arrow, body=a_body, state=a_open):
            if state[0]:
                body.pack_forget()
                arr.config(text="\u25b6")
                state[0] = False
            else:
                body.pack(fill="x")
                arr.config(text="\u25bc")
                state[0] = True

        for w in (aph, a_arrow):
            w.bind("<Button-1>", lambda e: _toggle_apool())

        # Body starts collapsed
        self._aaf_audio_paths     = []
        self._aaf_audio_rows      = {}   # full path → row Frame (sister of _aaf_video_rows)
        self._aaf_audio_file_list = tk.Frame(a_body, bg=SURF)
        self._aaf_audio_file_list.pack(fill="x", padx=12)

        if HAS_DND:
            for w in [apool_frame, aph]:
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>",
                           lambda e: self._aaf_add_audio_batch(engines.parse_dnd(e.data)))

        # ── Per-source assignment dropdowns ────────────────────────────────────
        assign_frame = tk.Frame(sf, bg=SURF,
                                highlightbackground=BORDER, highlightthickness=1)
        assign_frame.pack(fill="x", pady=(0,8), padx=2)

        ah = tk.Frame(assign_frame, bg=SURF)
        ah.pack(fill="x", padx=12, pady=(10, 4))
        # (no redundant label before tabs)

        # ── Initialise page-mode var NOW so _make_tab can read it ─────────────
        if not hasattr(self, "_aaf_page_mode"):
            self._aaf_page_mode = tk.StringVar(value="assign")
        if not hasattr(self, "_aaf_sync_state_vars"):
            self._aaf_sync_state_vars = {}
        if not hasattr(self, "_aaf_sync_dot_labels"):
            self._aaf_sync_dot_labels = {}   # base → dot Label widget
        if not hasattr(self, "_aaf_qa_btns"):
            self._aaf_qa_btns = {}           # base → QA button widget
        if not hasattr(self, "_aaf_qa_playing"):
            self._aaf_qa_playing = None      # base currently being QA-played
        if not hasattr(self, "_aaf_needs_sync_vars"):
            self._aaf_needs_sync_vars = {}   # base → BooleanVar: "show on sync page"
        if not hasattr(self, "_aaf_sync_locked_vars"):
            self._aaf_sync_locked_vars = {}  # base → BooleanVar: sync frozen
        if not hasattr(self, "_aaf_ck_lbl_map"):
            self._aaf_ck_lbl_map = {}        # base → checkbox Label (for drag-select)
        if not hasattr(self, "_aaf_ck_container_map"):
            self._aaf_ck_container_map = {}  # base → row container Frame
        if not hasattr(self, "_aaf_drag_sync"):
            self._aaf_drag_sync = None       # drag state: {"start_idx", "target", "last_idx"}
        if not hasattr(self, "_aaf_confirm_btns"):
            self._aaf_confirm_btns = {}      # base → confirm Label widget
        if not hasattr(self, "_aaf_lock_btns"):
            self._aaf_lock_btns = {}         # base → lock Label widget
        if not hasattr(self, "_aaf_hidden_sources"):
            self._aaf_hidden_sources = set()  # sources hidden by user (reversible)

        # ── Column order (persists across rebuilds; widths computed in _rebuild) ─
        if not hasattr(self, "_aaf_col_order"):
            self._aaf_col_order = ["video", "tracks"]  # Source|Video|Track|Sync|hide

        # ── Page toggle: ASSIGN  /  SYNC ──────────────────────────────────────
        tab_frame = tk.Frame(ah, bg=SURF)
        tab_frame.pack(side="left", padx=(4, 0))
        self._aaf_assign_tab_btn = None
        self._aaf_sync_tab_btn   = None

        def _make_tab(parent, label, mode):
            active  = self._aaf_page_mode.get() == mode
            bg      = ACCENT if active else SURF3
            fg      = BG     if active else SUB
            b = tk.Label(parent, text=label, font=FB, bg=bg, fg=fg,
                         cursor="hand2", padx=10, pady=2, bd=0,
                         highlightbackground=BORDER, highlightthickness=1)
            def _click(e, m=mode):
                self._aaf_page_mode.set(m)
                self._rebuild_aaf_source_rows()
            b.bind("<ButtonRelease-1>", _click)
            return b

        self._aaf_assign_tab_btn = _make_tab(tab_frame, "SOURCE ASSIGN", "assign")
        self._aaf_assign_tab_btn.pack(side="left")
        self._aaf_sync_tab_btn = _make_tab(tab_frame, "SOURCE SYNC", "sync")
        self._aaf_sync_tab_btn.pack(side="left", padx=(2, 0))

        # Undo / Redo — only meaningful on the SYNC page
        if not hasattr(self, "_sync_undo_stack"):
            self._sync_undo_stack = []
            self._sync_redo_stack = []

        # Column headers (rebuilt by _rebuild_aaf_source_rows)
        self._aaf_col_hdr_frame = tk.Frame(assign_frame, bg=SURF)
        self._aaf_col_hdr_frame.pack(fill="x", padx=12, pady=(2, 0))

        self._aaf_assign_frame = tk.Frame(assign_frame, bg=SURF)
        self._aaf_assign_frame.pack(fill="x", padx=12, pady=(0,10))
        # Preserve assignment state across back-navigation; only init on first visit
        if not hasattr(self, "_aaf_source_file_vars"):
            self._aaf_source_file_vars       = {}
        if not hasattr(self, "_aaf_source_sync_vars"):
            self._aaf_source_sync_vars       = {}
        if not hasattr(self, "_aaf_source_syncaudio_vars"):
            self._aaf_source_syncaudio_vars  = {}
        if not hasattr(self, "_aaf_source_audio_disp_vars"):
            self._aaf_source_audio_disp_vars = {}
        if not hasattr(self, "_aaf_source_offset_vars"):
            self._aaf_source_offset_vars     = {}
        if not hasattr(self, "_aaf_source_sync_label_vars"):
            self._aaf_source_sync_label_vars = {}
        if not hasattr(self, "_aaf_source_slot_counts"):
            self._aaf_source_slot_counts = {}   # base → int (default 1)
        if not hasattr(self, "_aaf_source_extra_vars"):
            self._aaf_source_extra_vars = {}    # base → list of StringVars (slots 2..N)
        self._aaf_sync_btns = {}   # always reset — widget refs are stale after _clear()
        # Close any sync preview dialogs left open from a previous visit
        for _dlg in getattr(self, "_aaf_sync_previews", {}).values():
            try: _dlg.close()
            except Exception: pass
        self._aaf_sync_previews = {}

        self._rebuild_aaf_source_rows()

        # ── Settings ───────────────────────────────────────────────────────────
        settings_frame = tk.Frame(sf, bg=SURF,
                                  highlightbackground=BORDER, highlightthickness=1)
        settings_frame.pack(fill="x", pady=(0,8), padx=2)

        sh = tk.Frame(settings_frame, bg=SURF)
        sh.pack(fill="x", padx=12, pady=(10,6))
        tk.Label(sh, text="SETTINGS", font=FL, bg=SURF, fg=ACCENT).pack(side="left")

        row_grp = tk.Frame(settings_frame, bg=SURF)
        row_grp.pack(anchor="w", padx=12, pady=(0,4))
        tk.Label(row_grp, text="Video tracks:", font=FB, bg=SURF, fg=TEXT,
                 width=18, anchor="w").pack(side="left")
        self._aaf_group_var = tk.StringVar(value="single")
        for val, lbl in [("single",    "Consolidate to one video track"),
                         ("by_source", "Group by source video file"),
                         ("by_track",  "Group by audio track")]:
            tk.Radiobutton(row_grp, text=lbl, variable=self._aaf_group_var, value=val,
                           font=FB, bg=SURF, fg=TEXT, activebackground=SURF,
                           selectcolor=BG).pack(side="left", padx=(0,14))

        # ── Sequence settings (preset + editable resolution/fps) ──────────────
        if not hasattr(self, "_aaf_preset_var"):
            self._aaf_preset_var = tk.StringVar(value="Detect from media")
        if not hasattr(self, "_aaf_fps_var"):
            self._aaf_fps_var = tk.StringVar(value="29.97")
        if not hasattr(self, "_aaf_seq_w_var"):
            self._aaf_seq_w_var = tk.StringVar(value="")
        if not hasattr(self, "_aaf_seq_h_var"):
            self._aaf_seq_h_var = tk.StringVar(value="")

        def _on_preset_change(*_):
            name = self._aaf_preset_var.get()
            # Fixed preset?
            if name in _AAF_PRESET_LOOKUP:
                pw, ph, pfps = _AAF_PRESET_LOOKUP[name]
                if pw is not None:
                    self._aaf_seq_w_var.set(str(pw))
                    self._aaf_seq_h_var.set(str(ph))
                    self._aaf_fps_var.set(
                        "{:.3f}".format(pfps).rstrip("0").rstrip("."))
                elif name == "Detect from media":
                    self._aaf_seq_w_var.set("")
                    self._aaf_seq_h_var.set("")
                    self._aaf_schedule_detect_fps()
                # "Custom" — leave fields untouched so user can type freely
                return
            # Detected-from-source entry?
            detected = getattr(self, "_aaf_detected_presets", {})
            if name in detected:
                pw, ph, pfps = detected[name]
                self._aaf_seq_w_var.set(str(pw))
                self._aaf_seq_h_var.set(str(ph))
                self._aaf_fps_var.set(
                    "{:.3f}".format(pfps).rstrip("0").rstrip("."))

        self._aaf_preset_var.trace_add("write", _on_preset_change)

        row_preset = tk.Frame(settings_frame, bg=SURF)
        row_preset.pack(anchor="w", padx=12, pady=(0, 4))
        tk.Label(row_preset, text="Sequence Preset:", font=FB, bg=SURF, fg=TEXT,
                 width=18, anchor="w").pack(side="left")
        self._aaf_preset_dd = _FlatDropdown(row_preset, self._aaf_preset_var,
                                            [p[0] for p in _AAF_SEQ_PRESETS],
                                            font=FB, width=26)
        self._aaf_preset_dd.pack(side="left")

        row_fps = tk.Frame(settings_frame, bg=SURF)
        row_fps.pack(anchor="w", padx=12, pady=(0, 10))
        tk.Label(row_fps, text="Resolution / FPS:", font=FB, bg=SURF, fg=TEXT,
                 width=18, anchor="w").pack(side="left")
        tk.Entry(row_fps, textvariable=self._aaf_seq_w_var,
                 font=FB, bg=SURF2, fg=TEXT, insertbackground=TEXT,
                 relief="flat", width=5).pack(side="left")
        tk.Label(row_fps, text=" × ", font=FB, bg=SURF, fg=SUB).pack(side="left")
        tk.Entry(row_fps, textvariable=self._aaf_seq_h_var,
                 font=FB, bg=SURF2, fg=TEXT, insertbackground=TEXT,
                 relief="flat", width=5).pack(side="left")
        tk.Label(row_fps, text="   FPS:", font=FB, bg=SURF, fg=TEXT).pack(side="left", padx=(10, 4))
        tk.Entry(row_fps, textvariable=self._aaf_fps_var,
                 font=FB, bg=SURF2, fg=TEXT, insertbackground=TEXT,
                 relief="flat", width=8).pack(side="left")
        tk.Label(row_fps, text="  (auto-filled from preset; edit to override)",
                 font=FB, bg=SURF, fg=SUB).pack(side="left")

        # ── Stereo mix (optional full-sequence audio track) ───────────────────
        mix_frame = tk.Frame(sf, bg=SURF,
                             highlightbackground=BORDER, highlightthickness=1)
        mix_frame.pack(fill="x", pady=(0, 8), padx=2)

        mh = tk.Frame(mix_frame, bg=SURF)
        mh.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(mh, text="STEREO MIX", font=FL, bg=SURF, fg=ACCENT).pack(side="left")
        tk.Label(mh, text="  (optional — full-sequence stereo mix that syncs to picture)",
                 font=FB, bg=SURF, fg=SUB).pack(side="left")

        if not hasattr(self, "_aaf_mix_var"):
            self._aaf_mix_var = tk.StringVar(value="")
        # If audio files were prefetched before this step rendered, scan them
        # now for a FOR VID file and pre-fill the mix field if it's still empty.
        if not self._aaf_mix_var.get():
            for _p in self._aaf_audio_paths:
                if "FOR VID" in os.path.basename(_p).upper():
                    self._aaf_mix_var.set(_p)
                    break

        mix_body = tk.Frame(mix_frame, bg=SURF)
        mix_body.pack(fill="x", padx=12, pady=(0, 10))

        mix_disp = tk.Label(mix_body, textvariable=self._aaf_mix_var,
                            font=FB, bg=SURF2, fg=TEXT, anchor="w",
                            relief="flat", padx=8, pady=4)
        mix_disp.pack(fill="x", pady=(0, 6))

        mix_btn_row = tk.Frame(mix_body, bg=SURF)
        mix_btn_row.pack(anchor="w")
        self._btn(mix_btn_row, "+ BROWSE MIX FILE", self._aaf_browse_mix,
                  small=True).pack(side="left", padx=(0, 8))
        self._btn(mix_btn_row, "✕ CLEAR", lambda: self._aaf_mix_var.set(""),
                  small=True).pack(side="left")

        if HAS_DND:
            mix_frame.drop_target_register(DND_FILES)
            mix_frame.dnd_bind("<<Drop>>",
                               lambda e: self._aaf_set_mix(engines.parse_dnd(e.data)))

        # ── Camera audio option ───────────────────────────────────────────────
        if not hasattr(self, "_aaf_cam_audio_var"):
            self._aaf_cam_audio_var = tk.BooleanVar(value=False)
        cam_row = tk.Frame(sf, bg=SURF)
        cam_row.pack(fill="x", padx=14, pady=(0, 8))
        tk.Checkbutton(cam_row, text="Include camera audio from video files",
                       variable=self._aaf_cam_audio_var,
                       font=FB, bg=SURF, fg=TEXT, selectcolor=SURF2,
                       activebackground=SURF, activeforeground=TEXT,
                       relief="flat", bd=0).pack(side="left")

        # Restore pool contents: back-navigation takes priority over prefetch
        if _saved_vpaths or _saved_apaths:
            if _saved_vpaths: self._aaf_add_video_batch(_saved_vpaths)
            if _saved_apaths: self._aaf_add_audio_batch(_saved_apaths)
        else:
            prefetch = getattr(self, "_prefetch_aaf_media", [])
            if prefetch:
                vp = [p for p in prefetch if is_video(p)]
                ap = [p for p in prefetch if is_audio(p)]
                if vp: self._aaf_add_video_batch(vp)
                if ap: self._aaf_add_audio_batch(ap)

    # Palette of subtle background tints — one per assigned video file.
    # Unassigned rows use a warm amber tint so they stand out immediately.
    _ASSIGN_PALETTE  = ["#162b16", "#16162b", "#2b1616",
                        "#2b2516", "#162b2b", "#261626"]
    _UNASSIGNED_ROW  = "#2a1a08"

    def _aaf_pool_label(self, path, pool):
        """Collision-free display label for `path` against a given media
        pool (list of full paths).  Used as the STABLE 1:1 key for the
        path everywhere the pool is shown or resolved (dropdown options,
        the assignment picker, and every {label: path} resolution map
        for sync / align / QA / build).

        The bare basename is NOT a safe key: multicam shoots routinely
        name files identically on every card (C0001.MP4 on card A *and*
        card B; scratch.wav on every cam), and the pool dedups only by
        full path — so two distinct files share a basename.  A
        basename-keyed lookup then collapses them last-one-wins, and a
        source assigned card A's clip silently syncs/exports against
        card B's.  When (and only when) a basename collides in the pool,
        this disambiguates by parent folder, then by a stable index, so
        the returned label maps back to exactly one path.  With no
        collision the label IS the basename, so behaviour is unchanged
        for the common case."""
        bn = basename(path)
        twins = [p for p in pool if basename(p) == bn]
        if len(twins) <= 1:
            return bn
        parent = os.path.basename(os.path.dirname(path)) or os.path.dirname(path)
        same_parent = [p for p in twins
                       if (os.path.basename(os.path.dirname(p))
                           or os.path.dirname(p)) == parent]
        if len(same_parent) > 1:
            # Parent folders also collide — fall back to a stable index
            # (position among the twins, which is deterministic for a
            # given pool ordering).
            return "{}  ·  {} ({})".format(bn, parent, twins.index(path) + 1)
        return "{}  ·  {}".format(bn, parent)

    def _aaf_vid_label(self, path):
        """Collision-free display label for a video-pool path (see _aaf_pool_label)."""
        return self._aaf_pool_label(path, getattr(self, "_aaf_video_paths", []))

    def _aaf_aud_label(self, path):
        """Collision-free display label for an audio-pool path.

        Sister of _aaf_vid_label.  Without this, the reference-audio
        dropdown showed bare basenames and the on-change trace re-resolved
        the display string to a full path via a {basename: path} dict —
        when two pool files shared a basename (e.g. `scratch.wav` on every
        camera card) the dict collapsed last-one-wins and every source
        that picked that filename pointed at the SAME physical file.  The
        canonical full path is stored in _aaf_source_syncaudio_vars, so
        making the display label injective is what actually disambiguates
        the user's choice end-to-end."""
        return self._aaf_pool_label(path, getattr(self, "_aaf_audio_paths", []))

    def _rebuild_aaf_source_rows(self):
        """Rebuild per-source rows; layout depends on current page mode."""
        for w in self._aaf_assign_frame.winfo_children():
            w.destroy()
        # Clear per-row widget refs — repopulated below
        if not hasattr(self, "_aaf_vid_btn_labels"):   self._aaf_vid_btn_labels   = {}
        if not hasattr(self, "_aaf_ctr_n_labels"):     self._aaf_ctr_n_labels     = {}
        if not hasattr(self, "_aaf_ctr_minus_btns"):   self._aaf_ctr_minus_btns   = {}
        self._aaf_vid_btn_labels.clear()
        self._aaf_ctr_n_labels.clear()
        self._aaf_ctr_minus_btns.clear()
        self._aaf_sync_btns       = {}   # stale widget refs — repopulated below
        self._aaf_sync_dot_labels = getattr(self, "_aaf_sync_dot_labels", {})
        self._aaf_sync_dot_labels.clear()
        self._aaf_qa_btns = getattr(self, "_aaf_qa_btns", {})
        self._aaf_qa_btns.clear()
        self._aaf_try_next_btns = getattr(self, "_aaf_try_next_btns", {})
        self._aaf_try_next_btns.clear()
        self._aaf_confirm_btns = getattr(self, "_aaf_confirm_btns", {})
        self._aaf_confirm_btns.clear()
        self._aaf_lock_btns = getattr(self, "_aaf_lock_btns", {})
        self._aaf_lock_btns.clear()
        self._aaf_ck_lbl_map = getattr(self, "_aaf_ck_lbl_map", {})
        self._aaf_ck_lbl_map.clear()
        self._aaf_ck_container_map = getattr(self, "_aaf_ck_container_map", {})
        self._aaf_ck_container_map.clear()
        self._aaf_drag_sync = None

        # Ensure vars exist even if called before full page init
        if not hasattr(self, "_aaf_page_mode"):
            self._aaf_page_mode = tk.StringVar(value="assign")
        if not hasattr(self, "_aaf_sync_state_vars"):
            self._aaf_sync_state_vars = {}

        # ── Auto-compute column widths from current content ────────────────────
        # Recomputes whenever _aaf_col_widths is absent OR the video path count has
        # changed (e.g. after loading a setup or browsing files).  Manual sash resizes
        # are preserved as long as the video pool doesn't change.
        _cur_vid_count = len(getattr(self, "_aaf_video_paths", []))
        _cur_aud_count = len(getattr(self, "_aaf_audio_paths", []))
        _cur_sentinel  = (_cur_vid_count, _cur_aud_count)
        if (not hasattr(self, "_aaf_col_widths") or
                getattr(self, "_aaf_col_widths_vid_count", (-1, -1)) != _cur_sentinel):
            # Derive per-character pixel width from the actual screen DPI so column
            # widths are correct at any Windows scaling level (96 → 192+ DPI).
            try:
                _dpi = self.winfo_fpixels('1i')           # pixels per inch
            except Exception:
                _dpi = 96.0
            _ppc = max(9, int(9.0 * _dpi / 96.0 + 0.9))  # px per char, round up

            _sources  = getattr(self, "_aaf_sources", [])
            _name_nat = max((len(b) for b in _sources), default=14) * _ppc + 40
            _trk_strs = [", ".join(sorted(getattr(self, "_aaf_source_tracks", {}).get(b, set())))
                         for b in _sources]
            _trk_nat  = max((len(t) for t in _trk_strs), default=8) * _ppc + 30
            _vid_fns  = [self._aaf_vid_label(p) for p in getattr(self, "_aaf_video_paths", [])]
            _vid_nat  = max((len(fn) for fn in _vid_fns), default=20) * _ppc + 80
            _aud_fns  = [basename(p) for p in getattr(self, "_aaf_audio_paths", [])]
            _aud_nat  = max((len(fn) for fn in _aud_fns), default=20) * _ppc + 40
            self._aaf_col_widths = {
                "name":   max(140, _name_nat),
                "tracks": min(max(60,  _trk_nat),  300),
                "video":  max(200, _vid_nat),
                "audio":  max(200, _aud_nat),
                "files":  82,
            }
            self._aaf_col_widths_vid_count = _cur_sentinel

        # ── Apply active column sort ───────────────────────────────────────────
        _sort_col = getattr(self, "_aaf_sort_col", None)
        _sort_dir = getattr(self, "_aaf_sort_dir", "asc")
        if _sort_col and hasattr(self, "_aaf_sources"):
            def _sort_key(b):
                if _sort_col == "name":
                    return b.lower()
                if _sort_col == "video":
                    return self._aaf_source_file_vars.get(
                        b, tk.StringVar(value="")).get().lower()
                if _sort_col == "tracks":
                    return ", ".join(sorted(
                        self._aaf_source_tracks.get(b, set()))).lower()
                return b.lower()
            self._aaf_sources.sort(key=_sort_key, reverse=(_sort_dir == "desc"))

        # ── Update tab button appearance ───────────────────────────────────────
        mode = self._aaf_page_mode.get()
        for btn, m in [(getattr(self, "_aaf_assign_tab_btn", None), "assign"),
                       (getattr(self, "_aaf_sync_tab_btn",   None), "sync")]:
            if btn:
                try:
                    active = (mode == m)
                    btn.config(bg=ACCENT if active else SURF3,
                               fg=BG     if active else SUB)
                except Exception:
                    pass

        # ── Rebuild column headers ─────────────────────────────────────────────
        hdr = getattr(self, "_aaf_col_hdr_frame", None)
        if hdr:
            for w in hdr.winfo_children():
                w.destroy()
            if mode == "assign":
                _col_widths = getattr(self, "_aaf_col_widths",
                                      {"name": 200, "tracks": 110, "video": 200})
                _col_order  = getattr(self, "_aaf_col_order",  ["video", "tracks"])

                # Sort indicator helper
                _sc = getattr(self, "_aaf_sort_col", None)
                _sd = getattr(self, "_aaf_sort_dir", "asc")
                def _sort_lbl(text, sort_key):
                    if _sc == sort_key:
                        return text + (" \u25b2" if _sd == "asc" else " \u25bc")
                    return text

                # SOURCE CLIP (click-to-sort, fixed width from content measurement)
                nf = tk.Frame(hdr, bg=SURF, width=_col_widths["name"], cursor="hand2")
                nf.pack_propagate(False)
                nf.pack(side="left", fill="y")
                _nl = tk.Label(nf, text=_sort_lbl("SOURCE CLIP", "name"),
                               font=FB, bg=SURF, fg=SUB, anchor="w", padx=4,
                               cursor="hand2")
                _nl.pack(fill="both", expand=True)
                for _w in (nf, _nl):
                    _w.bind("<ButtonRelease-1>", lambda e: self._aaf_sort_by("name"))
                self._aaf_col_sash(hdr, "name", SURF)

                # Middle columns (click-to-sort only)
                for _col in _col_order:
                    if _col == "tracks":
                        tf = tk.Frame(hdr, bg=SURF,
                                      width=_col_widths.get("tracks", 80),
                                      cursor="hand2")
                        tf.pack_propagate(False)
                        tf.pack(side="left", fill="y")
                        _th = tk.Label(tf, text=_sort_lbl("TRACK", "tracks"),
                                       font=FB, bg=SURF, fg=SUB,
                                       cursor="hand2", anchor="w", padx=4)
                        _th.pack(fill="both", expand=True)
                        for _w in (tf, _th):
                            _w.bind("<ButtonRelease-1>",
                                    lambda e: self._aaf_sort_by("tracks"))
                        self._aaf_col_sash(hdr, "tracks", SURF)
                    elif _col == "video":
                        vf = tk.Frame(hdr, bg=SURF,
                                      width=_col_widths.get("video", 200),
                                      cursor="hand2")
                        vf.pack_propagate(False)
                        vf.pack(side="left", fill="y")
                        _vh = tk.Label(vf, text=_sort_lbl("VIDEO FILE", "video"),
                                       font=FB, bg=SURF, fg=SUB,
                                       cursor="hand2", anchor="w", padx=4)
                        _vh.pack(fill="both", expand=True)
                        for _w in (vf, _vh):
                            _w.bind("<ButtonRelease-1>",
                                    lambda e: self._aaf_sort_by("video"))
                        self._aaf_col_sash(hdr, "video", SURF)

                # FILES — dedicated column for multi-slot counter
                _ff = tk.Frame(hdr, bg=SURF,
                               width=_col_widths.get("files", 82))
                _ff.pack_propagate(False)
                _ff.pack(side="left", fill="y")
                tk.Label(_ff, text="FILES", font=FB, bg=SURF, fg=SUB,
                         anchor="center").pack(fill="both", expand=True)

                # SYNC and HIDE — left-packed immediately after last column
                tk.Label(hdr, text="SYNC", font=FB, bg=SURF, fg=SUB,
                         width=5, anchor="center").pack(side="left", padx=(6, 0))
                tk.Label(hdr, text="HIDE", font=FB, bg=SURF, fg=SUB,
                         width=5, anchor="center").pack(side="left", padx=(2, 0))

            else:
                _cw_s   = getattr(self, "_aaf_col_widths", {"name": 200, "audio": 260})
                _nw_s   = _cw_s.get("name", 200)
                _aw_s   = _cw_s.get("audio", 260)
                _ow_s   = 110   # offset column width — shared with rows

                # Global controls anchor far right (packed right-to-left)
                self._btn(hdr, "SYNC ALL",
                          self._aaf_sync_all, small=True).pack(side="right", padx=(0, 4))
                self._btn(hdr, "RESET ALL",
                          self._aaf_reset_all, small=True).pack(side="right", padx=(0, 4))
                self._btn(hdr, "UNLOCK ALL",
                          self._aaf_unlock_all, small=True).pack(side="right", padx=(0, 4))

                # SOURCE CLIP (left, fixed width)
                _snf = tk.Frame(hdr, bg=SURF, width=_nw_s)
                _snf.pack_propagate(False)
                _snf.pack(side="left", fill="y")
                tk.Label(_snf, text="\u25cf SOURCE CLIP", font=FB, bg=SURF, fg=SUB,
                         anchor="w", padx=4).pack(fill="both", expand=True)
                self._aaf_col_sash(hdr, "name", SURF)

                # REFERENCE AUDIO (left, fixed width)
                af = tk.Frame(hdr, bg=SURF, width=_aw_s)
                af.pack_propagate(False)
                af.pack(side="left", fill="y")
                tk.Label(af, text="REFERENCE AUDIO", font=FB, bg=SURF, fg=SUB,
                         anchor="w", padx=4).pack(fill="both", expand=True)
                self._aaf_col_sash(hdr, "audio", SURF)

                # OFFSET (left, fixed width)
                _sof = tk.Frame(hdr, bg=SURF, width=_ow_s)
                _sof.pack_propagate(False)
                _sof.pack(side="left", fill="y")
                tk.Label(_sof, text="OFFSET", font=FB, bg=SURF, fg=SUB,
                         anchor="w", padx=4).pack(fill="both", expand=True)

                # SYNC (remaining space)
                tk.Label(hdr, text="SYNC", font=FB, bg=SURF, fg=SUB,
                         anchor="w", padx=8).pack(side="left", fill="x", expand=True)

        # Ensure clip counts exist — may be absent if AAF was loaded before this
        # field was added, or if a setup was restored without a fresh AAF parse.
        if not hasattr(self, "_aaf_source_clip_counts") or not self._aaf_source_clip_counts:
            _cc = {}
            for _trk in getattr(self, "_aaf_data", {}).get("tracks", []):
                for _cl in _trk.get("clips", []):
                    _b = get_clip_base_name(_cl.get("clip_name", ""))
                    if _b:
                        _cc[_b] = _cc.get(_b, 0) + 1
            self._aaf_source_clip_counts = _cc

        options      = ["— no video —"] + [self._aaf_vid_label(p) for p in self._aaf_video_paths]
        aud_options  = ["— no audio —"] + [self._aaf_aud_label(p) for p in self._aaf_audio_paths]
        aud_by_name  = {self._aaf_aud_label(p): p for p in self._aaf_audio_paths}

        vid_fns   = [self._aaf_vid_label(p) for p in self._aaf_video_paths]
        vid_color = {fn: self._ASSIGN_PALETTE[i % len(self._ASSIGN_PALETTE)]
                     for i, fn in enumerate(vid_fns)}

        def _row_bg(sv_val):
            return vid_color.get(sv_val, self._UNASSIGNED_ROW)

        def _apply_color(container, color):
            try:
                if not container.winfo_exists():
                    return
            except Exception:
                return
            container.config(bg=color)
            def _recurse(w):
                if hasattr(w, "set_bg"):
                    w.set_bg(color)
                else:
                    try:    w.config(bg=color)
                    except: pass
                for child in w.winfo_children():
                    _recurse(child)
            _recurse(container)

        # Sync state dot colours
        _STATE_DOT = {"": SUB, "auto": WARN, "manual": SUCCESS}

        for base in self._aaf_sources:
            # ── Persistent vars (survive rebuilds) ────────────────────────────
            if base not in self._aaf_source_file_vars:
                self._aaf_source_file_vars[base]       = tk.StringVar(value="— no video —")
            if base not in self._aaf_source_sync_vars:
                self._aaf_source_sync_vars[base]       = tk.BooleanVar(value=False)
            if base not in self._aaf_source_syncaudio_vars:
                self._aaf_source_syncaudio_vars[base]  = tk.StringVar(value="")
            if base not in self._aaf_source_offset_vars:
                self._aaf_source_offset_vars[base]     = tk.StringVar(value="0.000")
            if base not in self._aaf_source_sync_label_vars:
                self._aaf_source_sync_label_vars[base] = tk.StringVar(value="")
            if base not in self._aaf_sync_state_vars:
                self._aaf_sync_state_vars[base]        = tk.StringVar(value="")
            if base not in self._aaf_needs_sync_vars:
                self._aaf_needs_sync_vars[base]        = tk.BooleanVar(value=False)
            if base not in self._aaf_sync_locked_vars:
                self._aaf_sync_locked_vars[base]       = tk.BooleanVar(value=False)
            # Has-slate flag enables PHAT-weighted xcorr in Pass A, which
            # sharpens slate transient peaks instead of smearing them.
            if not hasattr(self, "_aaf_source_has_slate_vars"):
                self._aaf_source_has_slate_vars = {}
            if base not in self._aaf_source_has_slate_vars:
                self._aaf_source_has_slate_vars[base] = tk.BooleanVar(value=False)

            # Multi-slot state
            if not hasattr(self, "_aaf_source_slot_counts"):
                self._aaf_source_slot_counts = {}
            if not hasattr(self, "_aaf_source_extra_vars"):
                self._aaf_source_extra_vars = {}
            if base not in self._aaf_source_slot_counts:
                self._aaf_source_slot_counts[base] = 1
            if base not in self._aaf_source_extra_vars:
                self._aaf_source_extra_vars[base] = []
            _n_slots = self._aaf_source_slot_counts[base]
            # Trim extra_vars if count decreased
            del self._aaf_source_extra_vars[base][_n_slots - 1:]
            # Extend extra_vars if count increased
            while len(self._aaf_source_extra_vars[base]) < _n_slots - 1:
                self._aaf_source_extra_vars[base].append(
                    tk.StringVar(value="— no video —"))

            sv = self._aaf_source_file_vars[base]
            if sv.get() not in options:
                sv.set("— no video —")

            # ── Audio display var — recreated fresh each rebuild ───────────────
            # Use _aaf_aud_label so the displayed string matches the dropdown
            # options (which now go through the same label) on a basename
            # collision; bare basename here would leave the picker showing a
            # value the dropdown doesn't recognize.
            cur_full  = self._aaf_source_syncaudio_vars[base].get()
            cur_lbl   = (self._aaf_aud_label(cur_full)
                         if cur_full and cur_full in self._aaf_audio_paths
                         else "")
            aud_disp  = tk.StringVar(value=cur_lbl if cur_lbl else "— no audio —")
            self._aaf_source_audio_disp_vars[base] = aud_disp

            # Skip hidden sources (shown in collapsible section below)
            if base in getattr(self, "_aaf_hidden_sources", set()):
                continue

            # ── Container — accent border when source is marked for sync ──────
            _is_sync = self._aaf_needs_sync_vars[base].get()
            bg = _row_bg(sv.get())
            container = tk.Frame(self._aaf_assign_frame, bg=bg,
                                 highlightbackground=ACCENT if _is_sync else BORDER,
                                 highlightthickness=2 if _is_sync else 1)
            container.pack(fill="x", pady=2)

            row1 = tk.Frame(container, bg=bg)
            row1.pack(fill="x", padx=6, pady=(4, 0))

            if mode == "assign":
                # ── ASSIGN mode: SOURCE | VIDEO | PT TRACK | SYNC | hide ────
                _col_widths = getattr(self, "_aaf_col_widths",
                                      {"name": 200, "tracks": 110, "video": 200})
                _col_order  = getattr(self, "_aaf_col_order",  ["video", "tracks"])

                # Left side: name | video | tracks | SYNC | HIDE
                nf = tk.Frame(row1, bg=bg, width=_col_widths["name"])
                nf.pack_propagate(False)
                nf.pack(side="left", fill="y")
                name_lbl = tk.Label(nf, text=base, font=FB, bg=bg, fg=TEXT,
                                    anchor="w")
                name_lbl.pack(fill="x")
                self._tooltip(name_lbl, base)

                # Middle columns in stored order
                tracks_str = ", ".join(sorted(self._aaf_source_tracks.get(base, set())))
                for _col in _col_order:
                    if _col == "tracks":
                        tf = tk.Frame(row1, bg=bg,
                                      width=_col_widths.get("tracks", 110))
                        tf.pack_propagate(False)
                        tf.pack(side="left", fill="y", padx=(1, 0))
                        tk.Label(tf, text=tracks_str, font=FB, bg=bg, fg=SUB,
                                 anchor="w").pack(fill="x")
                    elif _col == "video":
                        _n_slots2  = self._aaf_source_slot_counts.get(base, 1)
                        _extra_evs = self._aaf_source_extra_vars.get(base, [])
                        _n_filled2 = (1 if sv.get() != "— no video —" else 0) + sum(
                            1 for _ev in _extra_evs if _ev.get() != "— no video —")
                        _partial2  = _n_slots2 > 1 and _n_filled2 < _n_slots2

                        # Build button label text
                        if _n_slots2 == 1:
                            _vid_lbl_txt = sv.get()
                        else:
                            _clip_cnt2 = getattr(self, "_aaf_source_clip_counts", {}).get(base, 0)
                            _vid_lbl_txt = "{}/{} files".format(_n_filled2, _n_slots2)
                            if _clip_cnt2 > 0:
                                _vid_lbl_txt += "  ·  {} clips".format(_clip_cnt2)

                        vrf = tk.Frame(row1, bg=bg,
                                       width=_col_widths.get("video", 200))
                        vrf.pack_propagate(False)
                        vrf.pack(side="left", fill="y", padx=(1, 0))

                        # Multi-select button (replaces _FlatDropdown)
                        _vid_btn_frame = tk.Frame(
                            vrf, bg=SURF3,
                            highlightbackground=BORDER, highlightthickness=1,
                            cursor="hand2")
                        _vid_btn_frame.pack(fill="x")
                        _vid_btn_lbl = tk.Label(
                            _vid_btn_frame,
                            text=_vid_lbl_txt,
                            font=FB, bg=SURF3,
                            fg=WARN if _partial2 else (SUB if sv.get() == "— no video —" else TEXT),
                            anchor="w", padx=6, pady=3)
                        _vid_btn_lbl.pack(side="left", fill="x", expand=True)
                        tk.Label(_vid_btn_frame, text="\u25bc", font=(_SANS, 7),
                                 bg=SURF3, fg=SUB, padx=4).pack(side="right")
                        # Store ref for in-place update (avoids full rebuild on popup apply)
                        self._aaf_vid_btn_labels[base] = _vid_btn_lbl

                        def _open_ms(e, b=base, w=_vid_btn_frame):
                            self._aaf_open_video_multiselect(b, w)
                        _vid_btn_frame.bind("<ButtonRelease-1>", _open_ms)
                        _vid_btn_lbl.bind("<ButtonRelease-1>",   _open_ms)

                # ── FILES column: dedicated counter [−] N [+] ─────────────────
                _n_slots   = self._aaf_source_slot_counts.get(base, 1)
                _extra_evs = self._aaf_source_extra_vars.get(base, [])
                _n_filled  = (1 if sv.get() != "— no video —" else 0) + sum(
                    1 for _ev in _extra_evs if _ev.get() != "— no video —")
                _partial   = _n_slots > 1 and _n_filled < _n_slots
                _clip_cnt  = getattr(self, "_aaf_source_clip_counts", {}).get(base, 0)

                _files_col = tk.Frame(row1, bg=bg,
                                      width=_col_widths.get("files", 82))
                _files_col.pack_propagate(False)
                _files_col.pack(side="left", fill="y")

                _ctr = tk.Frame(_files_col, bg=bg)
                _ctr.pack(anchor="center", expand=True)
                _minus_btn = tk.Label(
                    _ctr, text="\u2212", font=FB,
                    bg=SURF3, fg=TEXT if _n_slots > 1 else BORDER,
                    cursor="hand2" if _n_slots > 1 else "",
                    padx=5, pady=1, bd=0,
                    highlightbackground=BORDER, highlightthickness=1)
                _minus_btn.pack(side="left")
                _n_lbl = tk.Label(
                    _ctr, text=str(_n_slots), font=FB,
                    bg=SURF3, fg=WARN if _partial else TEXT,
                    padx=6, pady=1)
                _n_lbl.pack(side="left", padx=(1, 1))
                _plus_btn = tk.Label(
                    _ctr, text="+", font=FB,
                    bg=SURF3, fg=TEXT,
                    cursor="hand2", padx=5, pady=1, bd=0,
                    highlightbackground=BORDER, highlightthickness=1)
                _plus_btn.pack(side="left")
                # Store refs for in-place counter updates (no rebuild needed)
                self._aaf_ctr_n_labels[base]   = _n_lbl
                self._aaf_ctr_minus_btns[base] = _minus_btn

                # Always bind both buttons; handler guards against N<1
                _plus_btn.bind("<ButtonRelease-1>",
                               lambda e, b=base: self._aaf_update_slot_count(b, +1))
                _minus_btn.bind("<ButtonRelease-1>",
                                lambda e, b=base: self._aaf_update_slot_count(b, -1))

                # SYNC checkbox + HIDE — left-packed immediately after TRACK
                _ck_var = self._aaf_needs_sync_vars[base]
                _ck_lbl = tk.Label(row1,
                                   text="\u2611" if _ck_var.get() else "\u2610",
                                   font=(_SANS, 15), bg=bg,
                                   fg=ACCENT if _ck_var.get() else SUB,
                                   cursor="hand2", padx=4, width=5, anchor="center")
                _ck_lbl.pack(side="left", padx=(6, 0))
                # Register in maps for drag-select hit-testing
                self._aaf_ck_lbl_map[base]       = _ck_lbl
                self._aaf_ck_container_map[base] = container

                def _ck_press(e, b=base):
                    idx    = self._aaf_sources.index(b)
                    target = not self._aaf_needs_sync_vars[b].get()
                    self._aaf_drag_sync = {"start_idx": idx, "target": target,
                                           "last_idx": idx}
                    self._aaf_apply_sync_range(idx, idx, target)

                def _ck_move(e, b=base):
                    if not self._aaf_drag_sync:
                        return
                    cur = self._aaf_ck_hit_test(e.y_root)
                    if cur is None:
                        return
                    try:
                        cur_idx = self._aaf_sources.index(cur)
                    except ValueError:
                        return
                    if cur_idx == self._aaf_drag_sync.get("last_idx"):
                        return
                    self._aaf_drag_sync["last_idx"] = cur_idx
                    lo = min(self._aaf_drag_sync["start_idx"], cur_idx)
                    hi = max(self._aaf_drag_sync["start_idx"], cur_idx)
                    self._aaf_apply_sync_range(lo, hi,
                                               self._aaf_drag_sync["target"])

                def _ck_release(e):
                    self._aaf_drag_sync = None

                _ck_lbl.bind("<ButtonPress-1>",   _ck_press)
                _ck_lbl.bind("<B1-Motion>",        _ck_move)
                _ck_lbl.bind("<ButtonRelease-1>",  _ck_release)

                hide_lbl = tk.Label(row1, text="HIDE", font=FB, bg=bg, fg=SUB,
                                    cursor="hand2", padx=4, width=5, anchor="center")
                hide_lbl.pack(side="left", padx=(2, 0))
                hide_lbl.bind("<Enter>",  lambda e, w=hide_lbl: w.config(fg=WARN))
                hide_lbl.bind("<Leave>",  lambda e, w=hide_lbl: w.config(fg=SUB))
                hide_lbl.bind("<Button-1>",
                              lambda e, b=base: self._aaf_hide_source(b))

                # Remove stale video-change traces then re-add
                for _tr in list(sv.trace_info()):
                    if _tr[0] == "write":
                        try: sv.trace_remove("write", _tr[1])
                        except Exception: pass
                def _on_vid_change(*args, c=container, s=sv, b=base):
                    _apply_color(c, _row_bg(s.get()))
                    # Keep the button label in sync with the StringVar too —
                    # the trace previously only refreshed the background,
                    # so _aaf_auto_match (which writes sv directly) and any
                    # other programmatic sv.set() left the label stuck at
                    # its row-construction value (reported: bg updated,
                    # label still "— no video —" until picker round-trip).
                    self._aaf_refresh_vid_btn_label(b)
                    # If this source had a locked/completed sync, invalidate it —
                    # the assignment changed so the old sync result is stale.
                    lkv = self._aaf_sync_locked_vars.get(b)
                    was_locked = lkv and lkv.get()
                    ssv = self._aaf_sync_state_vars.get(b, tk.StringVar()).get()
                    if was_locked or ssv:
                        if lkv: lkv.set(False)
                        ov = self._aaf_source_offset_vars.get(b)
                        if ov: ov.set("0.000")
                        sv2 = self._aaf_source_sync_vars.get(b)
                        if sv2: sv2.set(False)
                        lv = self._aaf_source_sync_label_vars.get(b)
                        if lv: lv.set("")
                        self._aaf_set_sync_state(b, "")
                        # Drop the now-stale candidate list too — they
                        # were measured against the previous video.
                        # Without this, TRY NEXT remains visible and
                        # clicking it applies an offset that doesn't
                        # match the freshly-picked video.
                        self._aaf_clear_sync_candidates(b)
                        # Refresh sync-tab row so lock icon updates immediately
                        self._ui(self._rebuild_aaf_source_rows)
                sv.trace_add("write", _on_vid_change)

                # Remove stale traces on extra slot vars (no row rendered — managed via popup)
                for _slot_ev in self._aaf_source_extra_vars.get(base, []):
                    if _slot_ev.get() not in options:
                        _slot_ev.set("— no video —")
                    for _tr in list(_slot_ev.trace_info()):
                        if _tr[0] == "write":
                            try: _slot_ev.trace_remove("write", _tr[1])
                            except Exception: pass
                    _slot_ev.trace_add("write", lambda *_a: None)

                # Pad bottom of assign-mode row
                tk.Frame(container, bg=bg, height=4).pack()

            else:
                # ── SYNC mode: only show sources marked "needs sync" ───────────
                if not self._aaf_needs_sync_vars[base].get():
                    container.destroy()
                    continue

                locked    = self._aaf_sync_locked_vars[base].get()
                state_val = self._aaf_sync_state_vars[base].get()
                _cw_s     = getattr(self, "_aaf_col_widths", {"name": 200, "audio": 260})
                _nw_s     = _cw_s.get("name", 200)
                _aw_s     = _cw_s.get("audio", 260)
                _ow_s     = 110   # offset column — matches sync header

                # ── SOURCE CLIP (left, fixed width) ───────────────────────────
                dot_color  = _STATE_DOT.get(state_val, SUB)
                clip_frame = tk.Frame(row1, bg=bg, width=_nw_s)
                clip_frame.pack_propagate(False)
                clip_frame.pack(side="left", fill="y")
                dot_lbl = tk.Label(clip_frame, text="\u25cf", font=FB, bg=bg,
                                   fg=dot_color, padx=2)
                dot_lbl.pack(side="left")
                self._aaf_sync_dot_labels[base] = dot_lbl
                name_lbl = tk.Label(clip_frame, text=base, font=FB, bg=bg,
                                    fg=TEXT, anchor="w")
                name_lbl.pack(side="left", fill="x", expand=True)
                self._tooltip(name_lbl, base)

                # ── REFERENCE AUDIO (left, fixed width) ───────────────────────
                aud_frame    = tk.Frame(row1, bg=bg, width=_aw_s)
                aud_frame.pack_propagate(False)
                aud_frame.pack(side="left", fill="y")
                aud_dd_state = "disabled" if locked else "readonly"
                aud_dd = _FlatDropdown(aud_frame, textvariable=aud_disp,
                                       values=aud_options,
                                       state=aud_dd_state, font=FB)
                aud_dd.pack(fill="x", expand=True)

                # ── OFFSET (left, fixed width) ─────────────────────────────────
                off_frame = tk.Frame(row1, bg=bg, width=_ow_s)
                off_frame.pack_propagate(False)
                off_frame.pack(side="left", fill="y")
                tk.Label(off_frame,
                         textvariable=self._aaf_source_offset_vars[base],
                         font=FB, bg=bg, fg=ACCENT,
                         anchor="e").pack(side="left", fill="x", expand=True)
                tk.Label(off_frame, text="s", font=FB, bg=bg,
                         fg=SUB).pack(side="left", padx=(2, 4))

                # ── SYNC controls (left, remaining space) ─────────────────────
                # 🔒 Lock toggle
                lock_text = "\U0001f512" if locked else "\U0001f513"
                lock_fg   = WARN if locked else SUB
                lock_btn  = tk.Label(row1, text=lock_text, font=FB, bg=SURF3,
                                     fg=lock_fg, cursor="hand2", padx=6, pady=2,
                                     bd=0, highlightbackground=BORDER,
                                     highlightthickness=1)
                lock_btn.pack(side="left", padx=(8, 2))
                lock_btn.bind("<Enter>",  lambda e, w=lock_btn: w.config(bg=ACCENT))
                lock_btn.bind("<Leave>",  lambda e, w=lock_btn: w.config(bg=SURF3))
                lock_btn.bind("<ButtonRelease-1>",
                              lambda e, b=base: self._aaf_toggle_lock(b))
                self._aaf_lock_btns[base] = lock_btn

                # ✓ ACCEPT
                cfm_text, cfm_fg = self._confirm_btn_style(state_val)
                cfm_btn = tk.Label(row1, text=cfm_text, font=FB, bg=SURF3,
                                   fg=cfm_fg, cursor="hand2", padx=6, pady=2,
                                   bd=0, highlightbackground=BORDER,
                                   highlightthickness=1)
                cfm_btn.pack(side="left", padx=(2, 0))
                cfm_btn.bind("<Enter>",  lambda e, w=cfm_btn: w.config(bg=ACCENT))
                cfm_btn.bind("<Leave>",  lambda e, w=cfm_btn: w.config(bg=SURF3))
                cfm_btn.bind("<ButtonRelease-1>",
                             lambda e, b=base: self._aaf_confirm_sync(b))
                if locked:
                    cfm_btn.config(state="disabled", fg=SUB)
                self._aaf_confirm_btns[base] = cfm_btn

                # ▶ PREVIEW
                # Initial state must consult _aaf_qa_playing so a row
                # rebuild during QA playback (page toggle, ALIGN accept,
                # drag-select apply, etc.) doesn't show "play" on a
                # button whose audio is actively playing.  The previous
                # always-default construction left the button reading
                # PREVIEW while audio kept rolling; the eventual
                # _qa_poll-driven _aaf_qa_stop then tried to reset an
                # already-default-styled (new) widget \u2014 silent no-op.
                _is_playing = getattr(self, "_aaf_qa_playing", None) == base
                _qa_text    = "\u25a0 STOP" if _is_playing else "\u25b6 PREVIEW"
                _qa_fg      = WARN          if _is_playing else TEXT
                prev_btn = tk.Label(row1, text=_qa_text, font=FB, bg=SURF3,
                                    fg=_qa_fg, cursor="hand2", padx=6, pady=2, bd=0,
                                    highlightbackground=BORDER, highlightthickness=1)
                prev_btn.pack(side="left", padx=(2, 0))
                prev_btn.bind("<Enter>",  lambda e, w=prev_btn: w.config(bg=ACCENT))
                prev_btn.bind("<Leave>",  lambda e, w=prev_btn: w.config(bg=SURF3))
                prev_btn.bind("<ButtonRelease-1>",
                              lambda e, b=base: self._aaf_qa_toggle(b))
                self._aaf_qa_btns[base] = prev_btn

                # ALIGN…
                align_btn = tk.Label(row1, text="ALIGN\u2026", font=FB, bg=SURF3,
                                     fg=SUB if locked else TEXT,
                                     cursor="" if locked else "hand2",
                                     padx=6, pady=2, bd=0,
                                     highlightbackground=BORDER, highlightthickness=1)
                align_btn.pack(side="left", padx=(2, 0))
                if not locked:
                    align_btn.bind("<Enter>",  lambda e, w=align_btn: w.config(bg=ACCENT))
                    align_btn.bind("<Leave>",  lambda e, w=align_btn: w.config(bg=SURF3))
                    align_btn.bind("<ButtonRelease-1>",
                                   lambda e, b=base: self._aaf_open_align(b))

                # ↺ Reset
                rst_btn = tk.Label(row1, text="\u21ba", font=FB, bg=SURF3,
                                   fg=SUB, cursor="hand2", padx=6, pady=2, bd=0,
                                   highlightbackground=BORDER, highlightthickness=1)
                rst_btn.pack(side="left", padx=(2, 0))
                if locked:
                    rst_btn.config(state="disabled", cursor="")
                else:
                    rst_btn.bind("<Enter>",  lambda e, w=rst_btn: w.config(bg="#5a2020"))
                    rst_btn.bind("<Leave>",  lambda e, w=rst_btn: w.config(bg=SURF3))
                    rst_btn.bind("<ButtonRelease-1>",
                                 lambda e, b=base: self._aaf_reset_sync(b))

                # Slate-aware toggle — when checked, the next SYNC run uses
                # GCC-PHAT weighting to lock onto the slate transient peak
                # instead of treating it as broadband noise.  Useful when
                # the clapper is the strongest/cleanest sync signal in
                # short clips where the speech-rhythm xcorr is ambiguous.
                slate_var = self._aaf_source_has_slate_vars[base]
                slate_ck = tk.Label(row1,
                    text="☑ slate" if slate_var.get() else "☐ slate",
                    font=FB, bg=BG,
                    fg=ACCENT if slate_var.get() else SUB,
                    cursor="hand2", padx=4)
                slate_ck.pack(side="left", padx=(8, 0))
                if locked:
                    slate_ck.config(cursor="")
                else:
                    def _toggle_slate(_e=None, b=base, w=slate_ck,
                                       v=slate_var):
                        v.set(not v.get())
                        w.config(text="☑ slate" if v.get() else "☐ slate",
                                 fg=ACCENT if v.get() else SUB)
                    slate_ck.bind("<Button-1>", _toggle_slate)

                # SYNC button
                sync_btn = self._btn(row1, "SYNC",
                                     lambda b=base: self._aaf_do_sync(b),
                                     small=True)
                sync_btn.pack(side="left", padx=(2, 0))
                if locked:
                    sync_btn.config(state="disabled", fg=SUB)
                self._aaf_sync_btns[base] = sync_btn
                self._aaf_refresh_sync_btn(base, sync_btn)

                # ⏭ TRY NEXT — cycles through alternate sync candidates
                # right here on the row so the user doesn't have to open
                # ALIGN just to audition a runner-up.  Only visible when
                # auto-sync produced one or more alternates; otherwise
                # packed-hidden so the row width stays consistent.
                nxt_btn = tk.Label(
                    row1, text="⏭ NEXT", font=FB, bg=SURF3, fg=SUB,
                    cursor="hand2", padx=6, pady=2, bd=0,
                    highlightbackground=BORDER, highlightthickness=1)
                nxt_btn.bind(
                    "<Enter>", lambda e, w=nxt_btn: w.config(bg=ACCENT))
                nxt_btn.bind(
                    "<Leave>", lambda e, w=nxt_btn: w.config(bg=SURF3))
                nxt_btn.bind(
                    "<ButtonRelease-1>",
                    lambda e, b=base: self._aaf_cycle_candidate(b))
                if not hasattr(self, "_aaf_try_next_btns"):
                    self._aaf_try_next_btns = {}
                self._aaf_try_next_btns[base] = nxt_btn
                # Pack only when there are alternates to audition.
                self._aaf_refresh_try_next_btn(base)

                # Audio dropdown trace — keep the canonical full-path var
                # in sync with the displayed label AND fully invalidate
                # any prior sync state for this source.  Mirror of the
                # _on_vid_change invalidation: changing the reference
                # audio invalidates the offset just as fully as changing
                # the video does.  The previous version only cleared the
                # sync label, leaving offset / sync_var / state dot /
                # lock / candidates stale — TRY NEXT would still cycle
                # offsets measured against the previous reference audio.
                def _on_aud_change(*args, b=base, dv=aud_disp):
                    fn   = dv.get()
                    full = {self._aaf_aud_label(p): p for p in self._aaf_audio_paths}.get(fn, "")
                    self._aaf_source_syncaudio_vars[b].set(full)
                    lkv = self._aaf_sync_locked_vars.get(b)
                    was_locked = lkv and lkv.get()
                    ssv = self._aaf_sync_state_vars.get(b, tk.StringVar()).get()
                    lv  = self._aaf_source_sync_label_vars.get(b)
                    had_label = lv and lv.get()
                    if was_locked or ssv or had_label:
                        if lkv: lkv.set(False)
                        ov = self._aaf_source_offset_vars.get(b)
                        if ov: ov.set("0.000")
                        sv2 = self._aaf_source_sync_vars.get(b)
                        if sv2: sv2.set(False)
                        if lv: lv.set("")
                        self._aaf_set_sync_state(b, "")
                        self._aaf_clear_sync_candidates(b)
                        btn_ = self._aaf_sync_btns.get(b)
                        if btn_:
                            self._aaf_refresh_sync_btn(b, btn_)
                        # Refresh row layout so lock icon / state colour update.
                        self._ui(self._rebuild_aaf_source_rows)
                aud_disp.trace_add("write", _on_aud_change)

        # Show placeholder when SYNC page has nothing to display
        if mode == "sync":
            visible = [b for b in self._aaf_sources
                       if self._aaf_needs_sync_vars.get(b, tk.BooleanVar()).get()]
            if not visible:
                tk.Label(self._aaf_assign_frame,
                         text="No sources marked for sync.\n"
                              "Go to SOURCE ASSIGN and tick the sync checkbox on each source.",
                         font=FB, bg=SURF, fg=SUB, justify="center",
                         pady=20).pack(fill="x")

        # ── Hidden sources collapsible section (assign mode only) ─────────────
        if mode == "assign":
            _hidden_set = getattr(self, "_aaf_hidden_sources", set())
            _hidden_list = [b for b in self._aaf_sources if b in _hidden_set]
            if _hidden_list:
                if not hasattr(self, "_aaf_hidden_expanded"):
                    self._aaf_hidden_expanded = False
                _exp = self._aaf_hidden_expanded
                _count = len(_hidden_list)

                h_hdr = tk.Frame(self._aaf_assign_frame, bg=SURF3, cursor="hand2")
                h_hdr.pack(fill="x", pady=(10, 0))
                tk.Label(h_hdr,
                         text=("  \u25be " if _exp else "  \u25b8 ") +
                              "HIDDEN  ({})".format(_count),
                         font=FB, bg=SURF3, fg=SUB,
                         anchor="w", pady=4).pack(side="left")
                tk.Label(h_hdr, text="click to expand / collapse",
                         font=FB, bg=SURF3, fg=BORDER,
                         anchor="e", padx=8).pack(side="right")

                h_body = tk.Frame(self._aaf_assign_frame, bg=SURF3)
                if _exp:
                    h_body.pack(fill="x")
                    for _hb in _hidden_list:
                        _hr = tk.Frame(h_body, bg=SURF3)
                        _hr.pack(fill="x", padx=12, pady=1)
                        tk.Label(_hr, text=_hb, font=FB, bg=SURF3, fg=SUB,
                                 anchor="w").pack(side="left", fill="x", expand=True)
                        _show = tk.Label(_hr, text="SHOW", font=FB, bg=SURF3,
                                         fg=ACCENT, cursor="hand2", padx=8)
                        _show.pack(side="right")
                        _show.bind("<Button-1>",
                                   lambda e, b=_hb: self._aaf_unhide_source(b))

                def _toggle_hidden(e, body=h_body, hdr_lbl=h_hdr.winfo_children()[0],
                                   lst=_hidden_list):
                    self._aaf_hidden_expanded = not getattr(self, "_aaf_hidden_expanded", False)
                    ex = self._aaf_hidden_expanded
                    hdr_lbl.config(text=("  \u25be " if ex else "  \u25b8 ") +
                                        "HIDDEN  ({})".format(len(lst)))
                    if ex:
                        body.pack(fill="x")
                        for _hb in lst:
                            _hr = tk.Frame(body, bg=SURF3)
                            _hr.pack(fill="x", padx=12, pady=1)
                            tk.Label(_hr, text=_hb, font=FB, bg=SURF3, fg=SUB,
                                     anchor="w").pack(side="left", fill="x", expand=True)
                            _s = tk.Label(_hr, text="SHOW", font=FB, bg=SURF3,
                                          fg=ACCENT, cursor="hand2", padx=8)
                            _s.pack(side="right")
                            _s.bind("<Button-1>",
                                    lambda ev, b=_hb: self._aaf_unhide_source(b))
                    else:
                        for _w in list(body.winfo_children()):
                            _w.destroy()
                        body.pack_forget()
                h_hdr.bind("<Button-1>", _toggle_hidden)
                for _ch in h_hdr.winfo_children():
                    _ch.bind("<Button-1>", _toggle_hidden)

        self._aaf_auto_match()

        # If opened via the generic Open Session from the home screen, restore
        # any saved setup state that was stashed before navigating here.
        pending = getattr(self, "_pending_aaf_setup", None)
        if pending is not None:
            self._pending_aaf_setup = None
            self._ui(lambda d=pending: self._aaf_restore_setup(d))

    # FPS priority order: higher quality / higher frame rate wins
    _FPS_PRIORITY = [
        120.0, 119.88, 60.0, 59.94, 50.0, 48.0, 47.952,
        30.0, 29.97, 25.0, 24.0, 23.976, 23.98,
    ]

    def _aaf_schedule_detect_fps(self):
        """Debounce: cancel any pending detect job and schedule a fresh one."""
        job = getattr(self, "_aaf_fps_job", None)
        if job:
            self.after_cancel(job)
        self._aaf_set_status("Detecting frame rate…")
        self._aaf_fps_job = self.after(300, self._aaf_detect_fps)

    def _aaf_detect_fps(self):
        """Probe pooled video files off the main thread; update FPS field when done."""
        self._aaf_fps_job = None
        paths = list(self._aaf_video_paths)   # snapshot so thread is safe
        if not paths:
            return

        priority = self._FPS_PRIORITY

        def _probe():
            import subprocess, json as _json
            best_fps  = None
            best_rank = len(priority)
            for vp in paths:
                try:
                    result = run_hidden(
                        ["ffprobe", "-v", "quiet", "-print_format", "json",
                         "-show_streams", vp],
                        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
                    info = _json.loads(result.stdout)
                    for stream in info.get("streams", []):
                        if stream.get("codec_type") != "video":
                            continue
                        r = stream.get("r_frame_rate", "")
                        if "/" in r:
                            num, den = r.split("/")
                            fps = round(int(num) / int(den), 3)
                        else:
                            try:    fps = float(r)
                            except: continue
                        rank = len(priority)
                        for i, ref in enumerate(priority):
                            if abs(fps - ref) < 0.1:
                                rank = i
                                break
                        if rank < best_rank or best_fps is None:
                            best_rank = rank
                            best_fps  = fps
                except Exception:
                    continue
            return best_fps

        def _done(fut):
            try:
                best_fps = fut.result()
            except Exception:
                self._ui(lambda: self._aaf_set_status(""))
                return
            def _apply():
                if best_fps is not None:
                    # Only auto-fill FPS when a fixed preset hasn't been chosen
                    preset = getattr(self, "_aaf_preset_var", None)
                    preset_val = preset.get() if preset else "Detect from media"
                    if preset_val in ("Detect from media", "Custom"):
                        display = "{:.3f}".format(best_fps).rstrip("0").rstrip(".")
                        self._aaf_fps_var.set(display)
                self._aaf_set_status("")
            self._ui(_apply)

        try:
            from concurrent.futures import ThreadPoolExecutor
            ex = ThreadPoolExecutor(max_workers=1)
            fut = ex.submit(_probe)
            fut.add_done_callback(_done)
            ex.shutdown(wait=False)
        except Exception:
            pass   # ffprobe not available or thread error — leave FPS as typed

    def _aaf_update_seq_presets(self):
        """Probe loaded video files and add detected (W×H @ fps) options to the
        sequence preset dropdown below a divider, so the user can pick directly
        from their source media without having to look it up."""
        dd = getattr(self, "_aaf_preset_dd", None)
        if dd is None:
            return
        vpaths = list(getattr(self, "_aaf_video_paths", []))

        def _probe():
            seen     = set()
            detected = []
            for vp in vpaths:
                if not os.path.isfile(vp):
                    continue
                try:
                    w, h, fps, _ = engines.probe_media_settings([vp])
                    if w > 0 and h > 0 and fps > 0:
                        key = (w, h, round(fps, 3))
                        if key not in seen:
                            seen.add(key)
                            fps_s = "{:.3f}".format(fps).rstrip("0").rstrip(".")
                            label = "{}×{}  @  {}fps".format(w, h, fps_s)
                            detected.append((w, h, fps, label))
                except Exception:
                    pass
            return detected

        def _apply(detected):
            base_names = [p[0] for p in _AAF_SEQ_PRESETS]
            new_values = list(base_names)
            if detected:
                new_values.append(
                    _FlatDropdown.separator("──── Detected from source ────"))
                for _, _, _, label in detected:
                    new_values.append(label)
            dd.configure(values=new_values)
            self._aaf_detected_presets = {
                label: (w, h, fps) for w, h, fps, label in detected}

        try:
            from concurrent.futures import ThreadPoolExecutor
            ex = ThreadPoolExecutor(max_workers=1)
            fut = ex.submit(_probe)
            fut.add_done_callback(
                lambda f: self._ui(
                    lambda: _apply([] if f.exception() else f.result())))
            ex.shutdown(wait=False)
        except Exception:
            pass

    def _aaf_refresh_vid_btn_label(self, base):
        """Refresh the per-source video-assign button label + N-counter
        colour from the current _aaf_source_file_vars[base] /
        _aaf_source_extra_vars state.  Called from both the picker's
        in-place apply AND the sv 'write' trace so any sv change — manual
        (picker) or programmatic (_aaf_auto_match, restore-setup) —
        updates the visible label.  Without this, auto-match set sv and
        updated the row's background colour via the existing trace but
        left the button text reading '— no video —' until the user
        round-tripped the picker (reported)."""
        btn_lbl = getattr(self, "_aaf_vid_btn_labels", {}).get(base)
        if not (btn_lbl and btn_lbl.winfo_exists()):
            return
        sv = self._aaf_source_file_vars.get(base)
        if sv is None:
            return
        evs     = self._aaf_source_extra_vars.get(base, [])
        n_slots = self._aaf_source_slot_counts.get(base, 1)
        n_filled = ((1 if sv.get() != "— no video —" else 0)
                    + sum(1 for ev in evs if ev.get() != "— no video —"))
        partial  = n_slots > 1 and n_filled < n_slots
        if n_slots == 1:
            new_text = sv.get()
        else:
            _cc = getattr(self, "_aaf_source_clip_counts", {}).get(base, 0)
            new_text = "{}/{} files".format(n_filled, n_slots)
            if _cc > 0:
                new_text += "  ·  {} clips".format(_cc)
        new_fg = WARN if partial else (SUB if sv.get() == "— no video —" else TEXT)
        try:
            btn_lbl.config(text=new_text, fg=new_fg)
            n_lbl = getattr(self, "_aaf_ctr_n_labels", {}).get(base)
            if n_lbl and n_lbl.winfo_exists():
                n_lbl.config(fg=WARN if partial else TEXT)
        except tk.TclError:
            pass

    def _aaf_auto_match(self):
        """Auto-assign video AND reference audio files to unassigned sources."""
        vid_options = ["— no video —"] + [self._aaf_vid_label(p) for p in self._aaf_video_paths]
        aud_by_name = {self._aaf_aud_label(p): p for p in self._aaf_audio_paths}

        for base in self._aaf_sources:
            # Video auto-match (never override a manual assignment).
            # The picked path goes through _aaf_vid_label so the StringVar
            # value matches the collision-disambiguated dropdown options;
            # writing bare basename(vp) would NOT match options on a
            # multicam basename collision and the rebuild guard would
            # silently wipe the assignment.
            sv = self._aaf_source_file_vars.get(base)
            if sv and sv.get() == "— no video —" and self._aaf_video_paths:
                vp = match_source_to_video(base, self._aaf_video_paths)
                if vp:
                    fn = self._aaf_vid_label(vp)
                    if fn in vid_options:
                        sv.set(fn)

            # Audio auto-match (never override a manual assignment).
            # Same disambiguation as video: the display var carries the
            # collision-safe label so the on-change trace can map it back
            # to the right physical path.
            aud_disp = self._aaf_source_audio_disp_vars.get(base)
            full_var = self._aaf_source_syncaudio_vars.get(base)
            if (aud_disp and full_var
                    and aud_disp.get() == "— no audio —"
                    and self._aaf_audio_paths):
                ap = match_source_to_video(base, self._aaf_audio_paths)
                if ap:
                    fn = self._aaf_aud_label(ap)
                    if fn in aud_by_name:
                        # Set display var; the trace will update the full-path var
                        aud_disp.set(fn)

    # ── Sync checkbox drag-select helpers ────────────────────────────────────

    def _aaf_ck_hit_test(self, y_root):
        """Return the base whose checkbox row's screen rect contains y_root.
        Falls back to the nearest row if none contains it exactly (e.g. gap
        between rows).  Returns None if no checkboxes are registered."""
        best_base = None
        best_dist = float("inf")
        for base, lbl in self._aaf_ck_lbl_map.items():
            try:
                y_top = lbl.winfo_rooty()
                y_bot = y_top + lbl.winfo_height()
                if y_top <= y_root <= y_bot:
                    return base          # exact hit
                dist = min(abs(y_root - y_top), abs(y_root - y_bot))
                if dist < best_dist:
                    best_dist = dist
                    best_base = base
            except Exception:
                pass
        return best_base

    def _aaf_apply_sync_range(self, lo_idx, hi_idx, target):
        """Set needs-sync to *target* for every source in index range [lo, hi].
        Updates the BoolVar, checkbox label, and container highlight in-place
        without triggering a full row rebuild."""
        for i in range(lo_idx, hi_idx + 1):
            if i < 0 or i >= len(self._aaf_sources):
                continue
            base = self._aaf_sources[i]
            v   = self._aaf_needs_sync_vars.get(base)
            lbl = self._aaf_ck_lbl_map.get(base)
            con = self._aaf_ck_container_map.get(base)
            if v:
                v.set(target)
            if lbl:
                try:
                    lbl.config(text="\u2611" if target else "\u2610",
                               fg=ACCENT if target else SUB)
                except Exception:
                    pass
            if con:
                try:
                    con.config(
                        highlightbackground=ACCENT if target else BORDER,
                        highlightthickness=2 if target else 1)
                except Exception:
                    pass

    def _aaf_unlock_all(self):
        """Unlock every locked source on the sync page."""
        sources = [b for b in self._aaf_sources
                   if self._aaf_needs_sync_vars.get(b, tk.BooleanVar()).get()
                   and self._aaf_sync_locked_vars.get(b, tk.BooleanVar()).get()]
        if not sources:
            return
        for base in sources:
            lkv = self._aaf_sync_locked_vars.get(base)
            if lkv:
                lkv.set(False)
        self._rebuild_aaf_source_rows()

    def _aaf_reset_all(self):
        """Reset sync state for every unlocked source on the sync page."""
        sources = [b for b in self._aaf_sources
                   if self._aaf_needs_sync_vars.get(b, tk.BooleanVar()).get()
                   and not self._aaf_sync_locked_vars.get(b, tk.BooleanVar()).get()]
        if not sources:
            return
        if not messagebox.askyesno("Reset All",
                "Reset sync for {} unlocked source{}?\n"
                "This cannot be undone.".format(
                    len(sources), "s" if len(sources) != 1 else "")):
            return
        for base in sources:
            ov = self._aaf_source_offset_vars.get(base)
            if ov: ov.set("0.000")
            sv = self._aaf_source_sync_vars.get(base)
            if sv: sv.set(False)
            lv = self._aaf_source_sync_label_vars.get(base)
            if lv: lv.set("")
            self._aaf_set_sync_state(base, "")
        self._rebuild_aaf_source_rows()

    def _aaf_sync_all(self):
        """Run sync detection for every source marked 'needs sync' that has video + audio."""
        sources_to_sync = [
            base for base in self._aaf_sources
            if (self._aaf_needs_sync_vars.get(base, tk.BooleanVar()).get()
                and self._aaf_source_file_vars.get(base, tk.StringVar()).get() != "— no video —"
                and self._aaf_source_audio_disp_vars.get(base, tk.StringVar()).get() != "— no audio —")
        ]
        if not sources_to_sync:
            messagebox.showwarning("Nothing to sync",
                "Tick the 'sync' checkbox on at least one source that has both a video "
                "and a reference audio file assigned.")
            return
        # Gate concurrency: cpu_count-1 syncs running simultaneously (same
        # scheme as reconcile), so we don't saturate disk I/O or CPU.
        _cpu = os.cpu_count() or 2
        _sem = threading.Semaphore(max(1, _cpu - 1))
        for base in sources_to_sync:
            self._aaf_do_sync(base, _sem=_sem)

    def _aaf_update_slot_count(self, base, delta):
        """Increment or decrement the slot counter in-place — no row rebuild."""
        old_n = self._aaf_source_slot_counts.get(base, 1)
        new_n = max(1, old_n + delta)
        if new_n == old_n:
            return
        self._aaf_source_slot_counts[base] = new_n

        # Trim / extend extra_vars to match new count
        evs = self._aaf_source_extra_vars.get(base, [])
        del evs[new_n - 1:]
        while len(evs) < new_n - 1:
            evs.append(tk.StringVar(value="— no video —"))
        self._aaf_source_extra_vars[base] = evs

        sv = self._aaf_source_file_vars.get(base)
        n_filled = 0
        if sv and sv.get() != "— no video —":
            n_filled += 1
        n_filled += sum(1 for ev in evs if ev.get() != "— no video —")
        partial = new_n > 1 and n_filled < new_n

        # ── Update N label ────────────────────────────────────────────────────
        try:
            n_lbl = self._aaf_ctr_n_labels.get(base)
            if n_lbl and n_lbl.winfo_exists():
                n_lbl.config(text=str(new_n), fg=WARN if partial else TEXT)
        except Exception:
            pass

        # ── Update minus button state ─────────────────────────────────────────
        try:
            mb = self._aaf_ctr_minus_btns.get(base)
            if mb and mb.winfo_exists():
                mb.config(fg=TEXT if new_n > 1 else BORDER,
                          cursor="hand2" if new_n > 1 else "")
        except Exception:
            pass

        # ── Update video button label ─────────────────────────────────────────
        try:
            btn_lbl = self._aaf_vid_btn_labels.get(base)
            if btn_lbl and btn_lbl.winfo_exists():
                if new_n == 1:
                    new_text = sv.get() if sv else "— no video —"
                else:
                    clip_cnt = getattr(self, "_aaf_source_clip_counts", {}).get(base, 0)
                    new_text = "{}/{} files".format(n_filled, new_n)
                    if clip_cnt > 0:
                        new_text += "  ·  {} clips".format(clip_cnt)
                new_fg = WARN if partial else (
                    SUB if (not sv or sv.get() == "— no video —") else TEXT)
                btn_lbl.config(text=new_text, fg=new_fg)
        except Exception:
            pass

    def _aaf_open_video_multiselect(self, base, anchor_widget):
        """Open a scrollable listbox to assign video files to a source token.

        The slot counter (N) is intentionally NOT modified here — only the file
        assignments change.  When N=1 the listbox uses SINGLE select mode;
        when N>1 it uses MULTIPLE.  Selected files are clamped to N slots.
        """
        options_bn = [self._aaf_vid_label(p) for p in self._aaf_video_paths]
        if not options_bn:
            return
        sv        = self._aaf_source_file_vars[base]
        extra_evs = self._aaf_source_extra_vars.get(base, [])
        n_slots   = self._aaf_source_slot_counts.get(base, 1)

        # First listbox row is an explicit un-assign sentinel.  Without
        # it there was no way to clear a wrongly-assigned video — a
        # SINGLE-select listbox always keeps one item picked, and a
        # MULTIPLE one needs a "clear" affordance.  Clicking this row
        # (single mode) or selecting it (multi mode) un-assigns.
        _NONE_ROW = "  ⊘  (none — unassign)"

        current_set = set()
        if sv.get() != "— no video —":
            current_set.add(sv.get())
        for ev in extra_evs:
            if ev.get() != "— no video —":
                current_set.add(ev.get())

        popup = tk.Toplevel(self)
        popup.overrideredirect(True)
        popup.config(bg=BORDER)

        try:
            anchor_widget.update_idletasks()
            ax = anchor_widget.winfo_rootx()
            ay = anchor_widget.winfo_rooty() + anchor_widget.winfo_height()
            aw = anchor_widget.winfo_width()
        except Exception:
            ax, ay, aw = 200, 200, 200
        popup.geometry("+{}+{}".format(ax, ay))

        inner = tk.Frame(popup, bg=SURF2, padx=6, pady=6)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        list_frame = tk.Frame(inner, bg=SURF2)
        list_frame.pack(fill="both", expand=True)

        _max_rows  = 12
        _lb_w      = max(aw - 20, 200)
        _sel_mode  = tk.SINGLE if n_slots == 1 else tk.MULTIPLE
        _hint      = ("Click to select  \u2022  Enter to apply" if n_slots == 1
                      else "Click to select  \u2022  Shift/Ctrl for multi  \u2022  Enter to apply")

        sb = tk.Scrollbar(list_frame, orient="vertical")
        lb = tk.Listbox(
            list_frame,
            selectmode=_sel_mode,
            bg=SURF3, fg=TEXT, font=FB,
            selectbackground=ACCENT, selectforeground=BG,
            highlightthickness=0, bd=0, relief="flat",
            activestyle="dotbox",
            yscrollcommand=sb.set,
            height=min(len(options_bn) + 1, _max_rows),
            width=int(_lb_w // 9),
        )
        sb.config(command=lb.yview)
        lb.pack(side="left", fill="both", expand=True)
        if len(options_bn) + 1 > _max_rows:
            sb.pack(side="right", fill="y")

        # Row 0 = un-assign sentinel; rows 1.. = the video files.  File
        # index i therefore lives at listbox index i+1.
        lb.insert(tk.END, _NONE_ROW)
        for bn in options_bn:
            lb.insert(tk.END, "  " + bn)
        for i, bn in enumerate(options_bn):
            if bn in current_set:
                lb.selection_set(i + 1)

        def _scroll(e):
            lb.yview_scroll(int(-1 * (e.delta / 120)), "units")
        lb.bind("<MouseWheel>", _scroll)

        def _enforce_max(e):
            """Block selection of a new item once N slots are filled.
            The sentinel row (index 0) is always allowed — it clears."""
            if n_slots <= 1:
                return   # SINGLE mode handles itself
            idx = lb.nearest(e.y)
            if idx <= 0 or idx > len(options_bn):
                return   # sentinel row or out of range — always allow
            # Count only real file rows (index >= 1) toward the cap.
            real_sel = [i for i in lb.curselection() if i >= 1]
            if len(real_sel) >= n_slots and idx not in lb.curselection():
                return "break"   # at capacity and this item isn't selected — block
        lb.bind("<ButtonPress-1>", _enforce_max, add=True)

        if n_slots == 1:
            # Single-select: close immediately on item click — behaves like a
            # normal dropdown where choosing an item dismisses the list.
            lb.bind("<ButtonRelease-1>", lambda e: self.after(0, _apply))

        tk.Label(inner, text=_hint, font=(_SANS, 8),
                 bg=SURF2, fg=SUB).pack(anchor="w", pady=(4, 0))

        _closed = [False]

        def _apply():
            if _closed[0]:
                return
            _closed[0] = True

            # ── Assign selected files to existing slots — counter unchanged ──
            # Row 0 is the un-assign sentinel: if it's in the selection,
            # treat the whole thing as "clear".  Otherwise map listbox
            # rows back to files (file i is at listbox index i+1).
            _sel = lb.curselection()
            if 0 in _sel:
                selected = []
            else:
                selected = [options_bn[i - 1] for i in _sel if i >= 1]
            selected = selected[:n_slots]          # clamp to N — never exceeds

            # Ensure extra_vars list matches current N (may have drifted)
            evs = self._aaf_source_extra_vars.get(base, [])
            del evs[n_slots - 1:]
            while len(evs) < n_slots - 1:
                evs.append(tk.StringVar(value="— no video —"))
            self._aaf_source_extra_vars[base] = evs

            if selected:
                sv.set(selected[0])
                for i, ev in enumerate(evs):
                    ev.set(selected[i + 1] if i + 1 < len(selected) else "— no video —")
            else:
                sv.set("— no video —")
                for ev in evs:
                    ev.set("— no video —")

            popup.grab_release()
            popup.destroy()

            # In-place label update via the shared helper (also used by
            # the sv 'write' trace, so picker apply + programmatic
            # auto-match both keep the label in sync with sv).  If the
            # button widget is gone, fall back to a full row rebuild.
            btn_lbl = getattr(self, "_aaf_vid_btn_labels", {}).get(base)
            if btn_lbl and btn_lbl.winfo_exists():
                # The sv.set() calls above already fired the 'write'
                # trace which called the helper, but call it again here
                # in case any path bypassed the trace (e.g. setting sv
                # to the same value it already had — Tk skips the trace).
                self._aaf_refresh_vid_btn_label(base)
                return
            self._rebuild_aaf_source_rows()

        def _cancel():
            if _closed[0]:
                return
            _closed[0] = True
            popup.grab_release()
            popup.destroy()

        popup.grab_set()

        def _outside_release(e):
            # ButtonRelease events on children (listbox, scrollbar) bubble up
            # to this Toplevel binding.  Only close if the release was actually
            # outside the popup's screen bounds.
            try:
                if (popup.winfo_rootx() <= e.x_root <= popup.winfo_rootx() + popup.winfo_width()
                        and popup.winfo_rooty() <= e.y_root <= popup.winfo_rooty() + popup.winfo_height()):
                    return   # inside — keep popup open
            except Exception:
                pass
            _apply()

        popup.bind("<ButtonRelease-1>", _outside_release)
        popup.bind("<Return>",          lambda e: _apply())
        popup.bind("<Escape>",          lambda e: _cancel())
        lb.bind("<Return>",           lambda e: _apply())
        lb.focus_set()

    def _aaf_hide_source(self, base):
        """Hide a source from the assignment list (reversible — source preserved in AAF data)."""
        self._aaf_push_full_undo()
        if not hasattr(self, "_aaf_hidden_sources"):
            self._aaf_hidden_sources = set()
        self._aaf_hidden_sources.add(base)
        self._rebuild_aaf_source_rows()

    def _aaf_unhide_source(self, base):
        """Make a previously hidden source visible again."""
        self._aaf_push_full_undo()
        if hasattr(self, "_aaf_hidden_sources"):
            self._aaf_hidden_sources.discard(base)
        self._rebuild_aaf_source_rows()

    def _aaf_remove_source(self, base, container):
        """Legacy — now delegates to hide (keeps source in AAF data)."""
        self._aaf_hide_source(base)

    def _aaf_remove_unassigned_sources(self):
        """Remove all sources that have no video file assigned (legacy helper)."""
        self._aaf_remove_unassigned()

    def _aaf_remove_unassigned(self):
        """
        Clean up three categories of unused items:
          1. Source clips with no video assigned
          2. Video files in the pool not assigned to any source
          3. Audio files in the pool not used as sync audio for any source
        Confirms once before doing anything.
        """
        # 1 — unassigned sources (all slots empty)
        dead_sources = [b for b in self._aaf_sources
                        if self._aaf_source_file_vars.get(
                            b, tk.StringVar(value="— no video —")).get() == "— no video —"
                        and all(ev.get() == "— no video —"
                                for ev in getattr(self, "_aaf_source_extra_vars", {}).get(b, []))]

        # 2 — video pool files not used by any source (slot 1 or extra slots)
        used_vid_fns = {sv.get() for sv in self._aaf_source_file_vars.values()
                        if sv.get() not in ("— no video —", "")}
        for _evs in getattr(self, "_aaf_source_extra_vars", {}).values():
            used_vid_fns.update(
                ev.get() for ev in _evs if ev.get() not in ("— no video —", ""))
        dead_vids = [p for p in self._aaf_video_paths
                     if self._aaf_vid_label(p) not in used_vid_fns]

        # 3 — audio pool files not used as sync reference by any source
        used_aud_paths = {sv.get() for sv in self._aaf_source_syncaudio_vars.values()
                          if sv.get()}
        dead_auds = [p for p in self._aaf_audio_paths
                     if p not in used_aud_paths]

        if not dead_sources and not dead_vids and not dead_auds:
            messagebox.showinfo("Clean Up", "Nothing to remove — everything is assigned.")
            return

        lines = []
        if dead_sources:
            lines.append("{} unassigned source{}:".format(
                len(dead_sources), "s" if len(dead_sources) != 1 else ""))
            lines += ["  " + b for b in dead_sources[:5]]
            if len(dead_sources) > 5:
                lines.append("  … and {} more".format(len(dead_sources) - 5))
        if dead_vids:
            lines.append("{} unused video file{}".format(
                len(dead_vids), "s" if len(dead_vids) != 1 else ""))
        if dead_auds:
            lines.append("{} unused audio file{}".format(
                len(dead_auds), "s" if len(dead_auds) != 1 else ""))

        if not messagebox.askyesno("Clean Up", "\n".join(lines) + "\n\nHide unassigned sources / remove unused pool files?"):
            return

        self._aaf_push_full_undo()
        if not hasattr(self, "_aaf_hidden_sources"):
            self._aaf_hidden_sources = set()
        self._aaf_hidden_sources.update(dead_sources)

        # For each removed pool file, mirror _aaf_remove_video/_aaf_remove_audio:
        # drop from the path list AND destroy its row Frame in the pool
        # list view AND drop from the path→row registry.  Without this,
        # the count label said "3 files" while the list still showed all
        # the original rows (reported).
        vid_rows = getattr(self, "_aaf_video_rows", {})
        for p in dead_vids:
            if p in self._aaf_video_paths:
                self._aaf_video_paths.remove(p)
            _row = vid_rows.pop(p, None)
            if _row is not None:
                try: _row.destroy()
                except tk.TclError: pass
        nv = len(self._aaf_video_paths)
        if hasattr(self, "_aaf_count_lbl"):
            self._aaf_count_lbl.config(
                text="{} file{}".format(nv, "s" if nv != 1 else ""))

        aud_rows = getattr(self, "_aaf_audio_rows", {})
        for p in dead_auds:
            if p in self._aaf_audio_paths:
                self._aaf_audio_paths.remove(p)
            _row = aud_rows.pop(p, None)
            if _row is not None:
                try: _row.destroy()
                except tk.TclError: pass
        na = len(self._aaf_audio_paths)
        if hasattr(self, "_aaf_audio_count_lbl"):
            self._aaf_audio_count_lbl.config(
                text="{} file{}".format(na, "s" if na != 1 else ""))

        # Sequence-preset dropdown still offers W×H@fps entries detected
        # from videos that just got removed — same refresh _aaf_remove_video
        # does on the single-file path.
        if dead_vids:
            try: self._aaf_update_seq_presets()
            except Exception: pass

        self._rebuild_aaf_source_rows()

    def _aaf_col_sash(self, parent, col_key, bg):
        """
        Create a thin drag-handle between column headers.
        Dragging it adjusts _aaf_col_widths[col_key] and triggers a row rebuild.
        """
        sash = tk.Frame(parent, bg=SURF3, width=5, cursor="sb_h_double_arrow")
        sash.pack(side="left", fill="y", padx=0)
        drag = {}
        def _press(e, k=col_key):
            drag["x0"] = e.x_root
            drag["w0"] = getattr(self, "_aaf_col_widths", {}).get(k, 150)
        def _release(e, k=col_key):
            delta = e.x_root - drag.get("x0", e.x_root)
            old   = drag.get("w0", 150)
            new_w = max(60, old + delta)
            if not hasattr(self, "_aaf_col_widths"):
                self._aaf_col_widths = {}
            self._aaf_col_widths[k] = new_w
            self._rebuild_aaf_source_rows()
        sash.bind("<ButtonPress-1>",   _press)
        sash.bind("<ButtonRelease-1>", _release)
        # Visual feedback on hover
        sash.bind("<Enter>",  lambda e, w=sash: w.config(bg=ACCENT))
        sash.bind("<Leave>",  lambda e, w=sash: w.config(bg=SURF3))

    def _aaf_col_reorder(self, clicked_idx, col_name):
        """
        Reorder ASSIGN columns by swapping the clicked column with its neighbour.
        Each click on a column header rotates it one position to the right
        (wraps around).
        """
        order = getattr(self, "_aaf_col_order", ["video", "tracks"])
        if col_name not in order:
            return
        idx = order.index(col_name)
        # Swap with next (wraps to 0)
        next_idx = (idx + 1) % len(order)
        order[idx], order[next_idx] = order[next_idx], order[idx]
        self._aaf_col_order = order
        self._rebuild_aaf_source_rows()

    def _aaf_sort_by(self, col):
        """Sort the source list by col; second click on same col reverses direction."""
        if getattr(self, "_aaf_sort_col", None) == col:
            self._aaf_sort_dir = "desc" if getattr(self, "_aaf_sort_dir", "asc") == "asc" else "asc"
        else:
            self._aaf_sort_col = col
            self._aaf_sort_dir = "asc"
        self._rebuild_aaf_source_rows()

    def _aaf_refresh_sync_btn(self, base, btn):
        """Set button text/colour to reflect the stored sync state."""
        label = self._aaf_source_sync_label_vars.get(base)
        text  = label.get() if label else ""
        if not text:
            btn.config(text="SYNC", fg=SUB)
        elif text.startswith("\u2713"):          # ✓ green
            btn.config(text=text, fg=SUCCESS)
        elif text.startswith("\u26a0"):          # ⚠ orange or red
            # "verify ref audio!" means a large (suspicious) offset was detected
            clr = "#e05050" if ("no match" in text or "verify ref" in text) else "#e0a030"
            btn.config(text=text, fg=clr)
        else:
            btn.config(text=text, fg=SUB)


    def _aaf_refresh_try_next_btn(self, base):
        """Show / hide / restyle the inline TRY NEXT button on the
        sync row.  Visible only when auto-sync produced one or more
        alternate candidates; the label includes the cycle position
        so the user always knows which candidate the offset column
        currently reflects."""
        btn = getattr(self, "_aaf_try_next_btns", {}).get(base)
        if btn is None:
            return
        cands = getattr(self, "_aaf_source_sync_candidates", {}).get(
            base, [])
        if len(cands) <= 1:
            try:
                btn.pack_forget()
            except tk.TclError:
                pass
            return
        idx = getattr(self, "_aaf_source_sync_cand_idx", {}).get(base, 0)
        try:
            btn.config(text="⏭ NEXT  ({}/{})".format(
                idx + 1, len(cands)))
            if not btn.winfo_ismapped():
                sync_btn = self._aaf_sync_btns.get(base)
                if sync_btn is not None and sync_btn.winfo_ismapped():
                    btn.pack(side="left", padx=(2, 0), after=sync_btn)
                else:
                    btn.pack(side="left", padx=(2, 0))
        except tk.TclError:
            pass

    def _aaf_cycle_candidate(self, base):
        """Advance to the next sync candidate for `base` and apply its
        offset to the row.  Wraps around to the primary auto pick
        after the last alternate so the user can always return to the
        starting state without re-running SYNC.

        Cycling de-confirms the row (state -> "auto") since the
        offset is now unaccepted relative to the new pick."""
        if self._aaf_sync_locked_vars.get(
                base, tk.BooleanVar()).get():
            return
        cands = getattr(self, "_aaf_source_sync_candidates", {}).get(
            base, [])
        if len(cands) <= 1:
            return
        if not hasattr(self, "_aaf_source_sync_cand_idx"):
            self._aaf_source_sync_cand_idx = {}
        idx_cur = self._aaf_source_sync_cand_idx.get(base, 0)
        idx_new = (idx_cur + 1) % len(cands)
        self._aaf_source_sync_cand_idx[base] = idx_new
        offset, conf = cands[idx_new]

        # Push to offset column
        ov = self._aaf_source_offset_vars.get(base)
        if ov:
            try:
                ov.set("{:.3f}".format(offset))
            except tk.TclError:
                pass

        # Rebuild the SYNC button verdict label with the new offset
        # and confidence.  Mirrors the tier logic in
        # _aaf_do_sync._apply so the visual cue (verify recommended /
        # required / etc.) tracks the candidate's confidence, not the
        # primary's.
        pct = int(conf * 100)
        large = abs(offset) > 30.0
        if large:
            lbl = "⚠ {:.3f}s ({:d}%)  — verify ref audio!".format(
                offset, pct)
        elif conf >= 0.95:
            lbl = "✓ {:.3f}s ({:d}%)".format(offset, pct)
        elif conf >= 0.85:
            lbl = "⚠ {:.3f}s ({:d}%)  — verify recommended".format(
                offset, pct)
        elif conf >= 0.50:
            lbl = "⚠ {:.3f}s ({:d}%)  — verify required".format(
                offset, pct)
        else:
            lbl = "⚠ {:.3f}s ({:d}%)  — low conf, must verify".format(
                offset, pct)
        lbl = "[{}/{}] ".format(idx_new + 1, len(cands)) + lbl

        lv = self._aaf_source_sync_label_vars.get(base)
        if lv:
            lv.set(lbl)
        btn = self._aaf_sync_btns.get(base)
        if btn:
            self._aaf_refresh_sync_btn(base, btn)
        self._aaf_set_sync_state(base, "auto")
        self._aaf_refresh_try_next_btn(base)

        # Live audition: if a QA preview is already playing for this
        # source, re-extract + restart it at the NEW offset so the user
        # hears the candidate immediately instead of having to stop and
        # restart playback manually.  Only restart when this exact
        # source is the one currently auditioning.
        if getattr(self, "_aaf_qa_playing", None) == base:
            try:
                self._aaf_qa_stop()
                self._aaf_qa_play(base)
            except Exception:
                pass

    def _aaf_do_sync(self, base, _sem=None):
        """Run sync detection for this source using the selected reference audio."""
        if self._aaf_sync_locked_vars.get(base, tk.BooleanVar()).get():
            return
        self._aaf_push_undo(base)
        btn = self._aaf_sync_btns.get(base)

        # The reference audio is whatever the user has selected in the dropdown.
        # If nothing is selected yet, try one last auto-match.
        audio_var = self._aaf_source_syncaudio_vars.get(base)
        aud_disp  = self._aaf_source_audio_disp_vars.get(base)
        if audio_var and not audio_var.get() and self._aaf_audio_paths:
            ap = match_source_to_video(base, self._aaf_audio_paths)
            if ap:
                audio_var.set(ap)
                if aud_disp:
                    aud_disp.set(self._aaf_aud_label(ap))

        if not audio_var or not audio_var.get():
            messagebox.showwarning("No Reference Audio",
                "Select a reference audio file for this source using the dropdown,\n"
                "or add audio files to the reference audio pool.")
            return

        # Collect all assigned video files for this source (slot 1 + extras).
        # For N>1 files, sync is run against every file and the best confidence
        # winner is promoted to slot-1 — the user doesn't need to know which
        # file has the relevant audio in advance.
        path_by_fn = {self._aaf_vid_label(p): p for p in self._aaf_video_paths}
        _all_vps = []
        fn = self._aaf_source_file_vars.get(base, tk.StringVar()).get()
        if fn != "— no video —" and fn in path_by_fn:
            _all_vps.append(path_by_fn[fn])
        for _ev in getattr(self, "_aaf_source_extra_vars", {}).get(base, []):
            fn2 = _ev.get()
            if fn2 != "— no video —" and fn2 in path_by_fn:
                vp2 = path_by_fn[fn2]
                if vp2 not in _all_vps:
                    _all_vps.append(vp2)

        if not _all_vps:
            messagebox.showwarning("No Video",
                "Assign a video file to this source before syncing.")
            return

        ap = audio_var.get()
        if not os.path.isfile(ap):
            messagebox.showwarning("Missing Audio",
                "Reference audio file not found:\n{}".format(ap))
            return

        start_offset = 0.0
        # Tk vars must be read on the main thread; cache the flag here so
        # the worker thread doesn't touch Tk state.
        _has_slate_flag = bool(self._aaf_source_has_slate_vars.get(
            base, tk.BooleanVar()).get())

        # Disable button and start spinner animation while running
        if btn:
            btn.config(text="SYNCING \u00b7  ", state="disabled", fg=SUB)

        _animating = [True]
        _spin_frames = ["SYNCING \u00b7  ", "SYNCING \u00b7\u00b7 ", "SYNCING \u00b7\u00b7\u00b7"]
        _spin_idx    = [0]

        def _spin_tick():
            if not _animating[0]:
                return
            if btn:
                try:
                    btn.config(text=_spin_frames[_spin_idx[0] % len(_spin_frames)])
                except Exception:
                    return
            _spin_idx[0] += 1
            self.after(350, _spin_tick)

        self.after(350, _spin_tick)

        _ap_basename = basename(ap)
        _n_vps = len(_all_vps)

        def _run():
            if _sem is not None:
                _sem.acquire()
            try:
                if _n_vps == 1:
                    off, conf, alts = detect_sync_offset(
                        _all_vps[0], ap,
                        probe_duration=300.0,
                        start_offset=start_offset,
                        return_candidates=True,
                        has_slate=_has_slate_flag)
                    return off, conf, _all_vps[0], alts
                # Multi-file: probe all candidates in parallel, pick best confidence
                from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _asc
                results = []
                with _TPE(max_workers=_n_vps) as _ex:
                    _fmap = {_ex.submit(detect_sync_offset, _vp, ap,
                                        probe_duration=300.0,
                                        start_offset=start_offset,
                                        return_candidates=True,
                                        has_slate=_has_slate_flag): _vp
                             for _vp in _all_vps}
                    for _f in _asc(_fmap):
                        try:
                            _off, _conf, _alts = _f.result()
                            results.append((_off, _conf, _fmap[_f], _alts))
                        except Exception:
                            pass
                if not results:
                    raise RuntimeError(
                        "Sync failed on all {} video files".format(_n_vps))
                return max(results, key=lambda r: r[1])
            finally:
                if _sem is not None:
                    _sem.release()

        def _done(fut):
            _animating[0] = False
            try:
                offset, confidence, best_vp, alt_candidates = fut.result()
            except Exception as exc:
                def _err():
                    lv = self._aaf_source_sync_label_vars.get(base)
                    if lv: lv.set("\u26a0 error")
                    if btn:
                        btn.config(text="\u26a0 error", fg="#e05050", state="normal")
                    messagebox.showerror("Sync Failed", str(exc))
                self._ui(_err)
                return

            def _apply():
                # For multi-file sources, promote the winning file to slot-1 so
                # the build step always uses the sync-verified file for all clips.
                # Use _aaf_vid_label (not bare basename) so the stored value
                # matches the dropdown options on a multicam basename collision —
                # writing the bare basename would fall outside `options` and the
                # row's rebuild guard at line 968 would silently wipe the
                # assignment on the next rebuild.
                best_fn = self._aaf_vid_label(best_vp)
                sv = self._aaf_source_file_vars.get(base)
                if sv and sv.get() != best_fn:
                    sv.set(best_fn)
                    # For single-slot view, update the button label directly
                    n_slots = getattr(self, "_aaf_source_slot_counts", {}).get(base, 1)
                    if n_slots == 1:
                        btn_lbl = getattr(self, "_aaf_vid_btn_labels", {}).get(base)
                        if btn_lbl:
                            try:
                                btn_lbl.config(text=best_fn)
                            except Exception:
                                pass

                offset_var = self._aaf_source_offset_vars.get(base)
                if offset_var:
                    offset_var.set("{:.3f}".format(offset))
                sync_var = self._aaf_source_sync_vars.get(base)
                if sync_var:
                    sync_var.set(True)

                # Store the unified candidate list (primary auto pick at
                # index 0, alternates after) plus the active cycle index
                # so the inline TRY NEXT button can audition them
                # without opening the ALIGN dialog.
                if not hasattr(self, "_aaf_source_sync_candidates"):
                    self._aaf_source_sync_candidates = {}
                if not hasattr(self, "_aaf_source_sync_cand_idx"):
                    self._aaf_source_sync_cand_idx = {}
                self._aaf_source_sync_candidates[base] = (
                    [(float(offset), float(confidence))]
                    + [(float(t), float(c))
                       for (t, c) in (alt_candidates or [])
                       if abs(float(t) - float(offset)) > 0.01])
                self._aaf_source_sync_cand_idx[base] = 0

                pct = int(confidence * 100)
                # Confidence tiers calibrated from real-world false-positive
                # rates in sync_corrections.jsonl: 0.95+ was consistently
                # correct, 0.85-0.94 was usually within ~0.5 s of truth,
                # below 0.85 had a meaningful rate of multi-second errors
                # that need manual verification.  Very large offsets (>30 s)
                # are almost always wrong-reference-file matches — flag
                # them distinctly regardless of confidence.
                large = abs(offset) > 30.0
                if large:
                    lbl = "\u26a0 {:.3f}s ({:d}%)  — verify ref audio!".format(offset, pct)
                elif confidence >= 0.95:
                    lbl = "\u2713 {:.3f}s ({:d}%)".format(offset, pct)
                elif confidence >= 0.85:
                    lbl = "\u26a0 {:.3f}s ({:d}%)  — verify recommended".format(offset, pct)
                elif confidence >= 0.50:
                    lbl = "\u26a0 {:.3f}s ({:d}%)  — verify required".format(offset, pct)
                else:
                    lbl = "\u26a0 {:.3f}s ({:d}%)  — low conf, must verify".format(offset, pct)

                lv = self._aaf_source_sync_label_vars.get(base)
                if lv: lv.set(lbl)
                if btn:
                    btn.config(state="normal")
                    self._aaf_refresh_sync_btn(base, btn)

                # Mark as auto-synced (amber dot) until manually confirmed
                self._aaf_set_sync_state(base, "auto")
                # Show / refresh the inline TRY NEXT button on the row
                # so the user can cycle through alternates without
                # opening ALIGN.
                try:
                    self._aaf_refresh_try_next_btn(base)
                except Exception:
                    pass

            self._ui(_apply)

        from concurrent.futures import ThreadPoolExecutor
        ex  = ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(_run)
        fut.add_done_callback(_done)
        ex.shutdown(wait=False)

    # ── Sync Preview ────────────────────────────────────────────────────────

    def _aaf_open_align(self, base):
        """Open the waveform alignment dialog for manual verification/adjustment."""
        if self._aaf_sync_locked_vars.get(base, tk.BooleanVar()).get():
            return
        fn = self._aaf_source_file_vars.get(base, tk.StringVar()).get()
        if fn == "— no video —":
            messagebox.showwarning("No Video",
                "Assign a video file to this source before opening alignment.")
            return
        path_by_fn = {self._aaf_vid_label(p): p for p in self._aaf_video_paths}
        vp = path_by_fn.get(fn)
        if not vp:
            return
        ap = self._aaf_source_syncaudio_vars.get(base, tk.StringVar()).get()
        if not ap or not os.path.isfile(ap):
            messagebox.showwarning("No Reference Audio",
                "Select a reference audio file for this source before aligning.")
            return
        try:
            offset = float(self._aaf_source_offset_vars[base].get())
        except (KeyError, ValueError):
            offset = 0.0
        self._aaf_open_sync_preview(base, vp, ap, offset)

    def _aaf_open_sync_preview(self, base, video_path, audio_path, offset):
        """Open the waveform sync-preview dialog for manual verification."""
        def _on_accept(accepted_offset):
            # ── Log correction for feedback analysis ──────────────────────
            try:
                import json as _json, datetime as _dt, os as _os
                _cache_dir = _os.path.join(
                    _os.path.dirname(_os.path.abspath(__file__)), ".pb_cache")
                _os.makedirs(_cache_dir, exist_ok=True)
                _correction_ms = round(abs(accepted_offset - offset) * 1000, 1)
                _verdict = ("exact"  if _correction_ms < 50  else
                            "close"  if _correction_ms < 500 else
                            "wrong")
                # Record the cal_global active at the time of this run so
                # detect_sync_offset can decode the auto_T correctly even if
                # _T_CAL_GLOBAL changes later.
                try:
                    from parsers import detect_sync_offset as _dso
                    import inspect as _ins
                    _src = _ins.getsource(_dso)
                    import re as _re
                    _m = _re.search(r"_T_CAL_GLOBAL\s*=\s*([-+]?[\d.]+)", _src)
                    _cal_global = float(_m.group(1)) if _m else -0.006
                except Exception:
                    _cal_global = -0.006

                _entry = {
                    "ts":            _dt.datetime.now().isoformat(timespec="seconds"),
                    "source":        base,
                    "video":         os.path.basename(video_path),
                    "audio":         os.path.basename(audio_path),
                    "auto_T":        round(offset, 4),
                    "accepted_T":    round(accepted_offset, 4),
                    "correction_ms": _correction_ms,
                    "verdict":       _verdict,
                    "cal_global":    _cal_global,
                }
                with open(_os.path.join(_cache_dir, "sync_corrections.jsonl"),
                          "a", encoding="utf-8") as _fh:
                    _fh.write(_json.dumps(_entry) + "\n")
            except Exception:
                pass

            # ── Background signal verification ────────────────────────────
            # Run a Stage-3 xcorr at the accepted offset to confirm waveform
            # agreement.  Appends a "verified" entry to sync_corrections.jsonl
            # so future algorithm runs have signal-backed ground truth.
            def _verify_bg(vp=video_path, ap=audio_path,
                           acc=accepted_offset, src=base):
                try:
                    import json as _jv, datetime as _dv, os as _ov
                    _v_T, _v_conf = verify_sync_at_offset(vp, ap, acc)
                    _cache = _ov.path.join(
                        _ov.path.dirname(_ov.path.abspath(__file__)), ".pb_cache")
                    _ve = {
                        "ts":            _dv.datetime.now().isoformat(timespec="seconds"),
                        "source":        src,
                        "video":         _ov.path.basename(vp),
                        "audio":         _ov.path.basename(ap),
                        "accepted_T":    round(acc, 4),
                        "verified_T":    _v_T,
                        "verified_conf": _v_conf,
                        "verdict":       "verified",
                    }
                    with open(_ov.path.join(_cache, "sync_corrections.jsonl"),
                              "a", encoding="utf-8") as _fv:
                        _fv.write(_jv.dumps(_ve) + "\n")
                except Exception:
                    pass
            import threading as _thr
            _thr.Thread(target=_verify_bg, daemon=True).start()
            # ── Apply accepted offset to UI ────────────────────────────────
            self._aaf_push_undo(base)
            ov = self._aaf_source_offset_vars.get(base)
            if ov:
                ov.set("{:.3f}".format(accepted_offset))
            sv = self._aaf_source_sync_vars.get(base)
            if sv:
                sv.set(True)
            lv = self._aaf_source_sync_label_vars.get(base)
            if lv:
                lv.set("\u2713 {:.3f}s (manual)".format(accepted_offset))
            btn = self._aaf_sync_btns.get(base)
            if btn:
                self._aaf_refresh_sync_btn(base, btn)
            # Mark as manually confirmed (green dot) and auto-lock
            self._aaf_set_sync_state(base, "manual")
            self._aaf_apply_lock(base, True)

        # Stop any in-progress sync preview playback before opening ALIGN.
        # The user clicking ALIGN signals they want to inspect — silence first.
        try:
            import winsound as _ws
            _ws.PlaySound(None, _ws.SND_PURGE)
        except Exception:
            pass

        # Close any existing preview for this source before opening a fresh one
        prev = self._aaf_sync_previews.get(base)
        if prev is not None:
            try: prev.close()
            except Exception: pass

        dlg = SyncPreviewDialog(
            self, video_path, audio_path,
            initial_offset=offset,
            on_accept=_on_accept,
            source_name=base)
        self._aaf_sync_previews[base] = dlg

    # ── Sync helpers ─────────────────────────────────────────────────────────

    _STATE_DOT_COLORS = {"": SUB, "auto": WARN, "manual": SUCCESS}

    @staticmethod
    def _confirm_btn_style(state):
        """Return (text, fg) for the ACCEPT confirm button given sync state."""
        if state == "auto":
            return "\u2713 ACCEPT", WARN
        if state == "manual":
            return "\u2713 ACCEPTED", SUCCESS
        return "\u2713 ACCEPT", SUB

    def _aaf_sync_snapshot(self, base):
        """Return a dict capturing the current sync state of *base*."""
        return {
            "offset":  self._aaf_source_offset_vars.get(base, tk.StringVar()).get(),
            "state":   self._aaf_sync_state_vars.get(base, tk.StringVar()).get(),
            "locked":  self._aaf_sync_locked_vars.get(base, tk.BooleanVar()).get(),
            "enabled": self._aaf_source_sync_vars.get(base, tk.BooleanVar()).get(),
            "label":   self._aaf_source_sync_label_vars.get(base, tk.StringVar()).get(),
        }

    def _aaf_push_undo(self, base):
        """Snapshot current state onto the undo stack; clear redo stack."""
        stack = getattr(self, "_sync_undo_stack", None)
        if stack is None:
            self._sync_undo_stack = []
            self._sync_redo_stack = []
            stack = self._sync_undo_stack
        stack.append((base, self._aaf_sync_snapshot(base)))
        if len(stack) > 30:
            stack.pop(0)
        self._sync_redo_stack.clear()

    def _aaf_full_snapshot(self):
        """Capture the complete source list and all state for full undo (e.g. source removal)."""
        snap = {
            "__type__": "full",
            "sources": list(self._aaf_sources),
            "hidden_sources": sorted(getattr(self, "_aaf_hidden_sources", set())),
            "assignments": {b: sv.get()
                            for b, sv in self._aaf_source_file_vars.items()},
            "sync": {},
        }
        for b in self._aaf_sources:
            snap["sync"][b] = {
                "enabled":    self._aaf_source_sync_vars.get(b, tk.BooleanVar()).get(),
                "audio_path": self._aaf_source_syncaudio_vars.get(b, tk.StringVar()).get(),
                "audio_disp": self._aaf_source_audio_disp_vars.get(b, tk.StringVar()).get(),
                "offset":     self._aaf_source_offset_vars.get(b, tk.StringVar(value="0.000")).get(),
                "label":      self._aaf_source_sync_label_vars.get(b, tk.StringVar()).get(),
                "state":      self._aaf_sync_state_vars.get(b, tk.StringVar()).get(),
                "needs_sync": self._aaf_needs_sync_vars.get(b, tk.BooleanVar()).get(),
                "locked":     self._aaf_sync_locked_vars.get(b, tk.BooleanVar()).get(),
            }
        return snap

    def _aaf_push_full_undo(self):
        """Push a full-state snapshot — used before destructive operations like source removal."""
        stack = getattr(self, "_sync_undo_stack", None)
        if stack is None:
            self._sync_undo_stack = []
            self._sync_redo_stack = []
            stack = self._sync_undo_stack
        stack.append(self._aaf_full_snapshot())
        if len(stack) > 30:
            stack.pop(0)
        self._sync_redo_stack.clear()

    def _aaf_full_restore(self, snap):
        """Restore a full-state snapshot captured by _aaf_full_snapshot."""
        saved_sources = snap.get("sources", [])
        assignments   = snap.get("assignments", {})
        sync_data     = snap.get("sync", {})

        # Restore source list order (only sources present in the snapshot)
        self._aaf_sources[:] = [b for b in saved_sources if b in self._aaf_sources
                                 or b in sync_data]
        self._aaf_hidden_sources = set(snap.get("hidden_sources", []))

        # Re-create any vars that were removed
        options = ["— no video —"] + [self._aaf_vid_label(p) for p in self._aaf_video_paths]
        for b in saved_sources:
            if b not in self._aaf_source_file_vars:
                self._aaf_source_file_vars[b]       = tk.StringVar(value="— no video —")
            if b not in self._aaf_source_sync_vars:
                self._aaf_source_sync_vars[b]       = tk.BooleanVar(value=False)
            if b not in self._aaf_source_syncaudio_vars:
                self._aaf_source_syncaudio_vars[b]  = tk.StringVar(value="")
            if b not in self._aaf_source_audio_disp_vars:
                self._aaf_source_audio_disp_vars[b] = tk.StringVar(value="— no audio —")
            if b not in self._aaf_source_offset_vars:
                self._aaf_source_offset_vars[b]     = tk.StringVar(value="0.000")
            if b not in self._aaf_source_sync_label_vars:
                self._aaf_source_sync_label_vars[b] = tk.StringVar(value="")
            if b not in self._aaf_sync_state_vars:
                self._aaf_sync_state_vars[b]        = tk.StringVar(value="")
            if b not in self._aaf_needs_sync_vars:
                self._aaf_needs_sync_vars[b]        = tk.BooleanVar(value=False)
            if b not in self._aaf_sync_locked_vars:
                self._aaf_sync_locked_vars[b]       = tk.BooleanVar(value=False)

        # Apply saved values
        for b in saved_sources:
            fn = assignments.get(b, "— no video —")
            if fn in options:
                self._aaf_source_file_vars[b].set(fn)
            sd = sync_data.get(b, {})
            self._aaf_source_sync_vars[b].set(sd.get("enabled", False))
            self._aaf_source_syncaudio_vars[b].set(sd.get("audio_path", ""))
            self._aaf_source_audio_disp_vars[b].set(sd.get("audio_disp", "— no audio —"))
            self._aaf_source_offset_vars[b].set(sd.get("offset", "0.000"))
            self._aaf_source_sync_label_vars[b].set(sd.get("label", ""))
            self._aaf_sync_state_vars[b].set(sd.get("state", ""))
            self._aaf_needs_sync_vars[b].set(sd.get("needs_sync", False))
            self._aaf_sync_locked_vars[b].set(sd.get("locked", False))

        self._rebuild_aaf_source_rows()

    def _aaf_sync_restore(self, base, snap):
        """Apply a snapshot dict to *base* and refresh the UI."""
        ov = self._aaf_source_offset_vars.get(base)
        if ov: ov.set(snap.get("offset", "0.000"))
        lv = self._aaf_source_sync_label_vars.get(base)
        if lv: lv.set(snap.get("label", ""))
        sv = self._aaf_source_sync_vars.get(base)
        if sv: sv.set(snap.get("enabled", False))
        lkv = self._aaf_sync_locked_vars.get(base)
        if lkv: lkv.set(snap.get("locked", False))
        self._aaf_set_sync_state(base, snap.get("state", ""))
        btn = self._aaf_sync_btns.get(base)
        if btn:
            self._aaf_refresh_sync_btn(base, btn)
        # If locked state changed, rebuild so button enable/disable updates
        self._rebuild_aaf_source_rows()

    def _aaf_sync_undo(self):
        stack = getattr(self, "_sync_undo_stack", [])
        if not stack:
            return
        entry = stack.pop()
        if isinstance(entry, dict) and entry.get("__type__") == "full":
            redo = self._aaf_full_snapshot()
            getattr(self, "_sync_redo_stack", []).append(redo)
            self._aaf_full_restore(entry)
        else:
            base, snap = entry
            redo_snap = self._aaf_sync_snapshot(base)
            getattr(self, "_sync_redo_stack", []).append((base, redo_snap))
            self._aaf_sync_restore(base, snap)

    def _aaf_sync_redo(self):
        stack = getattr(self, "_sync_redo_stack", [])
        if not stack:
            return
        entry = stack.pop()
        if isinstance(entry, dict) and entry.get("__type__") == "full":
            undo = self._aaf_full_snapshot()
            getattr(self, "_sync_undo_stack", []).append(undo)
            self._aaf_full_restore(entry)
        else:
            base, snap = entry
            undo_snap = self._aaf_sync_snapshot(base)
            getattr(self, "_sync_undo_stack", []).append((base, undo_snap))
            self._aaf_sync_restore(base, snap)

    def _aaf_set_sync_state(self, base, state):
        """Set sync state var and update dot + confirm button colours."""
        ssv = self._aaf_sync_state_vars.get(base)
        if ssv:
            ssv.set(state)
        dot = getattr(self, "_aaf_sync_dot_labels", {}).get(base)
        if dot:
            try:
                dot.config(fg=self._STATE_DOT_COLORS.get(state, SUB))
            except Exception:
                pass
        cfm = getattr(self, "_aaf_confirm_btns", {}).get(base)
        if cfm:
            try:
                text, fg = self._confirm_btn_style(state)
                # Only update colour if not locked (locked items are greyed out)
                locked = self._aaf_sync_locked_vars.get(base, tk.BooleanVar()).get()
                if not locked:
                    cfm.config(text=text, fg=fg)
            except Exception:
                pass

    def _aaf_confirm_sync(self, base):
        """Accept the auto-detected offset, mark as confirmed, and lock."""
        if self._aaf_sync_locked_vars.get(base, tk.BooleanVar()).get():
            return
        state = self._aaf_sync_state_vars.get(base, tk.StringVar()).get()
        if not state:
            return   # nothing to confirm
        self._aaf_qa_stop()
        self._aaf_push_undo(base)
        sv = self._aaf_source_sync_vars.get(base)
        if sv:
            sv.set(True)
        self._aaf_set_sync_state(base, "manual")
        # Auto-lock after accepting so it can't be accidentally changed
        self._aaf_apply_lock(base, True)
        # ── Log acceptance for feedback analysis ──────────────────────────
        try:
            import json as _json, datetime as _dt, os as _os
            _ov = self._aaf_source_offset_vars.get(base)
            _offset = float(_ov.get()) if _ov else 0.0
            _vp = self._aaf_source_file_vars.get(base, tk.StringVar()).get()
            _ap = self._aaf_source_syncaudio_vars.get(base, tk.StringVar()).get()
            _cache_dir = _os.path.join(
                _os.path.dirname(_os.path.abspath(__file__)), ".pb_cache")
            _os.makedirs(_cache_dir, exist_ok=True)
            _entry = {
                "ts":      _dt.datetime.now().isoformat(timespec="seconds"),
                "source":  base,
                "video":   _os.path.basename(_vp) if _vp else "",
                "audio":   _os.path.basename(_ap) if _ap else "",
                "auto_T":  round(_offset, 4),
                "verdict": "accepted",   # user was happy with auto result
            }
            with open(_os.path.join(_cache_dir, "sync_corrections.jsonl"),
                      "a", encoding="utf-8") as _fh:
                _fh.write(_json.dumps(_entry) + "\n")
        except Exception:
            pass

    def _aaf_apply_lock(self, base, locked):
        """Set lock state and refresh lock button + dependent widgets."""
        lkv = self._aaf_sync_locked_vars.get(base)
        if lkv:
            lkv.set(locked)
        lock_btn = getattr(self, "_aaf_lock_btns", {}).get(base)
        if lock_btn:
            try:
                lock_btn.config(text="\U0001f512" if locked else "\U0001f513",
                                fg=WARN if locked else SUB)
            except Exception:
                pass
        # Rebuild so all dependent buttons (SYNC, ALIGN, RESET, apply ck) update
        self._rebuild_aaf_source_rows()

    def _aaf_toggle_lock(self, base):
        """Toggle the sync lock for *base*."""
        current = self._aaf_sync_locked_vars.get(base, tk.BooleanVar()).get()
        self._aaf_push_undo(base)
        self._aaf_apply_lock(base, not current)

    def _aaf_clear_sync_candidates(self, base):
        """Drop the per-source sync candidate list + cycle index and
        refresh the inline TRY NEXT button.  Candidates are measured
        against a SPECIFIC (video, audio) pair — when either changes, or
        sync is reset/invalidated, the stored candidates are stale and
        clicking TRY NEXT would otherwise apply an offset measured
        against the previous file."""
        cands = getattr(self, "_aaf_source_sync_candidates", None)
        if cands is not None:
            cands.pop(base, None)
        idx = getattr(self, "_aaf_source_sync_cand_idx", None)
        if idx is not None:
            idx.pop(base, None)
        try:
            self._aaf_refresh_try_next_btn(base)
        except Exception:
            pass

    def _aaf_reset_sync(self, base):
        """Reset sync state for *base* back to zero."""
        if self._aaf_sync_locked_vars.get(base, tk.BooleanVar()).get():
            return
        self._aaf_push_undo(base)
        ov = self._aaf_source_offset_vars.get(base)
        if ov: ov.set("0.000")
        sv = self._aaf_source_sync_vars.get(base)
        if sv: sv.set(False)
        lv = self._aaf_source_sync_label_vars.get(base)
        if lv: lv.set("")
        self._aaf_set_sync_state(base, "")
        # Drop stale candidates + hide the inline TRY NEXT — they were
        # measured against this base's previous sync run, so cycling
        # would apply a now-irrelevant offset.
        self._aaf_clear_sync_candidates(base)
        btn = self._aaf_sync_btns.get(base)
        if btn:
            self._aaf_refresh_sync_btn(base, btn)

    def _aaf_qa_stop(self):
        """Stop any currently-playing QA audio and reset its button."""
        try:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
        prev = getattr(self, "_aaf_qa_playing", None)
        if prev:
            btn = self._aaf_qa_btns.get(prev)
            if btn:
                try:
                    btn.config(text="\u25b6 PREVIEW", fg=TEXT)
                except Exception:
                    pass
        self._aaf_qa_playing = None

    def _aaf_qa_toggle(self, base):
        """Toggle QA playback: start if idle/different source, stop if already playing."""
        if getattr(self, "_aaf_qa_playing", None) == base:
            self._aaf_qa_stop()
            return
        self._aaf_qa_stop()   # stop any other source first
        self._aaf_qa_play(base)

    def _aaf_qa_play(self, base):
        """Extract a 10-second middle segment of the aligned pair and play it."""
        fn = self._aaf_source_file_vars.get(base, tk.StringVar()).get()
        if fn == "— no video —":
            messagebox.showwarning("No Video",
                "Assign a video file to this source before QA playback.")
            return
        path_by_fn = {self._aaf_vid_label(p): p for p in self._aaf_video_paths}
        vp = path_by_fn.get(fn)
        if not vp:
            return
        ap = self._aaf_source_syncaudio_vars.get(base, tk.StringVar()).get()
        if not ap or not os.path.isfile(ap):
            messagebox.showwarning("No Reference Audio",
                "Select and sync a reference audio file before QA playback.")
            return
        try:
            offset_s = float(self._aaf_source_offset_vars[base].get())
        except (KeyError, ValueError):
            offset_s = 0.0

        import tempfile, wave as _wave
        tmp_dir = getattr(self, "_qa_tmp_dir", None)
        if not tmp_dir or not os.path.isdir(tmp_dir):
            self._qa_tmp_dir = tempfile.mkdtemp(prefix="pb_qa_")
            tmp_dir = self._qa_tmp_dir

        QA_DUR = 10.0
        QA_SR  = 16000

        # Mark as playing and switch button to stop icon
        self._aaf_qa_playing = base
        btn = self._aaf_qa_btns.get(base)
        if btn:
            try:
                btn.config(text="\u25a0 STOP", fg=WARN)
            except Exception:
                pass

        def _run():
            import numpy as _np
            # Probe reference duration; fall back to 60s estimate if probe fails
            ref_dur = engines._probe_duration(ap)
            if ref_dur <= 0:
                ref_dur = 60.0   # assume at least 60 s and try anyway

            # Try to find a speech-rich region near the middle of the file
            # using blob detection; fall back to simple midpoint if it fails.
            mid = ref_dur / 2.0
            start_s = max(0.0, mid - QA_DUR / 2.0)
            try:
                blobs = engines.detect_speech_blobs(ap)
                if blobs:
                    # Pick the blob whose onset is closest to the file midpoint
                    best = min(blobs, key=lambda b: abs(b["onset"] - mid))
                    # Start just before the onset so the speech begins naturally
                    start_s = max(0.0, best["onset"] - 0.5)
            except Exception:
                pass

            vid_start = start_s + offset_s
            if vid_start < 0.0:
                # Negative offset shifts video before frame 0 — trim audio start
                # forward by the overshoot to keep A/V locked together in the preview.
                start_s  = start_s + (-vid_start)
                vid_start = 0.0

            ref_wav = os.path.join(tmp_dir, "_qa_ref.wav")
            vid_wav = os.path.join(tmp_dir, "_qa_vid.wav")
            out_wav = os.path.join(tmp_dir, "_qa_mix.wav")

            engines.extract_audio_segment(ap, start_s,   QA_DUR, ref_wav, sample_rate=QA_SR)
            engines.extract_audio_segment(vp, vid_start, QA_DUR, vid_wav, sample_rate=QA_SR)

            def _rd(path):
                with _wave.open(path, "rb") as w:
                    data = w.readframes(w.getnframes())
                arr = _np.frombuffer(data, _np.int16).astype(_np.float32) / 32768.0
                rms = _np.sqrt(_np.mean(arr ** 2))
                return arr / rms * 0.4 if rms > 1e-6 else arr

            a = _rd(ref_wav); b = _rd(vid_wav)
            n = max(len(a), len(b))
            a = _np.pad(a, (0, n - len(a))); b = _np.pad(b, (0, n - len(b)))
            # Stereo: reference (DAW) → left ear, camera audio → right ear.
            # Interleave L/R samples: [L0, R0, L1, R1, ...]
            stereo = _np.empty(n * 2, dtype=_np.float32)
            stereo[0::2] = _np.clip(a, -1.0, 1.0)   # left  = reference
            stereo[1::2] = _np.clip(b, -1.0, 1.0)   # right = camera
            pcm = (stereo * 32767).astype(_np.int16)
            with _wave.open(out_wav, "wb") as w:
                w.setnchannels(2); w.setsampwidth(2)
                w.setframerate(QA_SR); w.writeframes(pcm.tobytes())
            return out_wav

        def _done(fut):
            try:
                wav = fut.result()
            except Exception as exc:
                def _err():
                    self._aaf_qa_stop()
                    messagebox.showerror("QA Play failed", str(exc))
                self._ui(_err)
                return
            if not wav:
                self._ui(self._aaf_qa_stop)
                return

            def _play():
                # Guard: user may have clicked stop while extraction was running
                if getattr(self, "_aaf_qa_playing", None) != base:
                    return
                try:
                    import winsound
                    winsound.PlaySound(wav,
                        winsound.SND_FILENAME | winsound.SND_ASYNC)
                except Exception:
                    pass
                # winsound async gives no completion callback; poll until silent
                self._qa_poll(base, wav)

            self._ui(_play)

        from concurrent.futures import ThreadPoolExecutor
        ex = ThreadPoolExecutor(max_workers=1)
        ex.submit(_run).add_done_callback(_done)
        ex.shutdown(wait=False)

    def _qa_poll(self, base, wav_path):
        """Poll every 500 ms; reset button when the file has finished playing."""
        if getattr(self, "_aaf_qa_playing", None) != base:
            return   # stopped manually
        import os as _os
        # Heuristic: re-check if winsound is still holding the file open
        # by attempting a no-op rename — not reliable cross-platform, so
        # instead we estimate from file duration.
        try:
            import wave as _wave
            with _wave.open(wav_path, "rb") as w:
                dur_ms = int(w.getnframes() / w.getframerate() * 1000)
        except Exception:
            dur_ms = 10000  # 10 s fallback
        # Schedule the reset slightly after the expected end
        self.after(dur_ms + 200, lambda: self._aaf_qa_stop() if
                   getattr(self, "_aaf_qa_playing", None) == base else None)

    # ── Reference-audio pool ─────────────────────────────────────────────────

    def _aaf_add_audio(self, path, _batch=False):
        """Add one reference audio/video file to the audio pool."""
        if not path or not os.path.isfile(path): return
        if os.path.basename(path).startswith("._"): return
        if path in self._aaf_audio_paths: return
        if not is_media(path): return
        self._aaf_audio_paths.append(path)
        # Auto-fill the stereo mix field when a FOR VID file is added and
        # nothing has been set manually yet.
        if "FOR VID" in os.path.basename(path).upper():
            _mv = getattr(self, "_aaf_mix_var", None)
            if _mv is not None and not _mv.get():
                _mv.set(path)
        row = tk.Frame(self._aaf_audio_file_list, bg=SURF2,
                       highlightbackground=BORDER, highlightthickness=1)
        row.pack(fill="x", pady=1)
        tk.Label(row, text="AUDIO", font=("Courier New", 9, "bold"),
                 bg="#2a2a3d", fg="#8888ff", padx=5, pady=3).pack(side="left")
        tk.Label(row, text=basename(path), font=FB, bg=SURF2, fg=TEXT,
                 padx=6, anchor="w").pack(side="left", fill="x", expand=True)
        rm = tk.Label(row, text=" \u2715 ", font=FB, bg=SURF2, fg=SUB,
                      cursor="hand2", padx=4)
        rm.pack(side="right")
        rm.bind("<Button-1>", lambda e, p=path, r=row: self._aaf_remove_audio(p, r))
        # Path \u2192 row Frame registry \u2014 mirror of _aaf_video_rows, so bulk
        # operations (CLEAN UP via _aaf_remove_unassigned) can find and
        # destroy the row by its stable identity instead of leaving an
        # orphaned Frame packed in _aaf_audio_file_list.
        if not hasattr(self, "_aaf_audio_rows"):
            self._aaf_audio_rows = {}
        self._aaf_audio_rows[path] = row
        if not _batch:
            n = len(self._aaf_audio_paths)
            self._aaf_audio_count_lbl.config(
                text="{} file{}".format(n, "s" if n != 1 else ""))

    def _aaf_remove_audio(self, path, row):
        if path in self._aaf_audio_paths:
            self._aaf_audio_paths.remove(path)
        getattr(self, "_aaf_audio_rows", {}).pop(path, None)
        row.destroy()
        n = len(self._aaf_audio_paths)
        self._aaf_audio_count_lbl.config(
            text="{} file{}".format(n, "s" if n != 1 else ""))

    def _aaf_mix_reference_dialog(self):
        """Sum several synchronous reference tracks into one aggregate
        WAV for dual-system sync.

        An on-camera GROUP mic matches the SUM of the individual lav
        tracks far better than any single lav, so syncing a camera
        against this mix locks cleanly — and you get a correct,
        independent offset for each camera.
        """
        pool = list(self._aaf_audio_paths)
        if len(pool) < 2:
            messagebox.showinfo(
                "Mix to Reference",
                "Add at least two reference audio tracks first (e.g. your "
                "individual lav files).\n\nThen mix them into one aggregate "
                "that a camera's group mic can sync against.")
            return
        pool_sorted = sorted(pool, key=lambda p: basename(p).lower())

        win = tk.Toplevel(self)
        win.title("Mix to Reference")
        win.configure(bg=BG)
        win.transient(self); win.grab_set()
        win.minsize(560, 420)

        tk.Label(win, text="Mix Reference Tracks", font=FH,
                 bg=BG, fg=TEXT, padx=20).pack(anchor="w", pady=(14, 2))
        tk.Label(win,
                 text=("Check the synchronous tracks to sum into one "
                       "aggregate (e.g. all 4 lavs).  A camera's group "
                       "mic matches this mix far better than any single "
                       "track, so sync locks cleanly.  Assumes the tracks "
                       "start at the same instant."),
                 font=FB, bg=BG, fg=SUB, padx=20, wraplength=520,
                 justify="left").pack(anchor="w", pady=(0, 8))

        nav = tk.Frame(win, bg=BG)
        nav.pack(side="bottom", fill="x", padx=20, pady=(8, 14))
        status_lbl = tk.Label(win, text="", font=FB, bg=BG, fg=SUB,
                              padx=20, anchor="w", wraplength=520,
                              justify="left")
        status_lbl.pack(side="bottom", fill="x", pady=(0, 4))

        list_wrap = tk.Frame(win, bg=SURF2, padx=2, pady=2)
        list_wrap.pack(fill="both", expand=True, padx=20, pady=(0, 8))
        check_vars = {}
        for p in pool_sorted:
            v = tk.BooleanVar(value=True)   # default: mix all (the common case)
            check_vars[p] = v
            tk.Checkbutton(
                list_wrap, text="  " + basename(p), variable=v,
                font=FB, bg=SURF2, fg=TEXT, selectcolor=SURF3,
                activebackground=SURF2, activeforeground=TEXT,
                anchor="w", highlightthickness=0, bd=0).pack(
                    fill="x", anchor="w")

        out_box = {"path": None}

        def _selected():
            return [p for p in pool_sorted if check_vars[p].get()]

        def _default_out(sel):
            d = os.path.dirname(sel[0])
            stem = getattr(self, "_aaf_data", {}).get("session_name") \
                if getattr(self, "_aaf_data", None) else None
            stem = stem or "reference"
            stem = re.sub(r"[^A-Za-z0-9_\- ]+", "_", stem)
            return os.path.join(d, stem + "_mix.wav")

        _busy = {"running": False}

        def _do_mix():
            if _busy["running"]:
                return
            sel = _selected()
            if len(sel) < 2:
                status_lbl.config(text="Check at least two tracks.",
                                  fg=WARN)
                return
            out_path = out_box["path"] or _default_out(sel)
            if os.path.exists(out_path) and not messagebox.askyesno(
                    "Overwrite?",
                    "{} already exists. Overwrite?".format(
                        basename(out_path))):
                return
            _busy["running"] = True
            status_lbl.config(
                text="Mixing {} tracks…".format(len(sel)), fg=ACCENT)
            mix_btn.config(state="disabled")
            cancel_btn.config(state="disabled")

            def _worker():
                # Wrap mix_reference_audio — bare call would leave
                # _busy["running"]=True on exception, making the modal
                # uncloseable (same class as join-split's preflight).
                try:
                    ok, msg = engines.mix_reference_audio(sel, out_path)
                except Exception as e:
                    ok, msg = False, "exception: {}".format(e)
                def _finish():
                    _busy["running"] = False
                    try:
                        cancel_btn.config(state="normal")
                        mix_btn.config(state="normal")
                    except tk.TclError:
                        return
                    if not ok:
                        status_lbl.config(
                            text="Mix failed: " + (msg or "unknown error"),
                            fg=ERR)
                        return
                    self._aaf_add_audio(out_path)
                    win.grab_release(); win.destroy()
                    messagebox.showinfo(
                        "Mixed",
                        "Created {}\n\nAdded to the reference-audio pool — "
                        "assign it as the SYNC reference for each camera, "
                        "then sync each (they'll get their own "
                        "offset).".format(basename(out_path)))
                self._ui(_finish)

            threading.Thread(target=_worker, daemon=True).start()

        def _change_out():
            sel = _selected()
            init = out_box["path"] or (_default_out(sel) if sel
                                       else "reference_mix.wav")
            p = filedialog.asksaveasfilename(
                title="Save mixed reference as",
                initialdir=os.path.dirname(init),
                initialfile=os.path.basename(init),
                defaultextension=".wav",
                filetypes=[("WAV", "*.wav"), ("All", "*.*")])
            if p:
                out_box["path"] = p
                status_lbl.config(text="Output: " + basename(p), fg=SUB)

        _W = 12
        def _close(_e=None):
            # Release the modal grab BEFORE destroying — relying on Tk's
            # implicit release-on-destroy is fragile across platforms.
            if _busy["running"]:
                return
            try: win.grab_release()
            except tk.TclError: pass
            win.destroy()

        mix_btn = self._btn(nav, "MIX", _do_mix, color=ACCENT, width=_W)
        mix_btn.pack(side="right")
        cancel_btn = self._btn(nav, "CANCEL", _close, width=_W)
        cancel_btn.pack(side="right", padx=(0, 8))
        self._btn(nav, "OUTPUT…", _change_out, width=_W).pack(side="left")

        win.bind("<Escape>", _close)
        self._center_dialog(win)

    def _aaf_browse_audio_files(self):
        exts = sorted(MEDIA_EXTS)
        ext_glob = (" ".join("*" + e for e in exts) + " " +
                    " ".join("*" + e.upper() for e in exts))
        paths = filedialog.askopenfilenames(
            title="Select reference audio / video files",
            filetypes=[("Audio / video", ext_glob), ("All files", "*.*")])
        self._aaf_add_audio_batch(paths)

    def _aaf_browse_audio_folder(self):
        folder = filedialog.askdirectory(title="Select folder with reference audio files")
        if not folder: return
        paths = []
        for root, _, files in os.walk(folder):
            for fn in sorted(files):
                if os.path.splitext(fn)[1].lower() in MEDIA_EXTS:
                    paths.append(os.path.join(root, fn))
        self._aaf_add_audio_batch(paths)

    def _aaf_add_audio_batch(self, paths):
        """Add multiple reference audio files efficiently."""
        for p in paths:
            self._aaf_add_audio(p, _batch=True)
        n = len(self._aaf_audio_paths)
        self._aaf_audio_count_lbl.config(
            text="{} file{}".format(n, "s" if n != 1 else ""))
        # Rebuild source rows so audio dropdowns include the new files
        # and auto-match can populate unassigned rows.
        if hasattr(self, "_aaf_assign_frame"):
            self._rebuild_aaf_source_rows()

    def _aaf_browse_mix(self):
        path = filedialog.askopenfilename(
            title="Select stereo mix file",
            filetypes=[("Audio files", "*.wav *.aiff *.aif *.mp3 *.m4a *.flac"),
                       ("All files", "*.*")])
        if path:
            self._aaf_mix_var.set(path)

    def _aaf_set_mix(self, paths):
        """Accept a DnD drop for the mix file — use the first valid audio path."""
        for p in (paths or []):
            if p and is_audio(p):
                self._aaf_mix_var.set(p)
                return

    # ── Video pool ───────────────────────────────────────────────────────────

    def _aaf_add_video(self, path, _batch=False):
        """Add one video file to the pool.

        Pass _batch=True when adding many files at once; the caller is then
        responsible for calling _rebuild_aaf_source_rows() and
        _aaf_schedule_detect_fps() once after the loop finishes.
        """
        if not path or not os.path.isfile(path): return
        if os.path.basename(path).startswith("._"): return
        if path in self._aaf_video_paths: return
        if not is_video(path): return
        self._aaf_video_paths.append(path)
        row = tk.Frame(self._aaf_file_list, bg=SURF2,
                       highlightbackground=BORDER, highlightthickness=1)
        row.pack(fill="x", pady=1)
        tk.Label(row, text="VIDEO", font=("Courier New",9,"bold"),
                 bg="#2a3d2a", fg=SUCCESS, padx=5, pady=3).pack(side="left")
        tk.Label(row, text=basename(path), font=FB, bg=SURF2, fg=TEXT,
                 padx=6, anchor="w").pack(side="left", fill="x", expand=True)
        rm = tk.Label(row, text=" \u2715 ", font=FB, bg=SURF2, fg=SUB,
                      cursor="hand2", padx=4)
        rm.pack(side="right")
        rm.bind("<Button-1>", lambda e, p=path, r=row: self._aaf_remove_video(p, r))
        # Register by FULL PATH so removal is keyed to identity, never to
        # the displayed basename (two pool files can share a basename).
        if not hasattr(self, "_aaf_video_rows"):
            self._aaf_video_rows = {}
        self._aaf_video_rows[path] = row
        if not _batch:
            n = len(self._aaf_video_paths)
            self._aaf_count_lbl.config(text="{} file{}".format(n, "s" if n != 1 else ""))
            if hasattr(self, "_aaf_assign_frame"):
                self._rebuild_aaf_source_rows()
            self._aaf_schedule_detect_fps()

    def _aaf_remove_video(self, path, row):
        if path in self._aaf_video_paths:
            self._aaf_video_paths.remove(path)
        getattr(self, "_aaf_video_rows", {}).pop(path, None)
        row.destroy()
        n = len(self._aaf_video_paths)
        self._aaf_count_lbl.config(text="{} file{}".format(n, "s" if n != 1 else ""))
        if hasattr(self, "_aaf_assign_frame"):
            self._rebuild_aaf_source_rows()
        self._aaf_update_seq_presets()

    def _aaf_join_split_dialog(self):
        """Join gapless file-size-split camera clips into one continuous
        file (lossless stream-copy concat), then add it to the pool.

        Cameras split long recordings at the FAT32 4 GB limit into
        successive files that are byte-identical in format, so they
        rejoin perfectly with `-c copy`.  Picking the pieces here and
        joining them turns a split camera into a single continuous
        source that syncs and lays out exactly like an un-split one.
        """
        pool = list(self._aaf_video_paths)
        if len(pool) < 2:
            messagebox.showinfo(
                "Join Split Clips",
                "Add at least two video files to the pool first.\n\n"
                "Then check the successive pieces of a single camera "
                "roll and join them into one continuous file.")
            return
        # Name order is the correct concat order for camera splits
        # (CLIP_0001, CLIP_0002, …).
        pool_sorted = sorted(pool, key=lambda p: basename(p).lower())

        win = tk.Toplevel(self)
        win.title("Join Split Clips")
        win.configure(bg=BG)
        win.transient(self); win.grab_set()
        win.minsize(560, 420)

        tk.Label(win, text="Join Split Camera Clips", font=FH,
                 bg=BG, fg=TEXT, padx=20).pack(anchor="w", pady=(14, 2))
        tk.Label(win,
                 text=("Check the successive pieces of ONE camera roll "
                       "(file-size splits).  They join losslessly in the "
                       "order shown — no re-encode."),
                 font=FB, bg=BG, fg=SUB, padx=20, wraplength=520,
                 justify="left").pack(anchor="w", pady=(0, 8))

        # Bottom action row first so it survives a short window.
        nav = tk.Frame(win, bg=BG)
        nav.pack(side="bottom", fill="x", padx=20, pady=(8, 14))
        status_lbl = tk.Label(win, text="", font=FB, bg=BG, fg=SUB,
                              padx=20, anchor="w", wraplength=520,
                              justify="left")
        status_lbl.pack(side="bottom", fill="x", pady=(0, 4))

        # Scrollable checklist of pool files.
        list_wrap = tk.Frame(win, bg=SURF2, padx=2, pady=2)
        list_wrap.pack(fill="both", expand=True, padx=20, pady=(0, 8))
        check_vars = {}
        for p in pool_sorted:
            v = tk.BooleanVar(value=False)
            check_vars[p] = v
            cb = tk.Checkbutton(
                list_wrap, text="  " + basename(p), variable=v,
                font=FB, bg=SURF2, fg=TEXT, selectcolor=SURF3,
                activebackground=SURF2, activeforeground=TEXT,
                anchor="w", highlightthickness=0, bd=0)
            cb.pack(fill="x", anchor="w")

        # Output path + remove-originals option.
        opt = tk.Frame(win, bg=BG)
        opt.pack(side="bottom", fill="x", padx=20, pady=(0, 4))
        remove_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            opt, text="Remove the source pieces from the pool after joining",
            variable=remove_var, font=FB, bg=BG, fg=TEXT,
            selectcolor=SURF2, activebackground=BG, activeforeground=TEXT,
            highlightthickness=0, bd=0).pack(anchor="w")

        out_box = {"path": None}

        def _selected():
            return [p for p in pool_sorted if check_vars[p].get()]

        def _default_out(sel):
            base0 = os.path.splitext(basename(sel[0]))[0]
            # Engine picks the right container (.mov for PCM-audio
            # sources, else the source extension).
            ext0  = engines.join_output_ext(sel)
            return os.path.join(os.path.dirname(sel[0]),
                                base0 + "_joined" + ext0)

        _busy = {"running": False}

        def _do_join():
            if _busy["running"]:
                return
            sel = _selected()
            if len(sel) < 2:
                status_lbl.config(
                    text="Check at least two files to join.", fg=WARN)
                return

            # Stage 1 — preflight probes on a WORKER thread.  Compat +
            # continuity + default-name probing spawn ~15-20 ffprobe
            # processes; running them on the Tk thread froze the
            # dialog for seconds after clicking JOIN.
            _busy["running"] = True
            join_btn.config(state="disabled")
            cancel_btn.config(state="disabled")
            status_lbl.config(
                text="Checking {} files…".format(len(sel)), fg=SUB)

            def _preflight():
                # Wrap engine probes — if either raises (missing ffprobe,
                # malformed media), the bare call would leave
                # _busy["running"]=True and the modal would be
                # uncloseable (CANCEL/Escape gate on _busy["running"])
                # until app restart.  Surface the error and clear busy.
                try:
                    ok, reason = engines.probe_concat_compat(sel)
                    cstatus, cmsg, csummary = engines.probe_join_continuity(sel)
                    out_default = out_box["path"] or _default_out(sel)
                except Exception as e:
                    self._ui(lambda err=e: _abort_preflight(err))
                    return
                self._ui(lambda: _confirm(ok, reason, cstatus, cmsg,
                                          csummary, out_default))

            def _abort_preflight(err):
                _busy["running"] = False
                try:
                    join_btn.config(state="normal")
                    cancel_btn.config(state="normal")
                    status_lbl.config(text="", fg=SUB)
                except tk.TclError:
                    return
                messagebox.showerror("Preflight failed",
                                     "Could not probe the selected files:\n\n{}".format(err))

            def _confirm(ok, reason, cstatus, cmsg, csummary, out_path):
                # Back on the Tk thread: release the busy lock so a "No"
                # answer leaves a usable dialog, then walk the
                # confirmation dialogs in order.
                _busy["running"] = False
                try:
                    join_btn.config(state="normal")
                    cancel_btn.config(state="normal")
                except tk.TclError:
                    return            # dialog was torn down mid-preflight
                if not ok:
                    if not messagebox.askyesno(
                            "Different formats",
                            "{}\n\nThese may not be splits of the same "
                            "recording.  A lossless join can still be "
                            "attempted but the result may not play "
                            "correctly.\n\nJoin anyway?".format(reason),
                            default="no", icon="warning"):
                        status_lbl.config(text="", fg=SUB)
                        return
                # Continuity — catch a missing middle piece or wrong
                # order before joining.  The summary (start TC + length)
                # is shown so the user can also spot a missing FIRST
                # piece, which can't be detected from the files alone.
                if cstatus == "gap":
                    if not messagebox.askyesno(
                            "Possible gap or missing piece",
                            "{}\n\n{}\n\nJoin anyway?".format(
                                cmsg, csummary),
                            default="no", icon="warning"):
                        status_lbl.config(text="", fg=SUB)
                        return
                elif csummary:
                    # Surface the span so a missing front/end is visible.
                    status_lbl.config(text=csummary, fg=SUB)
                if os.path.exists(out_path):
                    if not messagebox.askyesno(
                            "Overwrite?",
                            "{} already exists. Overwrite?".format(
                                basename(out_path))):
                        return
                _start_join(sel, out_path, csummary)

            def _start_join(sel, out_path, csummary):
                _busy["running"] = True
                status_lbl.config(
                    text="Joining {} clips… (lossless, no re-encode)".format(
                        len(sel)), fg=ACCENT)
                join_btn.config(state="disabled")
                cancel_btn.config(state="disabled")

                def _worker():
                    # Wrap engines.concat_video_files — bare call would
                    # leave _busy["running"]=True on an exception, with
                    # the same uncloseable-modal symptom as the preflight.
                    try:
                        ok2, msg, actual = engines.concat_video_files(sel, out_path)
                    except Exception as e:
                        ok2, msg, actual = False, "exception: {}".format(e), out_path
                    def _finish():
                        _busy["running"] = False
                        try:
                            cancel_btn.config(state="normal")
                            join_btn.config(state="normal")
                        except tk.TclError:
                            return
                        if not ok2:
                            status_lbl.config(
                                text="Join failed: " + (msg or "unknown error"),
                                fg=ERR)
                            return
                        # Add joined file to the pool; remove sources if asked.
                        if remove_var.get():
                            for p in sel:
                                self._aaf_remove_video_by_path(p)
                        self._aaf_add_video(actual)
                        win.grab_release(); win.destroy()
                        note = ""
                        if (actual.lower().endswith(".mov")
                                and not out_path.lower().endswith(".mov")):
                            note = ("\n\n(Saved as .mov — the source's PCM "
                                    "audio can't live in an .mp4 container.)")
                        # Engine-level caution (e.g. joined duration not
                        # matching the sum of the inputs).
                        if msg:
                            note += "\n\n⚠ " + msg
                        span = ("\n\n" + csummary) if csummary else ""
                        messagebox.showinfo(
                            "Joined",
                            "Created {}\n\nAdded to the video pool — assign "
                            "and sync it like any single continuous "
                            "file.{}{}\n\nSanity-check that length against the "
                            "full recording — if it's short, a piece may be "
                            "missing.".format(basename(actual), note, span))
                    self._ui(_finish)

                threading.Thread(target=_worker, daemon=True).start()

            threading.Thread(target=_preflight, daemon=True).start()

        def _change_out():
            sel = _selected()
            init = out_box["path"] or (_default_out(sel) if len(sel) >= 1
                                       else "joined.mp4")
            p = filedialog.asksaveasfilename(
                title="Save joined file as",
                initialdir=os.path.dirname(init),
                initialfile=os.path.basename(init),
                defaultextension=os.path.splitext(init)[1] or ".mp4")
            if p:
                out_box["path"] = p
                status_lbl.config(text="Output: " + basename(p), fg=SUB)

        _W = 12
        def _close(_e=None):
            # Release the modal grab BEFORE destroying — relying on Tk's
            # implicit release-on-destroy is fragile across platforms.
            if _busy["running"]:
                return
            try: win.grab_release()
            except tk.TclError: pass
            win.destroy()

        join_btn = self._btn(nav, "JOIN", _do_join, color=ACCENT, width=_W)
        join_btn.pack(side="right")
        cancel_btn = self._btn(nav, "CANCEL", _close, width=_W)
        cancel_btn.pack(side="right", padx=(0, 8))
        self._btn(nav, "OUTPUT…", _change_out, width=_W).pack(side="left")

        win.bind("<Escape>", _close)
        self._center_dialog(win)

    def _aaf_remove_video_by_path(self, path):
        """Remove a pooled video by path, destroying its list row.  Used
        by Join Split so the source pieces disappear from the pool.

        The row is looked up in the path→row registry — never by the
        displayed basename, which is ambiguous when two pool files from
        different folders share a filename (typical for camera cards)."""
        if path in self._aaf_video_paths:
            self._aaf_video_paths.remove(path)
        row = getattr(self, "_aaf_video_rows", {}).pop(path, None)
        if row is not None:
            try:
                row.destroy()
            except tk.TclError:
                pass
        n = len(self._aaf_video_paths)
        if hasattr(self, "_aaf_count_lbl"):
            self._aaf_count_lbl.config(
                text="{} file{}".format(n, "s" if n != 1 else ""))

    def _aaf_remove_unmatched(self):
        """Remove any pooled video files that aren't assigned to any source clip."""
        assigned_fns = {sv.get() for sv in self._aaf_source_file_vars.values()
                        if sv.get() != "— no video —"}
        for _evs in getattr(self, "_aaf_source_extra_vars", {}).values():
            assigned_fns.update(ev.get() for ev in _evs if ev.get() != "— no video —")
        to_remove = [p for p in self._aaf_video_paths
                     if self._aaf_vid_label(p) not in assigned_fns]
        if not to_remove:
            messagebox.showinfo("Remove Unmatched",
                                "All files in the pool are currently assigned.")
            return
        msg = ("Remove {} unmatched file{} from the pool?\n\n{}".format(
            len(to_remove),
            "s" if len(to_remove) != 1 else "",
            "\n".join(basename(p) for p in to_remove[:10])
            + ("\n…and {} more".format(len(to_remove) - 10) if len(to_remove) > 10 else "")))
        if not messagebox.askyesno("Remove Unmatched", msg):
            return
        # Destroy the file list rows for removed paths, then rebuild the
        # survivors through _aaf_add_video so the path\u2192row registry stays
        # coherent (an inline re-render here used to bypass it).
        for w in self._aaf_file_list.winfo_children():
            w.destroy()
        remaining = [p for p in self._aaf_video_paths if p not in to_remove]
        self._aaf_video_paths = []
        self._aaf_video_rows  = {}
        for path in remaining:
            self._aaf_add_video(path, _batch=True)
        n = len(self._aaf_video_paths)
        self._aaf_count_lbl.config(text="{} file{}".format(n, "s" if n != 1 else ""))
        self._rebuild_aaf_source_rows()
        self._aaf_update_seq_presets()

    def _aaf_browse_files(self):
        exts = sorted(VIDEO_EXTS)
        ext_glob = (" ".join("*" + e for e in exts) + " " +
                    " ".join("*" + e.upper() for e in exts))
        paths = filedialog.askopenfilenames(
            title="Select video files",
            filetypes=[("Video files", ext_glob), ("All files", "*.*")])
        self._aaf_add_video_batch(paths)

    def _aaf_browse_folder(self):
        folder = filedialog.askdirectory(title="Select folder with video files")
        if not folder: return
        self._aaf_set_status("Scanning folder…")
        paths = []
        for root, _, files in os.walk(folder):
            for fn in sorted(files):
                if os.path.splitext(fn)[1].lower() in VIDEO_EXTS:
                    paths.append(os.path.join(root, fn))
        self._aaf_add_video_batch(paths)

    def _aaf_add_video_batch(self, paths):
        """Add multiple video files efficiently — one UI rebuild at the end."""
        total = len(paths)
        added = 0
        for i, p in enumerate(paths):
            if total > 1:
                self._aaf_set_status("Adding file {} of {}…".format(i + 1, total))
                if i % 5 == 0:
                    self.update_idletasks()
            self._aaf_add_video(p, _batch=True)
            added += 1

        n = len(self._aaf_video_paths)
        self._aaf_count_lbl.config(text="{} file{}".format(n, "s" if n != 1 else ""))
        if hasattr(self, "_aaf_assign_frame"):
            self._aaf_set_status("Building assignment rows…")
            self.update_idletasks()
            self._rebuild_aaf_source_rows()
        self._aaf_schedule_detect_fps()
        self._aaf_update_seq_presets()

    def _aaf_refresh_pool(self):
        """Scan each directory already in the video/audio pools for new media files.

        Existing entries are kept as-is.  Any file found in the same folder(s)
        that isn't already in a pool is added automatically.  This lets editors
        drop new footage into a watched folder and hit Refresh instead of
        re-browsing.
        """
        dirs = set()
        for p in self._aaf_video_paths + self._aaf_audio_paths:
            d = os.path.dirname(p)
            if os.path.isdir(d):
                dirs.add(d)
        if not dirs:
            return

        existing = set(self._aaf_video_paths) | set(self._aaf_audio_paths)
        new_video, new_audio = [], []
        for d in sorted(dirs):
            try:
                for fn in sorted(os.listdir(d)):
                    if fn.startswith("._"):
                        continue
                    fp = os.path.join(d, fn)
                    if not os.path.isfile(fp) or fp in existing:
                        continue
                    if is_video(fp):
                        new_video.append(fp)
                    elif is_audio(fp):
                        new_audio.append(fp)
            except OSError:
                pass

        if not new_video and not new_audio:
            self._aaf_set_status("No new files found.")
            return

        if new_video:
            self._aaf_add_video_batch(new_video)
        if new_audio:
            self._aaf_add_audio_batch(new_audio)

        total = len(new_video) + len(new_audio)
        self._aaf_set_status(
            "Added {} new file{}.".format(total, "s" if total != 1 else ""))

    def _aaf_set_status(self, msg):
        """Update the import status label (no-op if label not yet created)."""
        if hasattr(self, "_aaf_status_lbl"):
            self._aaf_status_lbl.config(text=msg)
            self.update_idletasks()

    def _aaf_sidecar_path(self):
        if not hasattr(self, "_aaf_path") or not self._aaf_path:
            return None
        return os.path.splitext(self._aaf_path)[0] + "_setup.json"

    def _aaf_build_setup_data(self):
        """Return the current AAF setup as a serialisable dict."""
        return {
            "version":     2,
            "aaf":         getattr(self, "_aaf_path", ""),
            "video_paths": self._aaf_video_paths,
            "audio_paths": self._aaf_audio_paths,
            # Bound by the CURRENT source set — _aaf_source_file_vars is
            # never pruned across AAF loads, so iterating it directly would
            # leak assignments for sources from a previously-opened AAF
            # into this setup's saved JSON (and mis-restore them later).
            "assignments": {base: self._aaf_source_file_vars[base].get()
                            for base in self._aaf_sources
                            if base in self._aaf_source_file_vars},
            "slot_counts": {base: getattr(self, "_aaf_source_slot_counts", {}).get(base, 1)
                            for base in self._aaf_sources
                            if getattr(self, "_aaf_source_slot_counts", {}).get(base, 1) > 1},
            "extra_assignments": {
                base: [ev.get() for ev in
                       getattr(self, "_aaf_source_extra_vars", {}).get(base, [])]
                for base in self._aaf_sources
                if getattr(self, "_aaf_source_slot_counts", {}).get(base, 1) > 1
            },
            "sync": {
                base: {
                    "enabled":    self._aaf_source_sync_vars[base].get(),
                    "audio_path": self._aaf_source_syncaudio_vars[base].get(),
                    "offset":     self._aaf_source_offset_vars[base].get(),
                    "label":      self._aaf_source_sync_label_vars.get(
                                      base, tk.StringVar()).get(),
                    "state":      self._aaf_sync_state_vars.get(
                                      base, tk.StringVar()).get(),
                    "needs_sync": self._aaf_needs_sync_vars.get(
                                      base, tk.BooleanVar()).get(),
                    "locked":     self._aaf_sync_locked_vars.get(
                                      base, tk.BooleanVar()).get(),
                }
                for base in self._aaf_sources
                if base in self._aaf_source_sync_vars
            },
            "hidden_sources": sorted(getattr(self, "_aaf_hidden_sources", set())),
            "grouping":      self._aaf_group_var.get(),
            "fps":           self._aaf_fps_var.get(),
            "seq_preset":    getattr(self, "_aaf_preset_var",  tk.StringVar(value="Detect from media")).get(),
            "seq_w":         getattr(self, "_aaf_seq_w_var",   tk.StringVar()).get(),
            "seq_h":         getattr(self, "_aaf_seq_h_var",   tk.StringVar()).get(),
            "seq_name":      self.seq_name.get(),
            "mix_path":      getattr(self, "_aaf_mix_var", tk.StringVar()).get(),
            "camera_audio":  getattr(self, "_aaf_cam_audio_var", tk.BooleanVar()).get(),
        }

    def _aaf_save_setup(self, prompt=True):
        """Save the AAF setup.

        Behaves like a normal app: the FIRST save prompts for a location
        and name (Save As), and subsequent quick-saves (Ctrl+S) write
        back to that chosen file silently.  `prompt=True` always shows
        the dialog.  `_aaf_saved_path` holds the user-chosen file (None
        until first saved or loaded from).

        Returns True only when a file was actually written — callers use
        this to decide whether to show "SAVED" feedback (a cancelled
        Save-As dialog must NOT read as a successful save)."""
        data  = self._aaf_build_setup_data()
        saved = getattr(self, "_aaf_saved_path", None)

        # Quick-save to a file the user has already chosen — no dialog.
        if not prompt and saved:
            try:
                with open(saved, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                self._mark_saved()
                return True
            except Exception as e:
                messagebox.showerror("Save failed", str(e))
            return False

        # No chosen file yet (or explicit Save As) → prompt.  Suggest the
        # AAF-adjacent <name>_setup.json as a default, but let the user
        # put it wherever they like.
        sidecar = self._aaf_sidecar_path()
        init_dir  = (os.path.dirname(saved) if saved
                     else (os.path.dirname(sidecar) if sidecar else ""))
        init_file = (os.path.basename(saved) if saved
                     else (os.path.basename(sidecar) if sidecar
                           else "aaf_setup.json"))
        path = filedialog.asksaveasfilename(
            title="Save AAF setup",
            defaultextension=".json",
            filetypes=[("JSON","*.json"),("All","*.*")],
            initialdir=init_dir, initialfile=init_file)
        if not path:
            return False                  # user cancelled — nothing saved
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            self._aaf_saved_path = path   # future quick-saves go here
            self._mark_saved()
            return True
        except Exception as e:
            messagebox.showerror("Save failed", str(e))
            return False

    def _aaf_load_setup(self):
        path = filedialog.askopenfilename(
            title="Load AAF setup",
            filetypes=[("JSON setup files","*.json"),("All","*.*")])
        if not path or not os.path.exists(path): return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            messagebox.showerror("Load failed", str(e)); return
        self._aaf_restore_setup(data)
        # Quick-saves now write back to the file the user just loaded, and
        # the freshly-restored state is the clean baseline — without this,
        # closing an untouched setup falsely prompts "unsaved changes".
        self._aaf_saved_path = path
        self._mark_saved()

    def _aaf_restore_setup(self, data):
        """Apply a saved AAF setup dict to the current Step 2 UI state."""
        # ── Batch-add pool files (one rebuild at the very end) ────────────────
        missing = []
        for vp in data.get("video_paths", []):
            if os.path.exists(vp):
                self._aaf_add_video(vp, _batch=True)
            else:
                missing.append(vp)

        for ap in data.get("audio_paths", []):
            if os.path.exists(ap):
                self._aaf_add_audio(ap, _batch=True)
            else:
                missing.append(ap)

        # Update pool count labels
        nv = len(self._aaf_video_paths)
        self._aaf_count_lbl.config(
            text="{} file{}".format(nv, "s" if nv != 1 else ""))
        na = len(self._aaf_audio_paths)
        self._aaf_audio_count_lbl.config(
            text="{} file{}".format(na, "s" if na != 1 else ""))

        # ── Restore assignments and sync state (before the single rebuild) ────
        assignments = data.get("assignments", {})
        options = ["— no video —"] + [self._aaf_vid_label(p) for p in self._aaf_video_paths]
        for base, fn in assignments.items():
            if base in self._aaf_source_file_vars and fn in options:
                self._aaf_source_file_vars[base].set(fn)

        # ── Restore multi-slot counts and extra assignments ───────────────────
        if not hasattr(self, "_aaf_source_slot_counts"):
            self._aaf_source_slot_counts = {}
        if not hasattr(self, "_aaf_source_extra_vars"):
            self._aaf_source_extra_vars = {}
        for base, n in data.get("slot_counts", {}).items():
            if base in self._aaf_sources and n > 1:
                self._aaf_source_slot_counts[base] = n
                if base not in self._aaf_source_extra_vars:
                    self._aaf_source_extra_vars[base] = []
                while len(self._aaf_source_extra_vars[base]) < n - 1:
                    self._aaf_source_extra_vars[base].append(
                        tk.StringVar(value="— no video —"))
        for base, fns in data.get("extra_assignments", {}).items():
            _evs = self._aaf_source_extra_vars.get(base, [])
            for _i, _fn in enumerate(fns):
                if _i < len(_evs) and _fn in options:
                    _evs[_i].set(_fn)

        aud_by_name = {self._aaf_aud_label(p): p for p in self._aaf_audio_paths}
        for base, sd in data.get("sync", {}).items():
            if base in self._aaf_source_sync_vars:
                self._aaf_source_sync_vars[base].set(sd.get("enabled", False))
            if base in self._aaf_source_syncaudio_vars:
                full = sd.get("audio_path", "")
                self._aaf_source_syncaudio_vars[base].set(full)
                # Populate display var so the dropdown shows the filename.
                # Resolve through _aaf_aud_label so a multicam basename
                # collision restores to the EXACT same physical file the
                # setup was saved against — bare basename would route to
                # whichever same-named file is now last in the pool.
                if base not in self._aaf_source_audio_disp_vars:
                    self._aaf_source_audio_disp_vars[base] = tk.StringVar()
                if full and full in self._aaf_audio_paths:
                    lbl = self._aaf_aud_label(full)
                    self._aaf_source_audio_disp_vars[base].set(
                        lbl if lbl in aud_by_name else "— no audio —")
                else:
                    self._aaf_source_audio_disp_vars[base].set("— no audio —")
            if base in self._aaf_source_offset_vars:
                self._aaf_source_offset_vars[base].set(sd.get("offset", "0.000"))
            if base in self._aaf_source_sync_label_vars:
                self._aaf_source_sync_label_vars[base].set(sd.get("label", ""))
            if base in self._aaf_sync_state_vars:
                self._aaf_sync_state_vars[base].set(sd.get("state", ""))
            if base in self._aaf_needs_sync_vars:
                self._aaf_needs_sync_vars[base].set(sd.get("needs_sync", False))
            if base in self._aaf_sync_locked_vars:
                self._aaf_sync_locked_vars[base].set(sd.get("locked", False))

        # Restore hidden sources
        self._aaf_hidden_sources = set(data.get("hidden_sources", []))

        # Single rebuild + FPS probe for the entire load
        if hasattr(self, "_aaf_assign_frame"):
            self._rebuild_aaf_source_rows()
        self._aaf_schedule_detect_fps()

        # Restore other settings
        if "grouping" in data:  self._aaf_group_var.set(data["grouping"])
        if "fps"      in data:  self._aaf_fps_var.set(data["fps"])
        if "seq_preset" in data:
            if not hasattr(self, "_aaf_preset_var"):
                self._aaf_preset_var = tk.StringVar()
            self._aaf_preset_var.set(data["seq_preset"])
        if "seq_w" in data:
            if not hasattr(self, "_aaf_seq_w_var"):
                self._aaf_seq_w_var = tk.StringVar()
            self._aaf_seq_w_var.set(str(data.get("seq_w", "")))
        if "seq_h" in data:
            if not hasattr(self, "_aaf_seq_h_var"):
                self._aaf_seq_h_var = tk.StringVar()
            self._aaf_seq_h_var.set(str(data.get("seq_h", "")))
        if "seq_name" in data:  self.seq_name.set(data["seq_name"])
        if "mix_path" in data:
            if not hasattr(self, "_aaf_mix_var"):
                self._aaf_mix_var = tk.StringVar()
            self._aaf_mix_var.set(data["mix_path"])
        if "camera_audio" in data:
            if not hasattr(self, "_aaf_cam_audio_var"):
                self._aaf_cam_audio_var = tk.BooleanVar()
            self._aaf_cam_audio_var.set(bool(data["camera_audio"]))

        # Probe loaded files and populate the "Detected from source" preset entries
        self._aaf_update_seq_presets()

        if missing:
            messagebox.showwarning("Missing files",
                "{} video file(s) from the saved setup were not found:\n{}".format(
                    len(missing), "\n".join(basename(p) for p in missing[:5])))

        # Opened from a saved setup file — route future quick-saves back
        # to it instead of prompting again.
        _sp = getattr(self, "_pending_aaf_saved_path", None)
        if _sp is not None:
            self._aaf_saved_path = _sp
            self._pending_aaf_saved_path = None

        # Restore complete — capture the clean baseline for an opened
        # AAF setup so closing an unchanged setup doesn't falsely prompt.
        if getattr(self, "_pending_mark_saved", False):
            self._pending_mark_saved = False
            self._mark_saved()

    def _aaf_build_progress_hide(self):
        """Pack-forget the build progress frame, on any exit path that
        doesn't transition to a follow-up screen.  Without this, an
        _aaf_build early-return (no matches, user cancelled Save-As)
        leaves the progress bar stuck at its last percentage/text until
        the user navigates away from Step 2."""
        frame = getattr(self, "_aaf_prog_frame", None)
        if frame is not None:
            try: frame.pack_forget()
            except tk.TclError: pass

    def _aaf_build_progress(self, pct, text):
        """Show/update the build progress bar.  pct is 0–100.  Thread-safe."""
        def _upd():
            frame = getattr(self, "_aaf_prog_frame", None)
            if frame is None:
                return
            if not frame.winfo_ismapped():
                frame.pack(side="bottom", fill="x", pady=(6, 0))
            self._aaf_prog_lbl.config(text=text)
            self._aaf_prog_bar.set(pct, 100)
        if threading.current_thread() is threading.main_thread():
            _upd()
            self.update_idletasks()
        else:
            self._ui(_upd)

    def _aaf_build(self):
        vpaths = self._aaf_video_paths
        path_by_fn = {self._aaf_vid_label(p): p for p in vpaths}

        try:
            fps = float(self._aaf_fps_var.get())
        except ValueError:
            messagebox.showerror("Invalid FPS", "Enter a valid frame rate (e.g. 29.97).")
            return

        group_mode = self._aaf_group_var.get()   # "single" or "by_source"

        # Build source → video path list (slot 1 + any extra slots)
        source_video = {}
        for base, sv in self._aaf_source_file_vars.items():
            _paths = []
            fn = sv.get()
            if fn != "— no video —" and fn in path_by_fn:
                _paths.append(path_by_fn[fn])
            for _ev in getattr(self, "_aaf_source_extra_vars", {}).get(base, []):
                fn2 = _ev.get()
                if fn2 != "— no video —" and fn2 in path_by_fn:
                    _paths.append(path_by_fn[fn2])
            if _paths:
                source_video[base] = _paths

        # Warn if any multi-slot source has unfilled slots
        _partial_fills = []
        for base in self._aaf_sources:
            _n = getattr(self, "_aaf_source_slot_counts", {}).get(base, 1)
            if _n > 1:
                _sv0 = self._aaf_source_file_vars.get(base)
                _evs = getattr(self, "_aaf_source_extra_vars", {}).get(base, [])
                _filled = (1 if _sv0 and _sv0.get() != "— no video —" else 0) + sum(
                    1 for _ev in _evs if _ev.get() != "— no video —")
                if _filled < _n:
                    _partial_fills.append((base, _filled, _n))
        if _partial_fills:
            _pmsg = "Some sources have partially assigned slots:\n\n"
            for _pb, _pf, _pn in _partial_fills[:5]:
                _pmsg += "  {}  ({}/{} slots filled)\n".format(_pb, _pf, _pn)
            if len(_partial_fills) > 5:
                _pmsg += "  \u2026 and {} more\n".format(len(_partial_fills) - 5)
            _pmsg += "\nProceed? Unfilled slots fall back to the last assigned file."
            if not messagebox.askyesno("Incomplete Assignments", _pmsg):
                return

        # Build per-source sync offset (seconds) and reference audio path.
        # Also track which sources have been sync'd (affects multi-file build logic).
        source_offset     = {}
        source_syncaudio  = {}
        source_sync_done  = set()
        for base in self._aaf_sources:
            if self._aaf_source_sync_vars.get(base, tk.BooleanVar()).get():
                source_sync_done.add(base)
                try:
                    source_offset[base] = float(
                        self._aaf_source_offset_vars[base].get())
                except ValueError:
                    source_offset[base] = 0.0
                ap = self._aaf_source_syncaudio_vars[base].get()
                if ap and os.path.isfile(ap):
                    source_syncaudio[base] = ap

        self._aaf_build_progress(10, "Processing clips\u2026")

        # Flatten all clips from all tracks
        all_clips = []
        for track in self._aaf_data["tracks"]:
            for clip in track["clips"]:
                all_clips.append({**clip, "track_name": track["name"]})

        matched      = 0
        unmatched    = 0
        clip_results = []   # (clip_name, video_label, color) — for the colored summary
        clips_with_media    = []
        track_names_ordered = []   # preserves insertion order for XML track sequence
        _occ_count = {}   # base → occurrence index (for multi-slot positional assignment)

        for clip in all_clips:
            base = get_clip_base_name(clip["clip_name"])
            if base is None:
                continue   # skip fades and unresolvable clips entirely

            _paths = source_video.get(base)
            # Multi-angle stacking: when a source has sync enabled AND
            # holds >1 video, treat every video as a parallel camera
            # angle of the same take rather than picking one.  Sync is
            # what tells us "these files are aligned to the same audio,"
            # which is precisely the semantic that makes stacking
            # correct.  Each angle emits its own clips_with_media
            # entry on its own track_name so build_xml_from_pt renders
            # them as stacked <track>s; the sync audio path is attached
            # only to angle 1 to avoid triplicating the audio.
            _stack = (_paths and base in source_sync_done and len(_paths) > 1)

            if _paths:
                if _stack:
                    vp = _paths[0]
                elif base in source_sync_done and len(_paths) > 1:
                    vp = _paths[0]   # unreachable — _stack covers this
                else:
                    _idx = _occ_count.get(base, 0)
                    vp   = _paths[min(_idx, len(_paths) - 1)]
                    _occ_count[base] = _idx + 1
                matched += 1
                if _stack:
                    _lbl = "{}  ({} angles stacked)".format(
                        basename(vp), len(_paths))
                    clip_results.append((clip["clip_name"], _lbl, SUCCESS))
                else:
                    clip_results.append((clip["clip_name"], basename(vp), SUCCESS))
            else:
                vp = None
                unmatched += 1
                clip_results.append((clip["clip_name"], "— no video assigned —", WARN))

            # Build the per-angle track name list.  Single-video sources
            # get one track (the historical behaviour); stacked sources
            # get one per angle — the first keeps the source's normal
            # label so its audio track lines up with the visual, then
            # ANGLE 2/3/... land on their own tracks above it.
            def _tn_for(_vp, _angle_i):
                if group_mode == "by_source":
                    _base_tn = self._aaf_vid_label(_vp) if _vp else "No Video"
                elif group_mode == "by_track":
                    _base_tn = clip.get("track_name") or "Unknown Track"
                else:
                    # Consolidate mode: keep the placeholder verbatim
                    # so the interval scheduler below distributes each
                    # angle to the first free track by overlap.  Stacked
                    # angles share start_secs so they naturally land on
                    # distinct tracks.
                    return "_consolidate_"
                if _angle_i == 0:
                    return _base_tn
                return "{}  ·  ANGLE {}".format(_base_tn, _angle_i + 1)

            if _stack:
                _angle_paths = list(_paths)
            else:
                _angle_paths = [vp]   # historical single-angle case

            for _angle_i, _avp in enumerate(_angle_paths):
                tn = _tn_for(_avp, _angle_i)
                if tn not in track_names_ordered and tn != "_consolidate_":
                    track_names_ordered.append(tn)
                clips_with_media.append({
                    **clip,
                    "track_name":        tn,
                    "video_path":        _avp,
                    # Attach the sync audio only to angle 0 so it doesn't
                    # appear on every stacked track.  Camera audio (from
                    # the video file itself, when include_camera_audio is
                    # on) stays per-angle since each angle has its own
                    # embedded audio channel.
                    "audio_path":        (source_syncaudio.get(base)
                                          if _angle_i == 0 else None),
                    "video_offset_secs": source_offset.get(base, 0.0),
                })

        # Interval scheduling for "consolidate" mode — assign minimum tracks
        if group_mode == "single":
            clips_with_media.sort(key=lambda c: c["start_secs"])
            track_ends = []   # end_secs of the last clip assigned to each track
            track_names_ordered = []
            for c in clips_with_media:
                assigned = None
                for i, tend in enumerate(track_ends):
                    if c["start_secs"] >= tend:
                        assigned = i
                        break
                if assigned is None:
                    assigned = len(track_ends)
                    track_ends.append(0.0)
                tn = "Video {}".format(assigned + 1)
                track_ends[assigned] = c["end_secs"]
                c["track_name"] = tn
                if tn not in track_names_ordered:
                    track_names_ordered.append(tn)

        if matched == 0:
            self._aaf_build_progress_hide()
            messagebox.showerror("No Matches",
                "No clips were matched to video files.\n"
                "Add video files and assign at least one source to a video.")
            return

        self._aaf_build_progress(35, "Choose output file\u2026")
        out = filedialog.asksaveasfilename(
            title="Save Premiere XML",
            defaultextension=".xml",
            filetypes=[("XML","*.xml"),("All","*.*")],
            initialfile="{}_aaf.xml".format(
                self._aaf_data.get("session_name","postbridge").replace(" ","_")))
        if not out:
            self._aaf_build_progress_hide()
            return

        # Snapshot Tkinter vars before handing off to the worker thread.
        sr       = self._aaf_data.get("sample_rate", 48000)
        try:
            seq_w = int(getattr(self, "_aaf_seq_w_var", tk.StringVar()).get() or "0")
            seq_h = int(getattr(self, "_aaf_seq_h_var", tk.StringVar()).get() or "0")
        except ValueError:
            seq_w, seq_h = 0, 0
        mix_path    = getattr(self, "_aaf_mix_var", None)
        mix_path    = mix_path.get() if mix_path else ""
        cam_audio   = getattr(self, "_aaf_cam_audio_var", None)
        cam_audio_v = bool(cam_audio and cam_audio.get())
        seq_name    = (self.seq_name.get()
                       or self._aaf_data.get("session_name", "PostBridge"))

        # Disable the button while the worker runs.
        try:
            self._aaf_build_btn.config(state="disabled")
        except Exception:
            pass

        # ── Worker thread — all heavy work runs here ───────────────────────────
        def _worker():
            _seq_w, _seq_h = seq_w, seq_h   # local copies (may be updated by probe)
            try:
                # Sequence dimensions
                if (_seq_w <= 0 or _seq_h <= 0) and vpaths:
                    self._aaf_build_progress(55, "Probing media settings\u2026")
                    print("Probing media settings ({} files)…".format(len(vpaths)))
                    _seq_w, _seq_h, _, _ = engines.probe_media_settings(vpaths)
                    print("  -> {}x{}".format(_seq_w, _seq_h))
                if _seq_w <= 0: _seq_w = 1280
                if _seq_h <= 0: _seq_h = 720

                # ── Fragment auto-sync ─────────────────────────────────────
                # When an AAF source's audio file is a short rendered fragment
                # (AudioSuite/RX/bounce) rather than the original full recording,
                # src_in from the AAF is relative to the fragment — not the
                # assigned video.  Detect where the fragment appears in the video
                # via cross-correlation so the video <in> points are correct.
                self._aaf_build_progress(60, "Scanning for audio fragments\u2026")
                print("Scanning {} clips for AudioSuite fragments…".format(
                    len(clips_with_media)))

                fragment_jobs = []
                frag_seen_sf  = set()
                for c in clips_with_media:
                    sf = c.get("source_file", "")
                    if not sf or sf in frag_seen_sf:
                        continue
                    base = get_clip_base_name(c["clip_name"])
                    off  = source_offset.get(base, 0.0)
                    if (not c.get("video_path")
                            or abs(off) >= 0.002
                            or bool(source_syncaudio.get(base))):
                        frag_seen_sf.add(sf)
                        continue
                    if not os.path.isfile(sf):
                        print("  skip (missing): {}".format(os.path.basename(sf)))
                        frag_seen_sf.add(sf)
                        continue
                    src_dur = engines._probe_duration(sf)
                    vid_dur = engines._probe_duration(c["video_path"])
                    if src_dur <= 0 or vid_dur <= 0 or src_dur >= vid_dur * 0.5:
                        print("  skip (dur ratio {:.1f}s / {:.1f}s): {}".format(
                            src_dur, vid_dur, os.path.basename(sf)))
                        frag_seen_sf.add(sf)
                        continue
                    print("  fragment: {} ({:.1f}s) vs {} ({:.1f}s)".format(
                        os.path.basename(sf), src_dur,
                        os.path.basename(c["video_path"]), vid_dur))
                    frag_seen_sf.add(sf)
                    fragment_jobs.append((sf, c["video_path"], src_dur, vid_dur))

                print("{} fragment job(s) queued.".format(len(fragment_jobs)))
                frag_offsets = {}
                n_frags = len(fragment_jobs)
                for i, (sf, vp, _sd, _vd) in enumerate(fragment_jobs, 1):
                    clip_label = os.path.basename(sf)
                    if len(clip_label) > 45:
                        clip_label = clip_label[:42] + "\u2026"
                    is_cached = engines._rx_offset_cache_load(sf, vp) is not None
                    print("  [{}/{}]{} {}".format(
                        i, n_frags, " [cached]" if is_cached else "", clip_label))
                    self._aaf_build_progress(
                        60 + int(9 * i / max(n_frags, 1)),
                        "Fragment sync {}/{}{}: {}".format(
                            i, n_frags,
                            " [cached]" if is_cached else "",
                            clip_label))
                    T = engines.detect_rx_offset(sf, vp)
                    print("    -> offset {:.3f}s{}".format(
                        T, " (cached)" if is_cached else ""))
                    if T != 0.0:
                        frag_offsets[os.path.normpath(sf).lower()] = T

                if frag_offsets:
                    for c in clips_with_media:
                        sf = c.get("source_file", "")
                        if sf:
                            key = os.path.normpath(sf).lower()
                            if key in frag_offsets:
                                c["video_offset_secs"] = frag_offsets[key]

                if DEV_DIAGNOSTIC:
                    self._aaf_build_progress(70, "Writing diagnostic report\u2026")
                    try:
                        diag_path = os.path.splitext(out)[0] + "_diagnostic.txt"
                        engines.write_build_diagnostic(
                            clips_with_media, fps, _seq_w, _seq_h, sr,
                            out_path=diag_path)
                    except Exception:
                        pass

                self._aaf_build_progress(80, "Building XML\u2026")
                _oor_warnings = []
                xmeml = engines.build_xml_from_pt(
                    clips_with_media,
                    track_names_ordered,
                    seq_name,
                    seq_w=_seq_w, seq_h=_seq_h, seq_fps=fps, seq_sr=sr,
                    mix_path=mix_path or None,
                    include_camera_audio=cam_audio_v,
                    warnings_out=_oor_warnings)

                self._aaf_build_progress(95, "Writing file\u2026")
                engines.write_xml(xmeml, out)
                engines.clear_rx_cache()

                def _finish():
                    self.out_path.set(out)
                    self._aaf_done(matched, unmatched,
                                   len(clips_with_media), clip_results)
                    # Surface any clips whose sync offset placed them
                    # outside their media \u2014 clamped (not dropped), but
                    # they need a re-sync.
                    if _oor_warnings:
                        shown = _oor_warnings[:12]
                        more  = len(_oor_warnings) - len(shown)
                        messagebox.showwarning(
                            "Sync offsets out of range",
                            "These clips had a sync offset that placed them "
                            "outside their video file.  They were clamped "
                            "back in (so they import instead of being "
                            "dropped), but they're mis-synced until "
                            "re-synced:\n\n" + "\n".join(
                                "\u2022 " + w for w in shown)
                            + ("\n\u2026and {} more".format(more) if more else ""))
                self._ui(_finish)

            except Exception as e:
                engines.clear_rx_cache()
                _err = str(e)
                def _show_err():
                    try:
                        self._aaf_build_btn.config(state="normal")
                    except Exception:
                        pass
                    messagebox.showerror("Build Error", _err)
                self._ui(_show_err)

        threading.Thread(target=_worker, daemon=True).start()

    def _aaf_done(self, matched, unmatched, total, clip_results=None):
        self._clear()
        tk.Frame(self.body, bg=BG, height=20).pack()
        tk.Label(self.body, text="\u2713",
                 font=("Courier New",52,"bold"), bg=BG, fg=SUCCESS).pack()
        tk.Label(self.body, text="PREMIERE XML EXPORTED",
                 font=FL, bg=BG, fg=TEXT).pack(pady=(6,2))
        tk.Label(self.body, text=self.out_path.get(),
                 font=FB, bg=BG, fg=SUB).pack()
        tk.Frame(self.body, bg=BG, height=10).pack()

        card = tk.Frame(self.body, bg=SURF,
                        highlightbackground=BORDER, highlightthickness=1)
        card.pack(padx=60, fill="x")
        for lbl, val, col in [
            ("Total AAF clips",   str(total),     TEXT),
            ("Matched to video",  str(matched),   SUCCESS),
            ("Unmatched",         str(unmatched), WARN if unmatched else SUB),
        ]:
            r = tk.Frame(card, bg=SURF); r.pack(fill="x", padx=16, pady=3)
            tk.Label(r, text="{:<22}".format(lbl),
                     font=FB, bg=SURF, fg=SUB).pack(side="left")
            tk.Label(r, text=val, font=FB, bg=SURF, fg=col).pack(side="left")
        tk.Frame(card, bg=BG, height=8).pack()

        tk.Frame(self.body, bg=BG, height=10).pack()
        self._btn(self.body, "START NEW EPISODE", self._reset).pack()

        # ── Per-clip colored log (same scheme as Script→AAF reconcile view) ──
        if clip_results:
            tk.Frame(self.body, bg=BG, height=12).pack()
            tk.Label(self.body, text="CLIP DETAIL",
                     font=FL, bg=BG, fg=ACCENT).pack(anchor="w", pady=(0,4))
            log_outer = tk.Frame(self.body, bg=BG)
            log_outer.pack(fill="both", expand=True, pady=(0,6))
            log = tk.Text(log_outer,
                          font=("Courier New", 10), bg=SURF, fg=TEXT,
                          relief="flat", bd=0,
                          state="normal", wrap="none")
            sbv = tk.Scrollbar(log_outer, orient="vertical",   command=log.yview)
            sbh = tk.Scrollbar(log_outer, orient="horizontal", command=log.xview)
            log.configure(yscrollcommand=sbv.set, xscrollcommand=sbh.set)
            sbv.pack(side="right",  fill="y")
            sbh.pack(side="bottom", fill="x")
            log.pack(side="left", fill="both", expand=True)
            for clip_name, vid_label, color in clip_results:
                tag = "c{}".format(abs(hash(color)))
                log.tag_configure(tag, foreground=color)
                line = "  {:<50s}  →  {}\n".format(clip_name[:50], vid_label)
                log.insert("end", line, tag)
            log.configure(state="disabled")
