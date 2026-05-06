"""
P2-FIX: Event Bus for Event-Driven Architecture
Centralized event dispatcher with async handlers and event persistence.
"""
import asyncio
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger

from .event_types import Event, EventTypes


class EventHandler:
    """Event handler wrapper with priority support."""
    
    def __init__(
        self,
        callback: Callable,
        handler_name: str = "",
        priority: int = 0,  # Lower = higher priority
    ):
        self.callback = callback
        self.handler_name = handler_name or callback.__name__
        self.priority = priority
        self.is_async = asyncio.iscoroutinefunction(callback)


class EventBus:
    """Centralized event bus for inter-component communication.
    
    P2-FIX: Decouples components using publish-subscribe pattern.
    
    Features:
        - Async event handlers
        - Priority-based handler ordering
        - Event persistence for audit trail
        - Wildcard event subscriptions
        - Metrics for event processing
    
    Example:
        bus = EventBus()
        
        # Subscribe to specific event
        bus.subscribe(EventTypes.TRADE_EXECUTED, on_trade_executed, priority=1)
        
        # Subscribe to all events (wildcard)
        bus.subscribe_wildcard(log_all_events)
        
        # Publish event
        await bus.publish(TradeEvent(
            event_type=EventTypes.TRADE_EXECUTED,
            ticker="BTCUSDT",
            direction="long",
            status="filled",
        ))
    """
    
    def __init__(
        self,
        persist_events: bool = True,
        event_store_path: str = "./data/events",
        max_event_history: int = 1000,
    ):
        """Initialize EventBus.
        
        Args:
            persist_events: Enable event persistence for audit
            event_store_path: Path for event log files
            max_event_history: Maximum events to keep in memory
        """
        self._handlers: dict[EventTypes, list[EventHandler]] = defaultdict(list)
        self._wildcard_handlers: list[EventHandler] = []
        self._event_history: list[Event] = []
        self._max_event_history = max_event_history
        self._persist_events = persist_events
        self._event_store_path = Path(event_store_path)
        
        # Metrics
        self._metrics = {
            "events_published": 0,
            "events_processed": 0,
            "handlers_executed": 0,
            "handler_errors": 0,
        }
        
        # Initialize event store
        if self._persist_events:
            self._event_store_path.mkdir(parents=True, exist_ok=True)
            logger.info(f"[P2-FIX] EventBus initialized with event persistence: {self._event_store_path}")
        else:
            logger.info("[P2-FIX] EventBus initialized (no persistence)")
    
    def subscribe(
        self,
        event_type: EventTypes,
        handler: Callable,
        handler_name: str = "",
        priority: int = 0,
    ) -> None:
        """Subscribe to specific event type.
        
        Args:
            event_type: Event type to subscribe
            handler: Callback function (async or sync)
            handler_name: Handler identifier for logging
            priority: Handler priority (lower = higher priority)
        """
        handler_wrapper = EventHandler(handler, handler_name, priority)
        self._handlers[event_type].append(handler_wrapper)
        
        # Sort by priority (lower first)
        self._handlers[event_type].sort(key=lambda h: h.priority)
        
        logger.debug(
            f"[P2-FIX] Handler subscribed: {event_type} -> {handler_wrapper.handler_name} (priority={priority})"
        )
    
    def subscribe_wildcard(
        self,
        handler: Callable,
        handler_name: str = "",
        priority: int = 100,  # Wildcard handlers typically have lower priority
    ) -> None:
        """Subscribe to all events (wildcard).
        
        Args:
            handler: Callback function
            handler_name: Handler identifier
            priority: Handler priority
        """
        handler_wrapper = EventHandler(handler, handler_name, priority)
        self._wildcard_handlers.append(handler_wrapper)
        self._wildcard_handlers.sort(key=lambda h: h.priority)
        
        logger.debug(f"[P2-FIX] Wildcard handler subscribed: {handler_wrapper.handler_name}")
    
    async def publish(self, event: Event) -> None:
        """Publish event to all subscribers.
        
        Args:
            event: Event to publish
        """
        # Add to history
        self._event_history.append(event)
        if len(self._event_history) > self._max_event_history:
            self._event_history.pop(0)
        
        # Persist event
        if self._persist_events:
            await self._persist_event(event)
        
        # Update metrics
        self._metrics["events_published"] += 1
        
        # Get handlers for this event
        handlers = self._handlers.get(event.event_type, [])
        all_handlers = handlers + self._wildcard_handlers
        
        if not all_handlers:
            logger.debug(f"[P2-FIX] No handlers for event: {event.event_type}")
            return
        
        logger.debug(
            f"[P2-FIX] Publishing event {event.event_type} to {len(all_handlers)} handlers"
        )
        
        # Execute handlers
        for handler_wrapper in all_handlers:
            try:
                if handler_wrapper.is_async:
                    await handler_wrapper.callback(event)
                else:
                    await asyncio.to_thread(handler_wrapper.callback, event)
                
                self._metrics["handlers_executed"] += 1
                
            except Exception as e:
                self._metrics["handler_errors"] += 1
                logger.error(
                    f"[P2-FIX] Handler error: {handler_wrapper.handler_name} "
                    f"for event {event.event_type}: {e}"
                )
        
        self._metrics["events_processed"] += 1
    
    async def _persist_event(self, event: Event) -> None:
        """Persist event to disk for audit trail."""
        try:
            date_str = datetime.utcnow().strftime("%Y-%m-%d")
            event_file = self._event_store_path / f"events_{date_str}.json"
            
            # Append event to daily log
            event_dict = {
                "event_type": event.event_type,
                "timestamp": event.timestamp.isoformat(),
                "event_id": event.event_id,
                "source": event.source,
                "data": event.data,
                "metadata": event.metadata,
            }
            
            async with asyncio.Lock():
                # Read existing events
                events = []
                if event_file.exists():
                    with open(event_file, "r") as f:
                        events = json.load(f)
                
                # Append new event
                events.append(event_dict)
                
                # Write back
                with open(event_file, "w") as f:
                    json.dump(events, f, indent=2, default=str)
            
        except Exception as e:
            logger.warning(f"[P2-FIX] Event persistence error: {e}")
    
    def get_metrics(self) -> dict[str, Any]:
        """Get event bus metrics."""
        return {
            **self._metrics,
            "handlers_registered": sum(len(h) for h in self._handlers.values()),
            "wildcard_handlers": len(self._wildcard_handlers),
            "events_in_history": len(self._event_history),
        }
    
    def get_recent_events(self, limit: int = 50) -> list[Event]:
        """Get recent events from history."""
        return self._event_history[-limit:]
    
    def clear_handlers(self) -> None:
        """Clear all handlers (for testing)."""
        self._handlers.clear()
        self._wildcard_handlers.clear()
        logger.debug("[P2-FIX] All event handlers cleared")


# Global EventBus instance (singleton)
_EVENT_BUS: Optional[EventBus] = None


async def get_event_bus() -> EventBus:
    """Get or create global EventBus instance."""
    global _EVENT_BUS
    if _EVENT_BUS is None:
        from core.config import settings
        
        _EVENT_BUS = EventBus(
            persist_events=True,
            event_store_path="./data/events",
            max_event_history=1000,
        )
    
    return _EVENT_BUS