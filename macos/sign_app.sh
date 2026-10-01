#!/usr/bin/env bash
# Sign every piece of code inside a PyInstaller .app, inside-out, then the
# bundle itself.
#
#   macos/sign_app.sh IDENTITY path/to/PostBridge.app
#
# IDENTITY "-" ad-hoc signs (runs locally, but Gatekeeper still warns on a
# downloaded copy).  A "Developer ID Application: …" identity signs with the
# hardened runtime and a secure timestamp — what notarization requires.
#
# Why not `codesign --deep`: it skips Mach-O files outside the standard code
# locations (e.g. ffmpeg/ffprobe under Contents/Resources), and notarization
# rejects any unsigned Mach-O anywhere in the bundle.
set -euo pipefail

ID="$1"
APP="$2"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [[ "$ID" == "-" ]]; then
    FLAGS=(--force --sign -)
else
    FLAGS=(--force --sign "$ID" --options runtime --timestamp
           --entitlements "$HERE/entitlements.plist")
fi

# Every Mach-O regular file (symlinks skipped — they point at files that are
# signed in place), deepest paths first so containers are signed after
# their contents.
count=0
while IFS= read -r f; do
    codesign "${FLAGS[@]}" "$f"
    count=$((count + 1))
done < <(
    find "$APP/Contents" -type f -print0 |
    while IFS= read -r -d '' f; do
        if file -b "$f" | grep -q "Mach-O"; then
            printf '%s\t%s\n' "$(tr -cd '/' <<<"$f" | wc -c)" "$f"
        fi
    done | sort -rn | cut -f2-
)
echo "signed $count Mach-O files"

# Nested framework bundles (e.g. Python.framework), deepest first.
while IFS= read -r fw; do
    codesign "${FLAGS[@]}" "$fw"
done < <(find "$APP/Contents" -type d -name "*.framework" |
         awk -F/ '{print NF "\t" $0}' | sort -rn | cut -f2-)

codesign "${FLAGS[@]}" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"
