# 功能全景与体检报告（2026-09-05）

> 审计范围：master@1790e1f。四路并行只读审查（生命周期状态机 / 音视频合成链 / 队列与设置 / 前端与 API 面）+ 当日真机修复链复盘。
> 结论速览：核心链路无阻断性缺陷；发现若干中低severity问题与已知缺口，见 §4/§5。

## 1. 总览

- 架构：FastAPI（web/）+ 纯 engine（禁 web 依赖，未来抽 ComfyUI 节点）+ SQLite（WAL，8+ 表）+ 本地 Vue3 单页（无 CDN）+ ComfyUI（视频/TTS/关键帧）+ 本地/云端 LLM（分析/拆解/提示词）
- 数据：`./data`（DB + library + projects + voices），WSL 与 Windows 原生共享（POSIX 相对路径）
- 测试：pytest 483 通过；LLM 一律 FakeClient、ComfyUI 一律 comfy_mock，不触网

## 2. 功能点全景

### 2.1 项目创建与参数
- 三入口：上传小说（multipart）/ 主题两步流（preview 只生成不建 + 直建 ≥100 字免 LLM）/ 漫画导入（每图一镜，motion_comic 动态漫 | film_adaptation 漫改）
- 项目参数：五档画幅（9:16/16:9/3:4/4:3/1:1，创建后可改）、兆像素/倍速/速度档、**段时长（默认 0=LLM 动态估时）**、**总时长（默认 0=不限；>0 按镜数均摊下限 4s）**、prompt_mode（A 散文/B 结构化/C 构图/D 多镜/E 英文控制式，新项目默认 E）、lora_realism、era、style/style_vis 拆层
- 章节切分（中文数字+Chapter N）→ 按章范围拆分镜

### 2.2 分析与资产
- LLM 分析：角色/场景/道具提取（幻觉名机械丢弃、旁白/群体称谓不建）、外貌编辑（stale 联动下游）、资产绑定（白名单纪律）
- 参考图：gen_ref（画风拆层 style_vis、写实意图检测、模板方言 natural_zh/tags_en）、四视图/主图、批量生成
- 漫改：VLM 提取角色（音色库感知、建资产即绑音色）

### 2.3 分镜（shots）
- LLM 拆解：块 1300 字、分镜数配额、**台词组打包 3~8 句 + 字数预算 ≤48 字**、对白机械兜底 backfill（说话人三则：说话动词前名优先/双向就近/交替惯例）、**时长：对白=字数÷4+句间 0.6s、无对白按动作复杂度（打斗 8~12s/全景 6~8s）或统一段时长**、emotion/gesture/gaze/continuity 结构化字段、seed 延续组 +3
- 拆解后机械审计（时长守恒/换挡/台词字数 warn）；依赖链自动尾帧接力
- 分镜控制：无效镜（disabled 不进门禁/渲染/合成）、批量无效/生效/删除、框选（桌面鼠标+触控）、单镜时长手改
- 漫画：VLM 读图 describe_shots（读图+提示词+角色提取+对白聚合一遍完成，动态漫/漫改两模式产物格式）

### 2.4 提示词
- 四+一模式（E 英文控制式为默认）：素材职责声明、[Shot N] 段、对白 `<d>[Mandarin Chinese]`、Preserve/Add 音频句、结尾负面清单
- heal_h3_prompt 机械自愈（⑦音频协议缺节补齐、⑧围栏卫生、Meta 剥离、截断压缩重试）；紧凑重试（小模型结构失败换短骨架）
- 批量生成 + stale 重生（参考图重生后不卡门2）

### 2.5 渲染
- 模板链：h3_t2v / h3_i2v / h3_fl2v（首尾帧）/ h3_ref2va（角色参考+音色槽）/ zimage_t2i（关键帧）/ 自定义模板（manifest 声明 slots）
- 关键帧：ensure_keyframes 缺对自动补（同 seed 保构图、KF_NO_CUT 禁切）、fl2v 对齐头
- 渲染体验：多版本 video_v{N}、断点对账 reattach（重启不重渲）、失败降级（fl2v→i2v）、单镜重渲、批量渲染、批量渲染画幅注入（不支持的画幅回落 16:9）
- 🚄 整段快车道（导演台）：timeline 聚合一次提交、分批 512 帧、批间首帧接力、帧数轴整片混音（开关）

### 2.6 配音与音色
- 决策链：ref2va 渲染注入音色样本 → 保 H3 原声口型（h3_native_voice，合成跳过 TTS）；fl2v/i2v/t2v → 单说话人+绑音色 qwen_tts_clone 整镜克隆，多说话人/未绑 → Edge-TTS 按性别
- 音色库：15 预设（qwen_tts_design）、上传克隆两步制（staging→试听→confirm）、声线描述生成（R1 分析期自动生成+绑定）、项目级→全局晋升、LLM 音色库感知（注入系统词）
- 配音+字幕：POST /tts 一键生成 dialogue.mp3 + SRT

