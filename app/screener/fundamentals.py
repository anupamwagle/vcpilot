"""
AstraTrade Fundamental Criteria.

Rules:
  fundamental_eps_growth_recent   — EPS growth ≥ 25% in most recent quarter
  fundamental_eps_growth_accel    — EPS accelerating (each qtr > prior qtr)
  fundamental_eps_growth_annual   — Annual EPS growth ≥ 25% (3yr average)
  fundamental_sales_growth        — Revenue growth ≥ 25% in most recent quarter
  fundamental_roe                 — Return on Equity ≥ 17%
  fundamental_profit_margin       — Net profit margin > 0 and improving
  fundamental_earnings_accel      — Earnings acceleration: 2+ consecutive qtrs
  fundamental_institutional_own   — Some institutional ownership (not zero, not >80%)

Data availability policy (ASX/yfinance):
  yfinance free tier does not reliably provide quarterly EPS/revenue for most
  ASX stocks outside the top 50–100 by market cap. When data is genuinely
  unavailable (empty list / None), rules auto-pass rather than penalising the
  stock for a data gap. This matches Minervini's intent — the technical setup
  (Trend Template + VCP) is the primary signal; fundamentals are a secondary
  confirmation. Rules only FAIL when we have data and it is objectively bad.
"""
from __future__ import annotations
from typing import Optional
import pandas as pd
from loguru import logger
from app.screener.rules import RuleEngine, RuleResult


