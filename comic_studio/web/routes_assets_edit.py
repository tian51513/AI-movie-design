# comic_studio/web/routes_assets_edit.py
"""资产外貌编辑（服装修正入口，2026-08-24 服装教训）+ stale 联动。"""
import json

from fastapi import APIRouter, Body, HTTPException, Request

from ..engine.assets import get_asset
from ..engine.logbus import emit as emit_log
from ..engine.paths import data_to_abs
from ..engine.shots import mark_stale_for_asset

router = APIRouter(tags=["assets"])


@router.patch("/api/assets/{asset_id}")
def patch_detail(request: Request, asset_id: int, body: dict = Body(...)):
    db = request.app.state.db
    asset = get_asset(db, asset_id)
    if asset is None:
        raise HTTPException(404, "资产不存在")
    detail = str(body.get("detail", "")).strip()
    if not detail:
        raise HTTPException(422, "detail 不能为空")
    conn = db.connect()
    appearance = json.loads(asset["appearance_json"] or "{}")
    appearance["detail"] = detail
    conn.execute("UPDATE assets SET appearance_json=? WHERE id=?",
                 (json.dumps(appearance, ensure_ascii=False), asset_id))
    conn.commit()
    meta_path = data_to_abs(request.app.state.data_dir, asset["library_dir"]) / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["detail"] = detail
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    n = mark_stale_for_asset(db, asset_id)
    emit_log(db, "storyboard", "warn",
             f"资产「{asset['name']}」外貌已修正：{n} 个引用分镜标记 stale（请重生参考图与提示词）",
             project_id=asset["source_project"])
    return {"id": asset_id, "name": asset["name"], "detail": detail}
