import os
import tkinter as tk
import re
import threading
import json
from tkinter import filedialog, messagebox

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

def _get_dur_key(path):
    """Return file duration rounded to nearest second as a string key, or None.
    Used to identify duration-matched files for auto-linking.  Called in a
    background thread so blocking subprocess calls are fine."""
    import subprocess as _sp
    import json as _json
    ext = os.path.splitext(path)[1].lower()
    # Fast path: native Python for WAV files
    if ext == '.wav':
        try:
            import wave as _wave
            with _wave.open(path, 'rb') as wf:
                dur = wf.getnframes() / float(wf.getframerate())
                return str(round(dur))
        except Exception:
            pass  # fall through to ffprobe
    # General path: ffprobe for everything else (including AIFF, MP4, MOV…)
    try:
        try:
            from engines import _ffprobe_cmd
            probe = _ffprobe_cmd()
        except Exception:
            probe = ['ffprobe']
        res = _sp.run(
            probe + ['-v', 'quiet', '-print_format', 'json',
                     '-show_entries', 'format=duration', path],
            capture_output=True, timeout=10)
        if res.returncode == 0:
            data = _json.loads(res.stdout)
            dur = float(data['format']['duration'])
            return str(round(dur))
    except Exception:
        pass
    return None


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
        self._aaf_mode          = aaf_mode
        self._prune_scheduled   = False
        self._sort_scheduled    = False   # deferred _sort_rows flag
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
        threading.Thread(target=_walk, daemon=True).start()

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
            if not getattr(self, "_link_propagating", False):
                self._mirror_assignment(_p, _v.get())
            if (not getattr(self, "_in_mirror", False)
                    and not getattr(self, "_link_propagating", False)
                    and not self._bulk_loading):
                self._handle_link_change(_p, _v.get())
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

        # Link indicator — packed by _update_link_visuals when row is in a group
        link_lbl = tk.Label(row, text="↔", font=("Courier New", 9, "bold"),
                            bg=initial_bg, fg=ACCENT, padx=4, pady=2)
        tint_refs.append(link_lbl)
        # (not packed here — _update_link_visuals inserts it before rm when needed)

        # 🗑 — wipe cache for this file only (transcript + assigned-token
        # pull results).  Lets the user re-transcribe / re-reconcile one
        # outlier after fixing a bad pool assignment WITHOUT invalidating
        # the cache for every other file they've already brought over
        # the line.  Shown on every MediaPool row — MediaPool is only
        # ever instantiated for the Script→Session flow (the AAF→XML
        # workflow has its own pool in aaf_workflow.py), so there's no
        # workflow to hide from.
        wipe = tk.Label(row, text="🗑", font=FB, bg=initial_bg, fg=SUB,
                        cursor="hand2", padx=4)
        wipe.pack(side="left")
        tint_refs.append(wipe)

        rm = tk.Label(row, text="✕", font=FB, bg=initial_bg, fg=SUB,
                      cursor="hand2", padx=6)
        rm.pack(side="left")
        tint_refs.append(rm)

        rec = {"path": path, "var": var, "row": row, "fn_frame": fn_frame,
               "fn_lbl": fn_lbl, "dd_frame": dd_frame, "sash": sash,
               "link_id": None, "link_mirrored": False,
               "link_lbl": link_lbl, "rm_lbl": rm}
        self._rows.append(rec)
        rm.bind("<Button-1>", lambda e, r=rec: self._remove(r))
        wipe.bind("<Button-1>",
                   lambda e, r=rec: self._wipe_cache(r))

        self._dz_lbl.config(text="")
        self._refresh_count()
        if not self._sort_scheduled:
            self._sort_scheduled = True
            self.after(50, self._deferred_sort)
        # In AAF mode, schedule a deferred video-prune once after bulk imports
        if self._aaf_mode and not self._prune_scheduled:
            self._prune_scheduled = True
            self.after(80, self._deferred_prune)

        # Background: detect duration and assign to a link group
        def _fetch_dur(_p=path):
            dk = _get_dur_key(_p)
            if dk is not None:
                self.after(0, lambda: self._set_link_group(_p, dk))
        threading.Thread(target=_fetch_dur, daemon=True).start()

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
                # Rule 4 — word-similarity mirror.  The old check fired at
                # 0.6 with no speaker anchor: for a pool named e.g.
                # "Turks_and_Caicos_<SPEAKER>_take1.wav" the shared
                # prefix {turks, and, caicos, take1} alone exceeded 60 %
                # of BRYAN1 vs RYAN1, so assigning a token to ONE file
                # cascaded onto every same-prefix file regardless of
                # speaker (reported).  Anchor the mirror on the assigned
                # token appearing in BOTH filenames as a word — either
                # the exact form or the form without trailing digits, so
                # BRYAN1 matches "…BRYAN1…" and "…BRYAN…" alike but a
                # RYAN1 file has neither anchor and is skipped.  VO
                # tokens ("VO: Part 1") have no filename anchor; keep
                # the old behaviour there.
                if not new_token.startswith("VO:"):
                    _anchors = {new_token.lower().strip()}
                    _stripped_digits = re.sub(r"\d+$", "",
                                              new_token.lower().strip())
                    if _stripped_digits:
                        _anchors.add(_stripped_digits)
                    _sw = set(re.sub(r"[^a-z0-9]", " ", src_stripped).split())
                    _cw = set(re.sub(r"[^a-z0-9]", " ", cand_stripped).split())
                    if not (_anchors & _sw) or not (_anchors & _cw):
                        continue
                if _name_similarity(src_stripped, cand_stripped) >= 0.6:
                    r["var"].set(new_token)
        finally:
            self._in_mirror = False

    def _set_link_group(self, path, dur_key):
        """Called on the main thread after the background duration fetch.
        Assigns link_id to the row and refreshes the link indicators."""
        for r in self._rows:
            if r["path"] == path:
                r["link_id"] = dur_key
                break
        self._update_link_visuals()

    def _handle_link_change(self, path, new_token):
        """Called when the user changes a token (not via mirror or propagation).
        If the row was passively mirrored, break its link so it can diverge.
        Otherwise propagate the new token to all files in the same duration group."""
        for r in self._rows:
            if r["path"] == path:
                if r.get("link_mirrored"):
                    # User overrode a passively-linked row → unlink it completely
                    r["link_id"] = None
                    r["link_mirrored"] = False
                    self._update_link_visuals()
                else:
                    self._propagate_link_change(path, new_token)
                return

    def _propagate_link_change(self, path, new_token):
        """Mirror new_token to all rows sharing the same link_id as path."""
        src_link_id = None
        for r in self._rows:
            if r["path"] == path:
                src_link_id = r.get("link_id")
                break
        if not src_link_id:
            return
        self._link_propagating = True
        try:
            for r in self._rows:
                if r["path"] == path:
                    continue
                if r.get("link_id") == src_link_id:
                    r["link_mirrored"] = True
                    r["var"].set(new_token)
        finally:
            self._link_propagating = False
        self._update_link_visuals()

    def _update_link_visuals(self):
        """Show or hide the ↔ link indicator on each pool row.
        Active (source) rows show it in ACCENT; passively-mirrored rows in SUB."""
        # Bail early if the pool itself has been destroyed — background
        # duration-fetch threads can fire after the user has navigated past
        # Step 2 and the widgets are gone.
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return

        # Count rows per link_id
        counts: dict = {}
        for r in self._rows:
            lid = r.get("link_id")
            if lid:
                counts[lid] = counts.get(lid, 0) + 1

        for r in self._rows:
            lbl = r.get("link_lbl")
            rm  = r.get("rm_lbl")
            if lbl is None or rm is None:
                continue
            # Skip rows whose widgets were destroyed between scheduling
            # and execution of this deferred update.
            try:
                if not lbl.winfo_exists() or not rm.winfo_exists():
                    continue
            except tk.TclError:
                continue
            lid     = r.get("link_id")
            visible = lid is not None and counts.get(lid, 0) >= 2
            if visible:
                color = SUB if r.get("link_mirrored") else ACCENT
                try:
                    lbl.config(fg=color)
                except tk.TclError:
                    continue
                try:
                    lbl.pack(side="left", before=rm)
                except Exception:
                    pass
            else:
                try:
                    lbl.pack_forget()
                except Exception:
                    pass

    def _remove(self, rec):
        self._rows = [r for r in self._rows if r is not rec]
        rec["row"].destroy()
        self._refresh_count()
        self._update_link_visuals()

    def _wipe_cache(self, rec):
        """Delete the cached pull-reconciliation results for the token
        assigned to this row.  On the next reconcile, those pulls redo;
        every other token's pulls hit cache and finish instantly.

        Deliberately does NOT touch the file's transcript (either the
        sidecar or the .pb_cache/{md5}.json entry).  Multiple tokens
        commonly share the same audio file (e.g. one Part-N.wav used
        by every speaker who appears in that part), and deleting the
        file's transcript would cascade into re-transcription plus
        re-reconciliation for EVERY token touching it — the exact
        "reconcile started from the beginning" symptom the user just
        reported.  Wiping only pull_*.json for the token targets just
        the pulls that were misassigned, and their re-reconcile hits
        the still-cached transcript for whatever file the pool now
        points them at.
        """
        path = rec.get("path")
        if not path:
            return
        tok = (rec["var"].get() if rec.get("var") else "") or ""
        if not tok or tok == "— unassigned —":
            messagebox.showinfo(
                "No token to wipe",
                "This file isn't assigned to a token yet — nothing to "
                "wipe.  Assign it to a token first, then click 🗑.")
            return

        import engines   # lazy so gui_components stays leaf-ish
        fn = os.path.basename(path)

        # Ask which layer(s) to invalidate.  Three named scopes cascade
        # correctly because of content-addressing: dropping the mix
        # forces a new transcript AND new pulls (mix path is the
        # transcript's key); dropping the transcript forces new pulls
        # (transcript path is the pull key).
        scope = self._ask_wipe_scope(tok, fn)
        if scope is None:
            return   # user cancelled

        cache_dir = getattr(engines, "_cache_dir", None)
        n_pulls, n_transcripts, n_mixes = 0, 0, 0

        # 1. Pull results — always deleted (this is the smallest scope).
        if cache_dir and os.path.isdir(cache_dir):
            for _fn in os.listdir(cache_dir):
                if not (_fn.startswith("pull_") and _fn.endswith(".json")):
                    continue
                p = os.path.join(cache_dir, _fn)
                try:
                    with open(p, encoding="utf-8") as f:
                        d = json.load(f)
                    result = d.get("result") if isinstance(d, dict) else None
                    if isinstance(result, dict) and result.get("token") == tok:
                        os.remove(p); n_pulls += 1
                except Exception:
                    continue

        # Resolve THIS token's transcript path (mix path for multi-file,
        # source path for single-file) so we can target its transcript /
        # mix without touching other tokens'.
        paths_for_tok = []
        for _r in self._rows:
            if _r.get("var") and _r["var"].get() == tok:
                _p = _r.get("path")
                if _p: paths_for_tok.append(_p)
        if len(paths_for_tok) >= 2:
            try:
                transcript_key_path = engines._stable_mix_cache_path(paths_for_tok)
            except Exception:
                transcript_key_path = None
        elif len(paths_for_tok) == 1:
            transcript_key_path = paths_for_tok[0]
        else:
            transcript_key_path = None

        # 2. Transcript — deleted for "re-transcribe" and "re-mix" scopes.
        if scope in ("transcribe", "mix") and transcript_key_path:
            try:
                # .pb_transcript.json sidecar next to the media/mix
                sc = engines.pb_transcript_path(transcript_key_path)
                if sc and os.path.isfile(sc):
                    os.remove(sc); n_transcripts += 1
                # Internal .pb_cache/{md5}.json transcript entry
                tc = engines._cache_path(transcript_key_path)
                if tc and os.path.isfile(tc):
                    os.remove(tc); n_transcripts += 1
            except Exception:
                pass

        # 3. Mix WAV — deleted only for "re-mix" scope, and only if
        # this token is genuinely multi-file (a mix exists).
        if scope == "mix" and len(paths_for_tok) >= 2 and transcript_key_path:
            try:
                if os.path.isfile(transcript_key_path):
                    os.remove(transcript_key_path); n_mixes += 1
            except Exception:
                pass

        # Flash ✓ on the button briefly so the click is confirmed.
        try:
            for _child in rec["row"].winfo_children():
                if isinstance(_child, tk.Label) and _child.cget("text") == "🗑":
                    _child.config(text="✓")
                    _child.after(1400, lambda w=_child: w.config(text="🗑"))
                    break
        except Exception:
            pass

        _summary = ["{} cached pull result{}".format(n_pulls, "s" if n_pulls != 1 else "")]
        if n_transcripts:
            _summary.append("{} transcript entr{}".format(
                n_transcripts, "ies" if n_transcripts != 1 else "y"))
        if n_mixes:
            _summary.append("{} mixed audio file".format(n_mixes))
        messagebox.showinfo(
            "Cleared [{}]".format(tok),
            "Removed for token [{}]:\n  • {}\n\n"
            "Run reconcile — the redo happens against whichever file(s) "
            "the pool now points this token at.  Every other token's "
            "cache is untouched.".format(tok, "\n  • ".join(_summary)))

    def _ask_wipe_scope(self, tok, fn):
        """Modal chooser with 3 named scopes.  Returns 'pulls',
        'transcribe', 'mix', or None (cancel).  The scopes cascade
        naturally through content-addressing — see _wipe_cache."""
        win = tk.Toplevel(self)
        win.title("Redo [{}]".format(tok))
        win.configure(bg=BG)
        win.transient(self.winfo_toplevel())
        win.grab_set()
        win.resizable(False, False)

        result = {"scope": None}

        tk.Label(win, text="Redo work for token  [{}]".format(tok),
                 font=FBT, bg=BG, fg=TEXT
                 ).pack(anchor="w", padx=16, pady=(14, 2))
        tk.Label(win, text="This row: {}".format(fn),
                 font=FB, bg=BG, fg=SUB
                 ).pack(anchor="w", padx=16, pady=(0, 10))

        scope_var = tk.StringVar(value="pulls")
        _OPTS = [
            ("pulls",
             "Redo pull matching only  (fast)",
             "Deletes cached reconciliation results for this token.  "
             "Keeps the transcript and the mixed audio.  Use when the "
             "pool assignment was wrong but the audio is the same "
             "content."),
            ("transcribe",
             "Re-transcribe  (medium)",
             "Also deletes the cached transcript, so Whisper runs "
             "again for this token.  Keeps the mixed audio (it's "
             "reused).  Use if the transcript has errors or you "
             "switched Whisper models."),
            ("mix",
             "Re-mix + re-transcribe  (slow)",
             "Also deletes the mixed audio file, so multi-file tokens "
             "get remixed from source.  Use if you fixed the mix "
             "quality or changed which files feed a multi-file token."),
        ]
        for key, title, desc in _OPTS:
            row = tk.Frame(win, bg=BG)
            row.pack(fill="x", padx=16, pady=(2, 0))
            rb = tk.Radiobutton(row, text=title, variable=scope_var,
                                value=key, font=FBT, bg=BG, fg=TEXT,
                                selectcolor=BG, activebackground=BG,
                                activeforeground=TEXT,
                                highlightthickness=0, bd=0)
            rb.pack(anchor="w")
            tk.Label(row, text="   " + desc, font=FB, bg=BG, fg=SUB,
                     wraplength=460, justify="left"
                     ).pack(anchor="w", padx=(24, 0), pady=(0, 4))

        nav = tk.Frame(win, bg=BG)
        nav.pack(fill="x", padx=16, pady=(10, 14))
        cancel = tk.Label(nav, text="  Cancel  ", font=FB, bg=SURF3, fg=TEXT,
                          cursor="hand2", padx=8, pady=4, bd=0,
                          highlightbackground=BORDER, highlightthickness=1)
        cancel.pack(side="left")
        cancel.bind("<Enter>", lambda e: cancel.config(bg="#5a2020"))
        cancel.bind("<Leave>", lambda e: cancel.config(bg=SURF3))
        cancel.bind("<ButtonRelease-1>", lambda e: win.destroy())

        go = tk.Label(nav, text="  Redo  ", font=FBT, bg=SUCCESS, fg=TEXT,
                      cursor="hand2", padx=12, pady=4, bd=0,
                      highlightbackground=BORDER, highlightthickness=1)
        go.pack(side="right")
        go.bind("<Enter>", lambda e: go.config(bg="#4a9a51"))
        go.bind("<Leave>", lambda e: go.config(bg=SUCCESS))
        def _apply():
            result["scope"] = scope_var.get()
            win.destroy()
        go.bind("<ButtonRelease-1>", lambda e: _apply())
        win.bind("<Return>", lambda e: _apply())
        win.bind("<Escape>", lambda e: win.destroy())

        win.update_idletasks()
        pw = self.winfo_toplevel().winfo_width()
        ph = self.winfo_toplevel().winfo_height()
        px = self.winfo_toplevel().winfo_rootx()
        py = self.winfo_toplevel().winfo_rooty()
        w = max(520, win.winfo_reqwidth())
        h = max(320, win.winfo_reqheight())
        win.geometry("{}x{}+{}+{}".format(
            w, h, px + max(0, (pw - w) // 2), py + max(0, (ph - h) // 2)))
        win.wait_window()
        return result["scope"]

    def _refresh_count(self):
        n = len(self._rows)
        self._count_lbl.config(text="{} file{}".format(n, "s" if n!=1 else ""))

    def get_assignments(self):
        out = {}
        for r in self._rows:
            tok = r["var"].get()
            if tok == "— unassigned —": continue
            out.setdefault(tok, []).append(r["path"])
        return out

    def get_audio_paths(self, token):
        """Return all non-video file paths assigned to token."""
        asgn = self.get_assignments()
        return [p for p in asgn.get(token, []) if not is_video(p)]

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