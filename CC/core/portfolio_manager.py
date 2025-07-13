 # core/portfolio_manager.py
"""
Portfolio Manager for ChainCrawlr:
- Real-time position tracking across EVM and Solana chains
- Profit/loss calculation with entry/exit price tracking
- Risk-adjusted position sizing
- Performance analytics and reporting
- Integration with Uniswap, Raydium, and Jupiter price feeds
- Integrates with JSONFileCache for caching prices and portfolio data
"""

import json
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from threading import Lock

from solana.rpc.api import Client as SolanaClient
from web3 import Web3

from dex_clients.jupiter import JupiterClient
from dex_clients.raydium import RaydiumClient
from dex_clients.uniswap import UniswapV3Client
from utils.caching import JSONFileCache
from utils.helpers import ChainHelpers
from utils.logger import logger


@dataclass
class Position:
    token_address: str
    chain: str
    dex: str
    entry_time: float
    entry_price: Decimal
    amount: Decimal
    high_price: Decimal = Decimal('0')
    trailing_stop: Decimal = Decimal('0')
    exit_time: float = 0
    exit_price: Decimal = Decimal('0')
    tx_hash: str = ""


class PortfolioManager:
    def __init__(self, settings, wallets, chains, cache_dir=".cache"):
        """Initialize PortfolioManager with caching for prices and portfolio data."""
        self.settings = settings['trading'].get('portfolio', {})
        self.wallets = wallets
        self.chains = chains
        self.lock = Lock()
        self.positions = {}
        self.history = []
        self.helpers = ChainHelpers()
        self.cache = JSONFileCache(cache_dir=cache_dir, max_age=300)  # 5-minute cache
        self.uniswap = UniswapV3Client(cache_dir=cache_dir)
        self.raydium = RaydiumClient(cache_dir=cache_dir)
        self.jupiter = JupiterClient(cache_dir=cache_dir)
        self.file_path = 'portfolio_data.json'
        self._load_portfolio()

    def _load_portfolio(self):
        """Load portfolio from JSON file with caching."""
        if not os.path.exists(self.file_path):
            logger.debug("Portfolio file %s does not exist", self.file_path)
            return
        
        cache_key = f"portfolio_data_{os.path.getmtime(self.file_path)}"
        cached_data = self.cache.get(cache_key)
        if cached_data is not None:
            logger.debug("Loaded portfolio from cache: %s", cache_key)
            self.positions = {
                pos['token_address']: Position(
                    **{k: Decimal(str(v)) if k in ['entry_price', 'amount', 'high_price', 'trailing_stop', 'exit_price'] else v
                       for k, v in pos.items()}
                ) for pos in cached_data['positions']
            }
            self.history = cached_data['history']
            return

        try:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.positions = {
                    pos['token_address']: Position(
                        **{k: Decimal(str(v)) if k in ['entry_price', 'amount', 'high_price', 'trailing_stop', 'exit_price'] else v
                           for k, v in pos.items()}
                    ) for pos in data.get('positions', [])
                }
                self.history = data.get('history', [])
                self.cache.set(cache_key, {'positions': data.get('positions', []), 'history': self.history})
                logger.debug("Loaded and cached portfolio: %s", self.file_path)
        except Exception as e:
            logger.error("Failed to load portfolio %s: %s", self.file_path, str(e))

    def _save_portfolio(self):
        """Save portfolio to JSON file and update cache."""
        cache_key = f"portfolio_data_{time.time()}"
        try:
            with self.lock:
                portfolio_data = {
                    'positions': [
                        {k: str(v) if isinstance(v, Decimal) else v for k, v in p.__dict__.items()}
                        for p in self.positions.values()
                    ],
                    'history': self.history
                }
                with open(self.file_path, 'w', encoding='utf-8') as f:
                    json.dump(portfolio_data, f, indent=2)
                self.cache.set(cache_key, portfolio_data)
                logger.debug("Saved and cached portfolio: %s", self.file_path)
        except Exception as e:
            logger.error("Portfolio save failed: %s", str(e))

    def open_position(self, token_info, tx_hash):
        """Open a new position with cached price and save to portfolio."""
        token_address = token_info['token_address']
        with self.lock:
            if token_address in self.positions:
                logger.warning(
                    "Position already exists for %s",
                    self.helpers.shorten_address(token_address),
                    extra={"token_address": self.helpers.shorten_address(token_address)}
                )
                return

            entry_price = self._get_current_price(token_info)
            amount = self._calculate_position_size(token_info, entry_price)

            self.positions[token_address] = Position(
                token_address=token_address,
                chain=token_info['chain'],
                dex=token_info['dex'],
                entry_time=time.time(),
                entry_price=Decimal(str(entry_price)),
                amount=Decimal(str(amount)),
                high_price=Decimal(str(entry_price)),
                tx_hash=tx_hash
            )
            self._save_portfolio()
            logger.info(
                "Opened position for %s on %s: %s tokens at %s",
                self.helpers.shorten_address(token_address),
                token_info['chain'],
                amount,
                entry_price,
                extra={
                    "token_address": self.helpers.shorten_address(token_address),
                    "chain": token_info['chain'],
                    "amount": float(amount),
                    "entry_price": float(entry_price)
                }
            )

    def _get_current_price(self, token_info):
        """Fetch current token price from DEX client with caching."""
        token_address = token_info['token_address']
        chain = token_info['chain']
        dex = token_info['dex']
        cache_key = f"price_{chain}_{token_address}"

        cached_price = self.cache.get(cache_key)
        if cached_price is not None:
            logger.debug(
                "Retrieved price from cache for %s on %s: %s",
                self.helpers.shorten_address(token_address),
                chain,
                cached_price,
                extra={"token_address": self.helpers.shorten_address(token_address)}
            )
            return cached_price

        try:
            if chain == 'ethereum':
                price = self.uniswap.get_quote(
                    token_in=self.wallets['primary']['address'],
                    token_out=token_address,
                    amount_in=1,
                    fee_tier=self.settings.get('fee_tier', 3000)
                )
            elif dex == 'jupiter':
                price = self.jupiter.get_quote(
                    token_in=self.wallets['primary']['address'],
                    token_out=token_address,
                    amount_in=1
                )
            else:
                price = self.raydium.get_quote(
                    token_in=self.wallets['primary']['address'],
                    token_out=token_address,
                    amount_in=1
                )
            self.cache.set(cache_key, price)
            logger.debug(
                "Fetched price for %s on %s: %s",
                self.helpers.shorten_address(token_address),
                chain,
                price,
                extra={"token_address": self.helpers.shorten_address(token_address)}
            )
            return price
        except Exception as e:
            logger.error(
                "Failed to fetch price for %s on %s: %s",
                self.helpers.shorten_address(token_address),
                chain,
                str(e),
                extra={"token_address": self.helpers.shorten_address(token_address)}
            )
            raise

    def _calculate_position_size(self, token_info, entry_price):
        """Calculate position size based on risk parameters."""
        try:
            risk_percentage = Decimal(str(self.settings.get('max_risk_per_trade', 1.0)))
            portfolio_value = self.get_portfolio_value()
            risk_amount = portfolio_value * (risk_percentage / Decimal('100'))

            if 'volatility' in token_info:
                volatility_adjustment = min(Decimal('1.5'), Decimal('0.5') + Decimal(str(token_info['volatility'])))
                risk_amount /= volatility_adjustment

            position_size = risk_amount / Decimal(str(entry_price))
            min_size = Decimal(str(self.settings.get('min_position_size', 100)))
            max_size = Decimal(str(self.settings.get('max_position_size', 10000)))
            size = max(min_size, min(position_size, max_size))
            
            logger.debug(
                "Calculated position size for %s: %s (risk: %s%%, portfolio: %s)",
                self.helpers.shorten_address(token_info['token_address']),
                size,
                risk_percentage,
                portfolio_value,
                extra={
                    "token_address": self.helpers.shorten_address(token_info['token_address']),
                    "position_size": float(size)
                }
            )
            return size
        except Exception as e:
            logger.error(
                "Failed to calculate position size for %s: %s",
                self.helpers.shorten_address(token_info['token_address']),
                str(e),
                extra={"token_address": self.helpers.shorten_address(token_info['token_address'])}
            )
            return Decimal(str(self.settings.get('min_position_size', 100)))

    def close_position(self, token_address, exit_price=None, tx_hash=None):
        """Close a position and update history with caching."""
        with self.lock:
            if token_address not in self.positions:
                logger.warning(
                    "No position found for %s",
                    self.helpers.shorten_address(token_address),
                    extra={"token_address": self.helpers.shorten_address(token_address)}
                )
                return False

            pos = self.positions[token_address]
            cache_key = f"close_position_{pos.chain}_{token_address}"

            cached_result = self.cache.get(cache_key)
            if cached_result is not None:
                logger.debug(
                    "Retrieved close position from cache for %s: %s",
                    self.helpers.shorten_address(token_address),
                    cached_result,
                    extra={"token_address": self.helpers.shorten_address(token_address)}
                )
                if cached_result.get("status") == "success":
                    self.history.append(cached_result["history_entry"])
                    del self.positions[token_address]
                    self._save_portfolio()
                    return True
                return False

            try:
                if not exit_price:
                    exit_price = self._get_current_price({
                        'token_address': token_address,
                        'chain': pos.chain,
                        'dex': pos.dex
                    })

                pnl = (Decimal(str(exit_price)) / pos.entry_price - Decimal('1')) * Decimal('100')
                duration = time.time() - pos.entry_time

                history_entry = {
                    'token_address': token_address,
                    'chain': pos.chain,
                    'entry_price': float(pos.entry_price),
                    'exit_price': float(exit_price),
                    'amount': float(pos.amount),
                    'pnl': float(pnl),
                    'duration': duration,
                    'entry_time': pos.entry_time,
                    'exit_time': time.time(),
                    'tx_hash': tx_hash or pos.tx_hash
                }

                self.cache.set(cache_key, {"status": "success", "history_entry": history_entry})
                self.history.append(history_entry)
                del self.positions[token_address]
                self._save_portfolio()
                
                logger.info(
                    "Closed position for %s on %s: PNL %.2f%%",
                    self.helpers.shorten_address(token_address),
                    pos.chain,
                    pnl,
                    extra={
                        "token_address": self.helpers.shorten_address(token_address),
                        "pnl": float(pnl),
                        "exit_price": float(exit_price)
                    }
                )
                return True

            except Exception as e:
                logger.error(
                    "Failed to close position for %s: %s",
                    self.helpers.shorten_address(token_address),
                    str(e),
                    extra={"token_address": self.helpers.shorten_address(token_address)}
                )
                self.cache.set(cache_key, {"status": "failed", "error": str(e)})
                return False

    def update_positions(self):
        """Update position high prices and trailing stops."""
        with self.lock:
            for token_address, pos in list(self.positions.items()):
                try:
                    current_price = Decimal(str(self._get_current_price({
                        'token_address': token_address,
                        'chain': pos.chain,
                        'dex': pos.dex
                    })))

                    if current_price > pos.high_price:
                        pos.high_price = current_price
                        if self.settings.get('trailing_stop_enabled', False):
                            trail_pct = Decimal(str(self.settings.get('trailing_stop_percent', 10)))
                            pos.trailing_stop = current_price * (Decimal('1') - trail_pct / Decimal('100'))
                            logger.debug(
                                "Updated trailing stop for %s: %s",
                                self.helpers.shorten_address(token_address),
                                pos.trailing_stop,
                                extra={
                                    "token_address": self.helpers.shorten_address(token_address),
                                    "trailing_stop": float(pos.trailing_stop)
                                }
                            )

                    self._save_portfolio()

                except Exception as e:
                    logger.error(
                        "Update failed for %s: %s",
                        self.helpers.shorten_address(token_address),
                        str(e),
                        extra={"token_address": self.helpers.shorten_address(token_address)}
                    )

    def get_open_positions(self):
        """Return current open positions with real-time data."""
        results = []
        with self.lock:
            for p in self.positions.values():
                try:
                    current_price = self._get_current_price({
                        'token_address': p.token_address,
                        'chain': p.chain,
                        'dex': p.dex
                    })
                    results.append({
                        'token_address': p.token_address,
                        'chain': p.chain,
                        'dex': p.dex,
                        'entry_price': float(p.entry_price),
                        'current_price': current_price,
                        'amount': float(p.amount),
                        'pnl': (current_price / float(p.entry_price) - 1) * 100,
                        'high_price': float(p.high_price),
                        'trailing_stop': float(p.trailing_stop),
                        'entry_time': p.entry_time,
                        'duration': time.time() - p.entry_time,
                        'tx_hash': p.tx_hash
                    })
                except Exception as e:
                    logger.error(
                        "Failed to process position %s: %s",
                        self.helpers.shorten_address(p.token_address),
                        str(e),
                        extra={"token_address": self.helpers.shorten_address(p.token_address)}
                    )
        return results

    def get_portfolio_value(self):
        """Calculate total portfolio value including base currency and positions."""
        cache_key = f"portfolio_value_{int(time.time() // 300)}"  # Cache per 5-minute window
        cached_value = self.cache.get(cache_key)
        if cached_value is not None:
            logger.debug("Retrieved portfolio value from cache: %s", cached_value)
            return Decimal(str(cached_value))

        try:
            base = self.settings.get('base_currency', 'ETH')
            if base == 'ETH':
                w3 = Web3(Web3.HTTPProvider(self.chains['ethereum']['rpc']))
                bal = w3.eth.get_balance(self.wallets['primary']['address']) / 1e18
            else:
                client = SolanaClient(self.chains['solana']['rpc'])
                bal = client.get_balance(self.wallets['primary']['address'])['result']['value'] / 1e9

            total = Decimal(str(bal))
            for pos in self.get_open_positions():
                total += Decimal(str(pos['current_price'])) * Decimal(str(pos['amount']))

            self.cache.set(cache_key, float(total))
            logger.debug(
                "Calculated portfolio value: %s",
                total,
                extra={"portfolio_value": float(total)}
            )
            return total

        except Exception as e:
            logger.error("Failed to calculate portfolio value: %s", str(e))
            return Decimal('0')

    def get_performance_metrics(self):
        """Calculate portfolio performance metrics."""
        cache_key = f"performance_metrics_{len(self.history)}_{int(time.time() // 300)}"
        cached_metrics = self.cache.get(cache_key)
        if cached_metrics is not None:
            logger.debug("Retrieved performance metrics from cache: %s", cache_key)
            return cached_metrics

        metrics = {
            'total_value': float(self.get_portfolio_value()),
            'total_pnl': 0,
            'win_rate': 0,
            'avg_win': 0,
            'avg_loss': 0,
            'best_trade': {},
            'worst_trade': {},
            'recent_trades': []
        }
        if not self.history:
            self.cache.set(cache_key, metrics)
            return metrics

        try:
            wins = [t for t in self.history if t['pnl'] > 0]
            losses = [t for t in self.history if t['pnl'] <= 0]
            metrics['total_pnl'] = sum(t['pnl'] for t in self.history)
            metrics['win_rate'] = len(wins) / len(self.history) * 100 if self.history else 0
            metrics['avg_win'] = sum(t['pnl'] for t in wins) / len(wins) if wins else 0
            metrics['avg_loss'] = sum(t['pnl'] for t in losses) / len(losses) if losses else 0
            metrics['best_trade'] = max(self.history, key=lambda x: x['pnl']) if self.history else {}
            metrics['worst_trade'] = min(self.history, key=lambda x: x['pnl']) if self.history else {}
            metrics['recent_trades'] = self.history[-5:][::-1]
            self.cache.set(cache_key, metrics)
            
            logger.debug(
                "Calculated performance metrics: win_rate=%.2f%%, total_pnl=%.2f",
                metrics['win_rate'],
                metrics['total_pnl'],
                extra={"win_rate": metrics['win_rate'], "total_pnl": metrics['total_pnl']}
            )
            return metrics

        except Exception as e:
            logger.error("Failed to calculate performance metrics: %s", str(e))
            return metrics

    def liquidate_all(self):
        """Liquidate all open positions."""
        with self.lock:
            for token in list(self.positions.keys()):
                self.close_position(token)
            logger.warning("All positions liquidated.", extra={"action": "liquidate_all"})