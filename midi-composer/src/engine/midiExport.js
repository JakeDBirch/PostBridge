// Minimal MIDI file writer (Format 1 - multi-track)

function writeUint16(arr, val) {
  arr.push((val >> 8) & 0xFF, val & 0xFF);
}

function writeUint32(arr, val) {
  arr.push((val >> 24) & 0xFF, (val >> 16) & 0xFF, (val >> 8) & 0xFF, val & 0xFF);
}

function writeVariableLength(arr, val) {
  const bytes = [];
  bytes.push(val & 0x7F);
  let v = val >> 7;
  while (v > 0) {
    bytes.push((v & 0x7F) | 0x80);
    v >>= 7;
  }
  bytes.reverse();
  arr.push(...bytes);
}

function buildTrackChunk(events) {
  const data = [];

  // Sort events by absolute time
  const sorted = [...events].sort((a, b) => a.absTime - b.absTime);

  let prevTime = 0;
  for (const event of sorted) {
    const delta = Math.max(0, Math.round(event.absTime - prevTime));
    writeVariableLength(data, delta);

    if (event.type === 'noteOn') {
      data.push(0x90 | (event.channel & 0x0F));
      data.push(event.note & 0x7F);
      data.push(event.velocity & 0x7F);
    } else if (event.type === 'noteOff') {
      data.push(0x80 | (event.channel & 0x0F));
      data.push(event.note & 0x7F);
      data.push(0);
    } else if (event.type === 'tempo') {
      data.push(0xFF, 0x51, 0x03);
      const usPerBeat = Math.round(60000000 / event.tempo);
      data.push((usPerBeat >> 16) & 0xFF, (usPerBeat >> 8) & 0xFF, usPerBeat & 0xFF);
    } else if (event.type === 'trackName') {
      data.push(0xFF, 0x03);
      const nameBytes = Array.from(new TextEncoder().encode(event.name));
      writeVariableLength(data, nameBytes.length);
      data.push(...nameBytes);
    } else if (event.type === 'programChange') {
      data.push(0xC0 | (event.channel & 0x0F));
      data.push(event.program & 0x7F);
    }

    prevTime = event.absTime;
  }

  // End of track
  writeVariableLength(data, 0);
  data.push(0xFF, 0x2F, 0x00);

  // Build chunk
  const chunk = [];
  // MTrk
  chunk.push(0x4D, 0x54, 0x72, 0x6B);
  writeUint32(chunk, data.length);
  chunk.push(...data);

  return chunk;
}

// GM instrument map for quick lookup
const ROLE_INSTRUMENTS = {
  lead: 0,       // Acoustic Grand Piano
  melody: 0,     // Acoustic Grand Piano
  bass: 33,      // Electric Bass (finger)
  harmony: 48,   // String Ensemble 1
  chords: 4,     // Electric Piano 1
  arpeggio: 46,  // Orchestral Harp
  counter: 11,   // Vibraphone
  pad: 89,       // Pad 2 (warm)
};

export function exportMidi(tracks, tempo = 120) {
  const ticksPerBeat = 480;
  const allChunks = [];

  // MThd header
  allChunks.push(0x4D, 0x54, 0x68, 0x64); // "MThd"
  writeUint32(allChunks, 6); // Header length
  writeUint16(allChunks, 1); // Format 1
  writeUint16(allChunks, tracks.length + 1); // Number of tracks (+1 for tempo track)
  writeUint16(allChunks, ticksPerBeat);

  // Tempo track (track 0)
  const tempoEvents = [
    { absTime: 0, type: 'tempo', tempo },
    { absTime: 0, type: 'trackName', name: 'Tempo Track' },
  ];
  allChunks.push(...buildTrackChunk(tempoEvents));

  // Instrument tracks
  tracks.forEach((track, idx) => {
    const events = [];
    const channel = idx < 9 ? idx : idx + 1; // Skip channel 10 (drums)

    // Track name
    events.push({ absTime: 0, type: 'trackName', name: track.name });

    // Program change
    const program = ROLE_INSTRUMENTS[track.role] ?? 0;
    events.push({ absTime: 0, type: 'programChange', channel, program });

    // Note events
    for (const note of track.notes) {
      const startTick = Math.round(note.startTime * ticksPerBeat);
      const endTick = Math.round((note.startTime + note.duration) * ticksPerBeat);

      events.push({
        absTime: startTick,
        type: 'noteOn',
        channel,
        note: note.pitch,
        velocity: note.velocity,
      });
      events.push({
        absTime: endTick,
        type: 'noteOff',
        channel,
        note: note.pitch,
        velocity: 0,
      });
    }

    allChunks.push(...buildTrackChunk(events));
  });

  return new Uint8Array(allChunks);
}

export function downloadMidi(midiData, filename = 'composition.mid') {
  const blob = new Blob([midiData], { type: 'audio/midi' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// Export a single track as MIDI
export function exportSingleTrackMidi(track, tempo = 120) {
  return exportMidi([track], tempo);
}
