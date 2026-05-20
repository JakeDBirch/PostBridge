"""Pre-download Whisper models for bundling into a PostBridge build.

Run from the project root before PyInstaller:

    python _download_bundled_models.py

Models land in ./models/ in faster-whisper's standard cache layout.
The PyInstaller spec picks the folder up automatically when present
and engines.get_model() reads from it at runtime via download_root.

Bundling targets the practical CPU choices (tiny / base / small) so
first-launch users don't sit through a 100-500 MB download.  Medium
and large-v3 stay on-demand to keep installer size manageable.
"""
import os
import sys

# Default sizes — override on the command line:  python _download_bundled_models.py tiny base
SIZES = sys.argv[1:] or ["tiny", "base", "small"]
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
