@echo off
setlocal
cd /d "%~dp0"
py -3 -m venv .venv
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail
if not exist config.json copy config.example.json config.json >nul
echo Setup complete. Read README.md and configure config.json.
pause
exit /b 0
:fail
echo Setup failed. Install Python 3.12 or newer, then retry.
pause
exit /b 1
