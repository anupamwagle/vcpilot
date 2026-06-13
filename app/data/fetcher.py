"""
AstraTrade Data Fetcher — yfinance wrapper for price, volume, and fundamental data.

Strategy:
  - yfinance for ALL price/volume/MA data (free, unlimited EOD)
  - yfinance quarterly_financials for EPS, revenue, ROE (free, covers ASX)
  - FMP free tier (250 calls/day) used ONLY for supplemental data on shortlisted stocks
  - ASX index components fetched from Wikipedia / hardcoded lists (updated weekly)
"""
from __future__ import annotations
import time
from datetime import datetime, date, timedelta
from typing import Optional
import pandas as pd
import numpy as np
import yfinance as yf
from loguru import logger


# ---------------------------------------------------------------------------
# Universe helpers
# ---------------------------------------------------------------------------

ASX200_WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/S%26P/ASX_200"
ASX300_WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/S%26P/ASX_300"
ASX_ALL_LISTED_URL   = "https://www.asx.com.au/asx/research/ASXListedCompanies.csv"

# ---------------------------------------------------------------------------
# Ticker normalisation — convert user input to yfinance canonical format
# ---------------------------------------------------------------------------

# Exchange-specific benchmark indices for regime evaluation
EXCHANGE_BENCHMARKS: dict[str, str] = {
    "ASX":    "^AXJO",
    "NYSE":   "^GSPC",
    "NASDAQ": "^IXIC",
    "CRYPTO": "BTC-USD",
    "CRYPTO_BINANCE":            "BTC-USD",
    "CRYPTO_COINBASE":           "BTC-USD",
    "CRYPTO_KRAKEN":             "BTC-USD",
    "CRYPTO_INDEPENDENTRESERVE": "BTC-AUD",   # IR trades in AUD; use BTC-AUD as regime proxy
}

# Exchanges that settle/price in AUD (not USD/USDT)
CRYPTO_AUD_EXCHANGES = {"CRYPTO_INDEPENDENTRESERVE"}


def normalize_ticker(user_input: str, exchange_key: str) -> dict:
    """
    Convert raw user input (e.g. "BHP", "AAPL", "BTC") to canonical formats.

    Returns:
        {
            "yfinance_ticker": str,   # used for price fetching: "BHP.AX", "AAPL", "BTC-USD"
            "display_code":    str,   # clean display: "BHP", "AAPL", "BTC"
            "currency":        str,   # "AUD", "USD", "USDT"
            "asset_type":      str,   # "EQUITY" | "CRYPTO"
            "exchange_key":    str,   # as passed in
        }

    Exchange key conventions:
        ASX             → append ".AX" suffix if not present
        NYSE / NASDAQ   → use as-is (no suffix)
        CRYPTO_*        → append "-USD" suffix for yfinance
    """
    code = user_input.strip().upper()
    is_crypto = exchange_key.startswith("CRYPTO_")

    if exchange_key == "ASX":
        yf_ticker = code if code.endswith(".AX") else f"{code}.AX"
        display   = code.replace(".AX", "")
        currency  = "AUD"
        asset_type = "EQUITY"

    elif exchange_key in ("NYSE", "NASDAQ"):
        # Strip any accidental exchange suffix
        yf_ticker = code.replace(".AX", "").replace("-USD", "")
        display   = yf_ticker
        currency  = "USD"
        asset_type = "EQUITY"

    elif is_crypto:
        # Strip existing suffix if user typed "BTC-USD", "BTC-AUD", "BTC/USDT" etc.
        clean = (code.replace("/USDT", "").replace("/USD", "").replace("/AUD", "")
                     .replace("-USD", "").replace("-USDT", "").replace("-AUD", ""))
        # Independent Reserve trades in AUD; all other crypto exchanges use USD/USDT
        if exchange_key in CRYPTO_AUD_EXCHANGES:
            yf_ticker = f"{clean}-AUD"   # yfinance AUD crypto: "BTC-AUD"
            currency  = "AUD"
        else:
            yf_ticker = f"{clean}-USD"   # yfinance USD crypto: "BTC-USD"
            currency  = "USD"
        display    = clean
        asset_type = "CRYPTO"

    else:
        # Unknown exchange — pass through as-is
        yf_ticker = code
        display   = code
        currency  = "USD"
        asset_type = "EQUITY"

    return {
        "yfinance_ticker": yf_ticker,
        "display_code":    display,
        "currency":        currency,
        "asset_type":      asset_type,
        "exchange_key":    exchange_key,
    }


# ---------------------------------------------------------------------------
# FX Rate — AUD/USD
# ---------------------------------------------------------------------------

_FX_CACHE: dict[str, tuple] = {}  # {pair: (rate, fetched_at)}
_FX_CACHE_TTL = 3600  # 1 hour


