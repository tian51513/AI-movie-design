# 小说转漫剧工作站（comic_studio）设计文档

- 日期：2026-08-23
- 状态：已与用户逐节确认；v1 全部实现（Phase 5A 四模式提示词与渲染体验、Phase 5B 一键出片/FFmpeg 合成/断点对账/模型切换）；v2 提前实现：P6 配音字幕（2026-08-27）；分镜数指定与生效/无效状态（2026-08-27）
- 原始需求：`原始需求.txt`

## 1. 背景与目标

把小说章节自动转化为漫剧（连续短视频剧）：LLM 提取角色/场景/道具并固化外貌 → ComfyUI 生成多视角参考图（全局复用）→ LLM 拆分分镜并绑定资产 → 逐镜调用 ComfyUI 视频工作流（以 MiniMax H3 为主，LTX2.3 等为辅）→ FFmpeg 合成成片。

运行环境事实：

- GPU：RTX 5070 Laptop，**12GB 显存**（硬约束，工作流一律用量化 + 加速方案）
- ComfyUI Desktop 已装：`E:\Comfy-Desktop\ComfyUI-Installs\anime-gc\ComfyUI`（WSL 路径 `/mnt/e/Comfy-Desktop/...`），54 个自定义节点已就位
- 模型真实存储：`/mnt/e/Comfy-Desktop/ComfyUI-Shared/models/`（H3 ref2va/fl2va int8、LTX2.3 fp8、Krea2、Flux2-Klein、qwen3vl 文本编码器多档量化等）
- Ollama 已装（Windows 侧，`localhost:11434`，OpenAI 兼容接口可用）
- 本机当前无 FFmpeg → 项目自带（见 §10）

## 2. 已确认的产品决策

| 决策点 | 结论 |
|---|---|
| 产品形态 | 本地 Web 应用（浏览器操作） |
| 自动化程度 | 阶段门禁：资产确认 → 分镜确认 → 才开始渲染 |
| 音频 | v1 只做画面，数据结构预留音频位；配音/字幕/BGM 为 v2 |
| 画面比例 | 9:16 与 16:9 都支持，项目级可选 |
| LLM 策略 | 混合：分镜拆解等重活走线上 API，提取等轻活走本地 Ollama |
| 输入规模 | 章节级导入，一个项目对应一集成品 |
| 架构 | 方案 A：单体本地 Web 服务；内部引擎/Web 分层，**后期可把引擎抽取为 ComfyUI 节点** |
| 并发 | `settings.workers` 可配置，**默认 1**；队列从第一天按多 worker 安全设计 |
| 视频后端 | 以 MiniMax H3 为主，LTX 等为辅；提示词按后端做适配器 |
| 技能集成 | minimax-h3-video-prompt 技能 vendor 进项目作为提示词规程；remix-reference-video-prompt 借镜头语言分类法，结构迁移列 v2 |

## 3. 总体架构

### 3.1 技术栈

| 组件 | 选型 | 理由 |
|---|---|---|
| 语言 | Python 3.11+ | 与 ComfyUI 生态一致 |
| Web | FastAPI + uvicorn | 原生 async，SSE 推进度 |
| 数据库 | SQLite（stdlib，WAL） | 单机单用户；jobs 表兼作持久化队列 |
| 前端 | Vue 3 官方产物本地 vendor（无构建步骤、无运行时 CDN 依赖） | 免 node 工具链，规避 CDN 供应链风险，可离线运行 |
| LLM 客户端 | openai SDK | 一套通吃本地 Ollama 与线上端点，切 base_url |
| ComfyUI 客户端 | httpx + websockets | /upload/image、/prompt、/history、WebSocket |
| FFmpeg | imageio-ffmpeg 自带静态二进制 | 零安装动作 |

### 3.2 模块划分与边界规则

```
comic_studio/
├── engine/            # ★ 核心引擎——禁止 import 任何 Web 框架
│   ├── llm/           #   provider 抽象 + 分析任务提示词模板
│   ├── comfy/         #   ComfyUI API 客户端（上传/提交/监控/释放）
│   ├── workflows/     #   工作流模板注册表 + 注入填充器
│   ├── prompts/       #   vendored H3 技能 + 分镜→视频提示词适配器
│   ├── assets/        #   资产库（全局 + 项目引用）
│   ├── pipeline/      #   阶段状态机 + 门禁
│   ├── queue/         #   任务队列（SQLite 持久化 + asyncio worker 池）
│   └── merge/         #   FFmpeg 归一化 + concat
├── web/               # FastAPI 路由层（REST + SSE），只做编排和 IO 转换
├── frontend/          # Vue3 CDN 静态文件
├── templates/workflows/  # API 格式工作流 + manifest（用户可增删）
└── cli.py             # 调试后门（可选）
```

