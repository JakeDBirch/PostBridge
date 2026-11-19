"""Live playback engine for the waveform editor.

The old path rendered a WAV and handed it to winsound.  That gives no
control at all once a sound is playing: moving an edit point or changing
the speed could only take effect by stopping and starting again, which
also threw away your place in the clip.

This is a callback-driven output stream instead.  Every buffer the device
asks for is generated on the spot from three pieces of state -- the kept
segments, the speed, and the position -- all of which can be changed from
the UI thread at any moment.  So dragging a cut is heard on the next
buffer (~45 ms), and so is a speed change, with no gap and no restart.

Three things fall out of doing it this way:

  * the playhead is read from the engine's own cursor rather than
    extrapolated from a wall clock, so it stays correct across speed
    changes and cut jumps by construction -- and it is the moment being
    HEARD: the engine renders a buffer some 200 ms before the device
    plays it, so position() goes by when each buffer reaches the
    speaker (see _heard), not by how far rendering has got;
  * speed is a real-time time-stretch (WSOLA, pitch preserved) rather
    than an ffmpeg pass over a temp file;
  * playback stops being Windows-only -- PortAudio covers macOS and
    Linux, which winsound never did.

Requires sounddevice (PortAudio).  If it is missing the class still
imports and reports unavailable, so callers can fall back.
"""
import collections
import threading

import numpy as np

try:
    import sounddevice as _sd
except Exception:                      # pragma: no cover - optional dep
    _sd = None


def available():
    """True when a real output stream can be opened."""
    return _sd is not None


# ── WSOLA ─────────────────────────────────────────────────────────────────────
# Waveform Similarity Overlap-Add.  Plain overlap-add at a non-unity hop
# ratio makes speech warble, because successive windows land at arbitrary
# phases of the pitch period.  WSOLA fixes that by nudging each window,
# within a small search range, to wherever it best matches the tail of what
# was already emitted -- so the pitch periods line up and the overlap adds
# constructively.  This is the same idea ffmpeg's atempo uses; the point of
# reimplementing it is that it runs incrementally, inside the audio
# callback, at whatever rate the knob happens to be on right now.

_FRAME    = 1024        # analysis / synthesis window, ~23 ms at 44.1 kHz
_HOP_SYN  = _FRAME // 2   # synthesis hop — 50% overlap with a Hann window
_SEARCH   = 256         # ± samples the similarity search may shift a window


class _Wsola(object):
    def __init__(self, frame=_FRAME, hop_syn=_HOP_SYN, search=_SEARCH):
        self.frame   = frame
        self.hop_syn = hop_syn
        self.overlap = frame - hop_syn
        self.search  = search
        self.window  = np.hanning(frame).astype(np.float32)
        # Second half of the previous windowed frame, still waiting for its
        # partner in the overlap-add.
        self._tail = np.zeros(self.overlap, dtype=np.float32)
        # What the source would have played next had we just kept reading.
        # The search looks for whichever candidate window best continues it.
        self._template = None

    def reset(self):
        self._tail = np.zeros(self.overlap, dtype=np.float32)
        self._template = None

    def _best_offset(self, reader, pos):
        """Shift, within +/- search, whose audio best continues what was
        already emitted.  0 on a fresh start, when there is nothing to
        continue from."""
        if self.search <= 0 or self._template is None:
            return 0
        n    = self.overlap
        span = 2 * self.search + n
        cand = reader(pos - self.search, span)
        if cand is None or len(cand) < span:
            return 0
        # NORMALISED cross-correlation.  Raw correlation just picks whichever
        # candidate is loudest, which is not the same question at all.
        corr = np.correlate(cand, self._template, mode="valid")
        # Sliding energy of each candidate window, via a cumulative sum so
        # this stays one pass rather than one dot product per shift.
        c2 = np.concatenate(([0.0], np.cumsum(cand.astype(np.float64) ** 2)))
        energy = c2[n:] - c2[:-n]
        denom = np.sqrt(np.maximum(energy[:len(corr)], 1e-12))
        best = int(np.argmax(corr / denom))
        return best - self.search

    def synth(self, reader, pos):
        """Emit one synthesis hop from the analysis grid position `pos`.

        The shift the similarity search picks is applied ONLY to this read.
        It must not be folded back into the caller's position: the grid
        advances by the analysis hop (= synthesis hop x rate), and that grid
        IS the speed change.  Let the shift accumulate and the search simply
        walks the position back to wherever the audio best continues -- which
        is one synthesis hop along, i.e. rate 1.0 -- and the stretch quietly
        cancels itself out.
        """
        off = self._best_offset(reader, pos)
        frame = reader(pos + off, self.frame)
        if frame is None or len(frame) < self.frame:
            return None
        w = frame * self.window
        out = self._tail + w[:self.hop_syn]
        self._tail = w[self.hop_syn:].copy()
        # Template for the next search: how THIS frame would have continued.
        nxt = reader(pos + off + self.hop_syn, self.overlap)
        self._template = (nxt.copy()
                          if nxt is not None and len(nxt) == self.overlap
                          else None)
        return out