def get_fx_rate(from_currency: str = "AUD", to_currency: str = "USD") -> float:
    """
    Fetch the live exchange rate between two currencies.
    Supports fiat currency tickers (AUDUSD=X) and crypto tickers (BNB-USD) on yfinance,
    plus recursive USD-bridged FX rates.
    Uses a 1-hour Redis/memory cache.
    Falls back to 0.65 for AUDUSD, 1.54 for USDAUD, and 1.0 otherwise if all sources fail.

    Returns: float rate (e.g. 0.645 means 1 unit of from_currency = 0.645 to_currency)
    """
    if from_currency == to_currency:
        return 1.0

    pair_key = f"{from_currency}{to_currency}"
    now = datetime.utcnow()

    # Check memory cache
    cached = _FX_CACHE.get(pair_key)
    if cached:
        rate, fetched_at = cached
        if (now - fetched_at).total_seconds() < _FX_CACHE_TTL:
            return rate

    # Try Redis cache first
    try:
        import redis as _redis
        from app.config import settings as _s
        r = _redis.from_url(_s.redis_url, decode_responses=True)
        cached_rate = r.get(f"fx_rate:{pair_key}")
        if cached_rate:
            rate = float(cached_rate)
            _FX_CACHE[pair_key] = (rate, now)
            return rate
    except Exception:
        pass

    # Try different symbols
    symbols_to_try = [
        f"{from_currency}{to_currency}=X",  # Fiat format
        f"{from_currency}-{to_currency}",   # Crypto format (e.g., BNB-USD)
    ]
    
    rate = None
    for yf_symbol in symbols_to_try:
        try:
            ticker = yf.Ticker(yf_symbol)
            hist = ticker.history(period="2d", interval="1d")
            if hist is not None and not hist.empty:
                rate = float(hist["Close"].iloc[-1])
                break
        except Exception:
            pass

    # If that failed, try inverse
    if rate is None:
        inverse_symbols = [
            f"{to_currency}{from_currency}=X",
            f"{to_currency}-{from_currency}",
        ]
        for yf_symbol in inverse_symbols:
            try:
                ticker = yf.Ticker(yf_symbol)
                hist = ticker.history(period="2d", interval="1d")
                if hist is not None and not hist.empty:
                    inv_rate = float(hist["Close"].iloc[-1])
                    if inv_rate > 0:
                        rate = 1.0 / inv_rate
                        break
            except Exception:
                pass

    # Recursive USD bridging for cross-currency (e.g. BNB to AUD or AUD to BNB)
    if rate is None:
        cryptos = {"BTC", "ETH", "BNB", "USDT", "USDC", "SOL", "ADA", "XRP", "DOT", "DOGE"}
        if (from_currency in cryptos or to_currency in cryptos) and from_currency != "USD" and to_currency != "USD":
            try:
                rate_to_usd = get_fx_rate(from_currency, "USD")
                rate_from_usd = get_fx_rate("USD", to_currency)
                rate = rate_to_usd * rate_from_usd
            except Exception:
                pass

    if rate is not None:
        _FX_CACHE[pair_key] = (rate, now)
        try:
            r = _redis.from_url(_s.redis_url, decode_responses=True)
            r.set(f"fx_rate:{pair_key}", str(rate), ex=_FX_CACHE_TTL)
        except Exception:
            pass
        logger.debug(f"FX rate {pair_key}: {rate}")
        return rate

    # Hard fallback
    fallback = 0.65 if pair_key == "AUDUSD" else (1.54 if pair_key == "USDAUD" else 1.0)
    logger.warning(f"Using fallback FX rate for {pair_key}: {fallback}")
    return fallback


def aud_to_currency(aud_amount: float, target_currency: str) -> float:
    """Convert an AUD amount to the target currency."""
    if target_currency == "AUD":
        return aud_amount
    rate = get_fx_rate("AUD", target_currency)
    return aud_amount * rate


def currency_to_aud(amount: float, from_currency: str) -> float:
    """Convert an amount from any currency to AUD equivalent."""
    if from_currency == "AUD":
        return amount
    rate = get_fx_rate(from_currency, "AUD")
    return amount * rate


# ---------------------------------------------------------------------------
# Crypto universe — top 100 tokens supported by yfinance
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Independent Reserve — live coin map (yfinance base → IR lowercase code).
# Built from: GET https://api.independentreserve.com/Public/GetValidPrimaryCurrencyCodes
# Note: IR uses "xbt" for Bitcoin; "audm"/"ausd" are AUD tokens (excluded).
# ---------------------------------------------------------------------------
IR_SYMBOL_MAP: dict[str, str] = {
    "BTC":    "xbt",     # IR calls Bitcoin "xbt"
    "ETH":    "eth",
    "SOL":    "sol",
    "XRP":    "xrp",
    "USDC":   "usdc",
    "USDT":   "usdt",
    "AAVE":   "aave",
    "ADA":    "ada",
    "AVAX":   "avax",
    "BAT":    "bat",
    "BCH":    "bch",
    "BONK":   "bonk",
    "COMP":   "comp",
    "DAI":    "dai",
    "DOGE":   "doge",
    "DOT":    "dot",
    "ETC":    "etc",
    "GRT":    "grt",
    "HYPE":   "hype",
    "LINK":   "link",
    "LTC":    "ltc",
    "MANA":   "mana",
    "MATIC":  "matic",
    "MKR":    "mkr",
    "PENGU":  "pengu",
    "PEPE":   "pepe",
    "RENDER": "render",
    "RLUSD":  "rlusd",
    "SAND":   "sand",
    "SHIB":   "shib",
    "SNX":    "snx",
    "TRUMP":  "trump",
    "TRX":    "trx",
    "UNI":    "uni",
    "WIF":    "wif",
    "XAUT":   "xaut",
    "XLM":    "xlm",
    "YFI":    "yfi",
    "ZRX":    "zrx",
}


def get_ir_supported_tickers() -> list[str]:
    """
    Return all coins tradeable on Independent Reserve in yfinance AUD format.
    Fetches live list from IR's public API; falls back to hardcoded IR_SYMBOL_MAP.
    Excludes AUD-denominated tokens (audm, ausd) which are not crypto assets.
    """
    _SKIP_IR_CODES = {"audm", "ausd"}  # AUD money-market / stablecoin tokens
    try:
        import requests as _req
        r = _req.get(
            "https://api.independentreserve.com/Public/GetValidPrimaryCurrencyCodes",
            timeout=8,
            headers={"User-Agent": "AstraTrade/1.0 (+https://github.com/anupamwagle/vcpilot)"},
        )
        r.raise_for_status()
        ir_codes = [c.lower() for c in r.json()]
        result = []
        for ir_code in ir_codes:
            if ir_code in _SKIP_IR_CODES:
                continue
            # Look up our canonical yfinance symbol from the map
            yf_base = next((k for k, v in IR_SYMBOL_MAP.items() if v == ir_code), None)
            if yf_base:
                result.append(f"{yf_base}-AUD")
            else:
                # New coin IR added that isn't in our map yet — use uppercase directly
                result.append(f"{ir_code.upper()}-AUD")
        return result if result else [f"{sym}-AUD" for sym in IR_SYMBOL_MAP]
    except Exception:
        # Network failure — fall back to hardcoded map
        return [f"{sym}-AUD" for sym in IR_SYMBOL_MAP]


