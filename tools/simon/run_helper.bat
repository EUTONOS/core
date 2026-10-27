@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" agent_helper.py
) else (
  python agent_helper.py
)
if errorlevel 1 pause
