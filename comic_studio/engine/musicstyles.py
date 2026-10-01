# comic_studio/engine/musicstyles.py
"""音乐风格库（2026-10-01 官方 Music3 skill vendor）：templates/music_styles/
的 genre-router 18 族索引 → 卡表清单 → 卡全文（完整结构化 caption）。
音乐库风格选择器的数据面（仿 stylepresets）；来源与许可见
templates/music_styles/NOTICE.md。"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "templates" / "music_styles"

_INDEX_RE = re.compile(r"^index-(?P<id>[a-z0-9-]+)\.md$")
_HEAD_RE = re.compile(r"^#\s+(?P<name>.+?)\s*$", re.M)
_COUNT_RE = re.compile(r"(?P<n>\d+)\s+compact style cards", re.I)
# 索引表行：| `id` | style | routes | tempo/key | mood | vocal | palette | `templates/x.txt` |
_ROW_RE = re.compile(
    r"^\|\s*`(?P<id>[^`]+)`\s*\|\s*(?P<style>[^|]+)\|\s*(?P<routes>[^|]*)\|"
    r"\s*(?P<tempo>[^|]+)\|\s*(?P<mood>[^|]+)\|\s*(?P<vocal>[^|]+)\|"
    r"\s*(?P<palette>[^|]+)\|\s*`templates/(?P<file>[^`]+)`\s*\|$")



# ── 中文化（2026-10-01 用户需求）────────────────────────────────────────────
# 18 族名手写静态表；卡风格名 = 短语词典优先 + 词表机械合成（覆盖官方 1000 卡
# 全部独特词；查不到的词保留英文——宁缺毋滥不硬翻）。

_FAMILY_ZH = {
    "cinematic-orchestral-epic": "电影感管弦史诗",
    "cinematic-pop-ballad": "电影感流行抒情",
    "club-edm-house-trance": "俱乐部 EDM 浩室出神",
    "contemporary-folk-acoustic": "当代民谣原声",
    "country-americana": "乡村美式",
    "dance-pop-disco-funk": "舞曲流行迪斯科放克",
    "east-asian-ballad-heritage": "东亚抒情传统",
    "east-asian-modern": "东亚现代",
    "electronic-synth-ambient-pop": "电子合成器氛围流行",
    "general-pop-ballad": "通用流行抒情",
    "hip-hop-rap": "嘻哈说唱",
    "jazz-swing-big-band": "爵士摇摆大乐队",
    "metal-heavy-rock": "金属重型摇滚",
    "modern-rnb-neo-soul": "现代节奏布鲁斯新灵魂",
    "pop-alternative-rock": "流行另类摇滚",
    "roots-traditional-global": "根源传统世界",
    "soul-blues-gospel": "灵魂布鲁斯福音",
    "traditional-vocal-stage": "传统人声舞台",
}

# 多词短语先于逐词合成（避免 Hip+Hop→「嘻哈哈」这类碎词拼接）
_PHRASES = [
    (r"\bHip[- ]Hop\b", "嘻哈"), (r"\bA Cappella\b", "阿卡贝拉"),
    (r"\bNew Age\b", "新世纪"), (r"\bEasy Listening\b", "轻音乐"),
    (r"\bSpoken Word\b", "口白"), (r"\bBig Band\b", "大乐队"),
    (r"\bMusical Theatre\b", "音乐剧"), (r"\bBossa Nova\b", "波萨诺瓦"),
    (r"\bSea Shanty\b", "船歌"), (r"\bShow Tune\b", "演出曲"),
    (r"\bRock and Roll\b", "摇滚乐"), (r"\bDrum and Bass\b", "鼓打贝斯"),
    (r"\bBoom Bap\b", "鼓点说唱"), (r"\bRockabilly\b", "山地摇滚"),
]

_TERM_ZH = {
    "&": "与", "（CCM）": "（当代基督教）", "（Chinese": "（华语",
    "（EDM": "（EDM", "（Electronic": "（电子", "（Progressive": "（前卫",
    "（Traditional": "（传统", "House）": "浩室）", "Music）": "音乐）",
    "Style）": "风格）", "influences）": "影响）",
    "80s": "80年代", "A": "", "Abstract": "抽象", "Acoustic": "原声",
    "Adult": "成人", "Age": "", "Alternative": "另类", "Ambient": "氛围",
    "Americana": "美式乡村", "Anime": "动漫", "Anisong": "动漫歌",
    "Anthem": "颂歌式", "Anthemic": "颂歌感", "Arena": "体育场式",
    "Asian": "亚洲", "Atmospheric": "氛围化", "Ballad": "抒情曲",
    "Band": "乐队", "Bap": "说唱节拍", "Bass": "贝斯", "Big": "大",
    "Bluegrass": "蓝草", "Bluegrass-Pop": "蓝草流行", "Blues": "布鲁斯",
    "Blues-Rock": "布鲁斯摇滚", "Boogie-Woogie": "布吉乌吉", "Boom": "鼓点",
    "Bossa": "波萨", "C-Pop": "华语流行", "Cabaret": "卡巴莱",
    "Cantopop": "粤语流行", "Cappella": "", "Celtic": "凯尔特",
    "Chamber": "室内乐", "Children's": "童声", "Chill": "弛放",
    "Chillhop": "弛放嘻哈", "Chillwave": "弛放浪潮", "Chinese": "华语",
    "Choral": "合唱", "Christian": "基督教", "Cinematic": "电影感",
    "City": "城市", "Classic": "经典", "Classical": "古典乐",
    "Conscious": "意识", "Contemporary": "当代", "Cool": "冷调",
    "Country": "乡村", "Dance": "舞曲", "Dance-Pop": "舞曲流行",
    "Dark": "暗黑", "Darkwave": "暗黑浪潮", "Death": "死亡",
    "Deathcore": "死核", "Deep": "深邃", "Disco": "迪斯科",
    "Disco-Funk": "迪斯科放克", "Disco-Pop": "迪斯科流行",
    "Doo-Wop": "嘟喔普", "Downtempo": "慢拍", "Dream": "梦幻",
    "Drill": "Drill", "Dubstep": "回响贝斯", "EDM": "EDM",
    "EDM-Pop": "EDM流行", "East": "东方", "Eastern": "东欧风",
    "Electric": "电子", "Electro": "电子",
    "Electro-Pop": "电子流行", "Electronic": "电子",
    "Electronicore": "电子核", "Electropop": "电子流行", "Elements": "元素",
    "Emo": "情绪核", "Epic": "史诗", "Ethereal": "空灵",
    "Euphoric": "欣快", "Euro-Dance": "欧陆舞曲", "Euro-Disco": "欧陆迪斯科",
    "Eurodance": "欧陆舞曲", "Experimental": "实验", "Fantasy": "奇幻",
    "Festival": "音乐节", "Folk": "民谣", "Folk-Country": "民谣乡村",
    "Folk-Influenced": "民谣影响", "Folk-Pop": "民谣流行",
    "Folk-Rock": "民谣摇滚", "Folklore": "民间传说", "Funk": "放克",
    "Funk-Pop": "放克流行", "Funk-Rock": "放克摇滚", "Fusion": "融合",
    "Future": "未来", "G-Funk": "G放克", "Gospel": "福音",
    "Gospel-Blues": "福音布鲁斯", "Gospel-Influenced": "福音影响",
    "Gospel-Infused": "福音渗透", "Gospel-Pop": "福音流行", "Gothic": "哥特",
    "Groove": "律动", "Guofeng": "国风", "Happy": "欢快", "Hard": "硬",
    "Hardcore": "硬核", "Hardstyle": "硬派", "Hardstyle-EDM": "硬派EDM",
    "Hardstyle-infused": "硬派渗透", "Heavy": "重型", "Hip": "嘻",
    "Hop": "哈", "House": "浩室", "Hyperpop": "超流行", "Indie": "独立",
    "Industrial": "工业", "Italo": "意式", "J-Pop": "日系流行",
    "J-Rock": "日系摇滚", "Jazz": "爵士", "Jazz-infused": "爵士渗透",
    "Jump": "跳跃", "Listening": "", "Lo-fi": "低保真", "Lounge": "沙发",
    "Mandopop": "华语流行", "March": "进行曲", "Maritime": "海事",
    "Melodic": "旋律化", "Metal": "金属", "Metalcore": "金属核",
    "Modern": "现代", "Music": "音乐", "Musical": "音乐剧",
    "Narrative": "叙事", "Neo-Soul": "新灵魂", "Neoclassical": "新古典",
    "New": "", "Noir": "黑色", "Nova": "", "Novelty": "趣味",
    "Nu-Disco": "新迪斯科", "Nu-Metal": "新金属", "Opera": "歌剧",
    "Orchestral": "管弦", "Patriotic": "爱国", "Pop": "流行",
    "Pop-Rap": "流行说唱", "Post-Grunge": "后垃圾",
    "Post-Hardcore": "后硬核", "Post-Punk": "后朋克", "Power": "力量",
    "Progressive": "前卫", "Punk": "朋克", "R&B": "R&B", "Rap": "说唱",
    "Reggae": "雷鬼", "Retro": "复古", "Retro-Pop": "复古流行",
    "Retrowave": "复古浪潮", "Revival": "复兴", "Rock": "摇滚",
    "Rockabilly": "山地摇滚", "Roll": "", "Room": "室内",
    "Roots": "根源", "Sacred": "圣乐", "Sea": "", "Shanty": "",
    "Show": "", "Singer-Songwriter": "创作歌手", "Slow": "慢板",
    "Slowcore": "慢核", "Smooth": "顺滑", "Soft": "柔和", "Soul": "灵魂",
    "Soul-Blues": "灵魂布鲁斯", "Soul-Jazz": "灵魂爵士",
    "Soul-Pop": "灵魂流行", "Soundtrack": "原声配乐", "Southern": "南方",
    "Spiritual": "灵歌", "Spoken": "", "Storytelling": "叙事性",
    "Surf": "冲浪", "Swing": "摇摆", "Symphonic": "交响",
    "Synth-Pop": "合成器流行", "Synth-pop": "合成器流行",
    "Synthwave": "合成器浪潮", "Techno": "科技舞曲", "Theatre": "音乐剧",
    "Theatrical": "戏剧化", "Traditional": "传统", "Trance": "出神",
    "Trance-Pop": "出神流行", "Trap": "陷阱说唱",
    "Trap-Pop": "陷阱流行", "Tropical": "热带", "Tune": "",
    "UK": "英伦", "Up-tempo": "快板", "Upbeat": "欢快上扬",
    "Vocal": "人声", "Wave": "浪潮", "Word": "", "World": "世界",
    "Worship": "敬拜", "and": "与", "elements": "元素", "fusion": "融合",
    "influenced": "影响", "influences": "影响", "melodic": "旋律化",
    "strong": "浓郁", "stylistic": "风格化", "traditional": "传统",
    "with": "带",
}


def zh_style(style: str) -> str:
    """风格名机械中文化：短语词典优先 → 逐词合成（'/' 分隔→'·'）；
    未收录词保留英文原词。"""
    import re as _re
    parts = []
    for part in (p.strip() for p in style.split("/")):
        if not part:
            continue
        t = part.replace("(", "（").replace(")", "）")
        for pat, zh in _PHRASES:
            t = _re.sub(pat, zh, t, flags=_re.I)
        toks = [(_TERM_ZH.get(w, _TERM_ZH.get(w.lower(), w)) or "")
                for w in t.split()]
        parts.append("".join(toks) or part)
    return " · ".join(parts)


def _index_path(family: str) -> Path:
    """family id → 索引文件；路径安全（白名单由文件名枚举保证）。"""
    if not re.fullmatch(r"[a-z0-9-]+", family or ""):
        raise ValueError(f"非法族 id: {family!r}")
    p = ROOT / "references" / f"index-{family}.md"
    if not p.exists():
        raise ValueError(f"族不存在: {family}")
    return p


def list_families(repo_root: Path | None = None) -> list[dict]:
    """18 族清单：[{id, name, count}]——id 来自文件名、name 来自 H1、
    count 来自「N compact style cards」行。"""
    root = (repo_root / "templates" / "music_styles") if repo_root else ROOT
    out = []
    for p in sorted((root / "references").glob("index-*.md")):
        m = _INDEX_RE.match(p.name)
        if not m:
            continue
        text = p.read_text(encoding="utf-8")
        head = _HEAD_RE.search(text)
        cnt = _COUNT_RE.search(text)
        out.append({"id": m["id"],
                    "name": head["name"].strip() if head else m["id"],
                    "name_zh": _FAMILY_ZH.get(m["id"], ""),
                    "count": int(cnt["n"]) if cnt else 0})
    return out


def list_cards(family: str) -> list[dict]:
    """族内卡表：[{id, style, tempo, mood, vocal, palette, file}]——选择器的
    文本预览（代替缩略图）。"""
    text = _index_path(family).read_text(encoding="utf-8")
    cards = []
    for line in text.splitlines():
        m = _ROW_RE.match(line.strip())
        if m:
            cards.append({"id": m["id"].strip(),
                          "style": m["style"].strip(),
                          "style_zh": zh_style(m["style"].strip()),
                          "tempo": m["tempo"].strip(),
                          "mood": m["mood"].strip(),
                          "vocal": m["vocal"].strip(),
                          "palette": m["palette"].strip(),
                          "file": m["file"].strip()})
    return cards


def get_card(family: str, filename: str) -> str:
    """卡全文（完整英文结构化 caption——Music3TextEncode.caption 直接消费的
    格式）。文件名白名单校验（纯 basename 且确在族索引内），路径穿越拦死。"""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", filename or ""):
        raise ValueError(f"非法卡文件名: {filename!r}")
    if filename not in {c["file"] for c in list_cards(family)}:
        raise ValueError(f"卡 {filename} 不在族 {family} 索引内")
    p = (ROOT / "templates" / filename).resolve()
    if ROOT.resolve() not in p.parents:
        raise ValueError("路径越界")
    if not p.exists():
        raise ValueError(f"卡文件缺失: {filename}")
    return p.read_text(encoding="utf-8").strip()
