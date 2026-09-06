# comic_studio/web/routes_projects.py
"""项目 REST：创建（上传小说）、列表、详情。"""
import json
import re
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from ..engine.llm.provider import client_for_task
from ..engine.logbus import emit as emit_log
from ..engine.projects import create_project, get_project, list_projects

router = APIRouter(prefix="/api/projects", tags=["projects"])

GEN_STORY_SYSTEM = """你是漫剧改编用的小说作者。按给定主题写一部适合改编为漫画短剧的小说正文：
- 目标 {target_words}，多个具体场景（利于拆分镜），人物有名字与外形特征
- 以画面感写作：动作、表情、环境光线、对白（对白自然口语化）
- 段落之间用空行分隔；不写章节标题、目录、作者注或任何解释
- 直接输出正文本身。

短剧结构规范（借鉴短剧厂方法论，2026-08-29 第三批）：
- 黄金开头：正文前三句必须是强钩子——生死绝境/极端羞辱地位反差/身份暴跌或
  狂飙/物证背叛/反常理悬念任选其一；严禁"早晨拉窗帘"、大段环境铺陈、
  咖啡厅闲聊式开场
- 情绪流变：每场景有清晰的情绪链（如 遭受刁难→隐忍试探→强力反转→悬念收束），
  相邻场景情绪基调不重复
- 断章卡点：每个大场景结尾留在悬念上（真相将揭未揭/危机悬停/金句落地），
  严禁把反转爽完再收尾
- 对白语速：台词总量按 3.5~4.5 字/秒估算，单句常规 12~18 字、金句 8~12 字、
  不超 25 字；对白之间穿插动作与微表情留白（生理可观测动作如指节泛白、
  瞳孔骤缩，禁抽象心理词）
- 禁反向灌输：角色不口头互念双方已知的背景设定，信息用物证/动作带出"""

# 默认目标字数（不传 word_count 时）；可传 word_count 控制（真机 2026-08-27：
# 主题生成 21862 字正文 → 分镜分块过大撞上下文截断，源头控制篇幅）
DEFAULT_STORY_WORDS = "8000~12000 字"
WORD_COUNT_RANGE = (300, 20000)

_PUBLIC_COLUMNS = ("id", "slug", "name", "aspect_ratio", "stage", "created_at", "style", "style_vis", "era", "comic_mode",
                    "video_megapixels", "video_multiple", "video_speed", "default_shot_duration",
                    "prompt_mode", "lora_realism", "target_duration", "autopilot")


@router.post("/{project_id}/retry-transcribe", status_code=202)
def retry_transcribe(request: Request, project_id: int):
    """P10 转写手动重发（2026-09-05 真机：interrupted/failed 后此前只能删项目
    重传）。源音频在即可重发；在飞 409。"""
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    from ..engine.asr import load_segments
    from ..engine.paths import data_to_abs as _dta
    adir = _dta(request.app.state.data_dir, f"projects/{proj['slug']}/audio")
    src = next(adir.glob("source.*"), None) if adir.is_dir() else None
    if src is None:
        raise HTTPException(422, "项目无源音频（非有声书项目？）")
    if load_segments(request.app.state.data_dir, proj["slug"]) is not None:
        raise HTTPException(409, "转写已完成（segments 在盘）——无需重发")
    from ..engine import jobs as jobs_mod
    act = jobs_mod.latest_job(db, project_id, "transcribe")
    if act and act["status"] in ("pending", "running"):
        raise HTTPException(409, "转写已在队列/进行中")
    from ..engine.jobs import enqueue_job
    jid = enqueue_job(db, "transcribe", project_id=project_id,
                      payload={"project_id": project_id,
                               "ext": src.suffix.lstrip(".") or "mp3"})
    return {"job_id": jid}


