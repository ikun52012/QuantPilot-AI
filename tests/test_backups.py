"""
Regression tests for backups.py docker fallback.
Ensures that pg_dump missing on the host gracefully falls back to docker compose exec.
"""
import asyncio
import os
import sqlite3
import time
import zipfile
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
async def test_local_pg_dump_always_removes_temporary_pgpass(tmp_path, monkeypatch):
    import tempfile

    import backups

    pgpass_path = tmp_path / ".pgpass_test"

    def fake_mkstemp(*, prefix):
        assert prefix == ".pgpass_"
        fd = os.open(pgpass_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        return fd, str(pgpass_path)

    async def missing_pg_dump(*_args, **kwargs):
        assert kwargs["env"]["PGPASSFILE"] == str(pgpass_path)
        assert pgpass_path.exists()
        raise FileNotFoundError("pg_dump not found")

    monkeypatch.setattr(tempfile, "mkstemp", fake_mkstemp)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing_pg_dump)

    with pytest.raises(FileNotFoundError):
        await backups._pg_dump_local(
            tmp_path / "dump.sql",
            {
                "host": "localhost",
                "port": "5432",
                "user": "signal",
                "password": "secret",
                "dbname": "signal_server",
            },
        )

    assert not pgpass_path.exists()


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


@pytest.mark.asyncio
async def test_sqlite_backup_snapshots_configured_database_and_safety_state(tmp_path, monkeypatch):
    import backups

    source_db = tmp_path / "custom-runtime.db"
    connection = sqlite3.connect(source_db)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE fills (id INTEGER PRIMARY KEY, status TEXT)")
    connection.execute("INSERT INTO fills(status) VALUES ('filled')")
    connection.commit()

    runtime_data = tmp_path / "runtime-data"
    runtime_data.mkdir()
    (runtime_data / "account_risk_tracker.json").write_text(
        '{"global": {"limit_triggered": true}}',
        encoding="utf-8",
    )
    (runtime_data / "filter_performance.json").write_text(
        '{"trend": {"precision": 0.75}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(backups, "DATA_DIR", runtime_data)
    monkeypatch.setattr(
        "core.config.settings.database.url",
        f"sqlite+aiosqlite:///{source_db.as_posix()}",
    )

    try:
        result = await backups.create_backup(note="configured sqlite snapshot")
    finally:
        connection.close()

    assert result["status"] == "ok"
    archive = Path(result["file"])
    restored_db = tmp_path / "restored.db"
    with zipfile.ZipFile(archive) as zf:
        assert "account_risk_tracker.json" in zf.namelist()
        assert "filter_performance.json" in zf.namelist()
        restored_db.write_bytes(zf.read("server.db"))
    with sqlite3.connect(restored_db) as restored:
        assert restored.execute("SELECT status FROM fills").fetchone() == ("filled",)


@pytest.mark.asyncio
async def test_age_cleanup_compares_utc_timestamps_without_type_error(monkeypatch):
    import backups

    old_backup = backups.backup_path / "backup_old.zip"
    old_backup.write_bytes(b"old")
    old_timestamp = time.time() - 40 * 86400
    os.utime(old_backup, (old_timestamp, old_timestamp))
    monkeypatch.setattr(backups, "_BACKUP_MAX_AGE_DAYS", 30)

    result = await backups._cleanup_old_backups()

    assert result["status"] == "success"
    assert result["removed_count"] == 1
    assert not old_backup.exists()


@pytest.mark.asyncio
async def test_legacy_backup_scheduler_does_not_dump_immediately(monkeypatch):
    import backups

    create_backup = AsyncMock(return_value={"status": "ok", "backup_name": "unused"})
    monkeypatch.setattr(backups, "create_backup", create_backup)
    monkeypatch.setattr(backups, "_BACKUP_INTERVAL_HOURS", 0.5)
    monkeypatch.setattr(backups, "_BACKUP_SCHEDULER_TASK", None)
    monkeypatch.setattr(backups, "_BACKUP_SCHEDULER_RUNNING", False)

    result = backups.start_backup_scheduler()
    await asyncio.sleep(0)

    assert result["status"] == "started"
    create_backup.assert_not_awaited()
    task = backups._BACKUP_SCHEDULER_TASK
    backups.stop_backup_scheduler()
    if task is not None:
        await task
