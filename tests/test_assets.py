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


def test_reanalyze_links_not_duplicates(tmp_path):
    db = _db(tmp_path)
    proj = create_project(db, tmp_path / "data", "p", "9:16", "t")
    persist_assets(db, tmp_path / "data", proj["id"], _analysis())
    persist_assets(db, tmp_path / "data", proj["id"], _analysis())  # 同名重分析
    rows = list_project_assets(db, proj["id"])
    assert len(rows) == 3  # 同项目同名不重复入库
