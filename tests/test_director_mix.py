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


def test_mix_director_audio_mutes_quiet_spans(tmp_path):
    """无台词镜静音（2026-08-30 杂音封堵）：mute_quiet=True 时无台词镜输出静音、
    有台词镜配音不受影响；全无台词也不再原样返回（出全静音片）。"""
    import re as _re
    from comic_studio.engine.director_mix import mix_director_audio

    def mean_volume(path, dur=None):
        cmd = [ffmpeg_bin(), "-i", str(path)]
        if dur: cmd += ["-t", str(dur)]
        cmd += ["-af", "volumedetect", "-f", "null", "-"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        m = [l for l in (r.stderr or "").splitlines() if "mean_volume" in l]
        assert m, (r.stderr or r.stdout)[-400:]
        return float(_re.search(r"mean_volume:\s*(-?[\d.]+)", m[0]).group(1))

    video = _make_video(tmp_path / "in_m.mp4", 4)  # 全程 440Hz 正弦（响）
    tts2 = _make_speech(tmp_path / "tts_m.mp3", 1)
    spans = [(1, 0.0, 2.0, None), (2, 2.0, 2.0, tts2)]
    out = mix_director_audio(video, spans, tmp_path / "muted.mp4", mute_quiet=True)
    assert out.exists()
    assert mean_volume(out, dur=1.8) < -60   # 镜1（无台词）→ 静音
    assert mean_volume(out) > -45            # 镜2 有配音（全静音约 -91，均值被半片静音拉低）
    # 全无台词 + mute_quiet → 不原样返回，静音成片
    allq = mix_director_audio(video, [(1, 0.0, 4.0, None)], tmp_path / "allq.mp4",
                              mute_quiet=True)
    assert allq != video and allq.exists() and mean_volume(allq) < -60
