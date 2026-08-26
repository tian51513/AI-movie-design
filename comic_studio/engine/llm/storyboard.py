# comic_studio/engine/llm/storyboard.py
"""分镜拆解：schema、提示词、编排（spec §9.2，台账/绑定/workflow_type 建议）。"""
import json
import re
import time
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field, field_validator
from types import SimpleNamespace

SPLIT_SYSTEM = """你是小说改编漫剧的分镜师。把给定的小说文本拆成连续的分镜（shot）序列，供后续 AI 视频生成使用。

规则：
1. 每个分镜 = 一个可独立生成的视频镜头（通常 4~8 秒）；按剧情顺序，覆盖全部情节，不跳戏不脑补
2. 只使用名册中列出的资产 id 绑定角色/场景/道具；新出现的无名路人不绑定
3. camera 用中文枚举：景别(远景/全景/中景/近景/特写)、机位(平视/仰视/俯视/过肩)、运镜(固定/推/拉/摇/移/跟)、转场(切/叠化/无)。景别优先中景/近景；远景与大全景仅在环境叙事必需时使用，并在台账 must_keep 注明保持人物发型与服装轮廓特征
4. workflow_type：与上一镜衔接（同场景连续动作）→ "fl2v"；常规（参考角色/场景出图）→ "ref2va"；建立全新画面且无参考 → "t2v"
5. continue_prev：本镜是否紧接上一镜延续（同场景、动作连贯）——分块拆解时首镜若延续上一块结尾则 true
6. 台账四分类：must_appear(画面必须出现的实体/动作)、must_keep(必须保持的资产特征)、may_change(允许自由发挥)、must_avoid(易错必须避免项，如"左右手颠倒""换服装"）
7. description 写成可直接指导视频生成的画面描述：谁在哪做什么、构图与光线，80 字内中文
8. duration 一律填项目统一段时长（上下文给出），不得自行增减

只输出一个 JSON 对象：
{"shots":[{"text_span":"对应原文摘录","description":"...","shot_type":"对话/动作/场景/情绪",
 "camera":{"景别":"中景","机位":"平视","运镜":"固定","转场":"切"},
 "duration":5,"workflow_type":"ref2va",
 "must_appear":["萧炎"],"must_keep":["萧炎的黑发"],"may_change":["镜头角度"],"must_avoid":["服装变化"],
 "character_ids":[1],"scene_ids":[2],"prop_ids":[],"continue_prev":false}]}"""


class ShotDraft(BaseModel):
    text_span: str = ""
    description: str = Field(min_length=1)
    shot_type: str = ""
    camera: dict = Field(default_factory=dict)
    duration: float = Field(ge=4, le=15, default=5)
    workflow_type: str = "ref2va"
    must_appear: list[str] = []
    must_keep: list[str] = []
    may_change: list[str] = []
    must_avoid: list[str] = []
    character_ids: list[int] = []
    scene_ids: list[int] = []
    prop_ids: list[int] = []
    continue_prev: bool = False


class ChunkStoryboard(BaseModel):
    shots: list[ShotDraft] = Field(min_length=1)

    @field_validator("shots")
    @classmethod
    def _nonempty(cls, v):
        if not v:
            raise ValueError("分镜序列不能为空")
        return v


def build_split_user_prompt(chunk_text: str, assets_rows) -> str:
    roster = {"character": [], "scene": [], "prop": []}
    for r in assets_rows:
        appearance_json = r["appearance_json"] if hasattr(r, '__getitem__') else r.appearance_json
        detail = json.loads(appearance_json).get("detail", "")[:30]
        kind = r["kind"] if hasattr(r, '__getitem__') else r.kind
        rid = r["id"] if hasattr(r, '__getitem__') else r.id
        name = r["name"] if hasattr(r, '__getitem__') else r.name
        roster[kind].append(f"id={rid} {name}（{detail}）")
    lines = ["可用资产名册（只允许绑定以下 id）："]
    for kind, label in (("character", "角色"), ("scene", "场景"), ("prop", "道具")):
        if roster[kind]:
            lines.append(f"{label}：" + "；".join(roster[kind]))
    lines.append("")
    lines.append("小说文本：")
    lines.append(chunk_text)
    return "\n".join(lines)

from ..assets import list_project_assets
from ..logbus import emit as emit_log
from ..projects import get_project
from ..settings import get_setting
from ..shots import persist_shots
from .provider import ask_validated, client_for_task
from .text import split_chunks

class ContentBoundaryError(Exception):
    """输入或生成内容命中未成年性内容硬界线（项目级，跳过并显式报错）。"""

