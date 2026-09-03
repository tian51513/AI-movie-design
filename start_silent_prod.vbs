' comic_studio silent PROD launcher (2026-09-03): no hot-reload flag,
' LAN-enabled (0.0.0.0). For unattended long batches / overnight renders.
' Dev variant (with reload) stays in start_silent.vbs.
Dim sh: Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = Replace(WScript.ScriptFullName, "start_silent_prod.vbs", "")
sh.Run "cmd /c taskkill /f /im uvicorn.exe >nul 2>nul & timeout /t 1 /nobreak >nul & .venv-win\Scripts\uvicorn comic_studio.web.app:app --host 0.0.0.0 --port 8190 >> start-prod.log 2>&1", 0, False