边界规则：

1. `engine/` 不依赖 FastAPI/Starlette；输入输出都是纯数据结构（pydantic 模型）
2. Web 层只做：参数校验、调 engine、SSE 推进度
3. 未来抽取路径：`engine/` 打包 → custom node import → 暴露为 H3DirectorStudio 式聚合节点

### 3.3 进程拓扑

```
ComfyUI Desktop(:8188) ◄──HTTP── comic_studio(uvicorn :8190, worker 池)
Ollama(:11434, OpenAI兼容) ◄──────┘            │
线上 LLM API ◄─────────────────────────────────┘ ├──► 浏览器 UI
```

单 worker 串行是 v1 默认；并发通过 `settings.workers` 配置提升（§8.6）。

## 4. 数据模型与存储布局

### 4.1 文件布局（data 根目录可配置）

```
data/
├── studio.db                      # SQLite
├── library/                       # ★ 全局资产库，唯一存储，跨项目复用
│   ├── characters/<id>/
│   │   ├── meta.json              #   姓名/外貌固化描述/标签/来源项目
│   │   └── views/                 #   front.png side.png back.png …
│   ├── scenes/<id>/…
│   └── props/<id>/…
└── projects/<slug>/
    ├── project.json               # 画幅、章节信息、当前阶段、类型→模板映射覆盖（Plan 2/3 落盘；P1 阶段信息在 SQLite）
    ├── novel.txt
    ├── analysis/                  # LLM 产出 JSON 落盘（Plan 2/3 交付；P1 分析结果存 SQLite assets 表）
    ├── shots/<NNN>/
    │   ├── shot.json              # 绑定资产/提示词/需求台账/状态/模板覆盖
    │   ├── prompt.txt             # 视频提示词（人工可改，改后锁定）
    │   ├── key.png                # 首帧图（fl2v 类镜头）
    │   └── video.mp4              # 渲染产物
    └── output/epNNN.mp4           # 成片
```

资产存储语义：**库为唯一存储，项目存引用**（`project_assets` 关联表）。同一角色多项目复用不产生拷贝漂移；项目删除不删库。UI 的项目资产视图 = 按引用过滤。

### 4.2 SQLite 核心表

| 表 | 关键字段 |
|---|---|
| projects | slug、aspect_ratio(9:16/16:9)、novel_path、stage |
| assets | kind(character/scene/prop)、appearance_json、views 路径、标签、source_project |
| project_assets | project ↔ asset 引用 + 项目内备注 |
| shots | seq、text_span、description、shot_type、camera 枚举（景别/机位/运镜/转场）、duration、workflow_type、template_id 覆盖、ledger_json（必须出现/必须保持/允许变化/禁止出现）、prompt、status、video_path、depends_on（首尾帧衔接）、transition（v1 恒为 cut，字段预留） |
| jobs | type(analyze/gen_ref/gen_key/gen_shot/merge)、resource 标签、endpoint_id、payload、status、attempts、comfy_prompt_id、error |
| endpoints | ComfyUI 端点列表（URL、状态） |
| settings | workers 并发数、类型→模板映射、LLM provider 配置 |
| llm_calls | 任务、provider、模型、token、时间（审计） |

## 5. 阶段状态机与门禁

```
created → analyzed → assets_ready ─门1→ storyboard_ready ─门2→ rendering → rendered → merged
              ▲         (生成参考图,      (拆分镜+生成提示词,     (队列逐镜)    (门3:预览)  (FFmpeg)
              │          用户检查/重生)     用户编辑/确认)
              └──────── 任何阶段可回退重跑（重分析→下游标记 stale，不自动级联删除）
```

- 门 1/门 2 是显式用户动作（UI 按钮），门 3（渲染完预览后合成）可配置为自动
- 回退规则：重新 analyze → 已绑定分镜标 `stale`；重生某资产参考图 → 引用它的 shots 标 `stale`；是否重跑由用户决定
- 断点续跑：jobs 持久化 + `comfy_prompt_id`；重启时先对账 ComfyUI `/history`，再决定重排队，不重复渲染（对账机制 Phase 5 实现；当前重排重渲）

## 6. 工作流模板系统

### 6.1 模板 = API 格式 JSON + 注入 manifest

`/prompt` 只接受 API 格式；**不做通用 UI→API 转换器**（YAGNI）。每类用途一个模板，一次性转换后管线只消费 API 格式。

