# utils/logger.py
"""
Logging system for ChainCrawlr:
- Console and rotating file handlers with JSON-structured file logs
- Integrates with TelegramHandler and DiscordHandler for notifications
- Uses JSONFileCache for caching configuration
- Integrates with ChainHelpers for timestamp formatting and address shortening
- Enhanced error handling and configuration validation
"""

import json
import logging
import logging.handlers
from datetime import datetime
from pathlib import Path

import yaml

from utils.caching import JSONFileCache
from utils.discord_handler import DiscordHandler
from utils.helpers import ChainHelpers
from utils.telegram_handler import TelegramHandler


class ChainCrawlerLogger:
    def __init__(self, config_path="config/settings.yaml", chain="ethereum", cache_dir=".cache"):
        """Initialize the ChainCrawlr logger with console, file, and notification handlers."""
        self.logger = logging.getLogger("ChainCrawler")
        self.logger.setLevel(logging.DEBUG)
        self.cache = JSONFileCache(cache_dir=cache_dir, max_age=86400)
        self.config = self._load_config(config_path)
        self.helpers = ChainHelpers(chain=chain)
        self._last_notification = {}
        self._setup_handlers()

    def _load_config(self, config_path):
        """Load configuration from YAML file, with caching and fallback to defaults."""
        config_path = Path(config_path)
        cache_key = f"config_{config_path.name}_{config_path.stat().st_mtime if config_path.exists() else 0}"
        
        cached_config = self.cache.get(cache_key)
        if cached_config is not None:
            self.logger.debug("Loaded config from cache: %s", cache_key)
            return cached_config

        try:
            if not config_path.exists():
                self.logger.warning("Config file not found: %s, using default settings", config_path)
                config = {
                    "bot": {"logging_level": "INFO", "notification_rate_limit": 60},
                    "notifications": {
                        "telegram": {"enabled": False},
                        "discord": {"enabled": False}
                    }
                }
            else:
                with config_path.open('r', encoding='utf-8') as f:
                    config = yaml.safe_load(f)
                    if not config:
                        self.logger.warning("Empty config file: %s, using default settings", config_path)
                        config = {
                            "bot": {"logging_level": "INFO", "notification_rate_limit": 60},
                            "notifications": {
                                "telegram": {"enabled": False},
                                "discord": {"enabled": False}
                            }
                        }
            self.cache.set(cache_key, config)
            self.logger.debug("Loaded and cached config: %s", config_path)
            return config
        except Exception as e:
            self.logger.error("Error loading config %s: %s, using default settings", config_path, str(e))
            return {
                "bot": {"logging_level": "INFO", "notification_rate_limit": 60},
                "notifications": {
                    "telegram": {"enabled": False},
                    "discord": {"enabled": False}
                }
            }

    def _setup_handlers(self):
        """Set up console, file, and notification handlers for logging."""
        self.logger.handlers = []

        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(self._get_log_level())
        console_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt=self.helpers.now_utc_str()
        )
        console_handler.setFormatter(console_formatter)
        self.logger.addHandler(console_handler)

        # File handler with rotation
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / f"chaincrawler_{datetime.now().strftime('%Y%m%d')}.log"
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5
        )
        file_handler.setLevel(self._get_log_level())
        file_formatter = logging.Formatter(
            '{"timestamp": "%(asctime)s", "level": "%(levelname)s", "file": "%(filename)s", '
            '"line": %(lineno)d, "chain": "' + self.helpers.chain + '", "message": "%(message)s", "extra": %(extra)s}',
            datefmt=self.helpers.now_utc_str()
        )
        file_handler.setFormatter(file_formatter)
        self.logger.addHandler(file_handler)

        # Notification handlers
        notifications = self.config.get("notifications", {})
        rate_limit_seconds = self.config.get("bot", {}).get("notification_rate_limit", 60)

        if notifications.get("telegram", {}).get("enabled", False):
            telegram_config = notifications.get("telegram", {})
            if not telegram_config.get("bot_token") or not telegram_config.get("chat_id"):
                self.logger.warning("Telegram notification disabled: missing bot_token or chat_id")
            else:
                telegram_handler = TelegramHandler(telegram_config, rate_limit_seconds, self.cache)
                telegram_handler.setLevel(logging.WARNING)
                telegram_handler.setFormatter(console_formatter)
                self.logger.addHandler(telegram_handler)

        if notifications.get("discord", {}).get("enabled", False):
            discord_config = notifications.get("discord", {})
            if not discord_config.get("webhook_url"):
                self.logger.warning("Discord notification disabled: missing webhook_url")
            else:
                discord_handler = DiscordHandler(discord_config, rate_limit_seconds, self.cache)
                discord_handler.setLevel(logging.WARNING)
                discord_handler.setFormatter(console_formatter)
                self.logger.addHandler(discord_handler)

    def _get_log_level(self):
        """Get logging level from config, defaulting to INFO."""
        level = self.config.get("bot", {}).get("logging_level", "INFO").upper()
        try:
            return getattr(logging, level, logging.INFO)
        except AttributeError:
            self.logger.warning("Invalid logging level %s, defaulting to INFO", level)
            return logging.INFO

    def _enrich_message(self, message, extra):
        """Enrich log message with extra data, such as shortened addresses."""
        if not isinstance(message, str):
            message = str(message)
        extra = extra or {}
        try:
            for key, value in extra.items():
                if key.endswith("_address") and isinstance(value, str):
                    try:
                        shortened = self.helpers.shorten_address(value)
                        if shortened != value:
                            extra[key] = shortened
                    except ValueError:
                        self.logger.debug("Skipping address shortening for invalid address: %s", value)
            return message, extra
        except Exception as e:
            self.logger.error("Failed to enrich message: %s", str(e))
            return message, extra

    def debug(self, message, extra=None):
        """Log a debug message with optional extra data."""
        message, extra = self._enrich_message(message, extra)
        self.logger.debug(message, extra=extra)

    def info(self, message, extra=None):
        """Log an info message with optional extra data."""
        message, extra = self._enrich_message(message, extra)
        self.logger.info(message, extra=extra)

    def warning(self, message, extra=None):
        """Log a warning message with optional extra data."""
        message, extra = self._enrich_message(message, extra)
        self.logger.warning(message, extra=extra)

    def error(self, message, extra=None):
        """Log an error message with optional extra data."""
        message, extra = self._enrich_message(message, extra)
        self.logger.error(message, extra=extra)

    def critical(self, message, extra=None):
        """Log a critical message with optional extra data."""
        message, extra = self._enrich_message(message, extra)
        self.logger.critical(message, extra=extra)


# Singleton instance
logger = ChainCrawlerLogger()

# Convenience functions
def debug(message, extra=None):
    logger.debug(message, extra)

def info(message, extra=None):
    logger.info(message, extra)

def warning(message, extra=None):
    logger.warning(message, extra)

def error(message, extra=None):
    logger.error(message, extra)

def critical(message, extra=None):
    logger.critical(message, extra)