@router.delete("/{project_id}")
def delete_project(request: Request, project_id: int):
    """删除项目：行（jobs/shots/关联/日志/项目）+ 磁盘 projects/<slug>/ 全清；
    在跑渲染发 interrupt；全局资产库（data/library）保留——其他项目可能复用。"""
    db = request.app.state.db
    row = get_project(db, project_id)
    if row is None:
        raise HTTPException(404, "项目不存在")
    # 温和停止（2026-09-05 用户决策 A）：删除项目不再 interrupt ComfyUI
    #（部分版本 interrupt 崩实例）——在跑任务写盘报错自然落 failed
    conn = db.connect()
    try:
        # 删除顺序按外键依赖：logs(job_id→jobs) 先于 jobs；
        # jobs(shot_id→shots) 先于 shots；shots 自引用链按叶子序（见下）
        conn.execute("DELETE FROM logs WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM jobs WHERE project_id=?", (project_id,))
        # 镜间接力链：先删叶子（无人 depends_on 它的镜）再循环——单条 DELETE 会被
        # 自引用 FK 逐行检查卡住（真机 2026-08-25 Internal Server Error）
        for _ in range(1000):
            cur = conn.execute(
                "DELETE FROM shots WHERE project_id=? AND id NOT IN ("
                "SELECT depends_on FROM shots WHERE project_id=? AND depends_on IS NOT NULL)",
                (project_id, project_id))
            if cur.rowcount == 0:
                break
        # 全局资产保留（library 跨项目复用），仅清来源引用
        conn.execute("UPDATE assets SET source_project=NULL WHERE source_project=?",
                     (project_id,))
        for sql in ("DELETE FROM project_assets WHERE project_id=?",
                    "DELETE FROM projects WHERE id=?"):
            conn.execute(sql, (project_id,))
        conn.commit()
    except Exception:
        conn.rollback()  # 失败必须回滚——否则持锁把 worker 线程锁死（真机教训）
        raise
    import shutil
    shutil.rmtree(Path(request.app.state.data_dir) / "projects" / row["slug"],
                  ignore_errors=True)
    # L9（2026-09-05 审计低危）：卡死上报键顺手清（防模块级 dict 泄漏）
    from ..engine.autopilot import _STUCK_REPORTED
    _STUCK_REPORTED.pop(project_id, None)
    return {"deleted": project_id}


def _theme_story_common(db, body) -> tuple:
    """from-theme 预览/创建共用：校验参数、定位主题。返回 (theme, aspect)。"""
    from ..engine.themes import list_themes
    tid = body.get("theme_id")
    theme = next((t for t in list_themes(db) if t["id"] == tid), None)
    if theme is None:
        raise HTTPException(404, f"主题不存在: {tid}")
    aspect = body.get("aspect_ratio") or "9:16"
    from ..engine.projects import ASPECT_RATIOS
    if aspect not in ASPECT_RATIOS:
        raise HTTPException(422, f"aspect_ratio 只能是 {'/'.join(ASPECT_RATIOS)}")
    return theme, aspect


def _generate_story_text(db, theme, body) -> str:
    """按主题生成正文（预览与直建共用）。用户补充描述（extra_prompt）拼进上下文。"""
    protagonist = (body.get("protagonist") or "").strip()
    extra = (body.get("extra_prompt") or "").strip()
    if len(extra) > 2000:
        raise HTTPException(422, f"补充描述过长（{len(extra)} 字，上限 2000）")
    word_count = body.get("word_count")
    if word_count is not None:
        try:
            word_count = int(word_count)
        except (TypeError, ValueError):
            raise HTTPException(422, "word_count 需为整数")
        lo, hi = WORD_COUNT_RANGE
        if not lo <= word_count <= hi:
            raise HTTPException(422, f"word_count 需在 {lo}~{hi} 之间")
    target = DEFAULT_STORY_WORDS if word_count is None else \
        f"约 {word_count} 字（允许上下浮动 20%）"
    system = GEN_STORY_SYSTEM.format(target_words=target)
    user = (f"主题《{theme['name']}》（{theme['category']}）：{theme['description']}")
    if protagonist:
        user += f"\n主角姓名用「{protagonist}」。"
    if extra:
        user += f"\n用户补充要求（务必体现）：{extra}"
    client = client_for_task(db, "gen_story")
    text, _u = client.raw_chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}], temperature=0.7)
    text = (text or "").strip()
    # P7-G 敏感词机械转译（借鉴短剧厂替换库）：修复而非拦截，预览里可见可改
    from ..engine.textfix import apply_sensitive_replacements
    text, n_fixed = apply_sensitive_replacements(text)
    if n_fixed:
        emit_log(db, "llm", "info", f"生成正文敏感词转译 {n_fixed} 处", project_id=None)
    if len(text) < 500:
        raise HTTPException(422, f"生成的正文过短（{len(text)} 字），请重试或换主题")
    return text


