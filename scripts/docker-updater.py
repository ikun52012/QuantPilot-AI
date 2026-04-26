#!/usr/bin/env python3
"""Host-level Docker updater sidecar for QuantPilot AI."""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


BASE_DIR = Path(os.getenv("UPDATER_BASE_DIR", "/workspace")).resolve()
DATA_DIR = BASE_DIR / "data" / "updater"
REQUEST_DIR = DATA_DIR / "requests"
STATUS_DIR = DATA_DIR / "status"
HEALTH_FILE = STATUS_DIR / "updater-health.json"
COMPOSE_FILE = BASE_DIR / "docker-compose.yml"
COMPOSE_PROJECT = os.getenv("COMPOSE_PROJECT_NAME", "quantpilot-ai")
TARGET_SERVICE = os.getenv("UPDATE_TARGET_SERVICE", "signal-server")
TARGET_IMAGE = os.getenv("UPDATE_TARGET_IMAGE", "ghcr.io/ikun52012/quantpilot-ai:latest")
HEALTH_INTERVAL = int(os.getenv("UPDATER_HEARTBEAT_SECS", "10"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ensure_dirs() -> None:
    REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_DIR.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def append_log(payload: dict, message: str) -> None:
    payload.setdefault("log", []).append(message)
    payload["updated_at"] = now_iso()


def write_health(message: str, healthy: bool = True) -> None:
    ensure_dirs()
    write_json(
        HEALTH_FILE,
        {
            "service": "docker-updater",
            "updated_at": now_iso(),
            "healthy": healthy,
            "message": message,
            "compose_project": COMPOSE_PROJECT,
            "target_service": TARGET_SERVICE,
        },
    )


def compose_cmd(*args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "-p",
        COMPOSE_PROJECT,
        "-f",
        str(COMPOSE_FILE),
        *args,
    ]


def run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(BASE_DIR))


def queued_requests() -> list[Path]:
    ensure_dirs()
    return sorted(REQUEST_DIR.glob("upd_*.json"), key=lambda item: item.stat().st_mtime)


def process_request(path: Path) -> None:
    request = read_json(path)
    if not request:
        path.unlink(missing_ok=True)
        return

    task_id = str(request.get("task_id") or path.stem)
    status_path = STATUS_DIR / f"{task_id}.json"
    payload = read_json(status_path) or request
    payload["task_id"] = task_id
    payload["status"] = "running"
    payload["updated_at"] = now_iso()
    write_json(status_path, payload)

    try:
        append_log(payload, f"Pulling image {TARGET_IMAGE}.")
        write_json(status_path, payload)
        pull_result = run(["docker", "pull", TARGET_IMAGE], timeout=600)
        if pull_result.returncode != 0:
            append_log(payload, f"docker pull failed: {(pull_result.stderr or pull_result.stdout).strip()}")
            payload["status"] = "failed"
            payload["message"] = "docker pull failed"
            write_json(status_path, payload)
            return

        append_log(payload, f"Restarting service {TARGET_SERVICE} with latest image.")
        write_json(status_path, payload)
        up_result = run(compose_cmd("up", "-d", "--no-deps", TARGET_SERVICE), timeout=600)
        if up_result.returncode != 0:
            append_log(payload, f"docker compose up failed: {(up_result.stderr or up_result.stdout).strip()}")
            payload["status"] = "failed"
            payload["message"] = "docker compose up failed"
            write_json(status_path, payload)
            return

        ps_result = run(compose_cmd("ps", TARGET_SERVICE, "--format", "json"), timeout=60)
        append_log(payload, "Update rollout completed.")
        if ps_result.stdout.strip():
            append_log(payload, f"Service status: {ps_result.stdout.strip()[:500]}")
        payload["status"] = "completed"
        payload["message"] = f"Updated service {TARGET_SERVICE} to latest image"
        payload["completed_at"] = now_iso()
        write_json(status_path, payload)
    except subprocess.TimeoutExpired as exc:
        append_log(payload, f"Command timed out: {' '.join(exc.cmd)}")
        payload["status"] = "failed"
        payload["message"] = "Update timed out"
        write_json(status_path, payload)
    except Exception as exc:
        append_log(payload, f"Unexpected updater error: {exc}")
        payload["status"] = "failed"
        payload["message"] = str(exc)
        write_json(status_path, payload)
    finally:
        path.unlink(missing_ok=True)


def main() -> None:
    ensure_dirs()
    write_health("Updater started")
    while True:
        try:
            write_health("Updater idle")
            for path in queued_requests():
                write_health(f"Processing {path.stem}")
                process_request(path)
            time.sleep(HEALTH_INTERVAL)
        except KeyboardInterrupt:
            write_health("Updater stopped", healthy=False)
            raise
        except Exception as exc:
            write_health(f"Updater loop error: {exc}", healthy=False)
            time.sleep(HEALTH_INTERVAL)


if __name__ == "__main__":
    main()