# ---------------------------------------------------------------------------
# Generic top-~300 list for non-IR exchanges (Binance, Coinbase, Kraken).
# Suffix (-USD / -AUD) is applied by get_top_crypto_tickers().
# For IR, use get_ir_supported_tickers() instead — IR has its own coin list.
# ---------------------------------------------------------------------------
TOP_CRYPTO_SYMBOLS: list[str] = [
    # ── Mega-cap (top ~30) ────────────────────────────────────────────────────
    "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "AVAX", "DOGE", "TRX", "LINK",
    "DOT", "MATIC", "SHIB", "LTC", "BCH", "UNI", "ATOM", "XLM", "NEAR", "ETC",
    "TON", "PEPE", "IMX", "APT", "SUI", "RENDER", "OP", "ARB", "FET", "STX",
    # ── Large-cap (top 30–80) ─────────────────────────────────────────────────
    "FIL", "HBAR", "VET", "ALGO", "AAVE", "ICP", "MKR", "QNT", "SAND", "MANA",
    "FLOW", "AXS", "THETA", "GRT", "CHZ", "ZEC", "XMR", "COMP", "YFI", "BAT",
    "INJ", "LDO", "WIF", "FLOKI", "BONK", "JUP", "TIA", "GALA", "PYTH", "JASMY",
    "FTM", "AR", "SEI", "BEAM", "W", "ENA", "CORE", "EGLD", "WLD", "DYDX",
    # ── Notable large-caps not in original list ───────────────────────────────
    "KAS", "TAO", "HYPE", "ONDO", "TRUMP", "PENGU", "APE", "HNT",
    # ── Mid-cap DeFi / Infrastructure ────────────────────────────────────────
    "ENJ", "ZRX", "CRV", "SNX", "SUSHI", "OMG", "BAL", "REN", "1INCH", "RUNE",
    "STRK", "AGIX", "ORDI", "BTT", "MINA", "RON", "LRC", "GNO", "WOO", "RAY",
    "IOTA", "ENS", "GMT", "ONE", "QTUM", "DGB", "KAVA", "ZIL", "ANKR", "WAVES",
    # ── DeFi additions ────────────────────────────────────────────────────────
    "CAKE", "GMX", "BLUR", "MAGIC", "SSV", "BADGER", "PERP", "BNT", "NEXO", "PAXG",
    "SCRT", "GTC", "CREAM", "FIS", "AUCTION", "OSMO", "JST", "TRB", "IQ", "ZEN",
    # ── Infrastructure / L1–L2 ───────────────────────────────────────────────
    "METIS", "SKL", "CKB", "FLUX", "XDC", "GLMR", "MOVR", "COTI", "ICX", "XEC",
    "XEM", "RVN", "TOMO", "XNO", "KDA", "ASTR", "SXP", "RSR", "DUSK", "POND",
    "FLR", "SAFE", "FXS", "STG", "GFI", "ATM",
    # ── Gaming / NFT / Metaverse ─────────────────────────────────────────────
    "ALICE", "AUDIO", "HIGH", "DAR", "LOKA", "SLP", "HOOK", "VOXEL", "LIT", "SPELL",
    "KEY", "MNGO", "KUJI",
    # ── Fan tokens ────────────────────────────────────────────────────────────
    "LAZIO", "SANTOS", "ALPINE", "PORTO", "CITY", "BAR", "JUV", "ACM", "PSG", "OG",
    # ── Utilities / Misc ─────────────────────────────────────────────────────
    "CYBER", "ID", "AERGO", "ARPA", "CELR", "CHR", "ACH", "DENT", "WIN", "VRA",
    "VTHO", "POKT", "UTK", "CLV", "REEF", "OGN", "PHA", "STEEM", "KMD", "NULS",
    "TWT", "REQ", "ERN", "BIFI", "XVG", "NFT", "MAPS",
    # ── New / recent launches ─────────────────────────────────────────────────
    "MOVE", "ME", "USUAL", "VIRTUAL",
    # ── Extended meme / launch tokens ────────────────────────────────────────
    "PENDLE", "JTO", "PYUSD", "PIXEL", "PORTAL", "ALT", "DYM", "MANTA", "ZK", "SAGA",
    "ETHFI", "REZ", "BB", "OMNI", "LISTA", "ZRO", "BANANA", "DOGS", "HMSTR", "CATI",
    "EIGEN", "SCR", "KAIA", "CELO", "ROSE", "CFX", "LPT", "API3", "OCEAN", "BAND",
    # ── Long tail / extended coverage ────────────────────────────────────────
    "STORJ", "NMR", "RLC", "OXT", "CTSI", "LOOM", "ORN", "DOCK", "DATA", "AST",
    "BAKE", "BURGER", "DEGO", "MASK", "POLS", "ALPHA", "HARD", "WING", "BEL", "CTK",
    "CHESS", "DODO", "FOR", "MDX", "FRONT", "LINA", "UNFI", "TLM", "QUICK", "FARM",
    "GHST", "SUPER", "COMBO", "VITE", "FIRO", "STMX", "ONG", "COCOS", "XVS", "AUTO",
    # ── Trending meme / new coins ─────────────────────────────────────────────
    "PNUT", "POPCAT", "MOODENG", "NEIRO", "BOME", "MEW", "MOTHER", "WEN", "DEGEN",
    "PONKE", "BRETT", "TURBO", "MOG", "GIGA", "SLERF", "FARTCOIN", "MELANIA",
]


def get_top_crypto_tickers(exchange_key: str = "CRYPTO_INDEPENDENTRESERVE") -> list[str]:
    """
    Return crypto tickers in yfinance format for the given exchange.
    - CRYPTO_INDEPENDENTRESERVE → fetches live list from IR API (~40 AUD pairs)
    - Other exchanges (Binance, Coinbase, Kraken) → top-~300 generic USD list
    """
    if exchange_key in CRYPTO_AUD_EXCHANGES:
        return get_ir_supported_tickers()
    return [f"{sym}-USD" for sym in TOP_CRYPTO_SYMBOLS]


