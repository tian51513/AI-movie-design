@echo off
rem stop comic_studio server (both modes)
taskkill /f /im uvicorn.exe 2>nul
echo done.
pause
