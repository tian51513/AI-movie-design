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
        resp = _upload(c, ratio="4:3")
        assert resp.status_code == 422


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
