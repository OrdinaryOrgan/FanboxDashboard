from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from app.db.models import (
    Artifact,
    DownloadFailureKind,
    Post,
    PostOperationStatus,
    PostStatus,
    RefreshMode,
    Settings,
    Task,
    TaskKind,
    TaskStatus,
    TitleAnnotationSource,
    TitleAnnotationStatus,
)
from app.db.session import SessionLocal
from app.services.fanbox import (
    ExistingPostSnapshot,
    FanboxAuthError,
    FanboxConfigurationError,
    open_login_window,
    refresh_posts,
)
from app.services.files import (
    build_post_storage_paths,
    delete_archive_file,
    directory_has_content,
    ensure_directories,
    extract_zip,
    reconcile_archive_path,
    reconcile_extract_dir,
    rename_images,
    serialize_processed_files,
)
from app.services.mega import (
    MegaCommandConfigurationError,
    MegaDownloadError,
    classify_mega_download_failure,
    describe_mega_download_failure,
    download_public_link,
    resolve_mega_command,
)
from app.services.title_annotation import (
    TitleAnnotationResult,
    find_completed_annotation_for_same_title,
    load_llm_config,
    load_title_aliases,
    prepare_post_title_annotation,
    resolve_title_annotation,
)


class TaskManager:
    def __init__(self) -> None:
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._download_concurrency = _resolve_download_concurrency()
        self._download_semaphore = asyncio.Semaphore(self._download_concurrency)

    def reconcile_interrupted_tasks(self) -> None:
        interrupted_statuses = [
            TaskStatus.QUEUED.value,
            TaskStatus.RUNNING_DOWNLOAD.value,
            TaskStatus.RUNNING_EXTRACT.value,
            TaskStatus.RUNNING_RENAME.value,
            TaskStatus.RUNNING_REFRESH.value,
            TaskStatus.RUNNING_ANNOTATE.value,
            TaskStatus.RUNNING_LOGIN.value,
        ]
        with SessionLocal() as session:
            tasks = session.query(Task).filter(Task.status.in_(interrupted_statuses)).all()
            if not tasks:
                return

            for task in tasks:
                interrupted_status, interrupted_message = _interrupted_task_outcome(task.kind)
                task.status = interrupted_status
                task.error = interrupted_message
                task.message = interrupted_message
                task.finished_at = datetime.now(timezone.utc)

                if task.post is not None and task.kind == TaskKind.DOWNLOAD_POST.value:
                    task.post.status = _fallback_post_inventory_status(task.post)
                    task.post.operation_status = PostOperationStatus.FAILED_DOWNLOAD.value
                    task.post.last_error = interrupted_message
                    task.post.download_failure_kind = DownloadFailureKind.RETRYABLE.value
                    task.post.download_return_code = None
                    task.download_failure_kind = DownloadFailureKind.RETRYABLE.value
                    task.download_return_code = None
            session.commit()

    def set_download_concurrency(self, value: int) -> None:
        self._download_concurrency = max(1, value)
        self._download_semaphore = asyncio.Semaphore(self._download_concurrency)

    def enqueue_refresh(self, mode: RefreshMode = RefreshMode.INCREMENTAL, auto_rescan_after: bool = False) -> str:
        with SessionLocal() as session:
            active_task = self._find_active_refresh_task(session)
            if active_task is not None:
                return active_task.id
        task_id = self._create_task(TaskKind.REFRESH_POSTS, refresh_mode=mode)
        self._running_tasks[task_id] = asyncio.create_task(self._run_refresh(task_id, mode, auto_rescan_after))
        return task_id

    def enqueue_login(self) -> str:
        task_id = self._create_task(TaskKind.OPEN_LOGIN)
        self._running_tasks[task_id] = asyncio.create_task(self._run_login(task_id))
        return task_id

    def has_active_auth_profile_task(self) -> bool:
        with SessionLocal() as session:
            return self._find_active_auth_profile_task(session) is not None

    def enqueue_annotation(self) -> str:
        with SessionLocal() as session:
            active_task = self._find_active_annotation_task(session)
            if active_task is not None:
                return active_task.id
            pending_count = (
                session.query(Post).filter(Post.title_annotation_status == TitleAnnotationStatus.PENDING.value).count()
            )
            if pending_count == 0:
                return ""

        task_id = self._create_task(TaskKind.ANNOTATE_TITLES)
        self._running_tasks[task_id] = asyncio.create_task(self._run_annotate(task_id))
        return task_id

    def enqueue_downloads(self, post_ids: list[str]) -> list[str]:
        created: list[str] = []
        with SessionLocal() as session:
            settings = session.get(Settings, 1)
            assert settings is not None
            posts = session.query(Post).filter(Post.post_id.in_(post_ids)).all()
            self._validate_download_command_for_posts(session, settings, posts)
            for post in posts:
                post.operation_status = PostOperationStatus.QUEUED.value
                post.last_error = None
                post.download_failure_kind = None
                post.download_return_code = None
                task_id = self._create_task(TaskKind.DOWNLOAD_POST, post=post, session=session)
                created.append(task_id)
                self._running_tasks[task_id] = asyncio.create_task(self._run_download(task_id, post.id))
            session.commit()
        return created

    def enqueue_rescan(self) -> str:
        with SessionLocal() as session:
            active_task = self._find_active_rescan_task(session)
            if active_task is not None:
                return active_task.id
        task_id = self._create_task(TaskKind.RESCAN_LIBRARY)
        self._running_tasks[task_id] = asyncio.create_task(self._run_rescan(task_id))
        return task_id

    def retry_task(self, source_task_id: str) -> str:
        with SessionLocal() as session:
            task = session.get(Task, source_task_id)
            if task is None:
                raise ValueError("Task not found")
            if task.kind == TaskKind.DOWNLOAD_POST.value and task.post is not None:
                return self.enqueue_downloads([task.post.post_id])[0]
            if task.kind == TaskKind.REFRESH_POSTS.value:
                refresh_mode = RefreshMode(task.refresh_mode or RefreshMode.INCREMENTAL.value)
                return self.enqueue_refresh(refresh_mode)
            if task.kind == TaskKind.ANNOTATE_TITLES.value:
                return self.enqueue_annotation()
            if task.kind == TaskKind.OPEN_LOGIN.value:
                return self.enqueue_login()
            if task.kind == TaskKind.RESCAN_LIBRARY.value:
                return self.enqueue_rescan()
            raise ValueError("Unsupported task kind")

    def _create_task(
        self,
        kind: TaskKind,
        post: Post | None = None,
        session: Session | None = None,
        refresh_mode: RefreshMode | None = None,
    ) -> str:
        task_id = str(uuid4())
        db = session or SessionLocal()
        owns_session = session is None
        try:
            db.add(
                Task(
                    id=task_id,
                    kind=kind.value,
                    status=TaskStatus.QUEUED.value,
                    post_id=post.id if post else None,
                    refresh_mode=refresh_mode.value if refresh_mode else None,
                )
            )
            if owns_session:
                db.commit()
        finally:
            if owns_session:
                db.close()
        return task_id

    async def _run_refresh(self, task_id: str, mode: RefreshMode, auto_rescan_after: bool = False) -> None:
        try:
            self._mark_started(
                task_id,
                TaskStatus.RUNNING_REFRESH.value,
                _refresh_progress_message(mode, 0, None),
                progress_current=0,
                progress_total=None,
            )
            with SessionLocal() as session:
                settings = session.get(Settings, 1)
                assert settings is not None
                existing_posts = self._build_existing_post_snapshots(session)
            scraped_posts = await asyncio.wait_for(
                refresh_posts(
                    settings,
                    mode=mode,
                    progress_callback=lambda current, total: self._update_refresh_progress(task_id, mode, current, total),
                    existing_posts=existing_posts,
                ),
                timeout=_resolve_refresh_timeout_seconds(),
            )
            with SessionLocal() as session:
                settings = session.get(Settings, 1)
                assert settings is not None
                pending_annotation_count = 0
                for item in scraped_posts:
                    remote_changed = False
                    existing = session.query(Post).filter_by(post_id=item.post_id).one_or_none()
                    if existing is None:
                        remote_changed = True
                        existing = Post(
                            post_id=item.post_id,
                            title=item.title,
                            published_at=item.published_at,
                            detail_url=item.detail_url,
                            cover_url=item.cover_url,
                            mega_url=item.mega_url,
                            status=item.status,
                            operation_status=PostOperationStatus.IDLE.value,
                            last_error=item.last_error,
                        )
                        session.add(existing)
                        session.flush()
                    else:
                        remote_changed = self._apply_remote_post_updates(existing, item)

                    if prepare_post_title_annotation(existing, force_reset=remote_changed):
                        pending_annotation_count += 1
                    if remote_changed:
                        self._sync_local_status(session, settings, existing)
                session.commit()
            if pending_annotation_count:
                self.enqueue_annotation()
            with SessionLocal() as session:
                task = session.get(Task, task_id)
                progress_current = task.progress_current if task is not None else None
                progress_total = task.progress_total if task is not None else None
                stored_mode = RefreshMode(task.refresh_mode or mode.value) if task is not None else mode
            final_total = progress_total if stored_mode == RefreshMode.FULL else None
            self._mark_completed(
                task_id,
                _refresh_completed_message(stored_mode, len(scraped_posts)),
                progress_current=progress_current,
                progress_total=final_total,
            )
            if auto_rescan_after:
                self.enqueue_rescan()
        except FanboxConfigurationError as exc:
            self._mark_failed(task_id, TaskStatus.FAILED_CONFIG.value, str(exc))
        except FanboxAuthError as exc:
            self._mark_failed(task_id, TaskStatus.FAILED_AUTH.value, str(exc))
        except TimeoutError:
            self._mark_failed(
                task_id,
                TaskStatus.FAILED_PARSE.value,
                f"Refresh timed out after {_resolve_refresh_timeout_seconds()} seconds.",
            )
        except Exception as exc:
            self._mark_failed(task_id, TaskStatus.FAILED_PARSE.value, str(exc))
        finally:
            self._running_tasks.pop(task_id, None)

    async def _run_annotate(self, task_id: str) -> None:
        try:
            self._mark_started(task_id, TaskStatus.RUNNING_ANNOTATE.value, "Annotating titles.")
            with SessionLocal() as session:
                settings = session.get(Settings, 1)
                assert settings is not None
                aliases = load_title_aliases(settings=settings)
                llm_config = load_llm_config(settings)
            local_cache: dict[str, TitleAnnotationResult] = {}
            annotation_concurrency = _resolve_annotation_concurrency()
            processed = 0
            completed = 0
            skipped = 0
            failed = 0

            while True:
                with SessionLocal() as session:
                    posts = (
                        session.query(Post)
                        .filter(Post.title_annotation_status == TitleAnnotationStatus.PENDING.value)
                        .order_by(Post.updated_at.desc(), Post.id.desc())
                        .limit(_resolve_annotation_batch_size())
                        .all()
                    )
                    if not posts:
                        break

                    batch_results: dict[int, TitleAnnotationResult] = {}
                    unresolved_titles: list[str] = []
                    for post in posts:
                        cached_result = local_cache.get(post.title)
                        if cached_result is None:
                            cached_result = find_completed_annotation_for_same_title(session, post)
                        if cached_result is None:
                            unresolved_titles.append(post.title)
                            continue
                        local_cache[post.title] = cached_result
                        batch_results[post.id] = cached_result

                    unique_unresolved_titles = list(dict.fromkeys(unresolved_titles))
                    if unique_unresolved_titles:
                        resolved_results = await self._resolve_annotation_batch(
                            unique_unresolved_titles,
                            aliases,
                            llm_config,
                            annotation_concurrency,
                        )
                        local_cache.update(resolved_results)

                    for post in posts:
                        cached_result = batch_results.get(post.id) or local_cache.get(post.title)
                        if cached_result is None:
                            cached_result = TitleAnnotationResult(
                                annotation=None,
                                source=TitleAnnotationSource.NONE.value,
                                status=TitleAnnotationStatus.FAILED.value,
                                error="Title annotation result was missing after batch processing.",
                            )
                        self._apply_annotation_result(post, cached_result)

                        processed += 1
                        if cached_result.status == TitleAnnotationStatus.COMPLETED.value:
                            completed += 1
                        elif cached_result.status == TitleAnnotationStatus.SKIPPED.value:
                            skipped += 1
                        else:
                            failed += 1

                    session.commit()

                self._update_task_status(
                    task_id,
                    TaskStatus.RUNNING_ANNOTATE.value,
                    (
                        "Annotating titles. "
                        f"Processed {processed}, completed {completed}, skipped {skipped}, failed {failed}."
                    ),
                )

            self._mark_completed(
                task_id,
                (
                    "Title annotation finished. "
                    f"Processed {processed}, completed {completed}, skipped {skipped}, failed {failed}."
                ),
            )
        except Exception as exc:
            self._mark_failed(task_id, TaskStatus.FAILED_ANNOTATE.value, str(exc))
        finally:
            self._running_tasks.pop(task_id, None)

    async def _resolve_annotation_batch(
        self,
        titles: list[str],
        aliases,
        llm_config,
        concurrency: int,
    ) -> dict[str, TitleAnnotationResult]:
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def resolve_one(title: str) -> tuple[str, TitleAnnotationResult]:
            async with semaphore:
                try:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(resolve_title_annotation, title, aliases, llm_config),
                        timeout=_resolve_annotation_timeout_seconds(),
                    )
                except TimeoutError:
                    result = TitleAnnotationResult(
                        annotation=None,
                        source=TitleAnnotationSource.NONE.value,
                        status=TitleAnnotationStatus.FAILED.value,
                        error=f"Title annotation timed out after {_resolve_annotation_timeout_seconds()} seconds.",
                    )
                return title, result

        resolved_pairs = await asyncio.gather(*(resolve_one(title) for title in titles))
        return dict(resolved_pairs)

    async def _run_login(self, task_id: str) -> None:
        try:
            self._mark_started(task_id, TaskStatus.RUNNING_LOGIN.value, "Opening login window.")
            with SessionLocal() as session:
                settings = session.get(Settings, 1)
                assert settings is not None
            message = await open_login_window(settings)
            self._mark_completed(task_id, message)
        except Exception as exc:
            self._mark_failed(task_id, TaskStatus.FAILED_AUTH.value, str(exc))
        finally:
            self._running_tasks.pop(task_id, None)

    async def _run_download(self, task_id: str, post_db_id: int) -> None:
        try:
            async with self._download_semaphore:
                with SessionLocal() as session:
                    settings = session.get(Settings, 1)
                    post = session.get(Post, post_db_id)
                    assert settings is not None and post is not None
                    if not post.mega_url:
                        raise ValueError("Current post does not have a usable MEGA link.")

                    paths = build_post_storage_paths(
                        library_root=settings.library_dir,
                        download_root=settings.download_dir,
                        title=post.title,
                        published_at=post.published_at,
                        fallback_name=post.post_id,
                    )
                    ensure_directories(settings.download_dir, settings.library_dir, settings.temp_dir)
                    ensure_directories(paths.download_dir, str(Path(paths.extract_dir).parent))
                    artifact = session.query(Artifact).filter_by(post_id=post_db_id).one_or_none()
                    existing_archive_path = reconcile_archive_path(
                        current_archive_path=artifact.archive_path if artifact else None,
                        library_root=settings.library_dir,
                        download_root=settings.download_dir,
                        title=post.title,
                        published_at=post.published_at,
                        fallback_name=post.post_id,
                    )
                    mega_url = post.mega_url
                    mega_command = settings.mega_command
                    auto_delete_archive = settings.auto_delete_archive

                if existing_archive_path:
                    archive_path = existing_archive_path
                    self._mark_post_operation_status(post_db_id, PostOperationStatus.RUNNING_EXTRACT.value)
                    self._mark_started(task_id, TaskStatus.RUNNING_EXTRACT.value, "Extracting existing archive from local download folder.")
                    self._append_log(task_id, f"Reused existing archive: {archive_path}")
                else:
                    self._mark_post_operation_status(post_db_id, PostOperationStatus.RUNNING_DOWNLOAD.value)
                    self._mark_started(task_id, TaskStatus.RUNNING_DOWNLOAD.value, "Downloading archive from MEGA.")
                    archive_path, mega_log = await asyncio.wait_for(
                        self._download_archive_with_retries(
                            mega_url,
                            paths.download_dir,
                            mega_command,
                            expected_archive_prefixes=(paths.folder_name, post.post_id),
                        ),
                        timeout=_resolve_download_timeout_seconds(),
                    )
                    self._append_log(task_id, mega_log)

                    self._mark_post_operation_status(post_db_id, PostOperationStatus.RUNNING_EXTRACT.value)
                self._update_task_status(task_id, TaskStatus.RUNNING_EXTRACT.value, "Extracting zip archive.")
                extract_zip(archive_path, paths.extract_dir)

                self._mark_post_operation_status(post_db_id, PostOperationStatus.RUNNING_RENAME.value)
                self._update_task_status(task_id, TaskStatus.RUNNING_RENAME.value, "Renaming extracted image files.")
                rename_changes = rename_images(paths.extract_dir)
                stored_archive_path = archive_path
                if auto_delete_archive and delete_archive_file(archive_path):
                    stored_archive_path = None
                    self._append_log(task_id, "Archive deleted automatically after extraction and rename.")

                with SessionLocal() as session:
                    artifact = session.query(Artifact).filter_by(post_id=post_db_id).one_or_none()
                    if artifact is None:
                        artifact = Artifact(post_id=post_db_id)
                        session.add(artifact)
                    artifact.archive_path = stored_archive_path
                    artifact.extract_dir = paths.extract_dir
                    artifact.processed_files = serialize_processed_files(rename_changes)
                    post = session.get(Post, post_db_id)
                    assert post is not None
                    post.status = PostStatus.COMPLETED.value
                    post.operation_status = PostOperationStatus.IDLE.value
                    post.last_error = None
                    post.download_failure_kind = None
                    post.download_return_code = None
                    task = session.get(Task, task_id)
                    assert task is not None
                    task.status = TaskStatus.COMPLETED.value
                    task.message = "Archive processing finished."
                    task.error = None
                    task.download_failure_kind = None
                    task.download_return_code = None
                    task.finished_at = datetime.now(timezone.utc)
                    session.commit()
        except MegaCommandConfigurationError as exc:
            self._mark_post_download_failed(post_db_id, PostOperationStatus.FAILED_CONFIG.value, str(exc))
            self._mark_failed(task_id, TaskStatus.FAILED_CONFIG.value, str(exc))
        except MegaDownloadError as exc:
            failure_kind = classify_mega_download_failure(exc.return_code, exc.output)
            error_message = describe_mega_download_failure(failure_kind, exc.return_code, exc.output)
            self._append_log(task_id, exc.output)
            self._mark_post_download_failed(
                post_db_id,
                PostOperationStatus.FAILED_DOWNLOAD.value,
                error_message,
                failure_kind=failure_kind.value,
                return_code=exc.return_code,
            )
            self._mark_failed(
                task_id,
                TaskStatus.FAILED_DOWNLOAD.value,
                error_message,
                failure_kind=failure_kind.value,
                return_code=exc.return_code,
            )
        except TimeoutError:
            timeout_message = f"Download timed out after {_resolve_download_timeout_seconds()} seconds."
            self._mark_post_download_failed(
                post_db_id,
                PostOperationStatus.FAILED_DOWNLOAD.value,
                timeout_message,
                failure_kind=DownloadFailureKind.RETRYABLE.value,
            )
            self._mark_failed(
                task_id,
                TaskStatus.FAILED_DOWNLOAD.value,
                timeout_message,
                failure_kind=DownloadFailureKind.RETRYABLE.value,
            )
        except ValueError as exc:
            self._mark_post_download_failed(post_db_id, PostOperationStatus.FAILED_EXTRACT.value, str(exc))
            self._mark_failed(task_id, TaskStatus.FAILED_EXTRACT.value, str(exc))
        except Exception as exc:
            self._mark_post_download_failed(post_db_id, PostOperationStatus.FAILED_RENAME.value, str(exc))
            self._mark_failed(task_id, TaskStatus.FAILED_RENAME.value, str(exc))
        finally:
            self._running_tasks.pop(task_id, None)

    async def _run_rescan(self, task_id: str) -> None:
        try:
            self._mark_started(task_id, TaskStatus.RUNNING_REFRESH.value, "Rescanning local library paths.")
            with SessionLocal() as session:
                settings = session.get(Settings, 1)
                assert settings is not None
                posts = session.query(Post).all()
                for post in posts:
                    self._sync_local_status(session, settings, post)
                session.commit()
            self._mark_completed(task_id, "Local rescan finished.")
        except Exception as exc:
            self._mark_failed(task_id, TaskStatus.FAILED_PARSE.value, str(exc))
        finally:
            self._running_tasks.pop(task_id, None)

    def _sync_local_status(self, session: Session, settings: Settings, post: Post) -> None:
        paths = build_post_storage_paths(
            library_root=settings.library_dir,
            download_root=settings.download_dir,
            title=post.title,
            published_at=post.published_at,
            fallback_name=post.post_id,
        )
        artifact = session.query(Artifact).filter_by(post_id=post.id).one_or_none()
        if artifact is None:
            artifact = Artifact(post_id=post.id)
            session.add(artifact)

        artifact.archive_path = reconcile_archive_path(
            current_archive_path=artifact.archive_path,
            library_root=settings.library_dir,
            download_root=settings.download_dir,
            title=post.title,
            published_at=post.published_at,
            fallback_name=post.post_id,
        )
        resolved_extract_dir = reconcile_extract_dir(
            current_extract_dir=artifact.extract_dir,
            library_root=settings.library_dir,
            title=post.title,
            published_at=post.published_at,
            fallback_name=post.post_id,
        )

        if directory_has_content(resolved_extract_dir):
            artifact.extract_dir = resolved_extract_dir
            post.status = PostStatus.COMPLETED.value
            post.operation_status = PostOperationStatus.IDLE.value
            post.last_error = None
            post.download_failure_kind = None
            post.download_return_code = None
        elif post.mega_url:
            artifact.extract_dir = resolved_extract_dir
            post.status = PostStatus.MISSING_LOCAL.value
            if artifact.archive_path:
                post.operation_status = PostOperationStatus.IDLE.value
                post.last_error = None
                post.download_failure_kind = None
                post.download_return_code = None

    def _build_existing_post_snapshots(self, session: Session) -> dict[str, ExistingPostSnapshot]:
        posts = session.query(Post).all()
        return {
            post.post_id: ExistingPostSnapshot(
                title=post.title,
                published_at=post.published_at,
                detail_url=post.detail_url,
                cover_url=post.cover_url,
                mega_url=post.mega_url,
            )
            for post in posts
        }

    def _apply_remote_post_updates(self, post: Post, scraped_post) -> bool:
        changed = (
            post.title != scraped_post.title
            or post.published_at != scraped_post.published_at
            or post.detail_url != scraped_post.detail_url
            or post.cover_url != scraped_post.cover_url
            or post.mega_url != scraped_post.mega_url
            or post.last_error != scraped_post.last_error
        )
        if not changed:
            return False
        post.title = scraped_post.title
        post.published_at = scraped_post.published_at
        post.detail_url = scraped_post.detail_url
        post.cover_url = scraped_post.cover_url
        post.mega_url = scraped_post.mega_url
        post.last_error = scraped_post.last_error
        if post.status != PostStatus.COMPLETED.value:
            post.status = scraped_post.status
        return True

    def _apply_annotation_result(self, post: Post, result: TitleAnnotationResult) -> None:
        post.title_annotation = result.annotation
        post.title_annotation_source = result.source
        post.title_annotation_status = result.status
        post.title_annotation_error = result.error
        post.title_annotation_updated_at = datetime.now(timezone.utc)

    def _validate_download_command_for_posts(self, session: Session, settings: Settings, posts: list[Post]) -> None:
        command_checked = False

        for post in posts:
            if not post.mega_url:
                continue

            paths = build_post_storage_paths(
                library_root=settings.library_dir,
                download_root=settings.download_dir,
                title=post.title,
                published_at=post.published_at,
                fallback_name=post.post_id,
            )
            artifact = session.query(Artifact).filter_by(post_id=post.id).one_or_none()
            existing_archive_path = reconcile_archive_path(
                current_archive_path=artifact.archive_path if artifact else None,
                library_root=settings.library_dir,
                download_root=settings.download_dir,
                title=post.title,
                published_at=post.published_at,
                fallback_name=post.post_id,
            )
            if existing_archive_path:
                continue

            if not command_checked:
                resolve_mega_command(settings.mega_command)
                command_checked = True

    def _find_active_refresh_task(self, session: Session) -> Task | None:
        return (
            session.query(Task)
            .filter(
                Task.kind == TaskKind.REFRESH_POSTS.value,
                Task.status.in_([TaskStatus.QUEUED.value, TaskStatus.RUNNING_REFRESH.value]),
            )
            .order_by(Task.created_at.desc())
            .first()
        )

    def _find_active_annotation_task(self, session: Session) -> Task | None:
        return (
            session.query(Task)
            .filter(
                Task.kind == TaskKind.ANNOTATE_TITLES.value,
                Task.status.in_([TaskStatus.QUEUED.value, TaskStatus.RUNNING_ANNOTATE.value]),
            )
            .order_by(Task.created_at.desc())
            .first()
        )

    def _find_active_auth_profile_task(self, session: Session) -> Task | None:
        return (
            session.query(Task)
            .filter(
                Task.kind.in_([TaskKind.OPEN_LOGIN.value, TaskKind.REFRESH_POSTS.value]),
                Task.status.in_(
                    [
                        TaskStatus.QUEUED.value,
                        TaskStatus.RUNNING_LOGIN.value,
                        TaskStatus.RUNNING_REFRESH.value,
                    ]
                ),
            )
            .order_by(Task.created_at.desc())
            .first()
        )

    def _find_active_rescan_task(self, session: Session) -> Task | None:
        return (
            session.query(Task)
            .filter(
                Task.kind == TaskKind.RESCAN_LIBRARY.value,
                Task.status.in_([TaskStatus.QUEUED.value, TaskStatus.RUNNING_REFRESH.value]),
            )
            .order_by(Task.created_at.desc())
            .first()
        )

    def _update_refresh_progress(self, task_id: str, mode: RefreshMode, current: int, total: int | None) -> None:
        safe_total = max(current, total) if total is not None else None
        display_total = safe_total if mode == RefreshMode.FULL else None
        self._update_task_status(
            task_id,
            TaskStatus.RUNNING_REFRESH.value,
            _refresh_progress_message(mode, current, display_total),
            progress_current=current,
            progress_total=display_total,
        )

    def _mark_started(
        self,
        task_id: str,
        status: str,
        message: str,
        progress_current: int | None = None,
        progress_total: int | None = None,
    ) -> None:
        with SessionLocal() as session:
            task = session.get(Task, task_id)
            if task is None:
                return
            task.status = status
            task.message = message
            task.started_at = datetime.now(timezone.utc)
            task.progress_current = progress_current
            task.progress_total = progress_total
            session.commit()

    def _update_task_status(
        self,
        task_id: str,
        status: str,
        message: str,
        progress_current: int | None = None,
        progress_total: int | None = None,
    ) -> None:
        with SessionLocal() as session:
            task = session.get(Task, task_id)
            if task is None:
                return
            task.status = status
            task.message = message
            if progress_current is not None or task.progress_current is None:
                task.progress_current = progress_current
            if progress_total is not None or task.progress_total is None:
                task.progress_total = progress_total
            session.commit()

    def _mark_completed(
        self,
        task_id: str,
        message: str,
        progress_current: int | None = None,
        progress_total: int | None = None,
    ) -> None:
        with SessionLocal() as session:
            task = session.get(Task, task_id)
            if task is None:
                return
            task.status = TaskStatus.COMPLETED.value
            task.message = message
            if progress_current is not None or task.progress_current is None:
                task.progress_current = progress_current
            if progress_total is not None or task.progress_total is None:
                task.progress_total = progress_total
            task.finished_at = datetime.now(timezone.utc)
            session.commit()

    def _mark_failed(
        self,
        task_id: str,
        status: str,
        error: str,
        *,
        failure_kind: str | None = None,
        return_code: int | None = None,
    ) -> None:
        with SessionLocal() as session:
            task = session.get(Task, task_id)
            if task is None:
                return
            task.status = status
            task.error = error
            task.download_failure_kind = failure_kind
            task.download_return_code = return_code
            task.finished_at = datetime.now(timezone.utc)
            session.commit()

    def _append_log(self, task_id: str, log_line: str) -> None:
        with SessionLocal() as session:
            task = session.get(Task, task_id)
            if task is None:
                return
            task.log = "\n".join(filter(None, [task.log, log_line]))
            session.commit()

    def _mark_post_operation_status(self, post_db_id: int, status: str, error: str | None = None) -> None:
        with SessionLocal() as session:
            post = session.get(Post, post_db_id)
            if post is None:
                return
            post.operation_status = status
            if error:
                post.last_error = error
            session.commit()

    def _mark_post_download_failed(
        self,
        post_db_id: int,
        operation_status: str,
        error: str,
        *,
        failure_kind: str | None = None,
        return_code: int | None = None,
    ) -> None:
        with SessionLocal() as session:
            post = session.get(Post, post_db_id)
            if post is None:
                return
            post.status = _fallback_post_inventory_status(post)
            post.operation_status = operation_status
            post.last_error = error
            post.download_failure_kind = failure_kind
            post.download_return_code = return_code
            session.commit()

    async def _download_archive_with_retries(
        self,
        mega_url: str,
        download_dir: str,
        mega_command: str,
        *,
        expected_archive_prefixes: tuple[str, ...] = (),
    ) -> tuple[str, str]:
        attempts = 3
        logs: list[str] = []
        last_error: MegaDownloadError | None = None

        for attempt in range(1, attempts + 1):
            try:
                archive_path, mega_log = await download_public_link(
                    mega_url,
                    download_dir,
                    mega_command,
                    expected_prefixes=expected_archive_prefixes,
                )
                if logs:
                    mega_log = "\n".join([*logs, mega_log])
                return archive_path, mega_log
            except MegaDownloadError as exc:
                last_error = exc
                message = exc.output
                logs.append(message)
                failure_kind = classify_mega_download_failure(exc.return_code, exc.output)
                if attempt >= attempts or failure_kind != DownloadFailureKind.RETRYABLE:
                    break
                logs.append(f"Retrying MEGA download ({attempt + 1}/{attempts}) after temporary server access failure.")
                await asyncio.sleep(3)

        assert last_error is not None
        raise MegaDownloadError(
            "\n".join(logs) if logs else str(last_error),
            return_code=last_error.return_code,
            output="\n".join(logs) if logs else last_error.output,
        )


