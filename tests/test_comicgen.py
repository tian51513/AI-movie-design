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


def test_comic_split_fills_prompt(tmp_path, monkeypatch):
    """漫画镜拆解即填 prompt（=description）——gate2「全部镜有提示词」对
    comic 项目天然成立；视频项目照旧留空等 gen_prompts（2026-09-13 计划
    缺口补：否则 autopilot 把漫画镜带进 gen_prompts，H3 视频提示词链烧
    重度模型却从不被页面消费）。"""
    from comic_studio.engine.llm.storyboard import split_storyboards
    from comic_studio.engine.projects import set_stage
    from comic_studio.engine.shots import list_shots

    db, pid = _comic_project(tmp_path)
    set_stage(db, pid, "assets_ready")
    monkeypatch.setattr("comic_studio.engine.llm.storyboard.make_split_factory",
                        lambda db_: _split_fake({}, _COMIC_REPLY))
    split_storyboards(db, tmp_path / "data", pid)
    s = list_shots(db, pid)[0]
    assert s["prompt"] == s["description"]  # 拆解即填，gate2 无缺口

    monkeypatch.setattr("comic_studio.engine.llm.storyboard.make_split_factory",
                        lambda db_: _split_fake({}, _COMIC_REPLY))
    db2, pid2 = _comic_project(tmp_path / "v", comic_mode="")
    set_stage(db2, pid2, "assets_ready")
    split_storyboards(db2, tmp_path / "v" / "data", pid2)
    v = list_shots(db2, pid2)[0]
    assert v["workflow_type"] != "comic" and v["prompt"] == ""  # 视频链不变


def test_comic_autopilot_skips_video_prompts(tmp_path, monkeypatch):
    """拆解完成后（assets_ready，镜已落库）autopilot 决策不进 gen_prompts，
    直落 gate2——漫画页分支从 storyboard_ready 接手。"""
    from comic_studio.engine.autopilot import next_action
    from comic_studio.engine.llm.storyboard import split_storyboards
    from comic_studio.engine.projects import set_stage

    db, pid = _comic_project(tmp_path)
    set_stage(db, pid, "assets_ready")
    monkeypatch.setattr("comic_studio.engine.llm.storyboard.make_split_factory",
                        lambda db_: _split_fake({}, _COMIC_REPLY))
    split_storyboards(db, tmp_path / "data", pid)
    act = next_action(db, tmp_path / "data", pid)
    assert act["action"] == "gate2"  # 缺口修复前是 gen_prompts


def test_gen_prompt_comic_short_circuit(tmp_path, monkeypatch):
    """comic 镜的 gen_prompt handler 短路：不碰 LLM（client_for_task 即炸），
    prompt=description 机械填充——兜住 stale 联动/手动重生路径。"""
    from comic_studio.engine.jobs import enqueue_job
    from comic_studio.engine.pipeline_jobs import handle_gen_prompt

    db, pid = _comic_project(tmp_path)
    (sid,) = _comic_shot_ids(db, pid)

    def _boom(*a, **k):
        raise AssertionError("comic 镜不应调 LLM")

    monkeypatch.setattr("comic_studio.engine.llm.provider.client_for_task", _boom)
    jid = enqueue_job(db, "gen_prompt", project_id=pid, shot_id=sid,
                      payload={"shot_id": sid})
    job = db.connect().execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    handle_gen_prompt(db, tmp_path / "data", job, None)
    from comic_studio.engine.shots import get_shot
    s = get_shot(db, sid)
    assert s["prompt"] == "室内场景，少年推门"  # _comic_shot_ids 的 description
    assert s["status"] == "ready"


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
    # 提示词要素：场景描述 + 禁字指令（2026-09-13 气泡渲染反转：对白不再进
    # 提示词——t2i 画中文=乱码，气泡由 Pillow 后处理画）+ 画风
    assert len(m.prompts) == 1
    text = m.prompts[0]["prompt"]["57:27"]["inputs"]["text"]
    assert "单格漫画插画" in text and "少年推门" in text
    assert "我回来了" not in text          # 对白不进提示词
    assert "不要画任何文字" in text        # bubble 模式禁字指令
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


