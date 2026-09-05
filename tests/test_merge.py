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

def test_merge_pads_when_tts_longer_than_video(tmp_path, monkeypatch):
    """merge_project：配音长于视频 → _replace_audio 收到 pad≈差值+0.1 + warn 日志。"""
    import shutil as _sh
    from comic_studio.engine import merge as M
    from comic_studio.engine.db import Database
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.shots import persist_shots, update_shot
    grabbed = {}

    def fake_replace(video, audio, output, target=0.0):
        grabbed["target"] = target
        _sh.copy(video, output)
        return output

    def fake_probe(p):
        return {"duration": 6.0 if str(p).endswith(".mp3") else 4.0,
                "width": 640, "height": 360, "fps": 25,
                "sample_rate": 44100, "channels": 2}

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
    assert 6.4 < grabbed["target"] < 6.6, grabbed  # 配音 6s + 0.5 呼吸
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


def test_probe_reports_audio_params_and_absence(tmp_path):
    """probe 暴露 sample_rate/channels；无音轨文件返回 None（拼接体检依据）。"""
    from comic_studio.engine.merge import probe
    p = probe(_make_mp3_24k_mono(tmp_path / "a.mp3", 1))
    assert p["sample_rate"] == 24000 and p["channels"] == 1
    v = probe(_make(tmp_path / "v.mp4", 1))  # _make 无音轨
    assert v["sample_rate"] is None and v["channels"] is None


# ── 音频收口（2026-09-05）：对白镜段长恒=配音+0.5s 呼吸 ──

def test_replace_audio_fits_target_length(tmp_path):
    """长者末帧定格补齐、短者截尾收口（H3 口型表演撑满整镜，配音说完即切，
    消灭「嘴动无声尾巴」）；音频参数统一 44100 立体声不回退。"""
    from comic_studio.engine.merge import _replace_audio, probe
    v5 = _make(tmp_path / "v5.mp4", 5)
    out1 = _replace_audio(v5, _make_mp3_24k_mono(tmp_path / "a12.mp3", 1.2),
                          tmp_path / "o1.mp4", target=1.7)
    assert abs(probe(out1)["duration"] - 1.7) < 0.4, probe(out1)["duration"]
    out2 = _replace_audio(_make(tmp_path / "v2.mp4", 2),
                          _make_mp3_24k_mono(tmp_path / "a35.mp3", 3.5),
                          tmp_path / "o2.mp4", target=4.0)
    p2 = probe(out2)
    assert abs(p2["duration"] - 4.0) < 0.4, p2["duration"]
    assert p2["sample_rate"] == 44100 and p2["channels"] == 2


def test_merge_trims_when_tts_shorter(tmp_path, monkeypatch):
    """merge_project：配音短于视频 → target=配音+0.5 收口 + 日志透明。"""
    import shutil as _sh
    from comic_studio.engine import merge as M
    from comic_studio.engine.db import Database
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.shots import persist_shots, update_shot
    grabbed = {}

    def fake_replace(video, audio, output, target=0.0):
        grabbed["target"] = target
        _sh.copy(video, output)
        return output

    def fake_probe(p):
        return {"duration": 2.0 if str(p).endswith(".mp3") else 4.0,
                "width": 640, "height": 360, "fps": 25,
                "sample_rate": 44100, "channels": 2}

    monkeypatch.setattr(M, "_replace_audio", fake_replace)
    monkeypatch.setattr(M, "probe", fake_probe)
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "收口剧", "16:9", "正文")["id"]
    v = _make(tmp_path / "v.mp4", 1)
    (v.parent / "dialogue.mp3").write_bytes(b"fake-mp3")
    sid = persist_shots(db, pid, [
        NS(text_span="", description="镜1", shot_type="", camera={},
           duration=5.0, workflow_type="t2v", ledger={},
           character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)])[0]
    update_shot(db, sid, {"video_path": str(v), "status": "rendered"})
    M.merge_project(db, tmp_path / "data", pid)
    assert 2.4 < grabbed["target"] < 2.6, grabbed
    n = db.connect().execute(
        "SELECT COUNT(*) c FROM logs WHERE message LIKE '%收口%'").fetchone()["c"]
    assert n == 1


def test_merge_handler_prepares_tts_and_srt(tmp_path, monkeypatch):
    """H2b（2026-09-05 审计）：配音/字幕前置挪进 merge 任务——手动合成与自动
    合成同待遇（此前手动 POST /merge 只拼旧音轨，TTS 仅 autopilot 线程做）；
    TTS 失败只 warn 不阻断合成。"""
    import json as _json
    from comic_studio.engine import merge as M
    from comic_studio.engine.db import Database
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.queue.worker import HANDLERS
    M.register_merge_handler()
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "前置剧", "16:9", "正文")["id"]
    calls = {"tts": 0, "srt": 0, "merge": 0}

    def fake_tts(db_, dd, pid_):
        calls["tts"] += 1
        return []

    def fake_srt(db_, dd, pid_):
        calls["srt"] += 1

    def fake_merge(db_, dd, pid_, job_id=None):
        calls["merge"] += 1
        return tmp_path / "out.mp4"

    monkeypatch.setattr("comic_studio.engine.tts.generate_dialogue_audio", fake_tts)
    monkeypatch.setattr("comic_studio.engine.subtitles.generate_srt", fake_srt)
    monkeypatch.setattr(M, "merge_project", fake_merge)
    job = {"id": 1, "project_id": pid,
           "payload_json": _json.dumps({"project_id": pid})}
    HANDLERS["merge"](db, tmp_path / "data", job, None)
    assert calls == {"tts": 1, "srt": 1, "merge": 1}

    # TTS 炸了 → warn 但合成照跑
    monkeypatch.setattr("comic_studio.engine.tts.generate_dialogue_audio",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("tts down")))
    calls.update(tts=0, srt=0, merge=0)
    HANDLERS["merge"](db, tmp_path / "data", job, None)
    assert calls["merge"] == 1
    n = db.connect().execute(
        "SELECT COUNT(*) c FROM logs WHERE message LIKE '%继续合成%'").fetchone()["c"]
    assert n == 1


