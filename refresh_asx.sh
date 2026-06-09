#!/bin/bash
# =============================================================================
# VCPilot — ASX + IBKR Full Refresh & Diagnostic for AW Org
# Usage: wsl bash /mnt/c/vcpilot/refresh_asx.sh
# =============================================================================
cd /mnt/c/vcpilot

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  VCPilot ASX/IBKR Refresh — AW Org          ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ─── Pre-check: which services are running ────────────────────────────────────
echo "━━━ 0/6  Service check ━━━"
docker compose ps --format "table {{.Name}}\t{{.Status}}" 2>/dev/null | grep -E "(NAME|vcpilot)" || true
echo ""
echo "  Note: IBKR gateway (vcpilot-ibkr) only starts with --profile trading."
echo "  Without it, orders fall back to simulation mode automatically."
echo ""

# ─── Step 1: Refresh ASX200 universe ─────────────────────────────────────────
echo "━━━ 1/6  Refreshing ASX200 universe ━━━"
echo "  Fetching all ASX200 constituents from Wikipedia..."
docker compose exec -T worker-equities python << 'PY'
from app.tasks.screening import refresh_universe
refresh_universe.run()
from app.database import SessionLocal
from app.models.market import Stock
db = SessionLocal()
try:
    asx_count = db.query(Stock).filter(Stock.exchange_key=='ASX', Stock.is_active==True).count()
    print(f"  ASX stocks in universe: {asx_count}")
finally:
    db.close()
PY
echo ""

# ─── Step 2: Refresh ASX price data ──────────────────────────────────────────
echo "━━━ 2/6  Refreshing ASX price data ━━━"
echo "  Fetching 2yr OHLCV for all ASX200 tickers (may take 5-10 min)..."
docker compose exec -T worker-equities python << 'PY'
from app.tasks.screening import refresh_price_data
refresh_price_data.run(exchange_key='ASX')
from app.database import SessionLocal
from app.models.market import Stock, PriceBar
from sqlalchemy import func, desc
db = SessionLocal()
try:
    bars = db.query(func.count(PriceBar.id)).join(Stock, PriceBar.ticker==Stock.ticker).filter(Stock.exchange_key=='ASX').scalar()
    tickers = db.query(func.count(func.distinct(PriceBar.ticker))).join(Stock, PriceBar.ticker==Stock.ticker).filter(Stock.exchange_key=='ASX').scalar()
    latest = db.query(PriceBar).join(Stock, PriceBar.ticker==Stock.ticker).filter(Stock.exchange_key=='ASX').order_by(desc(PriceBar.date)).first()
    print(f"  ASX bars in DB: {bars} | Tickers with data: {tickers}")
    if latest:
        print(f"  Latest bar: {latest.ticker} on {latest.date} | close={latest.close}")
finally:
    db.close()
PY
echo ""

# ─── Step 3: Evaluate ASX market regime ──────────────────────────────────────
echo "━━━ 3/6  Evaluating ASX market regime ━━━"
docker compose exec -T worker-equities python << 'PY'
from app.tasks.screening import evaluate_market_regime_task
evaluate_market_regime_task.run(exchange_key='ASX')
from app.database import SessionLocal
from app.models.exchange import MarketRegimeRecord
from app.models.config import SystemConfig
from app.models.account import Organization
from sqlalchemy import desc
db = SessionLocal()
try:
    r = db.query(MarketRegimeRecord).filter(MarketRegimeRecord.exchange_key=='ASX').order_by(desc(MarketRegimeRecord.evaluated_at)).first()
    if r:
        pct = (float(r.index_close) / float(r.index_ma200) - 1) * 100
        print(f"  ASX regime: {r.regime} (^AXJO {float(r.index_close):,.2f} vs 200MA {float(r.index_ma200):,.2f} | {pct:+.1f}%)")
        if r.breadth_pct:
            print(f"  Breadth: {float(r.breadth_pct):.1f}% of stocks above 200MA")
    # Also update per-org
    for org in db.query(Organization).filter(Organization.is_active==True).all():
        cfg = db.query(SystemConfig).filter(
            SystemConfig.key=='last_market_regime_ASX',
            SystemConfig.organization_id==org.id
        ).first()
        print(f"  Org [{org.name}] last_market_regime_ASX: {cfg.value if cfg else 'not set'}")
    # Global key
    global_regime = db.query(SystemConfig).filter(
        SystemConfig.key=='last_market_regime',
        SystemConfig.organization_id==None
    ).first()
    print(f"  Global last_market_regime: {global_regime.value if global_regime else 'not set'}")
