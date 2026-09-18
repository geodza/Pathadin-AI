@echo off
setlocal
cd /d "%~dp0"
if not exist ".env" copy /y ".env.example" ".env" >nul
if not exist ".env" goto fail
echo Paste your key after OPENAI_API_KEY= in Notepad.
echo Save the file, close Notepad, and restart Pathadin.
notepad.exe "%~dp0.env"
exit /b 0
:fail
echo Could not create .env. Extract the ZIP into a folder you can write to.
pause
exit /b 1
