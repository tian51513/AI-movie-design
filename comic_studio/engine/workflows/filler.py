# comic_studio/engine/workflows/filler.py
"""注入填充器：模板 + 值 → 可提交的 API 工作流 + 待上传清单（spec §6.1）。"""
import copy


def apply_switch_links(wf, template, params: dict) -> None:
    """开关接线（2026-09-18）：真 → 注入 add_nodes 节点并改接 on 链；假/缺 → off
    直连。独立成函数供 director 手工注入路径共用（它不走 fill_workflow）。
    API 格式无节点禁用，旁路分支默认不存在（详见 registry.WorkflowTemplate）"""
    for key, spec in template.switch_links.items():
        if params.get(key):
            for nid, node_def in (spec.get("add_nodes") or {}).items():
                wf[str(nid)] = copy.deepcopy(node_def)
            wf[str(spec["node"])]["inputs"][spec["field"]] = spec["on"]
        else:
            wf[str(spec["node"])]["inputs"][spec["field"]] = spec["off"]


def fill_workflow(template, *, prompt: str | None, params: dict,
                   images: list | None, output_ctx: dict,
                   model_overrides: dict | None = None):
    wf = copy.deepcopy(template.api_json())

    def set_input(node: str, field_name: str, value):
        wf[str(node)]["inputs"][field_name] = value

    if prompt is not None and template.inject_prompt is not None:
        set_input(template.inject_prompt.node, template.inject_prompt.field, prompt)
    # prompt=None：保留工作流内置提示词（如四视图 LoRA 触发词）——管线只传图/参数
    # 模型槽位覆盖（settings model_overrides，键=模板 id → {label: 文件名}）。
    # 带开关字段的槽位（2026-09-14 LazyKreaLoraStack）：覆盖非空=设文件名+开、
    # 空串=只关开关（文件名保留）、未覆盖=模板默认
    for slot in template.models:
        value = (model_overrides or {}).get(slot.label)
        if value is None:
            continue
        if slot.switch_field:
            if value:
                set_input(slot.node, slot.field, value)
                set_input(slot.node, slot.switch_field, True)
            else:
                set_input(slot.node, slot.switch_field, False)
        elif value:
            set_input(slot.node, slot.field, value)
    for key, point in template.inject_params.items():
        value = params.get(key)
        if value is None:
            continue
        if key == "seed":
            value = int(value)
        set_input(point.node, point.field, value)

    # 开关接线（2026-09-18）：真 → 注入 add_nodes 节点并改接 on 链；假/缺 → off
    # 直连。API 格式无节点禁用，旁路分支默认不存在（详见 registry.WorkflowTemplate）
    apply_switch_links(wf, template, params)

    uploads: list[dict] = []
    for spec in template.inject_images:
        matched = next((im for im in (images or []) if im["slot"] == spec["slot"]), None)
        if matched is None:
            continue
        # 2026-08-30 音色：上传名保留源文件后缀（音频走 /upload/audio 按后缀分流）
        from pathlib import Path as _P
        suffix = _P(matched["path"]).suffix.lower() or ".png"
        # 上传名即 ComfyUI input 路径——清洗分隔符（2026-09-13 真机判例：
        # 链式 tag 带 / 被当子目录 → 服务端 open() FileNotFoundError → 500）
        _safe = lambda x: str(x).replace("/", "-").replace("\\", "-")
        name = f"cs__{_safe(output_ctx['project'])}__{_safe(output_ctx['asset'])}__{_safe(spec['slot'])}{suffix}"
        set_input(spec["node"], spec["field"], name)
        uploads.append({"path": matched["path"], "name": name})

    for out in template.outputs:
        prefix = out.filename_prefix.format(**output_ctx)
        set_input(out.node, "filename_prefix", prefix)
    return wf, uploads
