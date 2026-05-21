#!/usr/bin/env python3
"""
mix_test.py — GUI test tool for auto-mix + adaptive noise gate.
Double-click to open. No terminal required.
"""

import os
import sys
import threading
import subprocess
import wave
import tkinter as tk
from tkinter import filedialog

import numpy as np


# ── ffmpeg ─────────────────────────────────────────────────────────────────────

def _get_ffmpeg():
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from ffmpeg_bundled import get_ffmpeg_cmd
        return get_ffmpeg_cmd()
    except Exception:
        return ["ffmpeg"]

FFMPEG = _get_ffmpeg()


# ── DSP constants ──────────────────────────────────────────────────────────────

SR               = 16_000
FRAME_MS         = 20
FRAME_SAMP       = SR * FRAME_MS // 1000   # 320 samples

NOISE_PERCENTILE = 10
THRESHOLD_MULT   = 3.0
THRESHOLD_MIN    = 0.002
THRESHOLD_MAX    = 0.30

GATE_CLOSED_GAIN = 0.005
ATTACK_FRAMES    = 5
HOLD_FRAMES      = 15
RELEASE_FRAMES   = 40

MIX_PEAK_TARGET      = 0.90
LEVEL_MAX_BOOST_DB   = 20.0   # never boost a track more than this

# dynaudnorm settings (applied to final mix when leveling is requested)
DYN_FRAME_MS   = 500    # analysis frame length
DYN_GAUSS_SIZE = 31     # smoothing window (must be odd; larger = gentler gain changes)
DYN_PEAK       = 0.95   # target peak level
DYN_MAX_GAIN   = 5.0    # max boost factor (~14 dB); prevents over-amplifying silence


# ── DSP ───────────────────────────────────────────────────────────────────────

