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


def test_oversized_paragraph_split_not_own_chunk():
    """2026-08-25 行为变更：超长段落不再独占一块（会爆上下文），必须切到 max_chars 内。"""
    chunks = split_chunks("短段\n\n" + "长" * 100, max_chars=10)
    assert chunks[0] == "短段"
    assert all(len(c) <= 10 for c in chunks[1:])
    assert "".join(chunks[1:]) == "长" * 100


def test_split_chunks_oversized_paragraph_hard_split():
    """真机 bug（2026-08-25 验收）：网文只有单换行 → 整段成巨型块 → 53928 tok 爆上下文。
    超长段落必须再切：先按行、行超长硬切。"""
    # 单换行构成的超长"段落"（无 \n\n）
    text = "\n".join(f"第{i}行内容" + "字" * 40 for i in range(500))  # ~2.2 万字
    chunks = split_chunks(text, max_chars=8000)
    assert len(chunks) >= 3
    assert all(len(c) <= 8000 for c in chunks)
    # 内容无损
    assert "".join(c.replace("\n", "") for c in chunks) == text.replace("\n", "")


def test_split_chunks_single_giant_line_hard_split():
    """一个无换行的超长行也要硬切到 max_chars 内。"""
    text = "前段短文\n\n" + "巨" * 20000
    chunks = split_chunks(text, max_chars=8000)
    assert all(len(c) <= 8000 for c in chunks)
    assert "".join(c for c in chunks).count("巨") == 20000
