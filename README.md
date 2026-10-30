# PostBridge

> **Portfolio note:** PostBridge was built as internal tooling at MeatEater and remains their property. It's shared here publicly as a portfolio piece — not licensed for reuse or redistribution.

PostBridge is a desktop tool for post-production teams that reconciles interview transcripts against recordings and exports editor-ready timelines — either as Pro Tools AAF sessions or Premiere/FCP-compatible XML.

Built for **Windows** and **macOS** (Apple Silicon + Intel via Rosetta 2).

---

## Download

Pre-built app bundles are produced manually via [GitHub Actions](https://github.com/JakeDBirch/PostBridge/actions). Three independent workflows — pick the one you actually need so you don't burn artifact storage on builds you'll never download:

1. Go to the **Actions** tab and pick a workflow from the left sidebar:
   - **Build PostBridge — Windows (CPU)** → Windows (x64), CPU only, ~1.2 GB
   - **Build PostBridge — Windows (GPU)** → Windows (x64), NVIDIA CUDA bundled, ~4 GB
   - **Build PostBridge — macOS (Apple Silicon)** → macOS M1/M2/M3, ~1.2 GB
2. Click **Run workflow** → wait for the green check
3. Open the completed run and download the artifact from the **Artifacts** section

No Python installation required — everything is bundled. Each workflow ships its build only; nothing else runs in parallel.

**Which Windows build do I want?**

| You have... | Download |
|---|---|
| NVIDIA GPU (RTX 30/40/50 series, or older with CUDA 12.8 support) | `PostBridge-windows-gpu` — Whisper runs ~5–15× faster |
| AMD / Intel GPU, integrated graphics, or no GPU | `PostBridge-windows` — works fine on CPU |
| Not sure | Start with `PostBridge-windows`; switch to the GPU build later if reconcile feels slow |

**Installing on Windows:** each Windows run also produces an installer artifact (`PostBridge-windows-installer` / `PostBridge-windows-gpu-installer`). Unzip the download, run `PostBridge-Setup.exe` (GPU: `PostBridge-GPU-Setup.exe`, keeping the `.bin` files next to it), and PostBridge installs like any other app, with a Start menu entry, an optional desktop icon and an uninstaller. No admin rights are needed. The plain zip artifact is still there; if you use it, run `PostBridge.exe` from where you unzipped it, because copying the extracted folder elsewhere can silently drop files.

The GPU build is a drop-in replacement — same data files, same session JSONs, same UI. The only difference is what hardware it uses for inference. PostBridge auto-detects CUDA at startup; if torch fails to load, it silently falls back to CPU.

> **macOS first launch:** Right-click the app → **Open** → click **Open** in the dialog. This bypasses Gatekeeper for unsigned apps and only needs to be done once.

---

## What it does

PostBridge handles four workflows:

| Workflow | Use when |
|---|---|
| **Pull Quotes** | You have raw interview audio and want to read the transcript, highlight quotes, and copy them straight into your script with precise timecodes baked in. |
| **Format Script** | You have a draft script written in any format and want help converting it to PostBridge markup — block builders, token lists, an AI prompt for ChatGPT/Claude. |
| **Script → Session** | You have a formatted script with marked interview pulls and want PostBridge to find each quote on tape and assemble an editor-ready timeline — or a plain narration script and the narrator's raw read, to be cut down to the script. |
| **AAF → XML** | Pro Tools audio is locked. You need to hand it off to Premiere/FCP with the correct video synced underneath. |

The four workflows share transcript caches — a session transcribed in Pull Quotes is instantly available to Script → Session reconcile, and vice versa.

---

## GPU acceleration

On Windows with an NVIDIA GPU, PostBridge runs Whisper on the GPU instead of the CPU — typical speedup is **5–15× on a small/medium model**.

**Easy path:** download the `PostBridge-windows-gpu` artifact. Everything is bundled — no Python install required.

**Developer path (running from source or building your own bundle):** install a CUDA-matched PyTorch wheel before launch. For RTX 30/40/50 series:

```bash
pip install torch --index-url https://download.pytorch.org/whl/cu128
```

For older GPU families, pick the matching CUDA wheel index from [pytorch.org](https://pytorch.org/get-started/locally/). Without PyTorch installed, PostBridge falls back to CPU silently.

Step 3 (reconcile) shows a live resource monitor — GPU utilization, VRAM, and free RAM — so you can see at a glance whether CUDA is engaged. Click the ⓘ for a key.

---

## Episodes: one project for every workflow

The three workflows below each used to keep their own file and their own media links, so every stage began by rebuilding what the last one already knew. An **episode** is the one file they share. It lives at the root of the show's project folder as `<Title>.pb_episode.json`, every path inside it is relative to that folder, and it can move between machines with the folder.

### Choose the folder once

From the home screen pick **Episode** and choose the project folder — any folder; the `02_MEDIA` / `03_AUDIO` layout is not required. If an episode file is already there it opens; otherwise PostBridge looks through the folder once and sorts what it finds:

- **Recordings and footage** become candidate sources. Running times are measured; nothing is hashed in full.
- **Pro Tools copies** — the session's `Audio Files` folder holds a copy of every recording ever imported, plus every music cue and effect auditioned. Copies of a recording that exists elsewhere are folded onto the original. Everything else in that folder is listed, with its length, for a one-click **USE AS SOURCE**; it is never assumed to be a recording.
- **Music, effects, renders, caches** are put aside by folder name (`MUSIC`, `SFX`, `STEMS`, `Bounced Files`, `FINAL_ASSETS`, `.pb_cache`, backups, …) and by file name (library catalogue codes, `_STEM_(`, `_APM`, `SFX_`, Freesound). Clips under 60 s are put aside as too short. All of it stays visible under **PUT ASIDE** with the reason.
- **The stereo mix** is found by name or folder (`FOR VID`, `MIXED AUDIO`).
- **Sources** are proposed from the recordings: files with the same running time and a shared name are one multi-mic session; footage is attached to the source whose running time it matches (within 8 %) and whose name it shares, several cameras per source; a Pull Quotes session whose media match gives the source its token.

### The episode screen

Every decision is shown for review. Each source has its token, its recordings and its cameras; cameras can be attached or detached, recordings promoted to sources, anything put aside or restored. **RESCAN FOLDER** picks up new files while keeping your tokens, cameras and exclusions. **SAVE EPISODE** (or Ctrl+S) writes the file at the project root.

### Stages

The **STAGES** card opens each workflow already linked:

- **Pull Quotes** opens the episode's interview sessions, with **← EPISODE** to return. **+ FROM EPISODE SOURCE** lists the recordings the episode knows that have no session yet, each with a suggested token, so a session is one click rather than a file dialog. Saving a session puts it in the episode file and registers its token and recordings as a source (or updates the source that already owns those recordings).
- **Script → Session** asks for the script the first time, then opens Step 2 with every source's recordings and cameras in the pool, assigned to the source's token where the script uses it. Narrator recordings — sources with no token and no transcript — open assigned to `VO: <part>` when the script narrates a single part, and to whichever part they were given last time once that has been saved. Saving writes the pool's assignments back: a camera assigned to a token joins that token's source, a recording assigned to a token gives its source the token, and a `VO:` assignment is remembered on the source as its narrated part. The whole reconcile is saved in the episode file and reopens from there next time.
- **AAF → XML** asks for the AAF the first time, then opens Step 2 with the video pool, the per-source camera assignments and slot counts, each source's reference audio and the stereo mix already in place — only SYNC remains. Saving writes the setup into the episode file, and each source's cameras, sync (offset, state, lock, drift) and track row with the source. A later AAF export of the same session — a re-cut, or a fresh file on another machine — opens with those syncs in place, as long as the same camera and recording are still in play.

### One file

The episode file is the project. **SAVE** (Ctrl+S) on any stage opened from an episode amends `<Title>.pb_episode.json`: no dialog, and no other JSON of its own. **SAVE AS** saves the whole episode under a new name. A stage also goes in by itself when you step back to the episode or Home, and when a reconcile finishes. Paths under the project folder are stored relative to it, so the folder can move between machines.

A script or AAF counts as the episode's when the episode links to it or it sits inside the episode's folder, including one opened from Home: a first save finds the project's episode without it being open. Work from outside the folder keeps a file of its own.

An episode saved before this reads the `session.json`, `*_setup.json` and `.pb_session.json` files it linked to into itself the first time it opens, following the project if it has moved since. Those files are left on disk, unused, and safe to delete. `.pb_transcript.json` sidecars (transcript caches, next to the audio) and the `.pb_cache` folder (per-edit recovery and telemetry) are still written: they are caches, not the project.

Per-workflow files (`*_session.json`, `*_setup.json`, the old Pull Quotes `.pb_episode.json`) keep working as before when opened with no episode in the project.

### Google Docs

The script can stay in Google Docs, where the writing, comments and revision history live, and PostBridge works with it there.

- **LINK GOOGLE DOC…** on the Script → Session stage opens Google's picker; choose the episode's script Doc once.
- **FETCH & CHECK** pulls the Doc as text into the episode's script file, stamped with its Drive version, and runs Script Check. The findings window shows errors, warnings and notes, and how many pulls were added, removed or changed since the last fetch. Launching Script → Session fetches first, so the script that reconciles is always the current one.
- **WRITE NOTES INTO DOC** puts each finding into the Doc as a highlighted `// ⚠ PostBridge: …` line directly under the line it concerns. The script parser ignores `//` lines, so a note left behind never breaks a reconcile. The next write replaces PostBridge's own notes and leaves everything else alone; **CLEAR NOTES IN DOC** removes them. These are ordinary edits, attributed to whoever signed in — the Docs API cannot make suggestions.
- **SEND TO SCRIPT** in a Pull Quotes session (opened from the episode) appends the selected pull block to the Doc, so pulls travel without the clipboard.

**Setting it up** (once per team, in the Google Cloud console with a work account that may create projects): a project with the Google Drive, Docs and Picker APIs enabled; an OAuth consent screen of type Internal with the single scope `drive.file`; an OAuth client of type Desktop app, downloaded as `google_client.json`; and an API key restricted to the Picker API, saved as `google_picker_key.txt`. Both files go in PostBridge's app-data folder (next to `main.py` when running from source). The first use opens the browser for a one-time sign-in per machine. `drive.file` means PostBridge can only reach Docs a person picked for it, never the rest of Drive; if the Workspace restricts third-party apps, the admin allowlists the client for that scope.

## Workflow 1: Pull Quotes

For browsing raw interview audio and lifting quotes straight into a script.

### Project layout

Pull Quotes auto-discovers an editorial project root by walking up from your script file looking for `02_MEDIA` and `03_AUDIO` siblings. Inside `03_AUDIO/00_RAW AUDIO/`, each interview gets its own session file (`<TOKEN>.pb_session.json`).

### Step 1 — Create or open a project

From the home screen, choose **PULL QUOTES**. Drop a folder, open an existing project JSON, or start a new one.

### Step 2 — Add interview sessions

Each session is one or more audio files representing a single interview. Multi-track sessions (separate mics per speaker) get auto-diarized — each speaker labelled.

Click **TRANSCRIBE** to run Whisper on the audio. If a cached transcript already exists alongside the source audio (`.pb_transcript.json`), PostBridge prompts to **USE EXISTING** or **RE-TRANSCRIBE**. When re-transcribing, a *Preserve N edited words* checkbox keeps any prior user edits — they re-appear in place as the new transcription reaches their timestamps. While it runs the button reads **■ CANCEL TRANSCRIPTION**, and the session's row in the project list has **■ CANCEL**: either stops the run within a few seconds and puts back the transcript the session had before it.

**Progressive draft pass:** while you're viewing a session, PostBridge runs a tiny model first to get text on screen within seconds, then the configured (`small` / `medium` / `large-v3`) model refines on top. On GPU the two passes run in parallel; on CPU sequentially. Draft words render in muted grey; refined and user-edited words render solid. You can read and edit during the refine — your edits stick.

The transcript renders as paragraphs with speaker headers and supports inline editing, margin notes, and full-text search (press the 🔍 toggle in the header to open the search bar).

### Step 3 — Lift quotes

Select text in the transcript and right-click to:

- **Copy as Pull** — clipboard gets a script-ready block: `[TOKEN HH:MM:SS-HH:MM:SS]\n"<quote>"`
- **Play selection** (Space) — auditions the audio; a warm orange highlight follows the playhead word-by-word and auto-scrolls to keep it in view
- **Add margin note** (Ctrl+M) — annotate a passage without affecting the script output
- **Edit selected text…** — fix Whisper transcription errors in place. Edited words are marked with a thin underline as a tracked-changes indicator
- **Restore Whisper original** — appears when the selection contains user-edited words; one click splices the original Whisper text back in

Paste straight into your PostBridge script. Timecodes are Whisper-precise — no further reconciliation needed for those pulls.

---

## Workflow 2: Format Script

For when you have a draft script in plain prose, a Google Doc, a Word file, etc., and need to convert it into PostBridge's bracketed markup before reconciling.

From the home screen, choose **FORMAT SCRIPT**. The view is a set of cards:

### Format reference

Shows the directive cheat sheet inline — `[PART …]`, `[VO …]`, `[<TOKEN> HH:MM:SS-HH:MM:SS]` — with examples of how the body text flows under each header. Useful when you're writing from scratch.

### Copy AI Prompt

Generates a paste-ready instruction block for ChatGPT / Claude / any LLM. Paste the prompt followed by your raw script and the model rewrites it into PostBridge format. Faster than learning the syntax by hand — and the AI also catches token-naming inconsistencies that would break reconcile later.

### Episode Tokens

A multi-line text input where you list every speaker/asset token used in the episode. As you type, the cards below register chips for each token so the pull builder knows what to offer. Clicking **Copy [TOKENS] block** copies the full `[TOKENS]…[/TOKENS]` header for pasting into the top of your script.

### Block builders

Three composers for the three header types:

- **[PART name]** — type a part name, hit copy, paste at a section boundary in your script
- **[VO id]** — pick from your registered tokens, optionally paste the body text, copy the block
- **[<TOKEN> in-out]** — pick a token, enter in/out timecodes, paste the quote text, copy a complete pull block

Every "copy" button confirms with a brief ✓ Copied! marker so you can move quickly between PostBridge and your script editor.

The Format Script tool produces plain text only — no PostBridge state is saved. You assemble your script in whatever editor you prefer (VS Code, Google Docs, plain Notepad) and then save it as `.txt` for Script → Session to load.

---

## Workflow 3: Script → Session

For when you already have a script with marked interview pulls.

### Step 1 — Load Script

Drop or open a PostBridge-formatted `.txt` script. The parser validates token structure and reports any malformed blocks.

### Step 2 — Assign Media

Drop interview recordings onto the pool. PostBridge auto-detects speaker assignments from Riverside filenames. Adjust any incorrect assignments via the dropdown on each row.

### Plain scripts: cutting a narrator's raw read

A script with no markers at all — a title line, then paragraphs, the way a Gunfighters script comes out of Google Docs — is read as a plain narration script. Each paragraph is a stretch to find in the read; lines of underscores between sections are just paragraph breaks. Nothing in the script becomes a marker: the export is the VO track alone. Script Check is skipped (it checks markers), and Step 2 offers one target, **VO: Narration**, which every audio file dropped on the pool takes. Drop every raw recording of the read; they are searched in filename order, which is recording order.

Reconcile lines the whole script up against the whole read and keeps, for every stretch of script, the last time it was read in full. False starts, retakes, stumbles ("both these killing — both these killings") and talk between takes are cut; a line reworded as it was read, an ad-lib read straight through, and a pickup recorded after its paragraph are kept, the pickup spliced in where it belongs. A cut has to be earned by a retake: a short removal goes back in unless its words are said again right beside it, or the narrator stopped before saying them. Edit points come from the waveform: in just ahead of the voice, out about half a second after it dies away, and a silence over four seconds inside a reading taken down to about one. A read that carries on across a paragraph is one region and one pull, so a pull can cover several paragraphs; a new one starts where the tape breaks. Lines never found come up as no-match rows, so nothing goes missing quietly.

In the Assembly view, every place the cut skips tape shows a ✂ badge under the track with how much was taken out; hover it to read what was said there. A pull with more than one reading shows `reading 2/3` on its label; ↑ / ↓ swaps it to another reading, with undo. On Ep 6 the cut held 99.4% of the words the finished edit kept, and 98.6% of its own words were in that edit; most of the rest were editorial trims and choices of reading. `monolog_bench.py` scores a run against any finished edit's AAF → XML export.

### Step 3 — Reconcile

PostBridge picks the fastest path per token:

- **Pull Quotes session exists** → instant lookup, no Whisper
- **`.pb_transcript.json` cached next to the audio** → instant lookup, no Whisper
- **5+ pulls and no cache** → transcribes the full audio once and caches it, then looks up each pull
- **Under 5 pulls** → traditional per-pull Whisper on padded windows

**Two passes.** Anything that has to be transcribed gets the tiny model here, so Step 4 and the Assembly open straight away. The model chosen in the picker then runs in the background, one file at a time, and its words replace tiny's as they are decoded. When a file finishes, its transcript is saved labelled with that model and its pulls are matched again: a pull you accepted or reassigned keeps your cut, and your marks and gaps stay on every pull. A monolog is cut again once all its recordings are done, straight away if nothing has been touched, otherwise when you press **Update the cut**, which keeps the pulls you worked on. A session closed part way picks up the next time Step 4 opens. With tiny chosen in the picker, the one pass is all there is.

When a token has **two or more audio files**, reconcile mixes them and transcribes the mix — so a sidecar on one individual track doesn't help, and the Pull Quotes session is dropped as possibly stale. What does help is every track carrying the *same* transcript, which is exactly how Pull Quotes mirrors one when you save a session: reconcile reuses it for the mix and stamps it onto the mix for next time. A partial set (only some tracks have a sidecar) or a disagreeing set is rejected, and the mix is transcribed.

A sidecar is accepted when the media's mtime still matches it (±2 s). If the mtime drifted — copying media off a shared drive, restoring from backup, and re-saving all rewrite it — PostBridge falls back to the sidecar's baked `audio_signature` (size → duration → SHA-256) and reuses the transcript when the audio is provably identical. Sidecars written before signatures existed remain mtime-gated.

**CHECK TRANSCRIPTS (Step 2):** shows which path each token will take *before* you commit to a run — a ✓/✗ per token and VO take, the reason behind every sidecar hit or miss, and every Pull Quotes session with its signature verification. Read-only; it decodes no audio.

When something is missing, point PostBridge at it from the same window:

- **Locate transcript…** — pick a `.pb_transcript.json` or `.pb_session.json` for one file. PostBridge re-stamps it onto that media with a current mtime and signature, so whatever was causing the rejection stops mattering.
- **Adopt from folder…** — point at a folder of delivered transcripts and every file matching a media filename stem is adopted at once.

Adoption warns first if the transcript's timings don't fit the audio's duration — the usual sign it belongs to a different recording. Originals are never moved or modified.

The same report is available without opening the app, though it has to guess token→media assignments from filenames rather than reading your pool:

```bash
python check_transcripts.py "C:/path/to/script.txt"
```

A live progress log shows per-clip status. A **resource monitor** above the log shows GPU/RAM utilization. A **memory pre-flight check** warns before launch if Windows commit headroom is too tight (Whisper allocations can fail mid-run if the page file is near full).

**Script-conform editing:** when a full transcript is available, PostBridge aligns the script's quote text against the transcript and generates internal cuts for any words on tape that aren't in the script — e.g. "you know" or "um" survivors get cut automatically. Safe by design: if alignment confidence is low, falls back to single-segment match.

**Text-fallback reconcile:** PostBridge does a fuzzy word-sequence search through the cached transcript to find the quote anyway in three failure modes — syntactically bad timecodes (in > out), out-of-range timecodes (no words in the window), or **mis-pointed timecodes** (a valid range that happens to point at the wrong audio — usually a duplicate timecode copy-pasted across two different pulls). The mis-pointed case is detected by scoring how well the script quote fits the words inside the TC window; a poor local fit plus a clearly better match elsewhere triggers a snap to the better location. Any fallback lands the pull in "needs review" so you can verify.

Status badges:
- **ok** — strong match
- **low confidence** — partial match, worth reviewing
- **no match** — couldn't find the quote; may need manual assignment
- **error** — extraction failed

### Step 4 — Review

Review flagged clips. Adjust timecodes in the waveform editor. Manually accept or reject. All edits persist in the session JSON.

**Collapsible sections:** each part header is a fold control — click it to collapse that part, and the header reports how many clips are hidden. **COLLAPSE ALL** / **EXPAND ALL** sit on the sort row. Folding is remembered across rebuilds, so finishing a part and folding it away survives the next reassign or status change. It applies in Script order, where the part dividers show; the other sorts interleave parts, so there is nothing coherent to fold.

**Fixed row heights:** cards keep the same height in every state. Confirming or ignoring changes only the border and stripe colour — it does not resize the row and shift everything below it.

**Compact card header:** each row is built from as few widgets as it can be, because scroll cost tracks the widget count. The order number and token share a label; status carries its confidence; sub-clip count, timecode drift and the recorded range share one grey detail line; and one state label swaps between ACCEPTED and IGNORED. Measured on a 240-card list: 3905 widgets → 2185, and a scroll step from 54ms to 36ms. The trade is two accent colours — the order number takes the token's orange, and the clips / Δ badges go grey. Every state signal (status colour, stripe, border) is unchanged.

**FOCUS:** the toggle on the sort row keeps one part open at a time — opening a part folds the rest. Scroll cost tracks how many cards are laid out and nothing else, so this is the whole game: measured on a 184-card episode, **16.5 ms per scroll step with every part open, 0.8 ms with FOCUS on**. The setting is remembered between sessions. EXPAND ALL / COLLAPSE ALL sit beside it for manual control.

To get the numbers for your own episode, point the profiler at a saved session:

```bash
python profile_step4.py "C:/path/to/your_session.json"
```

It reports card count, widget count, and milliseconds per scroll step both expanded and collapsed. Read-only.

**Live playback:** the waveform editor streams its audio rather than rendering a file and playing it, so what you change is what you hear — immediately, with no gap and no restart:

- **Drag a cut while it plays** and the edit is audible about 45 ms later. If the playhead is inside a region you just removed, it jumps to the next kept segment.
- **Change speed while it plays** and it takes effect on the next buffer.

The − / × / + stepper covers 0.75× to 3× (or `[` and `]`; the end stops dim when the range runs out), and the speed is pitch-preserved by a real-time WSOLA time-stretch, so speech stays intelligible instead of going chipmunk. Quality holds right to the top — spectral correlation against ffmpeg's `atempo` is 0.906 at 3× against 0.913 at 1.5×. The playhead is read from the engine's own cursor, so it stays correct across speed changes and cut jumps rather than being extrapolated. Your speed carries to the next card.

This needs `sounddevice` (PortAudio). Without it PostBridge falls back to the old render-a-WAV-and-play-it path, which works on Windows only and has no live response.

Other editor shortcuts: `space` play/stop, `←`/`→` nudge IN, `shift+←`/`→` nudge OUT, `shift+S` toggle SKIP CUTS, `+`/`-` zoom.

### The Assembly's text

The text beside the Assembly's controls is the cut as words: the script's wording where it was said, with nothing else in the way. Only a difference gets a mark. Script wording that wasn't said is struck through, and words said that aren't in the script are tinted. An interview names its speaker where the speaker changes.

Highlighting the text selects the audio. Drag across words, double-click a word, triple-click a pull, Shift-click to extend; a selection can run across pulls. Delete takes it out, B splits at both ends, M / X / N spot at its start, Ctrl+C copies the words, and right-click offers all of these. Deleting or splitting across pulls is one undo step. Playback isn't held in by a highlight: Space plays from its start and on past its end. The text edits the audio, not the words: there is no typing corrections into it.

**Refine with `<model>`** in the TRANSCRIPT panel, beside its model picker, transcribes the cut's files again with the model chosen. While a pass runs, the button, a bar across the panel and the line under the track all show how far it has got.

On the track, **Home** returns to zero and **Alt+A** (or F) zooms out to the whole cut. Dragged edges and track highlights snap to where speech starts and stops, as in the waveform editor, and the ticks on the track show where; hold Ctrl to place one freely. `,` and `.` nudge a region by the amount set in the Edit panel (100 ms to start with), and `/` and Shift+`/` by the next amount up. Ctrl+click or right-click deletes a mark on the ruler. Return to start on stop (Shift+N) and a 2 s pre-roll are on by default, and Follow playhead is off.

### Step 5 — Export

Choose **AAF** or **XML** and where to save it, then export. The sequence takes the save file's name unless you retype it.

---

## Workflow 4: AAF → XML

For when audio is locked in Pro Tools and needs Premiere-compatible video sync.

### Step 1 — Load AAF

Export your Pro Tools session as AAF (**File → Export → As AAF**, **Link to Source Media**, not embedded), then drop it into PostBridge.

### Step 2 — Assign & Sync

A grid shows each source clip from the AAF. For each source:

- **Video file(s)** — assign one or more via the `[−] N [+]` counter. PostBridge probes all assigned files in parallel and promotes the highest-confidence match. To clear a wrong assignment, pick **⊘ (none — unassign)** at the top of the picker.
- **⛓ JOIN SPLIT** (pool header) — cameras split long recordings into successive files at the 4 GB FAT32 limit. This losslessly rejoins them (no re-encode) into one continuous file and adds it to the pool, so a split camera syncs and lays out exactly like an un-split one. Handles the awkward real-world cases automatically: PCM-audio sources are written to `.mov` (the `.mp4` container can't hold PCM), and the camera's timecode data track — which otherwise corrupts a stream-copy remux — is dropped. Before joining it runs a **continuity check** (using embedded timecode) that warns if a piece is missing in the middle or the pieces are out of order, and reports the joined length + start timecode so a missing first/last piece is easy to spot. After joining it cross-checks the result's duration against the sum of the inputs and flags a shortfall — the tell-tale that a segment was silently dropped.
- **Reference audio** — pre-selected for you: the AAF names, per clip, the file its source in-points are measured against, so PostBridge adds that file to the pool on load and selects it. This matters when the source is a rendered nest — its zero is wherever the nest began inside the footage, not the footage's zero, and nothing in the AAF records the difference. Syncing against a *different* recording of the same session returns a confident offset in the wrong origin: picture locks to the raw audio but misses the edit by the distance between the two files. Override it with any ISO or mix file if you need to; the build warns if the reference is not the file the AAF actually references. **⛓ MIX TO REF** (pool header) sums several synchronous tracks into one aggregate — when an on-camera *group* mic matches the sum of the individual lavs better than any single one, sync each camera against this mix and it locks cleanly. Each camera still gets its own independent offset.
- **☐ slate** — tick if production used a clapper/slate. Switches sync detection to GCC-PHAT weighting, which sharpens the slate transient peak instead of treating it as noise. Strongly recommended when available.
- **SYNC** — runs multi-window cross-correlation. The algorithm probes 3 positions through the file (25/50/75 %) to avoid being misled by setup/teardown noise at the head, and arbitrates by consistency across probes. The result is then cross-checked against an independent **onset-event histogram** (it aligns the timing of speech attacks rather than amplitude, so it stays accurate when the camera mic and reference mic have very different levels). When the two methods disagree and the histogram has a clear winner, PostBridge adopts the histogram's offset and demotes the cross-correlation pick to an audition candidate — this fixes the cases where amplitude correlation locks onto a spurious peak.

The result shows the offset, a confidence percentage, and one of:

- **✓ verified** (≥ 95 %)
- **⚠ verify recommended** (85–94 %)
- **⚠ verify required** (50–84 %)
- **⚠ low conf, must verify** (< 50 %)

When auto-sync produces runner-up candidates (alternate cross-correlation peaks), a **⏭ NEXT** button appears right on the source row — cycle through the alternates and audition each one without leaving Step 2. The button hides itself when there's nothing to cycle, so the row stays uncluttered.

Click **ALIGN** to open the waveform dialog for manual work: nudge the offset visually, audition the mix and the reference with the play buttons, and accept.

Sync results are cached to disk — re-running is fast.

**Camera-clock drift.** The three probes also measure how the camera's clock runs against the recorder's — typically single to double digits of parts per million, which is a frame or more across a long interview. When the probes agree on a straight line, the drift is kept with the offset (the SYNC button shows `· drift +8 ppm`) and applied per clip at build time, so one measured offset holds from the first clip to the last.

#### How picture is placed

Picture can only sit on sequence frames; the stereo mix is continuous. So every clip's picture lands somewhere inside a window exactly one frame wide around its true moment, and the only choice is where that window sits. Every clip is rounded **once**, against the session clock: the frame rounding its timeline position received is folded into its source in-point, so the two errors never add. (Rounding them separately, the obvious way, let them add — measured on a real export as 0 to 1.8 frames early, "one frame early" most often.) The window itself is placed the way viewers hear: sound arriving after the picture goes unnoticed far longer than sound arriving before it (ATSC allows audio to lag by 45 ms but lead by only 15 ms), so picture is never more than **15 ms late** and spends the rest of the frame early — at 23.976 that is late ≤ 0.36 frame, early < 0.64; at 29.97, 0.45 / 0.55. The limit is `PICTURE_LATE_LIMIT_MS` in `engines.py`. When a stereo mix is set, the build also measures where that bounce sits against the session clock from a handful of clips and places picture and clip audio against the mix; a bounce can carry a constant offset of a dozen milliseconds, enough to tip the rounding. The console lists, per source, the worst late, worst early and mean leftover in frames.

### Saving the setup, and re-exported AAFs

**Ctrl+S** saves everything on this screen — pool files, assignments, every sync offset and lock, hide selections, sequence settings, mix file — to a setup JSON (the first save asks where; it suggests `<aaf name>_setup.json` next to the AAF). **Open Session** on the home screen takes you straight back to Step 2 with all of it restored.

When the Pro Tools session changes after you've done this work — a notes pass, a re-cut — export a fresh AAF and press **REIMPORT AAF…** in the Step 2 header (next to UNDO / REDO / CLEAN UP). PostBridge swaps the new AAF in underneath the existing setup rather than starting over:

- Sources are matched by name. A source present in both AAFs keeps its video assignment, reference audio, sync offset, state and lock exactly as they were — nothing is re-measured.
- A source that only exists in the new AAF appears unassigned; a source the new AAF no longer has drops out, and its state goes with it.
- A summary lists what was kept, added and removed, naming the new sources that need assignment.

Quick-saves keep writing to the same setup file, now pointing at the new AAF; the window shows unsaved changes until you do. Clip names and source files must still be the ones the first AAF used — rename a clip in Pro Tools and it shows up as one source removed plus one added, not kept.

### Build XML

Click **Build XML** once all sources are synced. PostBridge generates an XMEML file with audio and video aligned; how each clip's picture frame is chosen is described under *How picture is placed* above.

**Video tracks** decides how clips are spread across tracks. Clip audio and camera audio follow the video track they belong to, so the choice sets the audio layout as well.

- **Consolidate to one video track** — packs clips onto as few tracks as overlap allows. Right for a single camera; with stacked angles it can't pack anything and the camera-to-track order is whatever frees up first.
- **Group by source video file** — one track per camera file, with ANGLE 2, 3… on their own tracks above it. The natural choice for one multicam source.
- **Group by audio track** — mirrors the Pro Tools track layout.
- **Custom track map** — a row per camera (each source × each slot) with a track picker. The default sends slot 1 of every source to Video 1, slot 2 to Video 2, and so on, so the same rig shot over several sessions collapses to one stack: a narrator recorded over two days on three cameras goes from 6 video and 8 audio tracks to 3 and 4 with no setup. Cameras on the same video track share its clip-audio and camera-audio tracks. Saved with the setup.

Each source is declared with its own audio channel count, probed from the file. This matters when the media imports offline — a project opened on a different drive, or media moved after the build. Premiere creates offline clips with whatever layout the XML claims, and refuses to relink a file whose real layout differs ("the selected file cannot be linked because it has 1 audio channel(s) and the clip was created with 2"). Mono sources — isolated participant tracks, Riverside `raw-audio` exports — are the usual casualties. If a file cannot be probed at build time, the count is left out rather than guessed, and Premiere reads it off the media.

---

## Script Format

A script with none of the markup below is read as a plain narration script; see [Plain scripts](#plain-scripts-cutting-a-narrators-raw-read). Otherwise, PostBridge scripts use a bracketed markup:

```
Episode Title

[TOKENS]
NATALIE
DAN
[/TOKENS]

[PART Cold Open]

[VO PART_1_NARRATOR]
When detectives stepped into the canvas tent, they thought the man on the
ground had been attacked by a bear.

[NATALIE 00:01:12-00:01:23]
"I was on my way home, he met me on my doorstep with dinner."

[VO PART_1_NARRATOR]
But the evidence inside pointed to someone else entirely.
```

- `[PART <name>]` — marks a new segment in the timeline
- `[<TOKEN> HH:MM:SS-HH:MM:SS]` — interview pull; the quoted text on following lines is the script body
- `[VO <id>]` — voice-over block; the text on following lines is the VO content
- `[TOKENS] … [/TOKENS]` — optional explicit token registry. Tokens are also auto-registered the first time they appear.
- Lines starting with `//` (or whitespace + `//`) are ignored as comments

Blank lines between blocks become a natural gap in the output timeline.

### Easy mistakes

- **The first line is the episode title.** It becomes `doc_title` (and the
  sequence name). Without it, the first bare line in the file is used
  instead — which, if `[TOKENS]` comes first, means your episode gets named
  after a speaker.
- **Every block of narration needs its own `[VO <id>]` header.** Prose sitting
  under a `[PART …]` with no `[VO …]` above it belongs to no block and is
  **silently dropped** — no error, no warning.
- **`[TOKENS]` lists interview speakers only.** VO ids live in `[VO …]`
  headers and must *not* be declared here; a VO id in `[TOKENS]` registers a
  phantom speaker that will then ask you for media.

---

## Session Files

All human-readable JSON:

| File | Contents | Lives in |
|---|---|---|
| `<Title>.pb_episode.json` | The whole project: sources and media, and every stage below | The project folder's root |
| `*_session.json` | Script→Session state: assignments, reconciliation results, Step 4 edits. In an episode, kept inside the episode file | Wherever you saved it |
| `*_setup.json` | AAF → XML setup: AAF path, pool files, assignments, sync offsets and locks, sequence settings. In an episode, kept inside the episode file | Wherever you saved it (suggested: next to the AAF) |
| `<TOKEN>.pb_session.json` | Pull Quotes session: media list, transcript, notes, speaker labels. In an episode, kept inside the episode file | `<project>/03_AUDIO/00_RAW AUDIO/` |
| `<media>.pb_transcript.json` | Cached transcript word list + audio signature | Next to the source audio |

The `.pb_transcript.json` sidecars are shared between workflows: transcribing in Pull Quotes populates them; reconcile in Script→Session reads them.

**Cross-machine handoff:** Session files open on a different computer. If PostBridge detects paths from a different OS, it prompts to locate your Video and Audio folders and remaps everything automatically.

**Closing safely:** closing the window with unsaved changes prompts to Save / Don't Save / Cancel (a first save behaves like Save As). If a transcription or reconcile is still running, PostBridge warns before closing so an in-flight pass isn't silently abandoned.

---

## Supported File Types

| Category | Extensions |
|---|---|
| Video | `.mp4 .mov .mxf .avi .mkv .m4v .mpg .mpeg .ts .mts .m2ts .wmv .flv .webm .ogv .3gp .dv .r3d .braw .ari` |
| Audio | `.wav .aif .aiff .bwf .rf64 .mp3 .m4a .aac .flac .ogg .opus .wma .caf` |
| Script | `.txt` (PostBridge bracketed format) |
| AAF | `.aaf` (Pro Tools, Link to Source Media) |
| Output | `.aaf` `.xml` `.json` |

---

## Settings

### Model picker (in-app)

Inline dropdowns live next to the TRANSCRIBE button (Pull Quotes session view), in the Pull Quotes project OPTIONS row, and next to the RECONCILE button (Script → Session step 2). Pick a size — it applies immediately. Click the **?** beside the dropdown for the size guide:

| Model | Params | Speed (CPU) | RAM/VRAM | Notes |
|---|---|---|---|---|
| `tiny` | 39 M | ~32× real-time | ~150 MB | Draft quality; fast first-pass on slow CPUs |
| `base` | 74 M | ~16× real-time | ~250 MB | Strong CPU choice |
| `small` | 244 M | ~6× real-time | ~600 MB | **Default** — balanced |
| `medium` | 769 M | ~2× real-time | ~1.5 GB | High accuracy; slow on CPU, comfortable on GPU |
| `large-v3` | 1.55 B | ~1× real-time | ~3 GB | Best accuracy; needs GPU to be practical |

Your choice persists across sessions (saved to `~/.postbridge_prefs.json`). On the next transcription the new model loads — first-load is slower because the weights are downloaded if not already cached. GPU acceleration multiplies all of these by ~5–15×.

**Bundled vs. on-demand:** PostBridge ships with only `tiny` pre-bundled (~75 MB) so the installer stays small enough to send via chat tools. Every other size — `base`, `small`, `medium`, `large-v3` — downloads on demand the first time you select it in the model picker, then caches locally to `~/.postbridge_models/`. Models persist across upgrades; the model picker shows a "downloading model — N MB" status the first time so you know it's not stuck. There's no background prefetch on launch — nothing downloads until you actually ask for it.

### Code-level settings

Most other settings live in `config.py` and require a code edit:

| Setting | Default | Description |
|---|---|---|
| `WHISPER_MODEL` | `"small"` | Whisper size — `small` is fastest, `large-v3` most accurate |
| `PAD_SECS` | `5` | Seconds added each side of a pull timecode for per-pull Whisper |
| `MAX_EXTRACT_S` | `600` | Maximum extraction window — guards against sentinel out-points |
| `MAX_ITEM_STALL_S` | `300` | Reconcile stall watchdog before abandoning a token batch |
| `AUTO_FULL_TRANSCRIBE_THRESHOLD` | `5` | Pulls per token that trigger full-audio transcribe (vs per-pull) |
| `FULL_TRANSCRIBE_CONCURRENCY` | `4` | Concurrent full-audio transcribes — drop to 2 on a low-VRAM GPU or 1 on CPU |
| `WHISPER_NUM_WORKERS` | `4` | CTranslate2 worker-pool size for the shared model — lets concurrent transcribe() calls share the GPU instead of serialising |
| `SCRIPT_CONFORM_ENABLED` | `True` | Master switch for auto-cut script-conform editing |
| `SCRIPT_CONFORM_MIN_RATIO` | `0.70` | Min fraction of script tokens that must align for cuts to be trusted |
| `MATCH_THRESH` | `0.55` | Minimum word-overlap score to accept a pull match (per-pull path) |
| `GAP_THRESH` | `1.5` | Silence gap that triggers a cut within a VO clip |
| `HOST_NAME` | `"jordan"` | Host speaker — shared host track in AAF output |
| `DEFAULT_GAP` | `0.6` | Inter-part gap in seconds |

In the UI, **Gap** and **Pad** spinboxes (Step 5 / AAF Step 2) are the main runtime-adjustable values.

---

## Building from Source

Requires Python 3.12+ and ffmpeg on PATH (for development). For distribution, the build scripts download static ffmpeg binaries automatically.

```bash
pip install -r requirements.txt
python main.py
```

**GPU support (optional):** install a CUDA PyTorch wheel before launch (see [GPU acceleration](#gpu-acceleration) above).

**Build distributable:**

```bash
# Windows
build.bat

# macOS
bash build.sh
```

Or trigger a build via GitHub Actions: **Actions tab → Build PostBridge → Run workflow**.

---

## Dependencies

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — speech recognition
- [pyaaf2](https://github.com/markreidvfx/pyaaf2) — AAF read/write
- [tkinterdnd2](https://github.com/pmgagne/tkinterdnd2) — drag-and-drop support
- [numpy](https://numpy.org/) — waveform processing + cross-correlation
- [ffmpeg](https://ffmpeg.org/) — media extraction (bundled in app builds)
- [PyTorch](https://pytorch.org/) — optional, enables CUDA acceleration on NVIDIA GPUs
