# tests/test_merge.py
import subprocess
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from comic_studio.engine.db import Database
from comic_studio.engine.merge import concat, ffmpeg_bin, merge_project, normalize, probe
from comic_studio.engine.projects import create_project, get_project, set_stage
from comic_studio.engine.shots import persist_shots, update_shot


def _make(src: Path, seconds=1, size="320x240") -> Path:
    subprocess.run(
        [ffmpeg_bin(), "-y", "-f", "lavfi", "-i",
         f"testsrc=duration={seconds}:size={size}:rate=10",
         "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True, timeout=60)
    return src


def test_probe_and_normalize_roundtrip(tmp_path):
    a = _make(tmp_path / "a.mp4", 1, "320x240")
    p = probe(a)
    assert p["width"] == 320 and p["fps"] > 0 and p["duration"] >= 0.9
    dst = normalize(a, tmp_path / "a_norm.mp4", 640, 360, 10)
    assert dst.exists() and dst.stat().st_size > 0
    p2 = probe(dst)
    assert p2["width"] == 640 and p2["height"] == 360


def test_concat_two_clips(tmp_path):
    a = normalize(_make(tmp_path / "a.mp4", 1, "320x240"), tmp_path / "a2.mp4", 640, 360, 10)
    b = normalize(_make(tmp_path / "b.mp4", 1, "320x240"), tmp_path / "b2.mp4", 640, 360, 10)
    out = concat([a, b], tmp_path / "ep.mp4")
    assert out.exists()
    p = probe(out)
    assert p["duration"] >= 1.8 and p["width"] == 640


def _proj_with_shots(tmp_path, name):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", name, "16:9", "t")["id"]
    set_stage(db, pid, "rendered")
    drafts = [NS(text_span="", description="x", shot_type="", camera={},
                 duration=5.0, workflow_type="ref2va", ledger={},
                 character_ids=[], scene_ids=[], prop_ids=[], depends_on=None,
                 prompt=f"提示{i}") for i in (1, 2)]
    sids = persist_shots(db, pid, drafts)
    for i, sid in zip((1, 2), sids):
        dest = tmp_path / "data" / "projects" / name / "shots" / str(i)
        dest.mkdir(parents=True, exist_ok=True)
        _make(dest / "video_v1.mp4", 1, "320x240")
        update_shot(db, sid, {"video_path": f"projects/{name}/shots/{i}/video_v1.mp4",
                             "status": "rendered"})
    return db, pid


def test_merge_project_end_to_end(tmp_path):
    db, pid = _proj_with_shots(tmp_path, "合成剧")
    out = merge_project(db, tmp_path / "data", pid)
    assert out.exists()
    assert probe(out)["duration"] >= 1.8
    assert get_project(db, pid)["stage"] == "merged"
    ep2 = merge_project(db, tmp_path / "data", pid)  # 幂等重跑 → ep002
    assert "ep002" in str(ep2)


def test_merge_missing_video_raises(tmp_path):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "缺片剧", "16:9", "t")["id"]
    set_stage(db, pid, "rendered")
    persist_shots(db, pid, [NS(text_span="", description="x", shot_type="",
        camera={}, duration=5.0, workflow_type="ref2va", ledger={},
        character_ids=[], scene_ids=[], prop_ids=[], depends_on=None,
        prompt="a")])
    with pytest.raises(ValueError, match="无视频"):
        merge_project(db, tmp_path / "data", pid)


def test_merge_canvas_five_ratios():
    """合成画布五档（2026-08-30 需求）；未知画幅回落 16:9（用户决策）。"""
    from comic_studio.engine.merge import _canvas
    assert _canvas("16:9") == (1920, 1080)
    assert _canvas("9:16") == (1080, 1920)
    assert _canvas("3:4") == (1440, 1920)
    assert _canvas("4:3") == (1920, 1440)
    assert _canvas("1:1") == (1920, 1920)
    assert _canvas("21:9") == (1920, 1080)  # 回落默认


def test_merge_mutes_quiet_shots_when_enabled(tmp_path):
    """无台词镜静音开关（2026-08-30）：comfy.mute_quiet_shots=True 时无 dialogue.mp3
    的镜音轨置零（H3 杂音不进成片）；默认关=保留原声。"""
    import re as _re
    from comic_studio.engine.settings import get_setting, set_setting

    def _make_loud(src: Path, seconds=1) -> Path:  # 带响亮正弦音轨的镜头视频
        subprocess.run([ffmpeg_bin(), "-y", "-f", "lavfi", "-i",
                        f"testsrc=duration={seconds}:size=320x240:rate=10",
                        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                        "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac",
                        str(src)], check=True, capture_output=True, timeout=60)
        return src

    def mean_volume(path):
        r = subprocess.run([ffmpeg_bin(), "-i", str(path), "-af", "volumedetect",
                            "-f", "null", "-"], capture_output=True, text=True, timeout=60)
        m = [l for l in (r.stderr or "").splitlines() if "mean_volume" in l]
        return float(_re.search(r"mean_volume:\s*(-?[\d.]+)", m[0]).group(1))

    db, pid = _proj_with_shots(tmp_path, "静音剧")
    # 换成带声音的视频（_proj_with_shots 默认无声）
    for i in (1, 2):
        _make_loud(tmp_path / "data" / "projects" / "静音剧" / "shots" / str(i) / "video_v1.mp4")
    out1 = merge_project(db, tmp_path / "data", pid)
    assert mean_volume(out1) > -30  # 默认：原声保留

    set_setting(db, "comfy", {"mute_quiet_shots": True})
    out2 = merge_project(db, tmp_path / "data", pid)
    assert mean_volume(out2) < -60  # 开关开：无台词镜全静音


def test_merge_skips_tts_for_native_voice(tmp_path, monkeypatch):
    """Phase 2 音色：渲染时注入过音色样本（ledger.h3_native_voice）→ 合成不再
    TTS 替换（保 H3 原声口型）。"""
    import json as _json
    calls = []
    monkeypatch.setattr("comic_studio.engine.merge._replace_audio",
                        lambda v, a, o: (calls.append(1), o)[1])
    db, pid = _proj_with_shots(tmp_path, "原声剧")
    for i in (1, 2):
        (tmp_path / "data" / "projects" / "原声剧" / "shots" / str(i)
         / "dialogue.mp3").write_bytes(b"mp3")
    from comic_studio.engine.shots import list_shots
    conn = db.connect()
    for s in list_shots(db, pid):
        ledger = _json.loads(s["ledger_json"] or "{}")
        ledger["h3_native_voice"] = True
        conn.execute("UPDATE shots SET ledger_json=? WHERE id=?",
                     (_json.dumps(ledger, ensure_ascii=False), s["id"]))
    conn.commit()
    out = merge_project(db, tmp_path / "data", pid)
    assert out.exists() and calls == []   # 未触发 TTS 替换


# ── C6 交叉淡化 + 统一调色（2026-09-01 台词驱动文档 C 级）──

def test_build_xfade_filter_offsets():
    from comic_studio.engine.merge import build_xfade_filter
    fc = build_xfade_filter([5.0, 4.0, 3.0], 0.3, grade=False)
    # offset_i = sum(dur[:i]) - i*fade → 4.7 / 8.4
    assert "xfade=transition=fade:duration=0.3:offset=4.700" in fc
    assert "xfade=transition=fade:duration=0.3:offset=8.400" in fc
    assert "eq=" not in fc


def test_build_xfade_filter_grade():
    from comic_studio.engine.merge import build_xfade_filter
    fc = build_xfade_filter([5.0, 4.0], 0.3, grade=True)
    assert "eq=gamma=1.05:contrast=1.02" in fc


def test_concat_xfade_shortens_by_fade(tmp_path):
    """真机 ffmpeg：两段各 2s，xfade 0.3 → 成片 ≈ 3.7s。"""
    from comic_studio.engine.merge import concat_xfade
    a = normalize(_make(tmp_path / "a.mp4", 2), tmp_path / "a_n.mp4", 640, 360, 10)
    b = normalize(_make(tmp_path / "b.mp4", 2), tmp_path / "b_n.mp4", 640, 360, 10)
    out = concat_xfade([a, b], tmp_path / "xf.mp4", fade=0.3)
    dur = probe(out)["duration"]
    assert abs(dur - 3.7) < 0.2, dur


def test_merge_project_respects_xfade_setting(tmp_path, monkeypatch):
    """开关开→走 xfade 链；段数超上限→回退硬拼。"""
    calls = {"xfade": 0, "concat": 0}
    monkeypatch.setattr("comic_studio.engine.merge.concat_xfade",
                        lambda parts, out, fade=0.3, grade=False: calls.__setitem__("xfade", calls["xfade"] + 1) or out)
    monkeypatch.setattr("comic_studio.engine.merge.concat",
                        lambda parts, out: calls.__setitem__("concat", calls["concat"] + 1) or out)
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "淡化剧", "16:9", "正文")["id"]
    set_stage(db, pid, "rendered")
    from comic_studio.engine.settings import set_setting
    vids = []
    for i in range(2):
        v = _make(tmp_path / f"v{i}.mp4", 1)
        vids.append(v)
    ids = persist_shots(db, pid, [
        NS(text_span="", description=f"镜{i}", shot_type="", camera={},
           duration=5.0, workflow_type="t2v", ledger={},
           character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)
        for i in range(2)])
    for sid, v in zip(ids, vids):
        update_shot(db, sid, {"video_path": str(v), "status": "rendered"})
    set_setting(db, "comfy", {"base_url": "", "merge_xfade": True})
    merge_project(db, tmp_path / "data", pid)
    assert calls["xfade"] == 1 and calls["concat"] == 0
    set_setting(db, "comfy", {"base_url": "", "merge_xfade": False})
    merge_project(db, tmp_path / "data", pid)
    assert calls["concat"] == 1


