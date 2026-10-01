# macOS signing files

- `entitlements.plist` — hardened-runtime exceptions a PyInstaller app needs
  (required for notarization): Python's ctypes/cffi write executable
  trampolines, and the bundled extension modules load as libraries. Kept as
  plain plist with no comments — Apple re-encodes entitlements during
  notarization and is strict about their format.
- `verify_signing.sh` — checks (never modifies) every signature in a built
  `.app`. PyInstaller does the signing; see `MAC_BUILD.md`.
