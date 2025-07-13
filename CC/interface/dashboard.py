# interface/dashboard.py
"""
Real-Time Monitoring Dashboard for ChainCrawlr:
- Streamlit-based interface for bot monitoring
- Displays portfolio, open positions, and system health
- Provides manual override controls
- Integrates with JSONFileCache for caching dashboard data
"""

import time
from datetime import datetime

import pandas as pd
import streamlit as st
from web3 import Web3

from interface.signal_payloads import AlertSeverity, TradeSignal
from utils.caching import JSONFileCache
from utils.helpers import ChainHelpers
from utils.logger import logger


class ChainCrawlrDashboard:
    def __init__(self, portfolio_manager, notifier, config, sniper, cache_dir=".cache"):
        """Initialize dashboard with portfolio, notifier, sniper, and caching."""
        self.portfolio = portfolio_manager
        self.notifier = notifier
        self.config = config
        self.sniper = sniper
        self.cache = JSONFileCache(cache_dir=cache_dir, max_age=60)  # 1-minute cache for dashboard data
        self.helpers = ChainHelpers()
        self._setup_ui()

    def _setup_ui(self):
        """Initialize dashboard layout."""
        st.set_page_config(layout="wide")
        st.title("ChainCrawlr Control Panel")

        # Create tabs
        self.tabs = st.tabs([
            "📊 Portfolio",
            "🚨 Alerts",
            "⚙️ Settings",
            "🛠️ Manual Controls"
        ])

    def render(self):
        """Update dashboard content."""
        with self.tabs[0]:
            self._render_portfolio_tab()

        with self.tabs[1]:
            self._render_alerts_tab()

        with self.tabs[3]:
            self._render_manual_controls()

    def _render_portfolio_tab(self):
        """Display portfolio metrics and positions with caching."""
        cache_key = f"portfolio_metrics_{int(time.time() // 60)}"
        cached_metrics = self.cache.get(cache_key)

        if cached_metrics is not None:
            logger.debug("Retrieved portfolio metrics from cache: %s", cache_key)
            metrics = cached_metrics
        else:
            try:
                metrics = self.portfolio.get_performance_metrics()
                self.cache.set(cache_key, metrics)
                logger.debug("Cached portfolio metrics: %s", cache_key, extra={"total_value": metrics['total_value']})
            except Exception as e:
                logger.error("Failed to fetch portfolio metrics: %s", str(e))
                metrics = {
                    'total_value': 0.0,
                    'total_pnl': 0.0,
                    'win_rate': 0.0,
                    'avg_win': 0.0,
                    'avg_loss': 0.0,
                    'best_trade': {},
                    'worst_trade': {},
                    'recent_trades': []
                }

        col1, col2, col3 = st.columns(3)
        col1.metric("Total Value", f"${metrics['total_value']:,.2f}")
        col2.metric(
            "Total PnL",
            f"{metrics['total_pnl']:.2f}%",
            delta_color="inverse" if metrics['total_pnl'] < 0 else "normal"
        )
        col3.metric("Win Rate", f"{metrics['win_rate']:.1f}%")

        st.subheader("Open Positions")
        cache_key_positions = f"open_positions_{int(time.time() // 60)}"
        cached_positions = self.cache.get(cache_key_positions)

        if cached_positions is not None:
            logger.debug("Retrieved open positions from cache: %s", cache_key_positions)
            positions = pd.DataFrame(cached_positions)
        else:
            try:
                positions_data = self.portfolio.get_open_positions()
                positions = pd.DataFrame(positions_data)
                self.cache.set(cache_key_positions, positions_data)
                logger.debug(
                    "Cached open positions: %s",
                    cache_key_positions,
                    extra={"position_count": len(positions_data)}
                )
            except Exception as e:
                logger.error("Failed to fetch open positions: %s", str(e))
                positions = pd.DataFrame()

        if not positions.empty:
            st.dataframe(
                positions.sort_values('pnl', ascending=False),
                column_config={
                    "token_address": st.column_config.TextColumn("Token", width="medium"),
                    "chain": st.column_config.TextColumn("Chain"),
                    "dex": st.column_config.TextColumn("DEX"),
                    "current_price": st.column_config.NumberColumn("Price", format="$%.8f"),
                    "pnl": st.column_config.ProgressColumn(
                        "PnL %",
                        format="%.2f",
                        min_value=-100,
                        max_value=1000
                    ),
                    "entry_price": st.column_config.NumberColumn("Entry Price", format="$%.8f"),
                    "amount": st.column_config.NumberColumn("Amount", format="%.4f"),
                    "high_price": st.column_config.NumberColumn("High Price", format="$%.8f"),
                    "trailing_stop": st.column_config.NumberColumn("Trailing Stop", format="$%.8f"),
                    "entry_time": st.column_config.DatetimeColumn("Entry Time", format="YYYY-MM-DD HH:mm:ss"),
                    "duration": st.column_config.NumberColumn("Duration (s)", format="%.0f"),
                    "tx_hash": st.column_config.TextColumn("TX Hash", width="medium")
                },
                hide_index=True,
                use_container_width=True
            )
        else:
            st.info("No active positions")

    def _render_alerts_tab(self):
        """Display alert history and system events."""
        st.subheader("Recent Alerts")
        cache_key = f"recent_alerts_{int(time.time() // 60)}"
        cached_alerts = self.cache.get(cache_key)

        if cached_alerts is not None:
            logger.debug("Retrieved alerts from cache: %s", cache_key)
            alerts = cached_alerts
        else:
            try:
                # Assuming notifier has a get_exit_history method from portfolio integration
                alerts = self.portfolio.get_exit_history(limit=50)
                self.cache.set(cache_key, alerts)
                logger.debug(
                    "Cached recent alerts: %s",
                    cache_key,
                    extra={"alert_count": len(alerts)}
                )
            except Exception as e:
                logger.error("Failed to fetch alerts: %s", str(e))
                alerts = []

        if alerts:
            df = pd.DataFrame(alerts)
            df['timestamp'] = pd.to_datetime(df['exit_time'], unit='s')
            st.dataframe(
                df.sort_values('timestamp', ascending=False),
                column_config={
                    "token_address": st.column_config.TextColumn("Token", width="medium"),
                    "chain": st.column_config.TextColumn("Chain"),
                    "entry_price": st.column_config.NumberColumn("Entry Price", format="$%.8f"),
                    "exit_price": st.column_config.NumberColumn("Exit Price", format="$%.8f"),
                    "amount": st.column_config.NumberColumn("Amount", format="%.4f"),
                    "pnl": st.column_config.NumberColumn("PnL %", format="%.2f"),
                    "timestamp": st.column_config.DatetimeColumn(
                        "Time",
                        format="YYYY-MM-DD HH:mm:ss"
                    ),
                    "tx_hash": st.column_config.TextColumn("TX Hash", width="medium")
                },
                hide_index=True,
                use_container_width=True
            )
        else:
            st.warning("No alerts recorded")

    def _render_manual_controls(self):
        """UI for manual trading and system overrides."""
        st.subheader("Manual Trade Execution")

        with st.form("manual_trade"):
            chain = st.selectbox("Chain", ["ethereum", "solana"])
            token = st.text_input("Token Address")
            amount = st.number_input("Amount", min_value=0.0, step=0.01)
            action = st.radio("Action", ["BUY", "SELL"])

            if st.form_submit_button("Execute"):
                try:
                    # Validate inputs
                    if chain == "ethereum" and not Web3.is_address(token):
                        st.error("Invalid Ethereum address")
                        logger.error(
                            "Invalid Ethereum address: %s",
                            self.helpers.shorten_address(token),
                            extra={"token_address": self.helpers.shorten_address(token)}
                        )
                        return

                    # Execute trade via Sniper
                    token_info = {
                        'token_address': token,
                        'chain': chain,
                        'dex': 'uniswapv3' if chain == 'ethereum' else 'raydium'
                    }
                    if action == "BUY":
                        result = self.sniper.execute(token_info)
                    else:
                        # For SELL, simulate a position for AutoExit
                        from core.auto_exit import AutoExit
                        position = {
                            'token_address': token,
                            'chain': chain,
                            'dex': token_info['dex'],
                            'amount': amount,
                            'entry_price': self.portfolio._get_current_price(token_info),
                            'entry_time': time.time(),
                            'high_price': self.portfolio._get_current_price(token_info)
                        }
                        auto_exit = AutoExit(
                            self.config, self.sniper.wallets, self.sniper.chains, self.portfolio, cache_dir=self.cache.cache_dir
                        )
                        auto_exit._execute_exit(position, is_emergency=False)
                        result = True  # Simplified; assumes AutoExit handles notification

                    if result:
                        price = self.portfolio._get_current_price(token_info)
                        tx_hash = "0x..."  # Placeholder; actual tx_hash from sniper or auto_exit
                        st.success(f"Trade executed: {action} {amount} of {self.helpers.shorten_address(token)}")
                        logger.info(
                            "Manual trade executed: %s %s %s on %s",
                            action,
                            amount,
                            self.helpers.shorten_address(token),
                            chain,
                            extra={
                                "token_address": self.helpers.shorten_address(token),
                                "chain": chain,
                                "action": action,
                                "amount": amount
                            }
                        )

                        # Notify
                        self.notifier.notify(
                            TradeSignal(
                                token_address=token,
                                chain=chain,
                                direction=action,
                                amount=amount,
                                price=price,
                                tx_hash=tx_hash,
                                notes="Manual trade via dashboard"
                            ),
                            priority=0  # Highest priority
                        )
                    else:
                        st.error("Trade failed")
                        logger.error(
                            "Manual trade failed: %s %s on %s",
                            action,
                            self.helpers.shorten_address(token),
                            chain,
                            extra={
                                "token_address": self.helpers.shorten_address(token),
                                "chain": chain,
                                "action": action
                            }
                        )

                except Exception as e:
                    st.error(f"Trade failed: {str(e)}")
                    logger.error(
                        "Manual trade error: %s %s on %s: %s",
                        action,
                        self.helpers.shorten_address(token),
                        chain,
                        str(e),
                        extra={
                            "token_address": self.helpers.shorten_address(token),
                            "chain": chain,
                            "action": action
                        }
                    )

# Example usage:
# dashboard = ChainCrawlrDashboard(portfolio, notifier, config, sniper)
# dashboard.render()