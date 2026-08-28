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
