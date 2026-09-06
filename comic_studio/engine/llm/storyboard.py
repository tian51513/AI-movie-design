# comic_studio/engine/llm/storyboard.py
"""分镜拆解：schema、提示词、编排（spec §9.2，台账/绑定/workflow_type 建议）。"""
import json
import re
import time
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field, field_validator
from types import SimpleNamespace

SPLIT_SYSTEM = """你是小说改编漫剧的分镜师。把给定的小说文本拆成连续的分镜（shot）序列，供后续 AI 视频生成使用。

规则：
1. 每个分镜 = 一个可独立生成的视频镜头（通常 4~8 秒）；按剧情顺序，覆盖全部情节，不跳戏不脑补
2. 白名单纪律：只使用名册中列出的资产 id 绑定角色/场景/道具
   - 泛称（众人/群臣/学生们等群体称谓）必须映射为白名单内的具体角色；映射不了就按无名背景处理，不绑定
   - 白名单外的具名角色一律改写为背景人物：不进 character_ids、不进 must_appear，description 里以剪影/背景群像带过
   - 新出现的无名路人不绑定
3. camera 用中文枚举：景别(远景/全景/中景/近景/特写)、机位(平视/仰视/俯视/过肩)、运镜(固定/推/拉/摇/移/跟)、转场(切/叠化/无)。景别节奏：近景/特写用于情绪点、中景承担叙事，**每 3~5 镜至少安排 1 个全景或远景环境镜**（给画面呼吸与空间感，人物缩小可辨即可，2026-08-28 真机教训：全部中近特导致人物撑满画面）；远景/大全景镜必须在台账 must_keep 注明保持人物发型与服装轮廓特征
4. workflow_type：与上一镜衔接（同场景连续动作）→ "fl2v"；常规（参考角色/场景出图）→ "ref2va"；建立全新画面且无参考 → "t2v"
5. continue_prev：本镜是否紧接上一镜延续（同场景、动作连贯）——分块拆解时首镜若延续上一块结尾则 true
6. 台账四分类：must_appear(画面必须出现的实体/动作)、must_keep(必须保持的资产特征)、may_change(允许自由发挥)、must_avoid(易错必须避免项，如"左右手颠倒""换服装"）
7. description 写成可直接指导视频生成的画面描述：谁在哪做什么、构图与光线，80 字内中文；
   ① 严禁「他/她/它」等代词——一律写角色名（多角色绑定的关键）；
   ② 只写镜头拍得到的内容：生理可观测动作与微表情（咬紧牙关/指节泛白/瞳孔骤缩），
   禁止「他很愤怒」等抽象心理词（借鉴 XiaoLuo/短剧厂规范 2026-08-28）
8. duration：有对白的镜按「对白总字数 ÷ 4 秒 + 每多一句加 0.6 秒停顿」估时长
   （中文配音实际语速约 4 字/秒——句数×2.5 无视句长，2026-09-05 真机长句对白被截半）；
   无对白镜按动作复杂度估：简单反应/走位=项目统一段时长（上下文给出）、
   连续打斗/多阶段动作 8~12 秒、全景/远景环境交代 6~8 秒（剧情要演完再切镜，
   禁止动作没做完就到时长）；一律在 4~15 秒区间内
9. dialogue：从 text_span 照录本镜人物对白，格式 [{"speaker":"说话人","line":"原话"}]；
   逐字保留原文（含语气词），不改写不概括；无对白则省略或空数组
10. 台词组打包（2026-09-01，反「一句台词一镜」）：连续同场景、同批人物的对白
    （3~8 句）合并为**一个**分镜——镜内 dialogue 依次照录多句，画面仅微动作/视线/
    说话人焦点变化，不切镜；只有 场景更换 / 时间跳转 / 剧情重大转折 才切新镜；
    打包预算：打包后对白总字数 ≤ 48 字（估时约 12 秒封口），超出开新镜
11. 结构化情绪字段（驱动视频动态，杜绝人物僵硬）：
    - emotion：本镜主导情绪，仅从枚举选：平静/温柔/开心/轻笑/严肃/愤怒/激动/委屈/悲伤/冷漠/惊讶/紧张/低语/嘶吼/淡然
    - gesture：主导微动作，**英文短句**（如 "slightly raising one hand"，供英文提示词直用），轻微不夸张
    - gaze：主要视线方向，**英文短句**（如 "looking at the speaker"）
    - continuity：与上一镜的延续关系，仅从枚举选：全程继承/微变延续/焦点跟随/缓慢推镜/缓慢拉镜/场景断点

只输出一个 JSON 对象：
{"shots":[{"text_span":"对应原文摘录","description":"...","shot_type":"对话/动作/场景/情绪",
 "camera":{"景别":"中景","机位":"平视","运镜":"固定","转场":"切"},
 "duration":5,"workflow_type":"ref2va",
 "must_appear":["萧炎"],"must_keep":["萧炎的黑发"],"may_change":["镜头角度"],"must_avoid":["服装变化"],
 "character_ids":[1],"scene_ids":[2],"prop_ids":[],"continue_prev":false,
 "emotion":"平静","gesture":"slightly raising one hand","gaze":"looking at the speaker",
 "continuity":"微变延续"}]}"""


