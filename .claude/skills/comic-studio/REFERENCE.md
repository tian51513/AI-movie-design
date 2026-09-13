# REFERENCE · 生产线与开发细则

> 深层参考；SKILL.md 是入口。权威源码以 engine/ 为准，历史教训见 CLAUDE.md 模块地图。

## 1. 阶段状态机与门禁

- stage：`created → analyzed → assets_ready → storyboard_ready → rendered → merged`（`rendering` 是死枚举，从未写入）
- 🎧 有声书（P10A）：created 起 transcribe job 回填正文后才放行分析（源音频在+segments 缺 → wait/409）；analyzed 零资产 → wait 不空转；约定路径 `projects/<slug>/audio/{source.*,segments.json}`；拆解时长=音频段实测（音频项目禁 target 均摊）
- 漫画导入（motion_comic 动态漫 / film_adaptation 漫改）直达 storyboard_ready；**画风选择**（2026-09-06）随创建提交 style/style_vis——漫改可见可选（转换目标），动态漫不消费（画风跟随原页，前端明示）
- 唯一写入口 `engine/projects.set_stage`；门禁 `engine/pipeline_gates.gate_pass`（GateStageError=409）
  - 门1：全部角色资产有 views 图 → assets_ready
  - 门2：全部生效镜有提示词 → storyboard_ready（检查型）
  - 门3：全部生效镜有 video_path → rendered
- storyboard_ready 允许重拆（覆盖式，引擎删镜前清 jobs.shot_id 外键）
- 🚄 整段快车道（gen_director）storyboard_ready → merged 旁路（导演台整片，v1）

## 2. autopilot 决策规则（engine/autopilot.py）

- `next_action` 纯决策（可测）+ `tick` 执行；每 3s 巡检幂等
- 失败守卫：批次型（analyze/split/describe_shots/merge）最新 job failed → wait「重试请手动发起」；逐镜型（gen_prompt/gen_shot）跳过失败镜推进其余、全卡死才 wait；卡死首次报一条 error（`_STUCK_REPORTED` 去重）
- 守卫解除 = 手动重发产生新 job（UI 各按钮即入口）
- 在飞感知：门3/merge 前查 gen_shot 在飞（重渲染收尾前不推进，防旧视频拼进成片）
- merge action 只入队 merge job——TTS/字幕前置在 merge handler 内（巡检线程零长活）

## 3. 分镜拆解与控制（engine/llm/storyboard.py）

**拆解上下文注入**（build_split_user_prompt）：可用资产白名单（只允许绑定名单 id，名单外转无名背景）＋可选【数量约束】配额（target_count 按块字数占比分配）＋【时长基准】（段时长>0 统一 / 0=动态估）。
**拆解参数**：块 1300 字上限（16k num_ctx 实证）；章节范围切分（中文数字+Chapter N）。

**对白机械兜底 backfill_dialogue**（模型不填 dialogue 字段时从 text_span 引号提取，说话人三则）：
1. 引号紧前有说话动词（说道：）→ 前名优先，后方窗口不争抢
2. 双向就近兜底
3. 相邻引号间无名字/提示 → 中文对白交替惯例换人

**结构化字段**（驱动动态，杜绝僵硬）：emotion 15 枚举、gesture/gaze 英文短句、continuity 6 枚举（全程继承/微变延续/焦点跟随/缓慢推镜/缓慢拉镜/场景断点——外值 staging 清空）。

**staging 机械校准**：continuity∈{全程继承,微变延续} 而 LLM 给 ref2va → 改 fl2v（t2v 永不覆盖）；延续组内 seed +3/镜（断点/空重开随机）；时长钳 [4,15]（见 §4）。
**拆解后**：`reestimate_durations` 补录镜重估；机械审计 warn 不拦截（时长守恒/画面换挡>30s/台词>25字）；全顺序镜自动依赖链（尾帧接力基础）。
**分镜操作**：disabled 无效镜（不进门禁/渲染/合成）、批量无效/生效/删除、框选（鼠标+触控）、单镜改时长（1~15）、单镜 workflow 下拉、重拆（assets_ready/storyboard_ready 均可，覆盖式）。

## 4. 时长体系

