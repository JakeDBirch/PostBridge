import os
import tkinter as tk
import re
from tkinter import filedialog

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False

from config import *
from utils import (
    is_video, basename, parse_dnd, suggest_token, suggest_vo_part,
    _strip_take_number, _riverside_person_segment, _strip_media_type, _name_similarity
)

# ── Token colour helpers ───────────────────────────────────────────────────────
_TOKEN_COLORS = [
    "#e06030",  # orange-red
    "#20a080",  # teal
    "#7040c0",  # purple
    "#30a040",  # green
    "#c03070",  # rose
    "#a08020",  # gold
    "#3070b0",  # blue
    "#b04040",  # red
    "#20a0a0",  # cyan
    "#8060c0",  # violet
    "#c07020",  # amber
    "#508020",  # lime
]

def _token_color(token):
    """Saturated accent colour for the left-edge strip."""
    if not token or token == "— unassigned —":
        return "#444444"
    return _TOKEN_COLORS[hash(token) % len(_TOKEN_COLORS)]

def _blend_hex(base, tint, alpha):
    """Blend *tint* into *base* at *alpha* (0-1) and return a hex string."""
    r1,g1,b1 = int(base[1:3],16), int(base[3:5],16), int(base[5:7],16)
    r2,g2,b2 = int(tint[1:3],16), int(tint[3:5],16), int(tint[5:7],16)
    r = int(r1 + alpha*(r2-r1))
    g = int(g1 + alpha*(g2-g1))
    b = int(b1 + alpha*(b2-b1))
    return "#{:02x}{:02x}{:02x}".format(r, g, b)

def _token_row_bg(token):
    """Subtle full-width row tint (20 % of the token accent blended into SURF2)."""
    tc = _token_color(token)
    if tc == "#444444":          # unassigned → plain surface
        return SURF2
    return _blend_hex(SURF2, tc, 0.20)

def _pool_norm(path):
    """Return (plain_norm, stripped_norm) for a file path.
    Both are lower-case with separators collapsed to single spaces.
    stripped_norm also removes Riverside-style _raw_audio / _raw_synced_video_cfr
    suffixes so counterpart pairs share the same core name."""
    base = os.path.splitext(os.path.basename(path))[0]
    plain    = ' '.join(re.sub(r'[_\-\.]+', ' ', base.lower()).split())
    stripped = ' '.join(re.sub(r'[_\-\.]+', ' ', _strip_media_type(base)).split())
    return plain, stripped

