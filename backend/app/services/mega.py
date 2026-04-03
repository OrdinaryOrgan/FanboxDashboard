from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from enum import StrEnum
from pathlib import Path


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
    stdout, _ = await process.communicate()
    output = stdout.decode("utf-8", errors="replace")
    if process.returncode != 0:
        message = output.strip() or "MEGAcmd download failed"
        raise MegaDownloadError(message, return_code=process.returncode, output=message)

    after = [path.resolve() for path in destination.rglob("*") if path.is_file() and path.resolve() not in before]
    if not after:
        fallback = sorted(destination.rglob("*"), key=lambda path: path.stat().st_mtime, reverse=True)
        after = [path for path in fallback if path.is_file()]

    if not after:
        raise MegaDownloadError("MEGAcmd completed without producing a file", output="MEGAcmd completed without producing a file")

    selected = next((path for path in after if path.suffix.lower() == ".zip"), after[0])
    return str(selected), output.strip()


def resolve_mega_command(command_name: str) -> tuple[list[str], bool]:
    raw_value = command_name.strip()
    if not raw_value:
        raise MegaCommandConfigurationError("尚未配置下载命令，请先在设置中填写可用的 mega-get 可执行文件或目录。")

    raw_path = Path(raw_value)
    if raw_path.is_dir():
        for candidate in ("mega-get.bat", "MegaClient.exe", "mega-get.exe"):
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
