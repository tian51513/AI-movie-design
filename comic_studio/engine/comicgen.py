# comic_studio/engine/comicgen.py
"""小说转漫画逐页生成（2026-09-12 设计定稿）：gen_comic_page 逐镜 t2i 出页。

链路：comic 分镜（Task 2，workflow_type='comic'）→ 本 handler 逐页提交
comic_page 模板（默认 Z-Image Turbo 文生图，尺寸/步数按项目列）→
落盘 projects/<slug>/pages/page_NNN.png → 对白后处理（bubble=提示词层
画气泡，footer=Pillow 底部字幕条，none=不呈现）→ shot 标 comic_ready。
"""
import json
import random
from pathlib import Path

from .logbus import emit as emit_log
from .queue.worker import register
from .shots import get_shot, update_shot

# 质量档位 → 采样步数
QUALITY_STEPS = {"fast": 8, "standard": 12, "high": 20}

# 尺寸预设 → (width, height)
SIZE_PRESETS = {
    "1024x1024": (1024, 1024), "1024x1536": (1024, 1536),
    "1536x1024": (1536, 1024), "832x1216": (832, 1216),
}


def _ledger(shot) -> dict:
    try:
        return json.loads(shot["ledger_json"] or "{}")
    except json.JSONDecodeError:
        return {}


def build_comic_prompt(db, proj, shot) -> str:
    """漫画页提示词：场景 + 对白 + 画风。

    对白仅 bubble 模式进提示词（由图像模型画气泡）；footer/none 保持
    画面无字——footer 的对白由后处理字幕条呈现，none 直接丢弃。
    """
    detail = (shot["description"] or "").strip() or "按分镜描述生成"
    prompt = f"单格漫画插画：{detail[:200]}"
    if (proj["dialogue_mode"] or "bubble") == "bubble":
        for d in (_ledger(shot).get("dialogue") or [])[:2]:
            sp, ln = d.get("speaker", ""), d.get("line", "")
            if sp and ln:
                prompt += f"。{sp}说「{ln[:50]}」"
    style = ((proj["style_vis"] or proj["style"]) or "").strip()
    if style:
        prompt += f"。画风：{style}"
    return prompt


@register("gen_comic_page")
def handle_gen_comic_page(db, data_dir, job, comfy):
    """逐页 t2i 生成 → pages/page_NNN.png → 对白后处理 → shot 标 comic_ready。"""
    from .jobs import attach_snapshot
    from .projects import get_project
    from .settings import get_setting
    from .workflows.filler import fill_workflow
    from .workflows.registry import resolve_template

    payload = json.loads(job["payload_json"] or "{}")
    shot = get_shot(db, payload["shot_id"])
    if shot is None:
        raise ValueError("分镜已删除（gen_comic_page）")
    if comfy is None:
        raise ValueError("ComfyUI 未配置地址（gen_comic_page 需 ComfyUI）")
    proj = get_project(db, shot["project_id"])
    tmpl = resolve_template(db, "comic_page")

    w, h = SIZE_PRESETS.get(proj["image_size"] or "1024x1536", (1024, 1536))
    steps = QUALITY_STEPS.get(proj["quality_tier"] or "standard", 12)
    prompt = build_comic_prompt(db, proj, shot)
    wf, uploads = fill_workflow(
        tmpl, prompt=prompt,
        params={"seed": random.randint(0, 2**31 - 1), "steps": steps,
                "width": w, "height": h},
        images=[],
        output_ctx={"project": proj["slug"], "asset": f"page-{shot['seq']}"},
        model_overrides=(get_setting(db, "model_overrides") or {}).get(tmpl.id))
    for up in uploads:
        comfy.upload_image(Path(up["path"]), up["name"])
    if job is not None:
        attach_snapshot(db, job["id"], prompt=prompt, workflow=wf, template_id=tmpl.id)
    emit_log(db, "comfy", "info",
             f"漫画页 {shot['seq']} 提交（模板 {tmpl.id}，{w}×{h}，{steps}步）",
             project_id=proj["id"], job_id=job["id"])
    results = comfy.wait_and_collect(
        comfy.submit(wf, client_id=f"cs-comic-{shot['id']}"), stall_seconds=600)
    img = next((r for r in results if r.get("_kind") == "image"), None)
    if img is None:
        raise RuntimeError(f"漫画页 {shot['seq']} 未返回图片")

    # 落盘 pages/page_NNN.png（与动态漫 pages/ 目录约定同名不同项目空间）
    pages_dir = Path(data_dir) / "projects" / proj["slug"] / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    dest = pages_dir / f"page_{shot['seq']:03d}.png"
    comfy.download(img["filename"], img.get("subfolder", ""), img.get("type", "output"), dest)

    # 对白后处理（dialogue_mode: bubble/footer/none）
    dialogue = _ledger(shot).get("dialogue") or []
    mode = proj["dialogue_mode"] or "bubble"
    if mode != "none" and not _postprocess_dialogue(dest, dialogue, mode) and dialogue:
        emit_log(db, "comfy", "warn",
                 f"漫画页 {shot['seq']} 对白后处理未生效（缺 Pillow 或图片异常），页面无字幕条",
                 project_id=proj["id"], job_id=job["id"])

    update_shot(db, shot["id"], {"status": "comic_ready"})
    emit_log(db, "comfy", "info", f"漫画页 {shot['seq']} 已生成落盘",
             project_id=proj["id"], job_id=job["id"])
    return dest


def _postprocess_dialogue(page_png, dialogue, mode) -> bool:
    """对白后处理：footer=底部字幕条（Pillow，可选依赖）。

    bubble=提示词层已画气泡（无需处理）；none=不呈现。返回 False 表示
    需要处理但未成功（Pillow 缺失/图片异常）——调用方负责 warn 透明。
    """
    if mode != "footer" or not dialogue:
        return True  # 无需处理 ≠ 失败
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False
    try:
        img = Image.open(page_png).convert("RGBA")
    except Exception:
        return False
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("msyh.ttc", 28)
    except (OSError, IOError):
        font = ImageFont.load_default()
    texts = [f"{d.get('speaker', '')}：{d.get('line', '')}"
             for d in dialogue if d.get("line")]
    line_h = 32
    bar_h = len(texts) * line_h + 16
    # 半透明黑底条：白字在浅色漫画页上不可见（真机观感），压暗后清晰
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rectangle(
        [0, img.height - bar_h - 10, img.width, img.height], fill=(0, 0, 0, 160))
    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)
    y = img.height - bar_h + 8
    for t in texts:
        draw.text((20, y), t[:80], fill="white", font=font)
        y += line_h
    img.save(page_png)
    return True
