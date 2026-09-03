@echo off
rem Production start (2026-09-03): no hot-reload flag. Use for long
rem unattended batches / overnight renders (dev edits will NOT restart it).
rem Dev mode stays in start.bat. Everything else matches start.bat
rem (port-8190-only cleanup). ASCII + CRLF only: cmd misparses UTF-8/LF.
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
rem Kill only processes listening on port 8190 (old service cleanup).
rem 2026-09-02 incident: broad "taskkill /im python.exe" also killed ComfyUI.
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8190 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_ -Force }" >nul 2>nul
timeout /t 2 /nobreak >nul
echo comic_studio (prod) - http://localhost:8190
for /f "delims=" %%i in ('powershell -NoProfile -Command "Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.IPAddress -like '192.168.*'} | Select-Object -First 1 -ExpandProperty IPAddress"') do echo LAN: http://%%i:8190
.venv-win\Scripts\uvicorn comic_studio.web.app:app --host 0.0.0.0 --port 8190 --log-level warning
pause
