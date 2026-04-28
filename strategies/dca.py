"""
DCA (Dollar Cost Average) Strategy Engine.
Manages position averaging down/up with configurable parameters.
Enhanced with live exchange execution support.
"""
import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from dataclasses import dataclass, field
from enum import Enum
from loguru import logger

from core.utils.datetime import utcnow
from models import TradeDecision, SignalDirection, TakeProfitLevel, TrailingStopConfig, TrailingStopMode


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
    closed_at: Optional[datetime] = None
    close_reason: str = ""


class DCAEngine:
    def __init__(self):
        self.positions: dict[str, DCAPosition] = {}
        self.configs: dict[str, DCAConfig] = {}
        self.price_cache: dict[str, float] = {}
        self._monitor_task: Optional[asyncio.Task] = None

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
        logger.info(f"[DCA] Paper mode - simulated initial entry")
        return self._finalize_position(position, config, entry, current_price)

    def create_position(
        self,
        config: DCAConfig,
        current_price: float,
        exchange_config: dict | None = None,
    ) -> DCAPosition:
        if config.paper_mode:
            return self._create_position_paper(config, current_price)

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
            return self._create_position_paper(config, current_price)

        self._ensure_strategy_id(config)
        position = self._build_position(config)

        initial_qty = self._calculate_initial_quantity(config, current_price)
        initial_capital = initial_qty * current_price

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

        return self._finalize_position(position, config, entry, current_price)

    def _calculate_initial_quantity(self, config: DCAConfig, price: float) -> float:
        if config.initial_capital_usdt > 0:
            return config.initial_capital_usdt / price
        elif config.fixed_size_usdt > 0:
            return config.fixed_size_usdt / price
        return 0.0

    def _calculate_stop_loss_price(self, config: DCAConfig, entry_price: float, direction: SignalDirection) -> Optional[float]:
        if config.stop_loss_pct <= 0:
            return None
        if direction == SignalDirection.LONG:
            return entry_price * (1 - config.stop_loss_pct / 100)
        else:
            return entry_price * (1 + config.stop_loss_pct / 100)

    def _calculate_take_profit_price(self, config: DCAConfig, entry_price: float, direction: SignalDirection) -> Optional[float]:
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

        next_entry_idx = len(position.entries) + 1

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
        result = {"action": "none", "reason": ""}

        if position_id not in self.positions:
            return {"action": "error", "reason": "Position not found"}

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

        if config.mode == "average_down":
            if position.direction == "long":
                loss_pct = (position.average_entry_price - current_price) / position.average_entry_price * 100
                return loss_pct >= config.activation_loss_pct
            else:
                loss_pct = (current_price - position.average_entry_price) / position.average_entry_price * 100
                return loss_pct >= config.activation_loss_pct

        elif config.mode == "average_up":
            if position.direction == "long":
                gain_pct = (current_price - position.average_entry_price) / position.average_entry_price * 100
                return gain_pct >= config.activation_loss_pct
            else:
                gain_pct = (position.average_entry_price - current_price) / position.average_entry_price * 100
                return gain_pct >= config.activation_loss_pct

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
        position.entries_remaining -= 1

        total_qty = position.total_quantity
        total_capital = position.total_capital_usdt

        weighted_avg = sum(e.entry_price * e.quantity for e in position.entries) / total_qty
        position.average_entry_price = weighted_avg

        self._update_stop_take(position, config)
        self._calculate_next_entry(position, config)

        position.updated_at = utcnow()

        logger.info(f"[DCA] Added entry #{new_entry_idx} for {position.ticker}: price={current_price}, qty={new_quantity}, avg_entry={weighted_avg:.4f}")

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
                else:
                    logger.error(f"[DCA] Failed to close position: {order_result}")

            except Exception as e:
                logger.error(f"[DCA] Exchange close failed: {e}")

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

    def list_active_positions(self) -> list[dict]:
        return [
            self.get_position_status(pid)
            for pid, pos in self.positions.items()
            if pos.status == "active"
        ]

    def remove_position(self, position_id: str) -> bool:
        if position_id in self.positions:
            del self.positions[position_id]
            if position_id in self.configs:
                del self.configs[position_id]
            return True
        return False

    def to_dict(self) -> dict:
        return {
            "active_positions": len([p for p in self.positions.values() if p.status == "active"]),
            "total_positions": len(self.positions),
            "positions": {pid: self.get_position_status(pid) for pid in self.positions},
        }
