from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.models import Artifact, Post, RefreshMode, Settings, Task, TaskStatus
from app.db.session import get_db
from app.schemas.archive import ArchiveActionResult, ArchiveDeleteRequest, ArchivePurgeRequest, OpenPathResult, YearOpenRequest
from app.schemas.common import Envelope, TaskResponse
from app.schemas.post import DownloadRequest, PostRead, RefreshRequest
from app.schemas.settings import AuthStatus, SettingsRead, SettingsUpdate
from app.schemas.task import RetryRequest, TaskClearResult, TaskRead
from app.services.fanbox import inspect_auth_status
from app.services.files import delete_archive_file, open_in_explorer, purge_archive_files, resolve_post_open_path
from app.services.tasks import TaskManager


def _post_id_sort_key(post_id: str) -> tuple[int, int | str]:
    return (0, int(post_id)) if post_id.isdigit() else (1, post_id)


def _dedupe_posts_by_mega(posts: list[Post]) -> list[Post]:
    selected_ids: set[int] = set()
    mega_winners: dict[str, Post] = {}

    for post in posts:
        if not post.mega_url:
            selected_ids.add(post.id)
            continue

        winner = mega_winners.get(post.mega_url)
        if winner is None or _post_id_sort_key(post.post_id) < _post_id_sort_key(winner.post_id):
            mega_winners[post.mega_url] = post

    selected_ids.update(post.id for post in mega_winners.values())
    return [post for post in posts if post.id in selected_ids]


