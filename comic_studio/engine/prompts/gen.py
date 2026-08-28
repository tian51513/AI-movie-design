# comic_studio/engine/prompts/gen.py
"""分镜 → 视频提示词：H3（vendored 规程）与 LTX（简化规程）双适配器（spec §9.2）。"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from ..assets import list_project_assets
from ..projects import get_project
from ..shots import get_shot
from . import H3_DIR

LTX_SYSTEM = """你是视频提示词写手（LTX 后端简化规程）。根据镜头上下文输出一段可直接使用的视频提示词：
一段连贯的中文描述（主体动作 + 场景光线 + 运镜），100~300 字，不输出标题/列表/分析。"""

_PIPELINE_NOTE = """【流水线适配】你在自动化管线中非交互运行：直接输出最终提示词正文，
不要输出"建议设置/素材编号/分析过程/可自行补充"等任何附加语；本阶段无音频，跳过声音系统模块。"""


def build_h3_system() -> str:
    parts = [_PIPELINE_NOTE]
    for rel in ("SKILL.md", "references/official-rules.md", "references/capability-map.md"):
        p = H3_DIR / rel
        if p.exists():
            parts.append(p.read_text(encoding="utf-8"))
    # 借鉴 XiaoLuo 规范（2026-08-28 第一批）：反代词具名 + 分级英文运镜标签
    parts.append(
        "【输出卫生规范（必须遵守）】\n"
        "1. 叙述中严禁使用「他/她/它」等代词指代角色——一律写角色名（多角色画面"
        "主体绑定的关键，代词会让模型换人）。\n"
        "2. 按景别混入固定英文运镜标签，每镜 2~4 个不堆砌：\n"
        "   - 特写/近景：locked-off tripod shot, ultra-stable micro-jitter\n"
        "   - 对峙/台词戏：subtle cinematic handheld, realistic camera inertia\n"
        "   - 全景/空镜/环境：documentary-style cinematic handheld, ambient light drift\n"
        "3. 禁止 Meta 词：电影级、9:16 画幅、生成模型、短片节奏、HEX 色码、导演/明星名。")
    return "\n\n---\n\n".join(parts)


def build_shot_context(shot_row, assets_by_id: dict, project_row,
                       prev_shot=None) -> str:
    ledger = json.loads(shot_row["ledger_json"] or "{}")
    assets = ledger.get("assets", {})
    bind_desc = []
    for kind, label in (("characters", "角色"), ("scenes", "场景"), ("props", "道具")):
        for aid in assets.get(kind, []):
            a = assets_by_id.get(aid)
            if a is not None:
                detail = json.loads(a["appearance_json"]).get("detail", "")[:60]
                bind_desc.append(f"{label} {a['name']}：{detail}")  # 不带 id——防 LLM 照抄进 <Picture N>
    era = project_row["era"] if "era" in project_row.keys() else ""
    # 图片槽位表（与渲染实际布局一致；真机 2026-08-26 教训：<Picture 70>=
    # 资产 id 照抄 → 角色锚定全失效——必须显式告知槽位号与内容）
    chars = (ledger.get("assets") or {}).get("characters") or []
    prev_chars = set()
    if prev_shot is not None:
        prev_chars = set(
            ((json.loads(prev_shot["ledger_json"] or "{}").get("assets")
              or {}).get("characters")) or [])
    relay = prev_shot is not None and bool(set(chars) & prev_chars)
    slots = []
    if relay:
        slots.append("<Picture 1> = 上一镜尾帧（仅供构图与姿态衔接，人物外貌不得以此为准）")
    from ..rendershot import pick_template_id as _pti
    from ..workflows import registry as _wreg
    try:
        _tid = _pti(shot_row, db=None)
        _regs = _wreg.scan_templates(_wreg.TEMPLATE_ROOT)
        max_slots = len(_regs[_tid].inject_images) if _tid in _regs else 2
    except Exception:
        max_slots = 2
    for aid in (chars + (ledger.get("assets") or {}).get("scenes", [])
                + (ledger.get("assets") or {}).get("props", [])):
        if len(slots) >= max_slots:
            break
        a = assets_by_id.get(aid)
        if a is None:
            continue
        tag = ("角色三视图，人物外貌唯一依据" if a["kind"] == "character"
               else ("场景参考" if a["kind"] == "scene" else "道具参考"))
        slots.append(f"<Picture {len(slots) + 1}> = {a['name']}（{tag}）")
    is_t2v = ((shot_row["workflow_type"] if "workflow_type" in shot_row.keys() else "") or "") == "t2v"
    lines = [
        f"镜头 {shot_row['seq']}（{shot_row['shot_type'] or '常规'}，{shot_row['duration']} 秒，"
        f"画幅 {project_row['aspect_ratio']}，后端工作流 {shot_row['workflow_type']}）",
        f"画面描述：{shot_row['description']}",
        f"镜头语言：{shot_row['camera_json']}",
        f"项目画风：{project_row['style'] or '未指定'}",
        (f"时代风格：{era}，人物服饰、发型、器物、建筑均须符合该时代形制，禁止现代元素"
         if era else "时代风格：未明确（按描述自行合理推断）"),
    ]
    if is_t2v:
        lines.append(
            "【文生视频模式】无图片参考——提示词是唯一画面约束，必须极度详尽：\n"
            "以下维度必须写在 detailed_description 分段内部（作为该段落的组成部分），不得另起分段或放在 non_diegetic_music 之后：\n"
            "· 色彩与光影（主色调/光源方向/氛围/对比度/景深）\n"
            "· 构图与镜头（机位/景别/运镜方式/黄金分割或中心构图）\n"
            "· 人物（服饰/发型/表情/动作/皮肤质感，逐人描述）\n"
            "· 节奏（前中后段的时间分配与情绪递进）\n"
            "· 避免项（写明禁止出现的风格/元素，如 禁止卡通化/禁止模糊边缘/禁止二次元化）\n"
            "不使用任何 <Picture N> 引用（无参考图）")
    else:
        lines.extend([
            f"图片槽位表（subject_definitions 引用图片只能用槽位号 1~{max_slots}，"
            "严禁使用资产 id 数字）：" + ("；".join(slots) if slots else "无图片参考"),
            ("身份锚定规则：<Picture 1> 尾帧仅衔接画面；人物五官与服装必须以角色三视图槽位为准"
             if relay else ""),
        ])
    lines.append("绑定资产概览：" + ("；".join(bind_desc) if bind_desc else "无"))
    lines.append(
        f"需求台账：必须出现={ledger.get('must_appear', [])}；必须保持={ledger.get('must_keep', [])}；"
        f"允许变化={ledger.get('may_change', [])}；禁止={ledger.get('must_avoid', [])}")
    dialogue = ledger.get("dialogue") or []
    if dialogue:
        lines.append("台词（视频对白必须逐字使用，不得改写）：" + "；".join(
            f"{d.get('speaker', '?')}：“{d.get('line', '')}”" for d in dialogue))
    if prev_shot is not None:
        # 连贯性③（2026-08-26）：姿态/位置/服装默认延续上镜结尾
        prev_desc = (prev_shot["description"] or "")[:80]
        lines.append(
            f"连贯性约束：人物姿态、位置、服装默认延续上一镜结尾状态，"
            f"仅当本镜描述明确写出变化（起身/更衣/换位等）才变化。"
            f"上一镜（第 {prev_shot['seq']} 镜）：{prev_desc}")
    return "\n".join(lines)


def validate_h3(prompt_text: str, duration, ratio: str, images=0, videos=0) -> tuple[bool, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as f:
        f.write(prompt_text); tmp = f.name
    try:
        r = subprocess.run(
            [sys.executable, str(H3_DIR / "scripts/validate_h3_prompt.py"),
             "--input", tmp, "--mode", "reference-to-video",
             "--duration", str(int(duration)), "--ratio", ratio,
             "--images", str(images), "--videos", str(videos), "--audios", "0"],
            capture_output=True, timeout=20, text=True)
        return r.returncode == 0, (r.stdout + r.stderr).strip()[:300]
    finally:
        Path(tmp).unlink(missing_ok=True)


def ledger_assets(shot_row) -> list[int]:
    ledger = json.loads(shot_row["ledger_json"] or "{}")
    assets = ledger.get("assets", {})
    return (assets.get("characters", []) + assets.get("scenes", [])
            + assets.get("props", []))


_REQUIRED_SECTIONS = ("subject_definitions:", "summary:", "retention_analysis:",
                      "detailed_description:", "overall_soundscape:", "non_diegetic_music:")


import re as _re


def _check_picture_refs(text: str, max_pics: int = 2) -> tuple[bool, str]:
    """<Picture N> 编号不能超过模板实际槽数（真机 2026-08-26 教训）。"""
    valid = {str(i) for i in range(1, max_pics + 1)}
    refs = _re.findall(r"<Picture (\d+)>", text or "")
    bad = [r for r in refs if r not in valid]
    if bad:
        return False, (f"<Picture> 引用了不存在的图片编号 {bad}"
                       f"（只能用 1~{max_pics}，严禁资产 id）")
    return True, ""


def structure_check(text: str, mode: str | None) -> tuple[bool, str]:
    """结构化模式（B/C/D）必需分段头校验；A/None 放行（散文模式）。
    2026-08-25：规范早有骨架要求但无校验，模型实际产出散文被放行。"""
    if mode is None or mode == "A":
        return True, ""
    low = (text or "").lower()
    missing = [s for s in _REQUIRED_SECTIONS if s not in low]
    if missing:
        return False, f"缺少必需分段: {missing}（{mode} 模式要求结构化骨架）"
    return True, ""


def _dedup_sentences(line: str) -> tuple[str, bool]:
    """行内去重句子（约束包裹膨胀：同一句禁令出现两次只留一次）。"""
    if "。" not in line or len(line) < 20:
        return line, False
    seen, out, changed = set(), [], False
    for s in line.split("。"):
        ss = s.strip()
        if len(ss) > 6:
            if ss in seen:
                changed = True
                continue
            seen.add(ss)
        out.append(s)
    return "。".join(out), changed


_META_WORDS_RE = _re.compile(r"电影级|9[:：]16\s*画幅|生成模型|短片节奏")


def heal_h3_prompt(text: str, shot_row, max_pics: int = 2):
    """P7-C 提示词 token 自愈（借鉴 Director reinforce 思想）：机械可修的问题
    直接修，不消耗 LLM 重试——①占位语删除 ②超界 <Picture N> 引用删除
    ③行内重复句子去重 ④有对白缺 <d>Chinese</d> 补标记
    ⑤Meta 词剥离（借鉴 XiaoLuo：电影级/9:16画幅/生成模型/短片节奏）
    ⑥结尾后缀协议（「无字幕，无背景音乐」固定收尾，抑制自动配乐字幕）。
    返回 (healed, fixes)。"""
    fixes = []
    t = text or ""
    if "可自行补充" in t:
        t = "\n".join(l for l in t.splitlines() if "可自行补充" not in l)
        fixes.append("删除占位语")
    if _META_WORDS_RE.search(t):
        t = _META_WORDS_RE.sub("", t)
        fixes.append("删 Meta 词")
    bad_refs = sorted({int(n) for n in _re.findall(r"<Picture (\d+)>", t)
                       if int(n) > max_pics})
    if bad_refs:
        for n in bad_refs:
            t = t.replace(f"<Picture {n}>", " ")
        t = _re.sub(r"[ \t]{2,}", " ", t)
        fixes.append(f"删除超界引用 {bad_refs}")
    kept, dup = [], False
    for line in t.splitlines():
        line2, changed = _dedup_sentences(line)
        dup = dup or changed
        kept.append(line2)
    if dup:
        t = "\n".join(kept)
        fixes.append("重复句去重")
    try:
        ledger = json.loads(shot_row["ledger_json"] or "{}") if shot_row is not None else {}
    except (json.JSONDecodeError, TypeError, KeyError):
        ledger = {}
    if ledger.get("dialogue") and "<d>" not in t:
        t = t.rstrip() + "\n<d>Chinese</d>"
        fixes.append("补 <d>Chinese</d>")
    if "无字幕" not in t:
        t = t.rstrip() + "\n无字幕，无背景音乐"
        fixes.append("补结尾后缀")
    return t, fixes


def generate_video_prompt(db, shot_id, client, backend: str = "h3",
                          mode: str | None = None,
                          max_attempts: int = 3) -> str:
    from .modes import PROMPT_MODES, mode_spec
    shot = get_shot(db, shot_id)
    proj = get_project(db, shot["project_id"])
    if mode is None:
        mode = (proj["prompt_mode"]
                if proj is not None and proj["prompt_mode"] in PROMPT_MODES else "D")
    assets_by_id = {a["id"]: a for a in list_project_assets(db, shot["project_id"])}
    # 模板实际图槽数（动态——导入多槽模板不再被 2 封顶）
    from ..workflows import registry as _reg
    from ..rendershot import pick_template_id
    try:
        _tid = pick_template_id(shot, db=db)
        _reg_cached = _reg.scan_templates(_reg.TEMPLATE_ROOT)
        max_ref_images = len(_reg_cached[_tid].inject_images) if _tid in _reg_cached else 2
    except Exception:
        max_ref_images = 2
    prev_shot = db.connect().execute(
        "SELECT * FROM shots WHERE project_id=? AND seq=?",
        (shot["project_id"], shot["seq"] - 1)).fetchone()
    ctx = build_shot_context(shot, assets_by_id, proj, prev_shot=prev_shot)
    system = (build_h3_system() + "\n\n---\n\n" + mode_spec(mode)
              if backend == "h3" else LTX_SYSTEM)
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": ctx}]
    last_err = ""
    for _ in range(max_attempts):
        try:
            text, _u = client.raw_chat(messages, temperature=0.4)
        except Exception as exc:
            # 思考模型 × 16k ctx（真机 2026-08-28 job 682：同输入两截断一成功，
            # 纯碰运气）：截断时追加「压缩思考直接输出」反馈再试，其余异常照抛
            from ..llm.provider import LLMError
            if isinstance(exc, LLMError) and "截断" in str(exc):
                last_err = str(exc)
                messages += [{"role": "user", "content":
                              "上次输出被上下文长度截断。请极度压缩思考过程，"
                              "跳过推理展开，直接输出最终提示词本体。"}]
                continue
            raise
        text = (text or "").strip()
        if backend == "h3":
            # P7-C 自愈：机械可修的问题直接修，不消耗重试（占位语/超界引用/
            # 重复句/缺对白标记——2026-08-27 前这些全靠 LLM 重生成，两次排障浪费）
            text, _fixes = heal_h3_prompt(text, shot, max_pics=max_ref_images)
        if backend != "h3":
            return text
        bound = len(ledger_assets(shot))  # 台账绑定资产数（ref 图数量）
        sok, smsg = structure_check(text, mode)
        pok, pmsg = _check_picture_refs(text, max_pics=max_ref_images)
        ok, msg = (validate_h3(text, max(4, int(shot["duration"])), proj["aspect_ratio"],
                               images=bound, videos=0)
                   if sok and pok else (False, pmsg or smsg))
        if sok and pok and ok and "可自行补充" not in text:
            return text
        last_err = (smsg or msg) or "输出含占位语"
        messages += [{"role": "assistant", "content": text},
                     {"role": "user", "content":
                      f"上一版未通过机械校验：{last_err}。请修正后重新输出完整提示词，只输出提示词。"}]
    raise RuntimeError(f"视频提示词 {max_attempts} 次尝试未通过校验：{last_err}")
