from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import create_router
from app.db.models import Settings
from app.db.session import SessionLocal, init_db
from app.services.tasks import TaskManager


task_manager = TaskManager()
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    with SessionLocal() as session:
        settings = session.get(Settings, 1)
        if settings is not None:
            task_manager.set_download_concurrency(settings.download_concurrency)
    task_manager.reconcile_interrupted_tasks()
    yield


app = FastAPI(title="Fanbox Dashboard API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(create_router(task_manager))


@app.get("/health")
def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


if FRONTEND_DIST.exists():
    assets_dir = FRONTEND_DIST / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="frontend-assets")

    def _serve_frontend_file(name: str) -> FileResponse:
        return FileResponse(FRONTEND_DIST / name)

    @app.get("/favicon.svg", include_in_schema=False)
    def frontend_favicon() -> FileResponse:
        return _serve_frontend_file("favicon.svg")

    @app.get("/icons.svg", include_in_schema=False)
    def frontend_icons() -> FileResponse:
        return _serve_frontend_file("icons.svg")

    @app.get("/", include_in_schema=False)
    def frontend_index() -> FileResponse:
        return _serve_frontend_file("index.html")

    @app.get("/{full_path:path}", include_in_schema=False)
    def frontend_spa_fallback(full_path: str) -> FileResponse:
        if full_path.startswith("api/") or full_path == "health":
            return FileResponse(FRONTEND_DIST / "index.html", status_code=404)
        return _serve_frontend_file("index.html")
