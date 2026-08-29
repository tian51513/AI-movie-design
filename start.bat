@echo off
rem comic_studio Windows launcher - keep this window OPEN while using the app
cd /d %~dp0
where python >nul 2>nul || (echo [ERROR] python not found & pause & exit /b 1)
if not exist .venv-win (
  python -m venv .venv-win 2>nul || py -3 -m venv .venv-win
)
if not exist .venv-win\Scripts\uvicorn.exe (
  echo Installing deps (a few minutes)...
  .venv-win\Scripts\python -m pip install -e ".[dev]" || (echo [ERROR] install failed & pause & exit /b 1)
)
echo ====================================
echo   comic_studio running at http://localhost:8190
echo   Keep this window OPEN. Close it to stop.
echo ====================================
.venv-win\Scripts\uvicorn comic_studio.web.app:app --port 8190 --reload
echo [server exited] code=%errorlevel%
pause
