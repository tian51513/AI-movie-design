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
    from .projects import ASPECT_RATIOS
    if aspect not in ASPECT_RATIOS:
        raise ValueError(f"aspect_ratio 只能是 {'/'.join(ASPECT_RATIOS)}: {aspect}")
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
        # 漫改电影模式：动画描述 + 画风转换（2026-08-29 用户需求：
        # 漫画二次元↔真人项目画风不匹配时需转换，不能出混合体）
        proj_style = (proj["style_vis"] or proj["style"] or "").strip()
        style_hint = ""
        if proj_style:
            style_hint = (
                f"\n\n【画风要求】目标画风：{proj_style}\n"
                "先判断漫画原画的画风（二次元动漫/真人摄影/水彩/美漫等），"
                "如果与目标画风不一致，必须在提示词中加入画风转换描述"
                "（如：将二次元角色转为真人质感，保持五官特征但皮肤/材质真实化）。"
                "如果一致，直接使用原画风描述。")
        system = (
            "你是漫改电影的动画导演。你会收到漫画的一格画面。\n"
            "你的任务：将这格静态漫画转化为动态动画场景的视频提示词。\n\n"
            "分析步骤（内部完成，不要输出）：\n"
            "1. 看画面：有哪些角色？谁在做什么？什么表情？什么场景？什么画风？\n"
            "2. 多角色处理：如果画面有多人，识别每个人的名字和位置；"
            "主要角色（有动作/有对白的）详细描述动作，"
            "背景人物简略（如「背景中宾客鼓掌」）\n"
            "3. 思考：如果要「活起来」，主要角色会做什么动作？镜头怎么运动？\n"
            "4. 对白：如有对白气泡，按阅读顺序整理\n"
            "5. 画风：检查漫画原画风格与目标画风是否一致，不一致则加入转换指令"
            + style_hint +
            "\n\n输出（直接输出，不解释）——严格按以下骨架，节标题逐字使用、独占一行：\n"
            "subject_definitions:\n"
            "<角色名> 是来自 <Picture 1> 的人物，其外观由该图提供（每个主要角色一条）\n"
            "summary:\n一句话：本镜核心内容与运镜\n"
            "retention_analysis:\n"
            "<角色名>：fully_preserved - 保持<发型/服装/身份特征>（每个角色一条）\n"
            "detailed_description:\n"
            "[环境与光线一段]\n"
            "主要角色具体动作（用角色名不用代词）+ 表情变化 + 镜头运动（如「镜头缓缓推近」）；"
            "对白写「角色名：「台词」」；背景角色简略但不遗漏\n"
            "overall_soundscape:\n"
            "只写与画面一致的对白声/环境声/动作音，音量轻微；禁止广播嘈杂等无关声源；"
            "无对白时写明无对白无哼唱\n"
            "non_diegetic_music: N/A\n"
            "关键：描述「正在发生的动画」，不是静态画面；画风转换要求（若有）写进 detailed_description。")
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
            "integrated_multimodal_description: [Shot 1] <一段描述：从第一格到第二格的具体"
            "过渡——人物名字+动作+表情变化+镜头运动+环境变化，120~200 字；"
            "对白按漫画实际顺序写「角色名：「台词」」>\n"
            "overall_soundscape: <只写与画面一致的对白声/环境声/动作音，音量轻微；"
            "禁止广播嘈杂等无关声源；无对白时写明无对白无哼唱>\n"
            "non_diegetic_music: N/A\n"
            "必须描述「从第一格到第二格的具体过渡过程」，不能只描述单帧静态画面。\n"
            "对白必须按漫画中的实际顺序排列，不能乱序。\n\n"
            "对白说话人音色标注（角色音色系统）：输出最后一行严格按此格式（不要代码块）：\n"
            'VOICES:{"voices":[{"name":"说话人","gender":"女","age":8,"voice":"库内音色名"}]}\n'
            "只列真实人物（旁白/画外音/内心独白不要）；gender 男/女，age 数字；\n"
            "voice 从下方可用音色库中按角色年龄/性别/气质选最贴切的。\n\n"
            "可用音色库（voice 只能从中选）：\n" + _voice_lib(db, data_dir, project_id))
    n = 0
    voices_meta: dict = {}   # 说话人 → {gender, age, voice}（VOICES 尾行聚合）
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
        voices_meta.update(_parse_voices_line(text))
        text = _strip_voices_line(text)
        if text:
            # 结构化+音频协议落库前统一自愈（2026-08-30）：缺音频节机械补、
            # 超界 Picture 引用清理、无字幕后缀——与小说链路同等待遇
            from .prompts.gen import heal_h3_prompt
            text, _heal_fixes = heal_h3_prompt(text, s, max_pics=2)
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
        # 动态漫角色音色（2026-08-31 用户需求）：对白聚合建角色（旁白过滤，
        # 不生参考图——fl2v 用原页）+ 音色绑定；幂等（同名不重建）
        if comic_mode != "film_adaptation":
            built = _build_speaker_assets(db, data_dir, project_id, voices_meta)
            if built:
                emit_log(db, "llm", "info",
                         f"对白角色 {built} 个入库（不生参考图，已自动匹配音色）",
                         project_id=project_id)
        # 读图顺手提取角色——仅漫改（2026-08-29 真机教训：动态漫 fl2v 用漫画原页
        # 渲染，角色资产毫无用处，还提取出 83 个旁白/叙述垃圾资产 + 1195 处绑定）
        if comic_mode == "film_adaptation":
            _extract_characters_from_prompts(db, data_dir, project_id)
            # 自动绑定角色到分镜
            from .llm.storyboard import auto_bind_characters
            bound = auto_bind_characters(db, project_id)
            if bound:
                emit_log(db, "llm", "info", f"角色自动绑定：{bound} 处",
                         project_id=project_id)
    return n


