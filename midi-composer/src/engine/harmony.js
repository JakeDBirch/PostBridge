import { SCALES, getChordTones } from './scales.js';

// Common chord progressions by mood category
const PROGRESSIONS = {
  major: [
    [0, 3, 4, 0],       // I-IV-V-I
    [0, 4, 5, 3],       // I-V-vi-IV
    [0, 5, 3, 4],       // I-vi-IV-V
    [0, 3, 0, 4],       // I-IV-I-V
    [0, 2, 3, 4],       // I-iii-IV-V
    [0, 3, 5, 4],       // I-IV-vi-V
  ],
  minor: [
    [0, 5, 3, 4],       // i-VI-iv-V
    [0, 3, 6, 4],       // i-iv-VII-V
    [0, 6, 5, 4],       // i-VII-VI-V
    [0, 3, 5, 6],       // i-iv-VI-VII
    [0, 5, 2, 4],       // i-VI-III-V
    [0, 2, 5, 6],       // i-III-VI-VII
  ],
  dark: [
    [0, 1, 4, 0],       // i-bII-V-i (phrygian flavor)
    [0, 5, 1, 4],       // i-VI-bII-V
    [0, 3, 1, 0],       // i-iv-bII-i
    [0, 6, 5, 0],       // i-VII-VI-i
  ],
  dreamy: [
    [0, 3, 1, 4],       // I-IV-II-V
    [0, 2, 3, 5],       // I-iii-IV-vi
    [0, 5, 1, 3],       // I-vi-ii-IV
  ],
  blues: [
    [0, 0, 3, 3],       // I-I-IV-IV
    [0, 3, 0, 4],       // I-IV-I-V
    [0, 3, 4, 0],       // I-IV-V-I
  ],
  epic: [
    [0, 5, 3, 6],       // i-VI-iv-VII
    [0, 6, 5, 0],       // i-VII-VI-i
    [0, 3, 6, 5],       // i-iv-VII-VI
    [0, 5, 6, 3],       // i-VI-VII-iv
  ],
};

// Map scale names to progression categories
const SCALE_TO_CATEGORY = {
  major: 'major',
  natural_minor: 'minor',
  harmonic_minor: 'minor',
  melodic_minor: 'minor',
  dorian: 'minor',
  phrygian: 'dark',
  lydian: 'dreamy',
  mixolydian: 'major',
  aeolian: 'minor',
  whole_tone: 'dreamy',
  pentatonic_major: 'major',
  pentatonic_minor: 'minor',
  blues: 'blues',
  chromatic: 'dark',
};

export function generateProgression(scaleName, durationBars, seed = Math.random()) {
  const category = SCALE_TO_CATEGORY[scaleName] || 'major';
  const progs = PROGRESSIONS[category] || PROGRESSIONS.major;
  const progIndex = Math.floor(seed * progs.length) % progs.length;
  const baseProgression = progs[progIndex];

  // Extend or trim to fill the bars
  const progression = [];
  const barsPerChord = Math.max(1, Math.floor(durationBars / baseProgression.length));
  for (let bar = 0; bar < durationBars; bar++) {
    const chordIndex = Math.floor(bar / barsPerChord) % baseProgression.length;
    progression.push(baseProgression[chordIndex]);
  }

  return progression;
}

export function getChordNotesForBar(root, scaleName, degree, registerRange) {
  const intervals = SCALES[scaleName] || SCALES.major;
  const len = intervals.length;
  const chordTones = [];

  // Build a triad + optional 7th
  for (let i = 0; i < 4; i++) {
    const idx = (degree + i * 2) % len;
    const octaveOffset = Math.floor((degree + i * 2) / len) * 12;
    let note = root + intervals[idx] + octaveOffset;

    // Fit into register range
    while (note < registerRange.low) note += 12;
    while (note > registerRange.high) note -= 12;

    if (note >= registerRange.low && note <= registerRange.high) {
      chordTones.push(note);
    }
  }

  return [...new Set(chordTones)].sort((a, b) => a - b);
}

// Get the root note for a chord degree in the scale
export function getChordRoot(root, scaleName, degree) {
  const intervals = SCALES[scaleName] || SCALES.major;
  return root + intervals[degree % intervals.length];
}
