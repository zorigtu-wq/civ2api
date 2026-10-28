@echo off
cd /d "%~dp0"
if not exist logs mkdir logs
python -X utf8 tools\smoke_win32.py > logs\smoke.txt 2>&1
echo exit=%errorlevel% >> logs\smoke.txt
