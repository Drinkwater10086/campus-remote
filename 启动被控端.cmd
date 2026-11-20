@echo off
cd /d "%~dp0"
python -B app.py host
if errorlevel 1 pause
