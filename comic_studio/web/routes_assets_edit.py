# comic_studio/web/routes_assets_edit.py
"""资产外貌/名称编辑（服装修正入口，2026-08-24 服装教训；改名 2026-09-20
乱码名判例）+ 删除（2026-09-20 用户需求）+ stale 联动。"""
import json

from fastapi import APIRouter, Body, HTTPException, Request

from ..engine.assets import delete_asset, get_asset, list_project_assets
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
    name = str(body.get("name", "")).strip()
    if not detail and not name:
        raise HTTPException(422, "detail 与 name 至少提供一个")
    if name and (len(name) > 100
                 or any(o["id"] != asset_id for o in list_project_assets(db, asset["source_project"])
                        if o["kind"] == asset["kind"] and o["name"] == name)):
        # 同项目同 kind 重名=合并语义歧义（persist 同名跳过逻辑会错乱），422 拦下
        raise HTTPException(422, "name 非法（超 100 字符或与同项目同类资产重名）")
    conn = db.connect()
    if name:
        conn.execute("UPDATE assets SET name=? WHERE id=?", (name, asset_id))
    if detail:
        appearance = json.loads(asset["appearance_json"] or "{}")
        appearance["detail"] = detail
        conn.execute("UPDATE assets SET appearance_json=? WHERE id=?",
                     (json.dumps(appearance, ensure_ascii=False), asset_id))
    conn.commit()
    meta_path = data_to_abs(request.app.state.data_dir, asset["library_dir"]) / "meta.json"
    if meta_path.exists() and (detail or name):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if name:
            meta["name"] = name
        if detail:
            meta["detail"] = detail
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    new_name = name or asset["name"]
    if detail:
        n = mark_stale_for_asset(db, asset_id)
        emit_log(db, "storyboard", "warn",
                 f"资产「{new_name}」外貌已修正：{n} 个引用分镜标记 stale（请重生参考图与提示词）",
                 project_id=asset["source_project"])
    if name:
        emit_log(db, "storyboard", "info", f"资产改名：「{asset['name']}」→「{name}」",
                 project_id=asset["source_project"])
    return {"id": asset_id, "name": new_name, "detail": detail or
            json.loads(asset["appearance_json"] or "{}").get("detail", "")}


@router.delete("/api/assets/{asset_id}")
def delete_asset_route(request: Request, asset_id: int):
    """删除资产（2026-09-20 用户需求：乱码名/误提取/重复资产清理）。
    清 project_assets 绑定 / jobs 引用 / 分镜 ledger 绑定 / 磁盘 library 目录。"""
    db = request.app.state.db
    asset = get_asset(db, asset_id)
    if asset is None:
        raise HTTPException(404, "资产不存在")
    pid = asset["source_project"]
    name = delete_asset(db, request.app.state.data_dir, asset_id)
    emit_log(db, "storyboard", "warn",
             f"资产「{name}」已删除（分镜绑定已解除，任务行保留作审计）",
             project_id=pid)
    return {"deleted": asset_id, "message": f"资产「{name}」已删除"}
