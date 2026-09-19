# tests/test_musiclib.py
"""音乐库（2026-09-19 spec）：music3 模板 / 库 CRUD / LLM 建议 / gen_music /
API / 合成混入。本文件随任务逐段追加。"""
import json
from pathlib import Path

import pytest

from comic_studio.engine.workflows.registry import scan_templates


def test_music3_template():
    reg = scan_templates(Path("templates/workflows"))
    t = reg["music3"]
    assert t.type == "music"
    wf = t.api_json()
    assert wf["37:13"]["class_type"] == "MiniMaxMusic3TextEncode"
    assert wf["35"]["class_type"] == "SaveAudioAdvanced"
    assert t.inject_prompt.node == "37:13" and t.inject_prompt.field == "caption"
    for key, node, field in (("seed", "37:38", "seed"),
                             ("max_duration", "37:13", "max_duration"),
                             ("lyrics", "37:13", "lyrics")):
        ip = t.inject_params[key]
        assert (ip.node, ip.field) == (node, field), key
    assert {s.label for s in t.models} == {"unet", "clip", "vae"}


def test_migrate_43_music_table(tmp_path):
    from comic_studio.engine.db import Database
    db = Database(tmp_path / "s.db"); db.migrate()
    conn = db.connect()
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(music_library)")]
    assert {"id", "name", "caption", "lyrics", "seed", "duration",
            "origin", "path", "created_at"} <= set(cols)
    pcols = [r["name"] for r in conn.execute("PRAGMA table_info(projects)")]
    assert "bgm_music_id" in pcols and "bgm_volume" in pcols


def test_library_crud_roundtrip(tmp_path):
    from comic_studio.engine.db import Database
    from comic_studio.engine.musiclib import (delete_music, list_music,
                                              save_to_library)
    db = Database(tmp_path / "s.db"); db.migrate()
    src = tmp_path / "draft.mp3"; src.write_bytes(b"mp3data")
    entry = save_to_library(db, tmp_path, src, "夜晚钢琴", "Lo-fi 钢琴 70BPM",
                            "", 42, 120)
    assert entry["id"] and (tmp_path / "music/custom/夜晚钢琴.mp3").exists()
    assert list_music(db)[0]["name"] == "夜晚钢琴"
    with pytest.raises(ValueError, match="已存在"):
        save_to_library(db, tmp_path, src, "夜晚钢琴", "", "", 1, 60)
    delete_music(db, tmp_path, entry["id"])
    assert list_music(db) == [] and not (tmp_path / "music/custom/夜晚钢琴.mp3").exists()
    with pytest.raises(ValueError, match="音乐不存在"):
        delete_music(db, tmp_path, entry["id"])


class FakeLLM:
    def __init__(self, reply): self.reply, self.calls = reply, []
    def raw_chat(self, messages, temperature=0.5):
        self.calls.append(messages); return self.reply, None


def test_gen_music_handler(tmp_path, monkeypatch):
    """Task 4：gen_music 处理器——music3 注入 caption/lyrics/max_duration/seed，
    staging 落盘 music/_staging/<job_id>.<后缀>，snapshot 记相对路径（save 回读）。"""
    from comic_studio.engine.comfy.client import ComfyClient
    from comic_studio.engine.db import Database
    from comic_studio.engine import musiclib
    from comic_studio.engine.jobs import enqueue_job, get_job
    from comic_studio.engine.workflows import registry
    from comfy_mock import comfy_server
    db = Database(tmp_path / "s.db"); db.migrate()
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    jid = enqueue_job(db, "gen_music", project_id=None, resource="gpu_comfy",
                      payload={"caption": "Global Metadata: lo-fi, 70 BPM.",
                               "lyrics": "[主歌]\n夜色温柔", "seed": 7,
                               "duration": 60})
    with comfy_server("ok", audio=True) as m:
        dest = musiclib.handle_gen_music(db, tmp_path, get_job(db, jid),
                                         ComfyClient(m.base_url))
        prompt = m.prompts[0]["prompt"]
        wf = prompt["37:13"]["inputs"]
        assert wf["caption"].startswith("Global Metadata")
        assert wf["lyrics"].startswith("[主歌]") and wf["max_duration"] == 60
        # seed 字面注入在 SeedNode 37:38（37:13.seed 是链引用 ["37:38", 0]）
        assert prompt["37:38"]["inputs"]["seed"] == 7
    # 后缀跟随产物文件名（mock 产物 cs_x.flac → .flac，同 voicelib 行为）
    st = musiclib.staging_dir(tmp_path) / f"{jid}.flac"
    assert st.exists() and dest == st
    snap = json.loads(get_job(db, jid)["snapshot_json"])
    assert snap["workflow"]["staging"] == str(st.relative_to(tmp_path))
    assert snap["prompt"].startswith("Global Metadata")
    assert snap["template"] == "music3"
    # 缺 caption 直接拒绝（不白烧 ComfyUI）
    jid2 = enqueue_job(db, "gen_music", resource="gpu_comfy", payload={})
    with pytest.raises(ValueError, match="caption"):
        musiclib.handle_gen_music(db, tmp_path, get_job(db, jid2),
                                  ComfyClient(m.base_url))


