# comic_studio/engine/musiclib.py
"""音乐库（2026-09-19 spec）：Music3 生成 → staging 试听 → 入库 → 项目引用。
同 voicelib 心智：文件在 data/music/custom，元数据在 music_library 表。"""
import json
import re as _re
import shutil
from pathlib import Path

# 顶层 import 一次，函数内直接引用模块属性——测试经
# monkeypatch.setattr(musiclib, "client_for_task", ...) 替换才命中
from .llm.provider import client_for_task

STAGING_REL = "music/_staging"
LIBRARY_REL = "music/custom"

# name 白名单（2026-09-19 安全评审）：name 进文件路径，防穿越写盘。
# \w 含下划线字母数字；显式留中文段与空格/连字符/圆点/括号。
_NAME_RE = _re.compile(r"^[\w一-鿿 \-().]{1,100}$")


def staging_dir(data_dir) -> Path:
    d = Path(data_dir) / STAGING_REL
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_to_library(db, data_dir, src: Path, name: str, caption: str, lyrics: str,
                    seed: int, duration: float, origin: str = "user") -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("音乐名不能为空")
    # name 白名单（2026-09-19 安全评审：防路径穿越写盘——name 会拼进
    # music/custom/<name><后缀>；'/' 与 '..' 片段一律拒绝）
    if not _NAME_RE.fullmatch(name) or name in {".", ".."}:
        raise ValueError("音乐名含有非法字符")
    conn = db.connect()
    if conn.execute("SELECT 1 FROM music_library WHERE name=?", (name,)).fetchone():
        raise ValueError(f"音乐名已存在: {name}")
    lib = Path(data_dir) / LIBRARY_REL
    lib.mkdir(parents=True, exist_ok=True)
    dest = lib / f"{name}{src.suffix or '.mp3'}"
    shutil.copy2(src, dest)
    from .paths import rel_to_data
    rel = rel_to_data(data_dir, dest)
    cur = conn.execute(
        "INSERT INTO music_library (name, caption, lyrics, seed, duration, origin, path) "
        "VALUES (?,?,?,?,?,?,?)",
        (name, caption, lyrics, int(seed), float(duration), origin, rel))
    conn.commit()
    return {"id": cur.lastrowid, "name": name, "path": rel}


def list_music(db) -> list[dict]:
    rows = db.connect().execute(
        "SELECT * FROM music_library ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def delete_music(db, data_dir, music_id: int) -> None:
    conn = db.connect()
    row = conn.execute("SELECT * FROM music_library WHERE id=?",
                       (music_id,)).fetchone()
    if row is None:
        raise ValueError(f"音乐不存在: {music_id}")
    from .paths import data_to_abs
    p = data_to_abs(data_dir, row["path"]).resolve()
    lib_root = (Path(data_dir) / LIBRARY_REL).resolve()
    if not p.is_relative_to(lib_root):
        # 库表脏数据/越界路径（2026-09-19 安全评审）：拒绝 unlink 任意文件
        raise ValueError("路径越界")
    p.unlink(missing_ok=True)
    conn.execute("DELETE FROM music_library WHERE id=?", (music_id,))
    conn.commit()


# 曲风/声部清单（2026-09-19 用户需求：市面常见曲风全量 + 歌唱音色选择）。
# caption 用英文描述（Music3 验证样例是英文 caption），中文进 UI/LLM 上下文
GENRES = [("流行", "Pop"), ("民谣", "Folk"), ("摇滚", "Rock"), ("国风", "Guofeng"),
          ("二次元", "Anime"), ("电子", "Electronic"), ("说唱", "Hip-hop"),
          ("爵士", "Jazz"), ("R&B", "R&B"), ("金属", "Metal"), ("乡村", "Country"),
          ("蓝调", "Blues"), ("古典", "Classical"), ("轻音乐", "Light instrumental"),
          ("影视配乐", "Cinematic"), ("氛围", "Ambient"), ("实验", "Experimental")]
VOICE_PARTS = [("女声", "female"), ("男声", "male"), ("男女对唱", "male & female duet"),
               ("童声", "child"), ("合唱", "chorus")]
_GENRE_EN = dict(GENRES)
_VOICE_EN = dict(VOICE_PARTS)
# 完整收尾指令（2026-09-19 真机：60s 上限下歌词唱不完被硬切「戛然而止」）
_COMPLETE_TAIL = (" The song must be complete with a natural ending; "
                  "never cut off mid-phrase.")


def compose_caption(caption: str, genre: str = "", voice: str = "") -> str:
    """生成 caption 组装：曲风+声部拼 Global Metadata 前缀（英文，用户已写
    Global Metadata 开头则不重复加）+ 完整收尾指令恒追加。"""
    caption = (caption or "").strip()
    gen_en = _GENRE_EN.get(genre) or (genre.strip() if genre else "")
    voc_en = _VOICE_EN.get(voice) or (voice.strip() if voice else "")
    parts = [x for x in (gen_en, f"{voc_en} vocals" if voc_en else "") if x]
    if parts and not caption.startswith("Global Metadata"):
        caption = f"Global Metadata: {', '.join(parts)}. " + caption
    elif parts and caption.startswith("Global Metadata") and gen_en \
            and gen_en.lower() not in caption.lower():
        # 用户写了 Global Metadata 行但没提曲风——补进去（首行末插入）
        head, _, rest = caption.partition(".")
        caption = f"{head}, {gen_en}.{rest}"
    return caption + _COMPLETE_TAIL


def planned_duration(target, lyrics: str) -> float:
    """完整歌曲时长平衡：max(目标, 歌词行数×5s) 钳 30~360——宁可超时
    放宽上限也不让歌唱到一半被截断（真机判例：60s 装不下长词=戛然而止）。"""
    base = float(target) if target else 120.0
    lines = [l for l in (lyrics or "").splitlines() if l.strip()]
    planned = max(base, len(lines) * 5.0)
    return min(360.0, max(30.0, planned))


def suggest_caption(db, hint: str = "", genre: str = "", voice: str = "",
                    lyrics: str = "") -> str:
    system = ("你是音乐曲风描述写手，为 MiniMax Music3 生成 caption。输出格式："
              "第一行 'Global Metadata: <风格>, <BPM>, <调性>.'，第二行起中文描述"
              "情绪走向与配器（2~3 句）。只输出正文，不要解释。")
    user = f"作品语境：{hint or '通用背景音乐'}"
    if genre or voice:
        user += f"；曲风：{genre or '未定'}（{voice or '未定'}）"
    if lyrics.strip():   # 曲风↔歌词联动：已有歌词则摘录进上下文定气质
        user += f"；已有歌词摘录：{lyrics.strip()[:120]}"
    text, _ = client_for_task(db, "gen_story").raw_chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}], temperature=0.5)
    text = (text or "").strip()
    if not text:
        raise ValueError("曲风建议为空，请重试")
    return text


