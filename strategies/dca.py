"""
DCA (Dollar Cost Average) Strategy Engine.
Manages position averaging down/up with configurable parameters.
Enhanced with live exchange execution support.
"""
import asyncio
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum

from loguru import logger

from core.config import settings
from core.redis_coordination import (
    distributed_lock,
    make_key,
    redis_hdel,
    redis_hget_json,
    redis_hgetall_json,
    redis_hset_json,
)
from core.utils.datetime import utcnow
from models import SignalDirection, TradeDecision

_DCA_STATE_HASH = make_key("strategy", "dca", "state")
_DCA_ACTIVE_HASH = make_key("strategy", "dca", "active")


def _parse_datetime(value):
    if isinstance(value, datetime) or value is None:
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return utcnow()


def _filter_dataclass(cls, data: dict) -> dict:
    fields = getattr(cls, "__dataclass_fields__", {})
    return {key: value for key, value in dict(data or {}).items() if key in fields}


class DCAMode(Enum):
    AVERAGE_DOWN = "average_down"
    AVERAGE_UP = "average_up"
    BOTH = "both"


class DCASizingMethod(Enum):
    FIXED = "fixed"
    MARTINGALE = "martingale"
    GEOMETRIC = "geometric"
    FIBONACCI = "fibonacci"


@dataclass
class DCAConfig:
    ticker: str = "BTCUSDT"
    direction: str = "long"
    initial_entry_price: float = 0.0
    initial_quantity: float = 0.0
    initial_capital_usdt: float = 1000.0
    max_entries: int = 5
    entry_spacing_pct: float = 2.0
    sizing_method: str = "fixed"
    sizing_multiplier: float = 1.5
    fixed_size_usdt: float = 200.0
    stop_loss_pct: float = 10.0
    take_profit_pct: float = 5.0
    take_profit_on_avg_entry: bool = True
    trailing_stop_pct: float = 0.0
    cooldown_minutes: int = 60
    max_total_capital_usdt: float = 5000.0
    max_single_entry_usdt: float = 0.0
    leverage: float = 1.0
    fee_pct: float = 0.04
    mode: str = "average_down"
    activation_loss_pct: float = 1.0
    strategy_id: str = ""
    user_id: str = ""
    enabled: bool = True
    auto_start: bool = False
    paper_mode: bool = True


@dataclass
class DCAEntry:
    entry_price: float
    quantity: float
    capital_usdt: float
    entry_time: datetime
    entry_idx: int
    order_id: str = ""
    reason: str = ""
    fees_usdt: float = 0.0


@dataclass
class DCAPosition:
    config_id: str
    ticker: str
    direction: str
    entries: list[DCAEntry] = field(default_factory=list)
    total_quantity: float = 0.0
    total_capital_usdt: float = 0.0
    average_entry_price: float = 0.0
    current_price: float = 0.0
    unrealized_pnl_usdt: float = 0.0
    unrealized_pnl_pct: float = 0.0
    realized_pnl_usdt: float = 0.0
    stop_loss_price: float = 0.0
    take_profit_price: float = 0.0
    status: str = "active"
    next_entry_price: float = 0.0
    next_entry_trigger_pct: float = 0.0
    entries_remaining: int = 0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    started_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    closed_at: datetime | None = None
    close_reason: str = ""
    stop_loss_order_id: str = ""
    take_profit_order_id: str = ""
    stop_loss_order_ids: list[str] = field(default_factory=list)
    take_profit_order_ids: list[str] = field(default_factory=list)
    cleanup_errors: list[dict] = field(default_factory=list)
    close_price: float = 0.0