_NON_SPEAKER_RE = None


def _voice_lib(db, data_dir, project_id) -> str:
    """音色库清单（注入 VLM 系统词；项目视角含项目级自定义）。"""
    from .voicelib import voice_library_prompt
    from .projects import get_project
    proj = get_project(db, project_id)
    return voice_library_prompt(data_dir, proj["slug"] if proj else None) or "（空）"


def _parse_voices_line(text: str) -> dict:
    """解析输出尾行 VOICES:{"voices":[...]} → {name: {gender, age, voice}}。"""
    import re as _re
    global _NON_SPEAKER_RE
    if _NON_SPEAKER_RE is None:
        _NON_SPEAKER_RE = _re.compile(r"旁白|画外音|独白|叙述|字幕|旁白声")
    m = _re.search(r"^VOICES:\s*(\{.*\})\s*$", text or "", _re.M)
    if not m:
        return {}
    try:
        items = json.loads(m.group(1)).get("voices") or []
    except (json.JSONDecodeError, AttributeError):
        return {}
    out = {}
    for v in items:
        name = str(v.get("name") or "").strip()
        if name and not _NON_SPEAKER_RE.search(name):
            out[name] = {"gender": v.get("gender") or "", "age": v.get("age"),
                         "voice": v.get("voice") or ""}
    return out


def _strip_voices_line(text: str) -> str:
    import re as _re
    return _re.sub(r"^VOICES:\s*\{.*\}\s*$\n?", "", text or "", flags=_re.M).rstrip()


def _bind_asset_voice(db, project_id, name: str, voice: str) -> bool:
    conn = db.connect()
    cur = conn.execute(
        "UPDATE assets SET voice=? WHERE id=("
        " SELECT a.id FROM assets a JOIN project_assets pa ON pa.asset_id=a.id"
        " WHERE pa.project_id=? AND a.name=? AND a.kind='character')",
        (voice, project_id, name))
    conn.commit()
    return bool(cur.rowcount)


