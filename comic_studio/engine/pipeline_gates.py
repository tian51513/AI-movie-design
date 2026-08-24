# comic_studio/engine/pipeline_gates.py
"""阶段门禁（spec §5）：engine 侧统一实现，routes 手动门与 autopilot 自动门共用。"""
from .assets import list_project_assets
from .logbus import emit as emit_log
from .paths import data_to_abs
from .projects import get_project, set_stage
from .shots import list_shots

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

GATE_STAGES = {1: ("analyzed", "assets_ready"),
               2: ("assets_ready", "storyboard_ready"),
               3: ("storyboard_ready", "rendered")}


class GateStageError(ValueError):
    """阶段不符（HTTP 层映射 409；autopilot 视为留给下一轮）。"""


def has_views(views_dir) -> bool:
    """目录内存在任一图片文件（注意必须双层展开——any(glob(...) for e) 是生成器恒真，
    2026-08-25 真机教训：autopilot 因此恒判「资产齐全」卡死过门1）。"""
    return views_dir.is_dir() and any(
        f for ext in IMAGE_EXTS for f in views_dir.glob(f"*{ext}"))


def gate_pass(db, data_dir, project_id: int, n: int, source: str = "确认") -> None:
    """过门 n（1/2/3）：条件不满足 raise（GateStageError=阶段不符，ValueError=缺件）。"""
    proj = get_project(db, project_id)
    if proj is None:
        raise ValueError("项目不存在")
    if n not in GATE_STAGES:
        raise ValueError(f"未知门禁 {n}")
    need, to = GATE_STAGES[n]
    if proj["stage"] != need:
        raise GateStageError(f"阶段 {proj['stage']} 不能过门{n}（需 {need}）")
    if n == 1:
        missing = [a["name"] for a in list_project_assets(db, project_id)
                   if not has_views(data_to_abs(data_dir, a["library_dir"]) / "views")]
        if missing:
            raise ValueError(f"以下资产还没有参考图: {missing}")
    else:
        shots = list_shots(db, project_id)
        if not shots:
            raise ValueError("尚无分镜，请先拆分镜")
        if n == 2:
            missing = [s["seq"] for s in shots if not (s["prompt"] or "").strip()]
            label = "缺提示词的镜头"
        else:
            missing = [s["seq"] for s in shots if not s["video_path"]]
            label = "缺视频的镜头"
        if missing:
            raise ValueError(f"{label}: {missing}")
    set_stage(db, project_id, to)
    emit_log(db, "system", "info", f"阶段流转 {need} → {to}（门{n} {source}）",
             project_id=project_id)
