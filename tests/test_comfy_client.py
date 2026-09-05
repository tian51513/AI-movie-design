# tests/test_comfy_client.py
import os
import pytest

from comic_studio.engine.comfy.client import ComfyClient, ComfyUnreachable
from comfy_mock import comfy_server


def test_health_and_unreachable():
    with comfy_server("ok") as m:
        assert ComfyClient(m.base_url).health()["system"]["os"] == "mock"
    # Bypass proxy for .invalid domains to ensure DNS failure
    old_no_proxy = os.environ.get("no_proxy", "")
    os.environ["no_proxy"] = ".invalid"
    try:
        with pytest.raises(ComfyUnreachable):
            ComfyClient("http://nonexistent-host.invalid").health()
    finally:
        os.environ["no_proxy"] = old_no_proxy


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


def test_upload_image_sends_overwrite():
    """同名上传必须带 overwrite（表单字段）：ComfyUI 默认不覆盖，
    漏掉则重生成/换主图后工作流仍加载旧图（真机 2026-08-25）。"""
    import pathlib as _p
    from comic_studio.engine.comfy.client import ComfyClient
    with comfy_server("ok") as m:
        f = _p.Path("/tmp/cs_up_test.png")
        f.write_bytes(b"x")
        ComfyClient(m.base_url).upload_image(f, "a.png")
        assert m.upload_overwrites == [True]
        f.unlink()


# ── H2c（2026-09-05 审计）：等待排队不计失速 + 只处置自己的任务 ──

def test_wait_pending_queue_not_counted_as_stall():
    """排队等待不计失速（此前 300s 从提交起算，排在前面的长渲染会把自己
    「等死」并 interrupt）；排队超宽限只把自己移出队列，不误杀在跑任务。"""
    import pytest
    from comic_studio.engine.comfy.client import ComfyClient, ComfyStalled
    with comfy_server(mode="hang", queue_pending=["p1"]) as s:
        c = ComfyClient(s.base_url)
        with pytest.raises(ComfyStalled) as ei:
            c.wait_and_collect("p1", stall_seconds=0.2, poll_interval=0.05,
                               queue_grace=0.4)
        assert "排队" in str(ei.value)
        assert s.interrupts == 0           # 没动全局 interrupt
        assert s.queue_deletes == ["p1"]   # 只移除自己


def test_wait_running_still_interrupts_on_stall():
    """自己在跑且失速 → 照旧 interrupt（旧行为保持）。"""
    import pytest
    from comic_studio.engine.comfy.client import ComfyClient, ComfyStalled
    with comfy_server(mode="hang", queue_running=["p1"]) as s:
        c = ComfyClient(s.base_url)
        with pytest.raises(ComfyStalled):
            c.wait_and_collect("p1", stall_seconds=0.2, poll_interval=0.05)
        assert s.interrupts == 1


def test_wait_unknown_state_raises_without_interrupt():
    """history 无果且不在队（如已淘汰/丢失）→ 报失速但绝不 interrupt
    ——全局 interrupt 会误杀 ComfyUI 当前在跑的他任务（分镜19 事故机制）。"""
    import pytest
    from comic_studio.engine.comfy.client import ComfyClient, ComfyStalled
    with comfy_server(mode="hang") as s:
        c = ComfyClient(s.base_url)
        with pytest.raises(ComfyStalled):
            c.wait_and_collect("p1", stall_seconds=0.2, poll_interval=0.05)
        assert s.interrupts == 0