def _resolve_download_concurrency() -> int:
    raw_value = os.getenv("FANBOX_DOWNLOAD_CONCURRENCY", "5").strip()
    try:
        concurrency = int(raw_value)
    except ValueError:
        return 5
    return max(1, concurrency)


def _fallback_post_inventory_status(post: Post) -> str:
    return PostStatus.MISSING_LOCAL.value if post.mega_url else PostStatus.FAILED_PARSE.value


def _resolve_download_timeout_seconds() -> int:
    raw_value = os.getenv("FANBOX_DOWNLOAD_TIMEOUT_SECONDS", "600").strip()
    try:
        timeout = int(raw_value)
    except ValueError:
        return 600
    return max(30, timeout)


def _resolve_refresh_timeout_seconds() -> int:
    raw_value = os.getenv("FANBOX_REFRESH_TIMEOUT_SECONDS", "90").strip()
    try:
        timeout = int(raw_value)
    except ValueError:
        return 90
    return max(15, timeout)


def _resolve_annotation_timeout_seconds() -> int:
    raw_value = os.getenv("FANBOX_ANNOTATION_TIMEOUT_SECONDS", "30").strip()
    try:
        timeout = int(raw_value)
    except ValueError:
        return 30
    return max(5, timeout)


def _resolve_annotation_batch_size() -> int:
    raw_value = os.getenv("FANBOX_ANNOTATION_BATCH_SIZE", "20").strip()
    try:
        batch_size = int(raw_value)
    except ValueError:
        return 20
    return max(1, batch_size)


