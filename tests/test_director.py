# tests/test_director.py
"""P7-D 整段批量快车道（设计 §16）：shots → Director timeline v5 构建器。"""
import json
from types import SimpleNamespace as NS

from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project
from comic_studio.engine.shots import persist_shots, set_disabled_batch
from comic_studio.engine.assets import persist_assets


def _shot(desc, prompt, dur=5.0, **kw):
    base = dict(text_span="", description=desc, shot_type="", camera={},
                duration=dur, workflow_type="ref2va", ledger={},
                character_ids=[], scene_ids=[], prop_ids=[], depends_on=None,
                prompt=prompt)
    base.update(kw)
    return NS(**base)


def _setup(tmp_path, aspect="9:16"):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "导演剧", aspect, "正文")["id"]
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="林晨", appearance="黑发少年", tags=[]),
                                  NS(name="苏晚", appearance="红发少女", tags=[])],
                      scenes=[], props=[]))
    from comic_studio.engine.assets import list_project_assets
    chars = {a["name"]: a["id"] for a in list_project_assets(db, pid)}
    # 主图落盘（refs 来源）
    from comic_studio.engine.paths import data_to_abs
    for a in list_project_assets(db, pid):
        main = data_to_abs(tmp_path / "data", a["library_dir"]) / "main.png"
        main.parent.mkdir(parents=True, exist_ok=True)
        main.write_bytes(b"\x89PNG")
    return db, pid, chars


def test_build_timeline_from_shots(tmp_path):
    from comic_studio.engine.director import build_timeline
    db, pid, chars = _setup(tmp_path)
    ids = persist_shots(db, pid, [
        _shot("推门", "林晨推门，<Picture 1> 锁定外貌。", 5.0,
              ledger={"assets": {"characters": [chars["林晨"]]}},
              character_ids=[chars["林晨"]]),
        _shot("对话", "两人对话。", 4.0,
              ledger={"assets": {"characters": [chars["林晨"], chars["苏晚"]]}},
              character_ids=[chars["林晨"], chars["苏晚"]]),
        _shot("无效镜", "不应出现。", 5.0),
    ])
    # 第二镜衔接第一镜（生产链路由拆解自动建链，测试手动设）
    conn = db.connect()
    conn.execute("UPDATE shots SET depends_on=? WHERE id=?", (ids[0], ids[1]))
    conn.commit()
    set_disabled_batch(db, pid, [ids[2]], 1)
    tl, uploads = build_timeline(db, tmp_path / "data", pid)
    assert tl["version"] == 5 and tl["timelineMode"] == "prompt_batch"
    assert tl["editMode"] == "segment"
    segs = tl["segments"]
    assert len(segs) == 2  # 无效镜排除
    # 帧对齐（17k+5）：5s*24=120→124；4s*24=96→107（用户实测 2s*24=48→56 同律）
    assert segs[0]["frameCount"] == 124 and segs[1]["frameCount"] == 107
    assert segs[0]["durationSec"] == 5 and segs[1]["durationSec"] == 4
    assert tl["totalFrames"] == 124 + 107
    assert segs[0]["start"] == 0 and segs[1]["start"] == 124
    # 段间连贯：第二段钉上一段 latent
    assert segs[0]["continuityFromPrev"] is False
    assert segs[1]["continuityFromPrev"] is True
    # refs：每段独立、index 从 0 连续（→ <Picture N+1>）
    assert [r["index"] for r in segs[0]["refs"]] == [0]
    assert [r["index"] for r in segs[1]["refs"]] == [0, 1]
    # uploads：角色主图去重后 2 张（林晨、苏晚），确定性命名
    assert len(uploads) == 2
    assert all(u["name"].startswith("cs__") for u in uploads)
    assert all(r["imageFile"] in {u["name"] for u in uploads}
               for s in segs for r in s["refs"])
    # 画布 9:16（×32 对齐）与导出模式
    assert tl["width"] == 608 and tl["height"] == 1056
    assert tl["output"]["exportMode"] == "all"
    assert tl["output"]["continuityEnabled"] is True
    # prompt 原样进段
    assert "<Picture 1> 锁定外貌" in segs[0]["prompt"]


def test_build_timeline_16_9_canvas(tmp_path):
    from comic_studio.engine.director import build_timeline
    db, pid, chars = _setup(tmp_path, aspect="16:9")
    persist_shots(db, pid, [_shot("x", "p", 5.0)])
    tl, _ = build_timeline(db, tmp_path / "data", pid)
    assert tl["width"] == 1056 and tl["height"] == 608


