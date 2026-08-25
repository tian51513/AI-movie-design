# comic_studio/web/routes_themes.py
"""预设主题管理（2026-08-25 需求）：列表 / 导入（default_theme.md 格式）/ 删除。"""
from fastapi import APIRouter, File, HTTPException, Request, UploadFile

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
    含「成人向/情欲」字样的节整节跳过（与启动同步同一约定）。"""
    if not (file.filename or "").lower().endswith(".md"):
        raise HTTPException(422, "只接受 .md 模板文件")
    try:
        raw = file.file.read().decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(422, "模板文件需为 UTF-8 编码")
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
