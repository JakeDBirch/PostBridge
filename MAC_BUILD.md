# PostBridge on macOS — build & handoff

Context doc for a Claude Code session running **on the Mac**. Written from a
Windows session that audited the tree for cross-platform issues but could not
run anything on macOS. Everything below marked "verified" was checked by
reading the code; everything marked "untested" needs a real Mac to confirm.

---

## 1. Get set up

```bash
git clone https://github.com/JakeDBirch/PostBridge.git
cd PostBridge
```

Python 3.12 is what CI uses (`macos-14` runner) — match it. The build needs
**tkinter**, which the python.org installer bundles but bare Homebrew Python
does not:

```bash
brew install python-tk    # only if using Homebrew Python
```

`build.sh` checks for tkinter up front and fails fast with this hint rather
than dying inside PyInstaller ten minutes later.

## 2. Build

```bash
bash build.sh
```

Produces `dist/PostBridge.app` (~1.2 GB). The script creates a `.venv-build`
venv, installs deps, downloads static ffmpeg/ffprobe for the detected arch,
pre-downloads the `tiny` Whisper model into `models/`, and runs PyInstaller
against `PostBridge.spec`.

The build ends by verifying itself: `codesign --verify` on the bundle, then
`PostBridge.app/Contents/MacOS/PostBridge --self-test build/selftest.txt`,
which makes the app find its **own** ffmpeg/ffprobe (PATH and Homebrew are
ignored), probe a clip with audio and one without, and extract a waveform.
If that fails the script exits non-zero — don't ship that build. CI runs the
same self-test on the built app and again on the unzipped artifact, and fails
the run on any miss. Run it by hand against any copy of the app to check it.

ffmpeg/ffprobe are saved into `ffmpeg-bin/` as plain `ffmpeg` / `ffprobe` and
ad-hoc signed *before* PyInstaller runs; the spec refuses to build without
them. (Earlier builds shipped them as `ffmpeg-osx-arm64` etc., which the app
never looked for — every sync then failed with "This file has no audio track".)

The build is Apple Silicon only; it will not launch on an Intel Mac.

To ship it:

```bash
ditto -c -k --keepParent dist/PostBridge.app PostBridge-mac.zip
```

Use `ditto`, not `zip` — plain zip flattens the symlinks inside the bundle.

First launch of an unsigned app: right-click → **Open**, or `xattr -cr
dist/PostBridge.app`.

**CI equivalent:** `.github/workflows/build-macos-arm.yml` does the same thing
on a GitHub `macos-14` runner (manual `workflow_dispatch`). `build.sh` was
rewritten to mirror it, so a local build and the CI artifact should now
contain the same thing. If they diverge, that's a bug in one of them.

## 3. Iterating without a full build

PyInstaller takes minutes. To work on the app itself, run from source:

```bash
.venv-build/bin/python main.py
```

Same code, same behaviour, ~2 s startup. Only rebuild to test bundling
(model paths, ffmpeg discovery, Gatekeeper).

---

## 4. Known macOS gaps

> **Partly addressed.** The Step 4 waveform editor now plays through
> `playback.LivePlayer` (PortAudio via `sounddevice`), which is
> cross-platform — so the editor's transport, scrubbing and speed control
> should work on macOS as soon as `sounddevice` is installed. The section
> below still applies to every OTHER playback path in the app (Pull Quotes
> auditioning, the AAF QA player), which remain on `winsound`. Nobody has
> run this on a Mac yet; it is reasoning from the code, not a test.

### 4.1 Audio playback is silent on macOS — the real one

**Verified by reading the code.** Every playback path in the app goes through
`winsound`, which is Windows-only. All 16 `PlaySound` calls sit inside
`try/except` or `sys.platform == "win32"` guards, so **nothing crashes** — the
app launches fine and every play button simply does nothing.

For an audio-post tool this is the difference between "builds" and "usable",
so it's probably the first thing to fix after the build succeeds.

