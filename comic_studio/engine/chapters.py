# comic_studio/engine/chapters.py
"""章节切分（P7-E，借鉴 NovelFlow 的正则方案）：中文章节号含中文数字 +
英文 Chapter N。纯机械、不依赖 LLM；返回字符区间供按章范围拆分镜切片。"""
import re

_CHAPTER_RE = re.compile(
    r"^(?:第([\d零一二三四五六七八九十百千万]+)章\s*(.*)|Chapter\s+(\d+)\s*:?\s*(.*))$",
    re.MULTILINE)

_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def cn2int(s: str) -> int | None:
    """中文数字 → int（支持到万；阿拉伯数字直接转）。解析失败返回 None。"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    total, num = 0, 0
    for ch in s:
        if ch in _CN_DIGIT:
            num = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            if num == 0:
                num = 1  # "十"开头的省略形式：十二 = 10+2
            if unit == 10000:
                total = (total + num) * unit
            else:
                total += num * unit
            num = 0
        else:
            return None
    return total + num or None


def parse_chapters(text: str) -> list[dict]:
    """切分章节，返回 [{idx, title, start, end}]（end 为Exclusive 字符偏移）。
    无章节标题 → []（调用方按单章全文处理）。"""
    marks = []  # (line_start_offset_in_text, idx, title)
    for m in _CHAPTER_RE.finditer(text):
        if m.group(1) is not None:
            idx, title = cn2int(m.group(1)), (m.group(2) or "").strip()
        else:
            idx, title = int(m.group(3)), (m.group(4) or "").strip()
        if idx is None:
            continue
        # 章节标题行必须在行首（标题内含正则元字符安全：m.start() 是行首）
        marks.append((m.start(), idx, title))
    if not marks:
        return []
    out = []
    for i, (pos, idx, title) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        out.append({"idx": idx, "title": title, "start": pos, "end": end})
    return out


def slice_chapters(text: str, chapters: list[dict], chapter_range: tuple) -> str:
    """按章范围切片。chapter_range=(from_idx, to_idx) 闭区间；
    范围外/无章节结构 → 原文全文。"""
    if not chapters or not chapter_range:
        return text
    lo, hi = chapter_range
    parts = [text[c["start"]:c["end"]] for c in chapters if lo <= c["idx"] <= hi]
    return "".join(parts).strip() or text
