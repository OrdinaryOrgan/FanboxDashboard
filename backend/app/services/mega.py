from __future__ import annotations

import asyncio
import os
import subprocess
import shutil
from pathlib import Path


class MegaDownloadError(RuntimeError):
    pass


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
        raise MegaDownloadError(output.strip() or "MEGAcmd download failed")

    after = [path.resolve() for path in destination.rglob("*") if path.is_file() and path.resolve() not in before]
    if not after:
        fallback = sorted(destination.rglob("*"), key=lambda path: path.stat().st_mtime, reverse=True)
        after = [path for path in fallback if path.is_file()]

    if not after:
        raise MegaDownloadError("MEGAcmd completed without producing a file")

    selected = next((path for path in after if path.suffix.lower() == ".zip"), after[0])
    return str(selected), output.strip()


def resolve_mega_command(command_name: str) -> tuple[list[str], bool]:
    raw_value = command_name.strip()
    if not raw_value:
        raise MegaDownloadError("MEGAcmd command is empty")

    raw_path = Path(raw_value)
    if raw_path.is_dir():
        for candidate in ("mega-get.bat", "MegaClient.exe", "mega-get.exe"):
                resolved = raw_path / candidate
                if resolved.exists():
                    return _normalize_command_path(resolved)
        raise MegaDownloadError(f"Unable to find MEGAcmd executable in directory: {raw_value}")

    if raw_path.exists():
        return _normalize_command_path(raw_path)

    resolved = (
        shutil.which(raw_value)
        or shutil.which(f"{raw_value}.exe")
        or shutil.which(f"{raw_value}.bat")
        or shutil.which(f"{raw_value}.cmd")
    )
    if resolved is None:
        raise MegaDownloadError(f"Unable to find MEGAcmd command: {command_name}")
    return _normalize_command_path(Path(resolved))


def _normalize_command_path(path: Path) -> tuple[list[str], bool]:
    suffix = path.suffix.lower()
    if suffix in {".bat", ".cmd"}:
        return [str(path)], True
    if path.name.lower() == "megaclient.exe":
        return [str(path), "get"], False
    return [str(path)], False


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
