from __future__ import annotations

import asyncio
from contextlib import suppress
import os
import shutil
import subprocess
from enum import StrEnum
from pathlib import Path
from zipfile import BadZipFile, ZipFile


class MegaDownloadError(RuntimeError):
    def __init__(self, message: str, *, return_code: int | None = None, output: str | None = None) -> None:
        super().__init__(message)
        self.return_code = return_code
        self.output = output or message


class MegaCommandConfigurationError(RuntimeError):
    pass


class MegaDownloadFailureKind(StrEnum):
    RETRYABLE = "retryable"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


DOWNLOAD_POLL_INTERVAL_SECONDS = 0.5
DOWNLOAD_STABLE_POLLS_REQUIRED = 3
DOWNLOAD_PROCESS_SHUTDOWN_TIMEOUT_SECONDS = 3.0
DOWNLOAD_OUTPUT_DRAIN_TIMEOUT_SECONDS = 1.0


async def download_public_link(link: str, download_dir: str, command_name: str) -> tuple[str, str]:
    command_parts, use_cmd_shell = resolve_mega_command(command_name)

    destination = Path(download_dir)
    destination.mkdir(parents=True, exist_ok=True)
    before = {path.resolve() for path in destination.rglob("*") if path.is_file()}

    args = [*command_parts, link, str(destination)]
    if use_cmd_shell:
        command_text = subprocess.list2cmdline(args)
        args = ["cmd.exe", "/c", command_text]

    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **_mega_subprocess_kwargs(),
    )
    output_chunks: list[bytes] = []
    output_task = asyncio.create_task(_read_process_output(process, output_chunks))
    recovered_candidate: Path | None = None
    stable_candidate: Path | None = None
    stable_size: int | None = None
    stable_polls = 0

    while True:
        try:
            await asyncio.wait_for(process.wait(), timeout=DOWNLOAD_POLL_INTERVAL_SECONDS)
            break
        except TimeoutError:
            candidate = _select_download_candidate(destination, before)
            if candidate is None:
                continue

            candidate_size = candidate.stat().st_size
            if stable_candidate == candidate and stable_size == candidate_size:
                stable_polls += 1
            else:
                stable_candidate = candidate
                stable_size = candidate_size
                stable_polls = 1

            if stable_polls >= DOWNLOAD_STABLE_POLLS_REQUIRED and _is_complete_download(candidate):
                recovered_candidate = candidate
                await _terminate_hung_process(process)
                break

    output = (await _resolve_output_bytes(output_task, output_chunks)).decode("utf-8", errors="replace")
    if recovered_candidate is not None:
        recovered_message = "\n".join(
            filter(
                None,
                [
                    output.strip(),
                    f"Recovered downloaded archive after MEGAcmd did not exit cleanly: {recovered_candidate}",
                ],
            )
        )
        return str(recovered_candidate), recovered_message

    selected = _select_download_candidate(destination, before)

    if process.returncode != 0:
        message = output.strip() or "MEGAcmd download failed"
        raise MegaDownloadError(message, return_code=process.returncode, output=message)

    if selected is None:
        raise MegaDownloadError("MEGAcmd completed without producing a file", output="MEGAcmd completed without producing a file")
    return str(selected), output.strip()


def resolve_mega_command(command_name: str) -> tuple[list[str], bool]:
    raw_value = command_name.strip()
    if not raw_value:
        raise MegaCommandConfigurationError("尚未配置下载命令，请先在设置中填写可用的 mega-get 可执行文件或目录。")

    raw_path = Path(raw_value)
    if raw_path.is_dir():
        for candidate in ("MegaClient.exe", "mega-get.exe", "mega-get.bat"):
                resolved = raw_path / candidate
                if resolved.exists():
                    return _normalize_command_path(resolved)
        raise MegaCommandConfigurationError("当前下载命令不可用，请先在设置中填写可用的 mega-get 可执行文件或目录。")

    if raw_path.exists():
        return _normalize_command_path(raw_path)

    resolved = (
        shutil.which(raw_value)
        or shutil.which(f"{raw_value}.exe")
        or shutil.which(f"{raw_value}.bat")
        or shutil.which(f"{raw_value}.cmd")
    )
    if resolved is None:
        raise MegaCommandConfigurationError("当前下载命令不可用，请先在设置中填写可用的 mega-get 可执行文件或目录。")
    return _normalize_command_path(Path(resolved))


