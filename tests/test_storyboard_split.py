import json
import pytest
from types import SimpleNamespace as NS

from comic_studio.engine.assets import persist_assets
from comic_studio.engine.db import Database
from comic_studio.engine.llm.storyboard import ContentBoundaryError, split_storyboards
from comic_studio.engine.projects import create_project
from comic_studio.engine.shots import list_shots
from comic_studio.engine.llm.provider import Usage
from comic_studio.engine.paths import data_to_abs

CHUNK = """{{"shots":[{{
 "text_span":"推门","description":"{desc}","shot_type":"动作",
 "camera":{{"景别":"全景","机位":"平视","运镜":"固定","转场":"切"}},
 "duration":4,"workflow_type":"ref2va",
 "must_appear":["林晨"],"must_keep":[],"may_change":[],"must_avoid":[],
 "character_ids":[{cid}],"scene_ids":[],"prop_ids":[],"continue_prev":false}}]}}"""


class FakeLLM:
    model = "fake"
    def __init__(self, replies): self.replies = list(replies); self.n = 0
    def raw_chat(self, messages, temperature=0.3, max_tokens=None):
        r = self.replies[min(self.n, len(self.replies) - 1)]; self.n += 1
        return r, Usage(1, 2)


def _setup(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "p", "9:16", "林晨推开门。庭院里站着一个白发少女。")["id"]
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="林晨", appearance="黑发少年", tags=[])],
                      scenes=[], props=[]))
    return db, pid


def test_split_single_chunk_persists(tmp_path):
    db, pid = _setup(tmp_path)
    fake = FakeLLM([CHUNK.format(desc="推门镜头", cid=1)])
    ids = split_storyboards(db, tmp_path / "data", pid, client_factory=lambda t: fake)
    rows = list_shots(db, pid)
    assert len(rows) == 1 and rows[0]["prompt"] == ""  # 提示词下一任务生成
    assert "推门镜头" in rows[0]["description"]
    import json
    assert json.loads(rows[0]["ledger_json"])["assets"]["characters"] == [1]


def test_split_multi_chunk_links_continue_prev(tmp_path):
    db, pid = _setup(tmp_path)
    long = "甲" * 60 + "\n\n" + "乙" * 60
    create_project  # noqa
    from comic_studio.engine.projects import get_project
    # 重设 novel 为长文（直接覆盖文件）
    import pathlib
    from comic_studio.engine.paths import data_to_abs
    novel = data_to_abs(tmp_path / "data", get_project(db, pid)["novel_path"])
    novel.parent.mkdir(parents=True, exist_ok=True)
    novel.write_text(long, encoding="utf-8")
    fake = FakeLLM([
        CHUNK.format(desc="第一块末镜", cid=1),
        CHUNK.format(desc="第二块首镜（延续）", cid=1).replace('"continue_prev":false', '"continue_prev":true'),
    ])
    ids = split_storyboards(db, tmp_path / "data", pid,
                            client_factory=lambda t: fake, max_chars=80)
    rows = list_shots(db, pid)
    assert [r["seq"] for r in rows] == [1, 2]
    assert rows[1]["depends_on"] == rows[0]["id"]


@pytest.mark.skip(reason="用户决策 2026-08-31：_content_guard 调用已被用户于 2026-08-29 的 opt 提交注释停用，此测试对应功能不再生效")
def test_content_boundary_blocks_and_reports(tmp_path):
    db, pid = _setup(tmp_path)
    bad = CHUNK.format(desc="涉及幼女的情欲画面", cid=1)
    fake = FakeLLM([bad])
    with pytest.raises(ContentBoundaryError):
        split_storyboards(db, tmp_path / "data", pid, client_factory=lambda t: fake)


