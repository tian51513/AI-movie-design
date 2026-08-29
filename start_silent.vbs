' comic_studio silent launcher: kills old instance first, then starts hidden
Dim sh: Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = Replace(WScript.ScriptFullName, "start_silent.vbs", "")
sh.Run "cmd /c taskkill /f /im uvicorn.exe >nul 2>nul & timeout /t 1 /nobreak >nul & .venv-win\Scripts\uvicorn comic_studio.web.app:app --port 8190 --reload >> start.log 2>&1", 0, False
