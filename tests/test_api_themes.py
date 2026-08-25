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
