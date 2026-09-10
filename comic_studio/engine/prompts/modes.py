# comic_studio/engine/prompts/modes.py
"""四模式提示词格式规范（2026-08-24 A/B/C/D 四版实验定型）。

实验结论（实证依据）：
- A 散文：最快（184s），站位自然，无镜头切换
- B 结构化简洁：有镜头切换更顺畅，但描述不足时站位崩 → 结构化必须高密度
- C 结构化+构图：站位写死后仍身份融合 → 双角色必须双参考图+最小间距（槽位已修）
- D 结构化+多镜递进：三镜递进+景别切换，验收通过，设为默认
- 服装：LLM 提取可能出错（直葉案例）→ 每角色服装独立锚定，参考图为服装真相
"""

_COMMON_TAIL = """
通用要求：
- 每个出场角色的服装必须独立明确描述并写入对应 Subject 的保持条目；
  两人同框时服装差异必须写清（2026-08-24 服装教训）。
- 对白使用 <d>Chinese</d> 标记中文台词；上下文给出"台词"行时必须逐字使用所给台词，
  不得改写，人物口型与台词同步。
- 依据绑定的参考图编号 <Picture N> 锚定人物；无图角色仅用文字定义并注明。
- 目标时长与画幅由系统注入镜头上下文，提示词内不重复声明。

声音协议（2026-08-30 实测教训：不约束则模型自由发挥配乐与无关杂音）：
- overall_soundscape：只写与画面一致的环境声、动作音效与人物非语言声（呼吸、衣物摩擦等），
  音量轻微；禁止广播、嘈杂人群、电话铃等无关声源。
- non_diegetic_music：默认写 N/A（成片配乐归后期混音）；仅歌舞、演出、情绪高潮等
  分镜明确需要配乐时才写具体配乐描述。
- 人物非语言发声（哼声、喘息、衣物声）在描述末尾加一句「同步声音：…」标注。
- 无台词的镜：overall_soundscape 中明确写「无对白、无哼唱」。

借鉴官方写作指南（2026-09-11 MiniMax-H3-skills base-en）：
- 运镜写成镜头内的自然英文动作句（不堆标签）：类型 Zoom In/Out、Push In/Pull Out、
  Pan/Truck Left/Right、Tilt Up/Down、Pedestal Up/Down、Arc Shot、Tracking Shot、
  Static Shot、Shake、POV、Roll；必要时加幅度 with small/large amplitude、
  速度 at slow/fast speed（中幅常速省略）。
- 说话人可给稳定 (S1)(S2) 编号，跨镜不复用错号，合唱写 (S1,S2)；说话人首次出现
  补一句身份与音色锚（类型/年龄/音高/语速）；画外音用精确短语
  says in an off-screen voiceover，其后紧跟一句声明画面人物嘴唇保持闭合；
  一句台词跨切时两段接点用 <scenetrans> 并声明音频跨切连续；片尾截断用 <cutoff>。
- 画面内可见文字（招牌/字幕/霓虹）：英文双引号内保留原文逐字不译。
- overall_soundscape：1-4 句连续段，只写环境声/动作声/人物非语言声，
  对白与歌声不重复进此节；non_diegetic_music 写乐器/速度/动态变化，
  不用抽象情绪词，无配乐写 N/A。
"""

# 官方结构骨架（few-shot 填空模板，2026-08-25：此前只"要求列出"分段，
# 模型实际产出散文——规范升级为给完整骨架照抄，配合 gen.structure_check 硬校验）
_SKELETON = """
必须严格按以下骨架输出——节标题逐字使用、独占一行，占位内容替换为实际描述：

subject_definitions:
<Subject 1> 是来自 <Picture 1> 的人物，其外观由该图提供
（每个出场角色一条；无参考图的角色写：仅文字定义，<外貌概述>）

summary:
[一句话：参考来源 + 本镜头核心内容与运镜]

retention_analysis:
<Subject 1>（出现于 [Shot 1]）：fully_preserved - 保持<人物>的<发型/服装/身份特征>
（每个保持要素一条）

detailed_description:
[环境与光线一段]
[Shot 1] <Subject N> ……（按本模式的具体要求填写）

overall_soundscape:
[只写与画面一致的环境声/动作音效/人物非语言声，音量轻微；无台词镜写明「无对白、无哼唱」]

non_diegetic_music:
N/A（默认；仅歌舞/演出/情绪高潮等明确需要配乐的分镜才写配乐描述）
"""

