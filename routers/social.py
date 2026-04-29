"""
Social Signals Router - Community signal sharing.
Allows users to share and subscribe to trading signals.
"""
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user
from core.database import SharedSignalModel, SignalSubscriptionModel, get_db
from core.utils.datetime import utcnow

router = APIRouter(prefix="/api/social", tags=["Social Signals"])


class SharedSignal(BaseModel):
    ticker: str
    direction: str
    entry_price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    reason: str = ""
    confidence: float = 0.0
    strategy_name: str = ""


class SignalSubscription(BaseModel):
    signal_id: str
    auto_execute: bool = False
    max_position_pct: float = 10.0


_SHARED_SIGNALS = {}
_SIGNAL_SUBSCRIPTIONS = {}
_USER_FOLLOWERS: dict[str, list[str]] = {}
_SIGNAL_STATS = {}


def _user_id(user: dict) -> str:
    return str(user.get("sub") or user.get("id") or "")


def _loads_dict(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _stats_default() -> dict:
    return {
        "views": 0,
        "subscriptions": 0,
        "executions": 0,
        "successful": 0,
        "failed": 0,
    }


def _signal_to_dict(row: SharedSignalModel) -> dict:
    return {
        "signal_id": row.id,
        "ticker": row.ticker,
        "direction": row.direction,
        "entry_price": row.entry_price,
        "stop_loss": row.stop_loss,
        "take_profit": row.take_profit,
        "reason": row.reason,
        "confidence": row.confidence,
        "strategy_name": row.strategy_name,
        "user_id": row.user_id,
        "username": row.username or "Anonymous",
        "shared_at": row.created_at.isoformat() if row.created_at else None,
        "status": row.status,
        "subscribers_count": row.subscribers_count or 0,
        "executions_count": row.executions_count or 0,
        "success_rate": row.success_rate or 0.0,
        "is_private": bool(row.is_private),
    }


def _subscription_to_dict(row: SignalSubscriptionModel) -> dict:
    return {
        "subscription_id": row.id,
        "user_id": row.user_id,
        "signal_id": row.signal_id,
        "auto_execute": bool(row.auto_execute),
        "max_position_pct": row.max_position_pct,
        "subscribed_at": row.created_at.isoformat() if row.created_at else None,
    }


async def _get_signal_or_404(db: AsyncSession, signal_id: str) -> SharedSignalModel:
    row = await db.get(SharedSignalModel, signal_id)
    if not row:
        raise HTTPException(404, f"Signal {signal_id} not found")
    return row


@router.post("/share")
async def share_signal(
    signal: SharedSignal,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Share a trading signal to community."""
    user_id = _user_id(user)
    signal_id = f"sig_{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{user_id}"

    shared = {
        "signal_id": signal_id,
        "ticker": signal.ticker,
        "direction": signal.direction,
        "entry_price": signal.entry_price,
        "stop_loss": signal.stop_loss,
        "take_profit": signal.take_profit,
        "reason": signal.reason,
        "confidence": signal.confidence,
        "strategy_name": signal.strategy_name,
        "user_id": user_id,
        "username": user.get("username", "Anonymous"),
        "shared_at": datetime.now(timezone.utc).isoformat(),
        "status": "active",
        "subscribers_count": 0,
        "executions_count": 0,
        "success_rate": 0.0,
        "is_private": False,
    }

    _SHARED_SIGNALS[signal_id] = shared
    _SIGNAL_STATS[signal_id] = {
        "views": 0,
        "subscriptions": 0,
        "executions": 0,
        "successful": 0,
        "failed": 0,
    }

    db.add(SharedSignalModel(
        id=signal_id,
        user_id=user_id,
        username=user.get("username", "Anonymous"),
        ticker=signal.ticker,
        direction=signal.direction,
        entry_price=signal.entry_price,
        stop_loss=signal.stop_loss,
        take_profit=signal.take_profit,
        reason=signal.reason,
        confidence=signal.confidence,
        strategy_name=signal.strategy_name,
        status="active",
        stats_json=json.dumps(_SIGNAL_STATS[signal_id], ensure_ascii=False),
    ))
    await db.flush()

    logger.info(f"[Social] Signal {signal_id} shared by {user.get('username')}")

    return {
        "status": "shared",
        "signal_id": signal_id,
        "ticker": signal.ticker,
        "direction": signal.direction,
    }


@router.get("/list")
async def list_shared_signals(
    ticker: str | None = None,
    direction: str | None = None,
    limit: int = 50,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List shared signals from community."""
    query = select(SharedSignalModel).where(SharedSignalModel.status == "active")
    if ticker:
        query = query.where(SharedSignalModel.ticker == ticker)
    if direction:
        query = query.where(SharedSignalModel.direction == direction)

    query = query.order_by(SharedSignalModel.created_at.desc()).limit(max(1, min(limit, 200)))
    result = await db.execute(query)
    signals = [_signal_to_dict(row) for row in result.scalars().all()]

    return {
        "signals": signals,
        "total": len(signals),
        "filter": {"ticker": ticker, "direction": direction},
    }


@router.get("/signal/{signal_id}")
async def get_shared_signal(
    signal_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get details of a shared signal."""
    signal = await _get_signal_or_404(db, signal_id)
    stats = _loads_dict(signal.stats_json) or _stats_default()
    stats["views"] = int(stats.get("views") or 0) + 1
    signal.stats_json = json.dumps(stats, ensure_ascii=False)
    signal.updated_at = utcnow()

    return {
        "signal": _signal_to_dict(signal),
        "stats": stats,
    }


@router.post("/subscribe/{signal_id}")
async def subscribe_to_signal(
    signal_id: str,
    subscription: SignalSubscription,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Subscribe to a shared signal."""
    signal = await _get_signal_or_404(db, signal_id)
    user_id = _user_id(user)
    sub_id = f"sub_{user_id}_{signal_id}"
    existing = await db.get(SignalSubscriptionModel, sub_id)
    if existing:
        return {
            "status": "subscribed",
            "subscription_id": sub_id,
            "signal_id": signal_id,
            "auto_execute": existing.auto_execute,
        }

    _SIGNAL_SUBSCRIPTIONS[sub_id] = {
        "subscription_id": sub_id,
        "user_id": user_id,
        "signal_id": signal_id,
        "auto_execute": subscription.auto_execute,
        "max_position_pct": subscription.max_position_pct,
        "subscribed_at": datetime.now(timezone.utc).isoformat(),
    }

    stats = _loads_dict(signal.stats_json) or _stats_default()
    stats["subscriptions"] = int(stats.get("subscriptions") or 0) + 1
    signal.stats_json = json.dumps(stats, ensure_ascii=False)
    signal.subscribers_count = int(signal.subscribers_count or 0) + 1
    signal.updated_at = utcnow()
    db.add(SignalSubscriptionModel(
        id=sub_id,
        user_id=user_id,
        signal_id=signal_id,
        auto_execute=subscription.auto_execute,
        max_position_pct=subscription.max_position_pct,
    ))
    await db.flush()

    author_id = signal.user_id
    if author_id not in _USER_FOLLOWERS:
        _USER_FOLLOWERS[author_id] = []
    if user_id not in _USER_FOLLOWERS[author_id]:
        _USER_FOLLOWERS[author_id].append(user_id)

    return {
        "status": "subscribed",
        "subscription_id": sub_id,
        "signal_id": signal_id,
        "auto_execute": subscription.auto_execute,
    }


@router.delete("/unsubscribe/{signal_id}")
async def unsubscribe_from_signal(
    signal_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Unsubscribe from a signal."""
    sub_id = f"sub_{_user_id(user)}_{signal_id}"

    subscription = await db.get(SignalSubscriptionModel, sub_id)
    if not subscription:
        raise HTTPException(404, "Subscription not found")

    _SIGNAL_SUBSCRIPTIONS.pop(sub_id, None)
    await db.delete(subscription)

    signal = await db.get(SharedSignalModel, signal_id)
    if signal:
        signal.subscribers_count = max(0, int(signal.subscribers_count or 0) - 1)
        stats = _loads_dict(signal.stats_json) or _stats_default()
        stats["subscriptions"] = max(0, int(stats.get("subscriptions") or 0) - 1)
        signal.stats_json = json.dumps(stats, ensure_ascii=False)
        signal.updated_at = utcnow()

    return {"status": "unsubscribed", "signal_id": signal_id}


@router.get("/subscriptions")
async def list_my_subscriptions(
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List my signal subscriptions."""
    result = await db.execute(
        select(SignalSubscriptionModel)
        .where(SignalSubscriptionModel.user_id == _user_id(user))
        .order_by(SignalSubscriptionModel.created_at.desc())
    )
    user_subs = [_subscription_to_dict(row) for row in result.scalars().all()]

    return {
        "subscriptions": user_subs,
        "count": len(user_subs),
    }


@router.get("/leaderboard")
async def get_signal_leaderboard(
    timeframe: str = "week",
    limit: int = 20,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get top performers leaderboard."""
    leaderboard = []

    result = await db.execute(
        select(SharedSignalModel)
        .where(SharedSignalModel.status == "active")
        .order_by(SharedSignalModel.success_rate.desc(), SharedSignalModel.executions_count.desc())
        .limit(max(1, min(limit * 3, 200)))
    )
    for signal in result.scalars().all():
        stats = _loads_dict(signal.stats_json) or _stats_default()

        success_rate = stats.get("successful", 0) / max(stats.get("executions", 1), 1) * 100

        leaderboard.append({
            "signal_id": signal.id,
            "username": signal.username,
            "ticker": signal.ticker,
            "subscribers": signal.subscribers_count,
            "executions": stats.get("executions"),
            "success_rate": round(success_rate, 2),
        })

    leaderboard.sort(key=lambda x: x.get("success_rate", 0), reverse=True)

    return {
        "leaderboard": leaderboard[:limit],
        "timeframe": timeframe,
    }


@router.get("/user/{username}/signals")
async def get_user_shared_signals(
    username: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get signals shared by a specific user."""
    result = await db.execute(
        select(SharedSignalModel)
        .where(SharedSignalModel.username == username)
        .order_by(SharedSignalModel.created_at.desc())
    )
    rows = result.scalars().all()
    user_signals = [_signal_to_dict(row) for row in rows]
    author_id = str(user_signals[0].get("user_id") or "") if user_signals else ""

    return {
        "username": username,
        "signals": user_signals,
        "count": len(user_signals),
        "followers": len(_USER_FOLLOWERS.get(author_id, [])),
    }


@router.post("/signal/{signal_id}/feedback")
async def provide_signal_feedback(
    signal_id: str,
    success: bool,
    pnl_pct: float = 0.0,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Provide feedback on a signal execution."""
    signal = await _get_signal_or_404(db, signal_id)
    stats = _loads_dict(signal.stats_json) or _stats_default()

    stats["executions"] = int(stats.get("executions") or 0) + 1
    if success:
        stats["successful"] = int(stats.get("successful") or 0) + 1
    else:
        stats["failed"] = int(stats.get("failed") or 0) + 1

    signal.executions_count = stats["executions"]
    signal.success_rate = round(stats["successful"] / max(stats["executions"], 1) * 100, 2)
    signal.stats_json = json.dumps(stats, ensure_ascii=False)
    signal.updated_at = utcnow()

    return {
        "status": "recorded",
        "signal_id": signal_id,
        "success": success,
        "new_success_rate": signal.success_rate,
    }


@router.post("/follow/{username}")
async def follow_user(
    username: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Follow a signal provider."""
    result = await db.execute(
        select(SharedSignalModel)
        .where(SharedSignalModel.username == username)
        .order_by(SharedSignalModel.created_at.desc())
        .limit(1)
    )
    target_signal = result.scalar_one_or_none()

    if not target_signal:
        raise HTTPException(404, f"User {username} not found or has no signals")

    target_user_id = target_signal.user_id
    user_id = _user_id(user)

    if target_user_id not in _USER_FOLLOWERS:
        _USER_FOLLOWERS[target_user_id] = []

    if user_id not in _USER_FOLLOWERS[target_user_id]:
        _USER_FOLLOWERS[target_user_id].append(user_id)

    return {
        "status": "following",
        "username": username,
        "followers_count": len(_USER_FOLLOWERS[target_user_id]),
    }


@router.delete("/unfollow/{username}")
async def unfollow_user(
    username: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Unfollow a signal provider."""
    result = await db.execute(
        select(SharedSignalModel)
        .where(SharedSignalModel.username == username)
        .order_by(SharedSignalModel.created_at.desc())
        .limit(1)
    )
    target_signal = result.scalar_one_or_none()

    if not target_signal:
        return {"status": "not_following", "username": username}

    target_user_id = target_signal.user_id
    user_id = _user_id(user)

    if target_user_id in _USER_FOLLOWERS and user_id in _USER_FOLLOWERS[target_user_id]:
        _USER_FOLLOWERS[target_user_id].remove(user_id)

    return {"status": "unfollowed", "username": username}


@router.get("/following")
async def list_following(
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List users I'm following."""
    following = []
    user_id = _user_id(user)

    for author_id, followers in _USER_FOLLOWERS.items():
        if user_id in followers:
            result = await db.execute(
                select(SharedSignalModel)
                .where(SharedSignalModel.user_id == author_id)
                .order_by(SharedSignalModel.created_at.desc())
            )
            author_signals = list(result.scalars().all())
            if author_signals:
                following.append({
                    "username": author_signals[0].username,
                    "signals_count": len(author_signals),
                    "latest_signal": author_signals[0].created_at.isoformat() if author_signals[0].created_at else None,
                })

    return {
        "following": following,
        "count": len(following),
    }


@router.get("/stats")
async def get_social_stats(
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get overall social signal statistics."""
    total_signals = int((await db.execute(select(func.count()).select_from(SharedSignalModel))).scalar() or 0)
    total_subscriptions = int((await db.execute(select(func.count()).select_from(SignalSubscriptionModel))).scalar() or 0)
    return {
        "total_signals": total_signals,
        "total_subscriptions": total_subscriptions,
        "active_users": len(_USER_FOLLOWERS),
        "top_tickers": await _get_top_tickers(db),
    }


async def _get_top_tickers(db: AsyncSession) -> list[dict]:
    result = await db.execute(
        select(SharedSignalModel.ticker, func.count())
        .group_by(SharedSignalModel.ticker)
        .order_by(func.count().desc())
        .limit(10)
    )
    return [{"ticker": ticker, "signals": count} for ticker, count in result.all()]
