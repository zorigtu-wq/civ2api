@echo off
cd /d "%~dp0"
if not exist logs mkdir logs
python -X utf8 mvp_move.py NE > logs\move_ne.txt 2>&1
echo exit=%errorlevel% >> logs\move_ne.txt
