# tests/test_comic.py
"""P8 漫画→视频（2026-08-29）：每图一镜复用 fl2v 链路（页 i=首帧/页 i+1=尾帧）。"""
import io

from fastapi.testclient import TestClient

from comic_studio.engine.db import Database
from comic_studio.web.app import create_app

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64  # 假 PNG 字节（导入不解码，原样落盘）


def test_import_comic_creates_fl2v_shots(tmp_path):
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


def test_describe_shots_motion_uses_integrated_format_and_heals(tmp_path):
    """动态漫读图提示词（2026-08-31 定稿：H3 六模块骨架中文输出）：
    system 要求六模块 + VOICES；落库前过 heal（缺音频节机械补）。"""
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
