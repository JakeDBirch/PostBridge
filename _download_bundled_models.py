"""Pre-download Whisper models for bundling into a PostBridge build.

Run from the project root before PyInstaller:

    python _download_bundled_models.py

Models land in ./models/ in faster-whisper's standard cache layout.
The PyInstaller spec picks the folder up automatically when present
and engines.get_model() reads from it at runtime via download_root.

Default bundle is just `tiny` (~75 MB) so the installer stays under
the size limits of common chat tools (Slack, Discord, etc.).  Other
models are fetched after launch:
  - base + small download in the background on first run
    (engines.prefetch_models_async)
  - medium + large-v3 download on demand the first time the user
    selects them in the model picker

Override on the command line if you want a fatter bundle, e.g.:
    python _download_bundled_models.py tiny base small
"""
import os
import sys

# Default size — override on the command line for a fatter bundle.
SIZES = sys.argv[1:] or ["tiny"]
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")

# faster-whisper exposes download_model() for fetching weights without
# loading them into RAM.  Older versions had different module paths;
# try a couple before giving up.
download_model = None
for _import in (
    "from faster_whisper.utils import download_model",
    "from faster_whisper import download_model",
):
    try:
        exec(_import, globals())
        break
    except ImportError:
        download_model = None
        continue

if download_model is None:
    # Last-ditch fallback: instantiate the model (loads it into RAM
    # which is wasteful but functionally identical for the download).
    from faster_whisper import WhisperModel
    for size in SIZES:
        print("downloading", size, "via WhisperModel()")
        _ = WhisperModel(size, device="cpu", compute_type="int8",
                          download_root=CACHE)
else:
    for size in SIZES:
        print("downloading", size, "via download_model()")
        download_model(size, cache_dir=CACHE)

print("done.  bundled models in:", CACHE)
