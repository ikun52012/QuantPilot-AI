#!/usr/bin/env python3
"""Host-level Docker updater sidecar for QuantPilot AI."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(os.getenv("UPDATER_BASE_DIR", "/workspace")).resolve()
DATA_DIR = BASE_DIR / "data" / "updater"
REQUEST_DIR = DATA_DIR / "requests"
STATUS_DIR = DATA_DIR / "status"
HEALTH_FILE = STATUS_DIR / "updater-health.json"
COMPOSE_FILE = BASE_DIR / "docker-compose.yml"
ENV_FILE = BASE_DIR / ".env"
COMPOSE_PROJECT = os.getenv("COMPOSE_PROJECT_NAME", "quantpilot-ai")
TARGET_SERVICE = os.getenv("UPDATE_TARGET_SERVICE", "signal-server")
TARGET_IMAGE = os.getenv("UPDATE_TARGET_IMAGE", "ghcr.io/ikun52012/quantpilot-ai:v4.5.3")
HEALTH_INTERVAL = int(os.getenv("UPDATER_HEARTBEAT_SECS", "10"))
ROLLOUT_TIMEOUT_SECS = int(os.getenv("UPDATER_ROLLOUT_TIMEOUT_SECS", "180"))
TARGET_HEALTH_URL = os.getenv("UPDATE_TARGET_HEALTH_URL", "http://127.0.0.1:8000/health")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ensure_dirs() -> None:
    REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_DIR.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def append_log(payload: dict[str, Any], message: str) -> None:
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


def run(cmd: list[str], timeout: int = 300, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({key: value for key, value in (extra_env or {}).items() if value})
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(BASE_DIR), env=env)


def _upsert_env_var(path: Path, key: str, value: str) -> None:
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    updated = False
    prefix = f"{key}="
    next_lines: list[str] = []
    for line in lines:
        if line.startswith(prefix):
            next_lines.append(f"{key}={value}")
            updated = True
        else:
            next_lines.append(line)
    if not updated:
        next_lines.append(f"{key}={value}")
    path.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")


def persist_target_images(target_image: str, target_updater_image: str = "") -> None:
    _upsert_env_var(ENV_FILE, "SIGNAL_SERVER_IMAGE", target_image)
    if target_updater_image:
        _upsert_env_var(ENV_FILE, "SIGNAL_UPDATER_IMAGE", target_updater_image)


def queued_requests() -> list[Path]:
    ensure_dirs()
    return sorted(REQUEST_DIR.glob("upd_*.json"), key=lambda item: item.stat().st_mtime)


def _safe_task_id(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", str(value))
    return safe[:64] or "unknown"


def process_request(path: Path) -> None:
    request = read_json(path)
    if not request:
        path.unlink(missing_ok=True)
        return

    task_id = _safe_task_id(str(request.get("task_id") or path.stem))
    status_path = STATUS_DIR / f"{task_id}.json"
    payload = read_json(status_path) or request
    payload["task_id"] = task_id
    payload["status"] = "running"
    payload["updated_at"] = now_iso()
    write_json(status_path, payload)

    try:
        target_image = str(payload.get("target_image") or TARGET_IMAGE).strip()
        target_version = str(payload.get("target_version") or "").strip().lstrip("v")
        target_updater_image = str(payload.get("target_updater_image") or "").strip()
        if not target_image or target_image.endswith(":latest"):
            append_log(payload, f"Refusing unsafe target image: {target_image or '<empty>'}")
            payload["status"] = "failed"
            payload["message"] = "Updater requires an explicit image tag"
            write_json(status_path, payload)
            return

        persist_target_images(target_image, target_updater_image)
        append_log(payload, f"Persisted SIGNAL_SERVER_IMAGE={target_image} to .env.")
        if target_updater_image:
            append_log(payload, f"Persisted SIGNAL_UPDATER_IMAGE={target_updater_image} to .env.")

        append_log(payload, f"Pulling image {target_image}.")
        write_json(status_path, payload)
        pull_result = run(["docker", "pull", target_image], timeout=600)
        if pull_result.returncode != 0:
            append_log(payload, f"docker pull failed: {(pull_result.stderr or pull_result.stdout).strip()}")
            payload["status"] = "failed"
            payload["message"] = "docker pull failed"
            write_json(status_path, payload)
            return

        append_log(payload, f"Restarting service {TARGET_SERVICE} with image {target_image}.")
        write_json(status_path, payload)
        up_result = run(
            compose_cmd("up", "-d", "--no-deps", TARGET_SERVICE),
            timeout=600,
            extra_env={
                "SIGNAL_SERVER_IMAGE": target_image,
                "SIGNAL_UPDATER_IMAGE": target_updater_image,
            },
        )
        if up_result.returncode != 0:
            append_log(payload, f"docker compose up failed: {(up_result.stderr or up_result.stdout).strip()}")
            payload["status"] = "failed"
            payload["message"] = "docker compose up failed"
            write_json(status_path, payload)
            return

        rollout = wait_for_rollout(target_version, target_image)
        payload.update({
            "observed_version": rollout.get("observed_version", ""),
            "target_image": target_image,
        })
        if rollout.get("status") != "completed":
            append_log(payload, rollout.get("message", "Rollout verification failed"))
            payload["status"] = "failed"
            payload["message"] = rollout.get("message", "Rollout verification failed")
            write_json(status_path, payload)
            return

        append_log(payload, rollout.get("message", "Update rollout completed."))
        if rollout.get("service_status"):
            append_log(payload, f"Service status: {str(rollout['service_status'])[:500]}")
        payload["status"] = "completed"
        payload["message"] = f"Updated service {TARGET_SERVICE} to {target_image}"
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


def _service_status() -> dict[str, Any]:
    ps_result = run(compose_cmd("ps", TARGET_SERVICE, "--format", "json"), timeout=60)
    raw = (ps_result.stdout or ps_result.stderr or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed[0] if parsed and isinstance(parsed[0], dict) else {}
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    return {"raw": raw}


def _health_payload() -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(TARGET_HEALTH_URL, timeout=5) as response:
            data = response.read().decode("utf-8", errors="ignore")
        payload = json.loads(data)
        return payload if isinstance(payload, dict) else None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return None
    except Exception:
        return None


def wait_for_rollout(target_version: str, target_image: str) -> dict[str, Any]:
    deadline = time.time() + max(30, ROLLOUT_TIMEOUT_SECS)
    last_status: dict[str, Any] = {}
    observed_version = ""

    while time.time() < deadline:
        service_status = _service_status()
        last_status = service_status or last_status
        state = str(service_status.get("State") or service_status.get("state") or service_status.get("Status") or service_status.get("raw") or "").lower()
        if "running" not in state:
            time.sleep(5)
            continue

        health = _health_payload() or {}
        observed_version = str(health.get("version") or "").strip()
        if str(health.get("status") or "").lower() != "healthy":
            time.sleep(5)
            continue
        if target_version and observed_version and observed_version != target_version:
            time.sleep(5)
            continue

        return {
            "status": "completed",
            "message": f"Update rollout completed with healthy service on {target_image}.",
            "service_status": service_status,
            "observed_version": observed_version,
        }

    return {
        "status": "failed",
        "message": f"Timed out waiting for healthy {TARGET_SERVICE} rollout.",
        "service_status": last_status,
        "observed_version": observed_version,
    }


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
