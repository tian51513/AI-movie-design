# TROUBLESHOOTING · 真机排障手册

> 全部为真机实战判例（2026-09-04/05 Windows 原生运行时暴露）。新事故修完必须回写此处。

## 通用技法

- **读被 Windows 服务锁住的 DB**：拷 `studio.db` + `-wal` + `-shm` 三件套到 /tmp 再开（WSL 直开 `disk I/O error`）
- **WSL 探测本机服务必须 `curl --noproxy '*'`**：WSL 代理环境变量会劫持 loopback 返回假 503
- **Windows exe 可从 WSL interop 直接跑**：`netstat.exe -ano`、`.venv-win/Scripts/pip.exe`、imageio-ffmpeg 的 ffmpeg-win exe（验滤镜/探流参数）
- 真机 ffmpeg 行为验证：`.venv/bin/python -c "from comic_studio.engine.merge import ffmpeg_bin; ..."`（.venv 自带 linux ffmpeg）

## 判例集

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
