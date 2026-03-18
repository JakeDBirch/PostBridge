// Scale intervals (semitones from root)
export const SCALES = {
  major:            [0, 2, 4, 5, 7, 9, 11],
  natural_minor:    [0, 2, 3, 5, 7, 8, 10],
  harmonic_minor:   [0, 2, 3, 5, 7, 8, 11],
  melodic_minor:    [0, 2, 3, 5, 7, 9, 11],
  dorian:           [0, 2, 3, 5, 7, 9, 10],
  phrygian:         [0, 1, 3, 5, 7, 8, 10],
  lydian:           [0, 2, 4, 6, 7, 9, 11],
  mixolydian:       [0, 2, 4, 5, 7, 9, 10],
  aeolian:          [0, 2, 3, 5, 7, 8, 10],
  whole_tone:       [0, 2, 4, 6, 8, 10],
  pentatonic_major: [0, 2, 4, 7, 9],
  pentatonic_minor: [0, 3, 5, 7, 10],
  blues:            [0, 3, 5, 6, 7, 10],
  chromatic:        [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
};

export const NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];

// Get all MIDI notes in a scale within a range
export function getScaleNotes(root, scaleName, lowNote = 36, highNote = 84) {
  const intervals = SCALES[scaleName] || SCALES.major;
  const notes = [];
  for (let octaveStart = 0; octaveStart <= 120; octaveStart += 12) {
    for (const interval of intervals) {
      const note = root + octaveStart + interval;
      if (note >= lowNote && note <= highNote) {
        notes.push(note);
      }
    }
  }
  return notes.sort((a, b) => a - b);
}

// Get chord tones for a given degree in the scale
export function getChordTones(root, scaleName, degree, extensions = 3) {
  const intervals = SCALES[scaleName] || SCALES.major;
  const len = intervals.length;
  const tones = [];
  for (let i = 0; i < extensions; i++) {
    const idx = (degree + i * 2) % len;
    const octaveOffset = Math.floor((degree + i * 2) / len) * 12;
    tones.push(root + intervals[idx] + octaveOffset);
  }
  return tones;
}

// Get note name from MIDI number
export function midiToNoteName(midi) {
  const octave = Math.floor(midi / 12) - 1;
  return NOTE_NAMES[midi % 12] + octave;
}

// Get MIDI number from note name
export function noteNameToMidi(name) {
  const match = name.match(/^([A-G]#?)(-?\d+)$/);
  if (!match) return 60;
  const noteIndex = NOTE_NAMES.indexOf(match[1]);
  const octave = parseInt(match[2]);
  return (octave + 1) * 12 + noteIndex;
}
