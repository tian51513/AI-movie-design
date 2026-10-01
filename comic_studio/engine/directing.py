# engine/directing.py
"""定向执导技能（2026-10-01 T8 包借鉴）：按项目题材自动匹配执导方法论，
注入拆解 system（跨镜动作衔接）与视频提示词 system（动作可读性）。

方法论为**自写中文浓缩**（思路借鉴 T8 directional-skills 与其引用的 MIT
screenwriting-skills；T8 仓库本体 All rights reserved，不逐字复制）。
匹配机制 v1 = 机械关键词（name/style/style_vis/era + 正文头 3k 字）——
题材分类是低难度任务，关键词零成本零延迟；手动覆写
projects.directing_override 兜底（''=自动 / 'off'=全关 / 'a,b'=强制）。
"""
from __future__ import annotations

from pathlib import Path

# 技能顺序即注入顺序（稳定）；wenwu 无关键词=只可手动指定（混合题材默认不开）
DIRECTING_SKILLS: dict[str, dict] = {
    "wushu": {
        "label": "武术打斗",
        "keywords": ["武侠", "江湖", "门派", "比武", "武林", "功夫", "武术",
                     "打斗", "过招", "拳法", "剑法", "刀法", "内力", "侠客",
                     "习武", "招式", "轻功"],
        "text": (
            "【武术打斗执导】（动作戏方法论）\n"
            "- 动作可读性：重要发力动作写清参与身体的支撑变化、重心转移、肢体"
            "路线与结束后的可用位置；只用已确立的解剖与装备，不发明武器、"
            "空手夺白刃或超能力。\n"
            "- 因果衔接：交手按既有因果推进——逼近改变距离、格挡改变来线、"
            "闪避留出空当、失衡让出反击；反击需要支撑依据，不自动轮流出手。\n"
            "- 跨镜接力：未完成的动作带进下一镜继续（同一场景时钟），不把双方"
            "重置回中立姿势重启；已发生的位移/倒地/伤势延续，不悄悄复原。\n"
            "- 受力分级：接触不必然导致飞出/出血/碎墙——按发力方向与支撑走"
            "力路径；格挡可以稳稳吸收，脱手可以只是脱手。\n"
            "- 静止合法：戒备、等待、完全不回应都可以保持不动；本执导不主动"
            "添加对手、招式或胜负。"),
    },
    "wenwu": {
        "label": "文武双全",
        "keywords": [],
        "text": (
            "【文武双全执导】（文戏武戏混合方法论）\n"
            "- 文戏段按「戏剧场面执导」执行：观看重点=信息与听者反应。\n"
            "- 武戏段按「武术打斗执导」执行：发力/受力/跨镜接力。\n"
            "- 对白转动作的混合场景：先确立信息落差，再用动作回应（拍桌而起/"
            "夺门而出要有前序情绪支撑），文武切换在镜内给一个可见触发点。"),
    },
    "drama": {
        "label": "戏剧场面",
        "keywords": ["悬疑", "都市", "情感", "谎言", "背叛", "家庭", "婚姻",
                     "复仇", "阴谋", "谈判", "对峙", "心理", "商战", "出轨",
                     "秘密"],
        "text": (
            "【戏剧场面执导】（文戏方法论）\n"
            "- 文戏的观看重点是「信息与反应」：本镜谁知道了什么/隐瞒了什么，"
            "听者接收到之后的微反应（眼神、停顿、手部小动作）优先于说话内容"
            "本身。\n"
            "- 人物按目标行动：每镜给角色一个当下目标（试探/隐瞒/施压/求和），"
            "台词意图与关系潜台词（地位变化、亲疏冷暖）落在动作与反应里，"
            "不靠旁白解释。\n"
            "- 不强制冲突/转折/笑点；沉默与不回应是合法的戏剧状态。"),
    },
    "pov": {
        "label": "POV 剧情导演",
        "keywords": ["第一人称", "POV", "pov", "主观视角"],
        "text": (
            "【POV 剧情导演执导】（第一人称方法论）\n"
            "- 第一人称视角组织画面：本镜眼睛属于谁、看向什么、如何移动；"
            "观众参与感来自「人物接收→动作结果」的完整交代（伸手前先看到"
            "手进入画面）。\n"
            "- 主观镜头的身体边界：画外之物用声音/入画动作提示，不凭空全知；"
            "POV 切换必须在分镜层显式标注。"),
    },
}

_NOVEL_HEAD_CHARS = 3000


def _scan_text(db, proj, data_dir) -> str:
    """匹配信号源：项目名/画风/时代（库字段）+ 正文头（有 data_dir 时）。"""
    def _col(key):
        try:
            return (proj[key] or "") if key in proj.keys() else ""
        except Exception:
            return ""
    text = " ".join([_col("name"), _col("style"), _col("style_vis"), _col("era")])
    if data_dir and proj["novel_path"]:
        p = Path(data_dir) / proj["novel_path"]
        try:
            if p.exists():
                text += " " + p.read_text(encoding="utf-8")[:_NOVEL_HEAD_CHARS]
        except OSError:
            pass
    return text


def match_directing_skills(db, proj, data_dir=None) -> list[str]:
    """机械关键词匹配（自动模式信号；题材分类低难度，关键词够用）。"""
    if proj is None:
        return []
    text = _scan_text(db, proj, data_dir)
    return [sid for sid, s in DIRECTING_SKILLS.items()
            if s["keywords"] and any(k in text for k in s["keywords"])]


def effective_directing(db, proj, data_dir=None) -> list[str]:
    """覆写优先：'off'=全关 / 'a,b'=强制（未知 id 忽略）/ 空=自动匹配。"""
    if proj is None:
        return []
    ov = ""
    try:
        ov = (proj["directing_override"] or "").strip() \
            if "directing_override" in proj.keys() else ""
    except Exception:
        ov = ""
    if ov == "off":
        return []
    if ov:
        forced = [x.strip() for x in ov.split(",") if x.strip()]
        return [x for x in forced if x in DIRECTING_SKILLS] or \
            match_directing_skills(db, proj, data_dir)
    return match_directing_skills(db, proj, data_dir)


def directing_block(db, proj, data_dir=None) -> str:
    """注入块：命中技能的中文方法论（拆解与提示词 system 共用）；未命中空串。"""
    ids = effective_directing(db, proj, data_dir)
    if not ids:
        return ""
    parts = ["【定向执导】（按项目题材自动匹配的方法论约束，写作时遵守）"]
    for sid in ids:
        parts.append(DIRECTING_SKILLS[sid]["text"])
    return "\n\n".join(parts)
