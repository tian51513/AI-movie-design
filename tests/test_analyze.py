# tests/test_analyze.py
from pathlib import Path

from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project, get_project
from comic_studio.engine.assets import list_project_assets
from comic_studio.engine.llm.analyze import analyze_project, EXTRACT_SYSTEM, MERGE_SYSTEM
from comic_studio.engine.llm.provider import LLMClient, Usage

CHUNK1 = '{"characters":[{"name":"萧炎","appearance":"黑发少年"}],"scenes":[],"props":[]}'
CHUNK2 = '{"characters":[{"name":"萧薰儿","appearance":"白衣少女"}],"scenes":[{"name":"乌坦城","description":"古城"}],"props":[]}'
MERGED = ('{"characters":[{"name":"萧炎","appearance":"黑发少年"},{"name":"萧薰儿","appearance":"白衣少女"}],'
          '"scenes":[{"name":"乌坦城","description":"古城"}],"props":[]}')


class FakeClient(LLMClient):
    def __init__(self, responses):
        super().__init__("http://x", "k", "fake")
        self.responses = list(responses)
        self.n = 0

    def raw_chat(self, messages, temperature=0.3):
        r = self.responses[min(self.n, len(self.responses) - 1)]
        self.n += 1
        return r, Usage(1, 2)


