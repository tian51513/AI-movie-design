# tests/test_comfy_wait.py
import pathlib
import tempfile

import pytest

from comic_studio.engine.comfy.client import ComfyClient, ComfyError, ComfyStalled
from comfy_mock import comfy_server


def test_wait_ok_and_download():
    with comfy_server("ok") as m:
        c = ComfyClient(m.base_url)
        pid = c.submit({}, "c1")
        images = c.wait_and_collect(pid, poll_interval=0.05)
        assert images == [{"filename": "cs_x.png", "subfolder": "", "type": "output"}]
        dest = pathlib.Path(tempfile.mkstemp(suffix=".png")[1])
        c.download("cs_x.png", "", "output", dest)
        assert dest.stat().st_size == 2
        dest.unlink()


def test_wait_error_mode_raises_with_messages():
    with comfy_server("error") as m:
        c = ComfyClient(m.base_url)
        with pytest.raises(ComfyError, match="节点 6"):
            c.wait_and_collect("p1", poll_interval=0.05)


def test_stall_triggers_interrupt():
    with comfy_server("hang") as m:
        c = ComfyClient(m.base_url)
        seen = []
        with pytest.raises(ComfyStalled):
            c.wait_and_collect("p1", stall_seconds=0.2, poll_interval=0.05,
                               on_interrupt=lambda: seen.append(1))
        assert m.interrupts == 1 and seen == [1]
