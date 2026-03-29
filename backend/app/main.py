from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
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

    @app.get("/{full_path:path}", include_in_schema=False)
    def frontend_spa_fallback(full_path: str) -> FileResponse:
        if full_path.startswith("api/") or full_path == "health":
            return FileResponse(FRONTEND_DIST / "index.html", status_code=404)
        return _serve_frontend_file("index.html")


@app.get("/", include_in_schema=False)
def frontend_index():
    if FRONTEND_DIST.exists():
        return FileResponse(FRONTEND_DIST / "index.html")

    return HTMLResponse(
        """
        <!doctype html>
        <html lang="en">
          <head>
            <meta charset="utf-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <title>Fanbox Dashboard</title>
            <style>
              :root {
                color-scheme: light dark;
                font-family: "Segoe UI", sans-serif;
              }
              body {
                margin: 0;
                min-height: 100vh;
                display: grid;
                place-items: center;
                background: linear-gradient(180deg, #f6efe6 0%, #f3f6f6 100%);
                color: #24313a;
              }
              .card {
                width: min(680px, calc(100vw - 40px));
                box-sizing: border-box;
                padding: 28px 30px;
                border-radius: 24px;
                background: rgba(255, 252, 247, 0.94);
                box-shadow: 0 18px 48px rgba(48, 68, 82, 0.12);
              }
              h1 {
                margin: 0 0 12px;
                font-size: 28px;
              }
              p {
                margin: 0 0 12px;
                line-height: 1.6;
              }
              code, pre {
                font-family: Consolas, "SFMono-Regular", monospace;
              }
              pre {
                margin: 16px 0 0;
                padding: 14px 16px;
                border-radius: 16px;
                background: #f0eee9;
                overflow-x: auto;
              }
            </style>
          </head>
          <body>
            <main class="card">
              <h1>Fanbox Dashboard</h1>
              <p>The backend is running, but the frontend build was not found under <code>frontend/dist</code>.</p>
              <p>Build the frontend first, then refresh this page:</p>
              <pre>cd frontend
npm install
npm run build</pre>
            </main>
          </body>
        </html>
        """
    )
