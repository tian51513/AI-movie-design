# comic_studio/engine/themes.py
"""主题模板：templates/tpl/*.md 解析入库（LLM 实时生成项目文本的预设主题）。

条目格式（default_theme.md）：
    1. **主题名称：**《名》
    **描述：** 描述文字

按 ### 分节；节标题含「成人向/情欲」的整节跳过（2026-08-25 约定：
成人向主题模板不入库、不接入生成链路——常规项照常整理）。
"""
import re
from pathlib import Path

THEME_ROOT = Path("templates/tpl")

_ENTRY = re.compile(
    r"\d+\.\s*\*\*主题名称：?\*\*\s*《(.+?)》\s*\n+\*\*描述：?\*\*\s*(.+?)"
    r"(?=\n\s*\d+\.|\n---|\Z)",
    re.S)


def _category(header: str) -> str:
    m = re.search(r"([一-鿿]{2,6})[（(]", header)
    return m.group(1) if m else header.strip()[:12]


def parse_theme_file(path: Path) -> list[dict]:
    return parse_text(Path(path).read_text(encoding="utf-8"))


def parse_text(text: str) -> list[dict]:
    """解析 default_theme.md 格式文本（上传导入与文件同步共用）。
    2026-08-29 用户决策：成人向节不再跳过，全部入库（分类=成人向）。"""
    items: list[dict] = []
    for section in re.split(r"\n###\s*", "\n" + text):
        header = section.split("\n", 1)[0]
        for name, desc in _ENTRY.findall(section):
            items.append({"name": name.strip(),
                          "category": _category(header),
                          "description": desc.strip()})
    return items


def sync_themes(db) -> int:
    """扫描 templates/tpl/*.md → 按 name upsert 入库；返回库内主题总数。"""
    conn = db.connect()
    if THEME_ROOT.is_dir():
        for f in sorted(THEME_ROOT.glob("*.md")):
            for item in parse_theme_file(f):
                conn.execute(
                    "INSERT INTO theme_templates (name, category, description) "
                    "VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET "
                    "category=excluded.category, description=excluded.description",
                    (item["name"], item["category"], item["description"]))
    conn.commit()
    return conn.execute("SELECT COUNT(*) c FROM theme_templates").fetchone()["c"]


def list_themes(db) -> list[dict]:
    return [dict(r) for r in db.connect().execute(
        "SELECT id, name, category, description FROM theme_templates "
        "ORDER BY category, id")]
