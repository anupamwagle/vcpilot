#!/bin/bash
# AstraTrade — AW Org Diagnostic
# Quick check of all DB state for AW org
cd /mnt/c/vcpilot

echo ""
echo "━━━ AstraTrade AW Org Diagnostic ━━━"
echo ""

docker compose exec -T worker-equities python -c "
from app.database import get_db
from app.models.account import Organization, Account
from app.models.config import SystemConfig, RuleConfig
from app.models.market import Stock, PriceBar
from app.models.signal import Signal, SignalStatus, Watchlist, WatchlistStatus
from app.models.audit import AuditLog, AuditAction
from app.models.exchange import MarketRegimeRecord
from sqlalchemy import func, desc
import datetime

with get_db() as db:

    # ── 1. Find AW org ────────────────────────────────────────
    aw = db.query(Organization).filter(Organization.name.ilike('%AW%')).first()
    if not aw:
        orgs = db.query(Organization).all()
        print('All orgs:')
        for o in orgs:
            print(f'  [{o.id}] {o.name} active={o.is_active}')
        print('ERROR: AW org not found by name. Set aw_id below manually.')
        exit(1)

    print(f'[ORG] {aw.name} | id={aw.id} | tier={aw.tier.value} | active={aw.is_active}')

    # ── 2. Config keys ────────────────────────────────────────
    print()
    print('[CONFIG]')
    cfg_keys = ['active_exchanges','crypto_exchange_key','working_capital_currency',
                'trading_paused','last_market_regime_CRYPTO_INDEPENDENTRESERVE',
                'last_market_regime_ASX']
    for k in cfg_keys:
        cfg = db.query(SystemConfig).filter(SystemConfig.key==k, SystemConfig.organization_id==aw.id).first()
        val = cfg.value if cfg else 'NOT SET'
        print(f'  {k}: {val}')

    # ── 3. Account / Capital ──────────────────────────────────
    print()
    acct = db.query(Account).filter(Account.organization_id==aw.id, Account.is_active==True).first()
    if acct:
        print(f'[ACCOUNT] capital=A\${acct.capital_aud} | paper={acct.is_paper}')
    else:
        print('[ACCOUNT] WARNING: no active account found for AW org')

    # ── 4. Crypto stocks in DB ────────────────────────────────
    print()
    ir_stocks = db.query(Stock).filter(Stock.exchange_key=='CRYPTO_INDEPENDENTRESERVE', Stock.is_active==True).all()
    print(f'[STOCKS] IR crypto stocks: {len(ir_stocks)}')
    if ir_stocks:
        print(f'  First 10: {[s.ticker for s in ir_stocks[:10]]}')

    # ── 5. Price bars ─────────────────────────────────────────
    crypto_bars = db.query(func.count(PriceBar.id)).join(
        Stock, PriceBar.ticker==Stock.ticker
    ).filter(Stock.asset_type=='CRYPTO').scalar()
    crypto_tickers_with_bars = db.query(func.count(func.distinct(PriceBar.ticker))).join(
        Stock, PriceBar.ticker==Stock.ticker
    ).filter(Stock.asset_type=='CRYPTO').scalar()
    latest_bar = db.query(PriceBar).join(Stock, PriceBar.ticker==Stock.ticker).filter(
        Stock.asset_type=='CRYPTO'
    ).order_by(desc(PriceBar.date)).first()
    print()
    print(f'[PRICE BARS] crypto bars: {crypto_bars} | tickers: {crypto_tickers_with_bars}')
    if latest_bar:
        print(f'  Latest bar: {latest_bar.ticker} on {latest_bar.date} close={latest_bar.close}')

    # ── 6. Market regime ─────────────────────────────────────
    print()
    regime_rec = db.query(MarketRegimeRecord).filter(
        MarketRegimeRecord.exchange_key=='CRYPTO_INDEPENDENTRESERVE'
    ).order_by(desc(MarketRegimeRecord.evaluated_at)).first()
    if regime_rec:
        print(f'[REGIME] CRYPTO_IR: {regime_rec.regime} at {regime_rec.evaluated_at}')
        print(f'  BTC-AUD: {regime_rec.index_close:.2f} vs MA200: {regime_rec.index_ma200:.2f}')
    else:
        print('[REGIME] CRYPTO_IR: NOT EVALUATED YET')

    # ── 7. Signals for AW ────────────────────────────────────
    print()
    all_sigs = db.query(Signal).filter(Signal.organization_id==aw.id).all()
    crypto_sigs = [s for s in all_sigs if s.asset_type=='CRYPTO']
    print(f'[SIGNALS] Total for AW: {len(all_sigs)} | Crypto: {len(crypto_sigs)}')
    by_status = {}
    for s in all_sigs:
        by_status[s.status.value] = by_status.get(s.status.value, 0) + 1
    for st, cnt in sorted(by_status.items()):
        print(f'  {st}: {cnt}')
    if crypto_sigs:
        print('  Top crypto signals:')
        for s in sorted(crypto_sigs, key=lambda x: x.rs_rating or 0, reverse=True)[:8]:
            print(f'    {s.ticker}: {s.status.value} | pivot={s.pivot_price} | RS={s.rs_rating:.0f} | date={s.signal_date}')

    # ── 8. Watchlist for AW ───────────────────────────────────
    print()
    wl_items = db.query(Watchlist).filter(
        Watchlist.organization_id==aw.id, Watchlist.status==WatchlistStatus.WATCHING
    ).all()
    crypto_wl = [w for w in wl_items if getattr(w,'asset_type','EQUITY')=='CRYPTO']
    print(f'[WATCHLIST] Total: {len(wl_items)} | Crypto: {len(crypto_wl)}')
    if crypto_wl:
        print('  Crypto watchlist:')
        for w in crypto_wl[:10]:
            print(f'    {w.ticker} added={w.added_date}')

    # ── 9. Recent audit log for AW ────────────────────────────
    print()
    recent_audit = db.query(AuditLog).filter(
        AuditLog.organization_id==aw.id
    ).order_by(desc(AuditLog.created_at)).limit(15).all()
    print(f'[AUDIT LOG] Last 15 entries for AW:')
    for log in recent_audit:
        ts = log.created_at.strftime('%H:%M:%S') if log.created_at else '??:??'
        msg = (log.message or '')[:90]
        print(f'  [{ts}] {log.action.value}: {msg}')

    # ── 10. Rules seeded for AW ───────────────────────────────
    print()
    rules = db.query(RuleConfig).filter(RuleConfig.organization_id==aw.id).count()
    crypto_rules = db.query(RuleConfig).filter(
        RuleConfig.organization_id==aw.id,
        RuleConfig.category.in_(['CRYPTO'])
    ).count() if rules > 0 else 0
    print(f'[RULES] Total for AW: {rules} | CRYPTO category: {crypto_rules}')
    if crypto_rules > 0:
        cr = db.query(RuleConfig).filter(
            RuleConfig.organization_id==aw.id,
            RuleConfig.category.in_(['CRYPTO'])
        ).all()
        for r in cr:
            status = 'ON' if r.enabled_globally else 'OFF'
            print(f'  [{status}] {r.rule_id}: threshold={r.threshold}')

    # ── 11. Screener ticker audit logs (most recent screen run) ──
    print()
    screener_logs = db.query(AuditLog).filter(
        AuditLog.organization_id==aw.id,
        AuditLog.action==AuditAction.SCREENER_TICKER
    ).order_by(desc(AuditLog.created_at)).limit(20).all()
    print(f'[SCREENER TICKER LOGS] Last 20 for AW:')
    if screener_logs:
        for log in screener_logs:
            ts = log.created_at.strftime('%H:%M:%S') if log.created_at else '??'
            msg = (log.message or '')[:100]
            print(f'  [{ts}] {log.ticker}: {msg}')
    else:
        print('  NONE — screener has not run for AW org or no ticker-level logs written')

    # ── 12. IR live price test ────────────────────────────────
    print()
    print('[LIVE PRICE TEST] Fetching BTC-AUD from Independent Reserve...')
    try:
        from app.data.fetcher import _get_ir_live_price
        result = _get_ir_live_price('BTC-AUD')
        if result:
            print(f'  BTC-AUD: A\${result[\"price\"]:,.2f} | bid=A\${result[\"bid\"]:,.2f} | ask=A\${result[\"ask\"]:,.2f} | src={result[\"data_source\"]}')
        else:
            print('  ERROR: _get_ir_live_price returned None')
    except Exception as e:
        print(f'  ERROR: {e}')

    # Also test a few more coins
    for ticker in ['ETH-AUD', 'SOL-AUD', 'XRP-AUD']:
        try:
            r = _get_ir_live_price(ticker)
            if r:
                print(f'  {ticker}: A\${r[\"price\"]:,.4f}')
            else:
                print(f'  {ticker}: not listed on IR (returns None)')
        except Exception as e:
            print(f'  {ticker}: ERROR {e}')
"

echo ""
echo "━━━ Diagnostic complete ━━━"
echo ""