def _build_speaker_assets(db, data_dir, project_id, voices_meta: dict) -> int:
    """动态漫：ledger.dialogue 说话人聚合 → 建 character 资产（无参考图）+ 音色。"""
    from types import SimpleNamespace as NS
    from .assets import list_project_assets, persist_assets
    from .shots import list_shots
    from .voices import match_voice
    from .voicelib import voice_library_names
    from .projects import get_project
    speakers: list = []
    for s in list_shots(db, project_id):
        for d in json.loads(s["ledger_json"] or "{}").get("dialogue") or []:
            sp = str(d.get("speaker") or "").strip()
            if sp and sp not in speakers and not _NON_SPEAKER_RE.search(sp):
                speakers.append(sp)
    if not speakers:
        return 0
    existing = {a["name"] for a in list_project_assets(db, project_id)
                if a["kind"] == "character"}
    drafts, metas = [], {}
    for sp in speakers:
        if sp in existing:
            continue
        v = voices_meta.get(sp) or {}
        app = (f"性别：{v.get('gender') or '未知'}\n年龄：{v.get('age') or '未知'}岁"
               if (v.get("gender") or v.get("age")) else f"（{sp}：动态漫对白角色）")
        drafts.append(NS(name=sp, appearance=app, tags=[]))
        metas[sp] = v
    if drafts:
        persist_assets(db, data_dir, project_id,
                       NS(characters=drafts, scenes=[], props=[]))
    slug = get_project(db, project_id)["slug"]
    lib = voice_library_names(data_dir, slug)
    bound = 0
    for sp in speakers:   # 已存在的同名角色也补绑（仅当 voice 为空）
        v = voices_meta.get(sp) or {}
        app = f"性别：{v.get('gender') or ''}\n年龄：{v.get('age') or ''}岁"
        voice = match_voice(app, v.get("voice", ""), library=lib)
        if voice:
            conn = db.connect()
            cur = conn.execute(
                "UPDATE assets SET voice=? WHERE id=("
                " SELECT a.id FROM assets a JOIN project_assets pa ON pa.asset_id=a.id"
                " WHERE pa.project_id=? AND a.name=? AND a.kind='character'"
                " AND a.voice='')", (voice, project_id, sp))
            conn.commit()
            bound += cur.rowcount
    return len(drafts) or bound


def _extract_characters_from_prompts(db, data_dir, project_id) -> int:
    """从已生成的提示词对白中提取角色名 → 建资产（仅漫改调用）。
    过滤规则（2026-08-29 真机教训：旧正则把旁白/对白标签、「随后前夫问道」类
    叙述短语全当人名，83 个资产里只有 1 个真名）：
    说话人须全篇出现 ≥2 次 + 长度 ≤4 + 叙述词黑名单 + 动词后缀排除。"""
    from collections import Counter
    from .shots import list_shots
    from .assets import list_project_assets, persist_assets
    from types import SimpleNamespace as NS

    # 已有角色名集合（不重复创建）
    existing = {a["name"] for a in list_project_assets(db, project_id)
                if a["kind"] == "character"}

    # 从对白提取说话人（_extract_dialogue 要求 名字+引号台词 严格格式，
    # 比裸正则可靠：不再匹配「随后前夫问道：「」前的叙述前缀以外的噪音）
    counts = Counter()
    contexts = {}
    for s in list_shots(db, project_id):
        prompt = s["prompt"] or ""
        for d in _extract_dialogue(prompt):
            name = (d.get("speaker") or "").strip()
            if not name or name in existing:
                continue
            counts[name] += 1
            if name not in contexts:
                contexts[name] = prompt[:80]

    found = {n: c for n, c in counts.items() if _is_real_character_name(n, c)}
    if not found:
        return 0

    # 建资产（外貌从上下文推断）
    char_ns = [NS(name=n,
                  appearance=f"性别：待确认\n来源：VLM 读图提取\n上下文：{contexts[n][:60]}",
                  tags=["comic"])
               for n in found]
    persist_assets(db, data_dir, project_id,
                   NS(characters=char_ns, scenes=[], props=[]))
    emit_log(db, "llm", "info",
             f"读图顺手提取角色：{len(char_ns)} 个（{', '.join(found)}）",
             project_id=project_id)
    return len(char_ns)


# 叙述词黑名单：VLM 提示词里的高频非人名说话人/标签（子串匹配）
_NARRATION_WORDS = ("旁白", "对白", "画外", "独白", "心声", "字幕", "文字", "气泡",
                    "伴随", "镜头", "画面", "背景", "角色", "女性", "男性", "音效",
                    "标题", "旁白说道")
# 动词后缀：叙述句尾缀被切成"名字"（儿子心想/贴榻榻米喊出）
_SPEAK_VERBS = ("说道", "问道", "喊出", "心想", "感叹", "回应", "低语", "开口",
                "回答", "自语", "大喊", "轻声")


