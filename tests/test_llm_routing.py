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
