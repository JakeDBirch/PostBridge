#!/usr/bin/env bash
# Verify — never modify — the code signatures of a built .app.
#
#   macos/verify_signing.sh path/to/PostBridge.app [--developer-id]
#
# PyInstaller does the signing (PostBridge.spec, PB_CODESIGN_IDENTITY).
# Re-signing its main executable afterwards breaks notarization, so this
# script only checks.  Every Mach-O file in the bundle must pass strict
# verification; with --developer-id each must also carry a Developer ID
# authority, a secure timestamp and the hardened runtime — what Apple's
# notary requires — so a gap fails here with the file named, instead of
# as an opaque rejection after the upload.
set -euo pipefail

APP="$1"
DEVID="${2:-}"
bad=0
n=0

while IFS= read -r -d '' f; do
    file -b "$f" | grep -q "Mach-O" || continue
    n=$((n + 1))
    rel="${f#"$APP"/}"
    if ! out="$(codesign --verify --strict --verbose=2 "$f" 2>&1)"; then
        echo "::error::invalid signature: $rel"; echo "$out"; bad=1; continue
    fi
    if [[ "$DEVID" == "--developer-id" ]]; then
        info="$(codesign -dvv "$f" 2>&1)"
        grep -q "Authority=Developer ID Application" <<<"$info" \
            || { echo "::error::not signed with Developer ID: $rel"; bad=1; }
        grep -q "^Timestamp=" <<<"$info" \
            || { echo "::error::no secure timestamp: $rel"; bad=1; }
        grep -Eq "flags=0x[0-9a-f]*\(.*runtime" <<<"$info" \
            || { echo "::error::hardened runtime not enabled: $rel"; bad=1; }
    fi
done < <(find "$APP/Contents" -type f -print0)

echo "checked $n Mach-O files"
echo "--- main executable ---"
codesign -dvv "$APP/Contents/MacOS/PostBridge" 2>&1 | grep -E "^(Identifier|Format|CodeDirectory|Authority|Timestamp|TeamIdentifier)=" || true

if ! codesign --verify --deep --strict --verbose=2 "$APP"; then
    echo "::error::bundle seal is invalid"; bad=1
fi
exit $bad
