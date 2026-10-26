# PostBridge Changelog

---

## October 26, 2026

### Script → Session: plain scripts cut a narrator's raw read (Gunfighters)
A script with no markers (title, paragraphs, underscore rules between sections) now loads as a plain narration script instead of being refused. Step 2 offers one target, **VO: Narration**, that every audio file takes; reconcile (`monolog.py`) lines the whole script up against the whole raw read and keeps the last full reading of every stretch. False starts, retakes, stumbles and talk between takes are cut. Lines reworded as they were read, ad-libs read straight through and pickups recorded after their paragraph are kept, the pickup spliced in place. Edit points come from the waveform: in ~0.05 s ahead of the voice, out ~0.6 s after it dies away, and silences over 2 s inside a reading are trimmed to ~1 s. Lines never found come up as no-match rows.
- Scored against the finished Ep 6 edit (98.9 min of raw read, 35.0 min kept): PostBridge keeps 35.7 min. Its cut holds 99.3% of the words the edit kept, and 98.6% of its words are in the edit, with edit points within 0.04 s of the editor's at the median. The remaining differences are mostly editorial trims and choices between readings.
- Assembly view: a ✂ badge under the track wherever the cut skips tape, showing how much went; hover it to read what was said there. Pulls with more than one reading show `reading n/N`; ↑ / ↓ swaps the reading, with undo (Step 4's undo now restores which reading a VO pull uses).
- Episodes launch a plain script's narrator recordings already assigned to VO: Narration.
- `monolog_bench.py` scores a run against any finished edit's AAF → XML export.

### AAF → XML: Step 2 stops hanging
Measured on a 16-source episode with a 600-clip AAF; every number is from that session.
- **Opening from the episode** read the AAF twice in a row on the UI thread, about two seconds each with the window frozen. It is now read once, in the background, while the AAF → XML button reads OPENING AAF…; the read is kept while the file is unchanged, so Step 2 and the setup pre-fill share it. Launch to Step 2: 3.7 s → 2.0 s, and the window never freezes for more than about 0.4 s.
- **Moving around Step 2.** Every SOURCE ASSIGN / SOURCE SYNC switch, lock, accept, ALIGN accept, undo and reference change tore the page down and rebuilt it, about 650 ms each. Each page is now kept while the other one shows, and only the rows whose state changed are redrawn. A redrawn row is laid out before it replaces the old one, so nothing else on the page resizes; each resize used to repaint every widget on the page. Tab switch: 650 → ~200 ms. Lock, accept, undo: 670 → ~50 ms. Results landing during SYNC ALL update their rows in place.
- **A sync that finished after its row was redrawn** (a tab switch or another row's lock while it ran) crashed partway through applying its result. The row was left with an offset but no state, so ACCEPT did nothing. It now lands on whatever row is showing, and a redrawn row keeps spinning until its result arrives. Pressing SYNC ALL twice no longer runs a source's sync twice.
- **ALIGN on long recordings.** Every redraw rescanned every sample of both recordings, which is 64 million for a 2 h 14 min Riverside file, so each drag step took 40 ms. The waveform is now drawn from a min/max peak pyramid built once on load: 4 ms per redraw, 7 ms per drag step.
- **Error messages that never appeared.** A failed SYNC now says why in a dialog instead of only showing "⚠ error". The same goes for a failed PREVIEW, a failed ALIGN load (which sat on "Extracting waveforms…" forever), a failed waveform-editor load and a failed episode folder scan. Each handler referred to the exception after Python had unbound it.
- Auto-match now picks a source's reference audio on SOURCE ASSIGN too, not only once SOURCE SYNC has been drawn.

## October 21, 2026

### Assembly view: Smart Tool, command bar, speaker colours, monitoring
- **The mouse works like Pro Tools' Smart Tool.**
  - In a pull's top half (text cursor), a click places the insertion point and a drag highlights a span.
  - In its bottom half (four-way move cursor), a click selects the whole pull and a drag moves it and everything after.
  - The edges trim, as before.
  - Shift-click extends a highlight. Delete takes out what is selected; a whole pull selected that way is IGNOREd.
  - With a span highlighted, Space plays just that span.
- **A command bar across the top is the window's key.** Every action is a button showing its key, grouped as Play, Monitor, Edit, Spot and View, with a line spelling out the mouse zones.
- **Each speaker has its own colour.** VO and the host keep theirs, and each interview token gets one in alphabetical order, with a colour key at the right of the bar. A pull Step 4 still flags keeps an amber strip and label over its speaker's colour.
- **Follow playhead (S).** It can be turned off, so the view stays put while you edit during playback. It never scrolls in the middle of a drag or while you're naming a mark.
- **Pre-roll (P).** Its length is set with the − / + beside it (0.5–10 s). Playback starts that far ahead of the insertion point or highlighted span. After any edit, the insertion point lands on the new seam, so play with pre-roll runs straight into it.
- **Return to start on stop (N).** When it is on, stopping goes back to the insertion point. When it is off, the playhead stays where it stopped; pressing **0** (stop) a second time goes back to where play started, as in Cubase. Clicking while the cut plays sets the insertion point (drawn dashed) without jumping playback.
- **R and T zoom out and in**, as in Pro Tools.
- The monitoring choices and the speed are kept for the rest of the run.

### Assembly view (phase 3: spotting, and three marker tracks in the AAF)
While the cut plays, **I** and **O** drop a music cue's in and out at the playhead and **M** an SFX spot. The marks sit on their own row of the ruler, music in blue with a bar from each in to its out, SFX in pink, and appear in the script after the pull they land on, which makes the script the spotting list. Drag a flag to move it, double-click it to name it in place, select it and press Delete to remove it. Unnamed marks are counted in timeline order (`Cue 3 in` / `Cue 3 out`, `SFX 7`); a named cue's out takes its name. Each mark lives on the pull it was dropped on, pinned to that moment of the source audio (or to a point in the gap after the pull), so trims and retiming carry it along with its word. It is saved with the pull, shares Step 4's undo, and survives BACK and re-reconcile. The AAF now ends with three marker tracks: **PARTS** as before, **MUSIC** and **SFX**, each a named silent clip per mark, the same form Pro Tools already shows for PARTS. A mark closer than two seconds to the next one on its track is cut short to fit, rather than pushing the rest out of time. Placement comes from one function, `engines.place_markers`, used by both the view and the export. Marks on an IGNOREd pull drop out with it and come back if it is restored.

### Assembly view (phase 2: the track is the editor)
Edits happen on the track itself. When the right edit point may lie outside what was pulled, double-click the pull (or select it and press Enter) to open Step 4's waveform editor on its whole source file; accepting there redraws the cut. Drag a pull's edge, or either side of a cut inside it, to trim it, shuffle-style: the pull's start stays put and everything after moves up or back. Trims can open a pull into three seconds of handle without waiting for a reload, and stop at the end of the file. Drag a pull, or the gap in front of it, to change that gap; `,` and `.` nudge it a frame (`<` `>` ten). Shift-drag across part of a pull and press Delete to take it out, which is how a pause gets tightened; marking all of it IGNOREs the pull. Every edit commits exactly as Step 4's own does, through the card's accept (so the card shows as adjusted and accepted) or the pull's gap after, and shares Step 4's undo: Ctrl+Z / Ctrl+Shift+Z work in either place. The status line reads out the edit point's source timecode and the change while you drag.

### Assembly view (phase 1: look and listen)
**ASSEMBLY** on Step 4 opens the reconciled episode as one continuous track, the script above it and the cut below, over-under. Every placed pull sits exactly where the AAF puts it: placement now comes from one function, `engines.layout_timeline`, which `build_aaf` also uses (verified clip-for-clip identical on a 181-pull episode). The track plays as the same single reference Step 4 does, an interview pull from its transcription mix and a VO pull from its take, with the shared speed control (`[` / `]`, 0.75× to 3×). Transcript words are drawn on the waveform and the word under the playhead lights up. The script follows the playhead, and clicking a pull in either pane jumps there. Part starts are flagged on the ruler, gap lengths are shown under the track, and pulls Step 4 still flags are amber. Any Step 4 change redraws the cut. Space plays and stops, ← → step between pulls, `+` / `−` / `F` zoom, Ctrl+wheel zooms at the mouse. Audio loads per pull in the background with two seconds of handle either side, nearest the view first; a 57-minute episode loads in about five seconds and holds about 300 MB.

## October 19, 2026

### Google Docs bridge
The script stays in Google Docs. From the episode's Script → Session stage: **LINK GOOGLE DOC…** (Google's picker, `drive.file` scope only), **FETCH & CHECK** (the Doc as the episode's script, version-stamped, Script Check run, pulls added/removed/changed since the last fetch counted), **WRITE NOTES INTO DOC** (each finding as a highlighted `// ⚠ PostBridge:` line under its line, replaced on the next write, removable with CLEAR NOTES IN DOC) and, in a Pull Quotes session opened from the episode, **SEND TO SCRIPT** (the selected pull appended to the Doc). Launching Script → Session fetches first. Setup is a Cloud project, an Internal consent screen, a desktop OAuth client and a picker key; the README says how.

## October 14, 2026

### Episodes: one project file for every workflow
Choose the project folder once. PostBridge looks through it, folds Pro Tools copies onto the originals, puts music, effects, renders and caches aside with the reason, finds the stereo mix, groups recordings into sources (multi-mic sessions by running time and name) and attaches the cameras that rolled on each one. Every decision is reviewable; tokens, cameras and exclusions survive a rescan. The episode file lives at the project root with relative paths, so the folder can move. From it, Pull Quotes, Script → Session and AAF → XML open already linked: the pool, the token assignments, the per-source cameras, reference audio and mix are in place, and each stage's own saved file is remembered and reopened. The stages write back too: a Pull Quotes session registers its token and recordings as a source; Script → Session's pool assignments move cameras to their token's source and remember each narrator recording's `VO:` part, which the next script version opens with (a single narrated part needs no history); an AAF setup save keeps each source's cameras, sync and track row in the episode, so a fresh AAF of the same session opens synced without the setup file. Pull Quotes opened from an episode shows **← EPISODE** and **+ FROM EPISODE SOURCE**, which makes a session from a recording the episode already knows. Existing per-workflow files are untouched.

### AAF → XML: custom track map
A fourth **Video tracks** choice. Each camera (source × slot) names the video track it goes to; cameras on the same video track share its clip-audio and camera-audio tracks. The default sends slot 1 of every source to Video 1, slot 2 to Video 2, and so on, so one narrator shot on the same three cameras over two days collapses from 6 video and 8 audio tracks to 3 and 4 without touching the map. Saved with the setup. The export-complete screen also gained **BACK TO SETUP**, and the Video tracks choice now survives Back / Next.

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
