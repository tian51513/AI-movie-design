# comic_studio/engine/llm/analyze.py
"""分析编排：分块 → 抽取 → 合并 → 入库（spec §5 created→analyzed）。"""
import json
import time
from pathlib import Path
from typing import Callable

from ..assets import list_project_assets, persist_assets
from ..db import Database
from ..logbus import emit as emit_log
from ..paths import data_to_abs
from ..projects import get_project, set_stage
from ..settings import get_setting
from .provider import LLMClient, Usage, ask_validated, client_for_task, log_llm_call
from .schemas import AssetsAnalysis
from .text import split_chunks

EXTRACT_SYSTEM = """你是小说改编漫剧的资产分析师。从给定的小说文本中提取：
1. 出场角色（characters）：name（原文姓名）、role（主角/配角/路人，默认配角）、
   appearance（外貌固化描述，必须严格按以下行模板逐行输出，标签逐字使用，
   值为一句话；未成年角色只写中性描述，不写身体曲线/肌肤细节）：
性别：（女/男）
年龄：（如 24岁）
发色发型：
瞳色：
肤色：
体型：
服装：
配饰：（无则写 无）
   原文信息不足时按合理默认补全并保持一致；不含性格心理。模板示例：
性别：女
年龄：24岁
发色发型：黑色长直发，垂落及腰
瞳色：深褐色
肤色：白皙
体型：高挑匀称
服装：米白色高领毛衣，深灰色长裤
配饰：细银链项链
   、
   tags（如 ["主角"]）
2. 必要场景（scenes）：name、description（环境、光线、时代风格、氛围）
3. 关键道具（props）：name、description（外观、材质、尺寸、时代与文化风格——
   如肚兜/罗裳等须写明"中式古风"及形制细节，避免生成模型误读为现代物品）
只提取对画面呈现有意义的条目；路人一般不建角色。
绝不建为角色（R6 2026-09-02）：旁白/画外音/内心独白等无姓名的叙述声音、
群体称谓（众人/大家/村民们/士兵们等一切「…们」）、仅被对话提及而从未实际
出场者、抽象概念或物种统称。角色必须是原文中实际出场、姓名在原文中出现
过的个体（系统会机械校验：名字不在原文的角色将被丢弃）。
对白体/有声书文本例外（P10 2026-09-05 真机：转写文本无第三人称叙述层，
第一人称说话人被「仅被提及不出场」规则误杀）：有声小说多为对白体或
第一/第三人称叙述混合——**有实际台词的第一人称说话人就是出场角色**：
自称（妈妈/老师/朕/老夫）与他人称呼（儿子/宝贝）在文中反复出现即可
作为 name 提取，不要求书面姓名；判据=该说话人有台词或有叙述动作，
而不是「有第三人称出场描写」。
同一人的不同称谓（2026-09-06 真机：少芬妈妈/少芬阿姨/少芬姐 实为一人被建成
重复资产）必须**合并为一个角色**：name 取原文中出现次数最多的称呼，
appearance 取信息最丰富的描述；判据=共享同一专名（少芬）+ 不同身份称谓后缀。
每个角色另给 suggested_voice：按年龄/性别/气质/着装从随提示附上的「可用音色库」
清单中选最贴切的一个（含用户自定义音色；清单外值会被忽略，走性别×年龄基线：
女童→萝莉 男童→正太 青年女→温柔少女 青年男→深沉男声 中年女→温柔淑女
中年男→大叔 老年女→老年女声 老年男→老年男声）。
若清单中没有贴合该角色的音色：suggested_voice 留空，改填 voice_description——
一句可据此合成该声线的描述（如"低沉沙哑的中年男声，语速慢，语气威严"）。
voice_description 只给**有台词的说话角色**；不出声的角色一律不填。

只输出一个 JSON 对象：{"characters":[{"name","role","appearance","tags","suggested_voice","voice_description"}],
"scenes":[{"name","description","tags"}],"props":[{"name","description","tags"}]}"""

