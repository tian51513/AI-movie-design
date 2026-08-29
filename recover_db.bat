@echo off
echo === comic_studio database recovery ===
echo Step 1: Stop server
taskkill /f /im uvicorn.exe >nul 2>nul
timeout /t 2 /nobreak >nul

echo Step 2: Clear stale WAL/SHM files
if exist data\studio.db-wal del /f data\studio.db-wal
if exist data\studio.db-shm del /f data\studio.db-shm
echo Done.

echo Step 3: Verify database integrity
.venv-win\Scripts\python -c "import sqlite3; db=sqlite3.connect('data/studio.db'); print('quick_check:', db.execute('PRAGMA quick_check').fetchone()[0]); db.close()"

echo Step 4: Restart server
start /min "" .venv-win\Scripts\uvicorn comic_studio.web.app:app --port 8190 --reload >> start.log 2>&1
timeout /t 3 /nobreak >nul
echo === Done! Try importing comic again ===
pause
