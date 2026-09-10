# tests/test_comic.py
"""P8 漫画→视频（2026-08-29）：每图一镜复用 fl2v 链路（页 i=首帧/页 i+1=尾帧）。"""
import io

from fastapi.testclient import TestClient

from comic_studio.engine.db import Database
from comic_studio.web.app import create_app

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64  # 假 PNG 字节（导入不解码，原样落盘）


def test_import_comic_creates_fl2v_shots(tmp_path):
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.comic import import_comic
    db = Database(tmp_path / "s.db"); db.migrate()
    images = [f"page{i:02d}.png" for i in range(3)]
    blobs = [PNG * (i + 1) for i in range(3)]
    pid = import_comic(db, tmp_path / "data", "漫画剧", "9:16",
                       list(zip(images, blobs)))["id"]
    from comic_studio.engine.projects import get_project
    from comic_studio.engine.shots import list_shots
    proj = get_project(db, pid)
    assert proj["stage"] == "storyboard_ready"  # 跳过分析/拆解，直达提示词阶段
    shots = list_shots(db, pid)
    assert len(shots) == 3
    assert all(s["workflow_type"] == "fl2v" for s in shots)
    # 关键帧落位：页 i = 镜 i 首帧；页 i+1 = 镜 i 尾帧（最后一镜无尾帧）
    from comic_studio.engine.paths import data_to_abs
    for i, s in enumerate(shots, 1):
        d = data_to_abs(tmp_path / "data", f"projects/漫画剧/shots/{i}")
        assert (d / "kf_start.png").read_bytes() == PNG * i
        if i < 3:
            assert (d / "kf_end.png").read_bytes() == PNG * (i + 1)
        else:
            assert not (d / "kf_end.png").exists()
    # novel 占位（链路兼容）
    assert data_to_abs(tmp_path / "data", proj["novel_path"]).exists()


def test_describe_shots_vision_calls(tmp_path, monkeypatch):
    """VLM 读图：多模态消息（image_url base64）→ 每镜提示词落库。"""
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.comic import import_comic, describe_shots
    from comic_studio.engine.llm.provider import LLMClient, Usage
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = import_comic(db, tmp_path / "data", "读图剧", "9:16",
                       [("p1.png", PNG), ("p2.png", PNG)])["id"]
    seen = []

    class FakeVision(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "nsfwvision")
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            seen.append(messages)
            return "少年推开门，画面延续至下一格：他走进房间。", Usage(10, 20)

    n = describe_shots(db, tmp_path / "data", pid, FakeVision())
    assert n == 2
    from comic_studio.engine.shots import list_shots
    rows = list_shots(db, pid)
    assert all(r["prompt"].startswith("少年推开") for r in rows)
    # 多模态消息：含 image_url base64（首帧与尾帧两张）
    content = seen[0][-1]["content"]
    assert isinstance(content, list)
    kinds = [c.get("type") for c in content]
    assert "text" in kinds and kinds.count("image_url") >= 1


def test_describe_shots_motion_skips_character_assets(tmp_path):
    """动态漫不提取角色（2026-08-29 真机：83 个旁白/叙述垃圾资产 + 1195 处绑定）。
    fl2v 用漫画原页渲染，角色资产毫无用处。"""
    import json
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.comic import import_comic, describe_shots
    from comic_studio.engine.llm.provider import LLMClient, Usage
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = import_comic(db, tmp_path / "data", "动态漫剧", "9:16",
                       [("p1.png", PNG), ("p2.png", PNG)])["id"]  # 默认 motion_comic

    class FakeVision(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "v")
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            return "旁白：「第一天」继父举起相机，定格成纪念照。", Usage(10, 20)

    describe_shots(db, tmp_path / "data", pid, FakeVision())
    from comic_studio.engine.assets import list_project_assets
    from comic_studio.engine.shots import list_shots
    assert list_project_assets(db, pid) == []  # 动态漫：零资产
    for s in list_shots(db, pid):
        ledger = json.loads(s["ledger_json"] or "{}")
        assert not (ledger.get("assets") or {}).get("characters")  # 零绑定


def test_describe_shots_film_extracts_only_recurring_real_names(tmp_path):
    """漫改提取过滤（2026-08-29 真机：旁白/对白/叙述短语全被当人名）：
    说话人须全篇出现 ≥2 次 + 长度 ≤4 + 叙述词黑名单。"""
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.comic import import_comic, describe_shots
    from comic_studio.engine.llm.provider import LLMClient, Usage
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = import_comic(db, tmp_path / "data", "漫改剧", "9:16",
                       [("p1.png", PNG), ("p2.png", PNG), ("p3.png", PNG)],
                       comic_mode="film_adaptation")["id"]
    replies = iter([
        "继父：「来拍照」继父举起相机，旁白：「温馨的一天」随后前夫问道：「谁更厉害？」",
        "继父：「看这里」镜头推近，旁白：「纪念照定格」",
        "继父微笑，画面渐暗，女性：「你好」",
    ])

    class FakeVision(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "v")
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            return next(replies), Usage(10, 20)

    describe_shots(db, tmp_path / "data", pid, FakeVision())
    from comic_studio.engine.assets import list_project_assets
    names = {a["name"] for a in list_project_assets(db, pid) if a["kind"] == "character"}
    assert names == {"继父"}  # 旁白（黑名单）/随后前夫问道（>4字）/女性（仅1次）全滤掉


def test_persist_assets_purges_ghost_dir(tmp_path):
    """id 复用防幽灵图（2026-08-29 真机：删行/回滚后 id 复用，新资产继承旧目录残留图）。"""
    from types import SimpleNamespace as NS
    from comic_studio.engine.assets import persist_assets
    from comic_studio.engine.projects import create_project
    db = Database(tmp_path / "s.db"); db.migrate()
    data = tmp_path / "data"; data.mkdir()
    pid = create_project(db, data, "幽灵剧", "9:16", "占位文本")["id"]
    # 预埋幽灵：下一个资产 id=1 的目录里残留旧项目的图
    ghost = data / "library" / "characters" / "1"
    (ghost / "views").mkdir(parents=True)
    (ghost / "main.png").write_bytes(PNG)
    (ghost / "views" / "sheet.png").write_bytes(PNG)
    persist_assets(db, data, pid, NS(
        characters=[NS(name="新角色", appearance="男，短发", tags=[])], scenes=[], props=[]))
    assert not (ghost / "main.png").exists()
    assert not (ghost / "views" / "sheet.png").exists()
    assert (ghost / "meta.json").exists()  # 新元数据就位