def test_split_resolves_relative_novel_path(tmp_path):
    """回归：novel_path 是相对 data 根的存储格式，必须经 data_to_abs 解析。
    用唯一项目名确保 CWD 下不存在可碰巧命中的同名目录（防假阳性）。"""
    from comic_studio.engine.llm.storyboard import split_storyboards
    from comic_studio.engine.shots import list_shots
    from comic_studio.engine.paths import data_to_abs
    db = Database(tmp_path / "s2.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "相对路径回归剧", "9:16", "林晨推门。")["id"]
    fake = FakeLLM([CHUNK.format(desc="推门镜", cid=1)])
    ids = split_storyboards(db, tmp_path / "data", pid, client_factory=lambda t: fake)
    assert len(list_shots(db, pid)) == 1


def test_split_auto_chains_depends_on(tmp_path):
    """连贯性①（2026-08-26）：拆分镜自动建立尾帧接力链——
    每镜 depends_on 指向上一镜，渲染时本镜首帧取上镜视频尾帧。"""
    db, pid = _setup(tmp_path)
    fake = FakeLLM([CHUNK.format(desc="甲", cid=1)])
    split_storyboards(db, tmp_path / "data", pid, client_factory=lambda t: fake)
    rows = list_shots(db, pid)
    assert rows[0]["depends_on"] is None
    for prev, cur in zip(rows, rows[1:]):
        assert cur["depends_on"] == prev["id"]


def test_split_extracts_verbatim_dialogue(tmp_path):
    """台词链路（2026-08-26）：拆分镜照录原文对白入 ledger.dialogue。"""
    db, pid = _setup(tmp_path)
    reply = CHUNK.format(desc="问诊", cid=1).replace(
        '"continue_prev":false',
        '"continue_prev":false,"dialogue":[{"speaker":"林晨","line":"你怎么了？"}]')
    fake = FakeLLM([reply])
    split_storyboards(db, tmp_path / "data", pid, client_factory=lambda t: fake)
    import json as _json
    ledger = _json.loads(list_shots(db, pid)[0]["ledger_json"])
    assert ledger["dialogue"] == [{"speaker": "林晨", "line": "你怎么了？"}]
    # 无对白镜：默认空数组不报错
    fake2 = FakeLLM([CHUNK.format(desc="空镜", cid=1)])
    split_storyboards(db, tmp_path / "data", pid, client_factory=lambda t: fake2)
    ledger2 = _json.loads(list_shots(db, pid)[0]["ledger_json"])
    assert ledger2.get("dialogue", []) == []


def test_auto_bind_characters_from_description(tmp_path):
    """角色自动补绑（2026-08-26 真机：拆解时 LLM 漏绑角色 → 关键帧无参考图）。
    拆完后扫描描述文本，提到的角色自动补绑。"""
    from comic_studio.engine.llm.storyboard import auto_bind_characters
    from comic_studio.engine.shots import list_shots, update_shot
    db, pid = _setup(tmp_path)
    # _setup 有一个角色（林晨），拆一镜描述里提到"林晨"但 character_ids 为空
    fake = FakeLLM([CHUNK.format(desc="林晨推门而入", cid=999)])  # cid=999 = 未绑定
    split_storyboards(db, tmp_path / "data", pid, client_factory=lambda t: fake)
    rows = list_shots(db, pid)
    # 拆解后自动补绑：描述含"林晨" → 应绑定到项目角色
    assert rows[0]["ledger_json"] and "林晨" in rows[0]["description"]
    import json as _json
    ledger = _json.loads(rows[0]["ledger_json"])
    char_ids = (ledger.get("assets") or {}).get("characters") or []
    assert len(char_ids) >= 1  # 自动补绑了


def test_split_default_chunk_fits_context(tmp_path):
    """默认分块必须按模型上下文容量取值（真机 2026-08-27 job 582：8127 字块
    输出撞 16384 num_ctx 硬截断；2026-09-03 job 38410：text_span 必填后输出
    密度上涨，1956 字块 >7.07 completion tok/字仍截断）。span 时代预算：
    密度上限按 11.5 + prompt 0.82 + ~350 开销 ≤ 16384 → 块 ≤ ~1300 字。"""
    db, pid = _setup(tmp_path)
    import pathlib
    from comic_studio.engine.projects import get_project
    novel = data_to_abs(tmp_path / "data", get_project(db, pid)["novel_path"])
    novel.write_text("\n\n".join("甲" * 900 for _ in range(4)), encoding="utf-8")

    class CapturingFake(FakeLLM):
        def __init__(self):
            super().__init__([CHUNK.format(desc="镜", cid=1)])
            self.users = []
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            self.users.append(messages[-1]["content"])
            return super().raw_chat(messages, temperature, max_tokens)

    fake = CapturingFake()
    split_storyboards(db, tmp_path / "data", pid, client_factory=lambda t: fake)
    assert len(fake.users) >= 2  # 3600 字默认必须拆多块（旧默认 8000 只会 1 块）
    chunks = [u.split("小说文本：\n", 1)[1] for u in fake.users]
    assert all(len(c) <= 1300 for c in chunks), [len(c) for c in chunks]


def test_split_target_count_allocates_per_chunk(tmp_path):
    """指定分镜数：按各块字数占比分配配额，注入每块 user 提示词。"""
    from comic_studio.engine.projects import get_project
    db, pid = _setup(tmp_path)
    novel = data_to_abs(tmp_path / "data", get_project(db, pid)["novel_path"])
    novel.write_text("林晨推门，庭院里站着一个白发少女。" * 220, encoding="utf-8")  # ~4400 字 ≥ 2 块
    captured = []

    class CapLLM(FakeLLM):
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            captured.append(messages[-1]["content"])
            return super().raw_chat(messages, temperature=temperature, max_tokens=max_tokens)

    fake = CapLLM([CHUNK.format(desc="镜头", cid=1)] * 6)
    split_storyboards(db, tmp_path / "data", pid, client_factory=lambda t: fake,
                      target_count=9)
    assert len(captured) >= 2
    for u in captured:
        assert "全文目标 9 个分镜" in u and "本块目标" in u
    # 配额合计 = 9（按字数占比 round 分配后补齐）
    quotas = [int(u.split("本块目标拆出约 ")[1].split(" 个")[0]) for u in captured]
    assert sum(quotas) == 9


def test_split_backfills_dialogue_mechanically(tmp_path):
    """对白机械兜底（2026-08-27 真机：nsfwvision-v3 等 RP 模型拆解 dialogue 恒空，
    但 text_span 原文照录了弯引号对白）：从 text_span 正则提取引号句，
    说话人取引号前后最近角色名；LLM 已给出的 dialogue 不动。"""
    from comic_studio.engine.projects import get_project
    db, pid = _setup(tmp_path)
    # 场景对齐真机：登记真嗣/明日香为项目角色（兜底按角色名册归说话人）
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="真嗣", appearance="黑发少年", tags=[]),
                                  NS(name="明日香", appearance="红发少女", tags=[])],
                      scenes=[], props=[]))
    novel = data_to_abs(tmp_path / "data", get_project(db, pid)["novel_path"])
    novel.write_text(
        "阳光洒在屋顶。真嗣抬头，嘴角抽了抽：“她很温柔，像风铃一样。”\n"
        "“喂，你到底在想什么？”明日香把饭盒推了推，“别光吃不说话。”", encoding="utf-8")
    # 模型不填 dialogue（复现真机：text_span 照录原文含引号、dialogue 缺省空数组）
    span = ("真嗣抬头，嘴角抽了抽：“她很温柔，像风铃一样。”"
            "“喂，你到底在想什么？”明日香把饭盒推了推：“别光吃不说话。”")
    no_dlg = CHUNK.format(desc="屋顶对话", cid=1).replace('"text_span":"推门"',
                                                           f'"text_span":"{span}"')
    fake = FakeLLM([no_dlg])
    split_storyboards(db, tmp_path / "data", pid, client_factory=lambda t: fake)
    rows = list_shots(db, pid)
    all_dlg = [d for s in rows for d in (json.loads(s["ledger_json"]).get("dialogue") or [])]
    assert any("风铃" in d["line"] for d in all_dlg)
    assert any("别光吃不说话" in d["line"] for d in all_dlg)
    speakers = {d["speaker"] for d in all_dlg}
    assert "真嗣" in speakers and "明日香" in speakers  # 前后窗口最近角色名


