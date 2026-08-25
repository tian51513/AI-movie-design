# comic_studio/engine/workflows/filler.py
"""注入填充器：模板 + 值 → 可提交的 API 工作流 + 待上传清单（spec §6.1）。"""
import copy


def fill_workflow(template, *, prompt: str | None, params: dict,
                   images: list | None, output_ctx: dict,
                   model_overrides: dict | None = None):
    wf = copy.deepcopy(template.api_json())

    def set_input(node: str, field_name: str, value):
        wf[str(node)]["inputs"][field_name] = value

    if prompt is not None and template.inject_prompt is not None:
        set_input(template.inject_prompt.node, template.inject_prompt.field, prompt)
    # prompt=None：保留工作流内置提示词（如四视图 LoRA 触发词）——管线只传图/参数
    # 模型槽位覆盖（settings model_overrides，键=模板 id → {label: 文件名}）
    for slot in template.models:
        value = (model_overrides or {}).get(slot.label)
        if value:
            set_input(slot.node, slot.field, value)
    for key, point in template.inject_params.items():
        value = params.get(key)
        if value is None:
            continue
        if key == "seed":
            value = int(value)
        set_input(point.node, point.field, value)

    uploads: list[dict] = []
    for spec in template.inject_images:
        matched = next((im for im in (images or []) if im["slot"] == spec["slot"]), None)
        if matched is None:
            continue
        name = f"cs__{output_ctx['project']}__{output_ctx['asset']}__{spec['slot']}.png"
        set_input(spec["node"], spec["field"], name)
        uploads.append({"path": matched["path"], "name": name})

    for out in template.outputs:
        prefix = out.filename_prefix.format(**output_ctx)
        set_input(out.node, "filename_prefix", prefix)
    return wf, uploads
