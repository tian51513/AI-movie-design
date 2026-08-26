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
    for aid in (chars + (ledger.get("assets") or {}).get("scenes", [])
                + (ledger.get("assets") or {}).get("props", [])):
        if len(slots) >= 2:
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
            "在结构化骨架的 detailed_description 中，额外包含以下维度的具体描述：\n"
            "· 色彩与光影（主色调/光源方向/氛围/对比度/景深）\n"
            "· 构图与镜头（机位/景别/运镜方式/黄金分割或中心构图）\n"
            "· 人物（服饰/发型/表情/动作/皮肤质感，逐人描述）\n"
            "· 节奏（前中后段的时间分配与情绪递进）\n"
            "· 避免项（写明禁止出现的风格/元素，如 禁止卡通化/禁止模糊边缘/禁止二次元化）\n"
            "不使用任何 <Picture N> 引用（无参考图）")
    else:
        lines.extend([
            "图片槽位表（subject_definitions 引用图片只能用槽位号 1/2，"
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


def _check_picture_refs(text: str) -> tuple[bool, str]:
    """<Picture N> 只允许 1/2（真机 2026-08-26：LLM 照抄资产 id → 锚定全失效）。"""
    refs = _re.findall(r"<Picture (\d+)>", text or "")
    bad = [r for r in refs if r not in ("1", "2")]
    if bad:
        return False, f"<Picture> 引用了不存在的图片编号 {bad}（只能用 1/2，严禁资产 id）"
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
        text, _u = client.raw_chat(messages, temperature=0.4)
        text = (text or "").strip()
        if backend != "h3":
            return text
        bound = len(ledger_assets(shot))  # 台账绑定资产数（ref 图数量）
        sok, smsg = structure_check(text, mode)
        pok, pmsg = _check_picture_refs(text)
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
