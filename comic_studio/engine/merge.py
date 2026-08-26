# comic_studio/engine/merge.py
"""FFmpeg 合成：归一化 + concat（spec §10）。二进制来自 imageio-ffmpeg。"""
import json
import re
import subprocess
from pathlib import Path


def ffmpeg_bin() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def probe(path: Path) -> dict:
    """ffmpeg -i 解析 stderr（imageio-ffmpeg 不带 ffprobe）。"""
    r = subprocess.run([ffmpeg_bin(), "-i", str(path)], capture_output=True, timeout=60, text=True)
    info = r.stderr or ""
    m = re.search(r"Duration: (\d+):(\d+):(\d+)\.(\d+)", info)
    duration = 0.0
    if m:
        duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 100
    w = h = 0
    ms = re.findall(r"(\d{2,5})x(\d{2,5})", info)
    if ms:
        w, h = int(ms[-1][0]), int(ms[-1][1])
    fps = 0.0
    fm = re.search(r"([\d.]+) fps", info)
    if fm:
        fps = float(fm.group(1))
    return {"duration": duration, "width": w, "height": h, "fps": fps}


def normalize(src: Path, dst: Path, w: int, h: int, fps: float) -> Path:
    """归一化：scale+pad 到画布、统一 fps、crf18 yuv420p、补静音音轨保 concat 一致。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg_bin(), "-y", "-i", str(src),
         "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
               f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={fps}",
         "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
         "-f", "mp4", str(dst)],
        check=True, capture_output=True, timeout=300)
    return dst


def concat(parts: list, out: Path) -> Path:
    """concat demuxer -c copy；失败回退逐段拼接重编码。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    lst = out.parent / "concat_list.txt"
    lst.write_text("\n".join(f"file '{Path(p).resolve().as_posix()}'" for p in parts),
                   encoding="utf-8")
    r = subprocess.run(
        [ffmpeg_bin(), "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c", "copy", str(out)],
        capture_output=True, timeout=300)
    if r.returncode == 0 and out.exists():
        return out
    # 回退：逐段 concat filter 重编码
    args = [ffmpeg_bin(), "-y"]
    for p in parts:
        args += ["-i", str(p)]
    n = len(parts)
    filt = "".join(f"[{i}:0][{i}:1] " for i in range(n)) + \
        f"concat=n={n}:v=1:a=1[v][a]"
    args += ["-filter_complex", filt, "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
             "-c:a", "aac", str(out)]
    subprocess.run(args, check=True, capture_output=True, timeout=600)
    return out


def _canvas(aspect_ratio: str) -> tuple:
    return (1920, 1080) if aspect_ratio == "16:9" else (1080, 1920)


def _replace_audio(video: Path, audio: Path, output: Path) -> Path:
    """P6：TTS 音轨替换——视频画面保留，音频换为 TTS 配音。"""
    subprocess.run([ffmpeg_bin(), "-y", "-i", str(video), "-i", str(audio),
                    "-map", "0:v", "-map", "1:a",
                    "-c:v", "copy", "-c:a", "aac", "-shortest",
                    str(output)],
                   check=True, capture_output=True, timeout=300)
    return output


def _burn_subtitles(video: Path, srt: Path) -> None:
    """P6：SRT 字幕烧入成片（原地覆盖）。"""
    tmp = video.with_suffix(".sub_tmp.mp4")
    style = ("FontName=SimSun,FontSize=22,PrimaryColour=&H00FFFFFF&,"
             "OutlineColour=&H00000000&,Outline=2,Bold=1,MarginV=25")
    subprocess.run([ffmpeg_bin(), "-y", "-i", str(video),
                    "-vf", f"subtitles={srt}:force_style='{style}'",
                    "-c:a", "copy", str(tmp)],
                   check=True, capture_output=True, timeout=600)
    tmp.replace(video)


def merge_project(db, data_dir, project_id, job_id=None) -> Path:
    """按 seq 收集选用视频 → 归一化 → concat → output/epNNN.mp4；置 stage=merged。"""
    from .logbus import emit as emit_log
    from .projects import get_project, set_stage
    from .shots import list_shots

    proj = get_project(db, project_id)
    if proj is None:
        raise ValueError(f"项目不存在: {project_id}")
    shots = list_shots(db, project_id)
    missing = [s["seq"] for s in shots if not s["video_path"]]
    if not shots or missing:
        raise ValueError(f"无法合成：以下镜头无视频: {missing or '（无分镜）'}")
    w, h = _canvas(proj["aspect_ratio"])
    out_dir = Path(data_dir) / "projects" / proj["slug"] / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    n = len(list(out_dir.glob("ep*.mp4"))) + 1
    out = out_dir / f"ep{n:03d}.mp4"
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        parts = []
        for s in shots:
            src = Path(data_dir) / s["video_path"]
            if not src.exists():
                raise ValueError(f"镜头 {s['seq']} 视频文件缺失: {src}")
            part = normalize(src, td / f"{s['seq']:04d}.mp4", w, h, 25)
            # P6：TTS 音轨替换（dialogue.mp3 存在时替换 H3 原生音频）
            tts_audio = src.parent / "dialogue.mp3"
            if tts_audio.exists():
                tts_part = td / f"{s['seq']:04d}_tts.mp4"
                _replace_audio(part, tts_audio, tts_part)
                part = tts_part
            parts.append(part)
        concat(parts, out)

    # P6：字幕烧录（subtitles.srt 存在时烧入成片）
    srt = out_dir / "subtitles.srt"
    if srt.exists():
        _burn_subtitles(out, srt)
    set_stage(db, project_id, "merged")
    emit_log(db, "merge", "info", f"成片合成完成 → {out.name}（{len(shots)} 镜）",
             project_id=project_id, job_id=job_id)
    return out


def register_merge_handler():
    """merge 的 worker handler 注册（延迟导入避免环）。"""
    from .queue.worker import register

    @register("merge")
    def handle_merge(db, data_dir, job, comfy):
        payload = json.loads(job["payload_json"] or "{}")
        merge_project(db, data_dir, payload.get("project_id", job["project_id"]),
                      job_id=job["id"])
    return handle_merge