def test_purge_comic_assets_cleans_rows_dirs_and_bindings(tmp_path):
    """清理工具：动态漫误提取善后——删资产行 + library 目录 + 分镜 ledger 绑定。"""
    import json
    from types import SimpleNamespace as NS
    from comic_studio.engine.assets import persist_assets, list_project_assets
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.comic import purge_comic_assets
    from comic_studio.engine.projects import create_project
    db = Database(tmp_path / "s.db"); db.migrate()
    data = tmp_path / "data"; data.mkdir()
    pid = create_project(db, data, "清理剧", "9:16", "占位文本")["id"]
    persist_assets(db, data, pid, NS(
        characters=[NS(name="旁白", appearance="垃圾", tags=["comic"])], scenes=[], props=[]))
    a = list_project_assets(db, pid)[0]
    lib_dir = data / "library" / "characters" / str(a["id"])
    assert (lib_dir / "meta.json").exists()
    conn = db.connect()
    conn.execute(
        "INSERT INTO shots (project_id, seq, text_span, ledger_json) VALUES (?,?,?,?)",
        (pid, 1, "t", json.dumps({"assets": {"characters": [a["id"]]}, "dialogue": []})))
    conn.commit()

    n = purge_comic_assets(db, data, pid)
    assert n == 1
    assert list_project_assets(db, pid) == []  # 行没了
    assert not lib_dir.exists()  # 目录没了
    ledger = json.loads(conn.execute(
        "SELECT ledger_json FROM shots WHERE project_id=?", (pid,)).fetchone()[0])
    assert not (ledger.get("assets") or {}).get("characters")  # 绑定清了
    assert "dialogue" in ledger  # 对白等其他字段不动


def test_from_comic_api(tmp_path):
    with TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data",
                               start_workers=False)) as c:
        r = c.post("/api/projects/from-comic",
                   data={"name": "接口剧", "aspect_ratio": "16:9"},
                   files=[("images", ("a.png", io.BytesIO(PNG), "image/png")),
                          ("images", ("b.png", io.BytesIO(PNG * 2), "image/png"))])
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        r2 = c.get(f"/api/projects/{pid}/shots")
        assert len(r2.json()) == 2


def test_from_comic_api_with_style(tmp_path):
    """漫画 tab 画风随创建提交（2026-09-06）：漫改模式创建即带画风——
    describe_shots 漫改分支的画风转换指令与参考图 genref 都消费它。"""
    with TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data",
                               start_workers=False)) as c:
        r = c.post("/api/projects/from-comic",
                   data={"name": "漫改画风剧", "aspect_ratio": "16:9",
                         "comic_mode": "film_adaptation",
                         "style": "写实风格，电影质感，真实皮肤与材质细节",
                         "style_vis": "写实风格，电影质感"},
                   files=[("images", ("a.png", io.BytesIO(PNG), "image/png"))])
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["style"] == "写实风格，电影质感，真实皮肤与材质细节"
        assert body["style_vis"] == "写实风格，电影质感"


def test_describe_shots_motion_uses_integrated_format_and_heals(tmp_path):
    """动态漫读图提示词（2026-08-31 定稿：H3 六模块骨架中文输出）：
    system 要求六模块 + VOICES；落库前过 heal（缺音频节机械补）。"""
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.comic import import_comic, describe_shots
    from comic_studio.engine.llm.provider import LLMClient, Usage
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = import_comic(db, tmp_path / "data", "格式剧", "9:16",
                       [("p1.png", PNG), ("p2.png", PNG)])["id"]
    seen = []

    class FakeVision(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "v")
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            seen.append(messages)
            return ("subject_definitions:\n少年 是本镜画面中的人物\nsummary:\n少年推门。\n"
                    "retention_analysis:\n少年：fully_preserved - 保持黑发\n"
                    "detailed_description:\n[Shot 1] 少年推开门走进房间，镜头缓推，"
                    "他说：「我回来了。」"), Usage(10, 20)

    describe_shots(db, tmp_path / "data", pid, FakeVision())
    system = seen[0][0]["content"]
    for sec in ("subject_definitions:", "summary:", "retention_analysis:",
                "detailed_description:", "overall_soundscape:", "non_diegetic_music"):
        assert sec in system, sec
    assert "overall_soundscape" in system and "non_diegetic_music" in system and "N/A" in system
    from comic_studio.engine.shots import list_shots
    for s in list_shots(db, pid):
        assert "detailed_description:" in s["prompt"]
        assert "无字幕" in s["prompt"]


def test_describe_shots_film_uses_skeleton_and_heals(tmp_path):
    """漫改读图提示词升级：subject_definitions 骨架（用户实测全能参考格式）+ heal。"""
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.comic import import_comic, describe_shots
    from comic_studio.engine.llm.provider import LLMClient, Usage
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = import_comic(db, tmp_path / "data", "漫改格式剧", "9:16",
                       [("p1.png", PNG)], comic_mode="film_adaptation")["id"]
    seen = []

    class FakeVision(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "v")
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            seen.append(messages)
            return ("subject_definitions: 雪是来自 <Picture 1> 的少女。\n"
                    "summary: 雪在庭院挥剑。\n"
                    "detailed_description: 庭院雪景，雪挥剑转身。"), Usage(10, 20)

    describe_shots(db, tmp_path / "data", pid, FakeVision())
    system = seen[0][0]["content"]
    assert "subject_definitions" in system and "detailed_description" in system
    assert "overall_soundscape" in system and "N/A" in system
    from comic_studio.engine.shots import list_shots
    for s in list_shots(db, pid):
        assert "无字幕" in s["prompt"]   # 2026-08-31 统一语言：E 式 heal


