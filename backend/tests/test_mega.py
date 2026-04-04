from pathlib import Path
from zipfile import ZipFile
import asyncio

from app.services.mega import _mega_subprocess_kwargs, download_public_link, resolve_mega_command


def test_resolve_mega_command_from_directory(tmp_path: Path) -> None:
    command_dir = tmp_path / "megacmd"
    command_dir.mkdir()
    target = command_dir / "mega-get.bat"
    target.write_text("@echo off\n", encoding="utf-8")

    command, use_cmd_shell = resolve_mega_command(str(command_dir))

    assert Path(command[0]) == target
    assert use_cmd_shell is True


def test_resolve_mega_command_from_executable_path(tmp_path: Path) -> None:
    executable = tmp_path / "MEGAclient.exe"
    executable.write_bytes(b"")

    command, use_cmd_shell = resolve_mega_command(str(executable))

    assert command == [str(executable), "get"]
    assert use_cmd_shell is False


def test_mega_subprocess_kwargs_hide_window_on_windows(monkeypatch) -> None:
    monkeypatch.setattr("app.services.mega.os.name", "nt")

    kwargs = _mega_subprocess_kwargs()

    assert kwargs["creationflags"] != 0
    assert kwargs["startupinfo"].dwFlags != 0


def test_download_public_link_recovers_when_process_hangs_after_zip_is_complete(monkeypatch, tmp_path: Path) -> None:
    class FakeStdout:
        def __init__(self, process) -> None:
            self._process = process
            self._chunks = [b"TRANSFERRING", b""]

        async def read(self, _: int) -> bytes:
            if self._chunks:
                chunk = self._chunks.pop(0)
                if chunk:
                    return chunk
            await self._process._done.wait()
            return b""

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode = None
            self._done = asyncio.Event()
            self.stdout = FakeStdout(self)

        async def wait(self) -> int:
            await self._done.wait()
            assert self.returncode is not None
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15
            self._done.set()

        def kill(self) -> None:
            self.returncode = -9
            self._done.set()

    async def fake_create_subprocess_exec(*args, **kwargs):
        destination = Path(args[-1])
        destination.mkdir(parents=True, exist_ok=True)

        async def write_zip() -> None:
            await asyncio.sleep(0.02)
            with ZipFile(destination / "sample.zip", "w") as archive:
                archive.writestr("sample.txt", b"hello")

        asyncio.create_task(write_zip())
        return FakeProcess()

    monkeypatch.setattr("app.services.mega.asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr("app.services.mega.DOWNLOAD_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("app.services.mega.DOWNLOAD_STABLE_POLLS_REQUIRED", 2)
    monkeypatch.setattr("app.services.mega.DOWNLOAD_PROCESS_SHUTDOWN_TIMEOUT_SECONDS", 0.1)

    fake_command = tmp_path / "mega-get.exe"
    fake_command.write_bytes(b"")

    archive_path, output = asyncio.run(
        download_public_link("https://mega.nz/file/test", str(tmp_path / "downloads"), str(fake_command))
    )

    assert Path(archive_path).name == "sample.zip"
    assert "Recovered downloaded archive after MEGAcmd did not exit cleanly" in output