def _db(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate(); return db


def test_prompts_pin_json_contract():
    assert '"characters"' in EXTRACT_SYSTEM and "appearance" in EXTRACT_SYSTEM
    assert "同名" in MERGE_SYSTEM  # 合并规则必须提到同名合并


def test_single_chunk_no_merge(tmp_path):
    db = _db(tmp_path)
    proj = create_project(db, tmp_path / "data", "p", "9:16", "萧炎的短文本")
    fake = FakeClient([CHUNK1])
    ids = analyze_project(db, tmp_path / "data", proj["id"], client_factory=lambda t: fake)
    assert len(ids) == 1
    assert get_project(db, proj["id"])["stage"] == "analyzed"
    assert fake.n == 1  # 没有合并调用


def test_multi_chunk_merges(tmp_path):
    db = _db(tmp_path)
    long_text = "\n\n".join(["萧炎甲" * 10, "萧薰儿乙" * 10])  # 触发两块
    proj = create_project(db, tmp_path / "data", "p", "9:16", long_text)
    fake = FakeClient([CHUNK1, CHUNK2, MERGED])
    ids = analyze_project(db, tmp_path / "data", proj["id"],
                          client_factory=lambda t: fake, max_chars=60)
    rows = list_project_assets(db, proj["id"])
    assert len(rows) == 3  # 2角色+1场景
    assert fake.n == 3  # 两块抽取 + 一次合并


def test_llm_calls_logged(tmp_path):
    db = _db(tmp_path)
    proj = create_project(db, tmp_path / "data", "p", "9:16", "萧炎短文本")
    analyze_project(db, tmp_path / "data", proj["id"],
                    client_factory=lambda t: FakeClient([CHUNK1]))
    n = db.connect().execute("SELECT COUNT(*) c FROM llm_calls").fetchone()["c"]
    assert n == 1


def test_merge_llm_call_logs_real_usage(tmp_path):
    """合并步骤的 llm_calls 应记录真实 usage 而非 0/0。"""
    db = _db(tmp_path)
    long_text = "\n\n".join(["萧炎甲" * 10, "萧薰儿乙" * 10])
    proj = create_project(db, tmp_path / "data", "p", "9:16", long_text)
    fake = FakeClient([CHUNK1, CHUNK2, MERGED])
    analyze_project(db, tmp_path / "data", proj["id"],
                    client_factory=lambda t: fake, max_chars=60)
    rows = db.connect().execute(
        "SELECT prompt_tokens, completion_tokens FROM llm_calls ORDER BY id").fetchall()
    assert len(rows) == 3  # 2 extract + 1 merge
    for r in rows:
        assert r["prompt_tokens"] == 1
        assert r["completion_tokens"] == 2


def test_analysis_emits_structured_logs(tmp_path):
    from comic_studio.engine.db import Database
    from comic_studio.engine.logbus import fetch_logs
    db = Database(tmp_path / "s.db"); db.migrate()
    proj = create_project(db, tmp_path / "data", "日志剧", "9:16", "萧炎的短文本")
    analyze_project(db, tmp_path / "data", proj["id"],
                    client_factory=lambda t: FakeClient([CHUNK1]))
    msgs = [(r["source"], r["level"], r["message"]) for r in fetch_logs(db, proj["id"])]
    texts = " | ".join(m for _, _, m in msgs)
    assert ("analyze", "info", texts.count("分块 1/1 开始")) == ("analyze", "info", 1)
    assert "extract_assets 完成 · fake ·" in texts
    assert "入库 1 角色 / 0 场景 / 0 道具" in texts
    assert "阶段流转 created → analyzed" in texts
    assert all(r["project_id"] == proj["id"] for r in fetch_logs(db, proj["id"]))


def test_merge_tree_batches_payload():
    """真机 bug（2026-08-25 验收）：56 块合并单请求 53928 tok 爆 16k 上下文。
    树状归并：每次请求 user 载荷 ≤ max_payload_chars，多轮直到单结果；用量累计；不丢项。"""
    import json
    from comic_studio.engine.llm.analyze import merge_analyses
    from comic_studio.engine.llm.schemas import AssetsAnalysis

    def mk(i):
        return AssetsAnalysis.model_validate_json(json.dumps(
            {"characters": [{"name": f"角{i}", "role": "配角",
                             "appearance": "外" * 300, "tags": []}],
             "scenes": [], "props": []}, ensure_ascii=False))

    results = [mk(i) for i in range(12)]  # 每个序列化约 350 字
    calls = []

    class RecFake(FakeClient):
        def raw_chat(self, messages, temperature=0.3):
            calls.append(len(messages[-1]["content"]))  # user 载荷长度
            names = [c["name"] for c in json.loads(messages[-1]["content"])["characters"]]
            return json.dumps(
                {"characters": [{"name": n, "role": "配角", "appearance": "x", "tags": []}
                                for n in names], "scenes": [], "props": []},
                ensure_ascii=False), Usage(100, 50)

    fake = RecFake([None])
    merged, usage = merge_analyses(fake, results, max_payload_chars=1200)
    assert all(c <= 1200 for c in calls), calls
    assert len(calls) >= 4  # 多轮树状（12→4→2→1 至少 7 次调用）
    assert usage.prompt_tokens == 100 * len(calls)
    assert usage.completion_tokens == 50 * len(calls)
    assert len(merged.characters) == 12  # 无丢项


def test_merge_tree_progress_callback():
    """on_progress 每轮回调（前端日志可见合并进度，防止长合并像卡死）。"""
    import json
    from comic_studio.engine.llm.analyze import merge_analyses
    from comic_studio.engine.llm.schemas import AssetsAnalysis

    def mk(i):
        return AssetsAnalysis.model_validate_json(json.dumps(
            {"characters": [{"name": f"角{i}", "appearance": "x"}],
             "scenes": [], "props": []}, ensure_ascii=False))

    rounds = []
    merged, _ = merge_analyses(FakeClient([MERGED]), [mk(i) for i in range(6)],
                               max_payload_chars=200,
                               on_progress=lambda msg: rounds.append(msg))
    assert len(rounds) >= 1 and any("合并" in m for m in rounds)


def test_analyze_suggests_voices(tmp_path, monkeypatch):
    """音色自动匹配（2026-08-30 用户需求）：LLM 分析角色时按年龄/性别/气质
    推荐 15 预设之一 → 落 assets.voice；非法值忽略；人工仍可改。"""
    import io
    from types import SimpleNamespace as NS
    from fastapi.testclient import TestClient
    from comic_studio.web.app import create_app
    from comic_studio.engine.llm.provider import Usage

    GOOD = ('{"characters":['
            '{"name":"小雪","role":"主角","appearance":"性别：女 年龄：8岁 服装：红裙",'
            '"tags":[],"suggested_voice":"萝莉"},'
            '{"name":"老爷","role":"配角","appearance":"性别：男 年龄：60岁",'
            '"tags":[],"suggested_voice":"老年男声"},'
            '{"name":"路人","role":"路人","appearance":"性别：男 年龄：30岁",'
            '"tags":[],"suggested_voice":"不存在音色"}],'
            '"scenes":[],"props":[]}')

    class FakeLLM:
        model = "fake"
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            return GOOD, Usage(1, 1)
        def ask_validated(self, system, user, schema, **kw):
            from comic_studio.engine.llm.schemas import AssetsAnalysis
            import json as _json
            return schema.model_validate(_json.loads(GOOD)), Usage(1, 1)

    monkeypatch.setattr("comic_studio.engine.llm.analyze.client_for_task",
                        lambda db, task: FakeLLM())
    with TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "d",
                               start_workers=False)) as c:
        pid = c.post("/api/projects", data={"name": "匹配剧", "aspect_ratio": "9:16"},
                     files={"novel": ("n.txt", io.BytesIO("小雪搀着老爷，路人让开。".encode()),
                                      "text/plain")}).json()["id"]
        c.post(f"/api/projects/{pid}/analyze")
        import time
        for _ in range(60):
            if c.get(f"/api/projects/{pid}/analyze/status").json()["status"] != "running":
                break
            time.sleep(0.05)
        assets = {a["name"]: a for a in c.get(f"/api/projects/{pid}/assets").json()}
        assert assets["小雪"]["voice"] == "萝莉"
        assert assets["老爷"]["voice"] == "老年男声"
        assert assets["路人"]["voice"] == "深沉男声"  # 非法建议忽略→性别×年龄兜底（青年男）


