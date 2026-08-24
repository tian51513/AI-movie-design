# comic_studio/engine/genref.py
"""gen_ref 处理器：为资产生成参考图并落库 views/（spec 门1 前置）。"""
import json
import random

from .assets import get_asset
from .logbus import emit as emit_log
from .paths import data_to_abs
from .queue.worker import register
from .settings import get_setting
from .workflows.filler import fill_workflow
from .workflows.registry import resolve_template

KIND_LABEL = {"character": "角色", "scene": "场景", "prop": "道具"}
KIND_SUFFIX = {
    "character": "，角色三视图设定图：同一画面中从左到右依次为 正面全身、左侧全身、背面全身，"
                 "三个视角必须明显不同且各占三分之一，全身像，白色干净背景",
    "scene": "，场景概念设定图，环境全景，无人物",
    "prop": "，道具设定图，白色背景，居中特写",
}


def build_gen_prompt(asset_row, style: str = ""):
    """style：项目级画风描述（公共参数），非空时作为风格段注入。"""
    detail = json.loads(asset_row["appearance_json"]).get("detail", "")
    base = KIND_LABEL[asset_row["kind"]] + "：" + asset_row["name"]
    if detail:
        base += "。" + detail.strip().rstrip("。；;，,")
    prompt = base + KIND_SUFFIX.get(asset_row["kind"], "")
    style = style.strip().rstrip("。；;，,").strip()
    if style:
        prompt += "。" + style   # 风格段：主导整体画风
    if asset_row["kind"] == "character":
        prompt += "。严格三视图布局：正面、左侧、背面各一个，禁止视角重复"  # 结构收尾再强调
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
        images=None, output_ctx=ctx,
        model_overrides=(get_setting(db, "model_overrides") or {}).get(tmpl.id))
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
    from .shots import mark_stale_for_asset
    n = mark_stale_for_asset(db, asset["id"])
    if n:
        emit_log(db, "storyboard", "warn",
                 f"资产「{asset['name']}」参考图已更新：{n} 个引用它的分镜标记为 stale",
                 project_id=job["project_id"], job_id=job["id"])
