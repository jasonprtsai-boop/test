@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Project Python environment not found. See README.md for setup.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" "main_gui.py"
if errorlevel 1 pause
