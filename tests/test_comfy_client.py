# tests/test_comfy_client.py
import pytest

from comic_studio.engine.comfy.client import ComfyClient, ComfyUnreachable
from comfy_mock import comfy_server


def test_health_and_unreachable():
    with comfy_server("ok") as m:
        assert ComfyClient(m.base_url).health()["system"]["os"] == "mock"
    with pytest.raises(ComfyUnreachable):
        ComfyClient("http://127.0.0.1:1").health()


def test_upload_submit_free():
    import pathlib, tempfile
    with comfy_server("ok") as m:
        c = ComfyClient(m.base_url)
        tmp = pathlib.Path(tempfile.mkstemp(suffix=".png")[1])
        tmp.write_bytes(b"\x89PNG...")
        c.upload_image(tmp, "cs__p__a__front.png")
        assert m.uploads == ["cs__p__a__front.png"]
        pid = c.submit({"6": {"class_type": "X", "inputs": {}}}, client_id="c1")
        assert pid == "p1" and m.prompts[0]["client_id"] == "c1"
        c.free()
        assert m.frees == 1
        tmp.unlink()
