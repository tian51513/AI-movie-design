# tests/test_api_settings.py
from fastapi.testclient import TestClient

from comic_studio.web.app import create_app

ROUTING_DEFAULTS = {   # asr_cleanup（2026-09-05 P10C）随后端默认同步
    "asr_cleanup": "local",
    "extract_assets": "local",
    "fix_appearance": "local",
    "split_storyboards": "online",
    "gen_video_prompt": "online",
    "optimize_prompt": "online",
    "gen_story": "online",
    "describe_shot": "local",
}


def _client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data", start_workers=False))


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


def test_put_comfy_base_url(tmp_path):
    with _client(tmp_path) as c:
        resp = c.put("/api/settings", json={"comfy": {"base_url": "http://192.168.3.1:8188"}})
        assert resp.status_code == 200
        assert c.get("/api/settings").json()["comfy"]["base_url"] == "http://192.168.3.1:8188"


def test_put_comfy_partial_update_keeps_base_url(tmp_path):
    """事故复盘（2026-09-01）：comfy 局部更新不得把未提供的键冲成默认——
    全量 model_dump 曾让 base_url 被覆盖成空串，36k 渲染任务以 AttributeError 批量失败。"""
    with _client(tmp_path) as c:
        assert c.put("/api/settings", json={"comfy": {"base_url": "http://127.0.0.1:8188"}}).status_code == 200
        resp = c.put("/api/settings", json={"comfy": {"mute_quiet_shots": True}})
        assert resp.status_code == 200
        assert c.get("/api/settings").json()["comfy"]["base_url"] == "http://127.0.0.1:8188"


def test_put_comfy_empty_base_url_rejected(tmp_path):
    """base_url 显式传空 → 422（渲染/参考图/快车道全依赖它，误清空必须在门口拦下）。"""
    with _client(tmp_path) as c:
        assert c.put("/api/settings", json={"comfy": {"base_url": "http://127.0.0.1:8188"}}).status_code == 200
        resp = c.put("/api/settings", json={"comfy": {"base_url": ""}})
        assert resp.status_code == 422
        assert c.get("/api/settings").json()["comfy"]["base_url"] == "http://127.0.0.1:8188"


def test_put_template_map_roundtrip(tmp_path):
    """PUT template_map {t2i: x} 能 roundtrip 读回。"""
    with _client(tmp_path) as c:
        resp = c.put("/api/settings", json={"template_map": {"t2i": "my_custom"}})
        assert resp.status_code == 200
        body = c.get("/api/settings").json()
        assert body["template_map"]["t2i"] == "my_custom"
        # 其他键保留默认
        assert body["template_map"]["character_views"] == "character_views"


def test_put_template_map_rejects_unknown_key(tmp_path):
    """template_map 未知键返回 422。"""
    with _client(tmp_path) as c:
        resp = c.put("/api/settings", json={"template_map": {"bad_key": "x"}})
        assert resp.status_code == 422


def test_providers_partial_put_preserves_unset(tmp_path):
    """M15（2026-09-05 审计）：llm_providers PUT 全量 model_dump——部分字段
    PUT 会把未提供键冲默认（09-01 comfy 事故同款）。exclude_unset 后局部
    PUT 只改提供的键。"""
    with _client(tmp_path) as c:
        r = c.put("/api/settings", json={"llm_providers": {
            "online": {"base_url": "http://a", "api_key": "k",
                       "model": "m1", "extra_body": None}}})
        assert r.status_code == 200, r.text
        r2 = c.put("/api/settings", json={"llm_providers": {
            "online": {"model": "m2"}}})
        assert r2.status_code == 200, r2.text
        prov = c.get("/api/settings").json()["llm_providers"]["online"]
        assert prov["model"] == "m2"
        assert prov["base_url"] == "http://a" and prov["api_key"] == "k"
