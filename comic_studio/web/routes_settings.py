"""LLM 设置接口：查看/编辑 provider 与任务路由（spec §9.1 可配置路由的 Web 面）。"""
import httpx
from fastapi import APIRouter, HTTPException, Query, Request
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


def _ollama_root(base_url: str) -> str:
    """OpenAI 兼容 base_url（…/v1）→ Ollama 原生 API 根（…）。"""
    root = base_url.strip().rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    return root


def _fetch_ollama_models(root_url: str) -> list[str]:
    """调 Ollama /api/tags 取模型名清单（网络在此，测试注入点）。"""
    with httpx.Client(timeout=5) as client:
        resp = client.get(f"{root_url}/api/tags")
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]


@router.get("/ollama-models")
def ollama_models(base_url: str = Query(...), request: Request = None):
    # 防浏览器跨站盲发（SSRF 向量）：浏览器会带 Sec-Fetch-Site，same-origin 放行；
    # cross-site 拒绝；非浏览器客户端（curl 等）无此头，不受影响（NAT 模式查局域网 Ollama 仍可用）
    sec_fetch_site = request.headers.get("sec-fetch-site") if request else None
    if sec_fetch_site == "cross-site":
        raise HTTPException(403, "拒绝跨站请求")
    try:
        models = _fetch_ollama_models(_ollama_root(base_url))
    except Exception as e:
        raise HTTPException(502, f"Ollama 不可达或响应异常（{e}）；确认 Ollama 正在运行且地址正确")
    return {"models": models}
