# PostBridge

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

The GPU build is a drop-in replacement — same data files, same session JSONs, same UI. The only difference is what hardware it uses for inference. PostBridge auto-detects CUDA at startup; if torch fails to load, it silently falls back to CPU.

> **macOS first launch:** Right-click the app → **Open** → click **Open** in the dialog. This bypasses Gatekeeper for unsigned apps and only needs to be done once.

---

## What it does

PostBridge handles four workflows:

| Workflow | Use when |
|---|---|
| **Pull Quotes** | You have raw interview audio and want to read the transcript, highlight quotes, and copy them straight into your script with precise timecodes baked in. |
| **Format Script** | You have a draft script written in any format and want help converting it to PostBridge markup — block builders, token lists, an AI prompt for ChatGPT/Claude. |
| **Script → Session** | You have a formatted script with marked interview pulls and want PostBridge to find each quote on tape and assemble an editor-ready timeline. |
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

## Workflow 1: Pull Quotes

For browsing raw interview audio and lifting quotes straight into a script.

### Project layout

Pull Quotes auto-discovers an editorial project root by walking up from your script file looking for `02_MEDIA` and `03_AUDIO` siblings. Inside `03_AUDIO/00_RAW AUDIO/`, each interview gets its own session file (`<TOKEN>.pb_session.json`).

### Step 1 — Create or open a project

From the home screen, choose **PULL QUOTES**. Drop a folder, open an existing project JSON, or start a new one.

### Step 2 — Add interview sessions

Each session is one or more audio files representing a single interview. Multi-track sessions (separate mics per speaker) get auto-diarized — each speaker labelled.

Click **TRANSCRIBE** to run Whisper on the audio. If a cached transcript already exists alongside the source audio (`.pb_transcript.json`), PostBridge prompts to **USE EXISTING** or **RE-TRANSCRIBE**. When re-transcribing, a *Preserve N edited words* checkbox keeps any prior user edits — they re-appear in place as the new transcription reaches their timestamps.

**Progressive draft pass:** while you're viewing a session, PostBridge runs a tiny model first to get text on screen within seconds, then the configured (`small` / `medium` / `large-v3`) model refines on top. On GPU the two passes run in parallel; on CPU sequentially. Draft words render in muted grey; refined and user-edited words render solid. You can read and edit during the refine — your edits stick.

The transcript renders as paragraphs with speaker headers and supports inline editing, margin notes, and full-text search (press the 🔍 toggle in the header to open the search bar).

### Step 3 — Lift quotes

Select text in the transcript and right-click to:

- **Copy as @PULL** — clipboard gets a script-ready block: `[TOKEN HH:MM:SS-HH:MM:SS]\n"<quote>"`
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

A multi-line text input where you list every speaker/asset token used in the episode. As you type, the cards below register chips for each token so the @PULL builder knows what to offer. Clicking **Copy [TOKENS] block** copies the full `[TOKENS]…[/TOKENS]` header for pasting into the top of your script.

### Block builders

Three composers for the three header types:

- **[PART name]** — type a part name, hit copy, paste at a section boundary in your script
- **[VO id]** — pick from your registered tokens, optionally paste the body text, copy the block
- **[<TOKEN> in-out]** — pick a token, enter in/out timecodes, paste the quote text, copy a complete @PULL block

Every "copy" button confirms with a brief ✓ Copied! marker so you can move quickly between PostBridge and your script editor.

The Format Script tool produces plain text only — no PostBridge state is saved. You assemble your script in whatever editor you prefer (VS Code, Google Docs, plain Notepad) and then save it as `.txt` for Script → Session to load.

---

## Workflow 3: Script → Session

For when you already have a script with marked interview pulls.

### Step 1 — Load Script

Drop or open a PostBridge-formatted `.txt` script. The parser validates token structure and reports any malformed blocks.

### Step 2 — Assign Media

Drop interview recordings onto the pool. PostBridge auto-detects speaker assignments from Riverside filenames. Adjust any incorrect assignments via the dropdown on each row.

### Step 3 — Reconcile

PostBridge picks the fastest path per token:

- **Pull Quotes session exists** → instant lookup, no Whisper
- **`.pb_transcript.json` cached next to the audio** → instant lookup, no Whisper
- **5+ pulls and no cache** → transcribes the full audio once and caches it, then looks up each pull
- **Under 5 pulls** → traditional per-pull Whisper on padded windows

A live progress log shows per-clip status. A **resource monitor** above the log shows GPU/RAM utilization. A **memory pre-flight check** warns before launch if Windows commit headroom is too tight (Whisper allocations can fail mid-run if the page file is near full).

**Script-conform editing:** when a full transcript is available, PostBridge aligns the script's quote text against the transcript and generates internal cuts for any words on tape that aren't in the script — e.g. "you know" or "um" survivors get cut automatically. Safe by design: if alignment confidence is low, falls back to single-segment match.

