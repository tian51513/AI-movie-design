"""FastAPI 应用工厂。Web 层只做：参数校验、调 engine、IO 转换（spec §3.2）。"""
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from ..engine.db import Database

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "index.html"


def _try_reattach(db, data_dir, rows) -> tuple:
    """启动期断点对账（spec §5）。返回 (立即接回数, 等待接回的 job id 列表)。
    - history 已有产物 → 立即落盘标 done
    - prompt 仍在 ComfyUI 队列/执行中 → 保持 running 并返回 id（后台等待接回，
      requeue 跳过——2026-08-25 真机教训：重启即重渲造成同镜双渲）
    - 不可达/两者皆无 → 不动（留给 requeue 重渲）"""
    if not rows:
        return 0, []
    from ..engine.comfy.client import ComfyClient
    from ..engine.jobs import finish_job
    from ..engine.logbus import emit as emit_log
    from ..engine.rendershot import reattach
    from ..engine.settings import get_setting

    base_url = (get_setting(db, "comfy") or {}).get("base_url")
    if not base_url:
        return 0, []
    comfy = ComfyClient(base_url)
    try:
        comfy.health()
    except Exception as exc:
        emit_log(db, "comfy", "warn", f"断点对账跳过：ComfyUI 不可达（{exc}）")
        return 0, []
    try:
        inflight = comfy.queued_prompt_ids()
    except Exception:
        inflight = set()
    done, waiting = 0, []
    from ..engine.comfy.client import ComfyError
    for row in rows:
        try:
            dest = reattach(db, data_dir, row, comfy)
        except ComfyError as exc:
            # history 有记录但状态 error——多为曾被 interrupt，属正常路径：重排重渲
            level, msg = ("info", f"job#{row['id']} 的 ComfyUI 任务已被中断——重排重渲") \
                if "interrupted" in str(exc) else \
                ("warn", f"断点对账失败 job#{row['id']}：{exc}")
            emit_log(db, "comfy", level, msg,
                     project_id=row["project_id"], job_id=row["id"])
            continue
        except Exception as exc:  # 单条失败不影响其余对账
            emit_log(db, "comfy", "warn",
                     f"断点对账失败 job#{row['id']}：{exc}",
                     project_id=row["project_id"], job_id=row["id"])
            continue
        if dest is not None:
            finish_job(db, row["id"], None)
            done += 1
        elif row["comfy_prompt_id"] in inflight:
            waiting.append(row["id"])  # ComfyUI 还在跑：等它，不重渲
    return done, waiting


def _reattach_waiting(db, data_dir, job_ids, comfy_base_url) -> None:
    """后台线程：等待在队 prompt 跑完落盘；失速/失败标 failed（下一轮真重渲）。"""
    from ..engine.comfy.client import ComfyClient
    from ..engine.jobs import finish_job, get_job
    from ..engine.logbus import emit as emit_log
    from ..engine.rendershot import reattach_wait

    comfy = ComfyClient(comfy_base_url)
    for jid in job_ids:
        row = get_job(db, jid)
        if row is None or row["status"] != "running":
            continue
        try:
            dest = reattach_wait(db, data_dir, row, comfy)
        except Exception as exc:
            emit_log(db, "comfy", "warn",
                     f"等待接回失败 job#{jid}：{exc}",
                     project_id=row["project_id"], job_id=jid)
            dest = None
        if dest is not None:
            finish_job(db, jid, None)
        else:
            finish_job(db, jid, "reattach_wait 未取得产物")


def _autopilot_once(db, data_dir) -> int:
    """巡检一轮：对所有 autopilot=1 项目执行 tick；单项目异常记日志不中断。"""
    from ..engine.autopilot import tick
    from ..engine.logbus import emit as emit_log
    rows = db.connect().execute(
        "SELECT id FROM projects WHERE autopilot=1").fetchall()
    n = 0
    for row in rows:
        try:
            tick(db, data_dir, row["id"])
        except Exception as exc:
            emit_log(db, "autopilot", "error",
                     f"tick 失败 project#{row['id']}：{type(exc).__name__}: {exc}",
                     project_id=row["id"])
            continue
        n += 1
    return n


