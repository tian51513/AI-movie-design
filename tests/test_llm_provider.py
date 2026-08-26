import pytest

from comic_studio.engine.llm import provider
from comic_studio.engine.llm.provider import (
    LLMClient, LLMError, Usage, ask_json, ask_validated, parse_json_text)
from comic_studio.engine.llm.schemas import AssetsAnalysis


def _client(responses: list[str]) -> LLMClient:
    c = LLMClient(base_url="http://x", api_key="k", model="m")
    calls = {"n": 0}

    def fake_raw_chat(messages, temperature=0.3):
        i = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        return responses[i], Usage(10, 20)
    c.raw_chat = fake_raw_chat
    c.call_count = lambda: calls["n"]
    return c


def test_parse_json_plain_and_fenced():
    assert parse_json_text('{"a": 1}') == {"a": 1}
    assert parse_json_text('```json\n{"a": 2}\n```') == {"a": 2}
    assert parse_json_text('前言 {"a": 3} 后记') == {"a": 3}


def test_parse_json_garbage_raises():
    with pytest.raises(LLMError):
        parse_json_text("完全不是JSON")


def test_ask_json_retries_then_succeeds():
    c = _client(["不是json", '{"a": 1}'])
    data, usage = ask_json(c, "sys", "usr")
    assert data == {"a": 1} and usage.completion_tokens == 20
    assert c.call_count() == 2


def test_ask_validated_feeds_error_back():
    good = '{"characters":[{"name":"萧炎","appearance":"黑发"}],"scenes":[],"props":[]}'
    c = _client(["{}", good])  # 第一次缺 sections
    result, _ = ask_validated(c, "s", "u", AssetsAnalysis)
    assert result.characters[0].name == "萧炎"


def test_ask_validated_gives_up_after_max():
    c = _client(["{}"])  # 永远坏
    with pytest.raises(LLMError):
        ask_validated(c, "s", "u", AssetsAnalysis, max_attempts=2)
    assert c.call_count() == 2


def test_raw_chat_raises_on_truncated_output():
    """finish_reason=length（输出撞上下文/长度上限被截断）必须立刻报错——
    截断的 JSON 重试必败，且 ask_validated 重试还会追加失败输出挤占空间
    （真机 2026-08-27：job 582 分块 8127 字，7006+9378=16384 恰好 num_ctx，3 次徒劳重试）。"""
    from types import SimpleNamespace as NS

    class FakeCompletions:
        def create(self, **kw):
            msg = NS(content='{"shots":[{"prop_ids')
            return NS(choices=[NS(message=msg, finish_reason="length")],
                      usage=NS(prompt_tokens=7006, completion_tokens=9378))

    class FakeClient:
        chat = type("C", (), {"completions": FakeCompletions()})()

    c = LLMClient("http://x", "k", "m")
    c._client = FakeClient()
    with pytest.raises(LLMError, match="截断"):
        c.raw_chat([{"role": "user", "content": "hi"}])


def test_raw_chat_handles_none_choices_and_usage():
    """协议不匹配（如 Anthropic 端点）时 resp.choices/usage 可能为 None——不崩，返回空文本。"""
    from comic_studio.engine.llm.provider import LLMClient

    class FakeCompletions:
        def create(self, **kw):
            class R:  # 模拟 openai SDK 对非 OpenAI 响应的解析结果
                choices = None
                usage = None
            return R()

    class FakeClient:
        chat = type("C", (), {"completions": FakeCompletions()})()

    c = LLMClient("http://x", "k", "m")
    c._client = FakeClient()
    text, usage = c.raw_chat([{"role": "user", "content": "hi"}], max_tokens=8)
    assert text == "" and usage.prompt_tokens == 0 and usage.completion_tokens == 0