# ── Slim scrollbar ────────────────────────────────────────────────────────────
class _SlimScrollbar(tk.Canvas):
    """
    Modern, thin vertical scrollbar — drop-in replacement for ttk.Scrollbar.
    No arrow buttons.  8 px wide, accent-coloured thumb on hover/drag.
    """
    _W = 8
    _P = 1

    def __init__(self, parent, command, **kw):
        kw.setdefault("cursor", "arrow")
        super().__init__(parent, width=self._W, bg=SURF3,
                         bd=0, highlightthickness=0, **kw)
        self._cmd        = command
        self._lo         = 0.0
        self._hi         = 1.0
        self._drag_start = None

        self._thumb = self.create_rectangle(
            self._P, 0, self._W - self._P, 30,
            fill=SUB, outline="", width=0)

        self.bind("<Configure>",       lambda e: self._redraw())
        self.bind("<ButtonPress-1>",   self._on_press)
        self.bind("<B1-Motion>",       self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>",           lambda e: self._set_thumb(ACCENT))
        self.bind("<Leave>",           lambda e: self._set_thumb(SUB)
                                                 if self._drag_start is None else None)

    def set(self, lo, hi):
        self._lo = float(lo); self._hi = float(hi); self._redraw()

    def _redraw(self):
        h = self.winfo_height()
        if h < 2: return
        visible = (self._hi - self._lo) < 0.9999
        self.itemconfigure(self._thumb, state="normal" if visible else "hidden")
        y0 = max(self._P, int(self._lo * h))
        y1 = min(h - self._P, int(self._hi * h))
        if y1 - y0 < 16: y1 = y0 + 16
        self.coords(self._thumb, self._P, y0, self._W - self._P, y1)

    def _set_thumb(self, color): self.itemconfigure(self._thumb, fill=color)
    def _thumb_hit(self, y):
        c = self.coords(self._thumb)
        return len(c) == 4 and c[1] <= y <= c[3]

    def _on_press(self, e):
        if self._thumb_hit(e.y):
            self._drag_start = (e.y, self._lo); self._set_thumb(ACCENT)
        else:
            h = self.winfo_height()
            if h > 0:
                self._cmd("scroll", -1 if e.y / h < self._lo else 1, "pages")

    def _on_drag(self, e):
        if self._drag_start is None: return
        start_y, start_lo = self._drag_start
        h = self.winfo_height()
        if h < 1: return
        span   = self._hi - self._lo
        new_lo = max(0.0, min(start_lo + (e.y - start_y) / h, 1.0 - span))
        self._cmd("moveto", str(new_lo))

    def _on_release(self, e):
        self._drag_start = None; self._set_thumb(SUB)


# ── Flat progress bar ─────────────────────────────────────────────────────────
class _FlatProgressBar(tk.Frame):
    """
    Slim canvas-based progress bar matching the app's dark theme.
    Call .set(value, maximum) to update.
    """
    def __init__(self, parent, height=6, fill=ACCENT, bg=SURF2, **kw):
        super().__init__(parent, bg=bg, height=height, **kw)
        self.pack_propagate(False)
        self._fill = fill
        self._pct  = 0.0
        self._cv   = tk.Canvas(self, bg=bg, height=height,
                               highlightthickness=0, bd=0)
        self._cv.pack(fill="both", expand=True)
        self._cv.bind("<Configure>", lambda _: self._draw())

    def set(self, value, maximum):
        self._pct = min(1.0, value / maximum) if maximum > 0 else 0.0
        self._draw()

    def _draw(self):
        w = self._cv.winfo_width()
        h = self._cv.winfo_height()
        if w < 2:
            return
        self._cv.delete("all")
        if self._pct > 0:
            self._cv.create_rectangle(
                0, 0, max(1, int(w * self._pct)), h,
                fill=self._fill, outline="")


# ── Flat dropdown (replaces ttk.Combobox) ─────────────────────────────────────
class _FlatDropdown(tk.Frame):
    """
    Fully-themed flat drop-down replacement for ttk.Combobox.

    Drop-in compatible for the usages in this app:
      - constructor args: textvariable, values, state, font, width, justify
      - virtual event <<ComboboxSelected>> fires on selection change
      - .configure(values=...) / .configure(state=...) work as expected
      - MouseWheel bindings are honoured (scroll guard pattern is no longer
        needed since the widget doesn't cycle values on scroll)
    """
    _ARROW = "▾"

    def __init__(self, parent, textvariable=None, values=(), state="readonly",
                 font=None, width=20, justify="left", **kw):
        self._base_bg = kw.pop("bg", SURF2)
        super().__init__(parent, bg=self._base_bg, bd=0,
                         highlightthickness=1, highlightbackground=BORDER,
                         cursor="hand2", **kw)
        self._var        = textvariable if textvariable is not None else tk.StringVar()
        self._values     = list(values)
        self._state      = state
        self._font       = font or FB
        self._width      = width
        self._popup      = None
        self._outside_id = None

        self._lbl = tk.Label(self, textvariable=self._var,
                             font=self._font, bg=self._base_bg, fg=TEXT,
                             padx=8, pady=5, anchor="w", width=width)
        self._lbl.pack(side="left", fill="x", expand=True)

        self._arr = tk.Label(self, text=self._ARROW,
                             font=self._font, bg=self._base_bg, fg=SUB,
                             padx=6, pady=5)
        self._arr.pack(side="right")

        # Tooltip shows full selected value on hover (useful for long filenames)
        self._tip     = None
        self._tip_job = None
        self._lbl.bind("<Enter>", self._tip_schedule)
        self._lbl.bind("<Leave>", self._tip_cancel)

        if state != "disabled":
            for w in (self, self._lbl, self._arr):
                w.bind("<Button-1>", self._toggle)
                w.bind("<Enter>",    lambda e: self._tint(True))
                w.bind("<Leave>",    lambda e: self._tint(False))

    def set_bg(self, color):
        """Update the base background color (used by row color coding)."""
        self._base_bg = color
        self.config(bg=color)
        self._lbl.config(bg=color)
        self._arr.config(bg=color)

    # ── Separator support ─────────────────────────────────────────────────────
    @staticmethod
    def separator(label=""):
        """Return a value string that renders as a non-selectable divider row."""
        return "\x00" + label

    def _is_sep(self, v):
        return isinstance(v, str) and v.startswith("\x00")

    def _disp(self, v):
        return v[1:] if self._is_sep(v) else v

    # ── Tooltip for the selected label ────────────────────────────────────────
    def _tip_schedule(self, event):
        self._tip_cancel(None)
        val = self._var.get()
        if not val or val.startswith("—"):
            return
        self._tip_job = self._lbl.after(600, lambda: self._tip_show(event.x_root, event.y_root, val))

    def _tip_cancel(self, event):
        if self._tip_job:
            try: self._lbl.after_cancel(self._tip_job)
            except Exception: pass
            self._tip_job = None
        if self._tip:
            try: self._tip.destroy()
            except Exception: pass
            self._tip = None

    def _tip_show(self, rx, ry, text):
        self._tip = tw = tk.Toplevel(self)
        tw.wm_overrideredirect(True)
        tw.attributes("-topmost", True)
        tk.Label(tw, text=text, font=self._font, bg="#2a2a2a", fg=TEXT,
                 padx=8, pady=4, relief="flat",
                 highlightthickness=1, highlightbackground=BORDER).pack()
        tw.update_idletasks()
        tw_w = tw.winfo_reqwidth()
        tw_h = tw.winfo_reqheight()
        sw   = tw.winfo_screenwidth()
        sh   = tw.winfo_screenheight()
        x    = min(rx + 12, sw - tw_w - 4)
        y    = min(ry + 16, sh - tw_h - 4)
        tw.geometry("+{}+{}".format(x, y))

    def _tint(self, on):
        if self._popup:
            return
        c = SURF3 if on else self._base_bg
        b = ACCENT if on else BORDER
        self.config(bg=c, highlightbackground=b)
        self._lbl.config(bg=c)
        self._arr.config(bg=c, fg=ACCENT if on else SUB)

    def _toggle(self, e=None):
        if self._popup: self._close()
        else:           self._open()

    def _open(self):
        if not self._values or self._state == "disabled":
            return
        self.update_idletasks()
        rx = self.winfo_rootx()
        ry = self.winfo_rooty()
        rh = self.winfo_height()
        rw = self.winfo_width()

        top = tk.Toplevel(self)
        top.wm_overrideredirect(True)
        top.wm_geometry("+{}+{}".format(rx, ry + rh))
        top.attributes("-topmost", True)
        top.config(bg=SURF2, highlightthickness=1, highlightbackground=ACCENT)

        vis = min(len(self._values), 14)
        need_sb = len(self._values) > vis

        lb = tk.Listbox(
            top,
            font=self._font, bg=SURF2, fg=TEXT,
            selectbackground=ACCENT, selectforeground=BG,
            activestyle="none", relief="flat", bd=0,
            highlightthickness=0, height=vis, exportselection=False,
        )
        if need_sb:
            sb = _SlimScrollbar(top, command=lb.yview)
            sb.pack(side="right", fill="y", pady=1, padx=(0,1))
            lb.config(yscrollcommand=sb.set)
        lb.pack(side="left", fill="both", expand=True, padx=1, pady=1)

        for i, v in enumerate(self._values):
            lb.insert("end", "  " + self._disp(v))
            if self._is_sep(v):
                lb.itemconfig(i, fg=SUB,
                              selectbackground=SURF2, selectforeground=SUB)

        try:
            idx = self._values.index(self._var.get())
            lb.selection_set(idx); lb.see(idx)
        except ValueError:
            pass

        def _motion(e):
            lb.selection_clear(0, "end")
            i = lb.nearest(e.y)
            if 0 <= i < len(self._values) and not self._is_sep(self._values[i]):
                lb.selection_set(i)

        lb.bind("<Motion>",        _motion)
        lb.bind("<ButtonRelease-1>", lambda e: self._pick(lb))
        lb.bind("<Up>",              lambda e: self._nav(lb, -1))
        lb.bind("<Down>",            lambda e: self._nav(lb,  1))
        lb.bind("<Return>",          lambda e: self._pick(lb))
        lb.bind("<Escape>",          lambda e: self._close())

        root = self.winfo_toplevel()
        def _outside(e):
            if not self._popup: return
            try:
                wx = self._popup.winfo_rootx(); wy = self._popup.winfo_rooty()
                ww = self._popup.winfo_width();  wh = self._popup.winfo_height()
                if wx <= e.x_root <= wx + ww and wy <= e.y_root <= wy + wh:
                    return
            except Exception:
                pass
            self._close()
        self._outside_id = root.bind("<Button-1>", _outside, add="+")

        self._popup = top
        self._lb    = lb

        top.update_idletasks()
        ph = top.winfo_reqheight()
        py = ry + rh
        if py + ph > self.winfo_screenheight() - 40:
            py = ry - ph
        # Make the popup wide enough to show the longest item without truncation
        try:
            import tkinter.font as _tkfont
            _f = _tkfont.Font(font=self._font)
            max_text_w = max((_f.measure("  " + self._disp(v))
                              for v in self._values), default=0) + 32
        except Exception:
            max_text_w = rw
        # Also constrain to screen width; anchor left edge at dropdown left
        screen_w  = self.winfo_screenwidth()
        popup_w   = max(rw, min(max_text_w, screen_w - rx - 8))
        top.geometry("{}x{}+{}+{}".format(popup_w, ph, rx, py))
        lb.focus_set()

    def _nav(self, lb, delta):
        sel = lb.curselection()
        cur = sel[0] if sel else -1
        nxt = cur + delta
        # Skip over separator rows
        while 0 <= nxt < len(self._values) and self._is_sep(self._values[nxt]):
            nxt += delta
        nxt = max(0, min(nxt, len(self._values) - 1))
        lb.selection_clear(0, "end"); lb.selection_set(nxt); lb.see(nxt)

    def _pick(self, lb):
        sel = lb.curselection()
        if sel:
            v = self._values[sel[0]]
            if not self._is_sep(v):
                self._var.set(v)
                self.event_generate("<<ComboboxSelected>>")
        self._close()

    def _close(self):
        if self._popup:
            try:
                root = self.winfo_toplevel()
                if self._outside_id:
                    root.unbind("<Button-1>", self._outside_id)
                    self._outside_id = None
            except Exception:
                pass
            self._popup.destroy()
            self._popup = None
        self._tint(False)

    def configure(self, **kw):
        if "values" in kw:
            self._values = list(kw.pop("values"))
            if self._popup: self._close()
        if "state" in kw:
            st = kw.pop("state")
            self._state = st
            self.config(cursor="hand2" if st != "disabled" else "")
            self._lbl.config(fg=TEXT if st != "disabled" else SUB)
        super().configure(**kw)
    config = configure


# ── VO Bin (one per @PART) ────────────────────────────────────────────────────
class VoBin(tk.Frame):
    """
    Holds any number of video+audio files for a part's VO narration.
    Files are paired by sort order (first video with first audio, etc.)
    giving one take per pair.  Extra files on either side are paired with None.
    """
    def __init__(self, parent, part_name, part_index, **kw):
        super().__init__(parent, bg=SURF,
                         highlightbackground=BORDER, highlightthickness=1, **kw)
        self.part_index = part_index
        self._paths     = []

        hdr = tk.Frame(self, bg=SURF); hdr.pack(fill="x", padx=12, pady=(8,4))
        tk.Label(hdr, text="[ VO ]", font=FL, bg=SURF, fg=ACCENT).pack(side="left")
        tk.Label(hdr, text="  " + part_name,
                 font=FB, bg=SURF, fg=TEXT).pack(side="left")
        self._count_lbl = tk.Label(hdr, text="", font=FB, bg=SURF, fg=SUB)
        self._count_lbl.pack(side="right")

        hint = "Drop files here  (video + audio, one pair per take)" if HAS_DND else "Use Browse"
        self.dz = tk.Frame(self, bg=SURF3,
                           highlightbackground=BORDER, highlightthickness=1)
        self.dz.pack(fill="x", padx=12, pady=(0,4))
        self._dz_lbl = tk.Label(self.dz, text=hint,
                                 font=FB, bg=SURF3, fg=SUB, pady=8)
        self._dz_lbl.pack(fill="x")

        self.file_frame = tk.Frame(self, bg=SURF)
        self.file_frame.pack(fill="x", padx=12)

        foot = tk.Frame(self, bg=SURF)
        foot.pack(anchor="w", padx=12, pady=(4,10))
        bb = tk.Label(foot, text="+ BROWSE", font=FB, bg=SURF, fg=SUB,
                      cursor="arrow", padx=6, pady=3,
                      bd=0, highlightbackground=BORDER, highlightthickness=1)
        bb.bind("<Enter>", lambda e, w=bb: w.config(bg=ACCENT, fg=TEXT))
        bb.bind("<Leave>", lambda e, w=bb: w.config(bg=SURF,   fg=SUB))
        bb.bind("<Button-1>", lambda e: self._browse())
        bb.pack(side="left")
        tk.Label(foot, text="  video + audio, one pair per take",
                 font=("Courier New",10), bg=SURF, fg=SUB).pack(side="left")

        if HAS_DND:
            for w in [self, self.dz, self._dz_lbl]:
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>",
                           lambda e: [self._add(p) for p in parse_dnd(e.data)])

    def _browse(self):
        paths = filedialog.askopenfilenames(
            title="Select VO files for {}".format(self.part_index),
            filetypes=[("Media","*.mp4 *.mov *.wav *.aif *.aiff *.mxf"),
                       ("All","*.*")])
        for p in paths: self._add(p)

    def _add(self, path):
        if not path or path in self._paths: return
        self._paths.append(path)
        self._dz_lbl.config(text="")
        self._render_tag(path)
        self._refresh_count()

    def _render_tag(self, path):
        row = tk.Frame(self.file_frame, bg=SURF2,
                       highlightbackground=BORDER, highlightthickness=1)
        row.pack(fill="x", pady=2)
        kind = "VIDEO" if is_video(path) else "AUDIO"
        kc   = ("#1e3a1e","#5aab61") if kind=="VIDEO" else ("#1e2a3a","#5588cc")
        tk.Label(row, text=kind, font=("Courier New",9,"bold"),
                 bg=kc[0], fg=kc[1], padx=5, pady=2).pack(side="left")
        tk.Label(row, text=basename(path), font=FB,
                 bg=SURF2, fg=TEXT, padx=6).pack(side="left")
        rm = tk.Label(row, text="  ✕", font=FB, bg=SURF2,
                      fg=SUB, cursor="hand2", padx=4)
        rm.pack(side="right")
        rm.bind("<Button-1>", lambda e, p=path, r=row: self._remove(p, r))

    def _remove(self, path, row):
        if path in self._paths: self._paths.remove(path)
        row.destroy(); self._refresh_count()

    def _refresh_count(self):
        n = len(self._paths)
        if n == 0:
            self._dz_lbl.config(
                text="Drop files here  (video + audio, one pair per take)"
                     if HAS_DND else "Use Browse")
            self._count_lbl.config(text="")
        else:
            v = sum(1 for p in self._paths if is_video(p))
            a = n - v
            takes = max(v, a)
            parts = []
            if v: parts.append("{}v".format(v))
            if a: parts.append("{}a".format(a))
            self._count_lbl.config(
                text="{}  ({} take{})".format(
                    "  ".join(parts), takes, "s" if takes != 1 else ""))

    def get_paths(self):      return list(self._paths)
    def get_audio(self):
        a = [p for p in self._paths if not is_video(p)]
        return a[0] if a else None
    def get_video(self):
        v = [p for p in self._paths if is_video(p)]
        return v[0] if v else None
    def is_assigned(self):    return bool(self._paths)


