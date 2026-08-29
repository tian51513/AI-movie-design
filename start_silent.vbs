' comic_studio silent launcher: no window, logs to start.log
Dim sh: Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = Replace(WScript.ScriptFullName, "start_silent.vbs", "")
sh.Run "cmd /c .venv-win\Scripts\uvicorn comic_studio.web.app:app --port 8190 --reload >> start.log 2>&1", 0, False
