"""Durable journal for exchange orders that require manual reconciliation.

The journal deliberately stores order metadata only.  Exchange credentials are
never written to disk.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from core.config import DATA_DIR
from core.utils.datetime import utcnow

_JOURNAL_PATH = DATA_DIR / "order_reconciliation.json"
_MAX_RECORDS = 5000
_LOCK = threading.RLock()


def _load_records() -> list[dict[str, Any]]:
    if not _JOURNAL_PATH.exists():
        return []
    try:
        loaded = json.loads(_JOURNAL_PATH.read_text(encoding="utf-8"))
        return [item for item in loaded if isinstance(item, dict)] if isinstance(loaded, list) else []
    except (OSError, json.JSONDecodeError):
        logger.warning("[Reconciliation] Existing journal is unreadable; starting a new journal")
        return []


def _write_records(records: list[dict[str, Any]]) -> None:
    _atomic_write(
        _JOURNAL_PATH,
        json.dumps(records[-_MAX_RECORDS:], ensure_ascii=False, indent=2, default=str),
    )


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.debug(f"[Reconciliation] Could not remove temporary journal {temp_path}")


def record_reconciliation_issue(
    *,
    ticker: str,
    order_ids: list[str],
    operation: str,
    reason: str,
    symbol: str = "",
    exchange: str = "",
    context: dict[str, Any] | None = None,
) -> str:
    """Persist an unresolved exchange operation and return its record ID."""
    record_id = f"recon_{uuid.uuid4().hex}"
    record = {
        "id": record_id,
        "created_at": utcnow().isoformat(),
        "status": "pending",
        "operation": str(operation or "unknown"),
        "ticker": str(ticker or ""),
        "symbol": str(symbol or ""),
        "exchange": str(exchange or ""),
        "order_ids": list(dict.fromkeys(str(item) for item in order_ids if item)),
        "reason": str(reason or "exchange operation could not be confirmed"),
        "context": dict(context or {}),
    }

    try:
        with _LOCK:
            records = _load_records()
            records.append(record)
            _write_records(records)
    except OSError as exc:
        logger.error(f"[Reconciliation] Failed to persist unresolved order {record_id}: {exc}")

    return record_id


def record_order_intent(
    *,
    ticker: str,
    direction: str,
    client_order_id: str,
    idempotency_key: str,
    user_id: str | None,
    exchange: str,
    quantity: float,
) -> str:
    """Fsync an order intent before crossing the exchange boundary."""
    record_id = f"intent_{uuid.uuid4().hex}"
    record = {
        "id": record_id,
        "created_at": utcnow().isoformat(),
        "updated_at": utcnow().isoformat(),
        "status": "prepared",
        "operation": "entry_order",
        "ticker": str(ticker or ""),
        "direction": str(direction or ""),
        "client_order_id": str(client_order_id or ""),
        "idempotency_key": str(idempotency_key or ""),
        "user_id": str(user_id or ""),
        "exchange": str(exchange or ""),
        "quantity": float(quantity or 0.0),
        "result": {},
    }
    with _LOCK:
        records = _load_records()
        records.append(record)
        _write_records(records)
    return record_id


def update_order_intent(
    record_id: str,
    *,
    status: str,
    result: dict[str, Any] | None = None,
) -> None:
    """Durably advance an order intent without storing credentials."""
    with _LOCK:
        records = _load_records()
        for record in reversed(records):
            if str(record.get("id") or "") != str(record_id):
                continue
            record["status"] = str(status or "unknown")
            record["updated_at"] = utcnow().isoformat()
            if result is not None:
                allowed = {
                    "status",
                    "reason",
                    "order_id",
                    "exchange_order_id",
                    "client_order_id",
                    "exchange_order_status",
                    "filled_quantity",
                    "failure_stage",
                    "requires_reconciliation",
                    "rollback_success",
                }
                record["result"] = {key: value for key, value in result.items() if key in allowed}
            _write_records(records)
            return
        logger.error(f"[Reconciliation] Order intent {record_id} was not found")


def count_uncommitted_order_intents() -> int:
    """Return intents that crossed or may have crossed the exchange boundary."""
    with _LOCK:
        return sum(
            1
            for record in _load_records()
            if record.get("operation") == "entry_order"
            and record.get("status") in {"prepared", "exchange_result", "manual_review"}
        )


def get_uncommitted_order_intents() -> list[dict[str, Any]]:
    """Return copies of order intents that are not yet tied to committed DB rows."""
    with _LOCK:
        return [
            dict(record)
            for record in _load_records()
            if record.get("operation") == "entry_order"
            and record.get("status") in {"prepared", "exchange_result", "db_staged", "manual_review"}
        ]