def test_describe_motion_builds_speaker_assets_with_voices(tmp_path):
    """动态漫角色音色（2026-08-31）：VOICES 尾行标注 → 对白聚合建角色（旁白过滤）→ 绑音色。"""
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.comic import import_comic, describe_shots
    from comic_studio.engine.llm.provider import LLMClient, Usage
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = import_comic(db, tmp_path / "data", "动态漫音色剧", "9:16",
                       [("p1.png", PNG), ("p2.png", PNG)])["id"]
    seen = []

    class FakeVision(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "v")
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            seen.append(messages)
            reply = ("integrated_multimodal_description: [Shot 1] 小雪说话。"
                     "小雪：「哥哥你回来啦。」旁白：「夜色渐深。」\n"
                     "overall_soundscape: 无对白。\nnon_diegetic_music: N/A\n"
                     'VOICES:{"voices":[{"name":"小雪","gender":"女","age":8,"voice":"萝莉"}]}')
            return reply, Usage(10, 20)

    n = describe_shots(db, tmp_path / "data", pid, FakeVision())
    assert n == 2
    from comic_studio.engine.assets import list_project_assets
    from comic_studio.engine.shots import list_shots
    chars = [a for a in list_project_assets(db, pid) if a["kind"] == "character"]
    assert [c["name"] for c in chars] == ["小雪"]      # 旁白被过滤
    assert chars[0]["voice"] == "萝莉"
    for s in list_shots(db, pid):                       # VOICES 行不进提示词
        assert "VOICES:" not in (s["prompt"] or "")
    assert "可用音色库" in seen[0][0]["content"]
    describe_shots(db, tmp_path / "data", pid, FakeVision(),
                   shot_id=list_shots(db, pid)[0]["id"])
    assert len([a for a in list_project_assets(db, pid)
                if a["kind"] == "character"]) == 1       # 幂等不重复建


def test_extract_comic_matches_voices(tmp_path):
    """漫改资产音色（2026-08-31）：VLM 提取含 suggested_voice + 音色库注入系统词；
    建资产后自动绑定（非法建议走性别×年龄基线）。"""
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.comic import import_comic, extract_comic_characters
    from comic_studio.engine.llm.provider import LLMClient, Usage
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = import_comic(db, tmp_path / "data", "漫改音色剧", "9:16",
                       [("p1.png", PNG)], comic_mode="film_adaptation")["id"]
    seen = []

    class FakeVLM(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "v")
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            seen.append(messages)
            return ('{"characters":['
                    '{"name":"小雪","appearance":"性别：女\\n年龄：8岁\\n服装：红裙",'
                    '"suggested_voice":"萝莉"},'
                    '{"name":"老仆","appearance":"性别：男\\n年龄：62岁\\n服装：灰袍",'
                    '"suggested_voice":"乱写的"}],'
                    '"scenes":[],"props":[]}'), Usage(1, 1)

    n = extract_comic_characters(db, tmp_path / "data", pid, FakeVLM())
    assert n == 2
    assert "可用音色库" in seen[0][0]["content"]      # 音色库已注入
    from comic_studio.engine.assets import list_project_assets
    by = {a["name"]: a["voice"] for a in list_project_assets(db, pid)
          if a["kind"] == "character"}
    assert by["小雪"] == "萝莉"                        # 建议命中
    assert by["老仆"] == "老年男声"                    # 非法建议→基线兜底


def test_motion_group_speakers_not_built_as_assets(tmp_path):
    """R6：动态漫对白聚合——群体称谓（众人/…们/观众）不建角色不绑音色。"""
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.comic import import_comic, describe_shots
    from comic_studio.engine.llm.provider import LLMClient, Usage
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = import_comic(db, tmp_path / "data", "群杂剧", "9:16",
                       [("p1.png", PNG), ("p2.png", PNG)])["id"]

    class FakeVision(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "v")
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            return "林战：「冲锋」众人：「杀——」士兵们紧随其后。", Usage(10, 20)

    describe_shots(db, tmp_path / "data", pid, FakeVision())
    from comic_studio.engine.assets import list_project_assets
    names = {a["name"] for a in list_project_assets(db, pid) if a["kind"] == "character"}
    assert names == {"林战"}  # 众人/士兵们不建


def test_film_group_nouns_not_extracted(tmp_path):
    """R6：漫改提取——群体称谓即使出现 ≥2 次也不得建角色。"""
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.comic import import_comic, describe_shots
    from comic_studio.engine.llm.provider import LLMClient, Usage
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = import_comic(db, tmp_path / "data", "漫改群杂", "9:16",
                       [("p1.png", PNG), ("p2.png", PNG), ("p3.png", PNG)],
                       comic_mode="film_adaptation")["id"]
    replies = iter(["众人：「哇」继父：「来拍照」", "众人：「好看」继父：「别动」",
                    "继父微笑，画面渐暗"])

    class FakeVision(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "v")
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            return next(replies), Usage(10, 20)

    describe_shots(db, tmp_path / "data", pid, FakeVision())
    from comic_studio.engine.assets import list_project_assets
    names = {a["name"] for a in list_project_assets(db, pid) if a["kind"] == "character"}
    assert names == {"继父"}  # 众人出现 2 次、2 字——旧规则会放行，R6 拦下


def test_drop_character_bindings_clears_ledger_refs(tmp_path):
    """M6（2026-09-05 审计）：重提取角色清空重建资产必须同步清分镜 ledger
    旧 id 绑定（此前悬空 → 渲染参考解析落空）。共用助手单测。"""
    from types import SimpleNamespace as NS
    import json as _json
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.comic import _drop_character_bindings
    from comic_studio.engine.db import Database
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.shots import list_shots, persist_shots
    from comic_studio.engine.assets import persist_assets
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "重建剧", "16:9", "t")["id"]
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="甲", appearance="黑发", tags=[]),
                                  NS(name="乙", appearance="白发", tags=[])],
                      scenes=[], props=[]))
    from comic_studio.engine.assets import list_project_assets
    aids = [a["id"] for a in list_project_assets(db, pid)]
    rows = persist_shots(db, pid, [
        NS(text_span="", description="a", shot_type="", camera={}, duration=5.0,
           workflow_type="t2v", ledger={},
           character_ids=[], scene_ids=[], prop_ids=[], depends_on=None),
        NS(text_span="", description="b", shot_type="", camera={}, duration=5.0,
           workflow_type="t2v", ledger={},
           character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)])
    conn = db.connect()
    for sid, chars in zip(rows, [aids, [aids[1]]]):
        conn.execute("UPDATE shots SET ledger_json=? WHERE id=?",
                     (_json.dumps({"assets": {"characters": chars}},
                                  ensure_ascii=False), sid))
    conn.commit()
    _drop_character_bindings(db, pid, {aids[0]})  # 只删资产1
    leds = [_json.loads(s["ledger_json"]).get("assets", {}).get("characters")
            for s in list_shots(db, pid)]
    assert leds[0] == [aids[1]] and leds[1] == [aids[1]]  # 引用者清、无关者留


