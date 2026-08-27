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


def test_raw_chat_raises_when_no_choices():
    """choices 缺失/为空必须报错——LM Studio 对未知端点返回 HTTP 200 + error JSON
    （真机 2026-08-27：base_url 误填 /api，openai SDK 解析出错体得 choices=None，
    raw_chat 静默返回空串，被上层当成"成功但空文本"，前端优化弹窗文本被清空）。"""
    from types import SimpleNamespace as NS

    def _make(choices_val):
        class FakeCompletions:
            def create(self, **kw):
                class R:
                    choices = choices_val
                    usage = None
                return R()

        class FakeClient:
            chat = type("C", (), {"completions": FakeCompletions()})()
        return FakeClient()

    for choices_val in (None, []):
        c = LLMClient("http://x", "k", "m")
        c._client = _make(choices_val)
        with pytest.raises(LLMError, match="choices|端点"):
            c.raw_chat([{"role": "user", "content": "hi"}])


def test_raw_chat_strips_inline_think_tags():
    """Qwen3 系思考模型可能把 <think>…</think> 内联在 content 里——剥掉再返回正文。"""
    from types import SimpleNamespace as NS

    class FakeCompletions:
        def create(self, **kw):
            msg = NS(content="<think>用户要两个字，我该答……</think>\n\n好的")
            return NS(choices=[NS(message=msg, finish_reason="stop")],
                      usage=NS(prompt_tokens=1, completion_tokens=2))

    class FakeClient:
        chat = type("C", (), {"completions": FakeCompletions()})()

    c = LLMClient("http://x", "k", "m")
    c._client = FakeClient()
    text, _ = c.raw_chat([{"role": "user", "content": "hi"}])
    assert text == "好的"


def test_raw_chat_reasoning_only_raises():
    """思考型模型（LM Studio/DeepSeek 把思考放 reasoning_content）：正文为空只剩思考
    时必须显式报错，不能当空文本返回。"""
    from types import SimpleNamespace as NS

    class FakeCompletions:
        def create(self, **kw):
            msg = NS(content="", reasoning_content="Let me think about this...")
            return NS(choices=[NS(message=msg, finish_reason="stop")],
                      usage=NS(prompt_tokens=1, completion_tokens=99))

    class FakeClient:
        chat = type("C", (), {"completions": FakeCompletions()})()

    c = LLMClient("http://x", "k", "m")
    c._client = FakeClient()
    with pytest.raises(LLMError, match="思考"):
        c.raw_chat([{"role": "user", "content": "hi"}])


def test_normalize_base_url():
    """Ollama/LM Studio 常见误填统一归一到 OpenAI 兼容根 /v1；线上深路径不动。"""
    from comic_studio.engine.llm.provider import normalize_base_url as nb
    assert nb("http://127.0.0.1:1234/api") == "http://127.0.0.1:1234/v1"      # Ollama 原生风格误填
    assert nb("http://127.0.0.1:1234") == "http://127.0.0.1:1234/v1"          # 裸主机
    assert nb("http://127.0.0.1:1234/") == "http://127.0.0.1:1234/v1"
    assert nb("http://127.0.0.1:1234/v1") == "http://127.0.0.1:1234/v1"       # 已正确
    assert nb("http://127.0.0.1:1234/v1/") == "http://127.0.0.1:1234/v1"
    assert nb("http://127.0.0.1:1234/v1/chat/completions") == "http://127.0.0.1:1234/v1"  # 误粘整条端点
    assert nb("http://127.0.0.1:1234/api/chat/completions") == "http://127.0.0.1:1234/v1"
    assert nb("http://127.0.0.1:1234/api/v0") == "http://127.0.0.1:1234/v1"   # LM Studio 原生前缀
    assert nb("localhost:11434") == "http://localhost:11434/v1"               # 缺 scheme
    assert nb("https://open.bigmodel.cn/api/paas/v4") == "https://open.bigmodel.cn/api/paas/v4"  # 线上深路径保持


def test_client_normalizes_and_passes_extra_body():
    """LLMClient 构造时规范化 base_url（Ollama/LM Studio 误填自动纠正）；
    配置了 extra_body 时透传给 create（思考模型屏蔽思考等场景）。"""
    captured = {}

    class FakeCompletions:
        def create(self, **kw):
            captured.update(kw)
            from types import SimpleNamespace as NS
            msg = NS(content="ok")
            return NS(choices=[NS(message=msg, finish_reason="stop")], usage=None)

    class FakeClient:
        chat = type("C", (), {"completions": FakeCompletions()})()

    c = LLMClient("http://127.0.0.1:1234/api", "k", "m",
                  extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    assert c.base_url == "http://127.0.0.1:1234/v1"
    c._client = FakeClient()
    c.raw_chat([{"role": "user", "content": "hi"}])
    assert captured["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
