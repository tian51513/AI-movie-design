# tests/test_api_llm_optimize.py
"""提示词优化接口（2026-08-25 需求）：文本框 ✨ 优化弹窗的后端。"""
import pytest
from fastapi.testclient import TestClient

from comic_studio.engine.db import Database
from comic_studio.engine.llm.provider import LLMClient, Usage
from comic_studio.web.app import create_app


class FakeClient(LLMClient):
    def __init__(self, replies):
        super().__init__("http://x", "k", "fake")
        self.replies, self.n, self.last_messages = list(replies), 0, None

    def raw_chat(self, messages, temperature=0.3):
        self.last_messages = messages
        r = self.replies[min(self.n, len(self.replies) - 1)]
        self.n += 1
        return r, Usage(10, 20)


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = Database(tmp_path / "s.db"); db.migrate()
    fake = FakeClient(["优化后的描述文本"])
    import comic_studio.web.routes_llm as rl
    monkeypatch.setattr(rl, "client_for_task", lambda db, task: fake)
    return db, fake, TestClient(create_app(tmp_path / "s.db", tmp_path / "data",
                                           start_workers=False))


def test_optimize_roundtrip(client):
    db, fake, c = client
    with c:
        r = c.post("/api/llm/optimize", json={
            "text": "他走进屋里", "kind": "shot_desc"})
        assert r.status_code == 200
        assert r.json()["text"] == "优化后的描述文本"
        system = fake.last_messages[0]["content"]
        assert "画面" in system or "镜头" in system  # 分镜描述优化指引
        # video_prompt 类型的系统词强调保持 H3 结构
        c.post("/api/llm/optimize", json={"text": "[Shot 1] …", "kind": "video_prompt"})
        assert "结构" in fake.last_messages[0]["content"]


def test_optimize_empty_text_422(client):
    _, _, c = client
    with c:
        assert c.post("/api/llm/optimize", json={"text": "  ", "kind": "x"}
                      ).status_code == 422


def test_optimize_prompt_is_routable_task(client):
    """optimize_prompt 是合法路由任务（设置页可改本地/线上）。"""
    _, _, c = client
    with c:
        r = c.put("/api/settings", json={"llm_routing": {"optimize_prompt": "local"}})
        assert r.status_code == 200
        assert c.get("/api/settings").json()["llm_routing"]["optimize_prompt"] == "local"


def test_optimize_rejects_oversized_text(client):
    """超大文本 422（防巨型 LLM 请求）。"""
    _, _, c = client
    with c:
        assert c.post("/api/llm/optimize",
                      json={"text": "x" * 20001}).status_code == 422


def test_optimize_appearance_variants_by_kind(client):
    """appearance 指引按资产类型分化（真机 2026-08-25：道具被角色化）。"""
    db, fake, c = client
    with c:
        c.post("/api/llm/optimize", json={"text": "红绸肚兜", "kind": "appearance:prop"})
        assert "材质" in fake.last_messages[0]["content"]
        assert "性别年龄" not in fake.last_messages[0]["content"]
        c.post("/api/llm/optimize", json={"text": "庭院", "kind": "appearance:scene"})
        assert "环境" in fake.last_messages[0]["content"]
        c.post("/api/llm/optimize", json={"text": "黑发少年", "kind": "appearance"})
        assert "性别：" in fake.last_messages[0]["content"]  # 默认仍角色（行模板版）


def test_appearance_template_labels(client):
    """角色外貌固定行模板（2026-08-26 需求）：分析与 ✨ 优化同一套标签。"""
    _, fake, c = client
    with c:
        c.post("/api/llm/optimize", json={"text": "黑发女子", "kind": "appearance"})
    system = fake.last_messages[0]["content"]
    for label in ("性别：", "年龄：", "发色发型：", "瞳色：", "肤色：", "体型：", "服装：", "配饰："):
        assert label in system, label
    from comic_studio.engine.llm.analyze import EXTRACT_SYSTEM
    for label in ("性别：", "发色发型：", "肤色：", "配饰："):
        assert label in EXTRACT_SYSTEM, label
    assert "未成年" in EXTRACT_SYSTEM and "未成年" in system  # 边界约束入词
