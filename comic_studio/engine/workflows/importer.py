# comic_studio/engine/workflows/importer.py
"""工作流导入分析器（2026-08-26 需求）：ComfyUI API JSON → 自动识别类型/注入点
→ 生成 manifest 数据。用户无需手写 yaml。"""
import json
import re
from pathlib import Path


def analyze_workflow(wf: dict, name: str) -> dict:
    """分析 ComfyUI API 格式工作流 JSON，返回 manifest 数据结构。"""
    if not isinstance(wf, dict) or not wf:
        raise ValueError("不是有效的 ComfyUI API 格式工作流（顶层应为节点字典）")

    # ===== 1. 类型识别 =====
    classes = {n.get("class_type", "") for n in wf.values()}
    if any("SaveVideo" in c or "CreateVideo" in c for c in classes):
        if "MiniMaxH3ReferenceToVideo" in classes:
            wtype = "ref2va"
        elif "MiniMaxH3ImageToVideo" in classes:
            # 有 LoadImage 且 ≥2 → 首尾帧；有 1 个 → 图生视频；无 → 文生
            load_imgs = [nid for nid, n in wf.items()
                         if n.get("class_type") == "LoadImage"]
            if len(load_imgs) >= 2:
                wtype = "fl2v"
            elif len(load_imgs) == 1:
                wtype = "i2v"
            else:
                wtype = "t2v"
        else:
            wtype = "t2v"  # 未知视频类型归为 t2v
    elif any("SaveImage" in c for c in classes):
        wtype = "t2i"
    else:
        wtype = "other"

    # ===== 2. 注入点识别 =====
    prompt_node, prompt_field = None, None
    seed_node, seed_field = None, None
    image_nodes = []
    output_nodes = []
    models = []

    # 提示词节点：优先 H3 原生节点（prompt 字段），其次 CLIPTextEncode（text），
    # 其次 PrimitiveStringMultiline（value）
    _PRIORITY = [
        ("MiniMaxH3ReferenceToVideo", "prompt"),
        ("MiniMaxH3ImageToVideo", "prompt"),
        ("Krea2EditGroundedEncode", "prompt"),
        ("CLIPTextEncode", "text"),
        ("PrimitiveStringMultiline", "value"),
        ("TextEncodeQwenImageEdit", "prompt"),
    ]
    for cls, field in _PRIORITY:
        for nid, node in wf.items():
            if node.get("class_type") == cls and field in (node.get("inputs") or {}):
                # 排除负面提示词（连接 ConditioningZeroOut 或 negative 命名）
                if cls == "CLIPTextEncode":
                    # 检查是否被用作 negative
                    title = (node.get("_meta") or {}).get("title", "").lower()
                    if "negative" in title:
                        continue
                prompt_node, prompt_field = str(nid), field
                break
        if prompt_node:
            break

    # seed：KSampler.seed / rgthree Seed.seed / PrimitiveInt(seed)
    for nid, node in wf.items():
        cls = node.get("class_type", "")
        ins = node.get("inputs") or {}
        if cls in ("KSampler", "KSamplerSelect") and "seed" in ins:
            seed_node, seed_field = str(nid), "seed"
            break
        if "Seed" in cls and "seed" in ins:
            seed_node, seed_field = str(nid), "seed"
            break

    # 图片槽：LoadImage 节点按 node id 排序
    for nid, node in sorted(wf.items(), key=lambda x: _node_sort_key(x[0])):
        if node.get("class_type") == "LoadImage":
            image_nodes.append({"node": str(nid), "field": "image",
                                "slot": f"ref{len(image_nodes)}"})

    # 输出节点
    for nid, node in wf.items():
        cls = node.get("class_type", "")
        if "SaveImage" in cls or "SaveVideo" in cls:
            prefix = (node.get("inputs") or {}).get("filename_prefix", "cs/{project}/{asset}")
            output_nodes.append({"node": str(nid),
                                 "filename_prefix": _norm_prefix(prefix)})

    # 模型槽位
    _LOADER_MAP = [
        ("UNETLoader", "unet_name", "unet", "主模型 UNet"),
        ("CheckpointLoaderSimple", "ckpt_name", "ckpt", "整合模型 Checkpoint"),
        ("CLIPLoader", "clip_name", "clip", "文本编码器 CLIP"),
        ("VAELoader", "vae_name", "vae", "VAE"),
        ("LoraLoaderModelOnly", "lora_name", "lora", "LoRA"),
    ]
    for nid, node in sorted(wf.items(), key=lambda x: _node_sort_key(x[0])):
        cls = node.get("class_type", "")
        for loader_cls, field, label, label_cn in _LOADER_MAP:
            if cls == loader_cls and field in (node.get("inputs") or {}):
                models.append({"label": f"{label}_{len([m for m in models if m['label'].startswith(label)])}"
                               if label in ("vae", "lora") else label,
                               "node": str(nid), "field": field,
                               "cls": loader_cls, "label_cn": label_cn})

    # ===== 3. 组装 manifest =====
    inject = {}
    if prompt_node:
        inject["prompt"] = {"node": prompt_node, "field": prompt_field}
    if seed_node:
        inject.setdefault("params", {})["seed"] = {"node": seed_node, "field": seed_field}
    if image_nodes:
        inject["images"] = image_nodes

    return {
        "id": name,
        "type": wtype,
        "name": name,
        "inject": inject,
        "outputs": output_nodes or [{"node": "1", "filename_prefix": "cs/{project}/{asset}"}],
        "models": models,
    }