def test_burn_subtitles_filter_ascii_and_cwd(tmp_path, monkeypatch):
    r"""Windows 真机修复（2026-09-04 job 38665）：libavfilter 滤镜串里 `\` 是转义符
    （data\\projects\\... 被吃成 dataprojects...）、非 ASCII 项目名滤镜内打开不可靠
    → 滤镜参数只允许纯 ASCII 裸文件名，srt 目录用 cwd 提供。"""
    from comic_studio.engine import merge as merge_mod
    grabbed = {}

    def fake_run(cmd, **kw):
        grabbed["cmd"], grabbed["kw"] = cmd, kw
        Path(cmd[-1]).write_bytes(b"mp4")  # 产出 tmp 供 replace

    monkeypatch.setattr(merge_mod.subprocess, "run", fake_run)
    out = tmp_path / "武侠风云" / "output"
    out.mkdir(parents=True)
    video = out / "ep001.mp4"; video.write_bytes(b"v")
    srt = out / "subtitles.srt"; srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n台词\n")
    merge_mod._burn_subtitles(video, srt)
    cmd = grabbed["cmd"]
    vf = cmd[cmd.index("-vf") + 1]
    assert vf.startswith("subtitles=subtitles.srt:")  # 裸 ASCII 文件名
    assert "\\" not in vf and "武侠风云" not in vf
    assert grabbed["kw"].get("cwd") == str(srt.parent)
    assert video.read_bytes() == b"mp4"  # tmp 已原地替换


