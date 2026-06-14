"""
CryptoBroker — ccxt unified API wrapper for crypto exchange order management.

Supports any exchange available in the ccxt library (Binance, Coinbase, Kraken, etc.).
The exchange is selected per-org via SystemConfig key 'crypto_exchange_key'.
API credentials (key + secret) are stored per-org in SystemConfig.

Design principles:
  - Mirror IBKRBroker interface: connect(), submit_bracket_order(), get_positions()
  - Simulation fallback when credentials not set or testnet=True
  - All P&L reported in USD; AUD conversion handled by risk/manager.py
  - Crypto bracket orders are emulated: entry market order + separate stop-loss + take-profit

Note on ccxt availability:
  ccxt is an optional dependency. Install with: pip install ccxt
  If not installed, CryptoBroker operates in simulation mode only.
"""
from __future__ import annotations
import time
from datetime import datetime
from typing import Optional
from loguru import logger

try:
    import ccxt
    CCXT_AVAILABLE = True
except ImportError:
    CCXT_AVAILABLE = False
    logger.warning("ccxt not installed — CryptoBroker in simulation mode. Install with: pip install ccxt")


class CryptoBroker:
    """
    Unified crypto exchange broker via ccxt.
    Instantiate with an exchange key matching ExchangeConfig.ccxt_provider,
    plus the org's API credentials from SystemConfig.
    """

    def __init__(
        self,
        ccxt_provider: str,       # e.g. "binance", "coinbase", "kraken"
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = True,     # Always default to testnet for safety
        organization_id: int = None,
    ):
        self.ccxt_provider   = ccxt_provider.lower()
        self.api_key         = api_key
        self.api_secret      = api_secret
        self.testnet         = testnet
        self.organization_id = organization_id
        self._exchange       = None
        self._connected      = False

        # If org_id provided, load credentials from SystemConfig
        if organization_id and not api_key:
            self._load_org_credentials()

    def _load_org_credentials(self):
        """Load API credentials from SystemConfig for this organisation."""
        try:
            from app.database import SessionLocal
            from app.models.config import SystemConfig
            db = SessionLocal()
            try:
                def cfg(key):
                    row = db.query(SystemConfig).filter(
                        SystemConfig.key == key,
                        SystemConfig.organization_id == self.organization_id
                    ).first()
                    return row.value if row else ""

                self.api_key    = cfg("crypto_api_key")    or ""
                self.api_secret = cfg("crypto_api_secret") or ""
                testnet_val     = cfg("crypto_testnet")    or "true"
                self.testnet    = testnet_val.lower() in ("true", "1", "yes")

                provider = cfg("crypto_exchange_key") or ""
                if provider and provider.startswith("CRYPTO_"):
                    self.ccxt_provider = provider.replace("CRYPTO_", "").lower()
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"CryptoBroker: could not load org credentials: {e}")

    def connect(self) -> bool:
        """
        Initialise the ccxt exchange instance.
        Returns True if credentials are set and connection is ready.
        In simulation mode (no credentials or testnet), returns False gracefully.
        """
        if not CCXT_AVAILABLE:
            logger.info("CryptoBroker: ccxt not installed — simulation mode")
            return False

        if not self.api_key or not self.api_secret:
            logger.info(f"CryptoBroker: no credentials for {self.ccxt_provider} — simulation mode")
            return False

        try:
            exchange_class = getattr(ccxt, self.ccxt_provider, None)
            if exchange_class is None:
                logger.error(f"CryptoBroker: unknown ccxt provider '{self.ccxt_provider}'")
                return False

            # Base config — extended per-provider below
            exchange_config: dict = {
                "apiKey":          self.api_key,
                "secret":          self.api_secret,
                "enableRateLimit": True,
                "options":         {"defaultType": "spot"},
            }

            # ── MEXC-specific options ──────────────────────────────────────────
            # MEXC v3 REST requires recvWindow and uses USDT as the quote currency.
            # MEXC does not support a sandbox/testnet via ccxt — use simulation mode
            # instead (no credentials) for paper trading on MEXC.
            if self.ccxt_provider == "mexc":
                exchange_config["options"].update({
                    "recvWindow": 60000,       # MEXC default is tight; extend to 60s
                    "adjustForTimeDifference": True,
                })
                # MEXC does not have a ccxt sandbox — force simulation if testnet requested
                if self.testnet:
                    logger.info("CryptoBroker: MEXC has no ccxt testnet — using simulation mode")
                    return False

            # ── Binance-specific options ───────────────────────────────────────
            elif self.ccxt_provider == "binance":
                exchange_config["options"].update({
                    "adjustForTimeDifference": True,
                })

            self._exchange = exchange_class(exchange_config)

            if self.testnet and self.ccxt_provider != "mexc":
                # Enable sandbox/testnet if supported (not MEXC)
                if hasattr(self._exchange, "set_sandbox_mode"):
                    self._exchange.set_sandbox_mode(True)
                    logger.info(f"CryptoBroker: {self.ccxt_provider} testnet enabled")

            # Verify credentials with a balance fetch
            self._exchange.fetch_balance()
            self._connected = True
            logger.info(f"CryptoBroker: connected to {self.ccxt_provider} (testnet={self.testnet})")
            return True

        except Exception as e:
            logger.error(f"CryptoBroker: connection failed for {self.ccxt_provider}: {e}")
            return False

    def disconnect(self):
        self._exchange  = None
        self._connected = False

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()

    @property
    def is_connected(self) -> bool:
        return self._connected and CCXT_AVAILABLE and self._exchange is not None

    # -------------------------------------------------------------------------
    # Account data
    # -------------------------------------------------------------------------

    def get_balance(self) -> dict:
        """Return available balance by currency. Returns {} on failure."""
        if not self.is_connected:
            return {}
        try:
            balance = self._exchange.fetch_balance()
            return {k: v for k, v in balance["total"].items() if v > 0}
        except Exception as e:
            logger.error(f"CryptoBroker: balance fetch failed: {e}")
            return {}

    def get_usd_balance(self) -> float:
        """Return total USD/USDT balance (approx)."""
        bal = self.get_balance()
        return float(bal.get("USDT", 0) or bal.get("USD", 0))

    # -------------------------------------------------------------------------
    # Order management
    # -------------------------------------------------------------------------

    def submit_bracket_order(
        self,
        ticker: str,          # yfinance format: "BTC-USD" → ccxt symbol: "BTC/USDT"
        action: str,          # "BUY"
        qty: float,
        entry_price: float,   # Limit price for entry
        stop_price: float,    # Stop-loss trigger price
        target_price: float,  # Take-profit limit price
        order_ref: str = "",
    ) -> dict:
        """
        Submit a crypto bracket order.
        Crypto exchanges don't natively support bracket orders, so we:
          1. Place the entry limit order
          2. Place a stop-loss order (stop-market)
          3. Place a take-profit limit order (limit sell)

        Note: Stop-loss and take-profit are conditional orders — not all exchanges
        support OCO (one-cancels-other). We place both and rely on
        check_exit_rules_task to cancel the remaining leg on fill.

        Returns a dict matching the IBKRBroker response format for consistency.
        """
        if not self.is_connected:
            return _simulate_crypto_order(ticker, action, qty, entry_price, stop_price, order_ref)

        ccxt_symbol = _yfinance_to_ccxt(ticker, self.ccxt_provider)
        try:
            # 1. Entry limit order
            entry_order = self._exchange.create_limit_order(
                symbol=ccxt_symbol,
                side=action.lower(),
                amount=qty,
                price=entry_price,
                params={"clientOrderId": order_ref[:36] if order_ref else ""},
            )

            result = {
                "status": "submitted",
                "ticker": ticker,
                "ccxt_symbol": ccxt_symbol,
                "qty": qty,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "target_price": target_price,
                "entry_order_id": entry_order.get("id"),
                "stop_order_id": None,
                "target_order_id": None,
                "broker": "ccxt",
                "exchange": self.ccxt_provider,
                "raw": str(entry_order),
            }

            # 2. Attempt stop-loss order (if exchange supports it)
            try:
                stop_order = self._exchange.create_order(
                    symbol=ccxt_symbol,
                    type="stop_market",
                    side="sell",
                    amount=qty,
                    price=stop_price,
                    params={"stopPrice": stop_price},
                )
                result["stop_order_id"] = stop_order.get("id")
            except Exception as e:
                logger.warning(f"CryptoBroker: stop-loss order failed for {ticker}: {e}")

            # 3. Attempt take-profit limit order
            try:
                tp_order = self._exchange.create_limit_order(
                    symbol=ccxt_symbol,
                    side="sell",
                    amount=qty,
                    price=target_price,
                )
                result["target_order_id"] = tp_order.get("id")
            except Exception as e:
                logger.warning(f"CryptoBroker: take-profit order failed for {ticker}: {e}")

            logger.info(
                f"CryptoBroker bracket submitted: {ticker} {action} {qty} "
                f"@ {entry_price} stop={stop_price} target={target_price}"
            )
            return result

        except Exception as e:
            logger.error(f"CryptoBroker: order failed for {ticker}: {e}")
            return {"status": "error", "error": str(e), "ticker": ticker}

    def get_open_orders(self, ticker: str = None) -> list[dict]:
        """Return all open orders, optionally filtered by ticker."""
        if not self.is_connected:
            return []
        try:
            ccxt_symbol = _yfinance_to_ccxt(ticker, self.ccxt_provider) if ticker else None
            orders = self._exchange.fetch_open_orders(symbol=ccxt_symbol)
            return orders
        except Exception as e:
            logger.error(f"CryptoBroker: open orders fetch failed: {e}")
            return []

    def cancel_order(self, order_id: str, ticker: str) -> bool:
        """Cancel an open order by ID."""
        if not self.is_connected:
            return False
        try:
            ccxt_symbol = _yfinance_to_ccxt(ticker, self.ccxt_provider)
            self._exchange.cancel_order(order_id, symbol=ccxt_symbol)
            logger.info(f"CryptoBroker: cancelled order {order_id} for {ticker}")
            return True
        except Exception as e:
            logger.error(f"CryptoBroker: cancel order failed for {order_id}: {e}")
            return False

    def get_positions(self) -> list[dict]:
        """
        Return current open crypto positions (non-zero balances).
        For spot trading, position = non-zero currency balance.
        """
        if not self.is_connected:
            return []
        try:
            balance = self._exchange.fetch_balance()
            positions = []
            # IR settles in AUD; other exchanges in USD/USDT
            quote_currency = "AUD" if self.ccxt_provider == "independentreserve" else "USD"
            stable_coins = {"USDT", "USD", "BUSD", "USDC", "AUD", "TUSD", "FDUSD"}
            for currency, amount in balance["total"].items():
                if amount > 0 and currency not in stable_coins:
                    ticker = f"{currency}-{quote_currency}"
                    positions.append({
                        "ticker":   ticker,
                        "currency": currency,
                        "qty":      float(amount),
                        "exchange": self.ccxt_provider,
                    })
            return positions
        except Exception as e:
            logger.error(f"CryptoBroker: positions fetch failed: {e}")
            return []

    def get_market_snapshot(self, ticker: str) -> Optional[dict]:
        """Fetch real-time ticker data (bid/ask/last)."""
        if not self.is_connected:
            return None
        try:
            ccxt_symbol = _yfinance_to_ccxt(ticker, self.ccxt_provider)
            ticker_data = self._exchange.fetch_ticker(ccxt_symbol)
            return {
                "last":   ticker_data.get("last"),
                "bid":    ticker_data.get("bid"),
                "ask":    ticker_data.get("ask"),
                "volume": ticker_data.get("baseVolume"),
                "timestamp": datetime.utcnow(),
                "data_source": "ccxt",
            }
        except Exception as e:
            logger.debug(f"CryptoBroker: snapshot failed for {ticker}: {e}")
            return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _yfinance_to_ccxt(yf_ticker: str, ccxt_provider: str = "") -> str:
    """
    Convert yfinance ticker format to ccxt unified symbol format.

    Independent Reserve (AUD pairs):
        "BTC-AUD" → "XBT/AUD"   (IR uses XBT, not BTC)
        "ETH-AUD" → "ETH/AUD"

    MEXC (USDT pairs):
        "BTC-USD"  → "BTC/USDT"
        "ETH-USD"  → "ETH/USDT"

    Binance / Kraken / Coinbase (USDT pairs):
        "BTC-USD" → "BTC/USDT"
        "ETH-USD" → "ETH/USDT"

    Note: ccxt always uses the "/" separator and the canonical symbol (e.g. "BTC/USDT").
    MEXC internally uses concatenated symbols (BTCUSDT) but ccxt abstracts this away.
    """
    # Independent Reserve uses AUD pairs, and calls Bitcoin "XBT" (not "BTC")
    # See: https://www.independentreserve.com/API
    _IR_SYMBOL_MAP = {
        "BTC": "XBT",   # IR uses XBT; all other exchanges use BTC
        "WBTC": "WBTC", # Wrapped BTC stays as-is
    }
    if "-AUD" in yf_ticker or ccxt_provider == "independentreserve":
        base = yf_ticker.replace("-AUD", "").replace("-USD", "").replace("-USDT", "").upper()
        if ccxt_provider == "independentreserve":
            base = _IR_SYMBOL_MAP.get(base, base)
        return f"{base}/AUD"
    if "-USD" in yf_ticker or "-USDT" in yf_ticker:
        # All USD-based crypto exchanges (Binance, MEXC, Coinbase, Kraken) use USDT quote
        base = yf_ticker.replace("-USD", "").replace("-USDT", "").upper()
        return f"{base}/USDT"
    return yf_ticker