def _autopilot_loop(db, data_dir, stop_event, interval: float = 3.0) -> None:
    """autopilot 巡检线程主体（计划5B 任务2）：本地单用户，单线程扫表足够。"""
    while not stop_event.wait(interval):
        try:
            _autopilot_once(db, data_dir)
        except Exception:
            pass  # _autopilot_once 已逐项目兜底；此处兜底巡检自身（如 DB 抖动）


def create_app(db_path: str | Path = "./data/studio.db",
               data_dir: str | Path = "./data",
               start_workers: bool = True) -> FastAPI:
    db_path, data_dir = Path(db_path), Path(data_dir)
    db = Database(db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 本地地址豁免代理（2026-08-29 真机：WSL 挂 http_proxy=127.0.0.1:1092 时
        # Ollama/ComfyUI 的 127.0.0.1 请求全被代理掐线——必须第一位执行）
        from ..engine.netenv import ensure_local_no_proxy
        ensure_local_no_proxy()
        db.migrate()
        # 日志自动清理（保留 7 天，防止 logs 表无限膨胀）
        try:
            conn = db.connect()
            conn.execute(
                "DELETE FROM logs WHERE created_at < datetime('now', '-7 days')")
            conn.commit()
        except Exception:
            pass
        # 主题模板同步入库（幂等 upsert；templates/tpl/*.md）
        try:
            from ..engine.themes import sync_themes
            sync_themes(db)
        except Exception as exc:
            from ..engine.logbus import emit as emit_log
            emit_log(db, "system", "warn", f"主题模板同步失败：{exc}")  # 不阻塞启动
        # 断点对账（spec §5）：先收集可对账的 gen_shot，再 requeue。
        # ComfyUI 可达且 /history 显示已完成 → 直接下载落盘不重渲；否则照常 requeue。
        from ..engine.jobs import collect_reattach_candidates, requeue_on_restart
        reattach_rows = collect_reattach_candidates(db, "gen_shot")
        reattached, waiting_ids = _try_reattach(db, data_dir, reattach_rows)
        # 重启后 BackgroundTasks 已消亡，running job 不可能合法存在
        #（waiting_ids：ComfyUI 仍在跑的保持 running 由后台接回，跳过重排防双渲）
        requeued = requeue_on_restart(
            db, ("gen_ref", "split_storyboards", "gen_prompt", "gen_shot"),
            exclude_ids=waiting_ids)
        if waiting_ids:
            from threading import Thread
            from ..engine.settings import get_setting
            Thread(target=_reattach_waiting, daemon=True, name="reattach-waiting",
                   args=(db, data_dir, waiting_ids,
                         (get_setting(db, "comfy") or {}).get("base_url", ""))).start()
        if start_workers:
            from ..engine import (director, genref,  # noqa: F401 注册触发
                                  merge as merge_mod, pipeline_jobs, rendershot)
            merge_mod.register_merge_handler()  # merge handler 延迟注册（避免环）
            from ..engine.queue.worker import start_workers as _spawn_workers, stop_workers
            from ..engine.settings import get_setting
            workers, worker_stop = _spawn_workers(
                db.path, str(data_dir), None,
                int(get_setting(db, "workers") or 1),
                comfy_from_settings=True)
            import threading
            ap_stop = threading.Event()
            threading.Thread(target=_autopilot_loop, args=(db, data_dir, ap_stop),
                             daemon=True, name="autopilot-loop").start()
            yield
            ap_stop.set()
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

    from .routes_merge import router as merge_router
    app.include_router(merge_router)

    from .routes_llm import router as llm_router
    app.include_router(llm_router)

    from .routes_themes import router as themes_router
    app.include_router(themes_router)

    from .routes_workflows import router as workflows_router
    app.include_router(workflows_router)

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
