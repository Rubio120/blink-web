@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt
if errorlevel 1 pause & exit /b 1
python -m waitress --listen=0.0.0.0:5000 app:app
pause
