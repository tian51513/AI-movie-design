@echo off
rem 生产启动（2026-09-03 用户需求）：不带热重载参数——挂机跑长批次/过夜渲染
rem 不会被文件改动重启（开发用 start.bat）。其余与 start.bat 一致（按端口清旧服务）。
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
rem 2026-09-02 incident: broad "taskkill /im python.exe" also killed ComfyUI
rem (an unrelated python app) on every restart. Guarded by tests/test_start_scripts.py
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8190 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_ -Force }" >nul 2>nul
timeout /t 2 /nobreak >nul
echo comic_studio (prod) - http://localhost:8190
for /f "delims=" %%i in ('powershell -NoProfile -Command "Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.IPAddress -like '192.168.*'} | Select-Object -First 1 -ExpandProperty IPAddress"') do echo LAN: http://%%i:8190
.venv-win\Scripts\uvicorn comic_studio.web.app:app --host 0.0.0.0 --port 8190 --log-level warning
pause
