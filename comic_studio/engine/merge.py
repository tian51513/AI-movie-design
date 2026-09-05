# comic_studio/engine/merge.py
"""FFmpeg 合成：归一化 + concat（spec §10）。二进制来自 imageio-ffmpeg。"""
import json
import re
import subprocess
from pathlib import Path


def ffmpeg_bin() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def probe(path: Path) -> dict:
    """ffmpeg -i 解析 stderr（imageio-ffmpeg 不带 ffprobe）。"""
    r = subprocess.run([ffmpeg_bin(), "-i", str(path)], capture_output=True, timeout=60, text=True)
    info = r.stderr or ""
    m = re.search(r"Duration: (\d+):(\d+):(\d+)\.(\d+)", info)
    duration = 0.0
    if m:
        duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 100
    w = h = 0
    ms = re.findall(r"(\d{2,5})x(\d{2,5})", info)
    if ms:
        w, h = int(ms[-1][0]), int(ms[-1][1])
    fps = 0.0
    fm = re.search(r"([\d.]+) fps", info)
    if fm:
        fps = float(fm.group(1))
    # 音频参数（2026-09-05 成片无声事故：concat 段参数一致性体检依据）
    sample_rate = channels = None
    rm = re.search(r"(\d+) Hz", info)
    if rm:
        sample_rate = int(rm.group(1))
    cm = re.search(r", (mono|stereo)\b", info)
    if cm:
        channels = 1 if cm.group(1) == "mono" else 2
    return {"duration": duration, "width": w, "height": h, "fps": fps,
            "sample_rate": sample_rate, "channels": channels}


def normalize(src: Path, dst: Path, w: int, h: int, fps: float) -> Path:
    """归一化：scale+pad 到画布、统一 fps、crf18 yuv420p、补静音音轨保 concat 一致。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg_bin(), "-y", "-i", str(src),
         "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
               f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={fps}",
         "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
         "-f", "mp4", str(dst)],
        check=True, capture_output=True, timeout=300)
    return dst


def concat(parts: list, out: Path) -> Path:
    """concat demuxer -c copy；失败回退逐段拼接重编码。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    lst = out.parent / "concat_list.txt"
    lst.write_text("\n".join(f"file '{Path(p).resolve().as_posix()}'" for p in parts),
                   encoding="utf-8")
    r = subprocess.run(
        [ffmpeg_bin(), "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c", "copy", str(out)],
        capture_output=True, timeout=300)
    if r.returncode == 0 and out.exists():
        return out
    # 回退：逐段 concat filter 重编码
    args = [ffmpeg_bin(), "-y"]
    for p in parts:
        args += ["-i", str(p)]
    n = len(parts)
    filt = "".join(f"[{i}:0][{i}:1] " for i in range(n)) + \
        f"concat=n={n}:v=1:a=1[v][a]"
    args += ["-filter_complex", filt, "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2", str(out)]
    subprocess.run(args, check=True, capture_output=True, timeout=600)
    return out


# ── C6 交叉淡化 + 统一调色（2026-09-01 台词驱动文档 C 级）──
XFADE_SEC = 0.3
XFADE_MAX_PARTS = 120  # 段数上限：xfade 链须全量重编码，段过多命令/耗时爆炸→回退硬拼


def build_xfade_filter(durations: list, fade: float = XFADE_SEC,
                       grade: bool = False) -> str:
    """xfade 链 filter 字符串（视频交叉淡化 + 音频 acrossfade）。
    offset_i = sum(dur[:i]) - i*fade（前一链输出已含交叠扣减）。"""
    n = len(durations)
    segs = []
    prev_v, prev_a = "[0:v]", "[0:a]"
    for i in range(1, n):
        offset = sum(durations[:i]) - i * fade
        last = i == n - 1
        lv = "[vout]" if last else f"[xv{i}]"
        la = "[aout]" if last else f"[xa{i}]"
        segs.append(f"{prev_v}[{i}:v]xfade=transition=fade:duration={fade}"
                    f":offset={offset:.3f}{lv}")
        segs.append(f"{prev_a}[{i}:a]acrossfade=d={fade}{la}")
        prev_v, prev_a = lv, la
    if grade:
        segs.append("[vout]eq=gamma=1.05:contrast=1.02[vout2]")
    return ";".join(segs)


