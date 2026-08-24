# tests/test_api_comfy_status.py
from fastapi.testclient import TestClient

from comic_studio.web.app import create_app
from comfy_mock import comfy_server


def _client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data",
                                  start_workers=False))


def test_comfy_status_down_with_invalid_host(tmp_path):
    from comic_studio.engine.settings import set_setting
    with _client(tmp_path) as c:
        set_setting(c.app.state.db, "comfy", {"base_url": "http://nonexistent.invalid"})
        body = c.get("/api/comfy/status").json()
        assert body == {"ok": False}


def test_comfy_status_up_with_mock(tmp_path):
    from comic_studio.engine.settings import set_setting
    with comfy_server("ok") as m:
        with _client(tmp_path) as c:
            set_setting(c.app.state.db, "comfy", {"base_url": m.base_url})
            assert c.get("/api/comfy/status").json() == {"ok": True}


def test_comfy_free_endpoint(tmp_path):
    """POST /api/comfy/free：转调 ComfyUI /free（显存/内存清理），不可达时 502。"""
    from comic_studio.engine.settings import set_setting
    with comfy_server("ok") as m:
        with _client(tmp_path) as c:
            set_setting(c.app.state.db, "comfy", {"base_url": m.base_url})
            r = c.post("/api/comfy/free", json={"unload_models": False})
            assert r.status_code == 200 and r.json() == {"ok": True}
            assert m.frees == 1  # mock 记录 /free 调用
    with _client(tmp_path) as c:
        set_setting(c.app.state.db, "comfy", {"base_url": "http://nonexistent.invalid"})
        assert c.post("/api/comfy/free").status_code == 502
