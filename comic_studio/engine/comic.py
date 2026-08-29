# comic_studio/engine/comic.py
"""P8 漫画→视频（2026-08-29）：每图一镜，复用 fl2v 链路。

页 i = 镜 i 的首帧（kf_start.png）、页 i+1 = 镜 i 的尾帧（kf_end.png）
→ 渲染走既有 fl2v 首尾帧插值 = 翻页过渡动画；门禁/渲染/合成/多版本全复用。
提示词由 VLM 读图生成（describe_shots，多模态 raw_chat）或人工填写。"""
import base64
import json
import threading

from .logbus import emit as emit_log

# LM Studio 不支持并发请求（两个同时进去 → terminated，2026-08-29 真机）
# 全局锁串行化所有 VLM 调用——多镜并发提交自然排队
_VLM_LOCK = threading.Lock()


def import_comic(db, data_dir, name: str, aspect: str,
                 image_blobs: list, comic_mode: str = "motion_comic") -> dict:
    """image_blobs：[(filename, bytes)]，顺序即页序。comic_mode：
    motion_comic（动态漫/fl2v 翻页）| film_adaptation（漫改/ref2va 动画）。"""
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
    proj = create_project(db, data_dir, name, aspect, placeholder,
                          comic_mode=comic_mode)
    pid = proj["id"]
    slug = proj["slug"]

    # 渲染方式按模式：动态漫=fl2v（翻页插值），漫改=ref2va（参考图动画）
    workflow = "fl2v" if comic_mode != "film_adaptation" else "ref2va"

    # 页落盘为各镜关键帧
    from pathlib import Path
    page_files: list[Path] = []
    for i, (fname, blob) in enumerate(image_blobs, 1):
        shot_dir = data_to_abs(data_dir, f"projects/{slug}/shots/{i}")
        shot_dir.mkdir(parents=True, exist_ok=True)
        p = shot_dir / "kf_start.png"
        p.write_bytes(blob)
        page_files.append(p)
    # 页 i+1 → 镜 i 尾帧（最后一镜无）——动态漫用，漫改模式仅供参考
    for i in range(1, n):
        end = page_files[i - 1].parent / "kf_end.png"
        end.write_bytes(page_files[i].read_bytes())

    drafts = [NS(text_span="", description=f"漫画第{i}页",
                 shot_type="", camera={"景别": "中景", "机位": "平视",
                                       "运镜": "固定", "转场": "切"},
                 duration=5.0, workflow_type=workflow, ledger={},
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
    comic_mode = proj["comic_mode"] if "comic_mode" in proj.keys() else "motion_comic"

    if comic_mode == "film_adaptation":
        # 漫改电影模式：动画描述（角色动起来、镜头运动、背景变化）
        system = (
            "你是漫改电影的动画导演。你会收到漫画的一格画面。\n"
            "你的任务：将这格静态漫画转化为动态动画场景的视频提示词。\n\n"
            "分析步骤（内部完成，不要输出）：\n"
            "1. 看画面：谁在做什么？什么表情？什么场景？\n"
            "2. 思考：如果要「活起来」，角色会做什么动作？镜头怎么运动？背景有什么变化？\n"
            "3. 对白：如有对白气泡，按阅读顺序整理\n\n"
            "输出（直接输出，不解释）：\n"
            "一段中文提示词，120 字以内，必须包含：\n"
            "- 具体的角色动作（如「缓步向前走」「转头看向」「伸手拿取」）\n"
            "- 镜头运动（如「镜头缓缓推近」「横移跟拍」「轻微俯仰」）\n"
            "- 背景/环境动态（如「风吹动树叶」「光线渐变」「雨滴落下」）\n"
            "- 表情变化过程（如「从微笑到惊讶」）\n"
            "- 对白（如有）：「角色名：「台词」」\n"
            "关键：描述「正在发生的动画」，不是静态画面描述。")
    else:
        # 动态漫模式：翻页过渡（现有行为）
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
            with _VLM_LOCK:  # LM Studio 串行化
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


def extract_comic_characters(db, data_dir, project_id, client, max_pages=3) -> int:
    """P8-B 漫改模式：VLM 读前几页漫画 → 提取角色（名字+外貌）→ 建资产。
    之后生成参考图（gen_ref），ref2va 渲染用。返回提取的角色数。"""
    from .paths import data_to_abs
    from .projects import get_project
    from .shots import list_shots
    from .assets import persist_assets
    from types import SimpleNamespace as NS
    import re as _re

    proj = get_project(db, project_id)
    if proj is None:
        raise ValueError(f"项目不存在: {project_id}")
    slug = proj["slug"]
    shots = list_shots(db, project_id)[:max_pages]
    if not shots:
        raise ValueError("无分镜可提取")

    emit_log(db, "llm", "info", f"VLM 读前 {len(shots)} 页提取角色…",
             project_id=project_id)
    system = (
        "你是角色设计师。给定漫画页面，提取所有出现的角色。\n"
        '输出 JSON 数组：[{"name":"角色名","appearance":"外貌行模板"}]\n'
        "外貌行模板格式（每行一项）：性别：\\n年龄：\\n发色发型：\\n服装：\\n…\n"
        "只输出 JSON，不解释。")
    content = [{"type": "text", "text": "提取这些漫画页面中的所有角色："}]
    for s in shots:
        png = data_to_abs(data_dir, f"projects/{slug}/shots/{s['seq']}/kf_start.png")
        if png.exists():
            b64 = base64.b64encode(png.read_bytes()).decode()
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"}})

    with _VLM_LOCK:
        text, _u = client.raw_chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": content}], temperature=0.3)
    text = (text or "").strip()

    m = _re.search(r"\[.*\]", text, _re.DOTALL)
    if not m:
        raise ValueError(f"VLM 未返回角色 JSON：{text[:100]}")
    chars = json.loads(m.group())

    char_ns = [NS(name=c["name"], appearance=c.get("appearance", ""), tags=["comic"])
               for c in chars if c.get("name")]
    if not char_ns:
        return 0
    persist_assets(db, data_dir, project_id, NS(characters=char_ns, scenes=[], props=[]))
    emit_log(db, "llm", "info",
             f"角色提取完成：{len(char_ns)} 个（{', '.join(c.name for c in char_ns)}）",
             project_id=project_id)
    return len(char_ns)


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
