@echo off
setlocal
rem Start Command Code with a loopback-only debugging port so the readout can attach.
rem A host that is already running is never restarted by this script, so the exit
rem code is passed through: 0 ready or launched, 2 host running without a port,
rem 1 host executable not found.
rem Messages stay ASCII because cmd.exe decodes this file in the OEM codepage.
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" "%~dp0entry.py" --launch
set "RC=%ERRORLEVEL%"

if "%RC%"=="2" echo Refused: Command Code is still running without a debugging port. Quit it fully ^(tray too^), then run this again.
if "%RC%"=="1" echo Not found: locate Command Code.exe, or run install.ps1 first.

endlocal & exit /b %RC%
