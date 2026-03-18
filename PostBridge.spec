# PostBridge — PyInstaller spec file  (Windows · macOS · Linux)
#
# Build with:  pyinstaller PostBridge.spec
# Or use the platform build script:
#   Windows  →  build.bat
#   macOS    →  bash build.sh
#
# ffmpeg binaries must be pre-downloaded into ffmpeg-bin/ by the build script
# before PyInstaller runs.  The spec includes whichever files exist there.

import sys as _sys
import os
import platform as _platform
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

block_cipher = None
src_dir = os.path.abspath(os.path.dirname(SPEC))   # noqa: F821

# ── Collect faster-whisper / ctranslate2 data and dynamic libs ────────────────
added_files = []
added_files += collect_data_files("faster_whisper")
added_files += collect_data_files("ctranslate2")
added_files += collect_dynamic_libs("ctranslate2")
added_files += collect_data_files("tkinterdnd2")

# ── Bundle platform-specific ffmpeg/ffprobe static binaries ──────────────────
# build.bat / build.sh download these before PyInstaller runs.
_ffbin = os.path.join(src_dir, "ffmpeg-bin")

def _add_ff(src_name):
    fp = os.path.join(_ffbin, src_name)
    if os.path.isfile(fp):
        added_files.append((fp, "ffmpeg"))
    else:
        import sys as _s
        print(
            "WARNING: ffmpeg-bin/{} not found — run the build script first.".format(src_name),
            file=_s.stderr,
        )

if _sys.platform == "win32":
    _add_ff("ffmpeg.exe")
    _add_ff("ffprobe.exe")
elif _sys.platform == "darwin":
    _machine = _platform.machine().lower()
    if _machine in ("arm64", "aarch64"):
        _add_ff("ffmpeg-osx-arm64")
        _add_ff("ffprobe-osx-arm64")
    else:
        _add_ff("ffmpeg-osx-x64")
        _add_ff("ffprobe-osx-x64")
else:  # Linux
    _machine = _platform.machine().lower()
    if _machine in ("aarch64", "arm64"):
        _add_ff("ffmpeg-linux-arm64")
        _add_ff("ffprobe-linux-arm64")
    else:
        _add_ff("ffmpeg-linux-x64")
        _add_ff("ffprobe-linux-x64")

# ── Include HTML helper files ─────────────────────────────────────────────────
for _fn in ("blood_trails_formatter.html",):
    _fp = os.path.join(src_dir, _fn)
    if os.path.isfile(_fp):
        added_files.append((_fp, "."))

# ── Analysis ──────────────────────────────────────────────────────────────────
a = Analysis(
    ["main.py"],
    pathex=[src_dir],
    binaries=[],
    datas=added_files,
    hiddenimports=[
        "tkinter",
        "tkinter.ttk",
        "tkinter.filedialog",
        "tkinter.messagebox",
        "tkinter.scrolledtext",
        "tkinterdnd2",
        "pyaaf2",
        "faster_whisper",
        "ctranslate2",
        "numpy",
        "av",
        "soundfile",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["matplotlib", "PIL", "scipy", "pandas"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)   # noqa: F821

exe = EXE(   # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PostBridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon="postbridge.ico",   # Windows — supply a .ico to set taskbar icon
    # icon="postbridge.icns",  # macOS   — supply a .icns for the Dock icon
)

coll = COLLECT(   # noqa: F821
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PostBridge",
)

# ── macOS .app bundle ─────────────────────────────────────────────────────────
# Wraps the COLLECT output in a proper .app so it launches from Finder/Spotlight.
if _sys.platform == "darwin":
    app = BUNDLE(   # noqa: F821
        coll,
        name="PostBridge.app",
        # icon="postbridge.icns",
        bundle_identifier="com.meateater.postbridge",
        info_plist={
            "NSPrincipalClass":          "NSApplication",
            "NSHighResolutionCapable":   True,
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleName":              "PostBridge",
            "LSMinimumSystemVersion":    "11.0",
        },
    )
