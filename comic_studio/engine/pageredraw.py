# comic_studio/engine/pageredraw.py
"""动态漫·角色重绘（2026-09-09 设计共识）：首尾帧版本留档/整页重绘/批量编排。

版本机制（镜像 video_v{N} 惯例）：磁盘存 kf_{role}_v{N}.png，活动名
kf_{role}.png 是当前版的拷贝——渲染/fl2v/合成/查看器零改动。
尾帧派生联动：镜 i 尾帧≡镜 i+1 首帧（同一页），切 start 版本自动同步
前镜尾帧并连带置空两镜 video_path（决策 12：kf 变更→待重渲）。"""
import json
import re
from pathlib import Path

from .logbus import emit as emit_log
from .shots import get_shot, list_shots, update_shot


def kf_versions(shot_dir, role: str) -> list:
    """列 kf_{role}_v{N}.png → ["v1","v2",…]（数字序，防 v10 < v2 字符串序）。"""
    d = Path(shot_dir)
    if not d.is_dir():
        return []
    out = []
    for f in d.iterdir():
        m = re.fullmatch(r"kf_" + role + r"_v(\d+)\.png", f.name)
        if f.is_file() and m:
            out.append((int(m.group(1)), f.name))
    return [f"v{n}" for n, _ in sorted(out)]


def active_kf_version(shot_row, role: str) -> str:
    try:
        led = json.loads(shot_row["ledger_json"] or "{}")
        v = (led.get("kf_active") or {}).get(role)
        if v:
            return str(v)
    except (ValueError, TypeError):
        pass
    return "v1"


def _set_active(db, shot, role: str, version: str) -> None:
    led = json.loads(shot["ledger_json"] or "{}")
    led.setdefault("kf_active", {})[role] = version
    update_shot(db, shot["id"], {"ledger_json": json.dumps(led, ensure_ascii=False)})


def save_kf_version(shot_dir, role: str, src: Path) -> str:
    """新版本落盘（v{max+1}）并刷新活动拷贝。返回 "vN"。"""
    d = Path(shot_dir); d.mkdir(parents=True, exist_ok=True)
    n = 0
    for v in kf_versions(d, role):
        n = max(n, int(v[1:]))
    data = Path(src).read_bytes()
    (d / f"kf_{role}_v{n + 1}.png").write_bytes(data)
    (d / f"kf_{role}.png").write_bytes(data)
    return f"v{n + 1}"


def _clear_video(db, shot_ids) -> list:
    """决策 12：kf 变更的镜 video_path 置空（文件留盘），status 回待重绘。"""
    out = []
    for sid in shot_ids:
        s = get_shot(db, sid)
        if s is not None and s["video_path"]:
            update_shot(db, sid, {"video_path": None, "status": "ready"})
            out.append(sid)
    return out


def _sync_prev_end(db, data_dir, shot, data: bytes, version: str) -> int | None:
    """尾帧派生联动：镜 i 首帧变更 → 前镜（depends_on 指向者）尾帧同名版本同步。
    返回前镜 id（无前镜/非连续链返回 None）。"""
    if not shot["depends_on"]:
        return None
    prev = get_shot(db, shot["depends_on"])
    if prev is None or prev["project_id"] != shot["project_id"]:
        return None
    from .projects import get_project
    proj = get_project(db, shot["project_id"])
    if proj is None or (proj["comic_mode"] if "comic_mode" in proj.keys() else "") \
            != "motion_comic":
        return None   # 漫改/小说链无翻页联动
    d = Path(data_dir) / "projects" / proj["slug"] / "shots" / str(prev["seq"])
    (d / f"kf_end_{version}.png").write_bytes(data)
    (d / "kf_end.png").write_bytes(data)
    _set_active(db, prev, "end", version)
    return prev["id"]


def activate_kf_version(db, data_dir, shot_id: int, role: str, version: str) -> dict:
    """切活动版本：拷回活动名 + start 联动前镜尾帧 + 双镜视频置空 + ledger 记录。
    尾帧独立切版仅最后一镜（其余镜尾帧=下镜首帧派生，抛 ValueError）。"""
    if role not in ("start", "end"):
        raise ValueError("role 只能是 start/end")
    shot = get_shot(db, shot_id)
    if shot is None:
        raise ValueError(f"分镜不存在: {shot_id}")
    from .projects import get_project
    proj = get_project(db, shot["project_id"])
    shot_dir = Path(data_dir) / "projects" / proj["slug"] / "shots" / str(shot["seq"])
    src = shot_dir / f"kf_{role}_{version}.png"
    if not src.exists():
        raise ValueError(f"版本不存在: {role} {version}")
    if role == "end":
        sibs = [s for s in list_shots(db, shot["project_id"]) if s["seq"] > shot["seq"]]
        if sibs and (proj["comic_mode"] if "comic_mode" in proj.keys() else "") \
                == "motion_comic":
            raise ValueError("动态漫尾帧=下镜首帧派生，请切下镜首帧版本")
    data = src.read_bytes()
    (shot_dir / f"kf_{role}.png").write_bytes(data)
    _set_active(db, shot, role, version)
    cleared = [shot_id]
    if role == "start":
        prev_id = _sync_prev_end(db, data_dir, shot, data, version)
        if prev_id is not None:
            cleared.append(prev_id)
    ids = _clear_video(db, cleared)
    emit_log(db, "comfy", "info",
             f"分镜 {shot['seq']} 首尾帧切至 {role} {version}（视频待重渲：{len(ids)} 镜）",
             project_id=shot["project_id"])
    return {"start": active_kf_version(get_shot(db, shot_id), "start"),
            "end": active_kf_version(get_shot(db, shot_id), "end"),
            "video_cleared": ids}
