from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Integer, cast
from sqlalchemy.orm import Session

from app.db.models import (
    Artifact,
    DownloadFailureKind,
    Post,
    PostOperationStatus,
    PostPrimaryAction,
    PostStatus,
    RefreshMode,
    Settings,
    Task,
    TaskKind,
    TaskStatus,
)
from app.db.session import get_db
from app.schemas.archive import ArchiveActionResult, ArchiveDeleteRequest, ArchivePurgeRequest, OpenPathResult, YearOpenRequest
from app.schemas.common import Envelope, TaskResponse
from app.schemas.post import DownloadRequest, PostRead, RefreshRequest, TitleAliasUpsertRequest, TitleAliasUpsertResult
from app.schemas.settings import (
    AuthStatus,
    SettingsRead,
    SettingsUpdate,
    TitleAliasesClearResult,
    TitleAnnotationCacheClearResult,
)
from app.schemas.task import RetryRequest, TaskClearResult, TaskRead
from app.services.fanbox import inspect_auth_status, logout_fanbox
from app.services.files import delete_archive_file, open_in_explorer, purge_archive_files, resolve_post_open_path
from app.services.mega import MegaCommandConfigurationError, classify_mega_download_failure
from app.services.tasks import TaskManager
from app.services.title_annotation import (
    build_display_title,
    clear_title_aliases_file,
    ensure_title_aliases_file,
    load_title_aliases,
    prepare_post_title_annotation,
    resolve_dictionary_annotation,
    resolve_title_aliases_path,
    title_needs_annotation,
    upsert_title_alias_entries,
)


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


def _resolve_download_failure(
    return_code: int | None,
    failure_kind: str | None,
    text: str | None,
) -> tuple[str | None, int | None]:
    if failure_kind:
        return failure_kind, return_code
    if not text and return_code is None:
        return None, return_code
    return classify_mega_download_failure(return_code, text).value, return_code


def _derive_post_primary_action(post: Post, artifact: Artifact | None, download_failure_kind: str | None) -> str:
    if post.status == PostStatus.COMPLETED.value:
        return PostPrimaryAction.COMPLETED.value

    if (
        post.operation_status == PostOperationStatus.FAILED_DOWNLOAD.value
        and download_failure_kind == DownloadFailureKind.UNAVAILABLE.value
    ):
        return PostPrimaryAction.STALE_LINK.value

    if post.operation_status in {
        PostOperationStatus.FAILED_CONFIG.value,
        PostOperationStatus.FAILED_DOWNLOAD.value,
        PostOperationStatus.FAILED_EXTRACT.value,
        PostOperationStatus.FAILED_RENAME.value,
    }:
        return PostPrimaryAction.REDOWNLOAD.value

    if artifact is not None and artifact.archive_path:
        return PostPrimaryAction.EXTRACT.value

    if post.mega_url:
        return PostPrimaryAction.DOWNLOAD.value

    return PostPrimaryAction.DOWNLOAD.value


