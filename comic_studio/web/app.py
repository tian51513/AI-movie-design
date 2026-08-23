"""FastAPI 应用工厂。Web 层只做：参数校验、调 engine、IO 转换（spec §3.2）。"""
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from ..engine.db import Database

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "index.html"


def create_app(db_path: str | Path = "./data/studio.db",
               data_dir: str | Path = "./data",
               start_workers: bool = True) -> FastAPI:
    db_path, data_dir = Path(db_path), Path(data_dir)
    db = Database(db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.migrate()
        # 重启后 BackgroundTasks 已消亡，running job 不可能合法存在
        from ..engine.jobs import requeue_on_restart
        requeued = requeue_on_restart(db, ("gen_ref",))
        if start_workers:
            from ..engine import genref  # 触发 @register 注册
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

    from .routes_comfy import router as comfy_router
    app.include_router(comfy_router)

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

    return app


app = create_app(
    db_path=os.environ.get("CS_DB", "./data/studio.db"),
    data_dir=os.environ.get("CS_DATA", "./data"),
)
