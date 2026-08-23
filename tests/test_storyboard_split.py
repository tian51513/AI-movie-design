import pytest
from types import SimpleNamespace as NS

from comic_studio.engine.assets import persist_assets
from comic_studio.engine.db import Database
from comic_studio.engine.llm.storyboard import ContentBoundaryError, split_storyboards
from comic_studio.engine.projects import create_project
from comic_studio.engine.shots import list_shots
from comic_studio.engine.llm.provider import Usage

CHUNK = """{{"shots":[{{
 "text_span":"推门","description":"{desc}","shot_type":"动作",
 "camera":{{"景别":"全景","机位":"平视","运镜":"固定","转场":"切"}},
 "duration":4,"workflow_type":"ref2va",
 "must_appear":["林晨"],"must_keep":[],"may_change":[],"must_avoid":[],
 "character_ids":[{cid}],"scene_ids":[],"prop_ids":[],"continue_prev":false}}]}}"""


class FakeLLM:
    model = "fake"
    def __init__(self, replies): self.replies = list(replies); self.n = 0
    def raw_chat(self, messages, temperature=0.3, max_tokens=None):
        r = self.replies[min(self.n, len(self.replies) - 1)]; self.n += 1
        return r, Usage(1, 2)


def _setup(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "p", "9:16", "林晨推开门。庭院里站着一个白发少女。")["id"]
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="林晨", appearance="黑发少年", tags=[])],
                      scenes=[], props=[]))
    return db, pid


def test_split_single_chunk_persists(tmp_path):
    db, pid = _setup(tmp_path)
    fake = FakeLLM([CHUNK.format(desc="推门镜头", cid=1)])
    ids = split_storyboards(db, tmp_path / "data", pid, client_factory=lambda t: fake)
    rows = list_shots(db, pid)
    assert len(rows) == 1 and rows[0]["prompt"] == ""  # 提示词下一任务生成
    assert "推门镜头" in rows[0]["description"]
    import json
    assert json.loads(rows[0]["ledger_json"])["assets"]["characters"] == [1]


def test_split_multi_chunk_links_continue_prev(tmp_path):
    db, pid = _setup(tmp_path)
    long = "甲" * 60 + "\n\n" + "乙" * 60
    create_project  # noqa
    from comic_studio.engine.projects import get_project
    # 重设 novel 为长文（直接覆盖文件）
    import pathlib
    novel = pathlib.Path(get_project(db, pid)["novel_path"])
    novel.parent.mkdir(parents=True, exist_ok=True)
    novel.write_text(long, encoding="utf-8")
    fake = FakeLLM([
        CHUNK.format(desc="第一块末镜", cid=1),
        CHUNK.format(desc="第二块首镜（延续）", cid=1).replace('"continue_prev":false', '"continue_prev":true'),
    ])
    ids = split_storyboards(db, tmp_path / "data", pid,
                            client_factory=lambda t: fake, max_chars=80)
    rows = list_shots(db, pid)
    assert [r["seq"] for r in rows] == [1, 2]
    assert rows[1]["depends_on"] == rows[0]["id"]


def test_content_boundary_blocks_and_reports(tmp_path):
    db, pid = _setup(tmp_path)
    bad = CHUNK.format(desc="涉及幼女的情欲画面", cid=1)
    fake = FakeLLM([bad])
    with pytest.raises(ContentBoundaryError):
        split_storyboards(db, tmp_path / "data", pid, client_factory=lambda t: fake)
