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
