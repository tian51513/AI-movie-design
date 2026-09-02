# comic_studio/web/routes_voices.py
"""Phase 2 音色库 API（2026-08-30）：列表 / 上传处理（全局+项目级）/ 预设生成 /
删除 / 角色绑定。

同步执行说明：TTS 生成单次约 10~60s，本地单用户应用同步等待可接受；
预设批量由前端逐个调用（天然进度展示）。样本试听走既有 /media 静态挂载
（list 返回 data 相对路径，前端拼 /media 前缀）。
"""
import tempfile
from pathlib import Path

from fastapi import APIRouter, Body, Form, HTTPException, Request, UploadFile

from ..engine import voicelib
from ..engine.assets import get_asset
from ..engine.projects import get_project
from ..engine.voices import VOICE_PRESETS

router = APIRouter()


def _comfy(request: Request):
    from ..engine.comfy.client import ComfyClient
    from ..engine.settings import get_setting
    url = (get_setting(request.app.state.db, "comfy") or {}).get(
        "base_url", "http://127.0.0.1:8188")
    return ComfyClient(url)


def _slug(request: Request, project_id: int | None) -> str | None:
    if not project_id:
        return None
    proj = get_project(request.app.state.db, project_id)
    if proj is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    return proj["slug"]


@router.get("/api/voices")
def list_voices(request: Request, project_id: int | None = None):
    return voicelib.list_voices(request.app.state.data_dir,
                                _slug(request, project_id))


@router.post("/api/voices/upload")
def upload_voice(request: Request, file: UploadFile, name: str = Form(...),
                 scope: str = Form("global"), project_id: int | None = Form(None),
                 start: float = Form(0), dur: float = Form(60)):
    """上传音色处理：走 qwen_tts_clone 模板（裁剪起止 + 默认句克隆）→
    **staging 暂存**，前端试听后 confirm 入库 / discard 放弃（2026-08-31）。"""
    if scope not in ("global", "project"):
        raise HTTPException(422, "scope 只能是 global 或 project")
    if scope == "project" and not project_id:
        raise HTTPException(422, "项目级音色必须带 project_id")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".mp3", ".wav", ".flac", ".ogg", ".m4a"):
        raise HTTPException(422, f"不支持的音频格式: {suffix or '(无后缀)'}")
    _slug(request, project_id)  # 校验项目存在（staging 阶段不需要 slug）
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file.file.read())
        tmp_path = Path(tmp.name)
    try:
        out = voicelib.process_upload(_comfy(request), request.app.state.data_dir,
                                      tmp_path, name=name, start=start, dur=dur,
                                      db=request.app.state.db)
    except Exception as e:
        raise HTTPException(502, f"音色处理失败（ComfyUI TTS）: {e}")
    finally:
        tmp_path.unlink(missing_ok=True)
    rel = out.relative_to(request.app.state.data_dir).as_posix()
    return {"name": name, "scope": scope, "staged": rel, "url": f"/media/{rel}"}


@router.post("/api/voices/confirm")
def confirm_voice(request: Request, body: dict = Body(...)):
    """试听满意 → 确认入库（staging → 全局/项目级正式目录）。"""
    staged = str(body.get("staged") or "")
    name = str(body.get("name") or "").strip()
    scope = body.get("scope") or "global"
    if not staged or not name:
        raise HTTPException(422, "staged 与 name 必填")
    if scope not in ("global", "project"):
        raise HTTPException(422, "scope 只能是 global 或 project")
    slug = _slug(request, body.get("project_id"))
    if scope == "project" and not slug:
        raise HTTPException(422, "项目级音色必须带 project_id")
    try:
        out = voicelib.confirm_staged(request.app.state.data_dir, staged, name,
                                      scope=scope, project=slug)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"name": name, "scope": scope,
            "path": out.relative_to(request.app.state.data_dir).as_posix()}


@router.post("/api/voices/discard")
def discard_voice(request: Request, body: dict = Body(...)):
    """放弃暂存样本。"""
    staged = str(body.get("staged") or "")
    if not staged:
        raise HTTPException(422, "staged 必填")
    try:
        voicelib.discard_staged(request.app.state.data_dir, staged)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"discarded": staged}


