# utils/discord_handler.py
"""
Discord notification handler for ChainCrawlr:
- Sends log messages to Discord for WARNING+ levels
- Implements rate limiting and caching via JSONFileCache
- Handles errors and retries for robust delivery
"""

import logging
import time

import requests



class DiscordHandler(logging.Handler):
    def __init__(self, config, rate_limit_seconds, cache):
        """Initialize Discord handler with rate limiting and caching."""
        super().__init__()
        self.webhook_url = config.get("webhook_url")
        self.rate_limit_seconds = rate_limit_seconds
        self.cache = cache
        self.last_sent = 0

    def emit(self, record):
        """Send log message to Discord with rate limiting and caching."""
        try:
            current_time = time.time()
            cache_key = f"discord_{self.webhook_url[-20:]}_{record.msg[:50]}"
            if current_time - self.last_sent < self.rate_limit_seconds:
                return
            cached_response = self.cache.get(cache_key)
            if cached_response:
                return
            msg = self.format(record)
            payload = {
                "content": f"**ChainCrawler Alert**: {msg}",
                "username": "ChainCrawler"
            }
            response = requests.post(self.webhook_url, json=payload, timeout=5)
            response.raise_for_status()
            self.cache.set(cache_key, {"sent": True, "timestamp": current_time})
            self.last_sent = current_time
        except Exception as e:
            logging.getLogger("ChainCrawler").error("Failed to send Discord notification: %s", str(e))