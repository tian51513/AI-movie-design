# comic_studio 开发约定

> **项目 skill**：`.claude/skills/comic-studio/`（SKILL.md 入口 + REFERENCE.md 生产线细则 + TROUBLESHOOTING.md 真机排障判例）——新会话优先调它定位；本文件仍是模块地图权威。

- 架构边界：`comic_studio/engine/` 禁止 import fastapi/starlette/uvicorn（未来抽取为 ComfyUI 节点）
- 测试：pytest，TDD（先失败测试后实现）；运行 `pytest -q`
- 安装：WSL 用 `.venv`、Windows 原生用 `.venv-win`（二进制不可混装）；激活后 `pip install -e ".[dev]"`
- 跨环境：DB 存相对 data 根的 POSIX 路径（engine/paths.py），WSL 与 Windows 可共享同一 data/；**两环境 sqlite3 编译差异**：WSL Debian 版接受 `UPDATE...LIMIT`，Windows python.org 版拒绝（未编译 UPDATE_DELETE_LIMIT）——UPDATE/DELETE 的 LIMIT 只能放子查询括号内（2026-09-02 analyze 音色绑定事故，`_VOICE_BIND_SQL` 常量 + tests/test_analyze.py 结构护栏）
- 启动：`uvicorn comic_studio.web.app:app --host 0.0.0.0 --port 8190`（app 提供 `create_app(db_path)` 工厂；0.0.0.0=手机局域网可访问）
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

## 模块地图（Phase 6 + 2026-08-27 增量）

- `comic_studio/engine/tts.py` — Edge-TTS 配音（按性别分配声音，ledger.dialogue 逐句 → dialogue.mp3）
- `comic_studio/engine/subtitles.py` — SRT 字幕（镜内均分 + 时间轴累计 → subtitles.srt）
- `engine/merge.py` 扩展 — TTS 音轨替换 + SRT 字幕烧录（autopilot merge 前自动生成）
- `web/routes_merge.py` 扩展 — POST /tts；`engine/llm/provider.py` — normalize_base_url（Ollama/LM Studio 误填自动归一 /v1）+ 思考模型响应处理（剥 <think>、reasoning-only 报错、extra_body 透传；本机 LM Studio 实测无法请求侧屏蔽思考；**Ollama /v1 可以**——extra_body `{"reasoning_effort":"none"}` 实测彻底关思考，2026-09-02 ornith_1.5 思考烧满 16k 窗口事故：3764 输入+12620 思考输出=16384；think:false 不彻底、chat_template_kwargs 无效）
- 分镜控制：shots.disabled（迁移 22）——无效镜不进门禁计数/渲染/合成；`routes_shots.py` POST shots/batch（disable/enable/delete）；拆分镜 target_count 按块字数占比分配配额
- 注意：llm-test 不限 max_tokens（思考模型预算）；空正文/无 choices 均显式报错，不再静默空串

## 模块地图（2026-08-27 下午增量）

- `web/routes_projects.py` — 主题生成两步流：POST from-theme/preview 只生成不建项目，from-theme 带 text 直建（≥100 字不再调 LLM）；extra_prompt 补充描述拼上下文；GET 列表富化（excerpt/char_count/shot_count/updated_at=最近 job 时间）
- `engine/genref.py` — 画风拆层（style_vis 迁移 23：主图/关键帧用视觉子集，叙事词只留在 style 给视频提示词）；写实意图检测（PHOTO_RE→PHOTO_BOOST+全身照措辞，"立绘"已移除）；外貌行模板压缩 condense_appearance（丢「无」行、性别英文锚）；**模板级提示词方言** prompt_style（manifest 声明 natural_zh/tags_en，SD 系 CLIP 走英文标签流 build_gen_prompt_tags_en）
- `engine/llm/storyboard.py` — 对白机械兜底 backfill_dialogue（模型不填 dialogue 字段时从 text_span 引号提取，说话人双向就近匹配角色名册）；拆分镜 target_count 配额注入
- `engine/shots.py` — 删镜/重拆前清 jobs.shot_id 外键引用（FK=ON 下 DELETE 被 jobs 引用拦下，job 653 教训）
- 前端：创建入口顶部弹窗（上传/主题两 tab+预览）；项目列表窗格瀑布流/列表分页（12/页，localStorage 记忆）；分镜卡 💬 对白显示；项目参数输入 editingProject 焦点守卫（轮询不再顶回输入值）

## 模块地图（P7 借鉴计划 2026-08-28）

- `engine/director.py` — 整段快车道：build_timeline（shots→Director timeline v5，17k+5 帧对齐、每段 refs=绑定角色主图、continuityFromPrev=depends_on 链）+ @register("gen_director")（一次提交→整片落盘 output/→直达 merged；v1 不混配音字幕）
- `templates/workflows/h3_director.*` — 导演台聚合节点模板（timeline_data/seed 注入，四模型槽位）；template_map 键 `director`
- `engine/chapters.py` — P7-E 章节正则切分（中文数字+Chapter N，字符偏移）；projects.chapters_json；split-storyboards 按 chapter_from/to 切片
- P7-A 审计：jobs.snapshot_json（四处提交点）+ llm_calls.prompt_text/reply_text + GET /api/jobs/{id}/snapshot
- P7-B 白名单纪律 / P7-C heal_h3_prompt（prompts/gen.py，自愈不耗重试）
- 注意：整段快车道与逐镜链路并存；「🚄 整段快车道」按钮仅 storyboard_ready；director 项目全镜 video_path 指同一整片

## 模块地图（P7 后半 2026-08-28 晚）

- `engine/director.py` 扩展 — 分批提交（job 721：整部一次 CPU 灰画布 39GB 爆 → `_batch_shots` 按 `comfy.director_batch_frames`（默认 512 帧）切块，批内 latent 连贯、批间 ffmpeg concat；前端 directorBusy 队列驱动状态+新失败才弹窗）
- `engine/llm/local.py` — LLM 让位（Ollama /api/ps + keep_alive=0）+ `ensure_vram_for_comfy`（gpu_comfy 前置：让位→不足自动 comfy.free()→轮询 vram_free 至 `comfy.min_free_vram_gb` 默认 8GB，超时 VramShortage）
- `engine/llm/provider.py` 扩展 — 路由值支持 `provider:model` 点对点钉模型（首冒号切分）
- `engine/storyboard_checks.py` — 拆解后机械审计（时长守恒/画面换挡>30s/台词>25字，warn 不拦截）；`engine/textfix.py` — 敏感词机械转译（gen_story）
- `engine/logbus.py` — 首拉（after=0）最新 N 条倒序；前端日志轮询整体兜底防猝死
- prompts 借鉴第一批（XiaoLuo/短剧厂）：反代词具名/可拍摄性约束/分级英文运镜标签入 system；heal 增加 Meta 词剥离+结尾后缀「无字幕，无背景音乐」；截断→压缩输出反馈重试

## 模块地图（P7 收官 2026-08-29）