### 2.7 合成与字幕
- merge_project：无效镜剔除 → normalize（画布/44.1k 立体声统一）→ 对白镜**音频收口（段长=配音+0.5s 呼吸：长者末帧定格补齐/短者截尾）** → 无台词镜可静音（mute_quiet_shots）→ concat（或 xfade 交叉淡化+统一调色，默认关）→ 字幕烧录（滤镜裸名+cwd，Windows 安全）→ output/epNNN.mp4
- SRT：对白镜=配音+0.5 镜像排轴、其余按真实视频时长（字段≠实际渲染时长）；镜内多句按字数比例分时
- merged 阶段可「重新合成」（出新 epNNN 不覆盖）
- TTS 优先于 SRT 生成（音长可知）

### 2.8 一键出片（autopilot）
- 小说/漫画双流全自动免门禁；3s 巡检幂等续跑（已完成自动跳过）
- **暂停联动全停**（⏹ 停止自动 = 关开关 + 取消排队 + 打断在跑）
- **失败守卫**：拆解/读图/合成失败不自动重烧（一次性 error 上报等手动）；单镜提示词/渲染失败跳过推进其余
- **跨类型在飞感知**：重渲染未收尾不门3、不合成（旧视频不抢拼）
- 完成自动关；移动端底栏按阶段出关键按钮

### 2.9 设置与运维
- LLM：多 provider + 本地轻重双模型（local2 物理隔离 extra_body）+ 路由钉选（provider:model）+ llm-test（不限 max_tokens）
- Comfy：base_url（空值三道防线）、显存串行（LLM 让位→free→门槛 8GB）、工作流模型切换（/object_info 枚举）、性能开关（批帧/接力/混音/静音/xfade）
- 任务：项目级停止、7 天清理（logs 先清外键）、purge
- 启动：start.bat/start.sh（开发热重载）、start-prod.bat/start_silent_prod.vbs（无热重载+0.0.0.0 局域网）；bat 只按 8190 精确杀
- 日志：logbus 首拉倒序、前端轮询兜底；jobs 快照审计 + LLM 调用留痕

## 3. 四类项目生命周期路径

stage 唯一写入口 `set_stage`（projects.py:58）；全部写点：analyze.py:286（→analyzed）、comic.py:69（漫画直达 storyboard_ready）、pipeline_gates.py:56（门1/2/3）、merge.py:296 与 director.py:295（→merged）。

**小说上传 / 主题直建**（comic_mode=""，`_novel_flow`）：
```
created →[analyze]→ analyzed →[gen_refs]→ 门1 → assets_ready
  →[split]→(拆解不改 stage)→[gen_prompts]→ 门2 → storyboard_ready
  →[render]→ 门3 → rendered →[merge]→ merged
旁路：🚄 整段快车道 storyboard_ready → merged 直达（有意跳过门3，v1 设计）
```

**漫画导入（动态漫/漫改）**（`_comic_flow`，comic.py:69 直达 storyboard_ready）：
```
导入即 storyboard_ready →[describe_shots 读图+提角色+提示词]
  →[仅漫改] gen_refs → render → 门3 → rendered → merge → merged
```

路径异常结论：
- **"rendering" 是死枚举**——全库从未写入，前端却有显示名（中）
- 漫画项目可进入无法自救的阶段：导入中途失败留 created；对漫画手动 /analyze 会到 analyzed——`_comic_flow` 对两者都只回 wait 无出路（中）
- 漫改 autopilot 的参考图检查 `not views.is_dir()` 与 persist 总是 mkdir 空目录矛盾 → **漫改模式永远不触发 gen_refs，空参考图直进 render**（高，见 §4-H1）
- 单镜渲染/批量提示词 API 无 stage 校验（API 比 UI 宽，低）；无回退转移（设计内）

## 4. 体检发现（分级）

> 四路只读审查汇总，全部含 file:line 证据；行号以 master@1790e1f 为准。
> **H1/H2/H3 与全部中危（M1~M20）已于当日修复**（§4 各条目随修复提交标注；低危仍开放择机）。

### 高（3）

