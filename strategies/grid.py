"""
Grid Trading Strategy Engine.
Manages automated buy/sell orders within a price range.
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
from core.utils.common import safe_bool, safe_float
from core.utils.datetime import utcnow
from models import SignalDirection, TradeDecision

_GRID_STATE_HASH = make_key("strategy", "grid", "state")
_GRID_ACTIVE_HASH = make_key("strategy", "grid", "active")


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


class GridMode(Enum):
    NEUTRAL = "neutral"
    LONG = "long"
    SHORT = "short"
    ARITHMETIC = "arithmetic"
    GEOMETRIC = "geometric"


@dataclass
class GridConfig:
    ticker: str = "BTCUSDT"
    upper_price: float = 0.0
    lower_price: float = 0.0
    grid_count: int = 10
    total_capital_usdt: float = 1000.0
    quantity_per_grid: float = 0.0
    grid_spacing_pct: float = 1.0
    spacing_mode: str = "arithmetic"
    leverage: float = 1.0
    fee_pct: float = 0.04
    slippage_pct: float = 0.05
    stop_loss_pct: float = 0.0
    take_profit_pct: float = 0.0
    cooldown_seconds: int = 30
    max_open_orders: int = 20
    auto_replenish: bool = True
    replenish_threshold_pct: float = 50.0
    profit_reinvest_pct: float = 0.0
    strategy_id: str = ""
    user_id: str = ""
    enabled: bool = True
    paper_mode: bool = True
    mode: str = "neutral"
    direction: str = "neutral"


@dataclass
class GridLevel:
    price: float
    quantity: float
    side: str
    order_id: str = ""
    status: str = "pending"
    filled_at: datetime | None = None
    filled_price: float = 0.0
    pnl_usdt: float = 0.0
    fees_usdt: float = 0.0
    grid_index: int | None = None
    pair_level: float | None = None
    exchange_order_status: str = ""
    take_profit_order_id: str = ""
    stop_loss_order_id: str = ""


@dataclass
class GridPosition:
    config_id: str
    ticker: str
    mode: str
    upper_price: float
    lower_price: float
    grid_levels: list[GridLevel] = field(default_factory=list)
    filled_buy_count: int = 0
    filled_sell_count: int = 0
    total_buy_quantity: float = 0.0
    total_sell_quantity: float = 0.0
    total_capital_usdt: float = 0.0
    realized_pnl_usdt: float = 0.0
    unrealized_pnl_usdt: float = 0.0
    total_fees_usdt: float = 0.0
    total_trades: int = 0
    status: str = "active"
    current_price: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    started_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    closed_at: datetime | None = None
    close_reason: str = ""
    pending_orders: int = 0
    open_pairs: list[dict] = field(default_factory=list)


class GridEngine:
    def __init__(self):
        self.positions: dict[str, GridPosition] = {}
        self.configs: dict[str, GridConfig] = {}
        self.price_cache: dict[str, float] = {}
        self._monitor_task: asyncio.Task | None = None

    def _state_record(self, position_id: str) -> dict | None:
        position = self.positions.get(position_id)
        config = self.configs.get(position_id)
        if not position or not config:
            return None
        return {
            "strategy_type": "grid",
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
            config_data = _filter_dataclass(GridConfig, record.get("config") or {})
            position_data = _filter_dataclass(GridPosition, record.get("position") or {})
            levels = []
            for raw_level in position_data.get("grid_levels", []):
                level_data = _filter_dataclass(GridLevel, dict(raw_level or {}))
                level_data["filled_at"] = _parse_datetime(level_data.get("filled_at"))
                levels.append(GridLevel(**level_data))

            position_data["grid_levels"] = levels
            for key in ("started_at", "updated_at", "closed_at"):
                if key in position_data:
                    position_data[key] = _parse_datetime(position_data.get(key))

            config = GridConfig(**config_data)
            position = GridPosition(**position_data)
            position_id = str(record.get("strategy_id") or position.config_id or config.strategy_id)
            if not position_id:
                return False
            config.strategy_id = position_id
            position.config_id = position_id
            self.configs[position_id] = config
            self.positions[position_id] = position
            return True
        except Exception as exc:
            logger.warning(f"[Grid/Redis] Failed to restore Redis state: {exc}")
            return False

    def _position_lock_name(self, position_id: str, position: GridPosition, config: GridConfig | None) -> str:
        owner = (config.user_id if config else "") or "global"
        return f"grid:{owner}:{position.ticker}"

    def _schedule_state_sync(self, position_id: str) -> None:
        if not settings.redis.enabled:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.sync_position_state(position_id))
        except RuntimeError:
            # No running event loop - log warning instead of creating conflicting loop
            logger.warning(f"[Grid] Cannot sync state: no running event loop for position {position_id}")

    async def sync_position_state(self, position_id: str) -> bool:
        """Persist latest local Grid state into Redis hashes."""
        record = self._state_record(position_id)
        if not record:
            return False

        saved = await redis_hset_json(_GRID_STATE_HASH, position_id, record)
        if record["status"] == "active":
            saved = await redis_hset_json(_GRID_ACTIVE_HASH, position_id, record) or saved
        else:
            await redis_hdel(_GRID_ACTIVE_HASH, position_id)
        return saved

    async def load_position_state(self, position_id: str, *, refresh: bool = False) -> bool:
        """Load a Grid position from Redis if local state is missing or stale."""
        if not refresh and position_id in self.positions and position_id in self.configs:
            return True
        record = await redis_hget_json(_GRID_ACTIVE_HASH, position_id)
        if record is None:
            record = await redis_hget_json(_GRID_STATE_HASH, position_id)
        if not isinstance(record, dict):
            return False
        return self._restore_state_record(record)

    async def refresh_active_from_redis(self) -> int:
        """Hydrate all Redis active Grid positions into this process."""
        records = await redis_hgetall_json(_GRID_ACTIVE_HASH)
        restored = 0
        for record in records.values():
            if isinstance(record, dict) and self._restore_state_record(record):
                restored += 1
        return restored

    def _ensure_strategy_id(self, config: GridConfig) -> None:
        if not config.strategy_id:
            # BUG FIX: Use UTC time to avoid timezone collisions
            config.strategy_id = f"grid_{config.ticker}_{utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _is_live_limit_level(level: GridLevel, config: GridConfig) -> bool:
        return (not config.paper_mode) and bool(level.order_id)

    async def _confirm_live_limit_fill(
        self,
        level: GridLevel,
        config: GridConfig,
        exchange_config: dict | None,
    ) -> dict:
        if not level.order_id or not exchange_config:
            return {"filled": False, "reason": "missing order or exchange config"}
        try:
            from exchange import get_recent_orders

            orders = await get_recent_orders(config.ticker, 50, exchange_config)
        except Exception as exc:
            logger.warning(f"[Grid] Could not verify live limit order {level.order_id}: {exc}")
            return {"filled": False, "reason": str(exc)}

        order = next((item for item in orders if str(item.get("id") or "") == str(level.order_id)), None)
        if not order:
            return {"filled": False, "reason": "order not found in recent orders"}
        status = str(order.get("status") or "").lower()
        level.exchange_order_status = status or level.exchange_order_status
        if status in {"canceled", "cancelled", "expired", "rejected"}:
            return {"filled": False, "failed": True, "reason": f"order {status}"}
        filled_qty = float(order.get("filled") or 0)
        if status not in {"closed", "filled"} or filled_qty <= 0:
            return {"filled": False, "reason": f"order still {status or 'unknown'}"}
        return {
            "filled": True,
            "price": float(order.get("average") or order.get("price") or level.price),
            "quantity": filled_qty,
            "status": status,
        }

    async def _place_live_fill_protection(
        self,
        level: GridLevel,
        config: GridConfig,
        exchange_config: dict | None,
        fill_price: float,
    ) -> dict:
        if not exchange_config or not safe_bool(exchange_config.get("live_trading"), False):
            return {"status": "skipped", "reason": "not live"}
        if level.stop_loss_order_id or level.take_profit_order_id:
            return {"status": "skipped", "reason": "protection already placed"}

        stop_loss = self._calculate_grid_stop_loss(config, fill_price, level.side)
        take_profit = self._calculate_grid_take_profit(config, fill_price, level.side)
        if not stop_loss and not take_profit:
            return {"status": "skipped", "reason": "no protection configured"}

        from exchange import _create_conditional_order, _get_or_create_exchange, _resolve_symbol

        exchange = _get_or_create_exchange(
            exchange_id=exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name,
            api_key=exchange_config.get("api_key") or "",
            api_secret=exchange_config.get("api_secret") or "",
            password=exchange_config.get("password") or "",
            live=True,
            sandbox=safe_bool(exchange_config.get("sandbox_mode", settings.exchange.sandbox_mode), False),
            market_type=exchange_config.get("market_type") or settings.exchange.market_type,
            margin_mode=exchange_config.get("margin_mode") or settings.risk.margin_mode,
        )
        symbol = await asyncio.to_thread(
            _resolve_symbol,
            exchange,
            config.ticker,
            exchange_config.get("market_type") or settings.exchange.market_type,
        )
        close_side = "sell" if level.side == "buy" else "buy"
        position_side = "long" if level.side == "buy" else "short"
        result = {"status": "placed"}
        if stop_loss:
            sl_order = await _create_conditional_order(
                exchange, symbol, "stop_loss", close_side, level.quantity, stop_loss, position_side
            )
            level.stop_loss_order_id = str(sl_order.get("id") or "")
            result["stop_loss_order_id"] = level.stop_loss_order_id
        if take_profit:
            tp_order = await _create_conditional_order(
                exchange, symbol, "take_profit", close_side, level.quantity, take_profit, position_side
            )
            level.take_profit_order_id = str(tp_order.get("id") or "")
            result["take_profit_order_id"] = level.take_profit_order_id
        return result

    async def _fail_safe_close_unprotected_level(
        self,
        position_id: str,
        level: GridLevel,
        config: GridConfig,
        exchange_config: dict | None,
    ) -> dict:
        if not exchange_config or not safe_bool(exchange_config.get("live_trading"), False):
            return {"status": "skipped", "closed": False, "reason": "not live"}

        cancelled_orders = []
        cancel_failures = []
        try:
            from exchange import cancel_order

            for attr in ("stop_loss_order_id", "take_profit_order_id"):
                order_id = str(getattr(level, attr) or "")
                if not order_id:
                    continue
                try:
                    await cancel_order(order_id, config.ticker, exchange_config)
                    setattr(level, attr, "")
                    cancelled_orders.append(order_id)
                except Exception as exc:
                    cancel_failures.append({"order_id": order_id, "reason": str(exc)})
        except Exception as exc:
            cancel_failures.append({"reason": str(exc)})

        try:
            from exchange import execute_trade

            close_direction = SignalDirection.CLOSE_LONG if level.side == "buy" else SignalDirection.CLOSE_SHORT
            decision = TradeDecision(
                execute=True,
                direction=close_direction,
                ticker=config.ticker,
                quantity=level.quantity,
                reason="Grid live fill protection failed; closing unprotected level",
                order_type="market",
                idempotency_key=f"grid:{position_id}:level:{level.grid_index or level.price}:{level.side}:protection-fail-close",
            )
            result = await execute_trade(decision, exchange_config)
        except Exception as exc:
            result = {"status": "error", "reason": str(exc)}

        status = str(result.get("status") or "").lower()
        closed_quantity = safe_float(
            result.get("closed_quantity") or result.get("filled_quantity") or result.get("quantity") or 0
        )
        min_close_quantity = max(level.quantity * 0.999, 0.0)
        closed = (
            status in {"closed", "filled", "simulated"}
            and (closed_quantity <= 0 or closed_quantity >= min_close_quantity)
        ) or (status == "partial_closed" and closed_quantity >= min_close_quantity)
        return {
            "status": status or "unknown",
            "closed": closed,
            "order_id": result.get("order_id"),
            "exit_price": result.get("exit_price") or result.get("entry_price"),
            "closed_quantity": closed_quantity,
            "cancelled_orders": cancelled_orders,
            "cancel_failures": cancel_failures,
            "reason": result.get("reason"),
            "raw": result,
        }

    def _initialize_grid_position(self, config: GridConfig, current_price: float) -> GridPosition:
        self._ensure_strategy_id(config)

        if config.upper_price <= 0 or config.lower_price <= 0:
            price_range_pct = config.grid_count * config.grid_spacing_pct
            config.upper_price = current_price * (1 + price_range_pct / 100)
            config.lower_price = current_price * (1 - price_range_pct / 100)

        grid_levels = self._calculate_grid_levels(config, current_price)

        position = GridPosition(
            config_id=config.strategy_id,
            ticker=config.ticker,
            mode=config.mode,
            upper_price=config.upper_price,
            lower_price=config.lower_price,
            grid_levels=grid_levels,
            total_capital_usdt=config.total_capital_usdt,
            current_price=current_price,
            highest_price=current_price,
            lowest_price=current_price,
            started_at=utcnow(),
        )

        position.pending_orders = len([level for level in grid_levels if level.status == "pending"])

        self.positions[config.strategy_id] = position
        self.configs[config.strategy_id] = config
        return position

    def create_grid(
        self,
        config: GridConfig,
        current_price: float,
        exchange_config: dict | None = None,
    ) -> GridPosition:
        """Synchronous wrapper for creating a grid position.

        P2-FIX: Removed asyncio.run() to avoid event loop conflicts.
        Use create_grid_async() instead for live exchange execution.
        """
        if config.paper_mode:
            position = self._initialize_grid_position(config, current_price)
            logger.info(f"[Grid] Paper mode - simulated grid creation with {len(position.grid_levels)} levels")
            logger.info(f"[Grid] Created grid for {config.ticker}: range={config.lower_price:.4f}-{config.upper_price:.4f}, levels={len(position.grid_levels)}")
            self._schedule_state_sync(position.config_id)
            return position

        # P2-FIX: Do not attempt to create a new event loop here.
        # This can cause RuntimeError in environments with existing event loops
        # (e.g., APScheduler async jobs, Jupyter notebooks, etc.)
        raise RuntimeError(
            "Live grid creation requires an async context. "
            "Use create_grid_async() instead of create_grid() for live exchange execution."
        )

    async def create_grid_async(
        self,
        config: GridConfig,
        current_price: float,
        exchange_config: dict | None = None
    ) -> GridPosition:
        if config.paper_mode:
            position = self._initialize_grid_position(config, current_price)
            logger.info(f"[Grid] Paper mode - simulated grid creation with {len(position.grid_levels)} levels")
            logger.info(f"[Grid] Created grid for {config.ticker}: range={config.lower_price:.4f}-{config.upper_price:.4f}, levels={len(position.grid_levels)}")
            await self.sync_position_state(position.config_id)
            return position

        async with distributed_lock(
            f"grid:create:{config.user_id or 'global'}:{config.ticker}",
            ttl_seconds=300,
            allow_local_fallback=config.paper_mode,
        ):
            position = self._initialize_grid_position(config, current_price)

            try:
                from exchange import execute_trade

                for index, level in enumerate(position.grid_levels, start=1):
                    if level.status != "pending":
                        continue

                    direction = SignalDirection.LONG if level.side == "buy" else SignalDirection.SHORT
                    decision = TradeDecision(
                        execute=True,
                        direction=direction,
                        ticker=config.ticker,
                        entry_price=level.price,
                        quantity=level.quantity,
                        stop_loss=self._calculate_grid_stop_loss(config, level.price, level.side),
                        take_profit=self._calculate_grid_take_profit(config, level.price, level.side),
                        reason=f"Grid {level.side} at {level.price}",
                        order_type="limit",
                        idempotency_key=f"grid:{position.config_id}:level:{index}:open",
                    )

                    order_result = await execute_trade(decision, exchange_config)

                    if order_result.get("status") in ["filled", "pending", "simulated"]:
                        level.order_id = order_result.get("order_id", "")
                        level.exchange_order_status = str(order_result.get("exchange_order_status") or order_result.get("status") or "")
                        logger.info(f"[Grid] Placed grid order {level.side} @ {level.price}: {order_result.get('order_id')}")
                    else:
                        logger.error(f"[Grid] Failed to place grid order: {order_result}")

            except Exception as e:
                logger.error(f"[Grid] Exchange execution failed: {e}")
                raise

            logger.info(f"[Grid] Created grid for {config.ticker}: range={config.lower_price:.4f}-{config.upper_price:.4f}, levels={len(position.grid_levels)}")

            await self.sync_position_state(position.config_id)
            return position

    def _calculate_grid_levels(self, config: GridConfig, current_price: float) -> list[GridLevel]:
        levels = []

        if config.spacing_mode == "arithmetic":
            price_step = (config.upper_price - config.lower_price) / config.grid_count

            for i in range(config.grid_count):
                price = config.lower_price + price_step * (i + 0.5)

                if price < current_price:
                    side = "buy"
                else:
                    side = "sell"

                if config.quantity_per_grid > 0:
                    quantity = config.quantity_per_grid
                elif config.total_capital_usdt > 0:
                    quantity = config.total_capital_usdt / config.grid_count / price
                else:
                    logger.warning(f"[Grid] Zero quantity for arithmetic grid at price {price:.4f}")
                    continue

                if quantity <= 0:
                    logger.warning(f"[Grid] Invalid quantity {quantity:.6f} at price {price:.4f}")
                    continue

                levels.append(GridLevel(
                    price=round(price, 8),
                    quantity=round(quantity, 6),
                    side=side,
                ))

        elif config.spacing_mode == "geometric":
            ratio = (config.upper_price / config.lower_price) ** (1 / config.grid_count)

            for i in range(config.grid_count):
                price = config.lower_price * ratio ** (i + 0.5)

                if price < current_price:
                    side = "buy"
                else:
                    side = "sell"

                if config.quantity_per_grid > 0:
                    quantity = config.quantity_per_grid
                elif config.total_capital_usdt > 0:
                    quantity = config.total_capital_usdt / config.grid_count / price
                else:
                    logger.warning(f"[Grid] Zero quantity for geometric grid at price {price:.4f}")
                    continue

                if quantity <= 0:
                    logger.warning(f"[Grid] Invalid quantity {quantity:.6f} at price {price:.4f}")
                    continue

                levels.append(GridLevel(
                    price=round(price, 8),
                    quantity=round(quantity, 6),
                    side=side,
                ))

        levels.sort(key=lambda level: level.price)

        for i, level in enumerate(levels):
            level.grid_index = i

        return levels

    def _calculate_grid_stop_loss(self, config: GridConfig, price: float, side: str) -> float | None:
        if config.stop_loss_pct <= 0:
            return None
        if side == "buy":
            return price * (1 - config.stop_loss_pct / 100)
        else:
            return price * (1 + config.stop_loss_pct / 100)

    def _calculate_grid_take_profit(self, config: GridConfig, price: float, side: str) -> float | None:
        if config.take_profit_pct <= 0:
            return None
        if side == "buy":
            return price * (1 + config.take_profit_pct / 100)
        else:
            return price * (1 - config.take_profit_pct / 100)

    async def check_and_execute(self, position_id: str, current_price: float, exchange_config: dict | None = None) -> dict:
        action = "none"
        trades: list[dict] = []

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

            if position.status != "active":
                return {"action": "none", "reason": f"Position {position.status}"}

            if current_price < position.lower_price or current_price > position.upper_price:
                if config.stop_loss_pct > 0:
                    await self._close_grid(position_id, current_price, "out_of_range", exchange_config)
                    return {"action": "close", "reason": "price_out_of_range"}

            triggered_levels = self._find_triggered_levels(position, current_price)
            if not config.paper_mode:
                seen_level_ids = {id(level) for level in triggered_levels}
                for level in position.grid_levels:
                    if level.status == "pending" and level.order_id and id(level) not in seen_level_ids:
                        triggered_levels.append(level)
                        seen_level_ids.add(id(level))

            for level in triggered_levels:
                trade_result = await self._execute_grid_level(position_id, level, current_price, config, exchange_config)
                if trade_result.get("success"):
                    trades.append(trade_result)

            if trades:
                action = "grid_trade"

            self._update_pnl(position, current_price)

            if config.auto_replenish and position.filled_buy_count > config.grid_count * config.replenish_threshold_pct / 100:
                self._replenish_grid(position, config, current_price)

            position.updated_at = utcnow()
            await self.sync_position_state(position_id)

            return {"action": action, "trades": trades}

    def _find_triggered_levels(self, position: GridPosition, current_price: float) -> list[GridLevel]:
        triggered = []

        for level in position.grid_levels:
            if level.status != "pending":
                continue

            if level.side == "buy" and current_price <= level.price:
                triggered.append(level)
            elif level.side == "sell" and current_price >= level.price:
                triggered.append(level)

        return triggered

    async def _execute_grid_level(
        self,
        position_id: str,
        level: GridLevel,
        fill_price: float,
        config: GridConfig,
        exchange_config: dict | None = None
    ) -> dict:
        position = self.positions[position_id]

        pnl = 0.0
        protection_result = {}

        if not config.paper_mode:
            if self._is_live_limit_level(level, config):
                fill_state = await self._confirm_live_limit_fill(level, config, exchange_config)
                if not fill_state.get("filled"):
                    if fill_state.get("failed"):
                        level.status = "error"
                    logger.info(f"[Grid] Live limit order {level.order_id} not filled yet: {fill_state.get('reason')}")
                    return {"success": False, "reason": fill_state.get("reason") or "Live limit order not filled"}
                fill_price = float(fill_state.get("price") or fill_price)
                level.quantity = float(fill_state.get("quantity") or level.quantity)
                level.exchange_order_status = str(fill_state.get("status") or "filled")
                logger.info(f"[Grid] Confirmed exchange limit fill for {config.ticker} @ {fill_price}")
                try:
                    protection_result = await self._place_live_fill_protection(level, config, exchange_config, fill_price)
                except Exception as exc:
                    protection_result = {"status": "error", "reason": str(exc)}
                    logger.error(f"[Grid] Failed to place protection for live fill {level.order_id}: {exc}")
                if protection_result.get("status") == "error":
                    fail_safe_close = await self._fail_safe_close_unprotected_level(position_id, level, config, exchange_config)
                    protection_result["fail_safe_close"] = fail_safe_close
                    if fail_safe_close.get("closed"):
                        fees = level.quantity * fill_price * config.fee_pct / 100
                        exit_price = safe_float(fail_safe_close.get("exit_price"), fill_price) or fill_price
                        if level.side == "buy":
                            pnl = (exit_price - fill_price) * level.quantity
                        else:
                            pnl = (fill_price - exit_price) * level.quantity
                        pnl -= fees
                        level.fees_usdt = fees
                        level.filled_at = utcnow()
                        level.filled_price = fill_price
                        level.status = "closed"
                        level.exchange_order_status = "protection_failed_closed"
                        level.pnl_usdt = pnl
                        position.total_fees_usdt += fees
                        position.realized_pnl_usdt += pnl
                        position.total_trades += 1
                        await self.sync_position_state(position_id)
                        return {
                            "success": True,
                            "closed_fail_safe": True,
                            "side": level.side,
                            "price": fill_price,
                            "quantity": level.quantity,
                            "pnl_usdt": pnl,
                            "fees": fees,
                            "level_price": level.price,
                            "protection": protection_result,
                        }
                    logger.error(f"[Grid] Fail-safe close did not fully close unprotected live fill: {fail_safe_close}")
            else:
                try:
                    from exchange import execute_trade

                    direction = SignalDirection.LONG if level.side == "buy" else SignalDirection.SHORT
                    decision = TradeDecision(
                        execute=True,
                        direction=direction,
                        ticker=config.ticker,
                        entry_price=fill_price,
                        quantity=level.quantity,
                        reason=f"Grid {level.side} filled at {fill_price}",
                        order_type="market",
                        idempotency_key=f"grid:{position_id}:level:{level.price}:{level.side}:market",
                    )

                    order_result = await execute_trade(decision, exchange_config)

                    if order_result.get("status") in ["filled", "partial", "simulated"]:
                        actual_price = float(order_result.get("entry_price") or fill_price)
                        actual_qty = float(order_result.get("filled_quantity") or order_result.get("quantity") or level.quantity)
                        if actual_qty <= 0:
                            return {"success": False, "reason": "Exchange returned zero filled quantity"}
                        fill_price = actual_price
                        level.quantity = actual_qty
                        level.order_id = order_result.get("order_id", "")
                        level.exchange_order_status = str(order_result.get("exchange_order_status") or order_result.get("status") or "")
                        logger.info(f"[Grid] Executed grid trade: {order_result.get('order_id')}")
                    else:
                        logger.error(f"[Grid] Failed to execute grid trade: {order_result}")
                        return {"success": False, "reason": order_result.get("reason") or "Exchange execution failed"}

                except Exception as e:
                    logger.error(f"[Grid] Exchange execution failed: {e}")
                    return {"success": False, "reason": str(e)}

        fees = level.quantity * fill_price * config.fee_pct / 100
        level.fees_usdt = fees
        level.filled_at = utcnow()
        level.filled_price = fill_price
        level.status = "filled"

        pair_level = self._find_pair_level(position, level)

        if pair_level and pair_level.status == "filled":
            if level.side == "buy" and pair_level.side == "sell":
                pnl = (pair_level.filled_price - fill_price) * min(level.quantity, pair_level.quantity)
            elif level.side == "sell" and pair_level.side == "buy":
                pnl = (fill_price - pair_level.filled_price) * min(level.quantity, pair_level.quantity)

            pnl -= fees + pair_level.fees_usdt

            level.status = "paired"
            pair_level.status = "paired"
            level.pair_level = pair_level.filled_price
            pair_level.pair_level = level.filled_price
            level.pnl_usdt = pnl
            pair_level.pnl_usdt = pnl
            position.realized_pnl_usdt += pnl
            position.total_trades += 1
            position.open_pairs.append({
                "buy_price": fill_price if level.side == "buy" else pair_level.filled_price,
                "sell_price": fill_price if level.side == "sell" else pair_level.filled_price,
                "quantity": min(level.quantity, pair_level.quantity),
                "pnl_usdt": pnl,
            })

        if level.side == "buy":
            position.filled_buy_count += 1
            position.total_buy_quantity += level.quantity
        else:
            position.filled_sell_count += 1
            position.total_sell_quantity += level.quantity

        position.total_fees_usdt += fees

        logger.info(f"[Grid] Filled {level.side} order at {fill_price:.4f}, qty={level.quantity:.4f}, pnl={pnl:.2f}")

        await self.sync_position_state(position_id)

        result = {
            "success": True,
            "side": level.side,
            "price": fill_price,
            "quantity": level.quantity,
            "pnl_usdt": pnl,
            "fees": fees,
            "level_price": level.price,
        }
        if protection_result:
            result["protection"] = protection_result
        return result

    def _find_pair_level(self, position: GridPosition, filled_level: GridLevel) -> GridLevel | None:
        """Find a pair level for the filled level using FIFO (first-in-first-out) principle.

        P2-FIX: Changed from price-optimized pairing to FIFO pairing.
        FIFO is the standard accounting method and ensures correct PnL calculations.
        """
        if filled_level.filled_at is None:
            return None
        if filled_level.pair_level is not None:
            return None

        filled_price = filled_level.filled_price
        if filled_price <= 0:
            return None

        candidate_pairs = []

        for level in position.grid_levels:
            if level == filled_level:
                continue
            if level.status != "filled":
                continue
            if level.filled_at is None:
                continue
            if level.pair_level is not None:
                continue

            if filled_level.side == "buy" and level.side == "sell":
                if level.filled_price > filled_price:
                    candidate_pairs.append((level, level.filled_at))
            elif filled_level.side == "sell" and level.side == "buy":
                if level.filled_price < filled_price:
                    candidate_pairs.append((level, level.filled_at))

        if not candidate_pairs:
            return None

        # P2-FIX: Use FIFO pairing (earliest filled time first) instead of price optimization
        candidate_pairs.sort(key=lambda x: x[1])
        paired = candidate_pairs[0][0]
        paired.pair_level = filled_level.price
        return paired

    def _update_pnl(self, position: GridPosition, current_price: float) -> None:
        unrealized = 0.0

        for level in position.grid_levels:
            if level.status != "filled" or level.filled_price <= 0:
                continue
            if level.side == "buy":
                unrealized += (current_price - level.filled_price) * level.quantity
            else:
                unrealized += (level.filled_price - current_price) * level.quantity

        position.unrealized_pnl_usdt = unrealized

    def _replenish_grid(self, position: GridPosition, config: GridConfig, current_price: float) -> None:
        pending_buys = [level for level in position.grid_levels if level.status == "pending" and level.side == "buy"]

        if len(pending_buys) < config.grid_count // 4:
            new_upper = position.upper_price * (1 + config.grid_spacing_pct / 100)

            # BUG FIX: Copy all relevant config fields from original config
            new_config = GridConfig(
                ticker=config.ticker,
                direction=config.direction,
                spacing_mode=config.spacing_mode,
                leverage=config.leverage,
                fee_pct=config.fee_pct,
                slippage_pct=config.slippage_pct,
                paper_mode=config.paper_mode,
                upper_price=new_upper,
                lower_price=position.lower_price,
                grid_count=config.grid_count // 2,
                total_capital_usdt=config.total_capital_usdt * 0.2,
                grid_spacing_pct=config.grid_spacing_pct,
                take_profit_pct=config.take_profit_pct,
                stop_loss_pct=config.stop_loss_pct,
                max_open_orders=config.max_open_orders,
            )
            new_levels = self._calculate_grid_levels(new_config, current_price)

            for level in new_levels:
                if level.side == "buy" and level.price > position.upper_price:
                    position.grid_levels.append(level)

            position.upper_price = new_upper
            position.grid_levels.sort(key=lambda level: level.price)

            logger.info(f"[Grid] Replenished grid: new upper={new_upper:.4f}")

    async def _close_grid(self, position_id: str, exit_price: float, reason: str, exchange_config: dict | None = None) -> None:
        position = self.positions[position_id]
        config = self.configs.get(position_id)

        if config and not config.paper_mode:
            close_confirmed = True
            cancel_failures: list[str] = []
            try:
                from exchange import cancel_order

                for level in position.grid_levels:
                    if level.status == "pending" and level.order_id:
                        try:
                            await cancel_order(level.order_id, position.ticker, exchange_config)
                            level.order_id = ""
                            level.exchange_order_status = "cancelled"
                        except Exception as e:
                            logger.warning(f"[Grid] Failed to cancel grid order {level.order_id}: {e}")
                            cancel_failures.append(level.order_id)

            except Exception as e:
                logger.error(f"[Grid] Exchange close failed: {e}")
                close_confirmed = False

            net_quantity = position.total_buy_quantity - position.total_sell_quantity
            if abs(net_quantity) > 1e-12:
                try:
                    from exchange import execute_trade

                    close_direction = SignalDirection.CLOSE_LONG if net_quantity > 0 else SignalDirection.CLOSE_SHORT
                    decision = TradeDecision(
                        execute=True,
                        direction=close_direction,
                        ticker=position.ticker,
                        entry_price=exit_price,
                        quantity=abs(net_quantity),
                        reason=f"Grid close net exposure: {reason}",
                        order_type="market",
                        idempotency_key=f"grid:{position_id}:close:{reason}",
                    )
                    order_result = await execute_trade(decision, exchange_config)
                    if order_result.get("status") in ["closed", "filled", "simulated"]:
                        logger.info(f"[Grid] Closed net grid exposure via exchange: {order_result.get('order_id')}")
                    elif order_result.get("status") == "partial_closed":
                        logger.error(f"[Grid] Net exposure close only partially filled: {order_result}")
                        close_confirmed = False
                    else:
                        logger.error(f"[Grid] Failed to close net grid exposure: {order_result}")
                        close_confirmed = False
                except Exception as e:
                    logger.error(f"[Grid] Net exposure close failed: {e}")
                    close_confirmed = False

            if cancel_failures or not close_confirmed:
                position.updated_at = utcnow()
                await self.sync_position_state(position_id)
                raise RuntimeError("Grid exchange close was not fully confirmed; keeping grid active")

        closing_pnl = 0.0
        for level in position.grid_levels:
            if level.status != "filled":
                continue
            if level.side == "buy":
                level.pnl_usdt = (exit_price - level.filled_price) * level.quantity - level.fees_usdt
            else:
                level.pnl_usdt = (level.filled_price - exit_price) * level.quantity - level.fees_usdt
            closing_pnl += level.pnl_usdt
            level.status = "closed"

        final_pnl = position.realized_pnl_usdt + closing_pnl

        position.status = "closed"
        position.closed_at = utcnow()
        position.close_reason = reason
        position.realized_pnl_usdt = final_pnl

        logger.info(f"[Grid] Closed grid {position_id}: reason={reason}, final_pnl={final_pnl:.2f}, trades={position.total_trades}")

        await self.sync_position_state(position_id)

    def get_grid_status(self, position_id: str) -> dict:
        position = self.positions.get(position_id)
        if not position:
            return {"error": "Position not found"}

        return {
            "config_id": position.config_id,
            "ticker": position.ticker,
            "mode": position.mode,
            "status": position.status,
            "upper_price": round(position.upper_price, 6),
            "lower_price": round(position.lower_price, 6),
            "current_price": round(position.current_price, 6),
            "grid_count": len(position.grid_levels),
            "filled_buy_count": position.filled_buy_count,
            "filled_sell_count": position.filled_sell_count,
            "pending_orders": len([level for level in position.grid_levels if level.status == "pending"]),
            "total_trades": position.total_trades,
            "realized_pnl_usdt": round(position.realized_pnl_usdt, 2),
            "unrealized_pnl_usdt": round(position.unrealized_pnl_usdt, 2),
            "total_fees_usdt": round(position.total_fees_usdt, 2),
            "total_capital_usdt": round(position.total_capital_usdt, 2),
            "started_at": position.started_at.isoformat(),
            "grid_levels": [
                {
                    "price": round(level.price, 6),
                    "quantity": round(level.quantity, 6),
                    "side": level.side,
                    "status": level.status,
                    "filled_price": round(level.filled_price, 6) if level.filled_price else None,
                    "pnl_usdt": round(level.pnl_usdt, 2),
                }
                for level in position.grid_levels[:20]
            ],
        }

    async def get_grid_status_async(self, position_id: str) -> dict:
        if position_id not in self.positions:
            await self.load_position_state(position_id)
        return self.get_grid_status(position_id)

    def list_active_grids(self) -> list[dict]:
        return [
            self.get_grid_status(pid)
            for pid, pos in self.positions.items()
            if pos.status == "active"
        ]

    async def list_active_grids_async(self) -> list[dict]:
        await self.refresh_active_from_redis()
        return self.list_active_grids()

    def remove_grid(self, position_id: str) -> bool:
        if position_id in self.positions:
            del self.positions[position_id]
            if position_id in self.configs:
                del self.configs[position_id]
            if settings.redis.enabled:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    asyncio.run(self.remove_grid_async(position_id))
                else:
                    loop.create_task(self.remove_grid_async(position_id))
            return True
        return False

    async def remove_grid_async(self, position_id: str) -> bool:
        existed = position_id in self.positions
        self.positions.pop(position_id, None)
        self.configs.pop(position_id, None)
        await redis_hdel(_GRID_ACTIVE_HASH, position_id)
        await redis_hdel(_GRID_STATE_HASH, position_id)
        return existed

    def to_dict(self) -> dict:
        return {
            "active_grids": len([p for p in self.positions.values() if p.status == "active"]),
            "total_grids": len(self.positions),
            "grids": {pid: self.get_grid_status(pid) for pid in self.positions},
        }
