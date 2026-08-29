@echo off
rem comic_studio Windows launcher - auto-restart, window stays open
cd /d %~dp0
where python >nul 2>nul || (echo [ERROR] python not found & pause & exit /b 1)
if not exist .venv-win (
  python -m venv .venv-win 2>nul || py -3 -m venv .venv-win
)
if not exist .venv-win\Scripts\uvicorn.exe (
  echo Installing deps...
  .venv-win\Scripts\python -m pip install -e ".[dev]"
)
taskkill /f /im uvicorn.exe >nul 2>nul
timeout /t 2 /nobreak >nul
echo ====================================
echo   comic_studio - http://localhost:8190
echo   Keep this window OPEN.
echo ====================================
.venv-win\Scripts\uvicorn comic_studio.web.app:app --port 8190 --reload
echo.
echo [server exited] code=%errorlevel%
echo.
pause
