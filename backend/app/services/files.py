from __future__ import annotations

import json
import os
import re
import time
import ctypes
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zipfile import BadZipFile, ZipFile


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
KNOWN_BAD_SUBSTRINGS = ("銆?", "銆")
INVALID_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*]')
EXPLORER_WINDOW_CLASSES = {"CabinetWClass", "ExploreWClass"}


@dataclass(frozen=True)
class PostStoragePaths:
    folder_name: str
    year: str
    download_dir: str
    extract_dir: str


def ensure_directories(*paths: str) -> None:
    for raw_path in paths:
        Path(raw_path).mkdir(parents=True, exist_ok=True)


def build_post_storage_paths(
    library_root: str,
    download_root: str,
    title: str,
    published_at: datetime | None,
    fallback_name: str,
) -> PostStoragePaths:
    year = str((published_at or datetime.now()).year)
    folder_name = sanitize_path_segment(title) or sanitize_path_segment(fallback_name) or "untitled"
    year_root = Path(library_root) / year
    archive_root = Path(download_root) / year
    return PostStoragePaths(
        folder_name=folder_name,
        year=year,
        download_dir=str(archive_root),
        extract_dir=str(year_root / folder_name),
    )


def sanitize_path_segment(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).strip()
    result = INVALID_WINDOWS_CHARS.sub("_", normalized)
    return result.rstrip(" .")


