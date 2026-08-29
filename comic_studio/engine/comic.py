# comic_studio/engine/comic.py
"""P8 漫画→视频（2026-08-29）：每图一镜，复用 fl2v 链路。

页 i = 镜 i 的首帧（kf_start.png）、页 i+1 = 镜 i 的尾帧（kf_end.png）
→ 渲染走既有 fl2v 首尾帧插值 = 翻页过渡动画；门禁/渲染/合成/多版本全复用。
提示词由 VLM 读图生成（describe_shots，多模态 raw_chat）或人工填写。"""
import base64
import json

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
    system = (
        "你是漫画转视频的分镜导演。你会收到同一漫画的两格画面（第一张=首帧，第二张=尾帧）。\n"
        "你的任务：分析两格之间的剧情变化（含对白），写出视频生成提示词。\n\n"
        "分析步骤（内部完成，不要输出）：\n"
        "1. 看第一张图：谁在做什么？什么表情？什么场景？有什么对白气泡？\n"
        "2. 看第二张图：发生了什么变化？有什么对白气泡？\n"
        "3. 对白排序：按漫画阅读顺序（从上到下、从右到左）整理两格中出现的所有对白，"
        "标注说话人；多段对白按先后顺序排列\n"
        "4. 推导：从第一格到第二格，人物做了什么动作？说了什么话？镜头怎么动？\n\n"
        "输出格式（直接输出，不解释）：\n"
        "一段中文提示词，120 字以内，包含：\n"
        "- 人物名字+动作+表情变化+镜头运动+环境变化\n"
        "- 对白：如果有对白气泡，按顺序写出「角色名：「台词」」\n"
        "必须描述「从第一格到第二格的具体过渡过程」，不能只描述单帧静态画面。\n"
        "对白必须按漫画中的实际顺序排列，不能乱序。")
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
        emit_log(db, "llm", "info",
                 f"镜 {s['seq']} 读图中（"
                 + ("首尾两帧" if end_png.exists() else "仅首帧") + "）…",
                 project_id=project_id)
        content = [{"type": "text", "text":
                    (f"第一张图=第 {s['seq']} 格（视频首帧），"
                     f"第二张图=第 {s['seq'] + 1} 格（视频尾帧）。"
                     if end_png.exists() else
                     f"只有一张图=第 {s['seq']} 格（最后一页，无下一格）。请描述这一格画面的动态延展。")}]
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
            # 从提示词中提取对白（「角色名：「台词」」格式）→ ledger.dialogue
            # 联动 TTS 配音 + 字幕烧录链路（2026-08-29 漫画对白需求）
            dialogue = _extract_dialogue(text)
            ledger = json.loads(s["ledger_json"] or "{}")
            if dialogue:
                ledger["dialogue"] = dialogue
            update_shot(db, s["id"], {
                "prompt": text, "description": text,
                "ledger_json": json.dumps(ledger, ensure_ascii=False)})
            n += 1
            # 逐镜日志（用户需求：每个操作都要可见——批量跑 16 镜不能只看最终汇总）
            emit_log(db, "llm", "info",
                     f"镜 {s['seq']} 提示词就绪（{len(text)} 字"
                     + (f"，{len(dialogue)} 句对白" if dialogue else "") + "）",
                     project_id=project_id)
        else:
            emit_log(db, "llm", "warn",
                     f"镜 {s['seq']}：模型返回空结果，跳过",
                     project_id=project_id)
    if n:
        emit_log(db, "llm", "info", f"VLM 读图生成提示词 {n} 镜", project_id=project_id)
    return n


_DIALOGUE_RE = None


def _extract_dialogue(text: str) -> list:
    """从 VLM 输出中提取「角色名：「台词」」格式的对白，按出现顺序返回。"""
    global _DIALOGUE_RE
    if _DIALOGUE_RE is None:
        import re
        _DIALOGUE_RE = re.compile(
            r"([一-龥A-Za-z·]{1,8})[：:]\s*[「“]([^」”]{1,80})[」”]")
    return [{"speaker": m.group(1), "line": m.group(2)}
            for m in _DIALOGUE_RE.finditer(text)]