@router.post("/from-comic", status_code=201)
def create_from_comic(request: Request,
                      name: str = Form(...), aspect_ratio: str = Form("9:16"),
                      comic_mode: str = Form("motion_comic"),
                      default_shot_duration: float = Form(0.0),
                      target_duration: float = Form(0.0),
                      style: str = Form(""), style_vis: str = Form(""),
                      images: list[UploadFile] = File(...)):
    """P8 漫画导入：每图一镜，直达分镜就绪。comic_mode：
    motion_comic（动态漫/fl2v）| film_adaptation（漫改/ref2va）。
    style/style_vis（2026-09-06）：漫改模式的画风转换目标，随创建提交。"""
    if comic_mode not in ("motion_comic", "film_adaptation"):
        comic_mode = "motion_comic"
    blobs = []
    for f in images:
        data = f.file.read()
        if len(data) > 20 * 1024 * 1024:
            raise HTTPException(422, f"{f.filename} 超过 20MB 上限")
        blobs.append((f.filename or "page.png", data))
    try:
        from ..engine.comic import import_comic
        proj = import_comic(request.app.state.db, request.app.state.data_dir,
                            name, aspect_ratio, blobs, comic_mode=comic_mode,
                            default_shot_duration=default_shot_duration,
                            target_duration=target_duration,
                            style=style, style_vis=style_vis)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return _public(proj)


@router.post("/{project_id}/asr-cleanup", status_code=202)
def asr_cleanup_route(request: Request, project_id: int, body: dict | None = None):
    """P10C 转写校对（2026-09-06 队列化）：入队 gpu_llm_local 与渲染互斥
    （同步 HTTP 时代绕过资源组抢显存）。theme 留空自动读项目持久化主题。"""
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    from ..engine.asr import load_segments
    from ..engine.paths import data_to_abs as _dta
    if not load_segments(request.app.state.data_dir, proj["slug"]):
        raise HTTPException(409, "无转写段落（非音频项目或转写未完成）")
    body = body or {}
    theme = str(body.get("theme") or "").strip()
    if not theme:
        tf = _dta(request.app.state.data_dir,
                  f"projects/{proj['slug']}/audio/theme.txt")
        if tf.exists():
            theme = tf.read_text(encoding="utf-8").strip()
    from ..engine.jobs import enqueue_job
    jid = enqueue_job(db, "asr_cleanup", project_id=project_id,
                      resource="gpu_llm_local",
                      payload={"project_id": project_id, "theme": theme,
                               "mode": str(body.get("mode") or "conservative")})
    return {"job_id": jid}


@router.get("/{project_id}/novel-text")
def novel_text(request: Request, project_id: int):
    """2026-09-05 用户需求：详情页查看正文（上传小说/音频转写通用）。
    from_audio=源音频在盘（转写正文）；文件缺失 404。"""
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    from ..engine.paths import data_to_abs
    f = data_to_abs(request.app.state.data_dir, proj["novel_path"])
    if not f.exists():
        raise HTTPException(404, "正文文件缺失（转写未完成或已删除）")
    text = f.read_text(encoding="utf-8")
    adir = data_to_abs(request.app.state.data_dir,
                       f"projects/{proj['slug']}/audio")
    from_audio = adir.is_dir() and next(adir.glob("source.*"), None) is not None
    return {"text": text, "char_count": len(text), "from_audio": from_audio}


@router.put("/{project_id}/novel-text")
def novel_text_edit(request: Request, project_id: int, body: dict):
    """2026-09-06 用户需求：正文人工校正——LLM 校正外的第二条路，LLM 校正后
    也可继续手改。写回 novel.txt + 重算章节；segments（音频时长锚）不动——
    正文大改后拆镜对不齐的段落回落估时（与 free 模式同语义）。"""
    db = request.app.state.db
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    text = str(body.get("text") or "").strip()
    if not text:
        raise HTTPException(422, "正文不能为空")
    from ..engine.chapters import parse_chapters
    from ..engine.paths import data_to_abs
    f = data_to_abs(request.app.state.data_dir, proj["novel_path"])
    if not f.exists():
        raise HTTPException(404, "正文文件缺失（转写未完成或已删除）")
    f.write_text(text, encoding="utf-8")
    conn = db.connect()
    conn.execute("UPDATE projects SET chapters_json=? WHERE id=?",
                 (json.dumps(parse_chapters(text), ensure_ascii=False), project_id))
    conn.commit()
    emit_log(db, "asr", "info", f"人工校正正文：{len(text)} 字", project_id=project_id)
    # 2026-09-06 有声2 教训第二入口：写回后分镜若已存在即脱节——明示重拆
    n_shots = conn.execute(
        "SELECT COUNT(*) FROM shots WHERE project_id=?", (project_id,)).fetchone()[0]
    if n_shots:
        emit_log(db, "asr", "warn",
                 f"正文已更新，但已有 {n_shots} 镜分镜基于旧文本——"
                 "请重拆分镜后重生成提示词", project_id=project_id)
    return {"char_count": len(text)}


