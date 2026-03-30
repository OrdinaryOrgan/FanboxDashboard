from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient
import app.main as main_module

TEST_DB = Path(__file__).resolve().parents[1] / "data" / "test.db"

from app.db.models import Artifact, Post, PostStatus, RefreshMode, Settings, Task, TaskKind, TaskStatus
from app.db.session import engine, init_db
from app.db.session import SessionLocal
from app.main import app


def setup_function() -> None:
    engine.dispose()
    if TEST_DB.exists():
        TEST_DB.unlink()
    init_db()


def test_healthcheck() -> None:
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_frontend_index_served() -> None:
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Fanbox Dashboard" in response.text
    if main_module.FRONTEND_DIST.exists():
        assert "<div id=\"app\"></div>" in response.text
    else:
        assert "frontend build was not found" in response.text


def test_settings_roundtrip() -> None:
    client = TestClient(app)
    payload = {
        "creator_url": "https://example.fanbox.cc/posts",
        "profile_dir": "C:/tmp/profile",
        "download_dir": "C:/tmp/downloads",
        "library_dir": "C:/tmp/library",
        "temp_dir": "C:/tmp/temp",
        "mega_command": "mega-get",
        "playwright_channel": "msedge",
        "refresh_interval_minutes": 0,
        "download_concurrency": 4,
        "posts_per_row": 5,
        "auto_delete_archive": True,
        "llm_enabled": True,
        "llm_api_key": "local-test-key",
        "llm_base_url": "https://api.deepseek.com",
        "llm_model": "deepseek-chat",
        "title_aliases_path": "C:/tmp/title_aliases.json",
    }

    save_response = client.post("/api/settings", json=payload)
    read_response = client.get("/api/settings")

    assert save_response.status_code == 200
    assert read_response.status_code == 200
    assert read_response.json()["data"]["creator_url"] == payload["creator_url"]
    assert read_response.json()["data"]["download_concurrency"] == 4
    assert read_response.json()["data"]["posts_per_row"] == 5
    assert read_response.json()["data"]["auto_delete_archive"] is True
    assert read_response.json()["data"]["llm_enabled"] is True
    assert read_response.json()["data"]["llm_api_key"] == "local-test-key"
    assert read_response.json()["data"]["llm_base_url"] == "https://api.deepseek.com"
    assert read_response.json()["data"]["llm_model"] == "deepseek-chat"
    assert read_response.json()["data"]["title_aliases_path"] == "C:/tmp/title_aliases.json"


def test_settings_defaults_enable_auto_delete_and_concurrency() -> None:
    client = TestClient(app)

    response = client.get("/api/settings")

    assert response.status_code == 200
    assert response.json()["data"]["download_concurrency"] == 5
    assert response.json()["data"]["posts_per_row"] == 4
    assert response.json()["data"]["auto_delete_archive"] is True
    assert response.json()["data"]["llm_enabled"] is False
    assert response.json()["data"]["llm_api_key"] == ""
    assert response.json()["data"]["llm_base_url"] == "https://api.deepseek.com"
    assert response.json()["data"]["llm_model"] == "deepseek-chat"
    assert Path(response.json()["data"]["title_aliases_path"]).name == "title_aliases.json"


def test_open_title_aliases_path_opens_parent_folder(monkeypatch, tmp_path: Path) -> None:
    alias_file = tmp_path / "aliases" / "title_aliases.json"

    with SessionLocal() as session:
        settings = session.get(Settings, 1)
        assert settings is not None
        settings.title_aliases_path = str(alias_file)
        session.commit()

    opened_paths: list[str] = []
    monkeypatch.setattr("app.api.routes.open_in_explorer", lambda path: opened_paths.append(path))

    client = TestClient(app)
    response = client.post("/api/title-aliases/open")

    assert response.status_code == 200
    assert alias_file.exists()
    assert opened_paths == [str(alias_file.parent)]
    assert response.json()["data"]["opened_path"] == str(alias_file.parent)


