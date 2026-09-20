# tests/test_llm_local.py
"""LLM 让位 + 显存门槛（2026-08-28 决策：LLM 与 ComfyUI 不并行——gpu_comfy 任务
前请求 Ollama 卸载模型释放显存，轮询等待至达标，不达标显式报错）。"""
import httpx
import pytest

from comic_studio.engine.db import Database
from comic_studio.engine.llm.local import yield_local_llm
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


def test_yield_covers_all_local_providers(tmp_path):
    """2026-09-17 动态化后曾硬编码 local 键——新连接（如 llama3）从不让位；
    2026-09-20 修：遍历全部本机连接；非本机地址不打；llama 型跳过（无 /api/ps）。"""
    db = Database(tmp_path / "s.db"); db.migrate()
    set_setting(db, "llm_providers", {
        "local": {"base_url": "http://127.0.0.1:11434/v1", "model": "m1"},
        "llama3": {"base_url": "http://127.0.0.1:11435/v1", "model": "m2"},
        "cloud": {"base_url": "https://api.example.com/v1", "model": "x"},
        "llamacpp": {"base_url": "http://127.0.0.1:8123/v1", "model": "Bonsai",
                     "kind": "llama"}})
    t = _Transport(ps_models=[{"name": "m"}, {"name": "n"}])
    n = yield_local_llm(db, transport=t)
    assert n == 4  # 两个本机 Ollama 型连接 × 各 2 个已加载模型
    hosts = {u.split("//")[1].split("/")[0] for (_, u, _) in t.calls}
    assert hosts == {"127.0.0.1:11434", "127.0.0.1:11435"}  # 线上/llama 未探测


def test_stop_llama_servers_runs_stop_bat_only_when_configured(tmp_path, monkeypatch):
    from comic_studio.engine.llm.local import stop_llama_servers
    db = Database(tmp_path / "s.db"); db.migrate()
    ran = []
    monkeypatch.setattr("comic_studio.engine.llm.local._run_bat",
                        lambda cfg, bat, *a: ran.append((bat, a)))
    monkeypatch.setattr("comic_studio.engine.llm.local._llama_health",
                        lambda url, transport=None: True)  # 在跑（幂等门槛）
    set_setting(db, "llm_providers",
                {"local": {"base_url": "http://127.0.0.1:11434/v1", "kind": "ollama"}})
    assert stop_llama_servers(db) is False and not ran  # 无 llama 型不执行
    set_setting(db, "llm_providers",
                {"bonsai": {"base_url": "http://127.0.0.1:8123/v1", "kind": "llama"}})
    assert stop_llama_servers(db) is True
    assert ran and ran[0][0] == "停止.bat" and not ran[0][1]


def test_ensure_llama_running_starts_and_waits(tmp_path, monkeypatch):
    from comic_studio.engine.llm.local import ensure_llama_running
    db = Database(tmp_path / "s.db"); db.migrate()
    ran, probes = [], {"n": 0}

    class Health(httpx.BaseTransport):
        def handle_request(self, request):
            probes["n"] += 1
            # 前两次健康失败（未运行），启动后成功
            return httpx.Response(200 if probes["n"] > 2 else 503)

    monkeypatch.setattr("comic_studio.engine.llm.local._run_bat",
                        lambda cfg, bat, *a: ran.append((bat, a)))
    monkeypatch.setattr("comic_studio.engine.llm.local.time.sleep", lambda s: None)
    monkeypatch.setattr("comic_studio.engine.llm.local.time.monotonic",
                        lambda: probes["n"])  # 单调推进防真等 150s
    ok = ensure_llama_running(db, {"base_url": "http://127.0.0.1:8123/v1",
                                   "model": "Bonsai:latest"}, transport=Health())
    assert ok is True and ran == [("启动.bat", ("Bonsai",))]  # 冒号后 tag 剥离


