# comic_studio/web/routes_themes.py
"""预设主题管理（2026-08-25 需求）：列表 / 导入（default_theme.md 格式）/ 删除。"""
from fastapi import APIRouter, File, HTTPException, Request, UploadFile, Body

from ..engine.textdecode import decode_text_bytes

router = APIRouter(prefix="/api/themes", tags=["themes"])


@router.get("")
def themes(request: Request):
    """主题模板列表（库为空时先同步 templates/tpl/）。"""
    from ..engine.themes import list_themes, sync_themes
    if not list_themes(request.app.state.db):
        sync_themes(request.app.state.db)
    return list_themes(request.app.state.db)


@router.post("/import")
def import_themes(request: Request, file: UploadFile = File(...)):
    """导入 .md 模板（default_theme.md 同格式）：解析后按 name upsert。
    成人向节同样导入（2026-08-29 用户决策：放开显示）。"""
    if not (file.filename or "").lower().endswith(".md"):
        raise HTTPException(422, "只接受 .md 模板文件")
    try:
        raw = decode_text_bytes(file.file.read())
    except ValueError as e:
        raise HTTPException(422, str(e))
    from ..engine.themes import parse_text
    items = parse_text(raw)
    if not items:
        raise HTTPException(422, "未解析到任何主题条目（检查格式：数字列表 + **主题名称：**《》+ **描述：**）")
    conn = request.app.state.db.connect()
    for it in items:
        conn.execute(
            "INSERT INTO theme_templates (name, category, description) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET category=excluded.category, "
            "description=excluded.description",
            (it["name"], it["category"], it["description"]))
    conn.commit()
    return {"imported": len(items), "names": [i["name"] for i in items]}


@router.delete("/{theme_id}")
def delete_theme(request: Request, theme_id: int):
    conn = request.app.state.db.connect()
    cur = conn.execute("DELETE FROM theme_templates WHERE id=?", (theme_id,))
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, f"主题不存在: {theme_id}")
    return {"deleted": theme_id}


@router.patch("/{theme_id}")
def update_theme(request: Request, theme_id: int, body: dict = Body(...)):
    """预设主题编辑（2026-08-30 用户需求：可预览/编辑）——name/category/description
    均可选更新；description 即主题正文（from-theme 生成小说的素材）。"""
    from ..engine.themes import get_theme
    theme = get_theme(request.app.state.db, theme_id)
    if theme is None:
        raise HTTPException(404, f"主题不存在: {theme_id}")
    fields, vals = [], []
    for k in ("name", "category", "description"):
        if k in body and str(body[k] or "").strip():
            fields.append(f"{k}=?")
            vals.append(str(body[k]).strip())
    if not fields:
        raise HTTPException(422, "无可更新字段（name/category/description）")
    vals.append(theme_id)
    conn = request.app.state.db.connect()
    conn.execute(f"UPDATE theme_templates SET {', '.join(fields)} WHERE id=?", vals)
    conn.commit()
    return get_theme(request.app.state.db, theme_id)
