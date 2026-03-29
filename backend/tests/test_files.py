from datetime import datetime
from pathlib import Path

from app.services.files import (
    build_post_storage_paths,
    delete_archive_file,
    open_in_explorer,
    purge_archive_files,
    reconcile_archive_path,
    rename_images,
)


def test_rename_images_normalizes_numbers_and_missing_indices(tmp_path: Path) -> None:
    target = tmp_path / "sample"
    target.mkdir()
    (target / "image1.jpg").write_bytes(b"1")
    (target / "bonus.png").write_bytes(b"2")

    changes = rename_images(str(target))

    assert (target / "image01.jpg").exists()
    assert (target / "bonus01.png").exists()
    assert len(changes) == 2


def test_rename_images_uses_00_when_01_exists(tmp_path: Path) -> None:
    target = tmp_path / "sample"
    target.mkdir()
    (target / "cover01.jpg").write_bytes(b"1")
    (target / "cover.jpg").write_bytes(b"2")

    rename_images(str(target))

    assert (target / "cover01.jpg").exists()
    assert (target / "cover00.jpg").exists()


def test_rename_images_normalizes_duplicate_suffix_style(tmp_path: Path) -> None:
    target = tmp_path / "sample"
    target.mkdir()
    (target / "title04。1.jpg").write_bytes(b"1")

    rename_images(str(target))

    assert (target / "title04_1.jpg").exists()


def test_build_post_storage_paths_uses_year_title_and_archive_dir() -> None:
    paths = build_post_storage_paths(
        library_root=r"D:\hmoe\Siu",
        download_root=r"C:\temp\downloads",
        title="信濃③C",
        published_at=datetime(2026, 3, 22, 8, 0),
        fallback_name="11560240",
    )

    assert paths.year == "2026"
    assert paths.folder_name == "信濃③C"
    assert paths.extract_dir == r"D:\hmoe\Siu\2026\信濃③C"
    assert paths.download_dir == r"D:\hmoe\Siu\2026\Archive"


def test_delete_archive_file_removes_single_file(tmp_path: Path) -> None:
    archive = tmp_path / "2026" / "Archive" / "sample.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"zip")

    deleted = delete_archive_file(str(archive))

    assert deleted is True
    assert not archive.exists()


def test_reconcile_archive_path_moves_legacy_archive_into_year_archive(tmp_path: Path) -> None:
    legacy_archive = tmp_path / "legacy" / "信濃③C.zip"
    legacy_archive.parent.mkdir(parents=True)
    legacy_archive.write_bytes(b"zip")

    reconciled = reconcile_archive_path(
        current_archive_path=str(legacy_archive),
        library_root=str(tmp_path),
        title="信濃③",
        published_at=datetime(2026, 3, 22, 8, 0),
        fallback_name="11560240",
    )

    expected = tmp_path / "2026" / "Archive" / "信濃③C.zip"
    assert reconciled == str(expected)
    assert expected.exists()
    assert not legacy_archive.exists()


def test_purge_archive_files_removes_year_archive_contents(tmp_path: Path) -> None:
    archive_dir = tmp_path / "2026" / "Archive"
    archive_dir.mkdir(parents=True)
    first = archive_dir / "a.zip"
    second = archive_dir / "b.zip"
    first.write_bytes(b"1")
    second.write_bytes(b"2")

    deleted = purge_archive_files(str(tmp_path), "2026")

    assert sorted(Path(path).name for path in deleted) == ["a.zip", "b.zip"]
    assert not first.exists()
    assert not second.exists()


def test_open_in_explorer_uses_startfile(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "folder"
    target.mkdir()
    opened: list[str] = []
    activated: list[Path] = []

    monkeypatch.setattr("app.services.files.os.startfile", lambda path: opened.append(path))
    monkeypatch.setattr("app.services.files._activate_explorer_window", lambda path: activated.append(path))

    open_in_explorer(str(target))

    assert opened == [str(target.resolve())]
    assert activated == [target.resolve()]
