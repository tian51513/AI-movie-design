"""LLM 设置接口：查看/编辑 provider 与任务路由（spec §9.1 可配置路由的 Web 面）。"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..engine.settings import get_setting, set_setting

router = APIRouter(prefix="/api/settings", tags=["settings"])

PROVIDER_NAMES = ("local", "online")
TASK_NAMES = ("extract_assets", "fix_appearance", "split_storyboards", "gen_video_prompt")


class ProviderConfig(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""


class SettingsUpdate(BaseModel):
    llm_providers: dict[str, ProviderConfig] | None = None
    llm_routing: dict[str, str] | None = None


@router.get("")
def read(request: Request):
    return {
        "llm_providers": get_setting(request.app.state.db, "llm_providers"),
        "llm_routing": get_setting(request.app.state.db, "llm_routing"),
    }


@router.put("")
def update(request: Request, body: SettingsUpdate):
    db = request.app.state.db
    if body.llm_providers is not None:
        bad = set(body.llm_providers) - set(PROVIDER_NAMES)
        if bad:
            raise HTTPException(422, f"未知 provider: {sorted(bad)}，只允许 {list(PROVIDER_NAMES)}")
        merged = get_setting(db, "llm_providers")
        merged.update({k: v.model_dump() for k, v in body.llm_providers.items()})
        set_setting(db, "llm_providers", merged)
    if body.llm_routing is not None:
        bad_tasks = set(body.llm_routing) - set(TASK_NAMES)
        if bad_tasks:
            raise HTTPException(422, f"未知任务: {sorted(bad_tasks)}，只允许 {list(TASK_NAMES)}")
        providers = get_setting(db, "llm_providers")
        bad_targets = {v for v in body.llm_routing.values() if v not in providers}
        if bad_targets:
            raise HTTPException(422, f"路由目标不存在: {sorted(bad_targets)}，可选 {list(providers)}")
        merged = get_setting(db, "llm_routing")
        merged.update(body.llm_routing)
        set_setting(db, "llm_routing", merged)
    return {"status": "ok"}
