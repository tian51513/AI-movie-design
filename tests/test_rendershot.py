# tests/test_rendershot.py
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from comic_studio.engine.assets import get_asset, persist_assets
from comic_studio.engine.comfy.client import ComfyClient
from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project
from comic_studio.engine.rendershot import (
    SPEED_STEPS, collect_ref_images, pick_template_id, render_shot)
from comic_studio.engine.shots import get_shot, list_shots, persist_shots, update_shot
from comfy_mock import comfy_server


def _setup(tmp_path, **proj_kw):
    db = Database(tmp_path / "s.db"); db.migrate()
    kw = dict(style="真人电影", video_megapixels=0.6, video_speed="高质量",
              default_shot_duration=5.0, lora_realism=0.6)
    kw.update(proj_kw)
    pid = create_project(db, tmp_path / "data", "渲染剧", "16:9", "林晨推门。", **kw)["id"]
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="林晨", appearance="黑发", tags=[])],
                      scenes=[NS(name="庭院", description="古宅", tags=[])], props=[]))
    assets = {r["name"]: r for r in
              __import__("comic_studio.engine.assets", fromlist=["list_project_assets"])
              .list_project_assets(db, pid)}
    # 给两个资产放 sheet.png
    from comic_studio.engine.paths import data_to_abs
    for a in assets.values():
        views = data_to_abs(tmp_path / "data", a["library_dir"]) / "views"
        views.mkdir(parents=True, exist_ok=True)
        (views / "sheet.png").write_bytes(b"\x89PNG")
    return db, pid, assets


def _shot_draft(**kw):
    base = dict(text_span="", description="推门", shot_type="", camera={},
                duration=5.0, workflow_type="ref2va", ledger={},
                character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)
    base.update(kw)
    return NS(**base)


def test_pick_template_mapping():
    assert pick_template_id({"workflow_type": "ref2va"}) == "h3_ref2va"
    assert pick_template_id({"workflow_type": "fl2v"}) == "h3_fl2v"
    assert pick_template_id({"workflow_type": "t2v"}) == "h3_t2v"
    assert pick_template_id({"workflow_type": None}) == "h3_ref2va"


def test_collect_ref_images_slots(tmp_path):
    db, pid, assets = _setup(tmp_path)
    sid = persist_shots(db, pid, [_shot_draft(
        character_ids=[assets["林晨"]["id"]],
        scene_ids=[assets["庭院"]["id"]])])[0]
    refs = collect_ref_images(db, get_shot(db, sid))
    assert [r["slot"] for r in refs] == ["ref0", "ref1"]
    # 单资产：复制补第二槽
    sid2 = persist_shots(db, pid, [_shot_draft(
        character_ids=[assets["林晨"]["id"]])])[0]
    refs2 = collect_ref_images(db, get_shot(db, sid2))
    assert len(refs2) == 2 and refs2[0]["path"] == refs2[1]["path"]


def test_render_shot_end_to_end(tmp_path, monkeypatch):
    db, pid, assets = _setup(tmp_path)
    sid = persist_shots(db, pid, [_shot_draft(
        character_ids=[assets["林晨"]["id"]])])[0]
    update_shot(db, sid, {"prompt": "林晨在庭院推门，真人电影质感。"})
    from comic_studio.engine.workflows import registry
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    with comfy_server("ok", video=True) as m:
        out = render_shot(db, tmp_path / "data", sid, ComfyClient(m.base_url))
        assert out.exists() and out.stat().st_size == 2
        wf = m.prompts[0]["prompt"]
        assert wf["110"]["inputs"]["prompt"].startswith("林晨在庭院推门")
        assert wf["116"]["inputs"]["megapixels"] == 0.6
        assert wf["117"]["inputs"]["strength_model"] == 0.6
    shot = get_shot(db, sid)
    assert shot["status"] == "rendered"
    assert shot["video_path"] == f"projects/渲染剧/shots/1/video_v1.mp4"


def test_render_shot_raises_when_image_slots_empty_ref2va(tmp_path, monkeypatch):
    """I1: ref2va 镜绑定资产但两资产都无 sheet → collect_ref_images 返回 [] → raise."""
    db, pid, assets = _setup(tmp_path)
    sid = persist_shots(db, pid, [_shot_draft(
        character_ids=[assets["林晨"]["id"]],
        scene_ids=[assets["庭院"]["id"]])])[0]
    update_shot(db, sid, {"prompt": "林晨推门。"})
    from comic_studio.engine.workflows import registry
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    monkeypatch.setattr("comic_studio.engine.rendershot.collect_ref_images", lambda db, s: [])
    from comic_studio.engine.comfy.client import ComfyClient
    with comfy_server("ok", video=True) as m:
        with pytest.raises(ValueError, match="未提供"):
            render_shot(db, tmp_path / "data", sid, ComfyClient(m.base_url))


