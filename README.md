# comic_studio · 小说转漫剧工作站

把小说章节自动转化为漫剧：LLM 提取角色/场景/道具 → ComfyUI 生成参考图 → LLM 拆分镜 →
逐镜生成视频 → FFmpeg 合成。完整设计见 `docs/superpowers/specs/`。

## 当前状态（Phase 1 已实现）

- [x] 项目管理：导入小说章节（txt）、画幅选择（9:16 / 16:9）
- [x] LLM 资产分析：本地 Ollama / 线上 API 提取角色（含外貌固化）、场景、道具，入库全局资产库
- [x] Web UI：项目列表/创建、分析进度轮询、资产浏览
- [x] Phase 2：任务队列 + ComfyUI 模板 + 资产参考图生成（门 1）
- [x] Phase 3：分镜拆解 + H3 提示词生成（门 2）
- [x] Phase 4：逐镜渲染
- [ ] Phase 5：FFmpeg 合成

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

## 参考图生成

设置页填写 ComfyUI 地址（默认 `http://127.0.0.1:8188`），状态灯变绿后：

1. 按 `templates/workflows/README.md` 指导从 ComfyUI 导出文生图工作流（API 格式）并编写 manifest
2. 在项目详情页点击「批量生成参考图」提交任务到队列
3. 查看队列条走动、日志面板出现 ComfyUI 提交/完成消息
4. 资产卡出现参考图缩略图；单个资产可点击「重生」重新生成（seed 随机）
5. 全部资产有图后点击「确认资产（过门1）」，项目 stage 进入 `assets_ready`

### Phase 2 真机验收

1. 设置页 ComfyUI 地址默认 `http://127.0.0.1:8188`，状态灯变绿
2. 按 `templates/workflows/README.md` 导出小枫文生图 API 格式 + 写 manifest
3. analyzed 项目点「批量生成参考图」→ 队列条走动、日志面板出现 comfy 提交/完成
4. 资产卡出现参考图缩略图；单个「重生」可换图（seed 随机）
5. 全部有图后「确认资产（过门1）」→ stage=assets_ready
6. 中途重启应用 → 未完成 job 自动重排继续

### 分镜与提示词

assets_ready 后进入分镜阶段：拆分分镜 → 检查/编辑分镜内容 → 批量生成 H3 提示词 → 确认门2（storyboard_ready）。

### Phase 3 真机验收

1. demo-SAO（assets_ready）→ 分镜 tab →「拆分分镜」→ 日志看 storyboard 分块进度
2. 分镜列表出现（含台账/绑定/workflow_type 建议）；点击镜头展开可编辑描述与提示词（失焦后保存，不会被后台刷新打断）
3. 「批量生成提示词」→ 逐镜 ready；点开看提示词质量（H3 规程特征）
4. stale 镜头点「重生提示词」换一版（基于最新资产参考）；改描述后重生对比
5. 全就绪 →「✓ 确认分镜（过门2）」→ stage=storyboard_ready
6. 资产重生一张参考图 → 对应分镜出现 stale 标记

### Phase 4 真机验收

1. demo-SAO（storyboard_ready）→ 标题旁「视频参数」确认 0.4MP/32/标准/5s
2. 分镜区「批量渲染」→ 队列条 17 镜串行、日志 comfy 提交/落盘（每镜约 1-3 分钟）
3. 逐镜卡出现可播放视频（点击播放）；单镜「重渲染」换版（seed 随机）
4. 分镜卡改时长数字 → 重渲染观察时长变化（17n+5 帧对齐，实际秒数近似）
5. 改视频参数（如 0.8MP/高质量）→ 重渲染对比清晰度
6. 全部有视频 →「✓ 确认渲染（过门3）」→ stage=rendered
7. 中途重启 → 未完成渲染自动重排重渲（已完成的不重复；断点续跑对账属 Phase 5）