def get_asx200_tickers() -> list[str]:
    """
    Fetch current ASX200 constituents from Wikipedia.
    Returns list in yfinance format: ["BHP.AX", "CBA.AX", ...]
    Falls back to a cached list if Wikipedia is unavailable.
    """
    import io, requests as _req
    try:
        # pd.read_html blocks Wikipedia without a browser UA — use requests first
        headers = {"User-Agent": "Mozilla/5.0 (compatible; AstraTrade/1.0; +https://github.com/anupamwagle/vcpilot)"}
        resp = _req.get(ASX200_WIKIPEDIA_URL, headers=headers, timeout=20)
        resp.raise_for_status()
        tables = pd.read_html(io.StringIO(resp.text))
        for tbl in tables:
            cols_lower = [str(c).lower() for c in tbl.columns]
            if "code" in cols_lower:
                col = next(c for c in tbl.columns if str(c).lower() == "code")
                codes = tbl[col].dropna().tolist()
                tickers = [f"{str(c).strip().upper()}.AX" for c in codes if isinstance(c, str) and len(c.strip()) >= 2]
                if len(tickers) > 100:
                    logger.info(f"Fetched {len(tickers)} ASX200 tickers from Wikipedia")
                    return tickers
    except Exception as e:
        logger.warning(f"Wikipedia ASX200 fetch failed: {e}. Using fallback list.")

    # Minimal fallback — top 20 by market cap (extend this in production)
    logger.warning("Using 20-stock fallback universe — Wikipedia unavailable")
    return [
        "BHP.AX", "CBA.AX", "NAB.AX", "WBC.AX", "ANZ.AX", "WES.AX",
        "MQG.AX", "CSL.AX", "RIO.AX", "WOW.AX", "FMG.AX", "TLS.AX",
        "GMG.AX", "TCL.AX", "WDS.AX", "STO.AX", "QBE.AX", "IAG.AX",
        "AMP.AX", "SUN.AX",
    ]


def get_asx300_tickers() -> list[str]:
    """
    Fetch current ASX300 constituents from Wikipedia.
    Returns list in yfinance format: ["BHP.AX", "CBA.AX", ...]
    Falls back to ASX200 if the ASX300 page is unavailable.
    """
    import io, requests as _req
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; AstraTrade/1.0; +https://github.com/anupamwagle/vcpilot)"}
        resp = _req.get(ASX300_WIKIPEDIA_URL, headers=headers, timeout=20)
        resp.raise_for_status()
        tables = pd.read_html(io.StringIO(resp.text))
        for tbl in tables:
            cols_lower = [str(c).lower() for c in tbl.columns]
            if "code" in cols_lower:
                col = next(c for c in tbl.columns if str(c).lower() == "code")
                codes = tbl[col].dropna().tolist()
                tickers = [f"{str(c).strip().upper()}.AX" for c in codes if isinstance(c, str) and len(c.strip()) >= 2]
                if len(tickers) > 150:
                    logger.info(f"Fetched {len(tickers)} ASX300 tickers from Wikipedia")
                    return tickers
    except Exception as e:
        logger.warning(f"Wikipedia ASX300 fetch failed: {e}. Falling back to ASX200.")
    return get_asx200_tickers()


def get_asx300_metadata() -> dict[str, dict]:
    """
    Fetch current ASX300 constituents with names and sectors from Wikipedia.
    Returns dict of ticker -> {"name": str, "sector": str, "in_asx200": bool}
    """
    import io, requests as _req
    results = {}
    # First get ASX200 metadata so we can flag membership
    asx200_meta = get_asx200_metadata()
    asx200_tickers = set(asx200_meta.keys())

    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; AstraTrade/1.0; +https://github.com/anupamwagle/vcpilot)"}
        resp = _req.get(ASX300_WIKIPEDIA_URL, headers=headers, timeout=20)
        resp.raise_for_status()
        tables = pd.read_html(io.StringIO(resp.text))
        for tbl in tables:
            cols_lower = [str(c).lower() for c in tbl.columns]
            if "code" in cols_lower:
                code_col = next(c for c in tbl.columns if str(c).lower() == "code")
                comp_col = next((c for c in tbl.columns if str(c).lower() == "company"), None)
                sect_col = next((c for c in tbl.columns if str(c).lower() == "sector"), None)

                for _, row in tbl.iterrows():
                    code = row[code_col]
                    if not isinstance(code, str) or len(code.strip()) < 2:
                        continue
                    ticker = f"{code.strip().upper()}.AX"
                    name = str(row[comp_col]).strip() if comp_col is not None and pd.notna(row[comp_col]) else ""
                    sector = str(row[sect_col]).strip() if sect_col is not None and pd.notna(row[sect_col]) else ""
                    results[ticker] = {
                        "name": name,
                        "sector": sector,
                        "in_asx200": ticker in asx200_tickers,
                        "in_asx300": True,
                    }

                if len(results) > 150:
                    logger.info(f"Fetched metadata for {len(results)} ASX300 tickers from Wikipedia")
                    # Merge any ASX200 tickers not in ASX300 table (some Wikipedia pages differ)
                    for t, m in asx200_meta.items():
                        if t not in results:
                            results[t] = {**m, "in_asx200": True, "in_asx300": True}
                    return results
    except Exception as e:
        logger.warning(f"Wikipedia ASX300 metadata fetch failed: {e}")
    # Fall back to ASX200 metadata
    return {t: {**m, "in_asx200": True, "in_asx300": True} for t, m in asx200_meta.items()}