class ShotDraft(BaseModel):
    # text_span 必填非空（2026-09-02 连夜两模型实证：ornith/nsfwvision 都不可靠
    # 填写 → backfill_dialogue 无米下锅 → 零对白 → 时长全落默认 5s；缺失交给
    # ask_validated 校验反馈重试兜住，不赌模型自觉）
    text_span: str = Field(min_length=1)
    description: str = Field(min_length=1)
    shot_type: str = ""
    camera: dict = Field(default_factory=dict)
    # 宽进严出（2026-09-01 台词组拆镜）：LLM 估时长允许越界，staging 机械钳 [4,15]
    duration: float = Field(ge=1, le=30, default=5)
    workflow_type: str = "ref2va"
    must_appear: list[str] = []
    must_keep: list[str] = []
    may_change: list[str] = []
    must_avoid: list[str] = []
    character_ids: list[int] = []
    scene_ids: list[int] = []
    prop_ids: list[int] = []
    continue_prev: bool = False
    # 台词链路（2026-08-26）：原文照录，供视频语音/TTS 逐字使用
    dialogue: list[dict[str, str]] = []
    # 台词组拆镜 A2（2026-09-01）：结构化情绪/微动作/视线/延续——枚举外值 staging 清空
    emotion: str = ""
    gesture: str = ""
    gaze: str = ""
    continuity: str = ""


# 枚举契约（SPLIT_SYSTEM 规则 11 同源；LLM 输出外值 → 机械清空不重试）
EMOTIONS = ("平静", "温柔", "开心", "轻笑", "严肃", "愤怒", "激动", "委屈",
            "悲伤", "冷漠", "惊讶", "紧张", "低语", "嘶吼", "淡然")
CONTINUITY_MODES = ("全程继承", "微变延续", "焦点跟随", "缓慢推镜", "缓慢拉镜", "场景断点")


def _random_seed() -> int:
    import random
    return random.randint(0, 2 ** 31 - 1)


class ChunkStoryboard(BaseModel):
    shots: list[ShotDraft] = Field(min_length=1)

    @field_validator("shots")
    @classmethod
    def _nonempty(cls, v):
        if not v:
            raise ValueError("分镜序列不能为空")
        return v