| 层 | 规则 |
|---|---|
| 拆解估时 | 对白镜 ⌈字数/4⌉+0.6×(句数-1)；无对白镜按动作复杂度（打斗 8~12s/全景 6~8s）或统一段时长；钳 [4,15] |
| 打包预算 | 台词组 3~8 句、总字数 ≤48 字（≈12s 封口）超了开新镜；duration=句数×2.5 已废（无视句长截对白） |
| 段时长 0 | =智能估（小说链 LLM 动态估 / 漫画链读图后对白字数基准重估，2026-09-06 起）；>0=统一段时长；总时长 >0=按镜数均摊（下限 4s，漫画导入时） |
| 成片收口 | 对白镜段长恒=配音+0.5s（`merge._replace_audio(target=)`：长者 tpad 末帧定格、短者 -t 截尾；音频 apad+44100 立体声统一） |
| 字幕轴 | 对白镜=配音+0.5 镜像；其余=probe(video) 真实时长（DB 字段≠实际渲染时长：17k+5 帧对齐 4.0→4.5） |

## 5. 资产分析与参考图（engine/llm/analyze.py、genref.py、assets.py）

**提取规则（防污染）**：不建旁白/画外音/群体称谓（…们/众人/路人，`_NON_SPEAKER_RE`/`_NARRATION_WORDS`）/未出场者；persist 前机械丢弃名字不在原文的角色（幻觉名，warn 透明）；合并打包 `exclude_defaults` + 按精确增量计费。
**外貌编辑**：routes_assets_edit（服装修正入口）→ `mark_stale_for_asset` 联动下游（相关镜提示词标 stale，批量生成时重生，不卡门2）。
**参考图 gen_ref**：画风拆层——style_vis（视觉子集）给主图/关键帧，叙事词只留在 style 给视频提示词；写实意图检测（PHOTO_RE→PHOTO_BOOST+全身照措辞）；外貌行压缩 condense_appearance（丢「无」行、性别英文锚）；**模板方言** prompt_style（manifest 声明 natural_zh/tags_en，SD 系 CLIP 走英文标签流）。
**目录结构**：`data/library?`→assets 表 library_dir 相对 POSIX；views/（三视图）+ main.png 为「有图」判据（**目录存在≠有图**——persist 恒建空目录，判断用 has_views 按文件）。
**漫改提取两入口**：describe_shots 顺手提取（对白说话人聚合，幂等）vs 手动 extract_comic_characters（采样读页，清空重建——注意不清 ledger 旧 id，审计中危）；两路按 (kind,name,source_project) 去重。

## 6. 动态漫角色重绘操作序（2026-09-09 建；2026-09-12 大修）

**适用**：motion_comic 项目勾「是否重绘角色」（漫改忽略——画风本就要转换）。目的：VLM 提取主要角色→按画风重绘角色主图→整页重绘分镜首尾帧出新版本；原页构图不动，只换画风/清气泡文字。设计决策表 docs/superpowers/plans/2026-09-09-motion-comic-character-redraw.md §0。

