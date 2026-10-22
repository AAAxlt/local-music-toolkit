@echo off
setlocal
if not exist "%~dp0.runtime\decoder\installed.json" (
  call "%~dp0bootstrap-decoder.cmd"
  if errorlevel 1 exit /b 1
)
"%~dp0.runtime\decoder\python\python.exe" -I "%~dp0src\music-decode.py" %*
set "decoder_result=%errorlevel%"
if "%~1"=="" pause
exit /b %decoder_result%