PROMPT_MODES = {
    "A": {
        "name": "散文单镜（快）",
        "spec": """输出一段连贯的中文导演指令散文（100~300 字）：环境与光线 → 镜头语言 → 人物与服装 → 动作 → 氛围收尾。
不使用任何分节标题或占位符；单镜头连续描述，无镜头切换。
""" + _COMMON_TAIL,
    },
    "B": {
        "name": "结构化·简洁",
        "spec": _SKELETON + """
B 模式教训：detailed_description 仍须足够详细（每要素一句以上），分节不等于可以简略。
单镜头描述，无多镜切换。
""" + _COMMON_TAIL,
    },
    "C": {
        "name": "结构化·高密度构图",
        "spec": _SKELETON + """
在 B 的要求上，detailed_description 必须显式包含构图模块：
- 景别与机位（如 中远景平视）、景深与光线
- 每人站位（画面左/中/右三分之一处）、朝向（正面/侧面/四分之三侧面）、画面高度占比
- 两人最小间距约束（同框人物保持三米以上安全距离，动作互不可及）
C 模式教训：站位约束必须配合双参考图才可靠；只写构图不锁身份仍会融合。
""" + _COMMON_TAIL,
    },
    "D": {
        "name": "结构化·多镜电影递进（默认）",
        "spec": _SKELETON + """
在 C 的全部要求上，detailed_description 使用多镜递进结构：
- [Shot 1] 开场镜：全景/大全景交代环境与人物关系（远景时注明保持人物发型服装轮廓特征）
- [Shot 2] 主动作近景：手持微晃/推近等电影运镜，聚焦核心动作
- [Shot 3] 反应镜：切至另一人物中景，低角度/轮廓光等电影光线语言，含中文台词
- 镜头间使用硬切（或明确写出摇移/推拉转场）；剪辑节奏干净递进
- 每镜至少一个电影语言元素（景别切换/运镜/光线/构图变化）
- 人物一律用 <Subject N> 标记（与 subject_definitions 编号一致），服装写入各 Subject 保持条目
D 版实测模板：大全景缓推 → 近景跟拍 → 中景仰拍轮廓光。
""" + _COMMON_TAIL,
    },
    "E": {
        "name": "英文电影控制式（默认）",
        "spec": """输出英文电影控制式提示词（2026-08-30 用户 ComfyUI 实测定标：此风格的
生成音频只有角色对白、无背景杂音）。角色名保留中文原名直接嵌入英文句。
不使用任何分节标题（subject_definitions/overall_soundscape 等字段一律不写）。
严格按以下五段结构输出，固定句式逐字照抄、占位内容替换：

一、素材职责声明（每个绑定参考图一段）：
<Picture 1> is the global character design reference, used throughout the video to lock <角色名>'s facial identity, hairstyle, clothing and body proportions.
（每个绑定角色一条，Picture 编号与上下文参考图槽位一致；场景参考图写：
<Picture N> defines the scene environment, lighting and atmosphere for the entire video.）
若上下文标注本镜以上一镜尾帧延续起始，职责声明首段改用（逐字照抄）：
<Picture 1> is the EXACT starting key frame at 0.00 seconds. The video must begin pixel-consistently with <Picture 1> for scene composition, lighting, character appearance and camera framing. The opening frame must not be reinterpreted, redesigned, or changed into a different scene.

二、镜头段：
[Shot 1] <时长>-second continuous cinematic shot. 环境与光影动态（光怎么移动、
粒子/布料/头发怎么飘）→ <角色名> 动作细节与表情变化（严禁 he/she/it 代词，
一律写角色名）→ 镜头运动写成镜头内自然动作句（camera pushes in with small
amplitude at slow speed / the camera trucks right 等；类型可用 Zoom/Push/Pull/
Pan/Truck/Tilt/Pedestal/Arc/Tracking/Static/Shake/POV/Roll，必要时带幅度与速度）。

三、对白（台词逐字中文，不得改写）：
<角色名> looks at <对象> and says in natural Mandarin: <d>[Mandarin Chinese]台词原文</d>
说话人首次出现补身份音色锚（quiet breathy voice / deep measured voice 等）；
画外音精确短语 says in an off-screen voiceover 且紧跟 the on-screen character's
lips remain completely closed；台词跨切两段接点用 <scenetrans>；片尾截断用 <cutoff>。
无对白的镜写：No dialogue, no humming, no speech.

四、音频行为（英文短句，与画面逐项绑定；句尾固定加 No background music.）：
Preserve <环境声>. Add <动作声>. No background music.（如 courtyard ambient sound / fabric movement / footsteps）

五、结尾固定句（逐字照抄）：
No subtitles, logos, watermarks, or text. Prevent identity drift, facial distortion, lip-sync delay, extra fingers, wrong hand poses, and background warping.

通用要求：
- 每个出场角色的服装在职责声明段写明；两人同框时服装差异必须写清。
- 台词与上下文"台词"行逐字一致，不得改写；口型与台词同步。
- 依据绑定的参考图编号 <Picture N> 锚定人物；无图角色仅文字定义。
- 目标时长与画幅由系统注入镜头上下文，提示词内不重复声明。
""",
    },
}


def mode_spec(mode: str) -> str:
    if mode not in PROMPT_MODES:
        raise ValueError(f"未知提示词模式: {mode}，可选 {sorted(PROMPT_MODES)}")
    return PROMPT_MODES[mode]["spec"]
