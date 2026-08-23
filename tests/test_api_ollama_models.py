# tests/test_api_ollama_models.py
from fastapi.testclient import TestClient

from comic_studio.web.app import create_app


def _client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data"))


def test_ollama_models_proxies_tags_and_strips_v1(tmp_path, monkeypatch):
    captured = {}

    def fake_fetch(root_url):
        captured["url"] = root_url
        return ["qwen3:14b", "qwen3:8b", "gemma3:12b"]

    monkeypatch.setattr("comic_studio.web.routes_settings._fetch_ollama_models", fake_fetch)
    with _client(tmp_path) as c:
        resp = c.get("/api/settings/ollama-models",
                     params={"base_url": "http://localhost:11434/v1"})
        assert resp.status_code == 200
        assert resp.json() == {"models": ["qwen3:14b", "qwen3:8b", "gemma3:12b"]}
        assert captured["url"] == "http://localhost:11434"  # /v1 已剥离


def test_ollama_models_unreachable_returns_502(tmp_path, monkeypatch):
    def fake_fetch(root_url):
        raise ConnectionError("refused")

    monkeypatch.setattr("comic_studio.web.routes_settings._fetch_ollama_models", fake_fetch)
    with _client(tmp_path) as c:
        resp = c.get("/api/settings/ollama-models",
                     params={"base_url": "http://localhost:11434/v1"})
        assert resp.status_code == 502
        assert "Ollama" in resp.json()["detail"] or "refused" in resp.json()["detail"]


def test_ollama_models_requires_base_url(tmp_path):
    with _client(tmp_path) as c:
        assert c.get("/api/settings/ollama-models").status_code == 422


def test_fetch_strips_trailing_v1_and_slashes():
    from comic_studio.web.routes_settings import _ollama_root
    assert _ollama_root("http://localhost:11434/v1") == "http://localhost:11434"
    assert _ollama_root("http://localhost:11434/v1/") == "http://localhost:11434"
    assert _ollama_root("http://192.168.3.1:11434") == "http://192.168.3.1:11434"


def test_ollama_models_rejects_cross_site_browser_request(tmp_path, monkeypatch):
    monkeypatch.setattr("comic_studio.web.routes_settings._fetch_ollama_models",
                        lambda root: ["qwen3:14b"])
    with _client(tmp_path) as c:
        resp = c.get("/api/settings/ollama-models",
                     params={"base_url": "http://localhost:11434/v1"},
                     headers={"Sec-Fetch-Site": "cross-site"})
        assert resp.status_code == 403
        # same-origin 与无头（curl）放行
        ok1 = c.get("/api/settings/ollama-models",
                    params={"base_url": "http://localhost:11434/v1"},
                    headers={"Sec-Fetch-Site": "same-origin"})
        ok2 = c.get("/api/settings/ollama-models",
                    params={"base_url": "http://localhost:11434/v1"})
        assert ok1.status_code == 200 and ok2.status_code == 200
