# tests/test_text.py
from comic_studio.engine.llm.text import split_chunks


def test_short_text_single_chunk():
    assert split_chunks("你好") == ["你好"]


def test_empty_text():
    assert split_chunks("") == []


def test_splits_on_blank_lines_not_mid_paragraph():
    paras = [f"第{i}段" + "字" * 10 for i in range(6)]
    text = "\n\n".join(paras)
    chunks = split_chunks(text, max_chars=40)
    assert len(chunks) >= 2
    # 每块都是完整段落序列
    rejoined = [p for c in chunks for p in c.split("\n\n")]
    assert rejoined == paras


def test_oversized_paragraph_own_chunk():
    chunks = split_chunks("短段\n\n" + "长" * 100, max_chars=10)
    assert chunks[0] == "短段"
    assert len(chunks[1]) == 100
