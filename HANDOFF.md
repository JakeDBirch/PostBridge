# PostBridge — Session Handoff
_Last updated: 2026-05-01_

This document covers everything built in the two most recent Claude Code sessions.
Use it to orient a new session without re-reading the full conversation history.

---

## ⭐ Most-Recent Session (2026-05-01) — Pull Quotes workflow

A new top-level workflow replaces "Transcribe Media" with a two-tier model
designed to remove friction from Jordan's quote-pulling step.

### Concepts

- **Episode Project** (`*.pb_episode.json`) — top-level container per episode.
  Stores: title + list of paths to session JSONs.  Saved explicitly via
  "SAVE EPISODE PROJECT" (file picker). `workflow="episode_project"`.
- **Interview Session** (`*.pb_session.json`) — one per interview.  Stores:
  token, list of media absolute paths, transcript word list with
  `{word, start, end}`.  Auto-saved next to the FIRST media file as
  `<TOKEN>.pb_session.json`. `workflow="interview_session"`.

### UI surface (in `main.py`)

- Home menu now lists **Pull Quotes** at the top (in pipeline order).
- `_pq_open_home()` — entry point; creates an empty in-memory project.
- `_pq_render_project_view()` — Episode Project view: title field, file path
  status line, scrollable session list (token / files / words / status),
  bottom nav with `+ ADD INTERVIEW SESSION` and `SAVE EPISODE PROJECT`.
- `_pq_add_session_dialog()` — modal with two paths: `NEW INTERVIEW SESSION`
  or `ADD EXISTING SESSION` (file picker for an existing `.pb_session.json`).
- `_pq_new_session_dialog()` — token entry + drag/drop media zone + browse +
  TRANSCRIBE button.  Auto-suggests token from filenames via
  `_pq_suggest_token()`.
- `_pq_open_session_view()` — Session view: token (read-only), media list,
  re-transcribe button, transcript Text widget, "COPY SELECTION AS @PULL"
  action button.  Bottom nav: `← BACK TO PROJECT`, `REMOVE FROM PROJECT`.
- `_pq_run_transcription()` — uses existing `engines.mix_for_transcript()`
  (multi-file) or `engines.extract_window()` (single file) → 16 kHz mono
  WAV → `engines.transcribe_clip()`.  Saves transcript words to the session
  and writes the session JSON.
- `_pq_copy_as_pull()` — bound to Ctrl+C in the transcript Text widget AND
  to the action button.  Reads the user's selection, looks up start/end
  timestamps from `_pq_word_index` (built when transcript renders), formats
  as `[TOKEN HH:MM:SS-HH:MM:SS]\n<text>` and copies to clipboard.

### Routing in `_open_session()`

After the existing AAF-setup branch, two new checks:
```python
if wf == "episode_project":
    self._pq_load_project(data, file_path=path); return
if wf == "interview_session":
    self._pq_open_standalone_session(data, file_path=path); return
```
A standalone session opens in an unsaved one-session project; the user can
"Save Episode Project" to formalise it later if desired.

### What was removed

