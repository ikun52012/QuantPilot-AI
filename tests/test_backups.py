"""
Regression tests for backups.py docker fallback.
Ensures that pg_dump missing on the host gracefully falls back to docker compose exec.
"""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_backup_path(tmp_path, monkeypatch):
    """Redirect backup path to a temp directory so tests don't touch real data."""
    import backups

    original_path = backups.backup_path
    backups.backup_path = tmp_path / "backups"
    backups.backup_path.mkdir(parents=True, exist_ok=True)
    yield
    backups.backup_path = original_path


def _make_mock_proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
    """Create a mock asyncio Process."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


@pytest.mark.asyncio
async def test_create_backup_uses_local_pg_dump_when_available(tmp_path, monkeypatch):
    """When local pg_dump exists, it is used directly (no docker fallback)."""
    import backups

    monkeypatch.setattr(
        "core.config.settings.database.url",
        "postgresql+asyncpg://signal:secret@localhost:5432/signal_server",
    )

    # Track whether pg_dump subprocess was invoked
    pg_dump_called = False

    async def mock_exec(*cmd, **kwargs):
        nonlocal pg_dump_called
        if cmd[0] == "pg_dump":
            pg_dump_called = True
            # Simulate pg_dump creating the output file
            dump_path = Path(cmd[cmd.index("-f") + 1])
            dump_path.write_bytes(b"fake_pg_dump_data")
            return _make_mock_proc(returncode=0)
        return _make_mock_proc(returncode=1)

    with patch("shutil.which", side_effect=lambda cmd: "/usr/bin/pg_dump" if cmd == "pg_dump" else None):
        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            result = await backups.create_backup(note="test")

    assert result["status"] == "ok"
    assert pg_dump_called is True


@pytest.mark.asyncio
async def test_create_backup_fallback_to_docker_pg_dump(tmp_path, monkeypatch):
    """When local pg_dump is missing but docker is available, fallback to docker compose exec."""
    import backups

    monkeypatch.setattr(
        "core.config.settings.database.url",
        "postgresql+asyncpg://signal:secret@localhost:5432/signal_server",
    )

    docker_called = False

    async def mock_exec(*cmd, **kwargs):
        nonlocal docker_called
        if cmd[0] == "pg_dump":
            raise FileNotFoundError("pg_dump not found")
        if "docker" in cmd and "compose" in cmd:
            docker_called = True
            # Simulate docker compose exec pg_dump writing to stdout
            return _make_mock_proc(returncode=0, stdout=b"fake_docker_dump_data")
        return _make_mock_proc(returncode=1)

    with patch("shutil.which", side_effect=lambda cmd: {
        "pg_dump": None,
        "docker": "/usr/bin/docker",
    }.get(cmd)):
        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            result = await backups.create_backup(note="test")

    assert result["status"] == "ok"
    assert docker_called is True


@pytest.mark.asyncio
async def test_create_backup_fails_when_neither_pg_dump_nor_docker_available(tmp_path, monkeypatch):
    """When both pg_dump and docker are missing, return a clear error."""
    import backups

    monkeypatch.setattr(
        "core.config.settings.database.url",
        "postgresql+asyncpg://signal:secret@localhost:5432/signal_server",
    )

    async def mock_exec(*cmd, **kwargs):
        if cmd[0] == "pg_dump":
            raise FileNotFoundError("pg_dump not found")
        return _make_mock_proc(returncode=1)

    with patch("shutil.which", return_value=None):
        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            result = await backups.create_backup(note="test")

    assert result["status"] == "error"
    assert "pg_dump not found" in result["reason"]


@pytest.mark.asyncio
async def test_create_backup_docker_fallback_failure_returns_error(tmp_path, monkeypatch):
    """If docker fallback is attempted but fails, return the docker error message."""
    import backups

    monkeypatch.setattr(
        "core.config.settings.database.url",
        "postgresql+asyncpg://signal:secret@localhost:5432/signal_server",
    )

    async def mock_exec(*cmd, **kwargs):
        if cmd[0] == "pg_dump":
            raise FileNotFoundError("pg_dump not found")
        # Docker fallback fails
        return _make_mock_proc(returncode=1, stderr=b"docker exec failed: container not running")

    with patch("shutil.which", side_effect=lambda cmd: {
        "pg_dump": None,
        "docker": "/usr/bin/docker",
    }.get(cmd)):
        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            result = await backups.create_backup(note="test")

    assert result["status"] == "error"
    assert "docker pg_dump failed" in result["reason"]