def test_split_whitelist_discipline_in_prompt():
    """P7-B 拆解白名单纪律（借鉴 NovelFlow）：泛称映射/名单外转背景条款进 system，
    名册措辞强调白名单语义。"""
    from comic_studio.engine.llm.storyboard import SPLIT_SYSTEM, build_split_user_prompt
    # 泛称（众人/群臣等）必须映射白名单内具体角色，映射不了按无名背景
    assert "泛称" in SPLIT_SYSTEM and "无名背景" in SPLIT_SYSTEM
    # 白名单外具名角色：改写为背景人物，不绑定不入 must_appear
    assert "背景人物" in SPLIT_SYSTEM and "must_appear" in SPLIT_SYSTEM
    u = build_split_user_prompt("正文", [])
    assert "白名单" in u


def test_split_project_render_mode_overrides(tmp_path):
    """优化#2（2026-09-07）：项目 render_mode 指定（非空）→ staging 后机械
    覆写全部镜 workflow_type；留空=LLM 逐镜智能选（衔接/常规/无参考）不动。"""
    db, pid = _setup(tmp_path)
    conn = db.connect()
    conn.execute("UPDATE projects SET render_mode='fl2v' WHERE id=?", (pid,))
    conn.commit()
    fake = FakeLLM([CHUNK.format(desc="推门镜头", cid=1)])
    split_storyboards(db, tmp_path / "data", pid, client_factory=lambda t: fake)
    rows = list_shots(db, pid)
    assert rows and all(r["workflow_type"] == "fl2v" for r in rows)