def test_merge_rejects_same_video_all_shots(tmp_path):
    """M5：导演台产物全镜同 video_path——重合成会拼出「整片×N」废片，
    merge 入口防呆。"""
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "快车道剧", "16:9", "t")["id"]
    set_stage(db, pid, "rendered")
    v = _make(tmp_path / "v.mp4", 1)
    rel = "projects/快车道剧/output/whole.mp4"
    dest = tmp_path / "data" / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    import shutil as _sh
    _sh.copy(v, dest)
    sids = persist_shots(db, pid, [
        NS(text_span="", description=f"镜{i}", shot_type="", camera={},
           duration=5.0, workflow_type="t2v", ledger={},
           character_ids=[], scene_ids=[], prop_ids=[], depends_on=None,
           prompt=f"p{i}") for i in (1, 2)])
    for sid in sids:
        update_shot(db, sid, {"video_path": rel, "status": "rendered"})
    with pytest.raises(ValueError, match="整片"):
        merge_project(db, tmp_path / "data", pid)


# ── 2026-09-05 审计中危批次 B ──

def test_normalize_adds_silent_audio_track(tmp_path):
    """M9b：源无音轨 → normalize 补静音轨（此前部分段无轨进 concat -c copy
    会失败——与 09-05 无声事故同族）。"""
    v = _make(tmp_path / "v.mp4", 1)  # testsrc 无音轨
    assert probe(v)["sample_rate"] is None
    out = normalize(v, tmp_path / "n.mp4", 640, 360, 10)
    p = probe(out)
    assert p["sample_rate"] == 44100 and p["channels"] == 2


def test_ep_numbering_uses_max_plus_one(tmp_path, monkeypatch):
    """M10：编号=max+1 而非 len+1——删掉 ep002 后新片必须是 ep004，
    不许覆盖现存 ep003（数据丢失点）。"""
    db, pid = _proj_with_shots(tmp_path, "编号剧")
    out_dir = tmp_path / "data" / "projects" / "编号剧" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ep001.mp4").write_bytes(b"x")
    (out_dir / "ep003.mp4").write_bytes(b"x")
    out = merge_project(db, tmp_path / "data", pid)
    assert out.name == "ep004.mp4", out.name


def test_concat_xfade_leaves_no_sil_residue(tmp_path):
    """M9d：_ensure_audio 的 *_sil.mp4 落 output 目录残留——改临时目录。"""
    from comic_studio.engine.merge import concat_xfade
    a = _make(tmp_path / "a.mp4", 1)   # 无音轨 → 触发补轨
    b = _make(tmp_path / "b.mp4", 1)
    out = tmp_path / "out" / "xf.mp4"
    out.parent.mkdir()
    concat_xfade([a, b], out, fade=0.3)
    assert out.exists()
    assert list(out.parent.glob("*_sil*")) == []


def test_next_ep_number_max_plus_one(tmp_path):
    """QC-B：快车道与逐镜合成共用 max+1 编号（director.py 此前仍 len+1——
    删部分旧片后出片覆盖现存正片）。"""
    from comic_studio.engine.merge import next_ep_number
    out = tmp_path / "output"; out.mkdir()
    assert next_ep_number(out) == 1
    (out / "ep001.mp4").write_bytes(b"x")
    (out / "ep003.mp4").write_bytes(b"x")   # 手动删过 ep002
    assert next_ep_number(out) == 4


def test_merge_handler_skips_tts_when_busy(tmp_path, monkeypatch):
    """QC-D：merge 任务内 TTS 与手动 /tts 并发仍会双烧——engine 级互斥，
    merge 侧遇忙沿用现有音轨不炸。"""
    import json as _json
    from comic_studio.engine import merge as M
    from comic_studio.engine.db import Database
    from comic_studio.engine.projects import create_project
    from comic_studio.engine.queue.worker import HANDLERS
    from comic_studio.engine.tts import tts_busy_add, tts_busy_remove
    M.register_merge_handler()
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", "忙剧", "16:9", "t")["id"]
    calls = {"merge": 0}

    def bomb(*a, **kw):
        raise AssertionError("忙碌期不得再跑 TTS")

    monkeypatch.setattr("comic_studio.engine.tts.generate_dialogue_audio", bomb)
    monkeypatch.setattr(M, "merge_project",
                        lambda *a, **kw: calls.__setitem__("merge", calls["merge"] + 1)
                        or (tmp_path / "o.mp4"))
    job = {"id": 1, "project_id": pid,
           "payload_json": _json.dumps({"project_id": pid})}
    tts_busy_add(pid)
    try:
        HANDLERS["merge"](db, tmp_path / "data", job, None)   # 不得抛
    finally:
        tts_busy_remove(pid)
    assert calls["merge"] == 1
    n = db.connect().execute(
        "SELECT COUNT(*) c FROM logs WHERE message LIKE '%沿用现有音轨%'"
    ).fetchone()["c"]
    assert n == 1
