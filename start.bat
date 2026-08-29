@echo off
cd /d %~dp0
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo ERROR: python not found
    pause
    exit /b 1
)
if not exist .venv-win (
    python -m venv .venv-win
)
taskkill /f /im uvicorn.exe >nul 2>nul
taskkill /f /im python.exe >nul 2>nul
timeout /t 2 /nobreak >nul
echo comic_studio - http://localhost:8190
.venv-win\Scripts\uvicorn comic_studio.web.app:app --port 8190 --reload --log-level warning
pause
