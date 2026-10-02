; PostBridge — Windows installer (Inno Setup 6)
;
; Wraps the PyInstaller output folder (dist\PostBridge) into a single
; Setup.exe, so users install PostBridge like any other Windows app instead
; of unzipping a folder of thousands of files and running it in place.
;
; Build (from the repo root, after `pyinstaller PostBridge.spec`):
;   iscc installer\PostBridge.iss
;   iscc /DAppVersion=1.0.0.42 /DOutputName=PostBridge-GPU-Setup /DSpan=1 installer\PostBridge.iss
;
; Defines:
;   AppVersion  version shown in Add/Remove Programs (default 1.0.0)
;   OutputName  file name of the Setup exe, without .exe
;   Span        1 = split the payload into Setup.exe + .bin files.  Needed
;               for the GPU build: a single Setup.exe tops out around 2 GB
;               and the CUDA bundle is bigger than that.  The .bin files
;               must stay next to Setup.exe when it runs.
;
; Installs per user (no admin prompt) into %LOCALAPPDATA%\Programs\PostBridge.
; That's safe because the packaged app never writes next to itself: logs and
; caches go to %APPDATA%\PostBridge (utils.app_state_dir) and Whisper models
; to ~\.postbridge_models.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef OutputName
  #define OutputName "PostBridge-Setup"
#endif
#ifndef Span
  #define Span 0
#endif
#define SourceDir "..\dist\PostBridge"

[Setup]
; AppId identifies the install for upgrades and uninstall — never change it.
AppId={{EC0B0234-82D7-49E0-B2D8-E47B5C8DA580}
AppName=PostBridge
AppVersion={#AppVersion}
AppPublisher=MeatEater
DefaultDirName={autopf}\PostBridge
DefaultGroupName=PostBridge
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=..\assets\icon\PostBridge.ico
UninstallDisplayIcon={app}\PostBridge.exe
OutputDir=..\installer-out
OutputBaseFilename={#OutputName}
; The bundle is mostly already-compressed DLLs and model weights; fast LZMA2
; gets nearly the same size as the slow presets in a fraction of the time.
Compression=lzma2/fast
SolidCompression=no
LZMANumBlockThreads=4
#if Span
DiskSpanning=yes
DiskSliceSize=max
#endif
; Upgrading over a running copy would leave a half-replaced install.
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; Clear the previous version's files first, so a module dropped between
; builds can't linger and get picked up by the new one.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\PostBridge"; Filename: "{app}\PostBridge.exe"
Name: "{autodesktop}\PostBridge"; Filename: "{app}\PostBridge.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\PostBridge.exe"; Description: "{cm:LaunchProgram,PostBridge}"; Flags: nowait postinstall skipifsilent
