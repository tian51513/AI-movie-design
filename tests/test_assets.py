# tests/test_assets.py
import json
from types import SimpleNamespace as NS

from comic_studio.engine.assets import list_project_assets, persist_assets
from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project


def _db(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate(); return db


def _analysis():
    return NS(
        characters=[NS(name="萧炎", role="主角", appearance="黑发少年", tags=["主角"])],
        scenes=[NS(name="乌坦城", description="喧嚣的集市", tags=[])],
        props=[NS(name="玄重尺", description="黑色重剑", tags=["武器"])],
    )


def test_persist_creates_rows_dirs_links(tmp_path):
    db = _db(tmp_path)
    proj = create_project(db, tmp_path / "data", "p", "9:16", "t")
    ids = persist_assets(db, tmp_path / "data", proj["id"], _analysis())
    assert len(ids) == 3
    rows = list_project_assets(db, proj["id"])
    kinds = {r["kind"] for r in rows}
    assert kinds == {"character", "scene", "prop"}
    for r in rows:
        meta = json.loads((tmp_path / "data" / "library" / f"{r['kind']}s" / str(r["id"]) / "meta.json").read_text(encoding="utf-8"))
        assert meta["name"] == r["name"]
        assert (tmp_path / "data" / "library" / f"{r['kind']}s" / str(r["id"]) / "views").is_dir()


def test_persist_rolls_back_on_mid_loop_failure(tmp_path):
    """persist_assets 中途失败应回滚所有未提交的 INSERT。"""
    db = _db(tmp_path)
    proj = create_project(db, tmp_path / "data", "p", "9:16", "t")

    class BrokenTags:
        name = "坏角色"
        role = "配角"
        appearance = "x"
        @property
        def tags(self):
            raise RuntimeError("模拟持久化失败")

    analysis = NS(
        characters=[NS(name="好角色", role="主角", appearance="黑发少年", tags=["主角"]), BrokenTags()],
        scenes=[],
        props=[],
    )
    import pytest
    with pytest.raises(RuntimeError, match="模拟持久化失败"):
        persist_assets(db, tmp_path / "data", proj["id"], analysis)

    # finish_job 调用 conn.commit()，如果 persist_assets 留下了未提交 INSERT
    # 它们会被一起提交——所以用 fresh connection 验证
    from comic_studio.engine.jobs import finish_job, create_job
    jid = create_job(db, proj["id"], "analyze")
    finish_job(db, jid, "test")  # 触发同线程 conn.commit()

    # 用独立连接检查：不应该有任何 asset 行
    import sqlite3
    raw = sqlite3.connect(tmp_path / "s.db")
    count = raw.execute("SELECT COUNT(*) FROM assets WHERE source_project=?",
                        (proj["id"],)).fetchone()[0]
    raw.close()
    assert count == 0


def test_reanalyze_links_not_duplicates(tmp_path):
    db = _db(tmp_path)
    proj = create_project(db, tmp_path / "data", "p", "9:16", "t")
    persist_assets(db, tmp_path / "data", proj["id"], _analysis())
    persist_assets(db, tmp_path / "data", proj["id"], _analysis())  # 同名重分析
    rows = list_project_assets(db, proj["id"])
    assert len(rows) == 3  # 同项目同名不重复入库


def test_library_dir_stored_relative_for_portability(tmp_path):
    from comic_studio.engine.paths import data_to_abs
    db = _db(tmp_path)
    proj = create_project(db, tmp_path / "data", "p", "9:16", "t")
    persist_assets(db, tmp_path / "data", proj["id"], _analysis())
    rows = list_project_assets(db, proj["id"])
    for r in rows:
        assert not r["library_dir"].startswith(("/", "\\")) and ":" not in r["library_dir"]
        assert r["library_dir"] == f"library/{r['kind']}s/{r['id']}"
        assert (data_to_abs(tmp_path / "data", r["library_dir"]) / "meta.json").exists()
