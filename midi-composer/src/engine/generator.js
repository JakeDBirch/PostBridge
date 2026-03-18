import { getScaleNotes } from './scales.js';
import { analyzeText } from './textAnalyzer.js';
import { generateProgression, getChordNotesForBar, getChordRoot } from './harmony.js';

// Seeded random for reproducibility
function seededRandom(seed) {
  let s = seed;
  return () => {
    s = (s * 16807 + 0) % 2147483647;
    return (s - 1) / 2147483646;
  };
}

function clamp(val, min, max) {
  return Math.min(max, Math.max(min, val));
}

// Apply a curve to a value based on position (0-1) through the piece
function applyCurve(baseValue, position, curve, amount = 0.5) {
  switch (curve) {
    case 'crescendo':
      return baseValue + (amount * baseValue * position);
    case 'decrescendo':
      return baseValue + (amount * baseValue * (1 - position));
    case 'swell':
      return baseValue + (amount * baseValue * Math.sin(position * Math.PI));
    case 'pulse':
      return baseValue + (amount * baseValue * Math.sin(position * Math.PI * 4));
    case 'wave':
      return baseValue + (amount * baseValue * Math.sin(position * Math.PI * 2));
    case 'increasing':
      return baseValue * (0.5 + position * 0.8);
    case 'decreasing':
      return baseValue * (1.3 - position * 0.8);
    case 'ascending':
      return position;
    case 'descending':
      return 1 - position;
    default:
      return baseValue;
  }
}

// Generate rhythm pattern for a bar based on density and complexity
function generateRhythm(density, complexity, rhythmic, rand, beatsPerBar = 4) {
  const subdivisions = 16; // 16th note resolution
  const totalSlots = subdivisions;
  const events = [];

  // Determine how many notes to place
  const targetNotes = Math.max(1, Math.round(density * totalSlots * 0.5));

  if (rhythmic > 0.7) {
    // Highly rhythmic - emphasize regular patterns
    const patternLength = complexity > 0.5 ? (rand() > 0.5 ? 3 : 5) : (rand() > 0.5 ? 2 : 4);
    for (let i = 0; i < totalSlots && events.length < targetNotes; i++) {
      if (i % patternLength === 0 || (complexity > 0.7 && rand() < 0.3)) {
        const duration = Math.min(patternLength, totalSlots - i);
        events.push({
          time: i / subdivisions * beatsPerBar,
          duration: duration / subdivisions * beatsPerBar * (0.5 + rand() * 0.5),
        });
      }
    }
  } else if (rhythmic < 0.3) {
    // Sustained / legato
    const numNotes = Math.max(1, Math.round(targetNotes * 0.3));
    let pos = 0;
    for (let i = 0; i < numNotes && pos < totalSlots; i++) {
      const dur = Math.max(2, Math.round((totalSlots / numNotes) * (0.7 + rand() * 0.6)));
      events.push({
        time: pos / subdivisions * beatsPerBar,
        duration: Math.min(dur, totalSlots - pos) / subdivisions * beatsPerBar,
      });
      pos += dur + Math.round(rand() * 2);
    }
  } else {
    // Mixed approach
    for (let i = 0; i < totalSlots && events.length < targetNotes; i++) {
      const probability = density * (complexity > 0.5 ? 0.6 : 0.4);
      if (rand() < probability) {
        const maxDur = Math.min(4, totalSlots - i);
        const dur = 1 + Math.floor(rand() * maxDur * (1 - rhythmic + 0.3));
        events.push({
          time: i / subdivisions * beatsPerBar,
          duration: dur / subdivisions * beatsPerBar,
        });
        i += dur - 1; // Skip ahead
      }
    }
  }

  if (events.length === 0) {
    events.push({ time: 0, duration: beatsPerBar });
  }

  return events;
}

// Select pitch based on available notes, previous pitch, and interval bias
function selectPitch(scaleNotes, prevPitch, intervalBias, rand, chordTones = null) {
  if (scaleNotes.length === 0) return 60;

  // Weight chord tones more heavily
  let candidates = [...scaleNotes];
  if (chordTones && chordTones.length > 0 && rand() < 0.6) {
    candidates = chordTones.filter(n => scaleNotes.includes(n));
    if (candidates.length === 0) candidates = chordTones;
  }

  if (prevPitch === null) {
    return candidates[Math.floor(rand() * candidates.length)];
  }

  // Calculate weights based on interval distance
  const weights = candidates.map(note => {
    const interval = Math.abs(note - prevPitch);
    switch (intervalBias) {
      case 'narrow':
        return interval === 0 ? 0.5 : Math.max(0.01, 1 / (interval * 0.5 + 1));
      case 'wide':
        return interval < 3 ? 0.3 : Math.min(2, interval * 0.15);
      default: // moderate
        return interval === 0 ? 0.3 : Math.max(0.1, 1 / (interval * 0.2 + 0.5));
    }
  });

  const totalWeight = weights.reduce((s, w) => s + w, 0);
  let pick = rand() * totalWeight;
  for (let i = 0; i < candidates.length; i++) {
    pick -= weights[i];
    if (pick <= 0) return candidates[i];
  }
  return candidates[candidates.length - 1];
}

