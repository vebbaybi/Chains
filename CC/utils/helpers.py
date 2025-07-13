# utils/helpers.py
"""
Utility class for ChainCrawlr:
- Gas estimations and conversions for Ethereum/BSC, lamports for Solana
- Time formatting helpers
- Number conversions (ETH/wei, SOL/lamports, rounding)
- Chain-specific constants and address formatters
"""

import datetime
import json
import logging
import logging.handlers
import time
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from solders.pubkey import Pubkey
from web3 import Web3
from web3.exceptions import InvalidAddress


class ChainHelpers:
    """Utility class for blockchain-related operations in ChainCrawlr."""
    
    # Chain-specific default gas limits
    CHAIN_GAS_LIMITS = {
        'ethereum': 300000,
        'binancesmartchain': 250000,
        'solana': 150000
    }

    # Chain-specific native token symbols
    CHAIN_NATIVE_UNITS = {
        'ethereum': 'ETH',
        'binancesmartchain': 'BNB',
        'solana': 'SOL'
    }

    DEFAULT_GAS_PRICE_GWEI = 5
    DEFAULT_LAMPORTS = 5000

    def __init__(self, chain='ethereum', log_dir='logs'):
        """Initialize ChainHelpers with a specific chain and logging configuration."""
        self.chain = chain.lower()
        if self.chain not in self.CHAIN_NATIVE_UNITS:
            raise ValueError(f"Unsupported chain: {chain}")
        
        # Configure instance-specific logging
        self.logger = logging.getLogger(f"ChainHelpers.{chain}")
        self.logger.setLevel(logging.INFO)
        if not self.logger.handlers:  # Avoid duplicate handlers
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(logging.Formatter(
                '%(asctime)s - %(levelname)s - %(message)s'
            ))
            log_dir = Path(log_dir)
            log_dir.mkdir(exist_ok=True)
            log_file = log_dir / f"chaincrawler_{datetime.datetime.now().strftime('%Y%m%d')}.log"
            file_handler = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=10*1024*1024,
                backupCount=5
            )
            file_handler.setFormatter(logging.Formatter(
                '{"timestamp": "%(asctime)s", "level": "%(levelname)s", "chain": "' + self.chain + '", "message": "%(message)s", "extra": %(extra)s}',
                datefmt="%Y-%m-%d %H:%M:%S UTC"
            ))
            self.logger.addHandler(console_handler)
            self.logger.addHandler(file_handler)

    # === Gas/Fee Utilities === #
    def gwei_to_wei(self, gwei):
        """Convert Gwei to Wei for Ethereum/BSC."""
        try:
            gwei = float(gwei)
            if gwei < 0:
                self.logger.error("Gwei value cannot be negative: %s", gwei)
                raise ValueError("Gwei value cannot be negative")
            result = int(gwei * 1e9)
            self.logger.debug("Converted %s Gwei to %s Wei", gwei, result)
            return result
        except (TypeError, ValueError) as e:
            self.logger.error("Invalid Gwei value: %s", str(e))
            raise ValueError(f"Invalid Gwei value: {e}")

    def wei_to_gwei(self, wei):
        """Convert Wei to Gwei, rounded to 2 decimal places."""
        try:
            wei = float(wei)
            if wei < 0:
                self.logger.error("Wei value cannot be negative: %s", wei)
                raise ValueError("Wei value cannot be negative")
            result = float(Decimal(str(wei / 1e9)).quantize(Decimal('0.01'), rounding=ROUND_DOWN))
            self.logger.debug("Converted %s Wei to %s Gwei", wei, result)
            return result
        except (TypeError, ValueError) as e:
            self.logger.error("Invalid Wei value: %s", str(e))
            raise ValueError(f"Invalid Wei value: {e}")

    def estimate_gas_cost(self, gas_limit, gas_price_gwei, chain=None):
        """Estimate transaction cost in native units for Ethereum/BSC."""
        try:
            chain = chain.lower() if chain else self.chain
            if chain not in self.CHAIN_NATIVE_UNITS:
                self.logger.error("Unsupported chain: %s", chain)
                raise ValueError(f"Unsupported chain: {chain}")
            if chain == 'solana':
                self.logger.error("Use estimate_solana_fee for Solana")
                raise ValueError("Use estimate_solana_fee for Solana")
            gas_limit = int(gas_limit)
            gas_price_gwei = float(gas_price_gwei)
            if gas_limit <= 0 or gas_price_gwei <= 0:
                self.logger.error("Gas limit or price cannot be zero or negative: limit=%s, price=%s", gas_limit, gas_price_gwei)
                raise ValueError("Gas limit and price must be positive")
            cost_wei = gas_limit * self.gwei_to_wei(gas_price_gwei)
            cost_native = self.round_float(cost_wei / 1e18)
            self.logger.info("Estimated gas cost for %s: %s %s", chain, cost_native, self.get_native_symbol(chain))
            return cost_native
        except (TypeError, ValueError) as e:
            self.logger.error("Gas estimation failed: %s", str(e))
            raise ValueError(f"Gas estimation failed: {e}")

    def estimate_solana_fee(self, lamports, priority_fee_lamports=0):
        """Estimate transaction fee in SOL for Solana."""
        try:
            lamports = int(lamports)
            priority_fee_lamports = int(priority_fee_lamports)
            if lamports < 0 or priority_fee_lamports < 0:
                self.logger.error("Lamports or priority fee cannot be negative: lamports=%s, priority=%s", lamports, priority_fee_lamports)
                raise ValueError("Lamports and priority fee must be non-negative")
            total_lamports = lamports + priority_fee_lamports
            cost_sol = self.round_float(total_lamports / 1e9, decimals=9)
            self.logger.info("Estimated Solana fee: %s SOL", cost_sol)
            return cost_sol
        except (TypeError, ValueError) as e:
            self.logger.error("Solana fee estimation failed: %s", str(e))
            raise ValueError(f"Solana fee estimation failed: {e}")

    def calculate_tx_fee(self, gas_used, gas_price_wei):
        """Calculate transaction fee in native units for Ethereum/BSC, rounded to 6 decimal places."""
        try:
            gas_used = int(gas_used)
            gas_price_wei = int(gas_price_wei)
            if gas_used < 0 or gas_price_wei < 0:
                self.logger.error("Gas used or price cannot be negative: used=%s, price=%s", gas_used, gas_price_wei)
                raise ValueError("Gas used and price must be non-negative")
            fee_wei = gas_used * gas_price_wei
            fee_native = self.round_float(fee_wei / 1e18)
            self.logger.info("Calculated transaction fee: %s %s", fee_native, self.get_native_symbol(self.chain))
            return fee_native
        except (TypeError, ValueError) as e:
            self.logger.error("Transaction fee calculation failed: %s", str(e))
            raise ValueError(f"Transaction fee calculation failed: {e}")

    # === Time Helpers === #
    def now_ts(self):
        """Return current timestamp in seconds since epoch."""
        ts = int(time.time())
        self.logger.debug("Current timestamp: %s", ts)
        return ts

    def now_utc_str(self):
        """Return current UTC time as formatted string."""
        utc_time = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        self.logger.debug("Current UTC time: %s", utc_time)
        return utc_time

    def duration_fmt(self, seconds):
        """Format duration in seconds to a human-readable string."""
        try:
            seconds = float(seconds)
            if seconds < 0:
                self.logger.error("Duration cannot be negative: %s", seconds)
                raise ValueError("Duration cannot be negative")
            hours, rem = divmod(seconds, 3600)
            mins, secs = divmod(rem, 60)
            if hours > 0:
                result = f"{int(hours)}h {int(mins)}m {int(secs)}s"
            elif mins > 0:
                result = f"{int(mins)}m {int(secs)}s"
            else:
                result = f"{int(secs)}s"
            self.logger.debug("Formatted duration %s seconds to %s", seconds, result)
            return result
        except (TypeError, ValueError) as e:
            self.logger.error("Invalid duration format: %s", str(e))
            raise ValueError(f"Invalid duration: {e}")

    def time_ago(self, ts):
        """Return time elapsed since given timestamp as human-readable string."""
        try:
            ts = float(ts)
            if ts < 0:
                self.logger.error("Timestamp cannot be negative: %s", ts)
                raise ValueError("Timestamp cannot be negative")
            delta = self.now_ts() - ts
            result = self.duration_fmt(delta) + " ago"
            self.logger.debug("Time ago for timestamp %s: %s", ts, result)
            return result
        except (TypeError, ValueError) as e:
            self.logger.error("Time ago calculation failed: %s", str(e))
            raise ValueError(f"Time ago calculation failed: {e}")

    # === Number Formatting === #
    def round_float(self, value, decimals=6):
        """Round a float to specified decimals using ROUND_DOWN."""
        try:
            decimals = int(decimals)
            if decimals < 0:
                self.logger.error("Decimals cannot be negative: %s", decimals)
                raise ValueError("Decimals cannot be negative")
            result = float(Decimal(str(value)).quantize(Decimal('0.' + '0' * decimals), rounding=ROUND_DOWN))
            self.logger.debug("Rounded %s to %s with %s decimals", value, result, decimals)
            return result
        except (TypeError, ValueError) as e:
            self.logger.error("Rounding failed: %s", str(e))
            raise ValueError(f"Invalid value for rounding: {e}")

    def format_token_amount(self, amount_wei, decimals=18):
        """Convert Wei or lamports amount to token amount with specified decimals."""
        try:
            amount_wei = float(amount_wei)
            decimals = int(decimals)
            if amount_wei < 0 or decimals < 0:
                self.logger.error("Amount or decimals cannot be negative: amount=%s, decimals=%s", amount_wei, decimals)
                raise ValueError("Amount and decimals must be non-negative")
            result = self.round_float(amount_wei / (10 ** decimals), decimals)
            self.logger.debug("Formatted %s Wei/lamports to %s tokens with %s decimals", amount_wei, result, decimals)
            return result
        except (TypeError, ValueError) as e:
            self.logger.error("Token amount formatting failed: %s", str(e))
            raise ValueError(f"Token amount formatting failed: {e}")

    def to_wei(self, amount, decimals=18):
        """Convert token amount to Wei or lamports with specified decimals."""
        try:
            amount = float(amount)
            decimals = int(decimals)
            if amount < 0 or decimals < 0:
                self.logger.error("Amount or decimals cannot be negative: amount=%s, decimals=%s", amount, decimals)
                raise ValueError("Amount and decimals must be non-negative")
            result = int(Decimal(str(amount)) * Decimal(str(10 ** decimals)))
            self.logger.debug("Converted %s tokens to %s Wei/lamports with %s decimals", amount, result, decimals)
            return result
        except (TypeError, ValueError) as e:
            self.logger.error("Wei/lamports conversion failed: %s", str(e))
            raise ValueError(f"Invalid value for Wei/lamports conversion: {e}")

    # === Address Utilities === #
    def checksum(self, addr, chain=None):
        """Convert address to checksum format for Ethereum/BSC or validate Solana address."""
        try:
            chain = chain.lower() if chain else self.chain
            if chain == 'solana':
                Pubkey.from_string(addr)  # Validates Solana address
                self.logger.debug("Validated Solana address: %s", addr)
                return addr
            else:
                if not isinstance(addr, str):
                    self.logger.error("Address must be a string: %s", addr)
                    raise ValueError("Address must be a string")
                result = Web3.to_checksum_address(addr)
                self.logger.debug("Converted address to checksum: %s", result)
                return result
        except (InvalidAddress, ValueError) as e:
            self.logger.error("Invalid address for checksum: %s", str(e))
            raise ValueError(f"Invalid address: {e}")

    def shorten_address(self, addr, length=6, chain=None):
        """Shorten a blockchain address for display."""
        try:
            chain = chain.lower() if chain else self.chain
            length = int(length)
            if length < 4:
                self.logger.error("Length too short for address shortening: %s", length)
                raise ValueError("Length must be at least 4")
            addr = self.checksum(addr, chain)
            result = f"{addr[:length + 2]}...{addr[-length:]}"
            self.logger.debug("Shortened address %s to %s on %s", addr, result, chain)
            return result
        except (TypeError, ValueError) as e:
            self.logger.error("Address shortening failed: %s", str(e))
            raise ValueError(f"Address shortening failed: {e}")

    def is_valid_address(self, addr, chain=None):
        """Check if a blockchain address is valid."""
        try:
            chain = chain.lower() if chain else self.chain
            if not isinstance(addr, str):
                self.logger.debug("Invalid address type: %s", type(addr))
                return False
            if chain == 'solana':
                try:
                    Pubkey.from_string(addr)
                    self.logger.debug("Validated Solana address: %s", addr)
                    return True
                except ValueError:
                    self.logger.debug("Invalid Solana address: %s", addr)
                    return False
            else:
                try:
                    Web3.to_checksum_address(addr)
                    self.logger.debug("Validated address: %s", addr)
                    return True
                except InvalidAddress:
                    self.logger.debug("Invalid address: %s", addr)
                    return False
        except Exception as e:
            self.logger.error("Address validation failed: %s", str(e))
            return False

    def get_native_symbol(self, chain=None):
        """Get native token symbol for a given chain."""
        try:
            chain = chain.lower() if chain else self.chain
            if not isinstance(chain, str):
                self.logger.error("Chain name must be a string: %s", chain)
                raise ValueError("Chain name must be a string")
            symbol = self.CHAIN_NATIVE_UNITS.get(chain, 'NATIVE')
            self.logger.debug("Retrieved native symbol for chain %s: %s", chain, symbol)
            return symbol
        except AttributeError as e:
            self.logger.error("Invalid chain name: %s", str(e))
            raise ValueError("Chain name must be a string")