```yaml
id: h3_ref2va
type: ref2va                  # character_views | t2i | ref2va | fl2v | t2v
name: MiniMax H3 参考图生视频
file: h3_ref2va.api.json
inject:
  prompt: {node: "138", field: "text"}
  images:
    - {node: "137", field: "image"}
    - {node: "139", field: "image"}
  params:
    seed:   {node: "129", field: "noise_seed"}
    width:  {node: "136", field: "width"}
    height: {node: "136", field: "height"}
    frames: {node: "136", field: "num_frames"}
outputs:
  - {node: "92", filename_prefix: "cs/{project}/{shot}"}
requires: [ComfyUI-GGUF, kjnodes]
```

（节点 id 为示意，以实际导出文件核对为准。）

执行流程：加载 JSON → 深合并注入值 → 图片 `POST /upload/image`（确定性命名 `cs__{项目}__{资产}__front.png`，overwrite）→ `POST /prompt` → WebSocket 监听 → 完成后 `/view` 拉取产物落项目目录。**data/ 是唯一真相源**，ComfyUI output 只是中转。

### 6.2 v1 模板清单

| 模板 id | 类型 | 来源工作流（workflows 目录下） | 转换动作 |
|---|---|---|---|
| character_views | character_views | `minimax/▶▷MiniMaxH3辅助四视图生成流-k2.json`（Krea2+QuadView，15 节点） | UI 里 Export (API)（P2 用 t2i 多视角提示词法，Krea2 模板为可选升级） |
| t2i_ref | t2i | `小枫/小枫-文生图工作流.json`（7 节点全标准） | UI 导出；场景/道具/关键帧共用 |
| h3_ref2va ★主力 | ref2va | `minimax/minimax_ref2va_gguf_workflow.json` | UI 导出（注入面最干净） |
| h3_fl2v | fl2v | `minimax/minimax_fl2v_gguf_workflow.json` | UI 导出（**子图需展平**；双帧输入留待关键帧功能） |
| ltx_fl2v | fl2v | `ltx/LTX-2.3-Workflows/*_api.json` | **现成**，改注入点 |
| t2v（可选） | t2v | H3 或 LTX t2v | 低优先，可缺省 |

转换分工：用户在 ComfyUI 界面打开工作流 → 开发者模式 → Export (API)；本项目建设侧负责写 manifest、核对注入点、验证跑通。**模板验收标准：注入一张测试图 + 一句测试提示词能出片。**

### 6.3 人工切换配置（三层）

1. 类型映射可换：项目级 + 全局默认配置"某类型用哪个模板"（如换非 GGUF 版、用 ltx_fl2v 替代 h3_fl2v）
2. 逐镜覆盖：分镜拆解时 LLM 建议每镜默认 workflow_type（与前一镜衔接→fl2v，常规→ref2va，建立新画面→t2i+视频），用户在分镜编辑界面下拉切换模板及参数（seed/步数/分辨率档）
3. 新模板即插即用：API JSON + manifest 放入 `templates/workflows/` 即注册；UI 模板管理页启用/禁用、查依赖

## 7. ComfyUI 客户端

- 提交前 `/system_stats` 健康检查；不可达 → 端点标 Down、任务回队、UI 状态灯
- 图片先上传后引用（§6.1）
- 监控：WebSocket 进度事件；**无假超时**，失速检测 = N 分钟无事件 → 查 `/history` → 确认卡死才 `/interrupt`（P2 实现为 /history 轮询 + 失速检测，WS 进度条排 P4）
- 节点错误：`/history` 错误明细（哪个节点、什么错）落 `jobs.error`，UI 可查

## 8. 任务队列与调度

### 8.1 资源标签与互斥

job 带 `resource` 标记：`gpu_comfy`（ComfyUI 渲染）/ `gpu_llm_local`（本地 Ollama，与 ComfyUI 共享 12GB）/ 无标签（线上 LLM、文件操作）。同一资源内互斥——即使未来多 worker。线上 LLM 任务可与渲染并行。

### 8.2 同模型分组

pending 队列按 workflow_template 分组排序，减少模型反复加载（t2i 一批跑完再切视频模型）；组内保持镜头顺序。

### 8.3 模型切换释放

不同模板组之间 `POST /free`（卸载模型 + 释放显存）。

### 8.4 首尾帧衔接

shot 可声明 `depends_on` 前一镜 → worker 用 ffmpeg 从前镜视频抽末帧作为本镜首帧（需求"首尾帧 I2V"的落地方式）。

### 8.5 失败处理

重试默认 2 次；失败 shot 标 `failed`，不阻塞队列；UI 失败列表一键重跑。

### 8.6 并发预留