@router.post("/from-audio", status_code=201)
def create_from_audio(request: Request, name: str = Form(...),
                      aspect_ratio: str = Form("9:16"),
                      default_shot_duration: float = Form(0.0),
                      target_duration: float = Form(0.0),
                      audio: UploadFile = File(...)):
    """P10 有声书导入（2026-09-05 计划）：存源音频 → 建项目（占位正文）→
    入队 transcribe；转写完成后回填 novel.txt，之后走现有小说链。"""
    from ..engine.projects import ASPECT_RATIOS
    if aspect_ratio not in ASPECT_RATIOS:
        raise HTTPException(422, f"aspect_ratio 只能是 {'/'.join(ASPECT_RATIOS)}")
    data = audio.file.read()
    if len(data) > 200 * 1024 * 1024:
        raise HTTPException(422, "音频超过 200MB 上限（请先切分）")
    ext = (Path(audio.filename or "a.mp3").suffix.lstrip(".") or "mp3").lower()
    if ext not in ("mp3", "wav", "m4a", "flac", "ogg"):
        raise HTTPException(422, f"不支持的音频格式 .{ext}")
    from ..engine.asr import TranscribeUnavailable  # noqa: 探测依赖
    try:
        from faster_whisper import WhisperModel  # noqa: F401 —— 缺包即 422
    except (ImportError, TranscribeUnavailable) as e:  # 破损安装抛普通 ImportError
        raise HTTPException(422, f"ASR 依赖未安装：{e}；"
                                 "WSL `.venv/bin/pip install -e '.[asr]'` / "
                                 "Windows `.venv-win/Scripts/pip.exe install -e '.[asr]'`")
    proj = create_project(request.app.state.db, request.app.state.data_dir,
                          name, aspect_ratio, "（有声书转写中，转写完成后自动回填正文）",
                          default_shot_duration=default_shot_duration,
                          target_duration=target_duration)
    adir = Path(request.app.state.data_dir) / f"projects/{proj['slug']}/audio"
    adir.mkdir(parents=True, exist_ok=True)
    (adir / f"source.{ext}").write_bytes(data)
    from ..engine.jobs import enqueue_job
    enqueue_job(request.app.state.db, "transcribe", project_id=proj["id"],
                payload={"project_id": proj["id"], "ext": ext})
    return _public(proj)


@router.post("/{project_id}/extract-comic-characters", status_code=202)
def extract_comic_characters_route(project_id: int, request: Request):
    """P8-B 漫改模式：VLM 读前几页提取角色 → 建资产（队列任务）。"""
    db = request.app.state.db
    from ..engine.projects import get_project
    if get_project(db, project_id) is None:
        raise HTTPException(404, "项目不存在")
    from ..engine.jobs import enqueue_job
    jid = enqueue_job(db, "extract_comic_characters", project_id=project_id,
                      resource="gpu_llm_local", payload={"project_id": project_id})
    return {"job_id": jid}


@router.post("/{project_id}/describe-shots", status_code=202)
def describe_shots_route(project_id: int, request: Request,
                         shot_id: int = 0, force: bool = False):
    """P8 VLM 读图生成提示词（队列任务化，2026-08-29 用户需求：
    队列状态可见）。shot_id>0 单镜；否则批量跑全部空提示词镜；
    force=true 批量也覆盖已有提示词（2026-09-06 改画风后整批重读）。"""
    db = request.app.state.db
    from ..engine.projects import get_project
    proj = get_project(db, project_id)
    if proj is None:
        raise HTTPException(404, "项目不存在")
    from ..engine.pipeline_jobs import enqueue_llm_job
    payload = {"project_id": project_id}
    if shot_id:
        payload["shot_id"] = shot_id
    if force:
        payload["force"] = True
    # QC-E：按 routing 定资源（此前硬编码 gpu_llm_local，配 online 时过度互斥）
    jid = enqueue_llm_job(db, "describe_shots", project_id=project_id,
                          shot_id=shot_id if shot_id else None,
                          payload=payload)
    return {"job_id": jid}