def build_split_user_prompt(chunk_text: str, assets_rows, quota_line: str | None = None,
                            dur_hint: float | None = None) -> str:
    roster = {"character": [], "scene": [], "prop": []}
    for r in assets_rows:
        appearance_json = r["appearance_json"] if hasattr(r, '__getitem__') else r.appearance_json
        detail = json.loads(appearance_json).get("detail", "")[:30]
        kind = r["kind"] if hasattr(r, '__getitem__') else r.kind
        rid = r["id"] if hasattr(r, '__getitem__') else r.id
        name = r["name"] if hasattr(r, '__getitem__') else r.name
        roster[kind].append(f"id={rid} {name}（{detail}）")
    lines = []
    if quota_line:
        lines.append(quota_line)
        lines.append("")
    if dur_hint is not None:
        # 2026-09-05：段时长基准进上下文（此前规则 8 声称「上下文给出」实际没给）；
        # 0=LLM 动态估时（无对白镜也按动作复杂度自估）
        if dur_hint > 0:
            lines.append(f"【时长基准】项目统一段时长 {dur_hint:g} 秒——"
                         f"无对白镜的简单反应/走位一律用 {dur_hint:g} 秒，复杂动作按规则 8 上调")
        else:
            lines.append("【时长基准】本项目无统一段时长（0）——所有镜 duration 由你按规则 8 逐镜估"
                         "（对白按字数÷4，无对白按动作复杂度），不要一律填 5")
        lines.append("")
    lines.append("可用资产白名单（只允许绑定以下 id；名单外一律转无名背景）：")
    for kind, label in (("character", "角色"), ("scene", "场景"), ("prop", "道具")):
        if roster[kind]:
            lines.append(f"{label}：" + "；".join(roster[kind]))
    lines.append("")
    lines.append("小说文本：")
    lines.append(chunk_text)
    return "\n".join(lines)

from ..assets import list_project_assets
from ..logbus import emit as emit_log
from ..projects import get_project
from ..settings import get_setting
from ..shots import persist_shots
from .provider import ask_validated, client_for_task
from .text import split_chunks

class ContentBoundaryError(Exception):
    """输入或生成内容命中未成年性内容硬界线（项目级，跳过并显式报错）。"""

_MINOR_SEXUAL = re.compile(r"(萝莉|幼女|女童|男童).{0,12}(性|色情|裸|吻|床|情欲)|(性|色情|裸|情欲).{0,12}(萝莉|幼女|女童)|校服.{0,8}(情欲|性爱|裸)")


def _content_guard(text: str) -> None:
    if _MINOR_SEXUAL.search(text):
        raise ContentBoundaryError("内容命中项目硬界线（涉及未成年人的性内容），该段已跳过并停止处理")


ClientFactory = Callable[[str], object]


def make_split_factory(db):
    from .analyze import make_client_factory
    return make_client_factory(db)




_QUOTE_RE = re.compile(r'[“「"]([^”」"]{2,})[”」"]')
# 说话动词/提示：紧邻引号前出现=前方名字是说话人（后方窗口不再争抢）
_SPEECH_CUE_RE = re.compile(r"说道|笑道|喊道|问道|骂道|冷笑|开口|回道|低语|说：|道：|问：")


