#!/bin/bash
# AstraTrade — AW Fix Part 3: proper atomic transactions
cd /mnt/c/vcpilot

echo ""
echo "━━━ AW Fix Part 3 ━━━"
echo ""

docker compose exec -T worker-equities python << 'PYEOF'
from app.database import SessionLocal
from app.models.account import Organization
from app.models.config import RuleConfig, RuleCategory
from app.models.signal import Watchlist, WatchlistStatus
from app.models.audit import AuditLog, AuditAction
from app.models.exchange import MarketRegimeRecord
from decimal import Decimal

db = SessionLocal()
try:
    aw = db.query(Organization).filter(Organization.name.ilike('%AW%')).first()
    aw_id = aw.id
    print(f"AW org id={aw_id}")

    # ── Fix 1: Add 5 new crypto rules (separate commit) ──────
    print("\n[FIX 1] Adding 5 enhanced crypto rules...")
    NEW_RULES = [
        dict(rule_id='crypto_rsi_momentum',        sort_order=106, label='RSI(14) >= 50 momentum',          threshold=Decimal('50.0'),  threshold_min=Decimal('40.0'), threshold_max=Decimal('70.0')),
        dict(rule_id='crypto_macd_bullish',         sort_order=107, label='MACD bullish (12/26/9)',          threshold=Decimal('0.0'),   threshold_min=Decimal('0.0'),  threshold_max=Decimal('0.0')),
        dict(rule_id='crypto_volume_surge',         sort_order=108, label='Volume surge >= 1.5x 20d avg',   threshold=Decimal('1.5'),   threshold_min=Decimal('1.0'),  threshold_max=Decimal('5.0')),
        dict(rule_id='crypto_min_rr_ratio',         sort_order=109, label='Min risk/reward >= 2.5:1',       threshold=Decimal('2.5'),   threshold_min=Decimal('1.5'),  threshold_max=Decimal('5.0')),
        dict(rule_id='crypto_btc_relative_strength',sort_order=110, label='RS vs BTC (50d) >= 0%',          threshold=Decimal('0.0'),   threshold_min=Decimal('-10.0'),threshold_max=Decimal('20.0')),
    ]
    added = 0
    for r in NEW_RULES:
        exists = db.query(RuleConfig).filter(
            RuleConfig.rule_id==r['rule_id'],
            RuleConfig.organization_id==aw_id
        ).first()
        if not exists:
            db.add(RuleConfig(
                rule_id=r['rule_id'], organization_id=aw_id,
                category=RuleCategory.CRYPTO, sort_order=r['sort_order'],
                asset_types='CRYPTO', label=r['label'],
                threshold=r['threshold'], threshold_label='threshold',
                threshold_min=r['threshold_min'], threshold_max=r['threshold_max'],
                enabled_globally=True, is_mandatory=False,
            ))
            added += 1
            print(f"  + {r['rule_id']}")
        else:
            print(f"  = {r['rule_id']} already exists")
    db.add(AuditLog(action=AuditAction.CONFIG_CHANGED, organization_id=aw_id,
                    message=f'Added {added} enhanced crypto rules to AW'))
    db.commit()
    print(f"  Committed {added} new rules")

    # ── Fix 2: Remove ETH-USD (separate commit) ───────────────
    print("\n[FIX 2] Removing ETH-USD from watchlist...")
    removed = 0
    stale = db.query(Watchlist).filter(
        Watchlist.organization_id==aw_id,
        Watchlist.ticker=='ETH-USD',
    ).all()
    for w in stale:
        db.delete(w)
        removed += 1
    if removed:
        db.add(AuditLog(action=AuditAction.CONFIG_CHANGED, organization_id=aw_id,
                        message='Removed stale ETH-USD watchlist entry (wrong suffix for IR)'))
        db.commit()
        print(f"  Removed {removed} entry(ies)")
    else:
        print("  ETH-USD already gone")

    # ── Verify final state ────────────────────────────────────
    print("\n[VERIFY] Final state for AW org:")

    total_rules = db.query(RuleConfig).filter(RuleConfig.organization_id==aw_id).count()
    crypto_rules = db.query(RuleConfig).filter(
        RuleConfig.organization_id==aw_id,
        RuleConfig.category==RuleCategory.CRYPTO,
    ).order_by(RuleConfig.sort_order).all()
    print(f"\n  Rules total: {total_rules} | Crypto: {len(crypto_rules)}")
    for r in crypto_rules:
        s = 'ON ' if r.enabled_globally else 'OFF'
        print(f"  [{s}] {r.rule_id}: threshold={r.threshold}")

    wl = db.query(Watchlist).filter(
        Watchlist.organization_id==aw_id,
        Watchlist.status==WatchlistStatus.WATCHING,
    ).all()
    crypto_wl = [w for w in wl if getattr(w,'asset_type','EQUITY')=='CRYPTO']
    print(f"\n  Watchlist total: {len(wl)} | Crypto: {len(crypto_wl)}")
    for w in crypto_wl:
        print(f"    {w.ticker}")

    # ── Recovery tracker (fixed Decimal types) ─────────────────
    from sqlalchemy import desc
    regime = db.query(MarketRegimeRecord).filter(
        MarketRegimeRecord.exchange_key=='CRYPTO_INDEPENDENTRESERVE'
    ).order_by(desc(MarketRegimeRecord.evaluated_at)).first()

    if regime:
        btc_now  = float(regime.index_close)
        ma200    = float(regime.index_ma200)
        gap      = ma200 - btc_now
        gap_pct  = (gap / ma200) * 100
        print(f"\n[RECOVERY TRACKER]")
        print(f"  BTC-AUD now:  A${btc_now:>12,.2f}")
        print(f"  200MA target: A${ma200:>12,.2f}")
        print(f"  Gap to BULL:  A${gap:>12,.2f} ({gap_pct:.1f}% recovery needed)")
        print(f"  Milestones:")
        for pct in [5, 10, 15, 20]:
            lvl = btc_now * (1 + pct/100)
            print(f"    +{pct:2d}% → A${lvl:>10,.2f}  {'← BULL ZONE' if lvl >= ma200 else ''}")
        print(f"  BULL zone  → A${ma200:>10,.2f}  (+{gap_pct:.1f}% from now)")

    # ── Live IR prices with 200MA status ──────────────────────
    print("\n[IR LIVE PRICES + 200MA STATUS]")
    from app.data.fetcher import _get_ir_live_price
    from app.models.market import PriceBar
    tokens = ['BTC-AUD','ETH-AUD','SOL-AUD','XRP-AUD','ADA-AUD',
              'DOGE-AUD','LINK-AUD','DOT-AUD','AVAX-AUD','ATOM-AUD']
    best = []
    for ticker in tokens:
        r = _get_ir_live_price(ticker)
        if not r:
            print(f"  {ticker:<12}: not on IR")
            continue
        price = r['price']
        bar = db.query(PriceBar).filter(PriceBar.ticker==ticker).order_by(desc(PriceBar.date)).first()
        if bar and bar.ma_200:
            vs_200 = (price / float(bar.ma_200) - 1) * 100
            vs_50  = (price / float(bar.ma_50) - 1) * 100 if bar.ma_50 else None
            status = '🟢 ABOVE' if vs_200 > 0 else '🔴 BELOW'
            best.append((vs_200, ticker, price, vs_200, vs_50))
            vs50_str = f" | vs 50MA: {vs_50:+.1f}%" if vs_50 is not None else ""
            print(f"  {ticker:<12}: A${price:>10,.4f} | 200MA: {vs_200:+.1f}%{vs50_str} {status}")
        else:
            print(f"  {ticker:<12}: A${price:>10,.4f} | 200MA: no bar data")

    # Which token is closest to 200MA?
    if best:
        best_sorted = sorted(best, key=lambda x: -x[0])
        print(f"\n  Closest to 200MA recovery (best performers):")
        for vs200, t, p, _, vs50 in best_sorted[:3]:
            print(f"    {t}: {vs200:+.1f}% vs 200MA")

finally:
    db.close()
PYEOF

echo ""
echo "━━━ All fixes applied ━━━"
echo ""