def test_client_for_task_llama_kind_triggers_start(tmp_path, monkeypatch):
    """llama 型连接经 client_for_task → 未运行自动拉起；拉起失败显式报错。"""
    from comic_studio.engine.llm.provider import LLMError, client_for_task
    db = Database(tmp_path / "s.db"); db.migrate()
    set_setting(db, "llm_providers",
                {"bonsai": {"base_url": "http://127.0.0.1:8123/v1", "model": "Bonsai",
                            "kind": "llama"}})
    calls = []
    monkeypatch.setattr("comic_studio.engine.llm.local.ensure_llama_running",
                        lambda db, p, name="": calls.append(name) or True)
    client_for_task(db, "extract_assets") if False else None
    # 路由缺 bonsai——直接用 provider 名调（client_for_task 按 routing 解析）
    set_setting(db, "llm_routing", {"extract_assets": "bonsai"})
    client_for_task(db, "extract_assets")
    assert calls  # 拉起钩子触发
    monkeypatch.setattr("comic_studio.engine.llm.local.ensure_llama_running",
                        lambda db, p, name="": False)
    with pytest.raises(LLMError, match="llama-server"):
        client_for_task(db, "extract_assets")


def test_ensure_llama_switches_when_wrong_model_loaded(tmp_path, monkeypatch):
    """切换语义（2026-09-20 用户确认预期）：跑着其它模型 → 先停再启目标；
    跑着目标模型 → 零命令直用（此前只看活没活，路由换模型时请求会静默打到旧模型）。"""
    from comic_studio.engine.llm.local import ensure_llama_running
    db = Database(tmp_path / "s.db"); db.migrate()
    state = {"loaded": "Ternary-Bonsai-2-27B-PQ2_0", "healthy": True}
    ran = []

    class T(httpx.BaseTransport):
        def handle_request(self, request):
            path = request.url.path
            if path.endswith("/v1/models"):
                if state["healthy"]:
                    return httpx.Response(200, json={"data": [{"id": state["loaded"]}]})
                raise httpx.ConnectError("down")
            if path.endswith("/health"):
                return httpx.Response(200 if state["healthy"] else 503)
            return httpx.Response(404)

    def fake_run(cfg, bat, *args):
        ran.append((bat, args))
        if bat == cfg["start"]:
            state["healthy"] = True
            state["loaded"] = "Ternary-Bonsai-2-27B-PQ2_0"

    monkeypatch.setattr("comic_studio.engine.llm.local._run_bat", fake_run)

    def ensure(model):
        return ensure_llama_running(db, {"base_url": "http://127.0.0.1:8123/v1",
                                         "model": model}, transport=T(),
                                    runner=fake_run)

    ran.clear()
    assert ensure("Bonsai:latest") is True  # 目标在跑（关键字包含匹配）——零命令
    assert ran == []

    state["loaded"] = "Qwen3.5-4B-Q4_K_M"
    ran.clear()
    assert ensure("Ternary-Bonsai-2-27B-PQ2_0") is True  # 换模型：停旧→启新
    assert [b for b, _ in ran] == ["停止.bat", "启动.bat"]
    assert ran[1][1] == ("Ternary-Bonsai-2-27B-PQ2_0",)


