# tests/test_api_shots.py
import io
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from comic_studio.engine.shots import persist_shots
from comic_studio.web.app import create_app


def _client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data",
                                 start_workers=False))


def _mk(c, name="分镜剧"):
    pid = c.post("/api/projects", data={"name": name, "aspect_ratio": "9:16"},
                 files={"novel": ("n.txt", io.BytesIO("正文".encode()), "text/plain")}).json()["id"]
    return pid


def _shot(desc="推门", **kw):
    base = dict(text_span="", description=desc, shot_type="动作", camera={"景别": "中景"},
                duration=5.0, workflow_type="ref2va", ledger={},
                character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)
    base.update(kw)
    return NS(**base)


def test_split_endpoint_guard_and_shots_listing(tmp_path):
    with _client(tmp_path) as c:
        pid = _mk(c)
        # stage=created → 409
        assert c.post(f"/api/projects/{pid}/split-storyboards").status_code == 409
        from comic_studio.engine.projects import set_stage
        set_stage(c.app.state.db, pid, "assets_ready")
        r = c.post(f"/api/projects/{pid}/split-storyboards")
        assert r.status_code == 202
        assert c.post(f"/api/projects/{pid}/split-storyboards").status_code == 409  # 拆解中
        # 直插 shots 供列表/PATCH/gate2 测试
        persist_shots(c.app.state.db, pid, [_shot(), _shot(desc="特写", workflow_type="fl2v")])
        shots = c.get(f"/api/projects/{pid}/shots").json()
        assert [s["seq"] for s in shots] == [1, 2]
        assert shots[0]["camera"]["景别"] == "中景"
        p = c.patch("/api/shots/%d" % shots[0]["id"], json={"prompt": "人工提示词"})
        assert p.status_code == 200
        assert any(s["prompt"] == "人工提示词" and s["status"] == "ready"
                   for s in c.get(f"/api/projects/{pid}/shots").json())


def test_gate2_requires_all_prompts(tmp_path):
    with _client(tmp_path) as c:
        pid = _mk(c)
        from comic_studio.engine.projects import set_stage
        set_stage(c.app.state.db, pid, "assets_ready")
        assert c.post(f"/api/projects/{pid}/gate2").status_code == 422  # 无分镜
        persist_shots(c.app.state.db, pid, [_shot(), _shot()])
        r = c.post(f"/api/projects/{pid}/gate2")
        assert r.status_code == 422 and "1" in r.json()["detail"] and "2" in r.json()["detail"]
        shots = c.get(f"/api/projects/{pid}/shots").json()
        for s in shots:
            c.patch(f"/api/shots/{s['id']}", json={"prompt": f"提示{s['seq']}"})
        assert c.post(f"/api/projects/{pid}/gate2").status_code == 200
        assert c.get(f"/api/projects/{pid}").json()["stage"] == "storyboard_ready"
        assert c.post(f"/api/projects/{pid}/gate2").status_code == 409


def test_patch_shot_duration_validation(tmp_path):
    """附带2: duration 非法值返回 422。"""
    with _client(tmp_path) as c:
        pid = _mk(c)
        from comic_studio.engine.projects import set_stage
        set_stage(c.app.state.db, pid, "assets_ready")
        ids = persist_shots(c.app.state.db, pid, [_shot()])
        sid = ids[0]
        r0 = c.patch(f"/api/shots/{sid}", json={"duration": ""})
        assert r0.status_code == 422
        r1 = c.patch(f"/api/shots/{sid}", json={"duration": 0})
        assert r1.status_code == 422
        r2 = c.patch(f"/api/shots/{sid}", json={"duration": 16})
        assert r2.status_code == 422


def test_regen_prompt_force_semantics(tmp_path):
    with _client(tmp_path) as c:
        pid = _mk(c)
        from comic_studio.engine.projects import set_stage
        set_stage(c.app.state.db, pid, "assets_ready")
        ids = persist_shots(c.app.state.db, pid, [_shot()])
        sid = ids[0]
        assert c.post(f"/api/shots/{sid}/regen-prompt").status_code == 202  # 空 prompt 直接生成
        from comic_studio.engine.shots import update_shot
        update_shot(c.app.state.db, sid, {"prompt": "已有", "status": "ready"})
        assert c.post(f"/api/shots/{sid}/regen-prompt").status_code == 409
        assert c.post(f"/api/shots/{sid}/regen-prompt", json={"force": True}).status_code == 202