def test_suggest_caption_and_lyrics(tmp_path, monkeypatch):
    from comic_studio.engine.db import Database
    from comic_studio.engine import musiclib
    db = Database(tmp_path / "s.db"); db.migrate()
    fake = FakeLLM("Global Metadata: Cinematic lo-fi, 72 BPM, A minor.\n情绪走向：由静谧渐至温暖。")
    monkeypatch.setattr(musiclib, "client_for_task", lambda db, task: fake)
    cap = musiclib.suggest_caption(db, "雨夜重逢的校园恋爱短剧")
    assert cap.startswith("Global Metadata") and "雨夜" in fake.calls[0][-1]["content"]
    fake2 = FakeLLM("[主歌]\n风穿过走廊\n[副歌]\n我想再见你一面")
    monkeypatch.setattr(musiclib, "client_for_task", lambda db, task: fake2)
    assert "[副歌]" in musiclib.suggest_lyrics(db, "毕业季告别")
    with pytest.raises(ValueError, match="曲风"):
        monkeypatch.setattr(musiclib, "client_for_task",
                            lambda db, task: FakeLLM("  "))
        musiclib.suggest_caption(db)


def test_save_to_library_name_whitelist(tmp_path):
    """安全评审（2026-09-19）：name 白名单防路径穿越写盘（引擎层防御，
    路由层 422 只是门面）。'a/b' 与 '../x' 均含 /，直接 ValueError 不落盘。"""
    from comic_studio.engine.db import Database
    from comic_studio.engine.musiclib import save_to_library
    db = Database(tmp_path / "s.db"); db.migrate()
    src = tmp_path / "draft.mp3"; src.write_bytes(b"mp3")
    for bad in ("a/b", "../x"):
        with pytest.raises(ValueError, match="非法字符"):
            save_to_library(db, tmp_path, src, bad, "", "", 1, 60)
    assert not (tmp_path / "music").exists()  # 未写盘


def test_delete_music_path_escape(tmp_path):
    """安全评审（2026-09-19）：库表脏数据（path 指向库外）删除必须拒绝，
    不越界 unlink data 根下的任意文件。"""
    from comic_studio.engine.db import Database
    from comic_studio.engine.musiclib import delete_music
    db = Database(tmp_path / "s.db"); db.migrate()
    evil = tmp_path / "evil.mp3"; evil.write_bytes(b"x")
    conn = db.connect()
    conn.execute(
        "INSERT INTO music_library (name, path) VALUES ('evil', '../evil.mp3')")
    conn.commit()
    mid = conn.execute(
        "SELECT id FROM music_library WHERE name='evil'").fetchone()[0]
    with pytest.raises(ValueError, match="越界"):
        delete_music(db, tmp_path, mid)
    assert evil.exists()  # 文件未被动


