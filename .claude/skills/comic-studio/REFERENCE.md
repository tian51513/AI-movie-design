# REFERENCE · 生产线与开发细则

> 深层参考；SKILL.md 是入口。行号为 2026-09-05 快照，以代码为准。

## 1. 阶段状态机与门禁

- stage：`created → analyzed → assets_ready → storyboard_ready → rendered → merged`（`rendering` 是死枚举，从未写入）
- 漫画导入（motion_comic 动态漫 / film_adaptation 漫改）直达 storyboard_ready
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

## 3. 时长体系

| 层 | 规则 |
|---|---|
| 拆解估时 | 对白镜 ⌈字数/4⌉+0.6×(句数-1)；无对白镜按动作复杂度（打斗 8~12s/全景 6~8s）或统一段时长；钳 [4,15] |
| 打包预算 | 台词组 3~8 句、总字数 ≤48 字（≈12s 封口）超了开新镜 |
| 段时长 0 | =LLM 动态估（默认）；>0=统一段时长；总时长 >0=按镜数均摊（下限 4s） |
| 成片收口 | 对白镜段长恒=配音+0.5s（`merge._replace_audio(target=)`：长者 tpad 末帧定格、短者 -t 截尾；音频 apad+44100 立体声统一） |
| 字幕轴 | 对白镜=配音+0.5 镜像；其余=probe(video) 真实时长（DB 字段≠实际渲染时长：17k+5 帧对齐 4.0→4.5） |

## 4. 音频/音色决策链

```
ref2va 渲染注入音色样本 → H3 原声口型（ledger.h3_native_voice → 合成跳过 TTS）
fl2v/i2v/t2v（无音频槽）→ 单说话人+绑音色 → qwen_tts_clone 整镜克隆
                        → 多说话人/未绑/失败 → Edge-TTS 按性别
```
- 音色库：15 预设 + 全局自定义 + 项目级；LLM 分析期音色库感知（R1 声线描述自动生成+绑定）
- 御姐系样本（高冷御姐/色气御姐）= 气声慢板基调，克隆继承 → 表现力需求高的角色避开
- TTS 生成先于 SRT（音长可知）；merge handler 自动前置 TTS+SRT

## 5. 合成链（engine/merge.py）

```
无效镜剔除 → normalize（画布统一/44.1k 立体声）→ 对白镜收口（§3）
→ 无台词镜可选静音（mute_quiet_shots）→ concat（或 xfade，默认关）
→ 字幕烧录（滤镜裸名+cwd；路径全绝对化）→ output/epNNN.mp4（merged 可重合成出新号）
```
已知坑（审计中危未修）：epNNN=len+1 非 max+1（删部分旧片会覆盖正片）；xfade 时 SRT 不扣交叠；硬拼 concat 无 _ensure_audio；快车道 director_mix 未适配收口。全清单见 docs/2026-09-05-feature-audit.md §4。

## 6. 队列与资源

- `jobs`：enqueue/claim（BEGIN IMMEDIATE 互斥）/retry_or_fail（attempts≥3 落 failed）/cancel_project_jobs（pending→cancelled、running→attempts=99）
- 资源组：gpu_comfy 与 gpu_llm_local 同属 "gpu" 组互斥（渲染与本地 LLM/VLM 串行）
- ComfyUI 等待：排队不计失速、排队超 1h 只 DELETE 自己、interrupt 仅当 /queue 确认在跑（防误杀他任务）
- 断点对账：重启先 reattach（history 已完成直接落盘）后 requeue

## 7. 开发细则补充

- LLM provider：路由值支持 `provider:model` 点对点钉选；local2=重度模型（extra_body 恒 null 物理隔离）；思考模型烧窗用 extra_body `{"reasoning_effort":"none"}`（Ollama /v1 实测有效）
- Ollama num_ctx=16384：拆分块 1300 字上限的推导依据；截断先查 finish_reason
- 提示词模式 A-E（默认 E 英文控制式）；heal_h3_prompt 机械自愈不耗重试
- 画幅五档 ASPECT_RATIOS 统一校验；工作流不支持的画幅回落 16:9
- 模板：templates/workflows/*.yaml manifest（inject slots: prompt/params/images/audio）+ filler 注入

## 8. API 速查（常用）

```
POST /api/projects（上传）· /from-theme[/preview] · /from-comic
POST /api/projects/{id}/analyze | /split-storyboards | /describe-shots | /merge | /tts | /stop-jobs
POST /api/shots/{id}/render | /regen-prompt   · POST /api/projects/{id}/generate-prompts | /render-batch
PATCH /api/projects/{id}（autopilot/style/画幅/段时长/render_mode…）
GET  /api/projects/{id}/merges · /api/jobs/{id}/snapshot
POST /api/voices/design|promote · DELETE /api/voices?scope=…
POST /api/jobs/purge?days=7-90 · GET /api/comfy/status
```
