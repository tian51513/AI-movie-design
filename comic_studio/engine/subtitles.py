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
            # 音频收口镜像（2026-09-05）：对白镜段长=配音+0.5 呼吸（与 merge
            # _replace_audio 严格一致）；其余镜按真实视频时长（duration 字段
            # ≠实际渲染时长——17k+5 帧对齐 4.0→4.5/5.0→5.2，46 镜累计漂 ~9s）
            eff = None
            if dialogue and not ledger.get("h3_native_voice"):
                mp3 = data_to_abs(data_dir, f"projects/{proj['slug']}/shots/{shot['seq']}") / "dialogue.mp3"
                if mp3.exists():
                    try:
                        from .merge import BREATH_SEC, probe
                        eff = probe(mp3)["duration"] + BREATH_SEC
                    except Exception:
                        eff = None
            if eff is None:
                eff = float(shot["duration"] or 5.0)
                vp = shot["video_path"]
                if vp:
                    vf = Path(data_dir) / vp
                    if vf.exists():
                        try:
                            from .merge import probe
                            eff = probe(vf)["duration"] or eff
                        except Exception:
                            pass
            start_base, duration = timeline, eff

        if dialogue:
            # C7 按字数比例分时长（2026-09-01 台词组拆镜配套）：一镜 3~8 句后
            # 均分会让短句占长、长句赶读——字数即朗读时长的先验
            weights = [max(1, len((d.get("line") or "").strip())) for d in dialogue]
            total_w = sum(weights)
            cursor = 0.0
            for d, w in zip(dialogue, weights):
                seg = duration * w / total_w
                start = start_base + cursor
                end = start_base + cursor + seg
                cursor += seg
                line = d.get("line", "").strip()
                speaker = d.get("speaker", "")
                if line:
                    entries.append((start, end, speaker, line))

        if span_map is None:
            # M9a（2026-09-05 审计）：xfade 开启时段间交叠 0.3s——轴同步扣减
            #（此前累计漂 ~0.3s/镜）
            _fade = 0.0
            try:
                from .merge import XFADE_SEC
                from .settings import get_setting
                if (get_setting(db, "comfy") or {}).get("merge_xfade"):
                    _fade = XFADE_SEC
            except Exception:
                pass
            timeline += duration - _fade

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