MERGE_SYSTEM = """合并多段小说文本的资产分析结果。规则：
- 同名（或明显同一人的别名，如"萧炎/炎少爷"）合并为一条，appearance 取信息最丰富的描述并可融合细节；
- 同一场景不同叫法合并；tags 取并集；
- 合并即精选：条目很多时优先保留主角与重要配角，appearance 不扩写；
  只在末尾露过一面的路人角色、无关紧要的场景/道具可以丢弃——
  宁可少而精，不可为了保全面输出超长清单。
- 角色的 suggested_voice / voice_description 必须保留（取非空者；都空则留空）——
  这是后续配音绑定的依据，丢了会导致角色无声线。
输出与输入相同结构的 JSON：{"characters":[...],"scenes":[...],"props":[...]}，
其中每条角色含 name/role/appearance/tags/suggested_voice/voice_description，
场景与道具含 name/description/tags。"""

# length 截断的极限压缩版（2026-09-13 防御层①）：输入已超墙时保树收敛
_MERGE_COMPACT_SYSTEM = """合并多段小说文本的资产分析结果（极限压缩模式——上次输出被长度上限截断）。
- 同名/别名合并为一条；
- 只保留主角与重要配角（role=主角/配角），appearance 压缩到 40 字以内；
- 场景只保留反复出现的主要地点（至多 5 个），道具只保留剧情关键物（至多 5 个）；
- 路人角色与一次性场景一律丢弃。
输出与输入相同结构的 JSON：{"characters":[...],"scenes":[...],"props":[...]}。"""

ClientFactory = Callable[[str], LLMClient]


def make_client_factory(db: Database) -> ClientFactory:
    """默认工厂：闭包持有 db，按任务名路由（spec §9.1）。
    独立成函数是为了让测试 monkeypatch analyze.client_for_task 能生效——
    默认参数在定义时绑定，模块属性查找在调用时发生。"""
    return lambda task: client_for_task(db, task)


def _results_payload(results: list[AssetsAnalysis]) -> str:
    # exclude_defaults：空音色字段（suggested_voice/voice_description=""）零信息，
    # 序列化进合并载荷每个角色白胖 ~44 字（2026-09-02 实测把 12 角色的合并树
    # 挤过 max_payload_chars）；tags:[] 同理剔除，解析侧默认值自动补回。
    return json.dumps(
        {"characters": [c.model_dump(exclude_defaults=True)
                        for r in results for c in r.characters],
         "scenes": [s.model_dump(exclude_defaults=True)
                    for r in results for s in r.scenes],
         "props": [p.model_dump(exclude_defaults=True)
                   for r in results for p in r.props]},
        ensure_ascii=False)


def _cap_analysis(result: AssetsAnalysis, budget_chars: int) -> AssetsAnalysis:
    """机械量控（2026-09-13 玉麟传奇 62 块真机）：按 主角>配角>路人 与
    角色>场景>道具 的优先级逐条累加裸字符到预算，超出丢弃——LLM 不守输出
    预算时的硬护栏，保证合并树每层减半、下层贪心永远装得下两份。"""
    def role_w(c):
        return {"主角": 0, "配角": 1}.get(c.role, 2)
    ordered = (sorted(result.characters, key=role_w)
               + list(result.scenes) + list(result.props))
    kept, used = [], 0
    for it in ordered:
        s = len(json.dumps(it.model_dump(exclude_defaults=True), ensure_ascii=False))
        if kept and used + s > budget_chars:
            continue  # 预算满：后面的（更次要）全跳过
        kept.append(it)
        used += s
    return AssetsAnalysis(
        characters=[i for i in kept if i in result.characters],
        scenes=[i for i in kept if i in result.scenes],
        props=[i for i in kept if i in result.props])


def _merge_call(client: LLMClient, batch: list[AssetsAnalysis]):
    """带 length 截断防御的合并调用：常规版 →（截断）极限压缩版 →（仍炸）
    机械取批内首份保树收敛（2026-09-13 真机：分析 50 分钟成果不再整轮报废）。"""
    from .provider import LLMError
    payload = _results_payload(batch)
    try:
        return ask_validated(client, MERGE_SYSTEM, payload, AssetsAnalysis)
    except LLMError as e:
        if getattr(e, "kind", "") != "length":
            raise
    try:
        return ask_validated(client, _MERGE_COMPACT_SYSTEM, payload, AssetsAnalysis)
    except LLMError as e:
        if getattr(e, "kind", "") != "length":
            raise
    return batch[0], Usage(0, 0)


