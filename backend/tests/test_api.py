from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient
import app.main as main_module

TEST_DB = Path(__file__).resolve().parents[1] / "data" / "test.db"

from app.db.models import Artifact, Post, PostStatus, RefreshMode, Task, TaskKind, TaskStatus
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
    }

    save_response = client.post("/api/settings", json=payload)
    read_response = client.get("/api/settings")

    assert save_response.status_code == 200
    assert read_response.status_code == 200
    assert read_response.json()["data"]["creator_url"] == payload["creator_url"]
    assert read_response.json()["data"]["download_concurrency"] == 4
    assert read_response.json()["data"]["posts_per_row"] == 5
    assert read_response.json()["data"]["auto_delete_archive"] is True


def test_settings_defaults_enable_auto_delete_and_concurrency() -> None:
    client = TestClient(app)

    response = client.get("/api/settings")

    assert response.status_code == 200
    assert response.json()["data"]["download_concurrency"] == 5
    assert response.json()["data"]["posts_per_row"] == 4
    assert response.json()["data"]["auto_delete_archive"] is True


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
