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
    dict(key="weekly_injection_aud", value=str(settings.weekly_capital_injection_env), value_type="FLOAT",
         label="Weekly Capital Injection (AUD)", group="capital",
         description="Weekly capital injected into trading calculations"),

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

    # --- Data APIs ---
    dict(key="fmp_api_key", value=str(settings.fmp_api_key_env), value_type="STRING",
         label="FMP API Key", group="data", is_secret=True,
         description="Financial Modeling Prep API key for supplemental fundamental data"),
]


# =============================================================================
# RuleConfig defaults — every Minervini rule
# =============================================================================
RULE_CONFIGS = [

    # =========================================================================
    # TREND TEMPLATE (Minervini's 8 criteria — all mandatory by default)
    # =========================================================================
    dict(rule_id="trend_price_above_200ma",
         category="TREND_TEMPLATE", sort_order=10,
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
         description="Current price must be within 25% of the 52-week high (closer = better).",
         minervini_ref="Trend Template Criterion 8",
         threshold=25.0, threshold_label="Max % below 52-week high",
         threshold_min=10.0, threshold_max=40.0,
         enabled_globally=True, is_mandatory=False),

    dict(rule_id="trend_rs_rating_min",
         category="TREND_TEMPLATE", sort_order=18,
         label="Relative Strength ≥ 70",
         description="Stock RS rating must be ≥ 70th percentile vs ASX200.",
         minervini_ref="RS Rating requirement (equivalent to IBD RS ≥ 70)",
         threshold=70.0, threshold_label="Min RS percentile (0–100)",
         threshold_min=50.0, threshold_max=90.0,
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
         category="PORTFOLIO", sort_order=91,
         label="Max portfolio heat: 15%",
         description="Total risk across all open positions must not exceed 15% of capital.",
         minervini_ref="Portfolio heat rule",
         threshold=15.0, threshold_label="Max portfolio heat %",
         threshold_min=5.0, threshold_max=30.0,
         enabled_globally=True, is_mandatory=False),
]


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
                    capital_aud=1000.00,
                ))
                logger.info("Seeded default account (paper, $1000, ADMIN tier)")

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
                db.add(RuleConfig(**fields))
                logger.debug(f"Seeded rule: {rule_data['rule_id']}")

    logger.info(f"Seed complete: {len(SYSTEM_CONFIGS)} configs, {len(RULE_CONFIGS)} rules, {len(TIERS)} tiers")


if __name__ == "__main__":
    seed_all()