def merge_analyses(client: LLMClient, results: list[AssetsAnalysis],
                   max_payload_chars: int = 8000, on_progress=None
                   ) -> tuple[AssetsAnalysis, Usage]:
    """树状归并（真机 2026-08-25 教训：56 块拼单请求 53928 tok 爆 16k 上下文）。
    每轮按 max_payload_chars 贪心分批合并，直到剩单结果；用量累计返回。

    2026-09-13 输出量控（玉麟传奇 62 块真机事故）：中间轮产物机械压到半预算
    ——旧版「不丢项」让中间产物贴满预算，下轮贪心装不下两份全走强制两两，
    16k chars 批（≈13k tok）在 16k 上下文下思考+输出必撞 finish_reason=length，
    整轮分析 fatal 后 attempts 又从 chunk 1 重跑 50 分钟。"""
    total_prompt = total_completion = 0
    level = list(results)
    rnd = 0
    # 精确增量打包（2026-09-02 修正）：按「去 JSON 包装的裸 dump 长度」累加，
    # 实际批发送只带一个包装（{"characters":…,"scenes":…,"props":…}）。旧算法
    # 按每份结果的完整载荷累加——每份都重复计一次包装，系统性偏高，小预算下
    # 末轮易剩两份各近预算，触发强制两两合并击穿 max_payload_chars。
    _WRAP = len('{"characters": [], "scenes": [], "props": []}')
    budget = max_payload_chars - 40  # 逗号与少量富余（精确计费后无需大余量）
    half = (budget - _WRAP) // 2     # 中间产物上限：保下层一批装得下两份
    while len(level) > 1:
        rnd += 1
        sizes = [len(_results_payload([r])) - _WRAP for r in level]
        batches, cur, cur_size = [], [], 0
        for r, s in zip(level, sizes):
            if cur and _WRAP + cur_size + s > budget:
                batches.append(cur)
                cur, cur_size = [], 0
            cur.append(r)
            cur_size += s
        if cur:
            batches.append(cur)
        if len(batches) >= len(level):  # 单份即超预算没并起来——强制两两防死循环
            batches = [level[i:i + 2] for i in range(0, len(level), 2)]
        nxt = []
        for batch in batches:
            if len(batch) == 1:
                nxt.append(batch[0])  # 单结果直通（不可再分）
                continue
            merged, usage = _merge_call(client, batch)
            total_prompt += usage.prompt_tokens
            total_completion += usage.completion_tokens
            nxt.append(merged)
        # 量控：中间轮压半预算（树下层健康）；最终产物压全预算（超长篇全量
        # 资产清单对拆解上下文注入也是爆炸——精选主要条目）
        nxt = [_cap_analysis(r, half if len(nxt) > 1 else budget) for r in nxt]
        if on_progress:
            on_progress(f"合并第 {rnd} 轮：{len(batches)} 批 → {len(nxt)} 份")
        level = nxt
    return level[0], Usage(total_prompt, total_completion)




def _system_with_voices(db, data_dir, project_id) -> str:
    """系统词 + 当前音色库清单（2026-08-31 用户需求：LLM 对着真实可用库挑，
    自定义音色也能被分配）。"""
    from ..projects import get_project
    from ..voicelib import voice_library_prompt
    proj = get_project(db, project_id)
    lib = voice_library_prompt(data_dir, proj["slug"] if proj else None)
    return EXTRACT_SYSTEM + "\n\n可用音色库（suggested_voice 只能从中选）：\n" + lib

_KINSHIP_GROUPS = [
    {"母亲", "妈妈", "妈", "娘", "老妈", "妈咪", "家母"},
    {"父亲", "爸爸", "爸", "爹", "老爹", "老爸", "家父"},
    {"儿子", "儿", "孩儿"},
    {"女儿", "闺女"},
    {"爷爷", "奶奶", "姥姥", "外婆", "姥爷", "外公"},
    {"哥哥", "姐姐", "弟弟", "妹妹", "大哥", "大姐"},
    {"丈夫", "妻子", "老公", "老婆", "夫人"},
    {"老师", "师傅", "师父"},
]


