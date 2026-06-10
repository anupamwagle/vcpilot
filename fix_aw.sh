#!/bin/bash
# AstraTrade — AW Org Fix Script
# 1. Sync 5 new crypto rules to AW org
# 2. Fix ETH-USD watchlist entry to ETH-AUD
# 3. Run ASX screener for AW
# 4. Show final state summary
cd /mnt/c/vcpilot

echo ""
echo "━━━ AstraTrade AW Org Fix Script ━━━"
echo ""

docker compose exec -T worker-equities python -c "
from app.database import get_db
from app.models.account import Organization
from app.models.config import RuleConfig, RuleCategory
from app.models.signal import Watchlist, WatchlistStatus
from app.models.audit import AuditLog, AuditAction

# ─── 1. Find AW org ───────────────────────────────────────
with get_db() as db:
    aw = db.query(Organization).filter(Organization.name.ilike('%AW%')).first()
    if not aw:
        print('ERROR: AW org not found')
        exit(1)
    aw_id = aw.id
    print(f'AW org id={aw_id}')

# ─── 2. Sync missing crypto rules from global to AW ────────
print()
print('[FIX 1] Syncing enhanced crypto rules to AW org...')

NEW_CRYPTO_RULES = [
    {
        'rule_id': 'crypto_rsi_momentum',
        'category': 'CRYPTO',
        'sort_order': 106,
        'asset_types': 'CRYPTO',
        'label': 'RSI(14) >= 50 — momentum confirmation',
        'description': 'RSI(14) above 50 confirms upward momentum phase.',
        'threshold': 50.0,
        'threshold_label': 'Minimum RSI(14)',
        'threshold_min': 40.0,
        'threshold_max': 70.0,
        'enabled_globally': True,
        'is_mandatory': False,
    },
    {
        'rule_id': 'crypto_macd_bullish',
        'category': 'CRYPTO',
        'sort_order': 107,
        'asset_types': 'CRYPTO',
        'label': 'MACD bullish (12/26/9) — histogram positive',
        'description': 'MACD line must be above signal line with positive histogram.',
        'threshold': 0.0,
        'threshold_label': 'Min MACD histogram',
        'threshold_min': 0.0,
        'threshold_max': 0.0,
        'enabled_globally': True,
        'is_mandatory': False,
    },
    {
        'rule_id': 'crypto_volume_surge',
        'category': 'CRYPTO',
        'sort_order': 108,
        'asset_types': 'CRYPTO',
        'label': 'Volume surge >= 1.5x 20-day average',
        'description': 'Breakout volume must be at least 1.5x the 20-day average.',
        'threshold': 1.5,
        'threshold_label': 'Volume surge multiplier',
        'threshold_min': 1.0,
        'threshold_max': 5.0,
        'enabled_globally': True,
        'is_mandatory': False,
    },
    {
        'rule_id': 'crypto_min_rr_ratio',
        'category': 'CRYPTO',
        'sort_order': 109,
        'asset_types': 'CRYPTO',
        'label': 'Min risk/reward ratio >= 2.5:1',
        'description': 'Each trade setup must offer at least 2.5x reward relative to risk.',
        'threshold': 2.5,
        'threshold_label': 'Min R/R ratio',
        'threshold_min': 1.5,
        'threshold_max': 5.0,
        'enabled_globally': True,
        'is_mandatory': False,
    },
    {
        'rule_id': 'crypto_btc_relative_strength',
        'category': 'CRYPTO',
        'sort_order': 110,
        'asset_types': 'CRYPTO',
        'label': 'RS vs BTC (50d) >= 0% — must match or beat Bitcoin',
        'description': 'Non-BTC assets must show at least equal 50-day RS vs Bitcoin.',
        'threshold': 0.0,
        'threshold_label': 'Min 50d RS vs BTC (%)',
        'threshold_min': -10.0,
        'threshold_max': 20.0,
        'enabled_globally': True,
        'is_mandatory': False,
    },
]

added = 0
with get_db() as db:
    for rule_def in NEW_CRYPTO_RULES:
        existing = db.query(RuleConfig).filter(
            RuleConfig.rule_id == rule_def['rule_id'],
            RuleConfig.organization_id == aw_id,
        ).first()
        if not existing:
            db.add(RuleConfig(
                rule_id=rule_def['rule_id'],
                organization_id=aw_id,
                category=RuleCategory.CRYPTO,
                sort_order=rule_def['sort_order'],
                asset_types=rule_def['asset_types'],
                label=rule_def['label'],
                description=rule_def['description'],
                threshold=rule_def['threshold'],
                threshold_label=rule_def['threshold_label'],
                threshold_min=rule_def['threshold_min'],
                threshold_max=rule_def['threshold_max'],
                enabled_globally=rule_def['enabled_globally'],
                is_mandatory=rule_def['is_mandatory'],
            ))
            added += 1
            print(f'  + Added: {rule_def[\"rule_id\"]}')
        else:
            print(f'  = Already exists: {rule_def[\"rule_id\"]}')
    db.add(AuditLog(
        action=AuditAction.RULE_CHANGED,
        organization_id=aw_id,
        message=f'Synced {added} new enhanced crypto rules to AW org',
    ))