# ---- Task 4: autopilot 漫画分支（storyboard_ready → 逐页 → comic_ready） ----


def _comic_shot_ns():
    from types import SimpleNamespace as NS
    return NS(
        text_span="少年推门", description="室内场景，少年推门",
        shot_type="", camera={"景别": "中景", "机位": "平视", "运镜": "固定", "转场": "切"},
        duration=5.0, workflow_type="comic", ledger={},
        character_ids=[], scene_ids=[], prop_ids=[], depends_on=None, prompt="x")


def test_autopilot_comic_flow(tmp_path):
    """comic_output：storyboard_ready + 有 prompt + 无页面 → gen_comic_pages 动作。"""
    from comic_studio.engine.autopilot import next_action
    from comic_studio.engine.projects import set_stage
    from comic_studio.engine.shots import persist_shots
    db, pid = _comic_project(tmp_path)
    set_stage(db, pid, "storyboard_ready")
    persist_shots(db, pid, [_comic_shot_ns()])
    act = next_action(db, tmp_path / "data", pid)
    assert act["action"] == "gen_comic_pages"


def test_autopilot_comic_tick_enqueues_pages(tmp_path):
    """tick：缺页镜逐个入队 gen_comic_page（shot_id 挂镜、gpu_comfy 资源）。"""
    from comic_studio.engine.autopilot import tick
    from comic_studio.engine.projects import set_stage
    from comic_studio.engine.settings import set_setting
    from comic_studio.engine.shots import persist_shots
    db, pid = _comic_project(tmp_path)
    set_setting(db, "comfy", {"base_url": "http://127.0.0.1:8188"})
    set_stage(db, pid, "storyboard_ready")
    persist_shots(db, pid, [_comic_shot_ns(), _comic_shot_ns()])
    act = tick(db, tmp_path / "data", pid)
    assert act["action"] == "gen_comic_pages"
    jobs = db.connect().execute(
        "SELECT * FROM jobs WHERE type='gen_comic_page' ORDER BY id").fetchall()
    assert len(jobs) == 2
    assert all(j["resource"] == "gpu_comfy" and j["shot_id"] for j in jobs)


def test_autopilot_comic_pages_ready_sets_stage(tmp_path):
    """全部页面落盘 → comic_done；tick 落 comic_ready 终态；此后 done。"""
    from comic_studio.engine.autopilot import next_action, tick
    from comic_studio.engine.paths import data_to_abs
    from comic_studio.engine.projects import get_project, set_stage
    from comic_studio.engine.shots import persist_shots
    db, pid = _comic_project(tmp_path)
    slug = get_project(db, pid)["slug"]
    set_stage(db, pid, "storyboard_ready")
    persist_shots(db, pid, [_comic_shot_ns()])
    pages_dir = data_to_abs(tmp_path / "data", f"projects/{slug}/pages")
    pages_dir.mkdir(parents=True)
    (pages_dir / "page_001.png").write_bytes(PNG)
    assert next_action(db, tmp_path / "data", pid)["action"] == "comic_done"
    tick(db, tmp_path / "data", pid)
    assert get_project(db, pid)["stage"] == "comic_ready"
    assert next_action(db, tmp_path / "data", pid)["action"] == "done"


def test_autopilot_comic_active_and_failed_guards(tmp_path):
    """在飞 → wait 生成中；最新 job 失败且无在飞 → wait 等手动重发。"""
    from comic_studio.engine.autopilot import next_action
    from comic_studio.engine.jobs import enqueue_job
    from comic_studio.engine.projects import set_stage
    from comic_studio.engine.shots import persist_shots
    db, pid = _comic_project(tmp_path)
    set_stage(db, pid, "storyboard_ready")
    (sid,) = persist_shots(db, pid, [_comic_shot_ns()])
    jid = enqueue_job(db, "gen_comic_page", project_id=pid, shot_id=sid,
                      resource="gpu_comfy", payload={"shot_id": sid})
    act = next_action(db, tmp_path / "data", pid)
    assert act["action"] == "wait" and "生成中" in act["detail"]
    conn = db.connect()
    conn.execute("UPDATE jobs SET status='failed' WHERE id=?", (jid,))
    conn.commit()
    act = next_action(db, tmp_path / "data", pid)
    assert act["action"] == "wait" and "失败" in act["detail"]


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


