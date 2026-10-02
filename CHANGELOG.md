# PostBridge Changelog

---

## October 2, 2026

### DVD .VOB files in the AAF workflow
VOB files were missing from the video extension list, so file pickers, folder scans and drag-and-drop silently ignored them. They now load like any other video. Folder scans skip the disc's menu files (`VIDEO_TS.VOB`, `VTS_nn_0.VOB`).

- **Sync on the picture's clock.** A VOB's audio can start a little before or after its first video frame. Sync now measures from the first frame, which is what the NLE shows, and VOB seeks land at the exact time asked for (plain input seeking in a VOB could land up to about half a second off).
- **Joining VTS_nn_1.VOB, _2, ….** A DVD title is split into 1 GB pieces of one stream. The join now glues the bytes back together instead of treating them as separate files, which broke the stream at the first seam. The output stays `.vob`, and the continuity check uses the disc's running clock to catch a missing or out-of-order piece.

---

## April 6, 2026

### Multi-source video: auto-detect best file on sync
When a source has multiple video files assigned (e.g. 4 candidate recordings for a 2-clip source), the sync button now probes all of them in parallel and automatically promotes the highest-confidence match. Previously only the first file was tried. This eliminates the need to know in advance which file contains the relevant audio.

### Multi-source clip count in video button
For sources with N>1 files, the video button now shows `"2/4 files  ·  8 clips"` — the clip count that was previously rendering outside the card boundary is now incorporated directly into the button label.

### Codebase audit
Across all 10 modules (18,341 lines reviewed): fixed 4 high-severity bugs, removed ~100 lines of dead code, eliminated redundant inline imports, replaced bare `except:` clauses with `except Exception:`, fixed a hardcoded show name in the reconcile report, and corrected logic errors in the match review waveform editor's silence detection and zoom coordinate system.

### CI: manual-trigger builds only
The GitHub Actions build workflow no longer runs automatically on every push. Builds are triggered manually from the Actions tab, preventing unnecessary artifact storage consumption on the free tier.

---

## April 4, 2026

### Sync algorithm: Stage-1 frequency corrected
Replaced the 5 Hz Stage-1 RMS envelope with 50 Hz — the root fix for systematic failures on clips with small or negative offsets. The prior 5 Hz pass was too coarse to distinguish offsets under ~0.5 s and returned false peaks that poisoned Stage 2.

### Sync: Stage-2 mirror check for false peaks
Added a Stage-2M mirror pass anchored at the negative of the Stage-1 result. When a false positive peak is detected, a narrow ±5 s retry is run on the already-extracted signals. Confidence gating selects the best result across all stages.

### Sync: speed and UX improvements
- Detection runs at CPU count − 1 threads, keeping the machine responsive
- Sync button shows a live spinner animation (SYNCING · / ·· / ···) during detection
- Ctrl+S triggers sync for the focused source in the AAF view

### AAF pool: refresh button and navigation fix
Added a manual pool refresh button. Fixed a bug where the video pool would retain stale contents after navigating away from and back to the home screen.

### Script→Session: VO matching and ordering fixes
Fixed smart apostrophe normalization (`'` → `'`), cursor ordering for VO blocks, and corrected the default gap/pad values that were not being restored from saved sessions.

### UI polish
- Redesigned header with centered PostBridge branding, two-color POST/BRIDGE treatment, and MeatEater horizontal logo
- Unified Step 3 progress display into a single status line
- Reorganized persistent nav bar, pool header, and confirm dialogs
- Fixed FILE/ASSIGNMENT column headers appearing at the bottom of the pool
- Fixed session open incorrectly jumping to Step 4 when saved at Step 2

### Concurrency: Fast/Background toggle
Replaced the per-workflow concurrency controls with a unified Fast/Background toggle. Fast mode uses CPU count − 1 threads; Background mode runs a single thread to leave the machine free for other work. Applies consistently across all workflows.

### Transcribe workflow improvements
- Replaced the binary background toggle with a live-adjustable worker stepper
- Added a progress bar with per-file status
- Parallelized VO pre-transcription
- Added a word search panel for browsing transcribed content

---

## April 3, 2026

### Standalone Transcribe Media workflow
Added a dedicated workflow for transcribing media files outside of a reconcile session. Includes a word search panel, progress feedback, and a fast path that reuses cached transcripts for VO clips already processed in a prior reconcile run.

### Session routing fix
Fixed a bug where opening a saved AAF setup file would incorrectly route to the Script→Session workflow instead of the AAF workflow.

---

## April 2, 2026

### Session management
- Cross-machine session handoff: opening a session from a different OS prompts for video/audio folder locations and remaps all paths automatically. Separate pickers for video and audio folders
- Fixed session open/save routing bugs for the AAF→XML workflow
- Sync offset is automatically reset when a video file is reassigned to a source

### Caching
- Disk cache for `detect_rx_offset` results — sync detection is skipped on re-runs when the source file hasn't changed
- Disk cache for `_probe_duration` — file duration probing no longer re-runs ffprobe on every build
- All auto-generated cache files moved into `.pb_cache/` to keep source directories clean

### AAF build
- Build runs in a background thread — the UI remains responsive during long exports
- Skips AV offset detection for AAF builds (not needed; handled by sync workflow)
- Temporal ordering constraint added to interview pull reconciliation — clips that appear in the wrong script order are flagged

### Match review and diagnostics
- Auto-snap: IN/OUT points snap to the nearest silence boundary when the waveform editor opens
- Progressive waveform loading: Phase 1 shows a fast preview window; Phase 2 loads the full file in the background
- Session diagnostic log includes matched text, timeline positions, and ordering violations
- Diagnostic output gated behind `DEV_DIAGNOSTIC` flag — off by default in production builds

### Audit fixes (April 1)
Addressed thread safety issues, AAF timeline position calculations, resource leaks in temp file handling, and dead code across multiple modules.

---

## March 29–30, 2026

### Sync algorithm development
- Multi-stage cross-correlation pipeline: Stage 1 (coarse, 1 Hz envelope) → Stage 2 (fine, 50 Hz) → Stage 3 (precision, 2 ms)
- QA pass with per-file feedback loop and confidence scoring
- Match review dialog: interactive waveform editor for manual IN/OUT adjustment, cut-point editing, silence snap, playback
- Signal verification step: after a user accepts an offset, a Stage-3 xcorr is run and logged to build a ground-truth calibration dataset
- Multi-peak Stage 1 tested and reverted — single-candidate Stage 2 produced better overall results

---

## March 19–28, 2026

### AAF→XML sync workflow overhaul
- Full sync workflow redesign: per-source video assignment, sync detection, manual alignment, lock/unlock
- Waveform sync preview dialog with zoom, pan, drag-to-adjust offset, and audio playback
- Fixed v_offset sign convention — positive offset now correctly means camera started before DAW
- Fragment detection for clips that span multiple recording files
- Resizable/reorderable columns in the source grid
- Source rows color-coded by sync state (unsynced / auto-synced / manually confirmed / locked)
- Color coding extended into dropdown widgets

---

## March 18, 2026

### Initial release
- Script→Session workflow: load PostBridge-formatted script, assign media, reconcile via Whisper, review in Step 4, export AAF or XML
- AAF→XML workflow: load Pro Tools AAF, assign and sync video, export Premiere-compatible XML
- Cross-platform: Windows (x64) and macOS (Apple Silicon + Intel via Rosetta 2)
- GitHub Actions CI: automated Windows and macOS builds
- Navigation pinned to bottom of screen for visibility with large file lists