def test_describe_batch_includes_stale_and_resets_status(tmp_path):
    """QC-A（2026-09-05 复审回归）：批量模式跳过「已有提示词」的镜但 stale
    镜恰有旧提示词，且写 prompt 不复位 status → autopilot 无限重入 describe。
    修复：stale 镜纳入批量、成功写 status='ready'。"""
    import json as _json
    from types import SimpleNamespace as NS
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.comic import describe_shots
    from comic_studio.engine.db import Database
    from comic_studio.engine.paths import data_to_abs
    from comic_studio.engine.projects import create_project, set_stage
    from comic_studio.engine.shots import list_shots, persist_shots
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "回归剧", "16:9", "t",
                         comic_mode="motion_comic")["id"]
    set_stage(db, pid, "storyboard_ready")
    sid = persist_shots(db, pid, [
        NS(text_span="", description="旧", shot_type="", camera={}, duration=5.0,
           workflow_type="fl2v", ledger={}, character_ids=[], scene_ids=[],
           prop_ids=[], depends_on=None, prompt="旧提示词")])[0]
    from comic_studio.engine.shots import update_shot
    update_shot(db, sid, {"status": "stale"})   # persist 白名单外的状态走 update
    d = data_to_abs(tmp_path / "data", "projects/回归剧/shots/1")
    d.mkdir(parents=True)
    (d / "kf_start.png").write_bytes(b"\x89PNG")

    class FakeVLM:
        model = "fake"
        def raw_chat(self, messages, temperature=0.4):
            return ("林凡踏前一步。subject_definitions:\n林凡 是来自 <Picture 1> 的人物\n"
                    "overall_soundscape: 无对白、无哼唱\nnon_diegetic_music: N/A", {})
    n = describe_shots(db, tmp_path / "data", pid, FakeVLM())
    assert n == 1, "stale 镜必须纳入批量重生"
    row = list_shots(db, pid)[0]
    assert row["status"] == "ready" and "林凡" in row["prompt"]


# ===== P11 漫改质量批（2026-09-05 用户四连报 + 时长硬编码）=====

def _png_bytes():
    return b"\x89PNG\r\n\x1a\n" + b"0" * 32


def test_import_comic_respects_durations(tmp_path):
    """P11-⑤：import_comic 此前硬编码 duration=5.0——段时长/总时长字段形同虚设。"""
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.comic import import_comic
    from comic_studio.engine.shots import list_shots
    db = Database(tmp_path / "s.db"); db.migrate()
    p1 = import_comic(db, tmp_path / "d", "段剧", "16:9", [("p.png", _png_bytes())],
                      default_shot_duration=7)
    assert [s["duration"] for s in list_shots(db, p1["id"])] == [7.0]
    p2 = import_comic(db, tmp_path / "d", "总剧", "16:9",
                      [("p.png", _png_bytes())] * 3, target_duration=12)
    assert [s["duration"] for s in list_shots(db, p2["id"])] == [4.0, 4.0, 4.0]
    p3 = import_comic(db, tmp_path / "d", "默剧", "16:9", [("p.png", _png_bytes())])
    assert [s["duration"] for s in list_shots(db, p3["id"])] == [5.0]


def test_describe_shots_force_overrides_existing(tmp_path):
    """强制重读（2026-09-06）：批量模式默认跳过已有提示词的镜（stale 除外），
    force=True 不跳——改画风/换模型后整批覆盖重生成，时长同步重估。"""
    import math
    from comic_studio.engine.comic import import_comic, describe_shots
    from comic_studio.engine.llm.provider import LLMClient, Usage
    from comic_studio.engine.shots import list_shots
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = import_comic(db, tmp_path / "data", "强制重读剧", "9:16",
                       [("p1.png", PNG), ("p2.png", PNG)])["id"]

    class FakeA(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "v")
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            return "少年推开门走进房间。", Usage(10, 20)

    line = "你休想从这里逃出去，今天就是你的死期，认命吧。"
    class FakeB(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "v")
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            return f"林晨：「{line}」", Usage(10, 20)

    assert describe_shots(db, tmp_path / "data", pid, FakeA()) == 2
    # 默认批量：已有提示词的镜全跳过（增量语义不变）
    assert describe_shots(db, tmp_path / "data", pid, FakeB()) == 0
    assert all("少年推开门" in s["prompt"] for s in list_shots(db, pid))
    # force=True：全覆盖 + 对白时长重估
    assert describe_shots(db, tmp_path / "data", pid, FakeB(), force=True) == 2
    rows = list_shots(db, pid)
    assert all("休想从这里逃出去" in s["prompt"] for s in rows)
    exp = min(15.0, max(4.0, math.ceil(len(line) / 4.0)))
    assert [s["duration"] for s in rows] == [exp, exp]


def test_describe_force_api_payload_and_handler(tmp_path, monkeypatch):
    """force 全链路：路由 ?force=true → job payload → handler 透传引擎
    （handler 直接调用，client_for_task 换 Fake——不触网）。"""
    import json as _json
    from comic_studio.engine.db import Database
    from comic_studio.engine.llm.provider import LLMClient, Usage
    from comic_studio.engine.shots import list_shots, update_shot
    with TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data",
                               start_workers=False)) as c:
        r = c.post("/api/projects/from-comic",
                   data={"name": "force链路剧", "aspect_ratio": "16:9"},
                   files=[("images", ("a.png", io.BytesIO(PNG), "image/png"))])
        pid = r.json()["id"]
        db = Database(tmp_path / "t.db")  # 第二连接（WAL 并存）
        sid = list_shots(db, pid)[0]["id"]
        update_shot(db, sid, {"prompt": "旧提示词", "description": "旧", "status": "ready"})

        r2 = c.post(f"/api/projects/{pid}/describe-shots?force=true")
        assert r2.status_code == 202, r2.text
        job = db.connect().execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
        assert _json.loads(job["payload_json"]).get("force") is True

        class FakeB(LLMClient):
            def __init__(self):
                super().__init__("http://x", "k", "v")
            def raw_chat(self, messages, temperature=0.3, max_tokens=None):
                return "林晨：「站住，你哪里跑！」", Usage(10, 20)

        monkeypatch.setattr("comic_studio.engine.llm.provider.client_for_task",
                            lambda db, task: FakeB())
        from comic_studio.engine import pipeline_jobs
        pipeline_jobs.handle_describe_shots(db, tmp_path / "data", job, None)
        s = list_shots(db, pid)[0]
        assert "旧提示词" not in (s["prompt"] or "")
        assert "站住" in s["prompt"]


