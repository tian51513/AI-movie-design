@echo off
rem Windows 快捷启动：自动建/修 .venv-win（含第三方 virtualenv 建的环境）并装依赖
cd /d %~dp0
where python >/dev/null 2>/dev/null || (echo [错误] 找不到 python，请安装 python.org 官方版 & pause & exit /b 1)
if not exist .venv-win (
  echo 首次运行：创建虚拟环境并安装依赖（几分钟）...
  python -m venv .venv-win 2>/dev/null || py -3 -m venv .venv-win
  if not exist .venv-win (echo [错误] venv 创建失败——当前 Python 缺 venv 模块，请安装 python.org 完整版 & pause & exit /b 1)
)
rem 判 uvicorn 是否就绪而非目录是否存在——手动/第三方 virtualenv 建的环境也能自愈
if not exist .venv-win\Scripts\uvicorn.exe (
  echo 依赖未就绪：安装中（几分钟）...
  .venv-win\Scripts\python -m pip install -e ".[dev]" || (echo [错误] 依赖安装失败 & pause & exit /b 1)
)
echo → http://localhost:8190
.venv-win\Scripts\uvicorn comic_studio.web.app:app --port 8190 --reload
pause
