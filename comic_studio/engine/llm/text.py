# comic_studio/engine/llm/text.py
"""章节文本分块：保段落边界（spec §9.1）。"""


def _split_paragraph(p: str, max_chars: int) -> list[str]:
    """超长段落二次切分：先按单换行行，行本身超长再硬切。
    网文常见只有单换行——没有这层会整段成巨型块爆上下文（2026-08-25 真机教训）。"""
    if len(p) <= max_chars:
        return [p]
    out: list[str] = []
    cur = ""
    for line in p.split("\n"):
        while len(line) > max_chars:  # 无换行的超长行硬切
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:max_chars])
            line = line[max_chars:]
        add = len(line) + (1 if cur else 0)
        if cur and len(cur) + add > max_chars:
            out.append(cur)
            cur = line
        else:
            cur += ("\n" + line if cur else line)
    if cur:
        out.append(cur)
    return out


def split_chunks(text: str, max_chars: int = 8000) -> list[str]:
    paragraphs = [sub
                  for p in (pp.strip() for pp in text.split("\n\n")) if p
                  for sub in _split_paragraph(p, max_chars)]
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