def _is_real_character_name(name: str, count: int) -> bool:
    """机械过滤：≥2 次出现（复发性=真角色）+ ≤4 字 + 非叙述词。"""
    if len(name) > 4 or count < 2:
        return False
    if any(w in name for w in _NARRATION_WORDS):
        return False
    if name.endswith(_SPEAK_VERBS):
        return False
    return name not in ("背景", "镜头", "画面", "角色", "模型")


def purge_comic_assets(db, data_dir, project_id) -> int:
    """清理读图提取的资产（2026-08-29 动态漫误提取善后）：
    删资产行 + project_assets 引用 + library 目录 + 分镜 ledger 角色绑定。
    只清 tags 含 comic 的——LLM 分析来的资产不动。返回清理数。"""
    import shutil
    from .assets import list_project_assets
    from .paths import data_to_abs
    from .shots import list_shots
    rows = []
    for a in list_project_assets(db, project_id):
        try:
            tags = json.loads(a["tags_json"] or "[]")
        except Exception:
            tags = []
        if "comic" in tags:
            rows.append(a)
    if not rows:
        return 0
    ids = {a["id"] for a in rows}
    for a in rows:
        if a["library_dir"]:
            d = data_to_abs(data_dir, a["library_dir"])
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
    conn = db.connect()
    ph = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM project_assets WHERE asset_id IN ({ph})", sorted(ids))
    conn.execute(f"DELETE FROM assets WHERE id IN ({ph})", sorted(ids))
    # 分镜绑定同步清理（对白等其他 ledger 字段不动）
    for s in list_shots(db, project_id):
        ledger = json.loads(s["ledger_json"] or "{}")
        assets = ledger.setdefault("assets", {})
        chars = assets.get("characters") or []
        rest = [c for c in chars if c not in ids]
        if rest != chars:
            assets["characters"] = rest
            conn.execute("UPDATE shots SET ledger_json=? WHERE id=?",
                         (json.dumps(ledger, ensure_ascii=False), s["id"]))
    conn.commit()
    emit_log(db, "llm", "info",
             f"清理提取资产 {len(rows)} 个（含分镜绑定）", project_id=project_id)
    return len(rows)


