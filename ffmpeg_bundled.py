"""
Bundled ffmpeg/ffprobe: use system binaries if available, otherwise download
static builds to an app directory and use those. Runs under the hood with no
user prompts.
"""
import os
import ssl
import sys
import stat
import subprocess
from utils import run_hidden
import urllib.request

# Shaka-project static builds (n7.1-2): one ffmpeg + one ffprobe per platform
_BASE = "https://github.com/shaka-project/static-ffmpeg-binaries/releases/download/n7.1-2"
_ASSETS = {
    ("darwin", "arm64"): ("ffmpeg-osx-arm64", "ffprobe-osx-arm64"),
    ("darwin", "x86_64"): ("ffmpeg-osx-x64", "ffprobe-osx-x64"),
    ("win32", "AMD64"): ("ffmpeg-win-x64.exe", "ffprobe-win-x64.exe"),
    ("linux", "x86_64"): ("ffmpeg-linux-x64", "ffprobe-linux-x64"),
    ("linux", "aarch64"): ("ffmpeg-linux-arm64", "ffprobe-linux-arm64"),
    ("linux", "arm64"): ("ffmpeg-linux-arm64", "ffprobe-linux-arm64"),
}

_ffmpeg_path = None
_ffprobe_path = None
_ensured = False
_ensure_failed_at = None
_last_ensure_error = None

# A failed auto-download is retried after this long rather than never:
# one blip in connectivity at launch used to disable ffmpeg for the
# whole session.  Not retried on every call, because a probe asks for
# the ffprobe path every time it runs and each attempt can block 120 s.
_ENSURE_RETRY_S = 300

# Set by the build's self-test so a runner with ffmpeg on PATH or in
# Homebrew cannot mask a bundle that is missing its own copy.
_BUNDLED_ONLY_ENV = "PB_FFMPEG_BUNDLED_ONLY"


def _bundled_only():
    return os.environ.get(_BUNDLED_ONLY_ENV) == "1"


def _app_ffmpeg_dir():
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "PostBridge", "ffmpeg")
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        return os.path.join(base, "PostBridge", "ffmpeg")
    return os.path.join(os.path.expanduser("~"), ".local", "share", "PostBridge", "ffmpeg")


def platform_machine():
    try:
        import platform
        m = (platform.machine() or "").strip()
        if m.upper() in ("AMD64", "X64"):
            return "x86_64"
        if m.lower() in ("aarch64", "arm64"):
            return "arm64" if sys.platform != "linux" else m.lower()
        return m or "x86_64"
    except Exception:
        return "x86_64"