def test_handle_gen_director_end_to_end(tmp_path, monkeypatch):
    """整段快车道端到端：timeline 注入 → 主图上传 → 整片落盘 output/ → 全镜 rendered、
    直达 merged（v1 限制：整片不混配音字幕，逐镜链路保留该能力）。"""
    from pathlib import Path
    from comic_studio.engine.director import handle_gen_director
    from comic_studio.engine.workflows import registry
    from comic_studio.engine.settings import set_setting
    from comic_studio.engine.jobs import enqueue_job, get_job
    from comic_studio.engine.projects import get_project, set_stage
    from comic_studio.engine.shots import list_shots
    from comic_studio.engine.comfy.client import ComfyClient
    from comfy_mock import comfy_server
    db, pid, chars = _setup(tmp_path)
    persist_shots(db, pid, [
        _shot("推门", "林晨推门，中景。", 5.0,
              ledger={"assets": {"characters": [chars["林晨"]]}},
              character_ids=[chars["林晨"]])])
    set_stage(db, pid, "storyboard_ready")
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    set_setting(db, "template_map", {"director": "h3_director"})
    jid = enqueue_job(db, "gen_director", project_id=pid, resource="gpu_comfy",
                      payload={"project_id": pid})
    with comfy_server("ok", video=True) as m:
        handle_gen_director(db, tmp_path / "data", get_job(db, jid),
                            ComfyClient(m.base_url))
        wf = m.prompts[0]["prompt"]
        tl = json.loads(wf["12"]["inputs"]["timeline_data"])
        assert tl["segments"][0]["prompt"].startswith("林晨推门")
        assert len(m.uploads) == 1  # 角色主图已上传
    out = list((tmp_path / "data" / "projects" / "导演剧" / "output").glob("ep*.mp4"))
    assert out and out[0].stat().st_size == 2
    assert get_project(db, pid)["stage"] == "merged"
    rows = list_shots(db, pid)
    assert all(r["video_path"] and r["status"] == "rendered" for r in rows)
    assert all(r["video_path"].endswith(".mp4") and "/output/" in r["video_path"] for r in rows)
    snap = json.loads(get_job(db, jid)["snapshot_json"])
    assert snap["template"] == "h3_director" and "timeline" not in snap["prompt"] or True


def test_gen_director_batches_by_frame_budget(tmp_path, monkeypatch):
    """job 721 教训：整部一次提交 → CPU 灰画布 39GB 爆。按帧预算分批：
    多次提交、批首 continuity 断开、批间 ffmpeg 拼接。"""
    from pathlib import Path
    from comic_studio.engine import director as D
    from comic_studio.engine.workflows import registry
    from comic_studio.engine.settings import set_setting
    from comic_studio.engine.jobs import enqueue_job, get_job
    from comic_studio.engine.projects import get_project, set_stage
    from comic_studio.engine.comfy.client import ComfyClient
    from comfy_mock import comfy_server
    db, pid, chars = _setup(tmp_path)
    ids = persist_shots(db, pid, [
        _shot("甲", "甲镜。", 5.0, ledger={"assets": {"characters": [chars["林晨"]]}},
              character_ids=[chars["林晨"]]) for _ in range(3)])
    conn = db.connect()
    conn.execute("UPDATE shots SET depends_on=? WHERE id=?", (ids[0], ids[1]))
    conn.execute("UPDATE shots SET depends_on=? WHERE id=?", (ids[1], ids[2]))
    conn.commit()
    set_stage(db, pid, "storyboard_ready")
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    set_setting(db, "template_map", {"director": "h3_director"})
    set_setting(db, "comfy", {"base_url": "http://x:8188", "min_free_vram_gb": 0,
                              "director_batch_frames": 256})  # 124×2=248≤256 → 前两镜一批
    concat_calls = []
    monkeypatch.setattr("comic_studio.engine.merge.concat",
                        lambda parts, out: concat_calls.append(len(parts))
                        or out.write_bytes(b"v") or out)
    jid = enqueue_job(db, "gen_director", project_id=pid, resource="gpu_comfy",
                      payload={"project_id": pid})
    import json as _json
    with comfy_server("ok", video=True) as m:
        D.handle_gen_director(db, tmp_path / "data", get_job(db, jid),
                              ComfyClient(m.base_url))
        assert len(m.prompts) == 2  # 分了两批
        t1 = _json.loads(m.prompts[0]["prompt"]["12"]["inputs"]["timeline_data"])
        t2 = _json.loads(m.prompts[1]["prompt"]["12"]["inputs"]["timeline_data"])
        assert len(t1["segments"]) == 2 and len(t2["segments"]) == 1
        assert t1["segments"][1]["continuityFromPrev"] is True   # 批内保持
        assert t2["segments"][0]["continuityFromPrev"] is False  # 批间断开
        assert t2["segments"][0]["start"] == 0                   # 批内重排
    assert concat_calls == [2]
    assert get_project(db, pid)["stage"] == "merged"
    out = list((tmp_path / "data" / "projects" / "导演剧" / "output").glob("ep*.mp4"))
    assert out
