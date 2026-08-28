# comic_studio/engine/subtitles.py
"""P6 Task 2：SRT 字幕生成——从 dialogue + 分镜时长计算时间戳（2026-08-27）。

时间戳规则：镜 N 的起始 = 前 N-1 镜时长之和；镜内多句均分镜时长。
"""
import json
from pathlib import Path

from .logbus import emit as emit_log
from .paths import data_to_abs
from .projects import get_project
from .shots import list_shots


def _fmt_srt_time(seconds: float) -> str:
    """秒 → SRT 时间格式 HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def generate_srt(db, data_dir, project_id, spans=None) -> Path:
    """为项目所有含台词的分镜生成 SRT 字幕文件。
    产出 projects/<slug>/output/subtitles.srt
    spans：快车道帧数轴（[(seq, start_sec, dur_sec)]，2026-08-29 混音需求——
    导演台实际时长=帧数/24，与 duration 有对齐漂移且累计；不传则按 duration）。"""
    proj = get_project(db, project_id)
    if proj is None:
        raise ValueError(f"项目不存在: {project_id}")

    shots = list_shots(db, project_id)
    span_map = {s[0]: (s[1], s[2]) for s in spans} if spans else None
    entries = []  # [(start, end, speaker, line)]
    timeline = 0.0  # 当前时间轴位置（秒）

    for shot in shots:
        ledger = json.loads(shot["ledger_json"] or "{}")
        dialogue = ledger.get("dialogue") or []
        if span_map is not None and shot["seq"] in span_map:
            start_base, duration = span_map[shot["seq"]]
        else:
            start_base, duration = timeline, float(shot["duration"] or 5.0)

        if dialogue:
            # 镜内多句均分时长
            n = len(dialogue)
            per = duration / n
            for i, d in enumerate(dialogue):
                start = start_base + i * per
                end = start_base + (i + 1) * per
                line = d.get("line", "").strip()
                speaker = d.get("speaker", "")
                if line:
                    entries.append((start, end, speaker, line))

        if span_map is None:
            timeline += duration

    # 写 SRT
    out_dir = data_to_abs(data_dir, f"projects/{proj['slug']}/output")
    out_dir.mkdir(parents=True, exist_ok=True)
    srt_path = out_dir / "subtitles.srt"

    lines = []
    for idx, (start, end, speaker, text) in enumerate(entries, 1):
        lines.append(str(idx))
        lines.append(f"{_fmt_srt_time(start)} --> {_fmt_srt_time(end)}")
        lines.append(f"{speaker}：{text}" if speaker else text)
        lines.append("")

    srt_path.write_text("\n".join(lines), encoding="utf-8")
    emit_log(db, "subtitles", "info",
             f"字幕生成完成：{len(entries)} 条 → {srt_path.name}",
             project_id=project_id)
    return srt_path