# ── The player ────────────────────────────────────────────────────────────────

class LivePlayer(object):
    """Streams from an in-memory mono buffer, honouring edits and speed live.

    All the public setters are safe to call from the UI thread while the
    stream is running; the callback takes a consistent snapshot each buffer.
    """

    def __init__(self, samplerate=44100, blocksize=2048, gain=0.85):
        self.sr        = int(samplerate)
        self.blocksize = int(blocksize)
        self.gain      = float(gain)

        self._lock   = threading.RLock()
        self._src    = np.zeros(0, dtype=np.float32)
        self._src_t0 = 0.0        # absolute source time of _src[0], seconds
        self._segments = None     # [(in_s, out_s)] absolute, or None
        self._rate     = 1.0
        self._pos      = 0.0      # absolute source time, seconds
        self._end      = None     # absolute source time to stop at
        self._playing  = False
        self._finished = False
        self._stream   = None
        self._wsola    = _Wsola()
        self._pending  = np.zeros(0, dtype=np.float32)   # spill-over samples
        # What has been handed to the device and when it sounds: one
        # (dac_time, seconds, pos_before, pos_after, rate) per buffer, on
        # the stream's clock.  _anchor is the position to report until
        # the first of them is audible.
        self._marks    = collections.deque(maxlen=256)
        self._anchor   = 0.0

    # ── Source ────────────────────────────────────────────────────────────
    def load(self, pcm, t0=0.0):
        """Install the source buffer. `pcm` is mono float32 at self.sr, and
        `t0` is the absolute source time of its first sample."""
        with self._lock:
            self._src    = np.ascontiguousarray(pcm, dtype=np.float32)
            self._src_t0 = float(t0)

    def load_source(self, source, t0=0.0):
        """Install a source that composes its audio on demand instead of a
        flat buffer -- the Assembly view's whole cut, which is too long to
        hold as float32.  `source` needs len() in samples at self.sr and
        read(start_idx, n) -> float32 array of n samples (or None)."""
        with self._lock:
            self._src    = source
            self._src_t0 = float(t0)

    def covers(self, start_s, end_s):
        """True when the loaded buffer spans [start_s, end_s]."""
        with self._lock:
            if not len(self._src):
                return False
            lo = self._src_t0
            hi = self._src_t0 + len(self._src) / float(self.sr)
            return lo <= start_s and end_s <= hi

    # ── Live state ────────────────────────────────────────────────────────
    def set_segments(self, segments):
        """Kept regions as [(in_s, out_s)] in absolute source time, or None
        to play straight through.  Takes effect on the next buffer."""
        with self._lock:
            self._segments = ([tuple(map(float, s)) for s in segments]
                              if segments else None)

    def set_rate(self, rate):
        with self._lock:
            self._rate = max(0.25, min(4.0, float(rate)))

    def seek(self, t):
        with self._lock:
            self._pos = float(t)
            self._pending = np.zeros(0, dtype=np.float32)
            self._wsola.reset()
            self._marks.clear()
            self._anchor = self._pos

    def position(self):
        """Where the playhead is, in source seconds: while playing, the
        moment coming out of the speaker now; stopped, where it stopped."""
        with self._lock:
            if self._playing and not self._finished:
                return self._heard()
            return self._pos

    def _stream_now(self):
        try:
            return float(self._stream.time)
        except Exception:
            return None

    def _heard(self):
        """The source time being heard now (lock held).  Rendering runs
        ahead of the speaker by the device's output latency -- about
        0.2 s on Windows' default driver, half as much again in source
        time at 1.5x -- so the engine's cursor is not it."""
        now = self._stream_now() if self._stream is not None else None
        if now is None or not self._marks:
            return self._anchor if self._marks or now is not None else self._pos
        heard = self._anchor
        for dac, dur, p0, p1, rate in self._marks:
            if now < dac:
                break
            lo, hi = min(p0, p1), max(p0, p1)
            heard = min(hi, max(lo, p0 + (now - dac) * rate))
        return heard

    def is_playing(self):
        with self._lock:
            return self._playing and not self._finished

    # ── Transport ─────────────────────────────────────────────────────────
    def play(self, start_s=None, end_s=None):
        if _sd is None:
            return False
        with self._lock:
            if start_s is not None:
                self._pos = float(start_s)
            self._end      = None if end_s is None else float(end_s)
            self._finished = False
            self._pending  = np.zeros(0, dtype=np.float32)
            self._wsola.reset()
            self._pos = self._snap_into_segment(self._pos)
            self._marks.clear()
            self._anchor = self._pos
            self._playing = True
        try:
            if self._stream is None:
                self._stream = _sd.OutputStream(
                    samplerate=self.sr, channels=1, dtype="float32",
                    blocksize=self.blocksize, callback=self._callback)
            if not self._stream.active:
                self._stream.start()
        except Exception:
            with self._lock:
                self._playing = False
            return False
        return True

    def stop(self):
        """Stop.  Stopped by hand, the sound ends now and the position is
        the moment that was being heard -- not the end of what was already
        queued for the device, which would otherwise play on for a fifth
        of a second and leave the playhead past the spot.  Run out to its
        end, the queued tail is played."""
        with self._lock:
            cut = self._playing and not self._finished
            if cut:
                self._pos = self._heard()
                self._pending = np.zeros(0, dtype=np.float32)
                self._wsola.reset()
            self._playing = False
            self._marks.clear()
            self._anchor = self._pos
        st = self._stream
        if st is not None:
            try:
                st.abort() if cut else st.stop()
            except Exception:
                pass

    def close(self):
        self.stop()
        st, self._stream = self._stream, None
        if st is not None:
            try:
                st.close()
            except Exception:
                pass

    # ── Internals ─────────────────────────────────────────────────────────
    def _snap_into_segment(self, t):
        """Move `t` forward to the first kept segment at or after it."""
        segs = self._segments
        if not segs:
            return t
        for a, b in segs:
            if t < b:
                return max(t, a)
        return segs[-1][1]        # past the end

    def _reader(self, start_idx, n):
        """Read n source samples starting at float sample index `start_idx`,
        relative to the source buffer.  Returns None when out of range."""
        i = int(round(start_idx))
        if i < 0 or n <= 0:
            return None
        src = self._src
        if i + n > len(src):
            return None
        if not isinstance(src, np.ndarray):
            return src.read(i, n)
        return src[i:i + n]

    def _render(self, frames):
        """Produce `frames` output samples.  Public for offline testing —
        the callback is a thin wrapper around this."""
        with self._lock:
            if not self._playing or self._finished:
                return np.zeros(frames, dtype=np.float32)
            sr    = float(self.sr)
            src   = self._src
            t0    = self._src_t0
            segs  = self._segments
            rate  = self._rate
            end   = self._end
            gain  = self.gain

            out = np.zeros(frames, dtype=np.float32)
            filled = 0
            # Anything left over from the previous buffer goes out first.
            if len(self._pending):
                take = min(frames, len(self._pending))
                out[:take] = self._pending[:take]
                self._pending = self._pending[take:]
                filled = take

            hop = self._wsola.hop_syn
            guard = 0
            while filled < frames and guard < 512:
                guard += 1
                # Where are we, and is that inside a kept segment?
                pos = self._pos
                if segs:
                    seg = next(((a, b) for a, b in segs if a <= pos < b), None)
                    if seg is None:
                        nxt = self._snap_into_segment(pos)
                        if nxt <= pos:          # nothing left
                            self._finished = True
                            break
                        self._pos = pos = nxt
                        self._wsola.reset()
                if end is not None and pos >= end:
                    self._finished = True
                    break

                idx = (pos - t0) * sr
                if idx < self._wsola.search or \
                        idx + self._wsola.frame + self._wsola.search >= len(src):
                    self._finished = True
                    break

                chunk = self._wsola.synth(self._reader, idx)
                if chunk is None:
                    self._finished = True
                    break
                # The analysis grid advances by rate x the synthesis hop.
                # That ratio is the entire speed change, and reading it back
                # out of _pos is what makes the playhead correct for free.
                self._pos = pos + (hop * rate) / sr

                take = min(frames - filled, len(chunk))
                out[filled:filled + take] = chunk[:take]
                filled += take
                if take < len(chunk):
                    self._pending = chunk[take:].copy()

            if self._finished:
                self._playing = False
            return out * gain

    def _callback(self, outdata, frames, time_info, status):
        try:
            with self._lock:
                p0, rate, live = self._pos, self._rate, self._playing
                outdata[:, 0] = self._render(frames)
                if live:
                    self._mark(time_info, frames, p0, self._pos, rate)
        except Exception:
            outdata[:] = 0

    def _mark(self, time_info, frames, p0, p1, rate):
        """Note when the buffer just rendered will sound (lock held)."""
        dac = float(getattr(time_info, "outputBufferDacTime", 0.0) or 0.0)
        if dac <= 0.0:
            # A host that does not say: now, plus the stream's latency.
            now = float(getattr(time_info, "currentTime", 0.0) or 0.0)                 or self._stream_now() or 0.0
            try:
                dac = now + float(self._stream.latency)
            except Exception:
                dac = now
        self._marks.append((dac, frames / float(self.sr), p0, p1, rate))
