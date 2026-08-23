# tests/test_api_analyze.py
import io

from fastapi.testclient import TestClient

from comic_studio.engine.llm.provider import Usage
from comic_studio.web.app import create_app

GOOD = '{"characters":[{"name":"萧炎","appearance":"黑发少年"}],"scenes":[],"props":[]}'


class FakeLLM:
    model = "fake"

    def raw_chat(self, messages, temperature=0.3):
        return GOOD, Usage(1, 2)


def _upload(c):
    return c.post("/api/projects", data={"name": "p", "aspect_ratio": "9:16"},
                  files={"novel": ("c.txt", io.BytesIO("短文本".encode()), "text/plain")})


def test_analyze_async_flow(tmp_path, monkeypatch):
    app = create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data")
    monkeypatch.setattr("comic_studio.engine.llm.analyze.client_for_task",
                        lambda db, task: FakeLLM())
    with TestClient(app) as c:
        pid = _upload(c).json()["id"]
        resp = c.post(f"/api/projects/{pid}/analyze")
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]
        status = c.get(f"/api/projects/{pid}/analyze/status").json()
        assert status["job_id"] == job_id
        assert status["status"] in ("running", "done")  # 后台线程可能已完成
        # 轮询到完成
        import time
        for _ in range(50):
            status = c.get(f"/api/projects/{pid}/analyze/status").json()
            if status["status"] != "running":
                break
            time.sleep(0.05)
        assert status["status"] == "done"
        assert c.get(f"/api/projects/{pid}").json()["stage"] == "analyzed"


def test_conflict_while_running_or_done_guard(tmp_path, monkeypatch):
    app = create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data")
    monkeypatch.setattr("comic_studio.engine.llm.analyze.client_for_task",
                        lambda db, task: FakeLLM())
    with TestClient(app) as c:
        pid = _upload(c).json()["id"]
        assert c.post(f"/api/projects/{pid}/analyze").status_code == 202
        import time
        for _ in range(50):
            if c.get(f"/api/projects/{pid}/analyze/status").json()["status"] != "running":
                break
            time.sleep(0.05)
        # 已 analyzed 阶段再次触发 → 409（回退重跑属计划 3 的 stale 流程）
        assert c.post(f"/api/projects/{pid}/analyze").status_code == 409
        assert c.get("/api/projects/999/analyze/status").status_code == 404
