# comic_studio/engine/tts.py
"""P6 Task 1：TTS 配音（Edge-TTS）——从 ledger.dialogue 生成语音（2026-08-27）。

逐镜逐句调 edge-tts，按角色性别分配声音（男→云希/女→晓晓），
产出 projects/<slug>/shots/<N>/dialogue.mp3。
"""
import asyncio
import json
import re
from pathlib import Path

from .logbus import emit as emit_log
from .paths import data_to_abs
from .projects import get_project
from .shots import list_shots
from .assets import get_asset

DEFAULT_VOICES = {
    "male": "zh-CN-YunxiNeural",       # 男声-云希（沉稳）
    "female": "zh-CN-XiaoxiaoNeural",  # 女声-晓晓（清晰）
}


_TTS_BUSY: set = set()  # 同项目互斥（QC-D）：手动 /tts 与 merge 任务内 TTS
                          # 并发会双烧 ComfyUI 并覆写同一 dialogue.mp3


def tts_busy(project_id: int) -> bool:
    return project_id in _TTS_BUSY


def tts_busy_add(project_id: int) -> None:
    _TTS_BUSY.add(project_id)


def tts_busy_remove(project_id: int) -> None:
    _TTS_BUSY.discard(project_id)


def detect_gender(appearance_text: str) -> str:
    """从八行外貌模板检测性别（性别：男/女）。"""
    m = re.search(r"性别[：:]\s*(男|女)", appearance_text or "")
    if m:
        return "female" if m.group(1) == "女" else "male"
    return "male"  # 默认男声


def voice_for_character(gender: str) -> str:
    """性别 → edge-tts 声音名。"""
    return DEFAULT_VOICES.get(gender, DEFAULT_VOICES["male"])


def _character_voice_map(db, project_id) -> dict:
    """项目内角色名 → 声音名（按外貌性别分配）。"""
    conn = db.connect()
    voice_map = {}
    rows = conn.execute(
        "SELECT a.id, a.name, a.appearance_json FROM assets a "
        "JOIN project_assets pa ON pa.asset_id = a.id "
        "WHERE pa.project_id=? AND a.kind='character'", (project_id,)).fetchall()
    for r in rows:
        detail = json.loads(r["appearance_json"]).get("detail", "")
        gender = detect_gender(detail)
        voice_map[r["name"]] = voice_for_character(gender)
    return voice_map


def generate_dialogue_audio(db, data_dir, project_id) -> list:
    """为项目所有含台词的分镜生成 TTS 语音。
    返回 [{shot_id, seq, lines: [{speaker, line, voice}]}]"""
    proj = get_project(db, project_id)
    if proj is None:
        raise ValueError(f"项目不存在: {project_id}")

    voice_map = _character_voice_map(db, project_id)
    shots = list_shots(db, project_id)
    results = []

    for shot in shots:
        ledger = json.loads(shot["ledger_json"] or "{}")
        dialogue = ledger.get("dialogue") or []
        if not dialogue:
            continue

        shot_dir = data_to_abs(data_dir, f"projects/{proj['slug']}/shots/{shot['seq']}")
        shot_dir.mkdir(parents=True, exist_ok=True)

        # ref2va 已注入音色样本（H3 原生配音）→ 不生成 TTS，保原声；
        # 顺带清掉历史残留的 dialogue.mp3（否则快车道 spans 会误用，2026-08-31）
        if ledger.get("h3_native_voice"):
            stale = shot_dir / "dialogue.mp3"
            if stale.exists():
                stale.unlink()
            continue

        # 角色音色配音（2026-08-31 用户决策）：单一说话人 + 绑定音色有样本 →
        # VoiceClone 一次生成整镜台词（fl2v/i2v/t2v 无音频槽，配音期用角色音色）；
        # 多说话人 / 未绑 / Comfy 不可用 → 逐句 Edge-TTS 按性别（原路径）
        if _try_voiced_tts(db, data_dir, proj, shot, dialogue, shot_dir):
            results.append({"shot_id": shot["id"], "seq": shot["seq"],
                            "lines": [{"speaker": d.get("speaker", "?"),
                                       "line": d.get("line", ""),
                                       "voice": "角色音色"} for d in dialogue
                                      if d.get("line", "").strip()]})
            emit_log(db, "tts", "info",
                     f"分镜 {shot['seq']} 配音按角色音色克隆生成",
                     project_id=project_id)
            continue

        lines_info = []
        # 逐句生成（多句合并为一个文件——ffmpeg 对齐阶段处理）
        audio_parts = []
        for i, d in enumerate(dialogue):
            speaker = d.get("speaker", "?")
            line = d.get("line", "").strip()
            if not line:
                continue
            voice = voice_map.get(speaker, DEFAULT_VOICES["male"])
            part_path = shot_dir / f"dialogue_part_{i}.mp3"
            try:
                _tts_sync(line, voice, part_path)
                audio_parts.append(part_path)
                lines_info.append({"speaker": speaker, "line": line,
                                   "voice": voice, "part": str(part_path)})
            except Exception as exc:
                emit_log(db, "tts", "warn",
                         f"分镜 {shot['seq']} 台词 {i+1} TTS 失败：{exc}",
                         project_id=project_id)

        # 合并多段音频为一个文件（如果有多个 part）
        if audio_parts:
            final_audio = shot_dir / "dialogue.mp3"
            if len(audio_parts) == 1:
                # 单段直接复制
                import shutil
                shutil.copy2(audio_parts[0], final_audio)
            else:
                # 多段用 ffmpeg concat
                _concat_audio_parts(audio_parts, final_audio)
                # 清理临时 part 文件
                for p in audio_parts:
                    p.unlink(missing_ok=True)

        results.append({"shot_id": shot["id"], "seq": shot["seq"],
                        "lines": lines_info})
        emit_log(db, "tts", "info",
                 f"分镜 {shot['seq']} 配音生成完成（{len(lines_info)} 句）",
                 project_id=project_id)

    return results


