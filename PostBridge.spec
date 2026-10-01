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

# ── Optional GPU support: collect torch when present ──────────────────────────
# CPU build: torch isn't installed; this block is a no-op and the resulting
# bundle has no CUDA dependency.
# GPU build: a CUDA-matched torch wheel is installed before PyInstaller runs;
# `collect_all` pulls in torch's Python modules, data files, AND its CUDA
# DLLs (cuDNN, cuBLAS, cudart, etc.) that ctranslate2 needs at runtime.
_gpu_bundled = False
_torch_binaries = []
_torch_hiddenimports = []
try:
    import torch  # noqa: F401
    from PyInstaller.utils.hooks import collect_all as _collect_all_torch
    _t_datas, _t_binaries, _t_hidden = _collect_all_torch("torch")
    added_files.extend(_t_datas)
    _torch_binaries = _t_binaries
    _torch_hiddenimports = _t_hidden
    _gpu_bundled = True
    print("PostBridge build: torch detected — bundling CUDA support.")
except ImportError:
    print("PostBridge build: torch not installed — CPU-only bundle.")

# ── Bundle platform-specific ffmpeg/ffprobe static binaries ──────────────────
# The build scripts and CI workflows download these into ffmpeg-bin/ before
# PyInstaller runs, saved as plain ffmpeg / ffprobe (.exe on Windows) — the
# names ffmpeg_bundled._package_ffmpeg_paths() looks for first.  The release
# asset names (ffmpeg-osx-arm64, ...) are still accepted here and at runtime
# for older checkouts.  A missing binary is a hard error: a bundle without
# its own ffmpeg works on the build machine (ffmpeg on PATH) and then fails
# on every clean Mac with "This file has no audio track".
_ffbin = os.path.join(src_dir, "ffmpeg-bin")

def _add_ff(*names):
    for src_name in names:
        fp = os.path.join(_ffbin, src_name)
        if os.path.isfile(fp):
            added_files.append((fp, "ffmpeg"))
            return
    raise SystemExit(
        "PostBridge build: none of ffmpeg-bin/{} found — run the build "
        "script (build.sh / build.bat) first.".format(" / ".join(names)))

if _sys.platform == "win32":
    _add_ff("ffmpeg.exe")
    _add_ff("ffprobe.exe")
elif _sys.platform == "darwin":
    _machine = _platform.machine().lower()
    _arch = "arm64" if _machine in ("arm64", "aarch64") else "x64"
    _add_ff("ffmpeg",  "ffmpeg-osx-"  + _arch)
    _add_ff("ffprobe", "ffprobe-osx-" + _arch)
else:  # Linux
    _machine = _platform.machine().lower()
    _arch = "arm64" if _machine in ("aarch64", "arm64") else "x64"
    _add_ff("ffmpeg",  "ffmpeg-linux-"  + _arch)
    _add_ff("ffprobe", "ffprobe-linux-" + _arch)

# ── App assets: header logo + window icon ─────────────────────────────────────
# main.py loads these from <bundle>/assets at runtime.  They were never
# bundled before, so packaged builds silently showed no header logo.
for _fn in ("ME_HortLogo_OrgWht.png", os.path.join("icon", "PostBridge.png")):
    _fp = os.path.join(src_dir, "assets", _fn)
    if os.path.isfile(_fp):
        added_files.append((_fp, os.path.dirname(os.path.join("assets", _fn))))

# ── macOS code signing ────────────────────────────────────────────────────────
# PyInstaller signs everything itself: it has to, because it rewrites the main
# executable's Mach-O headers to carry the embedded Python archive and then
# signs it on the spot.  Re-signing that executable afterwards with a separate
# codesign pass produced a binary Apple's notary rejects ("The signature of the
# binary is invalid") even though local codesign --verify accepts it.
# PB_CODESIGN_IDENTITY="Developer ID Application: …" → hardened runtime +
# timestamp + entitlements, ready for notarization; unset → ad-hoc.
_codesign_identity = (os.environ.get("PB_CODESIGN_IDENTITY") or "").strip() or None
if _codesign_identity == "-":
    _codesign_identity = None
