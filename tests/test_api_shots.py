# tests/test_api_shots.py
import io
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from comic_studio.engine.shots import persist_shots
from comic_studio.web.app import create_app


def _client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data",
                                 start_workers=False))


def _mk(c, name="分镜剧"):
    pid = c.post("/api/projects", data={"name": name, "aspect_ratio": "9:16"},
                 files={"novel": ("n.txt", io.BytesIO("正文".encode()), "text/plain")}).json()["id"]
    return pid


def _shot(desc="推门", **kw):
    base = dict(text_span="", description=desc, shot_type="动作", camera={"景别": "中景"},
                duration=5.0, workflow_type="ref2va", ledger={},
                character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)
    base.update(kw)
    return NS(**base)


def test_split_endpoint_guard_and_shots_listing(tmp_path):
    with _client(tmp_path) as c:
        pid = _mk(c)
        # stage=created → 409
        assert c.post(f"/api/projects/{pid}/split-storyboards").status_code == 409
        from comic_studio.engine.projects import set_stage
        set_stage(c.app.state.db, pid, "assets_ready")
        r = c.post(f"/api/projects/{pid}/split-storyboards")
        assert r.status_code == 202
        assert c.post(f"/api/projects/{pid}/split-storyboards").status_code == 409  # 拆解中
        # 直插 shots 供列表/PATCH/gate2 测试
        persist_shots(c.app.state.db, pid, [_shot(), _shot(desc="特写", workflow_type="fl2v")])
        shots = c.get(f"/api/projects/{pid}/shots").json()
        assert [s["seq"] for s in shots] == [1, 2]
        assert shots[0]["camera"]["景别"] == "中景"
        p = c.patch("/api/shots/%d" % shots[0]["id"], json={"prompt": "人工提示词"})
        assert p.status_code == 200
        assert any(s["prompt"] == "人工提示词" and s["status"] == "ready"
                   for s in c.get(f"/api/projects/{pid}/shots").json())


def test_gate2_requires_all_prompts(tmp_path):
    with _client(tmp_path) as c:
        pid = _mk(c)
        from comic_studio.engine.projects import set_stage
        set_stage(c.app.state.db, pid, "assets_ready")
        assert c.post(f"/api/projects/{pid}/gate2").status_code == 422  # 无分镜
        persist_shots(c.app.state.db, pid, [_shot(), _shot()])
        r = c.post(f"/api/projects/{pid}/gate2")
        assert r.status_code == 422 and "1" in r.json()["detail"] and "2" in r.json()["detail"]
        shots = c.get(f"/api/projects/{pid}/shots").json()
        for s in shots:
            c.patch(f"/api/shots/{s['id']}", json={"prompt": f"提示{s['seq']}"})
        assert c.post(f"/api/projects/{pid}/gate2").status_code == 200
        assert c.get(f"/api/projects/{pid}").json()["stage"] == "storyboard_ready"
        assert c.post(f"/api/projects/{pid}/gate2").status_code == 409


def test_patch_shot_duration_validation(tmp_path):
    """附带2: duration 非法值返回 422。"""
    with _client(tmp_path) as c:
        pid = _mk(c)
        from comic_studio.engine.projects import set_stage
        set_stage(c.app.state.db, pid, "assets_ready")
        ids = persist_shots(c.app.state.db, pid, [_shot()])
        sid = ids[0]
        r0 = c.patch(f"/api/shots/{sid}", json={"duration": ""})
        assert r0.status_code == 422
        r1 = c.patch(f"/api/shots/{sid}", json={"duration": 0})
        assert r1.status_code == 422
        r2 = c.patch(f"/api/shots/{sid}", json={"duration": 16})
        assert r2.status_code == 422


def test_regen_prompt_force_semantics(tmp_path):
    with _client(tmp_path) as c:
        pid = _mk(c)
        from comic_studio.engine.projects import set_stage
        set_stage(c.app.state.db, pid, "assets_ready")
        ids = persist_shots(c.app.state.db, pid, [_shot()])
        sid = ids[0]
        assert c.post(f"/api/shots/{sid}/regen-prompt").status_code == 202  # 空 prompt 直接生成
        from comic_studio.engine.shots import update_shot
        update_shot(c.app.state.db, sid, {"prompt": "已有", "status": "ready"})
        assert c.post(f"/api/shots/{sid}/regen-prompt").status_code == 409
        assert c.post(f"/api/shots/{sid}/regen-prompt", json={"force": True}).status_code == 202