_MINOR_SEXUAL = re.compile(r"(萝莉|幼女|女童|男童).{0,12}(性|色情|裸|吻|床|情欲)|(性|色情|裸|情欲).{0,12}(萝莉|幼女|女童)|校服.{0,8}(情欲|性爱|裸)")


def _content_guard(text: str) -> None:
    if _MINOR_SEXUAL.search(text):
        raise ContentBoundaryError("内容命中项目硬界线（涉及未成年人的性内容），该段已跳过并停止处理")


ClientFactory = Callable[[str], object]


def make_split_factory(db):
    from .analyze import make_client_factory
    return make_client_factory(db)


def split_storyboards(db, data_dir, project_id, client_factory=None, max_chars=8000):
    if client_factory is None:
        client_factory = make_split_factory(db)
    proj = get_project(db, project_id)
    if proj is None:
        raise ValueError(f"项目不存在: {project_id}")
    from ..paths import data_to_abs
    text = data_to_abs(data_dir, proj["novel_path"]).read_text(encoding="utf-8")
    _content_guard(text)
    chunks = split_chunks(text, max_chars=max_chars)
    assets = list_project_assets(db, project_id)
    emit_log(db, "storyboard", "info",
             f"开始分镜拆解：{len(chunks)} 块（共 {len(text)} 字，{len(assets)} 个资产入名册）",
             project_id=project_id)
    client = client_factory("split_storyboards")
    provider = get_setting(db, "llm_routing")["split_storyboards"]
    staged, link_first_of_block = [], []   # link_first_of_block[i] = i 块首镜在 staged 中的下标（需链上一块末镜）
    for i, chunk in enumerate(chunks, 1):
        emit_log(db, "storyboard", "info", f"分块 {i}/{len(chunks)} 拆解中（{len(chunk)} 字）",
                 project_id=project_id)
        t0 = time.monotonic()
        result, usage = ask_validated(client, SPLIT_SYSTEM,
                                      build_split_user_prompt(chunk, assets),
                                      ChunkStoryboard)
        emit_log(db, "llm", "info",
                 f"split_storyboards 完成 · {getattr(client, 'model', '?')} · "
                 f"{usage.prompt_tokens}+{usage.completion_tokens} tok · {time.monotonic()-t0:.1f}s · "
                 f"{len(result.shots)} 镜", project_id=project_id)
        for d in result.shots:
            _content_guard(d.description + " " + d.text_span)
        if result.shots[0].continue_prev and staged:
            link_first_of_block.append(len(staged))
        from ..projects import get_project as _gp
        _proj = _gp(db, project_id)
        _dur = float(_proj["default_shot_duration"]) if _proj else 5.0
        staged.extend(SimpleNamespace(
            text_span=d.text_span, description=d.description, shot_type=d.shot_type,
            camera=d.camera, duration=_dur, workflow_type=d.workflow_type,
            ledger={"must_appear": d.must_appear, "must_keep": d.must_keep,
                    "may_change": d.may_change, "must_avoid": d.must_avoid},
            character_ids=d.character_ids, scene_ids=d.scene_ids, prop_ids=d.prop_ids,
            depends_on=None) for d in result.shots)
    ids = persist_shots(db, project_id, staged)
    conn = db.connect()
    # 尾帧接力链（连贯性① 2026-08-26）：全顺序镜自动链接（含跨块衔接——
    # 此前仅 continue_prev 标记的块边界链接，绝大多数镜 depends_on 为空，
    # 渲染时首帧接力从未触发，镜间不连贯的根因之一）
    for prev, cur in zip(ids, ids[1:]):
        conn.execute("UPDATE shots SET depends_on=? WHERE id=?", (prev, cur))
    # 创建时设了预设总时长 → 拆完按镜数均摊（下限4s，连贯性/时长需求 2026-08-26）
    _proj = conn.execute("SELECT target_duration, default_shot_duration FROM projects "
                         "WHERE id=?", (project_id,)).fetchone()
    if _proj and _proj["target_duration"] and _proj["target_duration"] > 0:
        per = max(4, round(_proj["target_duration"] / max(1, len(ids))))
        conn.execute("UPDATE shots SET duration=? WHERE project_id=?", (per, project_id))
        conn.execute("UPDATE projects SET default_shot_duration=? WHERE id=?",
                     (per, project_id))
    conn.commit()
    emit_log(db, "storyboard", "info", f"分镜落库 {len(ids)} 镜（已替换旧分镜）",
             project_id=project_id)
    return ids