_APPELLATION_SUFFIXES = (
    "妈妈", "阿姨", "叔叔", "伯伯", "舅舅", "姑姑", "爷爷", "奶奶", "外婆", "姥姥",
    "哥哥", "姐姐", "弟弟", "妹妹", "大哥", "大姐", "先生", "女士", "老师",
    "医生", "护士", "警官", "师傅", "老板", "经理", "校长", "邻居",
)


def _strip_appellation(name: str) -> str:
    """剥离身份称谓后缀（少芬妈妈→少芬）；纯称谓（妈妈）返回空串——
    空串不参与归一（无专名锚点）。"""
    for suf in _APPELLATION_SUFFIXES:
        if name.endswith(suf):
            return name[: -len(suf)] if len(name) > len(suf) else ""
    return name


def merge_alias_characters(chars: list, text: str) -> tuple:
    """机械称谓归一（2026-09-06 真机：LLM 仍可能输出 少芬妈妈+少芬阿姨 两条）：
    剥离称谓后共享专名 → 同一人，高频名胜出、appearance 取最长（信息最丰富）。
    返回 (去重后列表, [(被并名, 保留名)])。"""
    from collections import Counter
    counts = Counter()
    for name in set(c.name for c in chars):
        counts[name] = text.count(name)
    by_core: dict = {}
    for c in chars:
        core = _strip_appellation(c.name)
        by_core.setdefault(core or c.name, []).append(c)
    out, merged = [], []
    for core, group in by_core.items():
        if len(group) == 1:
            out.append(group[0])
            continue
        keep = max(group, key=lambda c: (counts[c.name], len(c.appearance or "")))
        for c in group:
            if c is keep:
                continue
            if len(c.appearance or "") > len(keep.appearance or ""):
                keep.appearance = c.appearance  # 外貌取信息最丰富者
            if c.suggested_voice and not keep.suggested_voice:
                keep.suggested_voice = c.suggested_voice
            if c.voice_description and not keep.voice_description:
                keep.voice_description = c.voice_description
            merged.append((c.name, keep.name))
        out.append(keep)
    return out, merged


def _is_ghost_name(name: str, text: str) -> bool:
    """幻觉名判定：名字（或亲属称谓组内任一别名）不在原文 → 幻觉。
    P10 真机（2026-09-05 有声1）：LLM 把「妈妈」规范化成「母亲」被误杀——
    组内任一别名词在原文出现即放行。"""
    if name in text:
        return False
    for group in _KINSHIP_GROUPS:
        if name in group and any(a in text for a in group):
            return False
    return True


# 音色绑定 UPDATE。LIMIT 1 必须在子查询括号**内**——括号外即 UPDATE...LIMIT：
# Windows 版 sqlite3 未编译 SQLITE_ENABLE_UPDATE_DELETE_LIMIT，直接
# OperationalError: near "LIMIT"（WSL Debian 版却接受——同 SQL 跨环境两种命运，
# 2026-09-02 线上事故）。提出常量供 tests/test_analyze.py 结构护栏断言。
_VOICE_BIND_SQL = (
    "UPDATE assets SET voice=? WHERE id=("
    "  SELECT a.id FROM assets a JOIN project_assets pa ON pa.asset_id=a.id"
    "  WHERE pa.project_id=? AND a.name=? AND a.kind='character'"
    "  AND a.voice='' LIMIT 1)")


def _comfy_for_voices(db) -> "object | None":
    """音色生成用 ComfyClient；未配置返回 None（只探一次，warn 一次）。"""
    from ..comfy.client import ComfyClient
    from ..settings import ensure_comfy_configured
    try:
        ensure_comfy_configured(db)
    except ValueError:
        return None
    url = (get_setting(db, "comfy") or {}).get("base_url", "http://127.0.0.1:8188")
    return ComfyClient(url)


