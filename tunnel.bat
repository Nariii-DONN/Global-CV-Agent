@echo off
setlocal
cd /d %~dp0
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -q -r requirements.txt
if not exist .env copy .env.example .env >nul
where cloudflared >nul 2>nul
if errorlevel 1 (
  echo.
  echo cloudflared not found. Install it once with:
  echo   winget install --id Cloudflare.cloudflared
  echo.
  echo Then run this file again.
  pause
  exit /b 1
)
echo.
echo Starting Global CV Agent on http://127.0.0.1:8787 ...
start "Global CV Agent" python server.py
timeout /t 4 /nobreak >nul
echo.
echo Opening Cloudflare tunnel — your public https URL appears below.
echo Keep this window open. Close the "Global CV Agent" window to stop the app.
echo.
cloudflared tunnel --url http://127.0.0.1:8787
