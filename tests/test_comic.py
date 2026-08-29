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