def test_clear_title_aliases_rewrites_empty_payload(tmp_path: Path) -> None:
    alias_file = tmp_path / "aliases" / "title_aliases.json"
    alias_file.parent.mkdir(parents=True, exist_ok=True)
    alias_file.write_text(
        '{"version":1,"full_titles":{"A":"B"},"phrase_fragments":{"C":"D"},"fragments":{"E":"F"}}',
        encoding="utf-8",
    )

    with SessionLocal() as session:
        settings = session.get(Settings, 1)
        assert settings is not None
        settings.title_aliases_path = str(alias_file)
        session.commit()

    client = TestClient(app)
    response = client.post("/api/title-aliases/clear")

    assert response.status_code == 200
    assert response.json()["data"]["cleared_path"] == str(alias_file)
    assert alias_file.read_text(encoding="utf-8") == '{\n  "version": 1,\n  "full_titles": {},\n  "phrase_fragments": {},\n  "fragments": {}\n}'


def test_clear_title_annotation_cache_resets_posts_to_pending_or_skipped() -> None:
    with SessionLocal() as session:
        session.add_all(
            [
                Post(
                    post_id="31000001",
                    title="ベルファスト①",
                    title_annotation="贝尔法斯特①",
                    title_annotation_source="llm",
                    title_annotation_status="completed",
                    detail_url="https://siu.fanbox.cc/posts/31000001",
                    status=PostStatus.COMPLETED.value,
                ),
                Post(
                    post_id="31000002",
                    title="武蔵①",
                    title_annotation=None,
                    title_annotation_source="none",
                    title_annotation_status="skipped",
                    detail_url="https://siu.fanbox.cc/posts/31000002",
                    status=PostStatus.COMPLETED.value,
                ),
            ]
        )
        session.commit()

    client = TestClient(app)
    response = client.post("/api/title-annotations/clear-cache")

    assert response.status_code == 200
    assert response.json()["data"]["reset_count"] == 1

    with SessionLocal() as session:
        annotated = session.query(Post).filter_by(post_id="31000001").one()
        skipped = session.query(Post).filter_by(post_id="31000002").one()
        assert annotated.title_annotation is None
        assert annotated.title_annotation_status == "pending"
        assert skipped.title_annotation_status == "skipped"


def test_posts_expose_needs_title_annotation_flag() -> None:
    with SessionLocal() as session:
        session.add_all(
            [
                Post(
                    post_id="41000010",
                    title="ベルファスト①",
                    detail_url="https://siu.fanbox.cc/posts/41000010",
                    status=PostStatus.MISSING_LOCAL.value,
                ),
                Post(
                    post_id="41000011",
                    title="武蔵①",
                    detail_url="https://siu.fanbox.cc/posts/41000011",
                    status=PostStatus.MISSING_LOCAL.value,
                ),
            ]
        )
        session.commit()

    client = TestClient(app)
    response = client.get("/api/posts")

    assert response.status_code == 200
    posts = {item["post_id"]: item for item in response.json()["data"]}
    assert posts["41000010"]["needs_title_annotation"] is True
    assert posts["41000011"]["needs_title_annotation"] is False


