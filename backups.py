"""
Local backup helpers for persistent runtime data.
"""
import shutil
import zipfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
BACKUP_DIR = DATA_DIR / "backups"
RESTORE_DIR = DATA_DIR / "restore_pending"


def _safe_name(name: str) -> str:
    name = Path(name).name
    if not name.endswith(".zip"):
        raise ValueError("Invalid backup filename")
    return name


def create_backup() -> dict:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    path = BACKUP_DIR / f"signal-server-backup-{stamp}.zip"
    items = [
        DATA_DIR / "server.db",
        DATA_DIR / "runtime_settings.json",
        DATA_DIR / "app_encryption.key",
        ROOT / ".env",
    ]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in items:
            if item.exists() and item.is_file():
                zf.write(item, item.name)
    return {"filename": path.name, "size": path.stat().st_size, "created_at": datetime.utcnow().isoformat()}


def list_backups() -> list[dict]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    result = []
    for path in sorted(BACKUP_DIR.glob("signal-server-backup-*.zip"), reverse=True):
        result.append({
            "filename": path.name,
            "size": path.stat().st_size,
            "created_at": datetime.utcfromtimestamp(path.stat().st_mtime).isoformat(),
        })
    return result


def backup_path(filename: str) -> Path:
    name = _safe_name(filename)
    path = BACKUP_DIR / name
    if not path.exists() or not path.is_file():
        raise FileNotFoundError("Backup not found")
    return path


def stage_restore(filename: str) -> dict:
    src = backup_path(filename)
    target = RESTORE_DIR / src.stem
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src, "r") as zf:
        for member in zf.namelist():
            if Path(member).name != member:
                continue
            zf.extract(member, target)
    marker = RESTORE_DIR / "README_RESTORE.txt"
    marker.write_text(
        "Restore files have been staged. Stop the app, copy staged files into /app/data or project root as appropriate, then restart.\n",
        encoding="utf-8",
    )
    return {
        "status": "staged",
        "path": str(target),
        "message": "Restore staged. Stop the service before replacing live SQLite/runtime files.",
    }