def test_voice_match_update_sql_valid(tmp_path):
    """2026-09-02 线上事故：音色自动匹配的 UPDATE 把 LIMIT 1 写在子查询括号外，
    成了 UPDATE...LIMIT——Python sqlite3 未编译 SQLITE_ENABLE_UPDATE_DELETE_LIMIT，
    直接 OperationalError: near "LIMIT"。appearance 带性别/年龄即踩中音色分支
    （老 fixture「黑发少年」匹配不到音色，SQL 从未真正执行过）。"""
    db = _db(tmp_path)
    proj = create_project(db, tmp_path / "data", "p", "9:16", "林战的短文本")
    chunk = ('{"characters":[{"name":"林战","appearance":"性别：男；年龄：30；铁甲将军"}],'
             '"scenes":[],"props":[]}')
    analyze_project(db, tmp_path / "data", proj["id"],
                    client_factory=lambda t: FakeClient([chunk]))
    assets = list_project_assets(db, proj["id"])
    assert assets and assets[0]["voice"], "音色应写入资产"


def test_voice_bind_sql_no_update_level_limit():
    """结构护栏（引擎无关）：UPDATE 语句禁止括号外 LIMIT——WSL Debian 版
    sqlite 接受 UPDATE...LIMIT 而 Windows 版拒绝，行为测试在 WSL 上永远绿，
    只有静态断言能跨环境守住（2026-09-02 Windows 线上事故）。"""
    import re
    from comic_studio.engine.llm.analyze import _VOICE_BIND_SQL
    assert not re.search(r"\)\s*LIMIT", _VOICE_BIND_SQL), "LIMIT 落在子查询括号外=UPDATE级LIMIT"
    assert "'' LIMIT 1)" in _VOICE_BIND_SQL  # 在子查询内


def _voice_chunk(name="玄鸟", **extra):
    import json as _json
    ch = {"name": name, "appearance": "性别：女；年龄：28岁；银发斗篷",
          "suggested_voice": "", "voice_description": "空灵慵懒的低语女声"}
    ch.update(extra)
    return _json.dumps({"characters": [ch], "scenes": [], "props": []})


