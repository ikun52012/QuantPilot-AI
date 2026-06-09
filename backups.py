"""
Signal Server - Backup Module
Database backup and restore functionality.
Supports both SQLite (file copy) and PostgreSQL (pg_dump).
Includes automatic scheduled backup functionality.
"""
import asyncio
import json
import os
import re
import shutil
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from loguru import logger

from core.config import settings
from core.utils.datetime import utcnow

# Backup directory
backup_path = Path(__file__).parent / "data" / "backups"
backup_path.mkdir(parents=True, exist_ok=True)

# Backup scheduler settings
_BACKUP_SCHEDULER_TASK: asyncio.Task | None = None
_BACKUP_INTERVAL_HOURS: float = 24.0  # Default: daily backups
_BACKUP_MAX_AGE_DAYS: int = 30  # Keep backups for 30 days
_BACKUP_SCHEDULER_RUNNING: bool = False


def _backup_file_for_name(backup_name: str) -> Path:
    name = str(backup_name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("Invalid backup name")
    path = (backup_path / f"{name}.zip").resolve()
    root = backup_path.resolve()
    if root != path.parent and root not in path.parents:
        raise ValueError("Invalid backup path")
    return path


def _safe_extract_zip(zf: zipfile.ZipFile, destination: Path) -> None:
    destination = destination.resolve()
    for member in zf.infolist():
        target = (destination / member.filename).resolve()
        if target != destination and destination not in target.parents:
            raise ValueError(f"Unsafe path in backup archive: {member.filename}")
    zf.extractall(destination)


def _is_postgresql() -> bool:
    """Check if the current database is PostgreSQL."""
    return "postgresql" in settings.database.url.lower()


def _parse_pg_url() -> dict:
    """Parse PostgreSQL connection URL into components."""
    url = settings.database.url
    # Remove async driver prefix for pg_dump
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 5432),
        "user": parsed.username or "signal",
        "password": parsed.password or "",
        "dbname": parsed.path.lstrip("/") or "signal_server",
    }


def _docker_available() -> bool:
    """Check if docker CLI is available on this system."""
    return shutil.which("docker") is not None


def _compose_cmd() -> list[str]:
    """Build base docker compose command with project/file args."""
    compose_file = Path(__file__).parent / "docker-compose.yml"
    if not compose_file.exists():
        # Fallback: search one directory up
        compose_file = Path(__file__).parent.parent / "docker-compose.yml"
    project = os.getenv("COMPOSE_PROJECT_NAME", "quantpilot-ai")
    return [
        "docker", "compose",
        "-p", project,
        "-f", str(compose_file),
    ]


