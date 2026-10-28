@echo off
cd /d "%~dp0"
if not exist logs mkdir logs
python -X utf8 mvp_units.py > logs\units.txt 2>&1
echo exit=%errorlevel% >> logs\units.txt
