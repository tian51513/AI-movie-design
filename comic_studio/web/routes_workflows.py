# comic_studio/web/routes_workflows.py
"""工作流导入 REST（2026-08-26 需求）：上传 ComfyUI API JSON → 自动识别入库。"""
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


@router.post("/import")
def import_workflow(request: Request, file: UploadFile = File(...)):
    """导入 ComfyUI API 格式工作流 JSON：自动识别类型/注入点/模型槽位 →
    生成 manifest 入库（templates/workflows/）。"""
    if not (file.filename or "").lower().endswith(".json"):
        raise HTTPException(422, "只接受 .json 文件（ComfyUI 导出时选 API 格式）")
    raw = file.file.read()
    from ..engine.workflows.importer import import_workflow_json
    template_dir = Path("templates/workflows")
    try:
        analysis = import_workflow_json(raw, file.filename, template_dir)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return analysis