def backfill_dialogue(staged: list, character_names: list[str]) -> int:
    """对白机械兜底（2026-08-27 真机：nsfwvision-v3 等 RP 模型把对白写进描述正文、
    不填 schema 的 dialogue 字段 → TTS/字幕链路全空）。从 text_span 提取引号句
    （弯引号 “”、直引号 "、方引号 「」；内层单引号 ‘’ 不取），说话人取引号
    前后窗口内最近出现的角色名（前优先），找不到用首个角色或"旁白"。
    交替惯例（2026-09-03 武侠风云真机：女主无提示回怼被就近规则派给男主）：
    相邻引号之间的文本若没有任何角色名/说话动词提示，按中文对白惯例轮换到
    另一位（本 span 出现过的、≠上一句说话人的名字）。
    LLM 已给出 dialogue 的镜不动。返回补录句数。"""
    names = [n for n in character_names if n]
    n_filled = 0
    for s in staged:
        ledger = getattr(s, "ledger", None)
        if not isinstance(ledger, dict) or ledger.get("dialogue"):
            continue
        span = getattr(s, "text_span", "") or ""
        entries = []
        prev_speaker, prev_end = None, -1
        for m in _QUOTE_RE.finditer(span):
            before, after = span[:m.start()], span[m.end():m.end() + 30]
            between = span[prev_end:m.start()] if prev_end >= 0 else ""
            # 双向就近：中文小说对白两种形态都常见——“某人曰：“…”（名在前）与
            # “…”某人动作（名在后），谁离引号近归谁
            speaker, dist = None, None
            for name in names:
                i = before.rfind(name)
                if i != -1:
                    d0 = len(before) - i - len(name)
                    if dist is None or d0 < dist:
                        dist, speaker = d0, name
            # 引号紧前有说话动词（某某说道：）→ 前方名字即说话人；
            # 否则双向就近（“…”某人动作 的名在后形态）
            if not _SPEECH_CUE_RE.search(before[-10:]):
                for name in names:
                    j = after.find(name)
                    if j != -1 and (dist is None or j < dist):
                        dist, speaker = j, name
            # 交替惯例：无提示（speaker 空）或最近的候选=上一句说话人且
            # 两句之间没有任何名字/说话动词 → 轮换另一位（span 内出现过的）
            span_names = [n for n in names if n in span]
            if prev_speaker and ((speaker is None) or (speaker == prev_speaker
                                                       and not _SPEECH_CUE_RE.search(between)
                                                       and not any(n in between for n in names))):
                alt = next((n for n in span_names if n != prev_speaker), None)
                if alt:
                    speaker = alt
            entries.append({"speaker": speaker or (names[0] if names else "旁白"),
                            "line": m.group(1).strip()})
            prev_speaker, prev_end = entries[-1]["speaker"], m.end()
        if entries:
            ledger["dialogue"] = entries
            s.dialogue_backfilled = True  # 重估时长的作用域标记
            n_filled += len(entries)
    return n_filled


def dialogue_duration_seconds(dlg: list) -> float:
    """对白→时长字数基准（B1）：⌈总字数/4⌉ + 0.6×(句数-1)，钳 [4,15]
    （中文 TTS 语速约 4 字/s + 句间停顿）。小说拆解 staging 与漫画读图
    （describe_shots 智能估时 2026-09-06）共用同一基准。"""
    import math
    chars = sum(len((d.get("line") or "").strip()) for d in dlg)
    return min(15.0, max(4.0, math.ceil(chars / 4.0) + 0.6 * (len(dlg) - 1)))


def reestimate_durations(staged: list) -> int:
    """对白镜时长机械重估（2026-09-03 武侠风云真机：42 镜对白 24 句仍全 5s——
    句数×2.5 的估时交给 LLM，而本地模型填不出对白、一律写 5；对白是 backfill
    机械补的，补完没人回头改时长）。2026-09-05 B1 改字数基准：句数×2.5 无视
    句长（20 字长句与 5 字短句同估 2.5s，真机对白被截半）——按
    ⌈总字数/4⌉ + 0.6×(句数-1)（中文 TTS 语速约 4 字/s + 句间停顿）确定性重算，
    钳 [4,15]；无对白镜不动（维持项目统一段时长）。返回重估镜数。"""
    n = 0
    for s in staged:
        if not getattr(s, "dialogue_backfilled", False):
            continue  # LLM 自填对白的镜保留其估时（staging 已钳），只重算补录镜
        dlg = (getattr(s, "ledger", None) or {}).get("dialogue") or []
        if dlg:
            s.duration = dialogue_duration_seconds(dlg)
            n += 1
    return n