def test_describe_shots_reestimates_durations(tmp_path):
    """漫画链智能估时（2026-09-06）：段时长/总时长都为 0 时，读图产出对白后
    按字数基准重估（⌈字数/4⌉+0.6×(句数-1) 钳 4~15，与小说链 B1 同式）——
    「段时长留 0」在小说链=LLM 估时、漫画链此前却落死值 5.0，语义不一致。
    显式段时长与无对白镜不覆盖。"""
    import math
    from comic_studio.engine.comic import import_comic, describe_shots
    from comic_studio.engine.llm.provider import LLMClient, Usage
    from comic_studio.engine.shots import list_shots
    db = Database(tmp_path / "s.db"); db.migrate()

    class FakeDlg(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "v")
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            return "林晨：「站住！」林晨：「你休想从这里逃出去，今天就是你的死期。」", Usage(10, 20)

    class FakeSilent(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "v")
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            return "少年推开门走进房间，环顾四周。", Usage(10, 20)

    # ① 0/0 + 有对白：2 句共 22 字 → ⌈22/4⌉+0.6（不再是占位 5.0）
    expected = min(15.0, max(4.0, math.ceil(22 / 4.0) + 0.6))
    pid1 = import_comic(db, tmp_path / "data", "估时剧", "9:16",
                        [("p1.png", PNG), ("p2.png", PNG)])["id"]
    describe_shots(db, tmp_path / "data", pid1, FakeDlg())
    assert [s["duration"] for s in list_shots(db, pid1)] == [expected, expected]
    # ② 显式段时长 6：读图后不覆盖（P11-⑤ 语义保留）
    pid2 = import_comic(db, tmp_path / "data", "段时剧", "9:16",
                        [("p1.png", PNG)], default_shot_duration=6)["id"]
    describe_shots(db, tmp_path / "data", pid2, FakeDlg())
    assert [s["duration"] for s in list_shots(db, pid2)] == [6.0]
    # ③ 0/0 无对白：维持占位 5.0
    pid3 = import_comic(db, tmp_path / "data", "无对白剧", "9:16",
                        [("p1.png", PNG)])["id"]
    describe_shots(db, tmp_path / "data", pid3, FakeSilent())
    assert [s["duration"] for s in list_shots(db, pid3)] == [5.0]


def test_film_describe_voices_gender_and_bind(tmp_path):
    """P11-①④：漫改 system 注入 VOICES 尾行 → 性别年龄落资产外貌 + 音色自动绑。"""
    from types import SimpleNamespace as NS
    from comic_studio.engine.db import Database
    from comic_studio.engine.projects import create_project, set_stage
    from comic_studio.engine.comic import describe_shots
    from comic_studio.engine.assets import list_project_assets
    from comic_studio.engine.paths import data_to_abs
    from comic_studio.engine.projects import create_project, set_stage
    from comic_studio.engine.shots import persist_shots
    captured = {}

    class FakeVLM:
        model = "fake"
        def raw_chat(self, messages, temperature=0.4):
            captured["system"] = messages[0]["content"]
            return ("subject_definitions:\n妻子 是来自 第 1 格 的人物，其外观由该图提供\n"
                    "summary:一句话：本镜核心内容与运镜\n"
                    "overall_soundscape:无对白、无哼唱，仅环境声\n"
                    "non_diegetic_music: N/A\n"
                    'VOICES:{"voices":[{"name":"妻子","gender":"女","age":28,"voice":"元气少女"}]}', {})
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "d", "音剧", "16:9", "t",
                         comic_mode="film_adaptation")["id"]
    from comic_studio.engine.assets import persist_assets
    persist_assets(db, tmp_path / "d", pid,   # 名册注入前提：已有角色
                   NS(characters=[NS(name="妻子", appearance="性别：女", tags=[])],
                      scenes=[], props=[]))
    set_stage(db, pid, "storyboard_ready")
    persist_shots(db, pid, [NS(text_span="", description="d", shot_type="",
        camera={}, duration=5.0, workflow_type="ref2va", ledger={},
        character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)])
    d = data_to_abs(tmp_path / "d", "projects/音剧/shots/1")
    d.mkdir(parents=True)
    (d / "kf_start.png").write_bytes(_png_bytes())
    describe_shots(db, tmp_path / "d", pid, FakeVLM())
    assert "VOICES:" in captured["system"] and "音色库" in captured["system"]  # ①
    assert "已有角色名册" in captured["system"]                                # ②名册注入
    chars = [a for a in list_project_assets(db, pid) if a["kind"] == "character"]
    assert chars and "女" in chars[0]["appearance_json"]                      # 性别落库
    assert chars[0]["voice"]                                                   # ④音色自动匹配


def test_extract_merges_name_variants(tmp_path):
    """P11-②：包含式归一——「新婚妻子」并入已有「妻子」，提示词同步替换。"""
    from types import SimpleNamespace as NS
    from comic_studio.engine.db import Database
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.assets import persist_assets, list_project_assets
    from comic_studio.engine.comic import _extract_characters_from_prompts
    from comic_studio.engine.shots import list_shots, persist_shots, update_shot
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "d", "归剧", "16:9", "t",
                         comic_mode="film_adaptation")["id"]
    persist_assets(db, tmp_path / "d", pid,
                   NS(characters=[NS(name="妻子", appearance="性别：女", tags=[])],
                      scenes=[], props=[]))
    sids = persist_shots(db, pid, [
        NS(text_span="", description="d", shot_type="", camera={}, duration=5.0,
           workflow_type="ref2va", ledger={}, character_ids=[], scene_ids=[],
           prop_ids=[], depends_on=None) for _ in range(2)])
    for i, sid in enumerate(sids):
        update_shot(db, sid, {"prompt": f"正文{i}。新婚妻子：「你来看看吧」"})
    n = _extract_characters_from_prompts(db, tmp_path / "d", pid, {})
    names = {a["name"] for a in list_project_assets(db, pid)
             if a["kind"] == "character"}
    assert "新婚妻子" not in names and n == 0            # 变体不建新资产
    prompts = [s["prompt"] for s in list_shots(db, pid)]
    assert all("妻子：「" in p and "新婚妻子" not in p for p in prompts)  # 归一替换


