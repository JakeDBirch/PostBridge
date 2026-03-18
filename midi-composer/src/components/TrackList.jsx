import React from 'react';

const TRACK_COLORS = [
  '#6366f1', '#f59e0b', '#10b981', '#ef4444',
  '#8b5cf6', '#06b6d4', '#f97316', '#ec4899',
];

const ROLE_ICONS = {
  lead: '🎹',
  melody: '🎵',
  bass: '🎸',
  harmony: '🎻',
  chords: '🎹',
  arpeggio: '🔔',
  counter: '🎶',
  pad: '☁️',
};

export default function TrackList({ tracks, onToggleMute, onToggleSolo, onRemoveTrack, onSelectTrack, selectedTrackId }) {
  if (tracks.length === 0) {
    return (
      <div className="track-list-empty">
        <p>No tracks yet. Describe your music and generate a part!</p>
      </div>
    );
  }

  return (
    <div className="track-list">
      {tracks.map((track, idx) => {
        const color = TRACK_COLORS[idx % TRACK_COLORS.length];
        const isSelected = track.id === selectedTrackId;
        const noteCount = track.notes.length;
        const bars = Math.ceil(Math.max(...track.notes.map(n => n.startTime + n.duration), 0) / 4);

        return (
          <div
            key={track.id}
            className={`track-item ${isSelected ? 'selected' : ''} ${track.muted ? 'muted' : ''}`}
            style={{ borderLeftColor: color }}
            onClick={() => onSelectTrack(track.id)}
          >
            <div className="track-header">
              <span className="track-icon">{ROLE_ICONS[track.role] || '🎵'}</span>
              <div className="track-info">
                <span className="track-name">{track.name}</span>
                <span className="track-meta">
                  {track.role} &bull; {noteCount} notes &bull; {bars} bars
                </span>
                {track.descriptor && (
                  <span className="track-descriptor">"{track.descriptor}"</span>
                )}
              </div>
            </div>
            <div className="track-controls">
              <button
                className={`track-btn ${track.muted ? 'active' : ''}`}
                onClick={(e) => { e.stopPropagation(); onToggleMute(track.id); }}
                title="Mute"
              >
                M
              </button>
              <button
                className={`track-btn ${track.solo ? 'active' : ''}`}
                onClick={(e) => { e.stopPropagation(); onToggleSolo(track.id); }}
                title="Solo"
              >
                S
              </button>
              <button
                className="track-btn remove"
                onClick={(e) => { e.stopPropagation(); onRemoveTrack(track.id); }}
                title="Remove"
              >
                ×
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