_entitlements = (os.path.join(src_dir, "macos", "entitlements.plist")
                 if _codesign_identity else None)

# App icon — regenerate with  python assets/icon/make_icons.py
_icon_dir = os.path.join(src_dir, "assets", "icon")
_icon_win = os.path.join(_icon_dir, "PostBridge.ico")
_icon_mac = os.path.join(_icon_dir, "PostBridge.icns")

# ── Include HTML helper files ─────────────────────────────────────────────────
for _fn in ("blood_trails_formatter.html",):
    _fp = os.path.join(src_dir, _fn)
    if os.path.isfile(_fp):
        added_files.append((_fp, "."))

# ── Bundle pre-downloaded Whisper models when present ─────────────────────────
# Build scripts may populate `models/` with tiny/base/small.  When the folder
# exists, drop it whole into the bundle root so engines._bundled_models_root()
# finds it at runtime.  Missing models (medium / large-v3) fall back to a
# normal HuggingFace download on first use.
_models_dir = os.path.join(src_dir, "models")
if os.path.isdir(_models_dir):
    for _root, _dirs, _files in os.walk(_models_dir):
        for _f in _files:
            _src = os.path.join(_root, _f)
            _rel = os.path.relpath(_src, src_dir)
            # dest is the directory inside the bundle (without the file name)
            added_files.append((_src, os.path.dirname(_rel)))
    print("PostBridge build: bundling models from {}.".format(_models_dir))

# ── Analysis ──────────────────────────────────────────────────────────────────
a = Analysis(
    ["main.py"],
    pathex=[src_dir],
    binaries=_torch_binaries,
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
    ] + _torch_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 'torch' is large and excluding heavy sci-py deps it doesn't need keeps
    # the CPU bundle slim AND avoids accidentally pulling them into the GPU
    # bundle.  torch's own deps (sympy, jinja2, networkx, mpmath) come along
    # via the collect_all() pull above when GPU is enabled.
    excludes=["matplotlib", "PIL", "scipy", "pandas"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)   # noqa: F821

# UPX corrupts torch's CUDA DLLs on Windows — disable it whenever torch is
# bundled.  CPU builds keep UPX on for size savings.
_use_upx = not _gpu_bundled

exe = EXE(   # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PostBridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=_use_upx,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=_codesign_identity,
    entitlements_file=_entitlements,
    # macOS: ship the Python archive (PKG) as a separate file in the bundle
    # instead of embedding it in the executable.  Embedding stretches the
    # executable's Mach-O string table over the archive; Apple's notary
    # rejected that executable as "The signature of the binary is invalid"
    # even though it signed and verified cleanly.  Side-loaded, the
    # executable is essentially PyInstaller's stock bootloader.
    append_pkg=(_sys.platform != "darwin"),
    icon=_icon_win if _sys.platform == "win32" else None,
)

coll = COLLECT(   # noqa: F821
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=_use_upx,
    upx_exclude=[],
    name="PostBridge",
)

# ── macOS .app bundle ─────────────────────────────────────────────────────────
# Wraps the COLLECT output in a proper .app so it launches from Finder/Spotlight.
if _sys.platform == "darwin":
    app = BUNDLE(   # noqa: F821
        coll,
        name="PostBridge.app",
        icon=_icon_mac,
        bundle_identifier="com.meateater.postbridge",
        info_plist={
            "NSPrincipalClass":          "NSApplication",
            "NSHighResolutionCapable":   True,
            "CFBundleShortVersionString": "1.0.0",
            # Run number + commit from CI (Finder → Get Info shows it), so
            # a copy of the app can be traced to the code it was built from.
            "CFBundleVersion": os.environ.get("PB_BUILD_VERSION", "local"),
            "CFBundleName":              "PostBridge",
            "LSMinimumSystemVersion":    "11.0",
        },
    )
