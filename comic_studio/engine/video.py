# comic_studio/engine/video.py
"""ffmpeg 工具：静态二进制来自 imageio-ffmpeg（spec §10 复用）。"""
import subprocess
from pathlib import Path


def ffmpeg_bin() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def extract_last_frame(video_path: Path, out_png: Path, timeout: int = 30) -> Path:
    """抽取视频末帧为 PNG（首尾帧衔接用，spec §8.4）。"""
    out_png.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [ffmpeg_bin(), "-y", "-sseof", "-0.1", "-i", str(video_path),
         "-update", "1", "-frames:v", "1", str(out_png)],
        capture_output=True, timeout=timeout, text=True)
    if r.returncode != 0 or not out_png.exists():
        raise RuntimeError(f"末帧抽取失败: {(r.stderr or '')[-200:]}")
    return out_png
