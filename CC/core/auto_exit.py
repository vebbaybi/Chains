# core/auto_exit.py
"""
Auto Exit Module for ChainCrawlr:
- Implements multi-strategy profit-taking (time-based, percentage-based, trailing)
- Dynamic stop-loss system with rug pull detection
- Gas-aware transaction batching for efficient exits
- Integrated with portfolio manager for position tracking
- Supports both EVM and Solana chains
- Integrates with JSONFileCache for caching price and exit results
"""

import math
import time

from solana.rpc.api import Client as SolanaClient
from web3 import Web3

from core.anti_rug import AntiRugChecker
from dex_clients.jupiter import JupiterClient
from dex_clients.raydium import RaydiumClient
from dex_clients.uniswap import UniswapV3Client
from utils.caching import JSONFileCache
from utils.helpers import ChainHelpers
from utils.logger import logger


class AutoExit:
    def __init__(self, settings, wallets, chains, portfolio_manager, cache_dir=".cache"):
        """Initialize AutoExit with caching for price and exit results."""
        # Configuration Validation
        assert settings['trading']['auto_exit']['enabled'], "AutoExit disabled in config"
        assert float(settings['trading']['auto_exit']['max_slippage']) < 0.3, "Dangerous exit slippage (>30%)"

        # Set internal state from configuration
        self.settings = settings
        self.wallets = wallets
        self.chains = chains
        self.portfolio = portfolio_manager
        self.helpers = ChainHelpers()
        self.cache = JSONFileCache(cache_dir=cache_dir, max_age=300)  # 5-minute cache

        # Initialize DEX Clients
        self.uniswap = UniswapV3Client(cache_dir=cache_dir)
        self.raydium = RaydiumClient(cache_dir=cache_dir)
        self.jupiter = JupiterClient(cache_dir=cache_dir)

        # Strategy & Risk Parameters
        self.strategies = settings['trading']['auto_exit'].get('strategies', [])
        self.global_stop_loss = float(settings['trading']['auto_exit'].get('global_stop_loss', 0.1))
        self.rug_pull_threshold = float(settings['trading']['anti_rug'].get('rug_pull_threshold', 0.5))

        # Runtime State Tracking
        self.active_positions = {}
        self.exit_history = []

    def monitor_positions(self):
        """Main loop for position monitoring and exit execution."""
        while True:
            try:
                positions = self.portfolio.get_open_positions()
                for position in positions:
                    self._evaluate_position(position)
                time.sleep(self.settings['trading']['auto_exit'].get('monitor_interval', 60))
            except Exception as e:
                logger.error("Monitoring error: %s", str(e), exc_info=True)
                time.sleep(30)

    def _evaluate_position(self, position):
        """Determines if a position should trigger an exit."""
        token_address = position['token_address']
        chain = position['chain']

        if token_address in self.active_positions:
            logger.debug(
                "Skipping active position %s on %s",
                self.helpers.shorten_address(token_address),
                chain,
                extra={"token_address": self.helpers.shorten_address(token_address)}
            )
            return

        self.active_positions[token_address] = True

        try:
            current_price = self._get_current_price(position)
            profit_pct = (current_price / position['entry_price'] - 1) * 100

            # Check for rug pull emergency
            if self._detect_rug_pull(position, current_price):
                logger.critical(
                    "Rug pull detected for %s on %s! Emergency exiting...",
                    self.helpers.shorten_address(token_address),
                    chain,
                    extra={"token_address": self.helpers.shorten_address(token_address)}
                )
                self._execute_exit(position, is_emergency=True)
                return

            # Global Stop Loss
            if profit_pct <= -abs(self.global_stop_loss):
                logger.warning(
                    "Stop loss triggered for %s on %s (%.2f%%)",
                    self.helpers.shorten_address(token_address),
                    chain,
                    profit_pct,
                    extra={
                        "token_address": self.helpers.shorten_address(token_address),
                        "profit_pct": profit_pct
                    }
                )
                self._execute_exit(position)
                return

            # Evaluate exit strategies
            for strategy in self.strategies:
                if self._check_strategy(strategy, position, current_price, profit_pct):
                    logger.info(
                        "%s exit triggered for %s on %s (Strategy: %s)",
                        strategy['type'],
                        self.helpers.shorten_address(token_address),
                        chain,
                        strategy.get('name', 'unnamed'),
                        extra={
                            "token_address": self.helpers.shorten_address(token_address),
                            "strategy_type": strategy['type'],
                            "strategy_name": strategy.get('name', 'unnamed')
                        }
                    )
                    self._execute_exit(position)
                    return

        except Exception as e:
            logger.error(
                "Evaluation failed for %s on %s: %s",
                self.helpers.shorten_address(token_address),
                chain,
                str(e),
                extra={"token_address": self.helpers.shorten_address(token_address)}
            )
        finally:
            del self.active_positions[token_address]

    def _get_current_price(self, position):
        """Fetches current token price from the appropriate DEX client with caching."""
        token_address = position['token_address']
        chain = position['chain']
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
                    fee_tier=self.settings['trading']['sniping'].get('fee_tier', 3000)
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

    def _detect_rug_pull(self, position, current_price):
        """Checks for rug pull using price drop and contract check."""
        if position['chain'] == 'ethereum' and self.settings['trading']['anti_rug']['enabled']:
            w3 = Web3(Web3.HTTPProvider(self.chains['ethereum']['rpc']))
            checker = AntiRugChecker(w3, position['token_address'], 'ethereum', self.settings, cache_dir=self.cache.cache_dir)

            price_drop = (position['high_price'] / current_price - 1) * 100
            if price_drop >= self.rug_pull_threshold:
                logger.warning(
                    "Significant price drop detected for %s: %.2f%%",
                    self.helpers.shorten_address(position['token_address']),
                    price_drop,
                    extra={
                        "token_address": self.helpers.shorten_address(position['token_address']),
                        "price_drop": price_drop
                    }
                )
                return True

            return not checker.run_all_checks()  # Assuming run_all_checks for quick checks
        return False

    def _check_strategy(self, strategy, position, current_price, profit_pct):
        """Evaluates exit criteria based on strategy type."""
        try:
            if strategy['type'] == 'percentage':
                return profit_pct >= float(strategy.get('target', 0))

            elif strategy['type'] == 'time':
                hold_time = time.time() - position['entry_time']
                return hold_time >= strategy.get('duration', 3600) and profit_pct > 0

            elif strategy['type'] == 'trailing':
                trail_amount = float(strategy.get('trail_percent', 10))
                current_trail = current_price * (1 - trail_amount / 100)
                return current_trail > position.get('trailing_stop', current_price)

            return False
        except Exception as e:
            logger.error(
                "Strategy evaluation failed for %s (%s): %s",
                self.helpers.shorten_address(position['token_address']),
                strategy.get('name', 'unnamed'),
                str(e),
                extra={
                    "token_address": self.helpers.shorten_address(position['token_address']),
                    "strategy_name": strategy.get('name', 'unnamed')
                }
            )
            return False

    def _execute_exit(self, position, is_emergency=False):
        """Executes the actual exit transaction with caching."""
        chain = position['chain']
        token_address = position['token_address']
        amount = position['amount']
        cache_key = f"exit_{chain}_{token_address}_{amount}"

        cached_result = self.cache.get(cache_key)
        if cached_result is not None:
            logger.debug(
                "Retrieved exit result from cache for %s on %s: %s",
                self.helpers.shorten_address(token_address),
                chain,
                cached_result,
                extra={"token_address": self.helpers.shorten_address(token_address)}
            )
            if cached_result.get("status") == "success":
                self.portfolio.close_position(token_address)
                self.exit_history.append(cached_result["exit_info"])
                return
            logger.warning(
                "Cached exit failed for %s on %s: %s",
                self.helpers.shorten_address(token_address),
                chain,
                cached_result.get("error", "Unknown error"),
                extra={"token_address": self.helpers.shorten_address(token_address)}
            )
            return

        try:
            logger.info(
                "Initiating exit for %s on %s",
                self.helpers.shorten_address(token_address),
                chain,
                extra={"token_address": self.helpers.shorten_address(token_address)}
            )

            if chain == 'ethereum':
                result = self._exit_ethereum(position, is_emergency)
            else:
                result = self._exit_solana(position, is_emergency)

            if result:
                exit_info = {
                    'token': token_address,
                    'chain': chain,
                    'exit_time': time.time(),
                    'profit': (result['exit_price'] / position['entry_price'] - 1) * 100,
                    'tx_hash': result['tx_hash']
                }
                self.cache.set(cache_key, {"status": "success", "exit_info": exit_info})
                self.portfolio.close_position(token_address)
                self.exit_history.append(exit_info)
                logger.info(
                    "Exit successful for %s on %s: %s",
                    self.helpers.shorten_address(token_address),
                    chain,
                    exit_info,
                    extra={
                        "token_address": self.helpers.shorten_address(token_address),
                        "tx_hash": result['tx_hash'],
                        "profit_pct": exit_info['profit']
                    }
                )

        except Exception as e:
            logger.error(
                "Exit failed for %s on %s: %s",
                self.helpers.shorten_address(token_address),
                chain,
                str(e),
                extra={"token_address": self.helpers.shorten_address(token_address)}
            )
            self.cache.set(cache_key, {"status": "failed", "error": str(e)})
            if is_emergency:
                self._execute_fallback_exit(position)

    def _exit_ethereum(self, position, is_emergency):
        """Handles token selling on Ethereum-based DEX (Uniswap V3)."""
        w3 = Web3(Web3.HTTPProvider(self.chains['ethereum']['rpc']))

        if is_emergency:
            gas_params = {
                'maxFeePerGas': w3.to_wei(200, 'gwei'),
                'maxPriorityFeePerGas': w3.to_wei(5, 'gwei'),
                'type': 2
            }
            slippage = self.settings['trading']['auto_exit'].get('emergency_slippage', 0.2)
        else:
            try:
                current_block = w3.eth.get_block('pending')
                base_fee = current_block['baseFeePerGas']
                priority_fee = self.helpers.calculate_priority_fee(w3)
                gas_params = {
                    'maxFeePerGas': int(base_fee * 1.3 + priority_fee),
                    'maxPriorityFeePerGas': priority_fee,
                    'type': 2
                }
            except Exception as e:
                logger.error(
                    "Failed to calculate gas params for %s: %s",
                    self.helpers.shorten_address(position['token_address']),
                    str(e),
                    extra={"token_address": self.helpers.shorten_address(position['token_address'])}
                )
                raise
            slippage = self.settings['trading']['auto_exit'].get('max_slippage', 0.05)

        result = self.uniswap.execute_swap(
            token_in=position['token_address'],
            token_out=self.wallets['primary']['address'],
            amount_in=position['amount'],
            min_amount_out=0,  # Calculated with slippage
            fee_tier=self.settings['trading']['sniping'].get('fee_tier', 3000)
        )

        if result and result.get('status') == 'confirmed':
            exit_price = self.uniswap.get_quote(
                token_in=self.wallets['primary']['address'],
                token_out=position['token_address'],
                amount_in=1,
                fee_tier=self.settings['trading']['sniping'].get('fee_tier', 3000)
            )
            return {'tx_hash': result['tx_hash'], 'exit_price': exit_price}
        raise ValueError("Uniswap swap failed")

    def _exit_solana(self, position, is_emergency):
        """Handles token selling on Solana using Raydium or Jupiter."""
        client = SolanaClient(self.chains['solana']['rpc'])
        slippage = self.settings['trading']['auto_exit'].get('emergency_slippage', 0.2) if is_emergency else self.settings['trading']['auto_exit'].get('max_slippage', 0.05)

        try:
            if is_emergency or position['dex'] == 'jupiter':
                result = self.jupiter.execute_swap(
                    token_in=position['token_address'],
                    token_out=self.wallets['primary']['address'],
                    amount_in=position['amount'],
                    min_amount_out=0,
                    max_retries=3
                )
            else:
                try:
                    result = self.raydium.execute_swap(
                        token_in=position['token_address'],
                        token_out=self.wallets['primary']['address'],
                        amount_in=position['amount'],
                        min_amount_out=0,
                        max_retries=3
                    )
                except Exception:
                    logger.warning(
                        "Raydium exit failed for %s, falling back to Jupiter",
                        self.helpers.shorten_address(position['token_address']),
                        extra={"token_address": self.helpers.shorten_address(position['token_address'])}
                    )
                    result = self.jupiter.execute_swap(
                        token_in=position['token_address'],
                        token_out=self.wallets['primary']['address'],
                        amount_in=position['amount'],
                        min_amount_out=0,
                        max_retries=3
                    )

            if result and result.get('status') == 'confirmed':
                exit_price = self.raydium.get_quote(
                    token_in=self.wallets['primary']['address'],
                    token_out=position['token_address'],
                    amount_in=1
                )
                return {'tx_hash': result['tx_id'], 'exit_price': exit_price}
            raise ValueError("Solana swap failed")

        except Exception as e:
            logger.error(
                "Solana exit failed for %s: %s",
                self.helpers.shorten_address(position['token_address']),
                str(e),
                extra={"token_address": self.helpers.shorten_address(position['token_address'])}
            )
            raise

    def _execute_fallback_exit(self, position):
        """Performs emergency fallback exit (direct transfer)."""
        token_address = position['token_address']
        logger.critical(
            "Attempting fallback exit for %s on %s",
            self.helpers.shorten_address(token_address),
            position['chain'],
            extra={"token_address": self.helpers.shorten_address(token_address)}
        )

        cache_key = f"fallback_exit_{position['chain']}_{token_address}_{position['amount']}"
        cached_result = self.cache.get(cache_key)
        if cached_result is not None:
            logger.debug(
                "Retrieved fallback exit from cache for %s: %s",
                self.helpers.shorten_address(token_address),
                cached_result,
                extra={"token_address": self.helpers.shorten_address(token_address)}
            )
            if cached_result.get("status") == "success":
                return
            logger.warning(
                "Cached fallback exit failed for %s: %s",
                self.helpers.shorten_address(token_address),
                cached_result.get("error", "Unknown error"),
                extra={"token_address": self.helpers.shorten_address(token_address)}
            )

        try:
            if position['chain'] == 'ethereum':
                w3 = Web3(Web3.HTTPProvider(self.chains['ethereum']['rpc']))
                token_contract = w3.eth.contract(
                    address=position['token_address'],
                    abi=self.helpers.get_erc20_abi()
                )

                tx = token_contract.functions.transfer(
                    self.wallets['fallback']['address'],
                    position['amount']
                ).build_transaction({
                    'from': self.wallets['primary']['address'],
                    'gas': 100000,
                    'nonce': w3.eth.get_transaction_count(self.wallets['primary']['address']),
                    'maxFeePerGas': w3.to_wei(200, 'gwei'),
                    'maxPriorityFeePerGas': w3.to_wei(5, 'gwei'),
                    'type': 2
                })

                signed = w3.eth.account.sign_transaction(tx, self.wallets['primary']['private_key'])
                tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction).hex()
                self.cache.set(cache_key, {"status": "success", "tx_hash": tx_hash})

            else:
                client = SolanaClient(self.chains['solana']['rpc'])
                result = self.raydium.transfer_token(
                    client=client,
                    token_address=position['token_address'],
                    wallet=self.wallets['primary'],
                    recipient=self.wallets['fallback']['address'],
                    amount=position['amount']
                )
                self.cache.set(cache_key, {"status": "success", "tx_hash": result.get("tx_id")})

            logger.info(
                "Fallback exit successful for %s on %s",
                self.helpers.shorten_address(token_address),
                position['chain'],
                extra={"token_address": self.helpers.shorten_address(token_address)}
            )

        except Exception as e:
            logger.error(
                "Fallback exit failed for %s on %s: %s",
                self.helpers.shorten_address(token_address),
                position['chain'],
                str(e),
                extra={"token_address": self.helpers.shorten_address(token_address)}
            )
            self.cache.set(cache_key, {"status": "failed", "error": str(e)})

    def get_exit_history(self, limit=50):
        """Returns historical exit logs."""
        return self.exit_history[-limit:]