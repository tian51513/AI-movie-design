# comic_studio/engine/textfix.py
"""文本机械修复（P7-G 第二批，借鉴短剧厂敏感词替换转译库 2026-08-28）：
不是拦截而是转译——生成正文里的高危措辞替换为平台安全的同义表达。
默认表小而保守；按需在此扩充。"""

SENSITIVE_MAP = {
    "杀了他": "废了他",
    "杀了她": "废了她",
    "杀了": "废了",
    "黑社会": "灰色势力",
    "贩毒": "灰色生意",
    "吸毒": "碰了不该碰的东西",
}


def apply_sensitive_replacements(text: str) -> tuple[str, int]:
    """返回 (修复后文本, 替换次数)。无命中原样返回。"""
    n = 0
    for a, b in SENSITIVE_MAP.items():
        if a in text:
            n += text.count(a)
            text = text.replace(a, b)
    return text, n