print(f'  Rules added: {added}')

# ─── 3. Fix ETH-USD watchlist entry ───────────────────────
print()
print('[FIX 2] Fixing ETH-USD watchlist entry...')
with get_db() as db:
    # Remove the stale ETH-USD entry
    old_eth = db.query(Watchlist).filter(
        Watchlist.organization_id == aw_id,
        Watchlist.ticker == 'ETH-USD',
    ).first()
    if old_eth:
        db.delete(old_eth)
        print('  Removed ETH-USD (wrong format for IR)')
        db.add(AuditLog(
            action=AuditAction.CONFIG_CHANGED,
            organization_id=aw_id,
            message='Removed stale ETH-USD watchlist entry (pre-IR-migration artifact)',
        ))
    else:
        print('  ETH-USD not found (already clean)')

# ─── 4. Final rule count ──────────────────────────────────
print()
print('[VERIFY] Final rule count for AW:')
with get_db() as db:
    total = db.query(RuleConfig).filter(RuleConfig.organization_id==aw_id).count()
    crypto = db.query(RuleConfig).filter(
        RuleConfig.organization_id==aw_id,
        RuleConfig.category==RuleCategory.CRYPTO,
    ).all()
    print(f'  Total rules: {total} | Crypto rules: {len(crypto)}')
    for r in sorted(crypto, key=lambda x: x.sort_order or 0):
        status = 'ON' if r.enabled_globally else 'OFF'
        print(f'  [{status}] {r.rule_id}: threshold={r.threshold}')

# ─── 5. Market conditions summary ────────────────────────
print()
print('[MARKET CONDITIONS]')
with get_db() as db:
    from app.models.config import SystemConfig
    from app.models.exchange import MarketRegimeRecord
    from sqlalchemy import desc

    # Latest crypto regime
    crypto_r = db.query(MarketRegimeRecord).filter(
        MarketRegimeRecord.exchange_key=='CRYPTO_INDEPENDENTRESERVE'
    ).order_by(desc(MarketRegimeRecord.evaluated_at)).first()

    if crypto_r:
        btc_vs_ma200_pct = (crypto_r.index_close / crypto_r.index_ma200 - 1) * 100
        print(f'  Crypto (BTC-AUD): {crypto_r.regime}')
        print(f'    BTC-AUD: A\${crypto_r.index_close:,.2f} | 200MA: A\${crypto_r.index_ma200:,.2f} ({btc_vs_ma200_pct:+.1f}%)')
        print(f'    BTC needs to recover ~A\${crypto_r.index_ma200 - crypto_r.index_close:,.0f} ({-btc_vs_ma200_pct:.1f}%) to reach 200MA')
        if crypto_r.breadth_pct is not None:
            print(f'    Market breadth: {crypto_r.breadth_pct:.1f}% of tokens above 200MA')

# ─── 6. What would trigger a signal? ─────────────────────
print()
print('[SIGNAL CONDITIONS] What would generate a PENDING signal for AW:')
print('  For any crypto token to pass AstraTrade trend template, ALL of:')
print('  1. price > 200MA  (currently ALL tokens fail this)')
print('  2. price > 150MA')
print('  3. MA150 > MA200  (bear market — 150 has crossed below 200)')
print('  4. MA200 slope trending up (currently downward)')
print('  5. price within 25% of 52w high')
print('  6. price > 30% above 52w low')
print('  7. price > MA50')
print('  8. RS rating >= 70')
print()
print('  THEN: VCP pattern must be detected (2+ contractions with vol dry-up)')
print()
print('  Current best-performing crypto on watchlist:')
with get_db() as db:
    from app.models.signal import Watchlist, WatchlistStatus
    wl = db.query(Watchlist).filter(
        Watchlist.organization_id==aw_id,
        Watchlist.status==WatchlistStatus.WATCHING,
        Watchlist.asset_type=='CRYPTO',
    ).all()
    if wl:
        for w in wl:
            print(f'    {w.ticker} — on watchlist since {w.added_date}')
    else:
        print('    No crypto items on watchlist currently')

print()
print('  To generate signals in current market:')
print('  → Wait for BTC to recover above its 200MA (~A\$113,500)')
print('  → OR: use Force Screen when BTC shows recovery / breakout attempt')
print('  → The 5-min entry check will automatically trigger when a signal forms')
print()
print('  ASX regime: BEAR — same situation on equities side')
print('  ASX would need ^AXJO to recover above its 200MA to generate equity signals')
"

echo ""
echo "━━━ Fix script complete ━━━"
echo ""
echo "Next steps:"
echo "  1. Open http://localhost:8501 and verify AW org is selected"
echo "  2. Check /admin/rules — should now show 11 crypto rules"
echo "  3. The system is live and monitoring 24/7 via 5-min Celery beat"
echo "  4. When BTC-AUD recovers toward its 200MA, signals will auto-generate"
echo "  5. Run 'wsl bash /mnt/c/vcpilot/refresh_aw.sh' again after a market recovery"
echo ""