def auto_bind_characters(db, project_id):
    """角色自动补绑（2026-08-26 真机教训：LLM 拆解漏绑角色 → 关键帧无参考图）。
    扫描每镜描述文本，提到的项目角色自动补绑到 ledger.assets.characters。"""
    from ..assets import list_project_assets
    from ..shots import list_shots
    chars = [(a['id'], a['name']) for a in list_project_assets(db, project_id)
             if a['kind'] == 'character']
    if not chars:
        return 0
    conn = db.connect()
    bound = 0
    for shot in list_shots(db, project_id):
        ledger = json.loads(shot['ledger_json'] or '{}')
        assets = ledger.setdefault('assets', {})
        cur_ids = set(assets.get('characters') or [])
        desc = shot['description'] or ''
        for cid, cname in chars:
            if cid in cur_ids:
                continue
            if cname in desc:
                cur_ids.add(cid)
                bound += 1
        if cur_ids != set(assets.get('characters') or []):
            assets['characters'] = sorted(cur_ids)
            conn.execute('UPDATE shots SET ledger_json=? WHERE id=?',
                         (json.dumps(ledger, ensure_ascii=False), shot['id']))
    conn.commit()
    return bound


def split_storyboards(db, data_dir, project_id, client_factory=None, max_chars=1300,
                      target_count=None, chapter_range=None):
    """max_chars=1300：按上下文容量实证取值（2026-08-27 job 582：8127 字块撞
    16384 num_ctx 硬截断；2026-09-03 job 38410：text_span 必填后输出密度上涨，
    1956 字块 >7.07 completion tok/字仍截断——span 时代密度上限按 11.5 保守取，
    + prompt 0.82 tok/字 + ~350 开销 → 块 ≤ ~1300 字）。
    target_count：指定全文分镜数（2026-08-27 需求）——按各块字数占比分配配额注入提示词；
    None=自动拆分（现状）。"""
    if client_factory is None:
        client_factory = make_split_factory(db)
    proj = get_project(db, project_id)
    if proj is None:
        raise ValueError(f"项目不存在: {project_id}")
    from ..paths import data_to_abs
    text = data_to_abs(data_dir, proj["novel_path"]).read_text(encoding="utf-8")
    # P7-E 按章范围拆分：只吃选定章的文本（无章节结构/无范围 → 全文）
    from ..chapters import parse_chapters, slice_chapters
    if chapter_range:
        text = slice_chapters(text, parse_chapters(text), tuple(chapter_range))
    # _content_guard(text)
    chunks = split_chunks(text, max_chars=max_chars)
    # 配额分配：按字数占比 round，最后一块兜底补齐/削减到总数
    quotas = None
    if target_count:
        total_chars = sum(len(c) for c in chunks) or 1
        quotas = [max(1, round(target_count * len(c) / total_chars)) for c in chunks]
        quotas[-1] = max(1, target_count - sum(quotas[:-1]))
    assets = list_project_assets(db, project_id)
    emit_log(db, "storyboard", "info",
             f"开始分镜拆解：{len(chunks)} 块（共 {len(text)} 字，{len(assets)} 个资产入名册）"
             + (f"，目标 {target_count} 镜（配额 {quotas}）" if quotas else ""),
             project_id=project_id)
    client = client_factory("split_storyboards")
    provider = get_setting(db, "llm_routing")["split_storyboards"]
    # 段时长基准（2026-09-05）：进拆解上下文；0=LLM 动态估时（无对白镜也自估）
    _dur = float(proj["default_shot_duration"] or 0.0)
    # 有声书对白规则（2026-09-05 真机：转写无引号 → LLM 不识台词 → dialogue
    # 空 → 提示词无对白）：音频项目注入「全篇皆对白」规则+说话人推断+主题
    _audio_rules = ""
    from ..asr import load_segments
    _segs_for_rules = load_segments(data_dir, proj["slug"])
    if _segs_for_rules:
        _theme = ""
        _tfile = Path(data_dir) / f"projects/{proj['slug']}/audio" / "theme.txt"
        if _tfile.exists():
            _theme = _tfile.read_text(encoding="utf-8").strip()
        _audio_rules = (

            "\n\n【有声书对白规则——本文来自语音转写，全篇皆对白】\n"
            "- text_span 里的每一句话都是人物台词：必须把本镜全部语句逐字录入 "
            "dialogue 字段（每条含 speaker 与 line），没有引号也要照录\n"
            "- 说话人按语境推断：自称（妈妈/老师）或称呼对方（儿子/宝贝）者即该角色；"
            "无法判断时按上下文交替惯例\n"
            "- 台词是这类项目的核心内容，漏录=成片无对白无口型"
            + (f"\n- 内容主题（说话人推断参考）：{_theme}" if _theme else ""))
    staged, link_first_of_block = [], []   # link_first_of_block[i] = i 块首镜在 staged 中的下标（需链上一块末镜）
    for i, chunk in enumerate(chunks, 1):
        emit_log(db, "storyboard", "info", f"分块 {i}/{len(chunks)} 拆解中（{len(chunk)} 字）",
                 project_id=project_id)
        t0 = time.monotonic()
        quota_line = (f"【数量约束】全文目标 {target_count} 个分镜，本块目标拆出约 {quotas[i-1]} 个分镜"
                      f"（按篇幅分配，允许 ±1 浮动）") if quotas else None
        result, usage = ask_validated(client, SPLIT_SYSTEM + _audio_rules,
                                      build_split_user_prompt(chunk, assets, quota_line,
                                                              dur_hint=_dur),
                                      ChunkStoryboard)
        emit_log(db, "llm", "info",
                 f"split_storyboards 完成 · {getattr(client, 'model', '?')} · "
                 f"{usage.prompt_tokens}+{usage.completion_tokens} tok · {time.monotonic()-t0:.1f}s · "
                 f"{len(result.shots)} 镜", project_id=project_id)
        # for d in result.shots:
        #     _content_guard(d.description + " " + d.text_span)
        if result.shots[0].continue_prev and staged:
            link_first_of_block.append(len(staged))
        # B 级（2026-09-01）：延续状态在 staging 逐镜累积——组 seed 继承与
        # workflow 机械校准都要看「上一镜的 continuity」
        _prev_con = ""
        _prev_seed = None
        for d in result.shots:
            con = d.continuity if d.continuity in CONTINUITY_MODES else ""
            wf = d.workflow_type
            # B4：本镜延续上一镜（继承/微变）而 LLM 给了 ref2va → 机械校准 fl2v
            # （首尾帧接力链连贯性最好）；t2v 是 LLM 的「全新画面」判断，永不覆盖
            if con in ("全程继承", "微变延续") and wf == "ref2va":
                wf = "fl2v"
            # B5：延续组内 seed 继承 +3/镜（同场景画风漂移防治）；断点/空→组首随机
            if con and con != "场景断点" and _prev_seed is not None:
                seed = _prev_seed + 3
            else:
                seed = _random_seed()
            _prev_con, _prev_seed = con, seed
            staged.append(SimpleNamespace(
                text_span=d.text_span, description=d.description, shot_type=d.shot_type,
                camera=d.camera,
                # 台词组拆镜 A1（2026-09-01）：有对白的镜用 LLM 估时长（机械钳 4~15，
                # 多句台词要说完）；无对白镜维持项目统一段时长——
                # 2026-09-05 段时长 0=动态：无对白镜也用 LLM 估时（钳 4~15）
                duration=(min(15.0, max(4.0, float(d.duration)))
                          if ((d.dialogue or []) or _dur <= 0) else _dur),
                workflow_type=wf,
                emotion=d.emotion if d.emotion in EMOTIONS else "",
                gesture=d.gesture.strip(),
                gaze=d.gaze.strip(),
                continuity=con,
                seed=seed,
                ledger={"must_appear": d.must_appear, "must_keep": d.must_keep,
                        "may_change": d.may_change, "must_avoid": d.must_avoid,
                        "dialogue": d.dialogue},
                character_ids=d.character_ids, scene_ids=d.scene_ids,
                prop_ids=d.prop_ids,
                depends_on=None))
    # 对白机械兜底（2026-08-27 真机：nsfwvision-v3 等 RP 模型把对白写进描述正文、
    # 不填 schema 的 dialogue 字段 → TTS/字幕链路全空）：从 text_span 引号原文提取，
    # 说话人取引号前后最近角色名；LLM 已填的镜不动
    char_names = [r["name"] for r in assets if r["kind"] == "character"]
    n_dlg = backfill_dialogue(staged, char_names)
    if n_dlg:
        emit_log(db, "storyboard", "info", f"对白机械补录：{n_dlg} 句（模型未提取，从原文引号恢复）",
                 project_id=project_id)
    # 时长机械重估（2026-09-03 真机教训：LLM 没见过对白就把时长写死 5，
    # 补录后必须按文档公式句数×2.5 钳 4~15 回算，对白镜时长不再均分）
    n_dur = reestimate_durations(staged)
    if n_dur:
        emit_log(db, "storyboard", "info",
                 f"对白镜时长重估：{n_dur} 镜（句数×2.5s，钳 4~15）", project_id=project_id)
    # P10A（2026-09-05）：音频实测时长为最终权威——段文件存在时覆盖估时
    from ..asr import load_segments, span_duration_for  # 惰性，避免无 P10 项目时的开销
    _segs = load_segments(data_dir, proj["slug"])
    if _segs:
        for s in staged:
            d = span_duration_for(_segs, s.text_span)
            if d:
                s.duration = min(15.0, max(4.0, d))
    ids = persist_shots(db, project_id, staged)
    conn = db.connect()
    # 尾帧接力链（连贯性① 2026-08-26）：全顺序镜自动链接（含跨块衔接——
    # 此前仅 continue_prev 标记的块边界链接，绝大多数镜 depends_on 为空，
    # 渲染时首帧接力从未触发，镜间不连贯的根因之一）
    for prev, cur in zip(ids, ids[1:]):
        conn.execute("UPDATE shots SET depends_on=? WHERE id=?", (prev, cur))
    # 创建时设了预设总时长 → 拆完按镜数均摊（下限4s，连贯性/时长需求 2026-08-26）；
    # P10A 终审 I-2（2026-09-05）：音频项目（segments 存在）实测时长为最终权威，
    # 均摊不生效——否则上面逐镜音频覆盖会被这里的全表 UPDATE 整批抹平回均摊值
    _proj = conn.execute("SELECT target_duration, default_shot_duration FROM projects "
                         "WHERE id=?", (project_id,)).fetchone()
    if (_proj and _proj["target_duration"] and _proj["target_duration"] > 0
            and not _segs):
        per = max(4, round(_proj["target_duration"] / max(1, len(ids))))
        conn.execute("UPDATE shots SET duration=? WHERE project_id=?", (per, project_id))
        conn.execute("UPDATE projects SET default_shot_duration=? WHERE id=?",
                     (per, project_id))
    conn.commit()
    # 角色自动补绑（真机 2026-08-26：LLM 漏绑 → 关键帧/参考图无角色锚定）
    n_bound = auto_bind_characters(db, project_id)
    if n_bound:
        emit_log(db, "storyboard", "info", f"角色自动补绑：{n_bound} 处",
                 project_id=project_id)
    emit_log(db, "storyboard", "info", f"分镜落库 {len(ids)} 镜（已替换旧分镜）",
             project_id=project_id)
    # P7-G 拆解后机械审计（时长守恒/换挡/台词字数）：只告警不拦截
    from ..storyboard_checks import audit_storyboard
    for w in audit_storyboard(db, project_id):
        emit_log(db, "storyboard", "warn", w, project_id=project_id)
    return ids
