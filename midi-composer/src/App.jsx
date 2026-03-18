import React, { useState, useCallback } from 'react';
import ControlPanel from './components/ControlPanel.jsx';
import TrackList from './components/TrackList.jsx';
import PianoRoll from './components/PianoRoll.jsx';
import { generatePart } from './engine/generator.js';
import { getDescriptorSummary } from './engine/textAnalyzer.js';
import { exportMidi, downloadMidi, exportSingleTrackMidi } from './engine/midiExport.js';
import { startPlayback, stopPlayback, getIsPlaying } from './engine/playback.js';

let nextTrackId = 1;

export default function App() {
  const [tracks, setTracks] = useState([]);
  const [selectedTrackId, setSelectedTrackId] = useState(null);
  const [isGenerating, setIsGenerating] = useState(false);
  const [isPlaying, setIsPlaying] = useState(false);
  const [playbackBeat, setPlaybackBeat] = useState(-1);
  const [globalTempo, setGlobalTempo] = useState(120);

  const handleGenerate = useCallback((params) => {
    setIsGenerating(true);

    // Small delay so UI updates
    setTimeout(() => {
      try {
        const result = generatePart({
          ...params,
          existingParts: tracks,
        });

        const newTrack = {
          id: `track-${nextTrackId++}`,
          name: `${params.role.charAt(0).toUpperCase() + params.role.slice(1)} ${nextTrackId - 1}`,
          descriptor: params.descriptor,
          role: params.role,
          notes: result.notes,
          analysis: result.analysis,
          seed: result.seed,
          muted: false,
          solo: false,
        };

        setTracks(prev => [...prev, newTrack]);
        setSelectedTrackId(newTrack.id);
        setGlobalTempo(result.effectiveTempo);
      } catch (err) {
        console.error('Generation error:', err);
      }
      setIsGenerating(false);
    }, 50);
  }, [tracks]);

  const handleToggleMute = useCallback((id) => {
    setTracks(prev => prev.map(t =>
      t.id === id ? { ...t, muted: !t.muted } : t
    ));
  }, []);

  const handleToggleSolo = useCallback((id) => {
    setTracks(prev => {
      const track = prev.find(t => t.id === id);
      if (!track) return prev;
      const newSolo = !track.solo;
      return prev.map(t => ({
        ...t,
        solo: t.id === id ? newSolo : false,
        muted: newSolo ? t.id !== id : t.muted,
      }));
    });
  }, []);

  const handleRemoveTrack = useCallback((id) => {
    setTracks(prev => prev.filter(t => t.id !== id));
    if (selectedTrackId === id) {
      setSelectedTrackId(null);
    }
  }, [selectedTrackId]);

  const handlePlay = useCallback(async () => {
    if (isPlaying) {
      stopPlayback();
      setIsPlaying(false);
      setPlaybackBeat(-1);
      return;
    }

    const activeTracks = tracks.filter(t => !t.muted);
    if (activeTracks.length === 0) return;

    setIsPlaying(true);
    await startPlayback(activeTracks, globalTempo, (beat, total) => {
      if (beat < 0) {
        setIsPlaying(false);
        setPlaybackBeat(-1);
      } else {
        setPlaybackBeat(beat);
      }
    });
  }, [tracks, globalTempo, isPlaying]);

  const handleExportAll = useCallback(() => {
    const activeTracks = tracks.filter(t => !t.muted);
    if (activeTracks.length === 0) return;
    const midiData = exportMidi(activeTracks, globalTempo);
    downloadMidi(midiData, 'composition.mid');
  }, [tracks, globalTempo]);

  const handleExportTrack = useCallback(() => {
    const track = tracks.find(t => t.id === selectedTrackId);
    if (!track) return;
    const midiData = exportSingleTrackMidi(track, globalTempo);
    downloadMidi(midiData, `${track.name.replace(/\s+/g, '_')}.mid`);
  }, [tracks, selectedTrackId, globalTempo]);

  const handleRegenerate = useCallback(() => {
    const track = tracks.find(t => t.id === selectedTrackId);
    if (!track) return;

    const result = generatePart({
      descriptor: track.descriptor,
      tempo: globalTempo,
      density: 0.5,
      complexity: 0.5,
      rhythmic: 0.5,
      durationBars: Math.ceil(Math.max(...track.notes.map(n => n.startTime + n.duration)) / 4),
      role: track.role,
      root: 48,
      existingParts: tracks.filter(t => t.id !== selectedTrackId),
      seed: null, // New seed
    });

    setTracks(prev => prev.map(t =>
      t.id === selectedTrackId
        ? { ...t, notes: result.notes, seed: result.seed, analysis: result.analysis }
        : t
    ));
  }, [tracks, selectedTrackId, globalTempo]);

  const handleClearAll = useCallback(() => {
    stopPlayback();
    setTracks([]);
    setSelectedTrackId(null);
    setIsPlaying(false);
    setPlaybackBeat(-1);
  }, []);

  const selectedTrack = tracks.find(t => t.id === selectedTrackId);

  return (
    <div className="app">
      <header className="app-header">
        <h1>MIDI Composer</h1>
        <span className="app-subtitle">Generative MIDI for Music Composition</span>
      </header>

      <div className="app-layout">
        <aside className="sidebar">
          <ControlPanel
            onGenerate={handleGenerate}
            existingTracks={tracks}
            isGenerating={isGenerating}
          />

          <div className="sidebar-section">
            <h3>Tracks</h3>
            <TrackList
              tracks={tracks}
              onToggleMute={handleToggleMute}
              onToggleSolo={handleToggleSolo}
              onRemoveTrack={handleRemoveTrack}
              onSelectTrack={setSelectedTrackId}
              selectedTrackId={selectedTrackId}
            />
          </div>
        </aside>

        <main className="main-content">
          <div className="toolbar">
            <div className="toolbar-left">
              <button
                className={`toolbar-btn ${isPlaying ? 'playing' : ''}`}
                onClick={handlePlay}
                disabled={tracks.length === 0}
              >
                {isPlaying ? '⏹ Stop' : '▶ Play'}
              </button>

              <div className="tempo-display">
                <label>Tempo:</label>
                <input
                  type="number" min="40" max="240"
                  value={globalTempo}
                  onChange={(e) => setGlobalTempo(Number(e.target.value))}
                  className="tempo-input"
                />
                <span>BPM</span>
              </div>
            </div>

            <div className="toolbar-right">
              {selectedTrack && (
                <>
                  <button className="toolbar-btn" onClick={handleRegenerate} title="Generate new variation">
                    Regenerate
                  </button>
                  <button className="toolbar-btn" onClick={handleExportTrack}>
                    Export Track
                  </button>
                </>
              )}
              <button
                className="toolbar-btn primary"
                onClick={handleExportAll}
                disabled={tracks.length === 0}
              >
                Export All MIDI
              </button>
              <button
                className="toolbar-btn danger"
                onClick={handleClearAll}
                disabled={tracks.length === 0}
              >
                Clear
              </button>
            </div>
          </div>

          {selectedTrack && selectedTrack.analysis && (
            <div className="analysis-bar">
              {getDescriptorSummary(selectedTrack.analysis)}
              <span className="analysis-seed">Seed: {selectedTrack.seed}</span>
            </div>
          )}

          <div className="piano-roll-wrapper">
            <PianoRoll tracks={tracks} playbackBeat={playbackBeat} />
          </div>

          {tracks.length === 0 && (
            <div className="welcome">
              <h2>Welcome to MIDI Composer</h2>
              <p>Describe the music you want to create using natural language, adjust the parameters, and generate MIDI parts.</p>
              <div className="welcome-steps">
                <div className="step">
                  <span className="step-num">1</span>
                  <span>Describe your music (e.g., "Sad music that builds")</span>
                </div>
                <div className="step">
                  <span className="step-num">2</span>
                  <span>Adjust sliders for tempo, density, complexity, and rhythm</span>
                </div>
                <div className="step">
                  <span className="step-num">3</span>
                  <span>Generate your first part, then add more to build your composition</span>
                </div>
                <div className="step">
                  <span className="step-num">4</span>
                  <span>Export as MIDI to use in your DAW</span>
                </div>
              </div>
              <div className="welcome-examples">
                <h3>Try these descriptors:</h3>
                <div className="example-tags">
                  <span className="tag">Sad music that builds</span>
                  <span className="tag">Energetic and driving</span>
                  <span className="tag">Calm and peaceful</span>
                  <span className="tag">Dark and mysterious</span>
                  <span className="tag">Epic cinematic</span>
                  <span className="tag">Funky groovy</span>
                  <span className="tag">Ethereal floating</span>
                  <span className="tag">Tense suspenseful</span>
                </div>
              </div>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
