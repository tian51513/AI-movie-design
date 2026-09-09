# tests/test_api_projects.py
import io

from fastapi.testclient import TestClient

from comic_studio.web.app import create_app


def _client(tmp_path):
    app = create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data", start_workers=False)
    return TestClient(app)


def _upload(client, name="测试剧", ratio="9:16", text="第一章 正文"):
    return client.post("/api/projects", data={"name": name, "aspect_ratio": ratio},
                       files={"novel": ("chapter.txt", io.BytesIO(text.encode("utf-8")),
                                        "text/plain")})


def test_create_project_201(tmp_path):
    with _client(tmp_path) as c:
        resp = _upload(c)
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "测试剧" and body["stage"] == "created"
        assert "novel_path" not in body


def test_list_and_get(tmp_path):
    with _client(tmp_path) as c:
        _upload(c)
        listing = c.get("/api/projects").json()
        assert len(listing) == 1
        pid = listing[0]["id"]
        detail = c.get(f"/api/projects/{pid}")
        assert detail.status_code == 200 and detail.json()["slug"] == "测试剧"
        assert c.get("/api/projects/999").status_code == 404


def test_invalid_ratio_rejected(tmp_path):
    with _client(tmp_path) as c:
        resp = _upload(c, ratio="5:4")
        assert resp.status_code == 422


def test_five_aspect_ratios_accepted(tmp_path):
    """五档画幅（2026-08-30 需求）：9:16/16:9/3:4/4:3/1:1 建项目与创建后改画幅都放行。"""
    with _client(tmp_path) as c:
        for ratio in ("9:16", "16:9", "3:4", "4:3", "1:1"):
            r = _upload(c, name=f"剧{ratio}", ratio=ratio)
            assert r.status_code == 201, (ratio, r.text)
            assert r.json()["aspect_ratio"] == ratio
        pid = _upload(c, name="改画幅剧").json()["id"]
        r = c.patch(f"/api/projects/{pid}", json={"aspect_ratio": "3:4"})
        assert r.status_code == 200
        assert c.get(f"/api/projects/{pid}").json()["aspect_ratio"] == "3:4"
        assert c.patch(f"/api/projects/{pid}", json={"aspect_ratio": "5:4"}).status_code == 422


def test_gbk_upload_rejected_422(tmp_path):
    """GBK 编码文件应返回 422 而非 500。"""
    with _client(tmp_path) as c:
        gbk_bytes = "中文".encode("gbk")
        resp = c.post("/api/projects",
                       data={"name": "g", "aspect_ratio": "9:16"},
                       files={"novel": ("f.txt", io.BytesIO(gbk_bytes), "text/plain")})
        assert resp.status_code == 422
        assert "UTF-8" in resp.text


def test_style_create_patch_roundtrip(tmp_path):
    with _client(tmp_path) as c:
        r = _upload(c).json()
        assert "style" in r  # 默认空串
        pid = r["id"]
        # 创建后改风格
        p = c.patch(f"/api/projects/{pid}", json={"style": "日系动漫风格，赛璐璐上色"})
        assert p.status_code == 200
        assert c.get(f"/api/projects/{pid}").json()["style"] == "日系动漫风格，赛璐璐上色"
        assert c.patch(f"/api/projects/{pid}", json={"style": ""}).json()["style"] == ""
        assert c.patch("/api/projects/999", json={"style": "x"}).status_code == 404


def test_create_with_style(tmp_path):
    with _client(tmp_path) as c:
        r = _upload(c, name="风格剧").json()
        # 带风格创建（multipart 直传）
        import io
        resp = c.post("/api/projects",
                      data={"name": "动漫剧", "aspect_ratio": "16:9", "style": "日系动漫风格"},
                      files={"novel": ("n.txt", io.BytesIO("文".encode()), "text/plain")})
        assert resp.status_code == 201
        assert resp.json()["style"] == "日系动漫风格"


