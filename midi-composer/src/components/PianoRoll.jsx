import React, { useRef, useEffect } from 'react';

const NOTE_HEIGHT = 4;
const BEAT_WIDTH = 40;
const PIANO_KEY_WIDTH = 40;

const TRACK_COLORS = [
  '#6366f1', // indigo
  '#f59e0b', // amber
  '#10b981', // emerald
  '#ef4444', // red
  '#8b5cf6', // violet
  '#06b6d4', // cyan
  '#f97316', // orange
  '#ec4899', // pink
];

export default function PianoRoll({ tracks, playbackBeat = -1 }) {
  const canvasRef = useRef(null);
  const containerRef = useRef(null);

  // Get visible tracks
  const visibleTracks = tracks.filter(t => !t.muted);

  // Find note range across all visible tracks
  let minPitch = 127, maxPitch = 0, maxTime = 0;
  for (const track of visibleTracks) {
    for (const note of track.notes) {
      minPitch = Math.min(minPitch, note.pitch);
      maxPitch = Math.max(maxPitch, note.pitch);
      maxTime = Math.max(maxTime, note.startTime + note.duration);
    }
  }
  if (minPitch > maxPitch) { minPitch = 48; maxPitch = 72; }
  minPitch = Math.max(0, minPitch - 3);
  maxPitch = Math.min(127, maxPitch + 3);

  const pitchRange = maxPitch - minPitch + 1;
  const totalBeats = Math.ceil(maxTime) + 1;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const width = PIANO_KEY_WIDTH + totalBeats * BEAT_WIDTH;
    const height = pitchRange * NOTE_HEIGHT;
    const dpr = window.devicePixelRatio || 1;

    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = width + 'px';
    canvas.style.height = height + 'px';

    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, width, height);

    // Background
    ctx.fillStyle = '#1a1a2e';
    ctx.fillRect(0, 0, width, height);

    // Piano keys
    const noteNames = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
    for (let p = minPitch; p <= maxPitch; p++) {
      const y = (maxPitch - p) * NOTE_HEIGHT;
      const isBlack = [1, 3, 6, 8, 10].includes(p % 12);
      const isC = p % 12 === 0;

      // Key background
      ctx.fillStyle = isBlack ? '#1e1e3a' : '#252547';
      ctx.fillRect(PIANO_KEY_WIDTH, y, width - PIANO_KEY_WIDTH, NOTE_HEIGHT);

      // Piano key
      ctx.fillStyle = isBlack ? '#333' : '#eee';
      ctx.fillRect(0, y, PIANO_KEY_WIDTH - 2, NOTE_HEIGHT);

      // C marker
      if (isC) {
        ctx.fillStyle = '#666';
        ctx.font = '8px Inter, sans-serif';
        ctx.fillText(`C${Math.floor(p / 12) - 1}`, 2, y + NOTE_HEIGHT - 1);
      }

      // Grid line
      ctx.strokeStyle = isC ? '#444' : '#1e1e3a';
      ctx.lineWidth = isC ? 1 : 0.5;
      ctx.beginPath();
      ctx.moveTo(PIANO_KEY_WIDTH, y);
      ctx.lineTo(width, y);
      ctx.stroke();
    }

    // Beat lines
    for (let beat = 0; beat <= totalBeats; beat++) {
      const x = PIANO_KEY_WIDTH + beat * BEAT_WIDTH;
      const isBar = beat % 4 === 0;
      ctx.strokeStyle = isBar ? '#555' : '#2a2a4a';
      ctx.lineWidth = isBar ? 1 : 0.5;
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, height);
      ctx.stroke();

      if (isBar) {
        ctx.fillStyle = '#777';
        ctx.font = '9px Inter, sans-serif';
        ctx.fillText(`${Math.floor(beat / 4) + 1}`, x + 2, 10);
      }
    }

    // Draw notes for each track
    visibleTracks.forEach((track, trackIdx) => {
      const color = TRACK_COLORS[trackIdx % TRACK_COLORS.length];

      for (const note of track.notes) {
        const x = PIANO_KEY_WIDTH + note.startTime * BEAT_WIDTH;
        const y = (maxPitch - note.pitch) * NOTE_HEIGHT;
        const w = note.duration * BEAT_WIDTH;

        // Note body
        const alpha = Math.round((note.velocity / 127) * 200 + 55).toString(16).padStart(2, '0');
        ctx.fillStyle = color + alpha;
        ctx.fillRect(x, y, Math.max(w - 1, 2), NOTE_HEIGHT - 1);

        // Note border
        ctx.strokeStyle = color;
        ctx.lineWidth = 0.5;
        ctx.strokeRect(x, y, Math.max(w - 1, 2), NOTE_HEIGHT - 1);
      }
    });

    // Playback cursor
    if (playbackBeat >= 0) {
      const x = PIANO_KEY_WIDTH + playbackBeat * BEAT_WIDTH;
      ctx.strokeStyle = '#fff';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, height);
      ctx.stroke();
    }
  }, [tracks, playbackBeat, minPitch, maxPitch, totalBeats, pitchRange, visibleTracks]);

  // Auto-scroll to follow playback
  useEffect(() => {
    if (playbackBeat >= 0 && containerRef.current) {
      const scrollX = PIANO_KEY_WIDTH + playbackBeat * BEAT_WIDTH - containerRef.current.clientWidth / 2;
      containerRef.current.scrollLeft = Math.max(0, scrollX);
    }
  }, [playbackBeat]);

  if (visibleTracks.length === 0) {
    return (
      <div className="piano-roll-empty">
        <p>Generate a part to see the piano roll</p>
      </div>
    );
  }

  return (
    <div className="piano-roll-container" ref={containerRef}>
      <canvas ref={canvasRef} />
    </div>
  );
}
