# TROUBLESHOOTING · 真机排障手册

> 全部为真机实战判例（2026-09-04/05 Windows 原生运行时暴露）。新事故修完必须回写此处。

## 通用技法

- **读被 Windows 服务锁住的 DB**：拷 `studio.db` + `-wal` + `-shm` 三件套到 /tmp 再开（WSL 直开 `disk I/O error`）
- **WSL 探测本机服务必须 `curl --noproxy '*'`**：WSL 代理环境变量会劫持 loopback 返回假 503
- **Windows exe 可从 WSL interop 直接跑**：`netstat.exe -ano`、`.venv-win/Scripts/pip.exe`、imageio-ffmpeg 的 ffmpeg-win exe（验滤镜/探流参数）
- 真机 ffmpeg 行为验证：`.venv/bin/python -c "from comic_studio.engine.merge import ffmpeg_bin; ..."`（.venv 自带 linux ffmpeg）

## 判例集

### gen_shot 全灭 TypeError: emit() got an unexpected keyword argument 'shot_id'（2026-09-06）
- 漫改项目**未提取角色直接渲染**即触发：`rendershot` 原页兜底分支 `emit_log(..., shot_id=...)` 传了 `logbus.emit` 不存在的 kwarg（合法仅 project_id/job_id/data）——12e1d46 引入，该分支零测试
- 修 kwarg 后还有第二坑：通用 else 分支把漫改原页 images 覆盖成空 → I1 空图快失败（已收窄 `elif images is None`，优先级 ①角色资产 ②上镜接力 ③漫画原页）
- 教训：**emit 只认 project_id/job_id/data**；新分支必须有测试真实踩过一次（当次提交只测了有资产路径）
- start-prod 无热重载——修完必须重启服务，失败镜手动重发渲染（autopilot 失败守卫见新 job 自然解除）

