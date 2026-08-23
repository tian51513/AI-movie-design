# tests/test_api_settings.py
from fastapi.testclient import TestClient

from comic_studio.web.app import create_app

ROUTING_DEFAULTS = {
    "extract_assets": "local",
    "fix_appearance": "local",
    "split_storyboards": "online",
    "gen_video_prompt": "online",
}


def _client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data"))


def test_get_returns_effective_defaults(tmp_path):
    with _client(tmp_path) as c:
        body = c.get("/api/settings").json()
        assert body["llm_providers"]["local"]["model"] == "qwen3:14b"
        assert body["llm_routing"] == ROUTING_DEFAULTS


def test_put_roundtrip_persists(tmp_path):
    payload = {
        "llm_providers": {
            "local": {"base_url": "http://localhost:11434/v1", "api_key": "ollama",
                      "model": "qwen3:32b"},
            "online": {"base_url": "https://api.example.com/v1", "api_key": "sk-x",
                       "model": "deepseek-chat"},
        },
        "llm_routing": {"extract_assets": "online"},
    }
    with _client(tmp_path) as c:
        resp = c.put("/api/settings", json=payload)
        assert resp.status_code == 200
        body = c.get("/api/settings").json()
        assert body["llm_providers"]["local"]["model"] == "qwen3:32b"
        assert body["llm_providers"]["online"]["base_url"] == "https://api.example.com/v1"
        assert body["llm_routing"]["extract_assets"] == "online"
        # 未提供的路由键保留默认（深合并语义）
        assert body["llm_routing"]["split_storyboards"] == "online"


def test_put_rejects_routing_to_unknown_provider(tmp_path):
    payload = {"llm_routing": {"extract_assets": "nonexistent"}}
    with _client(tmp_path) as c:
        resp = c.put("/api/settings", json=payload)
        assert resp.status_code == 422


def test_put_rejects_unknown_task_or_provider_keys(tmp_path):
    with _client(tmp_path) as c:
        assert c.put("/api/settings", json={"llm_routing": {"unknown_task": "local"}}).status_code == 422
        assert c.put("/api/settings", json={"llm_providers": {
            "bad_name": {"base_url": "http://x", "api_key": "k", "model": "m"}}}).status_code == 422


def test_put_rejects_malformed_provider(tmp_path):
    with _client(tmp_path) as c:
        resp = c.put("/api/settings", json={"llm_providers": {"local": {"base_url": 123}}})
        assert resp.status_code == 422