# ---------- 2026-09-17 断点续跑 + 单块截断对半降级（job 43228 事故） ----------

class _LenErr:
    """含 marker 的块抛 kind=length 截断错（模拟 16384 窗口对白密集块撞墙）。"""
    model = "len-fake"
    def __init__(self, markers): self.markers = markers; self.n = 0
    def raw_chat(self, messages, temperature=0.3, max_tokens=None):
        user = messages[-1]["content"]
        if self.markers and all(m in user for m in self.markers):
            from comic_studio.engine.llm.provider import LLMError
            e = LLMError("输出被长度上限截断（finish_reason=length）")
            e.kind = "length"
            raise e
        self.n += 1
        return CHUNK.format(desc=f"镜{self.n}", cid=1), Usage(1, 2)


def _five_block_novel(tmp_path, db, pid):
    from comic_studio.engine.projects import get_project
    novel = data_to_abs(tmp_path / "data", get_project(db, pid)["novel_path"])
    novel.parent.mkdir(parents=True, exist_ok=True)
    novel.write_text("\n\n".join(m * 100 for m in "甲乙丙丁戊"), encoding="utf-8")
    return tmp_path / "data" / "projects" / "p" / "split_cache.json"


def test_split_checkpoint_resume(tmp_path):
    """块 3 截断 → 前两块进缓存不重烧；续跑只打块 3/4/5；成功后清缓存。"""
    db, pid = _setup(tmp_path)
    cache = _five_block_novel(tmp_path, db, pid)
    f1 = _LenErr(["丙" * 20])            # 块 3（丙段）必炸
    with pytest.raises(Exception):
        split_storyboards(db, tmp_path / "data", pid,
                          client_factory=lambda t: f1, max_chars=150)
    assert cache.exists()
    data = json.loads(cache.read_text(encoding="utf-8"))
    assert set(data["blocks"]) == {"1", "2"}          # 炸块不入缓存
    # 续跑：好脾气 client，只应打 3/4/5 三次（缓存命中跳过 1/2）
    f2 = _LenErr([])                                  # 无 marker → 全通过
    split_storyboards(db, tmp_path / "data", pid,
                      client_factory=lambda t: f2, max_chars=150)
    assert f2.n == 3
    assert not cache.exists()                         # 成功落库后清缓存
    assert len(list_shots(db, pid)) == 5


