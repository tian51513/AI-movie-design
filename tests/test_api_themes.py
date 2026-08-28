# tests/test_api_themes.py
"""主题生成项目（2026-08-25 需求）：GET /apiThemes + POST from-theme（LLM 生成正文建项目）。"""
import pytest
from fastapi.testclient import TestClient

from comic_studio.engine.db import Database
from comic_studio.engine.llm.provider import LLMClient, Usage
from comic_studio.web.app import create_app


class FakeLLM(LLMClient):
    def __init__(self, reply):
        super().__init__("http://x", "k", "fake")
        self.reply, self.last_user = reply, ""

    def raw_chat(self, messages, temperature=0.3):
        self.last_user = messages[-1]["content"]
        return self.reply, Usage(100, 5000)


STORY = "晨光穿过林间。\n\n" + "少年推门而入，见到久候的师父。" * 60  # 足够长


@pytest.fixture
def app_client(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    return db, TestClient(create_app(tmp_path / "s.db", tmp_path / "data",
                                     start_workers=False))


def test_themes_listing(app_client):
    _, c = app_client
    with c:
        r = c.get("/api/themes")
        assert r.status_code == 200
        items = r.json()
        assert len(items) >= 10
        assert not any("NTR" in t["name"] or "催眠" in t["name"] for t in items)


def test_create_project_from_theme(app_client, monkeypatch):
    db, c = app_client
    fake = FakeLLM(STORY)
    import comic_studio.web.routes_projects as rp
    monkeypatch.setattr(rp, "client_for_task", lambda db, task: fake)
    with c:
        themes = c.get("/api/themes").json()
        r = c.post("/api/projects/from-theme", json={
            "theme_id": themes[0]["id"], "aspect_ratio": "9:16",
            "protagonist": "林晨"})
        assert r.status_code == 201, r.text
        proj = r.json()
        assert proj["stage"] == "created" and proj["aspect_ratio"] == "9:16"
        assert "林晨" in fake.last_user  # 主角名进了生成上下文
        assert themes[0]["name"][:4] in fake.last_user
        from pathlib import Path
        novel = Path(c.app.state.data_dir) / proj["novel_path"] if "novel_path" in proj else None
    # 正文落盘（novel_path 不在公共列——直接扫项目目录）
    from pathlib import Path
    texts = list((Path(c.app.state.data_dir) / "projects").rglob("*.txt"))
    assert texts and len(texts[0].read_text(encoding="utf-8")) > 500


def test_create_from_theme_short_story_422(app_client, monkeypatch):
    db, c = app_client
    import comic_studio.web.routes_projects as rp
    monkeypatch.setattr(rp, "client_for_task",
                        lambda db, task: FakeLLM("太短了"))
    with c:
        themes = c.get("/api/themes").json()
        r = c.post("/api/projects/from-theme", json={
            "theme_id": themes[0]["id"], "aspect_ratio": "16:9"})
        assert r.status_code == 422


MD_OK = ("### 📐 测试项（全年龄）\n\n"
         "1. **主题名称：**《测试主题》\n**描述：** " + "温馨治愈的描述。" * 10 + "\n")
MD_ADULT = ("### 🔥 成人向（情欲向）\n\n"
            "1. **主题名称：**《不良主题》\n**描述：** " + "描述。" * 20 + "\n")


def test_import_and_delete(app_client):
    _, c = app_client
    with c:
        r = c.post("/api/themes/import",
                   files={"file": ("my.md", MD_OK.encode(), "text/markdown")})
        assert r.status_code == 200 and r.json()["imported"] == 1
        names = [t["name"] for t in c.get("/api/themes").json()]
        assert "测试主题" in names
        tid = next(t["id"] for t in c.get("/api/themes").json()
                   if t["name"] == "测试主题")
        assert c.delete(f"/api/themes/{tid}").status_code == 200
        assert c.delete(f"/api/themes/{tid}").status_code == 404


def test_import_skips_adult_section(app_client):
    _, c = app_client
    with c:
        r = c.post("/api/themes/import",
                   files={"file": ("x.md", MD_ADULT.encode(), "text/markdown")})
        assert r.status_code == 422  # 成人向节被跳过后无条目


def test_import_rejects_non_md(app_client):
    _, c = app_client
    with c:
        r = c.post("/api/themes/import",
                   files={"file": ("x.txt", b"...", "text/plain")})
        assert r.status_code == 422


def test_create_from_theme_word_count_controls_target(app_client, monkeypatch):
    """字数参数（2026-08-27 用户需求）：主题生成正文过长（真机 21862 字）导致
    分镜分块过大撞上下文截断——创建时可传 word_count 控制目标字数。"""
    class SysCaptureLLM(FakeLLM):
        def __init__(self, reply):
            super().__init__(reply)
            self.last_system = ""
        def raw_chat(self, messages, temperature=0.3):
            self.last_system = messages[0]["content"]
            return super().raw_chat(messages, temperature)

    db, c = app_client
    fake = SysCaptureLLM(STORY)
    import comic_studio.web.routes_projects as rp
    monkeypatch.setattr(rp, "client_for_task", lambda db, task: fake)
    with c:
        themes = c.get("/api/themes").json()
        r = c.post("/api/projects/from-theme", json={
            "theme_id": themes[0]["id"], "aspect_ratio": "9:16", "word_count": 3000})
        assert r.status_code == 201, r.text
        assert "3000" in fake.last_system      # 目标字数注入 system 提示词
        assert "8000~12000" not in fake.last_system  # 默认目标被替换


def test_create_from_theme_word_count_out_of_range_422(app_client, monkeypatch):
    db, c = app_client
    import comic_studio.web.routes_projects as rp
    monkeypatch.setattr(rp, "client_for_task",
                        lambda db, task: FakeLLM(STORY))
    with c:
        themes = c.get("/api/themes").json()
        r = c.post("/api/projects/from-theme", json={
            "theme_id": themes[0]["id"], "aspect_ratio": "9:16", "word_count": 50})
        assert r.status_code == 422


def test_preview_from_theme_generates_without_creating(app_client, monkeypatch):
    """两步创建第一步：预览只生成正文不建项目（2026-08-27 需求：用户确认文本再创建）。"""
    db, c = app_client
    captured = {}

    class CapLLM(FakeLLM):
        def raw_chat(self, messages, temperature=0.3):
            captured["system"] = messages[0]["content"]
            return super().raw_chat(messages, temperature=temperature)
    fake = CapLLM(STORY)
    import comic_studio.web.routes_projects as rp
    monkeypatch.setattr(rp, "client_for_task", lambda db, task: fake)
    with c:
        themes = c.get("/api/themes").json()
        before = db.connect().execute("SELECT COUNT(*) c FROM projects").fetchone()["c"]
        r = c.post("/api/projects/from-theme/preview", json={
            "theme_id": themes[0]["id"], "aspect_ratio": "9:16",
            "protagonist": "林晨", "word_count": 3000,
            "extra_prompt": "加入雨天告白情节"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["text"].startswith("晨光穿过林间")
        assert "3000" in captured["system"]  # 字数要求进了 system
        assert "雨天告白" in fake.last_user  # 用户补充描述进了上下文
        after = db.connect().execute("SELECT COUNT(*) c FROM projects").fetchone()["c"]
        assert before == after  # 没建项目


def test_create_from_theme_with_confirmed_text_skips_llm(app_client, monkeypatch):
    """两步创建第二步：带确认/编辑后的 text 直接建项目，不再调 LLM。"""
    db, c = app_client
    import comic_studio.web.routes_projects as rp

    class Boom(LLMClient):
        def __init__(self): super().__init__("http://x", "k", "fake")
        def raw_chat(self, *a, **k): raise AssertionError("确认文本后不应再调 LLM")
    monkeypatch.setattr(rp, "client_for_task", lambda db, task: Boom())
    with c:
        themes = c.get("/api/themes").json()
        edited = "用户改过的正文开头。\n\n" + "确认后的内容。" * 40
        r = c.post("/api/projects/from-theme", json={
            "theme_id": themes[0]["id"], "aspect_ratio": "9:16",
            "name": "确认剧", "text": edited})
        assert r.status_code == 201, r.text
        from pathlib import Path
        texts = list((Path(c.app.state.data_dir) / "projects" / "确认剧").rglob("*.txt"))
        assert texts and texts[0].read_text(encoding="utf-8") == edited


def test_create_from_theme_confirmed_text_too_short_422(app_client):
    _, c = app_client
    with c:
        themes = c.get("/api/themes").json()
        r = c.post("/api/projects/from-theme", json={
            "theme_id": themes[0]["id"], "text": "太短"})
        assert r.status_code == 422


def test_gen_story_system_contains_drama_craft(app_client):
    """P7-I 第三批编剧层（借鉴短剧厂）：钩子/情绪流变/断章/语速公式入 system。"""
    import comic_studio.web.routes_projects as rp
    s = rp.GEN_STORY_SYSTEM
    for token in ("黄金开头", "钩子", "情绪流变", "断章卡点", "3.5~4.5 字/秒",
                  "12~18 字", "反向灌输"):
        assert token in s, token
