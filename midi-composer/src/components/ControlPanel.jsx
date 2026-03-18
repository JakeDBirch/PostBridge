import React, { useState } from 'react';
import { suggestRoles } from '../engine/generator.js';
import { NOTE_NAMES } from '../engine/scales.js';

const ROLES = [
  { value: 'lead', label: 'Lead / Melody' },
  { value: 'bass', label: 'Bass' },
  { value: 'harmony', label: 'Harmony / Strings' },
  { value: 'chords', label: 'Chords' },
  { value: 'arpeggio', label: 'Arpeggio' },
  { value: 'counter', label: 'Counter-melody' },
  { value: 'pad', label: 'Pad / Atmosphere' },
];

export default function ControlPanel({ onGenerate, existingTracks, isGenerating }) {
  const existingRoles = existingTracks.map(t => t.role);
  const suggested = suggestRoles(existingRoles);

  const [descriptor, setDescriptor] = useState('');
  const [tempo, setTempo] = useState(120);
  const [density, setDensity] = useState(0.5);
  const [complexity, setComplexity] = useState(0.5);
  const [rhythmic, setRhythmic] = useState(0.5);
  const [durationBars, setDurationBars] = useState(8);
  const [role, setRole] = useState(existingTracks.length === 0 ? 'lead' : suggested[0]);
  const [root, setRoot] = useState(48); // C3

  const handleGenerate = () => {
    if (!descriptor.trim()) return;
    onGenerate({
      descriptor: descriptor.trim(),
      tempo,
      density,
      complexity,
      rhythmic,
      durationBars,
      role,
      root,
    });
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleGenerate();
    }
  };

  return (
    <div className="control-panel">
      <div className="descriptor-section">
        <label className="control-label">Describe Your Music</label>
        <textarea
          className="descriptor-input"
          value={descriptor}
          onChange={(e) => setDescriptor(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder='e.g. "Sad music that builds" or "Energetic and driving, dark and mysterious"'
          rows={2}
        />
      </div>

      <div className="params-grid">
        <div className="param-group">
          <label className="control-label">
            Role
            {existingTracks.length > 0 && suggested.length > 0 && (
              <span className="suggestion"> (suggested: {suggested[0]})</span>
            )}
          </label>
          <select value={role} onChange={(e) => setRole(e.target.value)} className="role-select">
            {ROLES.map(r => (
              <option key={r.value} value={r.value}>
                {r.label}
                {suggested.includes(r.value) ? ' ★' : ''}
              </option>
            ))}
          </select>
        </div>

        <div className="param-group">
          <label className="control-label">Key</label>
          <select value={root % 12} onChange={(e) => setRoot(48 + parseInt(e.target.value))} className="role-select">
            {NOTE_NAMES.map((name, idx) => (
              <option key={name} value={idx}>{name}</option>
            ))}
          </select>
        </div>

        <div className="param-group slider-group">
          <label className="control-label">
            Tempo <span className="param-value">{tempo} BPM</span>
          </label>
          <input
            type="range" min="40" max="240" step="1"
            value={tempo} onChange={(e) => setTempo(Number(e.target.value))}
          />
        </div>

        <div className="param-group slider-group">
          <label className="control-label">
            Density <span className="param-value">{Math.round(density * 100)}%</span>
          </label>
          <input
            type="range" min="0.05" max="1" step="0.01"
            value={density} onChange={(e) => setDensity(Number(e.target.value))}
          />
        </div>

        <div className="param-group slider-group">
          <label className="control-label">
            Complexity <span className="param-value">{Math.round(complexity * 100)}%</span>
          </label>
          <input
            type="range" min="0" max="1" step="0.01"
            value={complexity} onChange={(e) => setComplexity(Number(e.target.value))}
          />
        </div>

        <div className="param-group slider-group">
          <label className="control-label">
            Rhythmic <span className="param-value">
              {rhythmic < 0.3 ? 'Sustained' : rhythmic > 0.7 ? 'Rhythmic' : 'Mixed'}
            </span>
          </label>
          <input
            type="range" min="0" max="1" step="0.01"
            value={rhythmic} onChange={(e) => setRhythmic(Number(e.target.value))}
          />
        </div>

        <div className="param-group slider-group">
          <label className="control-label">
            Duration <span className="param-value">{durationBars} bars</span>
          </label>
          <input
            type="range" min="2" max="64" step="1"
            value={durationBars} onChange={(e) => setDurationBars(Number(e.target.value))}
          />
        </div>
      </div>

      <button
        className="generate-btn"
        onClick={handleGenerate}
        disabled={!descriptor.trim() || isGenerating}
      >
        {isGenerating ? 'Generating...' :
          existingTracks.length === 0 ? 'Generate Part' : 'Add Part'}
      </button>
    </div>
  );
}
