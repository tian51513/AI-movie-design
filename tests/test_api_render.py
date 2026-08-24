# tests/test_api_render.py
import io
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from comic_studio.engine.shots import persist_shots, update_shot
from comic_studio.web.app import create_app


def _client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data",
                                 start_workers=False))


def _shot(desc="推门", prompt="提示词", **kw):
    base = dict(text_span="", description=desc, shot_type="", camera={},
                duration=5.0, workflow_type="ref2va", ledger={},
                prompt=prompt,
                character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)
    base.update(kw)
    return NS(**base)


def test_render_endpoints_and_gate3(tmp_path):
    with _client(tmp_path) as c:
        pid = c.post("/api/projects", data={"name": "渲染剧", "aspect_ratio": "16:9"},
                     files={"novel": ("n.txt", io.BytesIO("文".encode()), "text/plain")}).json()["id"]
        # stage guard
        assert c.post(f"/api/projects/{pid}/render").status_code == 409
        from comic_studio.engine.projects import set_stage
        set_stage(c.app.state.db, pid, "storyboard_ready")
        ids = persist_shots(c.app.state.db, pid, [_shot(), _shot()])
        shots = c.get(f"/api/projects/{pid}/shots").json()
        assert shots[0]["video_url"] is None
        # single shot render: prompt empty reject
        update_shot(c.app.state.db, ids[0], {"prompt": ""})
        assert c.post(f"/api/shots/{ids[0]}/render").status_code == 422
        update_shot(c.app.state.db, ids[0], {"prompt": "有提示词"})
        assert c.post(f"/api/shots/{ids[0]}/render").status_code == 202
        assert c.post(f"/api/shots/{ids[0]}/render").status_code == 409  # dedup
        # batch: shot 1 already queued, shot 2 no video -> enqueue 1
        r = c.post(f"/api/projects/{pid}/render")
        assert r.status_code == 202 and r.json()["enqueued"] == 1
        # gate3: no video -> 422
        assert c.post(f"/api/projects/{pid}/gate3").status_code == 422
        for i in ids:
            update_shot(c.app.state.db, i, {"video_path": f"projects/x/shots/{i}/video.mp4",
                                            "status": "rendered"})
        body = c.get(f"/api/projects/{pid}/shots").json()
        assert body[0]["video_url"].startswith("/media/projects/")
        assert c.post(f"/api/projects/{pid}/gate3").status_code == 200
        assert c.get(f"/api/projects/{pid}").json()["stage"] == "rendered"


def test_media_serves_video_url(tmp_path):
    with _client(tmp_path) as c:
        (tmp_path / "data" / "projects" / "x" / "shots" / "1").mkdir(parents=True)
        v = tmp_path / "data" / "projects" / "x" / "shots" / "1" / "video.mp4"
        v.write_bytes(b"\x00\x00")
        r = c.get("/media/projects/x/shots/1/video.mp4")
        assert r.status_code == 200 and len(r.content) == 2