class DCAEngine:
    def __init__(self):
        self.positions: dict[str, DCAPosition] = {}
        self.configs: dict[str, DCAConfig] = {}
        self.price_cache: dict[str, float] = {}
        self._monitor_task: asyncio.Task | None = None

    def _state_record(self, position_id: str) -> dict | None:
        position = self.positions.get(position_id)
        config = self.configs.get(position_id)
        if not position or not config:
            return None
        return {
            "strategy_type": "dca",
            "strategy_id": position_id,
            "ticker": position.ticker,
            "user_id": config.user_id,
            "status": position.status,
            "config": asdict(config),
            "position": asdict(position),
            "updated_at": utcnow().isoformat(),
        }

    def _restore_state_record(self, record: dict) -> bool:
        try:
            config_data = _filter_dataclass(DCAConfig, record.get("config") or {})
            position_data = _filter_dataclass(DCAPosition, record.get("position") or {})
            entries = []
            for raw_entry in position_data.get("entries", []):
                entry_data = _filter_dataclass(DCAEntry, dict(raw_entry or {}))
                entry_data["entry_time"] = _parse_datetime(entry_data.get("entry_time"))
                entries.append(DCAEntry(**entry_data))

            position_data["entries"] = entries
            for key in ("started_at", "updated_at", "closed_at"):
                if key in position_data:
                    position_data[key] = _parse_datetime(position_data.get(key))

            config = DCAConfig(**config_data)
            position = DCAPosition(**position_data)
            if position.stop_loss_order_id and position.stop_loss_order_id not in position.stop_loss_order_ids:
                position.stop_loss_order_ids.append(position.stop_loss_order_id)
            if position.take_profit_order_id and position.take_profit_order_id not in position.take_profit_order_ids:
                position.take_profit_order_ids.append(position.take_profit_order_id)
            position_id = str(record.get("strategy_id") or position.config_id or config.strategy_id)
            if not position_id:
                return False
            config.strategy_id = position_id
            position.config_id = position_id
            self.configs[position_id] = config
            self.positions[position_id] = position
            return True
        except Exception as exc:
            logger.warning(f"[DCA/Redis] Failed to restore Redis state: {exc}")
            return False

    def _position_lock_name(self, position_id: str, position: DCAPosition, config: DCAConfig | None) -> str:
        owner = (config.user_id if config else "") or "global"
        return f"dca:{owner}:{position.ticker}"

    def _schedule_state_sync(self, position_id: str) -> None:
        if not settings.redis.enabled:
            return
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self.sync_position_state(position_id))
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() and t.exception() else None)
        except RuntimeError:
            logger.warning("[DCA] Cannot sync state: no running event loop")

    async def sync_position_state(self, position_id: str) -> bool:
        """Persist latest local DCA state into Redis hashes."""
        record = self._state_record(position_id)
        if not record:
            return False

        saved = await redis_hset_json(_DCA_STATE_HASH, position_id, record)
        if record["status"] in {"active", "cleanup_required"}:
            saved = await redis_hset_json(_DCA_ACTIVE_HASH, position_id, record) or saved
        else:
            await redis_hdel(_DCA_ACTIVE_HASH, position_id)
        return saved

    async def load_position_state(self, position_id: str, *, refresh: bool = False) -> bool:
        """Load a DCA position from Redis if local state is missing or stale."""
        if not refresh and position_id in self.positions and position_id in self.configs:
            return True
        record = await redis_hget_json(_DCA_ACTIVE_HASH, position_id)
        if record is None:
            record = await redis_hget_json(_DCA_STATE_HASH, position_id)
        if not isinstance(record, dict):
            return False
        return self._restore_state_record(record)

    async def refresh_active_from_redis(self) -> int:
        """Hydrate all Redis active DCA positions into this process."""
        records = await redis_hgetall_json(_DCA_ACTIVE_HASH)
        restored = 0
        for record in records.values():
            if isinstance(record, dict) and self._restore_state_record(record):
                restored += 1
        return restored

    def _ensure_strategy_id(self, config: DCAConfig) -> None:
        if not config.strategy_id:
            # BUG FIX: Use UTC time to avoid timezone collisions
            config.strategy_id = f"dca_{config.ticker}_{utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"

    def _build_position(self, config: DCAConfig) -> DCAPosition:
        return DCAPosition(
            config_id=config.strategy_id,
            ticker=config.ticker,
            direction=config.direction,
            entries_remaining=config.max_entries - 1,
            started_at=utcnow(),
        )

    def _finalize_position(
        self,
        position: DCAPosition,
        config: DCAConfig,
        entry: DCAEntry,
        current_price: float,
    ) -> DCAPosition:
        position.entries.append(entry)
        position.total_quantity = entry.quantity
        position.total_capital_usdt = entry.capital_usdt
        position.average_entry_price = entry.entry_price
        position.current_price = current_price
        position.highest_price = current_price
        position.lowest_price = current_price

        self._update_stop_take(position, config)
        self._calculate_next_entry(position, config)

        self.positions[config.strategy_id] = position
        self.configs[config.strategy_id] = config

        logger.info(f"[DCA] Created position for {config.ticker}: entry={entry.entry_price}, qty={entry.quantity}")

        return position

    def _create_position_paper(self, config: DCAConfig, current_price: float) -> DCAPosition:
        self._ensure_strategy_id(config)
        position = self._build_position(config)
        initial_qty = self._calculate_initial_quantity(config, current_price)
        initial_capital = initial_qty * current_price
        entry = DCAEntry(
            entry_price=current_price,
            quantity=initial_qty,
            capital_usdt=initial_capital,
            entry_time=utcnow(),
            entry_idx=1,
            reason="initial_entry_paper",
            fees_usdt=initial_capital * config.fee_pct / 100,
        )
        logger.info("[DCA] Paper mode - simulated initial entry")
        return self._finalize_position(position, config, entry, current_price)

    def create_position(
        self,
        config: DCAConfig,
        current_price: float,
        exchange_config: dict | None = None,
    ) -> DCAPosition:
        """Synchronous wrapper for creating a DCA position.

        P2-FIX: Removed asyncio.run() to avoid event loop conflicts.
        Use create_position_async() instead for live exchange execution.
        """
        if config.paper_mode:
            position = self._create_position_paper(config, current_price)
            self._schedule_state_sync(position.config_id)
            return position

        # P2-FIX: Do not attempt to create a new event loop here.
        # This can cause RuntimeError in environments with existing event loops
        # (e.g., APScheduler async jobs, Jupyter notebooks, etc.)
        raise RuntimeError(
            "Live DCA position creation requires an async context. "
            "Use create_position_async() instead of create_position() for live exchange execution."
        )

    async def create_position_async(
        self,
        config: DCAConfig,
        current_price: float,
        exchange_config: dict | None = None
    ) -> DCAPosition:
        if config.paper_mode:
            position = self._create_position_paper(config, current_price)
            await self.sync_position_state(position.config_id)
            return position

        self._ensure_strategy_id(config)
        async with distributed_lock(
            f"dca:create:{config.user_id or 'global'}:{config.ticker}:{config.direction}",
            ttl_seconds=300,
            allow_local_fallback=config.paper_mode,
        ):
            position = self._build_position(config)

            initial_qty = self._calculate_initial_quantity(config, current_price)

            # Pre-validate size and cost against exchange limits
            exchange_name = str((exchange_config or {}).get("name") or settings.exchange.name or "").lower().strip()
            market_type = str((exchange_config or {}).get("market_type") or settings.exchange.market_type or "contract").lower().strip()
            try:
                from exchange import get_market_limits
                limits = await asyncio.to_thread(get_market_limits, exchange_name, config.ticker, market_type)
                if limits:
                    min_amount = float(limits.get("min_amount") or 0.0)
                    min_cost = float(limits.get("min_cost") or 0.0)
                    contract_size = float(limits.get("contract_size") or 1.0)
                    initial_cost = initial_qty * current_price * contract_size
                    if min_amount > 0 and initial_qty < min_amount:
                        logger.warning(f"[DCA] Initial quantity {initial_qty} below exchange minimum {min_amount} for {config.ticker}")
                        raise ValueError(f"size_below_exchange_minimum: {initial_qty} < {min_amount}")
                    if min_cost > 0 and initial_cost < min_cost:
                        logger.warning(f"[DCA] Initial cost {initial_cost} below exchange minimum {min_cost} for {config.ticker}")
                        raise ValueError(f"cost_below_exchange_minimum: {initial_cost} < {min_cost}")
            except ValueError:
                raise
            except Exception as e:
                logger.warning(f"[DCA] Exchange limit pre-validation check failed or skipped: {e}")

            try:
                from exchange import execute_trade

                direction = SignalDirection.LONG if config.direction == "long" else SignalDirection.SHORT
                decision = TradeDecision(
                    execute=True,
                    direction=direction,
                    ticker=config.ticker,
                    entry_price=current_price,
                    quantity=initial_qty,
                    stop_loss=self._calculate_stop_loss_price(config, current_price, direction),
                    take_profit=self._calculate_take_profit_price(config, current_price, direction),
                    reason="DCA initial entry",
                    order_type="market",
                    idempotency_key=f"dca:{config.strategy_id}:entry:1",
                )

                order_result = await execute_trade(decision, exchange_config)

                if order_result.get("status") in ["filled", "partial", "simulated"]:
                    filled_price = float(order_result.get("entry_price") or current_price)
                    filled_qty = float(order_result.get("filled_quantity") or order_result.get("quantity") or initial_qty)
                    if filled_qty <= 0:
                        raise Exception("Exchange returned zero filled quantity")
                    filled_capital = filled_qty * filled_price
                    entry = DCAEntry(
                        entry_price=filled_price,
                        quantity=filled_qty,
                        capital_usdt=filled_capital,
                        entry_time=utcnow(),
                        entry_idx=1,
                        reason="initial_entry",
                        order_id=order_result.get("order_id", ""),
                        fees_usdt=filled_capital * config.fee_pct / 100,
                    )
                    position.stop_loss_order_id = str(order_result.get("stop_loss_order_id") or "")
                    position.take_profit_order_id = str(order_result.get("take_profit_order_id") or "")
                    position.stop_loss_order_ids = [position.stop_loss_order_id] if position.stop_loss_order_id else []
                    position.take_profit_order_ids = [position.take_profit_order_id] if position.take_profit_order_id else []
                    logger.info(f"[DCA] Placed initial order: {order_result.get('order_id')}")
                else:
                    logger.error(f"[DCA] Failed to place initial order: {order_result}")
                    raise Exception(f"Failed to place initial order: {order_result.get('reason')}")

            except Exception as e:
                logger.error(f"[DCA] Exchange execution failed: {e}")
                raise

            finalized = self._finalize_position(position, config, entry, current_price)
            await self.sync_position_state(finalized.config_id)
            return finalized

    def _calculate_initial_quantity(self, config: DCAConfig, price: float) -> float:
        if price <= 0:
            return 0.0
        if config.initial_capital_usdt > 0:
            return config.initial_capital_usdt / price
        elif config.fixed_size_usdt > 0:
            return config.fixed_size_usdt / price
        return 0.0

    def _calculate_stop_loss_price(self, config: DCAConfig, entry_price: float, direction: SignalDirection) -> float | None:
        if config.stop_loss_pct <= 0:
            return None
        if direction == SignalDirection.LONG:
            return entry_price * (1 - config.stop_loss_pct / 100)
        else:
            return entry_price * (1 + config.stop_loss_pct / 100)

    def _calculate_take_profit_price(self, config: DCAConfig, entry_price: float, direction: SignalDirection) -> float | None:
        if config.take_profit_pct <= 0:
            return None
        if direction == SignalDirection.LONG:
            return entry_price * (1 + config.take_profit_pct / 100)
        else:
            return entry_price * (1 - config.take_profit_pct / 100)

    def _calculate_next_entry_quantity(self, config: DCAConfig, entry_idx: int, base_quantity: float, position: DCAPosition | None = None, entry_price: float = 0.0) -> float:
        method = config.sizing_method

        if method == "fixed":
            quantity = base_quantity

        elif method == "martingale":
            quantity = base_quantity * (config.sizing_multiplier ** (entry_idx - 1))

        elif method == "geometric":
            quantity = base_quantity * entry_idx

        elif method == "fibonacci":
            fib = [1, 1, 2, 3, 5, 8, 13, 21]
            idx = min(entry_idx - 1, len(fib) - 1)
            quantity = base_quantity * fib[idx]

        else:
            quantity = base_quantity

        if config.max_total_capital_usdt > 0 and position is not None:
            remaining_capital = config.max_total_capital_usdt - position.total_capital_usdt
            if entry_price <= 0:
                return 0.0
            per_entry_cap = config.max_single_entry_usdt
            if per_entry_cap <= 0 and config.max_entries > 0:
                per_entry_cap = config.max_total_capital_usdt / config.max_entries
            if per_entry_cap > 0 and quantity * entry_price > per_entry_cap:
                quantity = per_entry_cap / entry_price
            if quantity * entry_price > remaining_capital:
                quantity = remaining_capital / entry_price
                quantity = max(0, quantity)

        return quantity

    def _calculate_next_entry(self, position: DCAPosition, config: DCAConfig) -> None:
        if position.entries_remaining <= 0:
            position.next_entry_price = 0.0
            position.next_entry_trigger_pct = 0.0
            return

        spacing_pct = config.entry_spacing_pct

        if config.mode == "average_down":
            if position.direction == "long":
                position.next_entry_price = position.average_entry_price * (1 - spacing_pct / 100)
            else:
                position.next_entry_price = position.average_entry_price * (1 + spacing_pct / 100)

        elif config.mode == "average_up":
            if position.direction == "long":
                position.next_entry_price = position.average_entry_price * (1 + spacing_pct / 100)
            else:
                position.next_entry_price = position.average_entry_price * (1 - spacing_pct / 100)

        if position.direction == "long":
            position.next_entry_trigger_pct = abs(position.average_entry_price - position.next_entry_price) / position.average_entry_price * 100
        else:
            position.next_entry_trigger_pct = abs(position.next_entry_price - position.average_entry_price) / position.average_entry_price * 100

    def _update_stop_take(self, position: DCAPosition, config: DCAConfig) -> None:
        avg_entry = position.average_entry_price

        if config.stop_loss_pct > 0:
            if position.direction == "long":
                position.stop_loss_price = avg_entry * (1 - config.stop_loss_pct / 100)
            else:
                position.stop_loss_price = avg_entry * (1 + config.stop_loss_pct / 100)

        if config.take_profit_pct > 0:
            if position.direction == "long":
                position.take_profit_price = avg_entry * (1 + config.take_profit_pct / 100)
            else:
                position.take_profit_price = avg_entry * (1 - config.take_profit_pct / 100)

    async def check_and_execute(self, position_id: str, current_price: float, exchange_config: dict | None = None) -> dict:
        result: dict[str, object] = {"action": "none", "reason": ""}

        if position_id not in self.positions:
            await self.load_position_state(position_id)

        if position_id not in self.positions:
            return {"action": "error", "reason": "Position not found"}

        position = self.positions[position_id]
        config = self.configs.get(position_id)

        if not config:
            return {"action": "error", "reason": "Config not found"}

        async with distributed_lock(
            self._position_lock_name(position_id, position, config),
            ttl_seconds=300,
            allow_local_fallback=config.paper_mode,
        ):
            await self.load_position_state(position_id, refresh=True)
            position = self.positions[position_id]
            config = self.configs.get(position_id)
            if not config:
                return {"action": "error", "reason": "Config not found"}

            position.current_price = current_price
            position.highest_price = max(position.highest_price, current_price)
            position.lowest_price = min(position.lowest_price, current_price)

            self._update_pnl(position, config)

            if position.status == "cleanup_required":
                await self._close_position(
                    position_id,
                    position.close_price or current_price,
                    position.close_reason or "cleanup_retry",
                    exchange_config,
                )
                return {
                    "action": "cleanup",
                    "reason": position.status,
                    "cleanup_errors": position.cleanup_errors,
                }

            if position.status != "active":
                return {"action": "none", "reason": f"Position {position.status}"}

            if self._check_stop_loss(position, current_price):
                await self._close_position(position_id, current_price, "stop_loss", exchange_config)
                result = {"action": "close", "reason": "stop_loss_hit", "pnl_pct": position.unrealized_pnl_pct}
                return result

            if self._check_take_profit(position, current_price):
                await self._close_position(position_id, current_price, "take_profit", exchange_config)
                result = {"action": "close", "reason": "take_profit_hit", "pnl_pct": position.unrealized_pnl_pct}
                return result

            if position.entries_remaining > 0:
                should_dca = self._should_add_entry(position, config, current_price)

                if should_dca:
                    entry_result = await self._add_entry(position_id, config, current_price, exchange_config)
                    result = {"action": "dca_entry", "reason": entry_result.get("reason", ""), "entry_idx": len(position.entries)}

            await self.sync_position_state(position_id)

        return result

    def _check_stop_loss(self, position: DCAPosition, current_price: float) -> bool:
        if position.stop_loss_price <= 0:
            return False

        if position.direction == "long":
            return current_price <= position.stop_loss_price
        else:
            return current_price >= position.stop_loss_price

    def _check_take_profit(self, position: DCAPosition, current_price: float) -> bool:
        if position.take_profit_price <= 0:
            return False

        if position.direction == "long":
            return current_price >= position.take_profit_price
        else:
            return current_price <= position.take_profit_price

    def _should_add_entry(self, position: DCAPosition, config: DCAConfig, current_price: float) -> bool:
        if position.entries_remaining <= 0:
            return False

        total_capital = position.total_capital_usdt
        if total_capital >= config.max_total_capital_usdt:
            return False

        if position.next_entry_price <= 0:
            return False

        if position.average_entry_price <= 0:
            return False

        if config.mode == "average_down":
            if position.direction == "long":
                loss_pct = (position.average_entry_price - current_price) / position.average_entry_price * 100
                return loss_pct >= config.activation_loss_pct and current_price <= position.next_entry_price
            else:
                loss_pct = (current_price - position.average_entry_price) / position.average_entry_price * 100
                return loss_pct >= config.activation_loss_pct and current_price >= position.next_entry_price

        elif config.mode == "average_up":
            if position.direction == "long":
                gain_pct = (current_price - position.average_entry_price) / position.average_entry_price * 100
                return gain_pct >= config.activation_loss_pct and current_price >= position.next_entry_price
            else:
                gain_pct = (position.average_entry_price - current_price) / position.average_entry_price * 100
                return gain_pct >= config.activation_loss_pct and current_price <= position.next_entry_price

        return False

    async def _add_entry(self, position_id: str, config: DCAConfig, current_price: float, exchange_config: dict | None = None) -> dict:
        position = self.positions[position_id]

        base_qty = position.entries[0].quantity

        new_entry_idx = len(position.entries) + 1
        new_quantity = self._calculate_next_entry_quantity(config, new_entry_idx, base_qty, position=position, entry_price=current_price)

        if config.sizing_method == "fixed" and config.fixed_size_usdt > 0:
            new_quantity = config.fixed_size_usdt / current_price

        new_capital = new_quantity * current_price

        if position.total_capital_usdt + new_capital > config.max_total_capital_usdt:
            max_additional = config.max_total_capital_usdt - position.total_capital_usdt
            new_quantity = max_additional / current_price
            new_capital = new_quantity * current_price

        fees = new_capital * config.fee_pct / 100

        entry = DCAEntry(
            entry_price=current_price,
            quantity=new_quantity,
            capital_usdt=new_capital,
            entry_time=utcnow(),
            entry_idx=new_entry_idx,
            reason=f"dca_entry_{new_entry_idx}",
            fees_usdt=fees,
        )

        if not config.paper_mode:
            # Pre-validate size and cost against exchange limits
            exchange_name = str((exchange_config or {}).get("name") or settings.exchange.name or "").lower().strip()
            market_type = str((exchange_config or {}).get("market_type") or settings.exchange.market_type or "contract").lower().strip()
            try:
                from exchange import get_market_limits
                limits = await asyncio.to_thread(get_market_limits, exchange_name, config.ticker, market_type)
                if limits:
                    min_amount = float(limits.get("min_amount") or 0.0)
                    min_cost = float(limits.get("min_cost") or 0.0)
                    contract_size = float(limits.get("contract_size") or 1.0)
                    entry_cost = new_quantity * current_price * contract_size
                    if (min_amount > 0 and new_quantity < min_amount) or (min_cost > 0 and entry_cost < min_cost):
                        logger.warning(
                            f"[DCA] Next entry #{new_entry_idx} for {config.ticker} below exchange requirements. "
                            f"size={new_quantity:.6f} (min={min_amount}), cost={entry_cost:.2f} (min={min_cost}). Aborting entry."
                        )
                        return {"success": False, "reason": "size_below_exchange_minimum"}
            except Exception as e:
                logger.warning(f"[DCA] Exchange limits fetch failed during pre-validation: {e}")

            try:
                from exchange import execute_trade

                direction = SignalDirection.LONG if config.direction == "long" else SignalDirection.SHORT
                decision = TradeDecision(
                    execute=True,
                    direction=direction,
                    ticker=config.ticker,
                    entry_price=current_price,
                    quantity=new_quantity,
                    stop_loss=self._calculate_stop_loss_price(config, position.average_entry_price, direction),
                    take_profit=self._calculate_take_profit_price(config, position.average_entry_price, direction),
                    reason=f"DCA entry #{new_entry_idx}",
                    order_type="market",
                    idempotency_key=f"dca:{position_id}:entry:{new_entry_idx}",
                )

                order_result = await execute_trade(decision, exchange_config)

                if order_result.get("status") in ["filled", "partial", "simulated"]:
                    filled_price = float(order_result.get("entry_price") or current_price)
                    filled_qty = float(order_result.get("filled_quantity") or order_result.get("quantity") or new_quantity)
                    if filled_qty <= 0:
                        raise Exception("Exchange returned zero filled quantity")
                    new_capital = filled_qty * filled_price
                    entry.entry_price = filled_price
                    entry.quantity = filled_qty
                    entry.capital_usdt = new_capital
                    entry.fees_usdt = new_capital * config.fee_pct / 100
                    entry.order_id = order_result.get("order_id", "")
                    logger.info(f"[DCA] Placed DCA entry #{new_entry_idx}: {order_result.get('order_id')}")
                else:
                    logger.error(f"[DCA] Failed to place DCA entry #{new_entry_idx}: {order_result}")
                    raise Exception(f"Failed to place DCA entry: {order_result.get('reason')}")

            except Exception as e:
                logger.error(f"[DCA] Exchange execution failed for entry #{new_entry_idx}: {e}")
                if not config.paper_mode:
                    raise
        else:
            logger.info(f"[DCA] Paper mode - simulated DCA entry #{new_entry_idx}")

        position.entries.append(entry)
        position.total_quantity += entry.quantity
        position.total_capital_usdt += entry.capital_usdt
        position.entries_remaining = max(0, position.entries_remaining - 1)

        total_qty = position.total_quantity

        if total_qty > 0:
            weighted_avg = sum(e.entry_price * e.quantity for e in position.entries) / total_qty
        else:
            weighted_avg = 0.0
        position.average_entry_price = weighted_avg

        self._update_stop_take(position, config)
        self._calculate_next_entry(position, config)

        if not config.paper_mode:
            from exchange import cancel_order, place_protective_stop, place_protective_take_profit

            old_sl_id = position.stop_loss_order_id
            old_tp_id = position.take_profit_order_id
            entry_sl_id = str(order_result.get("stop_loss_order_id") or "")
            entry_tp_id = str(order_result.get("take_profit_order_id") or "")
            if old_sl_id and old_sl_id not in position.stop_loss_order_ids:
                position.stop_loss_order_ids.append(old_sl_id)
            if old_tp_id and old_tp_id not in position.take_profit_order_ids:
                position.take_profit_order_ids.append(old_tp_id)
            if entry_sl_id and entry_sl_id not in position.stop_loss_order_ids:
                position.stop_loss_order_ids.append(entry_sl_id)
            if entry_tp_id and entry_tp_id not in position.take_profit_order_ids:
                position.take_profit_order_ids.append(entry_tp_id)

            if position.stop_loss_price:
                sl_result = await place_protective_stop(
                    ticker=config.ticker,
                    direction=position.direction,
                    quantity=position.total_quantity,
                    stop_price=position.stop_loss_price,
                    exchange_config=exchange_config,
                    existing_order_id=old_sl_id or None,
                )
                if sl_result.get("status") == "placed" and sl_result.get("order_id"):
                    position.stop_loss_order_id = str(sl_result["order_id"])
                    position.stop_loss_order_ids = [position.stop_loss_order_id] + [
                        order_id
                        for order_id in position.stop_loss_order_ids
                        if order_id not in {old_sl_id, position.stop_loss_order_id}
                    ]
                    if entry_sl_id and entry_sl_id != position.stop_loss_order_id:
                        entry_sl_cancel = await cancel_order(entry_sl_id, config.ticker, exchange_config)
                        if entry_sl_cancel.get("status") in {"cancelled", "canceled", "not_found", "simulated"}:
                            position.stop_loss_order_ids = [
                                order_id for order_id in position.stop_loss_order_ids if order_id != entry_sl_id
                            ]
                        else:
                            logger.error(
                                f"[DCA] New-entry SL {entry_sl_id} could not be cancelled after aggregate "
                                f"SL replacement: {entry_sl_cancel}"
                            )
                            position.cleanup_errors.append({
                                "order_id": entry_sl_id,
                                "kind": "stop_loss",
                                "reason": str(entry_sl_cancel.get("reason") or entry_sl_cancel.get("status")),
                                "reconciliation_id": entry_sl_cancel.get("reconciliation_id"),
                            })
                elif sl_result.get("status") == "manual_review":
                    position.stop_loss_order_ids = list(dict.fromkeys(
                        position.stop_loss_order_ids
                        + [str(item) for item in sl_result.get("active_order_ids") or [] if item]
                    ))
                    position.cleanup_errors.append({
                        "kind": "stop_loss_replacement",
                        "reason": str(sl_result.get("reason")),
                        "reconciliation_id": sl_result.get("reconciliation_id"),
                    })
                else:
                    logger.error(
                        f"[DCA] Aggregate SL replacement failed after entry #{new_entry_idx}; "
                        f"keeping existing per-entry protection: {sl_result}"
                    )

            if position.take_profit_price:
                tp_result = await place_protective_take_profit(
                    ticker=config.ticker,
                    direction=position.direction,
                    quantity=position.total_quantity,
                    take_profit_price=position.take_profit_price,
                    exchange_config=exchange_config,
                    existing_order_id=old_tp_id or None,
                )
                if tp_result.get("status") == "placed" and tp_result.get("order_id"):
                    position.take_profit_order_id = str(tp_result["order_id"])
                    position.take_profit_order_ids = [position.take_profit_order_id] + [
                        order_id
                        for order_id in position.take_profit_order_ids
                        if order_id not in {old_tp_id, position.take_profit_order_id}
                    ]
                    if entry_tp_id and entry_tp_id != position.take_profit_order_id:
                        entry_tp_cancel = await cancel_order(entry_tp_id, config.ticker, exchange_config)
                        if entry_tp_cancel.get("status") in {"cancelled", "canceled", "not_found", "simulated"}:
                            position.take_profit_order_ids = [
                                order_id for order_id in position.take_profit_order_ids if order_id != entry_tp_id
                            ]
                        else:
                            logger.error(
                                f"[DCA] New-entry TP {entry_tp_id} could not be cancelled after aggregate "
                                f"TP replacement: {entry_tp_cancel}"
                            )
                            position.cleanup_errors.append({
                                "order_id": entry_tp_id,
                                "kind": "take_profit",
                                "reason": str(entry_tp_cancel.get("reason") or entry_tp_cancel.get("status")),
                                "reconciliation_id": entry_tp_cancel.get("reconciliation_id"),
                            })
                elif tp_result.get("status") == "manual_review":
                    position.take_profit_order_ids = list(dict.fromkeys(
                        position.take_profit_order_ids
                        + [str(item) for item in tp_result.get("active_order_ids") or [] if item]
                    ))
                    position.cleanup_errors.append({
                        "kind": "take_profit_replacement",
                        "reason": str(tp_result.get("reason")),
                        "reconciliation_id": tp_result.get("reconciliation_id"),
                    })
                else:
                    logger.error(
                        f"[DCA] Aggregate TP replacement failed after entry #{new_entry_idx}; "
                        f"keeping existing per-entry protection: {tp_result}"
                    )

        position.updated_at = utcnow()

        logger.info(f"[DCA] Added entry #{new_entry_idx} for {position.ticker}: price={entry.entry_price}, qty={entry.quantity}, avg_entry={weighted_avg:.4f}")

        await self.sync_position_state(position_id)

        return {"success": True, "entry_idx": new_entry_idx, "quantity": entry.quantity, "average_entry": weighted_avg}

    def _update_pnl(self, position: DCAPosition, config: DCAConfig | None = None) -> None:
        # NOTE: Unrealized PnL does not deduct accumulated fees; fees are tracked separately
        if position.average_entry_price <= 0:
            return
        leverage = max(1.0, float(config.leverage if config else 1.0))
        if position.direction == "long":
            position.unrealized_pnl_usdt = (position.current_price - position.average_entry_price) * position.total_quantity
            position.unrealized_pnl_pct = (position.current_price - position.average_entry_price) / position.average_entry_price * 100 * leverage
        else:
            position.unrealized_pnl_usdt = (position.average_entry_price - position.current_price) * position.total_quantity
            position.unrealized_pnl_pct = (position.average_entry_price - position.current_price) / position.average_entry_price * 100 * leverage

    async def _close_position(self, position_id: str, exit_price: float, reason: str, exchange_config: dict | None = None) -> None:
        position = self.positions[position_id]
        config = self.configs.get(position_id)
        cleanup_retry = position.status == "cleanup_required"

        # A cleanup retry may mean either "already flat, cancel remaining
        # protection" or "legacy live position still needs flattening".  Only
        # skip the reduce-only close after a prior close was durably recorded.
        needs_exchange_close = bool(
            config
            and not config.paper_mode
            and (not cleanup_retry or position.closed_at is None)
        )
        if needs_exchange_close:
            close_confirmed = False
            try:
                from exchange import execute_trade

                direction = SignalDirection.LONG if config.direction == "long" else SignalDirection.SHORT
                close_direction = SignalDirection.CLOSE_LONG if direction == SignalDirection.LONG else SignalDirection.CLOSE_SHORT

                decision = TradeDecision(
                    execute=True,
                    direction=close_direction,
                    ticker=position.ticker,
                    entry_price=exit_price,
                    quantity=position.total_quantity,
                    reason=f"DCA close: {reason}",
                    order_type="market",
                    idempotency_key=f"dca:{position_id}:close:{reason}",
                )

                order_result = await execute_trade(decision, exchange_config)

                if order_result.get("status") in ["closed", "filled", "simulated", "no_position"]:
                    logger.info(f"[DCA] Closed position via exchange: {order_result.get('order_id')}")
                    close_confirmed = True
                elif order_result.get("status") == "partial_closed":
                    logger.error(f"[DCA] Close only partially filled; keeping position active: {order_result}")
                    position.updated_at = utcnow()
                    await self.sync_position_state(position_id)
                    raise RuntimeError("DCA exchange close was partial; keeping position active")
                else:
                    logger.error(f"[DCA] Failed to close position: {order_result}")

            except Exception as e:
                logger.error(f"[DCA] Exchange close failed: {e}")
                position.updated_at = utcnow()
                await self.sync_position_state(position_id)
                raise RuntimeError(f"DCA exchange close failed; keeping position active: {e}") from e

            if not close_confirmed:
                position.updated_at = utcnow()
                await self.sync_position_state(position_id)
                raise RuntimeError("DCA exchange close was not confirmed; keeping position active")

        cleanup_failures: list[dict] = []
        if config and not config.paper_mode:
            from exchange import cancel_order

            stop_ids = list(dict.fromkeys(
                [position.stop_loss_order_id, *position.stop_loss_order_ids]
            ))
            take_profit_ids = list(dict.fromkeys(
                [position.take_profit_order_id, *position.take_profit_order_ids]
            ))
            for order_id, kind in (
                *((order_id, "stop_loss") for order_id in stop_ids if order_id),
                *((order_id, "take_profit") for order_id in take_profit_ids if order_id),
            ):
                cancel_result = await cancel_order(order_id, position.ticker, exchange_config)
                if cancel_result.get("status") in {"cancelled", "canceled", "not_found", "simulated"}:
                    if kind == "stop_loss":
                        position.stop_loss_order_ids = [
                            item for item in position.stop_loss_order_ids if item != order_id
                        ]
                        if position.stop_loss_order_id == order_id:
                            position.stop_loss_order_id = ""
                    else:
                        position.take_profit_order_ids = [
                            item for item in position.take_profit_order_ids if item != order_id
                        ]
                        if position.take_profit_order_id == order_id:
                            position.take_profit_order_id = ""
                else:
                    cleanup_failures.append({
                        "order_id": order_id,
                        "kind": kind,
                        "reason": str(cancel_result.get("reason") or cancel_result.get("status")),
                        "reconciliation_id": cancel_result.get("reconciliation_id"),
                    })

        effective_exit_price = position.close_price or exit_price
        effective_reason = position.close_reason or reason
        if position.direction == "long":
            pnl_usdt = (effective_exit_price - position.average_entry_price) * position.total_quantity
        else:
            pnl_usdt = (position.average_entry_price - effective_exit_price) * position.total_quantity

        total_fees = sum(e.fees_usdt for e in position.entries)
        pnl_usdt -= total_fees

        position.realized_pnl_usdt = pnl_usdt
        position.status = "cleanup_required" if cleanup_failures else "closed"
        position.closed_at = position.closed_at or utcnow()
        position.close_reason = effective_reason
        position.close_price = effective_exit_price
        position.current_price = effective_exit_price
        position.cleanup_errors = cleanup_failures

        if cleanup_failures:
            logger.error(
                f"[DCA] Position {position_id} is flat but {len(cleanup_failures)} "
                "protective orders still require cleanup"
            )
        else:
            logger.info(
                f"[DCA] Closed position {position_id}: reason={effective_reason}, "
                f"pnl_usdt={pnl_usdt:.2f}, entries={len(position.entries)}"
            )

        await self.sync_position_state(position_id)

    def get_position_status(self, position_id: str) -> dict:
        position = self.positions.get(position_id)
        if not position:
            return {"error": "Position not found"}

        return {
            "config_id": position.config_id,
            "ticker": position.ticker,
            "direction": position.direction,
            "status": position.status,
            "entries_count": len(position.entries),
            "total_quantity": round(position.total_quantity, 6),
            "total_capital_usdt": round(position.total_capital_usdt, 2),
            "average_entry_price": round(position.average_entry_price, 6),
            "current_price": round(position.current_price, 6),
            "realized_pnl_usdt": round(position.realized_pnl_usdt, 2),
            "unrealized_pnl_usdt": round(position.unrealized_pnl_usdt, 2),
            "unrealized_pnl_pct": round(position.unrealized_pnl_pct, 2),
            "stop_loss_price": round(position.stop_loss_price, 6),
            "take_profit_price": round(position.take_profit_price, 6),
            "cleanup_errors": position.cleanup_errors,
            "next_entry_price": round(position.next_entry_price, 6),
            "entries_remaining": position.entries_remaining,
            "highest_price": round(position.highest_price, 6),
            "lowest_price": round(position.lowest_price, 6),
            "started_at": position.started_at.isoformat(),
            "entries": [
                {
                    "idx": e.entry_idx,
                    "price": round(e.entry_price, 6),
                    "quantity": round(e.quantity, 6),
                    "capital": round(e.capital_usdt, 2),
                    "time": e.entry_time.isoformat(),
                    "reason": e.reason,
                }
                for e in position.entries
            ],
        }

    async def get_position_status_async(self, position_id: str) -> dict:
        if position_id not in self.positions:
            await self.load_position_state(position_id)
        return self.get_position_status(position_id)

    def list_active_positions(self) -> list[dict]:
        return [
            self.get_position_status(pid)
            for pid, pos in self.positions.items()
            if pos.status in {"active", "cleanup_required"}
        ]

    async def list_active_positions_async(self) -> list[dict]:
        await self.refresh_active_from_redis()
        return self.list_active_positions()

    def remove_position(self, position_id: str) -> bool:
        if position_id in self.positions:
            del self.positions[position_id]
            if position_id in self.configs:
                del self.configs[position_id]
            if settings.redis.enabled:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self.remove_position_async(position_id))
                except RuntimeError:
                    logger.warning(f"[DCA] Cannot remove position {position_id} from Redis: no running event loop")
            return True
        return False

    async def remove_position_async(self, position_id: str) -> bool:
        existed = position_id in self.positions
        self.positions.pop(position_id, None)
        self.configs.pop(position_id, None)
        await redis_hdel(_DCA_ACTIVE_HASH, position_id)
        await redis_hdel(_DCA_STATE_HASH, position_id)
        return existed

    def to_dict(self) -> dict:
        return {
            "active_positions": len([p for p in self.positions.values() if p.status == "active"]),
            "total_positions": len(self.positions),
            "positions": {pid: self.get_position_status(pid) for pid in self.positions},
        }
