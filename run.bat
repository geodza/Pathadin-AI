@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto launch
where py >nul 2>nul
if errorlevel 1 goto try_python
py -3 setup.py
if errorlevel 1 goto fail
goto launch
:try_python
where python >nul 2>nul
if errorlevel 1 goto no_python
python setup.py
if errorlevel 1 goto fail
:launch
".venv\Scripts\python.exe" launch.py %*
if errorlevel 1 goto fail
exit /b 0
:no_python
echo Install Python 3.11 or newer, then run this file again.
:fail
pause
exit /b 1