def test_voice_chain_generates_project_voice_when_no_preset_matches(tmp_path, monkeypatch):
    """R1（2026-09-02）：库内 suggested 无效 + voice_description → 生成项目级
    音色（以角色名命名）并绑定。"""
    from comic_studio.engine import voicelib
    db = _db(tmp_path)
    proj = create_project(db, tmp_path / "data", "p", "9:16", "玄鸟的短文本")
    from comic_studio.engine.settings import set_setting
    set_setting(db, "comfy", {"base_url": "http://127.0.0.1:8188"})
    calls = []

    def fake_gen(comfy, data_dir, project, name, instruction, db=None):
        calls.append((project, name, instruction))
        out = Path(data_dir) / "projects" / project / "voices" / f"{name}.flac"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fLaC")
        return out
    monkeypatch.setattr(voicelib, "generate_custom", fake_gen)
    analyze_project(db, tmp_path / "data", proj["id"],
                    client_factory=lambda t: FakeClient([_voice_chunk()]))
    assert calls == [(proj["slug"], "玄鸟", "空灵慵懒的低语女声")]
    a = list_project_assets(db, proj["id"])[0]
    assert a["voice"] == "玄鸟"


def test_voice_chain_degrades_to_baseline_when_comfy_unavailable(tmp_path, monkeypatch):
    """R1：ComfyUI 未配置 → 不生成、不炸分析，落性别×年龄基线预设。"""
    from comic_studio.engine import voicelib
    db = _db(tmp_path)
    proj = create_project(db, tmp_path / "data", "p", "9:16", "玄鸟的短文本")
    monkeypatch.setattr(voicelib, "generate_custom",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应生成")))
    analyze_project(db, tmp_path / "data", proj["id"],
                    client_factory=lambda t: FakeClient([_voice_chunk()]))
    a = list_project_assets(db, proj["id"])[0]
    assert a["voice"]  # 基线兜底（女/28 → 青年女预设）
    assert get_project(db, proj["id"])["stage"] == "analyzed"


def test_analyze_drops_ghost_character_names(tmp_path):
    """R6（2026-09-02 用户需求）：LLM 幻觉名（原文中不存在的角色）落库前
    机械丢弃——防无关角色绑音色耗资源、污染资产表。"""
    db = _db(tmp_path)
    proj = create_project(db, tmp_path / "data", "p", "9:16", "林战踏马入城。")
    chunk = ('{"characters":[{"name":"林战","appearance":"性别：男；年龄：30岁"},'
             '{"name":"幽泉老祖","appearance":"性别：男；年龄：70岁"}],'
             '"scenes":[],"props":[]}')
    analyze_project(db, tmp_path / "data", proj["id"],
                    client_factory=lambda t: FakeClient([chunk]))
    names = {a["name"] for a in list_project_assets(db, proj["id"])}
    assert names == {"林战"}  # 幽泉老祖=幻觉名，丢弃


def test_extract_system_covers_dialogue_only_text():
    """P10 真机（2026-09-05 有声1）：ASR 文本无第三人称叙述层，第一人称
    说话人（自称 妈妈/老师）被「仅被提及不出场」规则误杀——规则补对白体。"""
    from comic_studio.engine.llm.analyze import EXTRACT_SYSTEM
    assert "对白体" in EXTRACT_SYSTEM
    assert "第一人称说话人" in EXTRACT_SYSTEM


def test_kinship_alias_survives_ghost_filter():
    """P10 真机（2026-09-05 有声1 重析）：LLM 把「妈妈」规范化成「母亲」被
    幻觉名护栏误杀——亲属称谓别名词典兜底（原文任一别名词出现即保留）。"""
    from comic_studio.engine.llm.analyze import _is_ghost_name
    text = "儿子 你这是怎么了 妈妈给你舔了"
    assert _is_ghost_name("母亲", text) is False   # 妈妈在文 → 别名放行
    assert _is_ghost_name("妈妈", text) is False
    assert _is_ghost_name("林凡", text) is True    # 真幻觉仍拦
    assert _is_ghost_name("路人甲", text) is True


def test_extract_system_alias_merge_rule():
    """2026-09-06 用户需求（有声2：少芬妈妈/少芬阿姨重复资产）：提取词明示
    同一人不同称谓合并为一个角色，name 取高频称呼。"""
    from comic_studio.engine.llm.analyze import EXTRACT_SYSTEM
    assert "不同称谓" in EXTRACT_SYSTEM
    assert "合并为一个角色" in EXTRACT_SYSTEM


def test_alias_suffix_dedup_mechanical():
    """机械兜底：共享专名+称谓后缀（少芬妈妈/少芬阿姨→少芬）判同一人；
    高频名胜出，绑定/外貌保留。纯专名不同（少芬/李婷）不合并。"""
    from types import SimpleNamespace as NS
    from comic_studio.engine.llm.analyze import merge_alias_characters
    chars = [
        NS(name="少芬妈妈", appearance="性别：女\n年龄：35岁", tags=["主角"],
           role="主角", suggested_voice="", voice_description=""),
        NS(name="少芬阿姨", appearance="性别：女", tags=[], role="配角",
           suggested_voice="", voice_description=""),
        NS(name="儿子", appearance="性别：男", tags=[], role="主角",
           suggested_voice="", voice_description=""),
        NS(name="李婷", appearance="性别：女", tags=[], role="配角",
           suggested_voice="", voice_description=""),
    ]
    out, merged = merge_alias_characters(chars, text="少芬妈妈"*15 + "少芬阿姨"*4 + "儿子李婷")
    names = [c.name for c in out]
    assert names == ["少芬妈妈", "儿子", "李婷"]      # 阿姨并入妈妈（高频胜出）
    assert merged == [("少芬阿姨", "少芬妈妈")]
    assert "35岁" in out[0].appearance                # 外貌保留（妈妈的信息丰富）


def test_alias_suffix_strip():
    from comic_studio.engine.llm.analyze import _strip_appellation
    assert _strip_appellation("少芬妈妈") == "少芬"
    assert _strip_appellation("少芬阿姨") == "少芬"
    assert _strip_appellation("王刚叔叔") == "王刚"
    assert _strip_appellation("李婷") == "李婷"        # 无称谓原样
    assert _strip_appellation("妈妈") == ""           # 纯称谓→空（不参与归一）


def test_reanalyze_prunes_orphan_characters(tmp_path, monkeypatch):
    """2026-09-06 有声2：重析后旧「少芬阿姨」残留——persist 合并式入库从不删
    旧角色。重析应清理本次输出中不存在的 character（资产+分镜绑定）。"""
    from types import SimpleNamespace as NS
    from comic_studio.engine.db import Database
    from comic_studio.engine.llm.analyze import analyze_project
    from comic_studio.engine.projects import create_project, set_stage
    from comic_studio.engine.assets import list_project_assets, persist_assets
    from comic_studio.engine.shots import list_shots, persist_shots
    import json as _json

    class FakeLLM:
        model = "fake"
        def __init__(self, names):
            self.names = names
        def raw_chat(self, messages, temperature=None, **kw):
            from comic_studio.engine.llm.provider import Usage
            chars = ",".join(f'{{"name":"{n}","role":"主角","appearance":"性别：女","tags":[]}}'
                             for n in self.names)
            return ('{"characters":[' + chars + '],'
                    '"scenes":[{"name":"卧室","description":"d","tags":[]}],'
                    '"props":[]}', Usage(100, 50))
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "d", "孤剧", "16:9",
                         "少芬妈妈说话 少芬阿姨也说话 女儿看着")["id"]
    # 第一轮：建出 少芬妈妈+少芬阿姨+女儿
    monkeypatch.setattr("comic_studio.engine.llm.analyze.client_for_task",
                        lambda db, task: FakeLLM(["少芬妈妈", "少芬阿姨", "女儿"]))
    analyze_project(db, tmp_path / "d", pid)
    names1 = sorted(a["name"] for a in list_project_assets(db, pid)
                    if a["kind"] == "character")
    # 第一轮即被称谓归一合并（阿姨→妈妈，机械护栏）
    assert names1 == ["女儿", "少芬妈妈"]
    # 分镜绑定到女儿（第二轮将不再输出女儿→孤儿）
    aids = {a["name"]: a["id"] for a in list_project_assets(db, pid)}
    sid = persist_shots(db, pid, [NS(text_span="", description="d", shot_type="",
        camera={}, duration=5.0, workflow_type="t2v",
        ledger={"assets": {"characters": [aids["女儿"]]}},
        character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)])[0]
    # 第二轮重析：LLM 只输出 少芬妈妈（女儿退场）
    monkeypatch.setattr("comic_studio.engine.llm.analyze.client_for_task",
                        lambda db, task: FakeLLM(["少芬妈妈"]))
    analyze_project(db, tmp_path / "d", pid)
    names2 = sorted(a["name"] for a in list_project_assets(db, pid)
                    if a["kind"] == "character")
    assert names2 == ["少芬妈妈"]                    # 孤儿女儿被清
    led = _json.loads(list_shots(db, pid)[0]["ledger_json"])
    assert led["assets"]["characters"] == []        # 绑定同步清（无悬空 id）


