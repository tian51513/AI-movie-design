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
- 对白使用 <d>Chinese</d> 标记中文台词；环境音与音乐按 overall_soundscape / non_diegetic_music 输出。
- 依据绑定的参考图编号 <Picture N> 锚定人物；无图角色仅用文字定义并注明。
- 目标时长与画幅由系统注入镜头上下文，提示词内不重复声明。
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
        "spec": """输出结构化提示词，各节标题独占一行：
subject_definitions: / summary: / retention_analysis: / detailed_description: / overall_soundscape: / non_diegetic_music:
B 模式教训：detailed_description 仍须足够详细（每要素一句以上），分节不等于可以简略。
单镜头描述，无多镜切换。
""" + _COMMON_TAIL,
    },
    "C": {
        "name": "结构化·高密度构图",
        "spec": """在 B 的分节结构上，detailed_description 必须显式包含构图模块：
- 景别与机位（如 中远景平视）、景深与光线
- 每人站位（画面左/中/右三分之一处）、朝向（正面/侧面/四分之三侧面）、画面高度占比
- 两人最小间距约束（同框人物保持三米以上安全距离，动作互不可及）
C 模式教训：站位约束必须配合双参考图才可靠；只写构图不锁身份仍会融合。
""" + _COMMON_TAIL,
    },
    "D": {
        "name": "结构化·多镜电影递进（默认）",
        "spec": """在 C 的全部要求上，detailed_description 使用多镜递进结构：
- [Shot 1] 开场镜：全景/大全景交代环境与人物关系（远景时注明保持人物发型服装轮廓特征）
- [Shot 2] 主动作近景：手持微晃/推近等电影运镜，聚焦核心动作
- [Shot 3] 反应镜：切至另一人物中景，低角度/轮廓光等电影光线语言，含中文台词
- 镜头间使用硬切（或明确写出摇移/推拉转场）；剪辑节奏干净递进
- 每镜至少一个电影语言元素（景别切换/运镜/光线/构图变化）
- 人物一律用 <Subject N> 标记（与 subject_definitions 编号一致），服装写入各 Subject 保持条目
D 版实测模板：大全景缓推 → 近景跟拍 → 中景仰拍轮廓光。
""" + _COMMON_TAIL,
    },
}


def mode_spec(mode: str) -> str:
    if mode not in PROMPT_MODES:
        raise ValueError(f"未知提示词模式: {mode}，可选 {sorted(PROMPT_MODES)}")
    return PROMPT_MODES[mode]["spec"]