def _normalize_command_path(path: Path) -> tuple[list[str], bool]:
    suffix = path.suffix.lower()
    if suffix in {".bat", ".cmd"}:
        return [str(path)], True
    if path.name.lower() == "megaclient.exe":
        return [str(path), "get"], False
    return [str(path)], False


def classify_mega_download_failure(
    return_code: int | None,
    output: str | None,
) -> MegaDownloadFailureKind:
    if return_code in {3, 4, 13}:
        return MegaDownloadFailureKind.RETRYABLE
    if return_code in {5, 8, 9, 11, 53}:
        return MegaDownloadFailureKind.UNAVAILABLE

    normalized = (output or "").lower()
    if any(token in normalized for token in ("not found", "does not exist", "expired", "access denied", "public node")):
        return MegaDownloadFailureKind.UNAVAILABLE
    if any(token in normalized for token in ("timed out", "timeout", "failed to access server: 231", "temporary", "rate limit")):
        return MegaDownloadFailureKind.RETRYABLE
    return MegaDownloadFailureKind.UNKNOWN


def describe_mega_download_failure(
    failure_kind: MegaDownloadFailureKind,
    return_code: int | None,
    output: str | None,
) -> str:
    if failure_kind == MegaDownloadFailureKind.UNAVAILABLE:
        return "当前 MEGA 链接已失效，无法继续下载。"
    if failure_kind == MegaDownloadFailureKind.RETRYABLE:
        return "下载压缩包失败，可稍后重试。"
    if return_code is not None:
        return f"下载压缩包失败（返回码 {return_code}）。"
    if output:
        return "下载压缩包失败，请查看任务日志。"
    return "下载压缩包失败。"


def _mega_subprocess_kwargs() -> dict[str, object]:
    if os.name != "nt":
        return {}

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


async def _read_process_output(process: asyncio.subprocess.Process, chunks: list[bytes] | None = None) -> bytes:
    stream = process.stdout
    if stream is None:
        return b""

    target_chunks = chunks if chunks is not None else []
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        target_chunks.append(chunk)
    return b"".join(target_chunks)


async def _terminate_hung_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return

    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=DOWNLOAD_PROCESS_SHUTDOWN_TIMEOUT_SECONDS)
        return
    except TimeoutError:
        pass

    process.kill()
    await asyncio.wait_for(process.wait(), timeout=DOWNLOAD_PROCESS_SHUTDOWN_TIMEOUT_SECONDS)


async def _resolve_output_bytes(output_task: asyncio.Task[bytes], chunks: list[bytes]) -> bytes:
    try:
        return await asyncio.wait_for(asyncio.shield(output_task), timeout=DOWNLOAD_OUTPUT_DRAIN_TIMEOUT_SECONDS)
    except TimeoutError:
        output_task.cancel()
        with suppress(asyncio.CancelledError):
            await output_task
        return b"".join(chunks)


def _select_download_candidate(destination: Path, before: set[Path]) -> Path | None:
    after = [path.resolve() for path in destination.rglob("*") if path.is_file() and path.resolve() not in before]
    if not after:
        fallback = sorted(destination.rglob("*"), key=lambda path: path.stat().st_mtime, reverse=True)
        after = [path.resolve() for path in fallback if path.is_file()]

    if not after:
        return None

    return next((path for path in after if path.suffix.lower() == ".zip"), after[0])


def _is_complete_download(path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    if path.stat().st_size <= 0:
        return False
    if path.suffix.lower() != ".zip":
        return True

    try:
        with ZipFile(path) as archive:
            archive.testzip()
        return True
    except (BadZipFile, OSError):
        return False