def _try_voiced_tts(db, data_dir, proj, shot, dialogue, shot_dir) -> bool:
    """单说话人 + 绑定音色有样本 → qwen_tts_clone 生成 dialogue.mp3。
    任何不满足/失败都返回 False（走 Edge-TTS 兜底），失败只告警不阻塞。"""
    speakers = {str(d.get("speaker") or "").strip() for d in dialogue
                if d.get("line", "").strip()}
    if len(speakers) != 1:
        return False
    speaker = speakers.pop()
    from .assets import list_project_assets
    from .voicelib import resolve_sample, clone_speech
    asset = next((a for a in list_project_assets(db, proj["id"])
                  if a["kind"] == "character" and a["name"] == speaker), None)
    voice = ((asset["voice"] if asset is not None and "voice" in asset.keys() else "")
             or "").strip()
    if not voice:
        return False
    from pathlib import PurePosixPath
    sample = None
    if PurePosixPath(voice).suffix:
        cand = data_to_abs(data_dir, voice)
        sample = cand if cand.exists() else None
    if sample is None:
        sample = resolve_sample(data_dir, voice, proj["slug"])
    if sample is None:
        return False
    text = " [pause:0.4] ".join(d.get("line", "").strip()
                                for d in dialogue if d.get("line", "").strip())
    try:
        comfy = _comfy_client(db)
        out = clone_speech(comfy, sample, text, shot_dir, f"dialogue_{shot['seq']}",
                             db=db)
        _to_mp3(out, shot_dir / "dialogue.mp3")
        out.unlink(missing_ok=True)
        return True
    except Exception as exc:
        emit_log(db, "tts", "warn",
                 f"分镜 {shot['seq']} 角色音色克隆失败，回退 Edge-TTS：{exc}",
                 project_id=proj["id"])
        return False


def _comfy_client(db):
    from .comfy.client import ComfyClient
    from .settings import get_setting
    url = (get_setting(db, "comfy") or {}).get("base_url", "http://127.0.0.1:8188")
    return ComfyClient(url)


def _to_mp3(src: Path, dest: Path) -> None:
    """克隆产物（flac 等）转 mp3（下游 merge/director_mix 只认 dialogue.mp3）。"""
    from .merge import ffmpeg_bin
    import subprocess
    subprocess.run([ffmpeg_bin(), "-y", "-i", str(src), "-codec:a", "libmp3lame",
                    "-q:a", "4", str(dest)], check=True, capture_output=True, timeout=120)


def _tts_sync(text: str, voice: str, output: Path):
    """同步调用 edge-tts（内部 async → sync 包装）。"""
    import edge_tts

    async def _run():
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(str(output))

    asyncio.run(_run())


def _concat_audio_parts(parts: list, output: Path):
    """FFmpeg 合并多段 MP3 为一个文件。"""
    from .merge import ffmpeg_bin
    import subprocess, tempfile
    list_file = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    for p in parts:
        list_file.write(f"file '{p}'\n")
    list_file.close()
    subprocess.run([ffmpeg_bin(), "-y", "-f", "concat", "-safe", "0",
                    "-i", list_file.name, "-c", "copy", str(output)],
                   capture_output=True, timeout=60)
    Path(list_file.name).unlink(missing_ok=True)
