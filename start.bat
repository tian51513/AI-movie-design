@echo off
rem Windows 快捷启动：首次自动建 .venv-win 并装依赖，热重载
cd /d %~dp0
if not exist .venv-win python -m venv .venv-win
if not exist .venv-win\Scripts\uvicorn.exe .venv-win\Scripts\pip install -e ".[dev]"
echo → http://localhost:8190
.venv-win\Scripts\uvicorn comic_studio.web.app:app --port 8190 --reload
