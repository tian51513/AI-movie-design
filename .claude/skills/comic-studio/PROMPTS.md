# PROMPTS · 提示词模式模板约束

> 权威源码：`engine/prompts/modes.py`（PROMPT_MODES 字典）——本文件是操作视角的契约摘要，改模式先改源码再同步这里。项目字段 `prompt_mode` 选模式；**新项目默认 E**，存量 D 不动可切。

## 模式选择表

| 模式 | 名称 | 结构 | 适用/教训（2026-08-24 实测定型） |
|---|---|---|---|
| A | 散文单镜 | 中文散文 100~300 字，无分节 | 最快（184s/镜）、站位自然；无镜头切换 |
| B | 结构化·简洁 | 官方骨架分节 | 分节≠可简略——描述不足站位崩 |
| C | 高密度构图 | B+构图模块 | 站位写死仍会身份融合，须配双参考图+最小间距 |
| D | 多镜电影递进 | C+[Shot 1~3] 递进 | 大全景缓推→近景跟拍→中景仰拍；验收通过（旧默认） |
| E | 英文电影控制式 | 五段固定句式 | **默认**。2026-08-30 实测：此风格生成音频只有角色对白、无背景杂音 |

## 全模式通用契约

- **对白**：`<d>[Mandarin Chinese]台词原文</d>`，与上下文台词行逐字一致不得改写，口型同步；无对白镜明示「无对白、无哼唱」（E：`No dialogue, no humming, no speech.`）
- **人物锚定**：`<Picture N>` 编号对应绑定参考图槽位；无图角色仅文字定义并注明
- **服装锚定**：每角色服装独立写入保持条目；两人同框服装差异必须写清（直葉教训）
- **禁代词**：一律写角色名（多角色绑定关键）
- 时长/画幅由系统注入镜头上下文，提示词内不重复声明

## 声音协议（A~D 字段式；E 用 Preserve/Add 句替代）

- `overall_soundscape`：只写与画面一致的环境声/动作音效/人物非语言声（音量轻微）；禁广播、嘈杂人群等无关声源
- `non_diegetic_music`：默认 **N/A**（配乐归后期）；仅歌舞/演出/情绪高潮分镜才写
- 人物非语言声在描述末尾加「同步声音：…」

## A~D 官方骨架（节标题逐字照抄）

```
subject_definitions:   <Subject 1> 是来自 <Picture 1> 的人物，其外观由该图提供
summary:               [一句话：参考来源+核心内容与运镜]
retention_analysis:    <Subject 1>（出现于 [Shot 1]）：fully_preserved - 保持<特征>
detailed_description:  [环境光线] + [Shot N] 按模式要求
overall_soundscape:    [……]
non_diegetic_music:    N/A
```
C 追加构图模块（景别机位/每人站位与朝向/画面占比/两人最小间距三米）；D 追加三镜递进（开场全景→主动作近景→反应镜，每镜至少一个电影语言元素）。

## E 模式五段结构（固定句式逐字照抄）

1. **素材职责声明**：`<Picture 1> is the global character design reference, used throughout…`（每角色一条；场景图写 defines the scene environment…）。上镜尾帧延续时首段换 `is the EXACT starting key frame at 0.00 seconds. The video must begin pixel-consistently…`
2. **镜头段**：`[Shot 1] <时长>-second continuous cinematic shot.` 环境光影动态 → 角色动作表情（禁代词）→ 具体运镜幅度
3. **对白**：`<角色名> looks at <对象> and says in natural Mandarin: <d>[Mandarin Chinese]台词</d>`
4. **音频行为**：`Preserve <环境声>. Add <动作声>.`
5. **结尾固定句**：`No subtitles, logos, watermarks, or text. Prevent identity drift, facial distortion, lip-sync delay, extra fingers, wrong hand poses, and background warping.`

E 不写任何分节字段（subject_definitions/soundscape 一律不写）；heal⑦ 按 mode 跳过音频协议补齐、`No subtitles` 满足尾缀检查。

## 机械配套（engine/prompts/gen.py）

- `heal_h3_prompt` 自愈不耗重试：⑦缺节补齐（music 缺省 N/A）、⑧围栏卫生（协议正文保文/工具代码整删）、Meta 词剥离、结尾后缀
- 结构失败 → `_compact_system` 短骨架紧凑重试（上下文重开不背失败输出）
- fl2v 机械注入：首尾帧对齐头（`Picture1→0.00s / Picture2→镜时长`）+ KF_NO_CUT 镜内禁切（英文句）
- 漫画读图产物同锚 H3 格式：动态漫=integrated_multimodal_description（fl2v 首尾帧格式）、漫改=subject_definitions 骨架；落库前统一过 heal
- 系统词注入前 `_strip_code_blocks`（SKILL.md 校验节曾让 9B 抄进提示词）
