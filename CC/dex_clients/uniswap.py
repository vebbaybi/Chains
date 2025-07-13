# dex_clients/uniswap.py
"""
Uniswap V3 DEX client for ChainCrawlr:
- Fetches pool liquidity from Ethereum blockchain
- Executes swaps with transaction signing and confirmation
- Integrates with ChainHelpers for formatting, gas estimation, and address handling
- Uses ChainCrawlerLogger for structured logging
- Integrates with JSONFileCache for caching configuration, ABI, quotes, and liquidity data
"""

import json
import time
from datetime import datetime
from pathlib import Path

import requests
import yaml
from web3 import Web3
from web3.middleware import geth_poa_middleware

from utils.caching import JSONFileCache
from utils.helpers import ChainHelpers
from utils.logger import logger


class UniswapV3Client:
    def __init__(self, config_path="config/chains.json", settings_path="config/settings.yaml", cache_dir=".cache"):
        """Initialize UniswapV3Client for Ethereum DEX operations with caching."""
        self.helpers = ChainHelpers(chain="ethereum")
        self.config = self._load_config(config_path)
        self.settings = self._load_settings(settings_path)
        self.cache = JSONFileCache(cache_dir=cache_dir, max_age=300)  # 5-minute cache for volatile data
        self.chain = next((c for c in self.config["chains"] if c["name"].lower() == "ethereum"), None)
        if not self.chain:
            logger.error("Ethereum chain not found in %s", config_path)
            raise ValueError("Ethereum chain configuration missing")
        if not self.chain.get("rpc_urls") or not self.chain.get("dexes"):
            logger.error("Invalid Ethereum chain configuration: missing rpc_urls or dexes")
            raise ValueError("Invalid Ethereum chain configuration")

        try:
            self.w3 = self._init_web3()
            self.factory_abi = self._load_abi("UniswapV3Factory")
            self.router_abi = self._load_abi("UniswapV3Router")
            self.quoter_abi = self._load_abi("UniswapV3Quoter")
            factory_address = self.chain["dexes"][0]["factory_address"]
            router_address = self.chain["dexes"][0]["router_address"]
            quoter_address = self.chain["dexes"][0]["quoter_address"]
            wallet_address = self.settings["wallets"]["primary"]["address"]
            if not self.helpers.is_valid_address(factory_address):
                logger.error("Invalid factory address: %s", factory_address)
                raise ValueError("Invalid factory address")
            if not self.helpers.is_valid_address(router_address):
                logger.error("Invalid router address: %s", router_address)
                raise ValueError("Invalid router address")
            if not self.helpers.is_valid_address(quoter_address):
                logger.error("Invalid quoter address: %s", quoter_address)
                raise ValueError("Invalid quoter address")
            if not self.helpers.is_valid_address(wallet_address):
                logger.error("Invalid wallet address: %s", wallet_address)
                raise ValueError("Invalid wallet address")
            self.factory_contract = self.w3.eth.contract(address=self.helpers.checksum(factory_address), abi=self.factory_abi)
            self.router_contract = self.w3.eth.contract(address=self.helpers.checksum(router_address), abi=self.router_abi)
            self.quoter_contract = self.w3.eth.contract(address=self.helpers.checksum(quoter_address), abi=self.quoter_abi)
            self.wallet_address = self.helpers.checksum(wallet_address)
            private_key = self.settings["wallets"]["primary"].get("private_key")
            if not private_key:
                logger.error("No private key provided for wallet")
                raise ValueError("No private key in settings")
            self.w3.eth.account.enable_unaudited_hdwallet_features()
            self.account = self.w3.eth.account.from_key(private_key)
            if self.account.address != self.wallet_address:
                logger.error("Private key does not match wallet address")
                raise ValueError("Private key mismatch")
        except Exception as e:
            logger.error("Failed to initialize Ethereum client or contracts: %s", e)
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

    def _load_abi(self, contract_name):
        """Load contract ABI from remote source with caching."""
        abi_urls = {
            "UniswapV3Factory": "https://unpkg.com/@uniswap/v3-core/artifacts/contracts/UniswapV3Factory.sol/UniswapV3Factory.json",
            "UniswapV3Router": "https://unpkg.com/@uniswap/v3-periphery/artifacts/contracts/SwapRouter.sol/SwapRouter.json",
            "UniswapV3Quoter": "https://unpkg.com/@uniswap/v3-periphery/artifacts/contracts/lens/QuoterV2.sol/QuoterV2.json",
            "UniswapV3Pool": "https://unpkg.com/@uniswap/v3-core/artifacts/contracts/UniswapV3Pool.sol/UniswapV3Pool.json"
        }
        cache_key = f"abi_{contract_name}"
        
        cached_abi = self.cache.get(cache_key)
        if cached_abi is not None:
            logger.debug("Loaded ABI from cache: %s", contract_name)
            return cached_abi

        try:
            response = requests.get(abi_urls[contract_name], timeout=5)
            response.raise_for_status()
            abi = response.json()["abi"]
            self.cache.set(cache_key, abi)
            logger.debug("Loaded and cached ABI for %s", contract_name)
            return abi
        except Exception as e:
            logger.error("Failed to load ABI for %s: %s", contract_name, e)
            raise ValueError(f"Failed to load ABI for {contract_name}: {e}")

    def _init_web3(self):
        """Initialize Web3 connection."""
        try:
            w3 = Web3(Web3.HTTPProvider(self.chain["rpc_urls"][0]))
            w3.middleware_onion.inject(geth_poa_middleware, layer=0)
            if not w3.is_connected():
                logger.error("Failed to connect to Ethereum RPC: %s", self.chain["rpc_urls"][0])
                raise ConnectionError("Ethereum RPC connection failed")
            return w3
        except Exception as e:
            logger.error("Failed to initialize Web3: %s", e)
            raise ConnectionError(f"Web3 initialization failed: {e}")

    def get_pool_liquidity(self, token_address, fee_tier, max_retries=3):
        """Fetch liquidity for a Uniswap V3 pool with caching."""
        try:
            if not self.helpers.is_valid_address(token_address):
                logger.error("Invalid token address: %s", token_address)
                raise ValueError("Invalid token address")
            if fee_tier not in self.chain["dexes"][0]["fee_tiers"]:
                logger.error("Invalid fee tier: %s", fee_tier)
                raise ValueError(f"Invalid fee tier, must be one of {self.chain['dexes'][0]['fee_tiers']}")
            cache_key = f"liquidity_{token_address}_{fee_tier}"
            
            cached_liquidity = self.cache.get(cache_key)
            if cached_liquidity is not None:
                logger.debug("Retrieved pool liquidity from cache: %s", cache_key)
                logger.info(
                    "Cached pool liquidity for %s (fee: %s): %s",
                    self.helpers.shorten_address(token_address),
                    fee_tier,
                    cached_liquidity,
                    extra={
                        "token": self.helpers.shorten_address(token_address),
                        "fee_tier": fee_tier,
                        "liquidity": cached_liquidity,
                        "wallet_address": self.helpers.shorten_address(self.wallet_address)
                    }
                )
                return cached_liquidity

            for attempt in range(max_retries):
                try:
                    pool_address = self.factory_contract.functions.getPool(
                        self.helpers.checksum(token_address),
                        self.helpers.checksum(self.settings["wallets"]["primary"]["address"]),
                        fee_tier
                    ).call()
                    if pool_address == "0x0000000000000000000000000000000000000000":
                        logger.warning(
                            "No pool found for token %s with fee tier %s",
                            self.helpers.shorten_address(token_address),
                            fee_tier
                        )
                        return 0
                    pool_contract = self.w3.eth.contract(address=self.helpers.checksum(pool_address), abi=self._load_abi("UniswapV3Pool"))
                    liquidity_wei = pool_contract.functions.liquidity().call()
                    liquidity = self.helpers.format_token_amount(liquidity_wei)
                    self.cache.set(cache_key, liquidity)
                    logger.info(
                        "Pool liquidity for %s (fee: %s): %s",
                        self.helpers.shorten_address(token_address),
                        fee_tier,
                        liquidity,
                        extra={
                            "token": self.helpers.shorten_address(token_address),
                            "fee_tier": fee_tier,
                            "liquidity": liquidity,
                            "wallet_address": self.helpers.shorten_address(self.wallet_address)
                        }
                    )
                    return liquidity
                except Exception as e:
                    if attempt == max_retries - 1:
                        logger.error("Failed to fetch pool liquidity for %s after %d retries: %s", token_address, max_retries, e)
                        return 0
                    time.sleep(1)
        except Exception as e:
            logger.error("Invalid parameters for pool liquidity: %s", e)
            return 0

    def get_quote(self, token_in, token_out, amount_in, fee_tier, max_retries=3):
        """Fetch a swap quote from Uniswap V3 Quoter with caching."""
        try:
            if not self.helpers.is_valid_address(token_in) or not self.helpers.is_valid_address(token_out):
                logger.error("Invalid token addresses: token_in=%s, token_out=%s", token_in, token_out)
                raise ValueError("Invalid token addresses")
            if amount_in <= 0:
                logger.error("Amount must be positive: %s", amount_in)
                raise ValueError("Amount must be positive")
            if fee_tier not in self.chain["dexes"][0]["fee_tiers"]:
                logger.error("Invalid fee tier: %s", fee_tier)
                raise ValueError(f"Invalid fee tier, must be one of {self.chain['dexes'][0]['fee_tiers']}")
            amount_in_wei = self.helpers.to_wei(amount_in)
            cache_key = f"quote_{token_in}_{token_out}_{amount_in_wei}_{fee_tier}"
            
            cached_quote = self.cache.get(cache_key)
            if cached_quote is not None:
                logger.debug("Retrieved quote from cache: %s", cache_key)
                amount_out = self.helpers.format_token_amount(cached_quote)
                logger.info(
                    "Cached Uniswap V3 quote for %s -> %s: %s",
                    self.helpers.shorten_address(token_in),
                    self.helpers.shorten_address(token_out),
                    amount_out,
                    extra={
                        "token_in": self.helpers.shorten_address(token_in),
                        "token_out": self.helpers.shorten_address(token_out),
                        "amount_in": amount_in,
                        "amount_out": amount_out,
                        "fee_tier": fee_tier,
                        "wallet_address": self.helpers.shorten_address(self.wallet_address)
                    }
                )
                return amount_out

            for attempt in range(max_retries):
                try:
                    quote = self.quoter_contract.functions.quoteExactInputSingle(
                        self.helpers.checksum(token_in),
                        self.helpers.checksum(token_out),
                        fee_tier,
                        amount_in_wei,
                        0
                    ).call()
                    amount_out = self.helpers.format_token_amount(quote[0])
                    self.cache.set(cache_key, quote[0])
                    logger.info(
                        "Uniswap V3 quote for %s -> %s: %s",
                        self.helpers.shorten_address(token_in),
                        self.helpers.shorten_address(token_out),
                        amount_out,
                        extra={
                            "token_in": self.helpers.shorten_address(token_in),
                            "token_out": self.helpers.shorten_address(token_out),
                            "amount_in": amount_in,
                            "amount_out": amount_out,
                            "fee_tier": fee_tier,
                            "wallet_address": self.helpers.shorten_address(self.wallet_address)
                        }
                    )
                    return amount_out
                except Exception as e:
                    if attempt == max_retries - 1:
                        logger.error("Failed to fetch Uniswap quote after %d retries: %s", max_retries, e)
                        return None
                    time.sleep(1)
        except Exception as e:
            logger.error("Invalid parameters for Uniswap quote: %s", e)
            return None

    def execute_swap(self, token_in, token_out, amount_in, min_amount_out, fee_tier, max_retries=3):
        """Execute a swap on Uniswap V3 with transaction signing and confirmation."""
        try:
            if not self.helpers.is_valid_address(token_in) or not self.helpers.is_valid_address(token_out):
                logger.error("Invalid token addresses: token_in=%s, token_out=%s", token_in, token_out)
                raise ValueError("Invalid token addresses")
            if amount_in <= 0 or min_amount_out <= 0:
                logger.error("Amount and min_amount_out must be positive: amount_in=%s, min_amount_out=%s", amount_in, min_amount_out)
                raise ValueError("Amount and min_amount_out must be positive")
            if fee_tier not in self.chain["dexes"][0]["fee_tiers"]:
                logger.error("Invalid fee tier: %s", fee_tier)
                raise ValueError(f"Invalid fee tier, must be one of {self.chain['dexes'][0]['fee_tiers']}")

            amount_in_wei = self.helpers.to_wei(amount_in)
            min_amount_out_wei = self.helpers.to_wei(min_amount_out)
            gas_price_gwei = self.w3.eth.gas_price / 1e9 * self.settings["wallets"]["primary"].get("gas_multiplier", 1.0)
            gas_limit = int(self.settings["trading"]["sniping"].get("gas_limit", 300000) * self.chain["gas_settings"].get("gas_limit_buffer", 1.1))
            gas_cost = self.helpers.estimate_gas_cost(gas_limit, gas_price_gwei)

            cache_key = f"swap_{token_in}_{token_out}_{amount_in_wei}_{min_amount_out_wei}_{fee_tier}"
            cached_swap = self.cache.get(cache_key)
            if cached_swap and cached_swap.get("status") == "confirmed":
                logger.debug("Retrieved swap result from cache: %s", cache_key)
                logger.info(
                    "Cached Uniswap V3 swap: %s -> %s, amount=%s, min_out=%s, gas_cost=%s %s, tx_hash=%s",
                    self.helpers.shorten_address(token_in),
                    self.helpers.shorten_address(token_out),
                    self.helpers.format_token_amount(amount_in_wei),
                    self.helpers.format_token_amount(min_amount_out_wei),
                    cached_swap["gas_cost"],
                    self.helpers.get_native_symbol(),
                    cached_swap["tx_hash"],
                    extra={
                        "type": "trade_executed",
                        "dex": "UniswapV3",
                        "token_in": self.helpers.shorten_address(token_in),
                        "token_out": self.helpers.shorten_address(token_out),
                        "amount_in": self.helpers.format_token_amount(amount_in_wei),
                        "min_amount_out": self.helpers.format_token_amount(min_amount_out_wei),
                        "gas_cost": cached_swap["gas_cost"],
                        "tx_hash": cached_swap["tx_hash"],
                        "wallet_address": self.helpers.shorten_address(self.wallet_address)
                    }
                )
                return cached_swap

            params = {
                "tokenIn": self.helpers.checksum(token_in),
                "tokenOut": self.helpers.checksum(token_out),
                "fee": fee_tier,
                "recipient": self.wallet_address,
                "deadline": int(time.time()) + 1800,
                "amountIn": amount_in_wei,
                "amountOutMinimum": min_amount_out_wei,
                "sqrtPriceLimitX96": 0
            }
            for attempt in range(max_retries):
                try:
                    tx = self.router_contract.functions.exactInputSingle(params).build_transaction({
                        "from": self.wallet_address,
                        "gas": gas_limit,
                        "gasPrice": self.helpers.gwei_to_wei(gas_price_gwei),
                        "nonce": self.w3.eth.get_transaction_count(self.wallet_address)
                    })
                    signed_tx = self.w3.eth.account.sign_transaction(tx, self.account.key)
                    tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
                    tx_receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                    if tx_receipt["status"] == 1:
                        swap_result = {
                            "tx_hash": tx_hash.hex(),
                            "status": "confirmed",
                            "gas_cost": gas_cost
                        }
                        self.cache.set(cache_key, swap_result)
                        logger.info(
                            "Executed Uniswap V3 swap: %s -> %s, amount=%s, min_out=%s, gas_cost=%s %s, tx_hash=%s",
                            self.helpers.shorten_address(token_in),
                            self.helpers.shorten_address(token_out),
                            self.helpers.format_token_amount(amount_in_wei),
                            self.helpers.format_token_amount(min_amount_out_wei),
                            gas_cost,
                            self.helpers.get_native_symbol(),
                            tx_hash.hex(),
                            extra={
                                "type": "trade_executed",
                                "dex": "UniswapV3",
                                "token_in": self.helpers.shorten_address(token_in),
                                "token_out": self.helpers.shorten_address(token_out),
                                "amount_in": self.helpers.format_token_amount(amount_in_wei),
                                "min_amount_out": self.helpers.format_token_amount(min_amount_out_wei),
                                "gas_cost": gas_cost,
                                "tx_hash": tx_hash.hex(),
                                "wallet_address": self.helpers.shorten_address(self.wallet_address)
                            }
                        )
                        return swap_result
                    logger.error("Swap transaction %s failed", tx_hash.hex())
                    return None
                except Exception as e:
                    if attempt == max_retries - 1:
                        logger.error("Failed to execute Uniswap swap after %d retries: %s", max_retries, e)
                        return None
                    time.sleep(1)
        except Exception as e:
            logger.error("Swap execution failed: %s", e)
            return None