def analyze_project(db: Database, data_dir: Path, project_id: int,
                    client_factory: ClientFactory | None = None,
                    max_chars: int = 8000) -> list[int]:
    if client_factory is None:
        client_factory = make_client_factory(db)
    proj = get_project(db, project_id)
    if proj is None:
        raise ValueError(f"项目不存在: {project_id}")
    text = data_to_abs(data_dir, proj["novel_path"]).read_text(encoding="utf-8")
    # 时代背景检测（2026-08-25）：明确朝代 → 存项目，参考图/视频提示词自动加时代限制
    from ..era import detect_era
    era = detect_era(text)
    if era:
        conn = db.connect()
        conn.execute("UPDATE projects SET era=? WHERE id=?", (era, project_id))
        conn.commit()
        emit_log(db, "analyze", "info", f"检测到时代背景：{era}（提示词将自动附加时代限制）",
                 project_id=project_id)
    chunks = split_chunks(text, max_chars=max_chars)
    emit_log(db, "analyze", "info", f"开始分析：{len(chunks)} 个文本块（共 {len(text)} 字）",
             project_id=project_id)
    extract_client = client_factory("extract_assets")
    provider_name = get_setting(db, "llm_routing")["extract_assets"]
    results: list[AssetsAnalysis] = []
    for i, chunk in enumerate(chunks, 1):
        emit_log(db, "analyze", "info", f"分块 {i}/{len(chunks)} 开始（{len(chunk)} 字）",
                 project_id=project_id)
        t0 = time.monotonic()
        result, usage = ask_validated(
            extract_client, _system_with_voices(db, data_dir, project_id),
            chunk, AssetsAnalysis,
            on_retry=lambda reason: emit_log(db, "llm", "warn", f"校验重试：{reason}",
                                             project_id=project_id))
        emit_log(db, "llm", "info",
                 f"extract_assets 完成 · {extract_client.model} · "
                 f"{usage.prompt_tokens}+{usage.completion_tokens} tok · {time.monotonic()-t0:.1f}s",
                 project_id=project_id)
        results.append(result)
        log_llm_call(db, "extract_assets", provider_name, extract_client.model, usage,
                     prompt=chunk[:2000], reply=getattr(usage, "last_reply", ""))
    if not results:
        final = AssetsAnalysis(characters=[], scenes=[], props=[])
    elif len(results) == 1:
        final = results[0]
    else:
        final, merge_usage = merge_analyses(
            extract_client, results,
            on_progress=lambda msg: emit_log(db, "analyze", "info", msg,
                                             project_id=project_id))
        emit_log(db, "analyze", "info", f"合并 {len(results)} 块分析结果", project_id=project_id)
        log_llm_call(db, "extract_assets", provider_name, extract_client.model, merge_usage,
                     reply=getattr(merge_usage, "last_reply", ""))
    # R6 机械防污染（2026-09-02 用户需求）：幻觉名（原文中不存在的角色名）落库
    # 前丢弃——真角色的名字（含别名）必然在原文出现过；不存在=模型编造。
    # P10 别名兜底（2026-09-05 真机）：LLM 常把口语称谓规范化成书面语
    # （妈妈→母亲）——亲属称谓组内任一词在原文出现即放行
    _ghost = {c.name.strip() for c in final.characters
              if c.name.strip() and _is_ghost_name(c.name.strip(), text)}
    if _ghost:
        final = final.model_copy(update={"characters": [
            c for c in final.characters if c.name.strip() not in _ghost]})
    # 称谓归一（2026-09-06 少芬妈妈/少芬阿姨）：机械兜底 LLM 未合并的别名
    _dedup_chars, _merged_aliases = merge_alias_characters(
        list(final.characters), text)
    if _merged_aliases:
        final = final.model_copy(update={"characters": _dedup_chars})
        emit_log(db, "analyze", "info",
                 f"称谓归一合并 {len(_merged_aliases)} 个重复角色（"
                 + "、".join(f"{a}→{b}" for a, b in _merged_aliases) + "）",
                 project_id=project_id)
        emit_log(db, "analyze", "warn",
                 f"丢弃 {len(_ghost)} 个原文不存在的角色（幻觉名）："
                 f"{'、'.join(sorted(_ghost))}", project_id=project_id)
    ids = persist_assets(db, data_dir, project_id, final)
    emit_log(db, "analyze", "info",
             f"入库 {len(final.characters)} 角色 / {len(final.scenes)} 场景 / {len(final.props)} 道具",
             project_id=project_id)
    # 孤儿清理（2026-09-06 有声2：persist 合并式入库不删旧角色——重析后
    # 上轮的少芬阿姨永远残留）。本次输出中不存在的 character → 删资产+清
    # 分镜绑定（同 comic._drop_character_bindings 模式，本地实现避免跨模块耦合）
    _keep = {c.name.strip() for c in final.characters}
    _orphans = [a for a in list_project_assets(db, project_id)
                if a["kind"] == "character" and a["name"] not in _keep]
    if _orphans:
        conn = db.connect()
        _oids = [a["id"] for a in _orphans]
        ph = ",".join("?" * len(_oids))
        conn.execute(f"DELETE FROM project_assets WHERE asset_id IN ({ph})", _oids)
        conn.execute(f"DELETE FROM assets WHERE id IN ({ph})", _oids)
        from ..shots import list_shots as _ls
        for sh in _ls(db, project_id):
            led = json.loads(sh["ledger_json"] or "{}")
            _ch = (led.get("assets") or {}).get("characters") or []
            if set(_ch) & set(_oids):
                (led["assets"])["characters"] = [c for c in _ch if c not in _oids]
                conn.execute("UPDATE shots SET ledger_json=? WHERE id=?",
                             (json.dumps(led, ensure_ascii=False), sh["id"]))
        conn.commit()
        emit_log(db, "analyze", "info",
                 f"清理 {len(_orphans)} 个上轮孤儿角色（"
                 + "、".join(a["name"] for a in _orphans) + "，含分镜绑定）",
                 project_id=project_id)
    # 音色决策链（2026-09-02 用户需求：预设优先，不匹配才生成）：
    # ① suggested_voice 库内有效 → 绑（零成本）
    # ② LLM 给了 voice_description → ComfyUI 可用时生成项目级音色（角色名命名）
    #    并绑定；不可用/失败 → warn + 落基线，分析照常完成（音色是增强不是门槛）
    # ③ 都没有 → 性别×年龄基线预设兜底；性别也判不出 → 不绑（渲染走 Edge-TTS）
    # 生成只发生在 LLM 分块全部结束后（本阶段），不会与分析 LLM 抢显存。
    from ..voices import match_voice
    from ..voicelib import voice_library_names
    _lib = voice_library_names(data_dir, proj["slug"])
    _lib_set = set(_lib)
    conn = db.connect()
    n_voice = 0
    comfy = None
    comfy_tried = False
    for ch in final.characters:
        suggested = (getattr(ch, "suggested_voice", "") or "").strip()
        voice = ""
        if suggested in _lib_set:
            voice = suggested
        else:
            desc = (getattr(ch, "voice_description", "") or "").strip()
            if desc:
                if not comfy_tried:
                    comfy_tried = True
                    comfy = _comfy_for_voices(db)
                    if comfy is None:
                        emit_log(db, "analyze", "warn",
                                 "ComfyUI 未配置，跳过角色音色生成（落基线预设）",
                                 project_id=project_id)
                if comfy is not None:
                    try:
                        from .. import voicelib
                        voicelib.generate_custom(comfy, data_dir, proj["slug"],
                                                 ch.name, desc, db=db)
                        voice = ch.name
                    except Exception as e:
                        emit_log(db, "analyze", "warn",
                                 f"角色 {ch.name} 音色生成失败，落基线预设：{e}",
                                 project_id=project_id)
            if not voice:
                voice = match_voice(ch.appearance, "", library=_lib)
        if not voice:
            continue
        cur = conn.execute(_VOICE_BIND_SQL, (voice, project_id, ch.name))
        n_voice += cur.rowcount
    conn.commit()
    if n_voice:
        emit_log(db, "analyze", "info",
                 f"音色自动匹配 {n_voice} 个角色（性别×年龄兜底，可人工改绑）",
                 project_id=project_id)
    set_stage(db, project_id, "analyzed")
    emit_log(db, "system", "info", "阶段流转 created → analyzed", project_id=project_id)
    return ids