# ── Media Pool ─────────────────────────────────────────────────────────────────
class MediaPool(tk.Frame):
    """
    Drop zone + table for all episode media.
    Each row: [TYPE] filename           [token dropdown] [✕]
    Assignment is live — get_assignments() returns
      { token: [paths] } and { "VO:N": [paths] } dicts.
    """
    def __init__(self, parent, tokens, parts, aaf_mode=False, **kw):
        super().__init__(parent, bg=SURF,
                         highlightbackground=BORDER, highlightthickness=1, **kw)
        self._tokens  = tokens
        self._parts   = parts
        self._rows         = []
        self._auto_assigned = set()
        self._src_vars = {tok: tk.StringVar(value="") for tok in tokens}
        self._aaf_mode          = aaf_mode
        self._prune_scheduled   = False
        self._sort_scheduled    = False   # deferred _sort_rows flag
        self._src_dd_scheduled  = False   # deferred _rebuild_src_dropdowns flag
        self._bulk_loading      = False   # suppresses trace-driven rebuild during restore
        self._user_unassigned   = set()   # paths the user explicitly cleared

        self._hdr = hdr = tk.Frame(self, bg=SURF)
        hdr.pack(fill="x", padx=12, pady=(10,4))
        tk.Label(hdr, text="EPISODE MEDIA POOL",
                 font=FL, bg=SURF, fg=ACCENT).pack(side="left")
        self._count_lbl = tk.Label(hdr, text="", font=FB, bg=SURF, fg=SUB)
        self._count_lbl.pack(side="left", padx=(10, 0))

        hint = ("Drop all episode files here at once  "
                "(Riverside audio + video + VO takes)"
                if HAS_DND else "Use Browse to add files")
        self.dz = tk.Frame(self, bg=SURF3,
                           highlightbackground=BORDER, highlightthickness=1)
        self.dz.pack(fill="x", padx=12, pady=(0,6))
        self._dz_lbl = tk.Label(self.dz, text=hint,
                                 font=FB, bg=SURF3, fg=SUB, pady=14)
        self._dz_lbl.pack(fill="x")

        # Column widths (pixels).  0 = not yet user-set; auto from content.
        self._pool_name_col_w   = 0
        self._pool_dd_col_w     = 0
        self._pool_col_user_set = False   # True once the user has dragged the sash
        self._pool_sash_drag    = {}      # transient drag state
        self._col_hdr_widths    = (0, 0)  # (name_w, dd_w) last drawn in header

        # Column header row — always kept in pack order; empty = zero height
        self._col_hdr = tk.Frame(self, bg=SURF)
        self._col_hdr.pack(fill="x", padx=12, pady=(2, 0))
        self._rebuild_col_hdr()

        self._table = tk.Frame(self, bg=SURF)
        self._table.pack(fill="x", padx=12)

        foot = tk.Frame(self, bg=SURF)
        foot.pack(anchor="w", padx=12, pady=(4,0))
        bb = tk.Label(foot, text="+ BROWSE FILES", font=FB, bg=SURF, fg=SUB,
                      cursor="arrow", padx=6, pady=3,
                      bd=0, highlightbackground=BORDER, highlightthickness=1)
        bb.bind("<Enter>", lambda e, w=bb: w.config(bg=ACCENT, fg=TEXT))
        bb.bind("<Leave>", lambda e, w=bb: w.config(bg=SURF,   fg=SUB))
        bb.bind("<Button-1>", lambda e: self._browse_files())
        bb.pack(side="left", padx=(0,8))
        bf = tk.Label(foot, text="+ BROWSE FOLDER", font=FB, bg=SURF, fg=SUB,
                      cursor="arrow", padx=6, pady=3,
                      bd=0, highlightbackground=BORDER, highlightthickness=1)
        bf.bind("<Enter>", lambda e, w=bf: w.config(bg=ACCENT, fg=TEXT))
        bf.bind("<Leave>", lambda e, w=bf: w.config(bg=SURF,   fg=SUB))
        bf.bind("<Button-1>", lambda e: self._browse_folder())
        bf.pack(side="left")

        if HAS_DND:
            for w in [self, self.dz, self._dz_lbl]:
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>",
                           lambda e: [self._add(p) for p in parse_dnd(e.data)])

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", padx=12, pady=(8,0))
        src_hdr = tk.Frame(self, bg=SURF)
        src_hdr.pack(fill="x", padx=12, pady=(6,2))
        tk.Label(src_hdr, text="TRANSCRIPT SOURCES  ",
                 font=FL, bg=SURF, fg=ACCENT).pack(side="left")
        tk.Label(src_hdr,
                 text="(which file Whisper reads for each session — choose guest audio)",
                 font=FB, bg=SURF, fg=SUB).pack(side="left")
        self._src_frame = tk.Frame(self, bg=SURF)
        self._src_frame.pack(fill="x", padx=12, pady=(0,10))
        self._src_dropdowns = {}
        self._rebuild_src_dropdowns()

    def _browse_files(self):
        paths = filedialog.askopenfilenames(
            title="Select episode media files",
            filetypes=[("Media",
                        "*.mp4 *.mov *.mxf *.avi *.mkv *.m4v *.webm "
                        "*.wmv *.mpg *.mpeg *.ts *.mts *.m2ts "
                        "*.flv *.ogv *.3gp *.dv *.r3d *.braw *.ari "
                        "*.wav *.aif *.aiff *.mp3 *.m4a *.aac *.flac *.ogg *.opus *.wma *.caf "
                        "*.MP4 *.MOV *.MXF *.M4V *.WMV *.WAV *.MP3 *.M4A *.FLAC *.WMA"),
                       ("All","*.*")])
        for p in paths: self._add(p)

    def _browse_folder(self):
        folder = filedialog.askdirectory(title="Select episode folder")
        if not folder: return
        # Lowercase only — comparison uses .lower() so cases are handled uniformly
        EXTS = {
            # Professional / camera formats
            ".mp4", ".mov", ".mxf", ".avi", ".mkv", ".m4v", ".webm",
            ".wmv", ".mpg", ".mpeg", ".ts", ".mts", ".m2ts",
            ".flv", ".ogv", ".3gp", ".dv", ".r3d", ".braw", ".ari",
            # Broadcast / studio audio
            ".wav", ".aif", ".aiff",
            # Consumer / archival audio
            ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus",
            ".wma", ".caf",
        }
        self._dz_lbl.config(text="Scanning folder…")
        def _walk():
            paths = []
            for root, _, files in os.walk(folder):
                for fn in sorted(files):
                    if os.path.splitext(fn)[1].lower() in EXTS:
                        paths.append(os.path.join(root, fn))
            self.after(0, lambda ps=paths: [self._add(p) for p in ps])
        import threading as _threading
        _threading.Thread(target=_walk, daemon=True).start()

    def _add(self, path):
        if not path: return
        fn_check = os.path.basename(path)
        if fn_check.startswith("._") or fn_check == ".DS_Store":
            return
        if any(r["path"] == path for r in self._rows): return

        fn  = basename(path)
        tok = suggest_token(fn, self._tokens) or ""
        if not tok:
            vo = suggest_vo_part(fn, self._parts)
            if vo:
                tok = vo

        options = ["— unassigned —"] + sorted(self._tokens) + [
            "VO: {}".format(p["name"]) for p in self._parts
        ]

        if tok:
            self._auto_assigned.add(path)
        var = tk.StringVar(value=tok if tok else "— unassigned —")

        # Mutable refs so the callback can update widgets created after it
        strip_ref   = [None]   # left-edge accent strip
        tint_refs   = []       # widgets that receive the full-row background tint

        def _apply_token_style(token):
            strip_color = _token_color(token)
            row_bg      = _token_row_bg(token)
            if strip_ref[0]:
                strip_ref[0].config(bg=strip_color)
            for w in tint_refs:
                try:
                    w.config(bg=row_bg)
                except Exception:
                    pass

        def _on_token_change(*a, _p=path, _v=var):
            if not getattr(self, "_in_mirror", False):
                self._auto_assigned.discard(_p)
                if _v.get() == "— unassigned —":
                    # User explicitly cleared this row — protect it from re-pairing
                    self._user_unassigned.add(_p)
                else:
                    self._user_unassigned.discard(_p)
            _apply_token_style(_v.get())
            self._mirror_assignment(_p, _v.get())
            if not self._bulk_loading:
                self._rebuild_src_dropdowns()
        var.trace_add("write", _on_token_change)

        initial_bg = _token_row_bg(tok)
        row = tk.Frame(self._table, bg=initial_bg,
                       highlightbackground=BORDER, highlightthickness=1)
        row.pack(fill="x", pady=2)
        tint_refs.append(row)

        # Saturated left-edge strip — strong visual anchor for the token colour
        strip = tk.Frame(row, bg=_token_color(tok), width=6, bd=0)
        strip.pack(side="left", fill="y")
        strip.pack_propagate(False)
        strip_ref[0] = strip

        kind = "VIDEO" if is_video(path) else "AUDIO"
        kc   = ("#1e3a1e","#5aab61") if kind=="VIDEO" else ("#1e2a3a","#5588cc")
        tk.Label(row, text=kind, font=("Courier New",9,"bold"),
                 bg=kc[0], fg=kc[1], padx=5, pady=3).pack(side="left")

        # ── Filename column (fixed pixel width, auto-sized to content) ──────────
        fn_w = self._pool_name_col_w if self._pool_name_col_w else 240
        fn_frame = tk.Frame(row, bg=initial_bg, width=fn_w)
        fn_frame.pack(side="left", fill="y")
        fn_frame.pack_propagate(False)
        tint_refs.append(fn_frame)

        fn_lbl = tk.Label(fn_frame, text=fn, font=FB, bg=initial_bg, fg=TEXT,
                          padx=6, anchor="w")
        fn_lbl.pack(fill="both", expand=True)
        tint_refs.append(fn_lbl)

        # ── Draggable column sash ─────────────────────────────────────────────
        sash = tk.Frame(row, bg=SURF3, width=5, cursor="sb_h_double_arrow")
        sash.pack(side="left", fill="y")
        sash.pack_propagate(False)

        def _sash_press(e, s=sash):
            self._pool_sash_drag["x0"] = e.x_root
            self._pool_sash_drag["w0"] = self._pool_name_col_w or fn_w
            s.config(bg=ACCENT)

        def _sash_motion(e):
            if "x0" not in self._pool_sash_drag:
                return
            delta = e.x_root - self._pool_sash_drag["x0"]
            new_w = max(80, self._pool_sash_drag["w0"] + delta)
            self._pool_name_col_w   = new_w
            self._pool_col_user_set = True
            for r in self._rows:
                ff = r.get("fn_frame")
                if ff:
                    try:
                        ff.config(width=new_w)
                    except Exception:
                        pass
            self._rebuild_col_hdr()

        def _sash_release(e, s=sash):
            self._pool_sash_drag.clear()
            s.config(bg=SURF3)

        sash.bind("<ButtonPress-1>",   _sash_press)
        sash.bind("<B1-Motion>",       _sash_motion)
        sash.bind("<ButtonRelease-1>", _sash_release)
        sash.bind("<Enter>", lambda e, w=sash: w.config(bg=ACCENT))
        sash.bind("<Leave>", lambda e, w=sash: (
            w.config(bg=SURF3) if "x0" not in self._pool_sash_drag else None))

        # ── Dropdown column (pixel-width frame so it never truncates) ─────────
        dd_w = self._pool_dd_col_w if self._pool_dd_col_w else 200
        dd_frame = tk.Frame(row, bg=initial_bg, width=dd_w)
        dd_frame.pack(side="left", fill="y", padx=(0, 4))
        dd_frame.pack_propagate(False)
        tint_refs.append(dd_frame)

        om = _FlatDropdown(
            dd_frame,
            textvariable=var,
            values=options,
            state="readonly",
            font=FB,
            width=1,   # label expands to fill dd_frame; width=1 is just a minimum
        )
        om.pack(fill="both", expand=True)

        rm = tk.Label(row, text="✕", font=FB, bg=initial_bg, fg=SUB,
                      cursor="hand2", padx=6)
        rm.pack(side="left")
        tint_refs.append(rm)

        rec = {"path": path, "var": var, "row": row, "fn_frame": fn_frame,
               "fn_lbl": fn_lbl, "dd_frame": dd_frame, "sash": sash}
        self._rows.append(rec)
        rm.bind("<Button-1>", lambda e, r=rec: self._remove(r))

        self._dz_lbl.config(text="")
        self._refresh_count()
        if not self._sort_scheduled:
            self._sort_scheduled = True
            self.after(50, self._deferred_sort)
        if not self._src_dd_scheduled:
            self._src_dd_scheduled = True
            self.after(60, self._deferred_src_dd)

        # In AAF mode, schedule a deferred video-prune once after bulk imports
        if self._aaf_mode and not self._prune_scheduled:
            self._prune_scheduled = True
            self.after(80, self._deferred_prune)

    def _sort_rows(self):
        """Re-pack pool rows alphabetically; auto-size columns unless user has
        manually resized via the sash."""
        self._rows.sort(key=lambda r: os.path.basename(r["path"]).lower())

        if self._rows:
            try:
                _dpi = self.winfo_fpixels('1i')
            except Exception:
                _dpi = 96.0
            _ppc = max(9, int(9.0 * _dpi / 96.0 + 0.9))   # pixels per char

            # Filename column: auto-size unless user has dragged the sash
            if not self._pool_col_user_set:
                _max_fn  = max(len(os.path.basename(r["path"])) for r in self._rows)
                _name_w  = max(160, _max_fn * _ppc + 32)
                self._pool_name_col_w = _name_w

            # Dropdown column: always auto-size from longest option (never user-set)
            if not self._pool_dd_col_w:
                # Sample options from the first row's dropdown children if possible;
                # otherwise derive from tokens + part names stored on the pool.
                _toks  = list(self._tokens) + ["VO: {}".format(p["name"])
                                               for p in self._parts]
                _opts  = ["— unassigned —"] + sorted(_toks)
                _max_o = max((len(o) for o in _opts), default=20)
                self._pool_dd_col_w = max(180, _max_o * _ppc + 40)

            # Apply widths to all rows
            for r in self._rows:
                ff = r.get("fn_frame")
                if ff:
                    try: ff.config(width=self._pool_name_col_w)
                    except Exception: pass
                df = r.get("dd_frame")
                if df:
                    try: df.config(width=self._pool_dd_col_w)
                    except Exception: pass

            _new_widths = (self._pool_name_col_w, self._pool_dd_col_w)
            if _new_widths != self._col_hdr_widths:
                self._col_hdr_widths = _new_widths
                self._rebuild_col_hdr()

        for r in self._rows:
            r["row"].pack_forget()
            r["row"].pack(fill="x", pady=1)

    def _rebuild_col_hdr(self):
        """Rebuild the column header labels to match current column widths."""
        for w in self._col_hdr.winfo_children():
            w.destroy()

        if not self._rows:
            # Show drop zone, leave _col_hdr in place (empty = zero height)
            try:
                self.dz.pack(fill="x", padx=12, pady=(0, 6))
            except Exception:
                pass
            return

        # Hide drop zone while rows are present
        try:
            self.dz.pack_forget()
        except Exception:
            pass

        _HDR_H = 20   # explicit header row height in pixels

        # Fixed prefix: strip (6px) + KIND badge.  Measure by asking tk for the
        # badge width after rendering; fall back to a safe constant.
        _PREFIX_W = 62   # 6px strip + ~56px "AUDIO"/"VIDEO" badge with padx=5

        tk.Frame(self._col_hdr, bg=SURF,
                 width=_PREFIX_W, height=_HDR_H).pack(side="left")

        # FILE header — fixed pixel width + explicit height avoids collapse
        name_w = self._pool_name_col_w or 240
        nf = tk.Frame(self._col_hdr, bg=SURF, width=name_w, height=_HDR_H)
        nf.pack_propagate(False)
        nf.pack(side="left")
        tk.Label(nf, text="FILE", font=("Courier New", 8, "bold"),
                 bg=SURF, fg=SUB, anchor="w", padx=6).pack(fill="both", expand=True)

        # Sash spacer (matches the 5px sash in every row)
        tk.Frame(self._col_hdr, bg=SURF3,
                 width=5, height=_HDR_H).pack(side="left")

        # ASSIGNMENT header
        dd_w = self._pool_dd_col_w or 200
        df = tk.Frame(self._col_hdr, bg=SURF, width=dd_w, height=_HDR_H)
        df.pack_propagate(False)
        df.pack(side="left", padx=(0, 4))
        tk.Label(df, text="ASSIGNMENT", font=("Courier New", 8, "bold"),
                 bg=SURF, fg=SUB, anchor="w", padx=6).pack(fill="both", expand=True)

    def _deferred_sort(self):
        self._sort_scheduled = False
        self._sort_rows()

    def _deferred_src_dd(self):
        self._src_dd_scheduled = False
        self._rebuild_src_dropdowns()

    def _deferred_prune(self):
        self._prune_scheduled = False
        self.prune_redundant_videos()

    def prune_redundant_videos(self):
        """Remove a video row only when an audio file with an *identical*
        normalised basename exists under the same token.

        Matching uses two forms (via ``_pool_norm``):
          • plain_norm  — lowercase, separators→space, extension stripped
          • stripped_norm — same but also removes Riverside _raw_audio /
            _raw_synced_video_cfr suffixes so counterpart pairs share a core name

        A video whose name does not exactly match any audio counterpart is
        always kept — even if other audio files share the same token.
        Safe to call multiple times.  Returns the number of rows removed."""
        # Build: token → set of normalised audio basenames (plain + stripped)
        token_audio_norms: dict = {}
        for r in self._rows:
            tok = r["var"].get()
            if tok == "— unassigned —" or is_video(r["path"]):
                continue
            plain, stripped = _pool_norm(r["path"])
            s = token_audio_norms.setdefault(tok, set())
            s.add(plain)
            s.add(stripped)

        to_remove = []
        for r in self._rows:
            if not is_video(r["path"]):
                continue
            tok = r["var"].get()
            if tok == "— unassigned —":
                continue
            audio_norms = token_audio_norms.get(tok)
            if not audio_norms:
                continue
            vid_plain, vid_stripped = _pool_norm(r["path"])
            # Only prune if the video's name exactly matches an audio counterpart
            if vid_plain in audio_norms or vid_stripped in audio_norms:
                to_remove.append(r)

        for rec in to_remove:
            self._remove(rec)
        return len(to_remove)

    def _mirror_assignment(self, changed_path, new_token):
        if new_token == "— unassigned —":
            return
        if getattr(self, "_in_mirror", False):
            return

        src_fn   = basename(changed_path)
        src_base = os.path.splitext(src_fn)[0].lower().strip()
        src_no_take = _strip_take_number(src_fn)
        src_person  = _riverside_person_segment(src_fn)
        src_stripped = _strip_media_type(src_fn)

        self._in_mirror = True
        try:
            for r in self._rows:
                if r["path"] == changed_path:
                    continue
                current = r["var"].get()
                if current != "— unassigned —":
                    continue
                # Skip rows the user explicitly cleared
                if r["path"] in self._user_unassigned:
                    continue

                cand_fn      = basename(r["path"])
                cand_base    = os.path.splitext(cand_fn)[0].lower().strip()
                cand_no_take = _strip_take_number(cand_fn)

                if src_base == cand_base:
                    r["var"].set(new_token)
                    continue

                if src_no_take and cand_no_take and src_no_take == cand_no_take:
                    r["var"].set(new_token)
                    continue

                cand_person = _riverside_person_segment(cand_fn)
                if src_person is not None and cand_person is not None:
                    if src_person == cand_person:
                        r["var"].set(new_token)
                    continue

                cand_stripped = _strip_media_type(cand_fn)
                if _name_similarity(src_stripped, cand_stripped) >= 0.6:
                    r["var"].set(new_token)
        finally:
            self._in_mirror = False

    def _remove(self, rec):
        self._rows = [r for r in self._rows if r is not rec]
        rec["row"].destroy()
        self._refresh_count()
        self._rebuild_src_dropdowns()

    def _refresh_count(self):
        n = len(self._rows)
        self._count_lbl.config(text="{} file{}".format(n, "s" if n!=1 else ""))

    def _rebuild_src_dropdowns(self):
        for w in self._src_frame.winfo_children():
            w.destroy()
        self._src_dropdowns = {}
        asgn = self.get_assignments()
        for tok in sorted(self._tokens):
            audio_paths = [p for p in asgn.get(tok, []) if not is_video(p)]
            if not audio_paths:
                continue
            options = [basename(p) for p in audio_paths]
            if tok not in self._src_vars:
                self._src_vars[tok] = tk.StringVar()
            sv = self._src_vars[tok]
            if sv.get() not in options:
                sv.set(options[0])
            row = tk.Frame(self._src_frame, bg=SURF); row.pack(anchor="w", pady=1)
            tk.Label(row, text="[{}]".format(tok), font=FB,
                     bg=SURF, fg=ACCENT, width=14, anchor="w").pack(side="left")
            om = _FlatDropdown(
                row,
                textvariable=sv,
                values=options,
                state="readonly",
                font=FB,
                width=52,
            )
            om.pack(side="left")
            self._src_dropdowns[tok] = (sv, audio_paths)

    def get_assignments(self):
        out = {}
        for r in self._rows:
            tok = r["var"].get()
            if tok == "— unassigned —": continue
            out.setdefault(tok, []).append(r["path"])
        return out

    def get_transcript_source(self, token):
        sv_tuple = self._src_dropdowns.get(token)
        if not sv_tuple:
            return None
        sv, audio_paths = sv_tuple
        sel = sv.get()
        for p in audio_paths:
            if basename(p) == sel:
                return p
        return audio_paths[0] if audio_paths else None

    def get_interview_assets(self):
        asgn = self.get_assignments()
        return {tok: asgn[tok] for tok in self._tokens if tok in asgn}

    def get_vo_assets(self):
        asgn = self.get_assignments()
        out  = {}
        part_by_name = {"VO: {}".format(p["name"]): p["index"] for p in self._parts}
        for key, paths in asgn.items():
            if key not in part_by_name: continue
            pi = part_by_name[key]
            out.setdefault(pi, {"audios": [], "videos": []})
            for p in paths:
                if is_video(p):
                    out[pi]["videos"].append(p)
                else:
                    out[pi]["audios"].append(p)
        return out