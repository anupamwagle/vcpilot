from __future__ import annotations
from abc import ABC, abstractmethod

class BaseNotifier(ABC):
    """Abstract Base Class defining the unified interface for all AstraTrade notification channels."""

    @abstractmethod
    def __init__(self, organization_id: int | None = None):
        pass

    @abstractmethod
    def send(self, message: str, chat_id: str | None = None) -> bool:
        """Send a generic text message."""
        pass

    @abstractmethod
    def send_signal_alert(self, signal_data: dict) -> bool:
        """Send a breakout buy signal alert."""
        pass

    @abstractmethod
    def send_order_fill(self, ticker: str, action: str, qty: int,
                        price: float, is_paper: bool) -> bool:
        """Send an order execution fill alert."""
        pass

    @abstractmethod
    def send_exit_alert(self, ticker: str, exit_reason: str,
                        pnl_pct: float, pnl_aud: float, is_paper: bool) -> bool:
        """Send a trade exit/close alert."""
        pass

    @abstractmethod
    def send_regime_change(self, old_regime: str, new_regime: str) -> bool:
        """Send a market regime status change alert."""
        pass

    @abstractmethod
    def send_daily_report(self, report: dict) -> bool:
        """Send the daily summary report."""
        pass

    @abstractmethod
    def send_health_alert(self, component: str, error: str) -> bool:
        """Send a system health/heartbeat alert."""
        pass