def test_footer_wrap_and_font_fallback():
    """footer 完善（Task 5）：长对白按像素宽逐字换行（不再 80 字硬截断）+
    跨平台 CJK 字体回退链（WSL 无 msyh.ttc 不落点阵小字）。"""
    from comic_studio.engine.comicgen import _footer_font, _wrap_footer

    class _F:  # 假字体：每字符 10px
        def getlength(self, s):
            return len(s) * 10.0

    assert _wrap_footer("一" * 25, _F(), 200) == ["一" * 20, "一" * 5]
    assert _wrap_footer("", _F(), 200) == []
    assert hasattr(_footer_font(), "getlength")  # 回退链终点仍可度量


def test_postprocess_dialogue_footer_wraps_long_line(tmp_path):
    """超长台词 footer：换行全部画出（ok=True），不截断不炸。"""
    pytest.importorskip("PIL")
    from PIL import Image
    from comic_studio.engine.comicgen import _postprocess_dialogue
    p = tmp_path / "page_001.png"
    Image.new("RGB", (400, 300), "white").save(p)
    ok = _postprocess_dialogue(
        p, [{"speaker": "少年", "line": "长" * 200}], "footer")
    assert ok is True


# ---- Task 5: 导出（PDF/长图） ----

def _write_png(path, w=8, h=12, shade=(200, 30, 30)):
    """纯 stdlib 造真 PNG（RGB 8bit 非隔行）：ffmpeg 可解码，strip 测试不吃 Pillow。"""
    import struct
    import zlib

    def chunk(tag, payload):
        c = tag + payload
        return struct.pack(">I", len(payload)) + c + struct.pack(">I", zlib.crc32(c))

    raw = b"".join(b"\x00" + bytes(shade) * w for _ in range(h))
    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _comic_pages(tmp_path, db, pid, shades=((200, 30, 30),)):
    from comic_studio.engine.paths import data_to_abs
    from comic_studio.engine.projects import get_project
    slug = get_project(db, pid)["slug"]
    pages = data_to_abs(tmp_path / "data", f"projects/{slug}/pages")
    pages.mkdir(parents=True, exist_ok=True)
    for i in range(1, 4):
        _write_png(pages / f"page_{i:03d}.png", shade=shades[(i - 1) % len(shades)])
    return pages


def test_export_comic_pdf(tmp_path):
    """PDF 导出：pages/ 三页 → output/comic.pdf（projects/<slug>/output/ 下）。"""
    pytest.importorskip("PIL")
    db, pid = _comic_project(tmp_path)
    from comic_studio.engine.comicexport import export_comic_pdf
    from comic_studio.engine.projects import get_project
    _comic_pages(tmp_path, db, pid)
    out = export_comic_pdf(db, tmp_path / "data", pid)
    assert out.suffix == ".pdf" and out.exists()
    assert out.read_bytes()[:5] == b"%PDF-"
    assert "output" in out.parts and get_project(db, pid)["slug"] in out.parts


def test_export_comic_strip(tmp_path):
    """长图导出：pages/ 三页 → output/comic_strip.png（真 ffmpeg vstack）。"""
    db, pid = _comic_project(tmp_path)
    from comic_studio.engine.comicexport import export_comic_strip
    _comic_pages(tmp_path, db, pid)
    out = export_comic_strip(db, tmp_path / "data", pid)
    assert out.suffix == ".png" and out.exists()
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_export_comic_strip_order(tmp_path):
    """长图按页号升序自上而下拼接：首/中/尾像素各页底色。"""
    pytest.importorskip("PIL")
    from PIL import Image
    db, pid = _comic_project(tmp_path)
    from comic_studio.engine.comicexport import export_comic_strip
    _comic_pages(tmp_path, db, pid,
                 shades=[(255, 0, 0), (0, 255, 0), (0, 0, 255)])
    out = export_comic_strip(db, tmp_path / "data", pid)
    img = Image.open(out).convert("RGB")
    assert img.size == (8, 36)  # 三页 8×12 竖排
    assert img.getpixel((4, 1)) == (255, 0, 0)    # page_001 在顶
    assert img.getpixel((4, 18)) == (0, 255, 0)
    assert img.getpixel((4, 35)) == (0, 0, 255)   # page_003 在底


