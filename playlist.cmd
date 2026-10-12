@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" goto setup
start "" ".venv\Scripts\pythonw.exe" "src\gui-launcher.py"
exit /b 0
:setup
echo Run setup.cmd first.
pause
exit /b 2