finally:
    db.close()
PY
echo ""

# ─── Step 4: Force screen ASX for AW org ─────────────────────────────────────
echo "━━━ 4/6  Force screen ASX for AW org ━━━"
echo "  Running full Minervini pipeline on ASX200 (may take 5-10 min)..."
docker compose exec -T worker-equities python << 'PY'
from app.database import SessionLocal
from app.models.account import Organization
from app.models.signal import Signal, SignalStatus, Watchlist, WatchlistStatus
from app.tasks.screening import _run_screen_force
from sqlalchemy import desc

db = SessionLocal()
try:
    aw = db.query(Organization).filter(Organization.name.ilike('%AW%')).first()
    aw_id = aw.id
    db.close()
finally:
    pass

print(f"  Screening ASX for AW (org {aw_id})...")
_run_screen_force.run(organization_id=aw_id, exchange_key='ASX')

db = SessionLocal()
try:
    sigs = db.query(Signal).filter(Signal.organization_id==aw_id, Signal.exchange_key=='ASX').all()
    pending = [s for s in sigs if s.status==SignalStatus.PENDING]
    wl = db.query(Watchlist).filter(
        Watchlist.organization_id==aw_id, Watchlist.exchange_key=='ASX',
        Watchlist.status==WatchlistStatus.WATCHING
    ).all()
    print(f"  ASX signals: {len(sigs)} total | {len(pending)} PENDING")
    print(f"  ASX watchlist: {len(wl)} items")
    if pending:
        print("  PENDING signals:")
        for s in sorted(pending, key=lambda x: x.rs_rating or 0, reverse=True)[:10]:
            print(f"    {s.ticker}: pivot={float(s.pivot_price):.3f} RS={float(s.rs_rating or 0):.0f}")
    elif wl:
        print("  Top watchlist items (nearest to VCP):")
        for w in wl[:10]:
            print(f"    {w.ticker}")
finally:
    db.close()
PY
echo ""

# ─── Step 5: Test IBKR simulation mode ───────────────────────────────────────
echo "━━━ 5/6  Testing IBKR simulation mode ━━━"
docker compose exec -T worker-equities python << 'PY'
from app.broker.ibkr import IBKRBroker
from app.config import settings

print(f"  ibkr_simulate setting: {settings.ibkr_simulate}")

# Test simulated bracket order
try:
    broker = IBKRBroker()
    result = broker._simulate_order(
        ticker='BHP', action='BUY', qty=10,
        entry_price=45.00, stop_price=43.00, order_ref='TEST-001'
    )
    print(f"  Simulated BUY BHP: status={result.get('status')} | entry_order_id={result.get('entry_order_id')}")
    print(f"  Simulation mode: WORKING ✓")
except Exception as e:
    print(f"  IBKR simulation error: {e}")

# Check if IBKR gateway container is running
import subprocess
try:
    r = subprocess.run(['docker','compose','ps','ibkr','--format','{{.Status}}'],
                       capture_output=True, text=True, cwd='/mnt/c/vcpilot')
    status = r.stdout.strip()
    if status:
        print(f"  IBKR Gateway container: {status}")
    else:
        print(f"  IBKR Gateway: not running (start with: docker compose --profile trading up ibkr -d)")
except Exception as e:
    print(f"  Could not check IBKR gateway status: {e}")
PY
echo ""

# ─── Step 6: Full diagnostic summary ─────────────────────────────────────────
echo "━━━ 6/6  Final ASX diagnostic ━━━"
docker compose exec -T worker-equities python << 'PY'
from app.database import SessionLocal
from app.models.account import Organization
from app.models.config import SystemConfig, RuleConfig
from app.models.market import Stock, PriceBar
from app.models.signal import Signal, SignalStatus, Watchlist, WatchlistStatus
from app.models.audit import AuditLog, AuditAction
from app.models.exchange import MarketRegimeRecord
from sqlalchemy import func, desc

