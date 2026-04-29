"""
Signal Server - Backup Module
Database backup and restore functionality.
Supports both SQLite (file copy) and PostgreSQL (pg_dump).
"""
import asyncio
import json
import os
import shutil
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from loguru import logger

from core.config import settings
from core.utils.datetime import utcnow

# Backup directory
backup_path = Path(__file__).parent / "data" / "backups"
backup_path.mkdir(parents=True, exist_ok=True)


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
        if pg["password"]:
            env["PGPASSWORD"] = pg["password"]

        cmd = [
            "pg_dump",
            "-h", pg["host"],
            "-p", pg["port"],
            "-U", pg["user"],
            "-d", pg["dbname"],
            "--format=custom",
            "-f", str(dump_file),
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                error_msg = stderr.decode().strip() if stderr else "Unknown error"
                logger.error(f"[Backup] pg_dump failed: {error_msg}")
                # Clean up partial dump
                dump_file.unlink(missing_ok=True)
                return {"status": "error", "reason": f"pg_dump failed: {error_msg}"}

            files_to_backup.append(("database.dump", dump_file))
            logger.info(f"[Backup] PostgreSQL dump created: {dump_file.stat().st_size / 1024:.1f} KB")
        except FileNotFoundError:
            logger.error("[Backup] pg_dump not found. Install PostgreSQL client tools.")
            return {"status": "error", "reason": "pg_dump not found. Install PostgreSQL client tools."}
    else:
        # SQLite: copy database file
        db_file = data_dir / "server.db"
        if db_file.exists():
            files_to_backup.append(("server.db", db_file))

    # Always include encryption key and runtime settings
    key_file = data_dir / "app_encryption.key"
    if key_file.exists():
        files_to_backup.append(("app_encryption.key", key_file))

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
        except Exception as e:
            logger.warning(f"[Backup] Could not read {file.name}: {e}")

    return sorted(backups, key=lambda x: x.get("created_at", ""), reverse=True)


async def delete_backup(backup_name: str) -> bool:
    """Delete a backup."""
    backup_file = backup_path / f"{backup_name}.zip"

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

    backup_file = backup_path / f"{backup_name}.zip"
    if not backup_file.exists():
        return {"status": "error", "reason": "Backup not found"}

    data_dir = Path(__file__).parent / "data"
    staging_dir = data_dir / "restore_staging"
    staging_dir.mkdir(exist_ok=True)

    # Extract
    with zipfile.ZipFile(backup_file, 'r') as zf:
        zf.extractall(staging_dir)

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
    backup_file = backup_path / f"{backup_name}.zip"

    if not backup_file.exists():
        return {"status": "error", "reason": "Backup not found"}

    data_dir = Path(__file__).parent / "data"
    staging_dir = data_dir / "restore_staging"
    staging_dir.mkdir(exist_ok=True)

    # Extract to staging
    with zipfile.ZipFile(backup_file, 'r') as zf:
        zf.extractall(staging_dir)

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
