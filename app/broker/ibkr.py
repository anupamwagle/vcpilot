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
    import logging
    logging.getLogger("ib_insync").setLevel(logging.CRITICAL)
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
    _last_fail_times: dict[tuple[str, int], float] = {}
    _FAIL_COOLDOWN = 60.0  # seconds cooldown

    def __init__(self, organization_id=None):
        self.organization_id = organization_id
        self._ib: Optional[object] = None
        self._connected = False
        self.last_error: str = ""
        
        # Load credentials dynamically based on organization
        self.host = settings.ibkr_host
        self.port = settings.ibkr_port
        self.client_id = settings.ibkr_client_id
        self.account = settings.ibkr_account
        self.paper_mode = settings.ibkr_paper_mode
        
        if organization_id:
            try:
                from app.database import SessionLocal
                from app.models.config import SystemConfig
                db = SessionLocal()
                try:
                    def cfg(key):
                        c = db.query(SystemConfig).filter(
                            SystemConfig.key == key, 
                            SystemConfig.organization_id == organization_id
                        ).first()
                        return c.value if c else None
                        
                    acc_val = cfg("ibkr_account")
                    if acc_val:
                        self.account = acc_val
                        
                    paper_val = cfg("ibkr_paper_mode")
                    if paper_val is not None:
                        self.paper_mode = paper_val.lower() in ("true", "1", "yes")
                        self.port = 4002 if self.paper_mode else 4001
                finally:
                    db.close()
            except Exception:
                pass


    def connect(self) -> bool:
        if settings.ibkr_simulate or not IB_AVAILABLE:
            self.last_error = (
                "IBKR_SIMULATE is on" if settings.ibkr_simulate
                else "ib_insync not installed in this container"
            )
            logger.info(f"IBKR sandbox/simulation mode enabled ({self.last_error})")
            return False

        # Cooldown check to prevent connection attempt spam when gateway is down
        now = time.time()
        key = (self.host, self.port)
        last_fail = IBKRBroker._last_fail_times.get(key, 0.0)
        if now - last_fail < IBKRBroker._FAIL_COOLDOWN:
            remaining = int(IBKRBroker._FAIL_COOLDOWN - (now - last_fail))
            self.last_error = f"in {remaining}s connection cooldown after a recent failure"
            return False

        # Celery prefork workers inherit a (often closed/stale) asyncio event
        # loop from the parent process. ib_insync drives the API handshake on
        # that loop, so a broken loop means the inbound bytes are never pumped
        # and connect() hangs until TimeoutError — with nothing logged on the
        # gateway side. When we're NOT already inside a running loop (i.e. the
        # Celery worker, not uvicorn), install a fresh loop for this connection.
        import asyncio
        try:
            asyncio.get_running_loop()  # raises if no loop is running (worker case)
        except RuntimeError:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_closed():
                    raise RuntimeError
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

        try:
            from ib_insync import util
            util.patchAsyncio()
        except Exception:
            pass

        # Try the configured clientId first; on failure (commonly a clientId
        # collision when multiple containers all use id=1, which the gateway
        # answers with silence → TimeoutError) retry with random high ids.
        import random
        candidate_ids = [self.client_id] + [random.randint(2000, 9999) for _ in range(2)]
        last_exc = None
        for cid in candidate_ids:
            try:
                self._ib = IB()
                self._ib.connect(
                    host=self.host,
                    port=self.port,
                    clientId=cid,
                    timeout=15,
                    readonly=False,
                )
                self._connected = True
                self.client_id = cid
                self.last_error = ""
                logger.info(
                    f"IBKR connected: host={self.host} port={self.port} "
                    f"clientId={cid} paper={self.paper_mode}"
                )
                return True
            except Exception as e:
                last_exc = e
                logger.warning(
                    f"IBKR connect attempt failed (clientId={cid}): {type(e).__name__}: {e}"
                )
                try:
                    self._ib.disconnect()
                except Exception:
                    pass

        IBKRBroker._last_fail_times[key] = time.time()
        self.last_error = (
            f"{type(last_exc).__name__}: {last_exc} "
            f"(host={self.host} port={self.port}, tried clientIds {candidate_ids}). "
            f"If the gateway console shows NO incoming connection, this is a network/"
            f"trusted-IP block, not a clientId clash."
        )
        logger.error(f"IBKR connection failed after retries: {self.last_error!r}")
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
            account = self.account or ""
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

    def _build_contract(self, ticker: str, exchange_key: str = "ASX"):
        """
        Build an ib_insync Stock contract appropriate for the given exchange.

        Exchange routing:
          ASX             → Stock(symbol, "ASX", "AUD")       e.g. BHP
          NYSE / NASDAQ   → Stock(symbol, "SMART", "USD")     e.g. AAPL
          Unknown         → Stock(symbol, "SMART", "USD")     fallback

        The ticker passed here is the exchange_code (display code), NOT the yfinance ticker.
        Callers must strip the yfinance suffix before calling:
          "BHP.AX" → "BHP"  for ASX
          "AAPL"   → "AAPL" for NYSE
        """
        if not IB_AVAILABLE:
            return None

        # Strip any yfinance suffix
        symbol = ticker.replace(".AX", "").replace("-USD", "").upper()

        if exchange_key == "ASX":
            return Stock(symbol, "ASX", "AUD")
        elif exchange_key in ("NYSE", "NASDAQ"):
            return Stock(symbol, "SMART", "USD")
        else:
            logger.warning(f"Unknown exchange_key '{exchange_key}' for IBKR — using SMART/USD")
            return Stock(symbol, "SMART", "USD")

    def submit_bracket_order(
        self,
        ticker: str,             # yfinance format: "BHP.AX", "AAPL"
        action: str,             # "BUY"
        qty: float,
        entry_price: float,      # Limit price (native currency)
        stop_price: float,       # Stop loss (native currency)
        target_price: float,     # Profit target (native currency)
        exchange_key: str = "ASX",
        order_ref: str = "",
    ) -> dict:
        """
        Submit a bracket order: entry limit + stop loss + profit target.
        Exchange-aware: routes to ASX or US SMART router based on exchange_key.
        Returns dict with order details and IBKR order IDs.
        """
        if not self.is_connected:
            return _simulate_order(ticker, action, qty, entry_price, stop_price, order_ref)

        try:
            contract = self._build_contract(ticker, exchange_key)
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
                if self.account:
                    order.account = self.account  # Routes to correct sub-account under FA

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

    def get_open_positions(self, exchange_key: str = None) -> list[dict]:
        """
        Fetch current IBKR positions.
        If exchange_key is specified, filter to only that exchange.
        If None, return all positions across all exchanges.
        """
        if not self.is_connected:
            return []
        try:
            positions = self._ib.positions()
            result = []
            for p in positions:
                contract_exchange = getattr(p.contract, "exchange", "")
                contract_currency = getattr(p.contract, "currency", "")
                # Map IBKR exchange to our exchange_key
                if exchange_key:
                    if exchange_key == "ASX" and contract_exchange != "ASX":
                        continue
                    if exchange_key in ("NYSE", "NASDAQ") and contract_currency != "USD":
                        continue
                result.append({
                    "ticker":        p.contract.symbol,
                    "exchange":      contract_exchange,
                    "currency":      contract_currency,
                    "qty":           p.position,
                    "avg_cost":      p.avgCost,
                    "market_value":  getattr(p, "marketValue", None),
                    "account":       getattr(p, "account", "") or "",
                })
            return result
        except Exception as e:
            logger.error(f"Positions fetch failed: {e}")
            return []

    def get_open_orders(self) -> list[dict]:
        """Fetch open orders on IBKR."""
        if not self.is_connected:
            return []
        try:
            trades = self._ib.openTrades()
            return [
                {
                    "ibkr_order_id": t.order.orderId,
                    "ticker": t.contract.symbol,
                    "action": t.order.action,
                    "qty": t.order.totalQuantity,
                    "status": t.orderStatus.status,
                }
                for t in trades
            ]
        except Exception as e:
            logger.error(f"Orders fetch failed: {e}")
            return []

    def get_market_snapshot(self, ticker: str, exchange_key: str = "ASX") -> Optional[dict]:
        """
        Request a real-time market data snapshot for a ticker on any supported exchange.
        Returns {last, bid, ask, volume, timestamp} or None if unavailable.
        Requires active IBKR market data subscription for the exchange.
        """
        if not self.is_connected or not IB_AVAILABLE:
            return None
        try:
            from ib_insync import Stock as IBStock
            from datetime import datetime as _dt
            contract = self._build_contract(ticker, exchange_key)
            self._ib.qualifyContracts(contract)
            # reqMktData with snapshot=True returns a Ticker object immediately
            ticker_data = self._ib.reqMktData(contract, "", True, False)
            self._ib.sleep(2)  # Wait for data to arrive
            last  = ticker_data.last or ticker_data.close or None
            bid   = ticker_data.bid  or None
            ask   = ticker_data.ask  or None
            vol   = ticker_data.volume or 0
            if last:
                return {
                    "last": float(last),
                    "bid": float(bid) if bid else None,
                    "ask": float(ask) if ask else None,
                    "volume": int(vol),
                    "timestamp": _dt.utcnow(),
                }
            return None
        except Exception as e:
            logger.debug(f"Market snapshot failed for {ticker}: {e}")
            return None


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
