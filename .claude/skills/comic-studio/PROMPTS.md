# PROMPTS · 提示词模式模板约束

> **权威源码**：`engine/prompts/modes.py`（PROMPT_MODES）+ `engine/prompts/gen.py`（heal/校验/注入）
> **同步状态**：✅ 与源码一致（2026-09-05 核对）｜**变更流程**：改源码 → 同步本文档 → CLAUDE.md 模块地图
> 新项目默认 E；存量 D 不动可切。§9 为**未实现提案区**——与正文机制严格隔离，采纳前先改源码。

## 1. 模式选择表（2026-08-24 实测定型，08-30 补 E）

| 模式 | 名称 | 结构 | 实测结论 |
|---|---|---|---|
| A | 散文单镜 | 中文散文 100~300 字，无分节 | 最快（184s/镜）、站位自然；无镜头切换 |
| B | 结构化·简洁 | 官方骨架分节 | 分节≠可简略——描述不足必崩站位 |
| C | 高密度构图 | B+构图模块 | 站位写死仍会身份融合，须配双参考图+最小间距 |
| D | 多镜电影递进 | C+[Shot 1~3] 递进 | 旧默认，验收通过：大全景缓推→近景跟拍→中景仰拍 |
| **E** | **英文电影控制式** | **五段固定句式** | **当前默认**。生成音频纯净（仅对白无杂音）、口型同步更稳 |

后端选型与模式**无关**：workflow_type（ref2va/fl2v/t2v/i2v）由拆解 staging 按 continuity 决定。

## 2. 全模式通用契约（红线）

- **对白**：`<d>[Mandarin Chinese]台词原文</d>`，与上下文台词行逐字一致禁止改写，口型同步；无对白镜明示「无对白、无哼唱」（E：`No dialogue, no humming, no speech.`）
- **人物锚定**：`<Picture N>` 严格对应绑定参考图槽位；无图角色仅文字定义并注明
- **服装锚定**：每角色独立写入保持条目；两人同框必须显式写服装差异（防融合关键）
- **禁代词**：全文禁 he/she/他/她，一律角色名（多角色绑定生命线）
- **元数据隔离**：时长/画幅由系统注入镜头上下文，提示词内不重复声明

## 3. 声音协议

| 模式 | 载体 | 约束 |
|---|---|---|
| A~D | `overall_soundscape` | 只写与画面一致的环境声/动作音效/人物非语言声（音量轻微）；禁广播、嘈杂人群、电话铃等无关声源 |
| A~D | `non_diegetic_music` | 默认 **N/A**（配乐归后期）；仅歌舞/演出/情绪高潮分镜可写 |
| E | `Preserve/Add` 句 | `Preserve <环境声>. Add <动作声>.`（如 courtyard ambient / fabric movement / footsteps） |
| 通用 | 同步声音 | 人物非语言声（哼/喘/衣物声）在描述末尾加「同步声音：…」 |

## 4. A~D 官方骨架（节标题逐字照抄，不可增删改序）

```
subject_definitions:   <Subject 1> 是来自 <Picture 1> 的人物，其外观由该图提供
summary:               [一句话：参考来源 + 核心内容与运镜]
retention_analysis:    <Subject 1>（出现于 [Shot 1]）：fully_preserved - 保持<特征>
detailed_description:  [环境光线] + [Shot N] 按模式要求展开
overall_soundscape:    [……]
non_diegetic_music:    N/A
```
- **C 追加**：构图模块（景别机位/景深光线/每人站位朝向与画面占比/两人最小间距≥3米）
- **D 追加**：三镜递进——开场全景交代→主动作近景（推/跟）→反应中景（低角度/轮廓光），镜间硬切，每镜至少一个电影语言元素

## 5. E 五段结构（固定句式逐字照抄；不写任何 A-D 字段）

1. **素材职责声明**（每绑定图一段）：角色图 `<Picture 1> is the global character design reference, used throughout the video to lock <角色名>'s facial identity, hairstyle, clothing and body proportions.`；场景图 `<Picture N> defines the scene environment, lighting and atmosphere for the entire video.`
   **尾帧续镜时**首段整体替换为：`<Picture 1> is the EXACT starting key frame at 0.00 seconds. The video must begin pixel-consistently with <Picture 1> for scene composition, lighting, character appearance and camera framing. The opening frame must not be reinterpreted, redesigned, or changed into a different scene.`
