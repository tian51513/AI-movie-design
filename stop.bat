@echo off
rem Stop ALL comic_studio processes (uvicorn + python workers + hidden instances)
echo Stopping comic_studio...
taskkill /f /im uvicorn.exe >nul 2>nul
taskkill /f /im python.exe >nul 2>nul
taskkill /f /im pythonw.exe >nul 2>nul
timeout /t 2 /nobreak >nul

rem Verify port is free
netstat -ano | findstr :8190 | findstr LISTENING >nul 2>nul
if %errorlevel%==0 (
    echo WARNING: Port 8190 still in use!
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8190 ^| findstr LISTENING') do (
        echo Force killing PID %%a
        taskkill /f /pid %%a >nul 2>nul
    )
    timeout /t 1 /nobreak >nul
)

netstat -ano | findstr :8190 | findstr LISTENING >nul 2>nul
if %errorlevel%==0 (
    echo FAILED: Port 8190 still occupied
) else (
    echo Port 8190 cleared OK
)
echo Done.
pause
