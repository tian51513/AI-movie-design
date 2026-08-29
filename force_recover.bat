@echo off
echo === Force recovery: kill ALL Python, clear locks, restart ===
echo Step 1: Kill ALL Python processes (including zombies)
taskkill /f /im python.exe >nul 2>nul
taskkill /f /im uvicorn.exe >nul 2>nul
taskkill /f /im pythonw.exe >nul 2>nul
timeout /t 3 /nobreak >nul

echo Step 2: Clear WAL/SHM
del /f data\studio.db-wal 2>nul
del /f data\studio.db-shm 2>nul
if exist data\studio.db-wal (
    echo WARNING: WAL still locked!
) else (
    echo WAL cleared OK
)

echo Step 3: Restart server
start /min "" .venv-win\Scripts\uvicorn comic_studio.web.app:app --port 8190 --reload >> start.log 2>&1
timeout /t 4 /nobreak >nul

echo === Try importing comic now ===
pause