| # | 发现 | 位置 |
|---|---|---|
| H1 | 漫改 autopilot 参考图存在性判断用目录而非文件（`not views.is_dir()` vs persist_assets 恒建空目录）→ 永不补参考图 | autopilot.py:243-244 ↔ assets.py:43、pipeline_gates.py:20-24 |
| H2 | autopilot 巡检线程同步跑 TTS 克隆：不占 job 槽（绕过 gpu 互斥）+ 默认 300s 失速**从提交起算含排队**+ 超时 `/interrupt` 杀的是 ComfyUI 全局当前任务 → 可误杀在跑渲染（真机分镜19 事故机制）。同暴露面：POST /tts、analyze 期 generate_custom、gen_director 内 | autopilot.py:370-385、voicelib.py:93、client.py:118-132 |
| H3 | storyboard_ready 显示「拆分分镜」按钮（title 承诺重拆覆盖）但后端 `stage != assets_ready` 必 409——过门2 后 UI 死路 | index.html:859-862 ↔ routes_shots.py:80-81 |

### 中（按域）

**生命周期/autopilot**
- stale 提示词语义分裂：手动批量会重生 stale 镜，autopilot 只查空提示词 → 参考图重生后自动流程渲染旧提示词（autopilot.py:39-45 ↔ routes_shots.py:221-231）
- cancelled 不触发失败守卫 → autopilot 开着时「停止任务」打不死（3s 后重入队）（autopilot.py:55-59 ↔ jobs.py:59-71）
- 全无效镜两个无信号死循环：assets_ready 反复 gate2 静默吞异常零日志；storyboard_ready 恒 render 每轮「入队 0 镜」（autopilot.py:94-104、386-391）
- 手动重合成不重生 TTS/SRT——修合成侧问题后直接点会拼旧音轨（merge.py:249-251 vs autopilot.py:370-385）
- gen_director 后手动重合成 = 全镜 video_path 指同一整片 → 拼出「整片×N」废片（director.py:292-294 + merge.py:224-245）
- 手动「提取角色」清空重建资产但不清 shots.ledger 旧 id 绑定 → 渲染参考解析落空（comic.py:543-559 对照 purge_comic_assets:449-462）

**合成/音视频**
- POST /tts 无去重锁：与 autopilot merge 分支可并发双烧 ComfyUI/覆写同一 mp3；巡检线程被 5min TTS 卡住则**全部 autopilot 项目停摆**（routes_merge.py:47-61、app.py:111-117）
- director_mix 未适配音频收口：TTS 长于 span 被 `apad+atrim` 硬截断；逐镜/快车道两套时长规则漂移（director_mix.py:35-36 ↔ merge.py:158-184）
- xfade 路径：SRT 轴不扣 0.3s 交叠（21 镜累计漂 ~6s）；**硬拼 concat 无 _ensure_audio 补轨**（部分段无音轨会失败，与 09-05 事故同族）；acrossfade 入侧吃每句对白起音 0.3s（merge.py:56-143、subtitles.py:85-86）
- epNNN 编号 `len(glob)+1` 非 `max+1`：手动删部分旧片后新片**覆盖现存正片**；失败毛坯无清理列进 merges（merge.py:233-234、routes_merge.py:38-43；director.py:251-258 同款）
- h3_native_voice 只设不清 + subtitles 不判该 flag：换无音频槽模板重渲后该镜永远无声/字幕轴错位（rendershot.py:136-140、subtitles.py:47-53）
- 多版本切换不失效 dialogue.mp3：v2 画面迁就 v1 配音节奏（routes_shots.py:151-164）

**队列/设置**
- stop-jobs 的 ComfyUI `clear_queue()+interrupt()` 是全局的——跨项目清队误伤（routes_shots.py:343-344）
- stop-jobs 的 cancelled 行不写 finished_at → 永不 purge，jobs 表缓慢膨胀（jobs.py:64-66、172-173）
- llm_providers PUT 用全量 model_dump 无 exclude_unset（09-01 comfy 事故同款潜伏路径）；前端保存恒写 local2 extra_body=null 会抹 API 配置（routes_settings.py:81-83、app.js:627-29/756）

**前端**
- 漫画 tab 段时长/总时长输入完全无效（前端不发送、后端不接收）（index.html:1099-1102、app.js:948-962 ↔ routes_projects.py:147-150）
- 漫改「批量生成参考图/重新生成图」按钮被 analyzed/assets_ready 阶段 v-if 隐藏，漫画直达 storyboard_ready 后入口消失（index.html:823-849）
- `v-if` 与 `v-for` 同元素（Vue3 下 v-if 先求值）→ 分镜卡渲染 ledger 垃圾行（index.html:989）
- `actionLabel` 缺 describe_shots 映射 → 漫画项目 autopilot 角标显英文原词（app.js:523-529）
- 导引页「暂停与续跑」文案未更新为联动全停语义（index.html:320-325 ↔ routes_projects.py:333-341）

### 低（摘要）

