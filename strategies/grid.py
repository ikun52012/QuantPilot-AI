"""
Grid Trading Strategy Engine.
Manages automated buy/sell orders within a price range.
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


@dataclass
class GridLevel:
    price: float
    quantity: float
    side: str
    order_id: str = ""
    status: str = "pending"
    filled_at: Optional[datetime] = None
    filled_price: float = 0.0
    pnl_usdt: float = 0.0
    fees_usdt: float = 0.0
    pair_level: Optional[int] = None


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
    closed_at: Optional[datetime] = None
    close_reason: str = ""
    pending_orders: int = 0
    open_pairs: list[dict] = field(default_factory=list)


class GridEngine:
    def __init__(self):
        self.positions: dict[str, GridPosition] = {}
        self.configs: dict[str, GridConfig] = {}
        self.price_cache: dict[str, float] = {}
        self._monitor_task: Optional[asyncio.Task] = None

    def _ensure_strategy_id(self, config: GridConfig) -> None:
        if not config.strategy_id:
            config.strategy_id = f"grid_{config.ticker}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

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

        position.pending_orders = len([l for l in grid_levels if l.status == "pending"])

        self.positions[config.strategy_id] = position
        self.configs[config.strategy_id] = config
        return position

    def create_grid(
        self,
        config: GridConfig,
        current_price: float,
        exchange_config: dict | None = None,
    ) -> GridPosition:
        if config.paper_mode:
            position = self._initialize_grid_position(config, current_price)
            logger.info(f"[Grid] Paper mode - simulated grid creation with {len(position.grid_levels)} levels")
            logger.info(f"[Grid] Created grid for {config.ticker}: range={config.lower_price:.4f}-{config.upper_price:.4f}, levels={len(position.grid_levels)}")
            return position

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.create_grid_async(config, current_price, exchange_config))

        raise RuntimeError("Use create_grid_async() for live exchange execution")

    async def create_grid_async(
        self,
        config: GridConfig,
        current_price: float,
        exchange_config: dict | None = None
    ) -> GridPosition:
        if config.paper_mode:
            return self.create_grid(config, current_price, exchange_config)

        position = self._initialize_grid_position(config, current_price)

        try:
            from exchange import execute_trade

            for level in position.grid_levels:
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
                )

                order_result = await execute_trade(decision, exchange_config)

                if order_result.get("status") in ["filled", "pending", "simulated"]:
                    level.order_id = order_result.get("order_id", "")
                    logger.info(f"[Grid] Placed grid order {level.side} @ {level.price}: {order_result.get('order_id')}")
                else:
                    logger.error(f"[Grid] Failed to place grid order: {order_result}")

        except Exception as e:
            logger.error(f"[Grid] Exchange execution failed: {e}")
            raise

        logger.info(f"[Grid] Created grid for {config.ticker}: range={config.lower_price:.4f}-{config.upper_price:.4f}, levels={len(position.grid_levels)}")

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
                else:
                    quantity = config.total_capital_usdt / config.grid_count / price

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
                else:
                    quantity = config.total_capital_usdt / config.grid_count / price

                levels.append(GridLevel(
                    price=round(price, 8),
                    quantity=round(quantity, 6),
                    side=side,
                ))

        levels.sort(key=lambda l: l.price)

        for i, level in enumerate(levels):
            level.pair_level = i

        return levels

    def _calculate_grid_stop_loss(self, config: GridConfig, price: float, side: str) -> Optional[float]:
        if config.stop_loss_pct <= 0:
            return None
        if side == "buy":
            return price * (1 - config.stop_loss_pct / 100)
        else:
            return price * (1 + config.stop_loss_pct / 100)

    def _calculate_grid_take_profit(self, config: GridConfig, price: float, side: str) -> Optional[float]:
        if config.take_profit_pct <= 0:
            return None
        if side == "buy":
            return price * (1 + config.take_profit_pct / 100)
        else:
            return price * (1 - config.take_profit_pct / 100)

    async def check_and_execute(self, position_id: str, current_price: float, exchange_config: dict | None = None) -> dict:
        result = {"action": "none", "trades": []}

        if position_id not in self.positions:
            return {"action": "error", "reason": "Position not found"}

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

        for level in triggered_levels:
            trade_result = await self._execute_grid_level(position_id, level, current_price, config, exchange_config)
            if trade_result.get("success"):
                result["trades"].append(trade_result)

        if result["trades"]:
            result["action"] = "grid_trade"

        self._update_pnl(position, current_price)

        if config.auto_replenish and position.filled_buy_count > config.grid_count * config.replenish_threshold_pct / 100:
            self._replenish_grid(position, config, current_price)

        position.updated_at = utcnow()

        return result

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

        fees = level.quantity * fill_price * config.fee_pct / 100
        level.fees_usdt = fees
        level.filled_at = utcnow()
        level.filled_price = fill_price
        level.status = "filled"

        pnl = 0.0

        if not config.paper_mode:
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
                )

                order_result = await execute_trade(decision, exchange_config)

                if order_result.get("status") in ["filled", "simulated"]:
                    level.order_id = order_result.get("order_id", "")
                    logger.info(f"[Grid] Executed grid trade: {order_result.get('order_id')}")
                else:
                    logger.error(f"[Grid] Failed to execute grid trade: {order_result}")

            except Exception as e:
                logger.error(f"[Grid] Exchange execution failed: {e}")

        pair_level = self._find_pair_level(position, level)

        if pair_level and pair_level.status == "filled":
            if level.side == "buy" and pair_level.side == "sell":
                pnl = (pair_level.filled_price - fill_price) * min(level.quantity, pair_level.quantity)
            elif level.side == "sell" and pair_level.side == "buy":
                pnl = (fill_price - pair_level.filled_price) * min(level.quantity, pair_level.quantity)

            pnl -= fees + pair_level.fees_usdt

            position.realized_pnl_usdt += pnl
            position.total_trades += 1

        if level.side == "buy":
            position.filled_buy_count += 1
            position.total_buy_quantity += level.quantity
        else:
            position.filled_sell_count += 1
            position.total_sell_quantity += level.quantity

        position.total_fees_usdt += fees

        logger.info(f"[Grid] Filled {level.side} order at {fill_price:.4f}, qty={level.quantity:.4f}, pnl={pnl:.2f}")

        return {
            "success": True,
            "side": level.side,
            "price": fill_price,
            "quantity": level.quantity,
            "pnl_usdt": pnl,
            "fees": fees,
            "level_price": level.price,
        }

    def _find_pair_level(self, position: GridPosition, filled_level: GridLevel) -> Optional[GridLevel]:
        if filled_level.filled_at is None:
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
                    candidate_pairs.append((level, level.filled_price))
            elif filled_level.side == "sell" and level.side == "buy":
                if level.filled_price < filled_price:
                    candidate_pairs.append((level, level.filled_price))

        if not candidate_pairs:
            return None

        if filled_level.side == "buy":
            candidate_pairs.sort(key=lambda x: x[1])
            paired = candidate_pairs[0][0]
            paired.pair_level = filled_level.price
            return paired
        else:
            candidate_pairs.sort(key=lambda x: x[1], reverse=True)
            paired = candidate_pairs[0][0]
            paired.pair_level = filled_level.price
            return paired

    def _update_pnl(self, position: GridPosition, current_price: float) -> None:
        unrealized = 0.0

        for level in position.grid_levels:
            if level.status == "filled" and level.side == "buy":
                unrealized += (current_price - level.filled_price) * level.quantity

        position.unrealized_pnl_usdt = unrealized

    def _replenish_grid(self, position: GridPosition, config: GridConfig, current_price: float) -> None:
        pending_buys = [l for l in position.grid_levels if l.status == "pending" and l.side == "buy"]

        if len(pending_buys) < config.grid_count // 4:
            new_upper = position.upper_price * (1 + config.grid_spacing_pct / 100)

            new_levels = self._calculate_grid_levels(GridConfig(
                upper_price=new_upper,
                lower_price=position.lower_price,
                grid_count=config.grid_count // 2,
                total_capital_usdt=config.total_capital_usdt * 0.2,
            ), current_price)

            for level in new_levels:
                if level.side == "buy" and level.price > position.upper_price:
                    position.grid_levels.append(level)

            position.upper_price = new_upper
            position.grid_levels.sort(key=lambda l: l.price)

            logger.info(f"[Grid] Replenished grid: new upper={new_upper:.4f}")

    async def _close_grid(self, position_id: str, exit_price: float, reason: str, exchange_config: dict | None = None) -> None:
        position = self.positions[position_id]
        config = self.configs.get(position_id)

        if config and not config.paper_mode:
            try:
                from exchange import execute_trade

                for level in position.grid_levels:
                    if level.status == "pending" and level.order_id:
                        try:
                            direction = SignalDirection.LONG if level.side == "buy" else SignalDirection.SHORT
                            close_dir = SignalDirection.CLOSE_LONG if direction == SignalDirection.LONG else SignalDirection.CLOSE_SHORT
                            decision = TradeDecision(
                                execute=True,
                                direction=close_dir,
                                ticker=position.ticker,
                                quantity=level.quantity,
                                reason=f"Grid close: {reason}",
                                order_type="market",
                            )
                            await execute_trade(decision, exchange_config)
                        except Exception as e:
                            logger.warning(f"[Grid] Failed to cancel grid order {level.order_id}: {e}")

            except Exception as e:
                logger.error(f"[Grid] Exchange close failed: {e}")

        final_pnl = position.realized_pnl_usdt + position.unrealized_pnl_usdt - position.total_fees_usdt

        position.status = "closed"
        position.closed_at = utcnow()
        position.close_reason = reason
        position.realized_pnl_usdt = final_pnl

        logger.info(f"[Grid] Closed grid {position_id}: reason={reason}, final_pnl={final_pnl:.2f}, trades={position.total_trades}")

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
            "pending_orders": len([l for l in position.grid_levels if l.status == "pending"]),
            "total_trades": position.total_trades,
            "realized_pnl_usdt": round(position.realized_pnl_usdt, 2),
            "unrealized_pnl_usdt": round(position.unrealized_pnl_usdt, 2),
            "total_fees_usdt": round(position.total_fees_usdt, 2),
            "total_capital_usdt": round(position.total_capital_usdt, 2),
            "started_at": position.started_at.isoformat(),
            "grid_levels": [
                {
                    "price": round(l.price, 6),
                    "quantity": round(l.quantity, 6),
                    "side": l.side,
                    "status": l.status,
                    "filled_price": round(l.filled_price, 6) if l.filled_price else None,
                    "pnl_usdt": round(l.pnl_usdt, 2),
                }
                for l in position.grid_levels[:20]
            ],
        }

    def list_active_grids(self) -> list[dict]:
        return [
            self.get_grid_status(pid)
            for pid, pos in self.positions.items()
            if pos.status == "active"
        ]

    def remove_grid(self, position_id: str) -> bool:
        if position_id in self.positions:
            del self.positions[position_id]
            if position_id in self.configs:
                del self.configs[position_id]
            return True
        return False

    def to_dict(self) -> dict:
        return {
            "active_grids": len([p for p in self.positions.values() if p.status == "active"]),
            "total_grids": len(self.positions),
            "grids": {pid: self.get_grid_status(pid) for pid in self.positions},
        }
