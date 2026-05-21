"""
DCA (Dollar Cost Average) Strategy Engine.
Manages position averaging down/up with configurable parameters.
Enhanced with live exchange execution support.
"""
import asyncio
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
        except RuntimeError:
            asyncio.run(self.sync_position_state(position_id))
            return
        loop.create_task(self.sync_position_state(position_id))

    async def sync_position_state(self, position_id: str) -> bool:
        """Persist latest local DCA state into Redis hashes."""
        record = self._state_record(position_id)
        if not record:
            return False

        saved = await redis_hset_json(_DCA_STATE_HASH, position_id, record)
        if record["status"] == "active":
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
            config.strategy_id = f"dca_{config.ticker}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

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
        if config.paper_mode:
            position = self._create_position_paper(config, current_price)
            self._schedule_state_sync(position.config_id)
            return position

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.create_position_async(config, current_price, exchange_config))

        raise RuntimeError("Use create_position_async() for live exchange execution")

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
        async with distributed_lock(f"dca:create:{config.user_id or 'global'}:{config.ticker}:{config.direction}", ttl_seconds=45):
            position = self._build_position(config)

            initial_qty = self._calculate_initial_quantity(config, current_price)

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
                )

                order_result = await execute_trade(decision, exchange_config)

                if order_result.get("status") in ["filled", "simulated"]:
                    filled_price = float(order_result.get("entry_price") or current_price)
                    filled_capital = initial_qty * filled_price
                    entry = DCAEntry(
                        entry_price=filled_price,
                        quantity=initial_qty,
                        capital_usdt=filled_capital,
                        entry_time=utcnow(),
                        entry_idx=1,
                        reason="initial_entry",
                        order_id=order_result.get("order_id", ""),
                        fees_usdt=filled_capital * config.fee_pct / 100,
                    )
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

    def _calculate_next_entry_quantity(self, config: DCAConfig, entry_idx: int, base_quantity: float) -> float:
        method = config.sizing_method

        if method == "fixed":
            return base_quantity

        elif method == "martingale":
            return base_quantity * (config.sizing_multiplier ** (entry_idx - 1))

        elif method == "geometric":
            return base_quantity * entry_idx

        elif method == "fibonacci":
            fib = [1, 1, 2, 3, 5, 8, 13, 21]
            idx = min(entry_idx - 1, len(fib) - 1)
            return base_quantity * fib[idx]

        return base_quantity

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

        async with distributed_lock(self._position_lock_name(position_id, position, config), ttl_seconds=45):
            await self.load_position_state(position_id, refresh=True)
            position = self.positions[position_id]
            config = self.configs.get(position_id)
            if not config:
                return {"action": "error", "reason": "Config not found"}

            position.current_price = current_price
            position.highest_price = max(position.highest_price, current_price)
            position.lowest_price = min(position.lowest_price, current_price)

            self._update_pnl(position)

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
        new_quantity = self._calculate_next_entry_quantity(config, new_entry_idx, base_qty)

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
                )

                order_result = await execute_trade(decision, exchange_config)

                if order_result.get("status") in ["filled", "simulated"]:
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
        position.total_quantity += new_quantity
        position.total_capital_usdt += new_capital
        position.entries_remaining = max(0, position.entries_remaining - 1)

        total_qty = position.total_quantity

        weighted_avg = sum(e.entry_price * e.quantity for e in position.entries) / total_qty
        position.average_entry_price = weighted_avg

        self._update_stop_take(position, config)
        self._calculate_next_entry(position, config)

        position.updated_at = utcnow()

        logger.info(f"[DCA] Added entry #{new_entry_idx} for {position.ticker}: price={current_price}, qty={new_quantity}, avg_entry={weighted_avg:.4f}")

        await self.sync_position_state(position_id)

        return {"success": True, "entry_idx": new_entry_idx, "quantity": new_quantity, "average_entry": weighted_avg}

    def _update_pnl(self, position: DCAPosition) -> None:
        if position.direction == "long":
            position.unrealized_pnl_usdt = (position.current_price - position.average_entry_price) * position.total_quantity
            position.unrealized_pnl_pct = (position.current_price - position.average_entry_price) / position.average_entry_price * 100
        else:
            position.unrealized_pnl_usdt = (position.average_entry_price - position.current_price) * position.total_quantity
            position.unrealized_pnl_pct = (position.average_entry_price - position.current_price) / position.average_entry_price * 100

    async def _close_position(self, position_id: str, exit_price: float, reason: str, exchange_config: dict | None = None) -> None:
        position = self.positions[position_id]
        config = self.configs.get(position_id)

        if config and not config.paper_mode:
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
                )

                order_result = await execute_trade(decision, exchange_config)

                if order_result.get("status") in ["closed", "filled", "simulated"]:
                    logger.info(f"[DCA] Closed position via exchange: {order_result.get('order_id')}")
                    close_confirmed = True
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

        if position.direction == "long":
            pnl_usdt = (exit_price - position.average_entry_price) * position.total_quantity
        else:
            pnl_usdt = (position.average_entry_price - exit_price) * position.total_quantity

        total_fees = sum(e.fees_usdt for e in position.entries)
        pnl_usdt -= total_fees

        position.realized_pnl_usdt = pnl_usdt
        position.status = "closed"
        position.closed_at = utcnow()
        position.close_reason = reason
        position.current_price = exit_price

        logger.info(f"[DCA] Closed position {position_id}: reason={reason}, pnl_usdt={pnl_usdt:.2f}, entries={len(position.entries)}")

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
            "unrealized_pnl_usdt": round(position.unrealized_pnl_usdt, 2),
            "unrealized_pnl_pct": round(position.unrealized_pnl_pct, 2),
            "stop_loss_price": round(position.stop_loss_price, 6),
            "take_profit_price": round(position.take_profit_price, 6),
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
            if pos.status == "active"
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
                except RuntimeError:
                    asyncio.run(self.remove_position_async(position_id))
                else:
                    loop.create_task(self.remove_position_async(position_id))
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
