#!/bin/bash
# =============================================================================
# VCPilot — AW Org Full Pipeline Refresh
# Runs: migrations → universe seed → price data → regime → screen → entry check
# Usage: wsl bash /mnt/c/vcpilot/refresh_aw.sh
# =============================================================================
set -e
cd /mnt/c/vcpilot

# Colours
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓ $1${NC}"; }
info() { echo -e "${CYAN}  → $1${NC}"; }
warn() { echo -e "${YELLOW}  ⚠ $1${NC}"; }
step() { echo -e "\n${CYAN}━━━ $1 ━━━${NC}"; }

# Helper: run Python in the worker-equities container (has all task code + DB access)
rpy() { docker compose exec -T worker-equities python -c "$1"; }

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║  VCPilot — AW Org Full Pipeline Refresh      ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════╝${NC}"
echo ""

# ─── Pre-check: services running? ─────────────────────────────────────────────
step "0/7  Pre-flight service check"
docker compose ps --format "table {{.Name}}\t{{.Status}}" 2>/dev/null | grep -E "(vcpilot|NAME)" || true
echo ""

# ─── Step 1: Run migrations ────────────────────────────────────────────────────
step "1/7  Applying migrations & seeding new rules"
info "Running migrate_saas.py (enables IR exchange, seeds 5 new crypto rules)..."
docker compose run --rm --no-deps app python -m scripts.migrate_saas 2>&1 | grep -E "(INFO|ERROR|WARNING|✓|migration|seeding|complete|Done|Error)" | head -40
ok "Migrations applied"

# ─── Step 2: Verify AW org exists ─────────────────────────────────────────────
step "2/7  Verifying AW org & config"
rpy "
from app.database import get_db
from app.models.account import Organization
from app.models.config import SystemConfig
with get_db() as db:
    orgs = db.query(Organization).filter(Organization.is_active==True).all()
    if not orgs:
        print('ERROR: No active organisations found! Run init_db first.')
        exit(1)
    for o in orgs:
        print(f'  Org [{o.id}]: {o.name} | tier={o.tier.value} | active={o.is_active}')
        ae = db.query(SystemConfig).filter(SystemConfig.key=='active_exchanges', SystemConfig.organization_id==o.id).first()
        ce = db.query(SystemConfig).filter(SystemConfig.key=='crypto_exchange_key', SystemConfig.organization_id==o.id).first()
        print(f'    active_exchanges: {ae.value if ae else \"NOT SET\"}')
        print(f'    crypto_exchange_key: {ce.value if ce else \"NOT SET\"}')
"
ok "Org verified"

# ─── Step 3: Seed crypto universe ─────────────────────────────────────────────
step "3/7  Seeding IR crypto universe (top 50 tokens)"
info "Seeding Stock records for CRYPTO_INDEPENDENTRESERVE..."
rpy "
from app.tasks.screening import refresh_crypto_universe
refresh_crypto_universe.run(exchange_key='CRYPTO_INDEPENDENTRESERVE')
from app.database import get_db
from app.models.market import Stock
with get_db() as db:
    stocks = db.query(Stock).filter(Stock.exchange_key=='CRYPTO_INDEPENDENTRESERVE', Stock.is_active==True).all()
    print(f'  Total IR crypto stocks: {len(stocks)}')
    for s in stocks[:10]:
        print(f'    {s.ticker} ({s.name or \"\"}) | {s.currency}')
    if len(stocks) > 10:
        print(f'    ... and {len(stocks)-10} more')
"
ok "Crypto universe seeded"

# ─── Step 4: Refresh price data ────────────────────────────────────────────────
step "4/7  Refreshing price data (IR crypto — up to 50 tokens)"
info "Fetching 2yr OHLCV history from yfinance for all IR tokens..."
info "This may take 3-6 minutes. Tokens with no AUD yfinance pair will be skipped."
rpy "
from app.tasks.screening import refresh_price_data
refresh_price_data.run(exchange_key='CRYPTO_INDEPENDENTRESERVE')
from app.database import get_db
from app.models.market import PriceBar, Stock
from sqlalchemy import func, desc
with get_db() as db:
    total_bars = db.query(PriceBar).join(Stock, PriceBar.ticker==Stock.ticker).filter(Stock.asset_type=='CRYPTO').count()
    distinct_tickers = db.query(func.count(func.distinct(PriceBar.ticker))).join(Stock, PriceBar.ticker==Stock.ticker).filter(Stock.asset_type=='CRYPTO').scalar()
    latest_bar = db.query(PriceBar).join(Stock, PriceBar.ticker==Stock.ticker).filter(Stock.asset_type=='CRYPTO').order_by(desc(PriceBar.date)).first()
    print(f'  Total crypto bars in DB: {total_bars}')
    print(f'  Distinct crypto tickers with data: {distinct_tickers}')
    print(f'  Latest bar date: {latest_bar.date if latest_bar else \"none\"}')
    # Show which tickers got data
    tickers_with_data = db.query(func.distinct(PriceBar.ticker)).join(Stock, PriceBar.ticker==Stock.ticker).filter(Stock.asset_type=='CRYPTO').all()
    tickers_list = [t[0] for t in tickers_with_data]
    print(f'  Tickers with price data: {tickers_list}')
"
ok "Price data refreshed"

