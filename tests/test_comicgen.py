# tests/test_comicgen.py
"""小说转漫画（2026-09-12）：第五种项目类型。"""
from comic_studio.engine.db import Database

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _comic_project(tmp_path, **kw):
    from comic_studio.engine.projects import create_project
    db = Database(tmp_path / "s.db"); db.migrate()
    defaults = dict(comic_mode="comic_output", dialogue_mode="bubble",
                    target_pages=0, image_size="1024x1536",
                    quality_tier="standard")
    defaults.update(kw)
    return db, create_project(db, tmp_path / "data", "漫画剧", "16:9",
                               "正文" * 100, **defaults)["id"]


def test_migration_36_comic_columns(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    conn = db.connect()
    cols = {c[1] for c in conn.execute("PRAGMA table_info(projects)")}
    assert {"dialogue_mode", "target_pages", "image_size", "quality_tier"} <= cols


def test_comic_project_created(tmp_path):
    db, pid = _comic_project(tmp_path)
    from comic_studio.engine.projects import get_project
    p = get_project(db, pid)
    assert p["comic_mode"] == "comic_output"
    assert p["dialogue_mode"] == "bubble"
    assert p["target_pages"] == 0
    assert p["image_size"] == "1024x1536"
    assert p["quality_tier"] == "standard"


# ---- Task 2: 漫画分镜拆解（模式分支） ----

_COMIC_REPLY = {
    "shots": [{
        # workflow_type 故意给视频值——断言引擎机械覆写为 comic，而不是赌 LLM 自觉
        "text_span": "少年推开门",
        "description": "室内玄关场景，少年推开门，回头看向屋内",
        "shot_type": "", "camera": {"景别": "中景"}, "duration": 5,
        "workflow_type": "ref2va",
        "dialogue": [{"speaker": "少年", "line": "我回来了"}],
    }],
}


def _split_fake(seen, reply):
    """构造 split_storyboards 的假 LLM：记录 system 提示词，回固定 JSON。"""
    import json as _json
    from comic_studio.engine.llm.provider import LLMClient, Usage

    class FakeSplit(LLMClient):
        def __init__(self):
            super().__init__("http://x", "k", "m")

        def raw_chat(self, messages, temperature=0.3, max_tokens=None):
            seen["system"] = messages[0]["content"]
            return _json.dumps(reply, ensure_ascii=False), Usage(10, 20)

    return lambda task: FakeSplit()


def test_comic_storyboard_split(tmp_path, monkeypatch):
    """comic_output 项目拆解：system 提示词走漫画页分支（场景+人物+对白，
    无运镜/声音/时长要求），全部镜 workflow_type 机械固定 'comic'，
    dialogue 照常进 ledger（speaker+line）。"""
    import json
    from comic_studio.engine.llm.provider import Usage
    from comic_studio.engine.llm.storyboard import split_storyboards
    from comic_studio.engine.projects import set_stage
    from comic_studio.engine.shots import list_shots

    db, pid = _comic_project(tmp_path)
    set_stage(db, pid, "assets_ready")
    seen = {}
    monkeypatch.setattr("comic_studio.engine.llm.storyboard.make_split_factory",
                        lambda db_: _split_fake(seen, _COMIC_REPLY))

    ids = split_storyboards(db, tmp_path / "data", pid)
    assert len(ids) >= 1
    assert "漫画页" in seen["system"] or "单格" in seen["system"]  # comic 模式提示词
    assert "无运镜" in seen["system"]  # 明确禁止运镜/声音/时长
    s = list_shots(db, pid)[0]
    assert s["workflow_type"] == "comic"
    led = json.loads(s["ledger_json"])
    assert led.get("dialogue") == [{"speaker": "少年", "line": "我回来了"}]


def test_comic_split_prompt_scoping(tmp_path, monkeypatch):
    """target_pages>0 注入页数指引；非漫画项目 system 不含漫画页分支。"""
    from comic_studio.engine.llm.storyboard import split_storyboards

    seen_c, seen_v = {}, {}
    monkeypatch.setattr("comic_studio.engine.llm.storyboard.make_split_factory",
                        lambda db_: _split_fake(seen_c, _COMIC_REPLY))
    db, pid = _comic_project(tmp_path, target_pages=12)
    split_storyboards(db, tmp_path / "data", pid)
    assert "12 个漫画页" in seen_c["system"]

    monkeypatch.setattr("comic_studio.engine.llm.storyboard.make_split_factory",
                        lambda db_: _split_fake(seen_v, _COMIC_REPLY))
    db2, pid2 = _comic_project(tmp_path / "v", comic_mode="")
    split_storyboards(db2, tmp_path / "v" / "data", pid2)
    assert "漫画页" not in seen_v["system"]  # 视频项目提示词不受漫画分支污染


# ---- Task 3: 逐页 t2i 生成（gen_comic_page） ----
import pytest


def _migrated_db(path):
    db = Database(path); db.migrate()
    return db


def _comic_shot_ids(db, pid, dialogue=True):
    from types import SimpleNamespace as NS
    from comic_studio.engine.shots import persist_shots
    led = {"dialogue": [{"speaker": "少年", "line": "我回来了"}]} if dialogue else {"dialogue": []}
    return persist_shots(db, pid, [NS(
        text_span="少年推门", description="室内场景，少年推门",
        shot_type="", camera={"景别": "中景", "机位": "平视", "运镜": "固定", "转场": "切"},
        duration=5.0, workflow_type="comic", ledger=led,
        character_ids=[], scene_ids=[], prop_ids=[], depends_on=None, prompt="")])


def _enqueue_comic_page(db, pid, shot_id):
    from comic_studio.engine.jobs import enqueue_job
    jid = enqueue_job(db, "gen_comic_page", project_id=pid, shot_id=shot_id,
                      resource="gpu_comfy", payload={"shot_id": shot_id})
    return db.connect().execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()


def test_gen_comic_page_e2e(tmp_path, monkeypatch):
    """逐页生成：提交 t2i → 落盘 pages/page_NNN.png → shot 标 comic_ready。
    bubble 模式：对白进提示词（模型画气泡），审计快照落 jobs。"""
    from pathlib import Path
    db, pid = _comic_project(tmp_path, style="日漫风")
    (sid,) = _comic_shot_ids(db, pid)
    from comic_studio.engine.settings import set_setting
    set_setting(db, "template_map", {"comic_page": "zimage_t2i"})
    from comic_studio.engine.workflows import registry
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    from tests.comfy_mock import comfy_server
    from comic_studio.engine.comfy.client import ComfyClient
    from comic_studio.engine.comicgen import handle_gen_comic_page
    job = _enqueue_comic_page(db, pid, sid)
    with comfy_server("ok") as m:
        handle_gen_comic_page(db, tmp_path / "data", job, ComfyClient(m.base_url))
    # 页面落盘
    from comic_studio.engine.paths import data_to_abs
    from comic_studio.engine.projects import get_project
    slug = get_project(db, pid)["slug"]
    page = data_to_abs(tmp_path / "data", f"projects/{slug}/pages/page_001.png")
    assert page.exists() and page.stat().st_size > 0
    # shot 状态
    from comic_studio.engine.shots import list_shots
    s = list_shots(db, pid)[0]
    assert s["status"] == "comic_ready"
    # 提示词要素：场景描述 + 对白（bubble）+ 画风
    assert len(m.prompts) == 1
    text = m.prompts[0]["prompt"]["57:27"]["inputs"]["text"]
    assert "单格漫画插画" in text and "少年推门" in text
    assert "少年说「我回来了」" in text
    assert "画风：日漫风" in text
    # 审计快照
    import json
    snap = json.loads(db.connect().execute(
        "SELECT snapshot_json FROM jobs WHERE id=?", (job["id"],)).fetchone()["snapshot_json"])
    assert snap["template"] == "zimage_t2i" and snap["prompt"] == text


def test_gen_comic_page_default_template_and_size(tmp_path, monkeypatch):
    """默认映射 comic_page→comic_page 模板：seed/steps/width/height 注入点齐备，
    尺寸/质量档按项目列注入工作流（1024x1536 + standard=12 步）。"""
    from pathlib import Path
    db, pid = _comic_project(tmp_path)  # 不动 template_map：走默认映射
    (sid,) = _comic_shot_ids(db, pid, dialogue=False)
    from comic_studio.engine.workflows import registry
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    from tests.comfy_mock import comfy_server
    from comic_studio.engine.comfy.client import ComfyClient
    from comic_studio.engine.comicgen import handle_gen_comic_page
    job = _enqueue_comic_page(db, pid, sid)
    with comfy_server("ok") as m:
        handle_gen_comic_page(db, tmp_path / "data", job, ComfyClient(m.base_url))
    wf = m.prompts[0]["prompt"]
    assert wf["57:13"]["inputs"]["width"] == 1024
    assert wf["57:13"]["inputs"]["height"] == 1536
    assert wf["57:3"]["inputs"]["steps"] == 12
    assert 0 <= wf["57:3"]["inputs"]["seed"] <= 2**31 - 1
    # 无对白镜提示词不带对白段
    assert "说「" not in wf["57:27"]["inputs"]["text"]
    # filename_prefix 带项目/页标识
    assert wf["9"]["inputs"]["filename_prefix"].startswith("cs/漫画剧/page-1")


def test_gen_comic_page_footer_mode_without_pillow(tmp_path, monkeypatch):
    """footer 模式：对白不进提示词；Pillow 缺失（或图片异常）不炸任务——
    shot 照常 comic_ready，仅 warn 透明（真机排障线索）。"""
    from pathlib import Path
    db, pid = _comic_project(tmp_path, dialogue_mode="footer")
    (sid,) = _comic_shot_ids(db, pid)
    from comic_studio.engine.workflows import registry
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    from tests.comfy_mock import comfy_server
    from comic_studio.engine.comfy.client import ComfyClient
    from comic_studio.engine.comicgen import handle_gen_comic_page
    job = _enqueue_comic_page(db, pid, sid)
    with comfy_server("ok") as m:
        handle_gen_comic_page(db, tmp_path / "data", job, ComfyClient(m.base_url))
    from comic_studio.engine.shots import list_shots
    assert list_shots(db, pid)[0]["status"] == "comic_ready"
    text = m.prompts[0]["prompt"]["57:27"]["inputs"]["text"]
    assert "说「" not in text  # footer 对白不进画面提示词
    warns = db.connect().execute(
        "SELECT message FROM logs WHERE level='warn'").fetchall()
    assert any("对白后处理" in w["message"] for w in warns)


def test_gen_comic_page_mode_none_no_dialogue(tmp_path, monkeypatch):
    """none 模式：对白既不进提示词也不做后处理，无 warn。"""
    from pathlib import Path
    db, pid = _comic_project(tmp_path, dialogue_mode="none")
    (sid,) = _comic_shot_ids(db, pid)
    from comic_studio.engine.workflows import registry
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    from tests.comfy_mock import comfy_server
    from comic_studio.engine.comfy.client import ComfyClient
    from comic_studio.engine.comicgen import handle_gen_comic_page
    job = _enqueue_comic_page(db, pid, sid)
    with comfy_server("ok") as m:
        handle_gen_comic_page(db, tmp_path / "data", job, ComfyClient(m.base_url))
    from comic_studio.engine.shots import list_shots
    assert list_shots(db, pid)[0]["status"] == "comic_ready"
    assert "说「" not in m.prompts[0]["prompt"]["57:27"]["inputs"]["text"]
    warns = db.connect().execute(
        "SELECT message FROM logs WHERE level='warn'").fetchall()
    assert not any("对白后处理" in w["message"] for w in warns)


def test_gen_comic_page_missing_shot(tmp_path, monkeypatch):
    """分镜已删除（入队后镜被删，payload 仍指旧 id）→ 显式 ValueError，不静默。"""
    from pathlib import Path
    db, pid = _comic_project(tmp_path)
    (sid,) = _comic_shot_ids(db, pid)
    from comic_studio.engine.shots import delete_shots_batch
    from comic_studio.engine.workflows import registry
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    from tests.comfy_mock import comfy_server
    from comic_studio.engine.comfy.client import ComfyClient
    from comic_studio.engine.comicgen import handle_gen_comic_page
    job = _enqueue_comic_page(db, pid, sid)
    delete_shots_batch(db, pid, [sid])  # 删镜清 jobs.shot_id 引用，payload 指向已删镜
    with comfy_server("ok"):
        with pytest.raises(ValueError, match="分镜已删除"):
            handle_gen_comic_page(db, tmp_path / "data", job, ComfyClient("http://x"))


def test_comic_page_template_registered(tmp_path, monkeypatch):
    """comic_page 模板 manifest 注册且默认映射可用；尺寸参数注入 57:13。"""
    from pathlib import Path
    from comic_studio.engine.workflows import registry
    from comic_studio.engine.workflows.filler import fill_workflow
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    t = registry.resolve_template(_migrated_db(tmp_path / "s.db"), "comic_page")
    assert t.id == "comic_page"
    for k in ("seed", "steps", "width", "height"):
        assert k in t.inject_params, f"comic_page 缺 {k} 注入点"
    wf, uploads = fill_workflow(
        t, prompt="测试", params={"seed": 1, "steps": 8, "width": 832, "height": 1216},
        images=[], output_ctx={"project": "p", "asset": "page-1"})
    assert wf["57:13"]["inputs"] == {"width": 832, "height": 1216, "batch_size": 1}
    assert wf["57:3"]["inputs"]["steps"] == 8
    assert wf["57:27"]["inputs"]["text"] == "测试"


def test_postprocess_dialogue_footer_draws(monkeypatch, tmp_path):
    """footer 后处理（有 Pillow 环境）：真 PNG 上画底部字幕条并覆写文件。"""
    pytest.importorskip("PIL")
    from PIL import Image
    from comic_studio.engine.comicgen import _postprocess_dialogue
    p = tmp_path / "page_001.png"
    Image.new("RGB", (200, 200), "white").save(p)
    ok = _postprocess_dialogue(
        p, [{"speaker": "少年", "line": "我回来了"}], "footer")
    assert ok is True
    img = Image.open(p)
    assert img.size == (200, 200)
    px = img.convert("RGB")
    # 白底上画了非白像素（文字落上了）
    assert any(px.getpixel((x, y)) != (255, 255, 255)
               for x in range(20, 180, 4) for y in range(150, 190, 4))
