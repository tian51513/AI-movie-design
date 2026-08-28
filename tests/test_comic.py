# tests/test_comic.py
"""P8 漫画→视频（2026-08-29）：每图一镜复用 fl2v 链路（页 i=首帧/页 i+1=尾帧）。"""
import io

from fastapi.testclient import TestClient

from comic_studio.engine.db import Database
from comic_studio.web.app import create_app

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64  # 假 PNG 字节（导入不解码，原样落盘）


def test_import_comic_creates_fl2v_shots(tmp_path):
    from comic_studio.engine.comic import import_comic
    db = Database(tmp_path / "s.db"); db.migrate()
    images = [f"page{i:02d}.png" for i in range(3)]
    blobs = [PNG * (i + 1) for i in range(3)]
    pid = import_comic(db, tmp_path / "data", "漫画剧", "9:16",
                       list(zip(images, blobs)))["id"]
    from comic_studio.engine.projects import get_project
    from comic_studio.engine.shots import list_shots
    proj = get_project(db, pid)
    assert proj["stage"] == "storyboard_ready"  # 跳过分析/拆解，直达提示词阶段
    shots = list_shots(db, pid)
    assert len(shots) == 3
    assert all(s["workflow_type"] == "fl2v" for s in shots)
    # 关键帧落位：页 i = 镜 i 首帧；页 i+1 = 镜 i 尾帧（最后一镜无尾帧）
    from comic_studio.engine.paths import data_to_abs
    for i, s in enumerate(shots, 1):
        d = data_to_abs(tmp_path / "data", f"projects/漫画剧/shots/{i}")
        assert (d / "kf_start.png").read_bytes() == PNG * i
        if i < 3:
            assert (d / "kf_end.png").read_bytes() == PNG * (i + 1)
        else:
            assert not (d / "kf_end.png").exists()
    # novel 占位（链路兼容）
    assert data_to_abs(tmp_path / "data", proj["novel_path"]).exists()


def test_describe_shots_vision_calls(tmp_path, monkeypatch):
    """VLM 读图：多模态消息（image_url base64）→ 每镜提示词落库。"""
    from comic_studio.engine.comic import import_comic, describe_shots
    from comic_studio.engine.llm.provider import LLMClient, Usage
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = import_comic(db, tmp_path / "data", "读图剧", "9:16",
                       [("p1.png", PNG), ("p2.png", PNG)])["id"]
    seen = []

    class FakeVision(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "nsfwvision")
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            seen.append(messages)
            return "少年推开门，画面延续至下一格：他走进房间。", Usage(10, 20)

    n = describe_shots(db, tmp_path / "data", pid, FakeVision())
    assert n == 2
    from comic_studio.engine.shots import list_shots
    rows = list_shots(db, pid)
    assert all(r["prompt"].startswith("少年推开") for r in rows)
    # 多模态消息：含 image_url base64（首帧与尾帧两张）
    content = seen[0][-1]["content"]
    assert isinstance(content, list)
    kinds = [c.get("type") for c in content]
    assert "text" in kinds and kinds.count("image_url") >= 1


def test_from_comic_api(tmp_path):
    with TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data",
                               start_workers=False)) as c:
        r = c.post("/api/projects/from-comic",
                   data={"name": "接口剧", "aspect_ratio": "16:9"},
                   files=[("images", ("a.png", io.BytesIO(PNG), "image/png")),
                          ("images", ("b.png", io.BytesIO(PNG * 2), "image/png"))])
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        r2 = c.get(f"/api/projects/{pid}/shots")
        assert len(r2.json()) == 2