def decode_to_pcm(path):
    cmd = FFMPEG + ["-y", "-i", path,
                    "-ar", str(SR), "-ac", "1", "-f", "s16le", "pipe:1"]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed on {os.path.basename(path)!r}:\n"
            + r.stderr.decode(errors="replace")
        )
    return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def write_wav(pcm, path):
    pcm16 = (np.clip(pcm, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm16.tobytes())


def frame_rms(pcm):
    n = len(pcm) // FRAME_SAMP
    if n == 0:
        return np.array([0.0], dtype=np.float32)
    return np.sqrt(
        np.mean(pcm[: n * FRAME_SAMP].reshape(n, FRAME_SAMP) ** 2, axis=1)
    ).astype(np.float32)


def dbfs(rms):
    import math
    return 20.0 * math.log10(max(float(rms), 1e-9))


def build_gate_envelope(pcm, threshold):
    """Return a per-sample gain envelope in [GATE_CLOSED_GAIN, 1.0]."""
    rms = frame_rms(pcm)
    n   = len(rms)
    fg  = np.empty(n, dtype=np.float32)
    g   = 0.0
    hold = 0
    for i, r in enumerate(rms):
        if r >= threshold:
            hold = HOLD_FRAMES
            g    = min(1.0, g + 1.0 / ATTACK_FRAMES)
        elif hold > 0:
            hold -= 1
            g    = min(1.0, g + 1.0 / ATTACK_FRAMES)
        else:
            g    = max(GATE_CLOSED_GAIN, g - 1.0 / RELEASE_FRAMES)
        fg[i] = g
    body = np.repeat(fg, FRAME_SAMP)
    tail = len(pcm) - len(body)
    if tail > 0:
        body = np.concatenate([body, np.full(tail, fg[-1], dtype=np.float32)])
    return body.astype(np.float32)


def apply_gate(pcm, threshold):
    return (pcm * build_gate_envelope(pcm, threshold)).astype(np.float32)


def speech_rms(pcm, envelope):
    """
    RMS measured only over frames where the gate envelope is substantially
    open (mean gain > 0.5).  Measures the true speaking level of the track,
    ignoring silence and noise-floor frames.
    """
    n_frames = len(pcm) // FRAME_SAMP
    if n_frames == 0:
        return 1e-9
    p = pcm[: n_frames * FRAME_SAMP].reshape(n_frames, FRAME_SAMP)
    e = envelope[: n_frames * FRAME_SAMP].reshape(n_frames, FRAME_SAMP)
    mask = e.mean(axis=1) > 0.5          # frames where gate is open
    if not np.any(mask):
        return 1e-9
    return float(np.sqrt(np.mean(p[mask] ** 2)))


def mix_tracks(tracks):
    maxlen = max(len(t) for t in tracks)
    mixed  = np.sum(
        [np.pad(t, (0, maxlen - len(t))) for t in tracks], axis=0
    ).astype(np.float32)
    peak = np.max(np.abs(mixed))
    if peak > 1e-9:
        mixed *= MIX_PEAK_TARGET / peak
    return mixed


def apply_dynaudnorm(path):
    """
    Run ffmpeg dynaudnorm on `path` in-place.
    Writes to a temp file then replaces the original so ffmpeg never
    tries to read and write the same file simultaneously.
    """
    tmp = path + "._tmp.wav"
    cmd = FFMPEG + [
        "-y", "-i", path,
        "-af", (f"dynaudnorm="
                f"f={DYN_FRAME_MS}:"
                f"g={DYN_GAUSS_SIZE}:"
                f"p={DYN_PEAK}:"
                f"m={DYN_MAX_GAIN}"),
        tmp,
    ]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError(
            "dynaudnorm failed:\n" + r.stderr.decode(errors="replace")
        )
    os.replace(tmp, path)


def process(paths, output_path, debug, level_match, dynorm, log):
    """
    Run mix + gate (+ optional level match + optional dynamic normalization).
    Two passes: analyse all tracks first, then scale and mix.
    Calls log(str) for each diagnostic line.
    """
    W = 54
    log("─" * W)
    log(f"  Tracks      : {len(paths)}")
    log(f"  Level match : {'on' if level_match else 'off'}")
    log(f"  Dyn. norm   : {'on' if dynorm else 'off'}")
    log(f"  Output      : {os.path.basename(output_path)}")
    log("─" * W)

    # ── Pass 1 — decode and analyse ───────────────────────────────────────────
    track_data = []   # list of dicts per track

    for i, path in enumerate(paths):
        log(f"\n  [{i + 1}]  {os.path.basename(path)}")

        pcm = decode_to_pcm(path)
        log(f"       Duration  : {len(pcm) / SR:.1f} s")
        log(f"       Peak      : {dbfs(np.max(np.abs(pcm))):+.1f} dBFS")

        nf_rms    = float(np.percentile(frame_rms(pcm), NOISE_PERCENTILE))
        threshold = float(np.clip(nf_rms * THRESHOLD_MULT, THRESHOLD_MIN, THRESHOLD_MAX))
        log(f"       Noise flr : {dbfs(nf_rms):+.1f} dBFS  (rms {nf_rms:.5f})")
        log(f"       Threshold : {dbfs(threshold):+.1f} dBFS  (rms {threshold:.5f})")

        rms_frames = frame_rms(pcm)
        pct = 100.0 * float(np.mean(rms_frames >= threshold))
        hint = ""
        if pct < 5:
            hint = "  ←  very little signal detected; check levels"
        elif pct > 80:
            hint = "  ←  nearly always open; very noisy or hot track"
        log(f"       Gate open : {pct:.1f}%{hint}")

        envelope = build_gate_envelope(pcm, threshold)
        sp_rms   = speech_rms(pcm, envelope)
        log(f"       Speech RMS: {dbfs(sp_rms):+.1f} dBFS")

        track_data.append({
            "path":      path,
            "pcm":       pcm,
            "threshold": threshold,
            "envelope":  envelope,
            "sp_rms":    sp_rms,
        })

    # ── Level-match scale factors ─────────────────────────────────────────────
    if level_match and len(track_data) > 1:
        sp_rms_vals = [td["sp_rms"] for td in track_data]
        valid_vals  = [v for v in sp_rms_vals if v > 1e-6]
        if valid_vals:
            target = float(np.median(valid_vals))
            max_scale = 10.0 ** (LEVEL_MAX_BOOST_DB / 20.0)
            for td in track_data:
                if td["sp_rms"] > 1e-6:
                    raw_scale = target / td["sp_rms"]
                    td["scale"] = min(raw_scale, max_scale)
                else:
                    td["scale"] = 1.0   # no speech found; don't guess
        else:
            for td in track_data:
                td["scale"] = 1.0
    else:
        for td in track_data:
            td["scale"] = 1.0

    # ── Pass 2 — apply gate + scale, collect for mix ──────────────────────────
    log(f"\n{'─' * W}")
    if level_match and len(track_data) > 1:
        log("  Level matching:")

    gated_tracks = []

    for td in track_data:
        gated = (td["pcm"] * td["envelope"]).astype(np.float32)
        scale = td["scale"]

        if level_match and len(track_data) > 1:
            db_adj = 20.0 * np.log10(max(scale, 1e-9))
            capped = scale == 10.0 ** (LEVEL_MAX_BOOST_DB / 20.0)
            cap_note = "  (capped)" if capped else ""
            log(f"    {os.path.basename(td['path'])[:38]:<38}"
                f"  {db_adj:+.1f} dB{cap_note}")
            gated = (gated * scale).astype(np.float32)

        gated_tracks.append(gated)

        if debug:
            gp = os.path.splitext(td["path"])[0] + "_gated.wav"
            # Normalise individually so the debug file is audible
            pk = np.max(np.abs(gated))
            debug_pcm = (gated * (0.9 / pk)).astype(np.float32) if pk > 1e-9 else gated
            write_wav(debug_pcm, gp)
            log(f"    Debug: saved {os.path.basename(gp)}")

    # ── Mix ───────────────────────────────────────────────────────────────────
    log("")
    if len(gated_tracks) == 1:
        log("  Single track — normalising only.")
        final = gated_tracks[0]
        pk = np.max(np.abs(final))
        if pk > 1e-9:
            final = (final * MIX_PEAK_TARGET / pk).astype(np.float32)
    else:
        log(f"  Mixing {len(gated_tracks)} tracks …")
        final = mix_tracks(gated_tracks)

    write_wav(final, output_path)

    if dynorm:
        log("  Applying dynamic normalization …")
        apply_dynaudnorm(output_path)

    log(f"  Duration    : {len(final) / SR:.1f} s")
    log(f"  Peak out    : {dbfs(np.max(np.abs(final))):+.1f} dBFS")
    log(f"  Written     : {output_path}")
    log("─" * W)


# ── Colours ───────────────────────────────────────────────────────────────────

BG     = "#1c1c1e"
SURF   = "#2c2c2e"
SURF2  = "#3a3a3c"
FG     = "#f0f0f0"
SUB    = "#8e8e93"
ACCENT = "#0a84ff"
GREEN  = "#30d158"
MONO   = "Courier New"


# ── GUI ───────────────────────────────────────────────────────────────────────

class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Audio Mix Test")
        self.configure(bg=BG)
        self.geometry("640x700")
        self.minsize(480, 520)
        self.resizable(True, True)

        # Full paths behind the listbox display
        self._paths       = []
        self._out_var     = tk.StringVar()
        self._level_var   = tk.BooleanVar(value=True)
        self._dynorm_var  = tk.BooleanVar(value=True)
        self._debug_var   = tk.BooleanVar(value=False)
        self._last_out    = None   # path of last successful output

        self._build()

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build(self):
        pad = dict(padx=16)

        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=BG)
        hdr.pack(fill="x", pady=(14, 0), **pad)
        tk.Label(hdr, text="Input Files", bg=BG, fg=FG,
                 font=("Helvetica", 12, "bold")).pack(side="left")
        tk.Button(hdr, text="  Add Files  ", command=self._add_files,
                  bg=ACCENT, fg="white", relief="flat",
                  padx=6, pady=4, cursor="hand2").pack(side="right")

        # ── File list ─────────────────────────────────────────────────────────
        list_outer = tk.Frame(self, bg=SURF2, bd=1, relief="flat")
        list_outer.pack(fill="both", expand=True, pady=(6, 0), **pad)

        self._lb = tk.Listbox(
            list_outer, bg=SURF, fg=FG,
            selectbackground=ACCENT, selectforeground="white",
            borderwidth=0, highlightthickness=0,
            height=6, font=(MONO, 10), activestyle="none",
        )
        vsb = tk.Scrollbar(list_outer, orient="vertical",   command=self._lb.yview)
        hsb = tk.Scrollbar(list_outer, orient="horizontal", command=self._lb.xview)
        self._lb.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side="right",  fill="y")
        hsb.pack(side="bottom", fill="x")
        self._lb.pack(side="left", fill="both", expand=True, padx=2, pady=2)

        # ── List buttons ──────────────────────────────────────────────────────
        lb_btns = tk.Frame(self, bg=BG)
        lb_btns.pack(fill="x", pady=(5, 0), **pad)
        for label, cmd in [("Remove Selected", self._remove_sel),
                            ("Clear All",       self._clear_all)]:
            tk.Button(lb_btns, text=label, command=cmd,
                      bg=SURF2, fg=SUB, relief="flat",
                      padx=8, pady=2, cursor="hand2").pack(side="left", padx=(0, 4))

        # ── Output path ───────────────────────────────────────────────────────
        tk.Frame(self, bg=SURF2, height=1).pack(fill="x", pady=(12, 0), **pad)

        out_hdr = tk.Frame(self, bg=BG)
        out_hdr.pack(fill="x", pady=(10, 0), **pad)
        tk.Label(out_hdr, text="Output file", bg=BG, fg=SUB,
                 font=("Helvetica", 10)).pack(side="left")

        out_row = tk.Frame(self, bg=BG)
        out_row.pack(fill="x", pady=(4, 0), **pad)
        tk.Entry(out_row, textvariable=self._out_var,
                 bg=SURF, fg=FG, insertbackground=FG,
                 relief="flat", font=(MONO, 10)).pack(
            side="left", fill="x", expand=True, ipady=5)
        tk.Button(out_row, text=" Browse ", command=self._browse_output,
                  bg=SURF2, fg=SUB, relief="flat",
                  padx=6, pady=5, cursor="hand2").pack(side="right", padx=(5, 0))

        # ── Options ───────────────────────────────────────────────────────────
        for text, var, top_pad in [
            ("Level match tracks to equal speech loudness", self._level_var,  8),
            ("Dynamic normalization (even out volume within mix)", self._dynorm_var, 2),
            ("Save individual gated tracks  (*_gated.wav)",       self._debug_var,  2),
        ]:
            tk.Checkbutton(self, text=text, variable=var,
                           bg=BG, fg=SUB, selectcolor=SURF2,
                           activebackground=BG, activeforeground=FG,
                           font=("Helvetica", 10)).pack(
                anchor="w", pady=(top_pad, 0), **pad)

        # ── Run button ────────────────────────────────────────────────────────
        self._run_btn = tk.Button(self, text="Run Mix",
                                  command=self._run,
                                  bg=ACCENT, fg="white", relief="flat",
                                  font=("Helvetica", 12, "bold"),
                                  padx=24, pady=10, cursor="hand2")
        self._run_btn.pack(fill="x", pady=(12, 0), **pad)

        # ── Bottom buttons — packed BEFORE the expanding text area so
        #    tkinter reserves space for them first ──────────────────────────────
        bot = tk.Frame(self, bg=BG)
        bot.pack(side="bottom", fill="x", pady=(8, 14), **pad)

        self._open_btn = tk.Button(bot, text="▶  Open Output",
                                   command=self._open_output,
                                   bg=GREEN, fg="white", relief="flat",
                                   font=("Helvetica", 11, "bold"),
                                   padx=16, pady=8, cursor="hand2",
                                   state="disabled")
        self._open_btn.pack(side="left", fill="x", expand=True)

        self._transcribe_btn = tk.Button(bot, text="Transcribe Output",
                                         command=self._transcribe,
                                         bg=SURF2, fg=SUB, relief="flat",
                                         font=("Helvetica", 11, "bold"),
                                         padx=16, pady=8, cursor="hand2",
                                         state="disabled")
        self._transcribe_btn.pack(side="right", fill="x", expand=True,
                                  padx=(8, 0))

        # ── Diagnostics ───────────────────────────────────────────────────────
        tk.Label(self, text="Diagnostics", bg=BG, fg=SUB,
                 font=("Helvetica", 10)).pack(
            anchor="w", pady=(12, 0), **pad)

        txt_outer = tk.Frame(self, bg=SURF2)
        txt_outer.pack(fill="both", expand=True, pady=(4, 0), **pad)

        self._txt = tk.Text(
            txt_outer, bg=SURF, fg=FG,
            font=(MONO, 10), relief="flat",
            borderwidth=0, highlightthickness=0,
            state="disabled", wrap="none",
            padx=8, pady=6,
        )
        txt_vsb = tk.Scrollbar(txt_outer, orient="vertical",   command=self._txt.yview)
        txt_hsb = tk.Scrollbar(txt_outer, orient="horizontal", command=self._txt.xview)
        self._txt.configure(yscrollcommand=txt_vsb.set, xscrollcommand=txt_hsb.set)
        txt_vsb.pack(side="right",  fill="y")
        txt_hsb.pack(side="bottom", fill="x")
        self._txt.pack(side="left", fill="both", expand=True)

    # ── File management ───────────────────────────────────────────────────────

    def _add_files(self):
        chosen = filedialog.askopenfilenames(
            title="Select audio files",
            filetypes=[
                ("Audio files", "*.wav *.mp3 *.m4a *.aiff *.aif *.flac *.ogg"),
                ("All files",   "*.*"),
            ],
        )
        for p in chosen:
            if p not in self._paths:
                self._paths.append(p)
                self._lb.insert("end", os.path.basename(p))
        self._auto_set_output()

    def _remove_sel(self):
        for i in reversed(self._lb.curselection()):
            self._lb.delete(i)
            del self._paths[i]

    def _clear_all(self):
        self._lb.delete(0, "end")
        self._paths.clear()

    def _browse_output(self):
        p = filedialog.asksaveasfilename(
            title="Save output as",
            defaultextension=".wav",
            filetypes=[("WAV file", "*.wav")],
            initialfile="mixed_gated.wav",
        )
        if p:
            self._out_var.set(p)

    def _auto_set_output(self):
        """Set a sensible default output path when files are first added."""
        if not self._paths:
            return
        cur = self._out_var.get()
        if not cur:
            folder = os.path.dirname(self._paths[0])
            self._out_var.set(os.path.join(folder, "mixed_gated.wav"))

    # ── Diagnostics text ──────────────────────────────────────────────────────

    def _log(self, text):
        """Append a line to the diagnostics pane (safe to call from any thread)."""
        def _do():
            self._txt.configure(state="normal")
            self._txt.insert("end", text + "\n")
            self._txt.see("end")
            self._txt.configure(state="disabled")
        self.after(0, _do)

    def _clear_log(self):
        self._txt.configure(state="normal")
        self._txt.delete("1.0", "end")
        self._txt.configure(state="disabled")

    # ── Run ───────────────────────────────────────────────────────────────────

    def _run(self):
        if not self._paths:
            self._log("No input files. Use Add Files to load audio.")
            return
        output = self._out_var.get().strip()
        if not output:
            self._log("No output path specified.")
            return
        missing = [p for p in self._paths if not os.path.exists(p)]
        if missing:
            for p in missing:
                self._log(f"ERROR: not found: {p}")
            return

        self._clear_log()
        self._run_btn.configure(state="disabled", text="Running…")
        self._open_btn.configure(state="disabled")

        paths  = list(self._paths)
        debug  = self._debug_var.get()
        level  = self._level_var.get()
        dynorm = self._dynorm_var.get()

        def _worker():
            try:
                process(paths, output, debug, level, dynorm, self._log)
                self._last_out = output
                self.after(0, self._on_done, True)
            except Exception as exc:
                self._log(f"\nERROR: {exc}")
                self.after(0, self._on_done, False)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_done(self, success):
        self._run_btn.configure(state="normal", text="Run Mix")
        if success and self._last_out and os.path.exists(self._last_out):
            self._open_btn.configure(state="normal")
            self._transcribe_btn.configure(state="normal", bg=ACCENT, fg="white")

    def _open_output(self):
        if self._last_out and os.path.exists(self._last_out):
            os.startfile(self._last_out)

    # ── Transcribe ────────────────────────────────────────────────────────────

    def _transcribe(self):
        if not self._last_out or not os.path.exists(self._last_out):
            return

        self._transcribe_btn.configure(state="disabled", text="Transcribing…")
        path = self._last_out

        def _worker():
            try:
                from engines import transcribe_clip
                self._log("\n" + "─" * 54)
                self._log("  Transcribing … (model load may take a moment)")

                words = transcribe_clip(path)

                if not words:
                    self._log("  No speech detected.")
                else:
                    self._log(f"  {len(words)} words found\n")

                    # Group words into utterances separated by gaps > 0.8 s
                    utterances = []
                    current    = []
                    for w in words:
                        if current and w["start"] - current[-1]["end"] > 0.8:
                            utterances.append(current)
                            current = []
                        current.append(w)
                    if current:
                        utterances.append(current)

                    for utt in utterances:
                        ts   = utt[0]["start"]
                        mins = int(ts) // 60
                        secs = ts % 60
                        text = " ".join(w["word"] for w in utt)
                        self._log(f"  [{mins}:{secs:05.2f}]  {text}")

                self._log("─" * 54)
                self.after(0, self._on_transcribe_done)

            except Exception as exc:
                self._log(f"\nERROR during transcription: {exc}")
                self.after(0, self._on_transcribe_done)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_transcribe_done(self):
        self._transcribe_btn.configure(state="normal", text="Transcribe Output")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    App().mainloop()
