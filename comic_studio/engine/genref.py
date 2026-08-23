# comic_studio/engine/genref.py
"""gen_ref 处理器：为资产生成参考图并落库 views/（spec 门1 前置）。"""
import json
import random

from .assets import get_asset
from .logbus import emit as emit_log
from .paths import data_to_abs
from .queue.worker import register
from .workflows.filler import fill_workflow
from .workflows.registry import resolve_template

KIND_LABEL = {"character": "角色", "scene": "场景", "prop": "道具"}
KIND_SUFFIX = {
    "character": "，角色设定图，三视图：正面、侧面、背面，全身，白色背景",
    "scene": "，场景概念设定图，环境全景，无人物",
    "prop": "，道具设定图，白色背景，居中特写",
}


def build_gen_prompt(asset_row, style: str = ""):
    """style：项目级画风描述（公共参数），非空时作为风格段注入。"""
    detail = json.loads(asset_row["appearance_json"]).get("detail", "")
    base = KIND_LABEL[asset_row["kind"]] + "：" + asset_row["name"]
    if detail:
        base += "。" + detail
    if style.strip():
        base += "。" + style.strip()
    prompt = base + KIND_SUFFIX.get(asset_row["kind"], "")
    ctx = {"project": f"p{asset_row['source_project']}", "asset": str(asset_row["id"])}
    return prompt, ctx


@register("gen_ref")
def handle_gen_ref(db, data_dir, job, comfy):
    payload = json.loads(job["payload_json"] or "{}")
    asset = get_asset(db, payload["asset_id"])
    if asset is None:
        raise ValueError(f"资产不存在: {payload['asset_id']}")
    tmpl = resolve_template(db, "t2i")  # 裁决 B：v1 统一 t2i 模板
    from .projects import get_project
    proj = get_project(db, asset["source_project"])
    prompt, ctx = build_gen_prompt(asset, style=(proj["style"] if proj else ""))
    wf, uploads = fill_workflow(
        tmpl, prompt=prompt,
        params={"seed": payload.get("seed") or random.randint(0, 2**31 - 1)},
        images=None, output_ctx=ctx)
    if comfy is None:
        raise RuntimeError("gen_ref 需要 ComfyUI 端点（settings.comfy.base_url）")
    for up in uploads:
        comfy.upload_image(up["path"], up["name"])
    emit_log(db, "comfy", "info",
             f"资产「{asset['name']}」参考图提交（模板 {tmpl.id}）",
             project_id=job["project_id"], job_id=job["id"])
    prompt_id = comfy.submit(wf, client_id=f"cs-job-{job['id']}")
    images = comfy.wait_and_collect(
        prompt_id, stall_seconds=600,
        on_interrupt=lambda: emit_log(db, "comfy", "warn",
                                      f"job {job['id']} 失速，已 interrupt",
                                      project_id=job["project_id"], job_id=job["id"]))
    if not images:
        raise RuntimeError("ComfyUI 未返回任何输出图片")
    views_dir = data_to_abs(data_dir, asset["library_dir"]) / "views"
    dest = views_dir / "sheet.png"
    comfy.download(images[0]["filename"], images[0].get("subfolder", ""),
                   images[0].get("type", "output"), dest)
    emit_log(db, "comfy", "info", f"资产「{asset['name']}」参考图已生成并落盘",
             project_id=job["project_id"], job_id=job["id"],
             data={"path": f"{asset['library_dir']}/views/sheet.png"})