def test_patch_video_params(tmp_path):
    with _client(tmp_path) as c:
        pid = _upload(c).json()["id"]
        r = c.patch(f"/api/projects/{pid}", json={
            "video_megapixels": 1.0, "video_multiple": 32,
            "video_speed": "高质量", "default_shot_duration": 6})
        assert r.status_code == 200
        body = c.get(f"/api/projects/{pid}").json()
        assert body["video_speed"] == "高质量" and body["video_megapixels"] == 1.0
        assert c.patch(f"/api/projects/{pid}", json={"video_speed": "极速"}).status_code == 422


def test_patch_style_and_video_params_compose(tmp_path):
    with _client(tmp_path) as c:
        pid = _upload(c).json()["id"]
        r = c.patch(f"/api/projects/{pid}", json={"style": "动漫风", "video_speed": "快速"})
        assert r.status_code == 200
        body = c.get(f"/api/projects/{pid}").json()
        assert body["style"] == "动漫风" and body["video_speed"] == "快速"


def test_patch_prompt_mode(tmp_path):
    with _client(tmp_path) as c:
        pid = _upload(c).json()["id"]
        r = c.patch(f"/api/projects/{pid}", json={"prompt_mode": "C"})
        assert r.status_code == 200 and r.json()["prompt_mode"] == "C"
        assert c.patch(f"/api/projects/{pid}", json={"prompt_mode": "X"}).status_code == 422


def test_projects_listing_enriched(tmp_path):
    """列表富信息（2026-08-27 需求）：摘要/字数/分镜数/创建与最近活动时间。"""
    import io
    from fastapi.testclient import TestClient
    from types import SimpleNamespace as NS
    from comic_studio.engine.shots import persist_shots
    from comic_studio.web.app import create_app
    with TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data",
                               start_workers=False)) as c:
        pid = c.post("/api/projects", data={"name": "富信息剧", "aspect_ratio": "16:9"},
                     files={"novel": ("n.txt", io.BytesIO(("晨光里的故事。" * 20).encode()),
                                      "text/plain")}).json()["id"]
        persist_shots(c.app.state.db, pid, [
            NS(text_span="", description="x", shot_type="", camera={}, duration=5.0,
               workflow_type="t2v", ledger={}, character_ids=[], scene_ids=[],
               prop_ids=[], depends_on=None, prompt="p")])
        item = next(p for p in c.get("/api/projects").json() if p["id"] == pid)
        assert item["excerpt"].startswith("晨光里")
        assert item["char_count"] == 140
        assert item["shot_count"] == 1
        assert item["created_at"]
        assert item["updated_at"] >= item["created_at"]  # 无任务时回退创建时间


def test_style_vis_roundtrip_and_patch(tmp_path):
    """画风拆层（2026-08-27 方案A）：style_vis 建/PATCH/查。"""
    import io
    from fastapi.testclient import TestClient
    from comic_studio.web.app import create_app
    with TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data",
                               start_workers=False)) as c:
        pid = c.post("/api/projects", data={
            "name": "拆层剧", "aspect_ratio": "16:9",
            "style": "剧情PV风格，叙事性构图，场景切换流畅，情绪递进",
            "style_vis": "电影质感，叙事性构图"},
            files={"novel": ("n.txt", io.BytesIO("正文".encode()), "text/plain")}).json()["id"]
        item = next(p for p in c.get("/api/projects").json() if p["id"] == pid)
        assert item["style_vis"] == "电影质感，叙事性构图"
        r = c.patch(f"/api/projects/{pid}", json={"style_vis": "胶片颗粒质感"})
        assert r.status_code == 200
        item = next(p for p in c.get("/api/projects").json() if p["id"] == pid)
        assert item["style_vis"] == "胶片颗粒质感"