def _node_sort_key(nid):
    """节点 id 排序：纯数字按数字序，带冒号按段序。"""
    parts = str(nid).split(":")
    return [int(p) if p.isdigit() else 0 for p in parts] + [str(nid)]


def _norm_prefix(prefix: str) -> str:
    """ComfyUI 导出的 filename_prefix 可能是绝对路径 → 规范为 cs/{project}/{asset}。"""
    if not prefix or "/" in prefix and "{" not in prefix:
        return "cs/{project}/{asset}"
    return prefix


def generate_yaml(analysis: dict) -> str:
    """分析结果 → manifest YAML 文本。"""
    lines = [f"id: {analysis['id']}",
             f"type: {analysis['type']}",
             f"name: {analysis['name']}",
             f"file: {analysis['id']}.api.json",
             "prompt_format: '{kind_label}：{name}。{detail}'"]
    inj = analysis.get("inject") or {}
    if inj.get("prompt"):
        p = inj["prompt"]
        lines.append(f"inject:\n  prompt:\n    node: '{p['node']}'\n    field: {p['field']}")
    if inj.get("params"):
        lines.append("  params:")
        for k, v in inj["params"].items():
            lines.append(f"    {k}:\n      node: '{v['node']}'\n      field: {v['field']}")
    if inj.get("images"):
        if not inj.get("params") and not inj.get("prompt"):
            lines.append("inject:")
        lines.append("  images:")
        for img in inj["images"]:
            lines.append(f"  - node: '{img['node']}'\n    field: {img['field']}\n    slot: {img['slot']}")
    if not inj:
        lines.append("inject: {}")
    lines.append("outputs:")
    for o in analysis["outputs"]:
        lines.append(f"- node: '{o['node']}'\n  filename_prefix: {o['filename_prefix']}")
    lines.append("requires: []")
    if analysis.get("models"):
        lines.append("\n# 模型槽位（settings model_overrides 按模板 id 覆盖）")
        lines.append("models:")
        for m in analysis["models"]:
            lines.append(f"- {{label: {m['label']}, node: '{m['node']}', "
                         f"field: {m['field']}, cls: {m['cls']}, label_cn: {m['label_cn']}}}")
    return "\n".join(lines) + "\n"


def import_workflow_json(json_bytes: bytes, filename: str,
                         template_dir: Path) -> dict:
    """导入入口：解析 JSON → 分析 → 生成 yaml + api.json 落盘 → 返回分析结果。"""
    try:
        wf = json.loads(json_bytes.decode("utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"不是有效的 JSON 文件：{e}")

    name = Path(filename).stem
    # 清理名称（只留字母数字下划线横线）
    name = re.sub(r"[^\w\-]", "_", name)

    analysis = analyze_workflow(wf, name)

    # 落盘
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / f"{name}.api.json").write_text(
        json.dumps(wf, ensure_ascii=False, indent=1), encoding="utf-8")
    (template_dir / f"{name}.yaml").write_text(
        generate_yaml(analysis), encoding="utf-8")

    return analysis
