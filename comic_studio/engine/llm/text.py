# comic_studio/engine/llm/text.py
"""章节文本分块：保段落边界（spec §9.1）。"""


def split_chunks(text: str, max_chars: int = 8000) -> list[str]:
    paragraphs = [p for p in (pp.strip() for pp in text.split("\n\n")) if p]
    if not paragraphs:
        return []
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for p in paragraphs:
        add = len(p) + (1 if current else 0)
        if current and size + add > max_chars:
            chunks.append("\n\n".join(current))
            current, size = [], 0
            add = len(p)
        current.append(p)
        size += add
    if current:
        chunks.append("\n\n".join(current))
    return chunks