def test_anchor_subject_definitions_by_binding(tmp_path):
    """P11-③：按绑定顺序把 subject_definitions 重写为 <Picture N> 锚定——
    ref2va 多参考身份生效；未绑定镜不动。"""
    from types import SimpleNamespace as NS
    from comic_studio.engine.db import Database
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.assets import persist_assets, list_project_assets
    from comic_studio.engine.comic import _anchor_subject_definitions
    from comic_studio.engine.shots import list_shots, persist_shots, update_shot
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "d", "锚剧", "16:9", "t",
                         comic_mode="film_adaptation")["id"]
    persist_assets(db, tmp_path / "d", pid,
                   NS(characters=[NS(name="妻子", appearance="x", tags=[]),
                                  NS(name="丈夫", appearance="y", tags=[])],
                      scenes=[], props=[]))
    aids = [a["id"] for a in list_project_assets(db, pid)
            if a["kind"] == "character"]
    PROMPT = ("subject_definitions:\n妻子 是来自 第 2 格 的人物，其外观由该图提供\n"
              "岳母 是来自 第 2 格 右上角小图的背景人物\n"
              "summary:一句话：本镜核心内容\n"
              "overall_soundscape:无对白、无哼唱\nnon_diegetic_music: N/A")
    sids = persist_shots(db, pid, [
        NS(text_span="", description="d", shot_type="", camera={}, duration=5.0,
           workflow_type="ref2va", ledger={},
           character_ids=aids, scene_ids=[], prop_ids=[], depends_on=None),
        NS(text_span="", description="d2", shot_type="", camera={}, duration=5.0,
           workflow_type="ref2va", ledger={}, character_ids=[], scene_ids=[],
           prop_ids=[], depends_on=None)])
    update_shot(db, sids[0], {"prompt": PROMPT})
    update_shot(db, sids[1], {"prompt": PROMPT})
    n = _anchor_subject_definitions(db, pid)
    shots = list_shots(db, pid)
    p0 = shots[0]["prompt"]
    assert "妻子 是来自 <Picture 1> 的人物" in p0
    assert "丈夫 是来自 <Picture 2> 的人物" in p0
    assert "第 2 格" not in p0 and "岳母 是来自" not in p0   # 旧格引用清除
    assert "summary:一句话：本镜核心内容" in p0               # 其余节保留
    assert n == 1
    assert shots[1]["prompt"] == PROMPT                        # 未绑定镜不动


def test_extract_dialogue_rejects_scaffold_speakers():
    """2026-09-06 manga7 真机：28 个"角色"过半是脚手架短语——对白顺序/
    undscape（soundscape 被正则截断）/画面中出现对白气泡/王叔叔轻声说。
    说话人净化：剥说话动词后缀、叙述/脚手架短语拒收、纯 ASCII 拒收；
    二轮加固（当晚实测新变体）：王叔叔开口说话/王叔叔则/上方浮现内心
    独白/白为回忆旁白/男性的手臂并开口；「旁白」本身保留（正片旁白配音）。"""
    from comic_studio.engine.comic import _extract_dialogue
    text = ("王叔叔轻声说：「小点声，别让人听见。」\n"
            "对白顺序：「无关脚手架文本」\n"
            "undscape:「garbage」\n"
            "画面中出现对白气泡：「废句」\n"
            "同时对他说：「你过来」\n"
            "静静轻哼回应：「嗯。」\n"
            "王叔叔开口说话：「第二句台词。」\n"
            "王叔叔则：「第三句。」\n"
            "上方浮现内心独白：「内心戏」\n"
            "白为回忆旁白：「回忆」\n"
            "男性的手臂并开口：「描述句」\n"
            "旁白：「旁白要保留。」")
    d = _extract_dialogue(text)
    assert [x["speaker"] for x in d] == ["王叔叔", "静静", "王叔叔", "王叔叔", "旁白"]
    assert d[0]["line"] == "小点声，别让人听见。"


def test_build_speaker_assets_merges_variants(tmp_path):
    """动态漫包含式归一（2026-09-06 manga7：妈妈/妈妈红 各建一个）——
    新说话人与已有名互为包含（双方 ≥2 字）→ 并入，不再另起变体资产。"""
    from comic_studio.engine.comic import import_comic, describe_shots
    from comic_studio.engine.llm.provider import LLMClient, Usage
    from comic_studio.engine.shots import list_shots
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = import_comic(db, tmp_path / "data", "归一剧", "9:16",
                       [("p1.png", PNG), ("p2.png", PNG)])["id"]

    class FakeV(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "v")
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            # 逐镜变体：第一镜 妈妈，第二镜 妈妈红（互为包含）
            shot_txt = {1: "妈妈：「回来啦。」", 2: "妈妈红：「快去洗手。」"}
            seq = 1 if "第 1 格" in str(messages[-1]["content"]) else 2
            return shot_txt[seq], Usage(10, 20)

    describe_shots(db, tmp_path / "data", pid, FakeV())
    from comic_studio.engine.assets import list_project_assets
    names = [a["name"] for a in list_project_assets(db, pid)
             if a["kind"] == "character"]
    assert names == ["妈妈"], names


def test_purge_covers_motion_speaker_assets(tmp_path):
    """🗑 清理提取资产覆盖动态漫 speaker 资产（2026-09-06 manga7 真机：
    tags=[] 无 comic 标记 → 清理按钮扫不到，垃圾角色清不掉）——
    motion_comic 项目的 character 资产全部可清（fl2v 用原页，资产只为配音）。"""
    from comic_studio.engine.comic import (import_comic, describe_shots,
                                           purge_comic_assets)
    from comic_studio.engine.llm.provider import LLMClient, Usage
    from comic_studio.engine.assets import list_project_assets
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = import_comic(db, tmp_path / "data", "清理剧", "9:16",
                       [("p1.png", PNG)])["id"]

    class FakeV(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "v")
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            return "林晨：「今天天气不错。」", Usage(10, 20)

    describe_shots(db, tmp_path / "data", pid, FakeV())
    assert len(list_project_assets(db, pid)) == 1  # 建了 speaker 资产
    n = purge_comic_assets(db, tmp_path / "data", pid)
    assert n == 1
    assert list_project_assets(db, pid) == []


