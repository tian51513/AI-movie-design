# comic_studio/web/routes_workflows.py
"""工作流导入 REST（2026-08-26 需求）：上传 ComfyUI API JSON → 自动识别入库。"""
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


@router.post("/import")
def import_workflow(request: Request, file: UploadFile = File(...)):
    """导入 ComfyUI API 格式工作流 JSON：自动识别类型/注入点/模型槽位 →
    生成 manifest 入库（templates/workflows/）。"""
    if not (file.filename or "").lower().endswith(".json"):
        raise HTTPException(422, "只接受 .json 文件（ComfyUI 导出时选 API 格式）")
    raw = file.file.read(5 * 1024 * 1024 + 1)  # 上限 5MB
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(422, "工作流 JSON 超过 5MB 上限")
    from ..engine.workflows.importer import import_workflow_json
    template_dir = Path("templates/workflows")
    try:
        analysis = import_workflow_json(raw, file.filename, template_dir)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return analysis


@router.get("/download")
def download_workflow(request: Request, tid: str = Query(...)):
    """下载模板的 api.json（用户拖入 ComfyUI 测试）。"""
    from ..engine.workflows import registry
    from fastapi.responses import Response
    reg = registry.scan_templates(registry.TEMPLATE_ROOT)
    t = next((t for t in reg.values() if t.id == tid), None)
    if t is None:
        raise HTTPException(404, f"模板不存在: {tid}")
    json_bytes = (t.dir / t.file).read_bytes()
    return Response(
        content=json_bytes, media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename={t.id}.json"})