- jobs 认领用事务级 `UPDATE…WHERE status='pending'…RETURNING`，多 worker 安全
- endpoints 为列表，worker 与端点绑定（未来一 GPU 一 ComfyUI 实例）
- `settings.workers` 默认 1，调大即用；同模型分组按端点独立计算

## 9. LLM 集成与视频提示词生成

### 9.1 Provider 与任务路由

openai SDK 统一（Ollama `localhost:11434/v1` / 线上端点，配置切换）。路由可配置：

| 任务 | 负载 | 默认 |
|---|---|---|
| 角色/场景/道具提取 + 外貌固化 | 轻 | 本地 Ollama |
| 分镜拆解 | 重 | 线上 API |
| 视频提示词生成 | 重 | 线上 API |

输出强制 JSON schema（pydantic 校验），失败带错误重试；章节文本按场景切块拆分镜后合并编号；`llm_calls` 记账。

### 9.2 视频提示词生成（minimax-h3-video-prompt 技能落地；5A 扩为四模式）

> **5A 注记（2026-08-24）**：实测 A/B/C/D 四版实验后扩为项目级四模式系统——A 散文单镜（快）、B 结构化简洁、C 结构化高密度构图、D 结构化多镜电影递进（**默认**）。实验教训全固化进 engine/prompts/modes.py 规范；重生提示词与渲染按项目当前模式。

```
shot.json → [H3 适配器]
   system = vendored SKILL.md 规程（适配为非交互管线版）+ capability-map + official-rules + mode_spec(mode)
   user   = 分镜上下文（描述、台账字段、绑定资产的外貌固化文本、画幅、时长）
 → validate_h3_prompt.py 机械校验（字符数/时长/素材数）
 → 不过 → 带错误重生成（≤2 次）→ 仍不过标 needs_review（门禁处用户可见）
 → prompt.txt（人工可改，改后锁定）
```

- 技能文件 vendor 进项目 `prompts/h3/`（SKILL.md + references + 校验脚本），随 git 版本化，不依赖 `~/.claude/skills`
- 需求台账字段（必须出现/必须保持/允许变化/禁止出现）在分镜 schema 中由拆解 LLM 产出，提示词阶段原样消费
- v1 无音频 → 跳过技能的"声音系统"模块（技能本身要求按需选模块）
- LTX 适配器：简化规程（无 H3 能力匹配），复用模块化结构
- remix-reference-video-prompt 技能：v1 借其镜头语言分类法作 shots 枚举字段（景别/机位/运镜/转场）；"参考片段结构迁移"列 v2

## 10. FFmpeg 合成（merge job）

1. ffprobe 逐镜探测 → 2. 归一化转码（统一 libx264 / crf18 / yuv420p / 统一 fps；画布按项目画幅：9:16→1080×1920，16:9→1920×1080）→ 3. concat（参数完全一致时走无损流复制快路径）→ `output/epNNN.mp4`

- FFmpeg 来源：`imageio-ffmpeg` 自带静态二进制（本机无 FFmpeg，必须自带；v2 字幕烧录/音轨混合复用）
- v1 硬切无转场（shots 的 `transition` 字段预留）；v2 音轨 `-map` 位预留
- merge 前磁盘空间预估检查

## 11. 错误处理汇总

| 故障面 | 策略 |
|---|---|
| LLM 输出不合规 | 重试 2 次 → 降级换 provider → 标记人工处理 |
| ComfyUI 节点报错 | 明细落库 → 重试 2 次 → shot 标 failed 不阻塞 |
| 应用重启 | jobs + comfy_prompt_id 对账 /history，不重复渲染 |
| 失速 | 无事件 N 分钟 → 查历史 → 真卡死才 interrupt |
| 磁盘不足 | merge 前预估检查 |

## 12. 测试策略

- 单元（TDD）：模板注入器（golden JSON 断言）、schema 校验、状态机合法转换表、ffmpeg 命令构造
- 集成（mock）：LLM mock；假 ComfyUI server（/prompt、/history、WS 事件流）
- 端到端（手动可选标记）：真实 ComfyUI 跑迷你项目（1 角色 1 场景 3 分镜），同时即模板验收流程

## 13. 版本范围

**v1（本设计）**：章节导入 → 分析 → 资产参考图（门1）→ 分镜+提示词（门2）→ 逐镜渲染 → FFmpeg 合成；9:16/16:9；5 个必备 + 1 个可选工作流模板；H3/LTX 提示词适配器；队列+门禁+断点续跑。

**v2 候选**（明确不在 v1；用户 2026-08-23 追加）：Web UI 整体美化与可视化升级（参考图墙/分镜时间轴/队列看板，建议 Phase 2-3 内容就绪后统一重设计）；配音/字幕/BGM；参考片段结构迁移（remix 技能全量）；导演台 H3DirectorStudio `segments_json` 整段批量快车道；视频超分模板；多 GPU 多端点实战配置；CLI 完整化。