def _resolve_annotation_concurrency() -> int:
    raw_value = os.getenv("FANBOX_ANNOTATION_CONCURRENCY", "5").strip()
    try:
        concurrency = int(raw_value)
    except ValueError:
        return 5
    return max(1, concurrency)


def _interrupted_task_outcome(kind: str) -> tuple[str, str]:
    message = "Task was interrupted because the app stopped before it finished."
    if kind == TaskKind.OPEN_LOGIN.value:
        return TaskStatus.FAILED_AUTH.value, message
    if kind == TaskKind.DOWNLOAD_POST.value:
        return TaskStatus.FAILED_DOWNLOAD.value, message
    if kind == TaskKind.ANNOTATE_TITLES.value:
        return TaskStatus.FAILED_ANNOTATE.value, message
    return TaskStatus.FAILED_PARSE.value, message


def _refresh_progress_message(mode: RefreshMode, current: int, total: int | None) -> str:
    if mode == RefreshMode.INCREMENTAL:
        return f"Incremental refresh checked {current} pages."
    if total is None:
        return f"Full refresh processed {current} pages."
    return f"Full refresh processed {current}/{total} pages."


def _refresh_completed_message(mode: RefreshMode, post_count: int) -> str:
    if mode == RefreshMode.INCREMENTAL:
        return f"Incremental refresh completed, checked latest pages and synced {post_count} posts."
    return f"Full refresh completed, synced {post_count} posts."
