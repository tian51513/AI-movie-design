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
