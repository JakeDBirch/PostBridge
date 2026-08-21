#!/usr/bin/env bash
# PostBridge — macOS build script
# Usage:  bash build.sh
#
# Produces dist/PostBridge.app — zip it and distribute internally.
# Recipients double-click PostBridge.app; ffmpeg is already inside.
#
# Mirrors .github/workflows/build-macos-arm.yml so a local build and the
# CI artifact contain the same thing (same deps, same bundled models).

set -euo pipefail

FFMPEG_BASE="https://github.com/shaka-project/static-ffmpeg-binaries/releases/download/n7.1-2"
FFBIN="ffmpeg-bin"
VENV=".venv-build"

echo ""
echo "============================================================"
echo " PostBridge Build  (macOS)"
echo "============================================================"
echo ""

# ── Detect architecture ───────────────────────────────────────────────────────
ARCH=$(uname -m)
if [[ "$ARCH" == "arm64" || "$ARCH" == "aarch64" ]]; then
    FFMPEG_ASSET="ffmpeg-osx-arm64"
    FFPROBE_ASSET="ffprobe-osx-arm64"
else
    FFMPEG_ASSET="ffmpeg-osx-x64"
    FFPROBE_ASSET="ffprobe-osx-x64"
fi

echo "Architecture: $ARCH  →  using $FFMPEG_ASSET"
echo ""

# ── Download ffmpeg static binaries ──────────────────────────────────────────
mkdir -p "$FFBIN"

if [[ ! -f "$FFBIN/$FFMPEG_ASSET" ]]; then
    echo "Downloading $FFMPEG_ASSET ..."
    curl -fL -o "$FFBIN/$FFMPEG_ASSET" "$FFMPEG_BASE/$FFMPEG_ASSET"
    chmod +x "$FFBIN/$FFMPEG_ASSET"
else
    echo "  $FFMPEG_ASSET already present — skipping."
fi

if [[ ! -f "$FFBIN/$FFPROBE_ASSET" ]]; then
    echo "Downloading $FFPROBE_ASSET ..."
    curl -fL -o "$FFBIN/$FFPROBE_ASSET" "$FFMPEG_BASE/$FFPROBE_ASSET"
    chmod +x "$FFBIN/$FFPROBE_ASSET"
else
    echo "  $FFPROBE_ASSET already present — skipping."
fi

echo ""

# ── Python check ──────────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Install Python 3.10+ and try again."
    exit 1
fi

echo "Using $(python3 --version) at $(command -v python3)"

# tkinter ships with python.org builds but NOT with bare Homebrew python —
# catch that here rather than 10 minutes later inside PyInstaller.
if ! python3 -c "import tkinter" &>/dev/null; then
    echo ""
    echo "ERROR: this python3 has no tkinter."
    echo "  Homebrew:    brew install python-tk"
    echo "  Or install the python.org 3.12 build, which bundles it."
    exit 1
fi

# ── Build venv ────────────────────────────────────────────────────────────────
# A dedicated venv keeps the build reproducible and sidesteps PEP 668
# ("externally-managed-environment"), which makes a bare `pip3 install`
# fail outright on Homebrew and recent python.org installs.
if [[ ! -d "$VENV" ]]; then
    echo ""
    echo "Creating build venv in $VENV ..."
    python3 -m venv "$VENV"
fi

PY="$VENV/bin/python"

echo ""
echo "Installing dependencies ..."
"$PY" -m pip install --upgrade pip
"$PY" -m pip install pyinstaller
"$PY" -m pip install -r requirements.txt

# ── Pre-download Whisper models ───────────────────────────────────────────────
# Bundles `tiny` only (~75 MB); every larger size downloads on demand at
# runtime.  The spec picks up models/ automatically when it exists.
echo ""
echo "Pre-downloading bundled Whisper model (tiny) ..."
"$PY" _download_bundled_models.py

# ── Clean previous build ──────────────────────────────────────────────────────
rm -rf build/PostBridge dist/PostBridge dist/PostBridge.app

# ── Build ─────────────────────────────────────────────────────────────────────
echo ""
echo "Running PyInstaller ..."
"$PY" -m PyInstaller PostBridge.spec --noconfirm

echo ""
echo "============================================================"
echo " Build complete!"
echo ""
echo " Output:  dist/PostBridge.app"
echo ""
echo " To distribute internally:"
echo "   ditto -c -k --keepParent dist/PostBridge.app PostBridge-mac.zip"
echo "   Share PostBridge-mac.zip — no Python or ffmpeg install needed."
echo ""
echo " (Use ditto, not zip — it preserves the symlinks and bundle"
echo "  metadata inside the .app that plain zip flattens.)"
echo ""
echo " NOTE: macOS Gatekeeper may block unsigned apps."
echo "   Recipients right-click → Open on first launch,"
echo "   or run:  xattr -cr dist/PostBridge.app"
echo "============================================================"
echo ""
