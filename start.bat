@echo off
rem Windows launcher: auto-create/repair .venv-win (works with 3rd-party virtualenv too)
cd /d %~dp0
where python >nul 2>nul || (echo [ERROR] python not found - install from python.org & pause & exit /b 1)
if not exist .venv-win (
  echo First run: creating venv and installing deps (a few minutes)...
  python -m venv .venv-win 2>nul || py -3 -m venv .venv-win
  if not exist .venv-win (echo [ERROR] venv creation failed & pause & exit /b 1)
)
rem Check uvicorn readiness (not dir existence) - self-heals manually created venvs
if not exist .venv-win\Scripts\uvicorn.exe (
  echo Deps missing: installing (a few minutes)...
  .venv-win\Scripts\python -m pip install -e ".[dev]" || (echo [ERROR] install failed & pause & exit /b 1)
)
echo Open http://localhost:8190
.venv-win\Scripts\uvicorn comic_studio.web.app:app --port 8190 --reload
pause
