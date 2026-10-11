@echo off
setlocal
rem Start Command Code with a loopback-only debugging port so the readout can attach.
rem A host that is already running is never restarted by this script.
cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if exist "%VENV_PY%" (
  "%VENV_PY%" "%~dp0entry.py" --launch
  goto end
)

python "%~dp0entry.py" --launch
if errorlevel 1 echo Run install.ps1 first if Python is not on PATH.

:end
endlocal
