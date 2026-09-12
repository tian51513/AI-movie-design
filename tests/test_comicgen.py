# tests/test_comicgen.py
"""小说转漫画（2026-09-12）：第五种项目类型。"""
from comic_studio.engine.db import Database

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _comic_project(tmp_path, **kw):
    from comic_studio.engine.projects import create_project
    db = Database(tmp_path / "s.db"); db.migrate()
    defaults = dict(comic_mode="comic_output", dialogue_mode="bubble",
                    target_pages=0, image_size="1024x1536",
                    quality_tier="standard")
    defaults.update(kw)
    return db, create_project(db, tmp_path / "data", "漫画剧", "16:9",
                               "正文" * 100, **defaults)["id"]


def test_migration_36_comic_columns(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    conn = db.connect()
    cols = {c[1] for c in conn.execute("PRAGMA table_info(projects)")}
    assert {"dialogue_mode", "target_pages", "image_size", "quality_tier"} <= cols


def test_comic_project_created(tmp_path):
    db, pid = _comic_project(tmp_path)
    from comic_studio.engine.projects import get_project
    p = get_project(db, pid)
    assert p["comic_mode"] == "comic_output"
    assert p["dialogue_mode"] == "bubble"
    assert p["target_pages"] == 0
    assert p["image_size"] == "1024x1536"
    assert p["quality_tier"] == "standard"


# ---- Task 2: 漫画分镜拆解（模式分支） ----

_COMIC_REPLY = {
    "shots": [{
        # workflow_type 故意给视频值——断言引擎机械覆写为 comic，而不是赌 LLM 自觉
        "text_span": "少年推开门",
        "description": "室内玄关场景，少年推开门，回头看向屋内",
        "shot_type": "", "camera": {"景别": "中景"}, "duration": 5,
        "workflow_type": "ref2va",
        "dialogue": [{"speaker": "少年", "line": "我回来了"}],
    }],
}


def _split_fake(seen, reply):
    """构造 split_storyboards 的假 LLM：记录 system 提示词，回固定 JSON。"""
    import json as _json
    from comic_studio.engine.llm.provider import LLMClient, Usage

    class FakeSplit(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "m")

        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            seen["system"] = messages[0]["content"]
            return _json.dumps(reply, ensure_ascii=False), Usage(10, 20)

    return lambda task: FakeSplit()


def test_comic_storyboard_split(tmp_path, monkeypatch):
    """comic_output 项目拆解：system 提示词走漫画页分支（场景+人物+对白，
    无运镜/声音/时长要求），全部镜 workflow_type 机械固定 'comic'，
    dialogue 照常进 ledger（speaker+line）。"""
    import json
    from comic_studio.engine.llm.provider import Usage
    from comic_studio.engine.llm.storyboard import split_storyboards
    from comic_studio.engine.projects import set_stage
    from comic_studio.engine.shots import list_shots

    db, pid = _comic_project(tmp_path)
    set_stage(db, pid, "assets_ready")
    seen = {}
    monkeypatch.setattr("comic_studio.engine.llm.storyboard.make_split_factory",
                        lambda db_: _split_fake(seen, _COMIC_REPLY))

    ids = split_storyboards(db, tmp_path / "data", pid)
    assert len(ids) >= 1
    assert "漫画页" in seen["system"] or "单格" in seen["system"]  # comic 模式提示词
    assert "无运镜" in seen["system"]  # 明确禁止运镜/声音/时长
    s = list_shots(db, pid)[0]
    assert s["workflow_type"] == "comic"
    led = json.loads(s["ledger_json"])
    assert led.get("dialogue") == [{"speaker": "少年", "line": "我回来了"}]


def test_comic_split_prompt_scoping(tmp_path, monkeypatch):
    """target_pages>0 注入页数指引；非漫画项目 system 不含漫画页分支。"""
    from comic_studio.engine.llm.storyboard import split_storyboards

    seen_c, seen_v = {}, {}
    monkeypatch.setattr("comic_studio.engine.llm.storyboard.make_split_factory",
                        lambda db_: _split_fake(seen_c, _COMIC_REPLY))
    db, pid = _comic_project(tmp_path, target_pages=12)
    split_storyboards(db, tmp_path / "data", pid)
    assert "12 个漫画页" in seen_c["system"]

    monkeypatch.setattr("comic_studio.engine.llm.storyboard.make_split_factory",
                        lambda db_: _split_fake(seen_v, _COMIC_REPLY))
    db2, pid2 = _comic_project(tmp_path / "v", comic_mode="")
    split_storyboards(db2, tmp_path / "v" / "data", pid2)
    assert "漫画页" not in seen_v["system"]  # 视频项目提示词不受漫画分支污染