def test_extract_logs_prompt_and_reply_text():
    """llm_calls 留痕补齐（2026-09-06：排障时发现 prompt_text/reply_text 全空
    ——log_llm_call 支持传参但 extract_assets 调用点没传）。"""
    import json as _json
    from types import SimpleNamespace as NS
    from comic_studio.engine.db import Database
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.llm.analyze import analyze_project
    from comic_studio.engine.llm.provider import Usage

    class FakeLLM:
        model = "fake"
        def raw_chat(self, messages, temperature=None, **kw):
            return '{"characters":[],"scenes":[],"props":[]}', Usage(10, 5)
    import tempfile as _tf
    from pathlib import Path as _P
    tmp_path = _P(_tf.mkdtemp())
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "d", "痕剧", "16:9", "正文内容若干")["id"]
    import unittest.mock as _mock
    with _mock.patch("comic_studio.engine.llm.analyze.client_for_task",
                     lambda db, task: FakeLLM()):
        analyze_project(db, tmp_path / "d", pid)
    rows = db.connect().execute(
        "SELECT length(prompt_text), length(reply_text) FROM llm_calls "
        "WHERE task='extract_assets' ORDER BY id DESC LIMIT 1").fetchone()
    assert rows[0] > 0, "prompt_text 应有内容（系统词+用户词摘要）"
    assert rows[1] > 0, "reply_text 应有内容（模型输出摘要）"


