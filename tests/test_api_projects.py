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
