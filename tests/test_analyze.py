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
    proj = create_project(db, tmp_path / "data", "p", "9:16", "一段短文本")
    fake = FakeClient([CHUNK1])
    ids = analyze_project(db, tmp_path / "data", proj["id"], client_factory=lambda t: fake)
    assert len(ids) == 1
    assert get_project(db, proj["id"])["stage"] == "analyzed"
    assert fake.n == 1  # 没有合并调用


def test_multi_chunk_merges(tmp_path):
    db = _db(tmp_path)
    long_text = "\n\n".join(["甲" * 50, "乙" * 50])  # 触发两块
    proj = create_project(db, tmp_path / "data", "p", "9:16", long_text)
    fake = FakeClient([CHUNK1, CHUNK2, MERGED])
    ids = analyze_project(db, tmp_path / "data", proj["id"],
                          client_factory=lambda t: fake, max_chars=60)
    rows = list_project_assets(db, proj["id"])
    assert len(rows) == 3  # 2角色+1场景
    assert fake.n == 3  # 两块抽取 + 一次合并


def test_llm_calls_logged(tmp_path):
    db = _db(tmp_path)
    proj = create_project(db, tmp_path / "data", "p", "9:16", "短文本")
    analyze_project(db, tmp_path / "data", proj["id"],
                    client_factory=lambda t: FakeClient([CHUNK1]))
    n = db.connect().execute("SELECT COUNT(*) c FROM llm_calls").fetchone()["c"]
    assert n == 1


def test_merge_llm_call_logs_real_usage(tmp_path):
    """合并步骤的 llm_calls 应记录真实 usage 而非 0/0。"""
    db = _db(tmp_path)
    long_text = "\n\n".join(["甲" * 50, "乙" * 50])
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
    proj = create_project(db, tmp_path / "data", "日志剧", "9:16", "一段短文本")
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