// Main generation function
export function generatePart({ descriptor, tempo, density, complexity, rhythmic, durationBars, root = 48, seed = null, role = 'lead', existingParts = [] }) {
  const s = seed ?? Math.floor(Math.random() * 99999);
  const rand = seededRandom(s);

  // Analyze text descriptor
  const analysis = analyzeText(descriptor);

  // Apply text analysis modifiers to parameters
  const effectiveTempo = clamp(tempo + analysis.tempoMod, 40, 240);
  const effectiveDensity = clamp(density + analysis.densityMod, 0.05, 1);
  const registerRange = analysis.registerRange;

  // Get scale notes in register range
  const rootNote = root % 12;
  const scaleNotes = getScaleNotes(rootNote, analysis.scale, registerRange.low, registerRange.high);

  // Generate chord progression
  const progression = generateProgression(analysis.scale, durationBars, s);

  const notes = [];
  let prevPitch = null;

  for (let bar = 0; bar < durationBars; bar++) {
    const position = bar / durationBars; // 0-1 through the piece
    const chordDegree = progression[bar];

    // Get chord tones for this bar
    const chordTones = getChordNotesForBar(rootNote, analysis.scale, chordDegree, registerRange);
    const chordRoot = getChordRoot(rootNote, analysis.scale, chordDegree);

    // Apply curves
    const barDensity = applyCurve(effectiveDensity, position, analysis.densityCurve);
    const barVelocity = applyCurve(analysis.velocityBase, position, analysis.velocityCurve, 0.6);

    // Adjust register based on register curve
    let registerShift = 0;
    if (analysis.registerCurve === 'ascending') {
      registerShift = Math.floor(position * 12);
    } else if (analysis.registerCurve === 'descending') {
      registerShift = Math.floor((1 - position) * 12) - 6;
    } else if (analysis.registerCurve === 'wave') {
      registerShift = Math.floor(Math.sin(position * Math.PI * 2) * 6);
    }

    // Generate role-specific content
    const roleNotes = generateRoleNotes({
      role,
      bar,
      barDensity: clamp(barDensity, 0.05, 1),
      barVelocity: clamp(barVelocity, 20, 127),
      complexity,
      rhythmic,
      scaleNotes,
      chordTones,
      chordRoot,
      registerRange,
      registerShift,
      intervalBias: analysis.intervalBias,
      prevPitch,
      rand,
      existingParts,
      beatsPerBar: 4,
    });

    for (const note of roleNotes) {
      notes.push({
        pitch: clamp(note.pitch + registerShift, 0, 127),
        startTime: bar * 4 + note.time,
        duration: note.duration,
        velocity: clamp(Math.round(note.velocity), 1, 127),
      });
      prevPitch = note.pitch;
    }
  }

  return {
    notes,
    analysis,
    effectiveTempo,
    seed: s,
    progression,
  };
}

