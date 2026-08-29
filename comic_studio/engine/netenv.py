# comic_studio/engine/netenv.py
"""本地地址不走代理（2026-08-29 真机教训）。

WSL 会话可能挂着 http_proxy=127.0.0.1:1092（手动 export / 代理工具注入）；
httpx/curl/openai-SDK 默认读环境变量 → 所有打到 127.0.0.1 的请求
（Ollama 11434 / ComfyUI 8188 / LM Studio 1234）被本地代理劫持掐线，
表现为 "Server disconnected without sending a response"——浏览器对
localhost 自动绕代理所以"web 正常、程序全挂"，极具迷惑性。

ensure_local_no_proxy()：把 127.0.0.1/localhost/::1 追加进 no_proxy/NO_PROXY
（大小写双份），已有则不重复；不动代理变量本身（线上 API 仍走代理）。
app lifespan 首位调用，进程内所有出站本地请求免疫。"""
import os

_LOCAL = ("127.0.0.1", "localhost", "::1")


def _append(var: str) -> None:
    cur = os.environ.get(var, "")
    if cur == "" and var in os.environ:
        return  # 显式置空 = 用户有意全禁，尊重
    parts = [p.strip() for p in cur.split(",") if p.strip()]
    for h in _LOCAL:
        if h not in parts:
            parts.append(h)
    if parts:
        os.environ[var] = ",".join(parts)


def ensure_local_no_proxy() -> None:
    """无任何代理变量时零副作用；有则补本地豁免。幂等。"""
    if not any(k in os.environ for k in
               ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "all_proxy", "ALL_PROXY")):
        return
    _append("no_proxy")
    _append("NO_PROXY")