def create_router(task_manager: TaskManager) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/posts", response_model=Envelope[list[PostRead]])
    def list_posts(db: Session = Depends(get_db)) -> Envelope[list[PostRead]]:
        posts = db.query(Post).order_by(Post.published_at.desc().nullslast(), Post.updated_at.desc()).all()
        posts = _dedupe_posts_by_mega(posts)
        payload: list[PostRead] = []
        for post in posts:
            artifact = db.query(Artifact).filter_by(post_id=post.id).one_or_none()
            open_path = resolve_post_open_path(
                artifact.extract_dir if artifact else None,
                artifact.archive_path if artifact else None,
            )
            payload.append(
                PostRead(
                    id=post.id,
                    post_id=post.post_id,
                    title=post.title,
                    published_at=post.published_at,
                    detail_url=post.detail_url,
                    cover_url=post.cover_url,
                    mega_url=post.mega_url,
                    status=post.status,
                    last_error=post.last_error,
                    archive_path=artifact.archive_path if artifact else None,
                    extract_dir=artifact.extract_dir if artifact else None,
                    can_open_local_path=bool(open_path),
                    updated_at=post.updated_at,
                )
            )
        return Envelope(data=payload)

    @router.post("/posts/refresh", response_model=Envelope[TaskResponse])
    async def refresh_posts_endpoint(payload: RefreshRequest | None = None) -> Envelope[TaskResponse]:
        mode = RefreshMode(payload.mode) if payload is not None else RefreshMode.INCREMENTAL
        task_id = task_manager.enqueue_refresh(mode)
        return Envelope(message="已创建刷新任务。", data=TaskResponse(task_id=task_id, task_status="queued"))

    @router.get("/auth/status", response_model=Envelope[AuthStatus])
    async def get_auth_status(db: Session = Depends(get_db)) -> Envelope[AuthStatus]:
        settings = db.get(Settings, 1)
        assert settings is not None
        authenticated, reason = await inspect_auth_status(settings)
        return Envelope(
            data=AuthStatus(
                authenticated=authenticated,
                profile_exists=Path(settings.profile_dir).exists(),
                reason=reason,
            )
        )

    @router.post("/auth/open-login", response_model=Envelope[TaskResponse])
    async def open_login() -> Envelope[TaskResponse]:
        task_id = task_manager.enqueue_login()
        return Envelope(message="已创建登录任务。", data=TaskResponse(task_id=task_id, task_status="queued"))

    @router.post("/tasks/download", response_model=Envelope[TaskResponse])
    async def create_download_tasks(payload: DownloadRequest) -> Envelope[TaskResponse]:
        if not payload.post_ids:
            raise HTTPException(status_code=400, detail="post_ids must not be empty")
        task_ids = task_manager.enqueue_downloads(payload.post_ids)
        return Envelope(
            message=f"已创建 {len(task_ids)} 个下载任务。",
            data=TaskResponse(task_ids=task_ids, task_status="queued"),
        )

    @router.post("/tasks/retry", response_model=Envelope[TaskResponse])
    async def retry_task(payload: RetryRequest) -> Envelope[TaskResponse]:
        try:
            task_id = task_manager.retry_task(payload.task_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Envelope(message="已重试任务。", data=TaskResponse(task_id=task_id, task_status="queued"))

    @router.get("/tasks", response_model=Envelope[list[TaskRead]])
    def list_tasks(db: Session = Depends(get_db)) -> Envelope[list[TaskRead]]:
        tasks = db.query(Task).order_by(Task.created_at.desc()).limit(100).all()
        payload = [
            TaskRead(
                id=task.id,
                kind=task.kind,
                status=task.status,
                refresh_mode=task.refresh_mode,
                post_id=task.post_id,
                message=task.message,
                error=task.error,
                log=task.log,
                progress_current=task.progress_current,
                progress_total=task.progress_total,
                created_at=task.created_at,
                started_at=task.started_at,
                finished_at=task.finished_at,
            )
            for task in tasks
        ]
        return Envelope(data=payload)

    @router.post("/tasks/clear", response_model=Envelope[TaskClearResult])
    def clear_tasks(db: Session = Depends(get_db)) -> Envelope[TaskClearResult]:
        active_statuses = [
            TaskStatus.QUEUED.value,
            TaskStatus.RUNNING_DOWNLOAD.value,
            TaskStatus.RUNNING_EXTRACT.value,
            TaskStatus.RUNNING_RENAME.value,
            TaskStatus.RUNNING_REFRESH.value,
            TaskStatus.RUNNING_LOGIN.value,
        ]
        active_count = db.query(Task).filter(Task.status.in_(active_statuses)).count()
        if active_count:
            raise HTTPException(status_code=409, detail="Cannot clear task list while tasks are still running.")

        deleted_count = db.query(Task).count()
        db.query(Task).delete(synchronize_session=False)
        db.commit()
        return Envelope(message="Task list cleared.", data=TaskClearResult(deleted_count=deleted_count))

    @router.post("/library/rescan", response_model=Envelope[TaskResponse])
    async def rescan_library() -> Envelope[TaskResponse]:
        task_id = task_manager.enqueue_rescan()
        return Envelope(message="已创建本地重扫任务。", data=TaskResponse(task_id=task_id, task_status="queued"))

    @router.post("/archives/delete", response_model=Envelope[ArchiveActionResult])
    def delete_archive(payload: ArchiveDeleteRequest, db: Session = Depends(get_db)) -> Envelope[ArchiveActionResult]:
        post = db.query(Post).filter_by(post_id=payload.post_id).one_or_none()
        if post is None:
            raise HTTPException(status_code=404, detail="Post not found")
        artifact = db.query(Artifact).filter_by(post_id=post.id).one_or_none()
        if artifact is None or not artifact.archive_path:
            return Envelope(message="该帖子没有可删除的压缩包。", data=ArchiveActionResult(deleted_count=0, deleted_paths=[]))

        archive_path = artifact.archive_path
        deleted = delete_archive_file(archive_path)
        if deleted:
            artifact.archive_path = None
            db.commit()
            return Envelope(
                message="已删除对应压缩包。",
                data=ArchiveActionResult(deleted_count=1, deleted_paths=[archive_path]),
            )

        artifact.archive_path = None
        db.commit()
        return Envelope(message="压缩包文件不存在，已清理记录。", data=ArchiveActionResult(deleted_count=0, deleted_paths=[]))

    @router.post("/posts/open-local-path", response_model=Envelope[OpenPathResult])
    def open_local_path(payload: ArchiveDeleteRequest, db: Session = Depends(get_db)) -> Envelope[OpenPathResult]:
        post = db.query(Post).filter_by(post_id=payload.post_id).one_or_none()
        if post is None:
            raise HTTPException(status_code=404, detail="Post not found")

        artifact = db.query(Artifact).filter_by(post_id=post.id).one_or_none()
        open_path = resolve_post_open_path(
            artifact.extract_dir if artifact else None,
            artifact.archive_path if artifact else None,
        )
        if not open_path:
            raise HTTPException(status_code=404, detail="Local path not found")

        open_in_explorer(open_path)
        return Envelope(message="已打开本地路径。", data=OpenPathResult(opened_path=open_path))

    @router.post("/library/open-year", response_model=Envelope[OpenPathResult])
    def open_year_folder(payload: YearOpenRequest, db: Session = Depends(get_db)) -> Envelope[OpenPathResult]:
        settings = db.get(Settings, 1)
        assert settings is not None

        year_path = Path(settings.library_dir) / payload.year
        if not year_path.exists() or not year_path.is_dir():
            raise HTTPException(status_code=404, detail="Year folder not found")

        open_in_explorer(str(year_path))
        return Envelope(message="已打开年份目录。", data=OpenPathResult(opened_path=str(year_path)))

    @router.post("/archives/purge", response_model=Envelope[ArchiveActionResult])
    def purge_archives(payload: ArchivePurgeRequest, db: Session = Depends(get_db)) -> Envelope[ArchiveActionResult]:
        settings = db.get(Settings, 1)
        assert settings is not None
        deleted_paths = purge_archive_files(settings.library_dir, payload.year)
        if deleted_paths:
            artifacts = db.query(Artifact).all()
            deleted_set = set(deleted_paths)
            for artifact in artifacts:
                if artifact.archive_path in deleted_set:
                    artifact.archive_path = None
            db.commit()

        target_label = f"{payload.year} 年" if payload.year else "所有年份"
        return Envelope(
            message=f"已清理 {target_label} 的 Archive 压缩包。",
            data=ArchiveActionResult(deleted_count=len(deleted_paths), deleted_paths=deleted_paths),
        )

    @router.get("/settings", response_model=Envelope[SettingsRead])
    def get_settings(db: Session = Depends(get_db)) -> Envelope[SettingsRead]:
        settings = db.get(Settings, 1)
        assert settings is not None
        return Envelope(
            data=SettingsRead(
                creator_url=settings.creator_url,
                profile_dir=settings.profile_dir,
                download_dir=settings.download_dir,
                library_dir=settings.library_dir,
                temp_dir=settings.temp_dir,
                mega_command=settings.mega_command,
                playwright_channel=settings.playwright_channel,
                refresh_interval_minutes=settings.refresh_interval_minutes,
                download_concurrency=settings.download_concurrency,
                posts_per_row=settings.posts_per_row,
                auto_delete_archive=settings.auto_delete_archive,
            )
        )

    @router.post("/settings", response_model=Envelope[SettingsRead])
    def update_settings(payload: SettingsUpdate, db: Session = Depends(get_db)) -> Envelope[SettingsRead]:
        settings = db.get(Settings, 1)
        assert settings is not None
        settings.creator_url = payload.creator_url
        settings.profile_dir = payload.profile_dir
        settings.download_dir = payload.download_dir
        settings.library_dir = payload.library_dir
        settings.temp_dir = payload.temp_dir
        settings.mega_command = payload.mega_command
        settings.playwright_channel = payload.playwright_channel
        settings.refresh_interval_minutes = payload.refresh_interval_minutes
        settings.download_concurrency = payload.download_concurrency
        settings.posts_per_row = payload.posts_per_row
        settings.auto_delete_archive = payload.auto_delete_archive
        db.commit()
        task_manager.set_download_concurrency(payload.download_concurrency)
        return Envelope(message="设置已保存。", data=SettingsRead.model_validate(payload.model_dump()))

    return router
