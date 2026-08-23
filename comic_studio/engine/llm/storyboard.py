# comic_studio/engine/llm/storyboard.py
"""分镜拆解：schema、提示词、编排（spec §9.2，台账/绑定/workflow_type 建议）。"""
import json

from pydantic import BaseModel, Field, field_validator

SPLIT_SYSTEM = """你是小说改编漫剧的分镜师。把给定的小说文本拆成连续的分镜（shot）序列，供后续 AI 视频生成使用。

规则：
1. 每个分镜 = 一个可独立生成的视频镜头（通常 3~8 秒）；按剧情顺序，覆盖全部情节，不跳戏不脑补
2. 只使用名册中列出的资产 id 绑定角色/场景/道具；新出现的无名路人不绑定
3. camera 用中文枚举：景别(远景/全景/中景/近景/特写)、机位(平视/仰视/俯视/过肩)、运镜(固定/推/拉/摇/移/跟)、转场(切/叠化/无)
4. workflow_type：与上一镜衔接（同场景连续动作）→ "fl2v"；常规（参考角色/场景出图）→ "ref2va"；建立全新画面且无参考 → "t2v"
5. continue_prev：本镜是否紧接上一镜延续（同场景、动作连贯）——分块拆解时首镜若延续上一块结尾则 true
6. 台账四分类：must_appear(画面必须出现的实体/动作)、must_keep(必须保持的资产特征)、may_change(允许自由发挥)、must_avoid(易错必须避免项，如"左右手颠倒""换服装"）
7. description 写成可直接指导视频生成的画面描述：谁在哪做什么、构图与光线，80 字内中文
8. duration 按动作量 3~8 秒取值

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
    duration: float = Field(ge=1, le=15, default=5)
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
        # Handle both dict and SimpleNamespace access
        appearance_json = getattr(r, "appearance_json", None) or r.get("appearance_json")
        detail = json.loads(appearance_json).get("detail", "")[:30]
        kind = getattr(r, "kind", None) or r.get("kind")
        rid = getattr(r, "id", None) or r.get("id")
        name = getattr(r, "name", None) or r.get("name")
        roster[kind].append(f"id={rid} {name}（{detail}）")
    lines = ["可用资产名册（只允许绑定以下 id）："]
    for kind, label in (("character", "角色"), ("scene", "场景"), ("prop", "道具")):
        if roster[kind]:
            lines.append(f"{label}：" + "；".join(roster[kind]))
    lines.append("")
    lines.append("小说文本：")
    lines.append(chunk_text)
    return "\n".join(lines)