def test_split_checkpoint_invalidated_on_text_change(tmp_path):
    """校正写回后重拆：指纹不符 → 缓存作废全量重跑（不吃旧块）。"""
    db, pid = _setup(tmp_path)
    cache = _five_block_novel(tmp_path, db, pid)
    f1 = _LenErr(["丙" * 20])
    with pytest.raises(Exception):
        split_storyboards(db, tmp_path / "data", pid,
                          client_factory=lambda t: f1, max_chars=150)
    assert cache.exists()
    # 改正文（>150 字新首段独立成块 → 指纹变；若缓存误命中只会打 5 次）
    from comic_studio.engine.projects import get_project
    novel = data_to_abs(tmp_path / "data", get_project(db, pid)["novel_path"])
    novel.write_text("新开头" * 60 + "\n\n" + "\n\n".join(m * 100 for m in "甲乙丙丁戊"),
                     encoding="utf-8")
    f2 = _LenErr([])
    split_storyboards(db, tmp_path / "data", pid,
                      client_factory=lambda t: f2, max_chars=150)
    assert f2.n == 6                                 # 6 段全量（无缓存命中）


def test_split_length_halves_block(tmp_path):
    """单块截断不炸 job：按段落边界对半降级，两半各拆一次。"""
    db, pid = _setup(tmp_path)
    from comic_studio.engine.projects import get_project
    novel = data_to_abs(tmp_path / "data", get_project(db, pid)["novel_path"])
    novel.parent.mkdir(parents=True, exist_ok=True)
    novel.write_text("甲" * 300 + "\n\n" + "乙" * 300, encoding="utf-8")
    # 整块（甲乙同框）必炸；半块（只含一段）通过——602 字 max_chars=1000 时单块
    f = _LenErr(["甲" * 50, "乙" * 50])
    split_storyboards(db, tmp_path / "data", pid,
                      client_factory=lambda t: f, max_chars=1000)
    assert f.n == 2                                  # 对半后两次成功调用
    rows = list_shots(db, pid)
    assert len(rows) == 2                            # 两半各一镜
    assert not (tmp_path / "data" / "projects" / "p" / "split_cache.json").exists()


def test_split_cache_invalidated_on_model_change(tmp_path):
    """换模型重拆（2026-09-17 真机：nsfwvision 跑到 99/111 才换 14B）——
    指纹必须含拆解模型，否则续跑回放旧模型结果+新模型只跑尾部=混血分镜。"""
    db, pid = _setup(tmp_path)
    cache = _five_block_novel(tmp_path, db, pid)

    class ModelFake:
        def __init__(self, model, fail_marker=None):
            self.model, self.n, self.fail_marker = model, 0, fail_marker
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            if self.fail_marker and self.fail_marker in messages[-1]["content"]:
                from comic_studio.engine.llm.provider import LLMError
                e = LLMError("输出被长度上限截断"); e.kind = "length"; raise e
            self.n += 1
            return CHUNK.format(desc=f"镜{self.n}", cid=1), Usage(1, 2)

    # 第一轮：nsfwvision 在块 3 截断 → 缓存 1、2
    with pytest.raises(Exception):
        split_storyboards(db, tmp_path / "data", pid,
                          client_factory=lambda t: ModelFake("nsfwvision-v3", "丙" * 20),
                          max_chars=150)
    assert cache.exists()
    # 同模型续跑：命中缓存只打 3/4/5
    f_resume = ModelFake("nsfwvision-v3")
    split_storyboards(db, tmp_path / "data", pid,
                      client_factory=lambda t: f_resume, max_chars=150)
    assert f_resume.n == 3
    # 重造部分缓存，换模型续跑：指纹不符全量重跑 5 块（不回放旧模型结果）
    with pytest.raises(Exception):
        split_storyboards(db, tmp_path / "data", pid,
                          client_factory=lambda t: ModelFake("nsfwvision-v3", "丙" * 20),
                          max_chars=150)
    f2 = ModelFake("qwen2.5:14b-uncen")
    split_storyboards(db, tmp_path / "data", pid,
                      client_factory=lambda t: f2, max_chars=150)
    assert f2.n == 5