db = SessionLocal()
try:
    aw = db.query(Organization).filter(Organization.name.ilike('%AW%')).first()
    aw_id = aw.id

    # ASX universe
    asx_stocks = db.query(Stock).filter(Stock.exchange_key=='ASX', Stock.is_active==True).count()
    asx_bars = db.query(func.count(PriceBar.id)).join(Stock, PriceBar.ticker==Stock.ticker).filter(Stock.exchange_key=='ASX').scalar()
    asx_tickers_with_bars = db.query(func.count(func.distinct(PriceBar.ticker))).join(Stock, PriceBar.ticker==Stock.ticker).filter(Stock.exchange_key=='ASX').scalar()
    latest_bar = db.query(PriceBar).join(Stock, PriceBar.ticker==Stock.ticker).filter(Stock.exchange_key=='ASX').order_by(desc(PriceBar.date)).first()

    print(f"[UNIVERSE] ASX stocks: {asx_stocks}")
    print(f"[PRICE DATA] ASX bars: {asx_bars} | Tickers with data: {asx_tickers_with_bars}")
    if latest_bar:
        print(f"  Latest: {latest_bar.ticker} on {latest_bar.date}")

    # Regime
    regime = db.query(MarketRegimeRecord).filter(
        MarketRegimeRecord.exchange_key=='ASX'
    ).order_by(desc(MarketRegimeRecord.evaluated_at)).first()
    if regime:
        print(f"[REGIME] ASX: {regime.regime} | ^AXJO {float(regime.index_close):,.2f} vs 200MA {float(regime.index_ma200):,.2f}")

    # Global regime key (used by check_entry_triggers for equities)
    global_r = db.query(SystemConfig).filter(SystemConfig.key=='last_market_regime', SystemConfig.organization_id==None).first()
    print(f"[GLOBAL REGIME KEY] last_market_regime = {global_r.value if global_r else 'NOT SET'}")

    # Signals
    asx_sigs = db.query(Signal).filter(Signal.organization_id==aw_id, Signal.exchange_key=='ASX').count()
    asx_pending = db.query(Signal).filter(Signal.organization_id==aw_id, Signal.exchange_key=='ASX', Signal.status==SignalStatus.PENDING).count()
    asx_wl = db.query(Watchlist).filter(Watchlist.organization_id==aw_id, Watchlist.exchange_key=='ASX', Watchlist.status==WatchlistStatus.WATCHING).count()
    print(f"[SIGNALS] AW ASX: {asx_sigs} total | {asx_pending} PENDING")
    print(f"[WATCHLIST] AW ASX: {asx_wl} items")

    # Equity rules for AW
    eq_rules = db.query(RuleConfig).filter(
        RuleConfig.organization_id==aw_id,
        RuleConfig.category.in_(['TREND_TEMPLATE','VCP','FUNDAMENTAL','ENTRY','EXIT_DEFENSIVE','EXIT_OFFENSIVE','POSITION_SIZING','PORTFOLIO'])
    ).count()
    print(f"[RULES] AW equity rules: {eq_rules}")

    # Recent audit for ASX activity
    asx_audit = db.query(AuditLog).filter(
        AuditLog.organization_id==aw_id,
        AuditLog.action==AuditAction.SCREENER_TICKER,
        AuditLog.ticker.like('%.AX'),
    ).order_by(desc(AuditLog.created_at)).limit(5).all()
    print(f"[SCREENER LOG] Recent ASX ticker results for AW:")
    if asx_audit:
        for log in asx_audit:
            ts = log.created_at.strftime('%H:%M:%S') if log.created_at else '??'
            print(f"  [{ts}] {log.ticker}: {(log.message or '')[:80]}")
    else:
        print("  No ASX screener ticker logs yet")

finally:
    db.close()
PY
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  ASX Refresh Complete                        ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "To start IBKR paper gateway:"
echo "  wsl docker compose --profile trading up ibkr -d"
echo ""
echo "Then configure in dashboard /admin/config:"
echo "  ibkr_username, ibkr_password, ibkr_account"
echo "  ibkr_paper_mode = true"
echo ""
echo "Verify IBKR connection at: http://localhost:8501/admin/health"
echo ""
