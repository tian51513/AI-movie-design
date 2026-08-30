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

# (中文名, 性别, 音色英文短语, 语速后缀)
# 2026-08-30 实测定稿：御姐走 D2 欲感版（用户试听通过，中英 instruct 等效）
_PRESETS: list[tuple[str, str, str, str]] = [
    ("萝莉", "female", "cute little girl voice, high-pitched youthful childish voice",
     "moderate speaking speed"),
    ("高冷御姐", "female",
     "sultry seductive mature woman voice, smoky breathy low female timbre, "
     "intimate alluring whispery texture",
     "slow sensual speaking pace"),
    ("正太", "male", "handsome young boy voice, bright juvenile male voice",
     "moderate speaking speed"),
    ("大叔", "male", "deep middle-aged man voice, rich gruff mature male voice",
     "moderate speaking speed"),
    ("软萌甜妹", "female", "sweet soft girly voice, bubbly sweet young female tone",
     "moderate speaking speed"),
    ("深沉男声", "male", "deep low resonant male voice, solemn baritone voice",
     "moderate speaking speed"),
    ("浪漫女声", "female", "romantic velvety female voice, warm sensual female tone",
     "moderate speaking speed"),
    ("文艺女生", "female", "intellectual soft female voice, gentle literary-style female timbre",
     "moderate speaking speed"),
    ("温柔少女", "female", "soft tender young girl voice, warm gentle adolescent female voice",
     "moderate speaking speed"),
    ("播音男声", "male", "professional broadcast male voice, clear formal radio announcer baritone",
     "moderate speaking speed"),
    ("播音女声", "female", "professional broadcast female voice, clear formal radio host female timbre",
     "moderate speaking speed"),
    ("温柔淑女", "female", "graceful soft lady voice, poised gentle elegant female tone",
     "moderate speaking speed"),
    ("元气少女", "female", "energetic upbeat young girl voice, lively bright adolescent female voice",
     "moderate speaking speed"),
    ("老年男声", "male", "elderly old man voice, hoarse warm senior male timbre",
     "moderate speaking speed"),
    ("老年女声", "female", "elderly old woman voice, soft warm senior female voice",
     "moderate speaking speed"),
]

VOICE_PRESETS = [{"name": n, "gender": g, "timbre": t, "pace": pc} for n, g, t, pc in _PRESETS]

# 禁词（用户文档避坑：ACG 梗词模型识别不稳）——写入/生成时校验
_BANNED = ("loli", "shota", "tsundere")


def voice_instruct(name: str) -> str:
    """音色名 → qwen3-tts VoiceDesign instruct（完整句含固定后缀）。
    未知音色名抛 ValueError。"""
    preset = next((p for p in VOICE_PRESETS if p["name"] == name), None)
    if preset is None:
        raise ValueError(f"未知音色: {name}，可选 {[p['name'] for p in VOICE_PRESETS]}")
    pace = preset.get("pace") or "moderate speaking speed"
    return f"Voice: {preset['timbre']}, {pace}, clear pronunciation, high fidelity audio, no background noise"


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


# ---------- 音色自动匹配（2026-08-30 用户需求）----------
# 两级：LLM 按 15 预设挑（年龄/性别/气质/着装）→ 挑不到按 性别×年龄 落 8 档基线。
# 8 档 → 预设映射（基线档均落在 15 预设内，产物一致走 VoiceDesign）
import re as _re

_AGE_BANDS = [  # (上限含, 女预设, 男预设)
    (12, "萝莉", "正太"),
    (39, "温柔少女", "深沉男声"),
    (59, "温柔淑女", "大叔"),
    (200, "老年女声", "老年男声"),
]


def default_voice_for(gender: str, age) -> str:
    """性别×年龄 → 8 档基线预设音色（女童/男童/青年女/男/中年女/男/老年女/男）。
    性别无法判定返回空（不绑，走 Edge-TTS）。"""
    g = (gender or "").strip()
    if not any(k in g for k in ("女", "female", "F")) and not any(k in g for k in ("男", "male", "M")):
        return ""
    is_female = any(k in g for k in ("女", "female")) or (not any(k in g for k in ("男", "male")) and "f" in g.lower())
    try:
        age_int = int(_re.findall(r"\d+", str(age))[0]) if age else 30
    except (ValueError, IndexError):
        age_int = 30
    for cap, f_voice, m_voice in _AGE_BANDS:
        if age_int <= cap:
            return f_voice if is_female else m_voice
    return ""


def match_voice(appearance: str, suggested: str = "") -> str:
    """角色 → 音色：LLM 建议（限 15 预设，非法忽略）→ 性别×年龄基线兜底。"""
    valid = {p["name"] for p in VOICE_PRESETS}
    if (suggested or "").strip() in valid:
        return suggested.strip()
    m = _re.search(r"性别[:：]\s*([^\s]+)", appearance or "")
    a = _re.search(r"年龄[:：]\s*(\d+)", appearance or "")
    return default_voice_for(m.group(1) if m else "", a.group(1) if a else None)
