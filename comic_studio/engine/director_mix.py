# comic_studio/engine/director_mix.py
"""P7-J 快车道整片混音：帧数轴 TTS 音轨替换。

快车道成片 = 多批拼接的单文件，无逐镜中间产物可走 P6 的 merge 混音路径；
本模块按 spans（(seq, start_sec, dur_sec, tts_path|None)，帧数/24 精确轴）
重建整条音轨：有台词镜 → dialogue.mp3 补齐到镜长（与逐镜 _replace_audio 同
语义）；无台词镜 → 从原声切对应时段保留（H3 原生环境音/音效不丢）。
画面流恒为 copy，只换音轨。"""
import subprocess
from pathlib import Path

from .merge import ffmpeg_bin

_UNIFY = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"


def mix_director_audio(video: Path, spans: list, output: Path) -> Path:
    """spans 覆盖整片时间轴且按时间顺序；tts 为 None 的镜保留原声切片。
    全部无台词 → 原样返回 video（不折腾）。"""
    tts_input_idx = {}  # span 下标 → ffmpeg 输入序号（0=原视频，1..=TTS 文件）
    inputs = ["-i", str(video)]
    for s in spans:
        if s[3] is not None:
            tts_input_idx[id(s)] = len(tts_input_idx) + 1
            inputs += ["-i", str(s[3])]
    if not tts_input_idx:
        return video

    parts = []
    for idx, (seq, start, dur, tts) in enumerate(spans):
        if tts is not None:
            ai = tts_input_idx[id(spans[idx])]
            parts.append(f"[{ai}:a]{_UNIFY},apad=whole_dur={dur:.3f},"
                         f"atrim=0:{dur:.3f},asetpts=PTS-STARTPTS[a{idx}]")
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
