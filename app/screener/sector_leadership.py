"""Sector relative-strength ranking used by the equity entry gate.

The ranking is deliberately calculated from the locally persisted EOD universe.
It never makes a per-ticker network call while screening and therefore produces a
repeatable, auditable decision for ASX and US equities.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict

from app.models.market import PriceBar, Stock
from app.screener.rules import RuleEngine, RuleResult


SECTOR_LOOKBACK_BARS = 63  # approximately one calendar quarter
MIN_SECTOR_MEMBERS = 3
MIN_SECTORS_TO_RANK = 3


def load_sector_returns(db, exchange_key: str) -> dict[str, list[float]]:
    """Return trailing-quarter stock returns grouped by sector for one market.

    A sector is included only when it has enough constituents with sufficient
    stored history.  This prevents a single volatile micro-cap from being called
    a "leading sector".
    """
    rows = db.query(Stock.ticker, Stock.sector).filter(
        Stock.is_active == True,
        Stock.blacklisted == False,
        Stock.asset_type == "EQUITY",
        Stock.exchange_key == exchange_key,
        Stock.sector.isnot(None),
        Stock.sector != "",
    ).all()
    returns: dict[str, list[float]] = defaultdict(list)
    for ticker, sector in rows:
        bars = db.query(PriceBar.close).filter(
            PriceBar.ticker == ticker,
            PriceBar.close.isnot(None),
        ).order_by(PriceBar.date.desc()).limit(SECTOR_LOOKBACK_BARS + 1).all()
        if len(bars) < SECTOR_LOOKBACK_BARS + 1:
            continue
        latest, baseline = float(bars[0][0]), float(bars[-1][0])
        if latest > 0 and baseline > 0:
            returns[str(sector).strip()].append((latest / baseline - 1) * 100)
    return {
        sector: values for sector, values in returns.items()
        if len(values) >= MIN_SECTOR_MEMBERS
    }


def evaluate_sector_leadership(
    sector: str | None,
    sector_returns: dict[str, list[float]],
    engine: RuleEngine,
) -> RuleResult | None:
    """Evaluate ``entry_sector_leadership`` or return ``None`` when disabled."""
    rule_id = "entry_sector_leadership"
    if not engine.is_enabled(rule_id):
        return None

    sector = (sector or "").strip()
    if not sector or sector not in sector_returns:
        return RuleResult(rule_id, False, None, engine.threshold(rule_id),
                          "Sector history unavailable; entry held on watchlist")

    ranked = sorted(
        ((name, statistics.median(values)) for name, values in sector_returns.items()),
        key=lambda item: item[1], reverse=True,
    )
    if len(ranked) < MIN_SECTORS_TO_RANK:
        return RuleResult(rule_id, False, None, engine.threshold(rule_id),
                          "Insufficient sector breadth to rank leadership")

    top_pct = max(1.0, min(100.0, float(engine.threshold(rule_id) or 20.0)))
    qualifying_count = max(1, math.ceil(len(ranked) * top_pct / 100.0))
    rank = next(index for index, (name, _ret) in enumerate(ranked, 1) if name == sector)
    sector_return = next(ret for name, ret in ranked if name == sector)
    passed = rank <= qualifying_count
    return RuleResult(
        rule_id, passed,
        {"sector": sector, "rank": rank, "sector_count": len(ranked), "return_pct": round(sector_return, 2)},
        top_pct,
        f"{sector} rank {rank}/{len(ranked)} by 63-bar median return {sector_return:.1f}% "
        f"({'leading' if passed else 'outside top sector cohort'})",
    )
