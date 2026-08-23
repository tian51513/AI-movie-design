# comic_studio · 小说转漫剧工作站

把小说章节自动转化为漫剧：LLM 提取角色/场景/道具 → ComfyUI 生成参考图 → LLM 拆分镜 →
逐镜生成视频 → FFmpeg 合成。完整设计见 `docs/superpowers/specs/`。

## 当前状态（Phase 1 已实现）

- [x] 项目管理：导入小说章节（txt）、画幅选择（9:16 / 16:9）
- [x] LLM 资产分析：本地 Ollama / 线上 API 提取角色（含外貌固化）、场景、道具，入库全局资产库
- [x] Web UI：项目列表/创建、分析进度轮询、资产浏览
- [ ] Phase 2：任务队列 + ComfyUI 工作流模板 + 资产参考图生成（门 1）
- [ ] Phase 3：分镜拆解 + H3 提示词生成（门 2）
- [ ] Phase 4：逐镜渲染；Phase 5：FFmpeg 合成

## 快速开始

```bash
pip install -e ".[dev]"
uvicorn comic_studio.web.app:app --port 8190
# 浏览器打开 http://localhost:8190
```

LLM 默认走本地 Ollama（`http://localhost:11434/v1`，模型 `qwen3:14b`，可用
`ollama pull qwen3:14b` 拉取）。分析（extract_assets）默认本地；后续分镜任务默认线上
API——在 `settings` 表配置 `llm_providers.online`（base_url / api_key / model）。

## 开发

```bash
pytest -q          # 全量测试
```

架构约定见 `CLAUDE.md`。
