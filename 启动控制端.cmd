@echo off
cd /d "%~dp0"
python -B app.py viewer
if errorlevel 1 pause
