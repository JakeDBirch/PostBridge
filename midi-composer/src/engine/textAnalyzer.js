// Maps text descriptors to musical parameters

const MOOD_KEYWORDS = {
  // Sad/dark moods
  sad:         { scale: 'natural_minor', register: 'mid_low', velocityBase: 60, densityMod: -0.15, tempoMod: -15, intervalBias: 'narrow' },
  melancholy:  { scale: 'natural_minor', register: 'mid_low', velocityBase: 55, densityMod: -0.2, tempoMod: -20, intervalBias: 'narrow' },
  dark:        { scale: 'phrygian', register: 'low', velocityBase: 70, densityMod: -0.1, tempoMod: -10, intervalBias: 'narrow' },
  gloomy:      { scale: 'natural_minor', register: 'low', velocityBase: 50, densityMod: -0.25, tempoMod: -25, intervalBias: 'narrow' },
  somber:      { scale: 'aeolian', register: 'mid_low', velocityBase: 55, densityMod: -0.2, tempoMod: -15, intervalBias: 'narrow' },
  mournful:    { scale: 'harmonic_minor', register: 'mid_low', velocityBase: 50, densityMod: -0.2, tempoMod: -20, intervalBias: 'narrow' },
  lonely:      { scale: 'natural_minor', register: 'mid', velocityBase: 45, densityMod: -0.3, tempoMod: -20, intervalBias: 'wide' },
  nostalgic:   { scale: 'dorian', register: 'mid', velocityBase: 55, densityMod: -0.15, tempoMod: -10, intervalBias: 'narrow' },

  // Happy/bright moods
  happy:       { scale: 'major', register: 'mid_high', velocityBase: 85, densityMod: 0.1, tempoMod: 15, intervalBias: 'moderate' },
  joyful:      { scale: 'major', register: 'high', velocityBase: 90, densityMod: 0.15, tempoMod: 20, intervalBias: 'moderate' },
  bright:      { scale: 'lydian', register: 'mid_high', velocityBase: 80, densityMod: 0.1, tempoMod: 10, intervalBias: 'moderate' },
  uplifting:   { scale: 'major', register: 'mid_high', velocityBase: 85, densityMod: 0.1, tempoMod: 15, intervalBias: 'wide' },
  playful:     { scale: 'pentatonic_major', register: 'mid_high', velocityBase: 80, densityMod: 0.15, tempoMod: 20, intervalBias: 'wide' },
  cheerful:    { scale: 'major', register: 'mid_high', velocityBase: 85, densityMod: 0.1, tempoMod: 15, intervalBias: 'moderate' },

  // Energetic/intense moods
  energetic:   { scale: 'mixolydian', register: 'mid_high', velocityBase: 95, densityMod: 0.2, tempoMod: 30, intervalBias: 'wide' },
  aggressive:  { scale: 'phrygian', register: 'low', velocityBase: 110, densityMod: 0.25, tempoMod: 25, intervalBias: 'wide' },
  intense:     { scale: 'harmonic_minor', register: 'mid', velocityBase: 100, densityMod: 0.2, tempoMod: 20, intervalBias: 'wide' },
  driving:     { scale: 'mixolydian', register: 'mid', velocityBase: 95, densityMod: 0.2, tempoMod: 25, intervalBias: 'moderate' },
  powerful:    { scale: 'natural_minor', register: 'mid_low', velocityBase: 105, densityMod: 0.15, tempoMod: 15, intervalBias: 'wide' },
  fierce:      { scale: 'phrygian', register: 'mid_low', velocityBase: 110, densityMod: 0.25, tempoMod: 30, intervalBias: 'wide' },

  // Calm/peaceful moods
  calm:        { scale: 'major', register: 'mid', velocityBase: 50, densityMod: -0.2, tempoMod: -20, intervalBias: 'narrow' },
  peaceful:    { scale: 'pentatonic_major', register: 'mid', velocityBase: 45, densityMod: -0.25, tempoMod: -25, intervalBias: 'narrow' },
  gentle:      { scale: 'major', register: 'mid', velocityBase: 45, densityMod: -0.2, tempoMod: -20, intervalBias: 'narrow' },
  serene:      { scale: 'pentatonic_major', register: 'mid', velocityBase: 40, densityMod: -0.3, tempoMod: -30, intervalBias: 'narrow' },
  dreamy:      { scale: 'lydian', register: 'mid_high', velocityBase: 45, densityMod: -0.2, tempoMod: -15, intervalBias: 'moderate' },
  ambient:     { scale: 'pentatonic_major', register: 'mid', velocityBase: 40, densityMod: -0.35, tempoMod: -30, intervalBias: 'wide' },
  ethereal:    { scale: 'lydian', register: 'high', velocityBase: 40, densityMod: -0.3, tempoMod: -20, intervalBias: 'wide' },
  floating:    { scale: 'whole_tone', register: 'mid_high', velocityBase: 40, densityMod: -0.3, tempoMod: -25, intervalBias: 'wide' },

  // Mysterious/tense moods
  mysterious:  { scale: 'whole_tone', register: 'mid', velocityBase: 55, densityMod: -0.15, tempoMod: -10, intervalBias: 'wide' },
  eerie:       { scale: 'phrygian', register: 'mid_low', velocityBase: 50, densityMod: -0.2, tempoMod: -15, intervalBias: 'wide' },
  tense:       { scale: 'harmonic_minor', register: 'mid', velocityBase: 75, densityMod: 0.05, tempoMod: 5, intervalBias: 'narrow' },
  suspenseful: { scale: 'harmonic_minor', register: 'mid_low', velocityBase: 65, densityMod: -0.1, tempoMod: -5, intervalBias: 'narrow' },
  haunting:    { scale: 'harmonic_minor', register: 'mid', velocityBase: 50, densityMod: -0.2, tempoMod: -15, intervalBias: 'wide' },
  ominous:     { scale: 'phrygian', register: 'low', velocityBase: 60, densityMod: -0.15, tempoMod: -15, intervalBias: 'narrow' },

  // Epic/cinematic
  epic:        { scale: 'natural_minor', register: 'full', velocityBase: 90, densityMod: 0.15, tempoMod: 10, intervalBias: 'wide' },
  cinematic:   { scale: 'natural_minor', register: 'full', velocityBase: 80, densityMod: 0.1, tempoMod: 5, intervalBias: 'wide' },
  heroic:      { scale: 'major', register: 'mid_high', velocityBase: 95, densityMod: 0.15, tempoMod: 15, intervalBias: 'wide' },
  triumphant:  { scale: 'major', register: 'high', velocityBase: 100, densityMod: 0.15, tempoMod: 15, intervalBias: 'wide' },
  majestic:    { scale: 'major', register: 'full', velocityBase: 85, densityMod: 0.1, tempoMod: 5, intervalBias: 'wide' },

  // Groove/funk
  funky:       { scale: 'blues', register: 'mid', velocityBase: 90, densityMod: 0.15, tempoMod: 10, intervalBias: 'moderate' },
  groovy:      { scale: 'dorian', register: 'mid', velocityBase: 85, densityMod: 0.1, tempoMod: 10, intervalBias: 'moderate' },
  jazzy:       { scale: 'dorian', register: 'mid', velocityBase: 75, densityMod: 0.1, tempoMod: 5, intervalBias: 'moderate' },
  bluesy:      { scale: 'blues', register: 'mid_low', velocityBase: 70, densityMod: 0, tempoMod: -5, intervalBias: 'moderate' },

  // Minimal/sparse
  minimal:     { scale: 'pentatonic_major', register: 'mid', velocityBase: 55, densityMod: -0.3, tempoMod: -10, intervalBias: 'narrow' },
  sparse:      { scale: 'pentatonic_major', register: 'mid', velocityBase: 50, densityMod: -0.35, tempoMod: -15, intervalBias: 'narrow' },
  delicate:    { scale: 'major', register: 'mid_high', velocityBase: 40, densityMod: -0.3, tempoMod: -15, intervalBias: 'narrow' },
};

