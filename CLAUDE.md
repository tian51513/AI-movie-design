# comic_studio 开发约定

- 架构边界：`comic_studio/engine/` 禁止 import fastapi/starlette/uvicorn（未来抽取为 ComfyUI 节点）
- 测试：pytest，TDD（先失败测试后实现）；运行 `pytest -q`
- 安装：`pip install -e ".[dev]"`
- 启动：`uvicorn comic_studio.web.app:app --port 8190`（app 提供 `create_app(db_path)` 工厂）
- 数据：默认 `./data`（SQLite + library + projects），不入 git
- 文档：每个里程碑同步更新 README.md / CLAUDE.md / docs/superpowers/specs/ 状态
- 设计文档：docs/superpowers/specs/2026-08-23-novel-to-comic-design.md

## 模块地图（Phase 1）

- `comic_studio/engine/db.py` — Database（线程本地连接、WAL、8 表迁移）
- `comic_studio/engine/settings.py` — 配置默认值与读写（workers/llm_providers/llm_routing/template_map）
- `comic_studio/engine/projects.py` / `assets.py` / `jobs.py` — 三个仓库
- `comic_studio/engine/llm/` — provider（LLMClient/ask_validated/路由记账）、schemas、text（分块）、analyze（编排）
- `comic_studio/web/` — app 工厂 + routes_projects/routes_analyze/routes_assets
- `frontend/index.html` — Vue3 单页（本地 vendor，无 CDN）
- 测试反模式提醒：LLM 相关测试一律注入 FakeClient（替换 raw_chat），不触网
