@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto setup
".venv\Scripts\python.exe" "src\refresh-library.py" %*
set RESULT=%ERRORLEVEL%
pause
exit /b %RESULT%
:setup
echo Run setup.cmd first.
pause
exit /b 2
