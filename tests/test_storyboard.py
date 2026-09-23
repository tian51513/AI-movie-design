

def test_collapse_repetition():
    """复读段压缩（2026-09-22 猫物语块 104 判例：1300 个「去死吧、」把模型
    诱导进复读机——对半到 25 字仍炸）。压缩保留首尾+计次标注；正常文本不动。"""
    from comic_studio.engine.llm.storyboard import collapse_repetition
    rep = "去死吧、" * 216
    out = collapse_repetition(rep)
    assert "重复约 216 次" in out and len(out) < 120
    assert out.startswith("去死吧、去死吧、去死吧、") and out.endswith("去死吧、")
    normal = "深夜的旧公寓里，林晚披着睡袍走到窗前。陈默端来两杯红酒。"
    assert collapse_repetition(normal) == normal          # 正常文本零改动
    short_run = "好。" * 8                                  # 低于 min_run 不压
    assert collapse_repetition(short_run) == short_run
    # 混合：复读段前后有正常文本，只压中段
    mixed = "她开始低声念诵。" + "去死吧、" * 50 + "念诵声停了。"
    out2 = collapse_repetition(mixed)
    assert "重复约 50 次" in out2 and out2.startswith("她开始低声念诵。") and out2.endswith("念诵声停了。")


def test_comic_split_sets_storyboard_ready(tmp_path, monkeypatch):
    """漫画链拆完直接就绪（2026-09-22 判例：714 镜拆完停在 assets_ready——
    storyboard_ready 由门2 设置是视频链设计，漫画镜提示词已预填门2 纯仪式）。"""
    from comic_studio.engine.db import Database
    from comic_studio.engine.projects import create_project, get_project
    from comic_studio.engine.llm.storyboard import split_storyboards
    from comic_studio.engine.llm.provider import LLMClient, Usage

    class FakeClient(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "fake")
            self.n = 0

        def raw_chat(self, messages, temperature=0.3):
            self.n += 1
            return ('{"shots":[{"text_span":"他推门","description":"推门进房"}]}', Usage(1, 1))

    db = Database(tmp_path / "s.db"); db.migrate()
    proj = create_project(db, tmp_path / "data", "p", "9:16",
                          "他推开门走进房间。", comic_mode="comic_output")
    conn = db.connect()
    conn.execute("UPDATE projects SET stage='assets_ready' WHERE id=?", (proj["id"],))
    conn.commit()
    split_storyboards(db, tmp_path / "data", proj["id"],
                      client_factory=lambda t: FakeClient())
    assert get_project(db, proj["id"])["stage"] == "storyboard_ready"


def test_split_clears_old_shots_at_start(tmp_path, monkeypatch):
    """点拆解分镜即清旧镜（2026-09-23 用户需求）：旧镜此前要到拆解尾部
    persist_shots 才被替换——拆解跑的整段时间（小时级）autopilot/前端都
    看得见旧镜。handler 启动即删；失败留空由块缓存续跑（重拆=推翻重来）。"""
    import pytest
    from comic_studio.engine import jobs
    from comic_studio.engine.db import Database
    from comic_studio.engine.pipeline_jobs import handle_split
    from comic_studio.engine.projects import create_project

    db = Database(tmp_path / "s.db"); db.migrate()
    proj = create_project(db, tmp_path / "data", "p", "9:16",
                          "他推开门走进房间。")
    conn = db.connect()
    conn.execute("INSERT INTO shots (project_id, seq, text_span, description) "
                 "VALUES (?,?,?,?)", (proj["id"], 1, "旧", "旧镜"))
    conn.commit()

    seen = {}

    def fake_split(db_, data_dir, pid, **kw):
        seen["shots_at_split_start"] = db_.connect().execute(
            "SELECT COUNT(*) c FROM shots WHERE project_id=?",
            (pid,)).fetchone()["c"]
        raise RuntimeError("boom")

    monkeypatch.setattr("comic_studio.engine.llm.storyboard.split_storyboards",
                        fake_split)
    jobs.enqueue_job(db, "split_storyboards", project_id=proj["id"],
                     payload={"project_id": proj["id"]})
    job = jobs.claim_next_job(db, ("split_storyboards",))
    with pytest.raises(RuntimeError):
        handle_split(db, tmp_path / "data", job, None)
    assert seen["shots_at_split_start"] == 0   # 拆解启动前旧镜已清
    n = conn.execute("SELECT COUNT(*) c FROM shots WHERE project_id=?",
                     (proj["id"],)).fetchone()["c"]
    assert n == 0
