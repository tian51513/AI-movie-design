

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
