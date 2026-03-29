from pathlib import Path

from app.services.mega import _mega_subprocess_kwargs, resolve_mega_command


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
