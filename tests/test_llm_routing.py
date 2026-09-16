# tests/test_llm_routing.py
import pytest

from comic_studio.engine.db import Database
from comic_studio.engine.llm.provider import LLMError, client_for_task, log_llm_call, Usage
from comic_studio.engine.settings import set_setting


def _db(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate(); return db


def test_local_task_uses_local_provider(tmp_path):
    db = _db(tmp_path)
    c = client_for_task(db, "extract_assets")
    assert c.model == "qwen3:14b"


def test_online_task_unconfigured_raises(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(LLMError, match="未配置"):
        client_for_task(db, "split_storyboards")


def test_online_task_configured(tmp_path):
    db = _db(tmp_path)
    set_setting(db, "llm_providers", {
        "local": {"base_url": "http://localhost:11434/v1", "api_key": "ollama", "model": "q"},
        "online": {"base_url": "https://api.example.com/v1", "api_key": "sk-x", "model": "big"},
    })
    c = client_for_task(db, "split_storyboards")
    assert c.model == "big"


def test_log_llm_call_writes_row(tmp_path):
    db = _db(tmp_path)
    log_llm_call(db, "extract_assets", "local", "qwen3:14b", Usage(100, 200))
    row = db.connect().execute("SELECT * FROM llm_calls").fetchone()
    assert row["task"] == "extract_assets" and row["completion_tokens"] == 200


def test_routing_value_can_pin_specific_model(tmp_path):
    """任务路由可点对点到具体模型（2026-08-28 需求：长文本任务用对话模型、
    精确任务用思考模型）。格式 "provider:model"——模型名自身含冒号（Ollama tag），
    只按首个冒号切分。"""
    db = _db(tmp_path)
    set_setting(db, "llm_providers", {
        "local": {"base_url": "http://localhost:11434/v1", "api_key": "ollama",
                  "model": "nsfw-qwen3.6:latest"},
        "online": {"base_url": "https://api.example.com/v1", "api_key": "sk-x", "model": "big"},
    })
    set_setting(db, "llm_routing", {
        "gen_video_prompt": "local:qwen3.5:4b",     # 钉非思考小模型跑长文本
        "extract_assets": "local",                    # 纯 provider = 默认模型
        "optimize_prompt": "online:gpt-x-large",     # 线上钉模型
    })
    assert client_for_task(db, "gen_video_prompt").model == "qwen3.5:4b"
    assert client_for_task(db, "extract_assets").model == "nsfw-qwen3.6:latest"
    assert client_for_task(db, "optimize_prompt").model == "gpt-x-large"
    # 未知 provider 仍报错
    set_setting(db, "llm_routing", {"extract_assets": "ghost:m"})
    with pytest.raises(LLMError, match="路由"):
        client_for_task(db, "extract_assets")


# ---------- 2026-09-17 按模型附加参数覆写（extra_body_models） ----------

def _ebm_db(tmp_path, extra_body=None, extra_body_models=None):
    db = _db(tmp_path)
    prov = {"base_url": "http://localhost:11434/v1", "api_key": "ollama", "model": "默认模型"}
    if extra_body is not None:
        prov["extra_body"] = extra_body
    if extra_body_models is not None:
        prov["extra_body_models"] = extra_body_models
    set_setting(db, "llm_providers", {"local": prov})
    set_setting(db, "llm_routing", {"extract_assets": "local"})
    return db


def test_extra_body_model_override_wins(tmp_path):
    """钉到有覆写的模型 → 用覆写；连接默认不生效。"""
    db = _ebm_db(tmp_path, extra_body={"reasoning_effort": "none"},
                 extra_body_models={"ornith_1.5": {"reasoning_effort": "low"}})
    set_setting(db, "llm_routing", {"extract_assets": "local:ornith_1.5"})
    c = client_for_task(db, "extract_assets")
    assert c.model == "ornith_1.5" and c.extra_body == {"reasoning_effort": "low"}


def test_extra_body_fallback_to_connection_default(tmp_path):
    """钉到无覆写的模型 → 回落连接默认附加参数。"""
    db = _ebm_db(tmp_path, extra_body={"reasoning_effort": "none"},
                 extra_body_models={"ornith_1.5": {"reasoning_effort": "low"}})
    set_setting(db, "llm_routing", {"extract_assets": "local:别的模型"})
    c = client_for_task(db, "extract_assets")
    assert c.extra_body == {"reasoning_effort": "none"}


def test_extra_body_override_for_default_model(tmp_path):
    """路由不钉模型（用连接默认模型）时，默认模型的覆写同样生效。"""
    db = _ebm_db(tmp_path, extra_body={"reasoning_effort": "none"},
                 extra_body_models={"默认模型": {"reasoning_effort": "low"}})
    c = client_for_task(db, "extract_assets")
    assert c.model == "默认模型" and c.extra_body == {"reasoning_effort": "low"}


def test_extra_body_override_needs_dict_values(tmp_path):
    """PUT 校验：extra_body_models 必须是 {模型名: 对象}——值非对象 422。"""
    from fastapi.testclient import TestClient

    from comic_studio.web.app import create_app
    app = create_app(tmp_path / "s.db", tmp_path / "data", start_workers=False)
    with TestClient(app) as c:
        r = c.put("/api/settings", json={"llm_providers": {
            "local": {"extra_body_models": {"m": {"reasoning_effort": "none"}}}}})
        assert r.status_code == 200, r.text
        assert c.get("/api/settings").json()["llm_providers"]["local"]["extra_body_models"] == \
            {"m": {"reasoning_effort": "none"}}
        assert c.put("/api/settings", json={"llm_providers": {
            "local": {"extra_body_models": {"m": "not-an-object"}}}}).status_code == 422
