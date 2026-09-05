# comic_studio 开发约定

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
- 注意：估时改字数只影响新拆解项目；存量项目想生效需重拆或手改镜时长；`merged` 阶段开放「重新合成」（POST /merge 放行 rendered/merged）
