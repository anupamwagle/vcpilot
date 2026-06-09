"""
VCPilot — Default Configuration Seeder
Seeds SystemConfig and ALL Minervini RuleConfig rows on first startup.
Safe to re-run: uses INSERT ... ON CONFLICT DO NOTHING.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger
from app.database import get_db
from app.models.config import SystemConfig, RuleConfig, RuleCategory, ConfigValueType
from app.models.account import Account, AccountTier, TierLevel
from app.config import settings


# =============================================================================
# SystemConfig defaults
# =============================================================================
SYSTEM_CONFIGS = [
    # --- Trading state ---
    dict(key="trading_paused",       value="false",   value_type="BOOLEAN",
         label="Trading Paused",     group="trading",
         description="Pause all new trade entries globally"),
    dict(key="last_market_regime",   value="UNKNOWN", value_type="STRING",
         label="Last Market Regime", group="system"),
    dict(key="last_regime_check",    value="",        value_type="STRING",
         label="Last Regime Check",  group="system"),
    dict(key="last_heartbeat",       value="",        value_type="STRING",
         label="Last Worker Heartbeat", group="system"),

    # --- Capital ---
    dict(key="working_capital_aud", value=str(settings.working_capital_env), value_type="FLOAT",
         label="Working Capital (AUD)", group="general",
         description="Working capital used for sizing and risk calculations"),
    dict(key="working_capital_currency", value="AUD", value_type="STRING",
         label="Working Capital Currency", group="general",
         description="Currency of the working capital (e.g. AUD, USD, USDT, BNB)"),

    # --- IBKR Configuration ---
    dict(key="ibkr_account", value=str(settings.ibkr_account_env), value_type="STRING",
         label="IBKR Account Number", group="ibkr",
         description="Paper account DU number or live account number from IBKR portal"),
    dict(key="ibkr_username", value=str(settings.ibkr_username_env), value_type="STRING",
         label="IBKR Username", group="ibkr", is_secret=True,
         description="IBKR account username used to log into Gateway"),
    dict(key="ibkr_password", value=str(settings.ibkr_password_env), value_type="STRING",
         label="IBKR Password", group="ibkr", is_secret=True,
         description="IBKR account password used to log into Gateway"),
    dict(key="ibkr_paper_mode", value="true" if settings.ibkr_paper_mode_env else "false", value_type="BOOLEAN",
         label="IBKR Paper Mode", group="ibkr",
         description="True for paper trading (port 4002), False for live trading (port 4001)"),

    # --- WhatsApp ---
    dict(key="whatsapp_enabled", value="true" if settings.whatsapp_enabled_env else "false", value_type="BOOLEAN",
         label="WhatsApp Alerts Enabled", group="whatsapp",
         description="Enable or disable all WhatsApp notifications and remote command handling"),
    dict(key="whatsapp_admin_number", value=str(settings.whatsapp_admin_number_env), value_type="STRING",
         label="WhatsApp Admin Number", group="whatsapp",
         description="Your WhatsApp phone number in international format (e.g. 61400000000, no + or spaces)"),

    # --- Notifications / Alert Channel ---
    dict(key="notification_channel", value="telegram", value_type="STRING",
         label="Notification Channel", group="whatsapp",
         description="Active communication channel ('whatsapp' or 'telegram')"),
    dict(key="telegram_enabled", value="true", value_type="BOOLEAN",
         label="Telegram Alerts Enabled", group="whatsapp",
         description="Enable or disable all Telegram notifications and remote command handling"),
    dict(key="telegram_bot_token", value="", value_type="STRING",
         label="Telegram Bot Token", group="whatsapp", is_secret=True,
         description="The Telegram Bot Token from @BotFather"),
    dict(key="telegram_chat_id", value="", value_type="STRING",
         label="Telegram Chat ID", group="whatsapp",
         description="The Telegram Chat ID to send alerts to"),

    # --- Data APIs ---
    dict(key="fmp_api_key", value=str(settings.fmp_api_key_env), value_type="STRING",
         label="FMP API Key", group="data", is_secret=True,
         description="Financial Modeling Prep API key for supplemental fundamental data"),

    # --- Timezone ---
    dict(key="org_timezone", value="UTC", value_type="STRING",
         label="Display Timezone", group="general",
         description="IANA timezone for displaying timestamps in the dashboard and reports "
                     "(e.g. UTC, Australia/Sydney, Asia/Singapore). Change to Australia/Sydney to see AEST times. "
                     "Beat schedules always run on AEST since ASX is in Sydney."),

    # --- Simulation & Time-Travel ---
    dict(key="mock_time_enabled", value="false", value_type="BOOLEAN",
         label="Mock Time Enabled", group="system",
         description="Enable global clock mocking for rule testing and data replaying"),
    dict(key="mock_current_time", value="", value_type="STRING",
         label="Mock Current Time", group="system",
         description="Simulated date and time in YYYY-MM-DD HH:MM:SS format to overwrite system clock"),
    dict(key="ibkr_simulate", value="false", value_type="BOOLEAN",
         label="IBKR Simulation Mode", group="system",
         description="When true, orders are simulated locally without sending to IBKR Gateway"),
    dict(key="mock_market_regime", value="BULL", value_type="STRING",
         label="Mock Market Regime", group="system",
         description="Simulated market regime shown when Mock Time is enabled (BULL / CAUTION / BEAR). Never overwrites the evaluated last_market_regime."),

    # --- Multi-market / Exchange config ---
    dict(key="active_exchanges", value="ASX,CRYPTO_INDEPENDENTRESERVE", value_type="STRING",
         label="Active Exchanges", group="trading",
         description="Comma-separated list of exchange keys this org trades on. "
                     "Allowed values from enabled ExchangeConfig rows, e.g. 'ASX', 'ASX,CRYPTO_INDEPENDENTRESERVE'. "
                     "Super admin must enable an exchange globally before orgs can activate it."),

    dict(key="ibkr_account_usd", value="", value_type="STRING",
         label="IBKR USD Account Number", group="ibkr",
         description="IBKR account number used for USD-denominated trades (US equities). "
                     "May be the same account as the AUD account for multi-currency IBKR accounts. "
                     "Leave blank to use the same account as ibkr_account."),

    dict(key="fx_audusd_override", value="", value_type="STRING",
         label="AUD/USD Rate Override", group="trading",
         description="Manual AUD/USD exchange rate override for position sizing (e.g. '0.65'). "
                     "Leave blank to use the live rate fetched from yfinance (AUDUSD=X). "
                     "Useful for backtesting or when live FX feed is unavailable."),

    # --- Crypto exchange credentials (per org) ---
    dict(key="crypto_exchange_key", value="CRYPTO_INDEPENDENTRESERVE", value_type="STRING",
         label="Crypto Exchange", group="crypto",
         description="Active crypto exchange key for this org (e.g. 'CRYPTO_INDEPENDENTRESERVE'). "
                     "Must be an enabled ExchangeConfig row. Set via super admin exchange management."),

    dict(key="crypto_api_key", value="", value_type="STRING",
         label="Crypto API Key", group="crypto", is_secret=True,
         description="API key for the org's crypto exchange account. "
                     "Obtain from the exchange's API management page. "
                     "Requires read + trade permissions (no withdrawal permissions needed)."),

    dict(key="crypto_api_secret", value="", value_type="STRING",
         label="Crypto API Secret", group="crypto", is_secret=True,
         description="API secret for the org's crypto exchange account."),

    dict(key="crypto_testnet", value="false", value_type="BOOLEAN",
         label="Crypto Testnet Mode", group="crypto",
         description="Use the exchange's testnet/sandbox for crypto orders. "
                     "True = no real funds at risk. Set to False only when ready to trade live."),

    # Per-exchange regime tracking (written by evaluate_market_regime_task, read by dashboard)
    dict(key="last_market_regime_ASX",    value="UNKNOWN", value_type="STRING",
         label="ASX Market Regime",    group="system"),
    dict(key="last_market_regime_NYSE",   value="UNKNOWN", value_type="STRING",
         label="NYSE Market Regime",   group="system"),
    dict(key="last_market_regime_NASDAQ", value="UNKNOWN", value_type="STRING",
         label="NASDAQ Market Regime", group="system"),
]


# =============================================================================
# RuleConfig defaults — every Minervini rule
# =============================================================================
RULE_CONFIGS = [

    # =========================================================================
    # TREND TEMPLATE — applies to BOTH equities and crypto
    # =========================================================================
    dict(rule_id="trend_price_above_200ma",
         category="TREND_TEMPLATE", sort_order=10, asset_types="BOTH",
         label="Price > 200-day MA",
         description="Stock price must be above the 200-day moving average.",
         minervini_ref="Trend Template Criterion 1",
         enabled_globally=True, is_mandatory=False, threshold=None),

    dict(rule_id="trend_price_above_150ma",
         category="TREND_TEMPLATE", sort_order=11,
         label="Price > 150-day MA",
         description="Stock price must be above the 150-day moving average.",
         minervini_ref="Trend Template Criterion 2",
         enabled_globally=True, is_mandatory=False, threshold=None),

    dict(rule_id="trend_ma150_above_ma200",
         category="TREND_TEMPLATE", sort_order=12,
         label="150-day MA > 200-day MA",
         description="The 150-day MA must be above the 200-day MA.",
         minervini_ref="Trend Template Criterion 3",
         enabled_globally=True, is_mandatory=False, threshold=None),

    dict(rule_id="trend_ma200_trending_up",
         category="TREND_TEMPLATE", sort_order=13,
         label="200-day MA trending up",
         description="200MA must be higher today than N trading days ago.",
         minervini_ref="Trend Template Criterion 4",
         threshold=21.0, threshold_label="Lookback period (trading days)",
         threshold_min=10.0, threshold_max=63.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="trend_ma50_above_ma150_200",
         category="TREND_TEMPLATE", sort_order=14,
         label="50-day MA > 150MA and 200MA",
         description="50MA must be above both the 150MA and 200MA.",
         minervini_ref="Trend Template Criterion 5",
         enabled_globally=True, is_mandatory=False, threshold=None),

    dict(rule_id="trend_price_above_ma50",
         category="TREND_TEMPLATE", sort_order=15,
         label="Price > 50-day MA",
         description="Stock price must be above the 50-day moving average.",
         minervini_ref="Trend Template Criterion 6",
         enabled_globally=True, is_mandatory=False, threshold=None),

    dict(rule_id="trend_pct_above_52w_low",
         category="TREND_TEMPLATE", sort_order=16,
         label="Price ≥ 30% above 52-week low",
         description="Current price must be at least 30% above the 52-week low.",
         minervini_ref="Trend Template Criterion 7",
         threshold=30.0, threshold_label="Min % above 52-week low",
         threshold_min=20.0, threshold_max=50.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="trend_pct_below_52w_high",
         category="TREND_TEMPLATE", sort_order=17,
         label="Price within 25% of 52-week high",
         description="Current price must be within 25% of the 52-week high (closer = better). For crypto, raise this to 65–75% via Admin → Rules to account for wider market swings.",
         minervini_ref="Trend Template Criterion 8",
         threshold=25.0, threshold_label="Max % below 52-week high",
         threshold_min=10.0, threshold_max=80.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="trend_rs_rating_min",
         category="TREND_TEMPLATE", sort_order=18,
         label="Relative Strength ≥ 70",
         description="Stock RS rating must be ≥ 70th percentile vs ASX200. Equity only — not applicable to crypto.",
         minervini_ref="RS Rating requirement (equivalent to IBD RS ≥ 70)",
         threshold=70.0, threshold_label="Min RS percentile (0–100)",
         threshold_min=50.0, threshold_max=90.0,
         asset_types="EQUITY",
         enabled_globally=True, is_mandatory=False),

    # =========================================================================
    # FUNDAMENTAL CRITERIA
    # =========================================================================
    dict(rule_id="fundamental_eps_growth_recent",
         category="FUNDAMENTAL", sort_order=20,
         label="EPS Growth ≥ 25% (recent quarter YoY)",
         description="Most recent quarter EPS must be ≥ 25% higher than same quarter last year.",
         minervini_ref="SEPA Fundamental: EPS growth 25%+",
         threshold=25.0, threshold_label="Min EPS growth %",
         threshold_min=10.0, threshold_max=100.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="fundamental_eps_growth_accel",
         category="FUNDAMENTAL", sort_order=21,
         label="EPS Acceleration",
         description="EPS growth rate must be accelerating — Q1 growth > Q2 growth.",
         minervini_ref="SEPA Fundamental: Earnings acceleration",
         enabled_globally=True, is_mandatory=False, threshold=None),

    dict(rule_id="fundamental_eps_growth_annual",
         category="FUNDAMENTAL", sort_order=22,
         label="Annual EPS Growth ≥ 25%",
         description="TTM EPS growth vs prior year must be ≥ 25%.",
         minervini_ref="SEPA Fundamental: 3-year EPS growth",
         threshold=25.0, threshold_label="Min annual EPS growth %",
         threshold_min=10.0, threshold_max=100.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="fundamental_sales_growth",
         category="FUNDAMENTAL", sort_order=23,
         label="Revenue Growth ≥ 25% (recent quarter)",
         description="Most recent quarter revenue must be ≥ 25% higher YoY.",
         minervini_ref="SEPA Fundamental: Sales growth 25%+",
         threshold=25.0, threshold_label="Min revenue growth %",
         threshold_min=10.0, threshold_max=100.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="fundamental_roe",
         category="FUNDAMENTAL", sort_order=24,
         label="Return on Equity ≥ 17%",
         description="ROE must be ≥ 17% (above-average capital efficiency).",
         minervini_ref="SEPA Fundamental: ROE ≥ 17%",
         threshold=17.0, threshold_label="Min ROE %",
         threshold_min=10.0, threshold_max=40.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="fundamental_profit_margin",
         category="FUNDAMENTAL", sort_order=25,
         label="Net Profit Margin positive and improving",
         description="Net margin must be positive and higher than prior period.",
         minervini_ref="SEPA Fundamental: Improving profit margins",
         enabled_globally=True, is_mandatory=False, threshold=None),

    dict(rule_id="fundamental_institutional_own",
         category="FUNDAMENTAL", sort_order=26,
         label="Institutional Ownership present",
         description="Some institutional ownership required (min 5%, max 80%).",
         minervini_ref="SEPA Fundamental: Institutional sponsorship",
         threshold=5.0, threshold_label="Min institutional ownership %",
         threshold_min=1.0, threshold_max=20.0,
         enabled_globally=True, is_mandatory=False),

    # =========================================================================
    # VCP (Volatility Contraction Pattern)
    # =========================================================================
    dict(rule_id="vcp_min_contractions",
         category="VCP", sort_order=30,
         label="VCP: Minimum 3 contractions",
         description="Must have at least 3 successive tightening price contractions.",
         minervini_ref="VCP: Minimum contraction count",
         threshold=3.0, threshold_label="Min contractions",
         threshold_min=2.0, threshold_max=6.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="vcp_base_weeks",
         category="VCP", sort_order=31,
         label="VCP: Base length 3–52 weeks",
         description="The base pattern must be between 3 and 52 weeks long.",
         minervini_ref="VCP: Base duration",
         threshold=3.0, threshold_label="Min base weeks",
         threshold_min=2.0, threshold_max=10.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="vcp_max_weeks",
         category="VCP", sort_order=32,
         label="VCP: Max base length",
         description="Upper limit on base length in weeks.",
         threshold=52.0, threshold_label="Max base weeks",
         threshold_min=20.0, threshold_max=104.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="vcp_volume_dry_up",
         category="VCP", sort_order=33,
         label="VCP: Volume dry-up on final contraction",
         description="Volume on the final tight contraction must be ≤ 50% of 50-day average.",
         minervini_ref="VCP: Volume diminishes to lowest point",
         threshold=50.0, threshold_label="Max volume as % of 50d avg",
         threshold_min=20.0, threshold_max=80.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="vcp_breakout_volume",
         category="VCP", sort_order=34,
         label="VCP: Breakout volume ≥ 150% of average",
         description="Breakout day volume must be at least 150% of the 50-day average.",
         minervini_ref="VCP: Volume expansion on breakout",
         threshold=150.0, threshold_label="Min breakout volume % of 50d avg",
         threshold_min=100.0, threshold_max=300.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="vcp_max_extension",
         category="VCP", sort_order=35,
         label="VCP: Max chase limit 5%",
         description="Do not buy if price is more than 5% above the pivot point (don't chase).",
         minervini_ref="VCP: Buy within 5% of pivot",
         threshold=5.0, threshold_label="Max % above pivot to still enter",
         threshold_min=2.0, threshold_max=10.0,
         enabled_globally=True, is_mandatory=False),

    # =========================================================================
    # MARKET REGIME
    # =========================================================================
    dict(rule_id="regime_index_above_200ma",
         category="MARKET_REGIME", sort_order=40,
         label="Market Regime: ASX200 above 200MA",
         description="ASX200 index must be above its 200-day moving average.",
         minervini_ref="Market direction: Never fight the tape",
         enabled_globally=True, is_mandatory=False, threshold=None),

    dict(rule_id="regime_pct_stocks_above_200ma",
         category="MARKET_REGIME", sort_order=41,
         label="Market Regime: ≥ 60% of stocks above 200MA",
         description="At least 60% of ASX200 stocks must be above their 200-day MA.",
         minervini_ref="Market breadth confirmation",
         threshold=60.0, threshold_label="Min % of stocks above 200MA",
         threshold_min=40.0, threshold_max=80.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="regime_distribution_days",
         category="MARKET_REGIME", sort_order=42,
         label="Market Regime: ≤ 4 distribution days in 25 sessions",
         description="No more than 4 distribution days (index down on higher volume) in last 25 sessions.",
         minervini_ref="Distribution day count — IBD Market Pulse equivalent",
         threshold=4.0, threshold_label="Max distribution days",
         threshold_min=2.0, threshold_max=8.0,
         enabled_globally=True, is_mandatory=False),

    # =========================================================================
    # ENTRY RULES
    # =========================================================================
    dict(rule_id="entry_sector_leadership",
         category="ENTRY", sort_order=50,
         label="Sector leadership: stock in top 20% RS sectors",
         description="Only buy stocks in leading sectors (top 20% by relative strength).",
         minervini_ref="Buy the leader in the leading sector",
         enabled_globally=True, is_mandatory=False,
         threshold=20.0, threshold_label="Top N% sectors to qualify"),

    dict(rule_id="entry_no_extension",
         category="ENTRY", sort_order=51,
         label="No buying extended stocks (up 100%+ without base)",
         description="Avoid stocks that have already made a large move without forming a base.",
         minervini_ref="Don't buy extended stocks — wait for a new base",
         enabled_globally=True, is_mandatory=False,
         threshold=100.0, threshold_label="Max % run from last base before skipping"),

    # =========================================================================
    # DEFENSIVE EXIT RULES
    # =========================================================================
    dict(rule_id="exit_stop_loss",
         category="EXIT_DEFENSIVE", sort_order=60,
         label="Hard Stop Loss (MANDATORY)",
         description="Exit immediately when price hits the stop price. This rule CANNOT be disabled.",
         minervini_ref="Cut losses short — never let a loss exceed stop",
         enabled_globally=True, is_mandatory=True, threshold=None),

    dict(rule_id="exit_time_stop",
         category="EXIT_DEFENSIVE", sort_order=61,
         label="Time Stop: Exit if not up 10% in 3 weeks",
         description="If a position hasn't moved ≥ 10% in the configured timeframe, exit.",
         minervini_ref="Time stop — capital tied up in non-performing stocks",
         threshold=10.0, threshold_label="Min gain % required",
         threshold_min=5.0, threshold_max=25.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="exit_time_stop_weeks",
         category="EXIT_DEFENSIVE", sort_order=62,
         label="Time Stop: Weeks before triggering",
         description="Number of weeks to wait before applying the time stop.",
         threshold=3.0, threshold_label="Weeks",
         threshold_min=2.0, threshold_max=8.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="exit_earnings_avoid",
         category="EXIT_DEFENSIVE", sort_order=63,
         label="Exit N days before earnings",
         description="Exit positions N trading days before the next earnings date.",
         minervini_ref="Never hold through earnings — binary risk event",
         threshold=2.0, threshold_label="Trading days before earnings to exit",
         threshold_min=1.0, threshold_max=5.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="exit_break_below_50ma",
         category="EXIT_DEFENSIVE", sort_order=64,
         label="Exit on break below 50MA on volume",
         description="Exit when price closes below the 50MA on above-average volume.",
         minervini_ref="Trend break signal",
         enabled_globally=True, is_mandatory=False, threshold=None),

    # =========================================================================
    # OFFENSIVE EXIT RULES
    # =========================================================================
    dict(rule_id="exit_profit_target_1",
         category="EXIT_OFFENSIVE", sort_order=70,
         label="Partial exit at 20% profit",
         description="Sell 50% of position at 20% gain to lock in profits.",
         minervini_ref="Sell into strength — take partial profits at 20-25%",
         threshold=20.0, threshold_label="First profit target %",
         threshold_min=15.0, threshold_max=35.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="exit_profit_target_1_sell_pct",
         category="EXIT_OFFENSIVE", sort_order=71,
         label="Partial exit 1: % of position to sell",
         threshold=50.0, threshold_label="% of position to sell at target 1",
         threshold_min=25.0, threshold_max=100.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="exit_profit_target_2",
         category="EXIT_OFFENSIVE", sort_order=72,
         label="Full exit at 40% profit",
         description="Exit remaining position at 40% gain.",
         minervini_ref="Take full profits at extended targets",
         threshold=40.0, threshold_label="Second profit target %",
         threshold_min=30.0, threshold_max=100.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="exit_climax_top",
         category="EXIT_OFFENSIVE", sort_order=73,
         label="Climax top detection",
         description="Exit on exhaustion gap-up: volume ≥ 250% of avg + wide range day after big run.",
         minervini_ref="Climax top — sell into euphoria",
         threshold=250.0, threshold_label="Min volume % of avg for climax",
         threshold_min=150.0, threshold_max=500.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="exit_climax_top_min_run",
         category="EXIT_OFFENSIVE", sort_order=74,
         label="Climax top: min prior run to qualify",
         threshold=50.0, threshold_label="Min % gain before checking climax",
         threshold_min=30.0, threshold_max=200.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="exit_parabolic_move",
         category="EXIT_OFFENSIVE", sort_order=75,
         label="Parabolic move: 3 consecutive weeks up ≥ 5%",
         description="If stock gains ≥ 5% per week for 3+ consecutive weeks, take partial profits.",
         minervini_ref="Parabolic move — unsustainable pace",
         threshold=5.0, threshold_label="Min weekly gain % for parabolic",
         threshold_min=3.0, threshold_max=15.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="exit_three_weeks_tight",
         category="EXIT_OFFENSIVE", sort_order=76,
         label="3-Weeks-Tight: hold through coiling",
         description="3 consecutive weekly closes within 1.5% = coiling for next move. HOLD.",
         minervini_ref="3-weeks-tight pattern — do not sell",
         threshold=1.5, threshold_label="Max weekly close spread %",
         threshold_min=0.5, threshold_max=3.0,
         enabled_globally=True, is_mandatory=False),

    # =========================================================================
    # POSITION SIZING
    # =========================================================================
    dict(rule_id="risk_max_pct_per_trade",
         category="POSITION_SIZING", sort_order=80,
         label="Max risk per trade: 2% of capital",
         description="Never risk more than 2% of total capital on a single trade.",
         minervini_ref="Position sizing: 1-2% risk per trade rule",
         threshold=2.0, threshold_label="Max risk % per trade",
         threshold_min=0.5, threshold_max=5.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="risk_max_position_pct",
         category="POSITION_SIZING", sort_order=81,
         label="Max position size: 30% of capital",
         description="No single position can exceed 30% of total capital.",
         threshold=30.0, threshold_label="Max position size % of capital",
         threshold_min=10.0, threshold_max=50.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="pyramid_min_profit_pct",
         category="POSITION_SIZING", sort_order=82,
         label="Pyramid: add only if up ≥ 2%",
         description="Add to a position only when it is already up at least 2%.",
         minervini_ref="Never add to a loser — pyramid only winners",
         threshold=2.0, threshold_label="Min profit % before adding",
         threshold_min=1.0, threshold_max=10.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="pyramid_max_count",
         category="POSITION_SIZING", sort_order=83,
         label="Pyramid: max 2 add-ons",
         description="Maximum number of pyramid add-on positions per stock.",
         minervini_ref="Controlled pyramiding",
         threshold=2.0, threshold_label="Max add-on positions",
         threshold_min=1.0, threshold_max=4.0,
         enabled_globally=True, is_mandatory=False),

    # =========================================================================
    # PORTFOLIO RULES
    # =========================================================================
    dict(rule_id="portfolio_max_positions",
         category="PORTFOLIO", sort_order=90,
         label="Max open positions: 5",
         description="Maximum number of simultaneously open positions.",
         threshold=5.0, threshold_label="Max open positions",
         threshold_min=1.0, threshold_max=20.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="portfolio_max_heat_pct",
         category="PORTFOLIO", sort_order=91, asset_types="BOTH",
         label="Max portfolio heat: 15%",
         description="Total risk across all open positions must not exceed 15% of capital.",
         minervini_ref="Portfolio heat rule",
         threshold=15.0, threshold_label="Max portfolio heat %",
         threshold_min=5.0, threshold_max=30.0,
         enabled_globally=True, is_mandatory=False),

    # =========================================================================
    # CRYPTO-SPECIFIC RULES (asset_types="CRYPTO" — ignored for equities)
    # =========================================================================

    dict(rule_id="crypto_btc_regime",
         category="CRYPTO", sort_order=100, asset_types="CRYPTO",
         label="Crypto Regime: BTC above 50MA",
         description="Bitcoin (BTC-USD) must be above its 50-day moving average. "
                     "BTC is the primary market indicator for all crypto assets — "
                     "trading altcoins when BTC is below its 50MA is fighting the trend. "
                     "Used as the crypto equivalent of the equity market regime filter.",
         minervini_ref="Crypto-specific: Never trade altcoins against BTC trend",
         enabled_globally=True, is_mandatory=False, threshold=None),

    dict(rule_id="crypto_market_cap_min",
         category="CRYPTO", sort_order=101, asset_types="CRYPTO",
         label="Minimum market cap: $100M USD",
         description="Crypto asset must have a minimum market capitalisation of $100M USD. "
                     "Low-cap coins can be manipulated to produce false VCP patterns and have "
                     "insufficient liquidity for reliable entry/exit. Applies to all crypto assets "
                     "except BTC and ETH which are always treated as qualifying.",
         threshold=100_000_000.0, threshold_label="Min market cap (USD)",
         threshold_min=10_000_000.0, threshold_max=1_000_000_000.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="crypto_volume_min_24h",
         category="CRYPTO", sort_order=102, asset_types="CRYPTO",
         label="Minimum 24h volume: $5M USD",
         description="24-hour trading volume must exceed $5M USD to ensure there is enough "
                     "liquidity to enter and exit positions without significant slippage. "
                     "Unlike equities (session volume), crypto uses rolling 24h volume.",
         threshold=5_000_000.0, threshold_label="Min 24h volume (USD)",
         threshold_min=1_000_000.0, threshold_max=100_000_000.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="crypto_stop_width_pct",
         category="CRYPTO", sort_order=103, asset_types="CRYPTO",
         label="Crypto min stop distance: 10%",
         description="Stops on crypto assets must be placed at least 10% below the pivot price. "
                     "Crypto assets are 3-5× more volatile than equities — the default 5-8% "
                     "equity stop is too tight and will cause premature stop-outs on normal "
                     "intraday noise. Overrides the equity stop distance calculation for crypto. "
                     "Position size is automatically reduced to maintain the 2% capital risk rule.",
         threshold=10.0, threshold_label="Min stop distance below pivot (%)",
         threshold_min=5.0, threshold_max=25.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="crypto_max_risk_pct",
         category="CRYPTO", sort_order=104, asset_types="CRYPTO",
         label="Crypto max risk per trade: 1%",
         description="Maximum capital at risk per crypto trade is 1% (vs 2% for equities). "
                     "The higher volatility and wider stops on crypto mean a 2% risk rule produces "
                     "very small position sizes. This rule caps the risk explicitly and independently "
                     "of the equity risk_max_pct_per_trade rule, allowing different limits per asset type. "
                     "Overrides risk_max_pct_per_trade for CRYPTO assets.",
         threshold=1.0, threshold_label="Max risk % per crypto trade",
         threshold_min=0.25, threshold_max=3.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="crypto_vcp_contraction_pct",
         category="CRYPTO", sort_order=105, asset_types="CRYPTO",
         label="Crypto VCP: contraction size ≥ 15%",
         description="Each VCP contraction in a crypto base must be at least 15% in depth "
                     "(vs ~10% for equities). Crypto markets are inherently more volatile so "
                     "shallow contractions are less significant. A 15%+ contraction followed by "
                     "tightening is a more meaningful consolidation signal in crypto markets.",
         threshold=15.0, threshold_label="Min contraction depth (%)",
         threshold_min=8.0, threshold_max=40.0,
         enabled_globally=True, is_mandatory=False),

    # ── Enhanced Wall St-grade crypto rules ───────────────────────────────
    dict(rule_id="crypto_rsi_momentum",
         category="CRYPTO", sort_order=106, asset_types="CRYPTO",
         label="RSI(14) ≥ 50 — momentum confirmation",
         description="RSI(14) above 50 confirms price is in an upward momentum phase. "
                     "Entries below RSI 50 fight the intermediate trend. This filter eliminates "
                     "counter-trend trades and focuses capital on assets already showing buyers "
                     "in control. Threshold adjustable: 50 = neutral, 55 = stronger requirement.",
         threshold=50.0, threshold_label="Minimum RSI(14)",
         threshold_min=40.0, threshold_max=70.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="crypto_macd_bullish",
         category="CRYPTO", sort_order=107, asset_types="CRYPTO",
         label="MACD bullish (12/26/9) — histogram positive",
         description="MACD line (12-period EMA minus 26-period EMA) must be above the signal "
                     "line (9-period EMA of MACD) with a positive histogram. This confirms "
                     "short-term momentum is aligned with the intermediate uptrend. Particularly "
                     "effective on crypto 24/7 markets where momentum shifts are faster than equities.",
         threshold=0.0, threshold_label="Min MACD histogram (0 = above signal only)",
         threshold_min=0.0, threshold_max=0.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="crypto_volume_surge",
         category="CRYPTO", sort_order=108, asset_types="CRYPTO",
         label="Volume surge ≥ 1.5× 20-day average",
         description="Breakout volume must be at least 1.5× the 20-day average volume. "
                     "This is the core Minervini volume confirmation principle — price breakouts "
                     "without volume are low-conviction. For crypto, higher volume surges (2×+) "
                     "indicate institutional or major retail accumulation.",
         threshold=1.5, threshold_label="Volume surge multiplier (×20-day avg)",
         threshold_min=1.0, threshold_max=5.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="crypto_min_rr_ratio",
         category="CRYPTO", sort_order=109, asset_types="CRYPTO",
         label="Min risk/reward ratio ≥ 2.5:1",
         description="Each trade setup must offer at least 2.5× reward relative to risk "
                     "(measured from entry to first target vs entry to stop). Professional traders "
                     "typically require minimum 3:1 R/R. This rule prevents taking poor-quality "
                     "setups where the reward does not justify the risk taken.",
         threshold=2.5, threshold_label="Min R/R ratio",
         threshold_min=1.5, threshold_max=5.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="crypto_btc_relative_strength",
         category="CRYPTO", sort_order=110, asset_types="CRYPTO",
         label="RS vs BTC (50d) ≥ 0% — must match or beat Bitcoin",
         description="Non-BTC crypto assets must show at least equal 50-day relative strength "
                     "vs Bitcoin. Assets underperforming BTC are in a relative downtrend — money "
                     "is rotating out. Focus capital on assets showing leadership: if ETH, SOL, or "
                     "any altcoin is lagging BTC, skip it. Threshold: 0% = must match BTC; "
                     "5% = must outperform BTC by 5% over 50 days.",
         threshold=0.0, threshold_label="Min 50d RS vs BTC (%)",
         threshold_min=-10.0, threshold_max=20.0,
         enabled_globally=True, is_mandatory=False),
]

# Rules that apply to EQUITY only — used in migrate_saas.py to backfill asset_types
EQUITY_ONLY_RULES = {
    "fundamental_eps_growth_recent",
    "fundamental_eps_growth_accel",
    "fundamental_eps_growth_annual",
    "fundamental_sales_growth",
    "fundamental_roe",
    "fundamental_profit_margin",
    "fundamental_institutional_own",
    "regime_pct_stocks_above_200ma",    # breadth meaningless for crypto
    "regime_distribution_days",         # distribution days meaningless for 24/7 crypto
    "entry_sector_leadership",          # crypto has different sector dynamics
    "exit_earnings_avoid",              # no earnings events in crypto
    "trend_rs_rating_min",              # RS rating calculated vs ASX200 — meaningless for crypto
}


# =============================================================================
# Account Tiers
# =============================================================================
TIERS = [
    dict(level=TierLevel.STARTER,  label="Starter",  universe="ASX200", max_positions=3,
         max_risk_pct_per_trade=1.0, max_portfolio_heat_pct=8.0, allow_pyramid=False),
    dict(level=TierLevel.STANDARD, label="Standard", universe="ASX300", max_positions=5,
         max_risk_pct_per_trade=1.5, max_portfolio_heat_pct=12.0, allow_pyramid=True),
    dict(level=TierLevel.ADVANCED, label="Advanced", universe="ALLASX", max_positions=10,
         max_risk_pct_per_trade=2.0, max_portfolio_heat_pct=20.0, allow_pyramid=True),
    dict(level=TierLevel.ADMIN,    label="Admin",    universe="ALLASX", max_positions=20,
         max_risk_pct_per_trade=3.0, max_portfolio_heat_pct=25.0, allow_pyramid=True,
         allow_manual_override=True),
]


def seed_all():
    """Seed all configuration tables. Safe to run multiple times."""
    with get_db() as db:
        # --- Tiers ---
        for tier_data in TIERS:
            existing = db.query(AccountTier).filter(
                AccountTier.level == tier_data["level"]
            ).first()
            if not existing:
                db.add(AccountTier(**tier_data))
                logger.info(f"Seeded tier: {tier_data['level']}")

        db.flush()

        # --- Default account (ADMIN tier) ---
        admin_tier = db.query(AccountTier).filter(
            AccountTier.level == TierLevel.ADMIN
        ).first()
        if admin_tier:
            existing_account = db.query(Account).first()
            if not existing_account:
                db.add(Account(
                    name="Primary",
                    tier_id=admin_tier.id,
                    is_active=True,
                    is_paper=True,
                    capital_aud=5000.00,
                ))
                logger.info("Seeded default account (paper, $5000, ADMIN tier)")

        # --- Clean up old/removed keys ---
        removed_keys = ["trading_universe", "base_currency", "account_capital_aud"]
        for rk in removed_keys:
            db.query(SystemConfig).filter(SystemConfig.key == rk).delete()
        db.flush()

        # --- SystemConfig ---
        for cfg_data in SYSTEM_CONFIGS:
            existing = db.query(SystemConfig).filter(
                SystemConfig.key == cfg_data["key"]
            ).first()
            if not existing:
                db.add(SystemConfig(**{
                    k: v for k, v in cfg_data.items()
                    if k in ("key", "value", "value_type", "label", "description", "group", "is_secret")
                }))
                logger.debug(f"Seeded config: {cfg_data['key']}")

        # --- RuleConfig ---
        for rule_data in RULE_CONFIGS:
            existing = db.query(RuleConfig).filter(
                RuleConfig.rule_id == rule_data["rule_id"],
                RuleConfig.organization_id == None
            ).first()
            if not existing:
                fields = {k: v for k, v in rule_data.items()
                          if k in RuleConfig.__table__.columns.keys()}
                # Apply EQUITY_ONLY backfill if asset_types not explicitly set
                if "asset_types" not in fields:
                    fields["asset_types"] = "EQUITY" if rule_data["rule_id"] in EQUITY_ONLY_RULES else "BOTH"
                db.add(RuleConfig(**fields))
                logger.debug(f"Seeded rule: {rule_data['rule_id']}")
            else:
                # Update asset_types on existing rules if still at default
                if hasattr(existing, "asset_types") and existing.asset_types == "BOTH":
                    correct = "EQUITY" if rule_data["rule_id"] in EQUITY_ONLY_RULES else "BOTH"
                    if correct != "BOTH":
                        existing.asset_types = correct

    logger.info(f"Seed complete: {len(SYSTEM_CONFIGS)} configs, {len(RULE_CONFIGS)} rules, {len(TIERS)} tiers")


if __name__ == "__main__":
    seed_all()
