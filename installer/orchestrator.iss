; Inno Setup 6 script: per-user install of the one-folder build (no admin rights needed).
; Built by scripts\build.ps1 when ISCC.exe is available.
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{6F7B1E2A-0C1D-4E52-9B7E-3A1F0B9C2D11}
AppName=Orchestrator
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\Orchestrator
DefaultGroupName=Orchestrator
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Orchestrator-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
SetupIconFile=..\build\orchestrator.ico
UninstallDisplayIcon={app}\Orchestrator.exe
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "..\dist\Orchestrator\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Orchestrator"; Filename: "{app}\Orchestrator.exe"; AppUserModelID: "Orchestrator.App"
Name: "{userdesktop}\Orchestrator"; Filename: "{app}\Orchestrator.exe"; Tasks: desktopicon; AppUserModelID: "Orchestrator.App"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\Orchestrator.exe"; Description: "Start Orchestrator"; Flags: nowait postinstall skipifsilent
