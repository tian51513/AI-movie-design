# comic_studio/engine/workflows/registry.py
"""工作流模板注册表：templates/workflows/ 下 yaml manifest 扫描加载（spec §6）。"""
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..db import Database
from ..settings import get_setting

TEMPLATE_ROOT = Path("templates/workflows")


class ManifestError(Exception):
    pass


@dataclass
class InjectPoint:
    node: str
    field: str


@dataclass
class OutputSpec:
    node: str
    filename_prefix: str


@dataclass
class WorkflowTemplate:
    id: str
    type: str
    name: str
    file: str
    prompt_format: str
    inject_prompt: InjectPoint
    inject_params: dict
    outputs: list
    requires: list
    dir: Path

    def api_json(self) -> dict:
        import json
        return json.loads((self.dir / self.file).read_text(encoding="utf-8"))


def load_manifest(path: Path) -> WorkflowTemplate:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ManifestError(f"{path.name} 不是合法 YAML: {e}") from e
    required = ("id", "type", "name", "file", "prompt_format", "inject", "outputs")
    missing = [k for k in required if k not in data]
    if missing:
        raise ManifestError(f"{path.name} 缺字段: {missing}")
    inj = data["inject"]
    if "prompt" not in inj:
        raise ManifestError(f"{path.name} inject.prompt 必填")
    return WorkflowTemplate(
        id=data["id"], type=data["type"], name=data["name"], file=data["file"],
        prompt_format=data["prompt_format"],
        inject_prompt=InjectPoint(**inj["prompt"]),
        inject_params={k: InjectPoint(**v) for k, v in (inj.get("params") or {}).items()},
        outputs=[OutputSpec(**o) for o in data["outputs"]],
        requires=list(data.get("requires") or []),
        dir=path.parent)


def scan_templates(root: Path) -> dict:
    reg: dict[str, WorkflowTemplate] = {}
    for path in sorted(root.glob("*.yaml")):
        t = load_manifest(path)
        if t.id in reg:
            raise ManifestError(f"模板 id 重复: {t.id}（{path.name}）")
        reg[t.id] = t
    return reg


def resolve_template(db: Database, tmpl_type: str) -> WorkflowTemplate:
    tmpl_id = get_setting(db, "template_map").get(tmpl_type)
    reg = scan_templates(TEMPLATE_ROOT)
    if not tmpl_id or tmpl_id not in reg:
        raise ManifestError(f"类型 {tmpl_type} 未映射到已注册模板（映射={tmpl_id!r}，已注册={sorted(reg)}）")
    return reg[tmpl_id]
