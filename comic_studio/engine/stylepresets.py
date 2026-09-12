# comic_studio/engine/stylepresets.py
"""Krea2 风格预设库（2026-09-12 借鉴 ComfyUI_Lazybuxuexi）：73 库 JSON
（templates/styles/krea2/，中英对照成品 prompt+negative）→ 创建弹窗画风
两级选择（库→风格），选中填入 style/style_vis。"""
import json
from pathlib import Path

_CACHE: dict | None = None   # 进程内缓存（文件不热更）


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
            styles = [{"name": str(s.get("name") or s.get("name_cn") or ""),
                       "prompt": str(s.get("prompt") or "")}
                      for s in items
                      if isinstance(s, dict) and (s.get("prompt") or "").strip()]
            if styles:
                out[lib] = styles
    _CACHE = out
    return out