- The entire `_transcribe_workflow()` method and its menu entry.
- The "Transcribe Media" copy in `_step3` warning message about VO cache.
- Engine-side `pb_transcript_load/save/path` functions remain (still used as
  a fast-path by script→session reconcile if `.pb_transcript.json` files
  happen to exist next to media — they're harmless).

### Token-name auto-suggestion

`_pq_suggest_token()` strips known boilerplate (`riverside`, `raw`,
`speaker-N`, digit-only fragments) from filenames, finds words common to
all files, returns the underscore-joined uppercase result.  Examples:
- `riverside_jena_speaker-0_raw.wav` + `riverside_jena_speaker-1_raw.wav`
  → `JENA`
- `David McDaniels.m4a` → `DAVID_MCDANIELS`

### File-association integration

The Windows file launcher (`open_session.bat`) added in a previous session
already handles `.pb_episode.json` and `.pb_session.json` files via the
existing `_open_session(path)` CLI arg path — no extra wiring required.

---

## Where to Find Run Logs

PostBridge writes two kinds of log on every reconcile run.

### 1. Debug mirror — always in the repo folder
```
F:\PostBridge\PostBridge_Modular\_debug_run.log
```
This file is **overwritten at the start of every run**, so it always contains the
most recent reconcile. It is the fastest way to look at what just happened.
Read it with the `Read` tool at that exact path.

### 2. Timestamped run log — placed next to the script file
```
{script_folder}\{script_basename}_run_{YYYYMMDD_HHMMSS}.log
```
For example, if the script is at:
```
C:\Users\jaked\Documents\Scripts\MyEpisode.txt
```
the log will appear as:
```
C:\Users\jaked\Documents\Scripts\MyEpisode_run_20260427_121358.log
```
These accumulate alongside the script. Use `Get-ChildItem` with `-Filter "*_run_*.log"`
to find the most recent one if the script location is unknown.

### Log format quick reference
```
[TOKEN] mixed N tracks → _pb_mix_xxx.wav   ← multi-track mix created
[TOKEN] starting N pull(s)  src=filename   ← reconcile starting for token
pull #NN 'TOKEN' → ok  (Xs)               ← individual pull result
#NNN  TOKEN  ok  conf:NN%  [Xs]           ← per-pull summary at end
#NNN  TOKEN  snapped  conf:NN%            ← match snapped to silence boundary
  ↳ PULL cache MISS / HIT for 'TOKEN'     ← transcription cache status
────────────────────────────────────────
MATCH RESULTS   Interview ok: N  low-conf: N  no-match: N
CONFIDENCE DISTRIBUTION  90–100%: N  70–89%: N  50–69%: N  <50%: N
```

**Status values:**
- `ok` — clean match above confidence threshold
- `snapped` — matched but endpoint snapped to nearest silence boundary
- `low_confidence` — matched but confidence below threshold (shows in Step 4)
- `no_match` — no usable match found

---

## Repository

`F:\PostBridge\PostBridge_Modular\`

Key files:
| File | Purpose |
|------|---------|
| `main.py` | App entry point, App class, all workflow steps |
| `engines.py` | Audio/video processing: transcription, reconcile, sync, mix |
| `gui_components.py` | MediaPool, VoBin, _FlatDropdown, _SlimScrollbar widgets |
| `match_review.py` | Waveform editor (Step 4 clip review) |
| `mix_test.py` | Standalone test tool for the multi-track mix feature |
| `parsers.py` | bracketed script parser ([PART …] / [VO …] / [TOKEN in-out]) |
| `config.py` | Theme colours, font constants |
| `utils.py` | Filename helpers, similarity, token suggestion |

---

## Features Built This Session

### 1. Auto-mix for transcript (multi-track tokens)
**Status: Complete and working.**

Instead of requiring the user to manually select a transcript source, the reconciler
now automatically mixes all audio files assigned to a token into a single 16 kHz
mono WAV before transcription.

**How it works:**
- `engines.mix_for_transcript(paths)` — public entry point, returns a temp WAV path
- Per-track adaptive noise gate: threshold = 10th-percentile RMS × 3.0, clamped
  [0.002, 0.30]; attack 5 frames / hold 15 / release 40 frames
- Speech-weighted level matching: measures RMS only during gate-open frames, scales
  each track to the median speech level, caps boost at 20 dB
- Peak normalisation to 0.90, then `_mix_dynaudnorm` (ffmpeg dynaudnorm filter:
  500 ms frames, 31-frame gaussian, 0.95 peak, 5× max gain)
- All helpers use `_ffmpeg_cmd()` / `_ffprobe_cmd()` (module-level wrappers)

**Mix file lifecycle:**
- Created in `_run_reconcile` background thread, stored in `self._temp_mix_files`
- **Kept alive** through all of Step 4 so the waveform editor shows the blended audio
- Cleaned up at the **start of the next `_run_reconcile`** call (not at finish)
- OS temp folder (`%TEMP%`) handles anything left over from crashed sessions

**`source_audio` field:** Points directly to the mix WAV — do NOT patch it back to
the original file. The old patch block was removed intentionally.

**`token_audio_paths` dict:** Replaces the old `transcript_sources` dict. Built in
`main.py` before launching the reconcile thread:
```python
asgn = self._pool.get_assignments()
token_audio_paths = {
    tok: [p for p in asgn.get(tok, []) if not is_video(p)]
    for tok in self.tokens
}
```

---

### 2. Transcript source UI removed from MediaPool
**Status: Complete.**

The old "TRANSCRIPT SOURCES" section (per-token dropdown in the media pool) is gone.
Removed from `gui_components.py`:
- `_src_vars`, `_src_dd_scheduled` instance vars
- The entire transcript sources widget frame
- `_rebuild_src_dropdowns()` method
- `_deferred_src_dd()` method
- `get_transcript_source()` method

Removed from `main.py`:
- `_build_setup_data` / `_apply_setup_data` / `_redo` / `_collect_session_paths` /
  `_remap_session_data` transcript-source blocks

Added to `gui_components.py`:
```python
def get_audio_paths(self, token):
    """Return all non-video file paths assigned to token."""
    asgn = self.get_assignments()
    return [p for p in asgn.get(token, []) if not is_video(p)]
```

---

### 3. X key shortcut — Step 4 IGNORE toggle
**Status: Complete.**

Pressing X on the keyboard toggles the IGNORE state of the most-recently-clicked
card in Step 4 (match review).

In `main.py`:
```python
def _s4_x_toggle_ignore(self, event=None):
    focused = self.focus_get()
    if isinstance(focused, (tk.Entry, tk.Text)):
        return
    fn = getattr(self, "_s4_active_toggle", None)
    if fn is not None:
        fn()
```

In `_hdr_click` (Step 4 card header click handler):
```python
def _hdr_click(event=None, sv=skip_var, fn=_open_review, ti=_toggle_ignore):
    self._s4_active_toggle = ti
    if not sv.get():
        fn()
```

Bindings added after undo/redo:
```python
self.bind_all("<x>", self._s4_x_toggle_ignore)
self.bind_all("<X>", self._s4_x_toggle_ignore)
```

---

### 4. Waveform editor — transport fixes
**Status: Complete.**

Two bugs introduced by the region-slip feature:

**Click to move cursor was broken:**
- `_on_press` was immediately setting `_drag = "slip"` on any click inside the region
  body, intercepting playhead placement.
- Fix: `"slip_pending"` state; promotes to `"slip"` only after mouse moves > 4 px.

**Click during playback didn't resume:**
- `_on_release` for `slip_pending` moved the playhead but didn't restart playback.
- Fix: full `was_playing` / `_play()` / `_pre_play_pos` logic added to
  `_on_release` slip_pending handler.

Constants: `_SLIP_DRAG_PX = 4` (line ~85 of match_review.py)

---

### 5. Waveform editor — openable files fix
**Status: Complete.**

Multi-track files couldn't be opened for editing because `source_audio` pointed to
the deleted temp mix WAV. Now the mix WAV is kept alive (see §1), so the editor
opens it directly and shows the full blended audio. The old patch that replaced
`source_audio` with `paths[0]` has been removed.

---

### 6. Waveform editor — cursor on handles
**Status: Complete.**

`_on_hover` in `match_review.py` already showed `sb_h_double_arrow` for the outer
IN/OUT handles. Added the missing interior segment cut boundary check (mirrors
`_on_press` hit-test logic):

```python
# Interior segment cut boundaries (CUT IN / CUT OUT lines)
n = len(self._segments)
for i in range(n):
    if i > 0:
        px = self._t_to_px(self._segments[i][0])
        if abs(event.x - px) <= _MARKER_HIT:
            self._cv.config(cursor="sb_h_double_arrow")
            return
    if i < n - 1:
        px = self._t_to_px(self._segments[i][1])
        if abs(event.x - px) <= _MARKER_HIT:
            self._cv.config(cursor="sb_h_double_arrow")
            return
```

---

### 7. Duration-based token linking in MediaPool
**Status: Complete.**

Files with identical run times (rounded to nearest second) auto-link in the media
pool. Changing one's token mirrors the change to all linked files. If a
passively-mirrored file is subsequently changed by the user, its link breaks and it
becomes independent.

**How it works:**

1. **Detection** — `_get_dur_key(path)` (module-level in gui_components.py):
   - Fast path: Python `wave` module for `.wav` files
   - General path: ffprobe JSON output for all other formats
   - Returns `str(round(dur_secs))` or `None`
   - Called in a daemon background thread per row in `_add`

2. **`rec` dict fields** — each row now has:
   - `link_id`: `str` (duration key) or `None`
   - `link_mirrored`: `bool` — True if this row was passively propagated to
   - `link_lbl`: the `↔` tk.Label widget (packed/unpacked by `_update_link_visuals`)
   - `rm_lbl`: reference to the `✕` button (used as `before=` anchor for pack order)

3. **Visual indicator** — `↔` badge between the dropdown and `✕`:
   - **ACCENT colour**: row is a link source (user changed it, others followed)
   - **SUB colour**: row was passively mirrored
   - Hidden entirely when fewer than 2 rows share a `link_id`

4. **`_on_token_change` guards** — link logic fires only when:
   - `_in_mirror` is False (not in filename-similarity mirror)
   - `_link_propagating` is False (not already propagating)
   - `_bulk_loading` is False (not restoring a session)

5. **Methods added to `MediaPool`:**
   - `_set_link_group(path, dur_key)` — main-thread callback from bg thread
   - `_handle_link_change(path, new_token)` — routes to unlink or propagate
   - `_propagate_link_change(path, new_token)` — mirrors token to all linked rows
   - `_update_link_visuals()` — show/hide `↔` badges

6. **`_mirror_assignment` guard** — the existing filename-similarity mirror now also
   skips when `_link_propagating` is True (prevents the two systems fighting).

---

## Key Technical Rules

- **ffmpeg/ffprobe**: always use `_ffmpeg_cmd()` / `_ffprobe_cmd()` from engines.py,
  never call `get_ffmpeg_cmd()` directly or use bare `['ffprobe']` in engines.
- **UI thread safety**: background threads must not call `self.after()` directly on
  Python 3.14+. Use `self._ui_q.put(cb)` (App queue) OR, for widget-level callbacks
  from gui_components, use the widget's own `self.after(0, cb)` — this is acceptable
  for MediaPool background threads since MediaPool is always on the main window.
- **`_in_mirror` / `_link_propagating`**: both use `getattr(self, "_in_mirror", False)`
  pattern — not set in `__init__`, only set locally within their respective methods.
- **Mix WAV lifecycle**: do NOT delete mix files in `_finish_reconcile`. They are
  deleted at the start of the NEXT `_run_reconcile` call.
- **No WM_DELETE_WINDOW handler**: a `protocol("WM_DELETE_WINDOW", ...)` override
  was tried and caused the GUI to fail to open on the user's machine. Do not add one.

---

## Known Issues / Pending

- **None at time of handoff.** All features described above are complete and tested
  by the user (unless noted otherwise in the conversation).

---

## Testing Notes

- `mix_test.py` is a standalone Tkinter tool for testing the multi-track mix.
  Run it directly: `python mix_test.py`. Has checkboxes for level match + dynorm,
  a Transcribe button (blue when enabled after a mix), and diagnostics pane.
- The waveform editor transport (click, click-during-playback, slip drag) should
  be re-verified any time match_review.py is modified.
- Duration linking: add 2+ files with the same duration to the pool; wait ~1s for
  background detection; `↔` badge should appear. Change one token; the other
  should mirror. Change the mirrored one again; its link should break.

---

## Feedback / Preferences

- **Never push to git without explicit permission** — free GitHub account, CI
  burns storage quota.