def test_mix_bgm_three_states(tmp_path, monkeypatch):
    """Task 6：_mix_bgm 三态——①无引用原样返回不触 ffmpeg；②有引用 amix 命令
    （-stream_loop -1 循环 + volume 音量 + duration=first 随片长 + -c:v copy
    零重编码）产出 _bgm.mp4 后覆盖引用；③库文件缺失 warn 降级原样返回。"""
    from comic_studio.engine import merge as M
    from comic_studio.engine.db import Database
    from comic_studio.engine.musiclib import save_to_library
    from comic_studio.engine.projects import create_project, get_project
    grabbed = {}

    def fake_run(cmd, **kw):
        grabbed["cmd"], grabbed["kw"] = cmd, kw
        Path(cmd[-1]).write_bytes(b"mp4")  # 产出 _bgm.mp4 供 move

    monkeypatch.setattr(M.subprocess, "run", fake_run)
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "混音剧", "16:9", "正文")["id"]
    final = tmp_path / "data" / "out" / "ep001.mp4"
    final.parent.mkdir(parents=True)
    final.write_bytes(b"v")

    # ① 无引用 → 原样返回、不触 ffmpeg
    assert M._mix_bgm(db, tmp_path / "data", get_project(db, pid), final) == final
    assert grabbed == {}

    # ② 有引用 → amix 混音 + 覆盖引用（epNNN.mp4 即成片，无 _bgm 残留）
    src = tmp_path / "draft.mp3"; src.write_bytes(b"music")
    mid = save_to_library(db, tmp_path / "data", src, "夜曲", "", "", 1, 60)["id"]
    conn = db.connect()
    conn.execute("UPDATE projects SET bgm_music_id=? WHERE id=?", (mid, pid))
    conn.commit()
    proj = get_project(db, pid)
    assert M._mix_bgm(db, tmp_path / "data", proj, final) == final
    cmd = grabbed["cmd"]
    assert "-stream_loop" in cmd and cmd[cmd.index("-stream_loop") + 1] == "-1"
    assert str(tmp_path / "data" / "music" / "custom" / "夜曲.mp3") in cmd
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert fc == ("[1:a]volume=0.2[bg];"
                  "[0:a][bg]amix=inputs=2:duration=first[a]")  # 默认音量 0.2
    assert cmd[cmd.index("-c:v") + 1] == "copy"   # 视频流零重编码
    assert final.read_bytes() == b"mp4"           # move 覆盖引用
    assert not final.with_name("ep001_bgm.mp4").exists()

    # vol 钳 0~0.5：超上限 → 0.5
    conn.execute("UPDATE projects SET bgm_volume=0.9 WHERE id=?", (pid,))
    conn.commit()
    M._mix_bgm(db, tmp_path / "data", get_project(db, pid), final)
    assert "volume=0.5" in grabbed["cmd"][grabbed["cmd"].index("-filter_complex") + 1]

    # ③ 库文件缺失 → warn 一条、原样返回不触 ffmpeg
    (tmp_path / "data" / "music" / "custom" / "夜曲.mp3").unlink()
    final.write_bytes(b"v2")
    grabbed.clear()
    assert M._mix_bgm(db, tmp_path / "data", get_project(db, pid), final) == final
    assert grabbed == {} and final.read_bytes() == b"v2"
    n = db.connect().execute("SELECT COUNT(*) c FROM logs "
                             "WHERE message LIKE '%配乐文件缺失%'").fetchone()["c"]
    assert n == 1


