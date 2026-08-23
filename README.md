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

## 快速开始（WSL / Debian 系，PEP 668 管控环境）

```bash
# 1. 创建并激活虚拟环境（首次；仓库自带的 .venv 是隐藏目录，已存在则跳过创建）
python3 -m venv .venv
source .venv/bin/activate    # 注意必须用 source；把 activate 当脚本直接执行对当前 shell 无效

# 2. 安装依赖（激活后 pip 即 venv 内的 pip，不再触发 externally-managed 报错）
pip install -e ".[dev]"

# 3. 启动
uvicorn comic_studio.web.app:app --port 8190
# 浏览器打开 http://localhost:8190
```

不想激活 venv 的话，全程用 `.venv/bin/` 前缀也可以：

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/uvicorn comic_studio.web.app:app --port 8190
```

LLM 默认走本地 Ollama（`http://localhost:11434/v1`，模型 `qwen3:14b`，可用
`ollama pull qwen3:14b` 拉取）。分析（extract_assets）默认本地；后续分镜任务默认线上
API——在 `settings` 表配置 `llm_providers.online`（base_url / api_key / model）。

## 开发

```bash
source .venv/bin/activate
pytest -q          # 全量测试（或直接 .venv/bin/pytest -q）
```

架构约定见 `CLAUDE.md`。
