# PostBridge Changelog

---

## October 14, 2026

### AAF → XML: picture lands within half a frame of the mix
On a real 23.976 export every clip's picture sat 0 to 1.8 frames early against the stereo mix, most often one frame, with no drift. Two causes.

- **One rounding per clip.** The timeline position and the source in-point were rounded to frames separately, so their errors added: up to a frame, and a different frame per clip. The timeline rounding is now folded into each clip's in-point target, so every clip's picture sits inside a window exactly one frame wide around its true moment.
- **The window sits where viewers hear.** Sound arriving late is tolerated far better than sound arriving early (ATSC: audio may lag 45 ms, lead 15 ms), so the window keeps picture at most 15 ms late and spends the rest of the frame early: late ≤ 0.36 frame and early < 0.64 at 23.976, 0.45 / 0.55 at 29.97. Verified on the same export: the one clip in ten whose picture sat 0.5 to 0.64 frame late under plain rounding now sits 0.36 to 0.5 frame early, and nothing else moves. Frame arithmetic also now runs at the exact NTSC rate (24000/1001, not 23.976), which was a quarter of the window off by the 80th minute of a source file.
- **The mix's own position.** A bounce can sit a constant few milliseconds off the session clock (14 ms on this one). With a stereo mix set, the build measures that from a handful of clips and places picture and clip audio against the mix. The console reports the figure.
- **Camera-clock drift.** The three sync probes measured the camera running against the recorder (8 ppm here) and then averaged it away. The drift is now kept with the offset, shown on the SYNC button, saved in the setup, and applied per clip at build time.
- Source windows are now exactly as long as their timeline span, so Premiere has no one-frame mismatch to read as a speed change.
- The post-build console report now gives, per source, the worst and mean picture leftover in frames instead of a single per-source residual.

### AAF → XML: sync survives REIMPORT AAF and Back
Returning to Step 2 — by pressing Back then Next, or by REIMPORT AAF, which rebuilds the page — cleared every source's sync offset, state and lock while leaving the video assignments in place. The page emptied its media pool while rebuilding and refilled it last, so the first row rebuild saw no videos, reset every assignment, and the assignment-change rule wiped the sync behind it; auto-match then re-assigned the videos by name, which is why only the sync looked lost. A manual assignment auto-match wouldn't make was lost outright. The pool is now restored before the rows are built, and re-setting an assignment to the video it already has no longer counts as a change.

- **REIMPORT AAF restores onto the rebuilt page**, through the same deferred path Open Session uses, instead of onto the page about to be torn down.
- **Quick-save still targets the setup file** after a reimport. The reimport used to drop the chosen file, so Ctrl+S asked where to save again.
- **No more silent failure from a Step 1 visit.** REIMPORT after reaching Step 2 from Step 1 raised an error on the old Step 1 status label and did nothing.
- README now documents saving the setup and REIMPORT AAF.

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
