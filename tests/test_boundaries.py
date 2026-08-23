"""spec §3.2 边界规则：engine/ 禁止 import Web 框架。"""
import ast
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / "comic_studio" / "engine"
BANNED = {"fastapi", "starlette", "uvicorn"}


def test_engine_imports_no_web_framework():
    offenders = []
    for py in ENGINE.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for n in names:
                if n in BANNED:
                    offenders.append(f"{py}:{node.lineno} imports {n}")
    assert not offenders, "engine 层违反边界规则: " + "; ".join(offenders)