死代码/死导入：autopilot tick 的 extract_comic_characters 分支永不触发（autopilot.py:315-321）、routes_shots.py:12 死导入 set_stage、jobs snapshot 端点无前端入口、describe_shot 不在路由表 UI、/api/health 无调用、`_justRead` 永不赋值、`refreshShots?.()` 不存在、video_multiple 无编辑控件；`_STUCK_REPORTED` 对已删项目不清（轻微泄漏）；finish_job 无状态守卫可把 cancelled 覆写成 done；段时长输入清空 `?? 0` 不兜空串（422）；voices upload/preset 缺 ensure_comfy_configured 门禁；describe/extract 资源硬编码 gpu_llm_local 与 routing 脱钩；PATCH autopilot=0 连手动任务一起取消（语义未区分）；手动 analyze 防重只查 running（与 autopilot pending 可双跑）；`rendering` 标签永不出现；导引「停止任务」卡与「清空队列」按钮语义混淆；delVoice/discardVoice 无失败反馈；createProject 失败仍清表单。

### 验证无异常（明确通过）

- 音频收口主干闭环：`-t target` 双流同截、concat 保长、烧录不截音频（仅烧录重编码参数未管控）
- duration=0 无法污染 shots 表（写入侧三处钳 4~15）；17 帧量化仅快车道
- claim/cancel 事务无任务逃逸窗口；retry_or_fail 不复活已取消任务（有测试护栏）
- latest_job 排序（id DESC=最新入队）多 worker 下正确；失败守卫「先查在飞再查失败」序正确
- gpu_llm_local ↔ gpu_comfy 资源组双向互斥（BEGIN IMMEDIATE 内聚合）
- lifespan 顺序：对账→requeue→workers→巡检，无抢跑重复入队
- 音色注入清单实时扫盘，删全局音色无幽灵引用（绑定自动回退 Edge-TTS）
- 漫改 describe_shots 与手动提取并发不重复建资产（资源互斥+同名去重）
- merged 重合成与 autopilot done 自动关交互无异常
- 手动过门2 后 autopilot 续跑不重复门2；门竞态由下一轮 wait 兜住
- 前后端接口面（含今日全部新增）URL/方法/参数逐一核对一致

## 5. 已知缺口与待办

**修复优先级建议**（据 §4）：
1. H1 漫改参考图空目录误判（一行改 has_views + 测试）
2. H2 autopilot 线程同步 TTS 的 300s 失速/全局 interrupt（TTS 挪进 job 队列或 stall 从执行起算+只 interrupt 自身 prompt）
3. H3 storyboard_ready 重拆按钮 409（UI 藏钮或后端放行带覆盖确认）
4. 中危按域批量修：epNNN max+1 与毛坯清理、concat 硬拼补轨、手动重合成自动前置 TTS、导引文案、describe_shots 标签、漫画 tab 无效输入、v-if/v-for
5. 低危择机

**遗留功能缺口**：
- #6 女主配音情绪平淡——根因已定位（色气御姐样本 4.7s 气声基调，克隆继承）；换绑表现力预设即可，待执行
- 快车道 director_mix 未适配音频收口（TTS 长于 span 硬截断）；xfade 开启时 SRT 不扣交叠
- 镜内分段接力（>15s 对白镜自动拆段渲染）未做——当前靠打包预算预防 + 末帧定格兜底
- ComfyUI 端口漂移无探测（8189 事故靠人肉发现，可加设置页多候选地址探测）
- 武侠风云为旧公式拆解的存量项目，剩余观感痛点（镜21 类时长错配已冻结在渲染产物里）——新项目按新链路（动态时长+字数估时+音频收口）不再产生

## 6. 今日修复时间线（2026-09-04 晚~09-05）

| commit | 内容 |
|---|---|
| 69ab58b | autopilot 收尾：暂停联动全停/失败守卫/跨类型在飞感知 |
| 6422719 | 字幕烧录滤镜串 Windows 崩（cwd+裸 ASCII 文件名） |
| 5f5d9f9 | edge-tts 补依赖（venv-win 缺包回退全灭） |
| 6e6da0d | 时长字数基准估时 + 打包预算 + 末帧定格补长 + SRT 音频轴 |
| 1a3cbbe | 文档同步 |
| 672aef5 | 字幕烧录续修：进 subprocess 前路径全绝对化 |
| 87a7f57 | 成片无声/卡死：TTS 替换段统一 44.1k 立体声 |
| 5a98e0a | merged 开放重新合成 |
| 202bd72 | 配音/分镜错位终局：-shortest 截片 ~40s + SRT 轴按真实媒体时长 |
| 9809cce | 音频收口：对白镜段长=配音+0.5s（补长/截尾两向） |
| 9d4dac3 | 段时长 0=LLM 动态估时 + 时长基准进拆解上下文 |
| 1790e1f | 创建默认 段时长=0/总时长=0 |
| dc93019+后续 | 审计文档 + 高危三修（H1 has_views/H2 TTS 进 merge 任务+安全 interrupt/H3 重拆放行） |
| 8240e52 | 文档同步 |
