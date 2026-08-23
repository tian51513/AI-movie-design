"""ComfyUI 连接状态探测（设置页指示灯）。"""
from fastapi import APIRouter, Request

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
