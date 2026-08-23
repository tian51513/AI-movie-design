# tests/test_comfy_mock.py
import urllib.request

from comfy_mock import comfy_server


def test_mock_serves_full_cycle():
    with comfy_server("ok") as m:
        stats = urllib.request.urlopen(m.base_url + "/system_stats", timeout=3).read()
        assert b"system" in stats
        body = b'{"prompt": {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a"}}}, "client_id": "c1"}'
        req = urllib.request.Request(m.base_url + "/prompt", data=body,
                                     headers={"Content-Type": "application/json"})
        pid = urllib.request.urlopen(req, timeout=3).read().decode()
        assert '"prompt_id"' in pid
        hist = urllib.request.urlopen(m.base_url + "/history/p1", timeout=3).read().decode()
        assert "images" in hist and "cs_x.png" in hist
        img = urllib.request.urlopen(m.base_url + "/view?filename=cs_x.png", timeout=3).read()
        assert len(img) == 2
        req = urllib.request.Request(m.base_url + "/free", data=b"", method="POST")
        urllib.request.urlopen(req, timeout=3)  # 计数
        assert m.prompts[0]["client_id"] == "c1" and m.frees == 1


def test_mock_upload_records_filename():
    with comfy_server("ok") as m:
        boundary = "BND"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"cs__p__a__front.png\"\r\n"
                f"Content-Type: image/png\r\n\r\n").encode() + b"\x89PNG" + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(m.base_url + "/upload/image?overwrite=true", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        resp = urllib.request.urlopen(req, timeout=3).read().decode()
        assert "cs__p__a__front.png" in m.uploads
