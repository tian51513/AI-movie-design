#!/usr/bin/env bash
# WSL/Linux 快捷启动：首次自动建 .venv 并装依赖，热重载
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
[ -x .venv/bin/uvicorn ] || .venv/bin/pip install -e ".[dev]"
echo "→ http://localhost:8190"
exec .venv/bin/uvicorn comic_studio.web.app:app --port 8190 --reload