# ---- 2026-09-13 玉麟传奇 62 块真机事故：合并树高层爆上下文 ----
# 根因：MERGE_SYSTEM「不丢项」+无输出预算 → 中间轮产物贴满 max_payload_chars
# → 下轮贪心装不下两份走「强制两两」→ 16k chars 批 ≈13k tok → 16k 上下文
# 思考+输出撞墙 finish_reason=length fatal → attempts 自动从头重跑 62 块。

def test_merge_system_selective_wording():
    """合并 system 弃「不丢项」承诺，改输出预算/精选措辞（超长篇树收敛前提）。"""
    assert "不丢项" not in MERGE_SYSTEM
    assert "精选" in MERGE_SYSTEM or "丢弃" in MERGE_SYSTEM or "保留主要" in MERGE_SYSTEM


def test_merge_intermediate_output_capped():
    """中间轮产物机械量控到半预算（保 主角>配角>路人 权重）——下层贪心
    永远装得下两份，强制两两分支不再出现 16k chars 爆批。"""
    import json
    from comic_studio.engine.llm.analyze import merge_analyses
    from comic_studio.engine.llm.schemas import AssetsAnalysis

    def item(i, role):
        return {"name": f"角色名字很长{i:03d}", "role": role,
                "appearance": "描述" * 30, "tags": []}

    big = AssetsAnalysis.model_validate_json(json.dumps(
        {"characters": [item(0, "主角"), item(1, "配角")] +
                        [item(i, "路人") for i in range(2, 30)],
         "scenes": [], "props": []}, ensure_ascii=False))  # 裸载荷远超半预算

    class OneShotFake(FakeClient):
        def raw_chat(self, messages, temperature=0.3):
            # round1 唯一一轮合并：返回超预算产物；level 剩 2 份→再合一轮返回小结果
            return json.dumps(
                {"characters": [item(0, "主角"), item(1, "配角")] +
                 [item(i, "路人") for i in range(2, 30)],
                 "scenes": [], "props": []}, ensure_ascii=False), Usage(10, 10)

    # 4 份小输入 → round1 两批（各 2 份）→ 每批返回 30 条大产物（中间轮）→
    # round2 把两份中间产物合一。若无中间量控：round2 载荷 = 2×大产物远超
    # 1200 预算（这正是 62 块真机强制两两爆批的结构复现）。
    def small_mk(i):
        return AssetsAnalysis.model_validate_json(json.dumps(
            {"characters": [{"name": f"小{i}", "role": "主角",
                             "appearance": "长" * 420, "tags": []}],
             "scenes": [], "props": []}, ensure_ascii=False))  # ~450 字：两份/批
    calls = []

    class BigFake(FakeClient):
        def raw_chat(self, messages, temperature=0.3):
            calls.append(len(messages[-1]["content"]))
            return json.dumps(
                {"characters": [item(0, "主角"), item(1, "配角")] +
                 [item(i, "路人") for i in range(2, 30)],
                 "scenes": [], "props": []}, ensure_ascii=False), Usage(10, 10)

    merged, _u = merge_analyses(BigFake([None]),
                                [small_mk(i) for i in range(4)], max_payload_chars=1200)
    assert len(calls) == 3  # round1×2 + round2×1
    assert all(c <= 1200 for c in calls), calls  # 中间量控后 round2 载荷合规
    names = [c.name for c in merged.characters]
    assert "角色名字很长000" in names  # 主角必保
    assert len(merged.characters) < 30  # 最终产物也被量控（路人丢弃）


