"""LLM 设置接口：查看/编辑 provider 与任务路由（spec §9.1 可配置路由的 Web 面）。"""
import re

import httpx

from ..engine.llm.provider import LLMClient
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..engine.settings import get_setting, set_setting

router = APIRouter(prefix="/api/settings", tags=["settings"])

PROVIDER_NAMES = ("local", "online")
TASK_NAMES = ("extract_assets", "fix_appearance", "split_storyboards", "gen_video_prompt",
              "optimize_prompt", "gen_story")


class ProviderConfig(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    # 附加请求参数（透传 chat.completions.create 的 extra_body），如屏蔽思考：
    # {"chat_template_kwargs": {"enable_thinking": false}}——本机 LM Studio 实测无效，留给支持的服务端
    extra_body: dict | None = None


class ComfyConfig(BaseModel):
    base_url: str = ""
    # 导演台性能开关（引擎注入覆盖模板值；OOM 时开清显存）
    director_clear_vram: bool = False
    director_export_source: bool = False
    director_batch_relay: bool = True  # P7-H 批间首帧接力
    director_mix: bool = True  # P7-J 整片混音（TTS+字幕）


TEMPLATE_MAP_KEYS = {"character_views", "t2i", "ref2va", "fl2v", "t2v", "i2v",
                     "keyframe", "director"}


class SettingsUpdate(BaseModel):
    llm_providers: dict[str, ProviderConfig] | None = None
    llm_routing: dict[str, str] | None = None
    comfy: ComfyConfig | None = None
    template_map: dict[str, str | None] | None = None
    model_overrides: dict[str, dict[str, str]] | None = None


@router.get("")
def read(request: Request):
    from ..engine.workflows import registry
    try:
        treg = registry.scan_templates(registry.TEMPLATE_ROOT)
        templates = [{"id": t.id, "name": t.name, "type": t.type}
                     for t in treg.values()]
    except registry.ManifestError:
        templates = []
    return {
        "llm_providers": get_setting(request.app.state.db, "llm_providers"),
        "llm_routing": get_setting(request.app.state.db, "llm_routing"),
        "comfy": get_setting(request.app.state.db, "comfy"),
        "template_map": get_setting(request.app.state.db, "template_map"),
        "model_overrides": get_setting(request.app.state.db, "model_overrides") or {},
        "model_templates": templates,
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
        # 路由值可为 "provider" 或 "provider:model"（点对点钉具体模型，首冒号切分）
        bad_targets = {v for v in body.llm_routing.values()
                       if (v.split(":", 1)[0] if v else v) not in providers}
        if bad_targets:
            raise HTTPException(422, f"路由目标不存在: {sorted(bad_targets)}，可选 {list(providers)}")
        merged = get_setting(db, "llm_routing")
        merged.update(body.llm_routing)
        set_setting(db, "llm_routing", merged)
    if body.comfy is not None:
        merged = get_setting(db, "comfy")
        merged.update(body.comfy.model_dump())
        set_setting(db, "comfy", merged)
    if body.template_map is not None:
        bad = set(body.template_map) - TEMPLATE_MAP_KEYS
        if bad:
            raise HTTPException(422, f"未知 template_map 键: {sorted(bad)}，只允许 {sorted(TEMPLATE_MAP_KEYS)}")
        merged = get_setting(db, "template_map")
        merged.update(body.template_map)
        set_setting(db, "template_map", merged)
    if body.model_overrides is not None:
        from ..engine.workflows import registry
        reg = registry.scan_templates(registry.TEMPLATE_ROOT)
        bad_tmpl = set(body.model_overrides) - set(reg)
        if bad_tmpl:
            raise HTTPException(422, f"未知模板: {sorted(bad_tmpl)}，只允许 {sorted(reg)}")
        merged = get_setting(db, "model_overrides") or {}
        for tmpl_id, slots in body.model_overrides.items():
            labels = {s.label for s in reg[tmpl_id].models}
            bad_labels = set(slots) - labels
            if bad_labels:
                raise HTTPException(
                    422, f"模板 {tmpl_id} 无模型槽位: {sorted(bad_labels)}，"
                         f"可用 {sorted(labels)}")
            if not slots:
                merged.pop(tmpl_id, None)  # 空字典=恢复该模板默认（清空覆盖）
            else:
                merged.setdefault(tmpl_id, {}).update(slots)
        set_setting(db, "model_overrides", merged)
    return {"status": "ok"}


@router.get("/models/choices")
def model_choices(template: str = Query(...), request: Request = None):
    """枚举模板各模型槽位的可选文件（ComfyUI /object_info/{cls}）。"""
    from ..engine.comfy.client import ComfyClient
    from ..engine.workflows import registry
    reg = registry.scan_templates(registry.TEMPLATE_ROOT)
    if template not in reg:
        raise HTTPException(404, f"模板不存在: {template}（已注册 {sorted(reg)}）")
    base_url = (get_setting(request.app.state.db, "comfy") or {}).get("base_url")
    if not base_url:
        raise HTTPException(409, "未配置 ComfyUI 地址（设置页先填 comfy.base_url）")
    comfy = ComfyClient(base_url)
    out = []
    wf = reg[template].api_json()
    for slot in reg[template].models:
        try:
            with comfy._client() as c:
                info = c.get(f"{base_url}/object_info/{slot.cls}").json()[slot.cls]
            choices = (info["input"]["required"].get(slot.field)
                       or info["input"].get("optional", {}).get(slot.field))
            choices = choices[0]
        except Exception as exc:
            raise HTTPException(502, f"ComfyUI 枚举失败（{slot.cls}.{slot.field}）：{exc}")
        current = str((wf.get(str(slot.node), {}).get("inputs") or {}).get(slot.field, ""))
        out.append({"label": slot.label, "label_cn": slot.label_cn or slot.label,
                    "cls": slot.cls, "field": slot.field, "current": current,
                    "choices": list(choices or [])})
    return out


def _ollama_root(base_url: str) -> str:
    """任意常见写法 → Ollama 原生 API 根。
    容忍：缺 scheme、末尾斜杠、/v1、/v1/chat/completions、/api、/api/tags 等后缀。"""
    u = base_url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", u):
        u = "http://" + u
    m = re.search(r"/(v1|api)(/|$)", u)
    if m:
        u = u[:m.start()]
    return u.rstrip("/")


def _fetch_ollama_models(root_url: str, transport=None) -> list[str]:
    """取模型名清单（网络在此，测试注入 transport）。
    优先 OpenAI 兼容 /v1/models（data[].id）——Ollama/LM Studio/vLLM 通吃；
    404/非 200/空清单再退回 Ollama 原生 /api/tags（2026-08-27 真机：
    LM Studio 测试连接通过但取模型失败，因其只服务 /v1/models 无 /api/tags）。"""
    with httpx.Client(timeout=5, transport=transport) as client:
        try:
            r = client.get(f"{root_url}/v1/models")
            if r.status_code == 200:
                ids = [m["id"] for m in r.json().get("data", []) if m.get("id")]
                if ids:
                    return ids
        except httpx.HTTPError:
            pass  # 连接层失败也交由 /api/tags 再试一次，错误统一在下面抛
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
    root = _ollama_root(base_url)
    try:
        models = _fetch_ollama_models(root)
    except Exception as e:
        raise HTTPException(502, f"LLM 服务不可达或响应异常（尝试了 {root}/v1/models "
                                 f"与 {root}/api/tags）：{e}；确认服务正在运行且地址正确")
    return {"models": models}


class LLMTestBody(BaseModel):
    provider: str
    base_url: str
    api_key: str = ""
    model: str = ""
    extra_body: dict | None = None


@router.post("/llm-test")
def llm_test(body: LLMTestBody, request: Request = None):
    # 与 ollama-models 相同的跨站盲发守卫
    if request and request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "拒绝跨站请求")
    if body.provider not in ("local", "online"):
        raise HTTPException(422, "provider 只能是 local 或 online")
    if not (body.base_url.strip() and body.model.strip()):
        return {"ok": False, "detail": "base_url 与模型名不能为空"}
    try:
        client = LLMClient(body.base_url.strip(), body.api_key.strip() or "none",
                           body.model.strip(), timeout=60, extra_body=body.extra_body)
        # 不限 max_tokens：思考型模型（reasoning_content）预算太小会只出思考不出正文
        # （真机 2026-08-27 LM Studio：max_tokens=8 时正文恒空，测试连接"通过"是假象）
        reply, _ = client.raw_chat(
            [{"role": "user", "content": "连接测试，请只回复：OK"}])
        if not (reply or "").strip():
            return {"ok": False, "detail": "连接成功但返回空正文——多为思考型模型（思考耗尽输出预算）"
                                           "或端点路径不对；可在 provider 配置 extra_body 屏蔽思考"}
        return {"ok": True, "detail": reply.strip()[:50]}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"[:200]}
