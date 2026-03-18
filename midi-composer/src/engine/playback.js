import * as Tone from 'tone';

const ROLE_SYNTHS = {
  lead: () => new Tone.PolySynth(Tone.Synth, {
    oscillator: { type: 'triangle' },
    envelope: { attack: 0.02, decay: 0.3, sustain: 0.4, release: 0.8 },
  }),
  melody: () => new Tone.PolySynth(Tone.Synth, {
    oscillator: { type: 'triangle' },
    envelope: { attack: 0.02, decay: 0.3, sustain: 0.4, release: 0.8 },
  }),
  bass: () => new Tone.PolySynth(Tone.Synth, {
    oscillator: { type: 'sawtooth' },
    envelope: { attack: 0.01, decay: 0.2, sustain: 0.6, release: 0.4 },
    volume: -6,
  }),
  harmony: () => new Tone.PolySynth(Tone.Synth, {
    oscillator: { type: 'sine' },
    envelope: { attack: 0.1, decay: 0.4, sustain: 0.5, release: 1.2 },
    volume: -10,
  }),
  chords: () => new Tone.PolySynth(Tone.Synth, {
    oscillator: { type: 'square' },
    envelope: { attack: 0.05, decay: 0.3, sustain: 0.3, release: 0.6 },
    volume: -12,
  }),
  arpeggio: () => new Tone.PolySynth(Tone.Synth, {
    oscillator: { type: 'triangle' },
    envelope: { attack: 0.01, decay: 0.15, sustain: 0.2, release: 0.5 },
    volume: -8,
  }),
  counter: () => new Tone.PolySynth(Tone.Synth, {
    oscillator: { type: 'sine' },
    envelope: { attack: 0.03, decay: 0.25, sustain: 0.35, release: 0.7 },
    volume: -8,
  }),
  pad: () => new Tone.PolySynth(Tone.Synth, {
    oscillator: { type: 'sine' },
    envelope: { attack: 0.5, decay: 0.8, sustain: 0.7, release: 2 },
    volume: -14,
  }),
};

let activeSynths = [];
let scheduledEvents = [];
let isPlaying = false;

function midiToFreq(midi) {
  return 440 * Math.pow(2, (midi - 69) / 12);
}

export async function startPlayback(tracks, tempo, onBeat = null) {
  await Tone.start();
  stopPlayback();

  Tone.getTransport().bpm.value = tempo;
  Tone.getTransport().position = 0;

  for (const track of tracks) {
    if (track.muted) continue;

    const createSynth = ROLE_SYNTHS[track.role] || ROLE_SYNTHS.lead;
    const synth = createSynth();
    synth.toDestination();
    activeSynths.push(synth);

    for (const note of track.notes) {
      const freq = midiToFreq(note.pitch);
      const vel = note.velocity / 127;

      const eventId = Tone.getTransport().schedule((time) => {
        synth.triggerAttackRelease(freq, note.duration * (60 / tempo), time, vel);
      }, note.startTime * (60 / tempo));

      scheduledEvents.push(eventId);
    }
  }

  // Beat callback for UI
  if (onBeat) {
    const totalBeats = Math.max(...tracks.map(t =>
      t.notes.length > 0 ? Math.max(...t.notes.map(n => n.startTime + n.duration)) : 0
    ));
    let beatInterval;
    beatInterval = Tone.getTransport().scheduleRepeat((time) => {
      const position = Tone.getTransport().seconds;
      const beat = position / (60 / tempo);
      Tone.getDraw().schedule(() => {
        onBeat(beat, totalBeats);
      }, time);
    }, '16n');
    scheduledEvents.push(beatInterval);

    // Auto-stop
    const totalTime = totalBeats * (60 / tempo) + 1;
    const stopEvent = Tone.getTransport().schedule(() => {
      stopPlayback();
      Tone.getDraw().schedule(() => {
        onBeat(-1, totalBeats);
      }, Tone.now());
    }, totalTime);
    scheduledEvents.push(stopEvent);
  }

  Tone.getTransport().start();
  isPlaying = true;
}

export function stopPlayback() {
  Tone.getTransport().stop();
  Tone.getTransport().cancel();

  for (const synth of activeSynths) {
    try { synth.releaseAll(); } catch {}
    try { synth.dispose(); } catch {}
  }
  activeSynths = [];
  scheduledEvents = [];
  isPlaying = false;
}

export function getIsPlaying() {
  return isPlaying;
}
