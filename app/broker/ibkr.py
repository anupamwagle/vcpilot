"""
IBKR Broker — ib_insync wrapper for order management and account data.
Always check IBKR_PAPER_MODE before submitting live orders.
"""
from __future__ import annotations
import time
from datetime import date
from typing import Optional
from loguru import logger

try:
    from ib_insync import IB, Stock, Order, LimitOrder, MarketOrder, StopOrder, BracketOrder
    IB_AVAILABLE = True
except ImportError:
    IB_AVAILABLE = False
    logger.warning("ib_insync not installed — broker in simulation mode")

from app.config import settings
from app.models.trade import OrderAction, OrderType, OrderStatus


class IBKRBroker:
    """
    Manages the IBKR Gateway connection and order lifecycle.
    Use as a context manager or call connect()/disconnect() explicitly.
    """

    def __init__(self):
        self._ib: Optional[object] = None
        self._connected = False

    def connect(self) -> bool:
        if not IB_AVAILABLE:
            logger.info("ib_insync unavailable — running in simulation mode")
            return False
        try:
            self._ib = IB()
            self._ib.connect(
                host=settings.ibkr_host,
                port=settings.ibkr_port,
                clientId=settings.ibkr_client_id,
                timeout=20,
                readonly=False,
            )
            self._connected = True
            logger.info(
                f"IBKR connected: host={settings.ibkr_host} port={settings.ibkr_port} "
                f"paper={settings.ibkr_paper_mode}"
            )
            return True
        except Exception as e:
            logger.error(f"IBKR connection failed: {e}")
            return False

    def disconnect(self):
        if self._ib and self._connected:
            try:
                self._ib.disconnect()
                logger.info("IBKR disconnected")
            except Exception as e:
                logger.warning(f"IBKR disconnect error: {e}")
        self._connected = False

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()

    @property
    def is_connected(self) -> bool:
        return self._connected and IB_AVAILABLE

    def get_account_summary(self) -> dict:
        """Fetch account balance and key metrics."""
        if not self.is_connected:
            return {}
        try:
            account = settings.ibkr_account or ""
            summary = self._ib.accountSummary(account)
            return {item.tag: item.value for item in summary}
        except Exception as e:
            logger.error(f"Account summary fetch failed: {e}")
            return {}

    def get_net_liquidation(self) -> Optional[float]:
        """Total account value in base currency."""
        summary = self.get_account_summary()
        try:
            return float(summary.get("NetLiquidation", 0))
        except (TypeError, ValueError):
            return None

    def submit_bracket_order(
        self,
        ticker: str,             # ASX code, e.g. "BHP"
        action: str,             # "BUY"
        qty: int,
        entry_price: float,      # Limit price
        stop_price: float,       # Stop loss
        target_price: float,     # Profit target (limit sell)
        order_ref: str = "",
    ) -> dict:
        """
        Submit a bracket order: entry limit + stop loss + profit target.
        Returns dict with order details and IBKR order IDs.
        """
        if not self.is_connected:
            return _simulate_order(ticker, action, qty, entry_price, stop_price, order_ref)

        try:
            contract = Stock(ticker, "ASX", "AUD")
            self._ib.qualifyContracts(contract)

            bracket = self._ib.bracketOrder(
                action,
                qty,
                limitPrice=round(entry_price, 3),
                takeProfitPrice=round(target_price, 3),
                stopLossPrice=round(stop_price, 3),
            )

            for order in bracket:
                order.orderRef = order_ref
                order.transmit = True

            trades = [self._ib.placeOrder(contract, o) for o in bracket]
            self._ib.sleep(1)  # Allow fill confirmation

            logger.info(
                f"Bracket submitted: {ticker} {action} {qty} @ {entry_price:.3f} "
                f"stop={stop_price:.3f} target={target_price:.3f}"
            )

            return {
                "status": "submitted",
                "ticker": ticker,
                "qty": qty,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "target_price": target_price,
                "ibkr_parent_id": trades[0].order.orderId if trades else None,
                "raw": [str(t) for t in trades],
            }

        except Exception as e:
            logger.error(f"Bracket order failed for {ticker}: {e}")
            return {"status": "error", "error": str(e), "ticker": ticker}

    def cancel_order(self, ibkr_order_id: int) -> bool:
        if not self.is_connected:
            logger.info(f"Simulation: cancel order {ibkr_order_id}")
            return True
        try:
            open_trades = self._ib.openTrades()
            for trade in open_trades:
                if trade.order.orderId == ibkr_order_id:
                    self._ib.cancelOrder(trade.order)
                    logger.info(f"Cancelled IBKR order {ibkr_order_id}")
                    return True
            logger.warning(f"Order {ibkr_order_id} not found in open trades")
            return False
        except Exception as e:
            logger.error(f"Cancel order failed: {e}")
            return False

    def get_open_positions(self) -> list[dict]:
        """Fetch current IBKR positions."""
        if not self.is_connected:
            return []
        try:
            positions = self._ib.positions()
            return [
                {
                    "ticker": p.contract.symbol,
                    "qty": p.position,
                    "avg_cost": p.avgCost,
                    "market_value": p.marketValue if hasattr(p, "marketValue") else None,
                }
                for p in positions
                if p.contract.exchange == "ASX" or p.contract.currency == "AUD"
            ]
        except Exception as e:
            logger.error(f"Positions fetch failed: {e}")
            return []


def _simulate_order(ticker, action, qty, entry_price, stop_price, order_ref) -> dict:
    """Return a simulated order response when IBKR is not connected."""
    logger.info(
        f"[SIMULATION] {action} {qty}x{ticker} @ {entry_price:.3f} "
        f"stop={stop_price:.3f} ref={order_ref}"
    )
    return {
        "status": "simulated",
        "ticker": ticker,
        "qty": qty,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "ibkr_parent_id": None,
        "raw": [],
    }
