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
    from ib_insync import IB, Stock, Order, LimitOrder, MarketOrder, StopOrder, StopLimitOrder, BracketOrder
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
        # I1 (CLAUDE.md #41): the real, gateway-derived paper/live state — set
        # on a successful connect() from the logged-in account's prefix
        # (DU*/DF* = paper, U* = live), not from any config flag. None until
        # connect() succeeds at least once.
        self.detected_paper_mode: Optional[bool] = None
        # Set True below only when an org-scoped broker resolves its OWN
        # explicit ibkr_account. Gates connect() — see the comment there for
        # why this can't just be "self.account is set", since self.account
        # defaults to the shared settings.ibkr_account below.
        self._org_account_ready = organization_id is None

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
                        self._org_account_ready = True

                    paper_val = cfg("ibkr_paper_mode")
                    if paper_val is not None:
                        self.paper_mode = paper_val.lower() in ("true", "1", "yes")
                finally:
                    db.close()
            except Exception as e:
                logger.warning(f"IBKRBroker init: failed to load org {organization_id} config (ibkr_account/ibkr_paper_mode) — falling back to defaults: {e}", exc_info=True)

        # gnzsnz/ib-gateway exposes the API via socat on 4004 (paper) / 4003
        # (live). The gateway's internal 4001/4002 are bound to localhost inside
        # the container and ALWAYS time out from other containers, so normalise
        # those to the socat ports here — regardless of whether the org set
        # ibkr_paper_mode explicitly. An explicitly non-standard port (e.g. a
        # direct TWS on 7497) is left untouched. connect() still falls back
        # across the socat ports if the first choice fails.
        if self.port in (None, 4001, 4002):
            self.port = 4004 if self.paper_mode else 4003

    def _detect_paper_mode(self) -> Optional[bool]:
        """
        I1 (CLAUDE.md #41): derive the REAL paper/live state from the
        logged-in account's prefix, not any config flag — IBKR itself
        separates paper (DU*/DF*) from live (U*) this way, and this can
        never silently disagree with what actually happens to orders.
        Resolves to this org's own configured account when set; falls back
        to the gateway's first managed account otherwise. Returns None if
        it can't be determined (e.g. managedAccounts() failed or is empty).
        Only meaningful to call after a successful connect().
        """
        try:
            managed = list(self._ib.managedAccounts() or [])
            resolve_acct = (self.account or "").strip() or (managed[0] if managed else "")
            return resolve_acct.upper().startswith(("DU", "DF")) if resolve_acct else None
        except Exception as e:
            logger.debug(f"IBKR paper/live detection failed: {e}")
            return None

    def connect(self) -> bool:
        # SAFETY: an org-scoped broker with no explicit ibkr_account of its own
        # must never silently fall back to the shared settings.ibkr_account —
        # the gateway holds ONE real login, so "falling back" means resolving
        # to whichever account a DIFFERENT org actually owns. This bit the AW
        # org on 2 Jul 2026: a brand-new org with no ibkr_account configured
        # showed another org's real IBKR position and open orders as if they
        # were its own (see CLAUDE.md #cross-org-ibkr-account-fallback). That
        # incident was first patched only inside sync_ibkr_positions_task, but
        # every other caller (open-orders panel, order submission) shared the
        # same unguarded fallback in __init__ — so the check belongs here,
        # once, instead of duplicated (and inevitably missed) per call site.
        if self.organization_id is not None and not self._org_account_ready:
            self.last_error = (
                "no ibkr_account configured for this organisation — refusing to "
                "connect using the shared gateway's default account (set "
                "ibkr_account in Admin → Config first)"
            )
            logger.warning(
                f"IBKR connect refused for org {self.organization_id}: {self.last_error}"
            )
            return False

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
        except Exception as e:
            logger.debug(f"IBKRBroker connect: util.patchAsyncio() failed (usually harmless/already-patched): {e}")

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
        port_errors: dict = {}   # port -> "ExcName: msg" of the most telling failure
        # 8s proved too tight in practice: when the gateway is mid account-sync
        # for another client (e.g. the web container's persistent connection),
        # a new client's handshake can legitimately take longer, and the old
        # timeout produced spurious "handshake never completed" failures.
        _CONNECT_TIMEOUT = int(os.environ.get("IBKR_CONNECT_TIMEOUT", "20"))
        for port in candidate_ports:
            for cid in candidate_ids:
                try:
                    self._ib = IB()
                    self._ib.connect(
                        host=self.host,
                        port=port,
                        clientId=cid,
                        timeout=_CONNECT_TIMEOUT,
                        readonly=False,
                    )
                    self._connected = True
                    self.port = port
                    self.client_id = cid
                    self.last_error = ""
                    self.detected_paper_mode = self._detect_paper_mode()

                    logger.info(
                        f"IBKR connected: host={self.host} port={port} clientId={cid} "
                        f"paper={self.paper_mode} detected_paper={self.detected_paper_mode}"
                    )
                    return True
                except Exception as e:
                    last_exc = e
                    # Prefer a Timeout over a ConnectionRefused as the port's
                    # "telling" error: refused just means nothing listens there
                    # (normal for the other mode's socat port), while a timeout
                    # means socat accepted TCP but the API handshake stalled —
                    # that's the diagnosis-worthy one.
                    prev = port_errors.get(port, "")
                    if not prev or "Refused" in prev or "refused" in prev:
                        port_errors[port] = f"{type(e).__name__}: {e}"
                    logger.warning(
                        f"IBKR connect attempt failed (port={port} clientId={cid}): "
                        f"{type(e).__name__}: {e}"
                    )
                    try:
                        self._ib.disconnect()
                    except Exception as disconnect_e:
                        logger.debug(f"IBKR connect: cleanup disconnect after failed attempt raised (harmless): {disconnect_e}")

        IBKRBroker._last_fail_times[key] = time.time()
        # Per-port summary — quoting only the LAST exception was actively
        # misleading: in paper mode the final fallback hits the live socat port
        # (4003), whose ConnectionRefused is expected noise that used to bury
        # the real story (handshake timeout on the paper port 4004).
        _per_port = "; ".join(f"port {p}: {msg}" for p, msg in port_errors.items())
        _primary = candidate_ports[0]
        _hint = ""
        if "Timeout" in port_errors.get(_primary, ""):
            _hint = (
                f" Handshake timed out on the {'paper' if self.paper_mode else 'live'} port {_primary} "
                f"after {_CONNECT_TIMEOUT}s — gateway may be busy with another client's account sync, "
                f"mid-restart, or its API settings block the connection; retry, and restart the ibkr "
                f"container if it persists."
            )
        elif "Refused" in port_errors.get(_primary, "") or "refused" in port_errors.get(_primary, ""):
            _hint = (
                f" Connection REFUSED on the {'paper' if self.paper_mode else 'live'} port {_primary} — "
                f"the gateway's socat for this mode isn't listening; check IBKR_PAPER_MODE matches the "
                f"gateway login and that the ibkr container is fully started."
            )
        self.last_error = (
            f"IBKR connect failed (host={self.host}, clientIds {candidate_ids}): {_per_port}.{_hint}"
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

    def _round_to_tick(self, price: float, exchange_key: str) -> float:
        """
        Round a price to the exchange's minimum price variation (tick size)
        to prevent IBKR API Error 110.
        """
        if exchange_key == "ASX":
            if price < 0.10:
                tick = 0.001
            elif price < 2.00:
                tick = 0.005
            else:
                tick = 0.01
        else: # US stocks (NYSE/NASDAQ/etc)
            if price < 1.00:
                tick = 0.0001
            else:
                tick = 0.01
        return round(round(price / tick) * tick, 4)

    def submit_bracket_order(
        self,
        ticker: str,             # yfinance format: "BHP.AX", "AAPL"
        action: str,             # "BUY"
        qty: float,
        entry_price: float,      # Limit price (native currency); for a BUY STOP-LIMIT
                                  # entry (pivot_price given) this is the confirmed
                                  # breakout price used to derive the stop trigger,
                                  # not the final limit price itself.
        stop_price: float,       # Stop loss (native currency)
        target_price: float,     # Profit target (native currency)
        exchange_key: str = "ASX",
        order_ref: str = "",
        pivot_price: float | None = None,   # Set only for the automated breakout-entry
                                             # path — switches the entry leg to BUY
                                             # STOP-LIMIT (see docstring below).
        limit_buffer_pct: float = 1.0,      # % above the stop trigger for the limit,
                                             # only used when pivot_price is set.
    ) -> dict:
        """
        Submit a bracket order: entry + stop loss + profit target.
        Exchange-aware: routes to ASX or US SMART router based on exchange_key.
        Returns dict with order details and IBKR order IDs.

        Entry order type (Minervini SEPA-aligned, see CLAUDE.md #39):
          - action == "BUY" and pivot_price given (the automated intraday breakout
            entry path in check_entry_triggers) → BUY STOP-LIMIT. Stop trigger =
            max(pivot_price, entry_price) — entry_price here is the already-confirmed
            live breakout price, which can be at or above the pivot. Limit =
            trigger × (1 + limit_buffer_pct / 100), capping slippage at roughly
            limit_buffer_pct past the trigger. This replaces a plain LIMIT at the
            confirm price, which sits below a still-running stock all day and dies
            unfilled at the close, or gets filled on a breakout that's already
            failing back through a stale limit.
          - Otherwise (SELL exits, or any caller that doesn't pass pivot_price,
            e.g. a manual/agent-placed entry) → unchanged plain LIMIT entry leg.
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

            t_price = self._round_to_tick(target_price, exchange_key)
            s_price = self._round_to_tick(stop_price, exchange_key)
            use_stop_limit_entry = (action == "BUY" and pivot_price is not None)
            trigger_price = None

            if use_stop_limit_entry:
                trigger_price = self._round_to_tick(max(pivot_price, entry_price), exchange_key)
                e_price = self._round_to_tick(trigger_price * (1 + limit_buffer_pct / 100.0), exchange_key)
                reverse_action = "SELL" if action == "BUY" else "BUY"
                parent = StopLimitOrder(
                    action, qty, lmtPrice=e_price, stopPrice=trigger_price,
                    orderId=self._ib.client.getReqId(), transmit=False,
                )
                take_profit = LimitOrder(
                    reverse_action, qty, t_price,
                    orderId=self._ib.client.getReqId(), transmit=False, parentId=parent.orderId,
                )
                stop_loss = StopOrder(
                    reverse_action, qty, s_price,
                    orderId=self._ib.client.getReqId(), transmit=True, parentId=parent.orderId,
                )
                bracket = [parent, take_profit, stop_loss]
            else:
                e_price = self._round_to_tick(entry_price, exchange_key)
                bracket = self._ib.bracketOrder(
                    action,
                    qty,
                    limitPrice=e_price,
                    takeProfitPrice=t_price,
                    stopLossPrice=s_price,
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

            # Wait for the parent to leave PendingSubmit before disconnecting.
            # Disconnecting while PendingSubmit causes the gateway to silently
            # cancel the order — it never reaches the exchange.
            parent = trades[0]
            stable = {"Submitted", "PreSubmitted", "Filled", "Cancelled",
                      "ApiCancelled", "Inactive", "Rejected", "PendingCancel"}
            waited = 0.0
            while parent.orderStatus.status not in stable and waited < 12.0:
                self._ib.sleep(0.5)
                waited += 0.5

            pstatus = parent.orderStatus.status
            statuses = [(t.order.orderType, t.orderStatus.status) for t in trades]
            trigger_note = f" trigger={trigger_price:.3f}" if trigger_price is not None else ""
            logger.info(
                f"Bracket {ticker} {action} {qty} @ {e_price:.3f}{trigger_note} "
                f"stop={s_price:.3f} target={t_price:.3f} → {statuses} "
                f"(waited {waited:.1f}s for stable status)"
            )

            # Surface rejections instead of pretending success.
            bad = {"Cancelled", "ApiCancelled", "Inactive", "Rejected", "PendingCancel"}
            if pstatus in bad:
                reason = ""
                try:
                    if parent.log:
                        reason = parent.log[-1].message or ""
                except Exception as e:
                    logger.debug(f"Bracket {ticker}: could not read rejection reason from order log: {e}")
                msg = f"IBKR {pstatus}" + (f": {reason}" if reason else "")
                logger.error(f"Bracket REJECTED for {ticker}: {msg}")
                return {"status": "error", "error": msg, "ticker": ticker,
                        "order_status": pstatus, "raw": [str(t) for t in trades]}

            # Still PendingSubmit after 12s — gateway never acknowledged it.
            # Cancel and return error so the signal stays PENDING for retry.
            if pstatus == "PendingSubmit":
                msg = ("Order stuck in PendingSubmit after 12s — "
                       "gateway did not acknowledge. Signal will retry.")
                logger.error(f"Bracket STUCK for {ticker}: {msg}")
                for t in trades:
                    try:
                        self._ib.cancelOrder(t.order)
                    except Exception as cancel_e:
                        logger.warning(f"Bracket {ticker}: failed to cancel stuck-pending order {t.order}: {cancel_e}", exc_info=True)
                self._ib.sleep(1)
                return {"status": "error", "error": msg, "ticker": ticker,
                        "order_status": pstatus}

            logger.info(f"Bracket accepted for {ticker}: parent status={pstatus}")
            return {
                "status": "submitted",
                "broker": "ibkr",
                "ticker": ticker,
                "qty": qty,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "target_price": target_price,
                "order_status": pstatus,
                "entry_order_type": "STP LMT" if use_stop_limit_entry else "LMT",
                "trigger_price": trigger_price,
                "limit_price": e_price,
                "ibkr_parent_id": parent.order.orderId if parent else None,
                "ibkr_parent_perm_id": getattr(parent.order, "permId", None) if parent else None,
                "raw": [str(t) for t in trades],
            }

        except Exception as e:
            logger.error(f"Bracket order failed for {ticker}: {e}")
            return {"status": "error", "error": str(e), "ticker": ticker}

    def submit_market_sell(
        self,
        ticker: str,             # yfinance format: "BHP.AX", "AAPL" — or bare symbol, either works
        qty: float,
        exchange_key: str = "ASX",
        order_ref: str = "",
    ) -> dict:
        """
        Plain market SELL — used ONLY when a position has no working stop order
        left at the broker (an imported/orphaned position, or a bracket that was
        already cancelled). Never call this while a bracket's stop-loss child is
        still working: whichever of the two sells fills first would leave the
        other trying to sell shares already gone — a naked short in either
        direction. See CLAUDE.md #37 / sync_stop_orders' equity branch, which is
        responsible for checking get_open_orders() before ever calling this.

        Cancels any stray working orders left on this symbol first so nothing
        else can execute against the same shares after this order is placed.
        Returns a dict in the same shape as submit_bracket_order's result.
        """
        if not self.is_connected:
            return _simulate_order(ticker, "SELL", qty, 0, 0, order_ref)

        try:
            contract = self._build_contract(ticker, exchange_key)
            qualified = self._ib.qualifyContracts(contract)
            if not qualified or not getattr(contract, "conId", 0):
                msg = f"contract not qualified for {ticker} on {exchange_key} (bad symbol / no permission)"
                logger.error(f"Market sell failed: {msg}")
                return {"status": "error", "error": msg, "ticker": ticker}

            self._ib.reqAllOpenOrders()
            self._ib.sleep(0.5)
            stray = [t for t in self._ib.openTrades() if t.contract.symbol == contract.symbol]
            for t in stray:
                try:
                    self._ib.cancelOrder(t.order)
                except Exception as ce:
                    logger.warning(f"Market sell: failed to cancel stray order for {ticker}: {ce}")
            if stray:
                self._ib.sleep(1)

            order = MarketOrder("SELL", qty)
            order.orderRef = order_ref
            order.tif = "DAY"
            if self.account:
                order.account = self.account

            trade = self._ib.placeOrder(contract, order)

            stable = {"Submitted", "PreSubmitted", "Filled", "Cancelled",
                      "ApiCancelled", "Inactive", "Rejected", "PendingCancel"}
            waited = 0.0
            while trade.orderStatus.status not in stable and waited < 12.0:
                self._ib.sleep(0.5)
                waited += 0.5

            status = trade.orderStatus.status
            bad = {"Cancelled", "ApiCancelled", "Inactive", "Rejected", "PendingCancel"}
            if status in bad:
                reason = ""
                try:
                    if trade.log:
                        reason = trade.log[-1].message or ""
                except Exception as e:
                    logger.debug(f"Market sell {ticker}: could not read rejection reason from order log: {e}")
                msg = f"IBKR {status}" + (f": {reason}" if reason else "")
                logger.error(f"Market sell REJECTED for {ticker}: {msg}")
                return {"status": "error", "error": msg, "ticker": ticker, "order_status": status}

            logger.info(f"Market sell submitted for {ticker}: qty={qty} status={status} "
                        f"(cancelled {len(stray)} stray order(s) first)")
            return {
                "status": "submitted",
                "broker": "ibkr",
                "ticker": ticker,
                "qty": qty,
                "order_status": status,
                "ibkr_order_id": order.orderId,
                "ibkr_perm_id": getattr(order, "permId", None),
            }
        except Exception as e:
            logger.error(f"Market sell failed for {ticker}: {e}")
            return {"status": "error", "error": str(e), "ticker": ticker}

    def cancel_order(self, ibkr_order_id: int) -> tuple[bool, str]:
        """Cancel an active IBKR order by orderId.

        Returns (True, "") on success or (False, reason_str) on failure.

        IBKR only allows cancelling orders that belong to the current client ID
        session. Orders placed by other sessions (e.g. bracket children submitted
        by a previous Celery worker, or orders placed directly in TWS) are visible
        via reqAllOpenOrders() but cannot be cancelled via cancelOrder() — IBKR
        will silently ignore or reject the request. Use cancel_all_orders()
        (reqGlobalCancel) to cancel everything across all client IDs at once.
        """
        if not self.is_connected:
            logger.info(f"Simulation: cancel order {ibkr_order_id}")
            return True, ""
        try:
            # Pull ALL open orders into the ib_insync cache (same as get_open_orders)
            self._ib.reqAllOpenOrders()
            self._ib.sleep(0.5)   # pump loop so openOrder* messages are processed
            open_trades = self._ib.openTrades()
            for trade in open_trades:
                if trade.order.orderId == ibkr_order_id:
                    self._ib.cancelOrder(trade.order)
                    self._ib.sleep(1)   # let gateway process the cancel
                    logger.info(f"Cancelled IBKR order {ibkr_order_id}")
                    return True, ""
            logger.warning(
                f"cancel_order: orderId {ibkr_order_id} not found in {len(open_trades)} "
                f"open trades after reqAllOpenOrders — it may belong to a different client "
                f"session (use Cancel All / reqGlobalCancel to cancel cross-session orders)"
            )
            return False, (
                f"Order {ibkr_order_id} cannot be cancelled — it was placed by a different "
                f"IBKR client session (e.g. a bracket child or a previous worker). "
                f"Use the 'Cancel All' button to cancel all orders on this account."
            )
        except Exception as e:
            logger.error(f"Cancel order failed: {e}")
            return False, str(e)

    def cancel_all_orders(self) -> tuple[bool, str]:
        """Cancel ALL open orders on this account across all client IDs.

        Uses reqGlobalCancel() — the only IBKR call that can cancel orders
        regardless of which client session or TWS placed them. This is the
        correct tool when you need to clear bracket children, stale pre-submitted
        orders, or orders from disconnected sessions.

        Returns (True, cancelled_count_str) on success or (False, error) on failure.
        """
        if not self.is_connected:
            logger.info("Simulation: global cancel all orders")
            return True, "simulated"
        try:
            # Snapshot how many orders exist before cancelling
            self._ib.reqAllOpenOrders()
            self._ib.sleep(0.5)
            before = len(self._ib.openTrades())

            self._ib.reqGlobalCancel()
            self._ib.sleep(2)   # give gateway time to process all cancellations

            # Re-check how many remain
            self._ib.reqAllOpenOrders()
            self._ib.sleep(0.5)
            after = len(self._ib.openTrades())

            cancelled = before - after
            logger.info(f"reqGlobalCancel: {before} orders before → {after} after ({cancelled} cancelled)")
            return True, f"Sent global cancel — {cancelled} order(s) cancelled ({after} remaining)"
        except Exception as e:
            logger.error(f"cancel_all_orders failed: {e}")
            return False, str(e)

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

    def get_executions(self, days: int = 2) -> list[dict]:
        """
        Fetch recent fills (executions + commission reports) for reconciling
        DB Order rows against what actually happened at the broker — used by
        sync_order_status. reqExecutions is blocking and returns fills directly
        (unlike positions/orders, no extra sleep() pump is needed).

        Returns one dict per fill: {perm_id, order_id, order_ref, ticker, side
        ("BOT"/"SLD"), qty, avg_price, commission, time, account}.

        `account` (ex.acctNumber) is included so callers can defend against a
        (rare) orderId collision across sub-accounts under a multi-org (FA/
        linked) gateway login — reqExecutions returns every sub-account's
        fills in one call, same leak shape as get_open_orders (CLAUDE.md #41).
        """
        if not self.is_connected:
            return []
        try:
            from ib_insync import ExecutionFilter
            from datetime import datetime, timedelta
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d-%H:%M:%S")
            fills = self._ib.reqExecutions(ExecutionFilter(time=cutoff))
            out = []
            for f in fills:
                ex, comm = f.execution, f.commissionReport
                out.append({
                    "perm_id":    ex.permId,
                    "order_id":   ex.orderId,
                    "order_ref":  getattr(ex, "orderRef", "") or "",
                    "ticker":     f.contract.symbol,
                    "side":       ex.side,   # "BOT" (bought) or "SLD" (sold)
                    "qty":        float(ex.shares or 0),
                    "avg_price":  float(ex.avgPrice or ex.price or 0),
                    "commission": float(comm.commission) if comm and comm.commission else 0.0,
                    "time":       f.time,
                    "account":    getattr(ex, "acctNumber", "") or "",
                })
            return out
        except Exception as e:
            logger.error(f"Executions fetch failed: {e}")
            return []

    def get_open_orders(self) -> list[dict]:
        """Fetch ALL open/working orders on the account.

        Uses reqAllOpenOrders() so orders placed by ANY client id (and via TWS)
        are returned — openTrades() alone only sees the current connection's
        client id, which would miss orders placed by other workers.

        sleep() pumps the ib_insync event loop so all inbound openOrder messages
        from the gateway are processed before openTrades() is called.
        """
        if not self.is_connected:
            return []
        try:
            self._ib.reqAllOpenOrders()
            self._ib.sleep(0.5)   # pump loop; gateway sends openOrder* then openOrderEnd
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
                    # Under a multi-org (FA/linked) gateway login, reqAllOpenOrders()
                    # returns every sub-account's orders in one call — without this
                    # field, every caller's account filter compares against None and
                    # silently keeps everything (CLAUDE.md #41 cross-org leak).
                    "account":       getattr(o, "account", "") or "",
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
        Returns {last, bid, ask, volume, timestamp, delayed} or None if unavailable.

        Tries live data (reqMarketDataType(1)) first. If no subscription is active
        for that exchange, IBKR returns nothing rather than an error, which used to
        make this method fall straight through to yfinance — worse latency than
        IBKR's own free ~15-min delayed feed. So on a live miss, this retries once
        with reqMarketDataType(3) (IBKR delayed) before giving up; the caller sets
        data_source accordingly (see get_intraday_price). Falls back to the bid/ask
        midpoint when last/close are unavailable (thin ASX names often have no last
        trade but a live quote).
        """
        if not self.is_connected or not IB_AVAILABLE:
            return None
        try:
            from datetime import datetime as _dt
            import math
            contract = self._build_contract(ticker, exchange_key)
            self._ib.qualifyContracts(contract)

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

            def _snapshot(market_data_type: int):
                self._ib.reqMarketDataType(market_data_type)
                ticker_data = self._ib.reqMktData(contract, "", True, False)
                self._ib.sleep(2)  # wait for data to arrive
                last = _num(ticker_data.last)
                if last is None:
                    last = _num(ticker_data.close)
                bid = _num(ticker_data.bid)
                ask = _num(ticker_data.ask)
                if last is None and bid is not None and ask is not None:
                    last = round((bid + ask) / 2, 4)
                vol = _num(ticker_data.volume) or 0.0
                self._ib.cancelMktData(contract)
                if last is None:
                    return None
                return {"last": last, "bid": bid, "ask": ask, "volume": int(vol), "timestamp": _dt.utcnow()}

            live = _snapshot(1)
            if live is not None:
                live["delayed"] = False
                return live

            delayed = _snapshot(3)
            if delayed is not None:
                delayed["delayed"] = True
                logger.debug(f"IBKR live data unavailable for {ticker} — using delayed (~15min) feed")
                return delayed

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
        "broker": "simulation",
        "ticker": ticker,
        "qty": qty,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "ibkr_parent_id": None,
        "raw": [],
    }
