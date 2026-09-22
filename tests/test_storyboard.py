

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
