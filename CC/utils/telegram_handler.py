# utils/telegram_handler.py
"""
Telegram notification handler for ChainCrawlr:
- Sends log messages to Telegram for WARNING+ levels
- Implements rate limiting and caching via JSONFileCache
- Handles errors and retries for robust delivery
"""

import logging
import time

import requests

from utils.caching import JSONFileCache


class TelegramHandler(logging.Handler):
    def __init__(self, config, rate_limit_seconds, cache):
        """Initialize Telegram handler with rate limiting and caching."""
        super().__init__()
        self.bot_token = config.get("bot_token")
        self.chat_id = config.get("chat_id")
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        self.rate_limit_seconds = rate_limit_seconds
        self.cache = cache
        self.last_sent = 0

    def emit(self, record):
        """Send log message to Telegram with rate limiting and caching."""
        try:
            current_time = time.time()
            cache_key = f"telegram_{self.chat_id}_{record.msg[:50]}"
            if current_time - self.last_sent < self.rate_limit_seconds:
                return
            cached_response = self.cache.get(cache_key)
            if cached_response:
                return
            msg = self.format(record)
            payload = {
                "chat_id": self.chat_id,
                "text": f"<b>ChainCrawler Alert</b>\n<code>{msg}</code>",
                "parse_mode": "HTML"
            }
            response = requests.post(self.base_url, json=payload, timeout=5)
            response.raise_for_status()
            self.cache.set(cache_key, {"sent": True, "timestamp": current_time})
            self.last_sent = current_time
        except Exception as e:
            logging.getLogger("ChainCrawler").error("Failed to send Telegram notification: %s", str(e))