- `engine/director_mix.py` — P7-J 整片混音：帧数轴 spans TTS 音轨替换（有台词镜换配音/无台词镜留原声切片）+ SRT 烧录；`subtitles.generate_srt(spans=)`；comfy.director_mix 开关
- 前端分镜框选（2026-08-30，导航为主选区）— `app.js marquee*`：**#shotNav 数字条上按住左键扫框 = 扫过数字即选中该镜**（按钮可作起拖点、选中按钮亮橙色、nav 模式不做边缘滚动）；胶片条卡片区为辅选区（交叠即选中、4px 阈值、Shift 追加、边缘 rAF 自动横滚、`.no-snap` 关吸附）；真实拖拽吞 click（按起拖容器挂捕获）防误跳转/误开灯箱；无效镜也参与框选
- 画幅五档 + 媒体查看器（2026-08-30）— `projects.ASPECT_RATIOS`（9:16/16:9/3:4/4:3/1:1）统一五处校验；迁移 28 重建 projects 表放宽 CHECK（SQLite 不能改 CHECK，id 保序 FK 安全）；director/merge 画布通用 a:b 解析、ASPECT_ENUM 五档映射（ResolutionSelector 枚举实测），**工作流不支持的画幅回落默认 16:9**；前端 `arCSS/vpStyle` 卡内 kf 图（高 200）/视频预览（长边 400）按画幅等比、`viewer` 弹窗全尺寸浏览（首尾帧拉平序列/视频序列，‹ › + ← → 循环翻页，Esc 关）；资产参考图仍走旧 lightbox
- 提示词音频协议（2026-08-30 用户 ComfyUI 实测格式）— 四模式统一「声音协议」：overall_soundscape 白名单（仅画面一致环境声/动作音/人物非语言声，禁广播嘈杂等无关声源）、non_diegetic_music 默认 N/A（按分镜可写配乐，用户决策）、人物非语言声标「同步声音：…」、无台词镜明示无对白、台词口型同步；`heal_h3_prompt ⑦` 缺节机械补（music 缺省补 N/A、已有配乐不覆盖）；fl2v 渲染前置「参考图片与目标视频的对齐关系」头（Picture1→0.00s / Picture2→镜时长）
- 无台词镜静音（2026-08-30 杂音终局）— H3 音频遵循是概率性的（提示词协议只提命中率），残留杂音在合成侧确定性封死：`comfy.mute_quiet_shots` 开关（默认关）→ 逐镜 merge `_mute_audio`（volume=0）/ 快车道 `mix_director_audio(mute_quiet=)` anullsrc；代价=丢自然环境声；有台词镜不受影响（配音本就整轨替换）
- 漫画读图提示词结构化（2026-08-30）— `comic.describe_shots` 两模式 VLM 输出升级为用户实测 H3 格式：动态漫=**integrated_multimodal_description**（fl2v 首尾帧格式）、漫改=**subject_definitions 骨架**（全能参考格式）；均带音频协议（soundscape 白名单 + music N/A）；落库前统一过 `heal_h3_prompt`（与小说链路同等待遇）；对白提取 `_extract_dialogue` 不变
- 模式 E 英文控制式（2026-08-30 杂音归因收官：排除法锁定提示词风格）— 新模式 E 设默认（D 保留）：素材职责声明（`<Picture N> is the global character design reference…`/首帧 `is the EXACT starting key frame…`）+ `[Shot N]` 内容段（英文）+ 对白 `<d>[Mandarin Chinese]台词</d>`（中文）+ Preserve/Add 音频句 + 结尾负面清单固定句；**E 不用 soundscape/music 字段**（heal ⑦ 按 mode 跳过、`No subtitles` 满足尾缀检查、structure_check 放行）；机械注入句同步英化（fl2v 对齐头/KF_NO_CUT）；项目默认 prompt_mode=E（存量 D 不动可切）
- Phase 2 角色音色系统（2026-08-30 全链落地）— `voices.py`（15 预设英文映射+pace 语速+禁词+两级匹配：LLM suggested_voice 优先→性别×年龄 8 档基线兜底）；`voicelib.py`（generate_preset 走 **qwen_tts_design** 模板 / process_upload 走 **qwen_tts_clone**（上传+裁剪起止+默认句克隆→**staging 暂存→试听→confirm 入库/discard 放弃**两步制）/ list_voices 双作用域 origin，path 存 data 相对 POSIX）；两张 TTS 模板=用户实测工作流落库；h3_ref2va 接 LoadAudio×2（manifest audio0/audio1 槽，filler 后缀分流→ComfyClient.upload_media **统一走 /upload/image**——真机无 /upload/audio）；渲染 `_voice_slots_for_shot`：dialogue 说话人→assets.voice→样本入槽+`<Audio N>` 声明前置+标 `ledger.h3_native_voice`→合成跳过 TTS（保 H3 原声口型）；迁移 29 assets.voice；API routes_voices（列表/上传 staging/confirm/discard/预设生成/删除/绑定）+ 主题 PATCH 编辑；前端设置页「🎵 音色库」tab（生成/⟳重生成/内嵌试听/上传两步制）、资产弹窗配音音色下拉、项目级上传入口；音色样本缓存 data/voices/{presets,custom,_staging}+projects/<slug>/voices；ComfyUI 侧 zzz-qwen3tts-shim（transformers 5.x rope 'default' 注入，抗换包）
- LLM 音色库感知 + 漫画角色音色（2026-08-31）— `voice_library_prompt/names`：当前音色库（预设+全局+项目级自定义）注入 LLM 系统词，match_voice 按 library 校验（自定义名直连）；小说 analyze 动态附库；**动态漫** describe_shots：VLM 尾行 `VOICES:{...}` 标注说话人性别/年龄/选音色→剥离→对白聚合建角色（旁白过滤、无参考图、幂等）+自动绑音色；**漫改** extract_comic_characters：suggested_voice+音色库注入，建资产即绑（非法走性别×年龄基线）；黑白漫画：动态漫原样保留（首尾帧锚定），转彩走漫改画风转换
- 配音期角色音色 + 显存串行（2026-08-31）— **配音决策链**：ref2va 渲染注入过（h3_native_voice）→保 H3 原声；fl2v/i2v/t2v 无音频槽（MiniMaxH3ImageToVideo 无 ref_audios）→ **tts._try_voiced_tts**：单说话人+绑音色有样本→qwen_tts_clone 整镜台词（[pause] 串句→mp3）；多说话人/未绑/失败→Edge-TTS 按性别；native 镜跳过 TTS 并清残留（快车道 spans 防误用）；qwen_tts_clone 补 **target_text 注入槽**；voicelib 三入口（generate_preset/process_upload/clone_speech，db= 可选）统一 **ensure_vram_for_comfy 前置**（LLM 让位+显存门槛；无显卡信息环境跳过——LLM/Comfy 串行，用户要求）
- `engine/director.py` 扩展 — P7-H 批间首帧接力（上批末帧→genImage 起始画面槽，开关 director_batch_relay）；画布按项目兆像素档（×32 ceil 对齐）
- `routes_projects.GEN_STORY_SYSTEM` — P7-I 短剧结构规范（钩子/情绪流变/断章/语速公式/微表情/禁反向灌输）
- jobs.cancel_project_jobs + POST stop-jobs — ⏹ 项目级停止；genref 道具/场景双重禁人物（道具产品静物框架）
- 注意：快车道画布/性能开关（清显存/源帧/接力/混音）全部在 settings comfy.* 可调；gen_director 单 job 多批提交

## 模块地图（移动端适配 2026-09-01）

- 服务监听 `--host 0.0.0.0`（start.sh/start.bat/README）——手机局域网访问 `http://<主机IP>:8190`；WSL mirrored 网络模式免端口转发，Windows 防火墙需放行 8190；**start.bat 旧服务清理只按端口 8190 精确杀**（曾用 `taskkill /im python.exe` 全域杀——ComfyUI 等独立 python 应用每次重启被连带杀，2026-09-02；护栏 tests/test_start_scripts.py）
- `frontend/index.html` — viewport meta + 全局 `box-sizing:border-box`（width:100% 卡片窄屏溢出根因）+ input/select `max-width:100%`；≤768px 断点（main 收紧/hdr-actions 换行/guide `.grid-2` 双栏→单栏/`.film-card` 92vw/`.proj-row` 换行）；`pointer:coarse`（输入 16px 防 iOS 聚焦缩放、按钮 min-height:32px）；编辑外貌/主题弹窗补内部滚动
- `frontend/app.js` — 框选触控化：`_marqueeStart` 重构共用（鼠标/触控同路径），`#shotNav` 绑 `@touchstart.prevent` + CSS `touch-action:none`（触控无 Shift——扫框=全新选择，追加用 checkbox；胶片条保持原生横滚）；查看器 `viewerTouchStart/End` 水平滑 >40px 翻页
- 已验：Playwright 375×667 各视图零横向溢出；CDP 真实触控框选/鼠标框选回归通过（合成 TouchEvent 不可信，测触控须走 CDP Input.dispatchTouchEvent）

## 模块地图（移动端专用版 2026-09-01 下午）

- `frontend/index.html` `.m-bottombar` — 项目详情 ≤768px 底部固定操作栏：按阶段出关键决策大按钮（🚀 一键出片/⏹ 停止自动/✓ 过门1-3/🎬 合成成片/⏹ 停止任务，全部复用页面内 methods 零新逻辑）+ 监控状态行（⚡动作 · 运行/排队/失败 · ComfyUI 灯）；44px 触控目标、`env(safe-area-inset-bottom)` iPhone 安全区；**注意 `.m-bottombar{display:none}` 必须写在 @media 之前**（同优先级后者胜，曾把移动端一起盖掉）
- 标题 pill 行窄屏单行横滑（`main>h2` nowrap+overflow-x）；执行日志 `logsOpen`（app.js 按宽度初始化：桌面展开/窄屏折叠，h3 收起/展开按钮）
- `web/app.py` — `/`、`/static/*`、`/vendor/*` 一律 `Cache-Control: no-cache`（当天两次浏览器启发式缓存旧 app.js 与新 HTML 混跑出怪相；ETag 重验证未变 304 零成本）
- `engine/jobs.purge_finished_jobs` + `POST /api/jobs/purge?days=1-90` — done/failed/cancelled 超 N 天删除（设置页「🧹 清理 7 天前记录」）；pending/running 与窗口内保留；**删前先清 logs.job_id 外键引用**（FK=ON 下删被引用 job 必炸 IntegrityError，2026-09-02 线上事故，同 shots.py 删镜清引用模式；删项目路径 routes_projects 本就 logs 先删无恙）
- jobs 表四索引（迁移 30）：shot/asset/proj/status——修「分镜内容错乱」（564 镜 /shots 95s→0.2s，慢响应乱序串台）；前端 loadShots/loadDetail pid 乱序守卫

## 模块地图（台词驱动连贯渲染 A/B/C 2026-09-01）

- 借鉴「AI漫剧工坊·台词驱动无缝分镜」工程文档三级落地（两份文档在 E:/AI/AI_Shared_Models/skills/）
- **A 级**：拆解规则 10/11——连续同场景对白 3~8 句打包一镜（反「一句台词一镜」）；duration=句数×2.5s 估（schema 宽进 1~30，staging 钳 4~15，无对白镜用项目统一段时长；**backfill 补录镜由 `reestimate_durations` 机械重算**——LLM 没见过对白就写死 5，2026-09-03 真机 42 镜全 5s 教训；LLM 自填对白的镜保留估时不覆盖）；shots 新列 emotion（15 枚举）/gesture/gaze（英文短句）/continuity（6 枚举，外值 staging 清空，迁移 31）；`build_shot_context` 织「情绪与动态」块（LLM 按模式织入，不机械拼接）
- **backfill_dialogue 说话人三则**（2026-09-03 真机「女主回怼全派男主」）：引号紧前有说话动词（说道：）→ 前名优先（后方窗口不争抢）；双向就近兜底；相邻引号间无名字/说话提示 → 中文对白交替惯例换人
- **B 级**：staging 机械校准 workflow——continuity∈{全程继承,微变延续} 且 LLM 给 ref2va → fl2v（t2v 永不覆盖）；迁移 32 shots.seed——延续组内 +3/镜（断点/空重开随机），`rendershot._video_seed` 优先库值
- **C 级**：`merge.concat_xfade`（开关 comfy.merge_xfade，默认关）段间 0.3s xfade+acrossfade 交叉淡化、可选 comfy.merge_grade 统一调色；段>120 回退硬拼（全链重编码代价）；无音轨段 `_ensure_audio` 补静音；`subtitles` 镜内多句按字数比例分时长（替均分）
- 注意：xfade 链整片重编码，合成耗时显著上升——追求速度保持默认关；文档的中文提示词模板未采纳（项目 E 模式为真机实测协议）

