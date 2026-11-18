@echo off
rem Launch Orchestrator without a console window.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Missing .venv. Create it first: py -3.13 -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "run.py" %*
