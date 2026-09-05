# tests/test_api_merge.py
"""一键出片后端接口（计划5B 任务4）：autopilot 开关/详情动作、merge 发起与列表。"""
from fastapi.testclient import TestClient

from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project, set_stage
from comic_studio.web.app import create_app


def _client(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "一键剧", "16:9", "正文")["id"]
    app = create_app(tmp_path / "s.db", tmp_path / "data", start_workers=False)
    return db, pid, TestClient(app)


def test_autopilot_patch_and_detail_action(tmp_path):
    db, pid, c = _client(tmp_path)
    with c:
        r = c.patch(f"/api/projects/{pid}", json={"autopilot": True})
        assert r.status_code == 200 and r.json()["autopilot"] == 1
        d = c.get(f"/api/projects/{pid}").json()
        assert d["autopilot"] == 1
        assert d["autopilot_action"]["action"] == "analyze"  # 当前动作角标
        r = c.patch(f"/api/projects/{pid}", json={"autopilot": False})
        assert r.json()["autopilot"] == 0
        d = c.get(f"/api/projects/{pid}").json()
        assert "autopilot_action" not in d  # 关闭时不计算


def test_merge_guard_dedupe_and_listing(tmp_path):
    db, pid, c = _client(tmp_path)
    with c:
        # 非 rendered 阶段 → 409
        assert c.post(f"/api/projects/{pid}/merge").status_code == 409
        set_stage(db, pid, "rendered")
        r = c.post(f"/api/projects/{pid}/merge")
        assert r.status_code == 202 and "job_id" in r.json()
        # 队列去重 → 409
        assert c.post(f"/api/projects/{pid}/merge").status_code == 409
        # 产物列表：output 目录扫描
        assert c.get(f"/api/projects/{pid}/merges").json() == []
        out_dir = tmp_path / "data" / "projects" / "一键剧" / "output"
        out_dir.mkdir(parents=True)
        (out_dir / "ep001.mp4").write_bytes(b"x")
        merges = c.get(f"/api/projects/{pid}/merges").json()
        assert len(merges) == 1 and merges[0]["file"] == "ep001.mp4"
        assert merges[0]["url"].startswith("/media/")


def test_remerge_allowed_when_merged(tmp_path):
    """2026-09-05 真机需求：修复音频参数后要重出片，但 merged 阶段无入口
    （ep006 音频废、修复在合成链上）。merged 允许再次发起 → 出新 epNNN。"""
    db, pid, c = _client(tmp_path)
    with c:
        set_stage(db, pid, "merged")
        r = c.post(f"/api/projects/{pid}/merge")
        assert r.status_code == 202 and "job_id" in r.json()
        # 队列去重守卫照常生效
        assert c.post(f"/api/projects/{pid}/merge").status_code == 409


def test_tts_endpoint_rejects_concurrent(tmp_path, monkeypatch):
    """M7（2026-09-05 审计）：POST /tts 无去重锁——与 autopilot/手动并发双烧
    ComfyUI、覆写同一 mp3。同项目并发生成 → 409。"""
    import io
    import threading
    import time as _t
    from pathlib import Path
    from comic_studio.engine.projects import set_stage

    def slow_tts(db, dd, pid):
        _t.sleep(0.5)
        return []

    monkeypatch.setattr("comic_studio.engine.tts.generate_dialogue_audio", slow_tts)
    monkeypatch.setattr("comic_studio.engine.subtitles.generate_srt",
                        lambda db, dd, pid: Path("/tmp/x.srt"))
    with TestClient(create_app(tmp_path / "t.db", tmp_path / "data",
                               start_workers=False)) as c:
        r = c.post("/api/projects", data={"name": "锁剧", "aspect_ratio": "16:9"},
                   files={"novel": ("n.txt", io.BytesIO("正文".encode()), "text/plain")})
        pid = r.json()["id"]
        set_stage(Database(tmp_path / "t.db"), pid, "rendered")
        t = threading.Thread(target=lambda: c.post(f"/api/projects/{pid}/tts"))
        t.start()
        _t.sleep(0.15)
        r2 = c.post(f"/api/projects/{pid}/tts")
        assert r2.status_code == 409
        t.join()