def test_export_comic_excludes_disabled_and_dead_pages(tmp_path):
    """无效镜页/已删镜残页不进导出（与「无效镜不进合成」纪律同口径）；
    无分镜行的项目（手工放页）以磁盘为准全量导出。"""
    from comic_studio.engine.comicexport import comic_pages
    from comic_studio.engine.shots import persist_shots
    db, pid = _comic_project(tmp_path)
    _comic_pages(tmp_path, db, pid)
    assert len(comic_pages(db, tmp_path / "data", pid)) == 3  # 无分镜 → 全量
    from types import SimpleNamespace as NS
    s1, s2, s3 = persist_shots(db, pid, [NS(
        text_span="页", description="页", shot_type="",
        camera={"景别": "中景"}, duration=5.0, workflow_type="comic", ledger={},
        character_ids=[], scene_ids=[], prop_ids=[], depends_on=None, prompt="")]
        * 3)
    assert len(comic_pages(db, tmp_path / "data", pid)) == 3
    from comic_studio.engine.shots import set_disabled_batch
    set_disabled_batch(db, pid, [s2], 1)  # 中间页对应镜被禁用
    pages = comic_pages(db, tmp_path / "data", pid)
    assert [p.name for p in pages] == ["page_001.png", "page_003.png"]


def test_export_comic_requires_pages(tmp_path):
    """无页面显式报错（路由 422 语义）——空 pages/ 不产空成品。"""
    from comic_studio.engine.comicexport import export_comic_pdf, export_comic_strip
    db, pid = _comic_project(tmp_path)
    with pytest.raises(ValueError, match="无已生成漫画页"):
        export_comic_pdf(db, tmp_path / "data", pid)
    with pytest.raises(ValueError, match="无已生成漫画页"):
        export_comic_strip(db, tmp_path / "data", pid)


def test_export_comic_api(tmp_path):
    """POST export-comic：404/422 守卫 + strip 快乐路径返回 /media 可访问路径。"""
    from fastapi.testclient import TestClient

    from comic_studio.web.app import create_app
    db, pid = _comic_project(tmp_path)
    app = create_app(tmp_path / "s.db", tmp_path / "data", start_workers=False)
    with TestClient(app) as c:
        assert c.post("/api/projects/9999/export-comic").status_code == 404
        assert c.post(
            f"/api/projects/{pid}/export-comic?format=zip").status_code == 422
        assert c.post(
            f"/api/projects/{pid}/export-comic?format=pdf").status_code == 422  # 无页
        _comic_pages(tmp_path, db, pid)
        r = c.post(f"/api/projects/{pid}/export-comic?format=strip")
        assert r.status_code == 200
        body = r.json()
        assert body["rel"].endswith("output/comic_strip.png")
        assert body["url"] == f"/media/{body['rel']}"
        assert (tmp_path / "data" / body["rel"]).exists()


# ---- Task 6: 前端全套后端侧（创建路由 + 手动补页路由） ----

