# comic_studio/engine/director_mix.py
"""P7-J 快车道整片混音：帧数轴 TTS 音轨替换。

快车道成片 = 多批拼接的单文件，无逐镜中间产物可走 P6 的 merge 混音路径；
本模块按 spans（(seq, start_sec, dur_sec, tts_path|None)，帧数/24 精确轴）
重建整条音轨：有台词镜 → dialogue.mp3 补齐到镜长（与逐镜 _replace_audio 同
语义）；无台词镜 → 从原声切对应时段保留（H3 原生环境音/音效不丢）。
画面流恒为 copy，只换音轨。"""
import subprocess
from pathlib import Path

from .merge import ffmpeg_bin, probe

_UNIFY = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"


def warn_long_tts(db, project_id, spans: list) -> None:
    """M8（2026-09-05 审计）：快车道 spans 帧数轴未适配音频收口——TTS 长于
    段会被 apad+atrim 硬截断；最小可见化 warn（完整收口需帧数轴重切，待决策）。"""
    from .logbus import emit as emit_log
    for seq, _st, dur, tts in spans:
        if tts is None or not Path(tts).exists():
            continue
        try:
            a = probe(Path(tts))["duration"]
        except Exception:
            continue
        if a > dur + 0.5:
            emit_log(db, "merge", "warn",
                     f"快车道镜 {seq}：配音 {a:.1f}s 长于段 {dur:.1f}s，将被截断"
                     "（快车道未适配音频收口；缩短台词或改逐镜合成）",
                     project_id=project_id)


def mix_director_audio(video: Path, spans: list, output: Path,
                       mute_quiet: bool = False, db=None, project_id=None) -> Path:
    """spans 覆盖整片时间轴且按时间顺序；tts 为 None 的镜默认保留原声切片，
    mute_quiet=True 时改为静音（2026-08-30：封死 H3 残留杂音进成片，代价是丢
    自然环境声）。全部无台词且不静音 → 原样返回 video（不折腾）。"""
    if db is not None and project_id is not None:
        warn_long_tts(db, project_id, spans)
    tts_input_idx = {}  # span 下标 → ffmpeg 输入序号（0=原视频，1..=TTS 文件）
    inputs = ["-i", str(video)]
    for s in spans:
        if s[3] is not None:
            tts_input_idx[id(s)] = len(tts_input_idx) + 1
            inputs += ["-i", str(s[3])]
    if not tts_input_idx and not mute_quiet:
        return video

    parts = []
    for idx, (seq, start, dur, tts) in enumerate(spans):
        if tts is not None:
            ai = tts_input_idx[id(spans[idx])]
            parts.append(f"[{ai}:a]{_UNIFY},apad=whole_dur={dur:.3f},"
                         f"atrim=0:{dur:.3f},asetpts=PTS-STARTPTS[a{idx}]")
        elif mute_quiet:
            parts.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{dur:.3f},"
                         f"asetpts=PTS-STARTPTS[a{idx}]")
        else:
            parts.append(f"[0:a]{_UNIFY},atrim=start={start:.3f}:end={start + dur:.3f},"
                         f"asetpts=PTS-STARTPTS[a{idx}]")
    concat = ("".join(f"[a{i}]" for i in range(len(spans)))
              + f"concat=n={len(spans)}:v=0:a=1[aout]")
    cmd = [ffmpeg_bin(), "-y", *inputs,
           "-filter_complex", ";".join(parts + [concat]),
           "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac",
           str(output)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=600)
    return output