@router.post("/from-theme/preview")
def preview_from_theme(request: Request, body: dict):
    """两步创建第一步（2026-08-27 需求）：只生成正文给用户确认/编辑，不建项目。"""
    theme, aspect = _theme_story_common(request.app.state.db, body)
    text = _generate_story_text(request.app.state.db, theme, body)
    return {"text": text, "name": (body.get("name") or theme["name"])}


@router.post("/from-theme", status_code=201)
def create_from_theme(request: Request, body: dict):
    """主题建项目。两步流：body 带 text（用户确认/编辑后的正文）则直接建项目不再调 LLM；
    不带 text 则一步到位生成+创建（兼容旧流程）。"""
    db = request.app.state.db
    data_dir = request.app.state.data_dir
    theme, aspect = _theme_story_common(db, body)
    text = (body.get("text") or "").strip()
    if text:
        if len(text) < 100:
            raise HTTPException(422, f"正文过短（{len(text)} 字，至少 100 字）")
    else:
        text = _generate_story_text(db, theme, body)
    row = create_project(db, data_dir, body.get("name") or theme["name"], aspect,
                         text, style=(body.get("style") or ""),
                         style_vis=(body.get("style_vis") or ""),
                         default_shot_duration=float(body.get("default_shot_duration") or 0.0),
                         target_duration=float(body.get("target_duration") or 0.0))
    return _public(row)


def _public(row) -> dict:
    return {k: row[k] for k in _PUBLIC_COLUMNS}


@router.post("", status_code=201)
def create(request: Request, name: str = Form(...),
           aspect_ratio: str = Form(...), novel: UploadFile = File(...),
           style: str = Form(""), style_vis: str = Form(""),
           video_megapixels: float = Form(0.4),
           video_multiple: int = Form(32), video_speed: str = Form("标准"),
           default_shot_duration: float = Form(0.0),  # 0=LLM 动态估时（2026-09-05 默认）
           prompt_mode: str = Form("D"), lora_realism: float = Form(0.75),
           target_duration: float = Form(0.0)):
    from ..engine.projects import ASPECT_RATIOS
    if aspect_ratio not in ASPECT_RATIOS:
        raise HTTPException(422, f"aspect_ratio 只能是 {'/'.join(ASPECT_RATIOS)}")
    try:
        text = novel.file.read().decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(422, "小说文件需为 UTF-8 编码（请转换后重新上传）")
    row = create_project(request.app.state.db, request.app.state.data_dir,
                         name, aspect_ratio, text, style=style, style_vis=style_vis,
                         video_megapixels=video_megapixels, video_multiple=video_multiple,
                         video_speed=video_speed, default_shot_duration=default_shot_duration,
                         prompt_mode=prompt_mode, lora_realism=lora_realism,
                         target_duration=target_duration)
    return _public(row)


@router.get("")
def listing(request: Request):
    db = request.app.state.db
    data_dir = request.app.state.data_dir
    # 富信息（2026-08-27 需求）：最近活动=各表最大时间；摘要/字数读小说文件（本地小文件，量级无虞）
    last_job = {r["project_id"]: r["m"] for r in db.connect().execute(
        "SELECT project_id, MAX(created_at) m FROM jobs GROUP BY project_id")}
    shot_counts = {r["project_id"]: r["c"] for r in db.connect().execute(
        "SELECT project_id, COUNT(*) c FROM shots GROUP BY project_id")}
    out = []
    for r in list_projects(db):
        item = _public(r)
        item["updated_at"] = last_job.get(r["id"]) or r["created_at"]
        item["shot_count"] = shot_counts.get(r["id"], 0)
        try:
            from ..engine.paths import data_to_abs
            text = data_to_abs(data_dir, r["novel_path"]).read_text(encoding="utf-8")
            item["char_count"] = len(text)
            item["excerpt"] = re.sub(r"\s+", " ", text).strip()[:60]
        except OSError:
            item["char_count"], item["excerpt"] = 0, ""
        if r["autopilot"]:
            from ..engine.autopilot import next_action
            item["autopilot_action"] = next_action(db, data_dir, r["id"])
        out.append(item)
    return out


