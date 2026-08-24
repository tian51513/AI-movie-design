"""FastAPI 应用工厂。Web 层只做：参数校验、调 engine、IO 转换（spec §3.2）。"""
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from ..engine.db import Database

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "index.html"


def _try_reattach(db, data_dir, rows) -> int:
    """启动期断点对账（spec §5）：running gen_shot 逐条查 ComfyUI /history，
    已完成 → reattach 落盘+标 done；不可达/未完成 → 不动（留给 requeue 重渲）。"""
    if not rows:
        return 0
    from ..engine.comfy.client import ComfyClient
    from ..engine.jobs import finish_job
    from ..engine.logbus import emit as emit_log
    from ..engine.rendershot import reattach
    from ..engine.settings import get_setting

    base_url = (get_setting(db, "comfy") or {}).get("base_url")
    if not base_url:
        return 0
    comfy = ComfyClient(base_url)
    try:
        comfy.health()
    except Exception as exc:
        emit_log(db, "comfy", "warn", f"断点对账跳过：ComfyUI 不可达（{exc}）")
        return 0
    n = 0
    for row in rows:
        try:
            dest = reattach(db, data_dir, row, comfy)
        except Exception as exc:  # 单条失败不影响其余对账
            emit_log(db, "comfy", "warn",
                     f"断点对账失败 job#{row['id']}：{exc}",
                     project_id=row["project_id"], job_id=row["id"])
            continue
        if dest is None:
            continue
        finish_job(db, row["id"], None)
        n += 1
    return n


def create_app(db_path: str | Path = "./data/studio.db",
               data_dir: str | Path = "./data",
               start_workers: bool = True) -> FastAPI:
    db_path, data_dir = Path(db_path), Path(data_dir)
    db = Database(db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.migrate()
        # 断点对账（spec §5）：先收集可对账的 gen_shot，再 requeue。
        # ComfyUI 可达且 /history 显示已完成 → 直接下载落盘不重渲；否则照常 requeue。
        from ..engine.jobs import collect_reattach_candidates, requeue_on_restart
        reattach_rows = collect_reattach_candidates(db, "gen_shot")
        reattached = _try_reattach(db, data_dir, reattach_rows)
        # 断点对账（spec §5）：先收集可对账的 gen_shot，再 requeue。
        # ComfyUI 可达且 /history 显示已完成 → 直接下载落盘不重渲；否则照常 requeue。
        from ..engine.jobs import collect_reattach_candidates, requeue_on_restart
        reattach_rows = collect_reattach_candidates(db, "gen_shot")
        reattached = _try_reattach(db, data_dir, reattach_rows)
        # 重启后 BackgroundTasks 已消亡，running job 不可能合法存在
        requeued = requeue_on_restart(db, ("gen_ref", "split_storyboards", "gen_prompt", "gen_shot"))
        if start_workers:
            from ..engine import genref, pipeline_jobs, rendershot  # 注册触发
            from ..engine.queue.worker import start_workers as _spawn_workers, stop_workers
            from ..engine.settings import get_setting
            workers, worker_stop = _spawn_workers(
                db.path, str(data_dir), None,
                int(get_setting(db, "workers") or 1),
                comfy_from_settings=True)
            yield
            stop_workers(workers, worker_stop)
        else:
            yield

    app = FastAPI(title="comic_studio", lifespan=lifespan)
    app.state.db = db
    app.state.data_dir = data_dir

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/")
    def index():
        return FileResponse(_FRONTEND)

    from fastapi.staticfiles import StaticFiles
    vendor_dir = _FRONTEND.parent / "vendor"
    if vendor_dir.is_dir():
        app.mount("/vendor", StaticFiles(directory=vendor_dir), name="vendor")
    if _FRONTEND.parent.is_dir():
        app.mount("/static", StaticFiles(directory=_FRONTEND.parent), name="static")

    from .routes_assets import router as assets_router
    app.include_router(assets_router)

    from .routes_settings import router as settings_router
    app.include_router(settings_router)

    from .routes_logs import router as logs_router
    app.include_router(logs_router)

    from .routes_shots import router as shots_router
    app.include_router(shots_router)

    from .routes_comfy import router as comfy_router
    app.include_router(comfy_router)

    from .routes_assets_edit import router as assets_edit_router
    app.include_router(assets_edit_router)

    from .routes_projects import router as projects_router
    app.include_router(projects_router)

    from .routes_analyze import router as analyze_router
    app.include_router(analyze_router)

    from .routes_refs import router as refs_router
    app.include_router(refs_router)

    from fastapi.staticfiles import StaticFiles
    lib_dir = Path(data_dir) / "library"
    lib_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/library", StaticFiles(directory=lib_dir), name="library")

    Path(data_dir).mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=data_dir), name="media")

    return app


app = create_app(
    db_path=os.environ.get("CS_DB", "./data/studio.db"),
    data_dir=os.environ.get("CS_DATA", "./data"),
)