## 模块地图（角色音色系统 2026-09-02）

- **R1 分析期音色决策链** — `schemas.CharacterAsset.voice_description`（仅给有台词角色）；EXTRACT_SYSTEM「库内不合适→suggested_voice 留空+填声线描述」；analyze 尾链：库内 suggested→绑（零成本）→有描述→`voicelib.generate_custom` 生成项目级（角色名命名，幂等不重烧）并自动绑→性别×年龄基线兜底；ComfyUI 不可用/失败只 warn 落基线**不炸分析**；生成后置 LLM 分块全部结束（不与分析抢显存）
- **R2/R3/R5 项目页「🎭 角色配音」面板**（替换旧「🎵 项目音色」按钮）— 每角色一行：当前音色徽章（项目级/预设/未绑定→Edge-TTS）/`/media` 试听/换绑下拉（未生成预设标灰+行内「⚡生成样本」）/声线描述「生成并绑定」/上传克隆收尾；`POST /api/voices/design`（生成+自动绑，空 base_url 422 门禁）/`POST /api/voices/promote`（项目级→全局自定义库 data/voices/custom，重名 409，复制保留源）——此后所有项目下拉可见且进 `voice_library_prompt` 注入 LLM；自定义音色可删（DELETE scope=project|global，被删绑定自动落 Edge-TTS）
- **R6 资产提取防污染** — 小说：EXTRACT_SYSTEM 明令不建旁白/画外音/群体称谓/未出场者 + **persist 前机械丢弃名字不在原文的角色**（幻觉名，warn 透明）；漫画：动态漫聚合 `_NON_SPEAKER_RE` 与漫改 `_NARRATION_WORDS` 扩群体称谓（「们」一字覆盖一切 …们；众人/观众/路人即使复现 ≥2 次也不建）——防无关角色绑音色耗 ComfyUI、污染资产表
- 合并打包两根修（schema 增字段暴露）：`_results_payload` 用 `exclude_defaults`（空音色字段零信息，每角色白胖 44 字）；`merge_analyses` 按精确增量（去 JSON 包装）计费替代整载荷累加（旧法重复计包装、系统性偏高，末轮易触发强制两两合并击穿 max_payload_chars）
- 移动端：`.voice-panel`/`.voice-row` 换行自适应；`.assets-grid .card{min-width:0;overflow-wrap:anywhere}`（修 375px 多列资产卡长串顶破轨道 406>360 既有溢出）；Playwright 375×667 面板全开零溢出实测
- 注意：**项目级音色=按角色各自绑定，无「项目级单一音色」语义**；音色消费两链路（ref2va 原声口型 / qwen_tts_clone 克隆配音，多说话人镜落 Edge-TTS）

## 模块地图（LLM 轻重双模型 2026-09-02 深夜）

- **设置页本地卡两模型字段**：轻度模型（常规任务）+ 重度模型（拆分镜/提示词/优化等结构重活，留空=不启用）——用户需求「本地的模型设置项新增一个作为重度模型」。实现上映射 provider `local2`（保存时自动继承 local 的 base_url/key，**extra_body 恒 null**：IQ2_M 等 27B 被 `reasoning_effort:none` 打哑需与轻度物理隔离）；路由下拉「本地·轻度/重度」+ 逐模型钉选（`local:tag`/`local2:tag`，首个冒号切分）
- **路由必须可从设置页选到**（本夜事故链：local2 只能 API 配置 → 表单无选项显示空白 → 保存拿旧表单冲掉 API 改的路由，二次事故）；表单快照含 local2，保存为正路
- 小模型纪律缺口连环修（当晚 ornith/nsfwvision 两模型实证）：`ShotDraft.text_span 必填非空`（缺→ask_validated 反馈重试，不再静默饿死 backfill_dialogue→零对白→时长全 5s）；gen_prompt **紧凑重试**（结构失败换 `_compact_system` 短骨架、上下文重开不背失败输出）；`build_h3_system` 注入前 `_strip_code_blocks`（SKILL.md 校验节 powershell 示例曾让 9B 抄进提示词）+ heal⑧围栏卫生（协议正文拆栏保文/工具代码整删）
- 注意：26k 上下文的 27B 拆分镜单块 ~90s（93.3s/2014 字块实测）——重活慢是常态，别误判卡死

## 模块地图（comfy.base_url 清空事故 2026-09-01）

- 事故：设置页保存曾把 comfy.base_url 冲成空串（全量 model_dump 默认值覆盖未提供键）→ worker `_comfy()` 返 None → 36k gen_shot 以 `AttributeError: NoneType.upload_media` 批量失败（暴露于 09-01 03:00 autopilot 夜间大批渲染）
- 防线①`routes_settings`：comfy 合并改 `model_dump(exclude_unset=True)`（未提供的键不覆盖）；显式传空 base_url → 422
- 防线②`settings.ensure_comfy_configured(db)`：ValueError 门禁；routes_shots（单镜/批量/快车道）、routes_refs（单/批量）、autopilot（render/gen_refs 分支记 error 跳过入队）全覆盖
- 防线③`queue/worker.py`：gpu_comfy 任务 + comfy None → 进 handler 前报「ComfyUI 未配置地址」
- 注意：设置表无更新时间戳，事故写入时刻无从考证；bat 的 for /f 内 PowerShell 管道**不要**写 `^|`（双引号内原样透传）

## 模块地图（autopilot 收尾 + 时长充足性 2026-09-05）