1. **创建**：漫画 tab 勾「是否重绘角色」（联动字幕默认开）；画风可选「Krea2 风格库…」预览面板（73 库缩略图点击确认）；存量项目详情页 🎨 pill → 画风面板改画风+Krea2 选择
2. **🚀 一键出片**：describe_shots 读原页 → autopilot 提取+主图 → 停等检查
3. **检查/重试**：资产卡「主图」按钮重roll；检查满意后继续
4. **🖌 批量重绘分镜**：逐镜 redraw_kf job（模板设置页可选 v4/v5.1/v7/**v8 Krea2 快道**推荐——10-15s/页；改模板不需重启）→ 新版本 kf_start_v{N}+前镜尾帧联动+视频置空
5. **续跑**：autopilot 自动渲染→合成
6. **不满意**：单镜「🔄 重生此帧」（新 seed 新版本）；chips 切版本秒级（缩略图 360px + 纳秒缓存戳）

**重绘模板矩阵**（设置页「整页重绘模板」切换）：v4 Edit-2511（画质道 1-3 分/页）/ v5.1 turbo+Fun-CN（速度道）/ v7 H3 抽帧（ref2va 短视频 2s）/ **v8 Krea2 快道**（主力——LazyKreaWorkbench 双图分工+identity_edit LoRA 必接+编辑LoRA 从 Krea2 子目录选）

**多人镜策略**（真机四修定稿）：模板 char 槽装不下全部绑定角色时（Krea2=1 槽 vs 2+ 角色）→ 全部槽填纯白图 + 不注入身份指令 → 纯风格转换（保人数不贴主图）；单人镜正常给参考图；零绑定=场景专用提示词（严禁添加人物+剥离肢体纠错词）

## 6a. 小说转漫画操作序（comic_output，2026-09-12 建）

**适用**：小说正文直接出漫画成书（页面即交付物，**无视频渲染/门3/合成链**；终态 `comic_ready`，autopilot done 自动关）。前半链与小说共用（分析→参考图→门1→拆解），拆解走漫画页分支（场景+人物+对白，禁运镜/声音/时长；workflow_type 机械固定 'comic'，**拆解即填 prompt=description 不烧视频提示词**）。

1. **创建**：创建弹窗「📖 漫画」tab——正文 .txt（UTF-8）+ 页数（0=按剧情密度自动）/尺寸（10 档含 512/768 小尺寸）/质量档（fast8·standard12·high20 步）/对白呈现（bubble/footer/none）；画风可选 Krea2 风格库
2. **🚀 一键出片**：分析→参考图→门1→拆解→逐页 t2i（`gen_comic_page`→`pages/page_NNN.png`，模板=template_map.comic_page 可切）→「漫画就绪」自动停
3. **浏览/补页/重出**：详情「漫画页」页签（x/N 就绪 pill、点击放大、对白行、多选批量：重出所选/无效/生效/删除）；缺页「🖼 生成缺失页」；改参数（尺寸/画风/对白呈现/气泡样式——📖 参数 pill 开面板即改）后「🔄 全部重出」或单卡「🔄 重出」（读最新参数新 seed 覆盖）
4. **导出**：📄 导出 PDF（Pillow 多页，需 `pip install -e ".[pdf]"`）/ 📜 导出长图（ffmpeg vstack 竖拼）→ `output/comic.pdf|comic_strip.png` 同名覆盖；无效镜/已删残页不进导出
5. **对白呈现**：bubble=顶部左右交错真气泡（Pillow 后处理：白底圆角+描边+尾巴，宽度内容自适应上限 40% 页宽；透明度只作用底色、字色/字号项目级可配——参数面板或创建时）；footer=底部字幕条（Pillow 画字，换行+字体回退）；none=不呈现。**改呈现/样式后：删对应页 →「🖼 生成缺失页」重出**（提示词禁字指令与后处理都吃新值）

**注意**：comic_output 项目详情只显示漫画链按钮（视频链按钮全隐藏——批量渲染/快车道/配音/读图/时长重估/角色配音等；拆分分镜/门1门2/参考图/补绑角色/批量无效保留）；导引页按「视频项目/漫画项目」两大区展示全部操作链，每卡「➕ 去创建」直跳预选。角色参考图分析/生成照跑但页面生成不消费（角色一致性=二期参考图注入）；gen_comic_page 不在重启重排白名单——重启丢在跑页任务用手动补页；subtitles 恒 0（对白页面自呈）。

## 7. 渲染链（engine/rendershot.py、workflows/）

**模板选型**（workflow_type → template_map 可配）：
| workflow_type | 模板 | 何时用 |
|---|---|---|
| ref2va | h3_ref2va | 常规（参考角色/场景出图，**带音色槽**——绑音色则注入样本保 H3 原声口型） |
| fl2v | h3_fl2v | 与上一镜衔接（同场景连续动作）；首尾帧插值 |
| t2v | h3_t2v | 建立全新画面且无参考 |
| i2v | h3_i2v | fl2v 失败降级 / 单帧起始 |
**关键帧**：ensure_keyframes 缺 kf_start/kf_end 时自动生成首尾对（zimage_t2i，同 seed 保构图、成对约束入词、KF_NO_CUT 镜内禁切、fl2v 对齐头）；生成失败降级 h3_i2v。
**渲染细节**：多版本 video_v{N} 落盘（版本切换即改 video_path）；lora_strength 注入（项目 lora_realism）；远景升兆像素；画幅注入（manifest 不支持回落 16:9）；seed 优先库值；上镜尾帧接力（extract_last_frame→本镜首帧槽）。
**可靠性**：断点对账（重启先 reattach ComfyUI history 已完成直接落盘，后 requeue）；单镜重渲=force 语义；批量渲染排队去重；jobs 快照审计（attach_snapshot 留提示词+工作流 JSON）。
**模型切换**：manifest `models:` 槽位 → settings model_overrides（键=模板 id）→ filler 注入；choices 从 /object_info 枚举。
**🚄 快车道开关**（settings comfy.*）：director_batch_frames（默认 512）、批间首帧接力 director_batch_relay、整片混音 director_mix、清显存/导出源帧；画布按项目兆像素 ×32 ceil 对齐。

## 8. 音频/音色决策链

```
ref2va 渲染注入音色样本 → H3 原声口型（ledger.h3_native_voice → 合成跳过 TTS）
fl2v/i2v/t2v（无音频槽）→ 单说话人+绑音色 → qwen_tts_clone 整镜克隆
                        → 多说话人/未绑/失败 → Edge-TTS 按性别
```
- **R1 分析期音色决策**（analyze 尾链）：LLM suggested_voice 在库内→直接绑（零成本）→ 有 voice_description→generate_custom 生成项目级并自动绑（角色名命名，幂等）→ 性别×年龄 8 档基线兜底；ComfyUI 不可用只 warn 不炸分析；生成后置 LLM 分块结束（不抢显存）
- 音色库：15 预设（qwen_tts_design）+ 全局自定义（可删，绑定自动回退 Edge-TTS）+ 项目级（可晋升全局，重名 409）；LLM 系统词注入当前库清单
- 御姐系样本（高冷御姐/色气御姐）= 气声慢板基调，克隆继承 → 表现力需求高的角色避开
- TTS 生成先于 SRT（音长可知）；merge handler 自动前置 TTS+SRT

## 9. 合成链（engine/merge.py）

```
无效镜剔除 → normalize（画布统一/44.1k 立体声）→ 对白镜收口（§4）
→ 无台词镜可选静音（mute_quiet_shots）→ concat（或 xfade，默认关）
→ 字幕烧录（滤镜裸名+cwd；路径全绝对化）→ output/epNNN.mp4（merged 可重合成出新号）
```
已修（2026-09-05 审计批次）：epNNN=max+1（merge.next_ep_number，快车道共用）；xfade 时 SRT 扣 0.3s 交叠；normalize 无轨补 anullsrc；xfade _sil 进临时目录。**仍开放**：快车道 director_mix 未适配音频收口（TTS 超 span 截断仅 warn）；>120 段 xfade 回退硬拼时 SRT 仍扣交叠（罕见）。全清单 docs/2026-09-05-feature-audit.md §4。

## 10. 队列与资源

- `jobs`：enqueue/claim（BEGIN IMMEDIATE 互斥）/retry_or_fail（attempts≥3 落 failed）/cancel_project_jobs（pending→cancelled、running→attempts=99）
- 资源组：gpu_comfy 与 gpu_llm_local 同属 "gpu" 组互斥（渲染与本地 LLM/VLM 串行）
- ComfyUI 等待：排队不计失速、排队超 1h 只 DELETE 自己、interrupt 仅当 /queue 确认在跑（防误杀他任务）
- 断点对账：重启先 reattach（history 已完成直接落盘）后 requeue

## 11. 开发细则补充

- LLM provider：路由值支持 `provider:model` 点对点钉选；local2=重度模型（extra_body 恒 null 物理隔离）；思考模型烧窗用 extra_body `{"reasoning_effort":"none"}`（Ollama /v1 实测有效）
- Ollama num_ctx=16384：拆分块 1300 字上限的推导依据；截断先查 finish_reason
- 转写校对遍（P10C 提前落地 2026-09-05）：cleanup_transcription 按段 LLM 清洗（同音错字/纯语气词段丢弃，时间轴保留）→ 重写正文+段落盘+章节；路由键 asr_cleanup 默认 local；正文弹窗「✨ LLM 校对」按钮触发
- 提示词模式 A-E 契约见 [PROMPTS.md](PROMPTS.md)（默认 E 英文控制式；heal 自愈不耗重试）
- 画幅五档 ASPECT_RATIOS 统一校验；工作流不支持的画幅回落 16:9
- 模板：templates/workflows/*.yaml manifest（inject slots: prompt/params/images/audio）+ filler 注入

## 12. API 速查（常用）

```
POST /api/projects（上传）· /from-theme[/preview] · /from-comic · /from-audio（🎧，P10A）· /from-comic-novel（📖 小说转漫画，§6a）
POST transcribe 经 from-audio 自动入队（无手动重发入口——失败重传项目）
POST /api/projects/{id}/analyze | /split-storyboards | /describe-shots | /merge | /tts | /stop-jobs | /asr-cleanup（✨转写校对）/retry-transcribe
GET  /api/projects/{id}/novel-text（📄正文查看）
POST /api/shots/{id}/render | /regen-prompt | /regen-keyframes | /use-kf · GET /api/shots/{id}/kf-versions
POST /api/projects/{id}/generate-prompts | /render-batch | /batch-redraw（🖌 动态漫整页重绘，§6）
POST /api/projects/{id}/generate-comic-pages（🖼 手动补页，§6a）· /export-comic?format=pdf|strip（§6a）
PATCH /api/projects/{id}（autopilot/style/画幅/段时长/render_mode…）
GET  /api/projects/{id}/merges · /api/jobs/{id}/snapshot
POST /api/voices/design|promote · DELETE /api/voices?scope=…
POST /api/jobs/purge?days=7-90 · GET /api/comfy/status
```
