@echo off
cd /d "%~dp0site"
".venv\Scripts\python.exe" vision_preview.py
if errorlevel 1 pause