def test_cross_provider_yield(tmp_path, monkeypatch):
    """跨服务商让位（2026-09-20 用户确认的多服务商路由场景）：
    ① llama 起模型前先请 Ollama 卸载驻留；② 本机 Ollama 型任务前停在跑的
    llama-server（未在跑零开销）；③ stop_llama_servers 没在跑不动作。"""
    from comic_studio.engine.llm.local import ensure_llama_running, stop_llama_servers
    from comic_studio.engine.llm.provider import client_for_task
    db = Database(tmp_path / "s.db"); db.migrate()
    set_setting(db, "llm_providers", {
        "bonsai": {"base_url": "http://127.0.0.1:8123/v1", "model": "Bonsai",
                   "kind": "llama"},
        "local": {"base_url": "http://127.0.0.1:11434/v1", "model": "m",
                  "kind": "ollama"}})
    set_setting(db, "llm_routing", {"extract_assets": "local"})
    ran, ps_hits = [], []

    class T(httpx.BaseTransport):
        def handle_request(self, request):
            path = request.url.path
            if path.endswith("/api/ps"):
                ps_hits.append(1)
                return httpx.Response(200, json={"models": [{"name": "m"}]})
            if path.endswith("/api/generate"):
                return httpx.Response(200, json={"done": True})
            if path.endswith("/v1/models"):
                return httpx.Response(503)  # llama 未跑
            if path.endswith("/health"):
                return httpx.Response(200)  # 启动命令后即就绪（免真等 150s）
            return httpx.Response(404)

    def fake_run(cfg, bat, *args):
        ran.append((bat, args))

    monkeypatch.setattr("comic_studio.engine.llm.local._run_bat", fake_run)
    monkeypatch.setattr("comic_studio.engine.llm.local.time.sleep", lambda s: None)
    # ① llama 冷启（未跑）→ 启动前 Ollama 让位（/api/ps 被打 + keep_alive=0）
    ensure_llama_running(db, {"base_url": "http://127.0.0.1:8123/v1",
                              "model": "Bonsai"}, transport=T(), runner=fake_run)
    assert ps_hits and ran[-1][0] == "启动.bat"
    # ② 本机 Ollama 型任务 → 停在跑的 llama（此 mock 下 health=404 未跑 → 不动作）
    ran.clear()
    client_for_task(db, "extract_assets")
    assert ran == []  # llama 未在跑：零命令零开销
    # ③ llama 在跑（health 200）→ Ollama 任务前执行停止
    class T2(T):
        def handle_request(self, request):
            if request.url.path.endswith("/health"):
                return httpx.Response(200)
            return super().handle_request(request)
    monkeypatch.setattr("comic_studio.engine.llm.local._llama_health",
                        lambda url, transport=None: True)
    ran.clear()
    client_for_task(db, "extract_assets")
    assert ran == [("停止.bat", ())]


def test_pinned_model_drives_llama_switch(tmp_path, monkeypatch):
    """路由钉选 连接:模型 时拉起/切换按钉选模型判断（llama-server 忽略请求
    model 名——按连接默认判断会静默答错模型）。"""
    from comic_studio.engine.llm.provider import client_for_task
    from comic_studio.engine.llm.local import ensure_llama_running
    db = Database(tmp_path / "s.db"); db.migrate()
    set_setting(db, "llm_providers",
                {"llm": {"base_url": "http://127.0.0.1:8123/v1", "model": "Bonsai",
                         "kind": "llama"}})
    set_setting(db, "llm_routing", {"extract_assets": "llm:Qwen3.5-4B-Q4_K_M"})
    seen = {}

    class T(httpx.BaseTransport):
        def handle_request(self, request):
            if request.url.path.endswith("/v1/models"):
                # 连接默认模型 Bonsai 在跑——钉的是 Qwen3.5，必须触发切换
                return httpx.Response(200, json={"data": [{"id": "Ternary-Bonsai-2-27B-PQ2_0"}]})
            return httpx.Response(200)

    ran = []
    monkeypatch.setattr("comic_studio.engine.llm.local._run_bat",
                        lambda cfg, bat, *a: ran.append((bat, a)) or
                        (_ for _ in ()).throw(SystemExit(0)) if False else ran.append((bat, a)))
    monkeypatch.setattr("comic_studio.engine.llm.local.time.sleep", lambda s: None)
    monkeypatch.setattr("comic_studio.engine.llm.local.ensure_llama_running",
                        lambda db, p, name="": seen.update(model=p.get("model")) or True)
    client_for_task(db, "extract_assets")
    assert seen["model"] == "Qwen3.5-4B-Q4_K_M"  # 钉选模型生效，不是连接默认