**Text-fallback reconcile:** if a pull's timecodes are bad (in > out, or out-of-range), PostBridge does a fuzzy word-sequence search through the cached transcript to find the quote anyway. Lands the pull in "needs review" so you can verify.

Status badges:
- **ok** — strong match
- **low confidence** — partial match, worth reviewing
- **no match** — couldn't find the quote; may need manual assignment
- **error** — extraction failed

### Step 4 — Review

Review flagged clips. Adjust timecodes in the waveform editor. Manually accept or reject. All edits persist in the session JSON.

### Step 5 — Export

Choose **AAF** or **XML**, set sequence name and inter-part gap, then export.

---

## Workflow 4: AAF → XML

For when audio is locked in Pro Tools and needs Premiere-compatible video sync.

### Step 1 — Load AAF

Export your Pro Tools session as AAF (**File → Export → As AAF**, **Link to Source Media**, not embedded), then drop it into PostBridge.

### Step 2 — Assign & Sync

A grid shows each source clip from the AAF. For each source:

- **Video file(s)** — assign one or more via the `[−] N [+]` counter. PostBridge probes all assigned files in parallel and promotes the highest-confidence match.
- **Reference audio** — assign an ISO or mix file for waveform sync.
- **☐ slate** — tick if production used a clapper/slate. Switches sync detection to GCC-PHAT weighting, which sharpens the slate transient peak instead of treating it as noise. Strongly recommended when available.
- **SYNC** — runs multi-window cross-correlation. The algorithm probes 3 positions through the file (25/50/75 %) to avoid being misled by setup/teardown noise at the head, and arbitrates by consistency across probes.

The result shows the offset, a confidence percentage, and one of:

- **✓ verified** (≥ 95 %)
- **⚠ verify recommended** (85–94 %)
- **⚠ verify required** (50–84 %)
- **⚠ low conf, must verify** (< 50 %)

Click **ALIGN** to open the waveform alignment dialog. If auto-sync returned alternative candidates (runner-up cross-correlation peaks), a **TRY NEXT ▶** button cycles through them — each candidate shows its offset and confidence so you can scan before auditioning. Adjust the offset visually, audition with the play buttons, and accept.

Sync results are cached to disk — re-running is fast.

### Build XML

Click **Build XML** once all sources are synced. PostBridge generates an XMEML file with audio and video aligned.

---

## Script Format

PostBridge scripts use a bracketed markup:

```
[TOKENS]
NATALIE
DAN
PART_0_NARRATOR
[/TOKENS]

[PART Cold Open]

When detectives stepped into the canvas tent, they thought the man on the
ground had been attacked by a bear.

[NATALIE 00:01:12-00:01:23]
"I was on my way home, he met me on my doorstep with dinner."

[VO PART_0_NARRATOR]
But the evidence inside pointed to someone else entirely.
```

- `[PART <name>]` — marks a new segment in the timeline
- `[<TOKEN> HH:MM:SS-HH:MM:SS]` — interview pull; the quoted text on following lines is the script body
- `[VO <id>]` — voice-over block; the text on following lines is the VO content
- `[TOKENS] … [/TOKENS]` — optional explicit token registry. Tokens are also auto-registered the first time they appear.
- Lines starting with `//` (or whitespace + `//`) are ignored as comments

Blank lines between blocks become a natural gap in the output timeline.

---

## Session Files

Two flavours, both human-readable JSON:

| File | Contents | Lives in |
|---|---|---|
| `*_session.json` | Script→Session state: assignments, reconciliation results, Step 4 edits | Wherever you saved it |
| `<TOKEN>.pb_session.json` | Pull Quotes session: media list, transcript, notes, speaker labels | `<project>/03_AUDIO/00_RAW AUDIO/` |
| `<media>.pb_transcript.json` | Cached transcript word list | Next to the source audio |

The `.pb_transcript.json` sidecars are shared between workflows: transcribing in Pull Quotes populates them; reconcile in Script→Session reads them.

**Cross-machine handoff:** Session files open on a different computer. If PostBridge detects paths from a different OS, it prompts to locate your Video and Audio folders and remaps everything automatically.

---

## Supported File Types

| Category | Extensions |
|---|---|
| Video | `.mp4 .mov .mxf .avi .mkv .m4v .mpg .mpeg .ts .mts .m2ts .wmv .r3d .braw` |
| Audio | `.wav .aif .aiff .bwf .rf64 .mp3 .m4a .aac .flac .ogg .opus .caf` |
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
| `FULL_TRANSCRIBE_CONCURRENCY` | `2` | Concurrent full-audio transcribes — drop to 1 on CPU or low-VRAM GPU |
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
