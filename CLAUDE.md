# comic_studio 开发约定

- 架构边界：`comic_studio/engine/` 禁止 import fastapi/starlette/uvicorn（未来抽取为 ComfyUI 节点）
- 测试：pytest，TDD（先失败测试后实现）；运行 `pytest -q`
- 安装：WSL 用 `.venv`、Windows 原生用 `.venv-win`（二进制不可混装）；激活后 `pip install -e ".[dev]"`
- 跨环境：DB 存相对 data 根的 POSIX 路径（engine/paths.py），WSL 与 Windows 可共享同一 data/
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

## 模块地图（Phase 2）

- `comic_studio/engine/comfy/client.py` — ComfyClient（健康/上传/提交/轮询/下载/释放/失速interrupt）
- `comic_studio/engine/workflows/` — registry（manifest 扫描/类型映射）+ filler（注入纯函数）
- `comic_studio/engine/queue/worker.py` — worker 线程 + @register 处理器注册表
- `comic_studio/engine/genref.py` — gen_ref 处理器（@register("gen_ref")）
- `comic_studio/engine/jobs.py` — 队列原语（enqueue/claim 互斥/retry_or_fail/requeue_on_restart）
- `templates/workflows/` — 模板目录（README 有导出指南）
- 测试反模式提醒：ComfyUI 相关测试一律用 tests/comfy_mock.py 的 comfy_server，不连真实 ComfyUI
- 测试反模式提醒：API 测试一律 create_app(start_workers=False)

## 模块地图（Phase 3）

- `comic_studio/engine/shots.py` — 分镜仓库（persist/list/update/mark_stale_for_asset）
- `comic_studio/engine/pipeline_jobs.py` — 阶段门禁状态机 + 分镜/提示词 batch 处理器
- `comic_studio/engine/llm/storyboard.py` — 分镜拆解（LLM 调用、章节分块、结果合并）
- `comic_studio/engine/prompts/gen.py` — 分镜→H3 视频提示词适配器（vendor 技能规程 + mechanical 校验）
- 测试反模式提醒：LLM 分镜/提示词测试注入 FakeClient，不触网
