# tests/test_api_voices.py
"""Phase 2 音色 API：列表 / 上传（mock 引擎）/ 预设生成 / 删除 / 角色绑定。"""
import io
from pathlib import Path

from fastapi.testclient import TestClient

from comic_studio.web.app import create_app


def _client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data",
                                 start_workers=False))


def test_list_and_generate_preset(tmp_path, monkeypatch):
    with _client(tmp_path) as c:
        rows = c.get("/api/voices").json()
        names = [r["name"] for r in rows]
        assert "高冷御姐" in names and "萝莉" in names
        assert all(r.get("missing") for r in rows if r["origin"] == "preset")

        def fake_gen(comfy, data_dir, name, db=None):
            out = Path(data_dir) / "voices" / "presets" / f"{name}.flac"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"fLaC")
            return out
        monkeypatch.setattr("comic_studio.web.routes_voices.voicelib.generate_preset",
                            fake_gen)
        r = c.post("/api/voices/presets/generate", json={"name": "高冷御姐"})
        assert r.status_code == 200, r.text
        assert r.json()["path"] == "voices/presets/高冷御姐.flac"
        row = next(x for x in c.get("/api/voices").json() if x["name"] == "高冷御姐")
        assert "missing" not in row and row["path"].endswith(".flac")
        assert c.post("/api/voices/presets/generate",
                      json={"name": "不存在的"}).status_code == 422


def test_upload_and_delete_scopes(tmp_path, monkeypatch):
    with _client(tmp_path) as c:
        pid = c.post("/api/projects", data={"name": "音色剧", "aspect_ratio": "9:16"},
                     files={"novel": ("n.txt", io.BytesIO("正文".encode()),
                                      "text/plain")}).json()["id"]

        def fake_upload(comfy, data_dir, audio_path, *, name, start, dur, db=None):
            out = Path(data_dir) / "voices" / "_staging" / f"{name}.flac"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"fLaC")
            return out
        monkeypatch.setattr("comic_studio.web.routes_voices.voicelib.process_upload",
                            fake_upload)
        # ① 上传 → staging（未入正式库，列表不可见）
        r = c.post("/api/voices/upload",
                   data={"name": "试音", "scope": "global", "start": 45, "dur": 60},
                   files={"file": ("a.mp3", io.BytesIO(b"ID3"), "audio/mpeg")})
        assert r.status_code == 200, r.text
        staged = r.json()["staged"]
        assert staged == "voices/_staging/试音.flac" and r.json()["url"].startswith("/media/")
        assert not any(x["name"] == "试音" for x in c.get("/api/voices").json())
        # ② 试听满意 → 确认入库
        r2 = c.post("/api/voices/confirm",
                    json={"staged": staged, "name": "试音", "scope": "global"})
        assert r2.status_code == 200 and r2.json()["path"] == "voices/custom/试音.flac"
        assert any(x["name"] == "试音" for x in c.get("/api/voices").json())
        # ③ 项目级：上传→staging→confirm 带 project_id
        r3 = c.post("/api/voices/upload",
                    data={"name": "专属", "scope": "project", "project_id": pid,
                          "start": 0, "dur": 10},
                    files={"file": ("b.wav", io.BytesIO(b"RIFF"), "audio/wav")})
        staged3 = r3.json()["staged"]
        r4 = c.post("/api/voices/confirm",
                    json={"staged": staged3, "name": "专属", "scope": "project",
                          "project_id": pid})
        assert r4.status_code == 200 and "projects/音色剧/voices" in r4.json()["path"]
        row = next(x for x in c.get(f"/api/voices?project_id={pid}").json()
                   if x["name"] == "专属")
        assert row["origin"] == "project"
        # ④ 放弃：staging 删除
        r5 = c.post("/api/voices/upload",
                    data={"name": "丢弃", "scope": "global", "start": 0, "dur": 5},
                    files={"file": ("c.mp3", io.BytesIO(b"ID3"), "audio/mpeg")})
        r6 = c.post("/api/voices/discard", json={"staged": r5.json()["staged"]})
        assert r6.status_code == 200
        assert c.post("/api/voices/confirm",
                      json={"staged": r5.json()["staged"], "name": "丢弃",
                            "scope": "global"}).status_code == 404
        # ⑤ 防目录穿越
        assert c.post("/api/voices/confirm",
                      json={"staged": "../projects/音色剧/novel.txt", "name": "x",
                            "scope": "global"}).status_code in (404, 422)
        # ⑥ 删除正式音色
        assert c.delete("/api/voices/试音?scope=global").status_code == 200
        assert c.delete(f"/api/voices/专属?scope=project&project_id={pid}").status_code == 200
        assert c.delete("/api/voices/试音?scope=global").status_code == 404