def concat_xfade(parts: list, out: Path, fade: float = XFADE_SEC,
                 grade: bool = False) -> Path:
    """交叉淡化拼接（整链重编码——观感顺滑的代价）。段 ==1 直接复制。
    无音轨段先补静音（acrossfade 要求两路音频流齐全）。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    if len(parts) == 1:
        import shutil
        shutil.copyfile(parts[0], out)
        return out
    parts = [_ensure_audio(Path(p), out.parent) for p in parts]
    durs = [probe(p)["duration"] for p in parts]
    fc = build_xfade_filter(durs, fade, grade)
    args = [ffmpeg_bin(), "-y"]
    for p in parts:
        args += ["-i", str(p)]
    args += ["-filter_complex", fc,
             "-map", "[vout2]" if grade else "[vout]", "-map", "[aout]",
             "-c:v", "libx264", "-crf", "20", "-preset", "fast",
             "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)]
    subprocess.run(args, check=True, capture_output=True, timeout=3600)
    return out


def _ensure_audio(src: Path, workdir: Path) -> Path:
    """无音轨 → 复制视频流并垫静音轨（视频直拷零重编码）。"""
    r = subprocess.run([ffmpeg_bin(), "-i", str(src)], capture_output=True,
                       timeout=60, text=True)
    if " Audio:" in (r.stderr or ""):
        return src
    dst = workdir / f"{src.stem}_sil.mp4"
    subprocess.run(
        [ffmpeg_bin(), "-y", "-i", str(src),
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
         "-shortest", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", str(dst)],
        check=True, capture_output=True, timeout=300)
    return dst


def _canvas(aspect_ratio: str) -> tuple:
    """合成画布：长边 1920 按画幅取比，短边偶数对齐（yuv420p 要求）。
    五档画幅通用解析（2026-08-30）；不支持的画幅回落默认 16:9（用户决策）。"""
    from .projects import ASPECT_RATIOS
    if aspect_ratio not in ASPECT_RATIOS:
        aspect_ratio = "16:9"
    w, h = (float(x) for x in aspect_ratio.split(":"))
    if w >= h:
        return (1920, int(1920 * h / w) // 2 * 2)
    return (int(1920 * w / h) // 2 * 2, 1920)


BREATH_SEC = 0.5  # 对白镜配音后的呼吸秒数（音频收口常量，2026-09-05）


def _replace_audio(video: Path, audio: Path, output: Path, target: float) -> Path:
    """对白镜音轨替换＋段长收口（2026-09-05 音频收口）：段长恒=target（配音+呼吸）。
    target>视频 → tpad 末帧定格补齐（需重编码，对白说得完）；
    target<视频 → 截尾收口（H3 口型表演常撑满整镜，配音说完即切——
    消灭「嘴动无声尾巴」）。音频 apad 静音补齐到 target，一律 44100 立体声
    （2026-09-05 真机无声事故：24k mono 与 44.1k stereo 混拼时间戳全废）。"""
    video = Path(video).resolve()
    audio = Path(audio).resolve()
    output = Path(output).resolve()
    v_dur = probe(video)["duration"] or target
    tail = ["-map", "0:v", "-map", "1:a", "-af", "apad",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-t", f"{target:.3f}", str(output)]
    if target > v_dur + 0.05:
        subprocess.run([ffmpeg_bin(), "-y", "-i", str(video), "-i", str(audio),
                        "-vf", f"tpad=stop_mode=clone:stop_duration={target - v_dur:.2f}",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                        *tail],
                       check=True, capture_output=True, timeout=600)
        return output
    subprocess.run([ffmpeg_bin(), "-y", "-i", str(video), "-i", str(audio),
                    "-c:v", "copy", *tail],
                   check=True, capture_output=True, timeout=300)
    return output


def _mute_audio(video: Path, output: Path) -> Path:
    """无台词镜静音（2026-08-30 杂音封堵）：画面保留，音轨置零——
    H3 原生残留杂音不进成片，代价是丢自然环境声（comfy.mute_quiet_shots）。"""
    subprocess.run([ffmpeg_bin(), "-y", "-i", str(video), "-af", "volume=0",
                    "-c:v", "copy", "-c:a", "aac", str(output)],
                   check=True, capture_output=True, timeout=300)
    return output


def _burn_subtitles(video: Path, srt: Path) -> None:
    """P6：SRT 字幕烧入成片（原地覆盖）。
    2026-09-04 Windows 真机修复（job 38665）：libavfilter 滤镜串里反斜杠是
    转义符（data\\projects\\... 被吃成 dataprojects...）、非 ASCII 项目名滤镜内
    打开不可靠 → 滤镜参数只给纯 ASCII 裸文件名，srt 所在目录用 cwd 传达。
    2026-09-05 续修（job 38666）：服务 data_dir 为相对路径，cwd 换目录后
    相对的 -i/输出跟着新 cwd 解析→凭空消失——进 subprocess 前一律绝对化。"""
    video = Path(video).resolve()
    srt = Path(srt).resolve()
    tmp = video.with_suffix(".sub_tmp.mp4")
    style = ("FontName=SimSun,FontSize=22,PrimaryColour=&H00FFFFFF&,"
             "OutlineColour=&H00000000&,Outline=2,Bold=1,MarginV=25")
    subprocess.run([ffmpeg_bin(), "-y", "-i", str(video),
                    "-vf", f"subtitles={srt.name}:force_style='{style}'",
                    "-c:a", "copy", str(tmp)],
                   check=True, capture_output=True, timeout=600, cwd=str(srt.parent))  # noqa: E501（srt 已绝对化）
    tmp.replace(video)


def merge_project(db, data_dir, project_id, job_id=None) -> Path:
    """按 seq 收集选用视频 → 归一化 → concat → output/epNNN.mp4；置 stage=merged。"""
    from .logbus import emit as emit_log
    from .projects import get_project, set_stage
    from .shots import list_shots

    proj = get_project(db, project_id)
    if proj is None:
        raise ValueError(f"项目不存在: {project_id}")
    shots = list_shots(db, project_id)
    # 无效镜（disabled=1）不进合成：守卫与 concat 都只看生效镜（2026-08-27 需求）
    shots = [s for s in shots if not s["disabled"]]
    missing = [s["seq"] for s in shots if not s["video_path"]]
    if not shots or missing:
        raise ValueError(f"无法合成：以下镜头无视频: {missing or '（无分镜）'}")
    # M5（2026-09-05 审计）：导演台产物全镜指同一整片——逐镜合成会拼出
    # 「整片×N」废片，入口防呆
    if len(shots) > 1 and len({s["video_path"] for s in shots}) == 1:
        raise ValueError("全部镜头指向同一整片（导演台产物）——逐镜重合成会拼出"
                         "整片×N；如需重出片请走快车道或重拆")
    w, h = _canvas(proj["aspect_ratio"])
    out_dir = Path(data_dir) / "projects" / proj["slug"] / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    n = len(list(out_dir.glob("ep*.mp4"))) + 1
    out = out_dir / f"ep{n:03d}.mp4"
    import tempfile
    from .settings import get_setting
    mute_quiet = bool((get_setting(db, "comfy") or {}).get("mute_quiet_shots", False))
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        parts = []
        for s in shots:
            src = Path(data_dir) / s["video_path"]
            if not src.exists():
                raise ValueError(f"镜头 {s['seq']} 视频文件缺失: {src}")
            part = normalize(src, td / f"{s['seq']:04d}.mp4", w, h, 25)
            # P6：TTS 音轨替换（dialogue.mp3 存在时替换 H3 原生音频）；
            # Phase 2 音色（2026-08-30）：渲染时注入过音色样本（H3 原生配音口型
            # 同步）→ 跳过替换保原声
            tts_audio = src.parent / "dialogue.mp3"
            native_voice = json.loads(s["ledger_json"] or "{}").get("h3_native_voice")
            if tts_audio.exists() and not native_voice:
                tts_part = td / f"{s['seq']:04d}_tts.mp4"
                # 音频收口（2026-09-05）：对白镜段长=配音+0.5s 呼吸——长者末帧
                # 定格补齐、短者截尾（口型表演尾巴消灭）；两向都日志透明
                try:
                    a_dur = probe(tts_audio)["duration"]
                    v_dur = probe(part)["duration"]
                except Exception:
                    a_dur = None
                target = (a_dur + BREATH_SEC) if a_dur else probe(part)["duration"]
                if a_dur:
                    if target > v_dur + 1:
                        emit_log(db, "merge", "warn",
                                 f"镜 {s['seq']} 配音 {a_dur:.1f}s 长于视频 {v_dur:.1f}s，"
                                 f"末帧定格补长 {target - v_dur:.1f}s 保对白完整",
                                 project_id=project_id)
                    elif v_dur - target > 1:
                        emit_log(db, "merge", "info",
                                 f"镜 {s['seq']} 口型表演 {v_dur:.1f}s 长于配音 {a_dur:.1f}s，"
                                 f"收口至 {target:.1f}s",
                                 project_id=project_id)
                _replace_audio(part, tts_audio, tts_part, target=target)
                part = tts_part
            elif mute_quiet:  # 无台词镜静音开关（2026-08-30）：杂音不进成片
                mute_part = td / f"{s['seq']:04d}_mute.mp4"
                _mute_audio(part, mute_part)
                part = mute_part
            parts.append(part)
        # C6 交叉淡化（2026-09-01）：开关 comfy.merge_xfade（默认关）→ 段间 0.3s
        # 交叉溶解 + 可选统一调色 comfy.merge_grade；段数超上限回退硬拼
        cfg = get_setting(db, "comfy") or {}
        if cfg.get("merge_xfade") and len(parts) <= XFADE_MAX_PARTS:
            concat_xfade(parts, out, fade=XFADE_SEC,
                         grade=bool(cfg.get("merge_grade")))
        else:
            if cfg.get("merge_xfade"):
                emit_log(db, "merge", "warn",
                         f"段数 {len(parts)} 超 xfade 上限 {XFADE_MAX_PARTS}，回退硬拼接缝",
                         project_id=project_id)
            concat(parts, out)

    # P6：字幕烧录（subtitles.srt 存在时烧入成片）
    srt = out_dir / "subtitles.srt"
    if srt.exists():
        _burn_subtitles(out, srt)
    set_stage(db, project_id, "merged")
    emit_log(db, "merge", "info", f"成片合成完成 → {out.name}（{len(shots)} 镜）",
             project_id=project_id, job_id=job_id)
    return out


def register_merge_handler():
    """merge 的 worker handler 注册（延迟导入避免环）。"""
    from .queue.worker import register

    @register("merge")
    def handle_merge(db, data_dir, job, comfy):
        from .logbus import emit as emit_log
        payload = json.loads(job["payload_json"] or "{}")
        pid = payload.get("project_id", job["project_id"])
        # H2b（2026-09-05 审计）：配音/字幕前置进 merge 任务（此前在 autopilot
        # 巡检线程同步跑，长克隆卡停全部项目巡检；手动合成也只拼旧音轨）；
        # 失败只 warn 不阻断合成
        try:
            from .tts import generate_dialogue_audio
            from .subtitles import generate_srt
            audio = generate_dialogue_audio(db, data_dir, pid)
            generate_srt(db, data_dir, pid)
            if audio:
                emit_log(db, "merge", "info",
                         f"合成前配音+字幕已生成（{len(audio)} 镜）", project_id=pid)
        except Exception as exc:
            emit_log(db, "merge", "warn",
                     f"TTS/字幕生成失败（{exc}），继续合成", project_id=pid)
        merge_project(db, data_dir, pid, job_id=job["id"])
    return handle_merge
