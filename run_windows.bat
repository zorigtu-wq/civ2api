@echo off
cd /d "%~dp0"
if not exist logs mkdir logs
python -X utf8 tools\list_windows.py > logs\windows.txt 2>&1