def test_from_comic_novel_api(tmp_path):
    """POST from-comic-novel：正文上传 + 漫画参数 → comic_output 项目（四列落库）。"""
    from fastapi.testclient import TestClient

    from comic_studio.web.app import create_app
    app = create_app(tmp_path / "s.db", tmp_path / "data", start_workers=False)
    with TestClient(app) as c:
        r = c.post("/api/projects/from-comic-novel",
                   data={"name": "漫画测试", "aspect_ratio": "3:4",
                         "dialogue_mode": "footer", "target_pages": "12",
                         "image_size": "832x1216", "quality_tier": "high",
                         "style": "水墨画风", "style_vis": "水墨留白"},
                   files={"novel": ("novel.txt", "少年推门而入。" * 30, "text/plain")})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["comic_mode"] == "comic_output"
        assert body["dialogue_mode"] == "footer"
        assert body["target_pages"] == 12
        assert body["image_size"] == "832x1216"
        assert body["quality_tier"] == "high"
        assert body["style"] == "水墨画风"
        # 非法参数逐个 422
        for k, v in {"dialogue_mode": "loud", "quality_tier": "ultra",
                     "image_size": "1x1", "target_pages": "999"}.items():
            data = {"name": "x", k: v}
            rr = c.post("/api/projects/from-comic-novel", data=data,
                        files={"novel": ("n.txt", "正文内容。" * 50, "text/plain")})
            assert rr.status_code == 422, (k, rr.text)
        # 非 UTF-8 文件 422
        rr = c.post("/api/projects/from-comic-novel", data={"name": "x"},
                    files={"novel": ("n.txt", b"\xff\xfe\x00bad", "text/plain")})
        assert rr.status_code == 422


def test_from_comic_novel_defaults(tmp_path):
    """不传可选参数：全部落默认（bubble/0=自动/1024x1536/standard）。"""
    from fastapi.testclient import TestClient

    from comic_studio.web.app import create_app
    app = create_app(tmp_path / "s.db", tmp_path / "data", start_workers=False)
    with TestClient(app) as c:
        r = c.post("/api/projects/from-comic-novel",
                   data={"name": "默认参数"},
                   files={"novel": ("n.txt", "正文。" * 100, "text/plain")})
        assert r.status_code == 201, r.text
        b = r.json()
        assert b["comic_mode"] == "comic_output" and b["dialogue_mode"] == "bubble"
        assert b["target_pages"] == 0 and b["quality_tier"] == "standard"


def test_generate_comic_pages_api(tmp_path):
    """POST generate-comic-pages：手动补页——404/409 守卫 + 缺页入队、
    已有页与在飞镜跳过。"""
    from fastapi.testclient import TestClient

    from comic_studio.web.app import create_app
    from comic_studio.engine.projects import get_project, set_stage
    db, pid = _comic_project(tmp_path)
    sids = _comic_shot_ids(db, pid)
    set_stage(db, pid, "storyboard_ready")
    app = create_app(tmp_path / "s.db", tmp_path / "data", start_workers=False)
    with TestClient(app) as c:
        assert c.post("/api/projects/9999/generate-comic-pages").status_code == 404
        # 未配置 ComfyUI → 409 门禁（2026-09-01 事故防线同款）——新库默认带
        # 127.0.0.1:8188，先清空再验证门禁
        from comic_studio.engine.settings import set_setting
        set_setting(db, "comfy", {"base_url": ""})
        r = c.post(f"/api/projects/{pid}/generate-comic-pages")
        assert r.status_code == 409
        # 配好 ComfyUI 地址（写 settings comfy.base_url）
        set_setting(db, "comfy", {"base_url": "http://mock:8188"})
        r = c.post(f"/api/projects/{pid}/generate-comic-pages")
        assert r.status_code == 202, r.text
        assert r.json() == {"enqueued": 1}
        # 阶段守卫：created 不能生成
        set_stage(db, pid, "created")
        r = c.post(f"/api/projects/{pid}/generate-comic-pages")
        assert r.status_code == 409
        # 非 comic_output 项目 422
        from comic_studio.engine.projects import create_project
        vid = create_project(db, tmp_path / "data", "普通项目", "9:16", "正文" * 100)["id"]
        assert c.post(f"/api/projects/{vid}/generate-comic-pages").status_code == 422


# ---- Part A（2026-09-13）：漫画项目三数据源——主题生成 text 模式 + 有声小说音频 ----

