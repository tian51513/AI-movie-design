# comic_studio/engine/llm/analyze.py
"""分析编排：分块 → 抽取 → 合并 → 入库（spec §5 created→analyzed）。"""
import json
import time
from pathlib import Path
from typing import Callable

from ..assets import persist_assets
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
只输出一个 JSON 对象：{"characters":[{"name","role","appearance","tags"}],
"scenes":[{"name","description","tags"}],"props":[{"name","description","tags"}]}"""

MERGE_SYSTEM = """合并多段小说文本的资产分析结果。规则：
- 同名（或明显同一人的别名，如"萧炎/炎少爷"）合并为一条，appearance 取信息最丰富的描述并可融合细节；
- 同一场景不同叫法合并；tags 取并集；
- 保留所有不同条目，不丢项。
输出与输入相同结构的 JSON：{"characters":[...],"scenes":[...],"props":[...]}，
其中每条角色含 name/role/appearance/tags，场景与道具含 name/description/tags。"""

ClientFactory = Callable[[str], LLMClient]


def make_client_factory(db: Database) -> ClientFactory:
    """默认工厂：闭包持有 db，按任务名路由（spec §9.1）。
    独立成函数是为了让测试 monkeypatch analyze.client_for_task 能生效——
    默认参数在定义时绑定，模块属性查找在调用时发生。"""
    return lambda task: client_for_task(db, task)


def _results_payload(results: list[AssetsAnalysis]) -> str:
    return json.dumps(
        {"characters": [c.model_dump() for r in results for c in r.characters],
         "scenes": [s.model_dump() for r in results for s in r.scenes],
         "props": [p.model_dump() for r in results for p in r.props]},
        ensure_ascii=False)


def merge_analyses(client: LLMClient, results: list[AssetsAnalysis],
                   max_payload_chars: int = 8000, on_progress=None
                   ) -> tuple[AssetsAnalysis, Usage]:
    """树状归并（真机 2026-08-25 教训：56 块拼单请求 53928 tok 爆 16k 上下文）。
    每轮按 max_payload_chars 贪心分批合并，直到剩单结果；用量累计返回。"""
    total_prompt = total_completion = 0
    level = list(results)
    rnd = 0
    while len(level) > 1:
        rnd += 1
        # 预算留 200 字余量给 JSON 包裹结构
        budget = max(1, max_payload_chars - 200)
        sizes = [len(_results_payload([r])) for r in level]
        batches, cur, cur_size = [], [], 0
        for r, s in zip(level, sizes):
            if cur and cur_size + s > budget:
                batches.append(cur)
                cur, cur_size = [], 0
            cur.append(r)
            cur_size += s
        if cur:
            batches.append(cur)
        if len(batches) >= len(level):  # 预算过小没并起来——强制两两合并防死循环
            batches = [level[i:i + 2] for i in range(0, len(level), 2)]
        nxt = []
        for batch in batches:
            if len(batch) == 1:
                nxt.append(batch[0])  # 单结果直通（不可再分）
                continue
            merged, usage = ask_validated(client, MERGE_SYSTEM,
                                          _results_payload(batch), AssetsAnalysis)
            total_prompt += usage.prompt_tokens
            total_completion += usage.completion_tokens
            nxt.append(merged)
        if on_progress:
            on_progress(f"合并第 {rnd} 轮：{len(batches)} 批 → {len(nxt)} 份")
        level = nxt
    return level[0], Usage(total_prompt, total_completion)


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
            extract_client, EXTRACT_SYSTEM, chunk, AssetsAnalysis,
            on_retry=lambda reason: emit_log(db, "llm", "warn", f"校验重试：{reason}",
                                             project_id=project_id))
        emit_log(db, "llm", "info",
                 f"extract_assets 完成 · {extract_client.model} · "
                 f"{usage.prompt_tokens}+{usage.completion_tokens} tok · {time.monotonic()-t0:.1f}s",
                 project_id=project_id)
        results.append(result)
        log_llm_call(db, "extract_assets", provider_name, extract_client.model, usage)
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
        log_llm_call(db, "extract_assets", provider_name, extract_client.model, merge_usage)
    ids = persist_assets(db, data_dir, project_id, final)
    emit_log(db, "analyze", "info",
             f"入库 {len(final.characters)} 角色 / {len(final.scenes)} 场景 / {len(final.props)} 道具",
             project_id=project_id)
    set_stage(db, project_id, "analyzed")
    emit_log(db, "system", "info", "阶段流转 created → analyzed", project_id=project_id)
    return ids