# ── B3 末帧定格补长（2026-09-05 设计B：对白说一半就截的合成端兜底）──

def test_replace_audio_pads_short_video(tmp_path):
    """2s 视频配 3.5s 音频：pad 后成片 ≈3.5s——末帧定格续到对白说完。"""
    from comic_studio.engine.merge import _replace_audio, probe
    v = _make(tmp_path / "v.mp4", 2)
    subprocess.run([ffmpeg_bin(), "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=3.5", str(tmp_path / "a.mp3")],
                   check=True, capture_output=True, timeout=60)
    out = _replace_audio(v, tmp_path / "a.mp3", tmp_path / "out.mp4", pad=1.6)
    d = probe(out)["duration"]
    assert abs(d - 3.5) < 0.5, d


def test_merge_pads_when_tts_longer_than_video(tmp_path, monkeypatch):
    """merge_project：配音长于视频 → _replace_audio 收到 pad≈差值+0.1 + warn 日志。"""
    import shutil as _sh
    from comic_studio.engine import merge as M
    from comic_studio.engine.db import Database
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.shots import persist_shots, update_shot
    grabbed = {}

    def fake_replace(video, audio, output, pad=0.0):
        grabbed["pad"] = pad
        _sh.copy(video, output)
        return output

    def fake_probe(p):
        return {"duration": 6.0 if str(p).endswith(".mp3") else 4.0,
                "width": 640, "height": 360, "fps": 25}

    monkeypatch.setattr(M, "_replace_audio", fake_replace)
    monkeypatch.setattr(M, "probe", fake_probe)
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "补长剧", "16:9", "正文")["id"]
    v = _make(tmp_path / "v.mp4", 1)
    (v.parent / "dialogue.mp3").write_bytes(b"fake-mp3")
    sid = persist_shots(db, pid, [
        NS(text_span="", description="镜1", shot_type="", camera={},
           duration=5.0, workflow_type="t2v", ledger={},
           character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)])[0]
    update_shot(db, sid, {"video_path": str(v), "status": "rendered"})
    M.merge_project(db, tmp_path / "data", pid)
    assert grabbed["pad"] > 2.0 and grabbed["pad"] < 2.2, grabbed
    n = db.connect().execute(
        "SELECT COUNT(*) c FROM logs WHERE message LIKE '%补长%'").fetchone()["c"]
    assert n == 1