def test_patch_autopilot_off_stops_jobs(tmp_path):
    """暂停联动全停（2026-09-04 设计A1）：autopilot 1→0 自动取消 pending、
    打断 running（attempts 打满）；0→1 与不含 autopilot 的 PATCH 不动任务。"""
    from comic_studio.engine import jobs as jobs_mod
    from comic_studio.engine.db import Database
    with _client(tmp_path) as c:
        pid = _upload(c).json()["id"]
        db = Database(tmp_path / "t.db"); db.migrate()
        conn = db.connect()
        conn.execute("UPDATE projects SET autopilot=1 WHERE id=?", (pid,))
        conn.commit()
        j1 = jobs_mod.enqueue_job(db, "gen_prompt", project_id=pid, payload={})
        j2 = jobs_mod.enqueue_job(db, "gen_prompt", project_id=pid, payload={})
        conn.execute("UPDATE jobs SET status='running' WHERE id=?", (j2,))
        conn.commit()
        assert c.patch(f"/api/projects/{pid}", json={"autopilot": False}).status_code == 200
        st = {r["id"]: r for r in conn.execute(
            "SELECT id, status, attempts FROM jobs WHERE project_id=?", (pid,))}
        assert st[j1]["status"] == "cancelled"
        assert st[j2]["attempts"] >= 99  # running 由 worker 收尾为 failed
        # 重新打开只翻开关，不杀任务
        j3 = jobs_mod.enqueue_job(db, "gen_prompt", project_id=pid, payload={})
        assert c.patch(f"/api/projects/{pid}", json={"autopilot": True}).status_code == 200
        assert conn.execute("SELECT status FROM jobs WHERE id=?", (j3,)).fetchone()[0] == "pending"


def test_novel_text_endpoint(tmp_path, monkeypatch):
    """2026-09-05 用户需求：详情页查看正文（上传小说/音频转写通用）。
    返回文本+字数+来源标记；正文文件缺失 404。"""
    with _client(tmp_path) as c:
        pid = _upload(c, text="林凡推门而入，雨水顺着发梢滴落。他愣住了。").json()["id"]
        r = c.get(f"/api/projects/{pid}/novel-text")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "林凡推门" in body["text"]
        assert body["char_count"] == len("林凡推门而入，雨水顺着发梢滴落。他愣住了。")
        assert body["from_audio"] is False
        # 音频项目标记（桩模块过依赖探测——Ruling-1 惯例）
        import io as _io
        import sys as _sys
        import types as _types
        _stub = _types.ModuleType("faster_whisper")
        _stub.WhisperModel = object
        monkeypatch.setitem(_sys.modules, "faster_whisper", _stub)
        r2 = c.post("/api/projects/from-audio",
                    data={"name": "音剧", "aspect_ratio": "16:9"},
                    files={"audio": ("a.mp3", _io.BytesIO(b"f"), "audio/mpeg")})
        pid2 = r2.json()["id"]
        body2 = c.get(f"/api/projects/{pid2}/novel-text").json()
        assert body2["from_audio"] is True
        # 删除正文文件 → 404
        (tmp_path / "data" / "projects" / "测试剧" / "novel.txt").unlink()
        assert c.get(f"/api/projects/{pid}/novel-text").status_code == 404


def test_novel_text_manual_edit(tmp_path, monkeypatch):
    """2026-09-06 用户需求：正文人工校正——LLM 校正外的第二条路，且 LLM 校正后
    可继续手改。PUT novel-text 写回 novel.txt + 重算章节；segments（音频时长锚）
    不动；空文本 422。"""
    with _client(tmp_path) as c:
        pid = _upload(c, text="第一章\n\n林凡推门而入。").json()["id"]
        # 音频项目：segments 在盘（音频时长锚）——编辑不得动它
        import sys as _sys
        import types as _types
        _stub = _types.ModuleType("faster_whisper")
        _stub.WhisperModel = object
        monkeypatch.setitem(_sys.modules, "faster_whisper", _stub)
        pid2 = c.post("/api/projects/from-audio",
                      data={"name": "音剧", "aspect_ratio": "16:9"},
                      files={"audio": ("a.mp3", io.BytesIO(b"f"), "audio/mpeg")}).json()["id"]
        from comic_studio.engine.asr import save_segments
        save_segments(tmp_path / "data", "音剧",
                      [{"start": 0.0, "end": 3.0, "text": "林凡推门而入"}])
        new_text = "第一章\n\n林凡推门而入，雨水顺着发梢滴落。他攥紧了拳。"
        r = c.put(f"/api/projects/{pid2}/novel-text", json={"text": new_text})
        assert r.status_code == 200, r.text
        assert r.json()["char_count"] == len(new_text)
        # 回读一致 + 章节重算
        got = c.get(f"/api/projects/{pid2}/novel-text").json()
        assert got["text"] == new_text and got["from_audio"] is True
        import json as _json
        row = c.get(f"/api/projects/{pid2}").json()
        assert row["stage"]  # 详情可用
        conn_chapters = c.app.state.db.connect().execute(
            "SELECT chapters_json FROM projects WHERE id=?", (pid2,)).fetchone()[0]
        assert _json.loads(conn_chapters)[0]["idx"] == 1   # 章节重算生效
        # segments 原样（音频锚未破坏）
        from comic_studio.engine.asr import load_segments
        assert load_segments(tmp_path / "data", "音剧") == [
            {"start": 0.0, "end": 3.0, "text": "林凡推门而入"}]
        # 空文本 422
        assert c.put(f"/api/projects/{pid2}/novel-text",
                     json={"text": "   "}).status_code == 422


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32  # 假 PNG 字节（from-comic 导入不解码，原样落盘）