def test_split_allowed_at_storyboard_ready(tmp_path):
    """H3（2026-09-05 审计）：storyboard_ready 显示「拆分分镜/重新拆解」按钮
    （confirm 承诺覆盖），后端却 409——UI 死路。放行重拆（引擎已做删镜清
    jobs 外键引用）。"""
    from comic_studio.engine.projects import set_stage
    with _client(tmp_path) as c:
        pid = _mk(c)
        set_stage(c.app.state.db, pid, "storyboard_ready")
        r = c.post(f"/api/projects/{pid}/split-storyboards", json={})
        assert r.status_code == 202, r.text


def test_stop_jobs_disables_autopilot(tmp_path):
    """M2：autopilot 开着时点「停止任务」自动关一键出片（此前 cancelled 不
    触发失败守卫，3s 后重入队——任务打不死）。"""
    from comic_studio.engine import jobs as jobs_mod
    with _client(tmp_path) as c:
        pid = _mk(c)
        conn = c.app.state.db.connect()
        conn.execute("UPDATE projects SET autopilot=1 WHERE id=?", (pid,))
        conn.commit()
        jobs_mod.enqueue_job(c.app.state.db, "gen_prompt", project_id=pid, payload={})
        r = c.post(f"/api/projects/{pid}/stop-jobs")
        assert r.status_code == 200
        assert c.app.state.db.connect().execute(
            "SELECT autopilot FROM projects WHERE id=?", (pid,)).fetchone()[0] == 0


def test_select_version_invalidates_dialogue_mp3(tmp_path):
    """M12：版本切换不失效配音（v2 画面迁就 v1 配音节奏）——切换即删旧
    dialogue.mp3（merge handler 会自动重生，删除是安全的）。"""
    from comic_studio.engine.shots import persist_shots, update_shot
    with _client(tmp_path) as c:
        pid = _mk(c, "版本剧")
        sid = persist_shots(c.app.state.db, pid, [_shot()])[0]
        d = tmp_path / "data" / "projects" / "版本剧" / "shots" / "1"
        d.mkdir(parents=True)
        for f in ("video_v1.mp4", "video_v2.mp4"):
            (d / f).write_bytes(b"v")
        (d / "dialogue.mp3").write_bytes(b"mp3")
        update_shot(c.app.state.db, sid,
                    {"video_path": "projects/版本剧/shots/1/video_v1.mp4"})
        r = c.post(f"/api/shots/{sid}/version",
                   json={"file": "video_v2.mp4"})
        assert r.status_code == 200, r.text
        assert not (d / "dialogue.mp3").exists()


def test_stop_jobs_targeted_queue_delete(tmp_path):
    """M13（2026-09-05 审计）：stop-jobs 的 clear_queue 是全局的——清掉他项目
    排在 ComfyUI 侧的 prompt。改为只删本项目 prompt_id + 仅本项目在跑才
    interrupt。"""
    import sys
    sys.path.insert(0, "tests")
    from comfy_mock import comfy_server
    from comic_studio.engine import jobs as jobs_mod
    from comic_studio.engine.settings import set_setting
    with _client(tmp_path) as c:
        pid = _mk(c, "定向停剧")
        db = c.app.state.db
        jid = jobs_mod.enqueue_job(db, "gen_shot", project_id=pid, payload={})
        conn = db.connect()
        conn.execute("UPDATE jobs SET status='running', comfy_prompt_id='px' WHERE id=?", (jid,))
        conn.execute("UPDATE projects SET autopilot=1 WHERE id=?", (pid,))
        conn.commit()
        with comfy_server("ok", queue_running=["px"],
                          queue_pending=["py"]) as m:  # py=他项目在 ComfyUI 排队
            set_setting(db, "comfy", {"base_url": m.base_url})
            r = c.post(f"/api/projects/{pid}/stop-jobs")
            assert r.status_code == 200
            assert m.interrupts == 0             # 温和停止（决策 A）：不打断
            assert m.queue_deletes == []          # 也不删 ComfyUI 队


