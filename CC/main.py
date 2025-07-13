# main.py
"""
ChainCrawlr Main Controller:
- Orchestrates all system components
- Manages startup/shutdown sequences
- Handles cross-component communication
- Implements emergency protocols
- Integrates with JSONFileCache for system state caching
"""

import signal
import threading
import time

from solana.rpc.api import Client as SolanaClient
from web3 import Web3

from config.config import load_config
from core.anti_rug import AntiRugChecker
from core.auto_exit import AutoExit
from core.portfolio_manager import PortfolioManager
from core.sniper import Sniper
from core.token_scanner import TokenScanner
from dex_clients.jupiter import JupiterClient
from dex_clients.raydium import RaydiumClient
from dex_clients.uniswap import UniswapV3Client
from interface.dashboard import ChainCrawlrDashboard
from interface.notifier import Notifier
from interface.signal_payloads import AlertSeverity, SystemAlert
from utils.caching import JSONFileCache
from utils.helpers import ChainHelpers
from utils.logger import logger


class ChainCrawlr:
    def __init__(self, cache_dir=".cache"):
        """Initialize ChainCrawlr with configuration and caching."""
        self.config = load_config()
        self.wallets = self._load_wallets()
        self.chains = self.config['chains']
        self.cache = JSONFileCache(cache_dir=cache_dir, max_age=86400)  # 24-hour cache for system state
        self.helpers = ChainHelpers()
        self.token_scanner = None
        self.sniper = None
        self.portfolio = None
        self.auto_exit = None
        self.notifier = None
        self.dashboard = None
        self.running = False
        self.emergency_stop = False
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    def _load_wallets(self):
        """Load and validate wallet configurations."""
        try:
            wallets = self.config['wallets']
            assert 'primary' in wallets, "Missing primary wallet config"
            assert 'fallback' in wallets, "Missing fallback wallet config"
            if self.config['chains'].get('ethereum') and not Web3.is_address(wallets['primary']['address']):
                raise ValueError("Invalid primary Ethereum wallet address")
            logger.debug("Wallets loaded successfully", extra={"wallet_count": len(wallets)})
            return wallets
        except Exception as e:
            logger.error("Failed to load wallets: %s", str(e))
            raise

    def initialize_components(self):
        """Initialize all system components in proper order."""
        cache_key = "system_state_initialized"
        cached_state = self.cache.get(cache_key)
        if cached_state is not None and cached_state.get("status") == "success":
            logger.debug("Components loaded from cache: %s", cache_key)
            return True

        try:
            logger.info("Initializing ChainCrawlr components...")

            # 1. Initialize notification system first (for system alerts)
            self.notifier = Notifier(self.config, cache_dir=self.cache.cache_dir)
            
            # 2. Setup DEX clients
            uniswap = UniswapV3Client(cache_dir=self.cache.cache_dir)
            raydium = RaydiumClient(cache_dir=self.cache.cache_dir)
            jupiter = JupiterClient(cache_dir=self.cache.cache_dir)
            
            # 3. Core security components
            anti_rug = AntiRugChecker(self.config, cache_dir=self.cache.cache_dir)
            
            # 4. Portfolio management
            self.portfolio = PortfolioManager(
                settings=self.config,
                wallets=self.wallets,
                chains=self.chains,
                cache_dir=self.cache.cache_dir
            )
            
            # 5. Trading components
            self.sniper = Sniper(
                settings=self.config,
                wallets=self.wallets,
                chains=self.chains,
                cache_dir=self.cache.cache_dir
            )
            
            self.token_scanner = TokenScanner(
                chains=self.chains,
                settings=self.config,
                notifier=self.notifier,
                cache_dir=self.cache.cache_dir
            )
            
            self.auto_exit = AutoExit(
                settings=self.config,
                wallets=self.wallets,
                chains=self.chains,
                portfolio_manager=self.portfolio,
                cache_dir=self.cache.cache_dir
            )
            
            # 6. User interface
            if self.config.get('interface', {}).get('dashboard', {}).get('enabled', False):
                self.dashboard = ChainCrawlrDashboard(
                    portfolio=self.portfolio,
                    notifier=self.notifier,
                    config=self.config,
                    sniper=self.sniper,
                    cache_dir=self.cache.cache_dir
                )
            
            self.cache.set(cache_key, {"status": "success", "timestamp": time.time()})
            logger.success("All components initialized successfully")
            self.notifier.notify(
                SystemAlert(
                    component="main",
                    alert_type="INIT_SUCCESS",
                    severity=AlertSeverity.SUCCESS,
                    message="ChainCrawlr components initialized successfully"
                )
            )
            return True
            
        except Exception as e:
            logger.critical("Initialization failed: %s", str(e), exc_info=True)
            self.notifier.notify(
                SystemAlert(
                    component="main",
                    alert_type="INIT_FAILURE",
                    severity=AlertSeverity.CRITICAL,
                    message=f"System initialization failed: {str(e)}"
                ),
                priority=0
            )
            self.cache.set(cache_key, {"status": "failed", "error": str(e)})
            return False

    def start(self):
        """Start all system services."""
        if not self.initialize_components():
            logger.error("Startup aborted due to initialization failure")
            return False

        self.running = True
        logger.info("Starting ChainCrawlr services...")
        
        try:
            threads = []
            # Token scanner thread
            scanner_thread = threading.Thread(
                target=self._run_token_scanner,
                daemon=True
            )
            threads.append(scanner_thread)
            
            # Auto-exit thread
            exit_thread = threading.Thread(
                target=self._run_auto_exit,
                daemon=True
            )
            threads.append(exit_thread)
            
            # Dashboard thread
            if self.dashboard:
                dashboard_thread = threading.Thread(
                    target=self._run_dashboard,
                    daemon=True
                )
                threads.append(dashboard_thread)
            
            # Start all threads
            for t in threads:
                t.start()
            
            logger.success("All services started")
            self.notifier.notify(
                SystemAlert(
                    component="main",
                    alert_type="SYSTEM_START",
                    severity=AlertSeverity.INFO,
                    message="ChainCrawlr started successfully"
                )
            )
            
            # Main control loop
            self._main_loop()
            return True
            
        except Exception as e:
            logger.critical("Startup failed: %s", str(e))
            self._emergency_shutdown()
            return False

    def _run_token_scanner(self):
        """Run token scanner in dedicated thread."""
        while self.running and not self.emergency_stop:
            try:
                new_tokens = self.token_scanner.scan()
                for token in new_tokens:
                    if self.sniper.execute(token):
                        logger.info(
                            "Successfully sniped token: %s on %s",
                            self.helpers.shorten_address(token['token_address']),
                            token['chain'],
                            extra={
                                "token_address": self.helpers.shorten_address(token['token_address']),
                                "chain": token['chain']
                            }
                        )
                        self.portfolio.open_position(token, tx_hash="0x...")  # Placeholder tx_hash
            except Exception as e:
                logger.error("Token scanner error: %s", str(e))
                self.notifier.notify(
                    SystemAlert(
                        component="token_scanner",
                        alert_type="SCANNER_ERROR",
                        severity=AlertSeverity.WARNING,
                        message=f"Token scanner failed: {str(e)}"
                    )
                )
                time.sleep(5)

    def _run_auto_exit(self):
        """Run auto-exit system in dedicated thread."""
        while self.running and not self.emergency_stop:
            try:
                self.auto_exit.monitor_positions()
            except Exception as e:
                logger.error("Auto-exit error: %s", str(e))
                self.notifier.notify(
                    SystemAlert(
                        component="auto_exit",
                        alert_type="EXIT_ERROR",
                        severity=AlertSeverity.WARNING,
                        message=f"Auto-exit failed: {str(e)}"
                    )
                )
                time.sleep(10)

    def _run_dashboard(self):
        """Run dashboard interface."""
        while self.running and not self.emergency_stop:
            try:
                self.dashboard.render()
                time.sleep(1)
            except Exception as e:
                logger.error("Dashboard error: %s", str(e))
                self.notifier.notify(
                    SystemAlert(
                        component="dashboard",
                        alert_type="DASHBOARD_ERROR",
                        severity=AlertSeverity.WARNING,
                        message=f"Dashboard render failed: {str(e)}"
                    )
                )
                time.sleep(5)

    def _main_loop(self):
        """Primary system control loop."""
        try:
            while self.running and not self.emergency_stop:
                self._check_system_health()
                self.portfolio.update_positions()
                time.sleep(self.config.get('main_loop_interval', 5))
                
        except KeyboardInterrupt:
            logger.info("Shutting down via keyboard interrupt")
            self.shutdown()
        except Exception as e:
            logger.critical("Main loop failure: %s", str(e))
            self._emergency_shutdown()

    def _check_system_health(self):
        """Perform system health checks."""
        try:
            self._check_balances()
            self._check_rpc_connections()
            self._check_component_status()
        except Exception as e:
            logger.error("System health check failed: %s", str(e))
            self.notifier.notify(
                SystemAlert(
                    component="health_check",
                    alert_type="HEALTH_CHECK_ERROR",
                    severity=AlertSeverity.WARNING,
                    message=f"System health check failed: {str(e)}"
                )
            )

    def _check_balances(self):
        """Verify wallet balances meet minimums."""
        cache_key = f"balance_check_{int(time.time() // 300)}"
        cached_result = self.cache.get(cache_key)
        if cached_result is not None and cached_result.get("status") == "success":
            logger.debug("Balance check skipped (cached): %s", cache_key)
            return

        try:
            min_balance = float(self.config['wallets']['primary'].get('min_balance', 0.01))
            current_balance = self.portfolio.get_portfolio_value()
            
            if current_balance < min_balance:
                self.notifier.notify(
                    SystemAlert(
                        component="wallet",
                        alert_type="LOW_BALANCE",
                        severity=AlertSeverity.WARNING,
                        message=f"Primary wallet balance low: {current_balance:.4f} {self.config['trading']['base_currency']}"
                    )
                )
                self.cache.set(cache_key, {"status": "warning", "balance": current_balance})
            else:
                self.cache.set(cache_key, {"status": "success", "balance": current_balance})
                logger.debug(
                    "Balance check passed: %s %s",
                    current_balance,
                    self.config['trading']['base_currency'],
                    extra={"balance": current_balance}
                )
                
        except Exception as e:
            logger.error("Balance check failed: %s", str(e))
            self.cache.set(cache_key, {"status": "failed", "error": str(e)})

    def _check_rpc_connections(self):
        """Verify all RPC connections are healthy."""
        cache_key = f"rpc_check_{int(time.time() // 300)}"
        cached_result = self.cache.get(cache_key)
        if cached_result is not None and cached_result.get("status") == "success":
            logger.debug("RPC check skipped (cached): %s", cache_key)
            return

        healthy = True
        try:
            if 'ethereum' in self.chains:
                w3 = Web3(Web3.HTTPProvider(self.chains['ethereum']['rpc']))
                if not w3.is_connected():
                    healthy = False
                    logger.warning("Ethereum RPC connection failed")
                    self.notifier.notify(
                        SystemAlert(
                            component="network",
                            alert_type="RPC_ISSUE",
                            severity=AlertSeverity.WARNING,
                            message="Ethereum RPC connection failed"
                        )
                    )

            if 'solana' in self.chains:
                client = SolanaClient(self.chains['solana']['rpc'])
                if not client.is_connected():
                    healthy = False
                    logger.warning("Solana RPC connection failed")
                    self.notifier.notify(
                        SystemAlert(
                            component="network",
                            alert_type="RPC_ISSUE",
                            severity=AlertSeverity.WARNING,
                            message="Solana RPC connection failed"
                        )
                    )

            self.cache.set(cache_key, {"status": "success" if healthy else "warning"})
            if healthy:
                logger.debug("RPC connections healthy", extra={"chains": list(self.chains.keys())})
                
        except Exception as e:
            logger.error("RPC check failed: %s", str(e))
            self.cache.set(cache_key, {"status": "failed", "error": str(e)})

    def _check_component_status(self):
        """Check status of all components."""
        cache_key = f"component_check_{int(time.time() // 300)}"
        cached_result = self.cache.get(cache_key)
        if cached_result is not None and cached_result.get("status") == "success":
            logger.debug("Component check skipped (cached): %s", cache_key)
            return

        try:
            components = {
                "token_scanner": self.token_scanner is not None,
                "sniper": self.sniper is not None,
                "portfolio": self.portfolio is not None,
                "auto_exit": self.auto_exit is not None,
                "notifier": self.notifier is not None,
                "dashboard": self.dashboard is not None if self.config.get('interface', {}).get('dashboard', {}).get('enabled') else True
            }
            failed_components = [comp for comp, status in components.items() if not status]
            
            if failed_components:
                self.notifier.notify(
                    SystemAlert(
                        component="main",
                        alert_type="COMPONENT_FAILURE",
                        severity=AlertSeverity.CRITICAL,
                        message=f"Components failed: {', '.join(failed_components)}"
                    ),
                    priority=0
                )
                self.cache.set(cache_key, {"status": "failed", "failed_components": failed_components})
                logger.error("Component check failed: %s", failed_components)
            else:
                self.cache.set(cache_key, {"status": "success"})
                logger.debug("All components healthy", extra={"components": list(components.keys())})

        except Exception as e:
            logger.error("Component check failed: %s", str(e))
            self.cache.set(cache_key, {"status": "failed", "error": str(e)})

    def _handle_shutdown(self, signum, frame):
        """Handle shutdown signals."""
        logger.info("Received shutdown signal: %s", signum, extra={"signal": signum})
        self.shutdown()

    def shutdown(self):
        """Graceful system shutdown."""
        logger.info("Initiating graceful shutdown...")
        self.running = False
        
        self.notifier.notify(
            SystemAlert(
                component="main",
                alert_type="SYSTEM_SHUTDOWN",
                severity=AlertSeverity.INFO,
                message="ChainCrawlr shutting down gracefully"
            )
        )
        
        time.sleep(2)  # Allow final messages to process
        logger.success("Shutdown complete")
        self.cache.set("system_state_shutdown", {"status": "success", "timestamp": time.time()})

    def _emergency_shutdown(self):
        """Immediate shutdown procedure."""
        logger.critical("EMERGENCY SHUTDOWN INITIATED!")
        self.emergency_stop = True
        self.running = False
        
        self.notifier.notify(
            SystemAlert(
                component="main",
                alert_type="EMERGENCY_STOP",
                severity=AlertSeverity.CRITICAL,
                message="ChainCrawlr emergency shutdown activated"
            ),
            priority=0
        )
        
        if self.config.get('safety', {}).get('emergency_liquidate', False):
            logger.warning("Liquidating all positions!")
            try:
                self.portfolio.liquidate_all()
            except Exception as e:
                logger.error("Failed to liquidate positions: %s", str(e))
        
        self.cache.set("system_state_emergency_shutdown", {"status": "executed", "timestamp": time.time()})
        exit(1)


if __name__ == "__main__":
    bot = ChainCrawlr()
    if bot.start():
        while bot.running:
            time.sleep(1)