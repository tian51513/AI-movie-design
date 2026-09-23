# comic_studio/engine/stylepresets.py
"""Krea2 风格预设库（2026-09-12 借鉴 ComfyUI_Lazybuxuexi）：73 库 JSON
（templates/styles/krea2/，中英对照成品 prompt+negative）→ 创建弹窗画风
两级选择（库→风格），选中填入 style/style_vis。"""
import json
from pathlib import Path


def parse_krea2_style(value: str) -> tuple[str, str]:
    """projects.krea2_style（"lib|style"）→ (风格库, 风格名)；空/坏格式返 ("","")。
    工作台槽值：lib=风格库文件 stem（object_info 下拉值），style=条目 name。"""
    value = (value or "").strip()
    if not value or "|" not in value:
        return "", ""
    lib, _, style = value.partition("|")
    lib, style = lib.strip(), style.strip()
    if not lib or not style:
        return "", ""
    return lib, style


def format_krea2_style(lib: str, style: str) -> str:
    """(lib, style) → 存库值；任一为空返 ""（=不套库风格）。"""
    lib, style = (lib or "").strip(), (style or "").strip()
    return f"{lib}|{style}" if lib and style else ""

_CACHE: dict | None = None   # 进程内缓存（文件不热更）
_STEMS: dict = {}            # {显示库名: 原始文件 stem}——槽注入值（ComfyUI 枚举）


def list_style_libs(repo_root: Path | None = None) -> dict:
    """{库名: [{name, prompt}…]}——按文件名排序稳定输出。"""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    root = repo_root or Path(__file__).resolve().parents[2]
    d = root / "templates" / "styles" / "krea2"
    out: dict = {}
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            try:
                items = json.loads(f.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if not isinstance(items, list):
                continue
            # 库名取文件名去 krea2_ 前缀与 .json（如 Anime-Cel_Illustration-1_动漫-赛璐璐与插画）
            lib = f.stem
            for prefix in ("krea2_",):
                if lib.startswith(prefix):
                    lib = lib[len(prefix):]
            styles = []
            for s in items:
                if not (isinstance(s, dict) and (s.get("prompt") or "").strip()):
                    continue
                # name=英文主键（查重/内部引用），zh=name_cn 中文名（库 JSON
                # 原生携带 3948/3948 全覆盖——2026-09-13 用户指正：ComfyUI 节点
                # 显示的就是它，此前扫描器优先 name 把中文丢了）
                entry = {"name": str(s.get("name") or s.get("name_cn") or ""),
                         "prompt": str(s.get("prompt") or "")}
                _cn = str(s.get("name_cn") or "").strip()
                if _cn:
                    entry["zh"] = _cn
                # 缩略图（2026-09-12 预览面板）：JSON 引用 samples/xxx.webp，
                # 文件在才给 URL（/styles 静态挂载直出）
                thumb = str(s.get("thumbnail") or "").strip()
                if thumb and (d / thumb).is_file():
                    entry["thumb"] = f"/styles/krea2/{thumb}"
                styles.append(entry)
            if styles:
                out[lib] = styles
                _STEMS[lib] = f.stem
    _CACHE = out
    return out


def resolve_style_lib(lib: str) -> str:
    """存库库名（=扫描器显示名，已剥 krea2_ 前缀）→ 工作台槽注入值。
    ComfyUI 风格库下拉枚举=风格库文件**原始 stem**（带 krea2_ 前缀，object_info
    实证）——直注剥前缀的显示名会 value_not_in_list 400（2026-09-23 猫物语
    真机：漫画页全批 400 全灭）。本地库补前缀能对上即返 stem；已带前缀/未知
    值（lazy_styles 等非本库、用户手填）原样透传。"""
    lib = (lib or "").strip()
    if not lib:
        return ""
    list_style_libs()          # 惰性暖缓存（_STEMS 随扫描填充）
    return _STEMS.get(lib) or _STEMS.get("krea2_" + lib) or lib
