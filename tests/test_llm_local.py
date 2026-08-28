# tests/test_llm_local.py
"""LLM 让位 + 显存门槛（2026-08-28 决策：LLM 与 ComfyUI 不并行——gpu_comfy 任务
前请求 Ollama 卸载模型释放显存，轮询等待至达标，不达标显式报错）。"""
import httpx
import pytest

from comic_studio.engine.db import Database
from comic_studio.engine.settings import set_setting


def _db(tmp_path, base_url="http://127.0.0.1:11434/v1", min_gb=8):
    db = Database(tmp_path / "s.db"); db.migrate()
    set_setting(db, "llm_providers", {
        "local": {"base_url": base_url, "api_key": "ollama", "model": "m"},
        "online": {"base_url": "", "api_key": "", "model": ""}})
    set_setting(db, "comfy", {"base_url": "http://x:8188", "min_free_vram_gb": min_gb})
    return db


class _Transport(httpx.BaseTransport):
    def __init__(self, ps_models=None, fail_ps=False):
        self.calls, self._ps, self._fail = [], ps_models or [], fail_ps

    def handle_request(self, request):
        self.calls.append((request.method, str(request.url),
                           request.content.decode() if request.content else ""))
        if request.url.path.endswith("/api/ps"):
            if self._fail:
                raise httpx.ConnectError("down")
            return httpx.Response(200, json={"models": self._ps})
        return httpx.Response(200, json={"done": True})


def test_yield_unloads_loaded_models(tmp_path):
    from comic_studio.engine.llm.local import yield_local_llm
    db = _db(tmp_path)
    t = _Transport(ps_models=[{"name": "nsfwvision-v3:latest"}, {"model": "qwen3.5:4b"}])
    n = yield_local_llm(db, transport=t)
    assert n == 2
    posts = [(u, b) for m, u, b in t.calls if m == "POST"]
    assert len(posts) == 2
    assert all('"keep_alive":0' in b.replace(" ", "") for _, b in posts)
    assert all(":11434/api/" in u for m, u, b in t.calls)  # /v1 已归一到根


def test_yield_silent_when_unreachable(tmp_path):
    from comic_studio.engine.llm.local import yield_local_llm
    db = _db(tmp_path, base_url="http://127.0.0.1:1234/v1")  # LM Studio 无 /api/ps
    assert yield_local_llm(db, transport=_Transport(fail_ps=True)) == 0
    set_setting(db, "llm_providers", {
        "local": {"base_url": "", "api_key": "", "model": ""},
        "online": {"base_url": "", "api_key": "", "model": ""}})
    assert yield_local_llm(db) == 0


class _FakeComfy:
    def __init__(self, free_seq):
        self.free_seq, self.calls, self.freed = list(free_seq), 0, 0

    def vram_free(self):
        self.calls += 1
        v = self.free_seq[min(self.calls - 1, len(self.free_seq) - 1)]
        if isinstance(v, Exception):
            raise v
        return v

    def free(self):
        self.freed += 1


def test_ensure_vram_waits_until_free(tmp_path):
    """卸载有延迟（秒~几十秒可接受）：先让位 → 仍不足自动 comfy.free()（真机
    job 720：占用方是 ComfyUI 自驻留模型）→ 轮询等待显存回升 → 达标放行。"""
    from comic_studio.engine.llm.local import ensure_vram_for_comfy
    db = _db(tmp_path)
    comfy = _FakeComfy([3.0, 5.5, 8.2])  # 第三次回升达标
    t = _Transport(ps_models=[{"name": "m"}])
    free = ensure_vram_for_comfy(db, comfy, transport=t, poll=0)
    assert free >= 8 and comfy.calls == 3
    assert comfy.freed == 1  # 首读不足 → 自动补了一发 /free
    assert any(m == "POST" for m, u, b in t.calls)  # 已请求让位


def test_ensure_vram_times_out_with_clear_error(tmp_path):
    from comic_studio.engine.llm.local import VramShortage, ensure_vram_for_comfy
    db = _db(tmp_path)
    comfy = _FakeComfy([2.0])  # 一直不达标
    with pytest.raises(VramShortage, match="显存不足.*2.0.*8"):
        ensure_vram_for_comfy(db, comfy, transport=_Transport(),
                              wait_s=0.1, poll=0.05)
