@echo off
REM PostBridge — Windows build script
REM Run this from the PostBridge_Modular directory.
REM
REM Requirements:
REM   Python 3.10+ on PATH
REM   pip install pyinstaller
REM   pip install -r requirements.txt

setlocal EnableDelayedExpansion

echo.
echo ============================================================
echo  PostBridge Build
echo ============================================================
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found on PATH. Install Python 3.10+ and try again.
    pause & exit /b 1
)

REM Locate Python's Scripts folder so we can call pyinstaller.exe directly.
REM  python -m pyinstaller doesn't work on all Python versions (e.g. 3.14).
for /f "tokens=*" %%i in ('python -c "import sys,os; print(os.path.join(sys.prefix,'Scripts'))"') do set PY_SCRIPTS=%%i
set PATH=%PY_SCRIPTS%;%PATH%
echo Python Scripts: %PY_SCRIPTS%

REM Check / install PyInstaller
if not exist "%PY_SCRIPTS%\pyinstaller.exe" (
    echo Installing PyInstaller...
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo ERROR: pip install failed. Make sure Python was installed with pip included.
        pause & exit /b 1
    )
)

REM Install/update dependencies
echo Installing dependencies from requirements.txt...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: dependency install failed. Check requirements.txt and try again.
    pause & exit /b 1
)

REM ── Download ffmpeg static binaries (bundled inside the package) ─────────────
echo.
echo Fetching ffmpeg static binaries...
if not exist ffmpeg-bin mkdir ffmpeg-bin

set FFMPEG_URL=https://github.com/shaka-project/static-ffmpeg-binaries/releases/download/n7.1-2
set FFMPEG_EXE=ffmpeg-bin\ffmpeg.exe
set FFPROBE_EXE=ffmpeg-bin\ffprobe.exe

if not exist "%FFMPEG_EXE%" (
    echo   Downloading ffmpeg-win-x64.exe ...
    powershell -NoProfile -Command ^
        "Invoke-WebRequest -Uri '%FFMPEG_URL%/ffmpeg-win-x64.exe' -OutFile '%FFMPEG_EXE%' -UseBasicParsing"
    if errorlevel 1 (
        echo ERROR: ffmpeg download failed. Check your internet connection.
        pause & exit /b 1
    )
) else (
    echo   ffmpeg already present — skipping download.
)

if not exist "%FFPROBE_EXE%" (
    echo   Downloading ffprobe-win-x64.exe ...
    powershell -NoProfile -Command ^
        "Invoke-WebRequest -Uri '%FFMPEG_URL%/ffprobe-win-x64.exe' -OutFile '%FFPROBE_EXE%' -UseBasicParsing"
    if errorlevel 1 (
        echo ERROR: ffprobe download failed. Check your internet connection.
        pause & exit /b 1
    )
) else (
    echo   ffprobe already present — skipping download.
)
echo   ffmpeg binaries ready.

REM Clean previous build artefacts
if exist build\PostBridge  rmdir /s /q build\PostBridge
if exist dist\PostBridge   rmdir /s /q dist\PostBridge

REM Run PyInstaller
echo.
echo Running PyInstaller...
"%PY_SCRIPTS%\pyinstaller.exe" PostBridge.spec --noconfirm
if errorlevel 1 (
    echo ERROR: PyInstaller failed. See output above.
    pause & exit /b 1
)

echo.
echo ============================================================
echo  Build complete!  Output:  dist\PostBridge\
echo.
echo  To distribute internally:
echo    1. Zip the entire dist\PostBridge\ folder
echo    2. Recipients unzip and run PostBridge.exe — no Python needed
echo    3. ffmpeg is already included — no separate install needed
echo ============================================================
echo.
pause
