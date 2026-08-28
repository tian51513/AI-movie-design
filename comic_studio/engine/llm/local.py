# comic_studio/engine/llm/local.py
"""本地 LLM 让位 + 显存门槛（2026-08-28 决策：LLM 与 ComfyUI 不并行，
12GB 共享显存——gpu_comfy 任务前请求 Ollama 卸载模型（keep_alive=0），
轮询等待 ComfyUI 侧显存回升至门槛，不达标显式报错不硬跑）。

best-effort：Ollama 不可达/LM Studio 无 /api/ps 时无声跳过（只影响让位，
不影响门槛检查）。"""
import time

import httpx


class VramShortage(RuntimeError):
    """显存未达门槛（消息含当前可用与门槛值，供 jobs.error 直读）。"""


def _root(base_url: str) -> str:
    from .provider import normalize_base_url
    u = normalize_base_url(base_url or "")
    return u[: -len("/v1")] if u.endswith("/v1") else u


def yield_local_llm(db, transport=None) -> int:
    """请求 Ollama 卸载已加载模型。返回请求数；不可达/不支持返回 0。"""
    from ..settings import get_setting
    p = (get_setting(db, "llm_providers") or {}).get("local") or {}
    root = _root(p.get("base_url") or "")
    if not root:
        return 0
    n = 0
    try:
        with httpx.Client(timeout=5, transport=transport) as c:
            r = c.get(f"{root}/api/ps")
            r.raise_for_status()
            for m in (r.json().get("models") or []):
                name = m.get("name") or m.get("model")
                if not name:
                    continue
                c.post(f"{root}/api/generate", json={"model": name, "keep_alive": 0})
                n += 1
    except Exception:
        return n
    return n


def ensure_vram_for_comfy(db, comfy, min_gb: float | None = None,
                          wait_s: float = 60.0, poll: float = 2.0,
                          transport=None) -> float:
    """gpu_comfy 前置：让位本地 LLM → 轮询 comfy.vram_free() 至 ≥ 门槛。
    期间间隔秒级~几十秒可接受（用户决策 2026-08-28）。达标返回可用 GB；
    超时 raise VramShortage（含当前值与门槛）。"""
    from ..settings import get_setting
    n = yield_local_llm(db, transport=transport)
    if n:
        from ..logbus import emit as emit_log
        emit_log(db, "system", "info",
                 f"ComfyUI 任务前 LLM 让位：已请求 Ollama 卸载 {n} 个模型（释放显存）")
    if min_gb is None:
        cfg = get_setting(db, "comfy") or {}
        min_gb = float(cfg.get("min_free_vram_gb") or 8)
    deadline = time.monotonic() + wait_s
    free = comfy.vram_free()
    while free < min_gb and time.monotonic() < deadline:
        time.sleep(poll)
        free = comfy.vram_free()
    if free < min_gb:
        raise VramShortage(
            f"显存不足：当前可用 {free:.1f}GB < 门槛 {min_gb}GB——本地 LLM 已请求让位"
            f"但未释放（或被其他程序占用）；可稍后重试、调小 comfy.min_free_vram_gb，"
            f"或重启 Ollama/ComfyUI")
    return free
