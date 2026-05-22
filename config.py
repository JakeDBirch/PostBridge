import sys

# ── Config ─────────────────────────────────────────────────────────────────────
WHISPER_MODEL    = "small"    # small / medium / large-v3
PAD_SECS         = 5          # seconds added each side of the pull timecode
MAX_EXTRACT_S    = 600        # max extraction window (seconds) per interview pull;
                              # prevents sentinel out-points like 99:59:59 from
                              # causing Whisper to transcribe an entire recording
MAX_ITEM_STALL_S = 300        # seconds before a stalled future is force-abandoned
MAX_WORKERS      = 4          # parallel transcription workers
AUTO_FULL_TRANSCRIBE_THRESHOLD = 5  # if a token has this many pulls or more AND no
                              # cached transcript, transcribe the whole audio
                              # source once rather than running per-pull Whisper.
                              # Per-pull has high fixed overhead (model warmup,
                              # audio load), so full-audio almost always wins
                              # at 5+ pulls.  Set to a very high number to
                              # disable the auto-promotion.
FULL_TRANSCRIBE_CONCURRENCY = 4  # how many auto-promoted full-audio transcribes
                              # may run simultaneously.  faster-whisper shares
                              # one global model and CTranslate2 serializes
                              # internally — but on a modern GPU there's
                              # plenty of headroom for several streams to
                              # share it without thrashing.  4 is comfortable
                              # on a mid-range NVIDIA (8 GB VRAM) with the
                              # small model (~600 MB per instance ⇒ ~2.4 GB
                              # used).  Set to 2 on a low-VRAM GPU or 1 on
                              # CPU-only systems.  Monitor _debug_run.log
                              # for [res] lines while a run is in flight —
                              # GPU% pinned high + workers near the limit
                              # means you're saturated; idle GPU and queued
                              # tokens means you can bump this higher.
# ── Script-conform editing ──────────────────────────────────────────────────
# When a pull has a full transcript available (PQ session or .pb_transcript
# .json sidecar), reconcile aligns the script's quote_text against the
# transcript and generates internal cuts where Whisper found words the
# script didn't.  These auto-conformed segments appear in Step 4 with the
# normal multi-segment review UI.
SCRIPT_CONFORM_ENABLED = True   # master switch
SCRIPT_CONFORM_MIN_RATIO   = 0.70  # minimum fraction of script tokens that must
                                  # align before auto-cuts are trusted; below
                                  # this we fall back to a single-segment match
SCRIPT_CONFORM_MIN_RUN     = 2     # ignore matching blocks shorter than this
                                  # (1-token "matches" are usually coincidental
                                  # — "the", "and", "I" turn up everywhere)
SCRIPT_CONFORM_MERGE_GAP_S = 0.4   # merge segments whose inter-cut gap is
                                  # shorter than this; Whisper's natural pauses
                                  # within a sentence shouldn't become cuts
MATCH_THRESH     = 0.55       # minimum word-overlap score to accept a match
GAP_THRESH       = 1.5        # seconds of silence triggering a cut within a clip
DEFAULT_GAP      = 0.6        # seconds between parts in the sequence
CACHE_VERSION    = 1          # bump to invalidate all cached transcripts
HOST_NAME        = "jordan"   # host's Riverside display name (case-insensitive)
CLIP_SRC_OFFSET  = 0.3        # seconds to shift source read-start for interview
                              # clips — script TCs tend to start slightly early
VO_TAIL_SECS     = 0.5        # extra seconds appended to the end of every VO
                              # segment — Whisper timestamps stop at the last
                              # detected word, which cuts off natural decay/room tone
WAVEFORM_CONFORM     = True   # enable waveform-assisted VO transcription + edge snapping:
                              #  • transcription: audio split at silence gaps ≥ CHUNK_SILENCE_S;
                              #    each chunk transcribed independently (better Whisper accuracy
                              #    on long files, lower memory, complete word coverage)
                              #  • edge snapping: blob boundaries refine segment in/out points
BLOB_SILENCE_DB      = -35    # dBFS threshold below which a frame counts as silence
BLOB_MIN_SPEECH_MS   = 300    # minimum speech blob to keep (ms) — edge snapping only
BLOB_MIN_SILENCE_MS  = 500    # minimum silence gap between speech blobs (ms) — edge snapping
BLOB_SNAP_THRESH     = 0.35   # seconds: snap segment edge to nearest blob boundary
SNAP_IN_OFFSET       = 0.08   # seconds added to IN  after auto-snap — advances past breath lead-in
SNAP_OUT_OFFSET      = 0.10   # seconds added to OUT after auto-snap — ensures word tail is included
CHUNK_SILENCE_S      = 1.5    # silence duration (seconds) that triggers a chunk split —
                              # catches paragraph/take breaks without splitting mid-sentence
CHUNK_MAX_S          = 90     # max chunk length (seconds); longer chunks are force-split

# ── Developer flags ────────────────────────────────────────────────────────────
# When True, exposes the DIAGNOSTIC button in Step-4 reconcile and writes
# <output>_build_debug.json / <output>_diagnostic.txt sidecars next to
# every AAF/XML export.  Useful for debugging the build pipeline but
# leaves mystery files next to user-facing exports — keep False for any
# build that ships to a non-dev user; flip True locally when debugging.
DEV_DIAGNOSTIC       = False

# ── Palette ────────────────────────────────────────────────────────────────────
# MeatEater dark theme — charcoal/near-black backgrounds, orange accent
BG      = "#191919"   # near-black app background
SURF    = "#232323"   # card / panel surface
SURF2   = "#2c2c2c"   # subtle inset / input background
SURF3   = "#363636"   # alternate row / deeper inset
BORDER  = "#3e3e3e"   # divider
ACCENT  = "#e05c00"   # MeatEater orange
TEXT    = "#f0ede8"   # warm off-white
SUB     = "#9a9a9a"   # secondary / muted text (lighter for readability)
SUCCESS = "#5aab61"   # muted green
WARN    = "#e8a020"   # amber
ERR     = "#d94040"   # red
INFO    = "#e05c00"   # same as accent
WAVE_REF = "#20a080"  # teal — reference audio waveform
WAVE_VID = "#e05c00"  # orange — video embedded audio waveform

# Font stack: Segoe UI on Windows, SF Pro on macOS, fallback to Helvetica
_SANS = "Segoe UI" if sys.platform == "win32" else (
        "SF Pro Display" if sys.platform == "darwin" else "Helvetica Neue")
FH  = (_SANS, 22, "bold")
FS  = (_SANS, 12, "italic")
FL  = (_SANS, 12, "bold")
FB  = (_SANS, 12)
FBT = (_SANS, 13, "bold")