def test_batch_redraw_route_guards(tmp_path):
    """非重绘项目 422；comfy 未配置 409；合法 202。"""
    with _client(tmp_path) as c:
        import io
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        files = [("images", ("p.png", io.BytesIO(png), "image/png")),
                 ("images", ("p2.png", io.BytesIO(png), "image/png"))]
        r = c.post("/api/projects/from-comic", data={
            "name": "路由剧", "aspect_ratio": "9:16"}, files=files)
        pid = r.json()["id"]
        assert c.post(f"/api/projects/{pid}/batch-redraw").status_code == 422
        assert c.patch(f"/api/projects/{pid}", json={"redraw_characters": True})
        from comic_studio.engine.settings import set_setting
        # comfy 默认地址非空——显式清空才能触发 409 门禁
        set_setting(c.app.state.db, "comfy", {"base_url": ""})
        assert c.post(f"/api/projects/{pid}/batch-redraw").status_code == 409
        assert c.post(f"/api/projects/99999/batch-redraw").status_code == 404
        set_setting(c.app.state.db, "comfy", {"base_url": "http://x:8188"})
        r = c.post(f"/api/projects/{pid}/batch-redraw")
        assert r.status_code == 202 and r.json() == {"enqueued": 2}


def _comic(c, name, redraw=True):
    """from-comic 建动态漫项目（2 页 → 2 镜；首镜 kf_start_v1/kf_end_v1 齐备）。"""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    files = [("images", (f"p{i}.png", io.BytesIO(png), "image/png")) for i in (1, 2)]
    r = c.post("/api/projects/from-comic", data={
        "name": name, "aspect_ratio": "9:16",
        "redraw": "true" if redraw else "false"}, files=files)
    return r.json()["id"]


def test_kf_versions_and_use(tmp_path):
    """kf 版本清单 + 切活动版（Task 9）：磁盘 glob 数字序 + 活动拷贝刷新。"""
    from comic_studio.engine.paths import data_to_abs
    with _client(tmp_path) as c:
        pid = _comic(c, "版本剧")
        sid = c.get(f"/api/projects/{pid}/shots").json()[0]["id"]
        v = c.get(f"/api/shots/{sid}/kf-versions").json()
        assert v["start"] == ["v1"] and v["active"]["start"] == "v1"
        assert v["end"] == ["v1"] and v["active"]["end"] == "v1"
        assert c.get("/api/shots/99999/kf-versions").status_code == 404
        # 造 v2 再切
        from comic_studio.engine.paths import data_to_abs
        d = data_to_abs(tmp_path / "data", "projects/版本剧/shots/1")
        (d / "kf_start_v2.png").write_bytes(b"\x89PNGxx")
        v = c.get(f"/api/shots/{sid}/kf-versions").json()
        assert v["start"] == ["v1", "v2"]
        r2 = c.post(f"/api/shots/{sid}/use-kf",
                    json={"role": "start", "version": "v2"})
        assert r2.status_code == 200 and r2.json()["start"] == "v2"
        assert (d / "kf_start.png").read_bytes() == b"\x89PNGxx"
        assert c.post("/api/shots/99999/use-kf",
                      json={"role": "start", "version": "v2"}).status_code == 404


def test_kf_versions_shot_without_files_defaults_v1(tmp_path):
    """ruling 3：无版本文件的镜（小说项目）GET 不报错——空清单 + 活动版默认 v1，
    GET 不做项目类型门禁。"""
    with _client(tmp_path) as c:
        pid = _mk(c, "小说剧")
        sid = persist_shots(c.app.state.db, pid, [_shot()])[0]
        v = c.get(f"/api/shots/{sid}/kf-versions").json()
        assert v == {"start": [], "end": [],
                     "active": {"start": "v1", "end": "v1"}}


def test_use_kf_rejects_bad_role_and_version(tmp_path):
    """路由是注入门禁（2026-09-09 评审）：role/version 白名单校验先于引擎
    （引擎把这两个值插值进文件路径，路径穿越等必须拦在 422）。"""
    with _client(tmp_path) as c:
        pid = _comic(c, "注入剧")
        sid = c.get(f"/api/projects/{pid}/shots").json()[0]["id"]
        for body in ({"role": "mid", "version": "v1"},
                     {"role": "start", "version": "v1/../../x"},
                     {"role": "start", "version": "vX"},
                     {"role": "start", "version": "v1; rm -rf"},
                     {"role": "start", "version": "v"},
                     {}):
            r = c.post(f"/api/shots/{sid}/use-kf", json=body)
            assert r.status_code == 422, (body, r.text)


