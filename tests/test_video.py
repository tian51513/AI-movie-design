# tests/test_video.py
import subprocess
from pathlib import Path

import pytest

from comic_studio.engine.video import extract_last_frame, ffmpeg_bin


def _make_test_video(path: Path) -> Path:
    subprocess.run([ffmpeg_bin(), "-y", "-f", "lavfi", "-i",
                    "testsrc=duration=1:size=320x240:rate=10",
                    "-pix_fmt", "yuv420p", str(path)],
                   check=True, capture_output=True, timeout=60)
    return path


def test_extract_last_frame(tmp_path):
    vid = _make_test_video(tmp_path / "t.mp4")
    assert vid.stat().st_size > 0
    out = extract_last_frame(vid, tmp_path / "last.png")
    assert out.exists() and out.stat().st_size > 0


def test_extract_failure_raises(tmp_path):
    bad = tmp_path / "not_video.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(RuntimeError):
        extract_last_frame(bad, tmp_path / "x.png")
