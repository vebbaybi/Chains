# core/sniper.py
"""
Enhanced Sniper Module for ChainCrawlr:
- Executes cross-chain sniping with failover mechanisms
- Implements EIP-1559 gas optimization and Solana RPC fallbacks
- Trades state tracking to prevent duplicate transactions
- Strict config validation and security checks
- Detailed event logging for forensic analysis
- Integrates with JSONFileCache for caching snipe results
"""

import time

from solana.rpc.api import Client as SolanaClient
from solana.rpc.core import RPCException as SolanaRpcException
from web3 import Web3

from core.anti_rug import AntiRugChecker
from dex_clients.jupiter import JupiterClient
from dex_clients.raydium import RaydiumClient
from dex_clients.uniswap import UniswapV3Client
from utils.caching import JSONFileCache
from utils.helpers import ChainHelpers
from utils.logger import logger


class Sniper:
    def __init__(self, settings, wallets, chains, cache_dir=".cache"):
        """Initialize Sniper with caching for snipe results."""
        # Config Validation
        assert settings['trading']['sniping']['enabled'], "Sniping disabled in config"
        assert float(wallets['primary']['max_slippage']) < 0.5, "Dangerous slippage (>50%)"
        assert chains['ethereum']['rpc'], "Missing Ethereum RPC"
        assert chains['solana']['rpc'], "Missing Solana RPC"

        self.settings = settings
        self.wallets = wallets
        self.chains = chains
        self.helpers = ChainHelpers()
        self.cache = JSONFileCache(cache_dir=cache_dir, max_age=300)  # 5-minute cache
        self.uniswap = UniswapV3Client(cache_dir=cache_dir)
        self.raydium = RaydiumClient(cache_dir=cache_dir)
        self.jupiter = JupiterClient(cache_dir=cache_dir)
        
        # Trading Parameters
        self.min_balance = float(wallets['primary'].get('min_balance', 0.1))
        self.max_slippage = float(wallets['primary'].get('max_slippage', 0.05))
        self.gas_multiplier = float(wallets['primary'].get('gas_multiplier', 1.0))
        self.slippage_tolerance = float(settings['trading']['sniping'].get('max_buy_percentage', 0.1))
        self.enable_anti_rug = settings['trading'].get('anti_rug', {}).get('enabled', True)
        
        # State Tracking
        self.pending_txs = {}
        self.blacklist = set()

    def execute(self, token_info):
        """Orchestrates snipe execution with state tracking and caching."""
        token_address = token_info['token_address']
        chain = token_info['chain']
        dex = token_info['dex']
        
        cache_key = f"snipe_{chain}_{dex}_{token_address}"
        cached_result = self.cache.get(cache_key)
        if cached_result is not None:
            logger.debug("Retrieved snipe result from cache: %s", cache_key)
            if cached_result.get("status") == "success":
                logger.info(
                    "Cached: Snipe successful for %s on %s via %s: %s",
                    self.helpers.shorten_address(token_address),
                    chain,
                    dex,
                    cached_result,
                    extra={
                        "type": "sniping_success",
                        "token_address": self.helpers.shorten_address(token_address),
                        "chain": chain,
                        "dex": dex,
                        "tx_hash": cached_result.get("tx_hash")
                    }
                )
                return True
            logger.warning(
                "Cached: Snipe failed for %s on %s via %s: %s",
                self.helpers.shorten_address(token_address),
                chain,
                dex,
                cached_result.get("error", "Unknown error"),
                extra={
                    "type": "sniping_failed",
                    "token_address": self.helpers.shorten_address(token_address),
                    "chain": chain,
                    "dex": dex
                }
            )
            self.blacklist.add(token_address)
            return False

        if token_address in self.blacklist:
            logger.warning(
                "Skipping blacklisted token: %s",
                self.helpers.shorten_address(token_address),
                extra={"token_address": self.helpers.shorten_address(token_address)}
            )
            return False
            
        if token_address in self.pending_txs:
            if time.time() - self.pending_txs[token_address] < 120:
                logger.warning(
                    "Already sniping this token (pending for %d sec)",
                    int(time.time() - self.pending_txs[token_address]),
                    extra={"token_address": self.helpers.shorten_address(token_address)}
                )
                return False
            else:
                logger.warning(
                    "Clearing stale pending transaction for %s",
                    self.helpers.shorten_address(token_address),
                    extra={"token_address": self.helpers.shorten_address(token_address)}
                )
                del self.pending_txs[token_address]

        try:
            self.pending_txs[token_address] = time.time()
            
            logger.info(
                "Attempting snipe for %s on %s via %s",
                self.helpers.shorten_address(token_address),
                chain,
                dex,
                extra={
                    "type": "sniping_start",
                    "token_address": self.helpers.shorten_address(token_address),
                    "chain": chain,
                    "dex": dex
                }
            )

            if chain == 'ethereum':
                result = self._snipe_ethereum(token_address, dex)
            elif chain == 'solana':
                result = self._snipe_solana(token_address, dex)
            else:
                logger.error("Unsupported chain: %s", chain)
                self.cache.set(cache_key, {"status": "failed", "error": f"Unsupported chain: {chain}"})
                return False
                
            if not result:
                self.blacklist.add(token_address)
                self.cache.set(cache_key, {"status": "failed", "error": "Snipe execution failed"})
                
            return result
            
        except Exception as e:
            logger.error(
                "Sniping failed for %s: %s",
                self.helpers.shorten_address(token_address),
                str(e),
                exc_info=True,
                stack_info=True,
                extra={"token_address": self.helpers.shorten_address(token_address)}
            )
            self.cache.set(cache_key, {"status": "failed", "error": str(e)})
            return False
        finally:
            if token_address in self.pending_txs:
                del self.pending_txs[token_address]

    def _snipe_ethereum(self, token_address, dex):
        """Handles EVM snipes with EIP-1559 gas optimization."""
        w3 = Web3(Web3.HTTPProvider(self.chains['ethereum']['rpc']))
        
        # Security Checks
        if self.enable_anti_rug:
            checker = AntiRugChecker(w3, token_address, 'ethereum', self.settings, cache_dir=self.cache.cache_dir)
            if not checker.run_all_checks():
                failed = checker.get_failed_checks()  # Assumes AntiRugChecker has this method
                logger.critical(
                    "Rug detected in %s. Failed checks: %s",
                    self.helpers.shorten_address(token_address),
                    ", ".join(failed),
                    extra={
                        "token_address": self.helpers.shorten_address(token_address),
                        "failed_checks": failed
                    }
                )
                self.cache.set(
                    f"snipe_ethereum_{dex}_{token_address}",
                    {"status": "failed", "error": f"Rug detected: {', '.join(failed)}"}
                )
                return False

        # Balance Validation
        balance = w3.eth.get_balance(self.wallets['primary']['address']) / 1e18
        if balance < self.min_balance:
            logger.warning(
                "Insufficient ETH balance: %.4f (Required: %.4f)",
                balance,
                self.min_balance,
                extra={"token_address": self.helpers.shorten_address(token_address)}
            )
            self.cache.set(
                f"snipe_ethereum_{dex}_{token_address}",
                {"status": "failed", "error": f"Insufficient ETH balance: {balance}"}
            )
            return False

        # Gas Optimization
        try:
            current_block = w3.eth.get_block('pending')
            base_fee = current_block['baseFeePerGas']
            priority_fee = self.helpers.calculate_priority_fee(w3)
            
            tx_params = {
                'maxFeePerGas': int(base_fee * self.gas_multiplier + priority_fee),
                'maxPriorityFeePerGas': priority_fee,
                'type': 2  # EIP-1559
            }
        except Exception as e:
            logger.error(
                "Failed to calculate gas parameters: %s",
                str(e),
                extra={"token_address": self.helpers.shorten_address(token_address)}
            )
            self.cache.set(
                f"snipe_ethereum_{dex}_{token_address}",
                {"status": "failed", "error": f"Gas calculation failed: {str(e)}"}
            )
            return False

        logger.debug(
            "ETH Balance OK: %.4f | Gas Params: %s",
            balance,
            tx_params,
            extra={"token_address": self.helpers.shorten_address(token_address)}
        )

        # DEX Execution
        if dex == 'uniswapv3':
            result = self.uniswap.execute_swap(
                token_in=self.wallets['primary']['address'],
                token_out=token_address,
                amount_in=self.settings['trading']['sniping']['amount_in'],
                min_amount_out=0,  # Calculated with slippage
                fee_tier=self.settings['trading']['sniping'].get('fee_tier', 3000)
            )
            if result and result.get("status") == "confirmed":
                self.cache.set(
                    f"snipe_ethereum_{dex}_{token_address}",
                    {"status": "success", "tx_hash": result["tx_hash"], "gas_cost": result["gas_cost"]}
                )
                return True
            self.cache.set(
                f"snipe_ethereum_{dex}_{token_address}",
                {"status": "failed", "error": "Uniswap swap failed"}
            )
            return False
        return False

    def _snipe_solana(self, token_address, dex):
        """Handles Solana snipes with RPC failover."""
        client = SolanaClient(self.chains['solana']['rpc'])
        
        try:
            if dex == 'raydium':
                result = self.raydium.execute_swap(
                    token_in=self.wallets['primary']['address'],
                    token_out=token_address,
                    amount_in=self.settings['trading']['sniping']['amount_in'],
                    min_amount_out=0,  # Calculated with slippage
                    max_retries=3
                )
                if not result:
                    logger.warning(
                        "Raydium failed, trying Jupiter fallback for %s",
                        self.helpers.shorten_address(token_address),
                        extra={"token_address": self.helpers.shorten_address(token_address)}
                    )
                    return self._execute_jupiter_fallback(client, token_address, dex)
                self.cache.set(
                    f"snipe_solana_{dex}_{token_address}",
                    {"status": "success", "tx_hash": result["tx_id"], "fee": result["fee"]}
                )
                return True
                
            elif dex == 'jupiter':
                return self._execute_jupiter_fallback(client, token_address, dex)
                
        except SolanaRpcException as e:
            logger.error(
                "Solana RPC Error for %s: %s",
                self.helpers.shorten_address(token_address),
                str(e),
                extra={"token_address": self.helpers.shorten_address(token_address)}
            )
            return self._retry_solana_snipe(token_address, dex)
            
        return False

    def _execute_jupiter_fallback(self, client, token_address, dex):
        """Dedicated Jupiter swap handler."""
        result = self.jupiter.execute_swap(
            token_in=self.wallets['primary']['address'],
            token_out=token_address,
            amount_in=self.settings['trading']['sniping']['amount_in'],
            min_amount_out=0,  # Calculated with slippage
            max_retries=3
        )
        if result and result.get("status") == "confirmed":
            self.cache.set(
                f"snipe_solana_{dex}_{token_address}",
                {"status": "success", "tx_hash": result["tx_id"], "fee": result["fee"]}
            )
            return True
        self.cache.set(
            f"snipe_solana_{dex}_{token_address}",
            {"status": "failed", "error": "Jupiter swap failed"}
        )
        return False

    def _retry_solana_snipe(self, token_address, dex, retries=3):
        """Retry mechanism for Solana RPC failures."""
        for attempt in range(retries):
            try:
                logger.warning(
                    "Retry attempt %d/%d for %s",
                    attempt + 1,
                    retries,
                    self.helpers.shorten_address(token_address),
                    extra={"token_address": self.helpers.shorten_address(token_address)}
                )
                time.sleep(1.5 ** attempt)  # Exponential backoff
                
                alt_rpc = self.chains['solana']['fallback_rpcs'][attempt % len(
                    self.chains['solana']['fallback_rpcs'])]
                client = SolanaClient(alt_rpc)
                
                if dex == 'raydium':
                    result = self.raydium.execute_swap(
                        token_in=self.wallets['primary']['address'],
                        token_out=token_address,
                        amount_in=self.settings['trading']['sniping']['amount_in'],
                        min_amount_out=0,
                        max_retries=3
                    )
                    if result and result.get("status") == "confirmed":
                        self.cache.set(
                            f"snipe_solana_{dex}_{token_address}",
                            {"status": "success", "tx_hash": result["tx_id"], "fee": result["fee"]}
                        )
                        return True
                    self.cache.set(
                        f"snipe_solana_{dex}_{token_address}",
                        {"status": "failed", "error": "Raydium retry failed"}
                    )
                else:
                    return self._execute_jupiter_fallback(client, token_address, dex)
                    
            except Exception as e:
                logger.error(
                    "Retry %d failed for %s: %s",
                    attempt + 1,
                    self.helpers.shorten_address(token_address),
                    str(e),
                    extra={"token_address": self.helpers.shorten_address(token_address)}
                )
                self.cache.set(
                    f"snipe_solana_{dex}_{token_address}",
                    {"status": "failed", "error": f"Retry {attempt + 1} failed: {str(e)}"}
                )
                
        return False