def test_music_api_matrix(tmp_path, monkeypatch):
    """Task 5：/api/music 全套。generate 入队 202（仓库入队端点惯例）+
    在飞 409 + 空 caption 422；save 从 job 快照回读 staging（无快照/文件缺
    409、穿越名 422、重名 409、正常 201）；discard 204→409；delete 204→404；
    suggest LLM 失败 502。"""
    from fastapi.testclient import TestClient
    from comic_studio.engine.db import Database
    from comic_studio.engine import musiclib
    from comic_studio.engine.jobs import attach_snapshot, get_job
    from comic_studio.web.app import create_app
    db = Database(tmp_path / "s.db"); db.migrate()
    with TestClient(create_app(tmp_path / "s.db", tmp_path / "data",
                               start_workers=False)) as c:
        # generate：caption 空 422
        r = c.post("/api/music/generate", json={"caption": "  "})
        assert r.status_code == 422
        # generate：正常入队 202 + jobs 表落 gen_music pending
        r = c.post("/api/music/generate",
                   json={"caption": "Global Metadata: lo-fi.", "seed": 3,
                         "duration": 60})
        assert r.status_code == 202 and r.json()["job_id"]
        jid = r.json()["job_id"]
        row = get_job(db, jid)
        assert row is not None and row["type"] == "gen_music" \
            and row["status"] == "pending"
        # generate：在飞互斥 409
        r2 = c.post("/api/music/generate",
                    json={"caption": "Global Metadata: lo-fi."})
        assert r2.status_code == 409

        # list：无快照 → staging 空、库空
        r = c.get("/api/music")
        assert r.status_code == 200
        assert r.json()["music"] == [] and r.json()["staging"] == []

        # save：job 不存在/无快照 → 409
        r4 = c.post("/api/music/save", json={"job_id": 999, "name": "x",
                                             "caption": "", "lyrics": "",
                                             "seed": 1, "duration": 60})
        assert r4.status_code == 409

        # 造 staging 文件 + 快照（正确写法 workflow={"staging": rel}）
        st = musiclib.staging_dir(tmp_path / "data") / f"{jid}.mp3"
        st.write_bytes(b"mp3")
        attach_snapshot(db, jid, prompt="Global Metadata: lo-fi.",
                        workflow={"staging": str(st.relative_to(tmp_path / "data"))})
        r = c.get("/api/music")
        assert r.json()["staging"] == [
            {"job_id": jid, "status": "pending",
             "path": str(st.relative_to(tmp_path / "data"))}]

        # save：穿越名 → 422（引擎白名单→路由映射）
        r = c.post("/api/music/save", json={"job_id": jid, "name": "../evil",
                                            "caption": "c", "lyrics": "",
                                            "seed": 1, "duration": 60})
        assert r.status_code == 422
        r = c.post("/api/music/save", json={"job_id": jid, "name": "a/b",
                                            "caption": "c", "lyrics": "",
                                            "seed": 1, "duration": 60})
        assert r.status_code == 422

        # save：正常 → 201；文件落 custom、库里可见
        r3 = c.post("/api/music/save", json={"job_id": jid, "name": "夜曲",
                                             "caption": "c", "lyrics": "",
                                             "seed": 1, "duration": 60})
        assert r3.status_code == 201 and r3.json()["id"]
        assert (tmp_path / "data/music/custom/夜曲.mp3").exists()
        assert r3.json()["path"] == "music/custom/夜曲.mp3"
        mid = r3.json()["id"]

        # save：重名 → 409
        r5 = c.post("/api/music/save", json={"job_id": jid, "name": "夜曲",
                                             "caption": "c", "lyrics": "",
                                             "seed": 1, "duration": 60})
        assert r5.status_code == 409

        # discard：删 staging → 204；再 discard → 409（文件已无）
        r = c.post("/api/music/discard", json={"job_id": jid})
        assert r.status_code == 204 and not st.exists()
        r = c.post("/api/music/discard", json={"job_id": jid})
        assert r.status_code == 409

        # delete：204 → 再删 404；库清空
        r = c.delete(f"/api/music/{mid}")
        assert r.status_code == 204
        assert c.get("/api/music").json()["music"] == []
        r = c.delete(f"/api/music/{mid}")
        assert r.status_code == 404

        # suggest：LLM 失败 → 502 包 detail；正常 → 200 {text}
        def boom(db, hint=""):
            raise ValueError("LLM 不可用")
        monkeypatch.setattr(musiclib, "suggest_caption", boom)
        r = c.post("/api/music/suggest-caption", json={"hint": "雨夜"})
        assert r.status_code == 502 and "LLM 不可用" in r.json()["detail"]
        monkeypatch.setattr(musiclib, "client_for_task",
                            lambda db, task: FakeLLM("[副歌]\n再见一面"))
        r = c.post("/api/music/suggest-lyrics", json={"hint": "毕业"})
        assert r.status_code == 200 and "[副歌]" in r.json()["text"]
