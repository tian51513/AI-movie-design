# tests/test_api_llm_test.py
from fastapi.testclient import TestClient

from comic_studio.web.app import create_app


def _client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data",
                                  start_workers=False))


def _body(provider="local", **kw):
    return {"provider": provider, "base_url": "http://x", "api_key": "k",
            "model": "m", **kw}


def test_llm_test_ok(tmp_path, monkeypatch):
    import comic_studio.web.routes_settings as rs
    calls = {}

    class FakeLLM:
        def __init__(self, base_url, api_key, model, timeout=30):
            calls.update(base_url=base_url, api_key=api_key, model=model)
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            calls.update(messages=messages, max_tokens=max_tokens)
            return "OK", None
    monkeypatch.setattr(rs, "LLMClient", FakeLLM)
    with _client(tmp_path) as c:
        r = c.post("/api/settings/llm-test", json=_body())
        assert r.status_code == 200 and r.json()["ok"] is True
        assert calls["max_tokens"] == 8 and calls["model"] == "m"


def test_llm_test_failure_returns_detail(tmp_path, monkeypatch):
    import comic_studio.web.routes_settings as rs

    class FakeLLM:
        def __init__(self, *a, **k): pass
        def raw_chat(self, *a, **k):
            raise RuntimeError("401 无效 key")
    monkeypatch.setattr(rs, "LLMClient", FakeLLM)
    with _client(tmp_path) as c:
        r = c.post("/api/settings/llm-test", json=_body())
        assert r.json() == {"ok": False, "detail": "RuntimeError: 401 无效 key"}


def test_llm_test_rejects_bad_provider_and_cross_site(tmp_path):
    with _client(tmp_path) as c:
        assert c.post("/api/settings/llm-test", json=_body(provider="xxx")).status_code == 422
        r = c.post("/api/settings/llm-test", json=_body(),
                   headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403
