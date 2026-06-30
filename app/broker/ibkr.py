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
                finally:
                    db.close()
            except Exception:
                pass

        # gnzsnz/ib-gateway exposes the API via socat on 4004 (paper) / 4003
        # (live). The gateway's internal 4001/4002 are bound to localhost inside
        # the container and ALWAYS time out from other containers, so normalise
        # those to the socat ports here — regardless of whether the org set
        # ibkr_paper_mode explicitly. An explicitly non-standard port (e.g. a
        # direct TWS on 7497) is left untouched. connect() still falls back
        # across the socat ports if the first choice fails.
        if self.port in (None, 4001, 4002):
            self.port = 4004 if self.paper_mode else 4003


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

        # ib_insync drives the API handshake on an asyncio event loop. When
        # called from a thread (asyncio.to_thread / Celery prefork worker),
        # there is no running loop in that thread. Python 3.10+ raises
        # RuntimeError from get_event_loop() in non-main threads when no loop
        # is set, so we must always install a fresh one in that case.
        import asyncio
        try:
            asyncio.get_running_loop()  # succeeds only inside async context
        except RuntimeError:
            # No running loop in this thread — install a fresh one.
            # Don't call get_event_loop() here: on Python 3.10+ it raises
            # "There is no current event loop in thread 'asyncio_0'" for
            # non-main threads, which is exactly the error we're seeing.
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        try:
            from ib_insync import util
            util.patchAsyncio()
        except Exception:
            pass

        # Build the list of ports to try. The gnzsnz/ib-gateway image runs the
        # gateway bound to 127.0.0.1:4001/4002 inside the container and exposes
        # it via socat on DIFFERENT ports — 4004 for paper, 4003 for live. A TCP
        # probe to 4002 can succeed (socat/half-open) while the API handshake
        # never completes, which presents as a TimeoutError with nothing logged
        # on the gateway. So if the configured port times out, fall back to the
        # socat ports automatically.
        import random, os
        socat_alts = [4004, 4003] if self.paper_mode else [4003, 4004]
        candidate_ports = [self.port] + [p for p in socat_alts if p != self.port]
        # Try a PROCESS-UNIQUE clientId first so we don't collide with another
        # container already holding the configured id (e.g. the api container on
        # clientId=1) — a collision makes the gateway silently ignore us and the
        # handshake times out. Derive a stable-per-process id from the PID, then
        # fall back to the configured id and a random one.
        uniq_id = 1000 + (os.getpid() % 8000)
        seen, candidate_ids = set(), []
        for c in (uniq_id, self.client_id, random.randint(2000, 9999)):
            if c not in seen:
                seen.add(c)
                candidate_ids.append(c)

        last_exc = None
        for port in candidate_ports:
            for cid in candidate_ids:
                try:
                    self._ib = IB()
                    self._ib.connect(
                        host=self.host,
                        port=port,
                        clientId=cid,
                        timeout=8,
                        readonly=False,
                    )
                    self._connected = True
                    self.port = port
                    self.client_id = cid
                    self.last_error = ""
                    logger.info(
                        f"IBKR connected: host={self.host} port={port} "
                        f"clientId={cid} paper={self.paper_mode}"
                    )
                    return True
                except Exception as e:
                    last_exc = e
                    logger.warning(
                        f"IBKR connect attempt failed (port={port} clientId={cid}): "
                        f"{type(e).__name__}: {e}"
                    )
                    try:
                        self._ib.disconnect()
                    except Exception:
                        pass

        IBKRBroker._last_fail_times[key] = time.time()
        self.last_error = (
            f"{type(last_exc).__name__}: {last_exc} "
            f"(host={self.host}, tried ports {candidate_ports} clientIds {candidate_ids}). "
            f"TCP reachable but API handshake never completed — check the gateway "
            f"socat port (paper=4004/live=4003) and API settings."
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

        # Use SMART routing (not direct exchange routing). Direct-routing to
        # "ASX" trips IBKR's API precaution (Error 10311) which DISCARDS the
        # order. SMART + primaryExchange gives best execution and avoids it.
        if exchange_key == "ASX":
            return Stock(symbol, "SMART", "AUD", primaryExchange="ASX")
        elif exchange_key in ("NYSE", "NASDAQ"):
            return Stock(symbol, "SMART", "USD", primaryExchange=exchange_key)
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
            qualified = self._ib.qualifyContracts(contract)
            if not qualified or not getattr(contract, "conId", 0):
                msg = f"contract not qualified for {ticker} on {exchange_key} (bad symbol / no permission)"
                logger.error(f"Bracket order failed: {msg}")
                return {"status": "error", "error": msg, "ticker": ticker}

            bracket = self._ib.bracketOrder(
                action,
                qty,
                limitPrice=round(entry_price, 3),
                takeProfitPrice=round(target_price, 3),
                stopLossPrice=round(stop_price, 3),
            )

            # IMPORTANT: keep ib_insync's transmit flags (parent=False,
            # takeProfit=False, stopLoss=True) so the whole bracket transmits
            # ATOMICALLY when the last leg is placed. Forcing transmit=True on all
            # legs submits the parent naked first and can leave the bracket in a
            # broken/rejected state.
            #
            # TIF: ib_insync's bracket children default to GTC, but many IBKR
            # accounts have an order preset that forces DAY — that TIF conflict
            # makes the gateway CANCEL the legs (Error 10349). Set every leg to
            # DAY explicitly so it matches the preset and isn't cancelled. (For
            # GTC you must change the gateway's order preset, then set it here.)
            for order in bracket:
                order.orderRef = order_ref
                order.tif = "DAY"
                if self.account:
                    order.account = self.account  # Routes to correct sub-account under FA

            trades = [self._ib.placeOrder(contract, o) for o in bracket]
            self._ib.sleep(2.5)  # let order status settle BEFORE we disconnect

            parent = trades[0]
            pstatus = parent.orderStatus.status
            statuses = [(t.order.orderType, t.orderStatus.status) for t in trades]
            logger.info(
                f"Bracket {ticker} {action} {qty} @ {entry_price:.3f} "
                f"stop={stop_price:.3f} target={target_price:.3f} → {statuses}"
            )

            # Surface rejections instead of pretending success.
            bad = {"Cancelled", "ApiCancelled", "Inactive", "Rejected", "PendingCancel"}
            if pstatus in bad:
                reason = ""
                try:
                    if parent.log:
                        reason = parent.log[-1].message or ""
                except Exception:
                    pass
                msg = f"IBKR {pstatus}" + (f": {reason}" if reason else "")
                logger.error(f"Bracket REJECTED for {ticker}: {msg}")
                return {"status": "error", "error": msg, "ticker": ticker,
                        "order_status": pstatus, "raw": [str(t) for t in trades]}

            logger.info(f"Bracket accepted for {ticker}: parent status={pstatus}")
            return {
                "status": "submitted",
                "ticker": ticker,
                "qty": qty,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "target_price": target_price,
                "order_status": pstatus,
                "ibkr_parent_id": parent.order.orderId if parent else None,
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
        """Fetch ALL open/working orders on the account.

        Uses reqAllOpenOrders() so orders placed by ANY client id (and via TWS)
        are returned — openTrades() alone only sees the current connection's
        client id, which would miss orders placed by other workers.

        Waits for the openOrderEnd event (with a 4-second timeout) instead of
        a blind sleep so we don't return before the gateway has pushed all orders.
        """
        if not self.is_connected:
            return []
        try:
            # Subscribe to openOrderEnd to know when the gateway is done sending.
            _done = []
            def _on_end():
                _done.append(True)
            self._ib.openOrderEndEvent += _on_end
            try:
                self._ib.reqAllOpenOrders()
                # Wait up to 4 seconds for the gateway to finish pushing orders.
                waited = 0.0
                while not _done and waited < 4.0:
                    self._ib.sleep(0.2)
                    waited += 0.2
                if not _done:
                    logger.debug("get_open_orders: openOrderEnd not received within 4s — using cached trades")
            finally:
                self._ib.openOrderEndEvent -= _on_end

            trades = self._ib.openTrades()
            out = []
            for t in trades:
                o, st = t.order, t.orderStatus
                lmt = getattr(o, "lmtPrice", 0) or 0
                aux = getattr(o, "auxPrice", 0) or 0
                out.append({
                    "ibkr_order_id": o.orderId,
                    "perm_id":       getattr(o, "permId", None),
                    "ticker":        t.contract.symbol,
                    "exchange":      getattr(t.contract, "primaryExchange", "") or getattr(t.contract, "exchange", ""),
                    "currency":      getattr(t.contract, "currency", ""),
                    "action":        o.action,
                    "qty":           float(o.totalQuantity or 0),
                    "order_type":    o.orderType,
                    "limit_price":   float(lmt) if lmt else None,
                    "stop_price":    float(aux) if aux else None,
                    "tif":           getattr(o, "tif", "") or "",
                    "status":        st.status,
                    "filled":        float(st.filled or 0),
                    "remaining":     float(st.remaining or 0),
                    "order_ref":     getattr(o, "orderRef", "") or "",
                })
            return out
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
            import math
            # reqMktData with snapshot=True returns a Ticker object immediately
            ticker_data = self._ib.reqMktData(contract, "", True, False)
            self._ib.sleep(2)  # Wait for data to arrive

            def _num(x):
                # IBKR returns float('nan') for unavailable fields (e.g. ASX
                # outside market hours). NaN is truthy, so `x or 0` keeps it and
                # int(nan) then raises "cannot convert float NaN to integer".
                try:
                    if x is None:
                        return None
                    f = float(x)
                    return None if math.isnan(f) else f
                except (TypeError, ValueError):
                    return None

            last = _num(ticker_data.last)
            if last is None:
                last = _num(ticker_data.close)
            bid = _num(ticker_data.bid)
            ask = _num(ticker_data.ask)
            vol = _num(ticker_data.volume) or 0.0
            if last is not None:
                return {
                    "last": last,
                    "bid": bid,
                    "ask": ask,
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
