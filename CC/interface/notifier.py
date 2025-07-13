# interface/notifier.py
"""
Multi-Channel Notification System for ChainCrawlr:
- Handles delivery of alerts to Telegram/Discord/Email
- Implements rate limiting and priority queues
- Supports attachment of blockchain explorer links
- Integrates with JSONFileCache for caching sent notifications
"""

import queue
import threading
import time

import requests

from interface.signal_payloads import AlertSeverity, RiskAlert, SystemAlert, TradeSignal
from utils.caching import JSONFileCache
from utils.helpers import ChainHelpers
from utils.logger import logger


class Notifier:
    def __init__(self, config, cache_dir=".cache"):
        """Initialize Notifier with caching for sent notifications."""
        self.config = config['notifications']
        self.queue = queue.PriorityQueue()
        self.cache = JSONFileCache(cache_dir=cache_dir, max_age=86400)  # 24-hour cache for notifications
        self.helpers = ChainHelpers()
        self.rate_limit = self.config.get('rate_limit', {'max_per_minute': 30, 'interval': 60})
        self.sent_count = 0
        self.last_reset = time.time()
        self._setup_channels()
        self._start_worker()

    def _setup_channels(self):
        """Initialize configured notification channels."""
        self.channels = {}

        if self.config.get('telegram', {}).get('enabled', False):
            try:
                from telegram import Bot
                self.channels['telegram'] = Bot(token=self.config['telegram']['bot_token'])
                logger.debug("Telegram channel initialized")
            except Exception as e:
                logger.error("Failed to initialize Telegram channel: %s", str(e))

        if self.config.get('discord', {}).get('enabled', False):
            self.channels['discord'] = {'webhook': self.config['discord']['webhook_url']}
            logger.debug("Discord channel initialized")

    def _start_worker(self):
        """Background thread for processing notifications."""
        def worker():
            while True:
                priority, payload = self.queue.get()
                try:
                    self._process_notification(payload)
                except Exception as e:
                    self._handle_notification_failure(e, payload)
                finally:
                    self.queue.task_done()

        threading.Thread(target=worker, daemon=True).start()

    def notify(self, payload, priority=1):
        """Add notification to processing queue with rate limiting."""
        cache_key = f"notification_{payload.__class__.__name__}_{payload.timestamp}_" + (
            self.helpers.shorten_address(payload.token_address) if hasattr(payload, 'token_address')
            else payload.component
        )
        
        if self.cache.get(cache_key) is not None:
            logger.debug(
                "Skipping duplicate notification: %s",
                cache_key,
                extra={
                    "alert_type": payload.__class__.__name__,
                    "token_address": self.helpers.shorten_address(payload.token_address) if hasattr(payload, 'token_address') else None,
                    "component": payload.component if hasattr(payload, 'component') else None
                }
            )
            return

        # Rate limiting
        current_time = time.time()
        if current_time - self.last_reset >= self.rate_limit['interval']:
            self.sent_count = 0
            self.last_reset = current_time

        if self.sent_count >= self.rate_limit['max_per_minute']:
            logger.warning(
                "Rate limit exceeded for notifications: %d/%d",
                self.sent_count,
                self.rate_limit['max_per_minute'],
                extra={"action": "rate_limit_exceeded"}
            )
            time.sleep(self.rate_limit['interval'] - (current_time - self.last_reset))
            self.sent_count = 0
            self.last_reset = time.time()

        self.queue.put((priority, payload))
        self.sent_count += 1
        logger.debug(
            "Queued notification: %s (priority: %d, count: %d/%d)",
            cache_key,
            priority,
            self.sent_count,
            self.rate_limit['max_per_minute'],
            extra={
                "alert_type": payload.__class__.__name__,
                "priority": priority,
                "sent_count": self.sent_count
            }
        )

    def _process_notification(self, payload):
        """Route notification to appropriate handlers."""
        cache_key = f"notification_{payload.__class__.__name__}_{payload.timestamp}_" + (
            self.helpers.shorten_address(payload.token_address) if hasattr(payload, 'token_address')
            else payload.component
        )

        try:
            if isinstance(payload, TradeSignal):
                self._send_trade_alert(payload)
            elif isinstance(payload, RiskAlert):
                self._send_risk_alert(payload)
            elif isinstance(payload, SystemAlert):
                self._send_system_alert(payload)
            self.cache.set(cache_key, {"status": "sent", "timestamp": time.time()})
        except Exception as e:
            logger.error(
                "Failed to process notification %s: %s",
                cache_key,
                str(e),
                extra={
                    "alert_type": payload.__class__.__name__,
                    "token_address": self.helpers.shorten_address(payload.token_address) if hasattr(payload, 'token_address') else None,
                    "component": payload.component if hasattr(payload, 'component') else None
                }
            )
            self.cache.set(cache_key, {"status": "failed", "error": str(e)})
            raise

    def _send_trade_alert(self, signal):
        """Format and send trade execution alerts."""
        message = (
            f"🚀 {signal.direction.upper()} Executed\n"
            f"• Token: {self.helpers.shorten_address(signal.token_address)}\n"
            f"• Chain: {signal.chain.upper()}\n"
            f"• Amount: {signal.amount:.4f}\n"
            f"• Price: {signal.price:.8f}\n"
            f"• TX: {signal.tx_hash or 'Pending'}\n"
            f"• Notes: {signal.notes or 'None'}"
        )

        attachments = []
        if signal.tx_hash:
            attachments.append({'type': 'tx_link', 'data': signal.tx_hash, 'chain': signal.chain})

        self._dispatch(message, signal.chain, attachments=attachments)

    def _send_risk_alert(self, alert):
        """Send risk management alerts."""
        emoji = "⚠️" if alert.severity == AlertSeverity.WARNING else "🚨"
        indicators = "\n".join([f"  • {k}: {v}" for k, v in alert.indicators.items()])

        message = (
            f"{emoji} Risk Alert: {alert.alert_type.replace('_', ' ').title()}\n"
            f"• Token: {self.helpers.shorten_address(alert.token_address)}\n"
            f"• Chain: {alert.chain.upper()}\n"
            f"• Severity: {alert.severity.name}\n"
            f"• Indicators:\n{indicators}"
        )

        self._dispatch(message, alert.chain, priority_override=(alert.severity in [AlertSeverity.CRITICAL, AlertSeverity.WARNING]))

    def _send_system_alert(self, alert):
        """Send system health alerts."""
        emoji = {
            AlertSeverity.INFO: "ℹ️",
            AlertSeverity.WARNING: "⚠️",
            AlertSeverity.CRITICAL: "🚨",
            AlertSeverity.SUCCESS: "✅"
        }.get(alert.severity, "⚙️")

        message = (
            f"{emoji} System Alert: {alert.component.upper()}\n"
            f"• Type: {alert.alert_type.replace('_', ' ').title()}\n"
            f"• Message: {alert.message}"
        )

        self._dispatch(message, chain=None, priority_override=(alert.severity in [AlertSeverity.CRITICAL, AlertSeverity.WARNING]))

    def _dispatch(self, message, chain=None, attachments=None, priority_override=False):
        """Send message through all enabled channels with rate limiting."""
        explorer_base = {
            'ethereum': 'https://etherscan.io',
            'solana': 'https://solscan.io'
        }.get(chain.lower() if chain else '', 'https://explorer.unknown.com')

        try:
            if 'telegram' in self.channels and (priority_override or self.config['telegram']['enabled']):
                try:
                    self.channels['telegram'].send_message(
                        chat_id=self.config['telegram']['chat_id'],
                        text=message,
                        disable_notification=not priority_override
                    )
                    logger.debug(
                        "Sent Telegram notification: %s",
                        message[:50] + "..." if len(message) > 50 else message,
                        extra={"channel": "telegram", "priority_override": priority_override}
                    )
                except Exception as e:
                    logger.error(
                        "Failed to send Telegram notification: %s",
                        str(e),
                        extra={"channel": "telegram"}
                    )

            if 'discord' in self.channels and (priority_override or self.config['discord']['enabled']):
                discord_msg = {
                    "content": message,
                    "embeds": []
                }

                if attachments:
                    for attach in attachments:
                        if attach['type'] == 'tx_link':
                            discord_msg['embeds'].append({
                                "title": "View Transaction",
                                "url": f"{explorer_base}/tx/{attach['data']}",
                                "color": 0x3498db
                            })

                response = requests.post(
                    self.channels['discord']['webhook'],
                    json=discord_msg,
                    timeout=5
                )
                response.raise_for_status()
                logger.debug(
                    "Sent Discord notification: %s",
                    message[:50] + "..." if len(message) > 50 else message,
                    extra={"channel": "discord", "priority_override": priority_override}
                )

        except Exception as e:
            logger.error(
                "Dispatch failed for message: %s",
                str(e),
                extra={"message_snippet": message[:50] + "..." if len(message) > 50 else message}
            )

    def _handle_notification_failure(self, error, payload):
        """Fallback handling when notifications fail."""
        cache_key = f"notification_{payload.__class__.__name__}_{payload.timestamp}_" + (
            self.helpers.shorten_address(payload.token_address) if hasattr(payload, 'token_address')
            else payload.component
        )
        
        logger.warning(
            "Retrying notification %s after failure: %s",
            cache_key,
            str(error),
            extra={
                "alert_type": payload.__class__.__name__,
                "token_address": self.helpers.shorten_address(payload.token_address) if hasattr(payload, 'token_address') else None,
                "component": payload.component if hasattr(payload, 'component') else None
            }
        )

        # Retry logic: re-queue with higher priority (lower number) up to 3 times
        retry_count = self.cache.get(f"retry_{cache_key}", 0)
        if retry_count < 3:
            time.sleep(2 ** retry_count)  # Exponential backoff: 2s, 4s, 8s
            self.queue.put((max(0, retry_count - 1), payload))
            self.cache.set(f"retry_{cache_key}", retry_count + 1)
            logger.debug(
                "Re-queued notification %s (retry %d/3)",
                cache_key,
                retry_count + 1,
                extra={"retry_count": retry_count + 1}
            )
        else:
            logger.error(
                "Max retries reached for notification %s",
                cache_key,
                extra={
                    "alert_type": payload.__class__.__name__,
                    "token_address": self.helpers.shorten_address(payload.token_address) if hasattr(payload, 'token_address') else None,
                    "component": payload.component if hasattr(payload, 'component') else None
                }
            )
            # Could implement dead letter queue here if needed