function generateRoleNotes({ role, bar, barDensity, barVelocity, complexity, rhythmic, scaleNotes, chordTones, chordRoot, registerRange, registerShift, intervalBias, prevPitch, rand, existingParts, beatsPerBar }) {
  const notes = [];

  switch (role) {
    case 'lead':
    case 'melody': {
      const rhythm = generateRhythm(barDensity, complexity, rhythmic, rand, beatsPerBar);
      for (const event of rhythm) {
        const pitch = selectPitch(scaleNotes, prevPitch, intervalBias, rand, chordTones);
        const velVariation = (rand() - 0.5) * 20;
        notes.push({
          pitch,
          time: event.time,
          duration: event.duration,
          velocity: barVelocity + velVariation,
        });
        prevPitch = pitch;
      }
      break;
    }

    case 'bass': {
      // Bass plays root notes and fifths on strong beats
      const bassRange = { low: Math.max(28, registerRange.low - 24), high: Math.min(55, registerRange.low) };
      const bassNotes = getScaleNotes(chordRoot % 12, 'major', bassRange.low, bassRange.high);
      const bassRoot = bassNotes.length > 0 ? bassNotes[0] : chordRoot;

      if (rhythmic > 0.6) {
        // Walking/driving bass
        const rhythm = generateRhythm(barDensity * 0.7, complexity * 0.5, rhythmic, rand, beatsPerBar);
        for (const event of rhythm) {
          let pitch = bassRoot;
          if (rand() < 0.3) pitch = bassRoot + 7; // fifth
          if (rand() < 0.15) pitch = bassRoot + 12; // octave
          notes.push({
            pitch: clamp(pitch, bassRange.low, bassRange.high),
            time: event.time,
            duration: event.duration,
            velocity: barVelocity + 5,
          });
        }
      } else {
        // Sustained bass
        notes.push({
          pitch: bassRoot,
          time: 0,
          duration: beatsPerBar * (0.75 + rand() * 0.25),
          velocity: barVelocity,
        });
        if (rand() < 0.4) {
          notes.push({
            pitch: bassRoot + 7,
            time: beatsPerBar * 0.5,
            duration: beatsPerBar * 0.5,
            velocity: barVelocity * 0.8,
          });
        }
      }
      break;
    }

    case 'harmony':
    case 'chords': {
      if (rhythmic > 0.6) {
        // Rhythmic chords
        const rhythm = generateRhythm(barDensity * 0.5, complexity * 0.3, rhythmic, rand, beatsPerBar);
        for (const event of rhythm) {
          for (const tone of chordTones.slice(0, 3)) {
            notes.push({
              pitch: tone,
              time: event.time,
              duration: event.duration * 0.8,
              velocity: barVelocity * 0.7,
            });
          }
        }
      } else {
        // Sustained pad chords
        for (const tone of chordTones.slice(0, 3 + (complexity > 0.5 ? 1 : 0))) {
          notes.push({
            pitch: tone,
            time: 0,
            duration: beatsPerBar,
            velocity: barVelocity * 0.6,
          });
        }
      }
      break;
    }

    case 'arpeggio': {
      const arpTones = chordTones.length > 0 ? chordTones : scaleNotes.slice(0, 4);
      const patterns = ['up', 'down', 'updown', 'random'];
      const pattern = patterns[Math.floor(rand() * patterns.length)];
      const notesPerBeat = Math.max(2, Math.round(barDensity * 6));
      const noteInterval = beatsPerBar / notesPerBeat;

      for (let i = 0; i < notesPerBeat; i++) {
        let toneIdx;
        switch (pattern) {
          case 'up': toneIdx = i % arpTones.length; break;
          case 'down': toneIdx = (arpTones.length - 1 - i % arpTones.length); break;
          case 'updown': {
            const cycle = arpTones.length * 2 - 2;
            const pos = i % cycle;
            toneIdx = pos < arpTones.length ? pos : cycle - pos;
            break;
          }
          default: toneIdx = Math.floor(rand() * arpTones.length);
        }
        notes.push({
          pitch: arpTones[clamp(toneIdx, 0, arpTones.length - 1)],
          time: i * noteInterval,
          duration: noteInterval * (0.7 + rand() * 0.3),
          velocity: barVelocity * (0.6 + rand() * 0.3),
        });
      }
      break;
    }

    case 'counter': {
      // Counter-melody: complement existing parts with contrary motion
      const rhythm = generateRhythm(barDensity * 0.6, complexity, 1 - rhythmic, rand, beatsPerBar);
      for (const event of rhythm) {
        // Favor notes that aren't in existing parts at this time
        let pitch = selectPitch(scaleNotes, prevPitch, intervalBias === 'narrow' ? 'wide' : 'narrow', rand, chordTones);
        notes.push({
          pitch,
          time: event.time,
          duration: event.duration,
          velocity: barVelocity * 0.75,
        });
        prevPitch = pitch;
      }
      break;
    }

    case 'pad': {
      // Long sustained tones
      for (const tone of chordTones.slice(0, 3)) {
        notes.push({
          pitch: tone,
          time: 0,
          duration: beatsPerBar,
          velocity: barVelocity * 0.45,
        });
      }
      break;
    }

    default: {
      // Default to melody behavior
      const rhythm = generateRhythm(barDensity, complexity, rhythmic, rand, beatsPerBar);
      for (const event of rhythm) {
        const pitch = selectPitch(scaleNotes, prevPitch, intervalBias, rand, chordTones);
        notes.push({
          pitch,
          time: event.time,
          duration: event.duration,
          velocity: barVelocity,
        });
        prevPitch = pitch;
      }
    }
  }

  return notes;
}

// Suggest complementary roles based on existing tracks
export function suggestRoles(existingRoles) {
  const allRoles = ['lead', 'bass', 'harmony', 'arpeggio', 'counter', 'pad'];
  const missing = allRoles.filter(r => !existingRoles.includes(r));

  // Priority order for building up a composition
  const priority = ['bass', 'harmony', 'arpeggio', 'counter', 'pad', 'lead'];
  const suggestions = priority.filter(r => missing.includes(r));

  return suggestions.length > 0 ? suggestions : ['lead'];
}
