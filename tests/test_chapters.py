# tests/test_chapters.py
"""P7-E 多章节支持（借鉴 NovelFlow）：中英文章节正则切分 + 按章范围拆分镜。"""
import io

from fastapi.testclient import TestClient
from types import SimpleNamespace as NS

from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project, get_project
from comic_studio.engine.shots import persist_shots
from comic_studio.web.app import create_app

NOVEL = """第一章 初遇
林晨推开图书馆的门，遇见了白发少女。

第二章 同行
两人结伴穿过雨巷，共撑一把伞。

第十二章 远行
多年后，林晨独自远行。
"""

NO_CHAPTER = "林晨推门。庭院里站着一个白发少女。"


def test_parse_chapters_mixed_numbering():
    from comic_studio.engine.chapters import parse_chapters
    chs = parse_chapters(NOVEL)
    assert [c["title"] for c in chs] == ["初遇", "同行", "远行"]
    assert [c["idx"] for c in chs] == [1, 2, 12]
    assert chs[0]["start"] == 0
    assert "图书馆" in NOVEL[chs[0]["start"]:chs[0]["end"]]
    assert "雨巷" in NOVEL[chs[1]["start"]:chs[1]["end"]]
    assert "远行" in NOVEL[chs[2]["start"]:chs[2]["end"]]
    # 无章节标题 → 空列表（单章项目）
    assert parse_chapters(NO_CHAPTER) == []
    # 英文章节
    en = parse_chapters("Chapter 1: Begin\nA\n\nChapter 2: End\nB")
    assert [c["idx"] for c in en] == [1, 2] and en[0]["title"] == "Begin"


def test_create_project_stores_chapters(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "章节剧", "9:16", NOVEL)["id"]
    import json
    chs = json.loads(get_project(db, pid)["chapters_json"] or "[]")
    assert len(chs) == 3 and chs[1]["title"] == "同行"
    # 无章节 → 空
    pid2 = create_project(db, tmp_path / "data", "单章剧", "9:16", NO_CHAPTER)["id"]
    assert json.loads(get_project(db, pid2)["chapters_json"] or "[]") == []


def test_split_respects_chapter_range(tmp_path):
    """拆分镜按章范围：只吃选定章的文本。"""
    from comic_studio.engine.llm.storyboard import split_storyboards
    from tests.test_storyboard_split import FakeLLM, CHUNK
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "范围剧", "9:16", NOVEL)["id"]
    captured = []

    class Cap(FakeLLM):
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            captured.append(messages[-1]["content"])
            return super().raw_chat(messages, temperature=temperature, max_tokens=max_tokens)

    split_storyboards(db, tmp_path / "data", pid, client_factory=lambda t: Cap([CHUNK.format(desc="镜", cid=1)]),
                      chapter_range=(2, 2))
    assert len(captured) == 1
    body = captured[0].split("小说文本：\n", 1)[1]
    assert "雨巷" in body and "图书馆" not in body and "远行" not in body


def test_chapters_endpoint_and_split_payload(tmp_path):
    with TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data",
                               start_workers=False)) as c:
        pid = c.post("/api/projects", data={"name": "端点剧", "aspect_ratio": "16:9"},
                     files={"novel": ("n.txt", io.BytesIO(NOVEL.encode()), "text/plain")}).json()["id"]
        r = c.get(f"/api/projects/{pid}/chapters")
        assert r.status_code == 200 and len(r.json()) == 3
        assert r.json()[2]["title"] == "远行"
        from comic_studio.engine.projects import set_stage
        set_stage(c.app.state.db, pid, "assets_ready")
        r = c.post(f"/api/projects/{pid}/split-storyboards",
                   json={"chapter_from": 1, "chapter_to": 2})
        assert r.status_code == 202
        import json as _json
        from comic_studio.engine import jobs as J
        row = J.latest_job(c.app.state.db, pid, "split_storyboards")
        assert _json.loads(row["payload_json"]).get("chapter_range") == [1, 2]