def get_asx_all_listed() -> list[dict]:
    """
    Fetch ALL listed companies on the ASX from the ASX research endpoint.
    Returns a list of dicts: [{"ticker": "BHP.AX", "name": str, "sector": str}, ...]

    Uses the ASX's own CSV export. Falls back to ASX300 if unavailable.
    Free, no auth, updates daily.
    """
    import io, requests as _req
    url = ASX_ALL_LISTED_URL
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; AstraTrade/1.0)",
            "Accept": "text/html,application/xhtml+xml,*/*",
            "Referer": "https://www.asx.com.au/",
        }
        resp = _req.get(url, headers=headers, timeout=60)
        resp.raise_for_status()
        text = resp.text

        # Real ASX CSV format (as at Jun 2026):
        #   Line 1: "ASX listed companies as at <date>"   ← preamble, skip
        #   Line 2: "Company name,ASX code,GICS industry group"  ← header
        #   Line 3+: data rows
        lines = text.strip().splitlines()
        # Skip preamble lines until we hit the CSV header
        csv_start = 0
        for i, line in enumerate(lines):
            low = line.lower()
            if "asx code" in low or ("company" in low and ("code" in low or "gics" in low)):
                csv_start = i
                break

        csv_text = "\n".join(lines[csv_start:])
        df = pd.read_csv(io.StringIO(csv_text), dtype=str)
        df.columns = [c.strip().lower() for c in df.columns]

        # Locate columns flexibly — ASX has changed column order/names before
        code_col = next((c for c in df.columns if "asx code" in c or (c == "code")), None)
        if code_col is None:
            code_col = next((c for c in df.columns if "code" in c), None)
        name_col = next((c for c in df.columns if "company" in c or "name" in c), None)
        gics_col = next((c for c in df.columns if "gics" in c or "industry" in c), None)
        mcap_col = next((c for c in df.columns if "market" in c and "cap" in c), None)

        if code_col is None:
            logger.warning(f"ASX all-listed CSV: could not find code column. Columns: {list(df.columns)}")
            return []

        results = []
        for _, row in df.iterrows():
            code = str(row[code_col]).strip().upper() if code_col else ""
            if not code or len(code) < 2 or code in ("NAN", "ASX CODE"):
                continue
            # Skip warrants / options (codes > 5 chars are usually not plain equities)
            if len(code) > 5:
                continue
            name     = str(row[name_col]).strip()  if name_col and pd.notna(row[name_col])  else ""
            gics     = str(row[gics_col]).strip()  if gics_col and pd.notna(row[gics_col])  else ""
            mcap_str = str(row[mcap_col]).strip()  if mcap_col and pd.notna(row[mcap_col])  else ""
            try:
                mcap = int(float(mcap_str.replace(",", "").replace("$", ""))) if mcap_str else None
            except (ValueError, TypeError):
                mcap = None

            results.append({
                "ticker":     f"{code}.AX",
                "name":       name,
                "sector":     gics,
                "industry":   gics,
                "market_cap": mcap,
            })

        logger.info(f"Fetched {len(results)} ASX listed companies from ASX website")
        return results

    except Exception as e:
        logger.warning(f"ASX all-listed fetch failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Sector label inference — maps GICS sector/industry to human-readable labels
# ---------------------------------------------------------------------------

# Priority-ordered rules: most specific first. Each entry is (keywords_any, sector, label_name).
# keywords_any: list of substrings — match if ANY appear in combined sector+industry string.
# sector:       GICS sector string to match (None = match any sector).
_SECTOR_LABEL_RULES: list[tuple[list[str], str | None, str]] = [
    # ── Materials / Mining sub-sectors (most specific first) ────────────────
    (["gold"],                          None,           "Gold"),
    (["lithium"],                       None,           "Lithium"),
    (["rare earth", "rare-earth"],      None,           "Rare Earth"),
    (["uranium"],                       None,           "Uranium"),
    (["silver"],                        None,           "Silver"),
    (["copper"],                        None,           "Copper"),
    (["nickel", "cobalt"],              None,           "Nickel & Cobalt"),
    (["iron", "steel"],                 None,           "Iron & Steel"),
    (["coal", "thermal"],               None,           "Coal"),
    (["oil", "petroleum", "lng", "lpg"],None,           "Oil & Gas"),
    (["gas", "natural gas"],            "Energy",       "Oil & Gas"),
    (["mining", "mineral"],             "Basic Materials","Mining (General)"),
    (["mining", "mineral"],             "Materials",    "Mining (General)"),
    # ── Energy ──────────────────────────────────────────────────────────────
    (["energy", "renewable", "solar", "wind"], "Energy","Energy"),
    (["energy"],                        None,           "Energy"),
    # ── Healthcare ──────────────────────────────────────────────────────────
    (["biotechnology", "biotech"],      None,           "Biotech"),
    (["pharmaceutical", "pharma"],      None,           "Healthcare / Pharma"),
    (["medical", "hospital", "health"], None,           "Healthcare / Pharma"),
    # ── Technology ──────────────────────────────────────────────────────────
    (["fintech", "payment", "neobank"], None,           "FinTech"),
    (["software", "saas", "cloud"],     None,           "Technology"),
    (["semiconductor", "chip"],         None,           "Technology"),
    (["technology", "tech", "data"],    None,           "Technology"),
    # ── Financials ──────────────────────────────────────────────────────────
    (["bank", "banking"],               None,           "Banks"),
    (["insurance"],                     None,           "Insurance"),
    (["asset management", "fund management"], None,     "Financials"),
    (["financial"],                     None,           "Financials"),
    # ── Real Estate ─────────────────────────────────────────────────────────
    (["reit", "real estate"],           None,           "Real Estate (REIT)"),
    # ── Consumer ────────────────────────────────────────────────────────────
    (["retail", "consumer", "food", "beverage"], None,  "Consumer"),
    # ── Industrials ─────────────────────────────────────────────────────────
    (["industrial", "transport", "logistics"], None,    "Industrials"),
    (["construction", "engineering"],   None,           "Industrials"),
    # ── Telecoms / Media ────────────────────────────────────────────────────
    (["telecom", "telco", "communication", "media"], None, "Telco / Media"),
    # ── Utilities ───────────────────────────────────────────────────────────
    (["utility", "utilities", "water", "electricity"], None, "Utilities"),
    # ── Crypto ──────────────────────────────────────────────────────────────
    (["crypto", "digital asset", "defi"], None,         "Crypto Core"),
]


def infer_sector_label(sector: str, industry: str) -> str | None:
    """
    Map a GICS sector + industry string to a human-readable watchlist label name.

    Args:
        sector:   e.g. "Basic Materials", "Information Technology"
        industry: e.g. "Gold", "Software—Application", "Biotechnology"

    Returns:
        Label name string (e.g. "Gold", "FinTech") or None if no match.
    """
    s = (sector or "").lower().strip()
    i = (industry or "").lower().strip()
    combined = f"{s} {i}"

    for keywords, req_sector, label_name in _SECTOR_LABEL_RULES:
        # Check sector filter if specified
        if req_sector and req_sector.lower() not in s:
            continue
        # Check if any keyword matches
        if any(kw in combined for kw in keywords):
            return label_name

    return None


def get_asx200_metadata() -> dict[str, dict]:
    """
    Fetch current ASX200 constituents with names and sectors from Wikipedia.
    Returns dict of ticker -> {"name": str, "sector": str}
    """
    import io, requests as _req
    results = {}
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; AstraTrade/1.0; +https://github.com/anupamwagle/vcpilot)"}
        resp = _req.get(ASX200_WIKIPEDIA_URL, headers=headers, timeout=20)
        resp.raise_for_status()
        tables = pd.read_html(io.StringIO(resp.text))
        for tbl in tables:
            cols_lower = [str(c).lower() for c in tbl.columns]
            if "code" in cols_lower:
                code_col = next(c for c in tbl.columns if str(c).lower() == "code")
                comp_col = next((c for c in tbl.columns if str(c).lower() == "company"), None)
                sect_col = next((c for c in tbl.columns if str(c).lower() == "sector"), None)

                for _, row in tbl.iterrows():
                    code = row[code_col]
                    if not isinstance(code, str) or len(code.strip()) < 2:
                        continue
                    ticker = f"{code.strip().upper()}.AX"
                    name = str(row[comp_col]).strip() if comp_col is not None and pd.notna(row[comp_col]) else ""
                    sector = str(row[sect_col]).strip() if sect_col is not None and pd.notna(row[sect_col]) else ""
                    results[ticker] = {"name": name, "sector": sector}

                if len(results) > 100:
                    logger.info(f"Fetched metadata for {len(results)} ASX200 tickers from Wikipedia")
                    return results
    except Exception as e:
        logger.warning(f"Wikipedia ASX200 metadata fetch failed: {e}")
    return results


# ---------------------------------------------------------------------------
# Price & OHLCV
# ---------------------------------------------------------------------------

def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute moving averages, volume metrics, 52-week range, and ATR."""
    if df is None or df.empty:
        return df

    # Ensure standard names
    if "adj close" in df.columns and "adj_close" not in df.columns:
        df = df.rename(columns={"adj close": "adj_close"})

    # Compute moving averages
    for period_ma, col in [(10, "ma_10"), (21, "ma_21"), (50, "ma_50"),
                           (150, "ma_150"), (200, "ma_200")]:
        df[col] = df["close"].rolling(period_ma, min_periods=period_ma).mean()

    df["ma_200_prev"] = df["ma_200"].shift(1)

    # Volume metrics
    df["avg_vol_50"] = df["volume"].rolling(50, min_periods=20).mean()
    df["vol_ratio"] = df["volume"] / df["avg_vol_50"].replace(0, np.nan)

    # 52-week range
    df["high_52w"] = df["high"].rolling(252, min_periods=50).max()
    df["low_52w"]  = df["low"].rolling(252, min_periods=50).min()
    df["pct_from_52w_high"] = (df["close"] - df["high_52w"]) / df["high_52w"] * 100
    df["pct_from_52w_low"]  = (df["close"] - df["low_52w"]) / df["low_52w"] * 100

    # ATR (14-day)
    df["prev_close"] = df["close"].shift(1)
    df["tr"] = df[["high", "low", "prev_close"]].apply(
        lambda r: max(r["high"] - r["low"],
                      abs(r["high"] - r["prev_close"]),
                      abs(r["low"] - r["prev_close"])), axis=1
    )
    df["atr_14"] = df["tr"].rolling(14, min_periods=14).mean()

    return df


def _fetch_yf_df(ticker: str, period: str, interval: str) -> Optional[pd.DataFrame]:
    """Raw yfinance fetch + normalisation. Returns None if empty."""
    try:
        df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)
        if df.empty:
            return None
        df = df.reset_index()
        df.columns = [c.lower() for c in df.columns]
        df = df.rename(columns={"adj close": "adj_close"})
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df.sort_values("date").reset_index(drop=True)
        return df
    except Exception:
        return None


def get_price_history(
    ticker: str,
    period: str = "2y",
    interval: str = "1d",
) -> Optional[pd.DataFrame]:
    """
    Fetch daily OHLCV for a single ticker.
    For crypto -AUD tickers that have no yfinance data, falls back to the -USD
    pair so VCP screening can still run (price patterns are FX-agnostic).
    Returns DataFrame with indicators added, or None on failure.
    """
    try:
        df = _fetch_yf_df(ticker, period, interval)

        # Crypto AUD→USD fallback: many altcoins only have -USD pairs on yfinance
        if df is None and ticker.endswith("-AUD"):
            usd_ticker = ticker[:-4] + "-USD"
            df = _fetch_yf_df(usd_ticker, period, interval)
            if df is not None:
                logger.debug(f"Price history for {ticker}: using {usd_ticker} fallback (no -AUD data)")

        if df is None:
            logger.debug(f"No price data for {ticker}")
            return None

        df = _add_indicators(df)
        return df

    except Exception as e:
        logger.error(f"Price fetch failed for {ticker}: {e}")
        return None


def get_batch_prices(
    tickers: list[str],
    period: str = "2y",
) -> dict[str, pd.DataFrame]:
    """
    Fetch price history for multiple tickers efficiently.
    Returns dict of ticker → DataFrame.
    """
    results = {}
    # yfinance batch download
    try:
        raw = yf.download(
            tickers, period=period, interval="1d",
            auto_adjust=False, progress=False, group_by="ticker"
        )
    except Exception as e:
        logger.error(f"Batch download failed: {e}")
        raw = None

    if raw is None or raw.empty:
        # Fallback: individual fetches (includes AUD→USD fallback via get_price_history)
        for ticker in tickers:
            df = get_price_history(ticker, period=period)
            if df is not None:
                results[ticker] = df
            time.sleep(0.1)  # Be polite to yfinance
        return results

    # Parse batch response
    missing_aud = []
    for ticker in tickers:
        try:
            if len(tickers) == 1:
                df = raw.copy()
            else:
                df = raw[ticker].copy()
            df = df.dropna(how="all")
            if df.empty:
                # Track -AUD tickers that had no data for USD fallback below
                if ticker.endswith("-AUD"):
                    missing_aud.append(ticker)
                continue
            df = df.reset_index()
            df.columns = [c.lower() if isinstance(c, str) else c for c in df.columns]
            df["date"] = pd.to_datetime(df["date"]).dt.date
            df = df.sort_values("date").reset_index(drop=True)
            df = _add_indicators(df)
            results[ticker] = df
        except Exception as e:
            logger.debug(f"Batch parse failed for {ticker}: {e}")
            if ticker.endswith("-AUD"):
                missing_aud.append(ticker)

    # AUD→USD fallback for any crypto tickers that had no -AUD batch data
    if missing_aud:
        usd_tickers = [t[:-4] + "-USD" for t in missing_aud]
        logger.info(f"AUD→USD fallback for {len(missing_aud)} tickers: {usd_tickers[:5]}...")
        usd_results = get_batch_prices(usd_tickers, period=period)
        for aud_t, usd_t in zip(missing_aud, usd_tickers):
            if usd_t in usd_results:
                results[aud_t] = usd_results[usd_t]  # stored under original -AUD key

    return results


def get_batch_prices_rate_limited(
    tickers: list[str],
    period: str = "2y",
    batch_size: int = 50,
    sleep_secs: float = 1.0,
) -> dict[str, pd.DataFrame]:
    """
    Fetch price history for large universes (full ASX, S&P500) with rate-limited batching.
    Splits tickers into batches and sleeps between each to avoid yfinance throttling.
    """
    results: dict[str, pd.DataFrame] = {}
    total = len(tickers)
    for i in range(0, total, batch_size):
        batch = tickers[i:i + batch_size]
        logger.info(f"Fetching price batch {i//batch_size + 1}/{(total-1)//batch_size + 1} ({len(batch)} tickers)")
        batch_results = get_batch_prices(batch, period=period)
        results.update(batch_results)
        if i + batch_size < total:
            time.sleep(sleep_secs)
    return results


# ---------------------------------------------------------------------------
# Intraday price (used by check_entry_triggers every 15 min)
# ---------------------------------------------------------------------------

def _get_ir_live_price(ticker: str) -> dict | None:
    """
    Fetch live price from Independent Reserve public API (no auth, 0-delay).
    Returns the standard intraday price dict or None on failure.

    IR primary currency codes:
        BTC → XBT, ETH → ETH, XRP → XRP, LTC → LTC, etc.
    IR API doc: https://www.independentreserve.com/API
    """
    # Only handles AUD pairs from IR
    if not ticker.endswith("-AUD"):
        return None

    import requests as _req
    from datetime import datetime as _dt

    # Use the module-level IR_SYMBOL_MAP (kept in sync with IR's live coin list)
    base = ticker.replace("-AUD", "").upper()
    ir_code = IR_SYMBOL_MAP.get(base)
    if not ir_code:
        return None  # coin not listed on IR

    import time as _time
    url = (
        f"https://api.independentreserve.com/Public/GetMarketSummary"
        f"?primaryCurrencyCode={ir_code}&secondaryCurrencyCode=aud"
    )
    headers = {"User-Agent": "AstraTrade/1.0 (+https://github.com/anupamwagle/vcpilot)"}
    last_exc = None
    for attempt in range(1, 4):  # up to 3 attempts
        try:
            resp = _req.get(url, timeout=6, headers=headers)
            if resp.status_code != 200:
                logger.warning(
                    f"IR API attempt {attempt}: {resp.status_code} for {ticker} ({ir_code}): {resp.text[:120]}"
                )
                if resp.status_code == 400:
                    return None  # Invalid coin on IR — retrying won't help
                if attempt < 3:
                    _time.sleep(1.5 * attempt)
                    continue
                return None
            data = resp.json()
            last_price = data.get("LastPrice")
            if last_price is None:
                logger.warning(f"IR API missing LastPrice for {ticker}: {data}")
                return None
            day_vol = data.get("DayVolumeXbtInSecondaryCurrrency")  # AUD volume (note typo in IR API)
            return {
                "price": float(last_price),
                "volume": int(day_vol / float(last_price)) if day_vol and last_price else 0,
                "bid": float(data.get("CurrentHighestBidPrice") or last_price),
                "ask": float(data.get("CurrentLowestOfferPrice") or last_price),
                "data_source": "independentreserve",
                "delay_mins": 0,
                "bar_timestamp": _dt.utcnow(),
                "ok": True,
            }
        except Exception as e:
            last_exc = e
            logger.warning(
                f"IR live price attempt {attempt} failed for {ticker} ({ir_code}): {type(e).__name__}: {e}"
            )
            if attempt < 3:
                _time.sleep(1.5 * attempt)
    logger.error(f"IR live price gave up after 3 attempts for {ticker} ({ir_code}): {last_exc}")
    return None


def get_intraday_price(
    ticker: str,
    organization_id: int = None,
    asset_type: str = "EQUITY",
) -> dict:
    """
    Fetch the most recent intraday price for a ticker.

    Priority:
      1. Independent Reserve public API (crypto -AUD pairs, 0-delay, no auth)
         Only attempted if asset_type is 'CRYPTO'.
      2. IBKR real-time snapshot if connected (equities, 0-delay)
      3. yfinance 15-min interval data (~15-20 min delayed)
      4. EOD fallback (returns ok=False — caller should use last close)

    Returns:
      {"price": float, "volume": int, "bid": float|None, "ask": float|None,
       "data_source": str, "delay_mins": int, "bar_timestamp": datetime|None, "ok": bool}
    """
    from datetime import datetime as _dt

    # 1. Independent Reserve live API for IR crypto pairs (free, 0-delay)
    # Refined: only route here if asset_type is explicitly CRYPTO.
    if asset_type == "CRYPTO" and ticker.endswith("-AUD"):
        ir_result = _get_ir_live_price(ticker)
        if ir_result:
            return ir_result

    # 2. Try IBKR real-time if available and connected (equities only — crypto uses IR/ccxt)
    if organization_id is not None and asset_type != "CRYPTO":
        try:
            from app.broker.ibkr import IBKRBroker
            from app.config import settings as _s
            if not _s.ibkr_simulate:
                with IBKRBroker(organization_id=organization_id) as broker:
                    if broker.is_connected:
                        snap = broker.get_market_snapshot(ticker)
                        if snap and snap.get("last"):
                            return {
                                "price": float(snap["last"]),
                                "volume": int(snap.get("volume", 0)),
                                "bid": snap.get("bid"),
                                "ask": snap.get("ask"),
                                "data_source": "ibkr",
                                "delay_mins": 0,
                                "bar_timestamp": snap.get("timestamp"),
                                "ok": True,
                            }
        except Exception as e:
            logger.debug(f"IBKR intraday snapshot failed for {ticker}: {e}")

    # 3. Fall back to yfinance 15-min bars (~15-20 min delayed for most markets)
    try:
        stock = yf.Ticker(ticker)
        df = stock.history(period="2d", interval="15m", auto_adjust=False)
        if df is not None and not df.empty:
            df = df.reset_index()
            df.columns = [c.lower() for c in df.columns]
            latest = df.iloc[-1]
            bar_ts = pd.to_datetime(latest.get("datetime") or latest.get("date"))
            price  = float(latest.get("close") or latest.get("adj close") or 0)
            volume = int(latest.get("volume") or 0)
            if price > 0:
                return {
                    "price": price,
                    "volume": volume,
                    "bid": None,
                    "ask": None,
                    "data_source": "yfinance",
                    "delay_mins": 20,
                    "bar_timestamp": bar_ts.to_pydatetime() if bar_ts is not None else None,
                    "ok": True,
                }
    except Exception as e:
        logger.debug(f"yfinance intraday fetch failed for {ticker}: {e}")

    logger.warning(f"No intraday data for {ticker} — all sources failed")
    return {"price": None, "volume": None, "bid": None, "ask": None,
            "data_source": "eod_fallback", "delay_mins": None, "bar_timestamp": None, "ok": False}


# ---------------------------------------------------------------------------
# Relative Strength
# ---------------------------------------------------------------------------

def compute_rs_ratings(
    stock_prices: dict[str, pd.DataFrame],
    exchange_key: str = "ASX",
    benchmark_ticker: str = None,
    lookback_days: int = 252,
) -> dict[str, float]:
    """
    Compute Relative Strength rating (0–100 percentile) for each stock,
    scoped to the same exchange's universe.

    RS formula: (stock weighted 12m performance) ranked as percentile within peers.
    AstraTrade uses IBD RS Rating; we implement a compatible percentile rank.

    For small watchlist-only universes (< 20 stocks), RS is still computed but
    should be interpreted relative to the available set, not the full market.

    Args:
        stock_prices:     ticker → DataFrame (must include 'close' column)
        exchange_key:     used to select the correct benchmark index
        benchmark_ticker: override benchmark; defaults to EXCHANGE_BENCHMARKS[exchange_key]
        lookback_days:    performance window (default 252 = 1 year)
    """
    if benchmark_ticker is None:
        benchmark_ticker = EXCHANGE_BENCHMARKS.get(exchange_key, "^GSPC")

    try:
        stock_perfs: dict[str, float] = {}
        for ticker, df in stock_prices.items():
            perf = _compute_performance(df, lookback_days)
            if perf is not None:
                stock_perfs[ticker] = perf

        if not stock_perfs:
            return {}

        perfs_series = pd.Series(stock_perfs)
        rs_ratings = perfs_series.rank(pct=True) * 100
        return rs_ratings.round(1).to_dict()

    except Exception as e:
        logger.error(f"RS rating computation failed for {exchange_key}: {e}")
        return {}


def _compute_performance(df: pd.DataFrame, days: int) -> Optional[float]:
    """Weighted RS performance: 40% last 3m + 20% prior 3m + 20% prior 3m + 20% first 3m."""
    if len(df) < days:
        return None
    closes = df["close"].tail(days).values
    if len(closes) < days:
        return None
    q4 = closes[-1] / closes[max(-63, -len(closes))] - 1   # Last 3 months
    q3 = closes[max(-63, -len(closes))] / closes[max(-126, -len(closes))] - 1
    q2 = closes[max(-126, -len(closes))] / closes[max(-189, -len(closes))] - 1
    q1 = closes[max(-189, -len(closes))] / closes[0] - 1
    return (q4 * 0.40) + (q3 * 0.20) + (q2 * 0.20) + (q1 * 0.20)


# ---------------------------------------------------------------------------
# Fundamentals (yfinance quarterly financials)
# ---------------------------------------------------------------------------

def get_fundamentals(ticker: str) -> dict:
    """
    Fetch fundamental data from yfinance.
    Returns dict with eps_quarterly, revenue_quarterly, roe, net_margin, etc.
    """
    result = {
        "company_name": "",
        "sector": "",
        "industry": "",
        "eps_quarterly": [],
        "revenue_quarterly": [],
        "roe": None,
        "net_margin": None,
        "net_margin_prev": None,
        "inst_ownership_pct": None,
        "next_earnings_date": None,
    }

    try:
        stock = yf.Ticker(ticker)
        info = stock.info or {}

        # EPS quarterly (trailing 8 quarters)
        try:
            q_earnings = stock.quarterly_earnings
            if q_earnings is not None and not q_earnings.empty:
                result["eps_quarterly"] = q_earnings["EPS"].tolist()[:8]
        except Exception:
            pass

        # Revenue quarterly
        try:
            q_financials = stock.quarterly_financials
            if q_financials is not None and not q_financials.empty:
                if "Total Revenue" in q_financials.index:
                    result["revenue_quarterly"] = (
                        q_financials.loc["Total Revenue"].dropna().tolist()[:8]
                    )
        except Exception:
            pass

        # Company name — prefer longName, fall back to shortName
        long_name = info.get("longName") or info.get("shortName") or ""
        if long_name:
            result["company_name"] = long_name.strip()

        # Sector / industry
        result["sector"]   = info.get("sector") or ""
        result["industry"] = info.get("industry") or ""

        # ROE, margins from info
        result["roe"] = info.get("returnOnEquity")
        result["net_margin"] = info.get("profitMargins")
        result["inst_ownership_pct"] = info.get("heldPercentInstitutions")
        # Next earnings date
        try:
            cal = stock.calendar
            if cal is not None and not cal.empty:
                if "Earnings Date" in cal.index:
                    ed = cal.loc["Earnings Date"].iloc[0] if hasattr(cal.loc["Earnings Date"], "iloc") else cal.loc["Earnings Date"]
                    result["next_earnings_date"] = str(ed)[:10]
        except Exception:
            pass

    except Exception as e:
        logger.debug(f"Fundamentals fetch failed for {ticker}: {e}")

    return result
