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

两套环境共享同一份 `data/` 目录与代码；**各自建各自的 venv**（Linux 与 Windows 的二进制不能混装）：
WSL 用 `.venv`，Windows 原生用 `.venv-win`（均已 git-ignore）。

### WSL（Linux）

```bash
./start.sh        # 自动建 venv/装依赖（首次），带热重载
```

或手动：`python3 -m venv .venv` → `source .venv/bin/activate`（必须 source）→ `pip install -e ".[dev]"` → `uvicorn comic_studio.web.app:app --port 8190 --reload`

不想激活可全程前缀：`.venv/bin/pip ...` / `.venv/bin/uvicorn ...` / `.venv/bin/pytest -q`

### Windows 原生（PowerShell / CMD）

```bat
start.bat
```

或手动：`python -m venv .venv-win` → `.venv-win\Scripts\activate` → `pip install -e ".[dev]"` → `uvicorn comic_studio.web.app:app --port 8190 --reload`

### 网络说明（WSL ↔ Windows 侧服务）

ComfyUI Desktop 与 Ollama 装在 Windows 侧，本应用默认用 `localhost:8188` / `localhost:11434` 访问：

- **mirrored 网络模式**（本机已启用，`.wslconfig` 中 `networkingMode=mirrored`）：localhost 双向直通，两环境通用，无需配置
- **NAT 模式**（其他机器默认）：WSL 内 localhost 到不了 Windows 侧——把 settings 中相应 base_url 换成 Windows 主机 IP（WSL 里 `ip route show default` 的网关地址），并让 Ollama 监听 `0.0.0.0`（设置环境变量 `OLLAMA_HOST=0.0.0.0` 后重启）

浏览器打开 `http://localhost:8190`。LLM 默认走本地 Ollama（`qwen3:14b`，
`ollama pull qwen3:14b` 拉取）；后续分镜任务默认线上 API——在 `settings` 表配置
`llm_providers.online`（base_url / api_key / model）。

## 开发

WSL：`source .venv/bin/activate && pytest -q`（或直接 `.venv/bin/pytest -q`）
Windows：`.venv-win` 的 Scripts 激活后 `pytest -q`

架构约定见 `CLAUDE.md`。
