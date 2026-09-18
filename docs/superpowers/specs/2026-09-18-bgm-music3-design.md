# 配乐（BGM）生成接入设计 — MiniMaxMusic3

日期：2026-09-18　状态：已与用户对齐（七项决策全锁定）

## 背景与目标

用户在 ComfyUI 侧验证了 `MiniMaxMusic3` 文生音乐工作流（`_raw/audio_minimax_music_3.json`：
`MiniMaxMusic3TextEncode(caption+lyrics, max_duration≤360s) → EmptyLatentAudio(seconds 自动)
→ KSampler 30 步 cfg 1.7 → VAEDecode(Tiled 开关) → SaveAudioAdvanced mp3 V0`）。
目标：每部成片可选一首 LLM 描述、用户试听确认的纯音乐铺底，合成时按固定音量比例混入。

## 已锁定决策

| # | 决策 | 内容 |
|---|---|---|
| 1 | 定位 | **整片一首纯音乐铺底**（无歌词）；不足片长循环、超出裁剪 |
| 2 | 时机 | **手动生成+试听确认**（staging 模式，同音色库判例）；autopilot 不停等 |
| 3 | caption | **LLM 自动写**（读画风/正文情绪流变），用户可改可重生成 |
| 4 | 混音 | **固定比例 amix**（默认 0.2，项目滑条 0~50%），不做对白闪避 |
| 5 | 开关 | **项目级 bgm_enabled 默认关**（用户明确要求「项目可以选择是否生成配乐」） |
| 6 | 范围 | 全项目类型含**有声书可用**（默认关，对白密集自己调低/不开） |
| 7 | 时长 | max_duration=项目总时长估（≤360s 上限，总时长 0=不限时用出厂默认 120s）；片长超出循环 |

## 组件设计

### 1. 模板 `music3`（type: music）

`templates/workflows/music3.{yaml,api.json}` ← 转换 raw：
- inject：prompt→`37:13.caption`、seed→`37:38.seed`、max_duration→`37:13.max_duration`
- outputs：`35` SaveAudioAdvanced `cs/{project}/bgm`
- models：unet(`37:6` music3_dit)/clip(`37:3` music3 文本编码器)/vae(`37:7` music3_dav)
- VAEDecode tiled 开关（`37:43`）保持用户验证值 true；ComfySwitchNode 属核心无需 requires

### 2. 引擎 `engine/bgm.py`（新）

- `suggest_caption(db, proj) -> str`：LLM（常规路由，FakeClient 测试）输入画风+正文前
  N 字+章节标题，输出 Music3 caption（风格/情绪/节拍/乐器段——参照 raw 内置示例格式）
- `@register("gen_bgm")`（gpu_comfy 队列）：payload `{caption, seed}` → fill → 提交轮询 →
  落盘 `projects/<slug>/bgm/bgm_draft.mp3`；显存前置 `ensure_vram_for_comfy`（voicelib 同款）
- `confirm_bgm(db, data_dir, proj_id)`：draft → `bgm/bgm.mp3` + `bgm_enabled=1`

### 3. 项目字段（迁移 43）

`bgm_caption TEXT ''`、`bgm_seed INTEGER`、`bgm_volume REAL 0.2`、`bgm_enabled INTEGER 0`
——`_PUBLIC_COLUMNS` 暴露；PATCH caption/volume/enabled。

### 4. 合成混入（merge handler 内）

`bgm_enabled=1` 且 `bgm/bgm.mp3` 存在 → 成片末段 ffmpeg：
`-stream_loop -1 -i bgm.mp3` + `-t 片长` + `volume={bgm_volume}` + `amix`（主轨不动）；
逐镜合成与快车道 `director_mix` 同一函数复用。改配乐后「重新合成」生效。
merge 缓存键（merge_cache）纳入 bgm mtime+volume。

### 5. API（routes_projects 扩展）

- `POST /{id}/bgm/suggest` → caption（不落库）
- `POST /{id}/bgm/generate`（caption+seed 入队，202；在飞 409；comfy 未配置 409）
- `POST /{id}/bgm/confirm`（draft→正式+enabled=1；无 draft 409）
- `POST /{id}/bgm/disable`（enabled=0，文件保留）
- 试听走 `/media`（draft 直读）

### 6. 前端

项目详情「🎼 配乐」面板（视频链项目全阶段可见，含有声书）：caption textarea（✨AI 写）
+ 音量滑条 + 生成按钮（bgmBusy 防重）+ 内嵌 `<audio>` 试听 draft + 「✓ 用这首」「🎲 换一版」
（新 seed 重生成）「✕ 不用配乐」；merged 后改配乐提示「重新合成生效」。

## 不做（YAGNI）

- 对白闪避（sidechaincompress ducking）——固定比例已可听，闪避调参敏感
- 分段情绪配乐 / 带歌词主题曲——铺底一首已满足；lyrics 输入留模板天然支持后续手玩
- autopilot 自动生成——主观产物走确认制（决策 2）

## 测试

- FakeClient：suggest_caption prompt/解析
- comfy_mock：gen_bgm 提交轮询落盘、confirm 流转、在飞 409
- merge：amix 参数构造（bgm 开关×音量×循环裁剪三态）、缓存键含 bgm
- API：五端点状态码矩阵 + PATCH 四字段
