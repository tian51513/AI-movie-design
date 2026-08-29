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
class ModelSlot:
    """模型加载槽位（计划5B 模型切换）：label 供 settings model_overrides 引用。"""
    label: str
    node: str
    field: str
    cls: str  # ComfyUI /object_info 的类名（枚举可选文件用）
    label_cn: str = ""  # 设置页中文名（英文标识旁展示）


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
    inject_images: list = field(default_factory=list)
    models: list = field(default_factory=list)
    # 提示词方言（2026-08-27 需求：不同模板提示风格各自映射）：
    # natural_zh = 中文自然语言（qwen/lumina2 类编码器，如 zimage_t2i）；
    # tags_en = 英文标签流（SD 系 CLIP 读不懂中文，如 t2i_ref——中文提示词对它是噪声）
    prompt_style: str = "natural_zh"

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
    # inject.prompt 可选：部分工作流用内置触发词（如四视图 LoRA），管线只传图/参数
    return WorkflowTemplate(
        id=data["id"], type=data["type"], name=data["name"], file=data["file"],
        prompt_format=data["prompt_format"],
        inject_prompt=(InjectPoint(**inj["prompt"]) if inj.get("prompt") else None),
        inject_params={k: InjectPoint(**v) for k, v in (inj.get("params") or {}).items()},
        outputs=[OutputSpec(**o) for o in data["outputs"]],
        requires=list(data.get("requires") or []),
        dir=path.parent,
        inject_images=list(inj.get("images") or []),
        models=[ModelSlot(**m) for m in (data.get("models") or [])],
        prompt_style=data.get("prompt_style") or "natural_zh")


def scan_templates(root: Path) -> dict:
    reg: dict[str, WorkflowTemplate] = {}
    for path in sorted(root.glob("*.yaml")):
        t = load_manifest(path)
        if t.id in reg:
            raise ManifestError(f"模板 id 重复: {t.id}（{path.name}）")
        reg[t.id] = t
    return reg


def resolve_template(db: Database, tmpl_type: str) -> WorkflowTemplate:
    from ..settings import DEFAULT_SETTINGS
    # 旧库缺新键或值被清成 null → 默认兜底（null 视同未设置，不覆盖默认；
    # 2026-08-29 真机：设置页崩掉时保存动作把 template_map 全清成 null）
    stored = {k: v for k, v in (get_setting(db, "template_map") or {}).items() if v}
    mapping = {**DEFAULT_SETTINGS["template_map"], **stored}
    tmpl_id = mapping.get(tmpl_type)
    reg = scan_templates(TEMPLATE_ROOT)
    if not tmpl_id or tmpl_id not in reg:
        raise ManifestError(f"类型 {tmpl_type} 未映射到已注册模板（映射={tmpl_id!r}，已注册={sorted(reg)}）")
    return reg[tmpl_id]
