@echo off
rem Launch Orchestrator without a console window.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Missing .venv. Create it first: py -3.13 -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)
rem With no arguments, prefer the Start menu shortcut (Settings > Background): same icon and taskbar identity
rem as a pinned Orchestrator.
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Orchestrator.lnk"
if "%~1"=="" if exist "%LNK%" (
  start "" "%LNK%"
  exit /b 0
)
start "" ".venv\Scripts\pythonw.exe" "run.py" %*