def test_burn_subtitles_absolutizes_paths(tmp_path, monkeypatch):
    r"""服务真机续修（2026-09-05 job 38666）：服务 data_dir 为相对路径，烧字幕把
    cwd 换到 output 目录后，-i/输出若仍相对会跟着新 cwd 解析→文件凭空消失。
    进 subprocess 前一律绝对化（含 cwd 自身）。"""
    import os
    from comic_studio.engine import merge as merge_mod
    out = tmp_path / "data" / "proj" / "output"
    out.mkdir(parents=True)
    (out / "ep001.mp4").write_bytes(b"v")
    (out / "subtitles.srt").write_text("1\n")
    grabbed = {}

    def fake_run(cmd, **kw):
        grabbed["cmd"], grabbed["kw"] = cmd, kw
        Path(cmd[-1]).write_bytes(b"mp4")

    monkeypatch.setattr(merge_mod.subprocess, "run", fake_run)
    monkeypatch.chdir(tmp_path)
    merge_mod._burn_subtitles(Path("data/proj/output/ep001.mp4"),
                              Path("data/proj/output/subtitles.srt"))
    cmd = grabbed["cmd"]
    assert os.path.isabs(cmd[cmd.index("-i") + 1]), cmd   # 输入绝对化
    assert os.path.isabs(cmd[-1]), cmd                    # 输出绝对化
    assert os.path.isabs(grabbed["kw"]["cwd"]), grabbed   # cwd 绝对化


def _make_mp3_24k_mono(dest, seconds):
    subprocess.run([ffmpeg_bin(), "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}",
                    "-ar", "24000", "-ac", "1", str(dest)],
                   check=True, capture_output=True, timeout=60)
    return dest


def test_replace_audio_unifies_audio_params(tmp_path):
    """2026-09-05 真机成片无声/卡死根因：TTS mp3 是 24k 单声道，替换段未统一
    采样率/声道 → concat -c copy 把 24k mono 与 44.1k stereo 硬缝一个容器，
    时间戳全废（ep005 前段无声后段配音错位、ep006 全无声+拖不动）。
    替换段必须与 normalize 同参：44100 Hz 立体声（pad 路径同理）。"""
    from comic_studio.engine.merge import _replace_audio, probe
    v = _make(tmp_path / "v.mp4", 2)
    out = _replace_audio(v, _make_mp3_24k_mono(tmp_path / "a.mp3", 1.5),
                         tmp_path / "o.mp4")
    p = probe(out)
    assert p["sample_rate"] == 44100 and p["channels"] == 2, p
    out2 = _replace_audio(v, _make_mp3_24k_mono(tmp_path / "a2.mp3", 3.5),
                          tmp_path / "o2.mp4", pad=1.6)
    p2 = probe(out2)
    assert p2["sample_rate"] == 44100 and p2["channels"] == 2, p2


def test_probe_reports_audio_params_and_absence(tmp_path):
    """probe 暴露 sample_rate/channels；无音轨文件返回 None（拼接体检依据）。"""
    from comic_studio.engine.merge import probe
    p = probe(_make_mp3_24k_mono(tmp_path / "a.mp3", 1))
    assert p["sample_rate"] == 24000 and p["channels"] == 1
    v = probe(_make(tmp_path / "v.mp4", 1))  # _make 无音轨
    assert v["sample_rate"] is None and v["channels"] is None


def test_replace_audio_never_truncates_video(tmp_path):
    """2026-09-05 真机（ep006/007 片长 197s 应为 237s）：非补长路 -shortest 把
    「配音短于视频」的镜整段截到配音长度（镜19 4.5→1.0s，17 镜共截 ~40s），
    片长塌缩+字幕轴全面错位。改为 apad 静音补齐到视频全长，段长=视频长。"""
    from comic_studio.engine.merge import _replace_audio, probe
    v = _make(tmp_path / "v.mp4", 2)
    out = _replace_audio(v, _make_mp3_24k_mono(tmp_path / "a.mp3", 1.2),
                         tmp_path / "o.mp4")
    d = probe(out)["duration"]
    assert d > 1.9, d  # 视频全长保留（旧行为 1.2s）
