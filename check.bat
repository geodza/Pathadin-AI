@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run run.bat first to create the environment.
  exit /b 1
)
".venv\Scripts\python.exe" diagnostics.py
".venv\Scripts\python.exe" -m unittest discover -s tests -v
pause