def test_render_shot_raises_when_fl2v_no_first_frame(tmp_path, monkeypatch):
    """I1: fl2v 关键帧生成失败且无任何帧 → 降级 h3_i2v 仍无 slot first → raise."""
    db, pid, assets = _setup(tmp_path)
    sid = persist_shots(db, pid, [_shot_draft(workflow_type="fl2v", character_ids=[])])[0]
    update_shot(db, sid, {"prompt": "推门。"})
    from comic_studio.engine.workflows import registry
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    monkeypatch.setattr("comic_studio.engine.rendershot.ensure_keyframes",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kf 失败")))
    from comic_studio.engine.comfy.client import ComfyClient
    with comfy_server("ok", video=True) as m:
        with pytest.raises(ValueError, match="未提供"):
            render_shot(db, tmp_path / "data", sid, ComfyClient(m.base_url))


def test_render_shot_t2v_no_image_check(tmp_path, monkeypatch):
    """I1: t2v 无 inject_images → 不受影响."""
    db, pid, _ = _setup(tmp_path)
    sid = persist_shots(db, pid, [_shot_draft(workflow_type="t2v", character_ids=[])])[0]
    update_shot(db, sid, {"prompt": "天空。"})
    from comic_studio.engine.workflows import registry
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    from comic_studio.engine.comfy.client import ComfyClient
    with comfy_server("ok", video=True) as m:
        out = render_shot(db, tmp_path / "data", sid, ComfyClient(m.base_url))
        assert out.exists()


def test_handle_gen_shot_registered_and_guard():
    """Step 1: 验证 handler 注册与守卫（非空 shot 校验）"""
    from comic_studio.engine.queue.worker import HANDLERS
    import comic_studio.engine.rendershot  # noqa: F401
    assert "gen_shot" in HANDLERS

    import tempfile
    import pathlib
    import json
    tmp = pathlib.Path(tempfile.mkdtemp())
    db = Database(tmp / "g.db")
    db.migrate()
    pid = create_project(db, tmp / "d", "g", "9:16", "t")["id"]
    # 手动插入 job（shot_id=999 不存在）以触发守卫
    # 禁用外键约束以插入无效 job
    conn = db.connect()
    conn.execute("PRAGMA foreign_keys = OFF")
    cur = conn.execute(
        "INSERT INTO jobs (project_id, shot_id, type, resource, payload_json, status) "
        "VALUES (?,?,?,?,?, 'pending')",
        (pid, 999, "gen_shot", "gpu_comfy", json.dumps({"shot_id": 999}, ensure_ascii=False))
    )
    jid = cur.lastrowid
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    from comic_studio.engine.jobs import get_job
    from comic_studio.engine.rendershot import handle_gen_shot
    with pytest.raises(ValueError, match="分镜"):
        handle_gen_shot(db, tmp / "d", get_job(db, jid), None)


def test_ref_slots_characters_priority(tmp_path):
    """回归（C 版教训）：双角色镜两张角色脸先占槽，场景退为文字。"""
    from comic_studio.engine.assets import list_project_assets, persist_assets
    from comic_studio.engine.paths import data_to_abs
    from types import SimpleNamespace as NS
    db, pid, assets = _setup(tmp_path)
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="第二角色", appearance="白发女性", tags=[])],
                      scenes=[], props=[]))
    second = next(a for a in list_project_assets(db, pid) if a["name"] == "第二角色")
    views = data_to_abs(tmp_path / "data", second["library_dir"]) / "views"
    views.mkdir(parents=True, exist_ok=True)
    (views / "sheet.png").write_bytes(b"\x89PNG2")
    sid = persist_shots(db, pid, [_shot_draft(
        character_ids=[assets["林晨"]["id"], second["id"]],
        scene_ids=[assets["庭院"]["id"]])])[0]
    refs = collect_ref_images(db, get_shot(db, sid))
    names = [get_asset(db, int(r["path"].split("/")[2]))["name"] for r in refs]
    assert names == ["林晨", "第二角色"], f"角色未优先占槽: {names}"
    # 单角色+场景：角色 ref0、场景 ref1（既有行为不变）
    sid2 = persist_shots(db, pid, [_shot_draft(
        character_ids=[assets["林晨"]["id"]],
        scene_ids=[assets["庭院"]["id"]])])[0]
    refs2 = collect_ref_images(db, get_shot(db, sid2))
    names2 = [get_asset(db, int(r["path"].split("/")[2]))["name"] for r in refs2]
    assert names2 == ["林晨", "庭院"]


