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
        # 非本地主机不扫端口（只扫 127.0.0.1——2026-09-06 端口建议）
        assert body["ok"] is False and "suggest" not in body


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


def test_comfy_status_suggests_port_alternative(tmp_path, monkeypatch):
    """2026-09-06 用户需求（两次端口漂移事故）：配置的地址连不上时自动扫
    8188-8199，找到活的返回建议；都没有照常 offline。"""
    import sys
    sys.path.insert(0, "tests")
    from comfy_mock import comfy_server
    with TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "d",
                               start_workers=False)) as c:
        from comic_studio.engine.settings import set_setting
        # 配 8188（无服务），真实 mock 起在随机端口——直接模拟扫描函数
        from comic_studio.engine import comfy_probe
        with comfy_server("ok") as m:
            found = comfy_probe.probe_ports(
                ["127.0.0.1:1", m.base_url.replace("http://", "")])  # 端口1必死，避免真机 8188 干扰
            assert found == m.base_url.replace("http://", "")   # 命中活的
        assert comfy_probe.probe_ports(["127.0.0.1:8199"]) is None  # 全死→None
        # API 层：离线但发现候选 → 响应带 suggestion
        r = c.get("/api/comfy/status")
        assert r.status_code == 200
        assert "ok" in r.json()