@router.get("/{project_id}")
def detail(request: Request, project_id: int):
    row = get_project(request.app.state.db, project_id)
    if row is None:
        raise HTTPException(404, "项目不存在")
    out = _public(row)
    if row["autopilot"]:
        from ..engine.autopilot import next_action
        out["autopilot_action"] = next_action(
            request.app.state.db, request.app.state.data_dir, project_id)
    return out


@router.patch("/{project_id}")
def patch_style(request: Request, project_id: int, body: dict):
    from pydantic import BaseModel

    class StylePatch(BaseModel):
        style: str = ""
        style_vis: str = ""

    db = request.app.state.db
    row = get_project(db, project_id)
    if row is None:
        raise HTTPException(404, "项目不存在")

    # Handle style parameter
    if "style" in body or "style_vis" in body:
        patch = StylePatch.model_validate(body)
        conn = db.connect()
        if "style" in body:
            conn.execute("UPDATE projects SET style=? WHERE id=?", (patch.style.strip(), project_id))
        if "style_vis" in body:
            conn.execute("UPDATE projects SET style_vis=? WHERE id=?",
                         (patch.style_vis.strip(), project_id))
        conn.commit()

    # Handle autopilot switch (一键出片)
    if "autopilot" in body:
        on = 1 if body["autopilot"] else 0
        conn = db.connect()
        conn.execute("UPDATE projects SET autopilot=? WHERE id=?", (on, project_id))
        conn.commit()
        if row["autopilot"] == 1 and on == 0:
            # 2026-09-04 设计A1 暂停联动全停：关开关同时取消 pending/打断 running——
            # 排队的 gen_prompt 不再跑完覆盖用户正要改的提示词（列表/详情/底栏共用此 PATCH）
            from ..engine.jobs import cancel_project_jobs
            from ..engine.logbus import emit as emit_log
            result = cancel_project_jobs(db, project_id)
            emit_log(db, "autopilot", "info",
                     f"停止自动：取消 {result['cancelled']} 个排队任务、"
                     f"停止 {result['stopping']} 个运行中任务", project_id=project_id)

    # Handle render_mode (视频渲染模式项目级切换 → 批量改全部镜 workflow_type；
    # 类型→具体模板由 settings 页 template_map 决定，两层协同 2026-08-26)
    if "render_mode" in body:
        mode = body["render_mode"]
        if mode not in ("ref2va", "fl2v", "t2v"):
            raise HTTPException(422, "render_mode 只能是 ref2va/fl2v/t2v")
        conn = db.connect()
        conn.execute("UPDATE shots SET workflow_type=? WHERE project_id=?",
                     (mode, project_id))
        conn.commit()

    # Handle era override (时代背景；检测错了可手动纠正，空串=清除)
    if "era" in body:
        conn = db.connect()
        conn.execute("UPDATE projects SET era=? WHERE id=?",
                     (str(body["era"] or "").strip(), project_id))
        conn.commit()

    # Handle video parameters (composable with style)
    if any(k in body for k in ("video_megapixels", "video_multiple", "video_speed", "default_shot_duration", "prompt_mode", "lora_realism", "target_duration", "aspect_ratio")):
        try:
            from ..engine.projects import update_video_params
            kwargs = {}
            if "aspect_ratio" in body:
                kwargs["aspect_ratio"] = body["aspect_ratio"]
            if "video_megapixels" in body:
                kwargs["video_megapixels"] = body["video_megapixels"]
            if "video_multiple" in body:
                kwargs["video_multiple"] = body["video_multiple"]
            if "video_speed" in body:
                kwargs["video_speed"] = body["video_speed"]
            if "default_shot_duration" in body:
                kwargs["default_shot_duration"] = body["default_shot_duration"]
            if "prompt_mode" in body:
                kwargs["prompt_mode"] = body["prompt_mode"]
            if "lora_realism" in body:
                kwargs["lora_realism"] = body["lora_realism"]
            if "target_duration" in body:
                kwargs["target_duration"] = body["target_duration"]
            row = update_video_params(db, project_id, **kwargs)
        except ValueError as e:
            raise HTTPException(422, str(e))

    return _public(get_project(db, project_id))