def evaluate_fundamentals(
    ticker: str,
    financials: dict,   # Dict from data.fetcher.get_fundamentals()
    engine: RuleEngine,
) -> dict[str, RuleResult]:
    """
    Evaluate fundamental rules.

    Args:
        ticker:     Stock ticker
        financials: Dict with keys:
                    eps_quarterly (list of floats, latest first),
                    revenue_quarterly (list of floats, latest first),
                    roe (float), net_margin (float),
                    net_margin_prev (float), inst_ownership_pct (float)
        engine:     RuleEngine instance

    Returns:
        Dict of rule_id → RuleResult
    """
    results: dict[str, RuleResult] = {}

    eps_q: list[float] = financials.get("eps_quarterly", [])
    rev_q: list[float] = financials.get("revenue_quarterly", [])
    roe: Optional[float] = financials.get("roe")
    net_margin: Optional[float] = financials.get("net_margin")
    net_margin_prev: Optional[float] = financials.get("net_margin_prev")
    inst_own: Optional[float] = financials.get("inst_ownership_pct")

    # -------------------------------------------------------------------------
    # EPS Growth (most recent quarter YoY)
    # -------------------------------------------------------------------------
    rule_id = "fundamental_eps_growth_recent"
    if engine.is_enabled(rule_id):
        threshold = float(engine.threshold(rule_id) or 25.0)
        # eps_q[0] = most recent, eps_q[4] = same quarter last year
        if len(eps_q) >= 5 and eps_q[4] and eps_q[4] > 0:
            growth = ((eps_q[0] - eps_q[4]) / abs(eps_q[4])) * 100
            passed = growth >= threshold
            results[rule_id] = RuleResult(rule_id, passed, round(growth, 2), threshold,
                f"EPS QoY growth {growth:.1f}% (min {threshold}%)")
        elif len(eps_q) >= 5 and eps_q[4] and eps_q[4] < 0 and eps_q[0] > 0:
            # Turned profitable
            results[rule_id] = RuleResult(rule_id, True, None, threshold, "Turned profitable (loss→profit)")
        else:
            # Data unavailable from yfinance — auto-pass, do not penalise
            results[rule_id] = RuleResult(rule_id, True, None, threshold, "EPS data unavailable — pass (no penalty)")

    # -------------------------------------------------------------------------
    # EPS Acceleration (each quarter improving vs prior)
    # -------------------------------------------------------------------------
    rule_id = "fundamental_eps_growth_accel"
    if engine.is_enabled(rule_id):
        if len(eps_q) >= 6:
            # Calculate YoY growth for last 2 quarters
            g1 = ((eps_q[0] - eps_q[4]) / abs(eps_q[4])) * 100 if eps_q[4] and eps_q[4] != 0 else 0
            g2 = ((eps_q[1] - eps_q[5]) / abs(eps_q[5])) * 100 if eps_q[5] and eps_q[5] != 0 else 0
            passed = g1 > g2
            results[rule_id] = RuleResult(rule_id, passed, round(g1, 2), round(g2, 2),
                f"EPS accel: Q1 {g1:.1f}% vs Q2 {g2:.1f}%")
        else:
            # Data unavailable — auto-pass
            results[rule_id] = RuleResult(rule_id, True, None, None, "EPS accel data unavailable — pass (no penalty)")

    # -------------------------------------------------------------------------
    # Annual EPS Growth (use last 4 quarters vs prior 4 quarters)
    # -------------------------------------------------------------------------
    rule_id = "fundamental_eps_growth_annual"
    if engine.is_enabled(rule_id):
        threshold = float(engine.threshold(rule_id) or 25.0)
        if len(eps_q) >= 8:
            ttm = sum(eps_q[:4])
            prior_ttm = sum(eps_q[4:8])
            if prior_ttm and prior_ttm > 0:
                annual_growth = ((ttm - prior_ttm) / prior_ttm) * 100
                passed = annual_growth >= threshold
                results[rule_id] = RuleResult(rule_id, passed, round(annual_growth, 2), threshold,
                    f"Annual EPS growth {annual_growth:.1f}% (min {threshold}%)")
            elif prior_ttm and prior_ttm < 0 and ttm > 0:
                results[rule_id] = RuleResult(rule_id, True, None, threshold, "Turned profitable annually (loss→profit)")
            else:
                results[rule_id] = RuleResult(rule_id, False, None, threshold, "Prior year EPS zero/negative")
        else:
            # Data unavailable — auto-pass
            results[rule_id] = RuleResult(rule_id, True, None, threshold, "Annual EPS data unavailable — pass (no penalty)")

    # -------------------------------------------------------------------------
    # Revenue / Sales Growth (most recent quarter YoY)
    # -------------------------------------------------------------------------
    rule_id = "fundamental_sales_growth"
    if engine.is_enabled(rule_id):
        threshold = float(engine.threshold(rule_id) or 25.0)
        if len(rev_q) >= 5 and rev_q[4] and rev_q[4] > 0:
            growth = ((rev_q[0] - rev_q[4]) / rev_q[4]) * 100
            passed = growth >= threshold
            results[rule_id] = RuleResult(rule_id, passed, round(growth, 2), threshold,
                f"Revenue growth {growth:.1f}% (min {threshold}%)")
        else:
            # Data unavailable — auto-pass
            results[rule_id] = RuleResult(rule_id, True, None, threshold, "Revenue data unavailable — pass (no penalty)")

    # -------------------------------------------------------------------------
    # Return on Equity
    # -------------------------------------------------------------------------
    rule_id = "fundamental_roe"
    if engine.is_enabled(rule_id):
        threshold = float(engine.threshold(rule_id) or 17.0)
        if roe is not None:
            roe_pct = roe * 100 if abs(roe) <= 1 else roe  # Handle decimal vs percentage
            passed = roe_pct >= threshold
            results[rule_id] = RuleResult(rule_id, passed, round(roe_pct, 2), threshold,
                f"ROE {roe_pct:.1f}% (min {threshold}%)")
        else:
            # Data unavailable — auto-pass
            results[rule_id] = RuleResult(rule_id, True, None, threshold, "ROE data unavailable — pass (no penalty)")

    # -------------------------------------------------------------------------
    # Net Profit Margin (positive + improving)
    # -------------------------------------------------------------------------
    rule_id = "fundamental_profit_margin"
    if engine.is_enabled(rule_id):
        if net_margin is not None:
            margin_pct = net_margin * 100 if abs(net_margin) <= 1 else net_margin
            improving = (net_margin > net_margin_prev) if net_margin_prev is not None else True
            passed = margin_pct > 0 and improving
            results[rule_id] = RuleResult(rule_id, passed, round(margin_pct, 2), 0,
                f"Net margin {margin_pct:.1f}% {'▲' if improving else '▼'}")
        else:
            # Data unavailable — auto-pass
            results[rule_id] = RuleResult(rule_id, True, None, None, "Margin data unavailable — pass (no penalty)")

    # -------------------------------------------------------------------------
    # Institutional Ownership (some, but not over-owned)
    # -------------------------------------------------------------------------
    rule_id = "fundamental_institutional_own"
    if engine.is_enabled(rule_id):
        min_own = float(engine.threshold(rule_id) or 5.0)   # Min 5%
        max_own = 80.0                                        # Max 80%
        if inst_own is not None:
            own_pct = inst_own * 100 if inst_own <= 1 else inst_own
            passed = min_own <= own_pct <= max_own
            results[rule_id] = RuleResult(rule_id, passed, round(own_pct, 2), min_own,
                f"Inst ownership {own_pct:.1f}% (range {min_own}%–{max_own}%)")
        else:
            # Pass if data not available (don't penalise small ASX stocks)
            results[rule_id] = RuleResult(rule_id, True, None, min_own, "Ownership data unavailable — pass")

    return results