@router.post("/api/voices/presets/generate")
def generate_preset(request: Request, body: dict = Body(...)):
    """单个预设生成（前端循环调用实现批量+进度）。"""
    name = body.get("name")
    if name not in {p["name"] for p in VOICE_PRESETS}:
        raise HTTPException(422, f"未知预设: {name}")
    try:
        out = voicelib.generate_preset(_comfy(request), request.app.state.data_dir,
                                       name, db=request.app.state.db)
    except Exception as e:
        raise HTTPException(502, f"预设生成失败（ComfyUI TTS）: {e}")
    return {"name": name,
            "path": out.relative_to(request.app.state.data_dir).as_posix()}


@router.post("/api/voices/design")
def design_voice(request: Request, body: dict = Body(...)):
    """按声线描述生成项目级音色并（可选）自动绑角色（2026-09-02 角色配音）。
    资产先校验再生成——不白烧 ComfyUI。生成失败 502（绑定未发生）。"""
    instruction = str(body.get("instruction") or "").strip()
    name = str(body.get("name") or "").strip()
    if not instruction or not name:
        raise HTTPException(422, "name 与 instruction 必填")
    project_id = body.get("project_id")
    slug = _slug(request, project_id)
    if not slug:
        raise HTTPException(422, "必须带 project_id")
    bind_asset_id = body.get("bind_asset_id")
    if bind_asset_id is not None and get_asset(request.app.state.db, bind_asset_id) is None:
        raise HTTPException(404, f"资产不存在: {bind_asset_id}")
    try:
        out = voicelib.generate_custom(_comfy(request), request.app.state.data_dir,
                                       slug, name, instruction,
                                       db=request.app.state.db)
    except Exception as e:
        raise HTTPException(502, f"音色生成失败（ComfyUI TTS）: {e}")
    rel = out.relative_to(request.app.state.data_dir).as_posix()
    if bind_asset_id is not None:
        conn = request.app.state.db.connect()
        conn.execute("UPDATE assets SET voice=? WHERE id=?", (name, bind_asset_id))
        conn.commit()
    return {"name": name, "path": rel, "url": f"/media/{rel}",
            "bound": bind_asset_id}


@router.post("/api/voices/promote")
def promote_voice(request: Request, body: dict = Body(...)):
    """项目级音色 → 全局自定义库（2026-09-02：满意的沉淀复用；复制不移动）。"""
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(422, "name 必填")
    slug = _slug(request, body.get("project_id"))
    if not slug:
        raise HTTPException(422, "必须带 project_id")
    try:
        out = voicelib.promote_to_global(request.app.state.data_dir, slug, name,
                                         new_name=str(body.get("new_name") or "").strip() or None)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    rel = out.relative_to(request.app.state.data_dir).as_posix()
    return {"name": out.stem, "path": rel, "url": f"/media/{rel}"}


@router.delete("/api/voices/{name}")
def delete_voice(request: Request, name: str, scope: str = "global",
                 project_id: int | None = None):
    """删除自定义音色（预设不可删）。"""
    if scope == "project":
        slug = _slug(request, project_id)
        target = Path(request.app.state.data_dir) / "projects" / slug / "voices" / name
    else:
        target = Path(request.app.state.data_dir) / "voices" / "custom" / name
    hits = list(target.parent.glob(target.name + ".*")) if target.parent.exists() else []
    if not hits:
        raise HTTPException(404, f"音色不存在: {name}（scope={scope}）")
    for h in hits:
        h.unlink()
    return {"deleted": name}


@router.patch("/api/assets/{asset_id}/voice")
def bind_asset_voice(request: Request, asset_id: int, body: dict = Body(...)):
    """角色绑定音色（音色名或样本相对路径；空串解绑）。"""
    asset = get_asset(request.app.state.db, asset_id)
    if asset is None:
        raise HTTPException(404, f"资产不存在: {asset_id}")
    voice = str(body.get("voice", "")).strip()
    conn = request.app.state.db.connect()
    conn.execute("UPDATE assets SET voice=? WHERE id=?", (voice, asset_id))
    conn.commit()
    return {"id": asset_id, "name": asset["name"], "voice": voice}
