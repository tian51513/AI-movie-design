"""ComfyUI 连接状态探测（设置页指示灯）+ 显存/内存清理。"""
from fastapi import APIRouter, Body, HTTPException, Request

from ..engine.comfy.client import ComfyClient, ComfyError
from ..engine.settings import get_setting

router = APIRouter(prefix="/api/comfy", tags=["comfy"])


@router.get("/status")
def status(request: Request):
    try:
        ComfyClient(get_setting(request.app.state.db, "comfy")["base_url"],
                    timeout=2).health()
        return {"ok": True}
    except ComfyError:
        return {"ok": False}
    except Exception:
        return {"ok": False}


@router.post("/free")
def free(request: Request, body: dict | None = Body(default=None)):
    """转调 ComfyUI /free：unload_models 卸载模型 + free_memory 清显存/内存。"""
    body = body or {}
    try:
        ComfyClient(get_setting(request.app.state.db, "comfy")["base_url"],
                    timeout=10).free(
            unload_models=bool(body.get("unload_models", True)))
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(502, f"ComfyUI 清理失败：{exc}")

