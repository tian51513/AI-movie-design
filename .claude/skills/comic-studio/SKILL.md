---
name: comic-studio
description: comic_studio（漫剧工坊）项目全流程操作手册——开发约定、启动运维、六类项目生产线（小说/主题/有声书/小说转漫画/漫画动态漫/漫改）、门禁与一键出片、音色配音、合成链、真机排障。Use when 在本仓库开发/调试/运营 comic_studio，或用户提到 漫剧工坊、一键出片、autopilot、分镜、提示词、渲染、合成成片、音色、配音、ComfyUI、门禁、start-prod、小说转漫画 等词。
---

# comic_studio 漫剧工坊 · 项目 Skill

小说/主题/漫画/有声书 → 全自动漫剧视频生产线。FastAPI + 纯 engine + SQLite + Vue3 单页 + ComfyUI + 本地/云端 LLM。

## 文档地图（按需深读，一处一层）

- `CLAUDE.md` — 模块地图全集（按日期增量，最新在最下）
- `docs/2026-09-05-feature-audit.md` — 功能全景 + 体检发现（已知 bug 清单在这）
- 本 skill 的 [REFERENCE.md](REFERENCE.md)（生产线/开发细则）· [PROMPTS.md](PROMPTS.md)（提示词模式模板约束 A~E）· [TROUBLESHOOTING.md](TROUBLESHOOTING.md)（真机排障）

## 开发铁律（违反必炸）

1. **TDD**：先失败测试后实现；`pytest -q` 全绿才提交；小步提交（上下文上限教训）
2. **engine 禁 import fastapi/starlette/uvicorn**（未来抽 ComfyUI 节点）
3. **venv 分侧**：WSL 用 `.venv`、Windows 原生用 `.venv-win`，二进制不可混装；改 pyproject 后两侧都要装（`.venv-win/Scripts/pip.exe` 可从 WSL interop 直装）
4. **测试反模式**：LLM 一律 FakeClient、ComfyUI 一律 tests/comfy_mock.py、API 一律 `create_app(start_workers=False)`
5. **UPDATE/DELETE 带 LIMIT 只能放子查询**（Windows sqlite 未编译 UPDATE_DELETE_LIMIT）
6. **每个里程碑同步 CLAUDE.md/README/docs**（随进度更新）

## 启动与运维速查

| 场景 | 命令/入口 |
|---|---|
| 开发（热重载） | `start.bat` / `start.sh` → http://localhost:8190 |
| 生产挂机 | `start-prod.bat` / `start_silent_prod.vbs`（无热重载+0.0.0.0 局域网；**改代码必须重启生效**） |
| 服务 | `uvicorn comic_studio.web.app:app --host 0.0.0.0 --port 8190` |
| 数据 | `./data`（studio.db + projects + voices），不入 git；WSL/Windows 共享 |
| 停止 | UI「⏹ 停止自动」=关 autopilot+取消排队+打断在跑；「⏹ 停止任务」=项目级停任务 |

## 生产线总览（细节见 REFERENCE.md）

**六类入口，终点两类（视频=merged / 漫画成书=comic_ready）：**
- 小说/主题/**🎧有声书**（上传音频→transcribe 回填正文，之后同小说链；**分镜时长=音频实测段**；转写未完成守卫拦分析；转写期间全队列串行）：`created→[transcribe]→analyze→gen_refs→门1→assets_ready→split→gen_prompts→门2→storyboard_ready→render→门3→rendered→merge→merged`
- **📖小说转漫画**（comic_output）：同小说前半链到拆解（每镜=一页，**不走视频提示词/渲染/门3/合成**）→停等确认资产→自动角色主图→停等检查→逐页生成（**Krea2 快道 20-30s/页主力**·双参考图+画风LoRA栈；回落 Edit-2511/纯t2i）`pages/page_NNN.png`+干净副本→**终态 comic_ready**（最后一页落盘自推进）；详情「漫画页」浏览+补页/重出/多选批量+**💬对白重排/✥拖气泡**（干净副本秒级）+📄PDF/📜长图导出；**🎬转视频**（三选：重渲染/动态漫/漫改，§6a）
- 漫画（动态漫/漫改）：导入直达 `storyboard_ready→describe_shots(读图+提角色+提示词)→[漫改 gen_refs]→render→门3→merge`

**autopilot（🚀 一键出片）**：3s 巡检幂等续跑；失败守卫（批次失败不重烧等手动、单镜失败跳过推进）；跨类型在飞感知（重渲染未收尾不门3/不合成）；merged 自动关。

**时长体系（2026-09-05 起）**：段时长默认 0=LLM 动态估（对白=字数÷4+句间 0.6s；无对白按动作复杂度）；总时长 0=不限；**对白镜成片段长=配音+0.5s 收口**（长者末帧定格补齐、短者截尾）。

**音频面**：有台词处=克隆/Edge-TTS 配音；无台词镜可静音封 H3 原生杂音（mute_quiet_shots）；ref2va 注入音色样本保 H3 原声口型（h3_native_voice）。

## 常见操作 Playbook

1. **出一部片**：创建项目（段时长留 0）→ 🚀 一键出片 → 等 merged → output/epNNN.mp4
1b. **有声书**：🎧 上传音频 → 等「转写完成」日志（正文非占位）→ 检查人名 → 一键出片（配音暂为 TTS，原声直用= P10B 未做）
2. **重合成**（修了合成侧问题）：merged 阶段「🎬 重新合成」——merge 任务会自动重生 TTS+字幕再拼
3. **换角色音色**：🎭 角色配音面板换绑 → 「重新合成」（配音自动重生）；御姐系预设音色偏气声淡，要情绪选浪漫女声/元气少女类
4. **暂停介入**：⏹ 停止自动（联动全停）→ 改提示词/分镜/时长 → 再点一键出片续跑
5. **出一本漫画**：创建弹窗「📖 漫画」tab（正文+百万像素档+对白呈现）→ 🚀 一键出片 → 确认资产 → 主图满意 → 等漫画就绪 → 导出 PDF/长图（或 🎬转视频）
6. **漫画微调**：个别页 🔄重出；改对白呈现/气泡样式 → 💬重排对白（秒级不重出）；气泡挡脸 → ✥拖气泡手动定位（肤色避让自动定位 v2 兜底）
7. **清任务记录**：设置页「🧹 清理 7 天前记录」

## 排障入口

真机异常先看 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)——字幕滤镜 Windows 崩、成片无声、配音错位、端口漂移、venv 缺包、DB 被服务锁住怎么读，全部有既判案例。新事故修完务必回写该手册 + CLAUDE.md 模块地图。