## 14. 文档维护约定

随开发进度持续同步（每个实施里程碑的验收步骤之一）：

- `README.md`：项目简介、安装启动、当前功能状态
- `CLAUDE.md`：架构约定（engine 无 Web 依赖等边界规则）、开发工作流、关键路径
- `docs/superpowers/specs/` 下设计文档：状态标注（设计/实施中/已实现/已修订），重大变更记录修订历史

## 15. 开放问题

1. remix-reference-video-prompt 技能文件未在本地 skills 目录定位到——v1 仅用其公开分类法设计枚举字段，不影响实施；找到文件后再 vendor
2. Ollama 本地模型清单待用户确认（默认建议 qwen3 系列量化版）
3. 线上 LLM 提供商与 API key 待用户配置（deepseek/qwen/gemini 均可，OpenAI 兼容即可）

## 16. P7-D 设计：段间运动上下文 + 整段批量快车道（2026-08-28，待用户确认后实施）

> 调研来源：`E:\Comfy-Desktop\workflows\MinMax-H3-导演台\custom_nodes\ComfyUI_MiniMaxH3_Director-main`
> （约 1.6 万行 Python；调研报告要点：timeline_data v4 schema、SegmentPlan/DirectorPlan
> 中间表示、AV-latent 运动上下文、段级指纹缓存、run_indices 选择运行）

### 16.1 目标与现状差距

现状逐镜链路：每镜独立提交 ComfyUI → 下载 mp4 → 下一镜 ffmpeg 抽尾帧作首帧（fl2v）
或 depends_on 仅作提示词衔接。镜间连贯性依赖关键帧质量，像素级接力有漂移。

导演台方案：**一次提交整部成片的全部段落**，在 ComfyUI 内部完成逐段采样，
段间用 **AV latent 运动上下文**（上一段 latent 尾部 22 帧钉入下一段 conditioning 头部，
解码后裁掉）实现 latent 级连贯——不依赖关键帧图像质量。

### 16.2 接入方式（不改 engine 架构）

```
shots（DB） → segments_builder（新 engine 模块）→ timeline_data JSON（v4）
           → h3_director 模板（API JSON + manifest，注入 timeline_data STRING 槽）
           → 现有 gen_shot 队列路径提交（一个 job = 整部）
```

- 新模板 `templates/workflows/h3_director.api.json`：用户在 ComfyUI 里摆好
  Director 节点（model/vae/clip 接线）导出 API 格式；manifest 声明唯一注入点
  `timeline_data`（STRING widget）+ 输出节点。类型 `director`，走 template_map 新键。
- `engine/director.py`：`build_timeline(db, data_dir, project_id) -> dict`——
  生效镜（disabled 过滤）按 seq 生成 segments[]：prompt=镜当前提示词、
  refs=现有参考图槽位、durationSec=镜时长、continuityFromPrev=depends_on 链。
- `engine/rendershot.py` 加 `handle_gen_director`（@register("gen_director")）：
  组 timeline → 注入 → 提交 → 轮询（复用 wait_and_collect）→ 拿分段输出落盘
  `shots/<seq>/video_v1.mp4`（对齐现有目录约定，merge/TTS/字幕链路零改动）。
- autopilot 加决策分支：项目勾选「整段快车道」→ 渲染阶段发一个 gen_director job
  替代逐镜 gen_shot。

### 16.3 风险与约束

- Director 节点要求 model/clip/vae 在 ComfyUI 侧已加载接线——模板导出质量决定成败，
  需用户先在 ComfyUI 验证一次（模板验收标准同 §6.2）。
- 12GB 显存下整段连跑：依赖导演台的 VRAM 段间清理（vram_cleanup）。
- 失败粒度变粗（一个 job=整部）：靠分段导出+段缓存做断点（导演台自带
  seg_XXXX.mp4 增量落盘，我们下载时对账）。
- 与现有逐镜链路**并存可切**：项目级开关，不删旧路径。

### 16.4 实施拆步（每步 TDD 绿即 commit）

1. segments_builder：shots→timeline v4 JSON（纯函数，FakeClient/comfy_mock 测试）
2. h3_director 模板 + manifest + 注册（注入点=timeline_data）
3. gen_director handler：提交/轮询/分段落盘对账
4. 项目级开关 + autopilot 分支 + 前端（渲染模式加「整段快车道」选项）
5. 真机验收：与逐镜链路同一项目双跑对比镜间连贯性
