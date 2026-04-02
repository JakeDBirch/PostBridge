# PostBridge

PostBridge is a desktop tool for post-production teams that reconciles interview transcripts against recordings and exports editor-ready timelines — either as Pro Tools AAF sessions or Premiere/FCP-compatible XML.

Built for **Windows** and **macOS** (Apple Silicon + Intel via Rosetta 2).

---

## Download

Pre-built app bundles are available from [GitHub Actions](https://github.com/JakeDBirch/PostBridge/actions):

1. Click the most recent **Build PostBridge** run
2. Download the artifact for your platform:
   - `PostBridge-windows` → Windows (x64)
   - `PostBridge-mac-arm64` → macOS Apple Silicon (M1/M2/M3)

No Python installation required — everything is bundled.

> **macOS first launch:** Right-click the app → **Open** → click **Open** in the dialog. This bypasses Gatekeeper for unsigned apps and only needs to be done once.

---

## What it does

PostBridge handles two distinct workflows:

### Workflow 1 — Script → Session

For when you have a formatted script with marked interview pulls and want to auto-reconcile those quotes against recorded interview footage, then export a sequenced timeline.

**Inputs:** A PostBridge-formatted `.txt` script + interview media files (video or audio)
**Output:** An AAF session or XMEML XML with all interview clips and VO blocks placed in script order

### Workflow 2 — AAF → XML (Pro Tools → Premiere)

For when audio has already been cut in Pro Tools and you need to hand that edit off to a Premiere editor with the correct video attached.

**Inputs:** A Pro Tools `.aaf` export + the corresponding video files
**Output:** A Premiere-compatible XMEML XML with audio and video in sync

---

## Workflow 1: Script → Session

### Step 1 — Load Script

Drop or open a PostBridge-formatted `.txt` script. The app parses `@PART`, `@PULL`, and `@VO` blocks and validates the token structure.

### Step 2 — Assign Media

Drop interview recordings onto the pool. PostBridge auto-detects speaker assignments from Riverside filenames. Adjust any incorrect assignments via the dropdown on each row.

You can also assign external transcript sources (`.srt` files) per speaker if you have them.

### Step 3 — Reconcile

PostBridge extracts audio windows around each `@PULL` timecode, transcribes them using Whisper, and matches the transcribed words against the quoted text in the script.

Results are cached — if you rerun, unchanged clips skip transcription entirely.

Status badges:
- **ok** — strong word-overlap match
- **low confidence** — partial match, worth reviewing
- **no match** — couldn't find the quote; may need manual assignment
- **error** — extraction failed (check media file)

### Step 4 — Review

Review flagged clips. Manually accept or reject uncertain matches. Timecodes can be adjusted directly in the review panel. All edits are preserved in the session file.

### Step 5 — Export

Choose **AAF** or **XML** output, set your sequence name and gap duration between parts, then export. The timeline is assembled in script order with VO blocks interspersed.

---

## Workflow 2: AAF → XML

### Step 1 — Load AAF

Export your Pro Tools session as an AAF (**File → Export → As AAF**, using **Link to Source Media**, not embedded audio), then drop it into PostBridge.

### Step 2 — Assign & Sync

A grid shows each source clip from the AAF. For each source:

- **Source Assign tab** — drop in the corresponding video file and (optionally) a reference audio file for sync detection
- **Source Sync tab** — PostBridge detects the offset between your audio and video using waveform cross-correlation. Review the result, adjust manually if needed, then lock it

Sync results are cached to disk — re-running the same session is fast.

### Build XML

Once sources are assigned and synced, click **Build XML** to export. PostBridge generates an XMEML file with all clips placed at their correct video positions.

---

## Script Format

PostBridge scripts use a simple markup:

```
--- INTERVIEW SESSIONS START ---
[JOHN_SMITH]
[JANE_DOE]
--- INTERVIEW SESSIONS END ---

--- EPISODE ASSETS START ---
[PART_0_NARRATOR]
--- EPISODE ASSETS END ---

@PART  Cold Open

@PULL JOHN_SMITH [00:12:34-00:12:50]
"He said something interesting here."

@VO PART_0_NARRATOR
Opening narration text goes here.
```

- `@PART` — marks a new segment in the timeline
- `@PULL` — an interview clip: token + timecode range + quoted text to match
- `@VO` — a voice-over block tied to a narrator asset
- Lines starting with `//` are ignored (comments)

Tokens in the header blocks define which speakers and assets the script references.

---

## Session Files

PostBridge saves sessions as `.json` files that store your script path, media assignments, reconciliation results, and Step 4 edits. Reopen a session at any point to pick up where you left off.

**Cross-machine handoff:** Session files can be opened on a different computer. If PostBridge detects that the file paths are from a different operating system, it will prompt you to locate your Video and Audio folders so it can remap all paths automatically.

---

## Supported File Types

| Category | Extensions |
|---|---|
| Video | `.mp4 .mov .mxf .avi .mkv .m4v .mpg .mpeg .ts .mts .m2ts .wmv .r3d .braw` |
| Audio | `.wav .aif .aiff .bwf .rf64 .mp3 .m4a .aac .flac .ogg .opus .caf` |
| Script | `.txt` (PostBridge format) |
| Transcript | `.srt` |
| AAF | `.aaf` (Pro Tools, Link to Source Media) |
| Output | `.aaf` `.xml` `.json` |

---

## Settings

Most settings live in `config.py` and require a code edit:

| Setting | Default | Description |
|---|---|---|
| `WHISPER_MODEL` | `"small"` | Whisper model size — `small` is fastest, `large-v3` is most accurate |
| `MATCH_THRESH` | `0.55` | Minimum word-overlap score to accept a pull match |
| `HOST_NAME` | `"jordan"` | Host speaker name — used to assign the shared host track in AAF output |
| `MAX_EXTRACT_S` | `600` | Maximum clip window in seconds (guards against sentinel timecodes) |
| `GAP_THRESH` | `1.5` | Silence gap (seconds) that triggers a cut within a VO clip |
| `BLOB_SILENCE_DB` | `-35` | dBFS threshold for speech edge detection |
| `MAX_WORKERS` | `4` | Parallel transcription threads |

In the UI, the **Gap** and **Pad** spinboxes (Step 5 / AAF Step 2) are the main runtime-adjustable values.

---

## Building from Source

Requires Python 3.12 and ffmpeg on PATH (for development). For distribution, the build scripts download static ffmpeg binaries automatically.

```bash
pip install -r requirements.txt
python main.py
```

**Build distributable:**
```bash
# Windows
build.bat

# macOS
bash build.sh
```

Or let GitHub Actions build it — every push to `main` produces Windows and macOS artifacts automatically.

---

## Dependencies

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — speech recognition
- [pyaaf2](https://github.com/markreidvfx/pyaaf2) — AAF read/write
- [tkinterdnd2](https://github.com/pmgagne/tkinterdnd2) — drag-and-drop support
- [numpy](https://numpy.org/) — waveform processing
- [ffmpeg](https://ffmpeg.org/) — media extraction (bundled in app builds)