def test_regen_keyframes_routes_to_redraw(tmp_path):
    """重绘项目单镜重生 → 202 入队 redraw_kf（不再同步 ensure_keyframes）。"""
    from comic_studio.engine.settings import set_setting
    import json as _json
    with _client(tmp_path) as c:
        pid = _comic(c, "单镜剧")
        sid = c.get(f"/api/projects/{pid}/shots").json()[0]["id"]
        set_setting(c.app.state.db, "comfy", {"base_url": "http://x:8188"})
        r2 = c.post(f"/api/shots/{sid}/regen-keyframes")
        assert r2.status_code == 202, r2.text
        assert isinstance(r2.json().get("job_id"), int)
        # ruling 2：无 list_jobs——直查 SQL
        rows = c.app.state.db.connect().execute(
            "SELECT * FROM jobs WHERE project_id=? AND type='redraw_kf'",
            (pid,)).fetchall()
        assert len(rows) == 1 and rows[0]["shot_id"] == sid
        payload = _json.loads(rows[0]["payload_json"])
        assert payload["shot_id"] == sid
        assert isinstance(payload.get("seed"), int) and 0 <= payload["seed"] <= 2 ** 31 - 1


def test_regen_keyframes_non_redraw_no_branch(tmp_path):
    """非重绘项目（动态漫但未开重绘）不走 redraw_kf 分流——保持原同步路径
    （comfy 空地址 → 409，且绝不入队 redraw_kf）。"""
    from comic_studio.engine.settings import set_setting
    with _client(tmp_path) as c:
        pid = _comic(c, "未开重绘剧", redraw=False)
        sid = c.get(f"/api/projects/{pid}/shots").json()[0]["id"]
        set_setting(c.app.state.db, "comfy", {"base_url": ""})
        r = c.post(f"/api/shots/{sid}/regen-keyframes")
        assert r.status_code == 409
        assert c.app.state.db.connect().execute(
            "SELECT COUNT(*) c FROM jobs WHERE project_id=? AND type='redraw_kf'",
            (pid,)).fetchone()["c"] == 0


def test_regen_keyframes_409_keeps_kf_files(tmp_path):
    """终审：门禁先于删帧——comfy 未配置 409 时活动关键帧必须原样留存
    （旧项目无 pages/，kf_start.png 是原图唯一副本，先删后检=不可逆丢数据）。"""
    from comic_studio.engine.settings import set_setting
    from comic_studio.engine.paths import data_to_abs
    from comic_studio.engine.projects import get_project
    with _client(tmp_path) as c:
        pid = _comic(c, "保帧剧", redraw=False)
        sid = c.get(f"/api/projects/{pid}/shots").json()[0]["id"]
        set_setting(c.app.state.db, "comfy", {"base_url": ""})
        slug = get_project(c.app.state.db, pid)["slug"]
        kd = data_to_abs(tmp_path / "data", f"projects/{slug}/shots/1")
        (kd / "kf_start.png").write_bytes(b"ORIGINAL-KF-BYTES")
        (kd / "kf_end.png").write_bytes(b"ORIGINAL-END-BYTES")
        r = c.post(f"/api/shots/{sid}/regen-keyframes")
        assert r.status_code == 409, r.text
        assert (kd / "kf_start.png").read_bytes() == b"ORIGINAL-KF-BYTES"
        assert (kd / "kf_end.png").read_bytes() == b"ORIGINAL-END-BYTES"


def test_stop_jobs_gentle_no_comfy_touch(tmp_path, monkeypatch):
    """温和停止（2026-09-05 用户决策 A）：stop-jobs 不再 interrupt/删队——
    ComfyUI 在跑任务跑完落盘（部分版本 interrupt 会崩实例）。"""
    import sys
    sys.path.insert(0, "tests")
    from comfy_mock import comfy_server
    from comic_studio.engine import jobs as jobs_mod
    with _client(tmp_path) as c:
        pid = _mk(c, "温停剧")
        db = c.app.state.db
        jid = jobs_mod.enqueue_job(db, "gen_shot", project_id=pid, payload={})
        conn = db.connect()
        conn.execute("UPDATE jobs SET status='running', comfy_prompt_id='px' "
                     "WHERE id=?", (jid,))
        conn.commit()
        with comfy_server("ok", queue_running=["px"],
                          queue_pending=["py"]) as m:
            from comic_studio.engine.settings import set_setting
            set_setting(db, "comfy", {"base_url": m.base_url})
            r = c.post(f"/api/projects/{pid}/stop-jobs")
            assert r.status_code == 200
            assert m.interrupts == 0           # 不打断在跑
            assert m.queue_deletes == []       # 不删 ComfyUI 队
        assert db.connect().execute(
            "SELECT autopilot FROM projects WHERE id=?",
            (pid,)).fetchone()[0] == 0         # M2 自动关 autopilot 保留