def test_from_comic_novel_text_param(tmp_path):
    """from-comic-novel 支持 text 直传（主题生成第二步：preview 已出正文，
    不再走文件）；文件与 text 二选一（file 优先），都空 422。"""
    from fastapi.testclient import TestClient

    from comic_studio.web.app import create_app
    app = create_app(tmp_path / "s.db", tmp_path / "data", start_workers=False)
    with TestClient(app) as c:
        text = "少年推门，屋内烛火摇曳。" * 20
        r = c.post("/api/projects/from-comic-novel",
                   data={"name": "主题漫画", "text": text,
                         "dialogue_mode": "footer", "target_pages": "8"})
        assert r.status_code == 201, r.text
        b = r.json()
        assert b["comic_mode"] == "comic_output" and b["dialogue_mode"] == "footer"
        assert b["target_pages"] == 8
        # 正文直传落库（读 novel.txt）
        novel = (tmp_path / "data" / "projects" / b["slug"] / "novel.txt")
        assert novel.exists() and text[:30] in novel.read_text(encoding="utf-8")
        # 都空 → 422
        r = c.post("/api/projects/from-comic-novel", data={"name": "x"})
        assert r.status_code == 422


def test_from_comic_audio_api(tmp_path, monkeypatch):
    """from-comic-audio：音频→comic_output 项目（占位正文+subtitles=0+四参数）
    →存源音频→入队 transcribe；格式 422；依赖缺失 422。"""
    import io
    import sys
    import types
    stub = types.ModuleType("faster_whisper")
    stub.WhisperModel = object
    monkeypatch.setitem(sys.modules, "faster_whisper", stub)

    from fastapi.testclient import TestClient
    from comic_studio.web.app import create_app
    app = create_app(tmp_path / "s.db", tmp_path / "data", start_workers=False)
    with TestClient(app) as c:
        r = c.post("/api/projects/from-comic-audio",
                   data={"name": "有声漫画", "aspect_ratio": "3:4",
                         "dialogue_mode": "footer", "target_pages": "10",
                         "image_size": "832x1216", "quality_tier": "high"},
                   files={"audio": ("book.mp3", io.BytesIO(b"ID3fake"),
                                    "audio/mpeg")})
        assert r.status_code == 201, r.text
        b = r.json()
        assert b["comic_mode"] == "comic_output"
        assert b["dialogue_mode"] == "footer" and b["target_pages"] == 10
        assert b["image_size"] == "832x1216" and b["quality_tier"] == "high"
        assert b["subtitles"] == 0  # 漫画页自呈对白，恒 0
        src = (tmp_path / "data" / "projects" / b["slug"] / "audio" / "source.mp3")
        assert src.exists() and src.read_bytes() == b"ID3fake"
        row = app.state.db.connect().execute(
            "SELECT type, status FROM jobs WHERE project_id=? "
            "ORDER BY id DESC LIMIT 1", (b["id"],)).fetchone()
        assert row["type"] == "transcribe" and row["status"] == "pending"
        # 非音频格式 422
        r = c.post("/api/projects/from-comic-audio", data={"name": "x"},
                   files={"audio": ("book.txt", io.BytesIO(b"no"), "text/plain")})
        assert r.status_code == 422
        # 缺 asr 依赖 → 422 安装指引
        monkeypatch.setitem(sys.modules, "faster_whisper", None)
        r = c.post("/api/projects/from-comic-audio", data={"name": "y"},
                   files={"audio": ("b.mp3", io.BytesIO(b"x"), "audio/mpeg")})
        assert r.status_code == 422 and "asr" in r.json()["detail"].lower()


def test_comic_small_size_presets(tmp_path):
    """2026-09-13 用户需求：小尺寸档（512/768 系，32 倍数对齐）——出图速度
    约为 1024 档 1/4；进 SIZE_PRESETS 即可被 from-comic-novel 接受。"""
    from comic_studio.engine.comicgen import SIZE_PRESETS
    assert {"512x768", "768x512", "512x512", "768x1024", "1024x768"} <= set(SIZE_PRESETS)
    from fastapi.testclient import TestClient
    from comic_studio.web.app import create_app
    app = create_app(tmp_path / "s.db", tmp_path / "data", start_workers=False)
    with TestClient(app) as c:
        r = c.post("/api/projects/from-comic-novel",
                   data={"name": "小尺寸", "image_size": "512x768"},
                   files={"novel": ("n.txt", "正文。" * 100, "text/plain")})
        assert r.status_code == 201, r.text
        assert r.json()["image_size"] == "512x768"


# ---- 气泡渲染（2026-09-13 二期①）：Pillow 后处理画真气泡 + 样式三参数 ----

