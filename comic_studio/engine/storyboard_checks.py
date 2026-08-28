# comic_studio/engine/storyboard_checks.py
"""拆解后机械审计（P7-G 第二批，借鉴 XiaoLuo/短剧厂 2026-08-28）：只告警不拦截。

① 时长守恒：设了 target_duration 时，生效镜总时长偏差 >15% 告警
② 画面换挡：超过 30s 无「景别或场景」大变化告警（防 AI 生成画面单调的量化指标，
   XiaoLuo「宏观视觉换挡 15-30s」）
③ 台词单句超 25 字告警（短剧厂语速公式：3.5-4.5 字/秒，5s 镜装不下超长句）
"""
import json


def audit_storyboard(db, project_id: int) -> list[str]:
    from .projects import get_project
    from .shots import list_shots

    proj = get_project(db, project_id)
    shots = [s for s in list_shots(db, project_id) if not s["disabled"]]
    if not shots:
        return []
    warns: list[str] = []

    total = sum(float(s["duration"]) for s in shots)
    tgt = float(proj["target_duration"] or 0) if proj else 0
    if tgt and abs(total - tgt) / tgt > 0.15:
        warns.append(f"时长守恒：生效镜总时长 {total:.0f}s 与预设 {tgt:.0f}s 偏差超 15%"
                     f"（差 {total - tgt:+.0f}s）")

    # 换挡检测：记录每次「景别或场景资产变化」的时间点，末段超 30s 告警
    change_at = [0.0]
    t, prev = 0.0, None
    for s in shots:
        cam = json.loads(s["camera_json"] or "{}")
        scenes = tuple(sorted((json.loads(s["ledger_json"] or "{}")
                               .get("assets") or {}).get("scenes") or []))
        cur = (cam.get("景别"), scenes)
        if prev is not None and cur != prev:
            change_at.append(t)
        prev = cur
        t += float(s["duration"])
    if t - change_at[-1] > 30:
        warns.append(f"画面换挡：自 {change_at[-1]:.0f}s 起超过 30s 无景别/场景大变化"
                     f"（最后一段易单调，可调整镜头语言）")

    for s in shots:
        for d in (json.loads(s["ledger_json"] or "{}").get("dialogue") or []):
            line = d.get("line") or ""
            if len(line) > 25:
                warns.append(f"镜 {s['seq']} 台词超 25 字（{len(line)} 字，自然语速"
                             f"在镜头时长内说不完）：{line[:20]}…")
    return warns
