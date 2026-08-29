# comic_studio/engine/comic.py
"""P8 漫画→视频（2026-08-29）：每图一镜，复用 fl2v 链路。

页 i = 镜 i 的首帧（kf_start.png）、页 i+1 = 镜 i 的尾帧（kf_end.png）
→ 渲染走既有 fl2v 首尾帧插值 = 翻页过渡动画；门禁/渲染/合成/多版本全复用。
提示词由 VLM 读图生成（describe_shots，多模态 raw_chat）或人工填写。"""
import base64

from .logbus import emit as emit_log


def import_comic(db, data_dir, name: str, aspect: str,
                 image_blobs: list) -> dict:
    """image_blobs：[(filename, bytes)]，顺序即页序。建项目（跳过分析/拆解，
    stage 直达 storyboard_ready）+ 每图一镜（fl2v）+ 关键帧落位。"""
    if not image_blobs:
        raise ValueError("至少需要一张漫画页")
    if aspect not in ("9:16", "16:9"):
        raise ValueError(f"aspect_ratio 只能是 9:16/16:9: {aspect}")
    from .projects import create_project, set_stage
    from .shots import persist_shots
    from .paths import data_to_abs
    from types import SimpleNamespace as NS

    n = len(image_blobs)
    placeholder = f"（漫画导入：{n} 页，画面见各镜关键帧）"
    proj = create_project(db, data_dir, name, aspect, placeholder)
    pid = proj["id"]
    slug = proj["slug"]

    # 页落盘为各镜关键帧
    from pathlib import Path
    page_files: list[Path] = []
    for i, (fname, blob) in enumerate(image_blobs, 1):
        shot_dir = data_to_abs(data_dir, f"projects/{slug}/shots/{i}")
        shot_dir.mkdir(parents=True, exist_ok=True)
        p = shot_dir / "kf_start.png"
        p.write_bytes(blob)
        page_files.append(p)
    # 页 i+1 → 镜 i 尾帧（最后一镜无）
    for i in range(1, n):
        end = page_files[i - 1].parent / "kf_end.png"
        end.write_bytes(page_files[i].read_bytes())

    drafts = [NS(text_span="", description=f"漫画第{i}页",
                 shot_type="", camera={"景别": "中景", "机位": "平视",
                                       "运镜": "固定", "转场": "切"},
                 duration=5.0, workflow_type="fl2v", ledger={},
                 character_ids=[], scene_ids=[], prop_ids=[],
                 depends_on=None, prompt="")
              for i in range(1, n + 1)]
    ids = persist_shots(db, pid, drafts)
    # 逐镜尾帧衔接链（既有 fl2v 依赖链语义）
    conn = db.connect()
    for prev, cur in zip(ids, ids[1:]):
        conn.execute("UPDATE shots SET depends_on=? WHERE id=?", (prev, cur))
    conn.commit()
    set_stage(db, pid, "storyboard_ready")
    emit_log(db, "system", "info",
             f"漫画导入：{n} 页 → {n} 镜（fl2v 翻页链），直达分镜就绪",
             project_id=pid)
    return proj


def describe_shots(db, data_dir, project_id, client, shot_id=None) -> int:
    """VLM 读图生成每镜视频提示词（多模态 raw_chat：首帧必带、尾帧可选）。
    shot_id 指定时只跑该镜（已有提示词也覆盖）；否则跑全部缺失的镜。返回生成数。"""
    from .paths import data_to_abs
    from .projects import get_project
    from .shots import list_shots, update_shot

    proj = get_project(db, project_id)
    if proj is None:
        raise ValueError(f"项目不存在: {project_id}")
    slug = proj["slug"]
    system = ("你是视频提示词工程师。给定漫画当前页（首帧）与下一页（尾帧），"
              "写一段可直接用于图生视频的中文提示词：描述从首帧画面到尾帧画面的"
              "自然过渡（人物动作/镜头微动/环境变化），80 字内单段，不解释。")
    n = 0
    for s in list_shots(db, project_id):
        if shot_id is not None and s["id"] != shot_id:
            continue  # 逐镜模式：只跑指定镜
        if shot_id is None and (s["prompt"] or "").strip():
            continue  # 批量模式：跳过已有提示词的镜
        shot_dir = data_to_abs(data_dir, f"projects/{slug}/shots/{s['seq']}")
        start_png = shot_dir / "kf_start.png"
        end_png = shot_dir / "kf_end.png"
        if not start_png.exists():
            continue
        content = [{"type": "text", "text":
                    f"第 {s['seq']} 页（首帧）→ 第 {s['seq'] + 1} 页（尾帧）"}]
        for png in (start_png, end_png):
            if png.exists():
                b64 = base64.b64encode(png.read_bytes()).decode()
                content.append({"type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{b64}"}})
        try:
            text, _u = client.raw_chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": content}], temperature=0.4)
        except Exception as img_exc:
            # 图片不被支持（Ollama 量化缺 mmproj 等）→ 文字降级不硬卡
            if "image" not in str(img_exc).lower() and "mmproj" not in str(img_exc).lower():
                raise  # 非图片类异常照常抛
            emit_log(db, "llm", "warn",
                     f"镜 {s['seq']}：模型不支持图片输入（{str(img_exc)[:60]}…），"
                     f"降级为纯文字描述", project_id=project_id)
            text, _u = client.raw_chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content":
                  f"第 {s['seq']} 页漫画（无图可看，按页码推断）："
                  f"从当前画面到下一页的自然过渡，写一段视频提示词。"}],
                temperature=0.4)
        text = (text or "").strip()
        if text:
            update_shot(db, s["id"], {"prompt": text, "description": text})
            n += 1
    if n:
        emit_log(db, "llm", "info", f"VLM 读图生成提示词 {n} 镜", project_id=project_id)
    return n
