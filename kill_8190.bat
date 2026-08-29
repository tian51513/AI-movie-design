@echo off
echo === Kill ALL processes on port 8190 ===
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8190 ^| findstr LISTENING') do (
    echo Killing PID %%a
    taskkill /f /pid %%a 2>nul
)
timeout /t 2 /nobreak >nul
netstat -ano | findstr :8190 | findstr LISTENING && echo STILL RUNNING || echo Port 8190 clear
pause