def _platform_key():
    if sys.platform == "win32":
        machine = os.environ.get("PROCESSOR_ARCHITEW6432", os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64")) or "AMD64"
        return (sys.platform, machine)
    machine = platform_machine()
    if hasattr(os, "uname") and os.uname():
        machine = (os.uname().machine or machine).strip()
        if machine.upper() in ("AMD64", "X86_64"):
            machine = "x86_64"
        if machine.lower() == "aarch64":
            machine = "arm64"
    return (sys.platform, machine)


def _system_ffmpeg_ok():
    try:
        r = run_hidden(["ffmpeg", "-version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _system_ffprobe_ok():
    try:
        r = run_hidden(["ffprobe", "-version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _brew_ffmpeg_paths():
    """On macOS, try Homebrew's ffmpeg (often not on PATH when app is launched from Finder)."""
    if sys.platform != "darwin":
        return None, None
    try:
        r = run_hidden(
            ["brew", "--prefix", "ffmpeg"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0 or not (r.stdout or "").strip():
            return None, None
        prefix = (r.stdout or "").strip()
        ff = os.path.join(prefix, "bin", "ffmpeg")
        fp = os.path.join(prefix, "bin", "ffprobe")
        if os.path.isfile(ff) and os.path.isfile(fp):
            return ff, fp
    except Exception:
        pass
    return None, None


def _bundled_paths():
    d = _app_ffmpeg_dir()
    exe = ".exe" if sys.platform == "win32" else ""
    return (
        os.path.join(d, "ffmpeg" + exe),
        os.path.join(d, "ffprobe" + exe),
    )


def _package_ffmpeg_paths():
    """
    When running as a PyInstaller bundle, look for ffmpeg binaries pre-shipped
    in a 'ffmpeg/' subdirectory inside the package.

    Handles three layouts:
      • Windows / Linux one-folder:  <exe_dir>/ffmpeg/ffmpeg[.exe]
      • macOS one-folder:            <exe_dir>/ffmpeg/ffmpeg
      • macOS .app bundle:           Contents/MacOS/../ffmpeg/ffmpeg
                                     i.e. Contents/ffmpeg/ffmpeg

    In development (non-frozen) always returns (None, None) so the normal
    system-PATH / download fallback chain runs instead.
    """
    if not getattr(sys, "frozen", False):
        return None, None
    ext = ".exe" if sys.platform == "win32" else ""
    exe_dir = os.path.dirname(sys.executable)

    candidates = [
        os.path.join(exe_dir, "ffmpeg"),                        # Windows / Linux flat folder
        os.path.join(exe_dir, "_internal", "ffmpeg"),           # PyInstaller ≥ 6 _internal layout
    ]
    # Wherever PyInstaller actually unpacked to (Contents/Frameworks inside
    # a PyInstaller ≥ 6 .app) — the authoritative root, whatever the layout.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.insert(0, os.path.join(meipass, "ffmpeg"))
    if sys.platform == "darwin":
        # Inside a .app bundle sys.executable is Contents/MacOS/PostBridge
        contents_dir = os.path.dirname(exe_dir)                 # → Contents/
        candidates += [
            os.path.join(contents_dir, "ffmpeg"),               # Contents/ffmpeg/
            os.path.join(contents_dir, "Resources", "ffmpeg"),  # Contents/Resources/ffmpeg/
        ]

    # build.bat renames the Windows binaries to ffmpeg.exe / ffprobe.exe, but
    # build.sh and PostBridge.spec ship the macOS / Linux ones under their
    # release asset names (ffmpeg-osx-arm64, ...).  Accept both, or the
    # bundled copy is never found and every probe silently fails.
    names = [("ffmpeg" + ext, "ffprobe" + ext)]
    assets = _ASSETS.get(_platform_key())
    if assets:
        names.append(assets)
    for d in candidates:
        for ff_name, ffp_name in names:
            ff  = os.path.join(d, ff_name)
            ffp = os.path.join(d, ffp_name)
            if os.path.isfile(ff) and os.path.isfile(ffp):
                _ensure_executable(ff)
                _ensure_executable(ffp)
                return ff, ffp
    return None, None


def _ensure_executable(path):
    """Restore the exec bit if packaging dropped it (a no-op on Windows)."""
    if sys.platform == "win32":
        return
    try:
        if not os.access(path, os.X_OK):
            os.chmod(path, os.stat(path).st_mode
                     | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def _bundled_exists():
    fp, fpp = _bundled_paths()
    return os.path.isfile(fp) and os.path.isfile(fpp)


def _ssl_context():
    """SSL context for HTTPS download. Works around macOS Python cert issues."""
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx
    except Exception:
        pass
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        return ctx
    except Exception:
        pass
    # Fallback: unverified only for our known GitHub URL (macOS python.org often has no certs)
    return ssl._create_unverified_context()


def _download_binary(url, dest_path):
    req = urllib.request.Request(url, headers={"User-Agent": "PostBridge/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120, context=_ssl_context()) as resp:
            data = resp.read()
    except ssl.SSLError:
        # Retry with unverified context (e.g. macOS Python without certs)
        with urllib.request.urlopen(req, timeout=120, context=ssl._create_unverified_context()) as resp:
            data = resp.read()
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "wb") as f:
        f.write(data)
    if sys.platform != "win32":
        os.chmod(dest_path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)


def _ensure_bundled():
    """Download ffmpeg and ffprobe to app dir if not present. Returns True if usable."""
    global _ensured, _ensure_failed_at, _last_ensure_error
    import time
    if _ensured:
        return _bundled_exists()
    if _bundled_exists():
        _ensured = True
        return True
    if (_ensure_failed_at is not None
            and time.monotonic() - _ensure_failed_at < _ENSURE_RETRY_S):
        return False
    _last_ensure_error = None
    _ensure_failed_at = time.monotonic()     # cleared below on success
    key = _platform_key()
    machine = key[1]
    # Normalise macOS/Linux machine strings; Windows uses "AMD64" directly in _ASSETS
    if sys.platform != "win32" and machine.upper() in ("AMD64", "X64"):
        key = (key[0], "x86_64")
    elif machine.lower() == "aarch64":
        key = (key[0], "arm64")
    assets = _ASSETS.get(key)
    if not assets:
        _last_ensure_error = "Unsupported platform: {} {}".format(sys.platform, machine)
        return False
    ffmpeg_asset, ffprobe_asset = assets
    ext          = ".exe" if sys.platform == "win32" else ""
    dest_ffmpeg  = os.path.join(_app_ffmpeg_dir(), "ffmpeg"  + ext)
    dest_ffprobe = os.path.join(_app_ffmpeg_dir(), "ffprobe" + ext)
    try:
        _download_binary("{}/{}".format(_BASE, ffmpeg_asset), dest_ffmpeg)
        _download_binary("{}/{}".format(_BASE, ffprobe_asset), dest_ffprobe)
        ok = os.path.isfile(dest_ffmpeg) and os.path.isfile(dest_ffprobe)
        if ok:
            _ensured = True
            _ensure_failed_at = None
        return ok
    except Exception as e:
        _last_ensure_error = str(e)
        try:
            if os.path.isfile(dest_ffmpeg):
                os.remove(dest_ffmpeg)
            if os.path.isfile(dest_ffprobe):
                os.remove(dest_ffprobe)
        except Exception:
            pass
        return False


def get_ffmpeg_cmd():
    """Return the ffmpeg command as a list for subprocess (e.g. [path] or ['ffmpeg'])."""
    global _ffmpeg_path
    if _ffmpeg_path is not None:
        return [_ffmpeg_path]
    # 1. Binaries pre-shipped inside the PyInstaller package (dist/PostBridge/ffmpeg/)
    pkg_ff, _ = _package_ffmpeg_paths()
    if pkg_ff:
        _ffmpeg_path = pkg_ff
        return [pkg_ff]
    if _bundled_only():
        return ["ffmpeg"]
    # 2. System PATH
    if _system_ffmpeg_ok():
        _ffmpeg_path = "ffmpeg"
        return ["ffmpeg"]
    # 3. Homebrew (macOS)
    brew_ff, brew_fp = _brew_ffmpeg_paths()
    if brew_ff and brew_fp:
        _ffmpeg_path = brew_ff
        return [brew_ff]
    # 4. Auto-download to APPDATA (fallback — requires internet)
    if _ensure_bundled():
        _ffmpeg_path = _bundled_paths()[0]
        return [_ffmpeg_path]
    return ["ffmpeg"]


def get_ffprobe_cmd():
    """Return the ffprobe command as a list for subprocess."""
    global _ffprobe_path
    if _ffprobe_path is not None:
        return [_ffprobe_path]
    # 1. Binaries pre-shipped inside the PyInstaller package
    _, pkg_ffp = _package_ffmpeg_paths()
    if pkg_ffp:
        _ffprobe_path = pkg_ffp
        return [pkg_ffp]
    if _bundled_only():
        return ["ffprobe"]
    # 2. System PATH
    if _system_ffprobe_ok():
        _ffprobe_path = "ffprobe"
        return ["ffprobe"]
    # 3. Homebrew (macOS)
    brew_ff, brew_fp = _brew_ffmpeg_paths()
    if brew_ff and brew_fp:
        _ffprobe_path = brew_fp
        return [brew_fp]
    # 4. Auto-download to APPDATA (fallback — requires internet)
    if _ensure_bundled():
        _ffprobe_path = _bundled_paths()[1]
        return [_ffprobe_path]
    return ["ffprobe"]


def check_ffmpeg():
    """
    Ensure ffmpeg (and ffprobe) are available (system, Homebrew on Mac, or download).
    Returns (True, "") if ready; (False, error_message) if not.
    """
    global _last_ensure_error
    try:
        cmd = get_ffmpeg_cmd()
        r = run_hidden(cmd + ["-version"], capture_output=True, timeout=10)
        if r.returncode != 0:
            return False, "ffmpeg failed to run"
        cmd_p = get_ffprobe_cmd()
        r2 = run_hidden(cmd_p + ["-version"], capture_output=True, timeout=10)
        if r2.returncode != 0:
            return False, "ffprobe failed to run"
        return True, ""
    except FileNotFoundError:
        detail = _last_ensure_error or "download failed (check internet) or platform not supported"
        if sys.platform == "darwin":
            return False, (
                "ffmpeg is required but was not found.\n\n"
                "• Install with Homebrew:  brew install ffmpeg\n"
                "  Then restart PostBridge (or run it from Terminal).\n\n"
                "• Auto-download failed: {}"
            ).format(detail)
        return False, "ffmpeg is required. Install it and add it to PATH. (Auto-download: {}.)".format(detail)
    except Exception as e:
        return False, str(e)