Call sites, all extracting a temp WAV via ffmpeg and then handing the path to
`winsound.PlaySound(path, SND_FILENAME | SND_ASYNC)`:

| File | Lines | What |
|---|---|---|
| `main.py` | 7682, 7736 | inline card-level segment playback (`_stop_playback`, `_play_segment`) |
| `pq_workflow.py` | 25, 5077, 5168 | Pull Quotes audition; guarded import at 25 |
| `aaf_workflow.py` | 2966, 3342, 3477 | AAF QA audition + stop-on-ALIGN |
| `match_review.py` | 2440, 2664, 2894, 2959 | match review audition |

Suggested approach — macOS ships `afplay`, so this needs **no new dependency**:
add one small backend to `utils.py` alongside the existing `run_hidden()`
wrapper (that file is already the single point of control for subprocess
behaviour), exposing `play_wav(path)` and `stop_playback()`. On Windows it
calls `winsound` exactly as today; on macOS it spawns `afplay` and tracks the
`Popen` handle so stop can kill it. Then replace the 16 call sites.

Two things to watch:
- `SND_PURGE` is global "stop everything"; `afplay` is per-process, so the
  backend has to own the handle to replicate stop semantics.
- `aaf_workflow.py:3488` `_qa_poll` estimates playback completion from file
  duration because winsound gives no callback. With `afplay` you get a real
  process exit — better signal, worth using.

`simpleaudio` was listed in `requirements.txt` as the playback dep but is
imported nowhere in the tree. It was removed (dead, plus it's a C extension
with no Python 3.12 wheels — pure install risk). Don't add it back without a
reason; `afplay` is the native answer.

### 4.2 Everything else looks genuinely portable — verified

The tree is already platform-aware, so there is less to do here than you'd
expect. Checked and clean:

- All four `ctypes.windll` uses (`main.py:4079`, `main.py:8641`, `8644`,
  `pq_workflow.py:4809`) sit behind `sys.platform == "win32"` guards.
- `config.py:118` already picks SF Pro Display on `darwin`.
- Paths use `os.path.expanduser("~")`, not `%APPDATA%` — `engines.py:880`
  (`~/.postbridge_models`) and `engines.py:4170` (`~/Downloads`) both work.
- `utils.run_hidden()` no-ops its Windows console-suppression flags off-Windows.
- `utils.basename()` uses `ntpath.basename`, which splits on `/` too, so POSIX
  paths are handled.
- `main.py:167` skips the Windows `state("zoomed")` and calls `_center()`.
- `main.py:8638` DPI-awareness block is Windows-gated; macOS gets HiDPI from
  `NSHighResolutionCapable` in the spec's `BUNDLE` info_plist.
- `utils.local_fraction()` (cloud-placeholder detection) returns 1.0 on
  non-Windows — meaning iCloud/Dropbox online-only files won't be detected on
  macOS. Degrades safely to "assume local", but it's a real feature gap if the
  team keeps media in iCloud Drive. `brctl` is the macOS equivalent if it
  matters.

**Untested on real hardware:** whether `tkinterdnd2` drag-and-drop works in the
bundled `.app` (it's guarded with a try/except per module, so worst case DnD is
silently unavailable), and whether `ctranslate2`/`faster-whisper` load cleanly
from inside the bundle on Apple Silicon.

### 4.3 GPU

There is none to bundle. The spec's `collect_all("torch")` block is a no-op
when torch isn't installed, so the macOS build is CPU-only by construction.
faster-whisper/ctranslate2 have no Metal backend — CPU inference on Apple
Silicon is respectable but slower than the Windows CUDA build. Don't install
torch into the build venv expecting a speedup; it'll just add ~2 GB.

---

## 5. Suggested first session on the Mac

1. `bash build.sh` — confirm it completes and `dist/PostBridge.app` opens.
2. Exercise each of the four workflows (Pull Quotes, Format Script,
   Script → Session, AAF → XML) far enough to catch bundling failures —
   especially anything that shells out to ffmpeg or loads a Whisper model.
3. Then take on the audio backend in 4.1.
