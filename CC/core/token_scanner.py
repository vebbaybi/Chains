# core/token_scanner.py
"""
TokenScanner Module for ChainCrawlr:
- Dynamically scans newly deployed tokens across supported chains
- Filters by liquidity and token age thresholds
- Integrates with UniswapV3, Raydium, and Jupiter DEX clients
- Uses AntiRugChecker for contract validation and safety
- Logs structured results for tokens passing all filters
- Integrates with JSONFileCache for caching configuration and token scan results
"""

import json
import time
from pathlib import Path

import requests
import yaml
from solana.rpc.api import Client
from web3 import Web3

from core.anti_rug import AntiRugChecker
from dex_clients.jupiter import JupiterClient
from dex_clients.raydium import RaydiumClient
from dex_clients.uniswap import UniswapV3Client
from utils.caching import JSONFileCache
from utils.helpers import ChainHelpers
from utils.logger import logger


class TokenScanner:
    def __init__(self, config_path="config/chains.json", settings_path="config/settings.yaml", cache_dir=".cache"):
        """Initialize TokenScanner with caching for config and settings."""
        self.cache = JSONFileCache(cache_dir=cache_dir, max_age=300)  # 5-minute cache
        self.config = self._load_config(config_path)
        self.settings = self._load_settings(settings_path)
        self.helpers = ChainHelpers()
        self.uniswap = UniswapV3Client(config_path, settings_path, cache_dir)
        self.raydium = RaydiumClient(config_path, settings_path, cache_dir)
        self.jupiter = JupiterClient(config_path, settings_path, cache_dir)
        self.clients = self._init_clients()
        self.min_liquidity = self.settings["trading"]["sniping"].get("min_liquidity_usd", 10000)
        self.max_token_age = self.settings["trading"]["sniping"].get("max_token_age_minutes", 60) * 60
        self.enable_anti_rug = self.settings["trading"].get("anti_rug", {}).get("enabled", True)

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
                    logger.error("No chains defined in %s", config_path)
                    raise ValueError("No chains defined in config")
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
                if not settings.get("trading"):
                    logger.error("Missing trading settings in %s", settings_path)
                    raise ValueError("Missing trading settings")
                self.cache.set(cache_key, settings)
                logger.debug("Loaded and cached settings: %s", settings_path)
                return settings
        except Exception as e:
            logger.error("Failed to load %s: %s", settings_path, e)
            raise ValueError(f"Failed to load {settings_path}: {e}")

    def _init_clients(self):
        """Initialize DEX clients for supported chains."""
        return {
            "ethereum": self.uniswap,
            "solana": {
                "raydium": self.raydium,
                "jupiter": self.jupiter
            }
        }

    def _fetch_new_tokens_ethereum(self, client, max_retries=3):
        """Fetch new tokens from Ethereum using an explorer API."""
        api_key = self.settings.get("explorer_api_key")
        if not api_key:
            logger.warning("Ethereum token scan skipped: No explorer API key provided")
            return []
        url = f"https://api.etherscan.io/api?module=account&action=tokenlist&address={client.wallet_address}&apikey={api_key}"
        for attempt in range(max_retries):
            try:
                response = requests.get(url, timeout=5)
                response.raise_for_status()
                data = response.json()
                if data.get("status") != "1" or not data.get("result"):
                    logger.warning("Invalid token list response: %s", data.get("message", "Unknown error"))
                    return []
                return [
                    {"address": token["contractAddress"], "created_at": int(time.time()) - 300}  # Mock creation time
                    for token in data["result"]
                ]
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error("Failed to fetch Ethereum tokens after %d retries: %s", max_retries, e)
                    return []
                time.sleep(1)
        return []

    def _fetch_new_tokens_solana(self, client, dex_name, max_retries=3):
        """Fetch new tokens from Solana using a DEX API (simplified)."""
        url = f"https://api.{dex_name}.io/v1/pools"
        for attempt in range(max_retries):
            try:
                response = requests.get(url, timeout=5)
                response.raise_for_status()
                data = response.json()
                return [
                    {"address": pool["tokenMint"], "pool_id": pool["poolId"], "created_at": int(time.time()) - 300}  # Mock creation time
                    for pool in data.get("pools", [])
                ]
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error("Failed to fetch Solana %s tokens after %d retries: %s", dex_name, max_retries, e)
                    return []
                time.sleep(1)
        return []

    def _run_anti_rug_check(self, web3, token_address, chain_name):
        """Run anti-rug checks with caching."""
        if not self.enable_anti_rug:
            logger.debug("Anti-rug checks disabled for %s", self.helpers.shorten_address(token_address))
            return True
        cache_key = f"anti_rug_{chain_name}_{token_address}"
        cached_result = self.cache.get(cache_key)
        if cached_result is not None:
            logger.debug("Retrieved anti-rug check from cache: %s", cache_key)
            return cached_result

        try:
            checker = AntiRugChecker(web3, token_address, chain_name, self.settings, cache_dir=self.cache.cache_dir)
            result = checker.run_all_checks()
            self.cache.set(cache_key, result)
            return result
        except Exception as e:
            logger.warning("Anti-rug check failed for %s: %s", self.helpers.shorten_address(token_address), e)
            self.cache.set(cache_key, False)
            return False

    def scan_new_tokens(self):
        """Scan for new tokens across supported chains with caching."""
        cache_key = f"scan_tokens_{int(time.time() // 300)}"  # Cache per 5-minute window
        cached_tokens = self.cache.get(cache_key)
        if cached_tokens is not None:
            logger.debug("Retrieved token scan results from cache: %s", cache_key)
            return cached_tokens

        new_tokens = []
        for chain_name, client in self.clients.items():
            try:
                if chain_name == "ethereum":
                    new_tokens.extend(self._scan_ethereum(client))
                elif chain_name == "solana":
                    for dex_name, dex_client in client.items():
                        new_tokens.extend(self._scan_solana(dex_client, dex_name))
            except Exception as e:
                logger.error("Error scanning tokens on %s: %s", chain_name, e)
        self.cache.set(cache_key, new_tokens)
        return new_tokens

    def _scan_ethereum(self, client):
        """Scan Ethereum for new tokens with liquidity and age filters."""
        tokens = []
        new_tokens = self._fetch_new_tokens_ethereum(client)
        for token in new_tokens:
            token_address = token["address"]
            cache_key = f"token_info_ethereum_{token_address}"
            cached_token = self.cache.get(cache_key)
            if cached_token is not None:
                logger.debug("Retrieved token info from cache: %s", cache_key)
                if cached_token["liquidity_usd"] >= self.min_liquidity and cached_token["age_seconds"] <= self.max_token_age:
                    tokens.append(cached_token)
                continue

            try:
                for fee_tier in client.chain["dexes"][0].get("fee_tiers", []):
                    liquidity = client.get_pool_liquidity(token_address, fee_tier)
                    liquidity_usd = liquidity * 1e-18 * 2000  # Simplified USD conversion
                    age_seconds = int(time.time()) - token["created_at"]
                    token_info = {
                        "chain": "ethereum",
                        "dex": "uniswapv3",
                        "token_address": token_address,
                        "liquidity_usd": liquidity_usd,
                        "age_seconds": age_seconds
                    }
                    if liquidity_usd >= self.min_liquidity and age_seconds <= self.max_token_age:
                        if self._run_anti_rug_check(client.w3, token_address, "ethereum"):
                            logger.info(
                                "New token passed filter: %s",
                                token_info,
                                extra={
                                    "type": "new_token_detected",
                                    "chain": "ethereum",
                                    "dex": "uniswapv3",
                                    "token_address": self.helpers.shorten_address(token_address),
                                    "liquidity_usd": liquidity_usd,
                                    "age_seconds": age_seconds
                                }
                            )
                            self.cache.set(cache_key, token_info)
                            tokens.append(token_info)
            except Exception as e:
                logger.error(
                    "Error scanning Ethereum token %s: %s",
                    self.helpers.shorten_address(token_address),
                    e
                )
        return tokens

    def _scan_solana(self, client, dex_name):
        """Scan Solana for new tokens with liquidity and age filters."""
        tokens = []
        new_tokens = self._fetch_new_tokens_solana(client, dex_name)
        for token in new_tokens:
            token_address = token["address"]
            pool_id = token["pool_id"]
            cache_key = f"token_info_solana_{dex_name}_{token_address}"
            cached_token = self.cache.get(cache_key)
            if cached_token is not None:
                logger.debug("Retrieved token info from cache: %s", cache_key)
                if cached_token["liquidity_usd"] >= self.min_liquidity and cached_token["age_seconds"] <= self.max_token_age:
                    tokens.append(cached_token)
                continue

            try:
                liquidity = client.get_pool_liquidity(pool_id)
                liquidity_usd = liquidity * 1e-9 * 100  # Simplified USD conversion
                age_seconds = int(time.time()) - token["created_at"]
                token_info = {
                    "chain": "solana",
                    "dex": dex_name,
                    "token_address": token_address,
                    "liquidity_usd": liquidity_usd,
                    "age_seconds": age_seconds
                }
                if liquidity_usd >= self.min_liquidity and age_seconds <= self.max_token_age:
                    logger.info(
                        "New token detected: %s",
                        token_info,
                        extra={
                            "type": "new_token_detected",
                            "chain": "solana",
                            "dex": dex_name,
                            "token_address": self.helpers.shorten_address(token_address),
                            "liquidity_usd": liquidity_usd,
                            "age_seconds": age_seconds
                        }
                    )
                    self.cache.set(cache_key, token_info)
                    tokens.append(token_info)
            except Exception as e:
                logger.error(
                    "Error scanning Solana %s token %s: %s",
                    dex_name,
                    self.helpers.shorten_address(token_address),
                    e
                )
        return tokens