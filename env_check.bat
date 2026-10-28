@echo off
cd /d "%~dp0"
(
echo === where
where python
where py
echo === python
python -c "import sys,struct;print(sys.version, struct.calcsize(chr(80))*8, sys.executable)"
echo === py launcher
py -0p
echo === civ2
tasklist /fi "imagename eq civ2.exe"
) > env_check.txt 2>&1