def test_from_comic_redraw_flag(tmp_path):
    """from-comic 带 redraw=on → projects.redraw_characters=1（漫改忽略恒 0）。"""
    with _client(tmp_path) as c:
        files = [("images", ("p1.png", io.BytesIO(PNG), "image/png")),
                 ("images", ("p2.png", io.BytesIO(PNG), "image/png"))]
        r = c.post("/api/projects/from-comic", data={
            "name": "重绘漫", "aspect_ratio": "9:16", "comic_mode": "motion_comic",
            "redraw": "true"}, files=files)
        assert r.status_code == 201, r.text
        assert r.json()["redraw_characters"] == 1
        pid = r.json()["id"]
        r2 = c.patch(f"/api/projects/{pid}", json={"redraw_characters": False})
        assert r2.status_code == 200
        r3 = c.get(f"/api/projects/{pid}")
        assert r3.json()["redraw_characters"] == 0


def test_extract_comic_characters_redraw_payload(tmp_path):
    """终审：手动「🎭 提取角色」在动态漫+开重绘项目必须携带重绘链键
    （characters_only/bind_shots——与 autopilot extract_comic 分支同语义，
    缺键=按钮入队走漫改全量提取，重绘项目凭空建场景/道具且不绑镜）；
    普通项目（非重绘链）不带。"""
    import json as _json
    with _client(tmp_path) as c:
        files = [("images", (f"p{i}.png", io.BytesIO(PNG), "image/png"))
                 for i in (1, 2)]
        r = c.post("/api/projects/from-comic", data={
            "name": "手动提取重绘剧", "aspect_ratio": "9:16",
            "comic_mode": "motion_comic", "redraw": "true"}, files=files)
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        assert c.post(
            f"/api/projects/{pid}/extract-comic-characters").status_code == 202
        row = c.app.state.db.connect().execute(
            "SELECT payload_json FROM jobs WHERE project_id=? "
            "AND type='extract_comic_characters'", (pid,)).fetchone()
        payload = _json.loads(row["payload_json"])
        assert payload["characters_only"] is True
        assert payload["bind_shots"] is True
        # 对照：普通项目（小说链）payload 不带这两个键
        r2 = c.post("/api/projects", data={"name": "普通剧", "aspect_ratio": "9:16"},
                    files={"novel": ("n.txt", io.BytesIO("正文".encode()),
                                     "text/plain")})
        pid2 = r2.json()["id"]
        assert c.post(
            f"/api/projects/{pid2}/extract-comic-characters").status_code == 202
        row2 = c.app.state.db.connect().execute(
            "SELECT payload_json FROM jobs WHERE project_id=? "
            "AND type='extract_comic_characters'", (pid2,)).fetchone()
        payload2 = _json.loads(row2["payload_json"])
        assert "characters_only" not in payload2
        assert "bind_shots" not in payload2


def test_patch_redraw_toggle(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/api/projects/from-comic", data={
            "name": "t", "aspect_ratio": "9:16"}, files=[
            ("images", ("p.png", io.BytesIO(PNG), "image/png"))])
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        assert c.patch(f"/api/projects/{pid}", json={"redraw_characters": True}).json()["redraw_characters"] == 1
