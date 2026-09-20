# tests/test_api_assets_delete.py
"""资产删除（2026-09-20 用户需求：资产页加删除按钮——源文件乱码名/误提取资产
无法清理）。FK 链齐清：project_assets 绑定 / jobs.asset_id 引用（保留任务行作
审计，同 shots.py 删镜判例）/ shots.ledger 绑定 / 磁盘 library 目录。"""
import io
import json
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from comic_studio.engine.assets import list_project_assets, persist_assets
from comic_studio.engine.paths import data_to_abs
from comic_studio.engine.shots import get_shot, persist_shots
from comic_studio.web.app import create_app


def _client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data",
                                 start_workers=False))


def test_delete_asset_cleans_chain(tmp_path):
    with _client(tmp_path) as c:
        pid = c.post("/api/projects", data={"name": "删资产剧", "aspect_ratio": "16:9"},
                     files={"novel": ("n.txt", io.BytesIO("文".encode()), "text/plain")}).json()["id"]
        persist_assets(c.app.state.db, tmp_path / "data", pid,
                       NS(characters=[NS(name="乱码名?Test?Name", appearance="x", tags=[])],
                          scenes=[], props=[]))
        asset = list_project_assets(c.app.state.db, pid)[0]
        lib_dir = data_to_abs(tmp_path / "data", asset["library_dir"])
        assert (lib_dir / "meta.json").exists()
        sid = persist_shots(c.app.state.db, pid, [NS(
            text_span="", description="x", shot_type="", camera={}, duration=5.0,
            workflow_type="ref2va", ledger={}, character_ids=[asset["id"]],
            scene_ids=[], prop_ids=[], depends_on=None)])[0]
        assert get_shot(c.app.state.db, sid)["ledger_json"].count(str(asset["id"])) >= 1
        # 引用该资产的任务行（gen_ref）——删除后任务行保留、引用解除
        conn = c.app.state.db.connect()
        conn.execute("INSERT INTO jobs (project_id, asset_id, type, status) "
                     "VALUES (?,?, 'gen_ref', 'done')", (pid, asset["id"]))
        conn.commit()

        r = c.delete(f"/api/assets/{asset['id']}")
        assert r.status_code == 200 and "乱码名" in r.json()["message"]
        assert list_project_assets(c.app.state.db, pid) == []
        row = conn.execute("SELECT asset_id FROM jobs WHERE project_id=?", (pid,)).fetchone()
        assert row["asset_id"] is None  # 任务行保留、FK 引用解除
        led = json.loads(get_shot(c.app.state.db, sid)["ledger_json"] or "{}")
        assert asset["id"] not in ((led.get("assets") or {}).get("characters") or [])
        assert not lib_dir.exists()  # 磁盘 library 目录连同图片一并清掉
        assert c.delete(f"/api/assets/{asset['id']}").status_code == 404


def test_dedup_assets_route(tmp_path, monkeypatch):
    """路由层冒烟（引擎层已有完整测试）：工厂/查重符号从 analyze 正确导入、
    404 门禁（2026-09-20 曾把 make_client_factory 错从 provider 导入）。"""
    import comic_studio.engine.llm.analyze as an
    with _client(tmp_path) as c:
        pid = c.post("/api/projects", data={"name": "查重剧", "aspect_ratio": "16:9"},
                     files={"novel": ("n.txt", io.BytesIO("文".encode()), "text/plain")}).json()["id"]
        assert c.post("/api/projects/999/assets/dedup-assets").status_code == 404
        monkeypatch.setattr(an, "make_client_factory", lambda db: (lambda t: None))
        monkeypatch.setattr(an, "dedup_project_assets", lambda *a, **k: 3)
        r = c.post(f"/api/projects/{pid}/assets/dedup-assets")
        assert r.status_code == 200 and r.json() == {"merged": 3}
