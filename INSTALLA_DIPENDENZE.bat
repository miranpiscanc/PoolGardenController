@echo off
cd /d %~dp0
echo Installazione dipendenze Python...
python -m pip install -r requirements.txt
pause
