@echo off
cd /d "%~dp0"

if exist ".venv\Scripts\pythonw.exe" (
  set "PYW=.venv\Scripts\pythonw.exe"
) else (
  set "PYW=pythonw.exe"
)

start "Simon Target" "%PYW%" "%~dp0simon_target.py"
timeout /t 1 /nobreak >nul
start "Simon" "%PYW%" "%~dp0agent_helper.py"

echo Launched Simon Target and Simon.
echo If no windows appeared, run run_target.bat and run_helper.bat to see errors.
