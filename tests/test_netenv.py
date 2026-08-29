# tests/test_netenv.py
"""本地地址永不走代理（2026-08-29 真机：WSL 会话挂了 http_proxy=127.0.0.1:1092，
Ollama/ComfyUI 的所有 127.0.0.1 请求被代理掐线——"Server disconnected"一整天
的元凶；浏览器绕 localhost 代理所以一直正常）。"""
import os

from comic_studio.engine.netenv import ensure_local_no_proxy


def _clean(monkeypatch):
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
              "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(k, raising=False)


def test_appends_local_hosts_when_proxy_present(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1092")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1092")
    ensure_local_no_proxy()
    assert "127.0.0.1" in os.environ["no_proxy"]
    assert "localhost" in os.environ["no_proxy"] and "::1" in os.environ["no_proxy"]
    assert "127.0.0.1" in os.environ["NO_PROXY"]
    # 代理变量本身不动（线上 API 仍需要它）
    assert os.environ["http_proxy"] == "http://127.0.0.1:1092"


def test_idempotent_and_preserves_existing(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1092")  # 有代理才需要豁免
    monkeypatch.setenv("no_proxy", "10.0.0.1,localhost")
    ensure_local_no_proxy()
    assert "10.0.0.1" in os.environ["no_proxy"]
    assert os.environ["no_proxy"].count("localhost") == 1
    ensure_local_no_proxy()  # 幂等：重复调用不膨胀
    assert os.environ["no_proxy"].count("127.0.0.1") == 1


def test_noop_when_no_proxy_at_all(monkeypatch):
    _clean(monkeypatch)
    ensure_local_no_proxy()
    assert os.environ.get("no_proxy", "") == ""  # 无代理环境零副作用
