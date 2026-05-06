"""
P2-FIX: Configuration Hot-Reload Mechanism
Dynamic configuration updates without service restart.

Features:
    - Watch config files for changes
    - Validate changes before applying
    - Notify registered callbacks
    - Atomic config updates
    - Change history logging
"""
import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class ConfigHotReloader:
    """Configuration hot-reloader with file watching.

    P2-FIX: Allows dynamic config updates without restart.

    Example:
        reloader = ConfigHotReloader("config.json")

        reloader.register_callback("trading.leverage", update_leverage)
        reloader.register_callback("ai.timeout", update_ai_timeout)

        await reloader.start()

        # Config changes will trigger callbacks automatically
    """

    def __init__(
        self,
        config_path: str = "./config/runtime.json",
        validate_changes: bool = True,
        history_file: str = "./data/config_changes.json",
    ):
        """Initialize config hot-reloader.

        Args:
            config_path: Config file to watch
            validate_changes: Enable validation before applying
            history_file: File to log change history
        """
        self.config_path = Path(config_path)
        self.validate_changes = validate_changes
        self.history_file = Path(history_file)

        self.callbacks: dict[str, Callable] = {}
        self._observer: Observer | None = None
        self._current_config: dict[str, Any] = {}
        self._current_hash: str = ""
        self._change_count: int = 0

        # Create history file
        self.history_file.parent.mkdir(parents=True, exist_ok=True)

        logger.info(
            f"[P2-FIX] ConfigHotReloader initialized: "
            f"config={config_path}, validate={validate_changes}"
        )

    def register_callback(
        self,
        section: str,
        callback: Callable[[Any], None],
    ) -> None:
        """Register callback for config section changes.

        Args:
            section: Config section path (e.g., "trading.leverage")
            callback: Function to call when section changes
        """
        self.callbacks[section] = callback
        logger.debug(f"[P2-FIX] Registered callback for config section: {section}")

    def unregister_callback(self, section: str) -> None:
        """Unregister callback for config section.

        Args:
            section: Config section path
        """
        if section in self.callbacks:
            del self.callbacks[section]
            logger.debug(f"[P2-FIX] Unregistered callback for config section: {section}")

    def load_config(self) -> dict[str, Any]:
        """Load current config from file.

        Returns:
            Current config dict
        """
        try:
            if not self.config_path.exists():
                logger.warning(f"[P2-FIX] Config file not found: {self.config_path}")
                return {}

            with open(self.config_path) as f:
                config = json.load(f)

            return config

        except Exception as e:
            logger.error(f"[P2-FIX] Error loading config: {e}")
            return {}

    def calculate_hash(self, config: dict[str, Any]) -> str:
        """Calculate hash of config for change detection.

        Args:
            config: Config dict

        Returns:
            SHA256 hash string
        """
        config_str = json.dumps(config, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(config_str.encode()).hexdigest()[:16]

    async def start(self) -> None:
        """Start watching config file for changes."""
        # Load initial config
        self._current_config = self.load_config()
        self._current_hash = self.calculate_hash(self._current_config)

        # Setup file watcher
        event_handler = ConfigChangeHandler(self._on_config_change)
        self._observer = Observer()
        self._observer.schedule(
            event_handler,
            str(self.config_path.parent),
            recursive=False,
        )
        self._observer.start()

        logger.info(
            f"[P2-FIX] Config hot-reloader started: "
            f"watching {self.config_path}, "
            f"initial_hash={self._current_hash}"
        )

    def _on_config_change(self, event_path: str) -> None:
        """Handle config file change event.

        Args:
            event_path: Path to changed file
        """
        if event_path != str(self.config_path):
            return

        # Schedule async processing
        asyncio.create_task(self._process_config_change())

    async def _process_config_change(self) -> None:
        """Process config file change asynchronously."""
        try:
            # Load new config
            new_config = self.load_config()
            new_hash = self.calculate_hash(new_config)

            # Check if actually changed
            if new_hash == self._current_hash:
                logger.debug("[P2-FIX] Config file modified but content unchanged")
                return

            logger.info(
                f"[P2-FIX] Config change detected: "
                f"old_hash={self._current_hash} -> new_hash={new_hash}"
            )

            # Validate changes
            if self.validate_changes:
                validation_errors = self._validate_config(new_config)
                if validation_errors:
                    logger.error(
                        f"[P2-FIX] Config validation failed: {validation_errors}. "
                        f"Changes not applied."
                    )
                    return

            # Detect which sections changed
            changed_sections = self._detect_changed_sections(
                self._current_config,
                new_config,
            )

            # Apply callbacks
            for section in changed_sections:
                if section in self.callbacks:
                    try:
                        new_value = self._get_section_value(new_config, section)

                        # Call callback
                        if asyncio.iscoroutinefunction(self.callbacks[section]):
                            await self.callbacks[section](new_value)
                        else:
                            self.callbacks[section](new_value)

                        logger.info(
                            f"[P2-FIX] Applied config change: {section} = {new_value}"
                        )

                    except Exception as e:
                        logger.error(
                            f"[P2-FIX] Error applying config change for {section}: {e}"
                        )

            # Update current config
            self._current_config = new_config
            self._current_hash = new_hash
            self._change_count += 1

            # Log change history
            self._log_change_history(changed_sections)

        except Exception as e:
            logger.error(f"[P2-FIX] Error processing config change: {e}")

    def _validate_config(self, config: dict[str, Any]) -> list[str]:
        """Validate config changes.

        Args:
            config: New config dict

        Returns:
            List of validation error messages
        """
        errors = []

        # Validate trading settings
        trading = config.get("trading", {})

        if "leverage" in trading:
            leverage = trading.get("leverage")
            if not (1 <= leverage <= 125):
                errors.append(f"Leverage must be between 1 and 125 (got {leverage})")

        if "max_position_pct" in trading:
            max_pct = trading.get("max_position_pct")
            if not (0 < max_pct <= 100):
                errors.append(f"max_position_pct must be between 0 and 100 (got {max_pct})")

        # Validate AI settings
        ai = config.get("ai", {})

        if "timeout" in ai:
            timeout = ai.get("timeout")
            if not (5 <= timeout <= 300):
                errors.append(f"AI timeout must be between 5 and 300 seconds (got {timeout})")

        if "temperature" in ai:
            temperature = ai.get("temperature")
            if not (0 <= temperature <= 2):
                errors.append(f"AI temperature must be between 0 and 2 (got {temperature})")

        return errors

    def _detect_changed_sections(
        self,
        old_config: dict[str, Any],
        new_config: dict[str, Any],
    ) -> list[str]:
        """Detect which config sections changed.

        Args:
            old_config: Previous config
            new_config: New config

        Returns:
            List of changed section paths
        """
        changed_sections = []

        # Compare registered sections
        for section in self.callbacks.keys():
            old_value = self._get_section_value(old_config, section)
            new_value = self._get_section_value(new_config, section)

            if old_value != new_value:
                changed_sections.append(section)

        return changed_sections

    def _get_section_value(
        self,
        config: dict[str, Any],
        section: str,
    ) -> Any:
        """Get value for config section path.

        Args:
            config: Config dict
            section: Section path (e.g., "trading.leverage")

        Returns:
            Section value or None
        """
        keys = section.split(".")
        value = config

        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return None

        return value

    def _log_change_history(self, changed_sections: list[str]) -> None:
        """Log config change to history file.

        Args:
            changed_sections: List of changed sections
        """
        try:
            history = []

            if self.history_file.exists():
                with open(self.history_file) as f:
                    history = json.load(f)

            history.append({
                "timestamp": datetime.utcnow().isoformat(),
                "change_count": self._change_count,
                "hash": self._current_hash,
                "changed_sections": changed_sections,
            })

            # Keep last 100 changes
            history = history[-100:]

            with open(self.history_file, "w") as f:
                json.dump(history, f, indent=2, default=str)

        except Exception as e:
            logger.warning(f"[P2-FIX] Error logging change history: {e}")

    def stop(self) -> None:
        """Stop watching config file."""
        if self._observer:
            self._observer.stop()
            self._observer.join()
            logger.info("[P2-FIX] Config hot-reloader stopped")

    def get_current_config(self) -> dict[str, Any]:
        """Get current config."""
        return self._current_config.copy()

    def get_change_count(self) -> int:
        """Get total number of config changes applied."""
        return self._change_count


class ConfigChangeHandler(FileSystemEventHandler):
    """File system event handler for config changes."""

    def __init__(self, callback: Callable[[str], None]):
        self.callback = callback

    def on_modified(self, event):
        """Handle file modified event."""
        if not event.is_directory:
            self.callback(event.src_path)


# Global config hot-reloader instance
_CONFIG_HOT_RELOADER: ConfigHotReloader | None = None


async def get_config_hot_reloader() -> ConfigHotReloader:
    """Get or create global config hot-reloader instance."""
    global _CONFIG_HOT_RELOADER

    if _CONFIG_HOT_RELOADER is None:
        _CONFIG_HOT_RELOADER = ConfigHotReloader(
            config_path="./config/runtime.json",
            validate_changes=True,
            history_file="./data/config_changes.json",
        )
        await _CONFIG_HOT_RELOADER.start()

    return _CONFIG_HOT_RELOADER


def register_config_callback(section: str, callback: Callable) -> None:
    """Register callback for config section changes.

    P2-FIX: Helper function to register callbacks without await.

    Args:
        section: Config section path
        callback: Callback function
    """
    if _CONFIG_HOT_RELOADER:
        _CONFIG_HOT_RELOADER.register_callback(section, callback)
    else:
        logger.warning(
            f"[P2-FIX] Config hot-reloader not initialized, "
            f"callback for {section} will be registered on next start"
        )
