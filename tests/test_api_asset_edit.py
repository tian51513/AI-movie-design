# tests/test_api_asset_edit.py
import io
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from comic_studio.engine.assets import list_project_assets, persist_assets
from comic_studio.engine.shots import get_shot, persist_shots
from comic_studio.web.app import create_app


def _client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data",
                                 start_workers=False))


def test_patch_asset_detail_and_stale_link(tmp_path):
    with _client(tmp_path) as c:
        pid = c.post("/api/projects", data={"name": "服装剧", "aspect_ratio": "16:9"},
                     files={"novel": ("n.txt", io.BytesIO("文".encode()), "text/plain")}).json()["id"]
        persist_assets(c.app.state.db, tmp_path / "data", pid,
                       NS(characters=[NS(name="直葉", appearance="绿色运动衫", tags=[])],
                          scenes=[], props=[]))
        asset = list_project_assets(c.app.state.db, pid)[0]
        sid = persist_shots(c.app.state.db, pid, [NS(
            text_span="", description="x", shot_type="", camera={}, duration=5.0,
            workflow_type="ref2va", ledger={}, character_ids=[asset["id"]],
            scene_ids=[], prop_ids=[], depends_on=None)])[0]
        r = c.patch(f"/api/assets/{asset['id']}", json={"detail": "白色T恤与黑色短裤，黑色短发女性"})
        assert r.status_code == 200 and r.json()["detail"].startswith("白色T恤")
        import json as _json
        from comic_studio.engine.paths import data_to_abs
        row = list_project_assets(c.app.state.db, pid)[0]
        assert _json.loads(row["appearance_json"])["detail"].startswith("白色T恤")
        meta = _json.loads((data_to_abs(tmp_path / "data", row["library_dir"]) / "meta.json").read_text(encoding="utf-8"))
        assert meta["detail"].startswith("白色T恤")
        assert get_shot(c.app.state.db, sid)["status"] == "stale"
        assert c.patch("/api/assets/999", json={"detail": "x"}).status_code == 404
        assert c.patch(f"/api/assets/{asset['id']}", json={"detail": "  "}).status_code == 422
