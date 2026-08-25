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

## 模块地图（Phase 4）

- `comic_studio/engine/video.py` — 渲染前端（batch_render、单镜重渲染、断点续跑对账）
- `comic_studio/engine/rendershot.py` — gen_shot 处理器（@register("gen_shot")，模板注入/提交/轮询/落盘）
- 测试反模式提醒：ComfyUI 渲染测试一律用 tests/comfy_mock.py 的 comfy_server，不连真实 ComfyUI
- 注意：`/media` 挂载需确保渲染产物可落盘；WSL 与 Windows 路径映射按 engine/paths.py 统一处理

## 模块地图（Phase 5A）

- `comic_studio/engine/prompts/modes.py` — 四模式提示词格式规范（A散文/B结构化/C构图/D多镜电影，默认D；2026-08-24 实验教训全固化）
- `comic_studio/web/routes_assets_edit.py` — 资产外貌编辑（服装修正入口 + stale 联动）
- `engine/rendershot.py` 扩展 — lora_strength 注入（项目 lora_realism）、远景升兆像素、多版本 video_v{N} 落盘、shot_versions 辅助
- 注意：提示词生成读项目 prompt_mode；重生提示词与渲染按镜头当前 prompt

## 模块地图（关键帧链路 2026-08-25）

- `engine/rendershot.py` — fl2v 关键帧：`ensure_keyframes`（缺 kf_start/kf_end.png 时经 t2i 模板自动生成首尾对，同 seed 保构图、成对约束入词）；`build_keyframe_prompt`（分镜描述+画风+时代+ZImage 尾缀）；fl2v 渲染提示词追加 `KF_NO_CUT` 镜内禁切约束；生成失败降级 h3_i2v
- 注意：fl2v → h3_fl2v（首尾帧插值）；关键帧复用 zimage_t2i 模板与 model_overrides

## 模块地图（Phase 5B）

- `comic_studio/engine/autopilot.py` — 一键出片决策引擎（next_action 纯决策 / tick 执行；幂等续跑）
- `comic_studio/engine/pipeline_gates.py` — 门1/2/3 统一 engine 实现（routes 与 autopilot 共用；GateStageError=409）
- `comic_studio/engine/merge.py` — FFmpeg 合成（ffmpeg_bin/probe/normalize/concat/merge_project + @register("merge")）
- `comic_studio/engine/rendershot.py` 扩展 — reattach 断点对账（comfy history_result 已完成直接落盘）+ _download_video_result 共用段
- `comic_studio/web/routes_merge.py` — POST merge（rendered 守卫/去重）、GET merges 扫 output
- `comic_studio/web/app.py` — lifespan：断点对账（先 reattach 后 requeue）+ autopilot 巡检线程（3 秒扫 autopilot=1）
- 工作流模型切换：manifest `models:` 槽位（registry.ModelSlot）→ settings `model_overrides`（键=模板 id）→ filler 注入；choices 从 ComfyUI /object_info 枚举
- 注意：merge handler 经 `register_merge_handler()` 延迟注册（app lifespan 调用）；`analyze` 是队列 job 类型（autopilot 用），手动分析仍是 BackgroundTask