async def _pg_dump_via_docker(dump_file: Path, pg: dict) -> tuple[int, bytes, bytes]:
    """Run pg_dump inside the postgres container via docker compose exec.

    Returns (returncode, stdout, stderr) similar to create_subprocess_exec.
    stdout is written to dump_file if returncode == 0.
    """
    base_cmd = _compose_cmd()
    # Inside the postgres container, connect via local socket (no password needed)
    cmd = base_cmd + [
        "exec", "-T", "postgres",
        "pg_dump",
        "-U", pg["user"],
        "-d", pg["dbname"],
        "--format=custom",
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode == 0 and stdout:
        dump_file.write_bytes(stdout)

    return (proc.returncode if proc.returncode is not None else 1), stdout, stderr


async def create_backup(note: str = "") -> dict:
    """Create a database backup (SQLite zip or PostgreSQL pg_dump)."""
    timestamp = utcnow().strftime("%Y%m%d_%H%M%S")
    backup_name = f"backup_{timestamp}"
    backup_file = backup_path / f"{backup_name}.zip"

    data_dir = Path(__file__).parent / "data"
    files_to_backup = []

    if _is_postgresql():
        # PostgreSQL: use pg_dump
        pg = _parse_pg_url()
        dump_file = backup_path / f"{backup_name}.sql"

        env = os.environ.copy()
        # SECURITY: Use .pgpass file instead of PGPASSWORD env var to avoid
        # password exposure in /proc/*/environ
        pgpass_file = None
        if pg["password"]:
            import tempfile
            pgpass_content = f"{pg['host']}:{pg['port']}:{pg['dbname']}:{pg['user']}:{pg['password']}\n"
            pgpass_fd, pgpass_path = tempfile.mkstemp(prefix=".pgpass_")
            with os.fdopen(pgpass_fd, 'w') as f:
                f.write(pgpass_content)
            os.chmod(pgpass_path, 0o600)
            env["PGPASSFILE"] = pgpass_path
            pgpass_file = pgpass_path

        cmd = [
            "pg_dump",
            "-h", pg["host"],
            "-p", pg["port"],
            "-U", pg["user"],
            "-d", pg["dbname"],
            "--format=custom",
            "-f", str(dump_file),
        ]

        # P0-FIX: Validate all command parameters to prevent injection
        for param in [pg["host"], pg["port"], pg["user"], pg["dbname"], str(dump_file)]:
            if not param or any(ord(ch) < 32 or ord(ch) > 126 for ch in str(param)):
                logger.error(f"[Backup] Invalid parameter detected in pg_dump command: {param[:20]}")
                return {"status": "error", "reason": "Invalid database connection parameter"}

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            # Cleanup pgpass file
            if pgpass_file and os.path.exists(pgpass_file):
                try:
                    os.unlink(pgpass_file)
                except OSError:
                    pass

            if proc.returncode != 0:
                error_msg = stderr.decode().strip() if stderr else "Unknown error"
                logger.error(f"[Backup] pg_dump failed: {error_msg}")
                # Clean up partial dump
                dump_file.unlink(missing_ok=True)
                return {"status": "error", "reason": f"pg_dump failed: {error_msg}"}

            files_to_backup.append(("database.dump", dump_file))
            logger.info(f"[Backup] PostgreSQL dump created: {dump_file.stat().st_size / 1024:.1f} KB")
        except FileNotFoundError:
            # Host pg_dump not available — try docker compose exec into postgres container
            if _docker_available():
                logger.info("[Backup] Host pg_dump not found, trying docker compose exec fallback.")
                try:
                    rc, stdout, stderr = await _pg_dump_via_docker(dump_file, pg)
                    if rc != 0:
                        error_msg = stderr.decode().strip() if stderr else "Unknown error"
                        logger.error(f"[Backup] docker pg_dump failed: {error_msg}")
                        dump_file.unlink(missing_ok=True)
                        return {"status": "error", "reason": f"docker pg_dump failed: {error_msg}"}
                    files_to_backup.append(("database.dump", dump_file))
                    logger.info(f"[Backup] PostgreSQL dump created via docker: {dump_file.stat().st_size / 1024:.1f} KB")
                except FileNotFoundError:
                    logger.error("[Backup] docker compose not found.")
                    return {"status": "error", "reason": "pg_dump not found and docker compose is unavailable."}
            else:
                logger.error("[Backup] pg_dump not found. Install PostgreSQL client tools.")
                return {"status": "error", "reason": "pg_dump not found. Install PostgreSQL client tools."}
    else:
        # SQLite: copy database file
        db_file = data_dir / "server.db"
        if db_file.exists():
            files_to_backup.append(("server.db", db_file))

    # P1-FIX: Exclude encryption key from ordinary downloadable backups to prevent secret exposure
    # Include runtime settings only
    settings_file = data_dir / "runtime_settings.json"
    if settings_file.exists():
        files_to_backup.append(("runtime_settings.json", settings_file))

    if not files_to_backup:
        return {"status": "error", "reason": "No files to backup"}

    # Create zip
    with zipfile.ZipFile(backup_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, path in files_to_backup:
            zf.write(path, name)

        # Add metadata
        metadata = {
            "created_at": utcnow().isoformat(),
            "note": note,
            "files": [name for name, _ in files_to_backup],
            "database_type": "postgresql" if _is_postgresql() else "sqlite",
            "version": settings.app_version,
        }
        zf.writestr("metadata.json", json.dumps(metadata, indent=2))

    size_mb = backup_file.stat().st_size / (1024 * 1024)

    # Clean up temporary pg_dump file
    if _is_postgresql():
        dump_file = backup_path / f"{backup_name}.sql"
        dump_file.unlink(missing_ok=True)

    logger.info(f"[Backup] Created {backup_name}.zip ({size_mb:.2f} MB)")

    return {
        "status": "ok",
        "backup_name": backup_name,
        "file": str(backup_file),
        "size_mb": round(size_mb, 2),
        "files": len(files_to_backup),
        "database_type": "postgresql" if _is_postgresql() else "sqlite",
    }


async def list_backups() -> list[dict]:
    """List available backups."""
    backups = []

    for file in backup_path.glob("backup_*.zip"):
        try:
            with zipfile.ZipFile(file, 'r') as zf:
                metadata_str = zf.read("metadata.json").decode()
                metadata = json.loads(metadata_str)

            stat = file.stat()
            backups.append({
                "name": file.stem,
                "file": str(file),
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "created_at": metadata.get("created_at"),
                "note": metadata.get("note", ""),
                "files": metadata.get("files", []),
                "database_type": metadata.get("database_type", "sqlite"),
            })
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"[Backup] Could not read {file.name}: {e}")

    return sorted(backups, key=lambda x: x.get("created_at", ""), reverse=True)


async def cleanup_old_backups(max_backups: int = 7) -> dict:
    """Delete oldest backups, keeping only the most recent `max_backups`."""
    backups = await list_backups()
    if len(backups) <= max_backups:
        return {"deleted": 0, "kept": len(backups)}

    to_delete = backups[max_backups:]
    deleted = 0
    for backup in to_delete:
        try:
            backup_file = Path(backup["file"])
            if backup_file.exists():
                backup_file.unlink()
                deleted += 1
                logger.info(f"[Backup] Deleted old backup: {backup['name']}")
        except (OSError, PermissionError) as e:
            logger.warning(f"[Backup] Failed to delete {backup.get('name', 'unknown')}: {e}")

    return {"deleted": deleted, "kept": len(backups) - deleted}


async def delete_backup(backup_name: str) -> bool:
    """Delete a backup."""
    try:
        backup_file = _backup_file_for_name(backup_name)
    except ValueError:
        return False

    if not backup_file.exists():
        return False

    backup_file.unlink()
    logger.info(f"[Backup] Deleted {backup_name}")
    return True


async def restore_postgresql(backup_name: str) -> dict:
    """
    Restore a PostgreSQL backup using pg_restore.
    WARNING: This will overwrite the current database.
    """
    if not _is_postgresql():
        return {"status": "error", "reason": "Not a PostgreSQL database"}

    try:
        backup_file = _backup_file_for_name(backup_name)
    except ValueError as exc:
        return {"status": "error", "reason": str(exc)}
    if not backup_file.exists():
        return {"status": "error", "reason": "Backup not found"}

    data_dir = Path(__file__).parent / "data"
    staging_dir = data_dir / "restore_staging"
    staging_dir.mkdir(exist_ok=True)

    # Extract
    with zipfile.ZipFile(backup_file, 'r') as zf:
        _safe_extract_zip(zf, staging_dir)

    dump_file = staging_dir / "database.dump"
    if not dump_file.exists():
        return {"status": "error", "reason": "No database dump found in backup"}

    pg = _parse_pg_url()
    env = os.environ.copy()
    if pg["password"]:
        env["PGPASSWORD"] = pg["password"]

    cmd = [
        "pg_restore",
        "-h", pg["host"],
        "-p", pg["port"],
        "-U", pg["user"],
        "-d", pg["dbname"],
        "--clean",
        "--if-exists",
        str(dump_file),
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return_code = proc.returncode if proc.returncode is not None else 1

        # pg_restore returns non-zero for warnings too, check stderr
        if return_code != 0:
            error_msg = stderr.decode().strip() if stderr else ""
            # pg_restore often returns 1 for non-fatal warnings
            if return_code > 1:
                return {"status": "error", "reason": f"pg_restore failed: {error_msg}"}
            logger.warning(f"[Backup] pg_restore completed with warnings: {error_msg[:200]}")

        # Restore encryption key if present
        key_file = staging_dir / "app_encryption.key"
        if key_file.exists():
            shutil.copy2(key_file, data_dir / "app_encryption.key")

        # Clean up staging
        shutil.rmtree(staging_dir, ignore_errors=True)

        logger.info(f"[Backup] PostgreSQL restore completed: {backup_name}")
        return {"status": "ok", "backup_name": backup_name}

    except FileNotFoundError:
        return {"status": "error", "reason": "pg_restore not found. Install PostgreSQL client tools."}


def stage_restore(backup_name: str) -> dict:
    """
    Stage a backup for restore (SQLite).
    Returns paths to restore without actually performing the restore.
    """
    try:
        backup_file = _backup_file_for_name(backup_name)
    except ValueError as exc:
        return {"status": "error", "reason": str(exc)}

    if not backup_file.exists():
        return {"status": "error", "reason": "Backup not found"}

    data_dir = Path(__file__).parent / "data"
    staging_dir = data_dir / "restore_staging"
    staging_dir.mkdir(exist_ok=True)

    # Extract to staging
    with zipfile.ZipFile(backup_file, 'r') as zf:
        _safe_extract_zip(zf, staging_dir)

    # Read metadata
    metadata_file = staging_dir / "metadata.json"
    metadata = {}
    if metadata_file.exists():
        metadata = json.loads(metadata_file.read_text())

    db_type = metadata.get("database_type", "sqlite")

    return {
        "status": "staged",
        "backup_name": backup_name,
        "staging_dir": str(staging_dir),
        "metadata": metadata,
        "database_type": db_type,
        "instructions": (
            "Backup staged for restore.\n"
            f"Database type: {db_type}\n"
            + (
                "For PostgreSQL: call POST /api/admin/backup/restore-pg\n"
                if db_type == "postgresql" else
                "For SQLite:\n"
                "1. Stop the server\n"
                "2. Copy files from staging_dir to data/\n"
                "3. Restart the server\n"
            )
            + "WARNING: This will overwrite existing data!"
        ),
    }


async def _cleanup_old_backups() -> dict:
    """Remove backups older than _BACKUP_MAX_AGE_DAYS."""
    try:
        cutoff = utcnow() - timedelta(days=_BACKUP_MAX_AGE_DAYS)
        removed_count = 0
        removed_size = 0

        for backup_file in backup_path.glob("backup_*.zip"):
            try:
                # Extract timestamp from filename
                stat = backup_file.stat()
                file_mtime = datetime.fromtimestamp(stat.st_mtime)
                if file_mtime < cutoff:
                    size = stat.st_size
                    backup_file.unlink()
                    removed_count += 1
                    removed_size += size
            except Exception as e:
                logger.warning(f"[Backup] Failed to remove old backup {backup_file}: {e}")

        return {
            "status": "success",
            "removed_count": removed_count,
            "removed_size_mb": round(removed_size / (1024 * 1024), 2),
        }
    except Exception as e:
        logger.error(f"[Backup] Cleanup failed: {e}")
        return {"status": "error", "reason": str(e)}


async def _backup_scheduler_loop() -> None:
    """Background task for scheduled backups."""
    global _BACKUP_SCHEDULER_RUNNING

    _BACKUP_SCHEDULER_RUNNING = True
    logger.info(f"[Backup] Scheduler started with interval {_BACKUP_INTERVAL_HOURS}h")

    while _BACKUP_SCHEDULER_RUNNING:
        try:
            # Create backup with note
            result = await create_backup(note=f"Scheduled backup (interval: {_BACKUP_INTERVAL_HOURS}h)")

            if result.get("status") == "ok":
                logger.info(f"[Backup] Scheduled backup created: {result.get('backup_name')}")
            else:
                logger.error(f"[Backup] Scheduled backup failed: {result.get('reason')}")

            # Cleanup old backups
            cleanup_result = await _cleanup_old_backups()
            if cleanup_result.get("status") == "success" and cleanup_result.get("removed_count", 0) > 0:
                logger.info(f"[Backup] Cleaned up {cleanup_result['removed_count']} old backups ({cleanup_result['removed_size_mb']} MB)")

        except Exception as e:
            logger.error(f"[Backup] Scheduler error: {e}")

        # Wait for next backup interval
        try:
            await asyncio.wait_for(
                asyncio.sleep(_BACKUP_INTERVAL_HOURS * 3600),
                timeout=None
            )
        except asyncio.CancelledError:
            logger.info("[Backup] Scheduler cancelled")
            break

    _BACKUP_SCHEDULER_RUNNING = False


def start_backup_scheduler(interval_hours: float | None = None, max_age_days: int | None = None) -> dict:
    """Start the automatic backup scheduler.

    Args:
        interval_hours: Hours between backups (default: 24.0)
        max_age_days: Days to keep backups (default: 30)

    Returns:
        Dict with status and message
    """
    global _BACKUP_SCHEDULER_TASK, _BACKUP_INTERVAL_HOURS, _BACKUP_MAX_AGE_DAYS

    # Update settings if provided
    if interval_hours is not None:
        _BACKUP_INTERVAL_HOURS = max(0.5, interval_hours)  # Minimum 30 minutes
    if max_age_days is not None:
        _BACKUP_MAX_AGE_DAYS = max(1, max_age_days)

    # Check if already running
    if _BACKUP_SCHEDULER_TASK and not _BACKUP_SCHEDULER_TASK.done():
        return {
            "status": "already_running",
            "interval_hours": _BACKUP_INTERVAL_HOURS,
            "max_age_days": _BACKUP_MAX_AGE_DAYS,
        }

    # Start scheduler task
    _BACKUP_SCHEDULER_TASK = asyncio.create_task(_backup_scheduler_loop())

    return {
        "status": "started",
        "interval_hours": _BACKUP_INTERVAL_HOURS,
        "max_age_days": _BACKUP_MAX_AGE_DAYS,
        "message": f"Backup scheduler started (every {_BACKUP_INTERVAL_HOURS}h, keep {_BACKUP_MAX_AGE_DAYS} days)",
    }


def stop_backup_scheduler() -> dict:
    """Stop the automatic backup scheduler.

    Returns:
        Dict with status and message
    """
    global _BACKUP_SCHEDULER_TASK, _BACKUP_SCHEDULER_RUNNING

    if not _BACKUP_SCHEDULER_TASK or _BACKUP_SCHEDULER_TASK.done():
        return {"status": "not_running", "message": "Backup scheduler is not running"}

    _BACKUP_SCHEDULER_RUNNING = False
    _BACKUP_SCHEDULER_TASK.cancel()

    return {"status": "stopped", "message": "Backup scheduler stopped"}


def get_backup_scheduler_status() -> dict:
    """Get the current status of the backup scheduler.

    Returns:
        Dict with status, interval, and age settings
    """
    is_running = (
        _BACKUP_SCHEDULER_TASK is not None
        and not _BACKUP_SCHEDULER_TASK.done()
        and _BACKUP_SCHEDULER_RUNNING
    )

    return {
        "status": "running" if is_running else "stopped",
        "interval_hours": _BACKUP_INTERVAL_HOURS,
        "max_age_days": _BACKUP_MAX_AGE_DAYS,
    }
