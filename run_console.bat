@echo off
cd /d "%~dp0"
start "" pythonw -m civ2api.server
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:8765/