def suggest_lyrics(db, hint: str = "", base_lyrics: str = "", duration=None,
                   genre: str = "", voice: str = "") -> str:
    """双模式（2026-09-19 用户需求）：base_lyrics 空=自由创作（hint 当主题）；
    非空=润色保留——原词进上下文，LLM 只补段落/润色衔接不得丢弃原句。
    曲风联动：genre/voice 进系统词（民谣叙事口语、摇滚短句冲击……由 LLM 按
    曲风自行把握）；duration 进上下文控制段落规模（60s≈主副歌各一遍）。"""
    preserve = bool((base_lyrics or "").strip())
    system = ("你是歌词作者。输出中文歌词，结构为 [主歌] / [副歌]（可含 [桥段]），"
              "每段 4 行内、口语可唱、末字尽量押韵。只输出歌词正文。")
    if preserve:
        system += ("\n【最高优先级】用户已有歌词原句必须逐句保留在原结构位置，"
                   "你只做：补足缺失段落、润色衔接句、完善韵脚——不得丢弃、"
                   "改写或压缩任何原有歌词行。")
    if genre:
        system += f"\n歌词风格贴合「{genre}」曲风的语感与意象。"
    if voice:
        system += f"\n演唱声部为{voice}，选词与音域匹配。"
    user = ""
    if preserve:
        user += f"用户原歌词（全文保留）：\n{base_lyrics.strip()}"
        if hint.strip():
            user += f"\n主题方向：{hint}"
    else:
        user += f"主题：{hint or '青春、遗憾与重逢'}"
    if duration:
        user += (f"\n目标时长约 {int(duration)} 秒（60 秒≈主歌+副歌各一遍，"
                 "按时长控制段落数，确保唱得完）")
    text, _ = client_for_task(db, "gen_story").raw_chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}], temperature=0.7)
    text = (text or "").strip()
    if not text:
        raise ValueError("歌词建议为空，请重试")
    return text


def handle_gen_music(db, data_dir, job, comfy) -> Path:
    payload = json.loads(job["payload_json"] or "{}")
    caption = (payload.get("caption") or "").strip()
    if not caption:
        raise ValueError("gen_music 缺 caption")
    lyrics = (payload.get("lyrics") or "").strip()
    genre = (payload.get("genre") or "").strip()
    voice = (payload.get("voice") or "").strip()
    # 完整歌曲（2026-09-19）：时长=max(目标, 歌词量估算)——歌词装不下就放宽
    duration = planned_duration(payload.get("duration"), lyrics)
    seed = int(payload.get("seed") or 0)
    final_caption = compose_caption(caption, genre, voice)
    from .voicelib import _run_template
    dest = _run_template(
        comfy, "music3",
        params={"seed": seed, "max_duration": duration, "lyrics": lyrics},
        images=None, dest_dir=staging_dir(data_dir), name=str(job["id"]),
        db=db, prompt=final_caption, stall_seconds=1800)
    from .paths import rel_to_data
    rel = rel_to_data(data_dir, dest)
    from .jobs import attach_snapshot
    attach_snapshot(db, job["id"], prompt=final_caption,
                    workflow={"staging": rel}, template_id="music3")
    return dest


from .queue.worker import register  # noqa: E402
register("gen_music")(handle_gen_music)
