# PostBridge

PostBridge is a desktop tool for post-production teams that reconciles interview transcripts against recordings and exports editor-ready timelines — either as Pro Tools AAF sessions or Premiere/FCP-compatible XML.

Built for **Windows** and **macOS** (Apple Silicon + Intel via Rosetta 2).

---

## Download

Pre-built app bundles are produced manually via [GitHub Actions](https://github.com/JakeDBirch/PostBridge/actions):

1. Go to the **Actions** tab → click a **Build PostBridge** run marked ✓
2. Download the artifact for your platform under the **Artifacts** section:
   - `PostBridge-windows` → Windows (x64) — **CPU only**, ~500 MB
   - `PostBridge-windows-gpu` → Windows (x64) — **NVIDIA CUDA bundled**, ~3.5 GB
   - `PostBridge-mac-arm64` → macOS Apple Silicon (M1/M2/M3)

No Python installation required — everything is bundled.

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

PostBridge handles three workflows:

| Workflow | Use when |
|---|---|
| **Pull Quotes** | You have raw interview audio and want to read the transcript, highlight quotes, and copy them straight into your script with precise timecodes baked in. |
| **Script → Session** | You have a formatted script with marked interview pulls and want PostBridge to find each quote on tape and assemble an editor-ready timeline. |
| **AAF → XML** | Pro Tools audio is locked. You need to hand it off to Premiere/FCP with the correct video synced underneath. |

The three workflows share transcript caches — a session transcribed in Pull Quotes is instantly available to Script → Session reconcile, and vice versa.

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

Click **TRANSCRIBE** to run Whisper on the audio. The transcript renders as paragraphs with speaker headers and can be searched, edited, and annotated. If a cached transcript already exists alongside the source audio (`.pb_transcript.json`), PostBridge offers to use it instead of re-running Whisper.

### Step 3 — Lift quotes

Select text in the transcript and right-click to:

- **Copy as @PULL** — clipboard gets a script-ready block: `[TOKEN HH:MM:SS-HH:MM:SS]\n"<quote>"`
- **Play selection** (Space) — auditions the audio
- **Add margin note** (Ctrl+M) — annotate a passage without affecting the script output
- **Edit selected text…** — fix Whisper transcription errors in place

Paste straight into your PostBridge script. Timecodes are Whisper-precise — no further reconciliation needed for those pulls.

---

## Workflow 2: Script → Session

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

## Workflow 3: AAF → XML

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

Most settings live in `config.py` and require a code edit:

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
