#!/usr/bin/env bash
# PostBridge — macOS build script
# Usage:  bash build.sh
#
# Produces dist/PostBridge.app — zip it and distribute internally.
# Recipients double-click PostBridge.app; ffmpeg is already inside.

set -euo pipefail

FFMPEG_BASE="https://github.com/shaka-project/static-ffmpeg-binaries/releases/download/n7.1-2"
FFBIN="ffmpeg-bin"

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

# ── Python / pip checks ───────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Install Python 3.10+ and try again."
    exit 1
fi

if ! python3 -m pyinstaller --version &>/dev/null; then
    echo "Installing PyInstaller ..."
    pip3 install pyinstaller
fi

echo "Installing dependencies ..."
pip3 install -r requirements.txt

# ── Clean previous build ──────────────────────────────────────────────────────
rm -rf build/PostBridge dist/PostBridge dist/PostBridge.app

# ── Build ─────────────────────────────────────────────────────────────────────
echo ""
echo "Running PyInstaller ..."
python3 -m pyinstaller PostBridge.spec --noconfirm

echo ""
echo "============================================================"
echo " Build complete!"
echo ""
echo " Output:  dist/PostBridge.app"
echo ""
echo " To distribute internally:"
echo "   zip -r PostBridge-mac.zip dist/PostBridge.app"
echo "   Share PostBridge-mac.zip — no Python or ffmpeg install needed."
echo ""
echo " NOTE: macOS Gatekeeper may block unsigned apps."
echo "   Recipients right-click → Open on first launch,"
echo "   or run:  xattr -cr dist/PostBridge.app"
echo "============================================================"
echo ""