### 成片无声 / 播放卡死 / 配音错位（三连根因，均已修）
1. **滤镜串路径**：libavfilter 里 `\` 是转义符 + 非 ASCII 项目名滤镜内打开不可靠 → 滤镜只给裸 `subtitles.srt`、srt 目录用 `cwd=`；**进 subprocess 前所有路径 resolve() 绝对化**（相对路径会跟着新 cwd 解析凭空消失）
2. **音频参数混拼**：TTS mp3=24k 单声道 vs normalize 段=44.1k 立体声，concat `-c copy` 缝一个容器 → 时间戳全废。一切出段统一 `-ar 44100 -ac 2 -b:a 128k`
3. **`-shortest` 截片**：裸用会把「配音短于视频」的镜整段截到配音长（曾截掉 40s）。收口一律 `-t {target}` 显式截断；**copy 视频+apad+shortest 组合会熄火**（48 字节挂死），勿用

### ComfyUI 连不上 / 枚举失败
- WinError 10061 = 没人监听。先 `netstat.exe -ano | grep 8188`：无监听=ComfyUI 没起；**端口漂移**（启动瞬间 8188 被占会顺延 8189）→ 改 studio 设置的 comfy.base_url 或 ComfyUI 启动参数固定 `--port`
- WSL 侧探测见「通用技法」第二条（假 503 干扰）

### .venv-win 缺包（No module named 'edge_tts'）
- pyproject 声明过但 Windows 侧没重装。WSL interop 直装：`.venv-win/Scripts/pip.exe install -e ".[dev]"` 或单包装。函数级 import 的包装完即被运行中服务吃到

### TTS 克隆 300s 被打断 / 误杀渲染
- 旧机制：失速从提交起算（排队白等也算）+ `/interrupt` 无 id 杀全局当前任务。已修（排队不计失速、只处置自己）。若复发：查 ComfyUI 队列堆积（前端渲染排队 vs TTS 抢跑）

### merge 失败循环
- autopilot 失败守卫已拦（最新 merge job failed → wait 等手动）。日志见「autopilot 卡住：上次合成失败」→ 排查后点「🎬 重新合成」解除
- 失败残留 ep 毛坯：concat 直接写终名，半截文件会留下且列进 merges 列表——人工清理 output/（注意 ep 编号 len+1 的覆盖坑，删旧片从小编号删起）

### 一键出片卡住不动
- 查日志「autopilot 卡住：…失败」= 失败守卫等手动重发（对应 UI 按钮解除）
- 卡 wait 无日志：全无效镜死循环（已知中危）或阶段不在流内（漫画项目被手动 analyze 过）

### 字幕不出现/错位
- 轴规则：对白镜=配音+0.5、其余=真实视频时长。xfade 开启时轴不扣交叠（已知中危，累计漂移 ~0.3s/镜）——追求对齐保持 xfade 关

### 音色平淡
- 克隆继承样本演绎基调。御姐系（气声/慢板）天然淡；表现力需求选浪漫女声/元气少女，或声线描述加「语气生动有力、情绪饱满」重生

### Windows bat 脚本
- 纯 ASCII + CRLF（UTF-8 中文注释被 cmd 按 GBK 误读执行）；杀进程只按端口 8190 精确杀（`taskkill /im python.exe` 会连带杀 ComfyUI）；for /f 内 PowerShell 管道不要写 `^|`

### 本地 LLM job 长跑 15 分钟不归（Ollama 5 分钟请求硬上限）
- **症状**（2026-09-06 有声2 校对 free 整文）：27B qwen3.8 跑 1506 字扩写，job running 15+ 分钟；同模型同文本手动测 1~2 分钟
- **真相**：Ollama（0.32.13）服务端对单请求有 **~5 分钟总时长硬上限，流式/非流式通杀**（实验矩阵：非流式 >300s 三次死于 `500|5m0s`/504；流式 >300s 在 294s 被服务器断连 RemoteProtocolError；<300s 流式 276s 成功）；openai 客户端对 5xx **静默重试 2 次** → 单 job 最长烧 15 分钟三连死
- **判定手法**：`/mnt/c/Users/<u>/AppData/Local/Ollama/server.log` grep `GIN.*chat`；`/api/ps` 的 `expires_at` 是否持续滑动（活跃生成）；GPU util >0
- **无效药**（勿再试）：`reasoning_effort:none` 对 qwen3.8（qwen35 家族）不生效——思考照跑 4500+ token；改流式聚合也躲不过总时长墙
- **边界**：docs 无官方 env 可调请求超时（GitHub issue #7526 关闭无解；`OLLAMA_LOAD_TIMEOUT` 管加载不管生成）。**规避=单次调用生成量控制在 ~4200 token 内（14.5 t/s × 290s）**：提示词控时长（free 校对的「整体校正、禁止逐句」实测把 6 分钟压回 2 分钟级）、重活换轻模型/线上、或任务拆批
- **附带**：改前端后必须硬刷新浏览器（SPA 不自刷新）——服务已新、页面仍旧会弹旧版错信息（本例「保留 undefined 段」）

## 拆分镜单块截断整单重烧（2026-09-17 job 43228 111 块）

**症状**：111 块拆解跑到第 47 块 `LLMError: 输出被长度上限截断（finish_reason=length）` 整 job 失败 → worker 自动重试**从块 1 重跑**；第二次跑到 ~25 块又炸（两次停在不同块号）；数小时 LLM 工作反复全丢。伴随现象：⏹ 停止自动/清空队列后拆解**仍在继续**（只取消排队、running 不 interrupt——温和停止设计）；期间点的资产主图重生**没动静**（默认 workers=1 单 worker，堵在拆解 job 后面，不是丢了）。

**根因**：①`split_storyboards` 单 job 进程内循环全部块，staged 只存内存，最后一块完成才落库——中途抛错前功尽弃；②对白密集块输出密度 >9 tok/字撞 16384 窗（4.2k 输入+正常 6~7k 输出能过，个别块输出冲 >12k 必炸），哪块炸随机；③`raw_chat` 对 length 立即抛错（截断 JSON 重试必败，设计如此）。

**修复**（2026-09-17）：①**块级断点缓存** `projects/<slug>/split_cache.json`——每块通过即落处理后的 staging 行，自动重试/手动重拆/重启重排只续跑失败块起，成功落库删缓存，指纹（正文+参数+提示词版本+资产名册 hash）不符自动作废；②**单块截断对半降级** `_ask_block`——kind=length 时按段落边界对半切（配额按字数分摊）重拆，下限 300 字，仍炸才上抛（此时缓存兜住）。

**排查要点**：看到「分块 N/111 拆解中」后接「开始分镜拆解：111 块」从头计数 = 又一次全量重烧（旧代码特征）；新代码续跑会先打「断点续跑：缓存命中 N/111 块」。拆解 job 想立即停只能重启服务（重启重排白名单含 split_storyboards，attempts<3 会自动续跑——带缓存后无损）。

## 超长篇分析合并爆上下文（2026-09-13 玉麟传奇 62 块）

**症状**：52 万字小说分析——62 块 extract 全过（~35 分钟）→「合并第 1 轮」后某调用跑 13 分钟 → `LLMError: 输出被长度上限截断（finish_reason=length）` 整轮失败；attempts 自动重试又从 chunk 1 重跑（看似「重复解析」）。

**根因**：合并树结构性爆批——MERGE_SYSTEM 旧版要求「不丢项」，round-1 每份产物贴满 7960 字符预算（≈6.5k tok）→ round-2 贪心装不下两份全部走「强制两两」→ 单批 16k 字符 ≈13k tok，16k 上下文下思考+输出必撞 length。**该规模下必然发生，不是偶发。**

**修复**（2026-09-13）：①合并即精选措辞（条目多时保主要角色、丢路人）②中间轮产物机械量控到半预算（`_cap_analysis` 按 主角>配角>路人 排序累加截断——下层贪心永远装得下两份）③length 截断防御链：常规版→极限压缩版→机械取首份（树必然收敛不 fatal）。

**排查要点**：`llm_calls` 表只记 extract 成功调用（merge 不落库）；jobs.attempts 看自动重试次数；Windows 服务持锁时拷 `studio.db`+`-wal`+`-shm` 三件套到 /tmp 再读。

## 漫画链真机判例集（2026-09-13 验收日）

### 上传 500（`/upload/image` Server error）
**先查上传名有没有 `/`**：链式日志标识「-链1/2」的斜杠进了文件名 → ComfyUI 当子目录 → 服务端 `open(不存在目录/xxx.png)` FileNotFoundError→500。tag 已改「-链1/-链2」+filler `_safe` 清洗三段分隔符双保险。附带防线：`_upload_with_retry`（5xx 退避 5s/15s 重试、4xx 立抛、**穷尽后异常带服务端正文**——日志直接可诊断）。排查法：curl 同名连传/中文名/10MB 复现全过 → 一定是请求内容差异，抓服务端 traceback。

### stale 联动把漫画页"藏没了"
编辑角色外貌 → `mark_stale_for_asset`（视频链语义）把绑定镜标 stale → 漫画页 UI 藏图只剩未绑定页（29 页只见 2 页，用户以为数据丢）。**页面文件一直在磁盘**。修：comic_output 豁免 stale 标记改提示日志（「绑定 N 页建议重出」）；前端 stale 态显示图+「资产已更新·建议重出」标签。

### 手动重出后卡「分镜就绪」看不到转视频按钮
终态推进 `comic_ready` 原本只在 autopilot tick 做——手动批量重出（autopilot 关）全页齐但 stage 不动。修：gen_comic_page 最后一页落盘查全生效镜缺页、零缺自动 set_stage。**存量卡住**：点一次 🚀 一键出片即推。

### 画风换了主图/页面不变
两处独立坑：①画风面板换 Krea2 风格 style_vis 仅空时才填 → 图像端永远吃旧风格（改 confirmStylePicker 双字段跟随）；②**设置冻结**（老判例重演）：settings 表存了旧默认值覆盖代码新默认——PUT 局部覆盖三键。

### 体型改了主图还偏胖
`condense_appearance` 剥「体型：」标签拼裸值（「…白皙，前凸后翘」夹肤色后=弱约束噪声）——中文流标签即语义锚。改保留 `标签：值`；✨优化器同步加模糊词强化规则（高挑→+纤细/四肢修长/体脂低）。

### 气泡盖脸（定位选了"最空"的脸）
动漫脸=大面积平滑肤色块，纯边缘能量比细致背景还低 → 被误判最空。能量 v2：+60×肤色占比（r>g>b）×分区位置权重（顶角 0.85/中带 1.35）。还不满意 → ✥ 手动拖位（ledger.bubble_pos 即时生效）。

### megapixels 槽被塞进文件路径
`char_refs` 循环里局部 `mp`（main.png 路径）遮蔽顶部 `mp`（百万像素）→ `params["megapixels"]=PosixPath` → json 序列化炸。**局部变量别与外层语义名复用**。

### Krea2 快道速度参考
百万像素=耗时闸：1.0MP 双参考 90s/单参考 50s → 0.4MP 30s/20s。**步数非瓶颈**（4步=10步=30s，固定开销在 Qwen3-VL 接地/编码）；turbo_4step LoRA 零收益已关。prompt 长度影响 Edit-2511 编码耗时（~0.22s/字）。

### 双角色参考上限
identity_edit LoRA 双图训练上限（scene恒图1/person恒图2，交换劣化）：总数>2 才 stitch 拼接（默认）/chain 链式（comic_dual_mode 可切）；≤2 直种。拼接有官方确认的脸趋同缺陷——用户实测链式两段输出几乎一样（描述已含两人）后按用户指令保留双方案可切、默认单次。

## 2026-09-18 · 提交 400 value_not_in_list（block_size int 化事故）

**症状**：gen_shot 提交 ComfyUI `/prompt` 400（重试 3 次全灭）；日志只有 HTTPStatusError 无详情。
**根因**：模板换链时把 `H3SLAAttention.block_size` 从 `"64"` 归一成 int 64——当前 ComfyUI 该输入是 **COMBO 字符串枚举 ['64','128']**，`value_not_in_list`。
**排障手法**：
1. 审计快照拿提交的完整 prompt：`GET /api/jobs/{id}/snapshot`（P7-A 落库，含 workflow）——服务占 db 时 WSL 侧 sqlite 直读会 disk I/O error（WAL 跨 OS），走 API。
2. **重放拿 400 响应体**：把快照 workflow POST 回 `/prompt`（无效 prompt 不会入队，安全）——响应 `node_errors` 直接点名节点+字段+枚举清单。
3. 离线校验器判例：object_info **新式组合** `["COMBO", {"options":[…]}]` 枚举在 `fdef[1].options`；旧式 `fdef[0]` 直接是列表。只按旧格式校验会漏检新式 COMBO 的 value_not_in_list。
**防线**：`h3_sla_params` 统一 `str()` 归一（settings 存 int 也兼容）；测试钉住模板 block_size ∈ {"64","128"}。
