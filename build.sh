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
# Saved as plain ffmpeg / ffprobe — the names the app looks for inside the
# bundle.  They are signed along with the rest of the bundle after
# PyInstaller (macos/sign_app.sh).
mkdir -p "$FFBIN"

for pair in "ffmpeg:$FFMPEG_ASSET" "ffprobe:$FFPROBE_ASSET"; do
    name="${pair%%:*}"; asset="${pair#*:}"
    if [[ ! -f "$FFBIN/$name" ]]; then
        echo "Downloading $asset ..."
        curl -fL --retry 3 -o "$FFBIN/$name" "$FFMPEG_BASE/$asset"
    else
        echo "  $name already present — skipping."
    fi
    chmod +x "$FFBIN/$name"
    "$FFBIN/$name" -version | head -1
done

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

# ── Verify ────────────────────────────────────────────────────────────────────
# Same checks as CI: the bundle's signature is intact, and the built app can
# find and use its OWN ffmpeg (PATH / Homebrew ignored) to probe audio and
# extract a waveform.  A failure here means the .app would fail on a clean Mac.
echo ""
# PB_SIGN_ID="Developer ID Application: Name (TEAMID)" signs for distribution
# (hardened runtime + timestamp); unset, the bundle is ad-hoc signed.
echo "Signing bundle as ${PB_SIGN_ID:-ad-hoc} ..."
macos/sign_app.sh "${PB_SIGN_ID:--}" dist/PostBridge.app
if ! dist/PostBridge.app/Contents/MacOS/PostBridge --self-test build/selftest.txt; then
    cat build/selftest.txt 2>/dev/null
    echo ""
    echo "ERROR: self-test failed — do not ship this build."
    exit 1
fi

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