def test_merge_length_failure_compact_retry():
    """merge 批遇 finish_reason=length：极限压缩 system 重试一次成功——
    树继续收敛，analyze 不 fatal。"""
    import json
    from comic_studio.engine.llm.provider import LLMError
    from comic_studio.engine.llm.analyze import merge_analyses
    from comic_studio.engine.llm.schemas import AssetsAnalysis

    small = AssetsAnalysis.model_validate_json(json.dumps(
        {"characters": [{"name": "角A", "role": "主角", "appearance": "x", "tags": []}],
         "scenes": [], "props": []}, ensure_ascii=False))
    state = {"n": 0, "systems": []}

    class FlakyFake(FakeClient):
        def raw_chat(self, messages, temperature=0.3):
            state["systems"].append(messages[0]["content"])
            state["n"] += 1
            if state["n"] == 1:
                e = LLMError("输出被长度上限截断（finish_reason=length）")
                e.kind = "length"
                raise e
            return json.dumps(
                {"characters": [{"name": "角A", "role": "主角", "appearance": "x", "tags": []}],
                 "scenes": [], "props": []}, ensure_ascii=False), Usage(5, 5)

    merged, _u = merge_analyses(FlakyFake([None]), [small, small], max_payload_chars=1200)
    assert len(merged.characters) == 1
    assert len(state["systems"]) == 2          # 常规版 → 压缩版
    assert state["systems"][0] != state["systems"][1]  # 第二次换了压缩 system


def test_merge_length_double_failure_takes_first():
    """压缩重试也炸：机械取批内首份保树收敛（warn 透明，不 fatal）。"""
    import json
    from comic_studio.engine.llm.provider import LLMError
    from comic_studio.engine.llm.analyze import merge_analyses
    from comic_studio.engine.llm.schemas import AssetsAnalysis

    a = AssetsAnalysis.model_validate_json(json.dumps(
        {"characters": [{"name": "角A", "role": "主角", "appearance": "x", "tags": []}],
         "scenes": [], "props": []}, ensure_ascii=False))
    b = AssetsAnalysis.model_validate_json(json.dumps(
        {"characters": [{"name": "角B", "role": "配角", "appearance": "y", "tags": []}],
         "scenes": [], "props": []}, ensure_ascii=False))

    class AlwaysLengthFake(FakeClient):
        def raw_chat(self, messages, temperature=0.3):
            e = LLMError("输出被长度上限截断（finish_reason=length）")
            e.kind = "length"
            raise e

    merged, _u = merge_analyses(AlwaysLengthFake([None]), [a, b], max_payload_chars=1200)
    assert [c.name for c in merged.characters] == ["角A"]  # 批内首份
