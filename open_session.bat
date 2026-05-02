@echo off
REM PostBridge — session launcher
REM
REM Forwards the dropped/double-clicked JSON file path to main.py.  This is
REM the file you point Windows at when registering a "Open with PostBridge"
REM association (right-click a .json -> Open with -> Choose another app ->
REM Browse to this .bat).
REM
REM Usage:
REM   open_session.bat                  # launches PostBridge with no session
REM   open_session.bat path\to\file.json  # launches and auto-loads the session

setlocal
set SCRIPT_DIR=%~dp0
pushd "%SCRIPT_DIR%"
start "" pythonw main.py %*
popd
endlocal
