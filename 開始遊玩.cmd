@echo off
cd /d "%~dp0"
node site\play.cjs --open
if errorlevel 1 pause
