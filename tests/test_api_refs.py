# tests/test_api_refs.py
import io
import time
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from comic_studio.engine.assets import persist_assets, list_project_assets
from comic_studio.engine.paths import data_to_abs
from comic_studio.web.app import create_app


def _client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data", start_workers=False))


def _mk_project(c, name="p"):
    return c.post("/api/projects", data={"name": name, "aspect_ratio": "9:16"},
                  files={"novel": ("n.txt", io.BytesIO("正文".encode()), "text/plain")}).json()["id"]


def _seed(tmp_path, c, app, pid):
    persist_assets(app.state.db, tmp_path / "data", pid,
                   NS(characters=[NS(name="萧炎", appearance="黑发", tags=[])],
                      scenes=[NS(name="庭院", description="院子", tags=[])], props=[]))
    from comic_studio.engine.projects import set_stage
    set_stage(app.state.db, pid, "analyzed")
    return list_project_assets(app.state.db, pid)


def test_views_listing_and_gate1(tmp_path):
    with _client(tmp_path) as c:
        pid = _mk_project(c)
        rows = _seed(tmp_path, c, c.app, pid)
        aid = rows[0]["id"]
        r = c.get(f"/api/assets/{aid}/views")
        assert r.json() == []
        # gate1 缺图 → 422
        assert c.post(f"/api/projects/{pid}/gate1").status_code == 422
        assert {m["name"] for m in r.json()} == set()  # 无图
        # 手工给所有资产放一张 sheet.png
        from comic_studio.engine.paths import data_to_abs
        for row in rows:
            views = data_to_abs(tmp_path / "data", row["library_dir"]) / "views"
            views.mkdir(parents=True, exist_ok=True)
            (views / "sheet.png").write_bytes(b"\x89PNG")
        r = c.get(f"/api/assets/{aid}/views").json()
        assert r and r[0]["name"] == "sheet" and "/library/" in r[0]["url"] and "?v=" in r[0]["url"]  # 版本号破缓存
        assert c.post(f"/api/projects/{pid}/gate1").status_code == 200
        assert c.get(f"/api/projects/{pid}").json()["stage"] == "assets_ready"
        assert c.post(f"/api/projects/{pid}/gate1").status_code == 409


def test_gen_enqueue_and_conflict(tmp_path):
    with _client(tmp_path) as c:
        pid = _mk_project(c)
        rows = _seed(tmp_path, c, c.app, pid)
        aid = rows[0]["id"]
        r = c.post(f"/api/assets/{aid}/gen")
        assert r.status_code == 202 and "job_id" in r.json()
        assert c.post(f"/api/assets/{aid}/gen").status_code == 409  # 同资产运行中
        q = c.get(f"/api/projects/{pid}/queue").json()
        assert q["pending"] == 1 and q["comfy_ok"] in (True, False)


def test_generate_refs_batch_only_missing(tmp_path):
    with _client(tmp_path) as c:
        pid = _mk_project(c)
        rows = _seed(tmp_path, c, c.app, pid)
        # 给第一个资产放好图 → 批量只入队第二个
        from comic_studio.engine.paths import data_to_abs
        views = data_to_abs(tmp_path / "data", rows[0]["library_dir"]) / "views"
        views.mkdir(parents=True, exist_ok=True)
        (views / "sheet.png").write_bytes(b"\x89PNG")
        r = c.post(f"/api/projects/{pid}/generate-refs")
        assert r.status_code == 202 and r.json()["enqueued"] == 1


def test_generate_refs_batch_dedup(tmp_path):
    """批量接口不得为已在队列（pending/running）的资产重复入队。"""
    with _client(tmp_path) as c:
        pid = _mk_project(c)
        rows = _seed(tmp_path, c, c.app, pid)
        assert c.post(f"/api/projects/{pid}/generate-refs").json()["enqueued"] == 2
        # 再次点击：全部已在队列 → 0；期间给一个资产手动加图 → 只该资产不入队
        assert c.post(f"/api/projects/{pid}/generate-refs").json()["enqueued"] == 0
        from comic_studio.engine.paths import data_to_abs
        views = data_to_abs(tmp_path / "data", rows[0]["library_dir"]) / "views"
        views.mkdir(parents=True, exist_ok=True)
        (views / "sheet.png").write_bytes(b"\x89PNG")
        # 第一批跑完（done）后，有图的不入队、无图的入队
        from comic_studio.engine.jobs import get_job
        conn = c.app.state.db.connect()
        conn.execute("UPDATE jobs SET status='done' WHERE status IN ('pending','running')")
        conn.commit()
        r = c.post(f"/api/projects/{pid}/generate-refs").json()
        assert r["enqueued"] == 1  # 只有缺图的那个


def test_views_listing_includes_main(tmp_path):
    """主图应在视图列表首位展示（2026-08-25 真机：只见四视图不见主图）。"""
    with _client(tmp_path) as c:
        pid = _mk_project(c)
        rows = _seed(tmp_path, c, c.app, pid)
        aid = rows[0]["id"]
        lib = data_to_abs(tmp_path / "data", rows[0]["library_dir"])
        (lib / "main.png").write_bytes(b"\x89PNG-main")
        (lib / "views").mkdir(parents=True, exist_ok=True)
        (lib / "views" / "sheet.png").write_bytes(b"\x89PNG-sheet")
        items = c.get(f"/api/assets/{aid}/views").json()
        assert items[0]["name"] == "主图 main"
        assert "main.png?" in items[0]["url"]
        assert any(i["name"] == "sheet" for i in items)


def test_queue_feed_covers_all_active_jobs(tmp_path):
    """2026-09-20 判例：jobs 列表曾「最近 20 条」——批量入队 46 个 gen_ref 时
    前 26 个对前端 assetBusy 不可见（不显示生成中）。非终态必须全量在窗 +
    完成数走 done_total 聚合（单调，边沿触发不漏拍）。"""
    with _client(tmp_path) as c:
        pid = c.post("/api/projects", data={"name": "批量参考图", "aspect_ratio": "9:16"},
                     files={"novel": ("n.txt", io.BytesIO("文".encode()), "text/plain")}).json()["id"]
        conn = c.app.state.db.connect()
        for i in range(46):
            cur = conn.execute("INSERT INTO assets (kind, name, appearance_json, tags_json, "
                               "library_dir, source_project) VALUES ('prop', ?, '{}', '[]', '', ?)",
                               (f"道具{i}", pid))
            conn.execute("INSERT INTO jobs (project_id, asset_id, type, status) "
                         "VALUES (?,?, 'gen_ref', 'pending')", (pid, cur.lastrowid))
        conn.execute("INSERT INTO jobs (project_id, type, status) "
                     "VALUES (?, 'gen_ref', 'done')", (pid,))
        conn.commit()
        q = c.get(f"/api/projects/{pid}/queue").json()
        pending_ids = [j["asset_id"] for j in q["jobs"] if j["status"] == "pending"]
        assert len(pending_ids) == 46 and None not in pending_ids  # 全量在窗（不再被 20 条截断）
        assert q["pending"] == 46
        assert q["done_total"] == 1  # 完成数聚合（done 行本身不进 jobs 列表）