def test_subtitles_flag_comic_default_off(tmp_path):
    """项目级字幕开关（2026-09-07 用户需求：漫画原页自带台词文字——
    动态漫/漫改默认不烧字幕；小说/有声书默认烧；PATCH 可翻）。"""
    from comic_studio.engine.projects import (create_project, get_project,
                                              subtitles_enabled)
    from comic_studio.engine.comic import import_comic
    db = Database(tmp_path / "s.db"); db.migrate()
    p1 = create_project(db, tmp_path / "data", "小说字幕剧", "16:9", "正文")["id"]
    assert get_project(db, p1)["subtitles"] == 1
    assert subtitles_enabled(get_project(db, p1)) is True
    p2 = import_comic(db, tmp_path / "data", "漫画字幕剧", "16:9",
                      [("p.png", PNG)])["id"]
    assert get_project(db, p2)["subtitles"] == 0
    assert subtitles_enabled(get_project(db, p2)) is False


def test_subtitles_patch_api(tmp_path):
    with TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data",
                               start_workers=False)) as c:
        r = c.post("/api/projects/from-comic",
                   data={"name": "字幕开关剧", "aspect_ratio": "16:9"},
                   files=[("images", ("a.png", io.BytesIO(PNG), "image/png"))])
        pid = r.json()["id"]
        r2 = c.patch(f"/api/projects/{pid}", json={"subtitles": True})
        assert r2.status_code == 200, r2.text
        from comic_studio.engine.db import Database
        db = Database(tmp_path / "t.db")
        row = db.connect().execute(
            "SELECT subtitles FROM projects WHERE id=?", (pid,)).fetchone()
        assert row["subtitles"] == 1


def test_create_routes_accept_advanced_params(tmp_path):
    """创建时高级参数（2026-09-07 用户需求：全套建时就定）——上传小说
    与漫画导入两入口接受 字幕/兆像素/倍速/质量档（from-audio/from-theme 同款透传）。"""
    with TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data",
                               start_workers=False)) as c:
        r = c.post("/api/projects", data={"name": "上传参数剧", "aspect_ratio": "16:9",
                                          "subtitles": "false", "video_megapixels": "0.8",
                                          "video_multiple": "64", "video_speed": "高质量"},
                   files={"novel": ("n.txt", io.BytesIO(("正文内容。" * 30).encode()), "text/plain")})
        assert r.status_code == 201, r.text
        from comic_studio.engine.db import Database
        db = Database(tmp_path / "t.db")
        p1 = db.connect().execute(
            "SELECT subtitles, video_megapixels, video_multiple, video_speed FROM projects WHERE id=?",
            (r.json()["id"],)).fetchone()
        assert (p1["subtitles"], p1["video_megapixels"], p1["video_multiple"], p1["video_speed"]) \
            == (0, 0.8, 64, "高质量")
        r2 = c.post("/api/projects/from-comic",
                    data={"name": "漫画参数剧", "aspect_ratio": "16:9", "subtitles": "true",
                          "video_megapixels": "0.6", "video_multiple": "16"},
                    files=[("images", ("a.png", io.BytesIO(PNG), "image/png"))])
        assert r2.status_code == 201, r2.text
        p2 = db.connect().execute(
            "SELECT subtitles, video_megapixels, video_multiple FROM projects WHERE id=?",
            (r2.json()["id"],)).fetchone()
        assert (p2["subtitles"], p2["video_megapixels"], p2["video_multiple"]) == (1, 0.6, 16)


def test_reestimate_project_durations_direct(tmp_path):
    """优化#4（2026-09-07）：只重估时长不重读图（免烧 VLM）——用户主动触发
    即覆写（不受段时长 0/0 门槛限制）；无对白镜不动。"""
    import math
    from comic_studio.engine.comic import (import_comic, describe_shots,
                                           reestimate_project_durations)
    from comic_studio.engine.llm.provider import LLMClient, Usage
    from comic_studio.engine.shots import list_shots
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = import_comic(db, tmp_path / "data", "直估剧", "9:16",
                       [("p1.png", PNG)], default_shot_duration=6)["id"]

    class FakeDlg(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "v")
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            return "林晨：「你休想从这里逃出去。」", Usage(10, 20)

    describe_shots(db, tmp_path / "data", pid, FakeDlg())  # 段时长 6：估时未动
    assert [s["duration"] for s in list_shots(db, pid)] == [6.0]
    n = reestimate_project_durations(db, pid)
    assert n == 1  # 主动重估：不受 0/0 门槛
    line = "你休想从这里逃出去。"
    exp = min(15.0, max(4.0, math.ceil(len(line) / 4.0)))
    assert [s["duration"] for s in list_shots(db, pid)] == [exp]


def test_import_comic_pages_canonical_and_versions(tmp_path):
    """原页落 pages/（单一事实源）+ kf v1 版本文件 + 活动拷贝；旧项目无 pages 时回落。"""
    from comic_studio.engine.db import Database
    from comic_studio.engine.comic import import_comic, page_source_paths
    from comic_studio.engine.paths import data_to_abs
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = import_comic(db, tmp_path / "data", "原页剧", "9:16",
                       [("p1.png", PNG), ("p2.png", PNG * 2)])["id"]
    from comic_studio.engine.projects import get_project
    slug = get_project(db, pid)["slug"]
    pages = data_to_abs(tmp_path / "data", f"projects/{slug}/pages")
    assert (pages / "page_001.png").read_bytes() == PNG
    assert (pages / "page_002.png").read_bytes() == PNG * 2
    d1 = data_to_abs(tmp_path / "data", f"projects/{slug}/shots/1")
    assert (d1 / "kf_start_v1.png").read_bytes() == PNG
    assert (d1 / "kf_start.png").read_bytes() == PNG          # 活动拷贝
    assert (d1 / "kf_end_v1.png").read_bytes() == PNG * 2     # 尾帧=下镜首帧
    assert (d1 / "kf_end.png").read_bytes() == PNG * 2
    # page_source_paths：原页优先；末镜 end 为不存在路径（由调用方判 exists）
    s1, e1 = page_source_paths(tmp_path / "data", slug, 1)
    assert s1.name == "page_001.png" and e1.name == "page_002.png"
    # 旧项目回落：删掉 pages 目录 → 返回 kf 活动文件
    import shutil; shutil.rmtree(pages)
    s1b, _ = page_source_paths(tmp_path / "data", slug, 1)
    assert s1b.name == "kf_start.png"


