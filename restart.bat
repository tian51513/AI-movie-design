@echo off
taskkill /f /im uvicorn.exe >nul 2>nul
timeout /t 2 /nobreak >nul
start /min "" .venv-win\Scripts\uvicorn comic_studio.web.app:app --port 8190 --reload >> start.log 2>&1
