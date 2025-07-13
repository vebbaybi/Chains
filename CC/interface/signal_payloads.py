# interface/signal_payloads.py
"""
Standardized Alert Payloads for ChainCrawlr:
- Defines consistent message structures for all platform alerts
- Enables cross-channel notification compatibility
- Supports rich formatting for different interfaces
- Integrates with JSONFileCache for caching alert payloads
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from utils.caching import JSONFileCache
from utils.helpers import ChainHelpers
from utils.logger import logger


class AlertSeverity(Enum):
    INFO = 1
    WARNING = 2
    CRITICAL = 3
    SUCCESS = 4


@dataclass
class TradeSignal:
    token_address: str
    chain: str
    direction: str  # BUY or SELL
    amount: float
    price: float
    tx_hash: str = ""
    timestamp: float = 0.0
    notes: str = ""

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = datetime.now().timestamp()


@dataclass
class RiskAlert:
    token_address: str
    chain: str
    alert_type: str  # RUG_PULL, LIQUIDITY_DRAIN, etc.
    severity: AlertSeverity
    indicators: dict  # {indicator_name: value}
    timestamp: float = 0.0

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = datetime.now().timestamp()


@dataclass
class SystemAlert:
    component: str
    alert_type: str  # CONNECTION_ISSUE, BALANCE_LOW, etc.
    severity: AlertSeverity
    message: str
    timestamp: float = 0.0

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = datetime.now().timestamp()


def format_for_webhook(payload, cache_dir=".cache"):
    """
    Converts any alert dataclass into a standardized webhook dictionary.
    Adds optional quick links if applicable.
    Caches formatted payloads to avoid redundant formatting.
    """
    helpers = ChainHelpers()
    cache = JSONFileCache(cache_dir=cache_dir, max_age=300)  # 5-minute cache
    cache_key = f"webhook_{payload.__class__.__name__}_{payload.timestamp}_{payload.token_address if hasattr(payload, 'token_address') else payload.component}"

    cached_result = cache.get(cache_key)
    if cached_result is not None:
        logger.debug(
            "Retrieved webhook format from cache: %s",
            cache_key,
            extra={
                "alert_type": payload.__class__.__name__,
                "token_address": helpers.shorten_address(payload.token_address) if hasattr(payload, 'token_address') else None,
                "component": payload.component if hasattr(payload, 'component') else None
            }
        )
        return cached_result

    try:
        base = {
            'type': payload.__class__.__name__,
            'data': {
                k: v.value if isinstance(v, AlertSeverity) else v
                for k, v in payload.__dict__.items()
            },
            'timestamp': payload.timestamp
        }

        if isinstance(payload, TradeSignal) and payload.tx_hash:
            chain = payload.chain.lower()
            explorer_base = {
                'ethereum': 'https://etherscan.io',
                'solana': 'https://solscan.io'
            }.get(chain, 'https://explorer.unknown.com')
            base['quick_actions'] = [
                {'label': 'View TX', 'url': f"{explorer_base}/tx/{payload.tx_hash}"},
                {'label': 'Token', 'url': f"{explorer_base}/token/{payload.token_address}"}
            ]

        cache.set(cache_key, base)
        logger.debug(
            "Formatted and cached webhook for %s: %s",
            payload.__class__.__name__,
            cache_key,
            extra={
                "alert_type": payload.__class__.__name__,
                "token_address": helpers.shorten_address(payload.token_address) if hasattr(payload, 'token_address') else None,
                "component": payload.component if hasattr(payload, 'component') else None
            }
        )
        return base

    except Exception as e:
        logger.error(
            "Failed to format webhook for %s: %s",
            payload.__class__.__name__,
            str(e),
            extra={
                "alert_type": payload.__class__.__name__,
                "token_address": helpers.shorten_address(payload.token_address) if hasattr(payload, 'token_address') else None,
                "component": payload.component if hasattr(payload, 'component') else None
            }
        )
        cache.set(cache_key, {"type": payload.__class__.__name__, "error": str(e)})
        return {"type": payload.__class__.__name__, "error": str(e)}