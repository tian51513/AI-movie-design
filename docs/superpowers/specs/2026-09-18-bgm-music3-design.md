# 音乐库（BGM）设计 — MiniMaxMusic3

日期：2026-09-18 初稿 / 2026-09-19 按用户需求改为全局音乐库制
状态：七决策锁定 + 库制改版待确认

## 需求（用户原话归纳）

音乐**类似音色库一样管理**：生成时用户可提供歌词（或 LLM 自动生成，或纯音乐）、
曲风风格（或 LLM 自动生成）→ 生成 → 试听 → 满意**保存到音乐库** → **应用到项目**。

## 核心结构（同音色库判例）

```
全局音乐库（跨项目复用）
  生成入口（设置页「🎵 音乐库」tab）
    caption 曲风描述：✨LLM 写 / 手填
    lyrics 歌词：手填 / ✨LLM 写 / 留空=纯音乐
    时长（默认 120s，≤360s）+ 🎲 seed
      ↓ gen_music 队列任务（gpu_comfy）
    staging 试听 → 「✓ 保存入库」 / 「🎲 换一版」 / 放弃
  库条目：试听 / 重命名 / 删除
项目侧（引用制）
  项目参数面板「配乐」下拉：从库选曲 / 无配乐（默认）
  音量滑条（默认 0.2）→ 合成时 amix 混入（循环补长/裁剪）
```

## 组件设计

### 1. 模板 `music3`（type: music）

`templates/workflows/music3.{yaml,api.json}` ← `_raw/audio_minimax_music_3.json` 转换：
- inject：prompt→`37:13.caption`、seed→`37:38.seed`、max_duration→`37:13.max_duration`、
  **lyrics→`37:13.lyrics`**（空串=纯音乐，Music3 原生支持）
- outputs：`35` SaveAudioAdvanced `cs/{project}/music`（format mp3 V0 保持）
- models：unet(`37:6`)/clip(`37:3`)/vae(`37:7`) 三槽可换

### 2. 引擎 `engine/musiclib.py`（新，命名对齐 voicelib）

- `suggest_caption(db, theme_hint) -> str`：LLM 写曲风（风格/情绪/节拍/乐器——
  Music3 caption 官方格式：Global Metadata 风格行 + 情绪走向描述）
- `suggest_lyrics(db, text_hint) -> str`：LLM 写词（中文，带主歌/副歌结构；
  text_hint=项目正文摘要或用户主题；用户明确留空=纯音乐不调用）
- `@register("gen_music")`（gpu_comfy）：payload `{caption, lyrics, seed, duration}`
  → fill → 提交轮询 → staging `data/music/_staging/<uuid>.mp3`；显存前置
  `ensure_vram_for_comfy`（voicelib 同款）
- `save_to_library(db, data_dir, staging_path, name, caption, lyrics, seed)`：
  入库 `data/music/custom/<name>.mp3` + `music_library` 表行
- `list_music(db)` / `delete_music(db, data_dir, music_id)`（删文件+行）

### 3. DB（迁移 43）：`music_library` 表

`(id, name UNIQUE, caption, lyrics, seed, duration REAL, origin 'user'|'llm',
 path 相对 data POSIX, created_at)`——重名 409（同音色库判例）

### 4. 项目侧（迁移 44）：projects 增列

- `bgm_music_id INTEGER NULL`（引用库条目；NULL=无配乐——**取代原设计的
  bgm_caption/bgm_seed 字段**，生成归库、项目只引用）
- `bgm_volume REAL 0.2`
- PATCH 两字段；选曲后「重新合成」生效

### 5. 合成混入（merge handler 内，autopilot 零改动）

`bgm_music_id` 非空且库文件存在 → 成片 ffmpeg：`-stream_loop -1 -i <music>` +
`-t 片长` + `volume=bgm_volume` + `amix`；逐镜合成与快车道 `director_mix`
同一函数复用；merge_cache 键纳入 music mtime+volume。

### 6. API `routes_music.py`（新）

- `POST /api/music/generate`（caption/lyrics/seed/duration 入队，202；在飞 409）
- `GET /api/music`（库清单+staging 状态）、`GET /api/music/staging`（试听 /media）
- `POST /api/music/save`（staging→入库，重名 409）、`POST /api/music/delete`
- `POST /api/music/suggest-caption` / `suggest-lyrics`（LLM 写，不入库）
- 项目引用走既有 PATCH（bgm_music_id/bgm_volume）

### 7. 前端

- 设置页「🎵 音乐库」tab（同音色库样式）：生成表单（曲风✨/歌词✨或手填或空/
  时长/🎲）+ staging 试听 `<audio>` + 保存命名 + 库列表（试听/删除）
- 项目参数面板：「配乐」下拉（无/库曲目名）+ 音量滑条；merged 后改选提示
  「重新合成生效」

## 与原 spec 的差异（2026-09-19 改版）

| 原设计 | 改版 |
|---|---|
| 项目级 caption/seed/draft | **全局音乐库** + 项目引用（bgm_music_id） |
| 只纯音乐（铺底） | **支持歌词**（手填/LLM 写/纯音乐三态） |
| 项目页生成试听确认 | 设置页库管理（同音色库心智）+ 项目仅选曲 |

## 不变项

整片一首（循环补长超长裁剪）/ 固定比例 amix 默认 0.2 / autopilot 不停等 /
全项目类型含有声书可用（默认无配乐）/ TRT 硬件门槛判例已收档（Music3 不受影响）

## 测试

FakeClient（caption/lyrics 生成）、comfy_mock（gen_music 轮询落盘、staging 流转、
在飞 409）、merge（amix 三态+缓存键）、API 状态码矩阵、迁移两表

## 不做（YAGNI）

对白闪避 ducking / 分段情绪配乐 / 项目内剪辑音乐（截取段落）——库文件整曲循环