const MOVEMENT_KEYWORDS = {
  builds:      { velocityCurve: 'crescendo', densityCurve: 'increasing', registerCurve: 'ascending' },
  building:    { velocityCurve: 'crescendo', densityCurve: 'increasing', registerCurve: 'ascending' },
  crescendo:   { velocityCurve: 'crescendo', densityCurve: 'increasing', registerCurve: 'static' },
  rising:      { velocityCurve: 'crescendo', densityCurve: 'static', registerCurve: 'ascending' },
  ascending:   { velocityCurve: 'static', densityCurve: 'static', registerCurve: 'ascending' },
  fades:       { velocityCurve: 'decrescendo', densityCurve: 'decreasing', registerCurve: 'descending' },
  fading:      { velocityCurve: 'decrescendo', densityCurve: 'decreasing', registerCurve: 'descending' },
  decrescendo: { velocityCurve: 'decrescendo', densityCurve: 'decreasing', registerCurve: 'static' },
  falling:     { velocityCurve: 'decrescendo', densityCurve: 'static', registerCurve: 'descending' },
  descending:  { velocityCurve: 'static', densityCurve: 'static', registerCurve: 'descending' },
  swelling:    { velocityCurve: 'swell', densityCurve: 'swell', registerCurve: 'static' },
  pulsing:     { velocityCurve: 'pulse', densityCurve: 'pulse', registerCurve: 'static' },
  steady:      { velocityCurve: 'static', densityCurve: 'static', registerCurve: 'static' },
  constant:    { velocityCurve: 'static', densityCurve: 'static', registerCurve: 'static' },
  evolving:    { velocityCurve: 'crescendo', densityCurve: 'increasing', registerCurve: 'ascending' },
  climactic:   { velocityCurve: 'crescendo', densityCurve: 'increasing', registerCurve: 'ascending' },
  winding:     { velocityCurve: 'swell', densityCurve: 'swell', registerCurve: 'wave' },
  undulating:  { velocityCurve: 'swell', densityCurve: 'swell', registerCurve: 'wave' },
};

