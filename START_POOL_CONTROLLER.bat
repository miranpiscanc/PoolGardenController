@echo off
cd /d %~dp0
echo Avvio Pool ^& Garden Controller...
echo Pagina web: http://192.168.10.135:5000
python app.py
pause