2. **镜头段**：`[Shot 1] <时长>-second continuous cinematic shot.` 环境光影动态（光怎么动/粒子布料头发怎么飘）→ 角色动作表情（禁代词）→ 具体运镜幅度（camera pushes in slowly 等）
3. **对白段**：`<角色名> looks at <对象> and says in natural Mandarin: <d>[Mandarin Chinese]台词</d>`
4. **音频行为**：`Preserve <环境声>. Add <动作声>.`
5. **结尾固定句**：`No subtitles, logos, watermarks, or text. Prevent identity drift, facial distortion, lip-sync delay, extra fingers, wrong hand poses, and background warping.`

## 6. 机械配套（engine/prompts/gen.py，实际存在）

- **heal 自愈（不耗重试）**：⑥结尾后缀（缺「无字幕/No subtitles」→ 补中文句「无字幕，无背景音乐」）｜⑦音频协议兜底（缺 soundscape/music 机械补，已有配乐不覆盖）｜⑧围栏卫生（代码围栏：协议正文拆栏保文、工具代码整删）＋Meta 词剥离
- **结构失败** → `_compact_system` 短骨架紧凑重试（上下文重开，不背失败输出）
- **FL2V 机械注入**：首尾帧对齐头（Picture1→0.00s / Picture2→镜时长）+ KF_NO_CUT 镜内禁切（英文句）——节点参数侧注入，正文不写
- **系统词净化**：注入前 `_strip_code_blocks`（防校验节被 9B 抄入）
- **漫画读图产物**：动态漫=integrated_multimodal_description（fl2v 首尾帧格式）、漫改=subject_definitions 骨架；落库前统一过 heal（与项目 prompt_mode 无关，漫画链路自成格式）

## 7. 故障排查（症状 → 真实根因 → 处置）

| 症状 | 根因 | 处置 |
|---|---|---|
| 变脸/身份漂移/人物融合 | retention 不足、参考图缺失或一图多职 | 切 B/C/E；补 fully_preserved 锁定项；每角色独立参考图（C 需≥2 张） |
| 随机杂音/人群交谈声 | A-D soundscape 写了嘈杂声源（遵循是概率性的） | 收紧 soundscape 白名单；台词向切 E；合成端开 mute_quiet_shots 封残留 |
| 画面硬切/跳变 | D 多镜递进是设计内行为；fl2v 链路缺禁切 | fl2v 确认 KF_NO_CUT+对齐头已注入；不要 D 的场景换 E |
| 口型错位 | 单镜多说话人交替 | 拆镜到单说话人；优先 E（口型同步更稳）；ref2va+音色注入走原生口型 |
| 提示词混入代码/字段 | 小模型抄系统词或混结构 | heal⑧/_strip_code_blocks 自动处理；复发查模型与 mode 匹配 |

## 8. 禁止行为总表（校验/规范层真实生效）

1. 禁改写台词与 `<d>` 标签内部文本（机械红线）
2. 禁代词（他/她/对方/此人）——多角色全程写角色名
3. A-D 的 non_diegetic_music 禁非 N/A（歌舞/情绪高潮分镜除外）
4. E 禁出现 A-D 字段结构（骨架校验按 mode 放行）
5. 提示词正文不写时长/画幅声明、不写 FL2V 首帧锁定语句（E 续镜首段声明除外——由系统上下文触发）

## 9. 提案区（未实现——采纳前必须先改 modes.py/gen.py 并同步本文档）

- **JSON 调用契约 + 错误码体系**（ERR_MODE_INVALID/ERR_REF_GAP/…、warning_list、后端路由返回）——当前 gen.py 是引擎内函数调用，无此接口
- E 音频句尾追加 `No background music.`（与结尾负面清单呼应，更早锚定）
- E 素材声明「角色参考图兼任首帧」合并句式（`both the global reference AND the EXACT starting key frame…`）
- A-D 追加中文防崩坏尾句（五官变形/手指畸形清单——目前仅 E 有英文版）
- heal 扩步：E 混入 A-D 字段整段删除、多运镜检测拦截（ERR_MULTI_CAMERA 类）
- D 废弃多镜写法改单镜+接力（与现行 D 设计相反，需产品决策）
- turbo 精简分支、参考视频/音频（VideoN/AudioN）素材语法、Token 超限自动通知拆镜