def test_speaker_blacklist_from_settings(tmp_path):
    """优化#6（2026-09-07）：净化词表外置——settings llm.speaker_blacklist
    追加黑名单（逗号分隔），免发版加词。"""
    from comic_studio.engine.comic import import_comic, describe_shots
    from comic_studio.engine.llm.provider import LLMClient, Usage
    from comic_studio.engine.settings import set_setting
    from comic_studio.engine.shots import list_shots
    import json
    db = Database(tmp_path / "s.db"); db.migrate()
    set_setting(db, "speaker_blacklist", "新废词")
    pid = import_comic(db, tmp_path / "data", "黑名单剧", "9:16",
                       [("p1.png", PNG)])["id"]

    class FakeV(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "v")
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            return "新废词：「这句不该被收。」王叔叔：「正常台词。」", Usage(10, 20)

    describe_shots(db, tmp_path / "data", pid, FakeV())
    led = json.loads(list_shots(db, pid)[0]["ledger_json"] or "{}")
    assert [d["speaker"] for d in led["dialogue"]] == ["王叔叔"]


def test_describe_shots_reads_original_pages_not_active(tmp_path):
    """决策 16：读原页不读活动 kf——重绘覆盖 kf 后 force 重读对白不丢。"""
    from comic_studio.engine.db import Database
    from comic_studio.engine.comic import import_comic, describe_shots
    from comic_studio.engine.llm.provider import LLMClient, Usage
    from comic_studio.engine.paths import data_to_abs
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = import_comic(db, tmp_path / "data", "原页读图", "9:16",
                       [("p1.png", PNG), ("p2.png", PNG * 2)])["id"]
    slug = "原页读图"
    # 模拟重绘覆盖活动 kf（无文字的图）；原页仍在 pages/
    d1 = data_to_abs(tmp_path / "data", f"projects/{slug}/shots/1")
    (d1 / "kf_start.png").write_bytes(b"REDAWN")
    seen = []

    class FakeVLM(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "m")
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            seen.append(messages)
            return "summary:过渡\n对白：「你好」", Usage(1, 1)

    describe_shots(db, tmp_path / "data", pid, FakeVLM(), force=True)
    import base64, json as _json
    sent = seen[0][1]["content"][1]["image_url"]["url"]
    assert base64.b64decode(sent.split(",", 1)[1]) == PNG   # 读的是原页字节


def test_describe_shots_skips_speaker_assets_when_redraw(tmp_path):
    """决策 5：重绘模式不建 speaker 资产（提取 job 是唯一建资产入口）。"""
    from comic_studio.engine.db import Database
    from comic_studio.engine.comic import import_comic, describe_shots
    from comic_studio.engine.llm.provider import LLMClient, Usage
    from comic_studio.engine.assets import list_project_assets
    db = Database(tmp_path / "s.db"); db.migrate()
    pid_redraw = import_comic(db, tmp_path / "data", "重绘不建", "9:16",
                              [("p1.png", PNG), ("p2.png", PNG)],
                              redraw_characters=1)["id"]
    pid_plain = import_comic(db, tmp_path / "data", "普通建", "9:16",
                             [("p1.png", PNG), ("p2.png", PNG)])["id"]

    class FakeVLM(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "m")
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            return "summary:x\n小明：「你好」", Usage(1, 1)

    describe_shots(db, tmp_path / "data", pid_redraw, FakeVLM())
    describe_shots(db, tmp_path / "data", pid_plain, FakeVLM())
    assert list_project_assets(db, pid_redraw) == []
    assert any(a["name"] == "小明" for a in list_project_assets(db, pid_plain))


def test_extract_comic_characters_main_only_and_bind(tmp_path):
    """决策 7：主要角色约束；characters_only 不建场景/道具；bind_shots 绑分镜。"""
    from comic_studio.engine.comic import import_comic, extract_comic_characters
    from comic_studio.engine.llm.provider import LLMClient, Usage
    from comic_studio.engine.assets import list_project_assets
    from comic_studio.engine.shots import list_shots
    import json as _json
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = import_comic(db, tmp_path / "data", "提取主", "9:16",
                       [("p1.png", PNG), ("p2.png", PNG)], redraw_characters=1)["id"]

    class FakeVLM(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "m")
            self.system = ""
        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            self.system = messages[0]["content"]
            return _json.dumps({
                "characters": [{"name": "小明", "appearance": "性别：男\n年龄：12岁",
                                "suggested_voice": ""}],
                "scenes": [{"name": "教室", "appearance": "明亮"}],
                "props": [{"name": "书包", "appearance": "红色"}]}), Usage(1, 1)

    c = FakeVLM()
    n = extract_comic_characters(db, tmp_path / "data", pid, c,
                                 characters_only=True, bind_shots=True)
    assert n == 1   # 只计角色
    kinds = {a["kind"] for a in list_project_assets(db, pid)}
    assert kinds == {"character"}                 # 场景/道具未建
    assert "主要角色" in c.system and "背景" in c.system  # prompt 约束注入
    # bind_shots：分镜 ledger 绑定小明（提示词含名字才会绑——先造提示词）
    from comic_studio.engine.shots import update_shot
    update_shot(db, list_shots(db, pid)[0]["id"],
                {"prompt": "小明：「你好」", "description": "小明：「你好」"})
    n2 = extract_comic_characters(db, tmp_path / "data", pid, c,
                                  characters_only=True, bind_shots=True)
    led = _json.loads(list_shots(db, pid)[0]["ledger_json"])
    aids = sorted(a["id"] for a in list_project_assets(db, pid))
    assert led.get("assets", {}).get("characters") == aids


def test_voices_tail_roster_carries_appearance_hint(tmp_path):
    """2026-09-10 四主角拆分：名册附外貌提示——两位男性只给名字 VLM 对不上号
    （729 镜描述 0 处区分命名），提示取外貌行首个有效值。"""
    from comic_studio.engine.db import Database
    from comic_studio.engine.comic import _voices_tail
    from comic_studio.engine.projects import create_project
    from types import SimpleNamespace as NS
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "名册剧", "9:16", "正文" * 60)["id"]
    from comic_studio.engine.assets import persist_assets
    persist_assets(db, tmp_path / "data", pid, NS(characters=[
        NS(name="黄毛男1", appearance="性别：男\n发色发型：金色短发", tags=[]),
        NS(name="怨主男2", appearance="性别：男\n发色发型：黑发戴眼镜", tags=[]),
    ], scenes=[], props=[]))
    tail = _voices_tail(db, tmp_path / "data", pid)
    assert "黄毛男1（金色短发）" in tail
    assert "怨主男2（黑发戴眼镜）" in tail
    assert "按外貌提示对号入座" in tail
