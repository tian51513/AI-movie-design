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

## 6. 渲染链（engine/rendershot.py、workflows/）

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

## 7. 音频/音色决策链

```
ref2va 渲染注入音色样本 → H3 原声口型（ledger.h3_native_voice → 合成跳过 TTS）
fl2v/i2v/t2v（无音频槽）→ 单说话人+绑音色 → qwen_tts_clone 整镜克隆
                        → 多说话人/未绑/失败 → Edge-TTS 按性别
```
- **R1 分析期音色决策**（analyze 尾链）：LLM suggested_voice 在库内→直接绑（零成本）→ 有 voice_description→generate_custom 生成项目级并自动绑（角色名命名，幂等）→ 性别×年龄 8 档基线兜底；ComfyUI 不可用只 warn 不炸分析；生成后置 LLM 分块结束（不抢显存）
- 音色库：15 预设（qwen_tts_design）+ 全局自定义（可删，绑定自动回退 Edge-TTS）+ 项目级（可晋升全局，重名 409）；LLM 系统词注入当前库清单
- 御姐系样本（高冷御姐/色气御姐）= 气声慢板基调，克隆继承 → 表现力需求高的角色避开
- TTS 生成先于 SRT（音长可知）；merge handler 自动前置 TTS+SRT

## 8. 合成链（engine/merge.py）

```
无效镜剔除 → normalize（画布统一/44.1k 立体声）→ 对白镜收口（§4）
→ 无台词镜可选静音（mute_quiet_shots）→ concat（或 xfade，默认关）
→ 字幕烧录（滤镜裸名+cwd；路径全绝对化）→ output/epNNN.mp4（merged 可重合成出新号）
```
已修（2026-09-05 审计批次）：epNNN=max+1（merge.next_ep_number，快车道共用）；xfade 时 SRT 扣 0.3s 交叠；normalize 无轨补 anullsrc；xfade _sil 进临时目录。**仍开放**：快车道 director_mix 未适配音频收口（TTS 超 span 截断仅 warn）；>120 段 xfade 回退硬拼时 SRT 仍扣交叠（罕见）。全清单 docs/2026-09-05-feature-audit.md §4。

## 9. 队列与资源

- `jobs`：enqueue/claim（BEGIN IMMEDIATE 互斥）/retry_or_fail（attempts≥3 落 failed）/cancel_project_jobs（pending→cancelled、running→attempts=99）
- 资源组：gpu_comfy 与 gpu_llm_local 同属 "gpu" 组互斥（渲染与本地 LLM/VLM 串行）
- ComfyUI 等待：排队不计失速、排队超 1h 只 DELETE 自己、interrupt 仅当 /queue 确认在跑（防误杀他任务）
- 断点对账：重启先 reattach（history 已完成直接落盘）后 requeue

## 10. 开发细则补充

- LLM provider：路由值支持 `provider:model` 点对点钉选；local2=重度模型（extra_body 恒 null 物理隔离）；思考模型烧窗用 extra_body `{"reasoning_effort":"none"}`（Ollama /v1 实测有效）
- Ollama num_ctx=16384：拆分块 1300 字上限的推导依据；截断先查 finish_reason
- 转写校对遍（P10C 提前落地 2026-09-05）：cleanup_transcription 按段 LLM 清洗（同音错字/纯语气词段丢弃，时间轴保留）→ 重写正文+段落盘+章节；路由键 asr_cleanup 默认 local；正文弹窗「✨ LLM 校对」按钮触发
- 提示词模式 A-E 契约见 [PROMPTS.md](PROMPTS.md)（默认 E 英文控制式；heal 自愈不耗重试）
- 画幅五档 ASPECT_RATIOS 统一校验；工作流不支持的画幅回落 16:9
- 模板：templates/workflows/*.yaml manifest（inject slots: prompt/params/images/audio）+ filler 注入

## 11. API 速查（常用）

```
POST /api/projects（上传）· /from-theme[/preview] · /from-comic · /from-audio（🎧，P10A）
POST transcribe 经 from-audio 自动入队（无手动重发入口——失败重传项目）
POST /api/projects/{id}/analyze | /split-storyboards | /describe-shots | /merge | /tts | /stop-jobs | /asr-cleanup（✨转写校对）/retry-transcribe
GET  /api/projects/{id}/novel-text（📄正文查看）
POST /api/shots/{id}/render | /regen-prompt   · POST /api/projects/{id}/generate-prompts | /render-batch
PATCH /api/projects/{id}（autopilot/style/画幅/段时长/render_mode…）
GET  /api/projects/{id}/merges · /api/jobs/{id}/snapshot
POST /api/voices/design|promote · DELETE /api/voices?scope=…
POST /api/jobs/purge?days=7-90 · GET /api/comfy/status
```