def _sample_shot_indices(total: int, sample_size: int = 9) -> list[int]:
    """全篇均匀采样（2026-08-29 用户需求：后面出场的人物不能漏）：
    开头密、中间稀、结尾密——覆盖故事弧度变化最大的位置。"""
    if total <= sample_size:
        return list(range(1, total + 1))
    # 位置：前 1/4 取 3 页 + 中间取 3 页 + 后 1/4 取 3 页
    q1 = max(1, total // 4)
    q3 = max(q1 + 1, total * 3 // 4)
    picks = sorted(set([
        1, min(2, total), min(3, total),           # 开头
        max(4, total // 2 - 1), total // 2, total // 2 + 1,  # 中间
        max(q3, total - 2), max(q3 + 1, total - 1), total,   # 结尾
    ]))
    return [i for i in picks if 1 <= i <= total][:sample_size]


def extract_comic_characters(db, data_dir, project_id, client, max_pages=9) -> int:
    """P8-B 漫改模式：VLM 全篇采样读漫画 → 提取角色（名字+外貌）→ 建资产。
    之后生成参考图（gen_ref），ref2va 渲染用。返回提取的角色数。
    采样策略：开头+中间+结尾均匀取页，覆盖后面出场的人物。"""
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
    all_shots = list_shots(db, project_id)
    if not all_shots:
        raise ValueError("无分镜可提取")
    # 全篇采样（不只前几页——后面出场的人物不能漏）
    indices = _sample_shot_indices(len(all_shots), sample_size=max_pages)
    shots = [all_shots[i - 1] for i in indices]  # seq 从 1 起
    emit_log(db, "llm", "info",
             f"VLM 采样 {len(shots)} 页提取角色（全篇 {len(all_shots)} 页，"
             f"采样位置：{indices}）",
             project_id=project_id)
    system = (
        "你是漫改电影的美术指导。给定漫画页面，提取角色、场景和重要道具。\n"
        "输出 JSON：\n"
        '{"characters":[{"name":"角色名","appearance":"外貌行模板","suggested_voice":"库内音色名"}],\n'
        ' "scenes":[{"name":"场景名","appearance":"场景描述"}],\n'
        ' "props":[{"name":"道具名","appearance":"道具描述"}]}\n'
        "外貌行模板格式（每行一项）：性别：\\n年龄：\\n发色发型：\\n服装：\\n…\n"
        "场景描述：空间结构、光线氛围、色调、时代风格。\n"
        "道具描述：外观形状、材质质感、颜色纹样。\n"
        "只输出 JSON，不解释。\n\n"
        "角色音色（角色音色系统）：suggested_voice 从下方可用音色库中"
        "按角色年龄/性别/气质选最贴切的；拿不准按 性别×年龄 基线选。\n\n"
        "可用音色库（suggested_voice 只能从中选）：\n" + _voice_lib(db, data_dir, project_id))
    content = [{"type": "text", "text": "提取这些漫画页面中的所有角色、场景和重要道具："}]
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

    m = _re.search(r"\{.*\}", text, _re.DOTALL)
    if not m:
        raise ValueError(f"VLM 未返回资产 JSON：{text[:100]}")
    data = json.loads(m.group())

    # 清理旧资产（用户需求：多次提取只保留最后一次）
    conn = db.connect()
    old_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM assets WHERE source_project=?", (project_id,)).fetchall()]
    if old_ids:
        ph = ",".join("?" * len(old_ids))
        # 连目录一起删（2026-08-29 幽灵图教训：删行不删目录，id 复用后新资产继承旧图）
        import shutil
        for r in conn.execute(
                f"SELECT library_dir FROM assets WHERE id IN ({ph})",
                old_ids).fetchall():
            if r["library_dir"]:
                d = data_to_abs(data_dir, r["library_dir"])
                if d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
        conn.execute(f"DELETE FROM project_assets WHERE asset_id IN ({ph})", old_ids)
        conn.execute(f"DELETE FROM assets WHERE id IN ({ph})", old_ids)
        conn.commit()
        emit_log(db, "llm", "info",
                 f"清理旧资产 {len(old_ids)} 个（重新提取）",
                 project_id=project_id)

    def _to_str(v):
        """VLM 可能返回列表而非字符串——统一转为换行分隔的字符串。"""
        if isinstance(v, list):
            return "\n".join(str(x) for x in v)
        return str(v or "")

    char_ns = [NS(name=c["name"], appearance=_to_str(c.get("appearance")), tags=["comic"])
               for c in (data.get("characters") or []) if c.get("name")]
    scene_ns = [NS(name=s["name"], appearance=_to_str(s.get("appearance")), tags=["comic"])
                for s in (data.get("scenes") or []) if s.get("name")]
    prop_ns = [NS(name=p["name"], appearance=_to_str(p.get("appearance")), tags=["comic"])
               for p in (data.get("props") or []) if p.get("name")]

    total = len(char_ns) + len(scene_ns) + len(prop_ns)
    if total == 0:
        return 0
    persist_assets(db, data_dir, project_id,
                   NS(characters=char_ns, scenes=scene_ns, props=prop_ns))
    # 角色音色绑定（2026-08-31）：建议优先（库内名），非法走性别×年龄基线
    from .voices import match_voice
    from .voicelib import voice_library_names
    lib = voice_library_names(data_dir, proj["slug"])
    conn = db.connect()
    for c in (data.get("characters") or []):
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        app = _to_str(c.get("appearance"))
        voice = match_voice(app, str(c.get("suggested_voice") or ""), library=lib)
        if voice:
            conn.execute(
                "UPDATE assets SET voice=? WHERE id=("
                " SELECT a.id FROM assets a JOIN project_assets pa ON pa.asset_id=a.id"
                " WHERE pa.project_id=? AND a.name=? AND a.kind='character')",
                (voice, project_id, name))
    conn.commit()
    parts = []
    if char_ns:
        parts.append(f"角色 {len(char_ns)}：{', '.join(c.name for c in char_ns)}")
    if scene_ns:
        parts.append(f"场景 {len(scene_ns)}：{', '.join(s.name for s in scene_ns)}")
    if prop_ns:
        parts.append(f"道具 {len(prop_ns)}：{', '.join(p.name for p in prop_ns)}")
    emit_log(db, "llm", "info", f"资产提取完成——{'；'.join(parts)}",
             project_id=project_id)
    return total


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