# ─── Step 5: Evaluate market regime ────────────────────────────────────────────
step "5/7  Evaluating market regime (CRYPTO_INDEPENDENTRESERVE)"
info "Checking BTC-AUD vs 200MA + breadth..."
rpy "
from app.tasks.screening import evaluate_market_regime_task
evaluate_market_regime_task.run(exchange_key='CRYPTO_INDEPENDENTRESERVE')
from app.database import get_db
from app.models.config import SystemConfig
from app.models.account import Organization
from app.models.exchange import MarketRegimeRecord
with get_db() as db:
    latest = db.query(MarketRegimeRecord).filter(MarketRegimeRecord.exchange_key=='CRYPTO_INDEPENDENTRESERVE').order_by(MarketRegimeRecord.evaluated_at.desc()).first()
    if latest:
        print(f'  Global regime: {latest.regime} (evaluated {latest.evaluated_at})')
        print(f'  BTC-AUD close: {latest.index_close:.2f} | MA200: {latest.index_ma200:.2f}')
        if latest.breadth_pct is not None:
            print(f'  Breadth (% above 200MA): {latest.breadth_pct:.1f}%')
    for org in db.query(Organization).filter(Organization.is_active==True).all():
        cfg = db.query(SystemConfig).filter(
            SystemConfig.key=='last_market_regime_CRYPTO_INDEPENDENTRESERVE',
            SystemConfig.organization_id==org.id
        ).first()
        print(f'  Org [{org.name}] regime stored: {cfg.value if cfg else \"Not set\"}')
"
ok "Market regime evaluated"

# ─── Step 6: Force screen ──────────────────────────────────────────────────────
step "6/7  Running force screen (CRYPTO_INDEPENDENTRESERVE)"
info "Running full Minervini pipeline (trend template → fundamentals → VCP) on all tokens..."
info "This may take 5-10 minutes. Watch Task Log in the dashboard for live progress."
rpy "
from app.tasks.screening import _run_screen_force
from app.database import get_db
from app.models.account import Organization
with get_db() as db:
    orgs = db.query(Organization).filter(Organization.is_active==True).all()
for org in orgs:
    print(f'  Screening for org: {org.name} [{org.id}]')
    _run_screen_force.run(organization_id=org.id, exchange_key='CRYPTO_INDEPENDENTRESERVE')
    from app.database import get_db as gdb
    from app.models.signal import Signal, SignalStatus, Watchlist, WatchlistStatus
    with gdb() as db2:
        sigs = db2.query(Signal).filter(Signal.organization_id==org.id, Signal.asset_type=='CRYPTO').count()
        pending = db2.query(Signal).filter(Signal.organization_id==org.id, Signal.asset_type=='CRYPTO', Signal.status==SignalStatus.PENDING).count()
        wl = db2.query(Watchlist).filter(Watchlist.organization_id==org.id, Watchlist.asset_type=='CRYPTO', Watchlist.status==WatchlistStatus.WATCHING).count()
        print(f'    Signals: {sigs} total, {pending} PENDING')
        print(f'    Watchlist: {wl} WATCHING')
        # Show top signals
        top_sigs = db2.query(Signal).filter(Signal.organization_id==org.id, Signal.asset_type=='CRYPTO').order_by(Signal.rs_rating.desc()).limit(5).all()
        if top_sigs:
            print(f'    Top signals by RS:')
            for s in top_sigs:
                print(f'      {s.ticker}: pivot={s.pivot_price:.4f} stop={s.stop_price:.4f} RS={s.rs_rating:.0f} status={s.status.value}')
        # Show top watchlist
        top_wl = db2.query(Watchlist).filter(Watchlist.organization_id==org.id, Watchlist.asset_type=='CRYPTO', Watchlist.status==WatchlistStatus.WATCHING).limit(5).all()
        if top_wl:
            print(f'    Watchlist (top 5):')
            for w in top_wl:
                print(f'      {w.ticker}')
"
ok "Force screen complete"

# ─── Step 7: Test entry check ──────────────────────────────────────────────────
step "7/7  Running entry check (CRYPTO) — tests live IR price feed"
info "Checking all PENDING signals against live IR prices..."
rpy "
from app.tasks.trading import check_entry_triggers
check_entry_triggers.run(exchange_key='CRYPTO')
from app.database import get_db
from app.models.market import EntryCheckLog
from app.models.account import Organization
from sqlalchemy import desc
with get_db() as db:
    for org in db.query(Organization).filter(Organization.is_active==True).all():
        logs = db.query(EntryCheckLog).filter(
            EntryCheckLog.organization_id==org.id
        ).order_by(desc(EntryCheckLog.checked_at)).limit(5).all()
        print(f'  Org [{org.name}]: {len(logs)} recent entry check logs')
        for l in logs:
            confirmed = '🟢 BREAKOUT' if l.breakout_confirmed else '🔴 not triggered'
            print(f'    {l.ticker}: current={l.price_current} pivot={l.price_pivot} | {confirmed} | src={l.data_source}')
"
ok "Entry check complete"

# ─── Final summary ─────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  ✅  All 7 steps completed successfully!     ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════╝${NC}"
echo ""
echo "  Open the VCPilot dashboard:"
echo "  → http://localhost:8501"
echo ""
echo "  Dashboard pages to check:"
echo "  → /signals      — PENDING crypto signals (ready to trade)"
echo "  → /watchlist    — VCP-forming tokens being monitored"
echo "  → /admin/tasks  — Task Log: live activity trail"
echo "  → /admin/data-log — Entry check results with IR live prices"
echo "  → /admin/health — Worker status, regime, manual triggers"
echo ""