def create_router(task_manager: TaskManager) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/posts", response_model=Envelope[list[PostRead]])
    def list_posts(db: Session = Depends(get_db)) -> Envelope[list[PostRead]]:
        posts = db.query(Post).order_by(Post.published_at.desc().nullslast(), cast(Post.post_id, Integer).desc()).all()
        posts = _dedupe_posts_by_mega(posts)
        settings = db.get(Settings, 1)
        assert settings is not None
        aliases = load_title_aliases(settings=settings)
        payload: list[PostRead] = []
        for post in posts:
            artifact = db.query(Artifact).filter_by(post_id=post.id).one_or_none()
            latest_download_task = (
                db.query(Task)
                .filter(Task.kind == TaskKind.DOWNLOAD_POST.value, Task.post_id == post.id)
                .order_by(Task.created_at.desc())
                .first()
            )
            open_path = resolve_post_open_path(
                artifact.extract_dir if artifact else None,
                artifact.archive_path if artifact else None,
            )
            download_failure_kind, download_return_code = _resolve_download_failure(
                post.download_return_code if hasattr(post, "download_return_code") else None,
                post.download_failure_kind if hasattr(post, "download_failure_kind") else None,
                post.last_error or (latest_download_task.error if latest_download_task else None),
            )
            primary_action = _derive_post_primary_action(post, artifact, download_failure_kind)
            dictionary_result = resolve_dictionary_annotation(post.title, aliases)
            effective_annotation = dictionary_result.annotation if dictionary_result and dictionary_result.annotation else post.title_annotation
            payload.append(
                PostRead(
                    id=post.id,
                    post_id=post.post_id,
                    title=post.title,
                    title_annotation=effective_annotation,
                    has_title_annotation=bool(effective_annotation),
                    display_title=build_display_title(post.title, effective_annotation),
                    published_at=post.published_at,
                    detail_url=post.detail_url,
                    cover_url=post.cover_url,
                    mega_url=post.mega_url,
                    status=post.status,
                    operation_status=post.operation_status,
                    primary_action=primary_action,
                    download_failure_kind=download_failure_kind,
                    download_return_code=download_return_code,
                    last_error=post.last_error,
                    archive_path=artifact.archive_path if artifact else None,
                    extract_dir=artifact.extract_dir if artifact else None,
                    can_open_local_path=bool(open_path),
                    needs_title_annotation=title_needs_annotation(post.title),
                    updated_at=post.updated_at,
                )
            )
        return Envelope(data=payload)

    @router.post("/posts/refresh", response_model=Envelope[TaskResponse])
    async def refresh_posts_endpoint(payload: RefreshRequest | None = None) -> Envelope[TaskResponse]:
        mode = RefreshMode(payload.mode) if payload is not None else RefreshMode.INCREMENTAL
        auto_rescan_after = payload.auto_rescan_after if payload is not None else False
        task_id = task_manager.enqueue_refresh(mode, auto_rescan_after=auto_rescan_after)
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

    @router.post("/auth/logout", response_model=Envelope[AuthStatus])
    async def logout(db: Session = Depends(get_db)) -> Envelope[AuthStatus]:
        if task_manager.has_active_auth_profile_task():
            raise HTTPException(status_code=409, detail="当前仍有登录或刷新任务在运行，请等待任务结束后再退出登录。")

        settings = db.get(Settings, 1)
        assert settings is not None
        await logout_fanbox(settings)
        return Envelope(
            message="已退出登录。",
            data=AuthStatus(
                authenticated=False,
                profile_exists=False,
                reason="Current Fanbox login state was cleared.",
            ),
        )

    @router.post("/tasks/download", response_model=Envelope[TaskResponse])
    async def create_download_tasks(payload: DownloadRequest) -> Envelope[TaskResponse]:
        if not payload.post_ids:
            raise HTTPException(status_code=400, detail="post_ids must not be empty")
        try:
            task_ids = task_manager.enqueue_downloads(payload.post_ids)
        except MegaCommandConfigurationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Envelope(
            message=f"已创建 {len(task_ids)} 个下载任务。",
            data=TaskResponse(task_ids=task_ids, task_status="queued"),
        )

    @router.post("/tasks/retry", response_model=Envelope[TaskResponse])
    async def retry_task(payload: RetryRequest) -> Envelope[TaskResponse]:
        try:
            task_id = task_manager.retry_task(payload.task_id)
        except MegaCommandConfigurationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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
                download_failure_kind=task.download_failure_kind,
                download_return_code=task.download_return_code,
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
            TaskStatus.RUNNING_ANNOTATE.value,
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

    @router.post("/title-aliases/open", response_model=Envelope[OpenPathResult])
    def open_title_aliases_path(db: Session = Depends(get_db)) -> Envelope[OpenPathResult]:
        settings = db.get(Settings, 1)
        assert settings is not None

        aliases_path = ensure_title_aliases_file(resolve_title_aliases_path(settings))
        open_target = aliases_path.parent if aliases_path.suffix else aliases_path
        open_in_explorer(str(open_target))
        return Envelope(message="Opened title aliases path.", data=OpenPathResult(opened_path=str(open_target)))

    @router.post("/title-aliases/clear", response_model=Envelope[TitleAliasesClearResult])
    def clear_title_aliases(db: Session = Depends(get_db)) -> Envelope[TitleAliasesClearResult]:
        settings = db.get(Settings, 1)
        assert settings is not None

        aliases_path = clear_title_aliases_file(resolve_title_aliases_path(settings))
        return Envelope(message="Local title aliases cleared.", data=TitleAliasesClearResult(cleared_path=str(aliases_path)))

    @router.post("/title-aliases/upsert", response_model=Envelope[TitleAliasUpsertResult])
    def upsert_title_aliases(payload: TitleAliasUpsertRequest, db: Session = Depends(get_db)) -> Envelope[TitleAliasUpsertResult]:
        settings = db.get(Settings, 1)
        assert settings is not None

        post = db.query(Post).filter_by(post_id=payload.post_id).one_or_none()
        if post is None:
            raise HTTPException(status_code=404, detail="Post not found")
        if not title_needs_annotation(post.title):
            raise HTTPException(status_code=400, detail="This title does not need annotation.")

        if payload.mode == "full_title":
            translation = (payload.full_title_translation or "").strip()
            if not translation:
                raise HTTPException(status_code=400, detail="full_title_translation is required.")
            updates = [(post.title, translation)]
        else:
            if not payload.entries:
                raise HTTPException(status_code=400, detail="entries must not be empty.")
            updates = [(entry.source, entry.target) for entry in payload.entries]

        aliases_path, updated_count = upsert_title_alias_entries(resolve_title_aliases_path(settings), payload.mode, updates)
        return Envelope(
            message="Local title aliases updated.",
            data=TitleAliasUpsertResult(updated_count=updated_count, aliases_path=str(aliases_path)),
        )

    @router.post("/title-annotations/clear-cache", response_model=Envelope[TitleAnnotationCacheClearResult])
    def clear_title_annotation_cache(db: Session = Depends(get_db)) -> Envelope[TitleAnnotationCacheClearResult]:
        posts = db.query(Post).all()
        reset_count = 0
        for post in posts:
            if prepare_post_title_annotation(post, force_reset=True):
                reset_count += 1
        db.commit()
        return Envelope(
            message="LLM title annotation cache cleared.",
            data=TitleAnnotationCacheClearResult(reset_count=reset_count),
        )

    @router.post("/archives/purge", response_model=Envelope[ArchiveActionResult])
    def purge_archives(payload: ArchivePurgeRequest, db: Session = Depends(get_db)) -> Envelope[ArchiveActionResult]:
        settings = db.get(Settings, 1)
        assert settings is not None
        deleted_paths = purge_archive_files(settings.download_dir, payload.year)
        if deleted_paths:
            artifacts = db.query(Artifact).all()
            deleted_set = set(deleted_paths)
            for artifact in artifacts:
                if artifact.archive_path in deleted_set:
                    artifact.archive_path = None
            db.commit()

        target_label = f"{payload.year} 年" if payload.year else "所有年份"
        return Envelope(
            message=f"已清理 {target_label} 下载目录中的压缩包。",
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
                llm_enabled=settings.llm_enabled,
                llm_api_key=settings.llm_api_key or "",
                llm_base_url=settings.llm_base_url,
                llm_model=settings.llm_model,
                title_aliases_path=settings.title_aliases_path,
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
        settings.llm_enabled = payload.llm_enabled
        settings.llm_api_key = payload.llm_api_key or None
        settings.llm_base_url = payload.llm_base_url
        settings.llm_model = payload.llm_model
        settings.title_aliases_path = payload.title_aliases_path
        db.commit()
        task_manager.set_download_concurrency(payload.download_concurrency)
        return Envelope(message="设置已保存。", data=SettingsRead.model_validate(payload.model_dump()))

    return router
