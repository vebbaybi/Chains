# dex_clients/jupiter.py
"""
Jupiter DEX client for ChainCrawlr:
- Fetches swap quotes via Jupiter API
- Executes swaps on Solana blockchain with confirmation
- Integrates with ChainHelpers for formatting and address handling
- Uses ChainCrawlerLogger for structured logging
- Integrates with JSONFileCache for caching quotes
"""

import json
import time
from pathlib import Path

import requests
import yaml
from base58 import b58decode, b58encode
from solana.keypair import Keypair
from solana.publickey import PublicKey
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solana.transaction import Transaction

from utils.caching import JSONFileCache
from utils.helpers import ChainHelpers
from utils.logger import logger


class JupiterClient:
    def __init__(self, config_path="config/chains.json", settings_path="config/settings.yaml", cache_dir=".cache"):
        """Initialize JupiterClient for Solana DEX operations with caching."""
        self.helpers = ChainHelpers(chain="solana")
        self.config = self._load_config(config_path)
        self.settings = self._load_settings(settings_path)
        self.cache = JSONFileCache(cache_dir=cache_dir, max_age=300)  # 5-minute cache for quotes
        self.chain = next((c for c in self.config["chains"] if c["name"].lower() == "solana"), None)
        if not self.chain:
            logger.error("Solana chain not found in %s", config_path)
            raise ValueError("Solana chain configuration missing")
        if not self.chain.get("rpc_urls") or not self.chain.get("dexes"):
            logger.error("Invalid Solana chain configuration: missing rpc_urls or dexes")
            raise ValueError("Invalid Solana chain configuration")

        try:
            self.client = Client(self.chain["rpc_urls"][0])
            aggregator_address = self.chain["dexes"][1]["aggregator_address"]
            wallet_address = self.settings["wallets"]["primary"]["address"]
            if not self.helpers.is_valid_address(aggregator_address):
                logger.error("Invalid aggregator address: %s", aggregator_address)
                raise ValueError("Invalid aggregator address")
            if not self.helpers.is_valid_address(wallet_address):
                logger.error("Invalid wallet address: %s", wallet_address)
                raise ValueError("Invalid wallet address")
            self.aggregator_address = PublicKey(aggregator_address)
            self.wallet_address = PublicKey(wallet_address)
            private_key = self.settings["wallets"]["primary"].get("private_key")
            if not private_key:
                logger.error("No private key provided for wallet")
                raise ValueError("No private key in settings")
            try:
                self.keypair = Keypair.from_seed(b58decode(private_key)[:32])
            except Exception as e:
                logger.error("Invalid private key format: %s", e)
                raise ValueError("Invalid private key format")
        except Exception as e:
            logger.error("Failed to initialize Solana client or addresses: %s", e)
            raise ValueError(f"Invalid configuration: {e}")

    def _load_config(self, config_path):
        """Load chain configuration from JSON file with caching."""
        config_path = Path(config_path)
        cache_key = f"config_{config_path.name}_{config_path.stat().st_mtime if config_path.exists() else 0}"
        
        cached_config = self.cache.get(cache_key)
        if cached_config is not None:
            logger.debug("Loaded config from cache: %s", config_path)
            return cached_config

        try:
            with config_path.open('r', encoding='utf-8') as f:
                config = json.load(f)
                if not config.get("chains"):
                    logger.error("No chains found in %s", config_path)
                    raise ValueError("No chains in configuration")
                self.cache.set(cache_key, config)
                logger.debug("Loaded and cached config: %s", config_path)
                return config
        except Exception as e:
            logger.error("Failed to load %s: %s", config_path, e)
            raise ValueError(f"Failed to load {config_path}: {e}")

    def _load_settings(self, settings_path):
        """Load settings from YAML file with caching."""
        settings_path = Path(settings_path)
        cache_key = f"settings_{settings_path.name}_{settings_path.stat().st_mtime if settings_path.exists() else 0}"
        
        cached_settings = self.cache.get(cache_key)
        if cached_settings is not None:
            logger.debug("Loaded settings from cache: %s", settings_path)
            return cached_settings

        try:
            with settings_path.open('r', encoding='utf-8') as f:
                settings = yaml.safe_load(f)
                if not settings.get("wallets", {}).get("primary"):
                    logger.error("No primary wallet found in %s", settings_path)
                    raise ValueError("No primary wallet in settings")
                self.cache.set(cache_key, settings)
                logger.debug("Loaded and cached settings: %s", settings_path)
                return settings
        except Exception as e:
            logger.error("Failed to load %s: %s", settings_path, e)
            raise ValueError(f"Failed to load {settings_path}: {e}")

    def get_quote(self, input_mint, output_mint, amount, max_retries=3):
        """Fetch a swap quote from Jupiter API with retry logic and caching."""
        try:
            if not self.helpers.is_valid_address(str(input_mint)) or not self.helpers.is_valid_address(str(output_mint)):
                logger.error("Invalid mint addresses: input=%s, output=%s", input_mint, output_mint)
                raise ValueError("Invalid mint addresses")
            if amount <= 0:
                logger.error("Amount must be positive: %s", amount)
                raise ValueError("Amount must be positive")
            amount_wei = self.helpers.to_wei(amount)
            cache_key = f"quote_{input_mint}_{output_mint}_{amount_wei}_{self.settings['wallets']['primary']['max_slippage']}"
            
            cached_quote = self.cache.get(cache_key)
            if cached_quote is not None:
                logger.debug("Retrieved quote from cache: %s", cache_key)
                out_amount = self.helpers.format_token_amount(cached_quote["outAmount"])
                logger.info(
                    "Cached Jupiter quote for %s -> %s: %s %s",
                    self.helpers.shorten_address(str(input_mint)),
                    self.helpers.shorten_address(str(output_mint)),
                    out_amount,
                    self.helpers.get_native_symbol(),
                    extra={
                        "input_mint": self.helpers.shorten_address(str(input_mint)),
                        "output_mint": self.helpers.shorten_address(str(output_mint)),
                        "amount": amount,
                        "out_amount": out_amount,
                        "wallet_address": self.helpers.shorten_address(str(self.wallet_address))
                    }
                )
                return cached_quote

            url = "https://quote-api.jup.ag/v4/quote"
            params = {
                "inputMint": str(input_mint),
                "outputMint": str(output_mint),
                "amount": amount_wei,
                "slippageBps": int(self.settings["wallets"]["primary"]["max_slippage"] * 10000)
            }
            for attempt in range(max_retries):
                try:
                    response = requests.get(url, params=params, timeout=5)
                    response.raise_for_status()
                    quote = response.json()
                    if not quote.get("outAmount"):
                        logger.error("Invalid quote response: no outAmount")
                        return None
                    self.cache.set(cache_key, quote)
                    out_amount = self.helpers.format_token_amount(quote["outAmount"])
                    logger.info(
                        "Jupiter quote for %s -> %s: %s %s",
                        self.helpers.shorten_address(str(input_mint)),
                        self.helpers.shorten_address(str(output_mint)),
                        out_amount,
                        self.helpers.get_native_symbol(),
                        extra={
                            "input_mint": self.helpers.shorten_address(str(input_mint)),
                            "output_mint": self.helpers.shorten_address(str(output_mint)),
                            "amount": amount,
                            "out_amount": out_amount,
                            "wallet_address": self.helpers.shorten_address(str(self.wallet_address))
                        }
                    )
                    return quote
                except requests.RequestException as e:
                    if attempt == max_retries - 1:
                        logger.error("Failed to fetch Jupiter quote after %d retries: %s", max_retries, e)
                        return None
                    time.sleep(1)
        except Exception as e:
            logger.error("Invalid parameters for Jupiter quote: %s", e)
            return None

    def confirm_transaction(self, tx_id, max_attempts=10, delay=2):
        """Confirm a transaction on Solana blockchain."""
        try:
            cache_key = f"tx_confirm_{tx_id}"
            cached_result = self.cache.get(cache_key)
            if cached_result is not None:
                logger.debug("Retrieved transaction confirmation from cache: %s", tx_id)
                if cached_result.get("status") == "confirmed":
                    logger.info(
                        "Cached transaction %s confirmed",
                        tx_id,
                        extra={"tx_id": tx_id, "wallet_address": self.helpers.shorten_address(str(self.wallet_address))}
                    )
                    return True
                logger.error("Cached transaction %s failed: %s", tx_id, cached_result.get("error", "Unknown error"))
                return False

            for attempt in range(max_attempts):
                response = self.client.get_transaction(tx_id, commitment=Confirmed)
                if response.get("result"):
                    status = response["result"]["meta"].get("status")
                    if status and not status.get("Err"):
                        self.cache.set(cache_key, {"status": "confirmed", "timestamp": time.time()})
                        logger.info(
                            "Transaction %s confirmed",
                            tx_id,
                            extra={"tx_id": tx_id, "wallet_address": self.helpers.shorten_address(str(self.wallet_address))}
                        )
                        return True
                    error = status.get("Err", "Unknown error")
                    self.cache.set(cache_key, {"status": "failed", "error": error, "timestamp": time.time()})
                    logger.error("Transaction %s failed: %s", tx_id, error)
                    return False
                time.sleep(delay)
            logger.error("Transaction %s confirmation timed out after %d attempts", tx_id, max_attempts)
            self.cache.set(cache_key, {"status": "timeout", "timestamp": time.time()})
            return False
        except Exception as e:
            logger.error("Failed to confirm transaction %s: %s", tx_id, e)
            return False

    def execute_swap(self, input_mint, output_mint, amount, min_amount_out, max_retries=3):
        """Execute a swap on Jupiter with transaction signing and confirmation."""
        try:
            if amount <= 0 or min_amount_out <= 0:
                logger.error("Amount and min_amount_out must be positive: amount=%s, min_amount_out=%s", amount, min_amount_out)
                raise ValueError("Amount and min_amount_out must be positive")
            quote = self.get_quote(input_mint, output_mint, amount)
            if not quote:
                logger.error("No valid quote available for swap execution")
                return None

            amount_wei = self.helpers.to_wei(amount)
            min_amount_out_wei = self.helpers.to_wei(min_amount_out)
            fee_lamports = self.client.get_minimum_balance_for_rent_exemption(0).get("result", 5000)
            fee_sol = self.helpers.format_token_amount(fee_lamports, decimals=9)

            cache_key = f"swap_{input_mint}_{output_mint}_{amount_wei}_{min_amount_out_wei}"
            cached_swap = self.cache.get(cache_key)
            if cached_swap and cached_swap.get("status") == "confirmed":
                logger.debug("Retrieved swap result from cache: %s", cache_key)
                logger.info(
                    "Cached swap: %s -> %s, amount=%s, min_out=%s, fee=%s %s, tx_id=%s",
                    self.helpers.shorten_address(str(input_mint)),
                    self.helpers.shorten_address(str(output_mint)),
                    self.helpers.format_token_amount(amount_wei),
                    self.helpers.format_token_amount(min_amount_out_wei),
                    fee_sol,
                    self.helpers.get_native_symbol(),
                    cached_swap["tx_id"],
                    extra={
                        "input_mint": self.helpers.shorten_address(str(input_mint)),
                        "output_mint": self.helpers.shorten_address(str(output_mint)),
                        "amount": self.helpers.format_token_amount(amount_wei),
                        "min_amount_out": self.helpers.format_token_amount(min_amount_out_wei),
                        "fee": fee_sol,
                        "tx_id": cached_swap["tx_id"],
                        "wallet_address": self.helpers.shorten_address(str(self.wallet_address))
                    }
                )
                return cached_swap

            url = "https://quote-api.jup.ag/v4/swap"
            payload = {
                "quoteResponse": quote,
                "userPublicKey": str(self.wallet_address),
                "wrapUnwrapSOL": True
            }
            for attempt in range(max_retries):
                try:
                    response = requests.post(url, json=payload, timeout=5)
                    response.raise_for_status()
                    swap_data = response.json()
                    if not swap_data.get("swapTransaction"):
                        logger.error("Invalid swap response: no swapTransaction")
                        return None
                    break
                except requests.RequestException as e:
                    if attempt == max_retries - 1:
                        logger.error("Failed to fetch swap instructions after %d retries: %s", max_retries, e)
                        return None
                    time.sleep(1)

            try:
                swap_tx = b58decode(swap_data["swapTransaction"])
                tx = Transaction.deserialize(swap_tx)
                tx.sign(self.keypair)
            except Exception as e:
                logger.error("Failed to deserialize or sign transaction: %s", e)
                return None

            try:
                response = self.client.send_transaction(tx, self.keypair, opts=Confirmed)
                tx_id = response.get("result")
                if not tx_id:
                    logger.error("Transaction failed: no transaction ID returned")
                    return None

                if not self.confirm_transaction(tx_id):
                    logger.error("Swap transaction %s failed confirmation", tx_id)
                    return None

                swap_result = {
                    "tx_id": tx_id,
                    "status": "confirmed",
                    "fee": fee_sol
                }
                self.cache.set(cache_key, swap_result)
                logger.info(
                    "Executed swap: %s -> %s, amount=%s, min_out=%s, fee=%s %s, tx_id=%s",
                    self.helpers.shorten_address(str(input_mint)),
                    self.helpers.shorten_address(str(output_mint)),
                    self.helpers.format_token_amount(amount_wei),
                    self.helpers.format_token_amount(min_amount_out_wei),
                    fee_sol,
                    self.helpers.get_native_symbol(),
                    tx_id,
                    extra={
                        "input_mint": self.helpers.shorten_address(str(input_mint)),
                        "output_mint": self.helpers.shorten_address(str(output_mint)),
                        "amount": self.helpers.format_token_amount(amount_wei),
                        "min_amount_out": self.helpers.format_token_amount(min_amount_out_wei),
                        "fee": fee_sol,
                        "tx_id": tx_id,
                        "wallet_address": self.helpers.shorten_address(str(self.wallet_address))
                    }
                )
                return swap_result
            except Exception as e:
                logger.error("Transaction signing/sending failed: %s", e)
                return None

        except Exception as e:
            logger.error("Swap execution failed: %s", e)
            return None