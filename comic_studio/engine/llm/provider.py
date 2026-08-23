"""LLM 统一客户端：openai SDK，本地 Ollama 与线上端点同构（spec §9.1）。"""
import json
import re
from dataclasses import dataclass
from typing import TypeVar

from openai import OpenAI
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    pass


@dataclass
class Usage:
    prompt_tokens: int
    completion_tokens: int


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 600):
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        self.model = model

    def raw_chat(self, messages: list[dict], temperature: float = 0.3) -> tuple[str, Usage]:
        resp = self._client.chat.completions.create(
            model=self.model, messages=messages, temperature=temperature)
        text = resp.choices[0].message.content or ""
        usage = Usage(getattr(resp.usage, "prompt_tokens", 0) or 0,
                      getattr(resp.usage, "completion_tokens", 0) or 0)
        return text, usage


def parse_json_text(text: str) -> dict:
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", t, re.DOTALL)
    if fence:
        t = fence.group(1)
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        raise LLMError("输出中找不到 JSON 对象")
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"JSON 解析失败: {e}") from e


def ask_json(client: LLMClient, system: str, user: str, max_attempts: int = 3) -> tuple[dict, Usage]:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    last: Exception | None = None
    for _ in range(max_attempts):
        text, usage = client.raw_chat(messages)
        try:
            return parse_json_text(text), usage
        except LLMError as e:
            last = e
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": f"上面的输出不是合法 JSON（{e}）。请重新输出，只输出一个合法 JSON 对象。"}]
    raise LLMError(f"{max_attempts} 次尝试均无法解析 JSON: {last}")


def ask_validated(client: LLMClient, system: str, user: str,
                  schema_cls: type[T], max_attempts: int = 3) -> tuple[T, Usage]:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    last: str = ""
    for _ in range(max_attempts):
        text, usage = client.raw_chat(messages)
        try:
            data = parse_json_text(text)
        except LLMError as e:
            last = str(e)
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": f"输出不是合法 JSON（{e}），请只输出一个合法 JSON 对象。"}]
            continue
        try:
            return schema_cls.model_validate(data), usage
        except ValidationError as e:
            last = str(e)
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": f"JSON 不符合要求的结构，错误如下，请修正后重新输出完整 JSON：\n{e}"}]
    raise LLMError(f"{max_attempts} 次尝试均未通过 {schema_cls.__name__} 校验: {last}")

# 追加到 comic_studio/engine/llm/provider.py 末尾
from ..db import Database          # noqa: E402（放末尾避免环：db 不依赖本模块，顶部导入亦可）
from ..settings import get_setting  # noqa: E402


def client_for_task(db: Database, task: str) -> "LLMClient":
    routing = get_setting(db, "llm_routing")
    providers = get_setting(db, "llm_providers")
    name = routing.get(task)
    if not name or name not in providers:
        raise LLMError(f"任务 {task} 的路由 {name!r} 不在 llm_providers 中")
    p = providers[name]
    if not p.get("base_url"):
        raise LLMError(f"线上 LLM 未配置：settings.llm_providers.{name}.base_url 为空")
    return LLMClient(base_url=p["base_url"], api_key=p.get("api_key") or "none",
                     model=p["model"])


def log_llm_call(db: Database, task: str, provider: str, model: str, usage: Usage) -> None:
    conn = db.connect()
    conn.execute(
        "INSERT INTO llm_calls (task, provider, model, prompt_tokens, completion_tokens) "
        "VALUES (?,?,?,?,?)", (task, provider, model, usage.prompt_tokens, usage.completion_tokens))
    conn.commit()