def test_migration_37_bubble_style(tmp_path):
    """bubble_style JSON 列（透明度/字色/字号）——空串=默认样式。"""
    db = Database(tmp_path / "s.db"); db.migrate()
    cols = {c[1] for c in db.connect().execute("PRAGMA table_info(projects)")}
    assert "bubble_style" in cols
    from comic_studio.engine.projects import create_project, get_project
    pid = create_project(db, tmp_path / "data", "气泡剧", "9:16", "正文" * 50,
                         comic_mode="comic_output",
                         bubble_style='{"opacity":100,"font_color":"#ff0000","font_size":24}')["id"]
    p = get_project(db, pid)
    assert p["bubble_style"] == '{"opacity":100,"font_color":"#ff0000","font_size":24}'


def test_bubble_style_parse(tmp_path):
    """_bubble_style 解析容错：空/坏 JSON→默认；缺键补默认；opacity 钳 0~100。"""
    from comic_studio.engine.comicgen import _bubble_style
    d = _bubble_style("")
    assert d == {"opacity": 85, "font_color": "#222222", "font_size": 0}
    assert _bubble_style("not json") == d
    assert _bubble_style('{"opacity":150}')["opacity"] == 100
    assert _bubble_style('{"opacity":-5}')["opacity"] == 0
    assert _bubble_style('{"font_size":30}')["font_size"] == 30


class _StubFont:
    """确定性字体：每字固定 15px 宽（load_default 无 CJK 字形，度量不可控）。"""
    size = 28
    def getlength(self, s):
        return 15 * len(s)


def test_bubble_layout_adaptive_and_staggered():
    """布局纯函数：宽度随内容自适应（短句窄）；上限 40% 页宽；
    奇数句靠左、偶数句靠右；y 垂直递增不越界；长句折行。"""
    from comic_studio.engine.comicgen import _bubble_layout
    font = _StubFont()
    W, H = 1024, 1536
    boxes, dropped = _bubble_layout(W, H, [
        {"speaker": "甲", "line": "好"},                      # 3 字 → 45px+pad
        {"speaker": "乙", "line": "今" * 40},                 # 42 字 → 折行顶 40% 上限
        {"speaker": "甲", "line": "行"},
        {"speaker": "乙", "line": "那走吧"},
    ], font)
    assert dropped == 0 and len(boxes) == 4
    short_w = boxes[0][2]
    x, y, long_w, long_h, lines = boxes[1]
    assert short_w < long_w                     # 短句气泡更窄（自适应）
    assert long_w <= W * 0.4 + 1                # 上限 40%
    assert len(lines) > 1                       # 42 字在 40% 宽内必然折行
    assert boxes[0][0] < W * 0.5 <= boxes[1][0]  # 奇左偶右
    assert boxes[1][1] == boxes[0][1]           # 同排左右齐头（双列游标）
    gap = int(H * 0.015)
    # 同左列堆叠：3 号气泡在 1 号底部 + 间隙 + 尾巴 14 之下（不受对侧挤压）
    assert boxes[2][1] >= boxes[0][1] + boxes[0][3] + gap + 14
    assert all(b[1] + b[3] < H for b in boxes)  # 不越界


def test_bubble_layout_caps_bubbles():
    """防爆：超 4 句只取前 4（返回带截断数）。"""
    from PIL import ImageFont
    from comic_studio.engine.comicgen import _bubble_layout
    font = ImageFont.load_default(20)
    dlg = [{"speaker": "甲", "line": f"第{i}句话"} for i in range(7)]
    out = _bubble_layout(1024, 1536, dlg, font)
    boxes, dropped = out if isinstance(out, tuple) else (out, 0)
    assert len(boxes) == 4 and dropped == 3