def test_upsert_title_aliases_writes_full_title_entry(tmp_path: Path) -> None:
    alias_file = tmp_path / "aliases" / "title_aliases.json"

    with SessionLocal() as session:
        settings = session.get(Settings, 1)
        assert settings is not None
        settings.title_aliases_path = str(alias_file)
        session.add(
            Post(
                post_id="41000001",
                title="ベルファスト①",
                detail_url="https://siu.fanbox.cc/posts/41000001",
                status=PostStatus.MISSING_LOCAL.value,
            )
        )
        session.commit()

    client = TestClient(app)
    response = client.post(
        "/api/title-aliases/upsert",
        json={
            "post_id": "41000001",
            "mode": "full_title",
            "full_title_translation": "贝尔法斯特①",
            "entries": [],
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["updated_count"] == 1
    assert "贝尔法斯特①" in alias_file.read_text(encoding="utf-8")


def test_upsert_title_aliases_writes_phrase_entries(tmp_path: Path) -> None:
    alias_file = tmp_path / "aliases" / "title_aliases.json"

    with SessionLocal() as session:
        settings = session.get(Settings, 1)
        assert settings is not None
        settings.title_aliases_path = str(alias_file)
        session.add(
            Post(
                post_id="41000002",
                title="一之瀬アスナx調月リオ①",
                detail_url="https://siu.fanbox.cc/posts/41000002",
                status=PostStatus.MISSING_LOCAL.value,
            )
        )
        session.commit()

    client = TestClient(app)
    response = client.post(
        "/api/title-aliases/upsert",
        json={
            "post_id": "41000002",
            "mode": "phrase_fragment",
            "entries": [
                {"source": "一之瀬アスナ", "target": "一之瀬明日奈"},
                {"source": "調月リオ", "target": "調月莉央"},
            ],
        },
    )

    assert response.status_code == 200
    contents = alias_file.read_text(encoding="utf-8")
    assert "一之瀬アスナ" in contents
    assert "調月リオ" in contents


def test_posts_deduped_by_mega_url_keep_smaller_post_id() -> None:
    with SessionLocal() as session:
        session.add_all(
            [
                Post(
                    post_id="20000091",
                    title="duplicate larger",
                    published_at=datetime(2026, 3, 17, 8, 0),
                    detail_url="https://siu.fanbox.cc/posts/20000091",
                    mega_url="https://mega.nz/file/shared",
                    status=PostStatus.MISSING_LOCAL.value,
                ),
                Post(
                    post_id="20000080",
                    title="duplicate smaller",
                    published_at=datetime(2026, 3, 17, 8, 0),
                    detail_url="https://siu.fanbox.cc/posts/20000080",
                    mega_url="https://mega.nz/file/shared",
                    status=PostStatus.MISSING_LOCAL.value,
                ),
                Post(
                    post_id="20000100",
                    title="unique",
                    published_at=datetime(2026, 3, 18, 8, 0),
                    detail_url="https://siu.fanbox.cc/posts/20000100",
                    cover_url="https://example.com/cover.jpg",
                    mega_url="https://mega.nz/file/unique",
                    status=PostStatus.MISSING_LOCAL.value,
                ),
            ]
        )
        session.commit()

    client = TestClient(app)
    response = client.get("/api/posts")

    assert response.status_code == 200
    returned_post_ids = [item["post_id"] for item in response.json()["data"]]
    assert "20000080" in returned_post_ids
    assert "20000091" not in returned_post_ids
    assert "20000100" in returned_post_ids
    unique_post = next(item for item in response.json()["data"] if item["post_id"] == "20000100")
    assert unique_post["cover_url"] == "https://example.com/cover.jpg"


def test_posts_include_display_title_when_annotation_exists() -> None:
    with SessionLocal() as session:
        session.add(
            Post(
                post_id="21000001",
                title="ベルファスト⑦",
                title_annotation="贝尔法斯特⑦",
                published_at=datetime(2026, 3, 17, 8, 0),
                detail_url="https://siu.fanbox.cc/posts/21000001",
                mega_url="https://mega.nz/file/annotated",
                status=PostStatus.MISSING_LOCAL.value,
            )
        )
        session.commit()

    client = TestClient(app)
    response = client.get("/api/posts")

    assert response.status_code == 200
    post = next(item for item in response.json()["data"] if item["post_id"] == "21000001")
    assert post["title"] == "ベルファスト⑦"
    assert post["title_annotation"] == "贝尔法斯特⑦"
    assert post["has_title_annotation"] is True
    assert post["display_title"] == "ベルファスト⑦（贝尔法斯特⑦）"


def test_posts_dictionary_annotation_overrides_stale_database_annotation(monkeypatch) -> None:
    with SessionLocal() as session:
        session.add(
            Post(
                post_id="21287418",
                title="ロザンナxレッドフード①",
                title_annotation="罗莎娜x红发①",
                published_at=datetime(2026, 2, 4, 7, 0),
                detail_url="https://siu.fanbox.cc/posts/21287418",
                mega_url="https://mega.nz/file/override",
                status=PostStatus.COMPLETED.value,
            )
        )
        session.commit()

    class FakeAliases:
        version = 1
        full_titles = {"ロザンナxレッドフード①": "罗珊娜×小红帽①"}
        fragments = {}

    monkeypatch.setattr("app.api.routes.load_title_aliases", lambda *args, **kwargs: FakeAliases())

    client = TestClient(app)
    response = client.get("/api/posts")

    assert response.status_code == 200
    post = next(item for item in response.json()["data"] if item["post_id"] == "21287418")
    assert post["title_annotation"] == "罗珊娜×小红帽①"
    assert post["display_title"] == "ロザンナxレッドフード①（罗珊娜×小红帽①）"


def test_posts_expose_can_open_local_path_based_on_existing_files(tmp_path: Path) -> None:
    existing_dir = tmp_path / "2026" / "existing"
    existing_dir.mkdir(parents=True)

    with SessionLocal() as session:
        openable = Post(
            post_id="11111111",
            title="openable",
            published_at=datetime(2026, 3, 17, 8, 0),
            detail_url="https://siu.fanbox.cc/posts/11111111",
            mega_url="https://mega.nz/file/openable",
            status=PostStatus.COMPLETED.value,
        )
        missing = Post(
            post_id="11143157",
            title="missing local",
            published_at=datetime(2025, 3, 17, 8, 0),
            detail_url="https://siu.fanbox.cc/posts/11143157",
            mega_url="https://mega.nz/file/missing",
            status=PostStatus.MISSING_LOCAL.value,
        )
        session.add_all([openable, missing])
        session.flush()
        session.add_all(
            [
                Artifact(
                    post_id=openable.id,
                    archive_path=None,
                    extract_dir=str(existing_dir),
                    processed_files="[]",
                ),
                Artifact(
                    post_id=missing.id,
                    archive_path=None,
                    extract_dir=str(tmp_path / "2025" / "missing-local"),
                    processed_files="[]",
                ),
            ]
        )
        session.commit()

    client = TestClient(app)
    response = client.get("/api/posts")

    assert response.status_code == 200
    posts = {item["post_id"]: item for item in response.json()["data"]}
    assert posts["11111111"]["can_open_local_path"] is True
    assert posts["11143157"]["can_open_local_path"] is False


def test_tasks_include_progress_fields() -> None:
    with SessionLocal() as session:
        session.add(
            Task(
                id="task-progress-1",
                kind=TaskKind.REFRESH_POSTS.value,
                status=TaskStatus.RUNNING_REFRESH.value,
                refresh_mode=RefreshMode.FULL.value,
                message="Refreshing Fanbox posts: 3/7 pages completed.",
                progress_current=3,
                progress_total=7,
            )
        )
        session.commit()

    client = TestClient(app)
    response = client.get("/api/tasks")

    assert response.status_code == 200
    task = response.json()["data"][0]
    assert task["id"] == "task-progress-1"
    assert task["refresh_mode"] == "full"
    assert task["progress_current"] == 3
    assert task["progress_total"] == 7


def test_clear_tasks_removes_finished_tasks() -> None:
    with SessionLocal() as session:
        session.add_all(
            [
                Task(
                    id="clear-task-1",
                    kind=TaskKind.REFRESH_POSTS.value,
                    status=TaskStatus.COMPLETED.value,
                ),
                Task(
                    id="clear-task-2",
                    kind=TaskKind.DOWNLOAD_POST.value,
                    status=TaskStatus.FAILED_DOWNLOAD.value,
                ),
            ]
        )
        session.commit()

    client = TestClient(app)
    response = client.post("/api/tasks/clear")

    assert response.status_code == 200
    assert response.json()["data"]["deleted_count"] == 2

    with SessionLocal() as session:
        assert session.query(Task).count() == 0


def test_clear_tasks_rejects_when_active_tasks_exist() -> None:
    with SessionLocal() as session:
        session.add(
            Task(
                id="active-task-1",
                kind=TaskKind.DOWNLOAD_POST.value,
                status=TaskStatus.RUNNING_DOWNLOAD.value,
            )
        )
        session.commit()

    client = TestClient(app)
    response = client.post("/api/tasks/clear")

    assert response.status_code == 409
    assert response.json()["detail"] == "Cannot clear task list while tasks are still running."

    with SessionLocal() as session:
        assert session.query(Task).count() == 1


def test_refresh_endpoint_defaults_to_incremental(monkeypatch) -> None:
    captured: list[str] = []

    def fake_enqueue_refresh(mode: RefreshMode) -> str:
        captured.append(mode.value)
        return "refresh-task-default"

    monkeypatch.setattr(main_module.task_manager, "enqueue_refresh", fake_enqueue_refresh)
    client = TestClient(app)

    response = client.post("/api/posts/refresh")

    assert response.status_code == 200
    assert captured == ["incremental"]
    assert response.json()["data"]["task_id"] == "refresh-task-default"


def test_refresh_endpoint_supports_full_mode(monkeypatch) -> None:
    captured: list[str] = []

    def fake_enqueue_refresh(mode: RefreshMode) -> str:
        captured.append(mode.value)
        return "refresh-task-full"

    monkeypatch.setattr(main_module.task_manager, "enqueue_refresh", fake_enqueue_refresh)
    client = TestClient(app)

    response = client.post("/api/posts/refresh", json={"mode": "full"})

    assert response.status_code == 200
    assert captured == ["full"]
    assert response.json()["data"]["task_id"] == "refresh-task-full"


def test_open_local_path_endpoint_opens_extract_dir(monkeypatch, tmp_path: Path) -> None:
    extract_dir = tmp_path / "2026" / "sample"
    extract_dir.mkdir(parents=True)

    with SessionLocal() as session:
        post = Post(
            post_id="30000001",
            title="sample",
            published_at=datetime(2026, 3, 27, 8, 0),
            detail_url="https://siu.fanbox.cc/posts/30000001",
            mega_url="https://mega.nz/file/sample",
            status=PostStatus.COMPLETED.value,
        )
        session.add(post)
        session.flush()
        session.add(
            Artifact(
                post_id=post.id,
                archive_path=None,
                extract_dir=str(extract_dir),
                processed_files="[]",
            )
        )
        session.commit()

    opened_paths: list[str] = []

    def fake_open_in_explorer(path: str) -> None:
        opened_paths.append(path)

    monkeypatch.setattr("app.api.routes.open_in_explorer", fake_open_in_explorer)
    client = TestClient(app)

    response = client.post("/api/posts/open-local-path", json={"post_id": "30000001"})

    assert response.status_code == 200
    assert response.json()["data"]["opened_path"] == str(extract_dir)
    assert opened_paths == [str(extract_dir)]


def test_open_year_folder_endpoint_opens_selected_year(monkeypatch, tmp_path: Path) -> None:
    year_dir = tmp_path / "2026"
    year_dir.mkdir(parents=True)

    client = TestClient(app)
    client.post(
        "/api/settings",
        json={
            "creator_url": "https://example.fanbox.cc/posts",
            "profile_dir": "C:/tmp/profile",
            "download_dir": "C:/tmp/downloads",
            "library_dir": str(tmp_path),
            "temp_dir": "C:/tmp/temp",
            "mega_command": "mega-get",
            "playwright_channel": "msedge",
            "refresh_interval_minutes": 0,
            "download_concurrency": 4,
            "posts_per_row": 4,
            "auto_delete_archive": True,
        },
    )

    opened_paths: list[str] = []

    def fake_open_in_explorer(path: str) -> None:
        opened_paths.append(path)

    monkeypatch.setattr("app.api.routes.open_in_explorer", fake_open_in_explorer)

    response = client.post("/api/library/open-year", json={"year": "2026"})

    assert response.status_code == 200
    assert response.json()["data"]["opened_path"] == str(year_dir)
    assert opened_paths == [str(year_dir)]
