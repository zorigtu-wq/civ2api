@echo off
cd /d "%~dp0"
if not exist logs mkdir logs
python -X utf8 -m unittest tests\test_offline.py -v > logs\tests.txt 2>&1
echo exit=%errorlevel% >> logs\tests.txt