def _simulate_crypto_order(
    ticker: str, action: str, qty: float,
    entry_price: float, stop_price: float, order_ref: str,
) -> dict:
    """Return a simulated order result when broker is not connected."""
    sim_id = f"SIM_{int(time.time())}"
    logger.info(f"[CRYPTO SIM] {action} {qty} {ticker} @ {entry_price} ref={order_ref}")
    return {
        "status": "simulated",
        "ticker": ticker,
        "qty": qty,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "entry_order_id": sim_id,
        "stop_order_id": f"{sim_id}_SL",
        "target_order_id": f"{sim_id}_TP",
        "broker": "simulation",
        "exchange": "simulation",
    }


def get_crypto_broker_for_org(organization_id: int, exchange_key: str = None) -> "CryptoBroker":
    """
    Factory: create a CryptoBroker instance for an organisation,
    loading credentials from SystemConfig.

    Args:
      organization_id: org scope — credentials and exchange_key are loaded from SystemConfig
      exchange_key:    optional override (e.g. "CRYPTO_MEXC"). If None, reads
                       `crypto_exchange_key` from SystemConfig (set by org admin in /admin/config).

    Supported exchanges (via ccxt):
      CRYPTO_INDEPENDENTRESERVE → ccxt_provider = "independentreserve"
      CRYPTO_MEXC               → ccxt_provider = "mexc"
      CRYPTO_BINANCE            → ccxt_provider = "binance"
      CRYPTO_COINBASE           → ccxt_provider = "coinbase"
      CRYPTO_KRAKEN             → ccxt_provider = "kraken"
    """
    # Derive initial provider hint from exchange_key if provided
    initial_provider = "independentreserve"  # safe default; overridden by _load_org_credentials
    if exchange_key:
        _ek_map = {
            "CRYPTO_MEXC":               "mexc",
            "CRYPTO_BINANCE":            "binance",
            "CRYPTO_COINBASE":           "coinbase",
            "CRYPTO_KRAKEN":             "kraken",
            "CRYPTO_INDEPENDENTRESERVE": "independentreserve",
        }
        initial_provider = _ek_map.get(exchange_key.upper(), "independentreserve")

    return CryptoBroker(
        ccxt_provider=initial_provider,
        organization_id=organization_id,
    )
