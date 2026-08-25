# tests/test_api_assets_main.py
"""主图人工上传（2026-08-25 需求）：生成不尽人意时用户自换 main.png。"""
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from comic_studio.engine.assets import list_project_assets, persist_assets
from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project
from comic_studio.web.app import create_app


def _client(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "主图剧", "16:9", "t")["id"]
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="林晨", appearance="黑发", tags=[])],
                      scenes=[], props=[]))
    app = create_app(tmp_path / "s.db", tmp_path / "data", start_workers=False)
    return db, list_project_assets(db, pid)[0], TestClient(app)


def test_upload_main_image(tmp_path):
    db, asset, c = _client(tmp_path)
    with c:
        r = c.post(f"/api/assets/{asset['id']}/main-image",
                   files={"file": ("my.png", b"\x89PNG-fake", "image/png")})
        assert r.status_code == 200
    main = tmp_path / "data" / asset["library_dir"] / "main.png"
    assert main.read_bytes() == b"\x89PNG-fake"


def test_upload_main_rejects_non_image(tmp_path):
    _, asset, c = _client(tmp_path)
    with c:
        r = c.post(f"/api/assets/{asset['id']}/main-image",
                   files={"file": ("x.txt", b"hello", "text/plain")})
        assert r.status_code == 422


def test_upload_main_missing_asset_404(tmp_path):
    _, _, c = _client(tmp_path)
    with c:
        assert c.post("/api/assets/999/main-image",
                      files={"file": ("a.png", b"x", "image/png")}).status_code == 404


def test_upload_main_rejects_oversized(tmp_path):
    """超 20MB 拒收（防塞盘——安全扫描建议）。"""
    _, asset, c = _client(tmp_path)
    with c:
        r = c.post(f"/api/assets/{asset['id']}/main-image",
                   files={"file": ("big.png", b"x" * (20 * 1024 * 1024 + 2), "image/png")})
        assert r.status_code == 422