const REGISTER_RANGES = {
  low:      { low: 28, high: 55 },
  mid_low:  { low: 36, high: 64 },
  mid:      { low: 48, high: 76 },
  mid_high: { low: 55, high: 84 },
  high:     { low: 64, high: 96 },
  full:     { low: 36, high: 90 },
};

export function analyzeText(text) {
  const lower = text.toLowerCase();
  const words = lower.split(/[\s,;.!?]+/).filter(Boolean);

  const result = {
    scale: 'major',
    register: 'mid',
    velocityBase: 70,
    densityMod: 0,
    tempoMod: 0,
    intervalBias: 'moderate',
    velocityCurve: 'static',
    densityCurve: 'static',
    registerCurve: 'static',
    registerRange: { low: 48, high: 76 },
    matchedMoods: [],
    matchedMovements: [],
  };

  // Match mood keywords (can combine multiple)
  let moodCount = 0;
  for (const word of words) {
    // Check exact matches and partial matches
    for (const [keyword, params] of Object.entries(MOOD_KEYWORDS)) {
      if (word === keyword || lower.includes(keyword)) {
        if (!result.matchedMoods.includes(keyword)) {
          result.matchedMoods.push(keyword);
          if (moodCount === 0) {
            Object.assign(result, params);
          } else {
            // Blend with existing
            result.velocityBase = Math.round((result.velocityBase + params.velocityBase) / 2);
            result.densityMod = (result.densityMod + params.densityMod) / 2;
            result.tempoMod = Math.round((result.tempoMod + params.tempoMod) / 2);
            // Later mood's scale takes precedence
            result.scale = params.scale;
            result.register = params.register;
          }
          moodCount++;
        }
      }
    }
  }

  // Match movement keywords
  for (const word of words) {
    for (const [keyword, params] of Object.entries(MOVEMENT_KEYWORDS)) {
      if (word === keyword || lower.includes(keyword)) {
        if (!result.matchedMovements.includes(keyword)) {
          result.matchedMovements.push(keyword);
          Object.assign(result, params);
        }
      }
    }
  }

  // Resolve register range
  result.registerRange = REGISTER_RANGES[result.register] || REGISTER_RANGES.mid;

  return result;
}

export function getDescriptorSummary(analysis) {
  const parts = [];
  if (analysis.matchedMoods.length) {
    parts.push(`Mood: ${analysis.matchedMoods.join(', ')}`);
  }
  if (analysis.matchedMovements.length) {
    parts.push(`Movement: ${analysis.matchedMovements.join(', ')}`);
  }
  parts.push(`Scale: ${analysis.scale.replace('_', ' ')}`);
  return parts.join(' | ');
}
