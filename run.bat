@echo off
setlocal
cd /d %~dp0
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
if not exist .env copy .env.example .env >nul
echo.
echo Global CV Agent is starting...
python server.py
pause