- **A1 暂停联动全停** — `routes_projects` PATCH：autopilot 1→0 自动 `cancel_project_jobs` + 日志（列表/详情/底栏三入口共用）；0→1 只翻开关不杀任务
- **A2 失败守卫（泛化 08-25 analyze 模式）** — `engine/autopilot.py`：批次型（split/describe_shots/merge）最新 job failed → wait 等手动重发；逐镜型（gen_prompt/gen_shot）`_failed_shot_ids` 跳过失败镜推进其余、全卡死才 wait；卡死 wait 首现报一条 error（`_STUCK_REPORTED` 去重，3s 巡检不刷屏）；手动重发产生新 job → 守卫自然解除
- **A3 跨类型在飞感知** — `_render_gap`/`_merge_gap`：门3 与 merge 前查 gen_shot 在飞（2026-09-04 武侠风云竞态：门3 过了镜15 还在重渲染，旧视频差点拼进成片）
- **B1 时长字数基准** — `llm/storyboard.reestimate_durations`：⌈字数/4⌉+0.6×(句数-1)（中文 TTS 语速约 4 字/s；句数×2.5 无视句长，长句对白被 -shortest 截半）；B2 规则8/10：无对白镜按动作复杂度估（打斗 8~12s/全景 6~8s）、打包字数预算 ≤48 字
- **B3 合成端兜底** — `merge._replace_audio(pad=)`：配音长于视频 → tpad 末帧定格补齐+warn（tpad 需重编码 libx264）；`subtitles.generate_srt` 有配音镜按 dialogue.mp3 实际时长排轴（与补长一致防漂移）；**快车道 director_mix 未做**（帧数轴重切代价大，当前主链逐镜合成）
- **字幕烧录 Windows 修复** — `merge._burn_subtitles`：滤镜串里 `\` 是转义符+非 ASCII 项目名滤镜内打开不可靠（job 38665 三连败，WSL POSIX 侧从未触发）→ 滤镜只用裸 `subtitles.srt`、srt 目录走 `cwd=`；**edge-tts 补进 pyproject dependencies**（.venv-win 缺包致 Edge-TTS 回退全灭，WSL interop 可直装 `.venv-win/Scripts/pip.exe`）
- **音频收口（2026-09-05 下午）** — `_replace_audio(target=)`：对白镜段长恒=配音+0.5s 呼吸（长者 tpad 末帧补齐、短者截尾收口——H3 口型表演撑满整镜的「嘴动无声尾巴」消灭）；字幕轴同规则镜像；`-t` 显式截断（copy+apad+shortest 组合实测熄火 48 字节挂死）；旧 `-shortest` 裸用曾把 17 个短对白镜截掉 ~40s（片长 197 vs 237）；搭配建议：无对白镜开 mute_quiet_shots 封 H3 原生杂音/乱语
- **段时长 0=LLM 动态估时（2026-09-05 用户需求）** — 校验 0~15、PATCH 0 不动存量镜、拆解时无对白镜也用 LLM 估时钳 4~15；【时长基准】行注入拆解上下文（规则 8 的「上下文给出」此前是空头支票）
- **审计高危三修（2026-09-05 docs/2026-09-05-feature-audit.md）** — H1 漫改参考图判断改 has_views（目录存在≠有图，persist 恒建空目录）；H2 TTS/SRT 前置挪进 merge handler（巡检线程不再同步跑 5min 卡停全部项目；手动合成同待遇自动重生配音）+ client.wait 排队不计失速 + interrupt 只打 /queue 确认在跑的自己（排队超宽限 DELETE 出队，不误杀他任务）；H3 storyboard_ready 放行重拆（对齐 UI 承诺，引擎删镜已清外键）
- **快车道段级音色（2026-09-06 用户需求「多参生视频模板支持多图多音频」）** — build_timeline 段构造调 _voice_slots_for_shot：说话人绑音色 → 段级 refAudios（样本入上传清单、提示词前置 <Audio N> 声明）+ 标 h3_native_voice（director_mix 混音跳过 TTS 替换保 H3 原声口型）；无对白/未绑段保持空（Director 合法）。快车道从此角色+音色双一致
- **ref2va 音频槽静音占位（2026-09-06 有声2 400 全灭）** — dialogue 空→音色槽不注入→模板默认 cs_voice_0.mp3 不在 input→ComfyUI 校验 400；fill_workflow 后未注入音频槽自动上传 0.5s 静音占位（data/_cache 缓存）；I1 空图快失败只看图片类条目
- **温和停止（2026-09-05 用户决策 A）** — stop-jobs/清空队列/删除项目一律不再 interrupt/删 ComfyUI 队（部分版本 interrupt 崩实例）：pending 取消、running 自然跑完落盘即止不重试；M13 定向删队随之退役；`retry-transcribe` 手动重发入口 + transcribe 进重启重排白名单（REQUEUE_ON_RESTART_TYPES）+ asr.transcribe(progress=) 心跳（空打事故后护栏硬化：断言转发）
- **P10 守卫两道（2026-09-05 16:02 真机命中终审 M-1）** — created 分支：源音频在而 segments 缺失 → wait（转写中/上次失败指引重传），手动 POST /analyze 同门 409（占位正文 20 字曾被分析成空资产）；analyzed 零资产 → wait「分析未产出任何角色资产」（旧 gen_refs 空 转=每 3s「入队 0 张」刷屏）；项目根 `.env` 支持（asr.load_env_file，转写前注入 HF_TOKEN 等，系统级变量优先；.gitignore 防泄露）
- **P11 漫改质量批（2026-09-05 用户四连报+时长）** — ①VOICES 尾行（gender/age/音色库）漫改同权注入（_voices_tail 共享助手；此前漫改性别的「待确认」、音色无法自动匹配）②已有角色名册注入 system + 提取包含式归一（新婚妻子→妻子，变体不另建资产、提示词同步替换）③_anchor_subject_definitions：绑定后把 subject_definitions 整块重写为「X 是来自 <Picture N>」（ref2va 多参考身份锚生效；节头前缀匹配判块界）④音色自动绑扩到已有角色（仅空绑）⑤import_comic 逐页时长读项目字段（段时长>0 用之/总时长>0 均摊下限4/兜底5.0——此前硬编码 5.0）
- **审计中危清零（2026-09-05 晚，M1~M20 四批）** — A 生命周期：stale 纳入 autopilot 缺口/停止任务自动关 autopilot/全无效镜明确 wait/director 重合成防呆/重提取清 ledger 绑定；B 合成：normalize 无轨补 anullsrc/xfade 字幕扣交叠/_sil 进临时目录/epNNN max+1/h3_native_voice 双修（无槽渲染清 flag+字幕判 flag）/版本切换删旧 mp3/快车道 TTS 超段 warn；C 队列设置：stop-jobs 定向删队（只删本项目 prompt_id+确认在跑才 interrupt）/cancelled 补 finished_at/llm_providers 局部 PUT exclude_unset+子字典增量；D 前端：POST /tts 并发 409 锁/漫画 tab 时长透传/参考图按钮阶段放宽/v-if v-for 解耦/describe_shots 标签/导引联动全停文案
- 注意：估时改字数只影响新拆解项目；存量项目想生效需重拆或手改镜时长；`merged` 阶段开放「重新合成」（POST /merge 放行 rendered/merged）

## 模块地图（P10 有声书 A 期 2026-09-05）

- 入口：`POST /api/projects/from-audio`（创建弹窗🎧有声书 tab）——存源音频 → 建项目（占位正文）→ 入队 `transcribe`（`engine/pipeline_jobs.py` @register）；转写完回填 `novel.txt` + `parse_chapters` 重算章节，之后走现有小说链（分析→拆分镜→出片）
- 约定路径：源音频 `projects/<slug>/audio/source.<ext>`；实测段 `projects/<slug>/audio/segments.json`——目录基名唯一出口 `engine/asr.audio_rel(slug)`，勿手拼
- asr extra 装法：`faster-whisper` 不进基础依赖——WSL `.venv/bin/pip install -e ".[asr]"` / Windows `.venv-win/Scripts/pip.exe install -e ".[asr]"`；缺包时入口路由显式 422 给安装指引（探测用 `except ImportError`，破损安装抛普通 ImportError 同待遇，终审顺手修）；转写函数惰性导入，无 asr 环境不影响服务
- audio_rel 语义：segments.json 存在 = 音频项目 → **分镜时长以音频实测为最终权威**（`span_duration_for` 规范化对齐 text_span 覆盖估时；终审 I-2：设了预设总时长也不得均摊覆盖，否则逐镜实测被全表 UPDATE 抹平）
- 注意：**转写期间全队列串行（单 worker，resource=None）——长书先切分或临时调 workers>1；running 转写不可中断**

## 模块地图（漫画链画风+智能估时 2026-09-06）

- **漫画 tab 画风随创建提交**——前端画风下拉从上传 form 挪公共字段区（上传/漫改可见；动态漫显示「画风跟随原页，转换请选漫改」提示——黑白+真人误走动态漫事故：漫画 tab 此前无画风字段，创建后改画风被静默忽略）；`from-comic` 路由 + `import_comic(style, style_vis)` 透传 create_project，漫改 describe_shots 画风转换指令与参考图 genref 即刻可消费
- **漫画链智能估时**——describe_shots 生成提示词后，项目段时长/总时长均 0（导入落的是 5.0 占位）→ 本次生成对白的镜按字数基准重估；公式抽 `llm/storyboard.dialogue_duration_seconds`（⌈字数/4⌉+0.6×(句数-1) 钳 4~15）与小说 staging 共用——「段时长留 0」从此两条链同为智能语义；显式段时长/总时长均摊（P11-⑤）、无对白镜、非本次生成的镜均不覆盖（手改存量不被冲掉）
- **漫改原页兜底双坑修复（2026-09-06 真机 gen_shot 全灭）**——`rendershot` 漫改无角色资产分支：①emit_log 误传 `shot_id` kwarg（logbus.emit 只收 project_id/job_id/data，12e1d46 引入且零测试）TypeError；②通用 else 分支把原页 images 覆盖成空 → I1 空图快失败——else 收窄 `elif images is None`（参考图优先级 ①角色资产 ②上镜接力 ③漫画原页）
- **强制重读（describe-shots force=true）**——批量读图默认增量（跳过已有提示词的镜），`?force=true` 整批覆盖：改画风/换模型后一键重生提示词+重估时长；前端「🔄 强制重读」按钮带确认框；创建弹窗改名「创建项目」（四类入口）。注意：估时在读图 job **全部页读完后**一次性落库，跑完前占位 5.0；describe_shots 不在重启重排白名单——重启杀在跑的读图 job 后需手动重触发
- **校正写回脱节提示（2026-09-06 有声2 真机）**——asr_cleanup handler + PUT novel-text 两处写回后，项目已有分镜 → warn「分镜基于旧文本，请重拆」（此前静默脱节：后半段拆的旧文本引号是 ‘’/缺失，backfill 按设计不取单引号 → 后 10 镜零对白）
- **动态漫说话人防垃圾（2026-09-06 manga7 真机：28 个"角色"过半脚手架短语）**——`comic._clean_speaker`：剥说话动词/连词尾缀（王叔叔轻声说→王叔叔）+ 脚手架叙述短语拒收（对白顺序/画面中出现对白气泡/undscape）+ 纯 ASCII 拒收，作用于 `_extract_dialogue`（ledger/TTS/字幕同步干净）；`_build_speaker_assets` 包含式归一（妈妈红→妈妈）；`purge_comic_assets` 扩到 motion 项目全部 character 资产（speaker 资产 tags=[] 无 comic 标记，清理按钮此前扫不到）
- **TTS 多句合并 Windows 静默全灭（2026-09-07 manga7 双对白事故）**——`tts._concat_audio_parts` 清单路径必须 `as_posix`（concat demuxer 里 `\` 是转义符，同字幕滤镜判例）+ `check=True` 失败必抛（此前吞错还照删 parts）；整镜失败 warn「保留 H3 原声」不殃及后续镜。教训：**假字节过不了真 ffmpeg 的测试要 mock ffmpeg 边界**；`subprocess.run` 不带 check 的静默失败=事故温床
- **创建时高级参数（2026-09-07）**——创建弹窗「▸ 高级参数」折叠区（字幕/兆像素/倍速/质量档）四入口全透传（upload/from-theme/from-audio/from-comic）；漫画 tab 字幕默认关且切换联动
- **优化清单四落地（2026-09-07 晚，全量 597 passed）**——①Edge-TTS 逐句重试×3+镜内并发×4；②render_mode 项目级（迁移 34，''=LLM 逐镜选/指定=staging 覆写，PATCH 落列重拆生效）；③reestimate-durations 只刷时长路由+⏱按钮（不烧 VLM）；④合成段缓存 output/merge_cache（键=视频/配音 mtime+画布+静音，未变镜零重编码）；⑤settings speaker_blacklist 说话人黑名单追加。快车道 B3 收口 SKIP（帧数轴重切架构级，主链已覆盖）
- **项目级字幕开关（迁移 33，2026-09-07 用户需求）**——`projects.subtitles` 默认 1，动态漫/漫改编入即 0（存量迁移归零；原页自带台词文字）；PATCH 可翻 + 参数面板复选框；`merge.py`/`director.py` 烧录点统一过 `projects.subtitles_enabled` 门控；改完「重新合成」即生效

## 模块地图（动态漫角色重绘 2026-09-09）

- **参数落库（迁移 35）** — `projects.redraw_characters`/`redraw_done`（后者=用户点过「批量重绘分镜」的 autopilot 停等标记）；create/PATCH/from-comic 三路透传，**漫改恒 0**（画风本就要转换，重绘无意义）；前端勾「重绘角色」联动字幕默认开——重绘清掉原页气泡文字，成片必须烧字幕补回
- **pages/ 单一事实源（决策 2/16）** — `comic.import_comic` 落 `pages/page_NNN.png`（永久存：重绘底图+重读图源），同时写 kf v1 版本文件+活动拷贝（渲染/查看器/合成零改动继续读活动名）；describe_shots 读图经 `comic.page_source_paths` 取原页——**重绘覆盖活动 kf 后 force 重读对白不丢**；迁移前旧项目无 pages/ 回落活动 kf
- **`engine/pageredraw.py`（新）** — 版本工具（`kf_versions` 数字序防 v10<v2、`save_kf_version` v{max+1}+刷活动拷贝、`activate_kf_version`）+ **尾帧派生联动**：镜 i 尾帧≡镜 i+1 首帧（fl2v 翻页链本是一页两帧），存/切 start 版本自动同步前镜 kf_end 同版本（仅 motion_comic）+ **kf 变更→双镜 video_path 置空**（决策 12：旧视频是旧画面渲的，文件留盘待重渲、status 回 ready）；尾帧独立切版仅末镜（其余镜尾帧=下镜首帧派生，违者 ValueError）
- **整页重绘 `redraw_page`** — 底图=原页（旧项目无 pages/ 从活动 kf_start bootstrap 源页）；模板 `template_map.page_redraw` 默认 zimage_i2i、`comfy.page_redraw_denoise` 默认 0.75（模板 manifest 声明 denoise 注入槽才真生效，47e519c）；模板声明第二图槽则注入该镜首绑定角色 main.png（**条件双槽**身份锚，无绑定/无图单槽照跑）；提示词=本镜描述+首绑定角色外貌锚+清文字（清气泡内全部文字、保留气泡形状与位置，决策 9）+画风段（**画风空=按原画风高清化**，决策 8）+PAGE_TAIL——**严禁用 genref.ZIMAGE_TAIL["scene"]，它带「画面中无人物」禁令**
- **redraw_kf 逐镜 job + 批量编排（决策 11）** — `enqueue_batch_redraw`：无效镜跳过→逐镜标 ledger `pending_redraw`+入队 gpu_comfy（温和停止可停/单镜失败不殃及）→`redraw_done=1`；**两阶段幂等**：handler 成功清标记（先重读 ledger 再清——redraw_page 已改过 ledger，勿拿旧行覆写），重发批量只补仍带标记的镜；强制单镜重绘走单镜重生；redraw_kf 进重启重排白名单（成功镜重跑只是多出一版无害，未完成镜 pending 标记仍真）
- **提取语义（决策 7/5）** — `comic.extract_comic_characters` +`characters_only`（只列主要角色——有台词/有动作/推动剧情，不建场景/道具）+`bind_shots`（提取后 auto_bind_characters 补绑分镜）；describe_shots **重绘模式跳过 speaker 资产**——重绘提取 job 是唯一建资产入口（speaker 资产撞名会被提取的清空重建丢外貌），ledger.dialogue 照常落库（TTS/字幕不受影响）
- **autopilot `_comic_flow` ①½ 重绘分支** — 无角色资产→extract_comic（VLM 采样提取）→缺 main.png→gen_refs **stage="main" 只烧主图**（sheet 不动、不标 stale）→**停等 redraw_done**「请检查/重试角色图后点批量重绘分镜」（停等 detail 刻意不含「失败」——tick 失败守卫按该子串上报卡死，停等是正常等待）→pending 在飞感知+失败守卫→落回 ③ 渲染缺口；`_latest_failed` 项目级（资产 A 失败挡整批，手动重发解除）；tick extract_comic 分支 payload 带 characters_only/bind_shots
- **API** — `POST /api/projects/{id}/batch-redraw`（202；项目门禁 422 优先于 comfy 未配置 409）；`GET /api/shots/{id}/kf-versions`（无版本文件返回空清单不报错）；`POST use-kf`（**路由层注入门禁：role 白名单+`^v\d+$`——引擎把值插值进文件路径，路径穿越拦在 422**）；`regen-keyframes` 重绘项目分流 202（新 seed 入队 redraw_kf 出新版本，不走同步 ensure_keyframes——决策 13 单镜重生=批量单镜版）；`TEMPLATE_MAP_KEYS` 白名单补 page_redraw（缺键=设置页「保存全部」整单 422，f103ee4）
- **前端** — 创建弹窗「是否重绘角色」复选（切漫改复位+联动字幕开）、参数面板复选（PATCH 即翻）、「🖌 批量重绘分镜」按钮（redrawBusy 防重，仅 motion+已开重绘可见）、分镜卡首帧版本 chips（点击切活动版，仅重绘项目加载）、单镜重绘文案引导、清资产确认文案含重绘警示、提取按钮 motion 重绘项目可见、设置页「整页重绘模板」行
- 注意：**清资产连重绘资产一起清**（含 main.png，确认框有警示文案）；存量项目 PATCH 开重绘不回填 pages/——bootstrap 拿「当前活动 kf」当源页，重绘过的镜再重绘=拿重绘结果当底图（逐代漂移）；重绘幅度手感调 `comfy.page_redraw_denoise`；12 项设计决策表见 docs/superpowers/plans/2026-09-09-motion-comic-character-redraw.md §0

## 模块地图（整页重绘真机四修 2026-09-10 晚）

- **真机根因四条（寝取美人妻 项目批量重绘 2 镜实证）**：①读图 VLM 与提取 VLM 各起各名（白发女子/黑发女性）→ auto_bind 全空 → 身份桥断；②shots.seed 全 NULL → 每镜随机 seed 风格漂移；③重绘提示词塞视频六段脚手架截断文本 + denoise 0.75 → 构图崩无美化方向；④旧 zimage_i2i cfg=5+euler 对 turbo 模型（cfg=1 蒸馏）过度烹煮——用户 _raw 实测 cfg=1+res_multistep+ConditioningZeroOut 定稿
- **F2a 新默认模板 `zimage_page_redraw`**（template_map.page_redraw）：双图槽 base=原页（VAEEncode 底图保构图）+ char_ref=角色主图（rembg u2net_human_seg → easy ipadapterApply PLUS 0.7 身份注入）；requires ComfyUI-Easy-Use + comfyui-rembg
- **F1 提示词弃 description**：i2i 内容在底图 latent——文本只给重绘指令/角色外貌锚/清文字/画风段；主图入槽时追加「人物的五官、发型与体态与角色参考图保持一致」
- **F2b 命名统一三件**：autopilot 重绘分支改序「提取→主图→读图」（①½a 前移，读图时名册已就位）；_voices_tail 名册约束扩到 subject_definitions/详细描述命名；enqueue_batch_redraw 入口补跑 auto_bind_characters（存量项目「强制重读」统一命名后点批量即绑上）
- **F3/F4**：批量重绘同批共用一个 seed（payload.seed，单镜重生仍随机）；denoise 默认 0.75→0.55（settings comfy.page_redraw_denoise）
- 存量项目恢复路径：点「🔄 强制重读」（名册约束统一命名）→「🖌 批量重绘分镜」（入口自动补绑+新模板+新提示词）；旧模板 zimage_i2i 保留（P3 资产精化用，cfg=5 问题不在本次范围）

## 模块地图（H3 提示词官方指南借鉴 + 重绘 v3 2026-09-11）

- **素材源** `E:/AI/AI_Shared_Models/MiniMax-H3-skills`（h3-prompt-writing/references base-en + ref-en）——官方 T2VA/I2VA/FL2VA/L2VA 与全能参考模式写作规范
- **T1 全局**：fl2v/i2v 渲染头换官方对齐原句（"How the reference pictures align with the target video —…" / "…is fully referenced"，训练分布内句式）；modes 公共尾追加运镜词表（13 型×幅度×速度、写镜头内自然动作句不堆标签）、说话人规范（(S1)(S2) 稳定 ID/首次音色锚/voiceover 精确短语+嘴唇闭合/<scenetrans>/<cutoff>）、画面文字原文双引号、soundscape 1-4 句对白不重复/music 写乐器速度动态禁情绪词
- **T2 漫改**：六段结构对齐官方 ref 规范（中文正文保留——2026-08-31 实测回调不翻案）：subject_definitions 用 `<Subject N>（角色名）` 标签、summary 加 `[reference generation]` 任务型前缀、retention 四档标记（fully/partially_preserved/attribute_transfer/weak_reference）、detailed_description 风格开篇句置于 [Shot 1] 前、运镜自然动作句；`_anchor_subject_definitions` 同步新句式
- **T3 动态漫**：**换官方 FL2VA 三段结构**（此前误喂六段 ref 格式给 base 模式）：`integrated_multimodal_description`（英文正文，首帧态→中间变化→收窄→尾帧路径 + 单镜头）+ soundscape + music + `No subtitles…` 固定结尾；对白 `<角色名> (S1) says: <d>[Mandarin Chinese]台词</d>`；`_extract_dialogue` 双格式（官方 <d> 优先——名字须以中文开头防 [Shot 1] 的 1 误中，旧「名：「台词」」兜底供漫改/存量）
- **整页重绘 v3**：`zimage_page_redraw` 换线稿 ControlNet——原页 AnimeLineArt→Resize 到项目画幅→Qwen-Image-2512-Fun-Controlnet-Union（SetUnionControlNetType lineart 型，strength 0.85）→ControlNetApplyAdvanced 锁构图，EmptyLatent t2i denoise 1.0 全幅风格化（v1 cfg5 烹煮/v2 latent 0.55 改人数/v2.5 IP-Adapter 全局盖章三连否决后的正解）；settings `page_redraw_denoise` 默认 1.0（语义=生成幅度，结构由 CN 保证）；requires kjnodes + controlnet_aux
- **设置冻结判例（2026-09-11 真机，同 09-01 base_url 事故类）**：设置页「保存全部」把**当时的服务器默认值整表固化进 settings 行**——之后代码改默认（v3 模板/新 denoise）全被存储值覆盖，v3 ControlNet「没生效」实为 template_map.page_redraw 冻结在 zimage_i2i+0.75；排查法：查 `settings` 表存储行 vs 代码 DEFAULT_SETTINGS；修复=整字典 PUT 回写（顶层键 exclude_unset 保其余，但字典本身是整替换——必须回带全部键，且旧服务白名单可能缺新键如 asr 要先剔）；**升级后若某功能「没生效」先查设置冻结再查代码**

## 模块地图（Krea2 风格库 + 重绘模板矩阵 + 性能优化 2026-09-12）

- **Krea2 风格预设库（借鉴 ComfyUI_Lazybuxuexi）**：73 库成品风格 JSON 入 `templates/styles/krea2/`（1.9MB + 87MB 缩略图 samples/）→ `engine/stylepresets.py` 扫描 + `GET /api/settings/style-presets` → 创建弹窗「Krea2 风格库…」选项 + **预览弹窗**（库下拉+名字过滤+缩略图网格点击确认，只渲染当前库 ~10 张 + loading=lazy 防卡顿）→ 选中填入 style/style_vis；项目详情画风 pill → 画风面板（textarea+Krea2 选择+PATCH 保存）——**注意 Vue methods 必须在 `const methods = {}` 内（放 createApp 选项上不绑定 this，2026-09-12 真机判例）**；`/styles` 静态挂载直出缩略图
- **重绘模板五道并存**（设置页「整页重绘模板」下拉切换，**改模板不需要重启**——registry 无缓存每次重扫磁盘）：v4 Edit-2511（画质道 1-3 分/页）/ v5.1 turbo+Fun-CN（速度道，PAI 格式 control_ 键需 `ModelPatchLoader+ZImageFunControlnet` 而非标准 ControlNetLoader）/ v6.1 多角色（PlusPro 三图槽已否决——白图占位搅浑参考+2511 对多人镜参与度不足）/ **v7 H3 抽帧**（ref2va 三参考短视频+ffmpeg 抽首帧，`comfy.page_redraw_h3_duration` 默认 2s 可调 1~4）/ **v8 Krea2 快道**（LazyKreaWorkbench 双图分工 10-15s/页——图1场景+图2人物、`编辑LoRA` 必接 `Krea2-编辑identity_edit_v1_2`，真机判例）；引擎按 `tmpl.type=="ref2va"` 分道 H3/图像
- **Krea2 多人镜策略（真机四修定稿）**：`multi = len(bound) > len(char_slots)` 时全部 char 槽填**纯白图**（原页回填=双图模式看到人脸复制成 3 个头像真机判例）+ 不注入角色身份指令 → 多人镜=纯风格转换（保人数不贴主图）；单人镜正常给参考图（一致性有效）；零绑定=场景专用提示词（**严禁添加人物**+剥离肢体纠错词——人像词暗示模型造人真机判例）
- **角色身份指令（条件式，五版迭代定稿）**：「若图1中存在「X」人物，则**仅**将其面部特征与发型（明细）调整为与图N参考图一致。图1中的**所有其他人物——包括仅部分露出的人物（如只显示下半身、手部、背影或侧面的人物）——必须完整保留在原位置，不得删除、隐藏、合并或补全**；若图1中无任何人物则不添加人物」——「使其完全一致」太强=模型只画参考图那个人
- **kf 缩略图性能优化**：`refresh_kf_thumb`（ffmpeg scale=360 jpg，~30KB）在 save/activate/upload 三写点生成；`_shot_public` 发 `kf_{phase}_thumb_url`（**纳秒缓存戳** `st_mtime_ns`——int(mtime) 秒级截断致快速切换版本吃旧缓存真机判例）；胶片条 `<img>` 用缩略图+`loading=lazy`+`decoding=async`（全尺寸 PNG ×729 镜入 DOM=切换卡顿根因）；查看器仍用全图；`useKf` 局部更新双 URL（免整页 reload）+ 白图 `data/_cache/blank_ref.png`（stdlib 手craft PNG 不引 PIL）
- 注意：**重绘提示词不用 shot.description**（F1 修复——视频六段脚手架截断文本）；多人镜的绑定名可能来自描述但**绑定≠人物在画面**（VLM 描述含下镜内容——首帧无人物但描述提到人物，绑定链据此绑了角色）；改模板后要检查 ComfyUI 节点接口匹配（`ImageResizeKJv2` 字段名=`upscale_method/keep_proportion`，非 LayerUtility 的 `aspect_ratio/scale_to_side`——张冠李戴 400 真机判例）

## 模块地图（H3 SLA 注意力 2026-09-10）

- **用户自定义节点 `H3SLAAttention`（SparseAttention 类）接入五个 h3_* 模板**（fl2v/i2v/ref2va/t2v/director）——插在 MiniMaxLowVRAMAttention 之后、BasicGuider/MiniMaxH3Director 之前的**最后一环**（节点作者定位「LoRA 之后、采样器前最后」）；settings `comfy.h3_sla_enabled`（默认开）/`h3_sla_sparsity`（0.9 本机验证值）/`h3_sla_block_size`（"64" 音频安全——128 块 1.6s 语音一个注意力模式会机器人腔，COMBO 字符串）；**Sage 联动**：SLA 开→`PathchSageAttentionKJ` 注入 `disabled`（T8 SLA 家族明令 KJ Sage 不得在前，同为注意力实现替换后包覆盖前包），SLA 关→回 `auto` 稠密基线（A/B 干净）；`rendershot.h3_sla_params(db)` 单源→render_shot params + director 手工注入循环共用；**加速 LoRA 也随开关联动**（`h3_lora_link(db, tmpl_id)`：开→SLA 蒸馏版、关→普通 turbo=整套历史基线，两版各按各的注意力蒸馏；设置页手动选过该模板 lora_turbo 槽→参数缺席手动优先；模板 api.json 默认=普通版）——fl2v/i2v/ref2va/t2v 本就有 lora_turbo 设置槽，director 这次补齐 lora_realism/lora_turbo 双槽；filler 只注入模板声明键——旧/自建模板零影响

## 模块地图（小说转漫画 comic_output 2026-09-12~13）

- **第五种项目类型「📖 小说转漫画」**——正文→LLM 分析/资产（沿用小说前半链）→拆解（每镜=一页）→逐页 t2i 单格漫画+对白后处理→PDF/长图导出；**页面即交付物：无视频渲染/门3/合成，`comic_ready` 是 comic_output 专属终态**（autopilot done 分支自动关开关）；设计 docs/superpowers/specs/2026-09-12-novel-to-comic-design.md（/grill-me 11 决策）、计划 docs/superpowers/plans/2026-09-12-novel-to-comic.md（T1~T7 全完成，e131208→a031e2a）
- **迁移 36 四列**：projects.`dialogue_mode`（bubble 气泡/footer 底部字幕条/none）/`target_pages`（0=按剧情密度自动，>0 注入拆解页数指引 ±20%）/`image_size`（SIZE_PRESETS 四预设 1024x1536/832x1216/1024x1024/1536x1024）/`quality_tier`（QUALITY_STEPS fast8/standard12/high20 步）；create_project 尾四参，`_PUBLIC_COLUMNS` 已暴露
- **拆解模式分支**（llm/storyboard）：comic_output → `comic_split_system`（COMIC_SPLIT_RULES：场景+人物+对白、**明确禁运镜/声音/时长**）；dur_hint 不注入（静态页无时长）；**workflow_type 机械固定 'comic'**（staging 覆写，排在 render_mode 覆写之后保证 comic 优先——LLM 的 fl2v/ref2va 建议一律作废）；有声书「全篇皆对白」规则照常生效
- **气泡渲染（2026-09-13 二期①，Pillow 后处理画真气泡）**——提示词层反转：对白一律不进 t2i 提示词（模型画中文=乱码），bubble 模式注入「上方留白+不要画任何文字/气泡/字幕」禁字指令，文字全部后处理；`_draw_bubbles`（白底圆角矩形+近黑 4px 描边+底边中央三角尾，透明度**只作用底色**——100%=背景全透明只剩描边+文字，字永远清晰）；`_bubble_layout` 纯函数（宽度随内容自适应上限 40% 页宽、**左右双列 y 游标**——同列间距=气泡高+间隙+尾巴 14px 不受对侧挤压；最多 4 气泡防爆）；字号 0=clamp(页宽÷36,16,34)；`_FOOTER_FONT_CANDIDATES` 补 `/mnt/c/Windows/Fonts/msyh.ttc`（WSL 挂 Windows 盘与生产同字形）+ 文泉驿正黑——**VLM 视检曾误报豆腐块（低分辨率），以像素哈希验证为准**（两不同汉字渲染位图不同=字体生效）
- **迁移 37 bubble_style**（projects JSON 列，空=默认 {opacity:85,font_color:#222222,font_size:0}）：from-comic-novel/from-comic-audio 透传（`_validate_bubble_style` 422：坏 JSON/opacity 0~100/font_size≥0）；PATCH 漫画参数段（dialogue_mode/target_pages/image_size/quality_tier/bubble_style——仅 comic_output，改完删对应页→「生成缺失页」重出）；前端创建弹窗 bubble 选中展开三字段（透明度/color picker/字号）+ 详情参数面板同款（patchComicParam/patchBubbleStyle）
- **`engine/comicgen.py`** — `@register("gen_comic_page")`：`build_comic_prompt`（description 前 200 字+前 2 句对白+画风段）→ `comic_page` 模板（t2i，settings template_map.comic_page 可切 Krea2/Z-Image）注入 width/height/steps/随机 seed → `pages/page_NNN.png` 落盘 → 对白后处理（footer=Pillow 底部字幕条带换行+字体回退 msyh→default；bubble/none 现为占位/跳过）→ shot 状态 `comic_ready`；attach_snapshot 审计
- **漫画镜不走视频提示词链（2026-09-13 计划缺口修 a031e2a）**：拆解 staging 即填 prompt=description（text_span 兜底）——gate2「全部镜有提示词」天然成立，`_prompt_gap` 无缺口不落 gen_prompts（否则 H3 视频六段烧重度模型、产出从不被页面消费；T4 测试直接落 storyboard_ready 绕过 assets_ready 段没暴露）；`handle_gen_prompt` 对 comic 镜短路（prompt=description 机械填充不调 LLM——兜住 stale 联动/手动重生路径）
- **autopilot 漫画分支**：`_comic_pages_gap`（storyboard_ready 时按磁盘 pages/page_NNN.png 找缺页；在飞 wait/最新失败 wait 手动——批型守卫）→ `gen_comic_pages` action → tick 逐镜入队 gpu_comfy → 全齐 `comic_done` → tick 落 comic_ready；`_novel_flow` 顶部 comic_ready 即 done
- **`engine/comicexport.py`** — `comic_pages`（磁盘 pages/ 为准按页号升序；有分镜行再按表过滤 disabled/已删残页——与「无效镜不进合成」同口径；无分镜行=手工放页全量）；`export_comic_pdf`（Pillow 多页合成 resolution=150，**可选依赖 `.[pdf]` extra**，缺失显式报安装指引；单页损坏跳过 warn 全坏才报）；`export_comic_strip`（ffmpeg vstack 竖排长图，format=rgba 统一像素格式、只要求同宽，stderr 尾段入异常 GBK 安全）；输出固定 projects/<slug>/output/comic.pdf|comic_strip.png 同名覆盖
- **API**：`POST /api/projects/from-comic-novel`（正文 UTF-8 上传+四参数，逐参数 422 校验，**subtitles 恒 0**——对白页面自呈，视频字幕烧录无意义）；`POST /{id}/generate-comic-pages`（手动补页：404/409（comfy 未配置/阶段不符）/422（非 comic_output），已有页/在飞/无效镜跳过——autopilot 失败守卫的解除入口）；`POST /{id}/export-comic?format=pdf|strip`（秒级同步返回 /media url）
- **前端**：创建弹窗第五 tab「📖 漫画」（页数/尺寸/质量档/对白呈现/画风复用 Krea2 选择；渲染模式隐藏）；详情「漫画页（N）」模式（x/N 页就绪 pill、🖼 生成中徽章、页面网格：comic_ready 显图+对白行/待生成占位；`comicPageUrl` 缓存戳 `_pagesStamp` 防重生成吃旧图；**pending→「待生成」**——漫画镜不走 gen_prompts 停在 pending 到 comic_ready）；pages 模式轮询接 shots 轮询同源；导出 PDF/长图按钮（disabled 无页）+ ⬇ 链接 pill
- **外貌压缩保留字段标签（2026-09-13 真机判例：体型调高挑主图仍偏胖）**：`condense_appearance` 剩余字段（瞳色/肤色/体型等）此前拼**裸值**（「…白皙，前凸后翘，身材高挑，大胸」夹在肤色后=弱约束噪声，模型不跟）；中文流（Z-Image qwen 编码器）**标签即语义锚**——改 `标签：值` 保留（`体型：身材高挑，纤瘦`）；性别/年龄/发型/服装自然短句与「无」行丢弃不变。2026-08-27 的压缩设计是给 majicmix CLIP 英文流的，中文流误伤
- **让位双向化（2026-09-13 真机判例：拆分切 27B 秒 504）**：ComfyUI 渲染模型跑完**驻留显存**（缓存不自动释放），重 LLM（27B IQ2_M 11.6G）装不下→Ollama 秒 504；旧让位只有单向（LLM→Comfy `ensure_vram_for_comfy`）。worker 补反向（**检查式**，对齐 ensure_vram_for_comfy 模式·用户要求）：`gpu_llm_local` 任务启动前查 `/system_stats`，**>10% 显存被占=有驻留才 `comfy.free()`**（gpu 组互斥保证此刻无渲染在跑，free 安全；ComfyUI 离线不拦 LLM）；普通任务与空闲态不打扰 ComfyUI 缓存。**12G 卡 27B 现实**：IQ2_M 常规部分下放（9.5G VRAM+2.3G RAM）慢但能跑（90s/块 09-02 实测前提=Comfy 显存空）；显存被占时任一 27B 量化档都装不下——真重度建议 14B 整卡装入（如 qwen3:14b 9.3G）
- **漫画拆分基准=剧情内容（2026-09-13 用户定调）**：COMIC_SPLIT_RULES 规则 1b——拆分以叙事拍点（动作推进/事件/情绪转折/场景变换）为基准**不是对白**，剧情连续性优先；一格承载同一拍点的多句对白（2~4 句常态，~60 字内气泡舒适），**严禁一句台词一格**；仅台词过密或细微表情值得独立特写才因台词/表情多拆。同批路由切重度：split_storyboards → local2（27B；轻度 9B 复读判例；~90s/块用户接受），机械轮换构图只作托底（用户原则：LLM 为主、机械兜底）
- **漫画拆解复读双修（2026-09-13 真机判例：42 页中 28 页描述逐字相同，页 9-30 连拷 22 页）**：每页对白各不相同但**描述=页面提示词**（拆解即填），LLM 对延续场景复读模板 → 画面全同只换气泡。①COMIC_SPLIT_RULES 规则⑥禁相邻页同/近描述（同场景延续必须换景别/机位/动作焦点/情绪）②staging 机械兜底：相邻完全相同描述自动追加轮换构图变化（`_FRAMES` 四档：全身全景/面部特写/侧面中景/俯视近景 循环——同场景换景别是漫画合法手法）
- **Krea2 快道 `comic_page_krea2`（2026-09-13 用户实测定稿·主力道）**：用户手工验证 Lazy_Krea2 全模式工作台——**百万像素 1.0→0.4 后双参考 90s→30s、单参考 50s→20s 且质量满意风格贴合**（Krea2=风格库原生底模）。模板复制 krea_page_redraw 结构（2000 Workbench→2001 Generate），slots：图1=场景（scene，无则灰）/ 图2=人物（char）——**槽序固定**（README：scene 恒 image1/person 恒 image2，交换即劣化）。**双角色双方案并存（迁移 40 `comic_dual_mode`·2026-09-13 用户终版决策）**：**仅当分镜参考资产总数 >2**（2角色+1场景）才特殊处理——`stitch` 拼接（默认：两人主图等高左右拼一张进图2+场景进图1，提示词「左侧A右侧B」；mtime 哈希缓存）/ `chain` 链式（官方方案：P1 场景+A→中间产物作 P2 图1 再插 B，tag 无斜杠）。**总数 ≤2 直种不特殊处理**：1角色+1场景照常双槽；2角色+0场景→第二角色主图直进图1（提示词「图2 A 与图1 B 分别保持一致」——无场景图时人物参考占场景槽）。参数面板「双角色处理」下拉切换，改完重出生效；`megapixels` 按项目尺寸档换算（512x768≈0.39 即 0.4 手感）；**宽高不注入**（用户决策：用 Krea2 原生结构，编辑模式输出跟输入图形状）、步数走质量档；Krea2 模板缺失回落 zimage_page_ref。**参考页路由优先级：krea2 → zimage_page_ref → 纯 t2i**。注意 2001 的百万像素字段是可选输入（api.json 显式写入才可注入）；prompt 长度影响 Edit-2511 编码耗时（~0.22s/字实测）。**同日用户栈同步**（_raw/Lazy_Krea2全模式工作台.json）：补 `LazyKreaLoraStack`（启用 PornMaster Detail+Realism 双 LoRA，模型/底模双输出接线；**turbo_4step 关**——用户实测 4 步=10 步=30s，瓶颈在 Qwen3-VL 接地/编码/双图 VAE 固定开销非采样步数，蒸馏 LoRA 零收益纯画质风险）+ 接地像素 512/参考强度 2/场景强度 1/CFG 1；**krea 道步数固定 10**（质量档映射无意义）。**上传 500 真根因（2026-09-13 判例闭环）**：链式 tag「-链1/2」的 `/` 进上传名 → ComfyUI 当子目录 → 服务端 `open(不存在的目录/xxx.png)` FileNotFoundError→500——**上传名即 input 路径，严禁 `/`**（tag 已改「-链1/-链2」+ filler `_safe` 清洗 project/asset/slot 三段分隔符双保险）。附带：`_upload_with_retry`（5xx/传输异常退避 5s/15s 重试、4xx 立抛、穷尽后异常带服务端正文——下次发作日志直接可诊断）；上传改读字节进内存（句柄复用重试会发空文件）
- **zimage_page_ref v1.4 画质修正（2026-09-13 真机四张判例）**：①Lightning 默认**关**（4 步蒸馏=画面/人物糊；开=草稿加速档，settings `page_ref_lightning` 1.0 时 steps 钳 4+cfg 2.5）——默认走质量档步数 + **cfg 3.5**（v4 重绘锐利基线）②缺省槽占位白图→**中性灰**（blank_gray.png 128——白图偏亮把「新场景」带偏户外，室内出成室外判例）③绑定场景名文字锚（无参考图也锚「场景：卧室，」）+「场所与光线严格按描述（室内就是室内）」+「画面高清锐利」尾。**注意 settings 冻结**：存量 comfy 行存了旧三值需 PUT 覆盖（已覆盖：lightning 0/cfg 3.5/denoise 1.0）
- **zimage_page_ref v1.3 空白画布底（2026-09-13 真机判例·底图锁死）**：旧版 char1 主图直作 latent 底，Edit「保持一致」把**构图（半身照）与风格（主图的摄影风）一并锁死**——换两种画风输出几乎不变、该全身的页也半身（分镜描述/画风词与底图拔河）。修：latent 底改 VAEEncode(空白画布)（新 canvas 槽，26→45→1085）+ denoise 1.0 纯生成——参考图只走 Plus 编码器做**身份条件**，构图/风格由「严格按场景描述重新构图 + 构图（全身/半身/特写）严格按描述执行 + 画风（严格执行）」驱动。同批 prompt 三修：`_anchor_lines` 拆 {"detail":…} 包装（JSON 整包泄漏 900 字噪声淹没 90 字分镜描述）、剥 description 的（id=N）绑定引用、scene_mode 文字锚降为名字映射
- **Krea2 风格库中文名（2026-09-13 用户指正）**：库 JSON 原生 `name_cn` 字段 **3948/3948 全覆盖**（ComfyUI 节点显示的就是它）——此前 stylepresets 扫描器 `s.get("name") or s.get("name_cn")` 优先英文把中文丢了。entry 增 `zh=name_cn`（name 仍为英文主键供查重/引用）；前端选择器卡片**中文名主显+英文小字副显**、过滤搜中英文都命中、创建流选中徽章 `kreaNameZh()` 显示中文。同批修复：画风面板换 Krea2 风格 style_vis 不同步（仅空时才填→图像端永远吃旧风格，真机判例）+ Krea2 prompt 尾部 `. Subject:{prompt}` 模板占位符剥离（`_kreaClean`，拼接式用法死文本）
- **漫画主图阶段手动入口 + 确认即开 autopilot（2026-09-13 验收反馈三连修）**：①确认路由只翻标记不启动生成——手动流点「✓ 确认」后主图不动（「要人工点吗」困惑根因），两确认按钮（资产/主图）确认即 `autopilot=1`（幂等）；②「主图满意」按钮此前不校验主图是否真生成齐（autopilot 关时主图全缺也亮）——`allComicMainsReady` computed（views[0]=「主图 main」）门控，缺主图时显示「🖼 生成角色主图」批量按钮（`POST generate-comic-mains`：缺 main 角色逐个入队 gen_ref stage=main，与 autopilot 分支同口径，在飞互斥）；③资产卡「主图」按钮已放开全类型（单卡手动生成/重生，scene/prop 主图=场景参考源）
- **超长篇分析合并树三修（2026-09-13 玉麟传奇 62 块真机）**——52 万字分析 round-2 合并 `finish_reason=length` fatal + attempts 从头重跑 50 分钟：根因是 MERGE_SYSTEM 旧「不丢项」让中间产物贴满预算 → 下轮贪心装不下两份全走强制两两 → 16k chars 批（≈13k tok）在 16k 上下文爆墙（结构性必然）。修①合并即精选措辞②`_cap_analysis` 中间轮机械量控到半预算（主角>配角>路人权重排序逐条累加，最终产物压全预算——超长篇全量清单对拆解上下文注入也是爆炸）③`_merge_call` length 防御链：常规→`_MERGE_COMPACT_SYSTEM` 极限压缩重试→机械取首份（树必然收敛）；provider LLMError 加 `kind="length"` 属性供调用方分流。注意 merge 调用不落 llm_calls（审计只有 extract）
- **二期 v1.1/v1.2（2026-09-13 分析报告落地 docs/2026-09-13-comic-workflow-analysis.md，零下载）**：`zimage_page_ref` 升级 **Edit-2511 多图**（`TextEncodeQwenImageEditPlus` 三图槽 image1/2/3）——char1+char2 双角色**真参考注入**（官方多人合照一致性强化，提示词按图号引用「图1的人物与图2的人物」）；**Lightning 4 步**（`LoraLoaderModelOnly` 挂 model 链，`comfy.page_ref_lightning` 默认 1.0 / 0=关；steps 钳 4、cfg=`page_ref_cfg` 默认 2.5——8 步→4 步速度约翻倍）；缺省槽**白图占位**（`_ensure_blank` data/_cache/blank_ref.png stdlib 生成，提示词不提的图号被模型忽略）；v1.2 场景槽：绑定场景资产有 main.png → 图3 场景参考（person+scene 官方组合，提示词「图3中的场景」）——场景主图手动入口=资产卡「主图」按钮（v-if 放开全 kind，tooltip 指引）；双角色文字锚降级为名字（图2 已是真参考）
- **二期角色参考注入（2026-09-13，spec docs/superpowers/specs/2026-09-13-comic-ref-injection-design.md）**：`zimage_page_ref` 模板（v4 同构：LoadImage(base=角色主图)→Resize→VAEEncode→Edit-2511 图条件）做**人物场景化**——「将图中人物置于{场景}」+面部发型服装保持一致；handler 分流：绑定角色 ≤2 且有 main.png → 参考模板（base=第一角色主图+第二角色文字锚，denoise=`comfy.page_ref_denoise` 默认 0.9 可调），无绑定/主图缺/>2 → 纯 t2i 文字锚；模板缺失 warn 退。**双角色 IPAdapter 叠加留 v1.1**（单角色真机验证后加）。链路：确认资产（B1）→ 自动 `gen_ref stage="main"` 只烧角色主图（三视图/场景道具不烧）→ 停等「✓ 主图满意，继续出片」（迁移 39 `comic_refs_done`，POST confirm-comic-refs）→ 拆解 → 逐页分流注入。存量项目「全部重出」即生效（分流读现状零迁移）
- **漫画参数动态调整 + 重生成 + 批量（2026-09-13 A）**：详情参数 pill 回归修复（漫画项目专用 `📖 尺寸 · 质量档 · 对白呈现` 入口——上轮隐藏视频 pill 时误把面板入口一起藏了）；`generate-comic-pages` 扩 `?force=1`（全部重出，改参数后用）与 `body.shot_ids`（单页/所选重出——**显式覆盖语义，已有页不再跳过**：真机单卡重出被补缺语义拦下 enqueued 0 判例）；漫画页模式多选（checkbox↔shotSel 复用 + 全选）批量条：重出所选/无效/生效/删除；comicGenBusy 忙→闲边沿翻新 `_pagesStamp`（重出新页不吃浏览器旧缓存）
- **漫画→视频转化（2026-09-13 用户需求·`POST convert-to-video`）**：comic_ready 后「🎬 转视频」开**设置面板**（同漫画导入面板减上传；**画风预填漫画项目**可改——未拆层/空 style_vis 同步双字段，独立拆层只动 style；画幅/兆像素/倍速/质量档/字幕/渲染模式/提示词模式全可设）。转化：`comic_mode→''`、stage→storyboard_ready、**镜清 prompt+workflow_type→ref2va（'comic' 会让视频渲染缺模板）+status ready**、subtitles→1（视频画面无气泡文字）、autopilot=1。之后 novel 链全自动：**gen_prompts（分镜描述→H3 视频提示词，用户决策不 VLM 读图——对白本在 ledger 不丢；不行手动🔄强制重读，转化后视频按钮自然重现）**→渲染→门3→TTS+字幕→合成。分镜/对白/角色资产+主图/正文/章节全复用，漫画页保留 pages/ 可导出。guard 404/422（非 comic_output/已转化）/409（非 comic_ready）
- **导引页分链 + 按钮链路门控（2026-09-13）**：导引页按产物类型分两大区（🎬 视频项目四卡 / 📖 漫画项目漫画成书卡——三数据源+链路+参数说明），每卡「➕ 去创建」直跳创建弹窗预选类型+入口（`guideCreate`）；hero/核心功能区补漫画成书；详情页 comic_output 隐藏九项视频链按钮（批量生成提示词/批量渲染/🚄快车道/🎙配音/👁读图/🔄强制重读/⏱重估时长/🎭角色配音/🗑清理提取资产[收窄 motion/film]——逐项安全依据：autopilot 恒走 `_comic_pages_gap`、describe_shots 是导入源页链、音色只被 TTS/H3 消费、comic 资产来自小说分析不可清）+ 参数面板视频参数整组隐藏（`isVideoChain`/`isComicOutput` computed 单源）；通用链按钮保留（拆分分镜/门1门2/参考图/补绑/批量无效/停止/正文）
- 注意：角色参考图分析/生成照跑（拆解上下文用），但**页面生成不消费资产参考图**（build_comic_prompt 不注入——角色一致性=二期「参考图注入」，spec 决策 6 预留）；gen_comic_page 不在重启重排白名单（重启丢在跑页任务需手动补页——同 describe_shots 约定）
