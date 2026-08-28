# tests/test_director_mix.py
"""P7-J 快车道整片混音：帧数轴 TTS 音轨替换 + SRT 烧录（真 ffmpeg）。"""
import subprocess
from pathlib import Path

from comic_studio.engine.merge import ffmpeg_bin


def _make_video(src: Path, seconds=4):
    subprocess.run([ffmpeg_bin(), "-y", "-f", "lavfi", "-i",
                    f"testsrc=duration={seconds}:size=320x240:rate=10",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac",
                    str(src)], check=True, capture_output=True, timeout=60)
    return src


def _make_speech(src: Path, seconds=1):
    subprocess.run([ffmpeg_bin(), "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=880:duration={seconds}",
                    "-c:a", "libmp3lame", str(src)], check=True, capture_output=True,
                   timeout=60)
    return src


def test_mix_director_audio_replaces_tts_spans(tmp_path):
    """镜1（0-2s）无台词→保留原声切片；镜2（2-4s）有配音→音轨替换+补齐时长；
    产出总时长≈4s，画面保留。"""
    from comic_studio.engine.director_mix import mix_director_audio
    video = _make_video(tmp_path / "in.mp4", 4)
    tts2 = _make_speech(tmp_path / "tts2.mp3", 1)  # 1s 配音补齐到 2s
    spans = [(1, 0.0, 2.0, None), (2, 2.0, 2.0, tts2)]
    out = mix_director_audio(video, spans, tmp_path / "mixed.mp4")
    assert out.exists() and out.stat().st_size > 0
    from comic_studio.engine.merge import probe
    p = probe(out)
    assert 3.8 <= p["duration"] <= 4.3  # 总长保持
    # 全无台词 → 原样返回（不折腾）
    none_out = mix_director_audio(video, [(1, 0.0, 4.0, None)], tmp_path / "keep.mp4")
    assert none_out == video
