# core/anti_rug.py
"""
Anti-Rug Protection Module for ChainCrawlr:
- Validates token contract before allowing snipe
- Enforces safety rules from settings.yaml
- Supports EVM chains with enhanced checks for honeypot, liquidity, and holder distribution
- Integrates with ChainHelpers for address handling and formatting
- Uses ChainCrawlerLogger for structured logging
- Integrates with JSONFileCache for caching check results and API responses
"""

import json
import time
from pathlib import Path

import requests
from web3 import Web3

from utils.caching import JSONFileCache
from utils.helpers import ChainHelpers
from utils.logger import logger


class AntiRugChecker:
    def __init__(self, web3: Web3, token_address: str, chain_name: str, settings: dict, cache_dir=".cache"):
        """Initialize AntiRugChecker for token validation on EVM chains with caching."""
        self.w3 = web3
        self.chain = chain_name.lower()
        self.helpers = ChainHelpers(chain=self.chain)
        self.cache = JSONFileCache(cache_dir=cache_dir, max_age=300)  # 5-minute cache for volatile data
        try:
            self.token_address = self.helpers.checksum(token_address)
        except ValueError as e:
            logger.error("Invalid token address: %s", e)
            raise ValueError(f"Invalid token address: {e}")
        self.settings = settings.get("trading", {}).get("anti_rug", {})
        if not self.settings:
            logger.warning("No anti-rug settings found, using defaults")
            self.settings = {
                "check_contract_verification": True,
                "check_honeypot": True,
                "check_renounced": True,
                "check_dev_holding": True,
                "check_holder_count": True,
                "check_liquidity_lock": True,
                "max_dev_ownership": 0.1,
                "min_holder_count": 50,
                "min_locked_liquidity_percentage": 0.7
            }

    def check_contract_verification(self, max_retries=3):
        """Check if the token contract has deployed code with caching."""
        cache_key = f"contract_verification_{self.token_address}"
        cached_result = self.cache.get(cache_key)
        if cached_result is not None:
            logger.debug("Retrieved contract verification from cache: %s", cache_key)
            if not cached_result:
                logger.warning(
                    "Cached: Token %s has no contract code",
                    self.helpers.shorten_address(self.token_address),
                    extra={"token_address": self.helpers.shorten_address(self.token_address)}
                )
            return cached_result

        for attempt in range(max_retries):
            try:
                code = self.w3.eth.get_code(self.token_address)
                is_verified = code not in (b'', '0x')
                self.cache.set(cache_key, is_verified)
                if not is_verified:
                    logger.warning(
                        "Token %s has no contract code",
                        self.helpers.shorten_address(self.token_address),
                        extra={"token_address": self.helpers.shorten_address(self.token_address)}
                    )
                    return False
                logger.debug(
                    "Contract code verified for %s",
                    self.helpers.shorten_address(self.token_address),
                    extra={"token_address": self.helpers.shorten_address(self.token_address)}
                )
                return True
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error(
                        "Contract verification check failed for %s after %d retries: %s",
                        self.helpers.shorten_address(self.token_address),
                        max_retries,
                        e,
                        extra={"token_address": self.helpers.shorten_address(self.token_address)}
                    )
                    self.cache.set(cache_key, False)
                    return False
                time.sleep(1)
        return False

    def check_honeypot(self, max_retries=3):
        """Check if the token is a honeypot using an external API with caching."""
        api_key = self.settings.get("honeypot_api_key")
        if not api_key:
            logger.warning(
                "Honeypot check skipped: No API key provided for %s",
                self.helpers.shorten_address(self.token_address),
                extra={"token_address": self.helpers.shorten_address(self.token_address)}
            )
            return True  # Skip if no API key
        cache_key = f"honeypot_{self.token_address}_{self.helpers.get_chain_id()}"
        cached_result = self.cache.get(cache_key)
        if cached_result is not None:
            logger.debug("Retrieved honeypot check from cache: %s", cache_key)
            if cached_result.get("is_honeypot", True):
                logger.warning(
                    "Cached: Token %s flagged as potential honeypot: %s",
                    self.helpers.shorten_address(self.token_address),
                    cached_result.get("details", "No details provided"),
                    extra={"token_address": self.helpers.shorten_address(self.token_address)}
                )
            return not cached_result.get("is_honeypot", True)

        url = f"https://api.honeypot.is/v2/IsHoneypot?address={self.token_address}&chainID={self.helpers.get_chain_id()}"
        for attempt in range(max_retries):
            try:
                headers = {"X-API-KEY": api_key}
                response = requests.get(url, headers=headers, timeout=5)
                response.raise_for_status()
                result = response.json()
                is_honeypot = result.get("isHoneypot", True)
                self.cache.set(cache_key, {"is_honeypot": is_honeypot, "details": result.get("details", "No details provided")})
                if is_honeypot:
                    logger.warning(
                        "Token %s flagged as potential honeypot: %s",
                        self.helpers.shorten_address(self.token_address),
                        result.get("details", "No details provided"),
                        extra={"token_address": self.helpers.shorten_address(self.token_address)}
                    )
                    return False
                logger.debug(
                    "Honeypot check passed for %s",
                    self.helpers.shorten_address(self.token_address),
                    extra={"token_address": self.helpers.shorten_address(self.token_address)}
                )
                return True
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.warning(
                        "Honeypot check failed for %s after %d retries: %s",
                        self.helpers.shorten_address(self.token_address),
                        max_retries,
                        e,
                        extra={"token_address": self.helpers.shorten_address(self.token_address)}
                    )
                    self.cache.set(cache_key, {"is_honeypot": True, "details": str(e)})
                    return False
                time.sleep(1)
        return False

    def check_renounced(self, max_retries=3):
        """Check if the token contract's ownership is renounced with caching."""
        cache_key = f"renounced_{self.token_address}"
        cached_result = self.cache.get(cache_key)
        if cached_result is not None:
            logger.debug("Retrieved ownership renounced check from cache: %s", cache_key)
            if not cached_result:
                logger.warning(
                    "Cached: Owner not renounced for %s: %s",
                    self.helpers.shorten_address(self.token_address),
                    cached_result.get("owner", "Unknown"),
                    extra={
                        "token_address": self.helpers.shorten_address(self.token_address),
                        "owner_address": cached_result.get("owner", "Unknown")
                    }
                )
            return cached_result.get("is_renounced", False)

        abi = [
            {"constant": True, "inputs": [], "name": "owner", "outputs": [{"name": "", "type": "address"}],
             "payable": False, "stateMutability": "view", "type": "function"}
        ]
        for attempt in range(max_retries):
            try:
                contract = self.w3.eth.contract(address=self.token_address, abi=abi)
                owner = contract.functions.owner().call()
                null_addresses = ["0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"]
                is_renounced = owner.lower() in null_addresses
                self.cache.set(cache_key, {"is_renounced": is_renounced, "owner": self.helpers.shorten_address(owner)})
                if is_renounced:
                    logger.debug(
                        "Ownership renounced for %s",
                        self.helpers.shorten_address(self.token_address),
                        extra={"token_address": self.helpers.shorten_address(self.token_address)}
                    )
                    return True
                logger.warning(
                    "Owner not renounced for %s: %s",
                    self.helpers.shorten_address(self.token_address),
                    self.helpers.shorten_address(owner),
                    extra={
                        "token_address": self.helpers.shorten_address(self.token_address),
                        "owner_address": self.helpers.shorten_address(owner)
                    }
                )
                return False
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.warning(
                        "Could not confirm owner status for %s after %d retries: %s",
                        self.helpers.shorten_address(self.token_address),
                        max_retries,
                        e,
                        extra={"token_address": self.helpers.shorten_address(self.token_address)}
                    )
                    self.cache.set(cache_key, {"is_renounced": False, "owner": "Unknown"})
                    return False
                time.sleep(1)
        return False

    def check_dev_holding(self, max_retries=3):
        """Check if the developer's token holding exceeds the allowed threshold with caching."""
        cache_key = f"dev_holding_{self.token_address}"
        cached_result = self.cache.get(cache_key)
        if cached_result is not None:
            logger.debug("Retrieved dev holding check from cache: %s", cache_key)
            if not cached_result.get("is_valid", False):
                logger.warning(
                    "Cached: Dev wallet holds %.2f%% of supply for %s, exceeding limit of %.2f%%",
                    cached_result.get("holding_percentage", 0),
                    self.helpers.shorten_address(self.token_address),
                    cached_result.get("max_dev_ownership", 0.1) * 100,
                    extra={
                        "token_address": self.helpers.shorten_address(self.token_address),
                        "dev_address": cached_result.get("dev_address", "Unknown"),
                        "holding_percentage": cached_result.get("holding_percentage", 0)
                    }
                )
            return cached_result.get("is_valid", False)

        abi = [
            {"constant": True, "inputs": [], "name": "totalSupply", "outputs": [{"name": "", "type": "uint256"}],
             "payable": False, "stateMutability": "view", "type": "function"},
            {"constant": True, "inputs": [{"name": "account", "type": "address"}], "name": "balanceOf",
             "outputs": [{"name": "", "type": "uint256"}], "payable": False, "stateMutability": "view", "type": "function"},
            {"constant": True, "inputs": [], "name": "owner", "outputs": [{"name": "", "type": "address"}],
             "payable": False, "stateMutability": "view", "type": "function"}
        ]
        for attempt in range(max_retries):
            try:
                contract = self.w3.eth.contract(address=self.token_address, abi=abi)
                total_supply = contract.functions.totalSupply().call()
                owner = contract.functions.owner().call()
                balance = contract.functions.balanceOf(owner).call()
                if total_supply == 0:
                    logger.warning(
                        "Total supply is zero for %s",
                        self.helpers.shorten_address(self.token_address),
                        extra={"token_address": self.helpers.shorten_address(self.token_address)}
                    )
                    self.cache.set(cache_key, {"is_valid": False, "holding_percentage": 0, "dev_address": self.helpers.shorten_address(owner)})
                    return False
                ratio = balance / total_supply
                max_dev_ownership = self.settings.get("max_dev_ownership", 0.1)
                is_valid = ratio <= max_dev_ownership
                self.cache.set(cache_key, {
                    "is_valid": is_valid,
                    "holding_percentage": ratio * 100,
                    "dev_address": self.helpers.shorten_address(owner),
                    "max_dev_ownership": max_dev_ownership
                })
                if not is_valid:
                    logger.warning(
                        "Dev wallet holds %.2f%% of supply for %s, exceeding limit of %.2f%%",
                        ratio * 100,
                        self.helpers.shorten_address(self.token_address),
                        max_dev_ownership * 100,
                        extra={
                            "token_address": self.helpers.shorten_address(self.token_address),
                            "dev_address": self.helpers.shorten_address(owner),
                            "holding_percentage": ratio * 100
                        }
                    )
                    return False
                logger.debug(
                    "Dev holding check passed for %s: %.2f%%",
                    self.helpers.shorten_address(self.token_address),
                    ratio * 100,
                    extra={
                        "token_address": self.helpers.shorten_address(self.token_address),
                        "dev_address": self.helpers.shorten_address(owner),
                        "holding_percentage": ratio * 100
                    }
                )
                return True
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.warning(
                        "Could not evaluate dev holding for %s after %d retries: %s",
                        self.helpers.shorten_address(self.token_address),
                        max_retries,
                        e,
                        extra={"token_address": self.helpers.shorten_address(self.token_address)}
                    )
                    self.cache.set(cache_key, {"is_valid": False, "holding_percentage": 0, "dev_address": "Unknown"})
                    return False
                time.sleep(1)
        return False

    def check_holder_count(self, max_retries=3):
        """Check if the token has sufficient holders using an external API with caching."""
        api_key = self.settings.get("explorer_api_key")
        if not api_key:
            logger.warning(
                "Holder count check skipped: No API key provided for %s",
                self.helpers.shorten_address(self.token_address),
                extra={"token_address": self.helpers.shorten_address(self.token_address)}
            )
            return True  # Skip if no API key
        cache_key = f"holder_count_{self.token_address}"
        cached_result = self.cache.get(cache_key)
        if cached_result is not None:
            logger.debug("Retrieved holder count from cache: %s", cache_key)
            if not cached_result.get("is_valid", False):
                logger.warning(
                    "Cached: Holder count too low for %s: %d (required: %d)",
                    self.helpers.shorten_address(self.token_address),
                    cached_result.get("holder_count", 0),
                    cached_result.get("min_holders", 50),
                    extra={
                        "token_address": self.helpers.shorten_address(self.token_address),
                        "holder_count": cached_result.get("holder_count", 0)
                    }
                )
            return cached_result.get("is_valid", False)

        chain_id = self.helpers.get_chain_id()
        explorer_url = {
            "ethereum": f"https://api.etherscan.io/api?module=token&action=tokenholderlist&contractaddress={self.token_address}&apikey={api_key}",
            "bsc": f"https://api.bscscan.com/api?module=token&action=tokenholderlist&contractaddress={self.token_address}&apikey={api_key}"
        }.get(self.chain)
        if not explorer_url:
            logger.warning(
                "Holder count check skipped: Unsupported chain %s for %s",
                self.chain,
                self.helpers.shorten_address(self.token_address),
                extra={"token_address": self.helpers.shorten_address(self.token_address)}
            )
            return True
        min_holders = self.settings.get("min_holder_count", 50)
        for attempt in range(max_retries):
            try:
                response = requests.get(explorer_url, timeout=5)
                response.raise_for_status()
                data = response.json()
                if data.get("status") != "1" or not data.get("result"):
                    logger.warning(
                        "Invalid holder count response for %s: %s",
                        self.helpers.shorten_address(self.token_address),
                        data.get("message", "Unknown error"),
                        extra={"token_address": self.helpers.shorten_address(self.token_address)}
                    )
                    self.cache.set(cache_key, {"is_valid": False, "holder_count": 0, "min_holders": min_holders})
                    return False
                holder_count = len(data["result"])
                is_valid = holder_count >= min_holders
                self.cache.set(cache_key, {"is_valid": is_valid, "holder_count": holder_count, "min_holders": min_holders})
                if not is_valid:
                    logger.warning(
                        "Holder count too low for %s: %d (required: %d)",
                        self.helpers.shorten_address(self.token_address),
                        holder_count,
                        min_holders,
                        extra={
                            "token_address": self.helpers.shorten_address(self.token_address),
                            "holder_count": holder_count
                        }
                    )
                    return False
                logger.debug(
                    "Holder count check passed for %s: %d holders",
                    self.helpers.shorten_address(self.token_address),
                    holder_count,
                    extra={
                        "token_address": self.helpers.shorten_address(self.token_address),
                        "holder_count": holder_count
                    }
                )
                return True
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.warning(
                        "Failed to check holder count for %s after %d retries: %s",
                        self.helpers.shorten_address(self.token_address),
                        max_retries,
                        e,
                        extra={"token_address": self.helpers.shorten_address(self.token_address)}
                    )
                    self.cache.set(cache_key, {"is_valid": False, "holder_count": 0, "min_holders": min_holders})
                    return False
                time.sleep(1)
        return False

    def check_liquidity_lock(self, max_retries=3):
        """Check if sufficient liquidity is locked for the token with caching."""
        lp_token_address = self.settings.get("lp_token_address")
        if not lp_token_address or not self.helpers.is_valid_address(lp_token_address):
            logger.warning(
                "Liquidity lock check skipped: Invalid or missing LP token address for %s",
                self.helpers.shorten_address(self.token_address),
                extra={"token_address": self.helpers.shorten_address(self.token_address)}
            )
            return True  # Skip if no LP token address
        cache_key = f"liquidity_lock_{self.token_address}_{lp_token_address}"
        cached_result = self.cache.get(cache_key)
        if cached_result is not None:
            logger.debug("Retrieved liquidity lock check from cache: %s", cache_key)
            if not cached_result.get("is_valid", False):
                logger.warning(
                    "Cached: Liquidity lock too low for %s: %.2f%% (required: %.2f%%)",
                    self.helpers.shorten_address(self.token_address),
                    cached_result.get("locked_percentage", 0),
                    cached_result.get("min_locked", 0.7) * 100,
                    extra={
                        "token_address": self.helpers.shorten_address(self.token_address),
                        "locked_percentage": cached_result.get("locked_percentage", 0)
                    }
                )
            return cached_result.get("is_valid", False)

        min_locked = self.settings.get("min_locked_liquidity_percentage", 0.7)
        abi = [
            {"constant": True, "inputs": [], "name": "totalSupply", "outputs": [{"name": "", "type": "uint256"}],
             "payable": False, "stateMutability": "view", "type": "function"},
            {"constant": True, "inputs": [{"name": "account", "type": "address"}], "name": "balanceOf",
             "outputs": [{"name": "", "type": "uint256"}], "payable": False, "stateMutability": "view", "type": "function"}
        ]
        null_addresses = ["0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"]
        for attempt in range(max_retries):
            try:
                contract = self.w3.eth.contract(address=self.helpers.checksum(lp_token_address), abi=abi)
                total_supply = contract.functions.totalSupply().call()
                if total_supply == 0:
                    logger.warning(
                        "LP token total supply is zero for %s",
                        self.helpers.shorten_address(self.token_address),
                        extra={"token_address": self.helpers.shorten_address(self.token_address)}
                    )
                    self.cache.set(cache_key, {"is_valid": False, "locked_percentage": 0, "min_locked": min_locked})
                    return False
                locked_balance = sum(
                    contract.functions.balanceOf(self.helpers.checksum(addr)).call()
                    for addr in null_addresses
                )
                locked_ratio = locked_balance / total_supply
                is_valid = locked_ratio >= min_locked
                self.cache.set(cache_key, {"is_valid": is_valid, "locked_percentage": locked_ratio * 100, "min_locked": min_locked})
                if not is_valid:
                    logger.warning(
                        "Liquidity lock too low for %s: %.2f%% (required: %.2f%%)",
                        self.helpers.shorten_address(self.token_address),
                        locked_ratio * 100,
                        min_locked * 100,
                        extra={
                            "token_address": self.helpers.shorten_address(self.token_address),
                            "locked_percentage": locked_ratio * 100
                        }
                    )
                    return False
                logger.debug(
                    "Liquidity lock check passed for %s: %.2f%% locked",
                    self.helpers.shorten_address(self.token_address),
                    locked_ratio * 100,
                    extra={
                        "token_address": self.helpers.shorten_address(self.token_address),
                        "locked_percentage": locked_ratio * 100
                    }
                )
                return True
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.warning(
                        "Liquidity lock check failed for %s after %d retries: %s",
                        self.helpers.shorten_address(self.token_address),
                        max_retries,
                        e,
                        extra={"token_address": self.helpers.shorten_address(self.token_address)}
                    )
                    self.cache.set(cache_key, {"is_valid": False, "locked_percentage": 0, "min_locked": min_locked})
                    return False
                time.sleep(1)
        return False

    def run_all_checks(self):
        """Run all anti-rug checks and return True if all pass."""
        checks = [
            ("check_contract_verification", self.check_contract_verification),
            ("check_honeypot", self.check_honeypot),
            ("check_renounced", self.check_renounced),
            ("check_dev_holding", self.check_dev_holding),
            ("check_holder_count", self.check_holder_count),
            ("check_liquidity_lock", self.check_liquidity_lock)
        ]
        for check_name, check_func in checks:
            if self.settings.get(check_name, True):
                if not check_func():
                    logger.error(
                        "Anti-rug check %s failed for %s",
                        check_name,
                        self.helpers.shorten_address(self.token_address),
                        extra={"token_address": self.helpers.shorten_address(self.token_address)}
                    )
                    return False
        logger.info(
            "All anti-rug checks passed for %s",
            self.helpers.shorten_address(self.token_address),
            extra={"token_address": self.helpers.shorten_address(self.token_address)}
        )
        return True