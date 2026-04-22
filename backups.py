"""
Signal Server - Backup Module
Database backup and restore functionality.
"""
import os
import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional
from loguru import logger

from core.config import settings
from core.utils.datetime import utcnow, utcnow_iso, utcnow_str


# Backup directory
backup_path = Path(__file__).parent / "data" / "backups"
backup_path.mkdir(parents=True, exist_ok=True)


async def create_backup(note: str = "") -> dict:
    """Create a database backup."""
    timestamp = utcnow().strftime("%Y%m%d_%H%M%S")
    backup_name = f"backup_{timestamp}"
    backup_file = backup_path / f"{backup_name}.zip"

    data_dir = Path(__file__).parent / "data"

    files_to_backup = []

    # SQLite database
    db_file = data_dir / "server.db"
    if db_file.exists():
        files_to_backup.append(("server.db", db_file))

    # Encryption key
    key_file = data_dir / "app_encryption.key"
    if key_file.exists():
        files_to_backup.append(("app_encryption.key", key_file))

    # Runtime settings
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
        }
        zf.writestr("metadata.json", json.dumps(metadata, indent=2))

    size_mb = backup_file.stat().st_size / (1024 * 1024)

    logger.info(f"[Backup] Created {backup_name}.zip ({size_mb:.2f} MB)")

    return {
        "status": "ok",
        "backup_name": backup_name,
        "file": str(backup_file),
        "size_mb": round(size_mb, 2),
        "files": len(files_to_backup),
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


def stage_restore(backup_name: str) -> dict:
    """
    Stage a backup for restore.
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

    return {
        "status": "staged",
        "backup_name": backup_name,
        "staging_dir": str(staging_dir),
        "metadata": metadata,
        "instructions": (
            "Backup staged for restore. To complete restore:\n"
            "1. Stop the server\n"
            "2. Copy files from staging_dir to data/\n"
            "3. Restart the server\n"
            "WARNING: This will overwrite existing data!"
        ),
    }
