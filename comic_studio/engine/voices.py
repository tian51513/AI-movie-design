# comic_studio/engine/voices.py
"""角色音色预设表（2026-08-30 用户提供英文映射文档，Phase 2 角色音色系统）。

双消费方：
1. qwen3-tts VoiceDesign 的 instruct（生成音色样本）
2. 模式 E 提示词的 <Audio N> 职责声明（音色文字锚，配合样本双保险）

措辞规范（用户文档避坑）：禁 ACG 梗词（loli/shota/tsundere 识别不稳），
「形容词 + voice/timbre/tone」3~5 个，固定后缀 clear pronunciation,
high fidelity audio, no background noise 降低杂音与发音模糊。
"""

_SUFFIX = "moderate speaking speed, clear pronunciation, high fidelity audio, no background noise"

# (中文名, 性别, 音色英文短语)
_PRESETS: list[tuple[str, str, str]] = [
    ("萝莉", "female", "cute little girl voice, high-pitched youthful childish voice"),
    ("高冷御姐", "female", "cold mature elegant woman voice, aloof deep female timbre"),
    ("正太", "male", "handsome young boy voice, bright juvenile male voice"),
    ("大叔", "male", "deep middle-aged man voice, rich gruff mature male voice"),
    ("软萌甜妹", "female", "sweet soft girly voice, bubbly sweet young female tone"),
    ("深沉男声", "male", "deep low resonant male voice, solemn baritone voice"),
    ("浪漫女声", "female", "romantic velvety female voice, warm sensual female tone"),
    ("文艺女生", "female", "intellectual soft female voice, gentle literary-style female timbre"),
    ("温柔少女", "female", "soft tender young girl voice, warm gentle adolescent female voice"),
    ("播音男声", "male", "professional broadcast male voice, clear formal radio announcer baritone"),
    ("播音女声", "female", "professional broadcast female voice, clear formal radio host female timbre"),
    ("温柔淑女", "female", "graceful soft lady voice, poised gentle elegant female tone"),
    ("元气少女", "female", "energetic upbeat young girl voice, lively bright adolescent female voice"),
    ("老年男声", "male", "elderly old man voice, hoarse warm senior male timbre"),
    ("老年女声", "female", "elderly old woman voice, soft warm senior female voice"),
]

VOICE_PRESETS = [{"name": n, "gender": g, "timbre": t} for n, g, t in _PRESETS]

# 禁词（用户文档避坑：ACG 梗词模型识别不稳）——写入/生成时校验
_BANNED = ("loli", "shota", "tsundere")


def voice_instruct(name: str) -> str:
    """音色名 → qwen3-tts VoiceDesign instruct（完整句含固定后缀）。
    未知音色名抛 ValueError。"""
    preset = next((p for p in VOICE_PRESETS if p["name"] == name), None)
    if preset is None:
        raise ValueError(f"未知音色: {name}，可选 {[p['name'] for p in VOICE_PRESETS]}")
    return f"Voice: {preset['timbre']}, {_SUFFIX}"


def voice_timbre(name: str) -> str:
    """音色名 → 英文音色短语（提示词 <Audio N> 声明用，不含后缀）。"""
    preset = next((p for p in VOICE_PRESETS if p["name"] == name), None)
    if preset is None:
        raise ValueError(f"未知音色: {name}")
    return preset["timbre"]


def assert_no_banned_words(text: str) -> None:
    """音色描述禁词校验（用户文档：loli/shota/tsundere 输出跑偏）。"""
    low = (text or "").lower()
    for w in _BANNED:
        if w in low:
            raise ValueError(f"音色描述含禁词 {w!r}（ACG 梗词模型识别不稳，请用写实描述）")