def extract_zip(archive_path: str, target_dir: str) -> list[str]:
    archive = Path(archive_path)
    output_dir = Path(target_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        with ZipFile(archive) as zip_file:
            zip_file.extractall(output_dir)
            return zip_file.namelist()
    except BadZipFile as exc:
        raise ValueError(f"Invalid zip archive: {archive}") from exc


def rename_images(root_dir: str) -> list[dict[str, str]]:
    root = Path(root_dir)
    changes: list[dict[str, str]] = []
    directories = [path for path in root.rglob("*") if path.is_dir()]
    directories.append(root)
    for directory in sorted(directories, key=lambda path: len(path.parts)):
        changes.extend(_rename_in_directory(directory))
    return changes


def serialize_processed_files(changes: list[dict[str, str]]) -> str:
    return json.dumps(changes, ensure_ascii=False, indent=2)


def directory_has_content(path: str) -> bool:
    directory = Path(path)
    return directory.exists() and any(directory.rglob("*"))


def archive_dir_for_year(download_root: str, year: str) -> Path:
    return Path(download_root) / year


def reconcile_archive_path(
    current_archive_path: str | None,
    library_root: str,
    download_root: str,
    title: str,
    published_at: datetime | None,
    fallback_name: str,
) -> str | None:
    paths = build_post_storage_paths(
        library_root=library_root,
        download_root=download_root,
        title=title,
        published_at=published_at,
        fallback_name=fallback_name,
    )
    archive_root = Path(paths.download_dir)
    archive_root.mkdir(parents=True, exist_ok=True)
    fallback_segment = sanitize_path_segment(fallback_name)

    if current_archive_path:
        current = Path(current_archive_path)
        if current.exists() and current.is_file():
            try:
                if current.resolve().parent == archive_root.resolve():
                    return str(current.resolve())
            except OSError:
                pass

    candidates = sorted(_matching_archive_candidates(archive_root, paths.folder_name, fallback_segment))
    if candidates:
        return str(candidates[0])
    return None


def reconcile_extract_dir(
    current_extract_dir: str | None,
    library_root: str,
    title: str,
    published_at: datetime | None,
    fallback_name: str,
) -> str:
    paths = build_post_storage_paths(
        library_root=library_root,
        download_root="",
        title=title,
        published_at=published_at,
        fallback_name=fallback_name,
    )
    resolved_root = Path(library_root) / paths.year
    resolved_root.mkdir(parents=True, exist_ok=True)

    if current_extract_dir:
        current = Path(current_extract_dir)
        if current.exists() and current.is_dir():
            return str(current.resolve())

    matched = _matching_extract_dir(resolved_root, paths.folder_name, sanitize_path_segment(fallback_name))
    if matched is not None:
        return str(matched.resolve())
    return str(Path(paths.extract_dir))


def delete_archive_file(path: str | None) -> bool:
    if not path:
        return False
    archive = Path(path)
    if not archive.exists() or not archive.is_file():
        return False
    archive.unlink()
    return True


def purge_archive_files(download_root: str, year: str | None = None) -> list[str]:
    deleted: list[str] = []
    roots: list[Path]
    if year:
        roots = [archive_dir_for_year(download_root, year)]
    else:
        base = Path(download_root)
        if not base.exists():
            return deleted
        roots = [path for path in base.iterdir() if path.is_dir()]

    for archive_root in roots:
        if not archive_root.exists():
            continue
        for file_path in archive_root.iterdir():
            if file_path.is_file():
                file_path.unlink()
                deleted.append(str(file_path))
    return deleted


def resolve_post_open_path(extract_dir: str | None, archive_path: str | None) -> str | None:
    if extract_dir:
        extract_path = Path(extract_dir)
        if extract_path.exists():
            return str(extract_path)

    if archive_path:
        archive = Path(archive_path)
        if archive.exists():
            return str(archive.parent)

    return None


def open_in_explorer(path: str) -> None:
    target = Path(path).resolve()
    if not target.exists():
        raise FileNotFoundError(f"Path does not exist: {target}")

    if hasattr(os, "startfile"):
        os.startfile(str(target))
        _activate_explorer_window(target)
        return

    raise RuntimeError("Opening local paths is only supported on Windows")


def _activate_explorer_window(target: Path) -> None:
    hwnd = _wait_for_explorer_window(target.name or str(target))
    if hwnd is None:
        return
    _bring_window_to_front(hwnd)


def _wait_for_explorer_window(window_title: str, timeout_seconds: float = 2.5) -> int | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        hwnd = _find_explorer_window(window_title)
        if hwnd is not None:
            return hwnd
        time.sleep(0.1)
    return None


def _find_explorer_window(window_title: str) -> int | None:
    if not hasattr(ctypes, "windll"):
        return None

    user32 = ctypes.windll.user32
    title_match = window_title.casefold()
    matches: list[int] = []
    enum_windows_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _callback(hwnd: int, _: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True

        class_buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buffer, 256)
        if class_buffer.value not in EXPLORER_WINDOW_CLASSES:
            return True

        title_length = user32.GetWindowTextLengthW(hwnd)
        if title_length <= 0:
            return True

        title_buffer = ctypes.create_unicode_buffer(title_length + 1)
        user32.GetWindowTextW(hwnd, title_buffer, title_length + 1)
        if title_match in title_buffer.value.casefold():
            matches.append(int(hwnd))
            return False
        return True

    callback = enum_windows_proc(_callback)
    user32.EnumWindows(callback, 0)
    return matches[0] if matches else None


def _bring_window_to_front(hwnd: int) -> None:
    if not hasattr(ctypes, "windll"):
        return

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    sw_restore = 9
    swp_nosize = 0x0001
    swp_nomove = 0x0002
    hwnd_topmost = -1
    hwnd_notopmost = -2

    foreground = user32.GetForegroundWindow()
    current_thread = kernel32.GetCurrentThreadId()
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0

    if foreground_thread:
        user32.AttachThreadInput(foreground_thread, current_thread, True)
    if target_thread:
        user32.AttachThreadInput(target_thread, current_thread, True)

    try:
        user32.ShowWindow(hwnd, sw_restore)
        user32.BringWindowToTop(hwnd)
        user32.SetWindowPos(hwnd, hwnd_topmost, 0, 0, 0, 0, swp_nomove | swp_nosize)
        user32.SetWindowPos(hwnd, hwnd_notopmost, 0, 0, 0, 0, swp_nomove | swp_nosize)
        user32.SetForegroundWindow(hwnd)
    finally:
        if target_thread:
            user32.AttachThreadInput(target_thread, current_thread, False)
        if foreground_thread:
            user32.AttachThreadInput(foreground_thread, current_thread, False)


def _rename_in_directory(directory: Path) -> list[dict[str, str]]:
    files = [path for path in directory.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS]
    if not files:
        return []

    changes: list[dict[str, str]] = []
    existing_names = {path.name for path in files}

    for file_path in sorted(files):
        new_name = _normalized_name(file_path.name, existing_names)
        if new_name == file_path.name:
            continue

        unique_name = _ensure_unique_name(directory, new_name, file_path.name)
        destination = directory / unique_name
        file_path.rename(destination)
        existing_names.discard(file_path.name)
        existing_names.add(unique_name)
        changes.append({"from": str(file_path), "to": str(destination)})

    return changes


def _normalized_name(filename: str, siblings: set[str]) -> str:
    stem, suffix = filename.rsplit(".", 1)
    suffix = f".{suffix}"
    stem = _sanitize_stem(stem)

    duplicate_match = re.search(r"^(.*?)(\d{2})[._。．-](\d+)$", stem)
    if duplicate_match:
        prefix, page_no, copy_no = duplicate_match.groups()
        return f"{prefix}{page_no}_{copy_no}{suffix}"

    match = re.search(r"\d+", stem)
    if match:
        digits = match.group(0)
        if len(digits) == 1:
            stem = f"{stem[:match.start()]}0{digits}{stem[match.end():]}"
        return f"{stem}{suffix}"

    candidate_01 = f"{stem}01{suffix}"
    candidate_1 = f"{stem}1{suffix}"
    if candidate_01 not in siblings and candidate_1 not in siblings:
        return candidate_01
    return f"{stem}00{suffix}"


def _sanitize_stem(stem: str) -> str:
    result = stem
    for token in KNOWN_BAD_SUBSTRINGS:
        result = result.replace(token, "_")
    result = result.replace("。", "_").replace("．", "_")
    return result


def _ensure_unique_name(directory: Path, candidate: str, original: str) -> str:
    if candidate == original or not (directory / candidate).exists():
        return candidate

    base = Path(candidate).stem
    suffix = Path(candidate).suffix
    index = 1
    while True:
        deduped = f"{base}_{index}{suffix}"
        if deduped == original or not (directory / deduped).exists():
            return deduped
        index += 1


def _matching_archive_candidates(archive_root: Path, folder_name: str, fallback_name: str) -> list[Path]:
    prefixes = tuple(filter(None, {folder_name, fallback_name}))
    if not prefixes:
        return []

    normalized_prefixes = tuple(_normalize_for_matching(prefix) for prefix in prefixes)
    candidates: list[Path] = []
    for path in archive_root.iterdir():
        if not path.is_file() or path.suffix.lower() != ".zip":
            continue
        normalized_stem = _normalize_for_matching(path.stem)
        if any(normalized_stem.startswith(prefix) for prefix in normalized_prefixes):
            candidates.append(path)
    return candidates


def _matching_extract_dir(year_root: Path, folder_name: str, fallback_name: str) -> Path | None:
    normalized_targets = {_normalize_for_matching(value) for value in (folder_name, fallback_name) if value}
    if not normalized_targets:
        return None

    for path in sorted(year_root.iterdir()):
        if not path.is_dir():
            continue
        if _normalize_for_matching(path.name) in normalized_targets:
            return path
    return None


def _normalize_for_matching(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()
