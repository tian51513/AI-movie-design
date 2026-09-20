# comic_studio/engine/llm/local.py
"""本地 LLM 让位 + 显存门槛（2026-08-28 决策：LLM 与 ComfyUI 不并行，
12GB 共享显存——gpu_comfy 任务前请求 Ollama 卸载模型（keep_alive=0），
轮询等待 ComfyUI 侧显存回升至门槛，不达标显式报错不硬跑）。

2026-09-20 服务商类型分流：llm_providers.<连接>.kind——ollama（/api/ps 让位）/
lmstudio（暂无专用动作）/llama（llama-server 启停器）/空=通用（best-effort
试 /api/ps，不可达无声跳过）。llama 型：常驻显存无自动卸载——Comfy 前跑
「停止.bat」释放；LLM 调用前健康探测失败则「启动.bat <模型名>」拉起再等就绪
（E:\AI\llama\server 通用命令，公共模型目录关键字匹配）。"""
import subprocess
import time

import httpx


class VramShortage(RuntimeError):
    """显存未达门槛（消息含当前可用与门槛值，供 jobs.error 直读）。"""


def _root(base_url: str) -> str:
    from .provider import normalize_base_url
    u = normalize_base_url(base_url or "")
    return u[: -len("/v1")] if u.endswith("/v1") else u


def _is_local(base_url: str) -> bool:
    return "127.0.0.1" in (base_url or "") or "localhost" in (base_url or "")


def yield_local_llm(db, transport=None) -> int:
    """请求 Ollama 卸载已加载模型。返回请求数；不可达/不支持返回 0。
    2026-09-20：遍历全部本机连接（曾硬编码 local 键——服务商动态化后新连接
    从不让位的缺口）；非本机地址跳过（不打线上 API）。"""
    from ..settings import get_setting
    n = 0
    for p in (get_setting(db, "llm_providers") or {}).values():
        if not p or not _is_local(p.get("base_url") or ""):
            continue
        root = _root(p.get("base_url") or "")
        if not root or str(p.get("kind") or "") in ("llama",):
            continue  # llama-server 无 /api/ps；其释放走 stop_llama_servers
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
            continue
    return n


def _launcher_cfg(db) -> dict:
    from ..settings import get_setting
    cfg = get_setting(db, "llama_server") or {}
    return {"dir": cfg.get("dir") or r"E:\AI\llama\server",
            "start": cfg.get("start") or "启动.bat",
            "stop": cfg.get("stop") or "停止.bat",
            "wait_s": float(cfg.get("wait_s") or 150)}


def _run_bat(cfg: dict, bat: str, *args) -> None:
    # cmd /c 跑 bat；启动.bat 内部 start /min 分离——命令即刻返回
    subprocess.run(["cmd", "/c", bat, *args], cwd=cfg["dir"], timeout=60,
                   capture_output=True)


def stop_llama_servers(db, cfg=None) -> bool:
    """任一 llama 型连接在配置中 → 执行「停止.bat」释放其显存（幂等，未运行无害）。
    返回是否执行了停止。"""
    from ..settings import get_setting
    providers = get_setting(db, "llm_providers") or {}
    if not any(p and str(p.get("kind") or "") == "llama" for p in providers.values()):
        return False
    cfg = cfg or _launcher_cfg(db)
    _run_bat(cfg, cfg["stop"])
    from ..logbus import emit as emit_log
    emit_log(db, "system", "info",
             "ComfyUI 任务前 LLM 让位：llama-server 已执行停止命令（释放显存）")
    return True


def _llama_health(base_url: str, transport=None) -> bool:
    root = _root(base_url)
    if not root:
        return False
    try:
        with httpx.Client(timeout=3, transport=transport) as c:
            return c.get(f"{root}/health").status_code == 200
    except Exception:
        return False


def ensure_llama_running(db, provider: dict, name: str = "",
                         transport=None, runner=None) -> bool:
    """llama 型连接前置：健康探测失败 → 启动.bat <模型名关键字> 拉起 → 轮询至
    就绪。超时 False（调用方决定报错或降级）。"""
    if _llama_health(provider.get("base_url") or "", transport=transport):
        return True
    cfg = _launcher_cfg(db)
    keyword = (provider.get("model") or "").strip()
    if not keyword:
        return False
    (runner or _run_bat)(cfg, cfg["start"], keyword.split(":")[0])
    from ..logbus import emit as emit_log
    emit_log(db, "system", "info",
             f"llama-server 未运行，已执行启动命令（模型关键字 {keyword.split(':')[0]}，"
             f"等待就绪至多 {cfg['wait_s']:.0f}s）")
    deadline = time.monotonic() + cfg["wait_s"]
    while time.monotonic() < deadline:
        if _llama_health(provider.get("base_url") or "", transport=transport):
            return True
        time.sleep(3)
    emit_log(db, "system", "warn",
             f"llama-server 启动超时（{cfg['wait_s']:.0f}s 未就绪）——检查 server 目录"
             f"命令/模型文件，或手动跑 启动.bat 排障")
    return False


def ensure_vram_for_comfy(db, comfy, min_gb: float | None = None,
                          wait_s: float = 60.0, poll: float = 2.0,
                          transport=None) -> float:
    """gpu_comfy 前置：让位本地 LLM（Ollama 卸载 + llama-server 停止）→ 轮询
    comfy.vram_free() 至 ≥ 门槛。期间间隔秒级~几十秒可接受（用户决策
    2026-08-28）。达标返回可用 GB；超时 raise VramShortage（含当前值与门槛）。"""
    from ..settings import get_setting
    n = yield_local_llm(db, transport=transport)
    if n:
        from ..logbus import emit as emit_log
        emit_log(db, "system", "info",
                 f"ComfyUI 任务前 LLM 让位：已请求 Ollama 卸载 {n} 个模型（释放显存）")
    stop_llama_servers(db)
    if min_gb is None:
        cfg = get_setting(db, "comfy") or {}
        min_gb = float(cfg.get("min_free_vram_gb") or 8)
    deadline = time.monotonic() + wait_s
    free = comfy.vram_free()
    if free < min_gb and hasattr(comfy, "free"):
        # 让位后仍不足：多为 ComfyUI 自驻留模型（2026-08-28 真机 job 720，
        # 用户手点清理即恢复）——自动补一发 /free 再等
        comfy.free()
    while free < min_gb and time.monotonic() < deadline:
        time.sleep(poll)
        free = comfy.vram_free()
    if free < min_gb:
        raise VramShortage(
            f"显存不足：当前可用 {free:.1f}GB < 门槛 {min_gb}GB——本地 LLM 已请求让位"
            f"但未释放（或被其他程序占用）；可稍后重试、调小 comfy.min_free_vram_gb，"
            f"或重启 Ollama/ComfyUI")
    return free
