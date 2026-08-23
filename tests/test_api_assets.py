# tests/test_api_assets.py
import io
import json

from fastapi.testclient import TestClient

from comic_studio.engine.llm.provider import Usage
from comic_studio.web.app import create_app

GOOD = '{"characters":[{"name":"萧炎","appearance":"黑发少年"}],"scenes":[],"props":[]}'


class FakeLLM:
    model = "fake"

    def raw_chat(self, messages, temperature=0.3):
        return GOOD, Usage(1, 2)


def test_assets_endpoint(tmp_path, monkeypatch):
    app = create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data", start_workers=False)
    monkeypatch.setattr("comic_studio.engine.llm.analyze.client_for_task",
                        lambda db, task: FakeLLM())
    with TestClient(app) as c:
        pid = c.post("/api/projects", data={"name": "p", "aspect_ratio": "9:16"},
                     files={"novel": ("c.txt", io.BytesIO("短".encode()), "text/plain")}).json()["id"]
        c.post(f"/api/projects/{pid}/analyze")
        import time
        for _ in range(50):
            if c.get(f"/api/projects/{pid}/analyze/status").json()["status"] != "running":
                break
            time.sleep(0.05)
        assets = c.get(f"/api/projects/{pid}/assets").json()
        assert assets == [{"id": assets[0]["id"], "kind": "character",
                           "name": "萧炎", "detail": "黑发少年", "tags": []}]


def test_frontend_served(tmp_path):
    app = create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data", start_workers=False)
    with TestClient(app) as c:
        resp = c.get("/")
        assert resp.status_code == 200
        assert "vue" in resp.text.lower() or "comic_studio" in resp.text
        v = c.get("/vendor/vue.global.prod.js")
        assert v.status_code == 200 and len(v.content) > 100000
