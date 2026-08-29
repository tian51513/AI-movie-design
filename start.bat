@echo off
rem comic_studio launcher
cd /d %~dp0
where python >/dev/null 2>/dev/null || (echo [ERROR] python not found ^& pause ^& exit /b 1)
if not exist .venv-win (
  python -m venv .venv-win 2>/dev/null || py -3 -m venv .venv-win
)
if not exist .venv-win\Scripts\uvicorn.exe (
  echo Installing deps...
  .venv-win\Scripts\python -m pip install -e ".[dev]"
)
taskkill /f /im uvicorn.exe >/dev/null 2>nul
taskkill /f /im python.exe >/dev/null 2>nul
timeout /t 2 /nobreak >nul
echo comic_studio - http://localhost:8190
.venv-win\Scripts\uvicorn comic_studio.web.app:app --port 8190 --reload --log-level warning
pause
