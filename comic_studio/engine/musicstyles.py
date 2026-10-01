# comic_studio/engine/musicstyles.py
"""音乐风格库（2026-10-01 官方 Music3 skill vendor）：templates/music_styles/
的 genre-router 18 族索引 → 卡表清单 → 卡全文（完整结构化 caption）。
音乐库风格选择器的数据面（仿 stylepresets）；来源与许可见
templates/music_styles/NOTICE.md。"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "templates" / "music_styles"

_INDEX_RE = re.compile(r"^index-(?P<id>[a-z0-9-]+)\.md$")
_HEAD_RE = re.compile(r"^#\s+(?P<name>.+?)\s*$", re.M)
_COUNT_RE = re.compile(r"(?P<n>\d+)\s+compact style cards", re.I)
# 索引表行：| `id` | style | routes | tempo/key | mood | vocal | palette | `templates/x.txt` |
_ROW_RE = re.compile(
    r"^\|\s*`(?P<id>[^`]+)`\s*\|\s*(?P<style>[^|]+)\|\s*(?P<routes>[^|]*)\|"
    r"\s*(?P<tempo>[^|]+)\|\s*(?P<mood>[^|]+)\|\s*(?P<vocal>[^|]+)\|"
    r"\s*(?P<palette>[^|]+)\|\s*`templates/(?P<file>[^`]+)`\s*\|$")


def _index_path(family: str) -> Path:
    """family id → 索引文件；路径安全（白名单由文件名枚举保证）。"""
    if not re.fullmatch(r"[a-z0-9-]+", family or ""):
        raise ValueError(f"非法族 id: {family!r}")
    p = ROOT / "references" / f"index-{family}.md"
    if not p.exists():
        raise ValueError(f"族不存在: {family}")
    return p


def list_families(repo_root: Path | None = None) -> list[dict]:
    """18 族清单：[{id, name, count}]——id 来自文件名、name 来自 H1、
    count 来自「N compact style cards」行。"""
    root = (repo_root / "templates" / "music_styles") if repo_root else ROOT
    out = []
    for p in sorted((root / "references").glob("index-*.md")):
        m = _INDEX_RE.match(p.name)
        if not m:
            continue
        text = p.read_text(encoding="utf-8")
        head = _HEAD_RE.search(text)
        cnt = _COUNT_RE.search(text)
        out.append({"id": m["id"],
                    "name": head["name"].strip() if head else m["id"],
                    "count": int(cnt["n"]) if cnt else 0})
    return out


def list_cards(family: str) -> list[dict]:
    """族内卡表：[{id, style, tempo, mood, vocal, palette, file}]——选择器的
    文本预览（代替缩略图）。"""
    text = _index_path(family).read_text(encoding="utf-8")
    cards = []
    for line in text.splitlines():
        m = _ROW_RE.match(line.strip())
        if m:
            cards.append({"id": m["id"].strip(),
                          "style": m["style"].strip(),
                          "tempo": m["tempo"].strip(),
                          "mood": m["mood"].strip(),
                          "vocal": m["vocal"].strip(),
                          "palette": m["palette"].strip(),
                          "file": m["file"].strip()})
    return cards


def get_card(family: str, filename: str) -> str:
    """卡全文（完整英文结构化 caption——Music3TextEncode.caption 直接消费的
    格式）。文件名白名单校验（纯 basename 且确在族索引内），路径穿越拦死。"""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", filename or ""):
        raise ValueError(f"非法卡文件名: {filename!r}")
    if filename not in {c["file"] for c in list_cards(family)}:
        raise ValueError(f"卡 {filename} 不在族 {family} 索引内")
    p = (ROOT / "templates" / filename).resolve()
    if ROOT.resolve() not in p.parents:
        raise ValueError("路径越界")
    if not p.exists():
        raise ValueError(f"卡文件缺失: {filename}")
    return p.read_text(encoding="utf-8").strip()
