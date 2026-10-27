@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" simon_target.py
) else (
  python simon_target.py
)
if errorlevel 1 pause