def test_wide_shot_megapixels_boost(tmp_path, monkeypatch):
    """远景规避：远景镜自动升一档兆像素（上限 1.2）。"""
    db, pid, assets = _setup(tmp_path)
    sid = persist_shots(db, pid, [_shot_draft(
        character_ids=[assets["林晨"]["id"]], scene_ids=[],
        camera={"景别": "远景", "机位": "平视", "运镜": "固定", "转场": "切"})])[0]
    update_shot(db, sid, {"prompt": "远景测试"})
    from comic_studio.engine.workflows import registry
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    with comfy_server("ok", video=True) as m:
        from comic_studio.engine.comfy.client import ComfyClient
        render_shot(db, tmp_path / "data", sid, ComfyClient(m.base_url))
        wf = m.prompts[0]["prompt"]
        assert wf["116"]["inputs"]["megapixels"] == 1.0  # 0.6+0.4 升档


def test_render_versions_increment(tmp_path, monkeypatch):
    """多版本：连续渲染产出 video_v1 / video_v2，video_path 指向最新。"""
    db, pid, assets = _setup(tmp_path)
    sid = persist_shots(db, pid, [_shot_draft(
        character_ids=[assets["林晨"]["id"]], scene_ids=[])])[0]
    update_shot(db, sid, {"prompt": "第一版"})
    from comic_studio.engine.workflows import registry
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    with comfy_server("ok", video=True) as m:
        from comic_studio.engine.comfy.client import ComfyClient
        out1 = render_shot(db, tmp_path / "data", sid, ComfyClient(m.base_url))
        out2 = render_shot(db, tmp_path / "data", sid, ComfyClient(m.base_url))
    assert out1.name == "video_v1.mp4" and out2.name == "video_v2.mp4"
    assert get_shot(db, sid)["video_path"].endswith("video_v2.mp4")
    from comic_studio.engine.rendershot import shot_versions
    vs = shot_versions(tmp_path / "data", "渲染剧", 1)
    assert vs == ["video_v1.mp4", "video_v2.mp4"]


def test_version_sort_numeric_not_lexicographic(tmp_path):
    """v10 必须排在 v2 之后（字符串序 bug 回归）。"""
    from comic_studio.engine.rendershot import _shot_versions_in, _max_version_number
    d = tmp_path / "shots" / "1"
    d.mkdir(parents=True)
    for n in [1, 2, 10]:
        (d / f"video_v{n}.mp4").write_bytes(b"x")
    (d / "video.mp4").write_bytes(b"x")
    vs = _shot_versions_in(d)
    assert vs == ["video.mp4", "video_v1.mp4", "video_v2.mp4", "video_v10.mp4"]
    assert _max_version_number(vs) == 10


def test_fl2v_uses_both_keyframes_when_present(tmp_path, monkeypatch):
    """关键帧接线（方案A 一期）：fl2v → h3_fl2v；shots/<seq>/kf_start.png + kf_end.png
    → first/last 双槽上传（首尾帧插值）。"""
    db, pid, _ = _setup(tmp_path)
    sid = persist_shots(db, pid, [_shot_draft(workflow_type="fl2v", character_ids=[])])[0]
    update_shot(db, sid, {"prompt": "转身。"})
    from comic_studio.engine.workflows import registry
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    kd = tmp_path / "data" / "projects" / "渲染剧" / "shots" / "1"
    kd.mkdir(parents=True)
    (kd / "kf_start.png").write_bytes(b"\x89PNG")
    (kd / "kf_end.png").write_bytes(b"\x89PNG")
    from comic_studio.engine.comfy.client import ComfyClient
    with comfy_server("ok", video=True) as m:
        out = render_shot(db, tmp_path / "data", sid, ComfyClient(m.base_url))
        assert out.exists()
        assert any("__first" in u for u in m.uploads), m.uploads
        assert any("__last" in u for u in m.uploads), m.uploads