def test_draw_bubbles_renders(tmp_path):
    """绘制冒烟：落盘被改、尺寸不变；opacity=100 背景全透明但文字仍在。"""
    from PIL import Image
    from comic_studio.engine.comicgen import _draw_bubbles
    p = tmp_path / "page.png"
    Image.new("RGB", (768, 1024), (200, 180, 160)).save(p)
    ok = _draw_bubbles(p, [{"speaker": "少年", "line": "我回来了"}],
                       {"opacity": 100, "font_color": "#222222", "font_size": 0})
    assert ok
    img = Image.open(p)
    assert img.size == (768, 1024)
    # 文字仍在：页面顶部区域应出现与底色不同的像素（透明底+深字）
    px = list(img.convert("RGB").crop((0, 0, 768, 300)).getdata())
    assert any(c != (200, 180, 160) for c in px)


def test_comic_prompt_bubble_no_dialogue(tmp_path):
    """提示词反转：bubble 不再拼对白（模型画中文=乱码），改注入禁字指令；
    footer/none 本就不注入。"""
    from comic_studio.engine.comicgen import build_comic_prompt
    from comic_studio.engine.projects import get_project
    db, pid = _comic_project(tmp_path)
    proj = get_project(db, pid)
    from types import SimpleNamespace as NS
    shot = {"description": "室内场景",
            "ledger_json": '{"dialogue":[{"speaker":"少年","line":"我回来了"}]}'}
    p = build_comic_prompt(db, proj, shot)
    assert "我回来了" not in p and "少年说" not in p   # 对白不进提示词
    assert "不要画任何文字" in p or "无文字" in p        # 禁字指令在
    # footer/none：同样无对白、且无禁字指令（footer 画面本就无字约束非必须）
    db2, pid2 = _comic_project(tmp_path / "f", dialogue_mode="footer")
    p2 = build_comic_prompt(db2, get_project(db2, pid2), shot)
    assert "我回来了" not in p2


def test_postprocess_dialogue_bubble_branch(tmp_path):
    """_postprocess_dialogue 分流：bubble 走气泡（真绘制）、footer 走字幕条。"""
    from PIL import Image
    from comic_studio.engine.comicgen import _postprocess_dialogue
    dlg = [{"speaker": "甲", "line": "你好"}]
    for mode, tag in (("bubble", "b"), ("footer", "f")):
        p = tmp_path / f"page_{tag}.png"
        Image.new("RGB", (768, 1024), (200, 180, 160)).save(p)
        ok = _postprocess_dialogue(p, dlg, mode,
                                   style={"opacity": 85, "font_color": "#222222", "font_size": 0})
        assert ok, mode
        img = Image.open(p)
        assert img.size == (768, 1024)


def test_from_comic_novel_bubble_style_and_patch(tmp_path):
    """from-comic-novel 透传 bubble_style；PATCH dialogue_mode/bubble_style 生效；
    非法值 422。"""
    import json as _json
    from fastapi.testclient import TestClient
    from comic_studio.web.app import create_app
    app = create_app(tmp_path / "s.db", tmp_path / "data", start_workers=False)
    with TestClient(app) as c:
        r = c.post("/api/projects/from-comic-novel",
                   data={"name": "气泡", "bubble_style": '{"opacity":100}'},
                   files={"novel": ("n.txt", "正文。" * 80, "text/plain")})
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        assert r.json()["bubble_style"] == '{"opacity":100}'
        # PATCH：对白呈现 + 气泡样式
        r = c.patch(f"/api/projects/{pid}", json={"dialogue_mode": "footer"})
        assert r.status_code == 200, r.text
        r = c.patch(f"/api/projects/{pid}", json={"bubble_style": '{"opacity":40,"font_size":26}'})
        assert r.status_code == 200, r.text
        body = c.get("/api/projects").json()
        proj = next(p for p in (body["projects"] if isinstance(body, dict) else body)
                    if p["id"] == pid)
        assert proj["dialogue_mode"] == "footer"
        assert _json.loads(proj["bubble_style"]) == {"opacity": 40, "font_size": 26}
        # 非法：dialogue_mode 枚举、bubble_style 坏 JSON、opacity 越界
        assert c.patch(f"/api/projects/{pid}", json={"dialogue_mode": "loud"}).status_code == 422
        assert c.patch(f"/api/projects/{pid}", json={"bubble_style": "{bad"}).status_code == 422
        assert c.patch(f"/api/projects/{pid}", json={"bubble_style": '{"opacity":300}'}).status_code == 422