def test_bind_asset_voice(tmp_path):
    with _client(tmp_path) as c:
        pid = c.post("/api/projects", data={"name": "绑定剧", "aspect_ratio": "9:16"},
                     files={"novel": ("n.txt", io.BytesIO("正文".encode()),
                                      "text/plain")}).json()["id"]
        from types import SimpleNamespace as NS
        from comic_studio.engine.assets import persist_assets
        db = c.app.state.db
        ids = persist_assets(db, tmp_path / "data", pid,
                             NS(characters=[NS(name="林晨", appearance="黑发", tags=[])],
                                scenes=[], props=[]))
        aid = ids[0]
        r = c.patch(f"/api/assets/{aid}/voice", json={"voice": "高冷御姐"})
        assert r.status_code == 200 and r.json()["voice"] == "高冷御姐"
        r2 = c.patch(f"/api/assets/{aid}/voice", json={"voice": ""})
        assert r2.json()["voice"] == ""


def test_design_generates_and_binds(tmp_path, monkeypatch):
    """/api/voices/design（R2 2026-09-02）：生成项目级音色 → 自动绑角色；
    资产不存在 404（生成前先校验，不白烧 ComfyUI）；空描述 422。"""
    with _client(tmp_path) as c:
        pid = c.post("/api/projects", data={"name": "配音剧", "aspect_ratio": "9:16"},
                     files={"novel": ("n.txt", io.BytesIO("正文".encode()),
                                      "text/plain")}).json()["id"]
        from types import SimpleNamespace as NS
        from comic_studio.engine.assets import persist_assets
        from comic_studio.engine.settings import set_setting
        ids = persist_assets(c.app.state.db, tmp_path / "data", pid,
                             NS(characters=[NS(name="林晨", appearance="黑发", tags=[])],
                                scenes=[], props=[]))
        aid = ids[0]
        # ComfyUI 空串门禁（2026-09-01 事故防线②：默认值带 8188，拦的是被存空的形态）
        set_setting(c.app.state.db, "comfy", {"base_url": ""})
        assert c.post("/api/voices/design", json={
            "project_id": pid, "name": "x", "instruction": "描述",
            "bind_asset_id": aid}).status_code == 422
        set_setting(c.app.state.db, "comfy", {"base_url": "http://127.0.0.1:8188"})

        def fake_gen(comfy, data_dir, project, name, instruction, db=None):
            out = Path(data_dir) / "projects" / project / "voices" / f"{name}.flac"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"fLaC")
            return out
        monkeypatch.setattr("comic_studio.web.routes_voices.voicelib.generate_custom",
                            fake_gen)
        r = c.post("/api/voices/design", json={
            "project_id": pid, "name": "林晨", "bind_asset_id": aid,
            "instruction": "清亮的青年男声"})
        assert r.status_code == 200, r.text
        assert r.json()["path"] == "projects/配音剧/voices/林晨.flac"
        row = c.app.state.db.connect().execute(
            "SELECT voice FROM assets WHERE id=?", (aid,)).fetchone()
        assert row["voice"] == "林晨"  # 生成后自动绑定（不能拿 PATCH 验——那是解绑）
        # 空描述 422；资产不存在 404
        assert c.post("/api/voices/design", json={
            "project_id": pid, "name": "x", "instruction": " "}).status_code == 422
        assert c.post("/api/voices/design", json={
            "project_id": pid, "name": "x", "instruction": "描述",
            "bind_asset_id": 9999}).status_code == 404


def test_promote_voice_to_custom_library(tmp_path):
    """/api/voices/promote（R4 2026-09-02）：项目级 → 全局自定义库；
    重名 409；项目级音色不存在 404。"""
    with _client(tmp_path) as c:
        pid = c.post("/api/projects", data={"name": "晋升剧", "aspect_ratio": "9:16"},
                     files={"novel": ("n.txt", io.BytesIO("正文".encode()),
                                      "text/plain")}).json()["id"]
        src = tmp_path / "data" / "projects" / "晋升剧" / "voices" / "林战.flac"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"fLaC")
        r = c.post("/api/voices/promote",
                   json={"project_id": pid, "name": "林战", "new_name": "林战·全局"})
        assert r.status_code == 200, r.text
        assert (tmp_path / "data" / "voices" / "custom" / "林战·全局.flac").exists()
        assert c.post("/api/voices/promote",
                      json={"project_id": pid, "name": "林战",
                            "new_name": "林战·全局"}).status_code == 409
        assert c.post("/api/voices/promote",
                      json={"project_id": pid, "name": "不存在"}).status_code == 404