def test_fl2v_falls_back_to_i2v_without_end_frame(tmp_path, monkeypatch):
    """降级（生成失败时）：无 kf_end.png → 用 h3_i2v（仅首帧）；kf_start 作首帧。"""
    db, pid, _ = _setup(tmp_path)
    sid = persist_shots(db, pid, [_shot_draft(workflow_type="fl2v", character_ids=[])])[0]
    update_shot(db, sid, {"prompt": "转身。"})
    from comic_studio.engine.workflows import registry
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    monkeypatch.setattr("comic_studio.engine.rendershot.ensure_keyframes",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kf 失败")))
    kd = tmp_path / "data" / "projects" / "渲染剧" / "shots" / "1"
    kd.mkdir(parents=True)
    (kd / "kf_start.png").write_bytes(b"\x89PNG")
    from comic_studio.engine.comfy.client import ComfyClient
    with comfy_server("ok", video=True) as m:
        out = render_shot(db, tmp_path / "data", sid, ComfyClient(m.base_url))
        assert out.exists()
        assert any("__first" in u for u in m.uploads)
        assert not any("__last" in u for u in m.uploads)


def _mk_kf_shot(tmp_path, db_pid=None):
    """建 fl2v 分镜（无关键帧文件）。"""
    db, pid, _ = _setup(tmp_path)
    sid = persist_shots(db, pid, [_shot_draft(workflow_type="fl2v",
                                              description="沈雪柔转身拔剑")])[0]
    update_shot(db, sid, {"prompt": "动作描述"})
    return db, pid, sid


def test_ensure_keyframes_generates_pair(tmp_path, monkeypatch):
    """关键帧二期：fl2v 缺 kf 文件 → t2i 生成首尾对；同 seed 保构图、约束入词。"""
    from comic_studio.engine import rendershot
    from comic_studio.engine.workflows import registry
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    db, pid, sid = _mk_kf_shot(tmp_path)
    with comfy_server("ok") as m:  # 图像结果模式
        comfy = ComfyClient(m.base_url)
        ks, ke = rendershot.ensure_keyframes(db, tmp_path / "data", sid, comfy)
        assert ks.exists() and ke.exists()
        assert len(m.prompts) == 2
        seeds = [p["prompt"]["57:3"]["inputs"]["seed"] for p in m.prompts]
        assert seeds[0] == seeds[1]  # 同 seed：两帧构图一致
        texts = [p["prompt"]["57:27"]["inputs"]["text"] for p in m.prompts]
        assert all("同机位" in t and "构图" in t for t in texts)
        assert all("ultra-detailed" in t for t in texts)  # ZImage 尾缀
        assert "起始" in texts[0] and "结尾" in texts[1]


def test_ensure_keyframes_skips_existing(tmp_path, monkeypatch):
    from comic_studio.engine import rendershot
    from comic_studio.engine.workflows import registry
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    db, pid, sid = _mk_kf_shot(tmp_path)
    kd = tmp_path / "data" / "projects" / "渲染剧" / "shots" / "1"
    kd.mkdir(parents=True)
    (kd / "kf_start.png").write_bytes(b"x"); (kd / "kf_end.png").write_bytes(b"x")
    with comfy_server("ok") as m:
        rendershot.ensure_keyframes(db, tmp_path / "data", sid, ComfyClient(m.base_url))
        assert len(m.prompts) == 0  # 已有不重生成


def test_fl2v_render_appends_no_cut_constraint(tmp_path, monkeypatch):
    """fl2v 渲染提示词追加镜内禁切约束（首尾帧插值与 D 多镜提示词冲突的和解）。"""
    from comic_studio.engine.workflows import registry
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    db, pid, sid = _mk_kf_shot(tmp_path)
    kd = tmp_path / "data" / "projects" / "渲染剧" / "shots" / "1"
    kd.mkdir(parents=True)
    (kd / "kf_start.png").write_bytes(b"x"); (kd / "kf_end.png").write_bytes(b"x")
    with comfy_server("ok", video=True) as m:
        out = render_shot(db, tmp_path / "data", sid, ComfyClient(m.base_url))
        assert out.exists()
        sent = m.prompts[0]["prompt"]["64"]["inputs"]["prompt"]
        assert "插值" in sent and ("禁止镜内切换" in sent or "机位" in sent)


def test_ref2va_prev_tail_frame_takes_ref0(tmp_path, monkeypatch):
    """连贯性①配套（2026-08-26）：全链后 ref2va 镜带上镜尾帧——尾帧占 ref0
    （画面/姿态延续），角色参考占 ref1；不得再触发图片快失败。"""
    db, pid, assets = _setup(tmp_path)
    aid = assets["林晨"]["id"]
    sid = persist_shots(db, pid, [_shot_draft(
        workflow_type="ref2va", character_ids=[aid],
        ledger={"assets": {"characters": [aid], "scenes": [], "props": []}})])[0]
    update_shot(db, sid, {"prompt": "对话。"})
    from comic_studio.engine.workflows import registry
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    frame = tmp_path / "prev_tail.png"
    frame.write_bytes(b"\x89PNG")
    from comic_studio.engine.comfy.client import ComfyClient
    with comfy_server("ok", video=True) as m:
        out = render_shot(db, tmp_path / "data", sid, ComfyClient(m.base_url),
                          first_frame_png=frame)
        assert out.exists()  # 未触发快失败
        ups = m.uploads
        assert any(u.endswith("__ref0.png") for u in ups), ups
        assert any(u.endswith("__ref1.png") for u in ups), ups
        wf = m.prompts[0]["prompt"]
        assert wf["96"]["inputs"]["image"].endswith("__ref0.png")   # 尾帧占 ref0
        assert wf["97"]["inputs"]["image"].endswith("__ref1.png")   # 角色三视图占 ref1
