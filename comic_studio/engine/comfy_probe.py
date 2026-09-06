# comic_studio/engine/comfy_probe.py
"""ComfyUI 地址探测（2026-09-06 两次端口漂移事故：重启后 8188 被占顺延
8189，用户只能人肉改设置）。probe_ports 顺序探测候选，返回第一个活的。"""
import httpx

DEFAULT_CANDIDATES = [f"127.0.0.1:{p}" for p in range(8188, 8200)]


def probe_ports(candidates: list, timeout: float = 1.0):
    """顺序探测 host:port 列表（/system_stats 可达即活）。返回命中的
    host:port；全败 None。"""
    for target in candidates:
        try:
            with httpx.Client(timeout=timeout, trust_env=False) as c:
                r = c.get(f"http://{target}/system_stats")
                if r.status_code == 200:
                    return target
        except Exception:
            continue
    return None
